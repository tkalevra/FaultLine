"""Unit tests for the OBJECT NAMED-VALUE chain (G4 — LongMemEval dense-capture "the name is dropped").

THE GAP (cluster ``dense-capture-single-fact``): a stated PROPER NAME that identifies a common-noun
THING — "a playlist called Summer Vibes", "a dog named Fraggle", "my favorite restaurant is Nobu",
"I named my project Phoenix" — is the ANSWER to a "what is the name of …" question, yet the
SVO/possessive backbone kept only the generic head (playlist / dog / restaurant / project) and DROPPED
the name entirely. ``_chain_object_named_value`` recovers the name and files it in the NAMING LAYER of
the referent: ``(referent-type, also_known_as, Name)``.

THE HARD LINE: the proper name is filed as an ALIAS of the referent (also_known_as — the naming layer),
NEVER classified INTO L4 as a type. The referent's generic head stays the type place.

ADDITIVE: the chain emits ONLY the alias edge — it never suppresses/re-anchors the existing
(user, <verb>, <referent>) edge, so nothing already captured changes; PERSON constructions (kinship /
social / role nouns) are skipped so the family/person-name lanes are untouched.

PURE tests — no DB, no network, no GLiNER2, no LLM; they call the deriver DIRECTLY (the connector
detector resolves cue maps via the in-code DB-DOWN bootstrap). They need the real spaCy model.

Run: python3 -m pytest tests/test_spine_object_named_value.py -q   (tests/ is gitignored → git add -f)
"""
import datetime
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts, linguistics_available  # noqa: E402

requires_model = pytest.mark.skipif(
    not linguistics_available(), reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2026, 4, 15)


def _facts(sentence):
    return {(f.subject, f.rel_type, f.object) for f in derive_sentence_facts(sentence, reference=_REF)}


def _name_captured(sentence, referent, name):
    """True iff the proper NAME is filed as an also_known_as alias of the referent (case-insensitive)."""
    referent, name = referent.lower(), name.lower()
    for s, r, o in _facts(sentence):
        if r == "also_known_as" and s == referent and name in (o or "").lower():
            return True
    return False


# ── CAPTURE across the whole named-value CONSTRUCTION CLASS (subject-agnostic, multi-exemplar) ─────
@requires_model
@pytest.mark.parametrize(
    "sentence,referent,name",
    [
        # LongMemEval exemplar 1e043500 — trailing reduced-relative "called" appositive on the object.
        ("I have been listening to a playlist that I created, called Summer Vibes.",
         "playlist", "summer vibes"),
        ("I created a playlist called Summer Vibes.", "playlist", "summer vibes"),
        # naming-verb "named" appositive — animal (a different domain, same construction).
        ("I have a dog named Fraggle.", "dog", "fraggle"),
        ("I adopted a cat named Whiskers.", "cat", "whiskers"),
        # copula identity — "my favorite <thing> is <PROPN>".
        ("My favorite restaurant is Nobu.", "favorite restaurant", "nobu"),
        # transitive naming verb — "I named my <thing> <PROPN>".
        ("I named my new project Phoenix.", "new project", "phoenix"),
    ],
)
def test_object_name_captured_as_alias(sentence, referent, name):
    assert _name_captured(sentence, referent, name), (
        f"name '{name}' dropped for referent '{referent}' in: {sentence!r} → {_facts(sentence)}")


# ── THE HARD LINE: the name is an ALIAS, NEVER classified INTO L4 as a type ────────────────────────
@requires_model
def test_name_is_never_a_type_the_hard_line():
    facts = _facts("I created a playlist called Summer Vibes.")
    # the name must NOT appear as the OBJECT of instance_of/subclass_of (that would file it as a type),
    # nor as the SUBJECT of a subclass_of (a name is never a class).
    for s, r, o in facts:
        if r in ("instance_of", "subclass_of"):
            assert "summer vibes" not in (o or "").lower(), f"HARD LINE violated: {(s, r, o)}"
            assert "summer vibes" not in (s or "").lower(), f"HARD LINE violated: {(s, r, o)}"


# ── ADDITIVE: the existing user↔referent edge is untouched (recall stays reachable) ────────────────
@requires_model
def test_existing_referent_edge_preserved():
    facts = _facts("I created a playlist called Summer Vibes.")
    assert ("user", "create", "playlist") in facts, facts
    assert ("playlist", "also_known_as", "summer vibes") in facts, facts


# ── NO PERTURBATION: a kinship construction is left entirely to the family lanes ───────────────────
@requires_model
def test_kinship_construction_not_perturbed():
    facts = _facts("My mother is 62 years old.")
    # the named-value chain must NOT fire on a kin role → no also_known_as edge injected here.
    assert not any(r == "also_known_as" for _s, r, _o in facts), facts
    # the kin + age capture is unchanged.
    assert ("mother", "parent_of", "user") in facts, facts


# ── INERT on a sentence with no (name↔type) binding (fail-safe, zero behaviour change) ─────────────
@requires_model
def test_inert_without_a_name_binding():
    facts = _facts("I upgraded to 500 Mbps about three weeks ago.")
    assert not any(r == "also_known_as" for _s, r, _o in facts), facts
