"""Query-side temporal bug fixes (both deterministic, lean-query rendering/routing).

FIX 1 — Q6: the QUESTION's requested unit drives duration rendering at FULL precision;
ambiguous → largest EXACT unit else days. NEVER the old "about N weeks" bucketing
(round(n/7)) that turned a correct 30-day answer into "about 4 weeks".

FIX 2 — Q9: a two-anchor temporal CALC ("how long between the festival OF Holi and the
mass at St. Mary'S Church") must NOT be hijacked by the possessive `" of "`/`'s`
decomposition (which returned a garbage chain and suppressed `temporal_between`). When a
2-anchor calc is detected for the query, `_decompose_query_steps` bails so the calc owns
the chain. A genuine possessive (no calc) still decomposes.
"""
from src.api import main


# ── FIX 1 — _humanize_delta requested-unit + exact-only coarsening ─────────────────

def test_humanize_requested_unit_full_precision_days():
    # value=30, requested days → "30 days" (was "about 4 weeks")
    assert main._humanize_delta(30, "day", requested_unit="days") == "30 days"
    assert main._humanize_delta(14, "day", requested_unit="days") == "14 days"


def test_humanize_requested_unit_weeks_and_months():
    assert main._humanize_delta(14, "day", requested_unit="weeks") == "2 weeks"
    assert main._humanize_delta(60, "day", requested_unit="months") == "2 months"


def test_humanize_ambiguous_largest_exact_unit_else_days():
    # exact week-multiple → weeks; else days. NEVER "about".
    assert main._humanize_delta(14, "day") == "2 weeks"
    assert main._humanize_delta(21, "day") == "3 weeks"
    assert main._humanize_delta(30, "day") == "30 days"
    assert main._humanize_delta(16, "day") == "16 days"


def test_humanize_never_emits_about():
    for n in range(0, 400):
        out = main._humanize_delta(n, "day")
        assert "about" not in out, f"n={n} rendered {out!r}"


def test_detect_requested_duration_unit():
    assert main._detect_requested_duration_unit(
        "how many days between x and y") == "days"
    assert main._detect_requested_duration_unit(
        "how many weeks between x and y") == "weeks"
    assert main._detect_requested_duration_unit(
        "how many months between x and y") == "months"
    # no unit asked → None (ambiguous)
    assert main._detect_requested_duration_unit(
        "how long between x and y") is None


def test_between_calc_intent_carries_requested_unit():
    ci = main._detect_temporal_calc_intent("how many days between a and b")
    assert ci.get("op") == "between"
    assert ci.get("requested_unit") == "days"
    ci2 = main._detect_temporal_calc_intent("how long between a and b")
    assert ci2.get("op") == "between"
    assert ci2.get("requested_unit") is None


# ── FIX 2 — calc gate on the possessive decomposition ─────────────────────────────

_Q9 = ("how long between the Hindu festival of Holi and the Sunday mass "
       "at St. Mary's Church?")


def test_q9_calc_detected_with_two_anchors():
    ci = main._detect_temporal_calc_intent(_Q9.strip().lower())
    assert ci.get("op") == "between"
    assert ci.get("anchor") and ci.get("anchor_b")


def test_q9_decompose_steps_is_gated_off():
    # The 2-anchor calc owns the chain → possessive decomposition must bail.
    assert main._decompose_query_steps(_Q9) is None
    # Same result whether or not the intent is passed (in-fn fallback detect).
    ci = main._detect_temporal_calc_intent(_Q9.strip().lower())
    assert main._decompose_query_steps(_Q9, intent={"calc": ci}) is None


def test_genuine_possessive_still_decomposes():
    # No calc → unchanged behaviour: real possessive/attribute chains decompose.
    assert main._decompose_query_steps("what is my mother's age") == \
        ["my", "mother", "age"]
    assert main._decompose_query_steps("the age of my mother") == \
        ["my", "mother", "age"]
    assert main._decompose_query_steps("how old is helen") == ["helen", "age"]


# ── FIX 3 — Q10: calc anchor resolution is COVERAGE-RANKED + WORD-BOUNDARY ──────────
# Bug: "how many days before the 'Rack Fest' did I participate in 'Turbocharged
# Tuesdays'" collapsed to "0 days". The 'rack fest' anchor was OR-substring-matched
# against an unrelated "local racing t<rack>" occurrence (different date) and won via the
# ORDER BY event_date ASC LIMIT 1 tiebreak — so BOTH anchors resolved to the same wrong
# date. Fix: rank by how many DISTINCT anchor terms match as WHOLE WORDS; the coherent
# multi-word name wins over a single-token accidental substring hit. Deterministic.

def _dated(subject, obj, iso):
    return {"subject": subject, "object": obj, "rel_type": "participated_in",
            "event_date": iso, "_subject_id": "user", "_object_id": obj}


def test_calc_anchor_substring_collision_does_not_win():
    # "rack" is a substring of "racing track" — the WRONG (06-14) fact must NOT win the
    # 'rack fest' anchor; the coherent two-term "rack fest" (06-18) must.
    facts = [
        _dated("you", "local racing track", "2026-06-14"),     # substring-collides on "rack"
        _dated("you", "turbocharged tuesdays", "2026-06-14"),
        _dated("you", "rack fest", "2026-06-18"),
    ]
    f = main._resolve_calc_anchor_fact("the 'rack fest'", facts)
    assert f is not None
    assert str(f.get("event_date"))[:10] == "2026-06-18", (
        f"rack fest must resolve to 06-18, got {f.get('event_date')}")


def test_calc_anchor_coverage_prefers_full_name():
    facts = [
        _dated("you", "turbocharged tuesdays", "2026-06-14"),
        _dated("you", "rack fest", "2026-06-18"),
    ]
    f = main._resolve_calc_anchor_fact("'turbocharged tuesdays' event", facts)
    assert str(f.get("event_date"))[:10] == "2026-06-14"


def test_q10_between_intent_parses_two_distinct_anchors():
    q = ("how many days before the 'rack fest' did i participate in the "
         "'turbocharged tuesdays' event?")
    ci = main._detect_temporal_calc_intent(q)
    assert ci.get("op") == "between"
    a, b = ci.get("anchor"), ci.get("anchor_b")
    assert a and b
    # the two anchors must name DIFFERENT events (rack vs turbocharged)
    assert "rack" in a.lower() and "turbocharged" in b.lower()


def test_q10_calc_resolves_4_days_from_two_distinct_dates():
    # End-to-end of the in-memory calc: distinct anchors → distinct dates → 4 days.
    facts = [
        _dated("you", "local racing track", "2026-06-14"),
        _dated("you", "turbocharged tuesdays", "2026-06-14"),
        _dated("you", "rack fest", "2026-06-18"),
    ]
    intent = {"calc": {"op": "between", "anchor": "the 'rack fest'",
                       "anchor_b": "the 'turbocharged tuesdays' event",
                       "requested_unit": "days"}}
    res = main._apply_temporal_calc(facts, intent)
    assert res and not res.get("miss"), res
    assert abs(res.get("value")) == 4, res
    cited = sorted(c[:10] for c in (res.get("cited_dates") or []))
    assert cited == ["2026-06-14", "2026-06-18"], cited


# ── CLUSTER: elapsed-duration-calc — "how many <unit> ago did I X" ELAPSED lane ──
# LongMemEval elapsed-duration-calc cluster (qids gpt4_b5700ca9, 6cb6f249, 71017276,
# gpt4_1d4ab0c9, gpt4_8279ba02): the ELAPSED operator only recognised the bare
# "how long ago" phrasing and MISSED the far-more-common "how many days/weeks/months/
# years ago did I …" — so those questions never entered the temporal-calc lane at all
# and fell through to the raw walk (garbage recall, no elapsed math). The "ago" cue is
# the elapsed signal; the unit-carrying lead-in is the same DURATION answer-type family.

def test_elapsed_how_many_days_ago_enters_calc_lane():
    ci = main._detect_temporal_calc_intent(
        "how many days ago did i buy a smoker")
    assert ci.get("op") == "elapsed", ci
    assert ci.get("requested_unit") == "days", ci
    assert ci.get("anchor") == "buy a smoker", ci


def test_elapsed_how_many_weeks_ago_enters_calc_lane():
    ci = main._detect_temporal_calc_intent(
        "how many weeks ago did i meet up with my aunt and receive "
        "the crystal chandelier")
    assert ci.get("op") == "elapsed", ci
    assert ci.get("requested_unit") == "weeks", ci
    # subject-agnostic: whatever noun the user named after the pivot
    assert "aunt" in (ci.get("anchor") or "")


def test_elapsed_how_long_ago_still_recognised():
    # Regression: the original bare "how long ago" phrasing is unchanged (no unit).
    ci = main._detect_temporal_calc_intent("how long ago did i move to boston")
    assert ci.get("op") == "elapsed", ci
    assert ci.get("requested_unit") is None, ci


def test_elapsed_how_many_days_ago_computes_over_dated_fact():
    # End-to-end: the resolved ELAPSED op grounds the anchor to its dated fact and
    # returns a real day-count from the event_date (not a fabricated / garbage value).
    from datetime import date, timedelta
    _iso = (date.today() - timedelta(days=10)).isoformat()
    facts = [_dated("you", "buy a smoker", _iso)]
    intent = {"calc": {"op": "elapsed", "anchor": "buy a smoker",
                       "requested_unit": "days"}}
    res = main._apply_temporal_calc(facts, intent)
    assert res and not res.get("miss"), res
    # 10 (exclusive) or 11 (inclusive last day, and/or a UTC-vs-local day boundary) —
    # both are the correct elapsed count for a 10-days-ago event; the point is a REAL
    # grounded number off the event_date, not the raw-walk garbage the miss produced.
    assert res.get("value") in (10, 11), res
    assert _iso in (res.get("cited_dates") or []), res
