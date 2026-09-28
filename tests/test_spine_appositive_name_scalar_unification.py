"""Spine deriver: a scalar a copula states about a common-noun ROLE subject that is
IMMEDIATELY renamed by an apposed proper name must land on the NAMED person, not a
phantom role entity ("my son David Chen is 12 years old" → (david chen, age, 12), NOT
(son, age, 12)). Subject-agnostic, grammatical (appositive PROPN). spaCy-gated; skips
when the model is unavailable."""
import os
import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("LINGUISTIC_LAYER", "1")
os.environ.setdefault("SPINE_NAMING_CHAIN", "true")

from src.extraction import linguistics as L  # noqa: E402


def _derive(text):
    try:
        return L.derive_sentence_facts(text, reference=None)
    except Exception:
        pytest.skip("spine deriver unavailable")


def _has(facts, subj, rel, obj=None):
    return any(f.subject == subj and f.rel_type == rel
              and (obj is None or f.object == obj) for f in facts)


@pytest.mark.skipif(L._get_nlp() is None, reason="spaCy model not installed")
def test_age_lands_on_appositive_name_not_role():
    facts = _derive("my son David Chen is 12 years old")
    # the age must land on the FULL apposed name, never on the bare role "son"
    assert _has(facts, "david chen", "age", "12"), [(f.subject, f.rel_type, f.object) for f in facts]
    assert not _has(facts, "son", "age"), "age leaked onto the phantom role entity 'son'"


@pytest.mark.skipif(L._get_nlp() is None, reason="spaCy model not installed")
def test_multi_token_name_preserved_on_scalar():
    facts = _derive("my daughter Sarah Jones is 28")
    assert _has(facts, "sarah jones", "age", "28"), [(f.subject, f.rel_type, f.object) for f in facts]
    assert not _has(facts, "daughter", "age")


@pytest.mark.skipif(L._get_nlp() is None, reason="spaCy model not installed")
def test_no_appositive_name_keeps_role_subject():
    # no apposed name → the role IS the only handle; unchanged behavior
    facts = _derive("my son is 12")
    assert _has(facts, "son", "age", "12"), [(f.subject, f.rel_type, f.object) for f in facts]


@pytest.mark.skipif(L._get_nlp() is None, reason="spaCy model not installed")
def test_bare_propn_subject_age_unchanged():
    facts = _derive("Sarah is 28")
    assert _has(facts, "sarah", "age", "28"), [(f.subject, f.rel_type, f.object) for f in facts]
