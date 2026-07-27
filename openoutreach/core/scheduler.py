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
(see ``core/quota.py``) and are reserved off the top.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from openoutreach.crm.models import DealState
from openoutreach.core import quota
from openoutreach.core.models import Task

logger = logging.getLogger(__name__)


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
    """Drain the READY_TO_EMAIL pool for *campaign* into immediate task slots.

    The eager counterpart to the window drains: email has no anti-bot rhythm to
    fake, so every queued deal is emitted as an immediate slot (scheduled ``now``,
    no spacing) and drains back-to-back. Two throttles: the pool-wide per-box daily
    cap (``Mailbox.objects.remaining_today()``, re-checked at send time) and this
    campaign's *allowance* — its share of today's openers under
    ``core/quota.py``. This is the drain the freemium fraction governs, because an
    opener is one new person contacted.

    No-op when a PENDING email task already exists, no box has headroom, the
    campaign is out of allowance, or the pool is empty. Count is scoped to
    ``campaign.pk`` directly because ``reconcile`` does not set ``session.campaign``
    before invoking it.
    """
    from openoutreach.crm.models import Deal
    from openoutreach.emails.models import Mailbox

    if _has_pending(Task.TaskType.EMAIL, campaign.pk):
        return 0

    remaining = min(Mailbox.objects.remaining_today(), allowance)
    if remaining <= 0:
        return 0

    queued = Deal.objects.filter(
        campaign_id=campaign.pk,
        state=DealState.READY_TO_EMAIL,
        lead__disqualified=False,
    ).count()
    n = min(queued, remaining)
    if n <= 0:
        return 0

    now = timezone.now()
    created = _create_lazy_slots(Task.TaskType.EMAIL, campaign.pk, [now] * n)
    logger.info(
        "[%s] flushed %d email slots to send now (queued=%d, budget=%d, allowance=%d)",
        campaign, created, queued, remaining, allowance,
    )
    return created


def flush_follow_up_queue(session, campaign) -> int:
    """Drain due EMAILED deals for *campaign* into immediate follow-up slots.

    The follow-up counterpart to ``flush_email_queue``: a follow-up has no anti-bot
    rhythm to fake, so every EMAILED deal whose countdown (``next_follow_up_at``)
    is due is emitted as an immediate slot, capped by the pool-wide per-box daily
    headroom (re-checked at send time). Reading the thread happens inside the
    handler at that slot — exactly when the countdown fires.

    No-op when a PENDING follow-up task already exists, no box has headroom, or
    nothing is due.
    """
    from openoutreach.crm.models import Deal
    from openoutreach.emails.models import Mailbox

    if _has_pending(Task.TaskType.FOLLOW_UP, campaign.pk):
        return 0

    remaining = Mailbox.objects.remaining_today()
    if remaining <= 0:
        return 0

    now = timezone.now()
    due = Deal.objects.filter(
        campaign_id=campaign.pk,
        state=DealState.EMAILED,
        outcome="",
        lead__disqualified=False,
        next_follow_up_at__lte=now,
    ).count()
    n = min(due, remaining)
    if n <= 0:
        return 0

    created = _create_lazy_slots(Task.TaskType.FOLLOW_UP, campaign.pk, [now] * n)
    logger.info(
        "[%s] flushed %d follow_up slots to run now (due=%d, cap_remaining=%d)",
        campaign, created, due, remaining,
    )
    return created


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
    """
    from openoutreach.emails.models import Mailbox

    budget = max(0, Mailbox.objects.remaining_today() - _due_follow_ups(campaigns))
    return quota.allocate(campaigns, budget, quota.opener_counts(campaigns))


def reconcile(session) -> None:
    """Recover stale RUNNING tasks, then top up the drains for every campaign:
    one ``find_email`` submit slot (if there's send headroom and allowance), every
    ready opener within allowance, and every due follow-up. Bound ``collect_email``
    polls are self-chaining and are not reconciled here. Runs on daemon startup and
    whenever no task is due.

    Today's opener budget is split across campaigns *before* the drains run
    (``core/quota.py``), so ``Campaign.action_fraction`` decides the freemium
    promo's share instead of the race between pipelines deciding it."""
    _recover_stale_running_tasks()
    campaigns = session.campaigns

    allowances = opener_allowances(campaigns)
    for campaign in campaigns:
        flush_find_email_queue(session, campaign, allowances[campaign.pk])
        flush_email_queue(session, campaign, allowances[campaign.pk])
        flush_follow_up_queue(session, campaign)

    quota.log_shares(campaigns)
    pending_count = Task.objects.pending().count()
    logger.info("Task queue reconciled: %d pending tasks", pending_count)
