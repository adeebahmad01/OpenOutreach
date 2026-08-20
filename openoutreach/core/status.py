# openoutreach/core/status.py
"""What the daemon would say if you asked it — as data, for a person or a program.

The reader here is as often a program as a person, and a program does not tail a
log: it **asks**. So this module builds one dict and the ``status`` command renders
it, either as a human summary or as ``--json``. Nothing here prints, and nothing
here mutates — ``status`` is safe to run against a live daemon (SQLite is in WAL
mode, so a read never blocks on the daemon's writes).

Four things it has to answer, and one it has to decide:

  * what is configured, and what is not;
  * what is **blocked**, and why — in the stable vocabulary of ``core/errors.py``,
    because *no leads yet* and *your key was rejected* must never look alike;
  * the counts toward the deliverable, and the credit balance;
  * **the next action** — one object an agent can relay to its human, carrying what
    it unlocks, how many leads are waiting, and the URL.
"""
from __future__ import annotations

import logging

from openoutreach.core.errors import ErrorType

logger = logging.getLogger(__name__)

BETTERCONTACT_SIGNUP_URL = "https://bettercontact.rocks?fpr=openoutreach"
"""Attribution is won at **signup**, not at payment, so every path we show carries it."""


def build_status() -> dict:
    """Assemble the whole status document. Reads only; never raises on a dead provider."""
    from openoutreach.core import onboarding

    onboarding_state = _onboarding_state(onboarding)
    campaigns = _campaign_counts()
    credits = _credits()
    totals = _totals(campaigns)
    blocked = _blocked(onboarding_state, credits, totals)

    return {
        "onboarding": onboarding_state,
        "campaigns": campaigns,
        "totals": totals,
        "credits": credits,
        "blocked": blocked,
        "export": _export_hint(campaigns),
        "next_action": next_action(onboarding_state, credits, totals, campaigns),
    }


# ── configuration ────────────────────────────────────────────────

def _onboarding_state(onboarding) -> dict:
    """Which steps are satisfied, and the variables that would satisfy the rest."""
    missing = onboarding.missing_env_keys()
    return {
        "complete": not missing,
        "done": [step.key for step in onboarding.STEPS if step.key not in missing],
        "missing": {
            key: [onboarding.ENV_PREFIX + name for name in names]
            for key, names in missing.items()
        },
    }


# ── the counts toward the deliverable ────────────────────────────

def _campaign_counts() -> list[dict]:
    """Per-campaign pipeline counts, oldest campaign first."""
    from openoutreach.core.models import Campaign
    from openoutreach.crm.models import Deal, DealState

    rows = []
    for campaign in Campaign.objects.all().order_by("id"):
        deals = Deal.objects.filter(campaign=campaign)
        by_state = {
            state: deals.filter(state=state).count()
            for state in (
                DealState.QUALIFIED,
                DealState.READY_TO_FIND_EMAIL,
                DealState.FINDING_EMAIL,
                DealState.RESOLVED,
                DealState.NO_EMAIL_BETTERCONTACT,
                DealState.FAILED,
            )
        }
        exportable, with_email = _export_counts(campaign)
        rows.append({
            "name": campaign.name,
            "leads_seen": deals.count(),
            "qualified": by_state[DealState.QUALIFIED],
            "ranked_for_lookup": by_state[DealState.READY_TO_FIND_EMAIL],
            "lookup_in_flight": by_state[DealState.FINDING_EMAIL],
            "resolved": by_state[DealState.RESOLVED],
            "no_email_found": by_state[DealState.NO_EMAIL_BETTERCONTACT],
            "rejected": by_state[DealState.FAILED],
            "exportable": exportable,
            "exportable_with_email": with_email,
            "exportable_without_email": exportable - with_email,
        })
    return rows


def _export_counts(campaign) -> tuple[int, int]:
    """Rows ``export_leads`` would write, and how many carry an address.

    **An exportable row is not necessarily a mailable one.** The export excludes only
    the two rejections, so a `QUALIFIED` lead exports with a blank ``email`` column —
    an address is an enrichment on top, never a precondition. A reader who is about to
    import into a sequencer needs both numbers, so both are counted here, from the
    records themselves rather than from a state that stands in for them.
    """
    from openoutreach.core.export import lead_records

    exportable = with_email = 0
    for record in lead_records(campaign):
        exportable += 1
        with_email += bool(record.get("email"))
    return exportable, with_email


def _totals(campaigns: list[dict]) -> dict:
    """Sum the per-campaign counts, so the top-level answer is one line of arithmetic."""
    keys = (
        "leads_seen", "qualified", "ranked_for_lookup", "lookup_in_flight",
        "resolved", "no_email_found", "rejected",
        "exportable", "exportable_with_email", "exportable_without_email",
    )
    return {key: sum(row[key] for row in campaigns) for key in keys}


# ── the balance ──────────────────────────────────────────────────

def _credits() -> dict:
    """Read the provider balance, reporting *why* it is unknown rather than guessing.

    A balance we could not read is not a balance of zero, and the difference decides
    whether the operator is asked to top up.
    """
    from openoutreach.enrichment import bettercontact

    if not bettercontact.is_configured():
        return {"balance": None, "error": ErrorType.NO_CREDENTIAL}

    try:
        return {"balance": bettercontact.credit_balance(), "error": None}
    except bettercontact.BetterContactUnavailable as exc:
        logger.debug("Could not read the credit balance: %s", exc)
        return {"balance": None, "error": exc.error_type, "detail": str(exc)}


# ── what is blocked, and why ─────────────────────────────────────

def _blocked(onboarding_state: dict, credits: dict, totals: dict) -> list[dict]:
    """Everything standing between the current state and more qualified rows."""
    blocked = []

    if not onboarding_state["complete"]:
        blocked.append({
            "type": ErrorType.ONBOARDING_INCOMPLETE,
            "message": "onboarding is incomplete: " + ", ".join(onboarding_state["missing"]),
        })

    if credits["error"] == ErrorType.NO_CREDENTIAL:
        blocked.append({
            "type": ErrorType.NO_CREDENTIAL,
            "message": "no BetterContact key — discovery and email finding are both off",
        })
    elif credits["error"] == ErrorType.PROVIDER_AUTH:
        blocked.append({
            "type": ErrorType.PROVIDER_AUTH,
            "message": "BetterContact rejected the API key",
        })
    elif credits["error"] == ErrorType.PROVIDER_OUT_OF_CREDITS:
        blocked.append({
            "type": ErrorType.PROVIDER_OUT_OF_CREDITS,
            "message": "BetterContact reports the credits are exhausted",
        })
    elif credits["error"] == ErrorType.PROVIDER_RATE_LIMITED:
        blocked.append({
            "type": ErrorType.PROVIDER_RATE_LIMITED,
            "message": "BetterContact is rate-limiting this client — the run is backing off",
        })
    elif credits["balance"] == 0 and totals["ranked_for_lookup"]:
        blocked.append({
            "type": ErrorType.PROVIDER_OUT_OF_CREDITS,
            "message": (
                f"{totals['ranked_for_lookup']} ranked lead(s) waiting, 0 credits left"
            ),
        })

    return blocked


# ── the export ───────────────────────────────────────────────────

def _export_hint(campaigns: list[dict]) -> dict:
    """How to get the rows out. There is no CSV path yet — the export writes stdout."""
    name = campaigns[0]["name"] if campaigns else "<campaign>"
    return {
        "path": None,
        "command": f'openoutreach export_leads --campaign "{name}" > leads.csv',
    }


# ── the next action ──────────────────────────────────────────────

def next_action(onboarding_state: dict, credits: dict, totals: dict, campaigns: list[dict]) -> dict:
    """The one thing to do next — arithmetic, not adjectives.

    Ordered by what actually blocks progress, which is why the credit ask sits above
    the export rather than below it: a ranked lead is one the run *cannot advance*
    without credits, whereas the export is available at any time and is reported
    under ``export`` regardless.

    That ordering does not break the *never before value* rule. Ranked leads are
    qualified leads with written reasons, so ``ranked_for_lookup > 0`` **is** the
    proof that value exists — a first run with nothing qualified yet reaches the
    ``wait`` branch and is asked for nothing.
    """
    if not onboarding_state["complete"]:
        variables = sorted({v for names in onboarding_state["missing"].values() for v in names})
        return {
            "type": "finish_onboarding",
            "message": "Onboarding is incomplete.",
            "unlocks": "the run can start",
            "variables": variables,
        }

    if credits["balance"] == 0 and totals["ranked_for_lookup"]:
        return {
            "type": "add_credits",
            "message": (
                f"{totals['ranked_for_lookup']} ranked lead(s) waiting, 0 credits left."
            ),
            "unlocks": "a work email address for each of them",
            "leads": totals["ranked_for_lookup"],
            "url": BETTERCONTACT_SIGNUP_URL,
        }

    if totals["exportable"]:
        name = next((c["name"] for c in campaigns if c["exportable"]), campaigns[0]["name"])
        return {
            "type": "export_leads",
            "message": f"{totals['exportable']} qualified lead(s) ready to export.",
            "unlocks": "a CSV your sequencer imports without column mapping",
            "leads": totals["exportable"],
            "command": f'openoutreach export_leads --campaign "{name}" > leads.csv',
        }

    return {
        "type": "wait",
        "message": "Running — no qualified leads yet.",
        "unlocks": None,
    }
