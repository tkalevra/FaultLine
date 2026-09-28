"""Unit tests for the POSSESSED SCALAR-OF-MEASURE chain (G2 companion — LongMemEval capture).

THE GAP (LongMemEval 6a1eabeb, knowledge-update): a scalar VALUE stated as the ``of``-complement
of a POSSESSED noun phrase — "my personal best time OF 25:50", "my score OF 95", "Sarah's salary
OF 50000" — was DROPPED by ``_chain_possessive``, which emitted only (user, owns, "personal best
time") and lost the number. The updated value 25:50 (the answer) never survived ingest, so recall
returned "No relevant facts found."

THE FIX: ``_chain_possessive`` now detects a possessed head carrying an ``of``-PP whose object is a
DIGIT-bearing value and routes it to the SCALAR path (attribute = the possessed NP phrase, value =
the verbatim of-object), suppressing the ``owns``/genitive twin. Sibling of ``_chain_measure_pp``
(PREP-governed "with a score of 9.8") but for the POSSESSED-noun frame. Grammar + value-shape gated,
subject-agnostic, NO attribute/unit word list.

PURE tests — no DB, no network, no GLiNER2, no LLM; they call the deriver DIRECTLY. The measure
DISCRIMINATOR is a digit-bearing ``of``-pobj, so these need the real spaCy model.

Run: python3 -m pytest tests/test_spine_possessive_of_measure.py -q   (tests/ is gitignored → git add -f)
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts  # noqa: E402


def _facts(sentence, reference=None):
    return [(f.subject, f.rel_type, f.object, f.scalar_datatype)
            for f in derive_sentence_facts(sentence, reference=reference)]


def _scalar_value_on(sentence, subject, value, reference=None):
    """True iff SOME emitted fact is a SCALAR (scalar_datatype set) on ``subject`` whose object
    contains ``value``."""
    subject = subject.lower()
    value = value.lower()
    for s, _r, obj, sdt in _facts(sentence, reference):
        if s == subject and sdt and value in (obj or "").lower():
            return True
    return False


# ── CAPTURE: the of-value lands as a scalar on the possessor ──────────────────────────────────────
@pytest.mark.parametrize(
    "sentence,subject,value",
    [
        # the flagship LongMemEval 6a1eabeb update value — mm:ss duration
        ("I am hoping to beat my personal best time of 25:50 this time around.", "user", "25:50"),
        # first-person possessive, bare number
        ("My score of 95 was the highest.", "user", "95"),
        # genitive PROPER-NOUN possessor → the named person carries the scalar
        ("Sarah's salary of 50000 is fixed.", "sarah", "50000"),
    ],
)
def test_possessed_of_measure_captured_as_scalar(sentence, subject, value):
    assert _scalar_value_on(sentence, subject, value), (
        f"expected SCALAR {value!r} on {subject!r} for {sentence!r}; got {_facts(sentence)!r}")


def test_no_owns_twin_when_scalar_fires():
    # the (user, owns, "personal best time") twin must be SUPPRESSED once the scalar is emitted.
    facts = _facts("I am hoping to beat my personal best time of 25:50 this time around.")
    assert not any(r == "owns" for _s, r, _o, _sdt in facts), facts
    assert any(sdt and "25:50" in (o or "") for _s, _r, o, sdt in facts), facts


# ── NON-FIRE: a partitive "of <num> <plural-noun>" is NOT a scalar measurement ────────────────────
@pytest.mark.parametrize(
    "sentence",
    [
        "My group of 5 friends went hiking.",   # pobj "friends" — no digit → left untouched
        "My box of chocolates is empty.",        # no digit at all
    ],
)
def test_partitive_of_phrase_not_captured_as_scalar(sentence):
    facts = _facts(sentence)
    assert not any(sdt for _s, _r, _o, sdt in facts), (
        f"partitive should NOT emit a scalar for {sentence!r}; got {facts!r}")


def test_plain_possessive_unaffected():
    # a possessed head with no of-PP keeps today's ownership reading (no regression).
    facts = _facts("My laptop is broken.")
    assert any(s == "user" and r == "owns" and "laptop" in (o or "") for s, r, o, _sdt in facts), facts
