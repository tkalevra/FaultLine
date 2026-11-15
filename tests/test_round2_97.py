"""#97 (critic round 2 on fix/ingest-capture-b1 @ 15590391).

(a) #68 regression — "My favorite car is my Tesla." lost (user, owns, tesla): the complement carries
    its own speaker possessive, so it is a possession as well as the preference value.
(b) #57 polysemy — `_noun_denotes_concrete_thing` rejected any noun with ANY attribute-like sense, so
    phone / hair / coffee / room kept the junk rel (user, <noun>, <adj>). It now decides by the
    DOMINANT (first-listed) WordNet sense.
(c) Active-voice naming — the oprd names the DIRECT OBJECT: "My dad calls me Champ" → a user alias
    Champ (round 1 and base: (champ, parent_of, user) + (champ, also_known_as, dad)); "They call my
    mother Didi" → the mother is Didi.
Item 6 of the round brief — `_possession_rel_for_type`'s WordNet decline had no pin (forcing it off
left everything green); pinned here as a unit with the overlay stubbed.

Fresh-domain controls: "My cousins call me Bix", "My kettle is shiny", "My favourite boat is my
Kestrel"; attribute nouns ("my mood is good") keep the scalar reading.
"""
from __future__ import annotations

import datetime

import pytest

pytest.importorskip("spacy")

from src.extraction import linguistics as L  # noqa: E402
from tests.laneb_e2e_support import (  # noqa: E402
    e2e_fixture, fact_rows, module_flags_fixture, state, user_aliases,
)

_flags = module_flags_fixture()
e2e = e2e_fixture()
_REF = datetime.datetime(2026, 10, 1)


def _facts(s):
    return {(f.subject, f.rel_type, (f.object or "").lower()) for f in L.derive_sentence_facts(s, _REF)}


# ── (a) ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,thing,pref", [
    ("My favorite car is my Tesla.", "tesla", "favorite_car"),
    ("My favourite boat is my Kestrel.", "kestrel", "favourite_boat"),     # fresh domain
])
def test_a_self_possessed_preference_value_is_also_owned(text, thing, pref):
    got = _facts(text)
    assert ("user", pref, thing) in got, got
    assert ("user", "owns", thing) in got, f"#97a: possession dropped: {got}"


def test_a_control_bare_preference_value_still_not_owned():
    assert not any(r == "owns" for (_s, r, o) in _facts("My favorite fruit is mango.")
                   if o == "mango")


# ── (b) ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("noun,adj", [
    ("phone", "new"), ("hair", "brown"), ("coffee", "cold"), ("room", "tidy"),
    ("kettle", "shiny"),                                                   # fresh domain
])
def test_b_dominant_sense_concrete_noun_takes_a_state(noun, adj):
    got = _facts(f"My {noun} is {adj}.")
    assert ("user", noun, adj) not in got, f"#97b: junk rel named after the noun: {got}"
    assert (noun, "has_state", adj) in got and ("user", "owns", noun) in got, got


def test_b_attribute_noun_keeps_scalar():
    assert ("user", "mood", "good") in _facts("My mood is good.")


# ── (c) ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,name,role", [
    ("My dad calls me Champ.", "champ", "dad"),
    ("My friends call me C.", "c", "friend"),
    ("My cousins call me Bix.", "bix", "cousin"),                         # fresh domain
])
def test_c_active_naming_names_the_object_speaker(text, name, role):
    got = _facts(text)
    assert ("user", "also_known_as", name) in got, f"#97c: speaker not named: {got}"
    assert not any(s.rstrip(".") == name for (s, _r, _o) in got), f"#97c: role rebound: {got}"


def test_c_active_naming_names_a_noun_object_role():
    got = _facts("They call my mother Didi.")
    assert ("didi", "parent_of", "user") in got, got


def test_c_active_without_secondary_predicate_names_nothing():
    got = _facts("My mother calls Anna every day.")
    assert not any(r in ("parent_of", "also_known_as") for (_s, r, _o) in got), got


# ── item 6 — _possession_rel_for_type's WordNet decline ─────────────────────────────────────────
def test_possession_rel_for_type_lexicon_decline(monkeypatch):
    from src.api import rel_type_overlay
    meta = {"owns": {"head_types": ["Person"], "tail_types": ["Animal", "Object"],
                     "is_hierarchy_rel": False},
            "has_pet": {"head_types": ["Person"], "tail_types": ["Animal"],
                        "is_hierarchy_rel": False}}
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://stub/none_test")
    monkeypatch.setattr(rel_type_overlay, "resolve_current", lambda _dsn: meta)
    assert L._possession_rel_for_type("dog", instance_type_tag="Animal") == "has_pet"
    assert L._possession_rel_for_type("specimen", instance_type_tag="Animal") == "owns", \
        "#38.1 decline: a NER Animal tag on a noun with no animal sense must not pick has_pet"


# ── E2E ──────────────────────────────────────────────────────────────────────────────────────────
def test_e2e_round2_97(e2e):
    state(e2e, "My dad calls me Champ.")
    state(e2e, "My phone is new.")
    state(e2e, "My favorite car is my Tesla.")
    assert "champ" in user_aliases(e2e), "#97c: speaker alias not filed"
    facts = fact_rows(e2e)
    assert not any(s == "champ" for (s, _r, _o) in facts), sorted(facts)
    assert ("phone", "has_state", "new") in facts and ("user", "phone", "new") not in facts, \
        sorted(facts)
    assert ("user", "owns", "tesla") in facts, sorted(facts)
