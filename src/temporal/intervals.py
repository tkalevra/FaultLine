"""Calendar-aligned relative-window intervals (deterministic, NO ML/LLM).

THE PRINCIPLE (CLAUDE.md temporal hinge): a calendar-PERIOD relative ("last week",
"last month", "last year") is an INTERVAL with CALENDAR-ALIGNED bounds — NOT a rolling
NOW-N..NOW window. Resolved against the ingest NOW reference:

    "last week"  → the PRIOR ISO calendar week  [Mon 00:00 .. next Mon 00:00)   (half-open)
    "this week"  → the CURRENT ISO calendar week
    "next week"  → the FOLLOWING ISO calendar week
    "last month" → the PRIOR calendar month      [1st .. 1st-of-next)
    "last year"  → the PRIOR calendar year       [Jan 1 .. Jan 1 next)

Week-start is a DECLARED config (default Monday / ISO-8601), env-overridable via
``TEMPORAL_WEEK_START`` (monday|sunday). All arithmetic is pure ``datetime`` — no
dateparser, no ML. Bounds are returned as half-open ``[start, end)`` date objects
(``end`` is the FIRST day NOT in the window) at the granularity of the period word.

Subject-agnostic, deterministic, fail-safe (any error → ``None`` → the caller keeps its
prior NULL / point-only behavior; NEVER fabricates and NEVER wall-clocks an un-anchored
fact).
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta

import structlog

log = structlog.get_logger()


# ── Week-start config (DECLARED, default Monday/ISO-8601) ─────────────────────────────
def _week_start_index() -> int:
    """Index of the configured week-start weekday (Monday=0 … Sunday=6).

    Declared via ``TEMPORAL_WEEK_START`` (monday|sunday, case-insensitive). Default
    Monday (ISO-8601). Any unrecognized value → Monday (fail-safe to the standard)."""
    raw = (os.environ.get("TEMPORAL_WEEK_START", "monday") or "monday").strip().lower()
    if raw in ("sunday", "sun", "0-sunday"):
        return 6  # Python weekday() Sunday == 6
    return 0  # Monday (ISO default)


# A BARE calendar-period relative window: <direction> <period>, whole string, nothing else.
# Closed formal class (3 directions × 3 period words) — calendar grammar, NOT a domain list.
_PERIOD_WINDOW_RE = re.compile(
    r"^\s*(last|previous|prev|past|this|current|next|coming|upcoming|following)\s+"
    r"(week|month|year)\s*$",
    re.IGNORECASE,
)
_DIR_BACK = frozenset({"last", "previous", "prev", "past"})
_DIR_THIS = frozenset({"this", "current"})
_DIR_FWD = frozenset({"next", "coming", "upcoming", "following"})


def is_calendar_period_window(span: str) -> bool:
    """True iff ``span`` is a BARE calendar-period relative window ("last week",
    "this month", "next year"). A span carrying a concrete day/year/weekday is NOT a bare
    window (handled by the point resolver). Surface-deterministic, fail-safe → False."""
    if not span:
        return False
    try:
        s = span.strip().lower()
        # A concrete day number or explicit 4-digit year means it is NOT a bare window.
        if re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\b", s) or re.search(r"\b(?:19|20)\d{2}\b", s):
            return False
        return bool(_PERIOD_WINDOW_RE.match(s))
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("temporal.is_calendar_period_window_failed", span=(span or "")[:48], error=str(e)[:120])
        return False


def _week_bounds(ref_d: date, offset_weeks: int) -> tuple[date, date]:
    """Half-open [start, end) of the calendar week ``offset_weeks`` from ``ref_d``'s week.

    Week-start honors the declared config. offset 0 = current week, -1 = prior week, +1 =
    following week. ``end`` is the first day of the NEXT week (exclusive)."""
    ws = _week_start_index()
    # days since the configured week-start
    delta = (ref_d.weekday() - ws) % 7
    cur_start = ref_d - timedelta(days=delta)
    start = cur_start + timedelta(weeks=offset_weeks)
    end = start + timedelta(weeks=1)
    return (start, end)


def _add_months(y: int, m: int, delta: int) -> tuple[int, int]:
    """Return (year, month) for month ``m`` of year ``y`` shifted by ``delta`` months."""
    idx = (y * 12 + (m - 1)) + delta
    return (idx // 12, idx % 12 + 1)


def _month_bounds(ref_d: date, offset_months: int) -> tuple[date, date]:
    """Half-open [first-of-month, first-of-next-month) for the month ``offset_months`` from ref."""
    y, m = _add_months(ref_d.year, ref_d.month, offset_months)
    start = date(y, m, 1)
    ny, nm = _add_months(y, m, 1)
    end = date(ny, nm, 1)
    return (start, end)


def _year_bounds(ref_d: date, offset_years: int) -> tuple[date, date]:
    """Half-open [Jan 1, Jan 1 next) for the year ``offset_years`` from ref."""
    y = ref_d.year + offset_years
    return (date(y, 1, 1), date(y + 1, 1, 1))


def resolve_calendar_window(span: str, reference):
    """Resolve a bare calendar-period relative window to CALENDAR-ALIGNED half-open bounds.

    Returns ``(start_date, end_date, granularity)`` where ``[start, end)`` is half-open and
    ``granularity`` ∈ {"week", "month", "year"} matches the period word — or ``None`` when
    the span is not a bare calendar-period window / ``reference`` is missing / on any error.

    "last week" → the PRIOR ISO calendar week [Mon..next Mon) — NOT a rolling NOW-14..-7.
    Deterministic, pure calendar arithmetic, NO ML, fail-safe (→ None, never fabricates)."""
    if not span or reference is None:
        return None
    try:
        m = _PERIOD_WINDOW_RE.match(span.strip().lower())
        if m is None:
            return None
        if not is_calendar_period_window(span):
            return None  # carries a concrete day/year → not a bare window
        direction = m.group(1)
        period = m.group(2)
        try:
            ref_d = reference.date()
        except Exception:  # noqa: BLE001 — reference is already a date
            ref_d = reference
        if direction in _DIR_BACK:
            off = -1
        elif direction in _DIR_FWD:
            off = 1
        elif direction in _DIR_THIS:
            off = 0
        else:
            return None
        if period == "week":
            start, end = _week_bounds(ref_d, off)
            return (start, end, "week")
        if period == "month":
            start, end = _month_bounds(ref_d, off)
            return (start, end, "month")
        if period == "year":
            start, end = _year_bounds(ref_d, off)
            return (start, end, "year")
        return None
    except Exception as e:  # noqa: BLE001 — fail-safe: never fabricate / never crash
        log.warning("temporal.resolve_calendar_window_failed", span=(span or "")[:48], error=str(e)[:120])
        return None
