"""Unit tests — POSSESSIVE PRESUPPOSITION marking (interrogative-survival).

Root cause fixed (LME-06878be2, single-session-preference "accessories for my photography setup";
gold = Sony-compatible). The user's ownership facts live INSIDE questions ("recommend a flash for
my Sony A7R IV?", "how do I clean my Sony 24-70mm lens?") as first-person POSSESSIVE PRESUPPOSITIONS
— GIVENs that survive interrogation. The spine harvest DROPS whole interrogative clauses (recall is
read-only for the question's OWN asked predicate), which also discarded these owned-thing facts, so
recall returned nothing. Fix: derive_sentence_facts marks its first-person possessive owns/kinship
emits ``presupposed=True``; the interrogative-harvest recovery lane keeps ONLY those from a dropped
question clause. The asked wh-focus SVO ("what car do I own?") is the ASKED predicate — NOT
presupposed — and stays dropped.

PURE tests — no DB, no network, no GLiNER2, no LLM. They exercise derive_sentence_facts' grammatical
(Person=1 ∧ Poss=Yes) marking only. Subject-agnostic — no brand/domain/rel literal is asserted.

Run: python3 -m pytest tests/test_spine_possessive_presupposition.py -q  (tests/ gitignored → git add -f)
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import (  # noqa: E402
    derive_sentence_facts, linguistics_available, is_interrogative_clause,
)

pytestmark = pytest.mark.skipif(
    not linguistics_available(), reason="spaCy model not installed")


def _facts(sentence):
    return list(derive_sentence_facts(sentence, None))


def _presup(sentence):
    return [(f.subject, f.rel_type, f.object) for f in _facts(sentence) if f.presupposed]


def _not_presup(sentence):
    return [(f.subject, f.rel_type, f.object) for f in _facts(sentence) if not f.presupposed]


def test_possessive_in_question_is_presupposed_owns():
    """A first-person possessive of a concrete thing inside a QUESTION is a presupposition."""
    s = "Can you recommend a flash that is compatible with my Sony A7R IV?"
    assert is_interrogative_clause(s) is True
    presup = _presup(s)
    assert any(rel == "owns" and subj == "user" for (subj, rel, _obj) in presup), presup


def test_second_possessive_question_is_presupposed_owns():
    s = "What is the best way to clean my Sony 24-70mm f/2.8 lens?"
    assert is_interrogative_clause(s) is True
    presup = _presup(s)
    assert any(rel == "owns" and subj == "user" for (subj, rel, _obj) in presup), presup


def test_nested_proper_name_compound_keeps_leading_brand_token():
    """A multi-token proper name ("my Sony A7R IV") is a left-branching compound CHAIN
    (``Sony →compound A7R →compound IV[head]``). The possessed-object phrase must descend the
    nested compound so the OUTERMOST name token ("Sony") is captured — a direct-children-only
    fold ("a7r iv") drops the brand the answer hinges on. Subject-agnostic: no brand/domain
    literal is asserted — only that the leading proper-noun modifier survives in the object."""
    s = "Can you recommend a flash that is compatible with my Sony A7R IV?"
    owned = [obj for (subj, rel, obj) in _presup(s) if subj == "user" and rel == "owns"]
    assert owned, _presup(s)
    # the outermost compound token must not be truncated away from the captured object surface
    assert any("sony" in (obj or "") for obj in owned), owned


def test_asked_wh_focus_svo_is_NOT_presupposed():
    """The ASKED predicate ("what car do I own?") is the question's content — never presupposed,
    so the recovery lane leaves it dropped. This is the illocutionary-force vs presupposition line."""
    s = "What kind of car do I own?"
    assert is_interrogative_clause(s) is True
    # the own(kind of car) SVO must NOT be marked presupposed
    assert not any(f.presupposed and f.rel_type in ("own", "owns") for f in _facts(s)), _facts(s)


def test_declarative_possessive_still_marked_presupposed():
    """The flag is grammatical, not mood-gated: a first-person possessive is presupposed even in a
    declarative (harvested normally there; the flag only matters on the dropped interrogative path)."""
    s = "I really like my new camera."
    presup = _presup(s)
    assert any(rel == "owns" and subj == "user" for (subj, rel, _obj) in presup), presup


def test_kinship_possessive_is_presupposed():
    """"my mother" asserts the kin tie as a GIVEN → presupposed (survives "what should I get my
    mother?"). Subject-agnostic: kin rel comes from the kinship_noun cue class, not a literal."""
    s = "What should I get my mother for her birthday?"
    assert is_interrogative_clause(s) is True
    # SOME presupposed kin/owns edge involving the user must survive
    assert any(f.presupposed and "user" in (f.subject, f.object) for f in _facts(s)), _facts(s)


def test_non_possessive_question_yields_no_presupposition():
    """A pure question with NO first-person possessive contributes zero presupposed edges."""
    s = "What is the best mirrorless camera under 2000 dollars?"
    assert _presup(s) == []
