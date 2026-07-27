from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from openoutreach.core.conf import WARM_CEILING_SENDS, WARM_FLOOR_SENDS
from openoutreach.emails.delivery_policy import Response
from openoutreach.emails import warmth
from openoutreach.emails.models import Mailbox, SendVerdict
from openoutreach.emails.warmth import (
    capacity_from,
    mark_measured,
    measurement_due,
    refresh_capacity,
)


def _history(*daily_counts: int) -> Counter:
    """Sent history from a sequence of per-day totals, one day apart."""
    start = date(2026, 7, 1)
    return Counter({start + timedelta(days=i): n for i, n in enumerate(daily_counts)})


@pytest.fixture(autouse=True)
def _clear_measurement_cache():
    """The measurement cadence is a process-held date — reset it between tests."""
    warmth._measured_on = None
    yield
    warmth._measured_on = None


def _box(**kwargs) -> Mailbox:
    return Mailbox.objects.create(
        username="a@b.com", password="pw", from_address="a@b.com", **kwargs,
    )


class TestCapacityFrom:
    def test_no_history_yields_floor(self):
        assert capacity_from(Counter(), clean=True) == WARM_FLOOR_SENDS

    def test_steps_above_sustained_volume_when_clean(self):
        # p75 of [10, 10, 10, 10] is 10; a clean window allows the growth step.
        assert capacity_from(_history(10, 10, 10, 10), clean=True) == 15

    def test_holds_at_sustained_volume_when_not_clean(self):
        assert capacity_from(_history(10, 10, 10, 10), clean=False) == 10

    def test_idle_days_do_not_drag_the_measurement_down(self):
        # Weekends off must not read as reduced capacity: zero-days are excluded,
        # so this measures the same as four solid days.
        assert capacity_from(_history(10, 0, 10, 0, 10, 0, 10), clean=True) == 15

    def test_single_anomalous_day_does_not_set_capacity(self):
        # p75 of [5, 5, 5, 80] is 5 — one burst is not a demonstrated volume.
        assert capacity_from(_history(5, 5, 5, 80), clean=True) == 7

    def test_ceiling_is_a_hard_rail(self):
        assert capacity_from(_history(200, 200, 200), clean=True) == WARM_CEILING_SENDS

    def test_floor_survives_a_near_silent_box(self):
        assert capacity_from(_history(1), clean=True) == WARM_FLOOR_SENDS

    def test_growth_recovers_from_a_throttled_window(self):
        # A box knocked down to 5/day climbs back to full band in under a week,
        # which an additive step could not do.
        capacity, days = 5, 0
        while capacity < 40:
            capacity = capacity_from(_history(capacity), clean=True)
            days += 1
        assert days <= 7


@pytest.mark.django_db
class TestRefreshCapacity:
    def test_persists_the_measurement(self):
        box = _box()
        with patch("openoutreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 30
        box.refresh_from_db()
        assert box.daily_limit == 30

    def test_unreachable_box_keeps_its_last_measurement(self):
        box = _box(daily_limit=30)
        with patch("openoutreach.emails.warmth.read_sent_history",
                   side_effect=OSError("connection reset")):
            assert refresh_capacity(box) == 30
        box.refresh_from_db()
        assert box.daily_limit == 30

    def test_receiver_pushback_holds_capacity_at_demonstrated_volume(self):
        box = _box()
        SendVerdict.objects.create(
            mailbox=box, response=Response.DEFERRED, smtp_code=421,
            detail="4.7.0 unusual rate of mail",
        )
        with patch("openoutreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 20

    def test_transport_failure_does_not_hold_capacity(self):
        # A dropped socket is not the receiver saying anything about this box;
        # a flaky network must not cost it capacity.
        box = _box()
        SendVerdict.objects.create(
            mailbox=box, response=Response.TRANSPORT, smtp_code=None,
            detail="connection reset by peer",
        )
        with patch("openoutreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 30

    def test_another_box_pushback_does_not_hold_this_one(self):
        box = _box()
        other = Mailbox.objects.create(
            username="c@d.com", password="pw", from_address="c@d.com",
        )
        SendVerdict.objects.create(mailbox=other, response=Response.DEFERRED, smtp_code=421)
        with patch("openoutreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 30


class TestMeasurementCadence:
    def test_due_before_the_first_pass(self):
        assert measurement_due()

    def test_not_due_again_the_same_day(self):
        mark_measured()
        assert not measurement_due()

    def test_due_again_the_next_day(self):
        mark_measured()
        with patch("openoutreach.emails.warmth.timezone.localdate",
                   return_value=date(2099, 1, 1)):
            assert measurement_due()
