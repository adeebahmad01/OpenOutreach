# tests/test_cycle.py
"""The cycle: the hierarchy, the ``not_before`` gate, and the spend condition.

The test that matters most is ``test_a_stalled_lookup_does_not_hold_up_a_send`` —
it is the 2026-08-05 incident written down. A deal's timestamp can gate that deal
and nothing else, which is the whole reason the task queue is gone.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from pydantic_ai.exceptions import ModelHTTPError

from openoutreach.core import cycle
from openoutreach.crm.models import DealState
from openoutreach.emails.models import Mailbox
from tests.emails import maillog
from tests.factories import DealFactory, LeadFactory


def _box(daily_limit=10) -> Mailbox:
    return maillog.mailbox("s@infra.com", daily_limit=daily_limit)


def _deal(campaign, state, **kwargs):
    lead_kwargs = {"email": kwargs.pop("email", None)}
    return DealFactory(
        campaign=campaign, lead=LeadFactory(**lead_kwargs), state=state, **kwargs)


@pytest.fixture
def steps():
    """Every step stubbed, so a test asserts *which* one the cycle chose."""
    with patch("openoutreach.emails.steps.lookup.check_lookup",
               return_value=None) as check, \
            patch("openoutreach.emails.steps.reply.answer_reply",
                  return_value=None) as reply, \
            patch("openoutreach.emails.steps.send.send_first_email",
                  return_value=DealState.EMAILED) as send, \
            patch("openoutreach.core.pipeline.ready_pool.promote_to_ready",
                  return_value=0) as score, \
            patch("openoutreach.core.ml.qualifier.qualifier_for",
                  return_value=object()), \
            patch("openoutreach.emails.steps.lookup.buy_address",
                  return_value=None) as buy, \
            patch("openoutreach.core.pipeline.top_up.top_up",
                  return_value=False) as fill:
        yield {"check": check, "reply": reply, "send": send,
               "score": score, "buy": buy, "top_up": fill}


def _called(steps):
    return {name for name, mock in steps.items() if mock.called}


# ── The hierarchy ─────────────────────────────────────────────────


@pytest.mark.django_db
class TestPriority:
    def test_an_in_flight_lookup_outranks_everything(self, campaign, steps):
        _box()
        _deal(campaign, DealState.FINDING_EMAIL, lookup_request_id="req1")
        _deal(campaign, DealState.READY_TO_EMAIL, email="a@corp.com")

        assert cycle.run_one_action(campaign) is True
        assert _called(steps) == {"check"}

    def test_a_lookup_with_no_job_handle_is_reclaimed_not_stranded(self, campaign, steps):
        """Measured on a live install: two deals sat at FINDING_EMAIL with an empty
        ``request_id`` for 206 hours — the poll row skipped them and no other row
        claims that state, while both kept counting against the day's headroom."""
        _box()
        deal = _deal(campaign, DealState.FINDING_EMAIL, lookup_request_id="")

        assert cycle.run_one_action(campaign) is True
        deal.refresh_from_db()
        assert deal.state == DealState.READY_TO_FIND_EMAIL
        assert "check" not in _called(steps)

    def test_a_reply_outranks_a_first_email(self, campaign, steps):
        """Someone who wrote back is owed an answer before a stranger is contacted."""
        box = _box()
        sent = maillog.outbound(box, to="p@corp.com",
                                sent_at=timezone.now() - timedelta(hours=1))
        _deal(campaign, DealState.EMAILED, mailbox=box, thread=sent.thread,
              email="p@corp.com")
        maillog.inbound(box, thread=sent.thread, sender="p@corp.com")
        _deal(campaign, DealState.READY_TO_EMAIL, email="a@corp.com")

        cycle.run_one_action(campaign)
        assert _called(steps) == {"reply"}

    def test_a_first_email_outranks_buying_another_address(self, campaign, steps):
        _box()
        _deal(campaign, DealState.READY_TO_EMAIL, email="a@corp.com")
        _deal(campaign, DealState.READY_TO_FIND_EMAIL)

        cycle.run_one_action(campaign)
        assert _called(steps) == {"send"}

    def test_topping_up_is_the_last_resort(self, campaign, steps):
        _box()
        cycle.run_one_action(campaign)
        assert _called(steps) == {"top_up"}

    def test_an_idle_campaign_with_nothing_to_do_says_so(self, campaign, steps):
        _box()
        assert cycle.run_one_action(campaign) is False


# ── not_before ────────────────────────────────────────────────────


@pytest.mark.django_db
class TestNotBefore:
    def test_a_deal_told_to_wait_is_not_served(self, campaign, steps):
        _box()
        _deal(campaign, DealState.FINDING_EMAIL, lookup_request_id="req1",
              not_before=timezone.now() + timedelta(hours=1))

        cycle.run_one_action(campaign)
        assert "check" not in _called(steps)

    def test_a_deal_whose_wait_has_elapsed_is_served(self, campaign, steps):
        _box()
        _deal(campaign, DealState.FINDING_EMAIL, lookup_request_id="req1",
              not_before=timezone.now() - timedelta(seconds=1))

        cycle.run_one_action(campaign)
        assert "check" in _called(steps)

    def test_a_stalled_lookup_does_not_hold_up_a_send(self, campaign, steps):
        """**The 2026-08-05 incident.** Two lookups had backed off 45 hours; they
        were the only rows in the queue, so the daemon slept 34 hours while 55
        ready deals sat with 70 sends of headroom. A timestamp now gates its own
        row and nothing else."""
        _box(daily_limit=70)
        for i in range(2):
            _deal(campaign, DealState.FINDING_EMAIL, lookup_request_id=f"req{i}",
                  not_before=timezone.now() + timedelta(hours=45))
        _deal(campaign, DealState.READY_TO_EMAIL, email="ready@corp.com")

        assert cycle.run_one_action(campaign) is True
        assert _called(steps) == {"send"}


# ── Finding leads without a mailbox ───────────────────────────────


@pytest.mark.django_db
class TestTheFinderRunsWithoutAMailbox:
    """The pivot, in tests: the product finds leads, and sending is a leg it may not have.

    These replace ``TestRoomToSendToday``. That gate — *never buy an address, and
    never qualify a lead, for someone there is no room to email today* — was correct
    while every lead ended in a send. It also meant an install with no ``Mailbox``
    row had zero pool headroom, so discovery and qualification never ran at all: the
    daemon looked alive and produced nothing, with no error and no log line saying
    why. That silence is what these tests exist to break.
    """

    def test_discovery_and_qualification_run_with_no_mailbox_at_all(
            self, campaign, steps):
        """The one that would have failed before: no boxes, and top-up still fires."""
        assert Mailbox.objects.count() == 0

        cycle.run_one_action(campaign)
        assert "top_up" in _called(steps)

    def test_a_full_send_queue_no_longer_stops_qualifying(self, campaign, steps):
        """A backed-up mailbox says nothing about whether the next lead is worth
        finding — the leads are the product, and they leave over a CSV."""
        _box(daily_limit=1)
        _deal(campaign, DealState.READY_TO_EMAIL, email="a@corp.com")
        # Box spaced out, so the send row cannot fire and hide the point.
        Mailbox.objects.update(next_send_at=timezone.now() + timedelta(minutes=5))

        cycle.run_one_action(campaign)
        assert "top_up" in _called(steps)

    def test_addresses_are_bought_with_no_mailbox(self, campaign, steps):
        """Enrichment is the finder's own leg: a resolved address is a column in the
        export, not a prerequisite for a send that may never happen."""
        _deal(campaign, DealState.READY_TO_FIND_EMAIL)

        with patch("openoutreach.emails.bettercontact.is_configured",
                   return_value=True):
            assert cycle.run_one_action(campaign) is True
        assert _called(steps) == {"buy"}

    def test_no_finder_key_means_no_buying(self, campaign, steps):
        """The one gate left on the paid row, and it is about the provider, not the
        pipeline: with no key there is nobody to submit the job to."""
        _deal(campaign, DealState.READY_TO_FIND_EMAIL)

        with patch("openoutreach.emails.bettercontact.is_configured",
                   return_value=False):
            cycle.run_one_action(campaign)
        assert "buy" not in _called(steps)


# ── Failure handling ──────────────────────────────────────────────


@pytest.mark.django_db
class TestFailures:
    def test_an_ordinary_failure_leaves_the_row_untouched(self, campaign):
        """The cycle's try/except is a bug backstop: log, skip, keep going."""
        _box()
        deal = _deal(campaign, DealState.FINDING_EMAIL, lookup_request_id="req1")

        with patch("openoutreach.emails.steps.lookup.check_lookup",
                   side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                cycle.run_one_action(campaign)

        deal.refresh_from_db()
        assert deal.state == DealState.FINDING_EMAIL
        assert deal.not_before is None

    def test_a_halting_error_is_not_swallowed(self, campaign):
        """A bad LLM key must stop the daemon loudly, or it retries every five
        seconds forever while looking alive."""
        assert ModelHTTPError in cycle.HALTING_ERRORS


# ── Scoring is not repeated for nothing ───────────────────────────


@pytest.mark.django_db
class TestScoringIsSkippedWhenNothingMoved:
    """Fitting the GP dominates the cost of using it (~1.1s at 300 labels, against
    a 5s cycle), and scoring the same pool with the same labels cannot promote
    anybody — so an unchanged campaign must not rebuild the model at all."""

    @pytest.fixture(autouse=True)
    def _clear(self):
        cycle._scored_at.clear()
        yield
        cycle._scored_at.clear()

    def _score_twice(self, campaign, between=None):
        _box()
        _deal(campaign, DealState.QUALIFIED)
        with patch("openoutreach.core.ml.qualifier.qualifier_for",
                   return_value=object()) as build, \
                patch("openoutreach.core.pipeline.ready_pool.promote_to_ready",
                      return_value=0):
            cycle._score_qualified(campaign)
            if between:
                between(campaign)
            cycle._score_qualified(campaign)
        return build

    def test_an_unchanged_pool_is_not_rescored(self, campaign):
        assert self._score_twice(campaign).call_count == 1

    def test_a_new_lead_reopens_scoring(self, campaign):
        build = self._score_twice(
            campaign, between=lambda c: _deal(c, DealState.QUALIFIED))
        assert build.call_count == 2

    def test_a_new_verdict_reopens_scoring(self, campaign):
        """A label the GP has not seen changes what it would say."""
        build = self._score_twice(
            campaign, between=lambda c: _deal(c, DealState.FAILED))
        assert build.call_count == 2

    def test_an_empty_pool_never_builds_the_model(self, campaign):
        _box()
        with patch("openoutreach.core.ml.qualifier.qualifier_for") as build:
            assert cycle._score_qualified(campaign) is False
        build.assert_not_called()
