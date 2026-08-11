# tests/emails/test_lookup.py
"""The paid lookup, in two steps: buy the address, then check on it.

The backoff is the part worth pinning down. It lives on the deal now
(``not_before`` + ``lookup_attempt``), so a job that never terminates delays that
one lead and nothing else — the same backoff in a shared queue row once put a live
install to sleep for 34 hours.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutreach.crm.models import DealState
from openoutreach.emails.bettercontact import BetterContactUnavailable, PollOutcome
from openoutreach.emails.steps.lookup import buy_address, check_lookup
from tests.factories import DealFactory, LeadFactory


def _ready_to_find(campaign, email=None):
    return DealFactory(
        campaign=campaign,
        lead=LeadFactory(email=email),
        state=DealState.READY_TO_FIND_EMAIL,
    )


def _in_flight(campaign, attempt=0, request_id="req1"):
    return DealFactory(
        campaign=campaign,
        lead=LeadFactory(),
        state=DealState.FINDING_EMAIL,
        lookup_request_id=request_id,
        lookup_attempt=attempt,
    )


# ── buy_address ───────────────────────────────────────────────────


@pytest.mark.django_db
class TestBuyAddress:
    def test_a_known_address_skips_the_hub_and_the_provider(self, campaign):
        deal = _ready_to_find(campaign, email="known@corp.com")

        with patch("openoutreach.contacts.service.resolve") as resolve, \
                patch("openoutreach.emails.bettercontact.submit") as submit:
            assert buy_address(deal) == DealState.READY_TO_EMAIL

        resolve.assert_not_called()
        submit.assert_not_called()

    def test_a_hub_hit_skips_the_paid_job(self, campaign):
        deal = _ready_to_find(campaign)

        with patch("openoutreach.contacts.service.resolve", return_value="hub@corp.com"), \
                patch("openoutreach.emails.bettercontact.submit") as submit:
            assert buy_address(deal) == DealState.READY_TO_EMAIL

        submit.assert_not_called()
        deal.lead.refresh_from_db()
        assert deal.lead.email == "hub@corp.com"

    def test_a_hub_miss_submits_and_parks_on_the_handle(self, campaign):
        deal = _ready_to_find(campaign)

        with patch("openoutreach.contacts.service.resolve", return_value=None), \
                patch("openoutreach.emails.bettercontact.is_configured", return_value=True), \
                patch("openoutreach.emails.bettercontact.submit", return_value="req-42"):
            assert buy_address(deal) == DealState.FINDING_EMAIL

        assert deal.lookup_request_id == "req-42"
        assert deal.lookup_attempt == 0
        assert deal.not_before > timezone.now()

    def test_an_unconfigured_finder_leaves_the_deal_queued(self, campaign):
        deal = _ready_to_find(campaign)

        with patch("openoutreach.contacts.service.resolve", return_value=None), \
                patch("openoutreach.emails.bettercontact.is_configured", return_value=False), \
                patch("openoutreach.emails.bettercontact.submit") as submit:
            assert buy_address(deal) is None

        submit.assert_not_called()
        assert deal.lookup_request_id == ""

    def test_an_outage_spends_no_credit_and_leaves_it_queued(self, campaign):
        """No handle exists to poll, so the next cycle simply tries again."""
        deal = _ready_to_find(campaign)

        with patch("openoutreach.contacts.service.resolve", return_value=None), \
                patch("openoutreach.emails.bettercontact.is_configured", return_value=True), \
                patch("openoutreach.emails.bettercontact.submit",
                      side_effect=BetterContactUnavailable("503")):
            assert buy_address(deal) is None

        assert deal.lookup_request_id == ""


# ── check_lookup ──────────────────────────────────────────────────


@pytest.mark.django_db
class TestCheckLookup:
    def test_a_hit_stores_the_address_and_gives_it_back(self, campaign):
        deal = _in_flight(campaign)

        with patch("openoutreach.emails.bettercontact.poll_once",
                   return_value=PollOutcome(running=False, email="found@corp.com")), \
                patch("openoutreach.contacts.service.contribute") as contribute:
            assert check_lookup(deal) == DealState.READY_TO_EMAIL

        deal.lead.refresh_from_db()
        assert deal.lead.email == "found@corp.com"
        contribute.assert_called_once()
        assert deal.not_before is None
        assert deal.lookup_request_id == ""

    def test_a_miss_is_its_own_terminal(self, campaign):
        """Reachability failed, not fit — the ML labeler keeps the lead positive."""
        deal = _in_flight(campaign)

        with patch("openoutreach.emails.bettercontact.poll_once",
                   return_value=PollOutcome(running=False, email="")):
            assert check_lookup(deal) == DealState.NO_EMAIL_BETTERCONTACT

    def test_a_running_job_backs_off_on_its_own_row(self, campaign):
        deal = _in_flight(campaign, attempt=0)

        with patch("openoutreach.emails.bettercontact.poll_once",
                   return_value=PollOutcome(running=True)):
            assert check_lookup(deal) is None

        assert deal.lookup_attempt == 1
        assert deal.not_before > timezone.now()

    def test_the_backoff_doubles_into_days(self, campaign):
        deal = _in_flight(campaign, attempt=15)

        with patch("openoutreach.emails.bettercontact.poll_once",
                   return_value=PollOutcome(running=True)):
            check_lookup(deal)

        assert deal.not_before - timezone.now() > timedelta(days=1)

    def test_an_extreme_attempt_count_stays_representable(self, campaign):
        """The rail exists so ``datetime`` can still express the schedule."""
        deal = _in_flight(campaign, attempt=200)

        with patch("openoutreach.emails.bettercontact.poll_once",
                   return_value=PollOutcome(running=True)):
            assert check_lookup(deal) is None

        assert deal.not_before is not None

    def test_a_provider_outage_retries_at_the_same_interval(self, campaign):
        """Nothing was learned about the job, so the backoff must not advance."""
        deal = _in_flight(campaign, attempt=3)

        with patch("openoutreach.emails.bettercontact.poll_once",
                   side_effect=BetterContactUnavailable("503")):
            assert check_lookup(deal) is None

        assert deal.lookup_attempt == 3
        assert deal.not_before > timezone.now()

    def test_a_stalled_job_is_never_abandoned(self, campaign):
        """Abandoning reverted the deal and bought a *second* job for the same lead."""
        deal = _in_flight(campaign, attempt=40)

        with patch("openoutreach.emails.bettercontact.poll_once",
                   return_value=PollOutcome(running=True)):
            assert check_lookup(deal) is None

        assert deal.lookup_request_id == "req1"
