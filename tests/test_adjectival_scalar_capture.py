"""Deriver-level pins for the POSSESSED-ATTRIBUTE ADJECTIVAL SCALAR lane.

Gap (from a live gauntlet run): an adjectival scalar value did not land —
"My vantick is teal" routed 'teal' to a spurious ``(vantick, has_state, teal)`` FACT (and never
to ``entity_attributes`` as a scalar), because ``_attr_scalar_binding`` deferred the 1st-person
possessive single-ADJ construction outright and gated the genitive one on the (initially empty)
``attribute_noun`` cue class. spaCy routes a PREDICATE ADJECTIVE to the ``acomp`` (adjectival
complement) dependency, so the ADJ value never reached the scalar-literal gate.

The fix (``src/extraction/linguistics.py``, ``_attr_scalar_binding`` V6): a bare predicate ADJ on
``acomp`` is the SCALAR VALUE of a possessed attribute noun, captured on GRAMMAR (exactly as a bare
predicate NUMERAL is — "my dremmage is seven"). The scalar-vs-preference discriminator is the
GRAMMATICAL preference SELECTOR ("my FAVOURITE colour is blue"), never a colour/adjective word list.
The DEFINITE / bare subject ("the hue is teal") — which carries no possession signal — is admitted
only when the ``attribute_noun`` cue class confirms the head noun, the guard against reading a world
statement ("the sky is blue") as a user scalar.

All parses were verified against en_core_web_sm before these assertions were written.

Run: python3 tools/fltest.py --bug ADJSCALAR --test tests/test_adjectival_scalar_capture.py
     (or: SPACY_MODEL=en_core_web_sm python3 -m pytest tests/test_adjectival_scalar_capture.py -q)
"""
import importlib
import os

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


# Skip cleanly when the spaCy model is unavailable — gate on the SAME predicate production uses.
_M = _reload(LINGUISTIC_LAYER="true")
requires_model = pytest.mark.skipif(
    not _M.linguistics_available(), reason="en_core_web_sm not installed in test env")


def _derive(sentence):
    """Return (edges, residue) for a sentence through the real deterministic deriver."""
    m = _reload(LINGUISTIC_LAYER="true")
    res = []
    edges = m.derive_sentence_facts(sentence, reference="user", residue_out=res)
    tuples = [(e.subject, e.rel_type, e.object, e.scalar_datatype) for e in edges]
    return tuples, sorted(res)


# ───────────────────────── CAPTURE: possessed-attribute adjectival scalar ─────────────────────────

@requires_model
def test_first_person_possessive_adjectival_value_is_a_scalar():
    # "My vantick is teal" → (user, vantick, teal) SCALAR on entity_attributes (scalar_datatype set),
    # NOT (vantick, has_state, teal) and NOT dropped. This is THE gap.
    edges, _ = _derive("My vantick is teal.")
    assert ("user", "vantick", "teal", "string") in edges
    # the spurious has_state twin must be suppressed (the copula-state chain consults the binding)
    assert not any(rel == "has_state" for _, rel, _, _ in edges)
    # and the attribute NP must not be minted as an owned entity for this construction
    assert not any(rel == "owns" for _, rel, _, _ in edges)


@requires_model
def test_genitive_noun_possessor_adjectival_value_is_a_scalar():
    # "My krellin's vantick is teal" → the scalar lands keyed on the GENITIVE possessor (krellin),
    # semantically correct; the has_state twin is suppressed. (user, owns, krellin) legitimately
    # grounds the possessor as an entity.
    edges, _ = _derive("My krellin's vantick is teal.")
    assert ("krellin", "vantick", "teal", "string") in edges
    assert not any(rel == "has_state" for _, rel, _, _ in edges)


@requires_model
def test_genitive_proper_name_possessor_adjectival_value_is_a_scalar():
    # Subject-agnostic: a PROPER-NAME genitive possessor works identically. "Sarah's aura is golden".
    edges, _ = _derive("Sarah's aura is golden.")
    assert ("sarah", "aura", "golden", "string") in edges
    assert not any(rel == "has_state" for _, rel, _, _ in edges)


@requires_model
@pytest.mark.parametrize(
    "sentence,expected",
    [
        ("My zorbil is olive.", ("user", "zorbil", "olive", "string")),
        ("My aura is golden.", ("user", "aura", "golden", "string")),
        ("My demeanor is calm.", ("user", "demeanor", "calm", "string")),
    ],
)
def test_adjectival_scalar_is_grammar_not_lexicon(sentence, expected):
    # Same construction, different attribute nouns AND different adjective values — no colour/adjective
    # word list is consulted, so an arbitrary novel attribute + arbitrary ADJ value both capture.
    edges, _ = _derive(sentence)
    assert expected in edges


# ───────────────────────── UNCHANGED: the cardinal path still works ─────────────────────────

@requires_model
def test_spelled_cardinal_scalar_is_unchanged():
    # The bare predicate NUMERAL path is byte-for-byte unchanged by the ADJ widening.
    edges, _ = _derive("My dremmage is seven.")
    assert ("user", "dremmage", "seven", "string") in edges


# ───────────────────────── GUARDS: what must NOT become a scalar ─────────────────────────

@requires_model
def test_preference_selector_is_not_a_scalar():
    # "My favorite color is blue" carries a preference SELECTOR (favorite) → the affect/preference
    # seam owns it; it must NOT be captured as a (user, color, blue) scalar by this lane.
    edges, _ = _derive("My favorite color is blue.")
    assert not any(
        subj == "user" and obj == "blue" and dt == "string" for subj, _, obj, dt in edges)


@requires_model
def test_preferred_selector_defers_too():
    # The selector class is grammatical, not just "favorite": "preferred" defers identically.
    edges, _ = _derive("My preferred zorbil is olive.")
    assert not any(
        subj == "user" and rel == "zorbil" and dt == "string" for subj, rel, _, dt in edges)


@requires_model
def test_measured_adjective_is_not_a_flat_scalar():
    # "My horse is 5 feet tall" is a MEASURED adjective (owned by the copula-measure chain → height),
    # excluded from this lane by the len(complement)==1 requirement. No (user, horse, tall) scalar.
    edges, _ = _derive("My horse is 5 feet tall.")
    assert not any(rel == "horse" and dt == "string" for _, rel, _, dt in edges)
    assert not any(obj == "tall" and dt == "string" for _, _, obj, dt in edges)


@requires_model
def test_relational_predicate_adjective_is_not_a_scalar():
    # "My cat is allergic to penicillin" is a RELATIONAL predicate (acomp ADJ governing a PP),
    # excluded by len(complement)==1. It must not be flattened into a (user, cat, allergic) scalar.
    edges, _ = _derive("My cat is allergic to penicillin.")
    assert not any(rel == "cat" and dt == "string" for _, rel, _, dt in edges)


@requires_model
def test_first_person_feeling_never_reaches_the_scalar_lane():
    # "I am excited" has a 1st-person PERSONAL-PRONOUN subject (PRON, not NOUN) → it never reaches
    # the NOUN-subject attribute-scalar binding, so no scalar edge is minted at the deriver level.
    edges, _ = _derive("I am excited.")
    assert not any(dt == "string" for _, _, _, dt in edges)
    assert not any(subj == "user" and obj == "excited" for subj, _, obj, _ in edges)


@requires_model
def test_nominal_role_complement_is_unchanged():
    # "I am a teacher" (NOMINAL complement → occupation elsewhere) is not swept into the scalar lane.
    edges, _ = _derive("I am a teacher.")
    assert not any(dt == "string" for _, _, _, dt in edges)


# ───────────────────────── DEFINITE / bare subject: cue-class gated ─────────────────────────

@requires_model
def test_definite_subject_fires_only_when_the_cue_class_knows_the_attribute(monkeypatch):
    # "The hue is teal" has NO possession marker, so it is admitted ONLY when the attribute_noun cue
    # class confirms the head noun. With the cue class populated, it captures as an implicit-speaker
    # scalar (possessor="user"). This mirrors the definite→speaker rule the identifier lane uses.
    m = _reload(LINGUISTIC_LAYER="true")
    monkeypatch.setattr(m, "_attribute_nouns", lambda: frozenset({"hue"}))
    res = []
    edges = m.derive_sentence_facts("The hue is teal.", reference="user", residue_out=res)
    tuples = [(e.subject, e.rel_type, e.object, e.scalar_datatype) for e in edges]
    assert ("user", "hue", "teal", "string") in tuples


@requires_model
def test_definite_world_statement_is_not_over_captured(monkeypatch):
    # "The sky is blue" — 'sky' is NOT in the cue class → the definite lane defers, so it is NEVER
    # read as a user scalar. This is the guard that keeps world statements out.
    m = _reload(LINGUISTIC_LAYER="true")
    monkeypatch.setattr(m, "_attribute_nouns", lambda: frozenset({"hue"}))
    res = []
    edges = m.derive_sentence_facts("The sky is blue.", reference="user", residue_out=res)
    tuples = [(e.subject, e.rel_type, e.object, e.scalar_datatype) for e in edges]
    assert not any(subj == "user" and dt == "string" for subj, _, _, dt in tuples)
