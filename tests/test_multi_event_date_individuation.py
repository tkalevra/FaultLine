"""MULTI-EVENT date individuation — same-type distinct-dated occurrence collapse (candidate b).

LongMemEval temporal-reasoning "how many X" cluster (a3838d2b and siblings: dinner parties,
weddings, graduation ceremonies, benefit concerts, movie festivals, …). A turn/chunk mentions
SEVERAL occurrences of the SAME named event type, each with its OWN date ("I attended a benefit
concert in February. … in April. … in August."). Every mention minted the IDENTICAL date-independent
occurrence handle (``_occurrence_handle`` is deliberately date-independent for convergence-by-
identity), so ``_detect_event_states_reified``'s per-call ``seen_handles`` dedup DROPPED every
occurrence after the first — N-1 distinct dated events silently lost, and the count-over-walk
under-reported "how many benefit concerts" (live: 3 stated → 1 stored).

FIX (subject-agnostic, deterministic, in-call/in-memory — NO DB read, NO LLM): when a same-type
occurrence carries a REAL date that DIFFERS from every real date already emitted for that base handle
THIS call, the two are provably distinct occurrences (one event cannot happen on two days) → the
later one is individuated into its own entity by suffixing the date onto the INTERNAL handle. It
still ``instance_of``-files at the SAME bare type, so it counts as a distinct instance.

CONVERGENCE PRESERVED (the fragmentation litmus, byte-for-byte): an undated restatement or an exact
same-date restatement of the same occurrence still folds onto ONE node — the fix fires ONLY on a
genuine real-date-vs-real-date conflict. Distinct-TITLED events were never collapsed and are
untouched. Gated by ``MULTI_EVENT_DATE_INDIVIDUATION`` (default ON); OFF → today's collapse exactly.

FAIL-ON-OLD: without the fix the three same-type dated concerts collapse to ONE occurrence handle.
"""
from datetime import datetime

import pytest


def _participation_objects(edges):
    """Distinct participated_in occurrence objects (the reified handles)."""
    return [
        (e.get("object") or "")
        for e in edges
        if (e.get("rel_type") or "") == "participated_in" and e.get("object")
    ]


def _dated_participation(edges):
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


REF = datetime(2026, 1, 1)

# Three occurrences of the SAME bare event type, each in its own sentence with its own month.
THREE_CONCERTS = ("I attended a benefit concert in February. I attended a benefit concert in April. "
                  "I attended a benefit concert in August.")


def test_three_same_type_dated_events_are_three_distinct_instances(reify):
    """THE FIX: three same-type dated occurrences → three DISTINCT occurrence entities."""
    edges = reify(THREE_CONCERTS, REF)
    objs = _participation_objects(edges)
    distinct = set(objs)
    assert len(distinct) >= 3, (
        f"same-type collapse: expected 3 distinct occurrence handles, got {sorted(distinct)}")
    # All three still file at the SAME bare type (instance_of object) — the L4 place is shared.
    types = {
        (e.get("object") or "")
        for e in edges
        if (e.get("rel_type") or "") == "instance_of"
        and (e.get("subject") or "").startswith("occurrence:")
    }
    assert types == {"benefit concert"}, f"instances must file at one bare type, got {types}"


def test_three_distinct_event_dates_survive(reify):
    """Each individuated occurrence keeps ITS OWN date (no smear / no loss)."""
    dates = _dated_participation(reify(THREE_CONCERTS, REF))
    distinct_dates = {d for d in dates.values() if d}
    assert len(distinct_dates) >= 3, f"expected 3 distinct event dates, got {dates}"


def test_flag_off_is_byte_for_byte_collapse(reify, monkeypatch):
    """Kill switch → today's behavior EXACTLY: three same-type mentions collapse to one handle."""
    import src.api.main as m
    monkeypatch.setattr(m, "MULTI_EVENT_DATE_INDIVIDUATION", False)
    objs = set(_participation_objects(reify(THREE_CONCERTS, REF)))
    assert len(objs) == 1, f"flag OFF must collapse to one occurrence, got {sorted(objs)}"


def test_same_date_restatement_still_converges(reify):
    """CONVERGENCE (not a conflict): the SAME occurrence restated with the SAME date folds to one."""
    turn = "I attended a benefit concert in April. I attended a benefit concert in April."
    objs = set(_participation_objects(reify(turn, REF)))
    assert len(objs) == 1, f"same-date restatement must converge, got {sorted(objs)}"


def test_undated_restatement_still_converges(reify):
    """CONVERGENCE (fragmentation litmus): an undated restatement folds onto the base handle —
    the fix fires ONLY on real-date-vs-real-date conflict, never on a dated+undated pair."""
    turn = "I attended a benefit concert in April. I also attended a benefit concert."
    objs = set(_participation_objects(reify(turn, REF)))
    assert len(objs) == 1, f"undated restatement must converge, got {sorted(objs)}"


def test_distinct_titled_events_unaffected(reify):
    """NO REGRESSION: distinctly-TITLED events were never collapsed and stay distinct."""
    turn = ("I attended the 'Spring Fling' gala in March. "
            "I attended the 'Harvest Ball' gala in October.")
    objs = set(_participation_objects(reify(turn, REF)))
    assert len(objs) >= 2, f"distinct titled events must stay distinct, got {sorted(objs)}"


def test_single_event_unchanged(reify):
    """NO REGRESSION: a single dated occurrence reifies to exactly one handle."""
    objs = set(_participation_objects(reify("I attended a benefit concert in April.", REF)))
    assert len(objs) == 1, f"single event must be one occurrence, got {sorted(objs)}"
