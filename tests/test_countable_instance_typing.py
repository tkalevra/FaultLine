"""COUNTABLE NAMED-INSTANCE TYPING — the LongMemEval "how many X" cluster ingest root cause.

Cluster (3 exemplars, ONE structural ingest defect):
  • 6d550036       — "How many projects have I led …"        (gold 2)
  • gpt4_59c863d7  — "How many model kits have I worked on …" (gold 5)
  • 0a995998       — "How many items of clothing …"           (gold 3)

The product answered "I don't have any <X> on record." for every one. Root cause proven at the
deriver: a countable item the user introduces ("I finished a Revell F-15 Eagle KIT", "I bought a
navy blue BLAZER") was captured ONLY as a bare ``(user, VERB, object)`` relation edge with NO
``instance_of`` typing rung — so the fully-built cardinality walk (``_apply_instance_count``, which
counts distinct ``instance_of`` a resolved type) had NOTHING to count.

The fix (``derive_sentence_facts._emit``, additive, subject-agnostic, deterministic): a NEWLY-
INTRODUCED (indefinite/cardinal determiner), PREMODIFIED, COMMON-NOUN object of a CONTENT verb is a
specific INSTANCE of its head-noun type → emit the ``(<full-NP>, instance_of, <head-lemma>)`` rung so
the item is COUNTABLE. This test FAILS on the old code (no instance_of companion) and PASSES on the
fix, and pins the non-regression (bare/definite/PROPN/copula objects get NO spurious typing).
"""

import pytest

from src.extraction.linguistics import derive_sentence_facts

_REF = "2023/05/30"


def _facts(sentence):
    return derive_sentence_facts(sentence, _REF) or []


def _instance_of(facts):
    return [(f.subject, f.object) for f in facts if f.rel_type == "instance_of"]


# ── THE FIX: countable items now carry an instance_of typing rung (fail-on-old) ────────────────
@pytest.mark.parametrize("sentence,inst_needle,type_head", [
    # model-kit exemplar (gpt4_59c863d7): premodified indefinite common-noun objects
    ("I recently finished a simple Revell F-15 Eagle kit.", "kit", "kit"),
    ("I built a wooden birdhouse.", "birdhouse", "birdhouse"),
    ("I built three model kits.", "kit", "kit"),             # cardinal-introduced (nummod)
    # clothing exemplar (0a995998): premodified indefinite garment object
    ("I bought a navy blue blazer.", "blazer", "blazer"),
])
def test_countable_instance_typing_emitted(sentence, inst_needle, type_head):
    facts = _facts(sentence)
    io = _instance_of(facts)
    assert io, (
        f"NO instance_of typing rung for a countable introduced item — the cardinality walk has "
        f"nothing to count: {sentence!r} → {[(f.subject, f.rel_type, f.object) for f in facts]}"
    )
    # the head-noun LEMMA is the L4 type place the item is filed at (what the count grounds against)
    assert any(t == type_head for _s, t in io), \
        f"typed at the wrong head for {sentence!r}: {io}"
    # THE HARD LINE: the instance is filed AT the type (subject=instance, object=type), never reversed
    assert any(inst_needle in s and t == type_head for s, t in io), \
        f"instance/type inverted or instance name lost for {sentence!r}: {io}"


def test_multiple_introduced_items_each_countable():
    """Two distinct introduced items in one turn → two distinct instance_of rungs (the count needs
    each as its own instance). Covers the 'N distinct items across a session' cluster shape."""
    facts = _facts("I bought a leather wallet and finished a plastic model.")
    io = _instance_of(facts)
    heads = {t for _s, t in io}
    assert {"wallet", "model"} <= heads, f"lost one of the two introduced items: {io}"


# ── NON-REGRESSION: the gate must NOT flood the graph on the common case (pass-on-both) ────────
@pytest.mark.parametrize("sentence", [
    "I have a dog.",                 # bare indefinite, no premodifier → not a distinguished instance
    "I own a car.",                  # bare indefinite, no premodifier
    "I saw the big dog.",            # DEFINITE 'the' — a known referent, not a newly-introduced one
    "I led the data analysis team.", # definite reference
    "I bought a Camaro.",            # PROPN object head — a named entity, not a common-noun type
    "The server crashed.",           # objectless change-of-state
    "My favorite color is blue.",    # copula, not a content verb
])
def test_no_spurious_instance_typing(sentence):
    io = _instance_of(_facts(sentence))
    assert io == [], f"spurious instance_of typing on a non-instance construction {sentence!r}: {io}"
