"""Knowledge-update SCALAR capture — LME KU cluster regression pins (bug KU-SCALAR).

These pin the deterministic spine deriver's capture of four LongMemEval knowledge-update scalars
whose stated VALUE is the gold answer. Two of the cluster already have dedicated fixes + pins
elsewhere and are re-asserted here as a cluster litmus:

  • 945e3d21  "three times a week" rate-in-relative-clause  → tests/test_spine_rate_adverbial.py
  • ce6d2d27  "on Friday" day-of-week schedule scalar       → tests/test_schedule_recurrence_scalar.py

The other two capture via GENERIC deterministic chains (a possessed measure-of NP, a locative
"on page N" numeral) but were previously UNPINNED — this file is their regression guard:

  • 6a1eabeb  "personal best time of 25:50"  → (user, personal_best_time, "25:50")   [colon-time]
  • 184da446  "on page 220"                  → (user, page, "220")                   [page count]

Plus a HARD-LINE / mis-typing guard: "cocktail-making class" is an Organization/Concept (GLiNER2's
own typing), NEVER an Animal, so the possession edge must NOT surface as (user, has_pet, class) —
a category error the recall junk "you have a pet that is Class" would show. The deterministic deriver
does not emit has_pet here; this pins that it never regresses into doing so.

Deterministic, subject-agnostic (no domain/word list, no colon-time/page/day literal). The exact
LME gold-turn phrasings are used so a stale-image regression on the live bench is caught at unit
level. Run: python3 -m pytest tests/test_ku_scalar_capture.py -q  (tests/ is gitignored → git add -f)
"""
from datetime import date

import pytest

REF = date(2026, 6, 21)


def _model_ok() -> bool:
    try:
        from src.extraction.linguistics import linguistics_available
        return linguistics_available()
    except Exception:
        return False


requires_model = pytest.mark.skipif(not _model_ok(), reason="en_core_web_sm not installed in test env")


def _derive(sentence, ref=REF):
    from src.extraction.linguistics import derive_sentence_facts
    return list(derive_sentence_facts(sentence, reference=ref))


def _scalars(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts if f.scalar_datatype == "string"]


# ── 6a1eabeb — colon-time value stated as the personal best (the gold 25:50) ───────────────────────
@requires_model
def test_colon_time_personal_best_captured_as_scalar():
    # LME gold turn: "By the way, I'm hoping to beat my personal best time of 25:50 this time around."
    facts = _derive("I'm hoping to beat my personal best time of 25:50 this time around.")
    sc = _scalars(facts)
    # the colon-time is captured VERBATIM as a user scalar (not split into 25 / 50, not dropped).
    assert any(s == "user" and v == "25:50" for (s, _r, v) in sc), sc


@requires_model
def test_colon_time_copula_variant_captured():
    facts = _derive("My personal best time is 25:50.")
    assert any(s == "user" and v == "25:50" for (s, _r, v) in _scalars(facts)), facts


# ── 184da446 — "on page N" reading-progress numeral (the gold 220) ─────────────────────────────────
@requires_model
def test_on_page_numeral_captured_as_scalar():
    # LME gold turn: "…I'm now on page 220, and it's amazing how much I've learned so far!"
    facts = _derive("I'm now on page 220, and it's amazing how much I've learned so far.")
    sc = _scalars(facts)
    assert any(s == "user" and v == "220" for (s, _r, v) in sc), sc


@requires_model
def test_on_page_earlier_value_also_captured():
    # the superseded earlier value must also land (recency picks the latest at query time).
    facts = _derive("I'm currently on page 200.")
    assert any(s == "user" and v == "200" for (s, _r, v) in _scalars(facts)), facts


# ── ce6d2d27 — day-of-week schedule scalar AND no has_pet mis-typing ───────────────────────────────
@requires_model
def test_cocktail_class_day_captured_and_not_mistyped_as_pet():
    # LME gold turn: "…I have a cocktail-making class on Friday, and I'm thinking of experimenting…"
    facts = _derive("I have a cocktail-making class on Friday, and I'm thinking of "
                    "experimenting with some new recipes.")
    # the day-of-week schedule scalar lands on the user anchor.
    assert any(s == "user" and "friday" in v for (s, _r, v) in _scalars(facts)), facts
    # HARD LINE / Pitfall-11 mis-typing guard: a class is NOT an animal → NEVER a has_pet edge.
    assert not any(r == "has_pet" for (_s, r, _o) in
                   [(f.subject, f.rel_type, f.object) for f in facts]), \
        f"class mis-typed as a pet (has_pet junk): {facts!r}"


# ── 945e3d21 — rate-in-relative-clause (the gold three times a week) ───────────────────────────────
@requires_model
def test_yoga_rate_relative_clause_captured():
    # LME gold turn: "…I attend yoga classes, which is three times a week - it really helps…"
    facts = _derive("I'm more focused on days when I attend yoga classes, which is three "
                    "times a week - it really helps me clear my head.")
    sc = _scalars(facts)
    assert any("yoga" in s and v == "three times a week" for (s, _r, v) in sc), sc


# ── Hole B — a DROPPED bare NUMBER is HELD in Class C (residue net), TYPE-FIRST ─────────────────────
# NET_UNTYPABLE_RESIDUE (default ON). A number is a distinct content-bearing dependent (UD nummod:
# "a number phrase that serves to modify the meaning of the noun with a quantity"). When the parse
# strands one so it binds into NO edge (0e4e4c46 — the dash construction "…Ride - 132 points!"
# mangles the parse and 132 lands in no edge), the residue guard must now flag it so the sentence is
# HELD in the Class-C short-term lane ("we don't forget"), not silently dropped from both lanes.
def _residue_for(sentence, ref=REF):
    from src.extraction.linguistics import derive_sentence_facts
    res: list = []
    derive_sentence_facts(sentence, reference=ref, residue_out=res)
    return res


@requires_model
def test_dropped_bare_number_routed_to_class_c_residue():
    # 0e4e4c46 — the gold answer 132 binds into NO deriver edge (dash-mangled parse). It MUST surface
    # as residue so the harvest routes the sentence to store_context (Class C) — held, not forgotten.
    res = _residue_for("I just got my highest score in Ticket to Ride - 132 points!")
    assert "132" in res, res


@requires_model
def test_typed_number_not_double_routed_to_residue():
    # TYPE-FIRST: a number that DID bind into an emitted edge value ("124 points", "95") is TYPED
    # (flows to entity_attributes/facts) and must NEVER be re-routed to Class C — no double capture.
    assert "124" not in _residue_for("My highest score so far is 124 points."), "124 double-routed"
    assert "95" not in _residue_for("I scored 95 on the exam."), "95 double-routed"


@requires_model
def test_bare_number_residue_off_restores_prior_behavior():
    # Flag OFF → byte-identical to the prior NOUN/PROPN-only residue behaviour (132 NOT flagged).
    import importlib
    import src.extraction.linguistics as _L
    import os
    _prev = os.environ.get("NET_UNTYPABLE_RESIDUE")
    os.environ["NET_UNTYPABLE_RESIDUE"] = "false"
    try:
        importlib.reload(_L)
        res: list = []
        _L.derive_sentence_facts("I just got my highest score in Ticket to Ride - 132 points!",
                                 reference=REF, residue_out=res)
        assert "132" not in res, res
    finally:
        if _prev is None:
            os.environ.pop("NET_UNTYPABLE_RESIDUE", None)
        else:
            os.environ["NET_UNTYPABLE_RESIDUE"] = _prev
        importlib.reload(_L)  # restore default-ON module state for other tests
