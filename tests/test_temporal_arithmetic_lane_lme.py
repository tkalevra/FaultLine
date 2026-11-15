"""Deterministic temporal-arithmetic LANE — end-to-end contract pins (LME exemplars).

WHY THIS FILE EXISTS
--------------------
FaultLine's temporal-arithmetic query lane is ALREADY BUILT: first/last ordering
(`_apply_get_first_last`), duration-between / days-before-pivot / elapsed / age-at-event
(`_apply_temporal_calc` + the pure operators `_op_between` / `_op_elapsed` / `_op_duration`
/ `_op_allen`), and honest unit rendering (`_humanize_delta`). A live diagnosis over the
LongMemEval-oracle temporal misses (2026-07-23) confirmed the lane FIRES and COMPUTES
CORRECTLY whenever BOTH operands carry a captured `event_date` — the residual LME misses
are UPSTREAM capture/typing gaps (the operand's date is absent, or captured under a surface
that shares no content words with the query phrase), NOT arithmetic gaps.

This suite PINS the deterministic contract the task cares about, grounded in the exact
LME-oracle exemplars + their gold numbers, so a future change cannot silently regress the
math or the fail-loud abstention. All arithmetic is PURE Python date/timedelta over the
captured `event_date` column, grounded in Allen's interval algebra (half-open [gs, ge)
intervals; `_op_between` measures earlier.end_inclusive → later.start; `_op_elapsed`
recomputes vs the *query* reference, NEVER the stale stored temporal_status). NO LLM, NO
cosine, NO fuzzy — a miss FAILS LOUD (miss=True) rather than fabricating a number.

Refs: Allen (1983) interval algebra; CLAUDE.md "Temporal Determination"; the operator
library docstrings in src/api/main.py; DEV design docs cited there.
"""
from datetime import date, timedelta

from src.api import main


def _day(d: date):
    """A single-day half-open granule interval: [d, d+1), granularity 'day'."""
    return (d, d + timedelta(days=1), "day")


def _dated(subject, obj, iso, rel="participated_in", gran="day"):
    """An in-memory recall fact dict carrying a real event_date (the authoritative column
    value). Surfaces are readable so the content-word anchor resolver can reach them."""
    return {
        "subject": subject, "object": obj, "rel_type": rel,
        "event_date": iso, "event_date_granularity": gran,
        "fact_class": "B", "confidence": 0.8,
    }


# ══════════════════════════════════════════════════════════════════════════════════
# 1. FIRST / LAST ordering — "which happened first, A or B" (both operands dated)
#    LME: gpt4_385a5000 (tomatoes vs marigolds), gpt4_6ed717ea (dog bed vs training pads)
# ══════════════════════════════════════════════════════════════════════════════════

def test_first_last_returns_earlier_operand_first():
    op_a, op_b = "uuid-dogbed", "uuid-trainingpads"
    facts = [
        _dated(op_a, "orthopedic dog bed", "2023-04-29"),   # later
        _dated(op_b, "training pads for luna", "2023-04-20"),  # EARLIER
    ]
    out = main._apply_get_first_last(facts, "first", operand_ids=[op_a, op_b])
    # deterministic min(event_date): the EARLIER operand is returned first.
    assert out, "operand-scoped first/last must return a non-empty ordered set"
    assert out[0]["object"] == "training pads for luna"
    assert out[0]["event_date"] == "2023-04-20"


def test_last_returns_later_operand_first():
    op_a, op_b = "uuid-a", "uuid-b"
    facts = [
        _dated(op_a, "coffee maker purchase", "2023-05-01"),
        _dated(op_b, "stand mixer malfunction", "2023-04-10"),  # earlier
    ]
    out = main._apply_get_first_last(facts, "last", operand_ids=[op_a, op_b])
    assert out and out[0]["object"] == "coffee maker purchase"  # max(event_date)


def test_first_last_no_dated_fact_fails_loud_empty():
    # A boundary ask with NO dated operand → undefined boundary → [] (never a guessed pick).
    undated = [{"subject": "u", "object": "x", "rel_type": "owns"}]
    assert main._apply_get_first_last(undated, "first", operand_ids=["u"]) == []


# ══════════════════════════════════════════════════════════════════════════════════
# 2. DURATION-BETWEEN — "how many days between X and Y"
#    LME: 08f4fc43 (Sunday mass → Ash Wednesday = 30 days)
# ══════════════════════════════════════════════════════════════════════════════════

def test_duration_between_exact_days():
    facts = [
        _dated("you", "sunday mass at st marys church", "2023-01-15"),
        _dated("you", "ash wednesday service", "2023-02-14"),
    ]
    intent = {"calc": {"op": "between", "anchor": "the sunday mass",
                       "anchor_b": "the ash wednesday service",
                       "requested_unit": "days"}}
    res = main._apply_temporal_calc(facts, intent, user_id="test")
    assert res and not res.get("miss")
    assert res["value"] == 30                       # 2023-02-14 − 2023-01-15
    assert res["answer"].startswith("30 days")
    assert set(res["cited_dates"]) == {"2023-01-15", "2023-02-14"}


# ══════════════════════════════════════════════════════════════════════════════════
# 3. DAYS-BEFORE a pivot — "how many days before <pivot> did I <event>"
#    LME: c8090214 (Holiday Market → iPhone = 7 days), a3045048 (gift → party = 7 days)
#    The before/after word only FRAMES the ask; the magnitude is the symmetric |B − A|
#    (detector rule 2b maps it onto the same proven `between` op).
# ══════════════════════════════════════════════════════════════════════════════════

def test_days_before_pivot_exact():
    facts = [
        _dated("you", "ordered the customized gift", "2022-04-15", rel="order_for"),
        _dated("you", "best friend birthday party", "2022-04-22"),
    ]
    intent = {"calc": {"op": "between", "anchor": "the birthday party",
                       "anchor_b": "ordered the gift", "requested_unit": "days"}}
    res = main._apply_temporal_calc(facts, intent, user_id="test")
    assert res and not res.get("miss")
    assert res["value"] in (7, -7)                  # sign-aware; magnitude is the answer
    assert res["answer"].startswith("7 days")


# ══════════════════════════════════════════════════════════════════════════════════
# 4. DAYS-SINCE / elapsed — recompute vs the QUERY reference (NOT stored temporal_status)
#    LME: gpt4_b5700ca9 (Maundy Thursday 2023-04-06, asked 2023-04-10 → 4 days)
# ══════════════════════════════════════════════════════════════════════════════════

def test_days_since_recomputed_from_event_date():
    event = _day(date(2023, 4, 6))          # captured event_date
    ref = date(2023, 4, 10)                 # the query's own reference ("now")
    # elapsed = ref − event.end_inclusive, deterministic, recomputed vs query-now.
    assert main._op_elapsed(event, ref) == 4
    assert main._humanize_delta(4, "day", requested_unit="days") == "4 days"


# ══════════════════════════════════════════════════════════════════════════════════
# 5. MONTHS-AGO — coarse unit rendered at full precision (NEVER "about N")
#    LME: 982b5123 (booked 2022-12-27, asked 2023-05-27 → "Five months ago")
# ══════════════════════════════════════════════════════════════════════════════════

def test_months_ago_full_precision():
    booked = _day(date(2022, 12, 27))
    asked = date(2023, 5, 27)
    days = main._op_elapsed(booked, asked)
    assert days == 151
    assert main._humanize_delta(days, "day", requested_unit="months") == "5 months"


# ══════════════════════════════════════════════════════════════════════════════════
# 6. UNDATED OPERAND → FAIL LOUD (abstain, never fabricate a number)
# ══════════════════════════════════════════════════════════════════════════════════

def test_undated_operand_pair_fails_loud(monkeypatch):
    # anchor A resolves to a dated fact; anchor B has NO dated fact anywhere → the DB
    # backfill returns None (monkeypatched to keep the test hermetic) → miss, not a guess.
    monkeypatch.setattr(main, "_resolve_calc_anchor_date_db", lambda *a, **k: None)
    facts = [_dated("you", "sunday mass at st marys church", "2023-01-15")]
    intent = {"calc": {"op": "between", "anchor": "the sunday mass",
                       "anchor_b": "the never mentioned festival",
                       "requested_unit": "days"}}
    res = main._apply_temporal_calc(facts, intent, user_id="test")
    assert res and res.get("miss") is True
    assert res.get("value") is None                 # NO fabricated number
    assert not res.get("cited_dates")


# ══════════════════════════════════════════════════════════════════════════════════
# 7. DETECTION — the three query SHAPES route to the correct op + anchors (deterministic)
# ══════════════════════════════════════════════════════════════════════════════════

def test_detect_between_shape():
    ci = main._detect_temporal_calc_intent(
        "how many days between the sunday mass and the ash wednesday service")
    assert ci.get("op") == "between"
    assert ci.get("requested_unit") == "days"


def test_detect_days_before_pivot_shape():
    ci = main._detect_temporal_calc_intent(
        "how many days before my birthday party did i order her gift")
    assert ci.get("op") == "between"          # before/after frames → symmetric between


def test_detect_elapsed_days_ago_shape():
    ci = main._detect_temporal_calc_intent(
        "how many days ago did i attend the maundy thursday service")
    assert ci.get("op") == "elapsed"
    assert ci.get("requested_unit") == "days"


def test_detect_first_last_ordinal_and_operands():
    # the ordinal + the two named operands the ordinal must be scoped to.
    intent = main._detect_temporal_query_intent(
        "which item did i purchase first, the dog bed or the training pads")
    assert intent.get("ordinal") == "first"
    ops = main._extract_comparison_operands(
        "which item did i purchase first, the dog bed or the training pads")
    assert "the dog bed" not in ops  # leading determiner trimmed
    assert any("dog bed" in o for o in ops) and any("training pad" in o for o in ops)
