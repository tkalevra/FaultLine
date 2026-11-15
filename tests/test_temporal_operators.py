"""Temporal interval operators — day-difference correctness (regression for the
`_op_between` off-by-one).

Intervals are (gs, ge, gran) with `ge` the EXCLUSIVE granule end (a day-granular event
on June 14 is gs=06-14, ge=06-15). The "how many days between A and B" / "how many days
before X did I do Y" answer is the plain day difference between the two event days
(LongMemEval Q10: Turbocharged Tuesdays 06-14 vs Rack Fest 06-18 → gold "4 days").

Before the fix `_op_between` measured B.start − A.ge (exclusive end) → 06-18 − 06-15 = 3
(off by one). It must use the INCLUSIVE last day (ge − 1), matching `_op_elapsed`.
"""
from datetime import date

from src.api import main


def _day(d: date):
    """A single-day granule: gs=d, ge=d+1 (exclusive), gran='day'."""
    from datetime import timedelta
    return (d, d + timedelta(days=1), "day")


def test_op_between_single_day_events_is_plain_day_difference():
    turbo = _day(date(2026, 6, 14))
    rack = _day(date(2026, 6, 18))
    # gold "4 days" — 18 − 14, NOT 3 (the exclusive-end off-by-one)
    assert main._op_between(turbo, rack) == 4
    assert main._op_between(rack, turbo) == -4  # sign-aware (reverse order)


def test_op_between_same_and_adjacent_days():
    d14 = _day(date(2026, 6, 14))
    d15 = _day(date(2026, 6, 15))
    assert main._op_between(d14, d14) == 0   # same day
    assert main._op_between(d14, d15) == 1   # one day apart


def test_op_between_none_safe():
    assert main._op_between(None, _day(date(2026, 6, 14))) is None
    assert main._op_between(_day(date(2026, 6, 14)), None) is None
