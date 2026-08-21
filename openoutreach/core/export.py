# openoutreach/core/export.py
"""The lead export contract — one record shape, one file per campaign, no command.

This is the finder's **public output**: what leaves OpenOutreach and reaches whatever
the operator actually sends with. The boundary card
(``roadmap/p1-e3-leadfinder-sequencer-boundary.md`` in openoutreach-docs) specifies it,
and the rule it exists to serve is that our own sender gets no privileged path — a
sequencer, a CRM and a spreadsheet all read the same rows.

**The columns are the importers', not ours.** Instantly and Smartlead both *require*
``email``, ``first_name`` and ``last_name``, and both recognise ``company``, ``title``,
``website`` and ``linkedin_url`` as standard fields, mapping anything else to a custom
variable. So the record uses those names exactly — ``company``, not ``company_name``;
``title``, not ``job_title`` — and a file exported here imports without column mapping.
Everything we might like to ship but they do not know (state, campaign, country,
discovery provenance) is left out rather than dumped in as noise variables.

``RECORD_FIELDS`` is the record, in one place. CSV is the only serialisation today because
it is the only one with a consumer; when the webhook of Flow 2 arrives it serialises the
same tuple, so the two cannot drift into separate schemas.

**There is no score column, deliberately.** An earlier version exported the GP's
``P(f>0.5)``. That was a category error: ``core/pipeline/ready_pool.py`` defines
``min_gp_confidence`` as "the paid-lookup spend gate **and nothing else**" — the GP
decides whether to spend a credit resolving an address, not whether a lead fits. The fit
verdict is the LLM's and it is already here as ``reason``, in language a person reads.
Exporting the posterior invited thresholding on a number nobody calibrated, and separated
nothing: every lead in this file already has a Deal, so it already passed the qualifier.

It also made the export expensive and unsafe. Scoring meant ``qualifier_for(campaign)``,
which warm-starts over every label and fits a GP — O(n³), minutes on a real campaign
(2,538 deals on the live install; the docstring there assumes "tens to low hundreds") —
and which calls ``ensure_anchors``, so a cold campaign would have made **LLM calls and
mutated campaign state from a read-only export**. This module now touches nothing but the
database.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import IO, Iterable

from django.conf import settings
from django.utils.text import slugify

# The record, in order.
#
# Required by Instantly + Smartlead: email, first_name, last_name.
# Standard-mapped by both: company, title, website, linkedin_url.
# A custom variable, and the reason this product exists: reason.
# Ours: lead_id — the join key for outcomes coming back, since a sequencer echoes
# custom variables in its webhooks and an address can change under us.
RECORD_FIELDS = (
    "email",
    "first_name",
    "last_name",
    "company",
    "title",
    "website",
    "linkedin_url",
    "reason",
    "lead_id",
)


def lead_record(deal) -> dict:
    """One Deal as an export record.

    The Deal, not the Lead, is the unit: the qualification verdict (``reason``) is
    per-campaign, and the same person can be a lead in two campaigns with two different
    answers.
    """
    lead = deal.lead
    company = lead.company
    return {
        "email": lead.email,
        # From the enrichment provider's own response, never split in-house. Null for a
        # lead resolved through the free hub cache, which never calls BetterContact.
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company": company.name if company else None,
        "title": lead.job_title,
        "website": company.domain if company else None,
        "linkedin_url": lead.profile_url,
        "reason": deal.reason,
        "lead_id": lead.pk,
    }


def lead_records(campaign) -> Iterable[dict]:
    """Every lead in ``campaign`` the qualifier **accepted**, as records, oldest first.

    A lead is judged once it has a Deal — that is where the LLM's ``reason`` lives, so
    an unjudged lead has nothing to say in a contract whose selling point is *why this
    lead*. But a Deal is not an endorsement: the two rejections are separate columns and
    both have to be excluded, which is the trap this filter exists to close.

    - **`FAILED`** is the LLM's own rejection, campaign-scoped (`FAILED` + `wrong_fit`).
      The `reason` on those rows reads *"does not align well with the target market"* —
      exporting them hands a sender the people the model explicitly said no to.
    - **`Lead.disqualified`** is the permanent, account-level exclusion (an opt-out).

    Filtering only on `disqualified` catches the second and misses the first, which is
    what shipped and what the live install exposed: 1,944 rows exported from a campaign
    where most deals were rejections.

    Lazy on purpose: one indexed query streamed straight to the writer, so a campaign
    with thousands of deals never materialises twice.
    """
    from openoutreach.crm.models import Deal, DealState

    deals = (
        Deal.objects.filter(campaign=campaign, lead__disqualified=False)
        .exclude(state=DealState.FAILED)
        .select_related("lead", "lead__company")
        .order_by("lead__creation_date")
    )
    return (lead_record(deal) for deal in deals.iterator())


# ── serialisation ────────────────────────────────────────────────

def write_csv(records: Iterable[dict], stream: IO[str]) -> int:
    """Write records as CSV with a header row. Returns the row count.

    ``None`` writes as an empty cell — the csv module's own behaviour, which is exactly
    what an importer expects for a field we were never told.
    """
    writer = csv.DictWriter(stream, fieldnames=list(RECORD_FIELDS), extrasaction="raise")
    writer.writeheader()
    count = 0
    for record in records:
        writer.writerow(record)
        count += 1
    return count


# ── the file the operator came for ───────────────────────────────
#
# There is no export command. The deliverable is a file that is already there, one per
# campaign, beside the database — so `~/.openoutreach/data/leads/…` installed, `data/leads/…`
# in a checkout, `/app/data/leads/…` in the container. A subdirectory keeps it out of the
# puddle of `db.sqlite3-wal` files, and `status` reports the real path, so the naming rule
# below never has to be guessed at by eye.

def campaign_csv_path(campaign) -> Path:
    """Where *campaign*'s leads are written. The naming rule lives here and nowhere else."""
    from openoutreach.core.models import Campaign

    slug = slugify(campaign.name)
    if not slug:
        # A name of only punctuation or non-Latin script slugifies to nothing.
        return _leads_dir() / f"campaign-{campaign.pk}.csv"

    # `Campaign.name` is unique but `slugify` is not injective, so two distinct names can
    # collide on one file. The **older** campaign keeps the bare slug and the newcomer
    # takes its pk as a suffix — a first-come rule, so a file an operator is already
    # reading never moves because someone created a similarly named campaign.
    collides = (
        Campaign.objects.filter(pk__lt=campaign.pk)
        .filter(name__isnull=False)
        .values_list("name", flat=True)
    )
    if any(slugify(other) == slug for other in collides):
        slug = f"{slug}-{campaign.pk}"
    return _leads_dir() / f"{slug}.csv"


def _leads_dir() -> Path:
    return Path(settings.DATABASE_PATH).parent / "leads"


def write_campaign_csv(campaign) -> int:
    """Rewrite *campaign*'s CSV from the database. Returns the row count.

    **The whole file, every time.** It is a projection of the database, so a rewrite makes
    it the current truth for that campaign: a restart cannot duplicate rows, and an email
    resolved *later* updates the row already written — which appending cannot do at all.

    The write is atomic. Agents poll, so a reader will eventually open this file mid-write
    unless it never exists mid-write: the rows go to a sibling temp file and `os.replace`
    swaps it in. A sibling and not `/tmp`, because `os.replace` is only atomic within one
    filesystem.
    """
    path = campaign_csv_path(campaign)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as stream:
            count = write_csv(lead_records(campaign), stream)
        os.replace(tmp, path)
        return count
    finally:
        tmp.unlink(missing_ok=True)
