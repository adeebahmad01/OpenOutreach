from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from openoutreach.core.conf import MIN_SEND_INTERVAL_SECONDS, SEND_INTERVAL_JITTER_SECONDS
from openoutreach.core.models import Task
from openoutreach.core.scheduler import _paced_slots, reconcile
from openoutreach.crm.models import DealState
from openoutreach.emails.models import Mailbox
from tests.factories import DealFactory, LeadFactory

MIN_GAP = timedelta(seconds=MIN_SEND_INTERVAL_SECONDS)
MAX_GAP = timedelta(seconds=MIN_SEND_INTERVAL_SECONDS + SEND_INTERVAL_JITTER_SECONDS)


def _box(daily_limit=10):
    return Mailbox.objects.create(
        username="a@b.com", password="pw", from_address="a@b.com", daily_limit=daily_limit,
    )


def _ready(campaign, email):
    return DealFactory(
        campaign=campaign, lead=LeadFactory(email=email), state=DealState.READY_TO_EMAIL,
    )


@pytest.mark.django_db
class TestPacedSlots:
    def test_first_slot_is_immediate_on_an_idle_daemon(self):
        # Nothing to be spaced from — the floor must not delay a cold start.
        assert _paced_slots(1)[0] <= timezone.now() + timedelta(seconds=1)

    def test_slots_respect_the_floor_and_the_ceiling(self):
        slots = _paced_slots(20)
        gaps = [b - a for a, b in zip(slots, slots[1:])]
        assert all(MIN_GAP <= gap <= MAX_GAP for gap in gaps)

    def test_spacing_is_jittered_not_fixed(self):
        # A send exactly every 180s is as machine-shaped as no spacing at all.
        gaps = {b - a for a, b in zip(_paced_slots(20), _paced_slots(20)[1:])}
        assert len(gaps) > 1

    def test_a_pending_slot_is_respected_by_the_next_mint(self, fake_session):
        # Two reconcile passes must not each stack a burst onto a queue that
        # looks empty because nothing has been *sent* yet.
        Task.objects.create(
            task_type=Task.TaskType.EMAIL,
            status=Task.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(hours=1),
            payload={"campaign_id": 1},
        )
        assert _paced_slots(1)[0] >= timezone.now() + timedelta(hours=1) + MIN_GAP

    def test_a_sent_message_is_respected_after_a_restart(self, fake_session):
        # The pending queue is empty here; only the send record prevents a burst.
        from openoutreach.chat.models import ChatMessage

        deal = _ready(fake_session.campaign, "x@c.com")
        ChatMessage.objects.create(
            deal=deal, external_id="<a@b>", content="hi", is_outgoing=True,
            owner=fake_session.django_user, creation_date=timezone.now(),
        )
        assert _paced_slots(1)[0] >= timezone.now() + MIN_GAP - timedelta(seconds=1)


@pytest.mark.django_db
class TestReconcileOrdersSendsByPriority:
    def test_a_due_follow_up_takes_the_earlier_slot(self, fake_session):
        """The reason both drains mint one slot at a time.

        With batch minting, a day of openers was scheduled up front and a
        follow-up minted afterwards landed behind all of them — hours late, and
        ranked *above* them by ``Task.pending()``. One slot per pass means the
        queue is never more than an interval deep, so ordering holds.
        """
        campaign = fake_session.campaign
        _box(daily_limit=10)
        for i in range(5):
            _ready(campaign, f"x{i}@c.com")
        DealFactory(
            campaign=campaign, lead=LeadFactory(email="thread@corp.com"),
            state=DealState.EMAILED, email_sent_at=timezone.now(),
            next_follow_up_at=timezone.now() - timedelta(hours=1),
        )

        reconcile(fake_session)

        follow_up = Task.objects.get(task_type=Task.TaskType.FOLLOW_UP)
        opener = Task.objects.get(task_type=Task.TaskType.EMAIL)
        assert follow_up.scheduled_at < opener.scheduled_at

    def test_the_queue_never_runs_more_than_one_interval_deep(self, fake_session):
        campaign = fake_session.campaign
        _box(daily_limit=10)
        for i in range(10):
            _ready(campaign, f"x{i}@c.com")

        reconcile(fake_session)

        pending = Task.objects.filter(
            task_type=Task.TaskType.EMAIL, status=Task.Status.PENDING,
        )
        assert pending.count() == 1
        assert pending.first().scheduled_at <= timezone.now() + MAX_GAP
