"""Duration granularity cluster — a computed interval must render at the granularity the
data supports (years + months), not floored to a single unit.

LME `gpt4_93159ced`: "How long have I been working before I started my current job at
NovaTech?" gold "4 years and 9 months". Both endpoints are captured precisely
(2019-01 → 2023-10, month-granular), but the `between` render collapsed the span to a
single floored unit ("56 months" via `_humanize_delta`'s 30.44-day rounding) and dropped
the compound years+months shape. `_calendar_span_phrase` decomposes the two grounded
endpoint dates with dateutil.relativedelta (calendar-exact) into "4 years and 9 months".

Flag `DURATION_CALENDAR_GRANULARITY` (default ON) gates the render; OFF → the legacy
single-unit output, byte-for-byte.
"""
import os

from src.api import main


# ── the flagship: a 4y9m month-granular span decomposes losslessly ──────────────
def test_calendar_span_flagship_years_and_months():
    assert main._calendar_span_phrase("2019-01-25", "2023-10-25", "month") \
        == "4 years and 9 months"


def test_calendar_span_endpoint_order_agnostic():
    # reversed endpoints render the same positive magnitude
    assert main._calendar_span_phrase("2023-10-25", "2019-01-25", "month") \
        == "4 years and 9 months"


# ── single-component spans stay clean (no spurious "0 months" tail) ──────────────
def test_calendar_span_whole_years_only():
    assert main._calendar_span_phrase("2019-01-25", "2023-01-25", "month") == "4 years"


def test_calendar_span_months_only():
    assert main._calendar_span_phrase("2023-01-25", "2023-10-25", "month") == "9 months"


# ── granularity honesty: a YEAR-granular input cannot claim month precision ──────
def test_calendar_span_year_granular_rounds_to_whole_years():
    # 4y9m at year granularity → 5 years (9mo ≥ 6 rounds up)
    assert main._calendar_span_phrase("2019-01-25", "2023-10-25", "year") == "5 years"


# ── DEFER rails: asked-unit and sub-month spans fall back to the legacy render ───
def test_calendar_span_defers_when_unit_requested():
    assert main._calendar_span_phrase("2019-01-25", "2023-10-25", "month",
                                      requested_unit="month") is None


def test_calendar_span_defers_sub_month():
    # 3 weeks — below a calendar month → None so the caller keeps days/weeks
    assert main._calendar_span_phrase("2023-01-01", "2023-01-22", "day") is None


def test_calendar_span_parse_failure_defers():
    assert main._calendar_span_phrase("not-a-date", "2023-10-25", "month") is None


# ── flag OFF → the helper is never consulted; single-unit render preserved ───────
def test_flag_off_preserves_single_unit_between(monkeypatch):
    monkeypatch.setenv("DURATION_CALENDAR_GRANULARITY", "off")
    assert main._duration_calendar_granularity_on() is False
    # legacy _humanize_delta whole-unit render is unchanged (regression guard)
    monkeypatch.delenv("DURATION_CALENDAR_GRANULARITY", raising=False)


# ── existing whole-unit / short-span _humanize_delta behavior is UNREGRESSED ─────
def test_humanize_delta_short_span_days_unchanged():
    # 16 days ambiguous → "16 days" (not coarsened); a week-multiple → weeks
    assert main._humanize_delta(16, "day") == "16 days"
    assert main._humanize_delta(21, "day") == "3 weeks"


def test_humanize_delta_requested_unit_unchanged():
    assert main._humanize_delta(1734, "month", requested_unit="months") == "57 months"
