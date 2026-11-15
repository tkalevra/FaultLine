"""Regression — HEARST APPOSITIVE INSTANCE-TYPING (LME multi-session count cluster, qid 80ec1f4f).

CLUSTER: "How many different museums or galleries did I visit in February?" (gold=2). The 2nd operand
is "The Art Cube", stated once as `... "The Art Cube", a new contemporary art gallery`. A "how many
<type>" count grounds the TYPE node then counts its `instance_of` members, so an operand is countable
only if it is typed `instance_of <type>`. The deriver's `_chain_appositive` emitted only a generic
`(cube, has_role, "new art gallery")` for that construction — the named entity was NEVER
`instance_of` a gallery, so the count FALSE-MISSED it.

THE FIX (Hearst 1992 "NP, a(n) NP" appositive-hyponymy, UD `appos`): a PROPER-NAMED entity apposed to
a determiner-introduced common-noun TYPE asserts NAME is-a TYPE. `_chain_appositive` now files the
full proper-name run and types it `(name, instance_of, <head-noun>)` — EXCEPT when the type is a
WordNet `noun.person` role (a person's occupation stays `has_role`). Deterministic + offline
(`wordnet_ladder.is_person_noun`), subject-agnostic, flag-gated `APPOSITIVE_INSTANCE_TYPING` (default
ON; OFF → byte-for-byte the legacy `has_role`).

FAIL-ON-OLD: old code emits `(cube, has_role, ...)` and NO `instance_of` → the instance_of assertions
fail. PASS-ON-FIX: `(art cube, instance_of, gallery)`.

Model-backed; skips cleanly if en_core_web_sm is not installed. POSTGRES_DSN unset (cue maps resolve
via the DB-DOWN bootstrap seed) — enough to prove the deriver logic without a live DB.
"""

import datetime
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


def _model_available() -> bool:
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm")
    return m.linguistics_available()


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(
    not _HAS_MODEL, reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2026, 6, 27)


def _facts(text, **env):
    _e = {"LINGUISTIC_LAYER": "true", "SPACY_MODEL": "en_core_web_sm",
          "APPOSITIVE_INSTANCE_TYPING": "true"}
    _e.update(env)   # caller env overrides the defaults (e.g. flag OFF)
    m = _reload(**_e)
    return {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(text, _REF)}


# ───────────────── THE FIX: a named non-person entity appositively types instance_of ─────────────

@requires_model
def test_named_entity_appositive_types_instance_of():
    """`"The Art Cube", a new contemporary art gallery` → (art cube, instance_of, gallery)."""
    facts = _facts('The Art Cube, a new contemporary art gallery.')
    # PASS-ON-FIX: the name is filed and typed instance_of the HEAD noun (bare "gallery", the
    # countable category — NOT "new art gallery"), so a "how many galleries" count grounds it.
    assert ("art cube", "instance_of", "gallery") in facts, facts
    # FAIL-ON-OLD: the legacy generic has_role must NOT survive for this construction.
    assert not any(r == "has_role" for (_s, r, _o) in facts), facts
    # THE HARD LINE: the name never classified into L4 as a TYPE node; "gallery" is the only type.
    assert not any(r in ("instance_of", "subclass_of") and s == "gallery"
                   for (s, r, _o) in facts), facts


@requires_model
def test_appositive_typing_in_the_full_gold_turn():
    """The real gold-bearing turn still types the venue (co-existing with the met/curator edges)."""
    facts = _facts(
        'I recently met a curator, Rachel Lee, at the opening night of '
        '"The Art Cube", a new contemporary art gallery.')
    assert ("art cube", "instance_of", "gallery") in facts, facts


@requires_model
def test_appositive_typing_generalizes_subject_agnostic():
    """Subject-agnostic: any named non-person entity apposed to a determiner-introduced type."""
    assert ("louvre", "instance_of", "museum") in _facts("I saw the Louvre, a museum."), \
        "named place appositive did not type instance_of"
    # the visit verb AND the type both land — one entity is both visited and gallery-typed.
    vf = _facts("I visited The Art Cube, a contemporary art gallery.")
    assert ("art cube", "instance_of", "gallery") in vf, vf
    assert ("user", "visit", "art cube") in vf, vf


# ───────────────── REGRESSION: a PERSON-role appositive stays has_role ─────────────────

@requires_model
def test_person_role_appositive_stays_has_role():
    """"Rachel, a real estate agent" — a PERSON occupation stays has_role, NEVER instance_of.

    The WordNet `noun.person` lexname gate keeps a person's occupation on the has_role lane so we
    never mis-type a person into L4 (Rachel is not an 'instance_of real estate agent' class node)."""
    facts = _facts("Rachel, a real estate agent.")
    assert any(r == "has_role" and s == "rachel" for (s, r, _o) in facts), facts
    assert not any(r == "instance_of" and s == "rachel" for (s, r, _o) in facts), facts


# ───────────────── GUARD: a non-appositive comma is never mis-bound ─────────────────

@requires_model
def test_non_appositive_comma_not_mistyped():
    """A comma that is NOT a name→type apposition never yields a spurious instance_of/has_role.

    "I called Sarah, and she answered" — the comma introduces a coordinate clause, not an
    appositive rename; nothing hangs off Sarah as a determiner-introduced common-noun type."""
    facts = _facts("I called Sarah, and she answered.")
    assert not any(r in ("instance_of", "has_role") and s == "sarah"
                   for (s, r, _o) in facts), facts


@requires_model
def test_proper_noun_appositive_not_typed():
    """A PROPN appositive ("Paris, France") is a place-name alias, NOT a type — never instance_of.

    Guards the determiner + common-noun gate: France is a PROPN with no determiner, so it is neither
    a Hearst type apposition nor a has_role role."""
    facts = _facts("I visited Paris, France.")
    assert not any(r == "instance_of" and o == "france" for (_s, r, o) in facts), facts
    assert not any(r == "has_role" for (_s, r, _o) in facts), facts


# ───────────────── FLAG OFF → byte-for-byte the legacy has_role ─────────────────

@requires_model
def test_flag_off_reverts_to_has_role():
    """APPOSITIVE_INSTANCE_TYPING=false → the legacy generic has_role, no instance_of typing."""
    facts = _facts('The Art Cube, a new contemporary art gallery.',
                   APPOSITIVE_INSTANCE_TYPING="false")
    assert not any(r == "instance_of" for (_s, r, _o) in facts), facts
    assert any(r == "has_role" for (_s, r, _o) in facts), facts
