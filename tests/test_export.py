# tests/test_export.py
"""The lead export — the finder's public output.

Three things are worth pinning down. The **column names are other people's**: Instantly
and Smartlead require ``email``/``first_name``/``last_name`` and recognise ``company``/
``title``/``website``/``linkedin_url``, so a file this writes imports without mapping,
and a rename here silently breaks that. And **CSV is a flattening of the JSON record**,
never a second schema — both come from ``RECORD_FIELDS``. And it is a **pure database
read**: no GP fit, no LLM call, nothing mutated.
"""
import csv
import io
import json
from unittest.mock import patch

import pytest

from openoutreach.core import export
from openoutreach.crm.models import Company, DealState
from tests.factories import DealFactory, LeadFactory


def _lead(**kwargs):
    defaults = {
        "full_name": "Ada Lovelace",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "job_title": "CTO",
        "email": "ada@acme.com",
        "company": Company.objects.get_or_create(
            key="acme.com", defaults={"name": "Acme", "domain": "acme.com"})[0],
    }
    return LeadFactory(embedded=True, **{**defaults, **kwargs})


def _deal(campaign, reason="fits the ICP", **lead_kwargs):
    return DealFactory(campaign=campaign, lead=_lead(**lead_kwargs),
                       state=DealState.READY_TO_EMAIL, reason=reason)


# ── the record ────────────────────────────────────────────────────


@pytest.mark.django_db
class TestLeadRecord:
    def test_maps_our_columns_onto_the_importers_names(self, campaign):
        deal = _deal(campaign)

        record = export.lead_record(deal)

        assert record["email"] == "ada@acme.com"
        assert (record["first_name"], record["last_name"]) == ("Ada", "Lovelace")
        assert record["company"] == "Acme"      # Company.name, not "company_name"
        assert record["title"] == "CTO"         # Lead.job_title, not "job_title"
        assert record["website"] == "acme.com"  # Company.domain, not "domain"
        assert record["linkedin_url"] == deal.lead.profile_url
        assert record["reason"] == "fits the ICP"
        assert record["lead_id"] == deal.lead.pk

    def test_a_lead_with_no_company_exports_nulls_not_blanks(self, campaign):
        record = export.lead_record(_deal(campaign, company=None))

        assert record["company"] is None and record["website"] is None

    def test_an_unenriched_lead_exports_null_name_parts(self, campaign):
        """A hub-cache hit resolves an address and no identity. Nothing is invented."""
        deal = _deal(campaign, first_name=None, last_name=None)

        record = export.lead_record(deal)

        assert record["first_name"] is None and record["last_name"] is None

    def test_the_record_carries_exactly_the_contract_fields(self, campaign):
        assert set(export.lead_record(_deal(campaign))) == set(export.RECORD_FIELDS)


# ── selection ─────────────────────────────────────────────────────


@pytest.mark.django_db
class TestLeadRecords:
    def test_disqualified_leads_are_left_out_by_default(self, campaign):
        _deal(campaign)
        _deal(campaign, disqualified=True)

        assert len(list(export.lead_records(campaign))) == 1
        assert len(list(export.lead_records(campaign, include_disqualified=True))) == 2

    def test_a_state_filter_narrows_the_export(self, campaign):
        _deal(campaign)
        DealFactory(campaign=campaign, lead=_lead(), state=DealState.QUALIFIED)

        records = list(export.lead_records(campaign, states=[DealState.READY_TO_EMAIL]))

        assert len(records) == 1

    def test_the_export_never_touches_the_qualifier(self, campaign):
        """A read-only export must not fit a GP, and must not spend an LLM call.

        Scoring used to mean ``qualifier_for``, which is an O(n^3) fit over every label
        (minutes on a real campaign) and which calls ``ensure_anchors`` — so a cold
        campaign would have generated anchors, mutating campaign state from an export.
        """
        _deal(campaign)

        with patch("openoutreach.core.ml.qualifier.qualifier_for") as qualifier_for:
            assert len(list(export.lead_records(campaign))) == 1

        qualifier_for.assert_not_called()

    def test_an_unembedded_lead_is_still_exported(self, campaign):
        DealFactory(campaign=campaign, lead=LeadFactory(email="a@b.com"),
                    state=DealState.READY_TO_EMAIL)

        assert len(list(export.lead_records(campaign))) == 1


# ── serialisation ─────────────────────────────────────────────────


@pytest.mark.django_db
class TestWriters:
    def test_csv_headers_are_the_contract_in_order(self, campaign):
        _deal(campaign)
        stream = io.StringIO()

        export.write_csv(export.lead_records(campaign), stream)

        stream.seek(0)
        assert next(csv.reader(stream)) == list(export.RECORD_FIELDS)

    def test_csv_writes_a_missing_field_as_an_empty_cell(self, campaign):
        _deal(campaign, first_name=None)
        stream = io.StringIO()

        export.write_csv(export.lead_records(campaign), stream)

        stream.seek(0)
        assert next(csv.DictReader(stream))["first_name"] == ""

    def test_jsonl_writes_one_object_per_line_keeping_nulls(self, campaign):
        _deal(campaign, first_name=None)
        stream = io.StringIO()

        count = export.write_jsonl(export.lead_records(campaign), stream)

        lines = stream.getvalue().strip().splitlines()
        assert count == 1 and len(lines) == 1
        assert json.loads(lines[0])["first_name"] is None

    def test_both_serialisations_carry_the_same_fields(self, campaign):
        _deal(campaign)
        records = list(export.lead_records(campaign))
        csv_stream, json_stream = io.StringIO(), io.StringIO()

        export.write_csv(records, csv_stream)
        export.write_jsonl(records, json_stream)

        csv_stream.seek(0)
        assert set(next(csv.DictReader(csv_stream))) == set(
            json.loads(json_stream.getvalue().strip()))
