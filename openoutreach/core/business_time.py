# openoutreach/core/business_time.py
"""Working-day arithmetic for outreach pacing.

Follow-up gaps are business time, not wall-clock time: a lead who gets an email
on Friday afternoon should not be nudged again on Sunday, and a thread that sat
untouched over a weekend has not really gone quiet. Both ends of that idea live
here — measuring elapsed time (``business_days_between``) and scheduling the next
touch (``add_business_hours``) skip Saturday and Sunday alike.

Public holidays are deliberately not modelled: they are per-country, per-year
data we do not carry, and the agent's pacing is coarse enough that a missed
holiday costs one badly-timed email, not a broken schedule.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

SATURDAY = 5


def is_business_day(day: date) -> bool:
    """True for Monday–Friday."""
    return day.weekday() < SATURDAY


def business_days_between(start: datetime, end: datetime) -> int:
    """Whole working days elapsed from ``start`` to ``end`` (weekends excluded).

    Counts the business days in the half-open range ``(start.date(), end.date()]``,
    so Friday → Monday is 1 and Friday → Sunday is 0. Never negative.
    """
    return _business_days_in_range(start.date() + timedelta(days=1),
                                   end.date() + timedelta(days=1))


def add_business_hours(start: datetime, hours: float) -> datetime:
    """Advance ``start`` by ``hours`` of business time, skipping weekends.

    Weekend hours do not count toward the countdown: 24 business hours from
    Friday 14:00 lands Monday 14:00. A ``start`` that already falls on a weekend
    resumes at the following Monday, so a countdown can never expire on a
    Saturday or Sunday. Non-positive ``hours`` only rolls off the weekend.
    """
    cursor = _next_business_moment(start)
    remaining = timedelta(hours=max(hours, 0.0))
    while remaining > timedelta(0):
        day_end = _midnight_after(cursor)
        available = day_end - cursor
        if remaining < available:
            return cursor + remaining
        remaining -= available
        cursor = _next_business_moment(day_end)
    return cursor


# ── Internals ─────────────────────────────────────────────────────


def _business_days_in_range(start: date, end: date) -> int:
    """Number of Monday–Friday days in the half-open range ``[start, end)``."""
    days = (end - start).days
    if days <= 0:
        return 0
    full_weeks, remainder = divmod(days, 7)
    count = full_weeks * 5
    for offset in range(remainder):
        if is_business_day(start + timedelta(days=offset)):
            count += 1
    return count


def _next_business_moment(when: datetime) -> datetime:
    """``when`` itself on a weekday, else midnight at the start of the next Monday."""
    if is_business_day(when.date()):
        return when
    days_to_monday = 7 - when.weekday()
    return datetime.combine(when.date() + timedelta(days=days_to_monday), time.min,
                            tzinfo=when.tzinfo)


def _midnight_after(when: datetime) -> datetime:
    """Start of the day after ``when``, in ``when``'s own timezone."""
    return datetime.combine(when.date() + timedelta(days=1), time.min, tzinfo=when.tzinfo)
