import logging

import numpy as np
from django.db import transaction

from openoutreach.crm.models import DealState

logger = logging.getLogger(__name__)


@transaction.atomic
def promote_lead_to_deal(campaign, profile_url: str, reason: str = ""):
    """Create a QUALIFIED Deal for a Lead.

    Returns the Deal.
    """
    from openoutreach.crm.models import Lead, Deal

    lead = Lead.objects.filter(profile_url=profile_url).first()
    if not lead:
        raise ValueError(f"No Lead for {profile_url}")

    deal = Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=DealState.QUALIFIED,
        reason=reason,
    )

    from termcolor import colored
    logger.info("%s %s", profile_url, colored("QUALIFIED", "green", attrs=["bold"]))
    return deal


def create_lead(row: dict, country_code: str = "", discovered_by=None,
                query_terms: str = "") -> bool:
    """Persist one Lead Finder row as an embedded Lead awaiting qualification.

    Keyed on ``profile_url`` (the provider's per-person URL). Stamps the firmographic
    ``profile_text`` (the LLM qualifier's input) and the embedding from the same row,
    so qualification never re-fetches anything. ``country_code`` is the ICP's target
    country (Lead Finder rows carry no ISO code) — blank means unknown, which the
    contacts-store geo-gate treats conservatively.

    ``discovered_by`` is the query node that surfaced this row; it lands only on first
    touch (a profile another query already created keeps its original node). Its
    ``query_terms`` are folded into the **embedding only** — not ``profile_text`` — so
    the GP learns which query keywords surface good leads while the LLM judges the
    person on firmographics alone.

    The row's person fields (``person_for``) land as named columns and its employer as a
    shared ``Company`` row (``company_for``) — apart from both the text and the vector,
    because the export hands them on as fields and a name would only add noise to the
    embedding. Returns True when a new Lead was created, False when one already existed
    (idempotent re-discovery).
    """
    from openoutreach.crm.models import Lead
    from openoutreach.discovery import (
        company_for, embed_profile, person_for, profile_text_for, source_fields_for,
    )

    profile_url = row.get("contact_linkedin_profile_url")
    if not profile_url:
        return False

    profile_text = profile_text_for(row)
    _, created = Lead.objects.get_or_create(
        profile_url=profile_url,
        defaults={
            "embedding": np.asarray(
                embed_profile(profile_text, query_terms), dtype=np.float32).tobytes(),
            "profile_text": profile_text,
            "source_fields": source_fields_for(row),
            "country_code": country_code,
            "discovered_by": discovered_by,
            "company": company_for(row),
            **person_for(row),
        },
    )
    return created


def disqualify_lead(profile_url: str):
    """Set Lead.disqualified = True (account-level, permanent, cross-campaign)."""
    from openoutreach.crm.models import Lead

    lead = Lead.objects.filter(profile_url=profile_url).first()
    if not lead:
        logger.warning("disqualify_lead: no Lead for %s", profile_url)
        return
    lead.disqualified = True
    lead.save(update_fields=["disqualified"])


def _closed_states() -> tuple:
    """States an unsubscribe leaves alone — the deal is already over.

    Overwriting one would destroy the outcome that closed it, and an opt-out from
    someone whose thread ended weeks ago changes nothing about that thread. Every
    other state moves to UNSUBSCRIBED, whatever leg of the funnel it was on.
    """
    from openoutreach.crm.models import DealState

    return (
        DealState.COMPLETED,
        DealState.FAILED,
        DealState.NO_EMAIL_BETTERCONTACT,
        DealState.UNSUBSCRIBED,
    )


def suppress_email(address: str) -> int:
    """Honour an opt-out from *address*: suppress every lead at it, close their deals.

    Suppression binds to the **person**, not to one thread — so it is written to
    ``Lead.disqualified`` (permanent, account-level, cross-campaign), which the
    candidate queries already filter, rather than to a state only one campaign
    would read. ``Lead.email`` has no unique constraint, so *every* row holding
    the address is suppressed, matched case-insensitively because a mail client
    echoes back whatever casing it was given.

    Returns the number of leads suppressed (0 when the address is unknown — an
    unsubscribe from someone we never emailed, which is not an error).

    Idempotent: re-running over the same address writes the same rows to the same
    values, so a rescanned mailbox costs nothing.
    """
    from openoutreach.crm.models import Deal, DealState, Lead

    address = (address or "").strip()
    if not address:
        return 0

    leads = list(Lead.objects.filter(email__iexact=address))
    if not leads:
        logger.info("unsubscribe from %s matched no lead", address)
        return 0

    Lead.objects.filter(pk__in=[lead.pk for lead in leads]).update(disqualified=True)
    closed = (
        Deal.objects.filter(lead__in=leads)
        .exclude(state__in=_closed_states())
        .update(state=DealState.UNSUBSCRIBED)
    )
    logger.info("unsubscribe from %s: %d lead(s) suppressed, %d deal(s) closed",
                address, len(leads), closed)
    return len(leads)
