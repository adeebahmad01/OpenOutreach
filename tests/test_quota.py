# tests/test_quota.py
"""The proportional opener quota — where ``Campaign.action_fraction`` binds.

Covers the weights (who is owed what), the Bresenham allocation (the prefix
invariant, not just the aggregate), and the ledger read off ``Deal.email_sent_at``.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from openoutreach.core.conf import QUOTA_WINDOW_DAYS
from openoutreach.core.models import Campaign, Task
from openoutreach.core.quota import (
    allocate, opener_counts, realized_share, weights,
)
from openoutreach.core.scheduler import reconcile
from openoutreach.crm.models import DealState
from openoutreach.emails.models import Mailbox
from tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


def _campaign(name, *, freemium=False, fraction=0.2):
    return Campaign.objects.create(
        name=name, is_freemium=freemium, action_fraction=fraction,
    )


def _opener(campaign, *, days_ago=0):
    """A deal whose opener has been sent — one row in the quota ledger."""
    return DealFactory(
        campaign=campaign,
        lead=LeadFactory(),
        state=DealState.EMAILED,
        email_sent_at=timezone.now() - timedelta(days=days_ago),
    )


# ── weights ───────────────────────────────────────────────────────────


class TestWeights:
    def test_freemium_takes_its_action_fraction(self):
        own, promo = _campaign("Mine"), _campaign("Promo", freemium=True, fraction=0.2)
        w = weights([own, promo])
        assert w[promo.pk] == pytest.approx(0.2)
        assert w[own.pk] == pytest.approx(0.8)

    def test_own_campaigns_split_the_remainder_evenly(self):
        a, b = _campaign("A"), _campaign("B")
        promo = _campaign("Promo", freemium=True, fraction=0.2)
        w = weights([a, b, promo])
        assert w[a.pk] == pytest.approx(0.4)
        assert w[b.pk] == pytest.approx(0.4)

    def test_freemium_alone_takes_everything(self):
        """Work-conserving: holding back the only sendable campaign stalls the
        daemon and protects nobody."""
        promo = _campaign("Promo", freemium=True, fraction=0.2)
        assert weights([promo])[promo.pk] == pytest.approx(1.0)

    def test_no_freemium_leaves_the_operator_whole(self):
        own = _campaign("Mine")
        assert weights([own])[own.pk] == pytest.approx(1.0)

    def test_zero_fraction_shuts_the_promo_off(self):
        own = _campaign("Mine")
        promo = _campaign("Promo", freemium=True, fraction=0.0)
        w = weights([own, promo])
        assert w[promo.pk] == 0.0
        assert w[own.pk] == pytest.approx(1.0)


# ── allocate ──────────────────────────────────────────────────────────


class TestAllocate:
    def test_splits_by_weight_from_an_empty_ledger(self):
        own = _campaign("Mine")
        promo = _campaign("Promo", freemium=True, fraction=0.2)
        grant = allocate([own, promo], 10, {own.pk: 0, promo.pk: 0})
        assert grant == {own.pk: 8, promo.pk: 2}

    def test_holds_the_ratio_at_every_prefix(self):
        """The point of error diffusion: the promo is dithered through the stream,
        never bursted. At f=0.2 no two promo slots land back to back."""
        own = _campaign("Mine")
        promo = _campaign("Promo", freemium=True, fraction=0.2)

        sent = {own.pk: 0, promo.pk: 0}
        stream = []
        for _ in range(20):
            pick = allocate([own, promo], 1, sent)
            winner = next(pk for pk, n in pick.items() if n)
            stream.append(winner)
            sent[winner] += 1
            total = sum(sent.values())
            assert abs(sent[promo.pk] - 0.2 * total) < 1  # prefix invariant

        assert stream.count(promo.pk) == 4
        assert not any(a == b == promo.pk for a, b in zip(stream, stream[1:]))

    def test_repays_an_overshoot_before_granting_again(self):
        """A campaign already past its share gets nothing until the stream catches
        up — how the existing 70/30 skew unwinds."""
        own = _campaign("Mine")
        promo = _campaign("Promo", freemium=True, fraction=0.2)
        grant = allocate([own, promo], 5, {own.pk: 0, promo.pk: 10})
        assert grant == {own.pk: 5, promo.pk: 0}

    def test_zero_weight_campaign_never_receives(self):
        own = _campaign("Mine")
        promo = _campaign("Promo", freemium=True, fraction=0.0)
        grant = allocate([own, promo], 6, {own.pk: 0, promo.pk: 0})
        assert grant == {own.pk: 6, promo.pk: 0}

    def test_no_slots_grants_nothing(self):
        own = _campaign("Mine")
        assert allocate([own], 0, {own.pk: 0}) == {own.pk: 0}

    def test_does_not_mutate_the_ledger(self):
        own = _campaign("Mine")
        sent = {own.pk: 3}
        allocate([own], 4, sent)
        assert sent == {own.pk: 3}


# ── the ledger ────────────────────────────────────────────────────────


class TestLedger:
    def test_counts_only_sent_openers(self):
        own = _campaign("Mine")
        _opener(own)
        DealFactory(campaign=own, lead=LeadFactory(), state=DealState.QUALIFIED)
        assert opener_counts([own]) == {own.pk: 1}

    def test_ignores_sends_older_than_the_window(self):
        own = _campaign("Mine")
        _opener(own, days_ago=1)
        _opener(own, days_ago=QUOTA_WINDOW_DAYS + 1)
        assert opener_counts([own]) == {own.pk: 1}

    def test_campaign_with_no_sends_is_present_at_zero(self):
        own, promo = _campaign("Mine"), _campaign("Promo", freemium=True)
        _opener(own)
        assert opener_counts([own, promo]) == {own.pk: 1, promo.pk: 0}

    def test_realized_share_is_the_audit_number(self):
        own, promo = _campaign("Mine"), _campaign("Promo", freemium=True)
        _opener(own)
        for _ in range(3):
            _opener(promo)
        assert realized_share(promo) == pytest.approx(0.75)
        assert realized_share(own) == pytest.approx(0.25)

    def test_realized_share_is_zero_on_a_silent_window(self):
        assert realized_share(_campaign("Mine")) == 0.0


# ── end to end through reconcile ──────────────────────────────────────


class TestReconcileSplitsOpeners:
    def _ready(self, campaign, n):
        for i in range(n):
            DealFactory(
                campaign=campaign,
                lead=LeadFactory(email=f"lead-{campaign.pk}-{i}@corp.com"),
                state=DealState.READY_TO_EMAIL,
            )

    def _email_slots(self, campaign):
        return Task.objects.filter(
            task_type=Task.TaskType.EMAIL, payload__campaign_id=campaign.pk,
        ).count()

    def test_freemium_gets_its_fraction_not_the_race(self, fake_session):
        """Both pools are deep enough to swallow the whole day; before the quota
        whichever campaign drained first took it all."""
        own = fake_session.campaign
        promo = _campaign("Promo", freemium=True, fraction=0.2)
        promo.users.add(fake_session.django_user)
        Mailbox.objects.create(
            username="a@b.com", password="pw", from_address="a@b.com", daily_limit=10,
        )
        self._ready(own, 10)
        self._ready(promo, 10)

        reconcile(fake_session)

        assert self._email_slots(own) == 8
        assert self._email_slots(promo) == 2

    def test_due_follow_ups_are_reserved_off_the_top(self, fake_session):
        """A follow-up is not new reach: it sits outside the quota and keeps its
        headroom rather than competing for an opener slot."""
        own = fake_session.campaign
        Mailbox.objects.create(
            username="a@b.com", password="pw", from_address="a@b.com", daily_limit=5,
        )
        DealFactory(
            campaign=own, lead=LeadFactory(email="thread@corp.com"),
            state=DealState.EMAILED, email_sent_at=timezone.now(),
            next_follow_up_at=timezone.now() - timedelta(hours=1),
        )
        self._ready(own, 5)

        reconcile(fake_session)

        assert self._email_slots(own) == 4  # 5 headroom − 1 owed follow-up
