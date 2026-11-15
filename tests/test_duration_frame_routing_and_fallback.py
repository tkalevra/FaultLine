"""Elapsed-duration cluster — duration-FRAME routing + durative START-anchor fallback.

Completes the "how long / how many <unit> … <activity> {before|until|since|when} <event>"
duration cluster (LongMemEval temporal-reasoning exemplars). Two seams, both flag-gated by
``TEMPORAL_CALC_DURATION_FRAMES`` (default ON) — flag OFF is byte-for-byte the base behavior:

ROUTING (``_detect_temporal_calc_intent``):
  • rule 2d WHEN-pivot extended to the explicit-unit lead-in "how many <days|weeks|months|
    years> … when <B>" (gpt4_4cd9eba1) — base matched only a bare "how long".
  • rule 2e SINCE-WHEN "how many days had passed since <A> when <B>" (dcfa8644) — a two-anchor
    ``between`` the base rules did not own.
  • rule 2c "until" as a before-frame synonym.
  A "how long"/"how many <unit>" ASK is a DURATION computation, never a "which came first"
  COMPARISON — the WH answer-type DOMINATES the relational cue (Li & Roth question taxonomy;
  ComplexTempQA duration-vs-comparison split). "which came first, A or B" stays NON-calc.

FALLBACK (``_apply_temporal_calc`` between-op):
  • a DURATIVE-activity lead-in ("how long had I been WORKING before I started at NovaTech")
    names an ACTIVITY, not a dated event — anchor-A cannot resolve by name. When the pivot B
    resolves but A does not, ground A to the EARLIEST dated fact strictly before B within the
    already-scope-projected candidate set (the beginning of the referenced history).

Deterministic (regex + min over the real event_date column), subject-agnostic (no domain
word-list), no DB / no LLM / no network for these pins.
"""
import os
import pytest


def _intent(q):
    import src.api.main as m
    return m._detect_temporal_calc_intent(q.lower())


# ── ROUTING (flag ON = default) ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,op,sf,ru", [
    # gpt4_93159ced — before-frame durative, unresolvable activity lead-in.
    ("how long have I been working before I started my current job at NovaTech?",
     "between", "earliest_before_b", None),
    # e4e14d04 — when-pivot durative (bare "how long").
    ("how long had I been a member of Book Lovers Unite when I attended the meetup?",
     "between", "earliest_before_b", None),
    # gpt4_4cd9eba1 — when-pivot durative with an EXPLICIT unit lead-in (NEW: rule 2d ext).
    ("how many weeks have I been accepted into the exchange program "
     "when I started attending the pre-departure orientation?",
     "between", "earliest_before_b", "weeks"),
    # dcfa8644 — since-when two-anchor (NEW: rule 2e).
    ("how many days had passed since I bought my Adidas running shoes "
     "when I realized one of the shoelaces broke?",
     "between", None, "days"),
    # gpt4_cd90e484 — before-frame durative.
    ("how long did I use my new binoculars before I saw the American goldfinches?",
     "between", "earliest_before_b", None),
    # 982b5123 — elapsed "how many <unit> ago".
    ("how many months ago did I book the Airbnb in San Francisco?",
     "elapsed", None, "months"),
])
def test_routing_flag_on(q, op, sf, ru):
    r = _intent(q)
    assert r.get("op") == op, r
    assert r.get("start_fallback") == sf, r
    assert r.get("requested_unit") == ru, r


def test_comparison_is_not_a_calc_regression_guard():
    # "which came first, A or B" is a COMPARISON (ordered-list render), NOT a duration calc.
    # It must NOT be hijacked into the calc lane by any duration-frame rule.
    assert _intent("which came first, the meetup or the book club?") == {}
    assert _intent("did the meetup happen before the book club?").get("op") == "allen"


def test_after_frame_not_marked_for_fallback():
    # Q7 "how many days … after <B>" — A FOLLOWS B and is a resolvable event, so it must
    # NOT carry the earliest-before-pivot start fallback (that is a before/when-only net).
    r = _intent("how many days did it take to find a house after starting to work with rachel?")
    assert r.get("op") == "between"
    assert r.get("start_fallback") is None


# ── FLAG OFF = byte-identical to base (the NEW shapes do not route / no marker) ───────────

def test_flag_off_byte_identical(monkeypatch):
    import src.api.main as m
    monkeypatch.setattr(m, "TEMPORAL_CALC_DURATION_FRAMES", False)
    # NEW shapes go dark (base 344776dd behavior).
    assert m._detect_temporal_calc_intent(
        "how many weeks have i been accepted into the exchange program "
        "when i started attending the pre-departure orientation?") == {}
    assert m._detect_temporal_calc_intent(
        "how many days had passed since i bought my adidas running shoes "
        "when i realized one of the shoelaces broke?") == {}
    # Base shapes still route — but WITHOUT the start_fallback marker.
    r = m._detect_temporal_calc_intent(
        "how long have i been working before i started my current job at novatech?")
    assert r.get("op") == "between"
    assert "start_fallback" not in r
    # A genuine comparison stays non-calc regardless of flag.
    assert m._detect_temporal_calc_intent("which came first, the meetup or the book club?") == {}


# ── FALLBACK compute — unresolvable A + resolvable pivot B + pre-pivot dated fact ─────────

def test_start_fallback_computes_span(monkeypatch):
    import src.api.main as m
    # No DB: force the by-name anchor resolvers to MISS so A ("working") is truly unresolved
    # and only the in-memory pivot B ("novatech") + the fallback drive the result.
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    facts = [
        {"subject": "you", "object": "first job", "rel_type": "participated_in",
         "event_date": "2019-01-26", "event_date_granularity": "day"},
        {"subject": "you", "object": "novatech", "rel_type": "start_at",
         "event_date": "2023-10-25", "event_date_granularity": "month"},
    ]
    intent = {"calc": {"op": "between", "anchor": "working", "anchor_b": "novatech",
                       "start_fallback": "earliest_before_b", "requested_unit": None}}
    res = m._apply_temporal_calc(facts, intent, user_id=None)
    assert res is not None
    assert res.get("miss") is False, res
    # grounded in the two REAL endpoint dates — the earliest pre-pivot fact + the pivot.
    assert "2019-01-26" in (res.get("cited_dates") or [])
    assert "2023-10-25" in (res.get("cited_dates") or [])
    # a multi-year span was computed (exact months is the calendar-render layer's concern).
    assert "year" in (res.get("answer") or "")


def test_start_fallback_fail_safe_no_pre_pivot_fact(monkeypatch):
    import src.api.main as m
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    # Only the pivot is dated; NOTHING dated before it → the fallback finds no start →
    # the existing undated-pair MISS (never a fabricated number).
    facts = [
        {"subject": "you", "object": "novatech", "rel_type": "start_at",
         "event_date": "2023-10-25", "event_date_granularity": "month"},
    ]
    intent = {"calc": {"op": "between", "anchor": "working", "anchor_b": "novatech",
                       "start_fallback": "earliest_before_b", "requested_unit": None}}
    res = m._apply_temporal_calc(facts, intent, user_id=None)
    assert res is not None
    assert res.get("miss") is True, res


def test_start_fallback_off_when_flag_off(monkeypatch):
    import src.api.main as m
    monkeypatch.setattr(m, "TEMPORAL_CALC_DURATION_FRAMES", False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    facts = [
        {"subject": "you", "object": "first job", "rel_type": "participated_in",
         "event_date": "2019-01-26", "event_date_granularity": "day"},
        {"subject": "you", "object": "novatech", "rel_type": "start_at",
         "event_date": "2023-10-25", "event_date_granularity": "month"},
    ]
    # Marker present, but the flag gates the fallback code → no substitution → MISS.
    intent = {"calc": {"op": "between", "anchor": "working", "anchor_b": "novatech",
                       "start_fallback": "earliest_before_b", "requested_unit": None}}
    res = m._apply_temporal_calc(facts, intent, user_id=None)
    assert res is not None
    assert res.get("miss") is True, res
