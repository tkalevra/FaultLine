"""NATAL / BIRTH-EVENT capture — the LongMemEval "how many babies were born" cluster ingest root.

Cluster (qid 2e6d26dc, gold=5): "How many babies were born to friends and family members in the
last few months?" The user stated 5 distinct births across sessions — Jasper (David's baby boy),
Max (cousin Rachel's son), Charlotte (Mike & Emma's baby girl), and the twins Ava & Lily (aunt's) —
but the deriver typed each newborn INCONSISTENTLY (instance_of boy/son/girl, or not at all for the
twins), so there was no unifying "baby" type node for the cardinality walk to count → the product
answered "I don't have any … on record."

The fix (``derive_sentence_facts._chain_natal_birth``, additive, subject-agnostic, deterministic):
a NATAL predicate — passive "was/were BORN" (natal_predicate cue class) OR a self-gating newborn noun
baby/newborn/infant (offspring_noun cue class, description='birth') — types the newborn NAMED person
``instance_of`` the birth type "baby". THE HARD LINE: the baby's NAME (Jasper/Ava) is the naming
layer (filed as the entity); "baby" is the TYPE. A NAME is typed ONLY when structurally bound
(naming ``acl`` / ``appos``/``conj``) to an OFFSPRING noun in a natal clause, so a non-birth clause
("my son likes soccer", "I bought a baby monitor") is never mis-typed.

FAILS on the old code (no unifying instance_of baby edge); PASSES on the fix. Pins the non-regression
(the parent is never typed a baby; non-birth clauses emit nothing).
"""

import pytest

from src.extraction.linguistics import derive_sentence_facts

_REF = "2023/05/13"


def _baby_subjects(sentence):
    facts = derive_sentence_facts(sentence, _REF) or []
    return sorted({f.subject for f in facts
                   if f.rel_type == "instance_of" and f.object == "baby"})


# ── THE FIX: each newborn NAME is typed instance_of the unifying "baby" type (fail-on-old) ──────
@pytest.mark.parametrize("sentence,expected_names", [
    # (B) self-gating natal noun "baby" + naming acl → the proper name is the newborn
    ("David had a baby boy named Jasper a few weeks ago.", {"jasper"}),
    ("My cousin Rachel had a baby boy named Max in March.", {"max"}),
    ("Our friends Mike and Emma welcomed their first baby, a girl named Charlotte.", {"charlotte"}),
    ("David and his wife just had their third child, a baby boy named Jasper.", {"jasper"}),
    # (A) passive "born" + apposed offspring noun / coordinated twin names
    ("My cousin Rachel's son Max was born in March.", {"max"}),
    ("My aunt has new twin girls, Ava and Lily, who were born in April.", {"ava", "lily"}),
])
def test_newborn_named_person_typed_baby(sentence, expected_names):
    got = set(_baby_subjects(sentence))
    assert expected_names.issubset(got), (
        f"newborn name(s) NOT typed instance_of baby — the 'how many babies' walk has nothing to "
        f"count: {sentence!r} → instance_of baby subjects={got}"
    )


def test_twins_are_two_distinct_countable_babies():
    # gold counts BABIES not birth EVENTS: the twins are TWO distinct baby entities from one event.
    got = _baby_subjects("My aunt has new twin girls, Ava and Lily, who were born in April.")
    assert {"ava", "lily"}.issubset(set(got)), got


# ── NON-REGRESSION: the PARENT is never typed a baby; non-birth clauses emit nothing ────────────
@pytest.mark.parametrize("sentence", [
    "My son likes soccer.",            # offspring noun, but NO natal predicate → not a birth
    "I bought a baby monitor.",        # "baby" modifies a non-offspring noun → no newborn name
    "My baby brother John called me.", # "baby" modifies "brother" (not offspring set) → no fire
    "Sarah had a baby.",               # birth but UNNAMED → nothing countable to file
])
def test_non_birth_or_unnamed_never_typed_baby(sentence):
    assert _baby_subjects(sentence) == [], (
        f"spurious instance_of baby on a non-birth / unnamed clause: {sentence!r} → "
        f"{_baby_subjects(sentence)}"
    )


def test_parent_name_not_typed_baby():
    # "David had a baby boy named Jasper" — David is the PARENT (nsubj), Jasper the newborn.
    got = _baby_subjects("David had a baby boy named Jasper a few weeks ago.")
    assert "david" not in got, f"the PARENT was mis-typed as a baby: {got}"
