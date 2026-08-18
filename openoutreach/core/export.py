# openoutreach/core/export.py
"""The lead export contract — one record shape, two serialisations.

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

CSV is a flattening of the JSON record, never a second schema: both are generated from
``RECORD_FIELDS``, so a field cannot appear in one and be forgotten in the other.

**The score is computed here, not stored.** ``Lead`` carries no probability column
on purpose (``core/pipeline/ready_pool.py`` explains why it must not be written over
``Deal.reason``), so the export fits the campaign's GP once and scores the batch in one
pass. A campaign whose GP cannot fit yet exports ``score=None`` rather than a
fabricated number.
"""
from __future__ import annotations

import csv
import json
import logging
from typing import IO, Iterable

import numpy as np

logger = logging.getLogger(__name__)

# The record, in order. The one definition both serialisations read.
#
# Required by Instantly + Smartlead: email, first_name, last_name.
# Standard-mapped by both: company, title, website, linkedin_url.
# Custom variables, and the reason this product exists: score, reason.
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
    "score",
    "reason",
    "lead_id",
)


def lead_record(deal, score: float | None = None) -> dict:
    """One Deal as an export record.

    The Deal, not the Lead, is the unit: the qualification verdict (``reason``) is
    per-campaign, and the same person can be a lead in two campaigns with two different
    answers.

    ``score`` is the GP's ``P(f>0.5)``, passed in by the caller because it is fitted per
    batch rather than per row; ``None`` when the model could not fit.
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
        "score": score,
        "reason": deal.reason,
        "lead_id": lead.pk,
    }


def lead_records(campaign, states: Iterable[str] | None = None,
                 include_disqualified: bool = False) -> list[dict]:
    """Every judged lead in ``campaign``, as export records, best-scoring first.

    A lead is judged once it has a Deal — that is where the LLM's ``reason`` lives, so
    an unqualified lead has nothing to say in a contract whose selling point is *why
    this lead*.

    Disqualified leads are excluded by default: they are the ones the operator must not
    contact, and the common case for this command is handing rows to a sender.
    """
    from openoutreach.crm.models import Deal

    deals = (
        Deal.objects.filter(campaign=campaign)
        .select_related("lead", "lead__company")
        .order_by("lead__creation_date")
    )
    if not include_disqualified:
        deals = deals.filter(lead__disqualified=False)
    if states:
        deals = deals.filter(state__in=list(states))

    deals = list(deals)
    scores = _score_batch(campaign, deals)
    records = [lead_record(deal, score) for deal, score in zip(deals, scores)]
    records.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0.0)))
    return records


def _score_batch(campaign, deals: list) -> list[float | None]:
    """GP ``P(f>0.5)`` for each deal's lead, aligned with ``deals``.

    One fit for the whole batch. ``None`` fills every slot the model cannot speak to:
    a lead with no cached embedding, or a campaign whose GP will not fit yet (one label
    class — the cold start).
    """
    from openoutreach.core.ml.qualifier import qualifier_for

    scores: list[float | None] = [None] * len(deals)
    scorable = [(i, deal.lead.embedding_array) for i, deal in enumerate(deals)]
    scorable = [(i, emb) for i, emb in scorable if emb is not None]
    if not scorable:
        return scores

    qualifier = qualifier_for(campaign)
    if qualifier is None:
        return scores

    X = np.array([emb for _, emb in scorable], dtype=np.float64)
    probs = qualifier.predict_probs(X)
    if probs is None:
        logger.info("export: the GP could not fit — exporting without scores")
        return scores

    for (index, _), prob in zip(scorable, probs):
        scores[index] = float(prob)
    return scores


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


def write_jsonl(records: Iterable[dict], stream: IO[str]) -> int:
    """Write records as newline-delimited JSON. Returns the row count."""
    count = 0
    for record in records:
        stream.write(json.dumps({key: record[key] for key in RECORD_FIELDS}) + "\n")
        count += 1
    return count


WRITERS = {"csv": write_csv, "jsonl": write_jsonl}
