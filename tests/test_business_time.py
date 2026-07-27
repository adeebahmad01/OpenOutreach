from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

import pytest

from openoutreach.core.business_time import add_business_hours, business_days_between

# Reference week: Mon 2026-03-16 … Sun 2026-03-22.
MON = datetime(2026, 3, 16, 10, 0, tzinfo=dt_timezone.utc)
FRI = datetime(2026, 3, 20, 10, 0, tzinfo=dt_timezone.utc)
SAT = datetime(2026, 3, 21, 10, 0, tzinfo=dt_timezone.utc)
SUN = datetime(2026, 3, 22, 10, 0, tzinfo=dt_timezone.utc)
NEXT_MON = datetime(2026, 3, 23, 10, 0, tzinfo=dt_timezone.utc)


class TestBusinessDaysBetween:
    def test_consecutive_weekdays(self):
        assert business_days_between(MON, MON.replace(day=17)) == 1

    def test_weekend_does_not_count(self):
        assert business_days_between(FRI, SAT) == 0
        assert business_days_between(FRI, SUN) == 0

    def test_friday_to_monday_is_one(self):
        assert business_days_between(FRI, NEXT_MON) == 1

    def test_spans_a_full_week(self):
        # Mon → Mon a week later: 5 working days, not 7.
        assert business_days_between(MON, MON.replace(day=23)) == 5

    def test_same_day_is_zero(self):
        assert business_days_between(MON, MON.replace(hour=23)) == 0

    def test_never_negative(self):
        assert business_days_between(NEXT_MON, FRI) == 0


class TestAddBusinessHours:
    def test_within_the_same_day(self):
        assert add_business_hours(MON, 4) == MON.replace(hour=14)

    def test_rolls_over_a_weekday_midnight(self):
        # Mon 10:00 + 24 business hours = Tue 10:00 (no weekend in the way).
        assert add_business_hours(MON, 24) == MON.replace(day=17)

    def test_skips_the_weekend(self):
        # Fri 10:00 + 24 business hours = Mon 10:00, not Sat.
        assert add_business_hours(FRI, 24) == NEXT_MON

    def test_never_lands_on_a_weekend(self):
        # Fri 10:00 + 8h would be Fri 18:00; + 48h must clear both weekend days.
        assert add_business_hours(FRI, 48).weekday() == 1  # Tuesday
        assert add_business_hours(FRI, 8).weekday() == 4

    def test_start_on_a_weekend_resumes_monday(self):
        # A countdown armed on Saturday starts ticking at Monday 00:00.
        assert add_business_hours(SAT, 2) == NEXT_MON.replace(hour=2)
        assert add_business_hours(SUN, 0) == NEXT_MON.replace(hour=0)

    def test_zero_hours_on_a_weekday_is_a_no_op(self):
        assert add_business_hours(MON, 0) == MON

    def test_negative_hours_clamped(self):
        assert add_business_hours(MON, -5) == MON

    def test_long_gap_spans_multiple_weekends(self):
        # Mon 10:00 + 200 business hours = 8 full working days + 8h.
        result = add_business_hours(MON, 200)
        assert result == datetime(2026, 3, 26, 18, 0, tzinfo=dt_timezone.utc)
        assert result.weekday() < 5

    def test_fractional_hours(self):
        assert add_business_hours(MON, 1.5) == MON.replace(hour=11, minute=30)

    @pytest.mark.parametrize("hours", [1, 6, 24, 49, 72, 96, 121])
    def test_result_is_always_a_working_day(self, hours):
        for start in (MON, FRI, SAT, SUN):
            assert add_business_hours(start, hours).weekday() < 5
