# openoutreach/emails/tasks/collect_email.py
"""COLLECT_EMAIL task — the *poll* leg of the paid email lookup.

The bound counterpart to ``find_email``: it polls one in-flight provider job
(the ``request_id`` the submit leg parked in this task's payload) exactly once,
then acts on the outcome:

    hit          → READY_TO_EMAIL   (address set + given back to the hub; 1 credit)
    miss         → NO_EMAIL_BETTERCONTACT (terminal — a fit positive the ML keeps)
    still running → chain the next poll with a doubled backoff, unless past the
                    give-up deadline → revert FINDING_EMAIL → READY_TO_FIND_EMAIL
                    for a fresh submit, or park at NO_EMAIL_BETTERCONTACT once
                    this deal has burned ``COLLECT_MAX_SUBMITS`` jobs
    couldn't poll → retry with the same backoff (transient outage, deadline-bounded)

Each still-running poll mints its successor (``attempt + 1``), so exactly one
live collect task exists per in-flight lookup — the chain, not the drain guard,
maintains that invariant. The request_id, backoff attempt, and deadline all live
in the payload, so the lookup survives a daemon restart on the persisted row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.utils import timezone

from openoutreach.core.conf import (
    COLLECT_BACKOFF_BASE_S,
    COLLECT_BACKOFF_MAX_S,
    COLLECT_DEADLINE_S,
    COLLECT_MAX_SUBMITS,
)
from openoutreach.core.logblock import block_header, step_line
from openoutreach.crm.models import DealState

logger = logging.getLogger(__name__)


def handle_collect_email(task, session, qualifiers):
    from openoutreach.crm.models import Deal
    from openoutreach.emails.bettercontact import BetterContactUnavailable

    campaign = session.campaign
    p = task.payload
    deal = (
        Deal.objects.filter(pk=p.get("deal_id"), state=DealState.FINDING_EMAIL)
        .select_related("lead")
        .first()
    )
    if deal is None:
        # The deal moved on (or was reset) since this poll was scheduled — the
        # chain is stale, so let it end here rather than act on a wrong state.
        logger.info("[%s] collect_email: deal %s no longer FINDING_EMAIL — dropping poll", campaign, p.get("deal_id"))
        return

    public_id = deal.lead.profile_url
    logger.info("%s", block_header(
        f"collect_email · {campaign} · {public_id}", "magenta", meta=f"attempt {p.get('attempt', 0)}"))

    try:
        outcome = _poll(p["provider"], p["request_id"])
    except BetterContactUnavailable as exc:
        # Transient — retry with the same backoff, still bounded by the deadline.
        logger.info("%s", step_line("poll", f"unavailable ({exc}) — retrying", glyph="⚠", color="yellow"))
        _reschedule_or_give_up(session, public_id, p, advance=False)
        return

    if outcome.hit:
        _on_hit(session, campaign, deal, public_id, outcome.email)
    elif outcome.miss:
        _on_miss(session, public_id)
    else:  # still running
        _reschedule_or_give_up(session, public_id, p, advance=True)


def submits_for(deal_id) -> int:
    """Paid provider jobs fired for *deal_id* — distinct ``request_id``s across its
    collect tasks.

    Derived rather than stored: every job this deal ever started left a collect row
    carrying both ids, so the count is already in the queue. Distinct on the
    request_id because one job is polled many times — counting rows would count
    backoff attempts, which is the wrong quantity by an order of magnitude.
    """
    from openoutreach.core.models import Task

    return (
        Task.objects
        .filter(task_type=Task.TaskType.COLLECT_EMAIL, payload__deal_id=deal_id)
        .values_list("payload__request_id", flat=True)
        .distinct()
        .count()
    )


def _poll(provider: str, request_id: str):
    from openoutreach.emails import bettercontact

    if provider == "bettercontact":
        return bettercontact.poll_once(request_id)
    raise ValueError(f"unknown email provider: {provider}")


# ── Outcome handling ──────────────────────────────────────────────────


def _on_hit(session, campaign, deal, public_id, email) -> None:
    """Persist the address, give it back to the hub (paid hit), route to send."""
    from openoutreach.contacts import service as contacts
    from openoutreach.core.db.deals import set_profile_state
    from openoutreach.core.scheduler import flush_email_queue, opener_allowances

    deal.lead.email = email
    deal.lead.save(update_fields=["email"])
    contacts.contribute(session, deal.lead, [email], contacts.ORIGIN_BETTERCONTACT)
    set_profile_state(session, public_id, DealState.READY_TO_EMAIL.value, log=False)
    # Queue the opener now so the send preempts the next find_email on claim —
    # within this campaign's opener allowance (core/quota.py).
    flush_email_queue(session, campaign, opener_allowances(session.campaigns)[campaign.pk])
    logger.info("%s", step_line("hit", f"{email} → {DealState.READY_TO_EMAIL.name}", glyph="✓", color="green"))


def _on_miss(session, public_id) -> None:
    """Terminal miss — enrichment found no address. Parks at its own terminal state
    (NO_EMAIL_BETTERCONTACT), distinct from FAILED: the lead was a fit positive
    (the ML labeler keeps it as label=1), only reachability failed. The dedicated
    state also gives downstream work a hook to build on (e.g. retry via another
    provider)."""
    from openoutreach.core.db.deals import set_profile_state

    set_profile_state(session, public_id, DealState.NO_EMAIL_BETTERCONTACT.value, log=False)
    logger.info("%s", step_line(
        "no email", f"terminal miss → {DealState.NO_EMAIL_BETTERCONTACT.name}", glyph="✗", color="yellow"))


def _reschedule_or_give_up(session, public_id, payload, advance: bool) -> None:
    """Chain the next poll, or give up on this job past the deadline.

    ``advance`` doubles the backoff (a genuine still-running poll); a transient
    outage retries at the same backoff. Either way the give-up deadline
    (``submitted_at + COLLECT_DEADLINE_S``) is the hard bound: past it, the job is
    abandoned and the deal re-queues for a fresh submit (no credit was spent) —
    but only while it is under ``COLLECT_MAX_SUBMITS`` jobs. A deal that has burned
    them all parks at NO_EMAIL_BETTERCONTACT instead, because the re-queue is a
    *loop*: the deal returns to the candidate pool, is re-selected, and submits
    again, so a provider whose jobs never terminate would spin it forever.
    """
    from openoutreach.core.db.deals import set_profile_state
    from openoutreach.core.scheduler import schedule_collect_email

    submitted_at = datetime.fromisoformat(payload["submitted_at"])
    if timezone.now() >= submitted_at + timedelta(seconds=COLLECT_DEADLINE_S):
        _on_deadline(session, public_id, payload)
        return

    attempt = payload.get("attempt", 0) + 1 if advance else payload.get("attempt", 0)
    delay = min(COLLECT_BACKOFF_BASE_S * (2 ** attempt), COLLECT_BACKOFF_MAX_S)
    schedule_collect_email(payload={**payload, "attempt": attempt}, delay_seconds=delay)
    # A genuine still-running poll reports its next wake-up; a transient outage
    # already logged its ⚠ retry step above, so don't double up.
    if advance:
        logger.info("%s", step_line("running", f"not ready — re-poll in {delay}s (attempt {attempt})"))


def _on_deadline(session, public_id, payload) -> None:
    """Abandon this job: re-queue the deal for a fresh submit, or give up on it.

    The submit budget is spent per *deal*, not per job, so it is read here from the
    queue rather than from the payload — the payload belongs to the one job that
    just timed out and knows nothing about its predecessors.
    """
    from openoutreach.core.db.deals import set_profile_state

    submits = submits_for(payload.get("deal_id"))
    if submits >= COLLECT_MAX_SUBMITS:
        set_profile_state(session, public_id, DealState.NO_EMAIL_BETTERCONTACT.value, log=False)
        logger.info("%s", step_line(
            "gave up",
            f"{submits} jobs, none terminated → {DealState.NO_EMAIL_BETTERCONTACT.name}",
            glyph="✗", color="yellow"))
        return

    set_profile_state(session, public_id, DealState.READY_TO_FIND_EMAIL.value, log=False)
    logger.info("%s", step_line(
        "deadline",
        f"poll deadline exceeded → {DealState.READY_TO_FIND_EMAIL.name}"
        f" · re-queued for a fresh submit ({submits}/{COLLECT_MAX_SUBMITS})",
        glyph="⚠", color="yellow"))
