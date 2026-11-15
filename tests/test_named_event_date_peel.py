"""Named-event date PEEL parity — LME datemath cluster (c8090214 DATE-DIFF operand).

ROOT CAUSE this pins: the date PEEL door used by ingest to ATTACH event_date to occurrences /
acquisitions — ``src.extraction.linguistics.extract_event_date_and_residue`` — resolved the OFFSET
named-event form ("a week before Black Friday") but NOT the BARE named-event form ("… on Black
Friday", "… on Christmas"), even though the sibling POINT resolver (``src.temporal.extract_event_date``
via ``resolve_named_event``) already knew those dates. Consequence: "I got my iPhone 13 Pro … on Black
Friday" committed ``(user, owns, iphone 13 pro)`` with a NULL event_date, so a date-diff / duration /
ordering question had a MISSING dated operand.

The fix mirrors the point path in the peel (behavior-preserving; fires only on a spaCy/regex engine
MISS + a known calendar-rule name — the SAME closed formal calendar class already in named_events).

Deterministic (dateparser + a calendar rule set — no ML, no LLM). Subject-agnostic: any "<verb> on
<named holiday>". FAILS on the pre-fix code (peel returned ``(None, None, text)`` for a bare holiday).
"""
from datetime import datetime, timezone

import pytest

REF = datetime(2023, 12, 10, tzinfo=timezone.utc)  # LME c8090214 session date


def _peel(text, ref=REF):
    from src.extraction.linguistics import extract_event_date_and_residue
    return extract_event_date_and_residue(text, ref)


def test_bare_named_event_black_friday_dates_and_peels():
    # Black Friday 2023 = 2023-11-24. Pre-fix: (None, None, <full text>).
    iso, gran, residue = _peel("I got my iPhone 13 Pro from Best Buy on Black Friday.")
    assert iso is not None and iso.startswith("2023-11-24"), iso
    assert gran == "day"
    # the holiday phrase + dangling "on" are excised out of the residue relation
    assert "black friday" not in residue.lower()
    assert "iphone 13 pro" in residue.lower()


def test_bare_named_event_christmas_dates_and_peels():
    iso, _gran, residue = _peel("I bought a laptop on Christmas.")
    assert iso is not None and iso.startswith("2022-12-25"), iso  # most-recent-past Christmas
    assert "christmas" not in residue.lower()


def test_acquisition_owns_edge_carries_named_event_date():
    """End-to-end at the ingest seam: the iPhone acquisition ``owns`` edge must carry the Black
    Friday event_date (the date-diff operand). This is the exact c8090214 iPhone anchor."""
    from src.api.main import _detect_event_states
    edges = _detect_event_states(
        "I got my iPhone 13 Pro at a discounted price of $800 from Best Buy on Black Friday, "
        "which was a great deal.",
        REF,
    )
    owns = [e for e in edges if e.get("rel_type") == "owns"
            and "iphone" in (e.get("object") or "")]
    assert owns, f"no owns edge for iphone: {edges}"
    assert owns[0].get("event_date", "").startswith("2023-11-24"), owns[0]


# ── REGRESSION GUARDS — the fix is gated to (engine-miss ∧ known-name); everything else unchanged ──

def test_plain_date_unchanged():
    iso, _g, residue = _peel("I got a phone on February 20th.",
                             datetime(2023, 3, 1, tzinfo=timezone.utc))
    assert iso is not None and iso.startswith("2023-02-20"), iso
    assert "february" not in residue.lower()


def test_offset_named_event_unchanged():
    # "a week before Black Friday" still resolves via the OFFSET resolver (2023-11-24 - 7 = 11-17).
    iso, _g, residue = _peel("I attended the Holiday Market a week before Black Friday.")
    assert iso is not None and iso.startswith("2023-11-17"), iso
    assert "black friday" not in residue.lower()


def test_no_date_is_still_none():
    iso, gran, residue = _peel("I really like coffee.")
    assert iso is None and gran is None
    assert residue == "I really like coffee."


def test_resolve_named_event_span_direct():
    from src.temporal.named_events import resolve_named_event_span
    res = resolve_named_event_span("we met on Black Friday", REF)
    assert res is not None
    d, gran, start, span = res
    assert (d.year, d.month, d.day) == (2023, 11, 24)
    assert span.lower() == "black friday"
    assert "we met on Black Friday"[start:start + len(span)] == span
    # a non-named clause → None (no fabrication)
    assert resolve_named_event_span("we met last week", REF) is None
