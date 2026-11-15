"""Unit tests for the ``src.temporal`` module — the single NOW-anchored temporal entry point.

These assert WITHOUT the full ingest/query stack (the speed win): they exercise the
calendar-aligned intervals, named-event rules, precision-aware duration, NOW-reference
derivation, and the unified resolver directly. Deterministic, calendar-aligned (NOT rolling),
week-start configurable, never-NULL/never-fabricate.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

import pytest

import importlib

import src.temporal as T
import src.temporal.intervals as iv
import src.temporal.named_events as ne

# NOTE: ``src.temporal.__init__`` re-exports the ``duration`` FUNCTION into the package
# namespace, which shadows the ``src.temporal.duration`` SUBMODULE attribute. Load the
# submodule explicitly so the test can reach ``point_anchor``/``duration`` from it.
dur = importlib.import_module("src.temporal.duration")


def _ref(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


# ── #1b NOW reference derivation ──────────────────────────────────────────────────────
def test_derive_now_marker():
    ref, txt = T.derive_now("[Date: 2023-05-15] I got a phone")
    assert ref.date() == date(2023, 5, 15)
    assert txt == "I got a phone"


def test_derive_now_no_marker_is_system_time():
    ref, txt = T.derive_now("just a plain statement")
    # system-time default (within a day of now) — never NULL, never fabricated
    assert abs((ref - datetime.now(timezone.utc)).total_seconds()) < 86400
    assert txt == "just a plain statement"


def test_derive_now_empty():
    ref, txt = T.derive_now(None)
    assert txt == ""
    assert ref.tzinfo is not None


# ── calendar-aligned INTERVALS (NOT rolling windows) ──────────────────────────────────
def test_last_week_is_prior_iso_week_mon_sun():
    # 2023-05-15 is a Monday. "last week" = the PRIOR ISO calendar week [Mon 05-08 .. Mon 05-15).
    win = T.resolve_calendar_window("last week", _ref(2023, 5, 15))
    assert win is not None
    start, end, gran = win
    assert (start, end, gran) == (date(2023, 5, 8), date(2023, 5, 15), "week")
    assert start.weekday() == 0  # Monday-start
    # NOT a rolling NOW-14..-7 window (which would be 05-01..05-08)
    assert start != date(2023, 5, 1)


def test_last_week_midweek_reference():
    # 2023-05-17 is a Wednesday; current week starts Mon 05-15 → last week = 05-08..05-15.
    win = T.resolve_calendar_window("last week", _ref(2023, 5, 17))
    start, end, _ = win
    assert (start, end) == (date(2023, 5, 8), date(2023, 5, 15))


def test_last_month_is_calendar_month():
    win = T.resolve_calendar_window("last month", _ref(2023, 5, 15))
    start, end, gran = win
    assert (start, end, gran) == (date(2023, 4, 1), date(2023, 5, 1), "month")


def test_last_month_january_rolls_year():
    win = T.resolve_calendar_window("last month", _ref(2023, 1, 10))
    start, end, _ = win
    assert (start, end) == (date(2022, 12, 1), date(2023, 1, 1))


def test_last_year_is_calendar_year():
    win = T.resolve_calendar_window("last year", _ref(2023, 5, 15))
    start, end, gran = win
    assert (start, end, gran) == (date(2022, 1, 1), date(2023, 1, 1), "year")


def test_this_and_next_week():
    this_w = T.resolve_calendar_window("this week", _ref(2023, 5, 17))  # Wed
    assert this_w[:2] == (date(2023, 5, 15), date(2023, 5, 22))
    next_w = T.resolve_calendar_window("next week", _ref(2023, 5, 17))
    assert next_w[:2] == (date(2023, 5, 22), date(2023, 5, 29))


def test_week_start_configurable_sunday(monkeypatch):
    monkeypatch.setenv("TEMPORAL_WEEK_START", "sunday")
    # 2023-05-17 is Wed; Sunday-start current week = Sun 05-14 .. Sun 05-21; last = 05-07..05-14.
    win = T.resolve_calendar_window("last week", _ref(2023, 5, 17))
    start, end, _ = win
    assert (start, end) == (date(2023, 5, 7), date(2023, 5, 14))
    assert start.weekday() == 6  # Sunday


def test_concrete_day_is_not_a_bare_window():
    # "next month on the 5th" carries a day → NOT a bare calendar window.
    assert iv.is_calendar_period_window("next month on the 5th") is False
    assert T.resolve_calendar_window("next month on the 5th", _ref(2023, 5, 15)) is None


# ── #3 named recurring events (rule + most-recent-past year) ───────────────────────────
def test_black_friday_said_may_2023_is_prior_november():
    # Black Friday 2022 = 2022-11-25 (4th Thursday Nov = 11-24, +1 day).
    res = T.resolve_named_event("Black Friday", _ref(2023, 5, 15))
    assert res is not None
    d, gran = res
    assert d == date(2022, 11, 25)
    assert gran == "day"


def test_black_friday_said_december_is_same_year():
    # Asked 2023-12-01 → the most-recent-past Black Friday is 2023-11-24.
    d, _ = T.resolve_named_event("Black Friday", _ref(2023, 12, 1))
    assert d == date(2023, 11, 24)


def test_christmas_named_rule():
    d, _ = T.resolve_named_event("Christmas", _ref(2023, 5, 15))
    assert d == date(2022, 12, 25)


def test_named_event_miss_returns_none():
    assert T.resolve_named_event("the lens broke", _ref(2023, 5, 15)) is None
    assert ne.is_named_event("a random workshop") is False


# ── OFFSET-FROM-NAMED-EVENT ("a week before Black Friday") — idx-22 second-operand dating ──────
def test_offset_a_week_before_black_friday():
    # Black Friday 2022 = 2022-11-25; "a week before" = MINUS 7 days = 2022-11-18.
    res = ne.resolve_offset_named_event("a week before Black Friday", _ref(2023, 5, 15))
    assert res is not None
    d, gran = res
    assert d == date(2022, 11, 18) and gran == "day"


def test_offset_after_and_count_and_units():
    assert ne.resolve_offset_named_event("a week after Black Friday", _ref(2023, 5, 15))[0] == \
        date(2022, 12, 2)
    assert ne.resolve_offset_named_event("2 weeks before Christmas", _ref(2023, 5, 15))[0] == \
        date(2022, 12, 11)
    assert ne.resolve_offset_named_event("a month before Black Friday", _ref(2023, 5, 15))[0] == \
        date(2022, 10, 25)


def test_offset_bare_named_event_is_not_an_offset():
    # A bare named event (no count+unit+before/after) is NOT this pattern → None here.
    assert ne.resolve_offset_named_event("Black Friday", _ref(2023, 5, 15)) is None


# ── OFFSET nearest-occurrence anchoring (LME c8090214 "days before I bought the iPhone …") ──────
# The offset must anchor to the SEASON nearest the reference, not the EVENT's most-recent-past year.
# "a week before Black Friday" said 2023-11-20 → Black Friday 2023 is Nov-24 (still upcoming), but
# the offset lands 2023-11-17 (already past) → that is the intended date, NOT 2022-11-18 (a year off,
# which would break the date-diff with a 2023 iPhone purchase). FAILS on the pre-fix code.
def test_offset_nearest_year_when_event_is_upcoming():
    # ref just before this year's Black Friday: the minus-7 offset is already in the past this year.
    assert ne.resolve_offset_named_event("a week before Black Friday", _ref(2023, 11, 20))[0] == \
        date(2023, 11, 17)
    # ref just after Black Friday: same nearest season.
    assert ne.resolve_offset_named_event("a week before Black Friday", _ref(2023, 11, 25))[0] == \
        date(2023, 11, 17)
    # early-January ref: the most-recent past season is still the PRIOR November.
    assert ne.resolve_offset_named_event("a week before Black Friday", _ref(2024, 1, 5))[0] == \
        date(2023, 11, 17)
    # a MID-YEAR ref (event genuinely last year) is behavior-preserved (prior-year offset).
    assert ne.resolve_offset_named_event("a week before Black Friday", _ref(2023, 5, 15))[0] == \
        date(2022, 11, 18)


def test_offset_nearest_year_full_sentence_ingest_peel():
    # The ingest peel (extract_event_date_and_residue) dates the Holiday Market operand to the nearest
    # season and excises the whole offset compound. This is the operand the date-diff computes on.
    iso, gran, residue = T.extract_event_date_and_residue(
        "I attended the annual Holiday Market at the local mall a week before Black Friday",
        _ref(2023, 11, 20))
    assert iso is not None and iso.startswith("2023-11-17") and gran == "day"
    assert "black friday" not in residue.lower() and "week" not in residue.lower()


def test_offset_nearest_year_flag_off_is_legacy():
    # Flag OFF → byte-identical legacy (most-recent-past-of-event), proving the fix is fully gated.
    os.environ["TEMPORAL_OFFSET_NEAREST_YEAR"] = "0"
    try:
        ne_off = importlib.reload(ne)
        assert ne_off.resolve_offset_named_event("a week before Black Friday", _ref(2023, 11, 20))[0] \
            == date(2022, 11, 18)
    finally:
        os.environ.pop("TEMPORAL_OFFSET_NEAREST_YEAR", None)
        importlib.reload(ne)  # restore default-ON module for subsequent tests


def test_offset_full_sentence_via_extract_event_date():
    # The full second-operand sentence resolves through the public door (precedence over dateparser).
    iso, gran = T.extract_event_date(
        "the Holiday Market was a week before Black Friday", _ref(2023, 5, 15))
    assert iso is not None and iso.startswith("2022-11-18") and gran == "day"


def test_offset_does_not_perturb_plain_relative():
    # Behavior-preserving: a plain relative is unchanged (not an offset-named pattern).
    iso, _ = T.extract_event_date("three weeks ago", _ref(2023, 5, 1))
    assert iso is not None and iso.startswith("2023-04-10")


# ── precision-aware DURATION (point ⊖ interval = a range/hedge) ────────────────────────
def test_two_points_exact_delta():
    a = dur.point_anchor(date(2023, 6, 14))
    b = dur.point_anchor(date(2023, 6, 18))
    res = dur.duration(a, b)
    assert res["crisp"] is True
    assert res["exact"] == 4


def test_point_minus_interval_is_a_range():
    # point (single day) vs a week-wide interval → a RANGE, never false-crisp.
    pt = dur.point_anchor(date(2023, 6, 1))
    interval = (date(2023, 6, 12), date(2023, 6, 19), "week")  # half-open week
    res = dur.duration(pt, interval)
    assert res["crisp"] is False
    assert res["min"] <= res["max"]
    assert res["min"] >= 0


def test_duration_width_of_interval():
    interval = (date(2023, 6, 12), date(2023, 6, 19), "week")
    assert dur.duration(interval) == 7
    assert dur.duration(dur.point_anchor(date(2023, 6, 1))) == 1


def test_duration_undecidable_on_missing_anchor():
    assert dur.duration(None, dur.point_anchor(date(2023, 6, 1))) is None


# ── unified resolver — store at the INPUT's precision ─────────────────────────────────
def test_resolve_event_dates_point_precise():
    out = T.resolve_event_dates("January 10th", _ref(2023, 5, 15))
    assert len(out) == 1
    assert out[0]["point"].startswith("2023-01-10")
    assert out[0]["granularity"] == "day"
    assert "interval" not in out[0]


def test_resolve_event_dates_calendar_window_interval():
    out = T.resolve_event_dates("last week", _ref(2023, 5, 15))
    assert len(out) == 1
    assert "interval" in out[0]
    assert out[0]["interval"]["start"] == "2023-05-08"
    assert out[0]["interval"]["end"] == "2023-05-15"
    assert out[0]["granularity"] == "week"


def test_resolve_event_dates_named_event_point():
    # Black Friday resolves to its rule date as a POINT (the engine/facade now owns it via the
    # named-event fallback; whether the named_event tag is set depends on which path resolved
    # it — the load-bearing assertion is the correct most-recent-past date).
    out = T.resolve_event_dates("Black Friday", _ref(2023, 5, 15))
    assert len(out) == 1
    assert out[0]["point"].startswith("2022-11-25")


def test_resolve_event_dates_no_anchor_empty():
    out = T.resolve_event_dates("the GPS system is broken", _ref(2023, 5, 15))
    assert out == []  # NEVER fabricates a date


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
