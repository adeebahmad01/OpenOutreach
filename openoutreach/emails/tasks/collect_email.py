# openoutreach/emails/tasks/collect_email.py
"""COLLECT_EMAIL task — the *poll* leg of the paid email lookup.

The bound counterpart to ``find_email``: it polls one in-flight provider job
(the ``request_id`` the submit leg parked in this task's payload) exactly once,
then acts on the outcome:

    hit          → READY_TO_EMAIL   (address set + given back to the hub; 1 credit)
    miss         → NO_EMAIL_BETTERCONTACT (terminal — a fit positive the ML keeps)
    still running → chain the next poll with a doubled backoff
    couldn't poll → retry with the same backoff (nothing was learned about the job)

Each still-running poll mints its successor (``attempt + 1``), so exactly one
live collect task exists per in-flight lookup — the chain, not the drain guard,
maintains that invariant. The request_id and backoff attempt live in the payload,
so the lookup survives a daemon restart on the persisted row.

**The only terminal outcomes are the provider's own.** There is no deadline and no
attempt limit: a job that has not terminated is queued, and the deal waits at
FINDING_EMAIL where nothing re-selects it. The alternative was tried and is worse
— abandoning the job reverted the deal to READY_TO_FIND_EMAIL, where the submit
leg bought a *second* job for the same lead, so a provider outage became a hot
resubmit loop (418 submits and 4,512 polls in a week for ~40 leads, none
terminating). Uncapped doubling makes waiting nearly free instead: a week costs
17 polls. It also refuses to mislabel — a timeout is evidence about the provider,
never about whether this lead has a findable address.
"""
from __future__ import annotations

import logging

from openoutreach.core.conf import COLLECT_BACKOFF_BASE_S, COLLECT_BACKOFF_MAX_S
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
        _reschedule(p, advance=False)
        return

    if outcome.hit:
        _on_hit(session, campaign, deal, public_id, outcome.email)
    elif outcome.miss:
        _on_miss(session, public_id)
    else:  # still running
        _reschedule(p, advance=True)


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


def _reschedule(payload, advance: bool) -> None:
    """Chain the next poll of this job, further out than the last.

    ``advance`` doubles the backoff (a genuine still-running poll); a transient
    outage retries at the same backoff, since nothing was learned about the job.

    There is no deadline and no attempt limit. The job is *queued*, not lost, so
    asking again later is strictly better than abandoning it: abandoning meant
    reverting the deal to READY_TO_FIND_EMAIL, where the submit leg immediately
    bought a second job for the same lead. Doubling reaches long waits for almost
    nothing — a week costs 17 polls — so a provider outage now costs a handful of
    HTTP requests and no submits at all, and the deal simply resumes when the
    queue drains. The interval rails at ``COLLECT_BACKOFF_MAX_S`` (a month) purely
    so the schedule stays representable; the chain itself never ends.
    """
    from openoutreach.core.scheduler import schedule_collect_email

    attempt = payload.get("attempt", 0) + 1 if advance else payload.get("attempt", 0)
    delay = min(COLLECT_BACKOFF_BASE_S * (2 ** min(attempt, 64)), COLLECT_BACKOFF_MAX_S)
    schedule_collect_email(payload={**payload, "attempt": attempt}, delay_seconds=delay)
    # A genuine still-running poll reports its next wake-up; a transient outage
    # already logged its ⚠ retry step above, so don't double up.
    if advance:
        logger.info("%s", step_line(
            "running", f"not ready — re-poll in {_human(delay)} (attempt {attempt})"))


def _human(seconds: float) -> str:
    """A backoff delay at a readable scale — these run from seconds to weeks."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.1f}{unit}"
    return f"{seconds:.0f}s"
