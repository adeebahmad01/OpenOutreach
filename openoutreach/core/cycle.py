# openoutreach/core/cycle.py
"""The cycle — wake up, do the single most valuable thing available, sleep.

**A queue is a status, not a table.** Work is found by asking the deals what they
need (``Deal.objects.filter(state=...)``), so a deal is available because of its own
row. Nothing is created in advance, which means nothing can drift, be lost, or need
reconciling — and no row's timestamp can gate anything but itself.

What this replaces was a *token loop*. ``core/scheduler.py`` wrote ``Task`` rows that
were permission tokens stamped with a time — "at 14:32 someone may send one email for
campaign A" — without saying to whom; the loop took the earliest-due token, and the
handler then went and found its own target. When no token was due it slept **until
the earliest token's timestamp**. On 2026-08-05 that put a live install to sleep for
34 hours: two BetterContact polls had never terminated, their uncapped backoff had
reached 45h30m, they were the only rows in the table, and 55 ready deals with 70
sends of headroom simply had no token and therefore were not work. The shape was
inherited from the Playwright era, when a token queue was how access to one browser
got serialised. The browser went; the queue outlived it.

Here the sleep is a fixed short interval and capacity is re-read every time.

## The hierarchy

One ordered list. The cycle walks it top to bottom and stops at the first thing it
can do, so priority is just the order these are written in:

    1  FINDING_EMAIL          check the lookup            (not_before elapsed)
    2  QUALIFIED              score with the campaign's model
    3  READY_TO_FIND_EMAIL    buy the address             (a provider is configured)
    4  (the campaign itself)  top up the pipeline         (always)

A state that is not listed is terminal, and terminal costs nothing: RESOLVED,
NO_EMAIL_BETTERCONTACT and FAILED. Most deals come to rest at one of those three,
and resting is free because nothing iterates them.

**There is no spend gate on rows 2 and 4, and that is the shape of the finder.**
Both rows used to share one — *never resolve an address, and never spend an LLM call
qualifying, for someone there is no room to email today* — which was right while
every lead ended in a send. Nothing rations discovery or qualification now: they cost
the operator's own LLM key and nothing else, and the loop is bounded the only way
that matters, at one unit of work per cycle. Row 3 is the single paid step and asks
only whether there is a provider to pay.

**Two rows are gone with the sending leg** — answering a reply and sending a first
email — along with the mail pass and the daily warmth re-measure that fed them. They
live in OpenEmailSequence now. Leads leave this process over a CSV
(``core/export.py``) and nothing comes back up that wire.

Campaigns take turns (``_rotate``). There is no share, no weight and no allocation:
with nothing minted in advance there is no budget to split, so the fairness question
collapses to whose turn it is.
"""
from __future__ import annotations

import logging
import time

from django.utils import timezone
from pydantic_ai.exceptions import ModelHTTPError
from termcolor import colored

from openoutreach.crm.models import DealState

logger = logging.getLogger(__name__)

# How long to wait after each action. Short, fixed, and derived from nothing: the
# cycle's whole point is that no data decides when it next wakes up. It bounds how
# precisely a ``not_before`` can be honoured, so keep it well under the send spacing.
CYCLE_SECONDS = 5

# How often an idle cycle says so. Idle is the *normal* state here — sends are paced
# minutes apart and a cycle is seconds — so a line per cycle would bury the lines that
# mean something. One a minute is a pulse: enough to tell a working daemon from a
# wedged one, and it carries the counts that say why there was nothing to do.
IDLE_LOG_INTERVAL_S = 60

# Errors that must stop the daemon rather than be retried. A misconfigured LLM key
# is not a transient fault: every cycle would raise it, log it and try again in five
# seconds, forever, while the operator sees a daemon that looks alive and does
# nothing. Everything else is logged and skipped — the row is left untouched and the
# next cycle moves on.
HALTING_ERRORS = (ModelHTTPError,)

# When the last "nothing to do" line went out, so the pulse is one a minute rather
# than one a cycle. Reset by any action, so the first idle after work always prints.
_idle_logged_at: float | None = None

# Per-campaign `(qualified, past-the-gate)` counts as of the last scoring pass, so a
# pool that has not moved is not re-scored. Process-local by design: there is one
# process, and a restart simply scores once more than it had to.
_scored_at: dict[int, tuple[int, int]] = {}

# ── The loop ──────────────────────────────────────────────────────


def run_daemon() -> None:
    """Run the cycle until the process is stopped or a halting error is raised."""
    from openoutreach.core.operator import campaigns

    _import_freemium_campaign()

    known = campaigns()
    if not known:
        logger.error("No campaigns found — cannot start daemon")
        return

    logger.info("%s — %d campaign(s), one action per cycle",
                colored("Daemon started", "green", attrs=["bold"]), len(known))

    rotation = _rotate()
    while True:
        try:
            run_one_action(next(rotation))
        except HALTING_ERRORS as exc:
            logger.error(
                colored("Daemon stopped", "red", attrs=["bold"]) + " — %s\n"
                "Check ai_model (provider:model), llm_api_key and llm_api_base "
                "in Admin → Site Configuration.", exc,
            )
            return
        except Exception:
            logger.exception("Cycle failed — skipping to the next one")
        time.sleep(CYCLE_SECONDS)


def _rotate():
    """Endless round-robin over the operator's campaigns, re-read each lap.

    Re-reading matters on a fresh install: the freemium campaign is imported at
    startup and a first campaign is created during onboarding, so a rotation frozen
    at boot would run one campaign forever.
    """
    from openoutreach.core.operator import campaigns

    while True:
        current = campaigns()
        if not current:
            yield None
            continue
        yield from current


def run_one_action(campaign) -> bool:
    """Do the highest-priority thing available for *campaign*. Returns whether it did.

    Each row is a query and a step. The first one that produces work wins and the
    cycle returns — nothing below it runs, which is what makes the order a priority.

    Every row is timed and named, because the hierarchy is also the only account of
    where the daemon's time goes: the steps log what they *did*, but a row that takes
    twenty seconds to decide it has nothing to do says so nowhere else.
    """
    global _idle_logged_at
    if campaign is None:
        return False

    for name, row in ROWS:
        logger.debug("[%s] → %s?", campaign, name)
        started = time.monotonic()
        acted = row(campaign)
        elapsed = time.monotonic() - started
        if acted:
            logger.info("[%s] %s — %.1fs", campaign,
                        colored(name, "cyan", attrs=["bold"]), elapsed)
            _idle_logged_at = None
            return True
        logger.debug("[%s] %s: nothing (%.1fs)", campaign, name, elapsed)
    _log_idle(campaign)
    return False


def _log_idle(campaign) -> None:
    """Say the daemon is alive and why it has nothing to do — at most once a minute.

    The counts are the hierarchy's own queries in summary form, so the line answers
    the question idleness actually raises: is there no work, or is there work behind
    a gate? A pipeline that is empty and a pipeline that is full but out of send
    headroom look identical from outside and are entirely different problems.
    """
    global _idle_logged_at

    now = time.monotonic()
    if _idle_logged_at is not None and now - _idle_logged_at < IDLE_LOG_INTERVAL_S:
        return
    _idle_logged_at = now
    logger.info("[%s] idle — %s", campaign, _pipeline_summary(campaign))


# What each waiting state means to someone reading a log, in pipeline order. The state
# names are the schema's vocabulary and belong in the schema — READY_TO_FIND_EMAIL says
# where a deal sits in a diagram, "waiting for an address to be bought" says what is
# actually happening to that person.
_WAITING_ON = (
    (DealState.QUALIFIED, "waiting to be ranked"),
    (DealState.READY_TO_FIND_EMAIL, "waiting for an address to be bought"),
    (DealState.FINDING_EMAIL, "address on order"),
    (DealState.RESOLVED, "resolved, ready to export"),
)


def _pipeline_summary(campaign) -> str:
    """One line of counts: who is waiting on what, and which gate is holding them."""
    from django.db.models import Count

    from openoutreach.crm.models import Deal
    from openoutreach.enrichment import bettercontact

    counts = dict(
        Deal.objects.filter(campaign=campaign, lead__disqualified=False)
        .values_list("state")
        .annotate(n=Count("state"))
        .values_list("state", "n"),
    )
    waiting = [f"{counts.get(state, 0)} {phrase}" for state, phrase in _WAITING_ON]

    # The one gate left, said as its consequence rather than as its name — "no finder
    # key, so not buying addresses" tells you why a row declined; a boolean tells you
    # nothing. Discovery and qualification have no gate to report: they always run.
    held = "" if bettercontact.is_configured() else (
        " · no finder key, so not buying addresses")
    return f"{' · '.join(waiting)}{held}"


# ── 1. Check an in-flight lookup ──────────────────────────────────


def _check_lookups(campaign) -> bool:
    from openoutreach.enrichment.lookup import check_lookup, reclaim_lookup

    deal = _due(campaign, DealState.FINDING_EMAIL).first()
    if deal is None:
        return False
    # A deal here without a handle has no job to poll — reclaim it rather than skip
    # it, or it sits in a state no other row claims for as long as the install runs.
    step = check_lookup if deal.lookup_request_id else reclaim_lookup
    return _apply(deal, step(deal))


# ── 4. Score the qualified pool ───────────────────────────────────


def _score_qualified(campaign) -> bool:
    """Promote every QUALIFIED deal the campaign's model is confident about.

    The one step that is per-campaign rather than per-deal: building the model
    dominates the cost of using it, so once it is in hand it scores the whole pool
    in a single pass and is then dropped.

    Skipped entirely while nothing has changed since the last pass. Scoring is a
    pure function of two things — the labels the GP fits on and the pool it scores —
    so re-running it against the same counts cannot promote anybody, and a campaign
    whose pool sits below the gate would otherwise refit a GP every few seconds
    forever (measured: ~1.1s at 300 labels, against a 5s cycle). The counts are two
    indexed `COUNT`s, and being wrong costs one cycle's delay, never a wrong answer.
    """
    from openoutreach.core.ml.qualifier import qualifier_for
    from openoutreach.core.pipeline.ready_pool import promote_to_ready

    before = _pool_signature(campaign)
    if before[0] == 0 or _scored_at.get(campaign.pk) == before:
        return False

    qualifier = qualifier_for(campaign)
    if qualifier is None:
        return False
    promoted = promote_to_ready(campaign, qualifier)
    _scored_at[campaign.pk] = _pool_signature(campaign)
    return promoted > 0


def _pool_signature(campaign) -> tuple[int, int]:
    """`(deals awaiting the gate, deals already past it)` — what scoring depends on.

    The second count stands in for the label set: every state but QUALIFIED is a
    verdict the GP fits on, and the anchors thin out in step with acceptances, so
    any change to the model's inputs moves one of these two numbers.
    """
    from openoutreach.crm.models import Deal

    of_campaign = Deal.objects.filter(campaign=campaign)
    return (
        of_campaign.filter(state=DealState.QUALIFIED).count(),
        of_campaign.exclude(state=DealState.QUALIFIED).count(),
    )


# ── 5. Buy an address ─────────────────────────────────────────────


def _buy_addresses(campaign) -> bool:
    from openoutreach.enrichment import bettercontact
    from openoutreach.enrichment.lookup import buy_address

    # The only gate left on the one paid step: is there a provider to pay. What
    # bounds the spend is the operator's own prepaid credit balance, which the
    # provider enforces and we cannot see — see ``_top_up`` for why nothing here
    # rations it on our side.
    if not bettercontact.is_configured():
        return False
    deal = _due(campaign, DealState.READY_TO_FIND_EMAIL).filter(
        lead__disqualified=False).first()
    if deal is None:
        return False
    return _apply(deal, buy_address(deal))


# ── 6. Top up the pipeline ────────────────────────────────────────


def _top_up(campaign) -> bool:
    """Discover and qualify, always. This row has no gate, and that is the pivot.

    It used to have two — a mailbox had to exist and the campaign had to have send
    headroom left today — because every lead ended in a send, so spending an LLM
    call on someone there was no room to email was waste. Rows 5 and 6 shared that
    one gate (``room_to_send_today``, now deleted): *never resolve an address, and
    never qualify a lead, for someone there is no room to email today.*

    **The product no longer sends, so neither clause survives.** The output is a
    qualified lead with a written reason, handed over as CSV; whether a mailbox
    exists says nothing about whether that lead is worth finding. Worse, the gate
    made a mailbox-less install produce *nothing* — with no ``Mailbox`` rows the
    pool headroom is 0, the comparison is never true, and discovery and
    qualification both stop. Not degraded: silent and empty.

    Nothing replaces it, because there is nothing left to ration. Discovery is free
    (``discovery.py``) and qualification costs one LLM call against a key the
    operator already pays for directly. A daily cap would be a knob invented to
    replace a gate that had a reason, and the loop is already rate-bounded the only
    way that matters: one unit of work per cycle, forever.
    """
    from openoutreach.core.pipeline.top_up import top_up

    return top_up(campaign)


# ── The hierarchy ─────────────────────────────────────────────────
#
# **The loop has no periodic side-effects any more.** It used to run two before every
# action: the mail pass (IMAP into each box, every five minutes) and the daily warmth
# re-measure (an IMAP round trip per box to re-derive its send cap). Both were about
# reading what the world did with our email, and neither has a caller here now — a
# finder emits no email to have an answer to. They moved with the sending leg.

# The ordered list from this module's docstring, made data so the walk can name the
# row it fired. Defined here rather than at the top because it holds the functions
# themselves — the order of this tuple *is* the priority, and there is nowhere else
# it is written down.
#
# The names are what the operator reads in the log, so they say what happens to a
# lead, not which function ran. "top up" named the function and explained nothing;
# "find & qualify new leads" is what that row actually does.
ROWS = (
    ("check for the email address we ordered", _check_lookups),
    ("rank the qualified leads", _score_qualified),
    ("buy an email address", _buy_addresses),
    ("find & qualify new leads", _top_up),
)


# ── Plumbing ──────────────────────────────────────────────────────


def _due(campaign, state):
    """This campaign's deals in *state* that are not waiting on a ``not_before``."""
    from django.db.models import Q

    from openoutreach.crm.models import Deal

    return (
        Deal.objects.filter(campaign=campaign, state=state)
        .filter(Q(not_before__isnull=True) | Q(not_before__lte=timezone.now()))
        .select_related("lead", "campaign")
        .order_by("creation_date")
    )


def _apply(deal, next_state) -> bool:
    """Persist whatever the step did to *deal*, including its new state.

    One save for the transition and the fields that justify it, so a deal can never
    be moved to RESOLVED without the address that justifies it, or parked at
    FINDING_EMAIL without the job handle to poll. A step that returns ``None`` has
    decided to stay put — it has usually written ``not_before`` — and that is saved
    just the same.
    """
    if next_state is not None:
        deal.state = next_state
    deal.save()
    return True


def _import_freemium_campaign() -> None:
    """Pull the published kit once at startup and mirror it into a local campaign.

    Config import, not model loading: the kit's *model* is fetched on demand by
    ``qualifier_for``. Seeds are imported here too, though the published seed list is
    empty in practice — the freemium campaign feeds on leads other campaigns have
    already discovered.
    """
    from openoutreach.core.ml.hub import fetch_kit
    from openoutreach.core.setup.freemium import import_freemium_campaign, seed_profiles

    kit = fetch_kit()
    if not kit:
        return
    campaign = import_freemium_campaign(kit["config"])
    if campaign:
        seed_profiles(campaign, kit["config"])
