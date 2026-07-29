# tests/test_discovery_wiring.py
"""The discovery loop end to end, with the provider stubbed.

What ``discover`` has to get right is entirely about *what an empty page means*: the
provider answers ``0`` for a query matching nobody, for one paged past its end, for one
that hit the 10k reach cap, and for a burst artifact that is not an answer at all. The
old walk conflated all four and permanently blacklisted good queries. See §4 and §7 of
``p1-e3-leadfinder-index-semantics-and-query-model-rethink``.
"""
from unittest.mock import patch

import pytest

from openoutreach.core.models import Campaign, Keyword, QueryNode, SiteConfig
from openoutreach.core.pipeline import discover as discover_mod
from openoutreach.core.pipeline import select, vocabulary
from openoutreach.core.pipeline.discover import discover
from openoutreach.crm.models import Deal, DealState, Lead, Outcome
from openoutreach.discovery import Page


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """The empty-at-offset-0 retry is a real 5s wait in production; skip it here."""
    monkeypatch.setattr(discover_mod, "EMPTY_RETRY_DELAY_S", 0)


@pytest.fixture(autouse=True)
def _finder_key(db):
    config = SiteConfig.load()
    config.bettercontact_api_key = "k"
    config.save()


class _Session:
    def __init__(self, campaign):
        self.campaign = campaign


def _campaign(**kw):
    defaults = dict(name="C", product_docs="p", campaign_target="t")
    defaults.update(kw)
    return Campaign.objects.create(**defaults)


def _node(campaign, pairs, **kw):
    node = QueryNode.objects.create(
        campaign=campaign, token_key=select.token_key(pairs), **kw)
    node.keywords.set(Keyword.rows_for(pairs))
    return node


def _row(url="https://linkedin.com/in/a", title="founder", headline="stealth ai startup"):
    return {
        "contact_linkedin_profile_url": url,
        "contact_job_title": title,
        "contact_headline": headline,
        "contact_location_country": "united states",
    }


def _labelled(campaign, profile_text, qualified, source_fields=None):
    lead = Lead.objects.create(
        profile_url=f"https://x/{Lead.objects.count()}", profile_text=profile_text,
        source_fields=source_fields or {})
    Deal.objects.create(
        lead=lead, campaign=campaign,
        state=DealState.QUALIFIED if qualified else DealState.FAILED,
        outcome="" if qualified else Outcome.WRONG_FIT)
    return lead


class TestGates:
    def test_freemium_campaigns_never_search(self, db):
        c = _campaign(is_freemium=True)
        with patch.object(discover_mod, "_fetch") as fetch:
            assert discover(_Session(c)) == 0
        fetch.assert_not_called()

    def test_no_finder_key_is_a_no_op(self, db):
        SiteConfig.objects.update(bettercontact_api_key="")
        with patch.object(discover_mod, "_fetch") as fetch:
            assert discover(_Session(_campaign())) == 0
        fetch.assert_not_called()

    def test_no_icp_text_is_a_no_op(self, db):
        c = _campaign(product_docs="", campaign_target="")
        with patch.object(discover_mod, "_fetch") as fetch:
            assert discover(_Session(c)) == 0
        fetch.assert_not_called()


class TestHarvest:
    def test_a_productive_page_creates_leads_and_advances_the_node(self, db):
        c = _campaign()
        node = _node(c, [("lead_job_title", "founder")])
        page = Page([_row()], 9027)

        with patch.object(discover_mod, "_fetch", return_value=page):
            created = discover(_Session(c))

        assert created == 1
        node.refresh_from_db()
        assert node.state == QueryNode.State.FIRED
        assert node.next_offset == select.DISCOVERY_PAGE_SIZE
        assert node.leads_found == 9027
        assert Lead.objects.get().discovered_by_id == node.pk

    def test_a_page_of_duplicates_is_not_a_stall(self, db):
        # Bug 8: `_harvest` counts *newly created* leads, and a page of already-seen
        # profiles used to read as "nothing left to do" and halt the engine with the
        # frontier wide open. The node must still advance.
        c = _campaign()
        node = _node(c, [("lead_job_title", "founder")])
        Lead.objects.create(profile_url="https://linkedin.com/in/a", profile_text="x")

        with patch.object(discover_mod, "_fetch", return_value=Page([_row()], 10)):
            assert discover(_Session(c)) == 0

        node.refresh_from_db()
        assert node.state == QueryNode.State.FIRED
        assert node.next_offset == select.DISCOVERY_PAGE_SIZE

    def test_source_fields_are_stored_for_the_vocabulary(self, db):
        c = _campaign()
        _node(c, [("lead_job_title", "founder")])
        with patch.object(discover_mod, "_fetch", return_value=Page([_row()], 10)):
            discover(_Session(c))

        stored = Lead.objects.get().source_fields
        assert stored["contact_job_title"] == "founder"
        assert stored["contact_location_country"] == "united states"


class TestEmptyPages:
    def test_offset_zero_retries_before_believing_a_zero(self, db):
        c = _campaign()
        node = _node(c, [("lead_job_title", "founder")])

        with patch.object(discover_mod, "_fetch",
                          side_effect=[Page([], 0), Page([], 0)]) as fetch:
            discover(_Session(c))

        assert fetch.call_count == 2
        node.refresh_from_db()
        assert node.state == QueryNode.State.DEAD

    def test_a_retry_that_finds_rows_keeps_the_node(self, db):
        c = _campaign()
        node = _node(c, [("lead_job_title", "founder")])

        with patch.object(discover_mod, "_fetch",
                          side_effect=[Page([], 0), Page([_row()], 5)]):
            discover(_Session(c))

        node.refresh_from_db()
        assert node.state == QueryNode.State.FRONTIER

    def test_a_positive_count_with_no_rows_is_a_transport_artifact(self, db):
        # §4: a burst answered a 71-million-lead query with an empty page in 0.0s. That
        # is a fact about our call, not about the query — it must never retire a node.
        c = _campaign()
        node = _node(c, [("lead_job_title", "founder")])

        with patch.object(discover_mod, "_fetch",
                          return_value=Page([], 71403396)) as fetch:
            assert discover(_Session(c)) == 0

        assert fetch.call_count == 1  # not even retried — the count already answered
        node.refresh_from_db()
        assert node.state == QueryNode.State.FRONTIER

    def test_an_empty_deep_page_drains_the_vein(self, db):
        c = _campaign()
        node = _node(c, [("lead_job_title", "founder")],
                     state=QueryNode.State.FIRED, next_offset=400)

        with patch.object(discover_mod, "_fetch", return_value=Page([], None)) as fetch:
            discover(_Session(c))

        assert fetch.call_count == 1  # no retry past offset 0 — this is the end, not a zero
        node.refresh_from_db()
        assert node.state == QueryNode.State.DRAINED

    def test_the_loop_tries_the_next_node_after_a_dead_one(self, db):
        c = _campaign()
        _node(c, [("lead_job_title", "dead")])
        _node(c, [("lead_job_title", "live")])

        pages = [Page([], 0), Page([], 0), Page([_row()], 10)]
        with patch.object(discover_mod, "_fetch", side_effect=pages):
            assert discover(_Session(c)) == 1

        assert QueryNode.objects.filter(campaign=c, state=QueryNode.State.DEAD).count() == 1

    def test_saturation_returns_zero(self, db):
        c = _campaign()
        _node(c, [("lead_job_title", "a")], state=QueryNode.State.DRAINED)
        with patch.object(discover_mod, "_fetch") as fetch:
            assert discover(_Session(c)) == 0
        fetch.assert_not_called()


class TestOutage:
    def test_an_outage_leaves_the_node_on_the_frontier(self, db):
        # Bug 7: the old walk called mark_exhausted here, which was final and had no
        # retry path — one hiccup permanently retired a campaign's best query.
        c = _campaign()
        node = _node(c, [("lead_job_title", "founder")])

        with patch.object(discover_mod, "_fetch", return_value=None):
            assert discover(_Session(c)) == 0

        node.refresh_from_db()
        assert node.state == QueryNode.State.FRONTIER


class TestVocabulary:
    def test_admits_a_token_at_document_frequency_two(self, db):
        c = _campaign()
        for _ in range(2):
            _labelled(c, "founder ai", True, {"contact_job_title": "founder ai"})
        _labelled(c, "founder solo", True, {"contact_job_title": "solo"})

        vocabulary.refresh(c)
        admitted = dict(vocabulary.admitted_keywords())

        assert ("lead_job_title", "founder") in vocabulary.admitted_keywords()
        assert ("lead_job_title", "ai") in vocabulary.admitted_keywords()
        # df=1 — the singleton tail is 65% of the vocabulary and mostly company names.
        assert ("lead_job_title", "solo") not in admitted.items()
        assert not Keyword.objects.filter(field="lead_job_title", token="solo").exists()

    def test_a_token_lands_in_the_field_it_came_from(self, db):
        # `cto` is alive in job_title and dead everywhere else; `belgium` the reverse.
        c = _campaign()
        for _ in range(2):
            _labelled(c, "cto belgium", True, {
                "contact_job_title": "cto", "contact_location_country": "belgium"})

        vocabulary.refresh(c)
        pairs = set(vocabulary.admitted_keywords())
        assert ("lead_job_title", "cto") in pairs
        assert ("lead_location", "belgium") in pairs
        assert ("lead_job_title", "belgium") not in pairs

    def test_rejected_leads_contribute_no_vocabulary(self, db):
        c = _campaign()
        for _ in range(3):
            _labelled(c, "plumber", False, {"contact_job_title": "plumber"})
        vocabulary.refresh(c)
        assert not Keyword.objects.filter(token="plumber").exists()

    def test_legacy_leads_without_source_fields_are_skipped(self, db):
        # They still count toward every node's a/b (that reads profile_text); they just
        # cannot say which field one of their words belongs in.
        c = _campaign()
        for _ in range(3):
            _labelled(c, "founder ai", True)
        assert vocabulary.refresh(c) == 0

    def test_seniorities_are_seeded_whole_not_grown(self, db):
        # The one axis whose vocabulary the provider publishes.
        assert vocabulary.seed_seniorities() == 12
        assert vocabulary.seed_seniorities() == 0

    def test_stopwords_never_become_search_terms(self):
        assert "of" not in vocabulary.tokenize("Head of Growth")
        assert vocabulary.tokenize("Head of Growth") == {"head", "growth"}
