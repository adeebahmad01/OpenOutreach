# openoutreach/core/scheduler.py
"""Task-slot creation — the only module that inserts ``Task`` rows.

The queue holds two kinds of work (see ``crm/models/deal.py`` and
``core/models.py``):

- **Drains** (``find_email`` submit, ``email`` opener, ``follow_up``) — lazy
  capacity tokens minted when there's eligible work under the day's send cap.
  ``flush_*_queue`` emits them; each handler picks its target at run time. There
  is no pre-materialized schedule — the daemon tops them up on startup and
  whenever the queue has no due task (``reconcile``).

- **Bound polls** (``collect_email``) — one persisted row per in-flight paid
  lookup, carrying the provider ``request_id`` + poll backoff in its *payload*.
  ``schedule_collect_email`` mints them: the submit handler creates the first,
  and the collect handler chains the next on a still-running poll. They bypass
  the drains' single-slot guard by construction (one live poll per lookup).

Paid spend rides on send capacity: ``find_email`` only fires when there's
mailbox headroom to send the result *today* — the GP confidence gate
(``ready_pool``) rations *which* leads qualify; the send cap bounds *how many*
lookups ride the pipeline. There is no separate spend cap.

That headroom is not first-come-first-served across campaigns: ``reconcile``
splits it by ``core/quota.py`` first, so the freemium promo's
``action_fraction`` binds instead of being decided by whichever pipeline
produces candidates faster. Each drain receives its campaign's *allowance* and
caps itself by that as well as by the pool. Follow-ups are outside the quota
(see ``core/quota.py``) and are reserved off the top — but only down to
``OPENER_FLOOR_FRACTION`` of the day, and only while openers are actually ready
to use what is held back. Without that floor the reservation is unbounded and
first contact stops: open threads accumulate faster than they close, so the owed
count outgrows the box, and because the same allowance gates ``find_email`` the
starvation compounds into the next day.
"""
from __future__ import annotations

import logging
import math
import random
from datetime import timedelta

from django.db.models import Count, Min
from django.utils import timezone

from openoutreach.crm.models import DealState
from openoutreach.core import quota
from openoutreach.core.conf import (
    MIN_SEND_INTERVAL_SECONDS,
    OPENER_FLOOR_FRACTION,
    SEND_INTERVAL_JITTER_SECONDS,
)
from openoutreach.core.models import Task

logger = logging.getLogger(__name__)


# ── Send pacing ───────────────────────────────────────────────────────


def _last_send_anchor():
    """The clock the next send must be spaced from, or None if nothing is queued
    or sent yet.

    Two things can put a send in the recent past or near future, and the anchor
    is the later of them: a message already *sent* (the newest outgoing
    ChatMessage) and a send already *scheduled* (the furthest-out pending email
    or follow-up slot). Reading only the first would let two reconcile passes
    each stack a burst onto an empty-looking queue; reading only the second
    would let a restart forget everything sent before it.
    """
    from openoutreach.chat.models import ChatMessage

    sent = ChatMessage.objects.filter(is_outgoing=True).order_by("-creation_date").first()
    scheduled = (
        Task.objects
        .filter(
            task_type__in=(Task.TaskType.EMAIL, Task.TaskType.FOLLOW_UP),
            status=Task.Status.PENDING,
        )
        .order_by("-scheduled_at")
        .first()
    )
    stamps = [s for s in (
        sent.creation_date if sent else None,
        scheduled.scheduled_at if scheduled else None,
    ) if s is not None]
    return max(stamps) if stamps else None


def _paced_slots(count: int) -> list:
    """``count`` send times, each at least ``MIN_SEND_INTERVAL_SECONDS`` after the
    one before it and after whatever was last sent or scheduled.

    Pool-wide, not per-campaign or per-mailbox: the receiver rate-limits the
    sending infrastructure, so two campaigns draining at once must share one
    rhythm rather than each keeping its own. Each gap draws fresh jitter — the
    spacing has to vary, or a fixed 180s cadence is its own machine signature.

    The first slot is ``now`` when nothing has been sent recently, so an idle
    daemon still sends immediately on startup; the floor only bites once there
    is a previous send to be spaced from.
    """
    now = timezone.now()
    cursor = _last_send_anchor()
    slots = []
    for _ in range(count):
        if cursor is None:
            cursor = now  # nothing to space from — send the first one immediately
        else:
            cursor = max(now, cursor + timedelta(
                seconds=MIN_SEND_INTERVAL_SECONDS
                + random.uniform(0, SEND_INTERVAL_JITTER_SECONDS),
            ))
        slots.append(cursor)
    return slots


# ── Slot creation primitives ──────────────────────────────────────────


def _has_pending(task_type: "Task.TaskType", campaign_id: int) -> bool:
    return Task.objects.filter(
        task_type=task_type,
        status=Task.Status.PENDING,
        payload__campaign_id=campaign_id,
    ).exists()


def _create_lazy_slots(task_type: "Task.TaskType", campaign_id: int, times: list) -> int:
    """Bulk-create lazy drain slots (``payload`` = campaign_id only)."""
    if not times:
        return 0
    Task.objects.bulk_create([
        Task(
            task_type=task_type,
            scheduled_at=t,
            payload={"campaign_id": campaign_id},
        )
        for t in times
    ])
    return len(times)


# ── Drains (lazy, send-cap gated) ─────────────────────────────────────


def flush_find_email_queue(session, campaign, allowance: int) -> int:
    """Mint a ``find_email`` (submit) slot when there's send-headroom for its
    result today. Returns the number of slots created (0 or 1).

    The paid lookup rides on send capacity, not a spend cap: we never resolve an
    email we couldn't send today. The GP confidence gate (``ready_pool``) rations
    *which* leads reach READY_TO_FIND_EMAIL; this gate bounds *how many* lookups
    ride the send pipeline — capped at ``remaining_today()`` minus everything
    already heading for a send (READY_TO_EMAIL + FINDING_EMAIL), and by this
    campaign's opener *allowance* (``core/quota.py``). A free miss drops out of the
    pipeline and re-opens the gate at no send-budget cost.

    The allowance belongs here as well as on the opener drain for the same reason
    the send-headroom check does: a campaign that may not send today must not buy
    an address today either, or it accumulates paid inventory it has no right to
    mail — the fraction would hold on sends while the credits ran unproportioned.

    One slot per call, not a batch: the handler is the pipeline *pump*
    (discover→qualify→rank→submit), so fanning out slots would trigger parallel
    discovery. The drain refills each ``reconcile`` while headroom lasts.

    No-op when a ``find_email`` task is already pending, the finder/mailbox is
    unconfigured, or the pipeline already fills today's send headroom.
    """
    from openoutreach.emails import bettercontact
    from openoutreach.emails.models import Mailbox, has_mailbox
    from openoutreach.crm.models import Deal

    if _has_pending(Task.TaskType.FIND_EMAIL, campaign.pk):
        return 0
    if not has_mailbox() or not bettercontact.is_configured():
        return 0

    remaining = min(Mailbox.objects.remaining_today(), allowance)
    in_pipeline = Deal.objects.filter(
        campaign_id=campaign.pk,
        state__in=(DealState.READY_TO_EMAIL, DealState.FINDING_EMAIL),
        lead__disqualified=False,
    ).count()
    if in_pipeline >= remaining:
        return 0

    _create_lazy_slots(Task.TaskType.FIND_EMAIL, campaign.pk, [timezone.now()])
    logger.info(
        "[%s] flushed 1 find_email slot (send_headroom=%d, in_pipeline=%d)",
        campaign, remaining, in_pipeline,
    )
    return 1


def flush_email_queue(session, campaign, allowance: int) -> int:
    """Mint **one** paced opener slot for *campaign*. Returns 0 or 1.

    One slot per call, matching ``flush_find_email_queue``: under send pacing a
    batch would schedule the whole day up front, which puts the last opener hours
    out and drags the shared pacing anchor with it — a follow-up minted behind
    that batch would then wait behind every opener in it, silently overriding the
    claim priority that puts follow-ups first. Minting one at a time keeps the
    queue no more than one interval deep, so priority is decided by
    ``Task.pending()`` again rather than by whoever was minted first.

    It also keeps the queue honest: a 40-slot batch bakes in this morning's
    capacity, while one slot per call re-reads the daily cap, the receiver's
    verdicts and the quota split every time.

    Two throttles besides the pacing: the pool-wide per-box daily cap
    (``Mailbox.objects.remaining_today()``, re-checked at send time) and this
    campaign's *allowance* — its share of today's openers under ``core/quota.py``.
    This is the drain the freemium fraction governs, because an opener is one new
    person contacted. Which campaign is offered the slot is ``quota.by_hunger``'s
    call, in ``reconcile``.

    No-op when a PENDING email task already exists for this campaign, no box has
    headroom, the campaign is out of allowance, or the pool is empty.
    """
    from openoutreach.crm.models import Deal
    from openoutreach.emails.models import Mailbox

    if _has_pending(Task.TaskType.EMAIL, campaign.pk):
        return 0
    if min(Mailbox.objects.remaining_today(), allowance) <= 0:
        return 0

    queued = Deal.objects.filter(
        campaign_id=campaign.pk,
        state=DealState.READY_TO_EMAIL,
        lead__disqualified=False,
    ).exists()
    if not queued:
        return 0

    slot = _paced_slots(1)[0]
    _create_lazy_slots(Task.TaskType.EMAIL, campaign.pk, [slot])
    logger.info("[%s] flushed 1 email slot for %s (allowance=%d)",
                campaign, timezone.localtime(slot).strftime("%H:%M"), allowance)
    return 1


def flush_follow_up_queue(campaign, reserve: int = 0) -> int:
    """Mint **one** paced follow-up slot for *campaign*. Returns 0 or 1.

    Single-slot for the same reason as the opener drain, and minted *before* it in
    ``reconcile`` so a reply owed to a human takes the earlier slot in the shared
    rhythm — the time-domain counterpart of ``Task.pending()`` claiming
    ``follow_up`` first. Follow-ups ride outside the quota (a reply owed to a human
    is not new reach), so there is no allowance here, only the pool-wide per-box
    headroom (re-checked at send time).

    *reserve* is the one thing they must yield to: sends held back for openers
    (``_opener_reserve``). Priority is not the same claim as unbounded priority —
    a campaign accumulates open threads faster than it closes them, so an
    unreserved follow-up drain eventually owns every send in the box and first
    contact stops entirely. The reserve is only ever non-zero when openers are
    actually ready to use it, so this never idles a box to protect work that
    doesn't exist.

    Reading the thread happens inside the handler at that slot, so the pacing
    delay is also fresher thread state.

    No-op when a PENDING follow-up task already exists for this campaign, headroom
    is down to the opener reserve, or nothing is due.
    """
    from openoutreach.crm.models import Deal
    from openoutreach.emails.models import Mailbox

    if _has_pending(Task.TaskType.FOLLOW_UP, campaign.pk):
        return 0
    if Mailbox.objects.remaining_today() <= reserve:
        return 0

    due = Deal.objects.filter(
        campaign_id=campaign.pk,
        state=DealState.EMAILED,
        outcome="",
        lead__disqualified=False,
        next_follow_up_at__lte=timezone.now(),
    ).exists()
    if not due:
        return 0

    slot = _paced_slots(1)[0]
    _create_lazy_slots(Task.TaskType.FOLLOW_UP, campaign.pk, [slot])
    logger.info("[%s] flushed 1 follow_up slot for %s",
                campaign, timezone.localtime(slot).strftime("%H:%M"))
    return 1


# ── Bound poll (collect leg) ──────────────────────────────────────────


def schedule_collect_email(payload: dict, delay_seconds: float) -> None:
    """Mint the next poll of an in-flight lookup — the collect leg's bound row.

    ``payload`` carries ``campaign_id``, ``deal_id``, ``provider``, ``request_id``,
    ``submitted_at`` (ISO, for the give-up deadline), and ``attempt``. Called by
    the submit handler (first poll, ``attempt=0``) and by the collect handler
    itself on a still-running poll (chained, ``attempt+1``, doubled backoff).

    The request_id + timing live on this persisted row, never on the deal, so an
    in-flight lookup rides entirely on the task and survives a daemon restart.
    One live collect row per lookup, chained poll→poll, so it bypasses the drains'
    single-slot guard by construction.
    """
    Task.objects.create(
        task_type=Task.TaskType.COLLECT_EMAIL,
        scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
        payload=payload,
    )


# ── Reconciliation ────────────────────────────────────────────────────


def _refresh_capacities() -> None:
    """Re-measure every mailbox's warm capacity, once a day.

    Runs here rather than at send time because it costs an IMAP round trip:
    reconcile is the once-per-idle-cycle hook, and capacity moves on the scale of
    days, not sends. Best-effort per box — one unreachable mailbox keeps its last
    measurement and must not stop the others being measured.
    """
    from openoutreach.emails.models import Mailbox
    from openoutreach.emails.warmth import mark_measured, measurement_due, refresh_capacity

    if not measurement_due():
        return
    for box in Mailbox.objects.all():
        refresh_capacity(box)
    mark_measured()


def _recover_stale_running_tasks() -> int:
    """Reset RUNNING tasks to PENDING. RUNNING rows can only linger if the
    daemon crashed mid-task, so they are always stale at reconcile time."""
    count = Task.objects.filter(status=Task.Status.RUNNING).update(
        status=Task.Status.PENDING,
    )
    if count:
        logger.info("Recovered %d stale running tasks", count)
    return count


def _due_follow_ups(campaigns) -> int:
    """Follow-ups owed right now across *campaigns* — reserved off today's headroom
    before the openers are split, since they outrank openers in ``Task.pending()``
    and sit outside the quota."""
    from openoutreach.crm.models import Deal

    return Deal.objects.filter(
        campaign__in=campaigns,
        state=DealState.EMAILED,
        outcome="",
        lead__disqualified=False,
        next_follow_up_at__lte=timezone.now(),
    ).count()


def opener_allowances(campaigns) -> dict[int, int]:
    """Today's remaining opener budget, split across *campaigns* by quota weight.

    The budget is the pool's send headroom minus the follow-ups already owed — the
    quota governs first contacts only, and follow-ups outrank them on claim, so
    they are reserved rather than competed with. Every caller that mints opener
    slots reads its allowance from here, so the split is computed one way.

    That reservation is **floored**, not unbounded: follow-ups may take at most
    ``1 - OPENER_FLOOR_FRACTION`` of the day. Reserving every owed follow-up was
    right in the small and ruinous in the large — open threads accumulate faster
    than they close, so the owed count outgrows the box and drives the opener
    budget to zero. That is not just a delayed opener: the same allowance gates
    ``flush_find_email_queue``, so a zero budget stops address buying too, and
    tomorrow has nothing ready to send either. Measured on a live install: 102
    follow-ups and 1 opener in a week.
    """
    from openoutreach.emails.models import Mailbox

    headroom = Mailbox.objects.remaining_today()
    reserved = min(_due_follow_ups(campaigns), headroom - _opener_floor(headroom))
    budget = max(0, headroom - reserved)
    return quota.allocate(campaigns, budget, quota.opener_counts(campaigns))


def _opener_floor(headroom: int) -> int:
    """Sends of *headroom* that follow-ups may never reserve away from openers.

    Rounded up, so a box small enough that the fraction lands under one send still
    keeps one for first contact rather than none.
    """
    return math.ceil(headroom * OPENER_FLOOR_FRACTION)


def _opener_reserve(campaigns, allowances: dict[int, int]) -> int:
    """Sends to actually hold back from the follow-up drain right now.

    The floor is a *ceiling on the reservation*, not a promise to idle: holding
    back capacity for openers that do not exist would leave a box half-used while
    replies wait. So the reserve is the floor capped by the openers a campaign
    could genuinely send this moment — a READY_TO_EMAIL deal, within that
    campaign's allowance.
    """
    from openoutreach.crm.models import Deal
    from openoutreach.emails.models import Mailbox

    ready = dict(
        Deal.objects.filter(
            campaign__in=campaigns,
            state=DealState.READY_TO_EMAIL,
            lead__disqualified=False,
        )
        .values_list("campaign_id")
        .annotate(n=Count("pk"))
    )
    sendable = sum(min(allowances[c.pk], ready.get(c.pk, 0)) for c in campaigns)
    return min(_opener_floor(Mailbox.objects.remaining_today()), sendable)


def _next_follow_up_slot(campaigns, reserve: int = 0) -> int:
    """Mint one follow-up slot for the campaign owed the oldest reply. Returns 0 or 1.

    Ordered by oldest due rather than by campaign order so a busy campaign cannot
    starve a quiet one's replies — with a single slot per pass, whoever iterates
    first would otherwise win every time. *reserve* is the opener floor, passed
    through to the drain.
    """
    from openoutreach.crm.models import Deal

    oldest = dict(
        Deal.objects.filter(
            campaign__in=campaigns,
            state=DealState.EMAILED,
            outcome="",
            lead__disqualified=False,
            next_follow_up_at__lte=timezone.now(),
        )
        .values_list("campaign_id")
        .annotate(due=Min("next_follow_up_at"))
    )
    for campaign in sorted(
        (c for c in campaigns if c.pk in oldest), key=lambda c: oldest[c.pk],
    ):
        if flush_follow_up_queue(campaign, reserve):
            return 1
    return 0


def _next_opener_slot(session, campaigns, allowances: dict[int, int]) -> int:
    """Mint one opener slot for the campaign furthest below its share. Returns 0 or 1.

    Falls through to the next-hungriest when the first has nothing ready, so the
    slot is never wasted on a campaign that cannot use it.
    """
    for campaign in quota.by_hunger(campaigns, quota.opener_counts(campaigns)):
        if flush_email_queue(session, campaign, allowances[campaign.pk]):
            return 1
    return 0


def reconcile(session) -> None:
    """Recover stale RUNNING tasks, re-measure warm capacity, then top up the drains:
    one ``find_email`` submit slot per campaign (if there's send headroom and
    allowance), **one** due follow-up, and **one** ready opener. Bound
    ``collect_email`` polls are self-chaining and are not reconciled here. Runs on
    daemon startup and whenever no task is due.

    The two send drains mint a single paced slot each rather than draining their
    pools, so the queue is never more than one send interval deep. That is what
    keeps ``Task.pending()``'s claim priority meaningful: a batch would schedule
    the whole day up front and anything minted afterwards — a follow-up especially
    — would sit behind all of it regardless of rank. The follow-up drain runs
    first for the same reason, so a reply owed to a human takes the earlier slot.

    Today's opener budget is still split across campaigns by ``core/quota.py``
    (``opener_allowances`` bounds what each may buy and send today);
    ``quota.by_hunger`` then decides who is owed the *next* slot, so
    ``Campaign.action_fraction`` holds per-send rather than per-day. The follow-up
    drain runs first but not unconditionally: ``_opener_reserve`` holds back up to
    ``OPENER_FLOOR_FRACTION`` of the box for openers that are ready to send, so
    priority-on-claim can't harden into owning the whole day."""
    _recover_stale_running_tasks()
    _refresh_capacities()
    campaigns = session.campaigns

    allowances = opener_allowances(campaigns)
    for campaign in campaigns:
        flush_find_email_queue(session, campaign, allowances[campaign.pk])
    _next_follow_up_slot(campaigns, _opener_reserve(campaigns, allowances))
    _next_opener_slot(session, campaigns, allowances)

    quota.log_shares(campaigns)
    pending_count = Task.objects.pending().count()
    logger.info("Task queue reconciled: %d pending tasks", pending_count)
