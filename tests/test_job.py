# tests/test_job.py
"""The bounded job: a goal, and the honest end of the work.

Three things carry the weight. **The goal is a delta** — "ten more than you had" is the
only reading under which running it twice gets you twenty. **Progress is a set, not a
subtraction**, so a lead the qualifier rejects mid-run cannot silently cancel out one that
was found. And **the job ends when nothing can advance**, which is the whole reason there
is no timeout: every wait that matters is already written on the row that is waiting.
"""
from unittest.mock import patch

import pytest

from openoutreach.core import job
from openoutreach.core.errors import ErrorType
from openoutreach.core.job import EMAILS, LEADS, Goal, run_job
from openoutreach.crm.models import DealState
from tests.factories import DealFactory, LeadFactory


def _exportable(campaign, email=None):
    """One lead the export would write — judged and accepted."""
    return DealFactory(campaign=campaign, lead=LeadFactory(email=email),
                       state=DealState.RESOLVED, reason="fits the ICP")


def _finds(campaign, per_action=1, email=None):
    """A `run_one_action` that produces leads, so a goal can actually be reached."""
    def action(_campaign, buy_addresses=True):
        for _ in range(per_action):
            _exportable(campaign, email=email)
        return True
    return patch("openoutreach.core.cycle.run_one_action", side_effect=action)


# ── reaching the goal ─────────────────────────────────────────────


@pytest.mark.django_db
class TestReachingTheGoal:
    def test_it_stops_as_soon_as_the_goal_is_met(self, campaign):
        with _finds(campaign) as action:
            result = run_job(campaign, Goal(3))

        assert result.reached and result.produced == 3
        assert action.call_count == 3

    def test_zero_does_no_work_and_is_already_met(self, campaign):
        """`find 0` is the "print what I have" case, and it falls out of the predicate
        rather than being a second verb."""
        with patch("openoutreach.core.cycle.run_one_action") as action:
            result = run_job(campaign, Goal(0))

        assert result.reached and result.produced == 0
        action.assert_not_called()

    def test_the_goal_is_a_delta_not_a_total(self, campaign):
        """Leads that were already there do not count toward the next ten."""
        _exportable(campaign)
        _exportable(campaign)

        with _finds(campaign) as action:
            result = run_job(campaign, Goal(2))

        assert result.produced == 2
        assert action.call_count == 2

    def test_a_rejection_cannot_cancel_out_a_find(self, campaign):
        """Progress is the set that *entered* the goal. Counting by subtraction would
        report zero here, having found a lead and lost an unrelated one."""
        doomed = _exportable(campaign)

        def find_one_lose_one(_campaign, buy_addresses=True):
            _exportable(campaign)
            doomed.state = DealState.FAILED
            doomed.outcome = "wrong_fit"
            doomed.save()
            return True

        with patch("openoutreach.core.cycle.run_one_action", side_effect=find_one_lose_one):
            result = run_job(campaign, Goal(1))

        assert result.reached and result.produced == 1


# ── the units are different sets ──────────────────────────────────


@pytest.mark.django_db
class TestUnits:
    def test_leads_counts_rows_without_an_address(self, campaign):
        """Exportable is not mailable: an address is an enrichment, never a
        precondition."""
        with _finds(campaign, email=None):
            result = run_job(campaign, Goal(2, LEADS))

        assert result.reached and result.produced == 2

    def test_emails_counts_only_the_rows_that_carry_one(self, campaign):
        """A lead already exportable that merely *gains* an address counts toward an
        `emails` goal — which is why progress is a set per unit and not a timestamp on
        the row."""
        deal = _exportable(campaign, email=None)

        def resolve(_campaign, buy_addresses=True):
            deal.lead.email = "ada@acme.com"
            deal.lead.save()
            return True

        with patch("openoutreach.core.cycle.run_one_action", side_effect=resolve):
            result = run_job(campaign, Goal(1, EMAILS))

        assert result.reached and result.produced_ids == [deal.lead.pk]

    def test_an_emails_goal_is_not_met_by_addressless_leads(self, campaign):
        with _finds(campaign, email=None):
            with patch("openoutreach.core.cycle.run_one_action") as action:
                action.side_effect = [True, False]
                result = run_job(campaign, Goal(1, EMAILS))

        assert not result.reached and result.produced == 0


# ── stopping short ────────────────────────────────────────────────


@pytest.mark.django_db
class TestStoppingShort:
    def test_an_idle_cycle_ends_the_job_rather_than_spinning(self, campaign):
        """There is no timeout because there is nothing to time out: when no row can
        act, more waiting cannot change that — the waits live on the rows."""
        with patch("openoutreach.core.cycle.run_one_action", return_value=False):
            result = run_job(campaign, Goal(10))

        assert not result.reached
        assert result.stopped_because == ErrorType.GOAL_UNREACHED

    def test_the_reason_says_what_it_is_short_by_and_what_it_waits_on(self, campaign):
        """*Nothing may be reported as an empty result*: a drained index and three
        addresses on order are a dead end and a reason to run again in an hour."""
        DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.FINDING_EMAIL,
                    lookup_request_id="req1")

        with patch("openoutreach.core.cycle.run_one_action", return_value=False):
            result = run_job(campaign, Goal(10))

        assert "0 of 10 leads" in result.detail
        assert "address on order" in result.detail

    def test_work_done_before_stopping_is_still_reported(self, campaign):
        """Seven leads are seven leads, and the caller gets both the count and the rows."""
        acted = []

        def once(_campaign, buy_addresses=True):
            if acted:
                return False
            acted.append(True)
            _exportable(campaign)
            return True

        with patch("openoutreach.core.cycle.run_one_action", side_effect=once):
            result = run_job(campaign, Goal(10))

        assert not result.reached and result.produced == 1

    def test_a_halting_error_ends_the_job_with_an_answer(self, campaign):
        """A bad LLM key is not transient — every action would raise it, so retrying is
        a way of failing slowly."""
        from pydantic_ai.exceptions import ModelHTTPError

        with patch("openoutreach.core.cycle.run_one_action",
                   side_effect=ModelHTTPError(status_code=401, model_name="m", body=None)):
            result = run_job(campaign, Goal(10))

        assert result.stopped_because == ErrorType.BAD_CONFIG
        assert "llm_api_key" in result.detail

    def test_ctrl_c_hands_back_the_rows_not_a_stack_trace(self, campaign):
        """The operator's own deadline, for the one case with no natural bound: a
        campaign whose leads are all rejected keeps finding, keeps rejecting, and every
        row honestly reports that it acted."""
        def find_then_interrupt(_campaign, buy_addresses=True):
            if not campaign.deals.exists():
                _exportable(campaign)
                return True
            raise KeyboardInterrupt

        with patch("openoutreach.core.cycle.run_one_action", side_effect=find_then_interrupt):
            result = run_job(campaign, Goal(10))

        assert not result.reached and result.produced == 1
        assert "interrupted" in result.detail


# ── watching leads land ───────────────────────────────────────────


@pytest.mark.django_db
def test_each_new_lead_is_announced_once_as_it_lands(campaign):
    """What `--open` rides on: a profile goes in front of the operator while the job is
    still running, not in a burst at the end, and never twice."""
    seen = []

    with _finds(campaign):
        result = run_job(campaign, Goal(3), on_new_lead=seen.append)

    assert [lead.pk for lead in seen] == result.produced_ids
    assert len(set(lead.pk for lead in seen)) == 3


@pytest.mark.django_db
def test_a_lead_already_there_is_never_announced(campaign):
    """It is not news, and opening a tab for it would be a lie about what just happened."""
    old = _exportable(campaign)
    seen = []

    with _finds(campaign):
        run_job(campaign, Goal(1), on_new_lead=seen.append)

    assert old.lead.pk not in [lead.pk for lead in seen]


@pytest.mark.django_db
def test_the_unit_helper_reads_the_export_not_a_state(campaign):
    """`status` and a goal must agree on what "ten leads" means, so both count the rows
    the export would actually write."""
    _exportable(campaign, email="ada@acme.com")
    DealFactory(campaign=campaign, lead=LeadFactory(email="no@acme.com"),
                state=DealState.FAILED, outcome="wrong_fit", reason="no fit")

    assert len(job._unit_ids(campaign, LEADS)) == 1
    assert len(job._unit_ids(campaign, EMAILS)) == 1
