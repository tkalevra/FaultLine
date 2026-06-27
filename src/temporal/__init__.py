"""``src.temporal`` — the SINGLE temporal entry point for FaultLine.

THE PRINCIPLE (CLAUDE.md temporal hinge): DERIVE NOW (the ingest reference) → resolve EVERY
temporal anchor against it → store at the INPUT's precision (point | calendar-aligned
interval | named-rule date) → carry precision through duration/comparison (point ⊖ interval
= a range/hedge, never false-crisp). NEVER NULL an anchor the user gave; NEVER wall-clock an
UN-anchored fact; NEVER LLM-guess a date. Deterministic throughout (dateparser is a rule
engine; intervals / named-events are pure calendar arithmetic).

New temporal logic is born HERE (modular), not grafted onto main.py. The mature span /
dateparser engine still lives in ``src.extraction.linguistics``; this package is the single
public door over it and the home of the new NOW-anchored capabilities.

Public interface (import from ``src.temporal``):
  • derive_now(text) / now_utc()              — the NOW reference (fix #1b).
  • extract_event_date(text, ref)             — precise point date (iso, gran).
  • extract_event_date_and_residue(text, ref) — peel the date out → (iso, gran, residue).
  • has_date_residue(text, ref)               — any date span present?
  • resolve_event_dates(text, now, prior_nps) — unified structured resolver.
  • resolve_calendar_window(span, ref)        — "last week" → calendar-aligned interval.
  • resolve_named_event(text, ref)            — "Black Friday" → rule date + most-recent year.
  • duration(a, b) / point_anchor(d, gran)    — precision-aware duration / comparison.
"""

from __future__ import annotations

from src.temporal.duration import duration, is_point, point_anchor
from src.temporal.intervals import is_calendar_period_window, resolve_calendar_window
from src.temporal.named_events import (
    is_named_event,
    most_recent_past_year,
    resolve_named_event,
)
from src.temporal.reference import derive_now, now_utc
from src.temporal.resolve import (
    extract_event_date,
    extract_event_date_and_residue,
    has_date_residue,
    resolve_event_dates,
)

__all__ = [
    "derive_now",
    "now_utc",
    "extract_event_date",
    "extract_event_date_and_residue",
    "has_date_residue",
    "resolve_event_dates",
    "resolve_calendar_window",
    "is_calendar_period_window",
    "resolve_named_event",
    "is_named_event",
    "most_recent_past_year",
    "duration",
    "point_anchor",
    "is_point",
]
