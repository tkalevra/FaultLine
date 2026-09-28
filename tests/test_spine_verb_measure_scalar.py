"""Unit tests for the MEASURE-VERB SCALAR chain (G2 — LongMemEval numeric/measure capture).

THE GAP: a measure quantity stated in a CONTENT-verb frame — "my commute takes 45 minutes",
"I spent 70 hours", "it costs 500 dollars" — was DROPPED by the SVO backbone, which minted
(X, <verb>, <UNIT NOUN>) losing the numeral AND mis-reading the measure noun as a relational
entity. ``_chain_verb_measure`` now captures the FULL measure span ("45 minutes") as a SCALAR
VALUE on the measured entity (the clause subject), rel = the user's own VERB LEMMA (a GROWN/novel
rel — NO pre-seeded unit→rel map), routed to entity_attributes by the object's ``scalar_datatype``
marker (main.py ``_edge_is_scalar`` / "forced scalar: edge carries object_datatype").

PURE tests — no DB, no network, no GLiNER2, no LLM; they call the deriver DIRECTLY. The measure
DISCRIMINATOR is spaCy NER (a TIME/MONEY/QUANTITY/PERCENT/DATE span over the direct object), so
these need the real spaCy model.

Run: python3 -m pytest tests/test_spine_verb_measure_scalar.py -q   (tests/ is gitignored → git add -f)
"""
import datetime
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts  # noqa: E402


def _facts(sentence, reference=None):
    """(subject, rel_type, object, scalar_datatype, event_date) tuples for one clean sentence."""
    return [(f.subject, f.rel_type, f.object, f.scalar_datatype, f.event_date)
            for f in derive_sentence_facts(sentence, reference=reference)]


def _scalar_value_on(sentence, subject, value, reference=None):
    """True iff SOME emitted fact is a SCALAR (scalar_datatype set) on ``subject`` whose object
    contains ``value`` (the numeral+unit was captured, not dropped)."""
    subject = subject.lower()
    value = value.lower()
    for s, _r, obj, sdt, _d in _facts(sentence, reference):
        if s == subject and sdt and value in (obj or "").lower():
            return True
    return False


# ── CAPTURE: measure in a content-verb frame lands as a scalar on the measured entity ─────────────
@pytest.mark.parametrize(
    "sentence,subject,value",
    [
        # duration (TIME) — the flagship LongMemEval Q2 case, plain frame
        ("My commute takes 45 minutes each way.", "commute", "45 minutes"),
        # duration (TIME) — first-person subject → user
        ("I spent 70 hours playing the game.", "user", "70 hours"),
        # money (MONEY)
        ("It costs 500 dollars.", "it", "500 dollars"),
        # duration (TIME) — word numeral "two"
        ("The movie lasts two hours.", "movie", "two hours"),
        # duration-as-DATE (a bare duration NER-labelled DATE, still a value not a when)
        ("The project took three years.", "project", "three years"),
        # physical quantity (QUANTITY)
        ("He weighs 180 pounds.", "he", "180 pounds"),
    ],
)
def test_measure_verb_captures_full_span_as_scalar(sentence, subject, value):
    """The FULL numeral+unit span is captured as a SCALAR on the measured entity."""
    assert _scalar_value_on(sentence, subject, value), (
        f"measure {value!r} not captured as a scalar on {subject!r} for {sentence!r}: "
        f"{_facts(sentence)}"
    )


# ── RELATIVE CLAUSE: "commute, which takes 45 minutes" → scalar lands on COMMUTE, not "which" ─────
def test_relative_clause_subject_resolves_to_antecedent():
    """The Q2 real sentence — a relative clause whose subject is the relative pronoun 'which'.
    The scalar must land on the antecedent ('commute'), NEVER on the function word 'which'."""
    sentence = ("I have been listening to audiobooks during my daily commute, "
                "which takes 45 minutes each way.")
    facts = _facts(sentence)
    assert _scalar_value_on(sentence, "commute", "45 minutes"), (
        f"relative-clause measure not landed on the antecedent 'commute': {facts}"
    )
    # THE HARD LINE — the relative pronoun is never an entity.
    assert not any(s == "which" for s, _r, _o, _sdt, _d in facts), (
        f"relative pronoun 'which' bound as an entity: {facts}"
    )


# ── NO NUMERAL DROP + NO SVO TWIN: the old (X, <verb>, <bare unit noun>) edge is suppressed ───────
def test_no_svo_twin_with_bare_unit_noun():
    """The numeral-dropping SVO twin (commute, take, minutes) must NOT co-exist with the scalar."""
    facts = _facts("My commute takes 45 minutes each way.")
    # exactly the scalar, no relational (take, minutes) twin
    assert not any(r == "take" and (o or "").lower() == "minutes" and sdt is None
                   for _s, r, o, sdt, _d in facts), (
        f"numeral-dropping SVO twin still emitted alongside the scalar: {facts}"
    )
    assert _scalar_value_on("My commute takes 45 minutes each way.", "commute", "45 minutes")


# ── UNDER-CAPTURE: a bare CARDINAL (no unit) is NOT invented into a measure ───────────────────────
@pytest.mark.parametrize(
    "sentence,subject",
    [
        ("I scored 95 on the exam.", "user"),   # a bare score — CARDINAL, no unit → not a measure
        ("I have 3 cats.", "user"),             # a COUNT of a countable entity — not a measure
        ("I read 5 books.", "user"),            # a COUNT of a countable entity — not a measure
    ],
)
def test_bare_cardinal_is_not_captured_as_measure(sentence, subject):
    """A bare CARDINAL (a count / score with no unit) must NOT be minted as a MEASURE scalar (a
    numeral GLUED to a unit, e.g. "45 minutes"). Refined for the count-scalar lane (fix#18/#21): a
    possessed bare count IS now a legitimate COUNT scalar whose object is a BARE INTEGER
    ("I have 3 cats" -> (user, cats, "3")) — that is NOT a fabricated measure. Forbid only a scalar
    whose object is NOT a bare integer; a bare-integer count scalar is permitted."""
    import re as _re
    for s, _r, _o, sdt, _d in _facts(sentence):
        _is_bare_int = bool(_re.fullmatch(r"\d[\d,]*", str(_o or "").strip()))
        assert not (s == subject and sdt and not _is_bare_int), (
            f"a bare cardinal was wrongly captured as a MEASURE scalar for {sentence!r}: "
            f"{_facts(sentence)}"
        )


# ── TEMPORAL FIREWALL: a resolved WHEN is an event_date, NEVER a measure scalar (dual-clock) ──────
@pytest.mark.parametrize(
    "sentence",
    [
        "I finished the project three years ago.",   # "three years ago" → a resolved WHEN
        "I bought a house 3 weeks ago.",             # "3 weeks ago" → a resolved WHEN
    ],
)
def test_resolved_when_is_not_captured_as_measure(sentence):
    """A relative WHEN ("three years ago") resolves to an event_date and must NOT be scalar-captured
    as a duration — the direct-object gate + the resolved-date peel keep the dual-clock separate."""
    ref = datetime.date(2026, 7, 16)
    facts = _facts(sentence, reference=ref)
    # nothing is captured as a duration/measure scalar
    assert not any(sdt for _s, _r, _o, sdt, _d in facts), (
        f"a resolved WHEN was wrongly captured as a measure scalar for {sentence!r}: {facts}"
    )
    # and the when did land on the temporal lane (some fact carries an event_date)
    assert any(d for _s, _r, _o, _sdt, d in facts), (
        f"the WHEN was not routed to the temporal lane for {sentence!r}: {facts}"
    )


# ── NO DOUBLE-FOLD: exactly ONE measure edge per measured entity ──────────────────────────────────
def test_no_double_fold_single_scalar_edge():
    """The measure is emitted ONCE (no duplicate scalar + relational twin for the same construction)."""
    facts = _facts("The subscription costs 15 dollars per month.")
    measure_edges = [(s, r, o) for s, r, o, sdt, _d in facts if sdt and "15 dollars" in (o or "")]
    assert len(measure_edges) == 1, f"expected exactly one measure scalar edge, got: {facts}"


# ── COPULA MEASURE UNTOUCHED: "she is 62 years old" still owned by _chain_copula_measure ───────────
def test_copula_measure_lane_unaffected():
    """A COPULA measure ("she is 62 years old") is owned by _chain_copula_measure (→ age); the new
    lexical-verb chain must not fire on the 'be' frame nor change that capture."""
    facts = _facts("She is 62 years old.")
    assert any(r == "age" and (o or "").strip() == "62" for _s, r, o, _sdt, _d in facts), (
        f"copula-measure age capture regressed: {facts}"
    )


# ── POSSESSED MEASURE → USER-ANCHORED SCALAR (LME-118b2229) ───────────────────────────────────────
# "How long is my daily commute to work?" (gold "45 minutes each way") returned EMPTY because the
# possessed duration was captured only on the ISLAND entity "commute", unreachable from the user
# anchor. The possessed-attribute detector now ALSO owns the measure-verb frame "my X takes N <unit>"
# (and its relative-clause form), landing (user, daily_commute, "45 minutes each way") — the SAME
# recallable shape as "my address is 123 Main St" — while the entity-local (commute, take, "45
# minutes") edge is preserved (no regression).
@pytest.mark.parametrize(
    "sentence",
    [
        # atomized possessed measure-verb frame
        "My daily commute takes 45 minutes each way.",
        # the real LongMemEval sentence — a RELATIVE CLAUSE on the possessed antecedent "commute"
        ("I have been listening to audiobooks during my daily commute, "
         "which takes 45 minutes each way."),
    ],
)
def test_possessed_measure_lands_on_possessor_scalar(sentence):
    """A possessed measure lands as a USER-anchored scalar containing the numeral+unit, AND the
    entity-local measured-entity scalar is preserved (both coexist)."""
    facts = _facts(sentence)
    assert _scalar_value_on(sentence, "user", "45 minutes"), (
        f"possessed measure did not land as a user-anchored scalar: {facts}"
    )
    # the entity-local measure edge on the measured entity ("commute") is NOT regressed away.
    assert _scalar_value_on(sentence, "commute", "45 minutes"), (
        f"entity-local measure scalar on 'commute' regressed: {facts}"
    )
    # THE HARD LINE — the relative pronoun is never an entity.
    assert not any(s == "which" for s, _r, _o, _sdt, _d in facts), (
        f"relative pronoun 'which' bound as an entity: {facts}"
    )


# ── UNPOSSESSED MEASURE → NO FABRICATED POSSESSOR SCALAR ──────────────────────────────────────────
@pytest.mark.parametrize(
    "sentence",
    [
        "The movie lasts two hours.",            # unpossessed subject → no possessor
        "The subscription costs 15 dollars per month.",
    ],
)
def test_unpossessed_measure_does_not_fabricate_possessor_scalar(sentence):
    """With NO possessor (no 'my'/genitive), the possessed-measure lane must NOT fabricate a
    user-anchored scalar — the measure stays only on the measured entity (the clause subject)."""
    facts = _facts(sentence)
    assert not any(s == "user" and sdt for s, _r, _o, sdt, _d in facts), (
        f"an unpossessed measure fabricated a user-anchored scalar: {facts}"
    )


# ── PRICE-OF-OBJECT: a MONEY/measure in a prep-PP is the price of the purchased THING (LME-7527f7e2)─
# "How much did I spend on a designer handbag?" (gold "$800") returned EMPTY because
# "I bought a designer handbag for $800" dropped the price entirely: the SVO backbone minted only
# the relational (user, buy, handbag), and the measure-verb pre-pass fired only on a NUMERAL DIRECT
# OBJECT ("I spent $800") — never on a price that rides a prep-PP ("...for $800"). The price now
# lands as a SCALAR on the purchased THING (the verb's direct object), recall-reachable via the
# intact user→verb→thing relational edge (like the founding car→gps walk / the rate pre-pass).
@pytest.mark.parametrize(
    "sentence,thing,price",
    [
        ("I bought a designer handbag for $800.", "designer handbag", "800"),
        ("I bought a designer handbag for 800 dollars.", "designer handbag", "800 dollars"),
        ("I bought a car for 30000 dollars.", "car", "30000 dollars"),
        ("She sold the painting for 5000 dollars.", "painting", "5000 dollars"),
    ],
)
def test_price_of_object_lands_on_purchased_thing(sentence, thing, price):
    """The price rides the prep-PP → captured as a scalar on the purchased THING (not dropped)."""
    assert _scalar_value_on(sentence, thing, price), (
        f"price {price!r} not captured as a scalar on {thing!r} for {sentence!r}: {_facts(sentence)}"
    )


def test_price_of_object_keeps_relational_edge_for_reachability():
    """The SVO relational edge (user → verb → thing) MUST survive so the walk reaches the thing whose
    price scalar we captured — the price lane must NOT suppress it (unlike the subject-attached case)."""
    facts = _facts("I bought a designer handbag for $800.")
    assert any(s == "user" and r == "buy" and "handbag" in (o or "").lower() and sdt is None
               for s, r, o, sdt, _d in facts), (
        f"the user→buy→handbag relational edge was lost (thing unreachable): {facts}"
    )
    # currency symbol preserved on the price value ("$800", not a bare "800")
    assert _scalar_value_on("I bought a designer handbag for $800.", "designer handbag", "$800"), (
        f"the leading currency symbol was dropped from the price value: {facts}"
    )
