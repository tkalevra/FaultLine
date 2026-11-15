"""Durative-activity duration cluster — "how long had I been <A> when <B>".

LongMemEval temporal-reasoning exemplars cc6d1ec1 (bird watching), c9f37c46 (stand-up),
e4e14d04 (Book Lovers Unite). The duration = B_date − A_START_date. TWO defects blocked it:

INGEST (cc6d1ec1) — COMPOUND-EVENTIVE-HEAD COLLAPSE. ``en_core_web_sm`` fractures the gerund-
nominal noun compound "bird watching (workshop)" into head ``bird`` + acl ``watching`` (+ object
``workshop``), so ``_np_phrase`` recovered only ``bird``. BOTH "getting into **bird** watching"
(the START) and "attended the **bird** watching workshop" (the WHEN) collapsed to the object
``bird`` and, being unique on ``(subject, rel_type, object)``, MERGED into one row — the duration
walk could only find ONE dated anchor where it needs two. Fix: ``_gerund_nominal_compound`` recovers
the full contiguous compound so the two occurrences stay DISTINCT ("bird watching" vs "bird watching
workshop"). Structural (right-headed English noun compound; spaCy dependency parse), subject-agnostic.

QUERY (c9f37c46, all three) — the "how long had I been <A> WHEN <B>" phrasing matched NO temporal-
calc shape (rules need before/after/between), so the proven ``between`` op + interval math never
fired even when BOTH dated anchors were captured cleanly. Fix: rule 2d maps the when-pivot durative
duration onto ``between(A_start, B)``.

Both FAIL on the pre-fix code:
  • pre-fix ``analyze_events`` returns event ``"bird"`` for BOTH turns (collapse) — the distinctness
    assertions fail.
  • pre-fix ``_detect_temporal_calc_intent`` returns ``{}`` for a when-pivot question — the op
    assertions fail.
Deterministic (spaCy en_core_web_sm), no DB, no LLM, no network.
"""
import pytest


# ── INGEST: gerund-nominal compound recovery keeps the two occurrences DISTINCT ──────────────

def _events(text):
    from src.extraction.linguistics import analyze_events, linguistics_available
    if not linguistics_available():
        pytest.skip("spaCy model unavailable")
    return [ea.event for ea in analyze_events(text) if ea and not ea.negated]


def test_when_turn_keeps_compound_workshop_head():
    # "attended the bird watching workshop" — the WHEN reference event. Pre-fix: "bird".
    evs = _events("I attended the bird watching workshop.")
    assert "bird watching workshop" in evs, evs


def test_start_turn_keeps_gerund_nominal_activity():
    # "getting into bird watching …" — the START of the durative activity. Pre-fix: "bird".
    evs = _events("I've been getting into bird watching for about three months now.")
    assert "bird watching" in evs, evs


def test_start_and_when_are_distinct_anchors_no_collision():
    # THE COLLISION the fix removes: on old code BOTH turns yield "bird" → one merged row.
    start = _events("I've been getting into bird watching for about three months now.")
    when = _events("I attended the bird watching workshop.")
    # each captured its own object …
    assert "bird watching" in start, start
    assert "bird watching workshop" in when, when
    # … and the two occurrence objects are NOT identical (no unique-key collapse).
    assert set(start) & set(when) != set(start), (start, when)
    assert "bird watching workshop" not in start, start


def test_generalizes_to_other_gerund_compounds():
    # Subject-agnostic: any NOUN + V-ing (+ NOUN) compound the parser splits.
    assert "wine tasting" in _events("I've been getting into wine tasting lately.") or \
           "wine tasting" in _events("I attended a wine tasting."), "wine tasting"


def test_does_not_over_capture_plain_eventive_head():
    # A plain eventive head with no trailing gerund is unchanged (no spurious merge).
    evs = _events("I attended a cooking class.")
    assert any("class" in e for e in evs), evs
    # the head is not mangled into a gerund compound
    assert "cooking class watching" not in evs


# ── QUERY: the when-pivot durative duration maps onto the between op ──────────────────────────

def _calc(q):
    import src.api.main as m
    return m._detect_temporal_calc_intent(q.lower())


@pytest.mark.parametrize("q,a_has,b_has", [
    ("How long had I been bird watching when I attended the bird watching workshop?",
     "bird watching", "workshop"),
    ("How long had I been watching stand-up comedy specials regularly when I attended "
     "the open mic night at the local comedy club?", "stand-up", "open mic"),
    ("How long had I been a member of 'Book Lovers Unite' when I attended the meetup?",
     "book lovers", "meetup"),
])
def test_when_pivot_maps_to_between(q, a_has, b_has):
    calc = _calc(q)
    assert calc.get("op") == "between", calc          # pre-fix: {}  (no shape matched)
    assert a_has in calc.get("anchor", ""), calc
    assert b_has in calc.get("anchor_b", ""), calc


def test_when_pivot_does_not_break_plain_between():
    calc = _calc("How long between my dentist visit and my surgery?")
    assert calc.get("op") == "between", calc
    assert "dentist" in calc.get("anchor", "")
    assert "surgery" in calc.get("anchor_b", "")


def test_when_pivot_does_not_hijack_allen_yesno():
    # A genuine yes/no boundary comparison (no "how long" cue) stays Allen.
    calc = _calc("Did I attend the workshop before the meetup?")
    assert calc.get("op") == "allen", calc
