"""Precision-aware duration / comparison over temporal anchors (deterministic).

THE PRINCIPLE (CLAUDE.md temporal hinge): precision must be CARRIED through
duration/comparison — a point ⊖ interval is a RANGE/hedge, never a false-crisp single
number. Two POINTS give an exact day-delta; a POINT against an INTERVAL (or two
intervals) gives a (min, max) span the caller renders as a hedge ("about N..M days").

An ANCHOR here is a half-open ``(start: date, end: date, granularity: str)`` interval —
the SAME ``(gs, ge, gran)`` shape ``main._fact_interval`` already produces. A precise
date is an interval of width 1 day (``[d, d+1)``) with granularity "day". Pure calendar
arithmetic, NO ML, fail-safe (any error → ``None`` → the caller treats it as undecidable
rather than emitting a fabricated number).
"""

from __future__ import annotations

from datetime import date, timedelta

import structlog

log = structlog.get_logger()


def point_anchor(d: date, granularity: str = "day"):
    """Build a width-1-day anchor ``(d, d+1, granularity)`` for a precise point date."""
    return (d, d + timedelta(days=1), str(granularity or "day").lower())


def is_point(anchor) -> bool:
    """True iff ``anchor`` is a single-day (point) interval [d, d+1)."""
    if not anchor:
        return False
    try:
        gs, ge, _ = anchor
        return (ge - gs).days == 1
    except Exception:  # noqa: BLE001
        return False


def duration(a, b=None):
    """Precision-aware temporal delta.

    • ``duration(a)``      → the WIDTH of interval ``a`` in days (point → 1).
    • ``duration(a, b)``   → the gap between the two anchors, PRECISION-AWARE:
        - both POINTS                  → ``{"exact": N, "crisp": True}`` (a single day delta)
        - any INTERVAL involved        → ``{"min": lo, "max": hi, "crisp": False}``
          (a RANGE the caller renders as a hedge — never a false-crisp single number)

    The gap is measured earlier-event.end(inclusive) → later-event.start, sign-aware on the
    point/point path (mirrors ``main._op_between``). For the range path the min/max bound the
    possible gaps across the two intervals' extents. Returns ``None`` on a missing/invalid
    anchor (the UNDECIDABLE signal — fail-loud, never a fabricated number)."""
    if a is None:
        return None
    if b is None:
        try:
            gs, ge, _ = a
            return (ge - gs).days
        except Exception as e:  # noqa: BLE001
            log.warning("temporal.duration_width_failed", error=str(e)[:120])
            return None
    try:
        a_gs, a_ge, _ = a
        b_gs, b_ge, _ = b
    except Exception as e:  # noqa: BLE001
        log.warning("temporal.duration_unpack_failed", error=str(e)[:120])
        return None

    # ── Both POINTS → exact, sign-aware day delta (inclusive-end convention) ──
    if is_point(a) and is_point(b):
        a_ge_inc = a_ge - timedelta(days=1)
        b_ge_inc = b_ge - timedelta(days=1)
        if a_gs <= b_gs:
            val = (b_gs - a_ge_inc).days
        else:
            val = -((a_gs - b_ge_inc).days)
        return {"exact": val, "crisp": True}

    # ── Any INTERVAL → a RANGE (min..max), rendered as a hedge, never false-crisp ──
    # Order the pair chronologically by start, then bound the gap by the two extents.
    if a_gs <= b_gs:
        earlier, later = (a_gs, a_ge), (b_gs, b_ge)
    else:
        earlier, later = (b_gs, b_ge), (a_gs, a_ge)
    e_gs, e_ge = earlier
    l_gs, l_ge = later
    e_ge_inc = e_ge - timedelta(days=1)   # last valid day of the earlier interval
    l_ge_inc = l_ge - timedelta(days=1)   # last valid day of the later interval
    # Smallest possible gap: later.start − earlier.end(inclusive); clamp at 0 if they overlap.
    lo = (l_gs - e_ge_inc).days
    if lo < 0:
        lo = 0
    # Largest possible gap: later.end(inclusive) − earlier.start.
    hi = (l_ge_inc - e_gs).days
    if hi < lo:
        hi = lo
    return {"min": lo, "max": hi, "crisp": False}
