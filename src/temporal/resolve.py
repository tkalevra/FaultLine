"""The single temporal RESOLUTION entry point.

Consolidates the scattered event-date resolution behind ONE module interface. The mature
span-detection + dateparser normalization implementation lives in
``src.extraction.linguistics`` (battle-tested across the LongMemEval work); this module is
the SINGLE PUBLIC DOOR the rest of the codebase calls — it delegates to that implementation
(Phase-1 behavior-preserving) and LAYERS the new NOW-anchored capabilities on top:

  • ``extract_event_date`` / ``extract_event_date_and_residue`` — re-exported point/peel API
    (unchanged behavior; the linguistics implementation is the engine).
  • calendar-aligned INTERVAL windows ("last week" → prior ISO week) — ``intervals``.
  • named recurring EVENTS ("Black Friday") — ``named_events`` (rule + most-recent-past year).
  • ``resolve_event_dates`` — the unified structured resolver that tries, in order:
    point/peel (precise date) → calendar window → named event, storing at the INPUT precision.

DETERMINISTIC throughout (dateparser is a rule engine; intervals/named-events are pure
calendar arithmetic). NEVER NULLs an anchor the user gave when a rule resolves it; NEVER
LLM-guesses a date; NEVER wall-clocks an un-anchored fact.
"""

from __future__ import annotations

import structlog

from src.temporal import intervals as _intervals
from src.temporal import named_events as _named_events
from src.temporal.duration import duration, point_anchor  # noqa: F401 (public re-export)
from src.temporal.reference import derive_now, now_utc  # noqa: F401 (public re-export)

log = structlog.get_logger()


# ── Phase-1 behavior-preserving re-exports (the linguistics engine is the implementation) ──
def extract_event_date(text: str, reference):
    """Deterministic point event-date: ``(iso|None, granularity|None)``. Single door over the
    linguistics resolver (spaCy DATE NER ∪ numeric regex → dateparser).

    Phase-1 behavior-preserving for everything the engine already resolves; ADDS a single
    NAMED-EVENT fallback (fix #3) — when the engine returns NOTHING but the text names a known
    calendar-rule event ("Black Friday", "Christmas"), resolve it deterministically to its
    rule date + most-recent-past year. NEVER NULL-on-name when the rule knows it; NEVER
    LLM-guessed. A non-named miss stays ``(None, None)`` (fail-safe, never fabricated)."""
    # OFFSET-FROM-NAMED-EVENT takes PRECEDENCE over the engine for its exact pattern only
    # ("a week before Black Friday"): dateparser drops the holiday and guesses a bogus date off
    # the bare unit ("week"), so trusting it here would mis-date the second comparison operand.
    # Gated to the precise grammatical pattern (count+unit+before/after+named-event) → fully
    # behavior-preserving for every other span; a non-match falls straight through.
    try:
        off = _named_events.resolve_offset_named_event(text, reference)
    except Exception as e:  # noqa: BLE001 — fail-safe: never fabricate
        log.warning("temporal.extract_event_date.offset_named_failed", error=str(e)[:120])
        off = None
    if off:
        d, ng = off
        try:
            from datetime import datetime, time
            ref_tz = getattr(reference, "tzinfo", None)
            dt = datetime.combine(d, time(0, 0)).replace(tzinfo=ref_tz)
            return (dt.isoformat(), ng or "day")
        except Exception as e:  # noqa: BLE001 — fail-safe
            log.warning("temporal.extract_event_date.offset_iso_failed", error=str(e)[:120])

    from src.extraction.linguistics import extract_event_date as _impl
    iso, gran = _impl(text, reference)
    if iso:
        return (iso, gran)
    # Engine returned nothing — try the named-event rule (additive, deterministic).
    try:
        ne = _named_events.resolve_named_event(text, reference)
    except Exception as e:  # noqa: BLE001 — fail-safe: never fabricate
        log.warning("temporal.extract_event_date.named_fallback_failed", error=str(e)[:120])
        ne = None
    if ne:
        d, ng = ne
        try:
            from datetime import datetime, time
            ref_tz = getattr(reference, "tzinfo", None)
            dt = datetime.combine(d, time(0, 0)).replace(tzinfo=ref_tz)
            return (dt.isoformat(), ng or "day")
        except Exception as e:  # noqa: BLE001 — fail-safe
            log.warning("temporal.extract_event_date.named_iso_failed", error=str(e)[:120])
    return (None, None)


def extract_event_date_and_residue(text: str, reference):
    """Peel the first resolvable date out of ``text``: ``(iso|None, gran|None, residue)``.
    Single door over the linguistics peel resolver. Behavior-preserving."""
    from src.extraction.linguistics import extract_event_date_and_residue as _impl
    return _impl(text, reference)


def has_date_residue(text: str, reference) -> bool:
    """True iff any date span exists in ``text``. Single door over the linguistics detector."""
    from src.extraction.linguistics import has_date_residue as _impl
    return _impl(text, reference)


# ── New unified, precision-carrying resolver ──────────────────────────────────────────
def resolve_calendar_window(span: str, reference):
    """Resolve a bare calendar-period relative ("last week") to CALENDAR-ALIGNED
    ``(start_date, end_date, granularity)`` half-open bounds, or None. (intervals)"""
    return _intervals.resolve_calendar_window(span, reference)


def resolve_named_event(text: str, reference):
    """Resolve a named recurring event ("Black Friday") to ``(date, "day")`` via its rule +
    most-recent-past year, or None. (named_events)"""
    return _named_events.resolve_named_event(text, reference)


def resolve_event_dates(text: str, now, prior_nps=None) -> list[dict]:
    """Unified structured temporal resolution against the NOW reference.

    Returns a list of resolved temporal anchors, each a dict:
      {
        "span":        the surface that resolved (the whole text on the point/window paths),
        "point":       ISO date string   (precise date / named-rule event),     XOR
        "interval":    {"start": iso, "end": iso}  (calendar-aligned window, half-open),
        "granularity": "day" | "month" | "week" | "year",
        "named_event": the canonical name when a named rule resolved it (else absent),
      }

    Resolution order (store at the INPUT's precision):
      1. PRECISE date / named-rule via the point resolver → POINT.
      2. CALENDAR-PERIOD relative ("last week"/"last month"/"last year") → INTERVAL with
         calendar-aligned bounds (NOT a rolling window).
      3. NAMED recurring event ("Black Friday") → POINT via rule + most-recent-past year.

    A precise date already covers most absolute/relative forms (the linguistics engine);
    the window + named-event paths catch what the point resolver deliberately leaves NULL
    (bare period windows, named holidays). Empty list = no temporal anchor (NEVER fabricated).

    ``prior_nps`` is accepted for interface stability (per-clause attribution context); the
    point/window/named paths are subject-agnostic and do not require it today.
    Deterministic, fail-safe (any error on a path → that path yields nothing, never crashes)."""
    out: list[dict] = []
    if not text or now is None:
        return out

    # 1. CALENDAR-PERIOD window ("last week" / "last month" / "last year") FIRST ───────
    # A bare calendar-period relative is an INTERVAL with calendar-aligned bounds, NOT a
    # point. The linguistics point resolver collapses "last week" to ref−7d (a single day);
    # the window path must own it so it stores as a calendar-aligned [start, end). Only a
    # BARE period window matches here (a span carrying a concrete day/year is NOT bare and
    # falls through to the point resolver), so precise dates are unaffected. (fix: calendar
    # intervals — not rolling.)
    try:
        win = resolve_calendar_window(text.strip(), now)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("temporal.resolve_event_dates.window_failed", error=str(e)[:120])
        win = None
    if win:
        start, end, wgran = win
        out.append({
            "span": text,
            "interval": {"start": start.isoformat(), "end": end.isoformat()},
            "granularity": wgran,
        })
        return out

    # 2. PRECISE point (incl. most relatives the engine handles) ──────────────────────
    try:
        iso, gran = extract_event_date(text, now)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("temporal.resolve_event_dates.point_failed", error=str(e)[:120])
        iso, gran = (None, None)
    if iso:
        out.append({"span": text, "point": iso, "granularity": gran or "day"})
        return out

    # 3. NAMED recurring event ("Black Friday") ───────────────────────────────────────
    try:
        ne = resolve_named_event(text, now)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("temporal.resolve_event_dates.named_failed", error=str(e)[:120])
        ne = None
    if ne:
        d, gran = ne
        nm = _named_events._canon_name(text)
        rec = {"span": text, "point": d.isoformat(), "granularity": gran or "day"}
        if nm:
            rec["named_event"] = nm
        out.append(rec)
        return out

    return out
