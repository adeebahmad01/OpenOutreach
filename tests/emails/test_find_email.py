# tests/emails/test_find_email.py
"""The two-leg paid email lookup: find_email (submit) → collect_email (poll).

Submit resolves free-hub-first, else fires a provider job and parks the deal at
FINDING_EMAIL with a bound collect task carrying the request_id. Collect polls
that job once and routes hit → READY_TO_EMAIL, miss → NO_EMAIL_BETTERCONTACT,
still-running → chained backoff, doubling without deadline or attempt limit.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutreach.core.conf import COLLECT_BACKOFF_BASE_S, COLLECT_BACKOFF_MAX_S
from openoutreach.core.models import Task
from openoutreach.crm.models import DealState
from openoutreach.emails.bettercontact import BetterContactUnavailable, PollOutcome
from openoutreach.core.scheduler import flush_find_email_queue
from openoutreach.emails.models import Mailbox
from openoutreach.emails.tasks.collect_email import handle_collect_email
from openoutreach.emails.tasks.find_email import handle_find_email
from tests.factories import DealFactory, LeadFactory


# Allowance high enough never to bind — these cases exercise the *other*
# bounds (pool headroom, pending guard). The quota split has its own tests.
_UNCAPPED = 10_000

pytestmark = pytest.mark.django_db


def _box(daily_limit=10):
    return Mailbox.objects.create(
        username="a@b.com", password="pw", from_address="a@b.com", daily_limit=daily_limit,
    )


def _deal(campaign, state, email=None):
    return DealFactory(campaign=campaign, lead=LeadFactory(email=email), state=state)


def _collect_tasks(attempt=None):
    qs = Task.objects.filter(task_type=Task.TaskType.COLLECT_EMAIL)
    return qs.filter(payload__attempt=attempt) if attempt is not None else qs


def _email_tasks(campaign):
    return Task.objects.filter(task_type=Task.TaskType.EMAIL, payload__campaign_id=campaign.pk)


# ── Submit leg (handle_find_email) ────────────────────────────────────


class TestSubmitLeg:
    def _run(self, session, candidate_url, resolve=None, submit_ret="req1", submit_exc=None):
        cand = {"profile_url": candidate_url} if candidate_url else None
        submit = patch("openoutreach.emails.bettercontact.submit",
                       side_effect=submit_exc, return_value=submit_ret)
        with patch("openoutreach.emails.tasks.find_email._select_candidate", return_value=cand), \
                patch("openoutreach.contacts.service.resolve", return_value=resolve), \
                patch("openoutreach.emails.bettercontact.is_configured", return_value=True), \
                submit as submit_mock:
            task = Task.objects.create(
                task_type=Task.TaskType.FIND_EMAIL,
                scheduled_at=timezone.now(),
                payload={"campaign_id": session.campaign.pk},
            )
            handle_find_email(task, session, qualifiers={})
        return submit_mock

    def test_known_email_routes_to_ready_without_hub_or_submit(self, fake_session):
        _box()
        deal = _deal(fake_session.campaign, DealState.READY_TO_FIND_EMAIL, email="known@acme.com")
        with patch("openoutreach.contacts.service.resolve") as resolve:
            submit = self._run(fake_session, deal.lead.profile_url)

        submit.assert_not_called()
        resolve.assert_not_called()  # own DB checked before the hub round-trip
        deal.refresh_from_db()
        assert deal.state == DealState.READY_TO_EMAIL
        assert deal.lead.email == "known@acme.com"
        assert _email_tasks(fake_session.campaign).count() == 1  # opener queued
        assert not _collect_tasks().exists()

    def test_hub_hit_routes_to_ready_to_email_without_submit(self, fake_session):
        _box()
        deal = _deal(fake_session.campaign, DealState.READY_TO_FIND_EMAIL)
        submit = self._run(fake_session, deal.lead.profile_url, resolve="hub@acme.com")

        submit.assert_not_called()
        deal.refresh_from_db()
        assert deal.state == DealState.READY_TO_EMAIL
        assert deal.lead.email == "hub@acme.com"
        assert _email_tasks(fake_session.campaign).count() == 1  # opener queued
        assert not _collect_tasks().exists()

    def test_hub_miss_submits_and_parks_finding_email(self, fake_session):
        _box()
        deal = _deal(fake_session.campaign, DealState.READY_TO_FIND_EMAIL)
        self._run(fake_session, deal.lead.profile_url, resolve=None, submit_ret="req1")

        deal.refresh_from_db()
        assert deal.state == DealState.FINDING_EMAIL
        poll = _collect_tasks().get()
        assert poll.payload["request_id"] == "req1"
        assert poll.payload["deal_id"] == deal.pk
        assert poll.payload["provider"] == "bettercontact"
        assert poll.payload["attempt"] == 0

    def test_submit_unavailable_leaves_ready_to_find_email(self, fake_session):
        _box()
        deal = _deal(fake_session.campaign, DealState.READY_TO_FIND_EMAIL)
        self._run(fake_session, deal.lead.profile_url, resolve=None,
                  submit_exc=BetterContactUnavailable("no key"))

        deal.refresh_from_db()
        assert deal.state == DealState.READY_TO_FIND_EMAIL
        assert not _collect_tasks().exists()

    def test_no_mailbox_is_idle(self, fake_session):
        deal = _deal(fake_session.campaign, DealState.READY_TO_FIND_EMAIL)
        self._run(fake_session, deal.lead.profile_url, resolve="hub@acme.com")
        deal.refresh_from_db()
        assert deal.state == DealState.READY_TO_FIND_EMAIL

    def test_no_candidate_is_noop(self, fake_session):
        _box()
        self._run(fake_session, candidate_url=None)
        assert not _collect_tasks().exists()
        assert not _email_tasks(fake_session.campaign).exists()


# ── Collect leg (handle_collect_email) ────────────────────────────────


class TestCollectLeg:
    def _task(self, session, deal, attempt=0, age_s=0):
        submitted = timezone.now() - timedelta(seconds=age_s)
        return Task.objects.create(
            task_type=Task.TaskType.COLLECT_EMAIL,
            scheduled_at=timezone.now(),
            payload={
                "campaign_id": session.campaign.pk,
                "deal_id": deal.pk,
                "provider": "bettercontact",
                "request_id": "req1",
                "submitted_at": submitted.isoformat(),
                "attempt": attempt,
            },
        )

    def _run(self, session, task, outcome=None, exc=None):
        with patch("openoutreach.emails.bettercontact.poll_once",
                   side_effect=exc, return_value=outcome) as poll:
            handle_collect_email(task, session, qualifiers={})
        return poll

    def test_hit_resolves_and_routes_to_send(self, fake_session):
        _box()
        deal = _deal(fake_session.campaign, DealState.FINDING_EMAIL)
        task = self._task(fake_session, deal)
        with patch("openoutreach.contacts.service.contribute") as contribute:
            self._run(fake_session, task, outcome=PollOutcome(running=False, email="bob@acme.com"))

        deal.refresh_from_db()
        assert deal.state == DealState.READY_TO_EMAIL
        assert deal.lead.email == "bob@acme.com"
        contribute.assert_called_once()
        assert _email_tasks(fake_session.campaign).count() == 1

    def test_miss_parks_no_email_state(self, fake_session):
        deal = _deal(fake_session.campaign, DealState.FINDING_EMAIL)
        task = self._task(fake_session, deal)
        self._run(fake_session, task, outcome=PollOutcome(running=False, email=""))

        deal.refresh_from_db()
        assert deal.state == DealState.NO_EMAIL_BETTERCONTACT

    def test_running_before_deadline_chains_next_poll(self, fake_session):
        deal = _deal(fake_session.campaign, DealState.FINDING_EMAIL)
        task = self._task(fake_session, deal, attempt=0, age_s=1)
        self._run(fake_session, task, outcome=PollOutcome(running=True))

        deal.refresh_from_db()
        assert deal.state == DealState.FINDING_EMAIL
        nxt = _collect_tasks(attempt=1).get()
        assert nxt.scheduled_at > timezone.now()  # backed off into the future

    def test_a_long_running_job_keeps_its_deal_and_keeps_polling(self, fake_session):
        """No deadline: an unterminated job is queued, not lost. Abandoning it sent
        the deal back to the pool, where the submit leg bought a *second* job for
        the same lead — a hot resubmit loop against an already-struggling provider."""
        deal = _deal(fake_session.campaign, DealState.FINDING_EMAIL)
        task = self._task(fake_session, deal, attempt=40, age_s=14 * 86400)
        self._run(fake_session, task, outcome=PollOutcome(running=True))

        deal.refresh_from_db()
        assert deal.state == DealState.FINDING_EMAIL  # never re-selected, never written off
        assert _collect_tasks(attempt=41).count() == 1

    def test_backoff_doubles_into_days(self, fake_session):
        """Doubling is what makes waiting cheap — a week of outage costs ~17 polls,
        where a minute-capped backoff would cost ten thousand."""
        deal = _deal(fake_session.campaign, DealState.FINDING_EMAIL)
        task = self._task(fake_session, deal, attempt=16)
        before = timezone.now()
        self._run(fake_session, task, outcome=PollOutcome(running=True))

        nxt = _collect_tasks(attempt=17).get()
        assert (nxt.scheduled_at - before).total_seconds() == pytest.approx(
            COLLECT_BACKOFF_BASE_S * 2 ** 17, rel=0.01)  # ~7.6 days

    def test_an_extreme_attempt_count_still_mints_its_successor(self, fake_session):
        """The rail is there so the schedule stays representable: uncapped, the
        delay overflows ``datetime``, the handler dies before chaining, and the
        deal is stranded at FINDING_EMAIL with no pending task at all."""
        deal = _deal(fake_session.campaign, DealState.FINDING_EMAIL)
        task = self._task(fake_session, deal, attempt=400)
        before = timezone.now()
        self._run(fake_session, task, outcome=PollOutcome(running=True))

        nxt = _collect_tasks(attempt=401).get()
        assert (nxt.scheduled_at - before).total_seconds() == pytest.approx(
            COLLECT_BACKOFF_MAX_S, rel=0.01)

    def test_unavailable_retries_same_attempt(self, fake_session):
        deal = _deal(fake_session.campaign, DealState.FINDING_EMAIL)
        task = self._task(fake_session, deal, attempt=2, age_s=1)
        self._run(fake_session, task, exc=BetterContactUnavailable("down"))

        deal.refresh_from_db()
        assert deal.state == DealState.FINDING_EMAIL
        assert _collect_tasks(attempt=2).count() == 2  # original + retry, attempt not advanced

    def test_stale_deal_drops_poll(self, fake_session):
        deal = _deal(fake_session.campaign, DealState.READY_TO_EMAIL)  # no longer FINDING_EMAIL
        task = self._task(fake_session, deal)
        poll = self._run(fake_session, task, outcome=PollOutcome(running=True))

        poll.assert_not_called()
        assert _collect_tasks(attempt=1).count() == 0


# ── Submit drain (flush_find_email_queue) — spend rides on send headroom ──


class TestFindEmailDrain:
    def _find_email_tasks(self, campaign):
        return Task.objects.filter(
            task_type=Task.TaskType.FIND_EMAIL, payload__campaign_id=campaign.pk,
        )

    def _flush(self, session, configured=True):
        with patch("openoutreach.emails.bettercontact.is_configured", return_value=configured):
            return flush_find_email_queue(session, session.campaign, allowance=_UNCAPPED)

    def test_no_op_without_mailbox(self, fake_session):
        assert self._flush(fake_session) == 0

    def test_no_op_when_finder_unconfigured(self, fake_session):
        _box()
        assert self._flush(fake_session, configured=False) == 0

    def test_mints_one_slot_with_send_headroom(self, fake_session):
        _box(daily_limit=10)  # 10 sends free today, pipeline empty
        assert self._flush(fake_session) == 1
        assert self._find_email_tasks(fake_session.campaign).count() == 1

    def _polling_in(self, session, deal, seconds):
        """An in-flight lookup whose next poll is *seconds* away."""
        return Task.objects.create(
            task_type=Task.TaskType.COLLECT_EMAIL,
            scheduled_at=timezone.now() + timedelta(seconds=seconds),
            payload={
                "campaign_id": session.campaign.pk, "deal_id": deal.pk,
                "provider": "bettercontact", "request_id": f"req-{deal.pk}",
                "submitted_at": timezone.now().isoformat(), "attempt": 0,
            },
        )

    def test_no_op_when_pipeline_fills_headroom(self, fake_session):
        _box(daily_limit=2)
        _deal(fake_session.campaign, DealState.READY_TO_EMAIL)
        deal = _deal(fake_session.campaign, DealState.FINDING_EMAIL)
        self._polling_in(fake_session, deal, 60)  # in_pipeline=2 == headroom
        assert self._flush(fake_session) == 0

    def test_a_lookup_landing_today_counts_toward_pipeline(self, fake_session):
        _box(daily_limit=1)
        deal = _deal(fake_session.campaign, DealState.FINDING_EMAIL)
        self._polling_in(fake_session, deal, 60)  # already fills the 1 slot
        assert self._flush(fake_session) == 0

    def test_a_stalled_lookup_does_not_wedge_the_drain(self, fake_session):
        """The poll backoff is uncapped, so a deal can sit at FINDING_EMAIL for
        days. It holds no claim on *today's* headroom — counting it would let a
        few stalled lookups shut the submit drain for as long as they stay
        stalled, which is exactly when new ones matter most."""
        _box(daily_limit=1)
        deal = _deal(fake_session.campaign, DealState.FINDING_EMAIL)
        self._polling_in(fake_session, deal, 3 * 86400)  # next poll is days out
        assert self._flush(fake_session) == 1

    def test_no_op_when_find_email_already_pending(self, fake_session):
        _box(daily_limit=10)
        Task.objects.create(
            task_type=Task.TaskType.FIND_EMAIL,
            scheduled_at=timezone.now(),
            payload={"campaign_id": fake_session.campaign.pk},
        )
        assert self._flush(fake_session) == 0
        assert self._find_email_tasks(fake_session.campaign).count() == 1
