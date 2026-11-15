"""DATE-BLEED regression: per-clause date scoping in the reification seam.

LongMemEval temporal-reasoning cluster (a3045048, dcfa8644, gpt4_b4a80587). A COMPOUND turn carries
several events, each with its OWN date in its OWN clause ("I ordered her gift on June 8th and her
birthday party is on June 15th"; "I bought the shoes on Jan 18 and noticed the lace had broken on
Feb 1"). ``src.extraction.decompose`` splits SENTENCES, so the compound turn reaches
``_detect_event_states_reified`` as ONE clause; the per-clause peel bound only the FIRST resolvable
date to EVERY occurrence, so the second event inherited the first event's date (the bleed) and the
downstream elapsed / ordering math computed on two wrong-dated operands → the question missed.

FIX (subject-agnostic, deterministic): ``linguistics.segment_finite_clauses`` re-segments a turn into
its finite/coordinated sub-clauses via the dependency parse (spaCy clause segmentation; temporal
dependency — a date attaches to its GOVERNING predicate, Zhang & Xue arXiv:1809.00370 — NOT linear
proximity). The reification seam re-segments a clause ONLY when the parse yields ≥2 segment heads and
≥2 sub-clauses resolve DISTINCT dates (behavior-preserving on single-date / date-less turns).

FAIL-ON-OLD: without the split, the second event in each turn shares the first event's date.
"""
from datetime import datetime

import pytest


def _dated_participation(edges):
    """{object -> YYYY-MM-DD} for the reified participated_in occurrence edges."""
    out = {}
    for e in edges:
        if (e.get("rel_type") or "") == "participated_in":
            out[(e.get("object") or "")] = (e.get("event_date") or "")[:10]
    return out


@pytest.fixture(scope="module")
def reify():
    from src.extraction.linguistics import linguistics_available
    if not linguistics_available():
        pytest.skip("spaCy model unavailable (set SPACY_MODEL=en_core_web_sm)")
    from src.api.main import _detect_event_states_reified
    return _detect_event_states_reified


REF = datetime(2023, 2, 1)


# Two coordinated FINITE clauses, each an occurrence with its OWN date — the ex1/ex2 shape.
CASES = [
    ("I had a dentist appointment on January 10th and I attended a wedding on February 20th.",
     "wedding", "2023-02-20", "2023-01-10"),
    ("I went to a concert on March 3rd and I had a job interview on March 25th.",
     "occurrence: interview job", "2023-03-25", "2023-03-03"),
]


@pytest.mark.parametrize("turn,second_obj,own_date,first_date", CASES)
def test_second_event_takes_its_own_clause_date(reify, turn, second_obj, own_date, first_date):
    dates = _dated_participation(reify(turn, REF))
    assert second_obj in dates, f"second occurrence not reified: {dates}"
    # THE FIX: the second event carries ITS OWN clause's date, not the first clause's (the bleed).
    assert dates[second_obj] == own_date, (
        f"date-bleed: second event bound to {dates[second_obj]!r}, expected {own_date!r}")
    assert dates[second_obj] != first_date, "second event bled to the first clause's date"


def test_two_events_get_two_distinct_dates(reify):
    dates = _dated_participation(reify(CASES[0][0], REF))
    distinct = {d for d in dates.values() if d}
    assert len(distinct) >= 2, f"expected two distinct event dates, got {dates}"


# ── segment_finite_clauses unit contract on the exact exemplar strings ──────────────────────────
def test_segment_scopes_exemplar_dates():
    from src.extraction.linguistics import (
        linguistics_available, segment_finite_clauses, extract_event_date_and_residue as peel,
    )
    if not linguistics_available():
        pytest.skip("spaCy model unavailable")
    for turn in [
        "I ordered her gift on June 8th and her birthday party is on June 15th.",
        "I bought my Adidas running shoes on January 18th and noticed one of the shoelaces "
        "on my old Converse sneakers had broken on February 1st.",
    ]:
        subs = segment_finite_clauses(turn)
        assert len(subs) >= 2, f"compound turn not segmented: {subs}"
        dates = set()
        for s in subs:
            iso, _g, _r = peel(s, REF)
            if iso:
                dates.add(iso[:10])
        assert len(dates) >= 2, f"sub-clauses did not scope 2 distinct dates: {subs} -> {dates}"


def test_single_date_turn_unchanged():
    """Behavior-preserving: a simple single-predicate turn is NOT segmented."""
    from src.extraction.linguistics import linguistics_available, segment_finite_clauses
    if not linguistics_available():
        pytest.skip("spaCy model unavailable")
    turn = "I went to the store yesterday."
    assert segment_finite_clauses(turn) == [turn]
