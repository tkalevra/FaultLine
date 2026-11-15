"""Unit tests for the UNIFIED name↔type binding detector (the ONE connector-agnostic named-instance
path) and the ``_chain_named_instance`` deriver chain.

THE BAR (the brief): deterministic + subject-agnostic. A proper NAME introduced and classified as a
common-noun TYPE binds the SAME way across ALL connectors (naming verb / apposition / copula); the
domain (pet vs kid vs server vs friend) lives ENTIRELY in METADATA — never a code branch. These tests
exercise: the 3 connectors, relation-from-metadata (kinship/gender/social/possession), the age scalar,
the nickname, the mixed-domain enumeration (THE AGNOSTIC PROOF), the bare-collective/type suppression,
determinism, and the no-regression people/pet chains.

Model-backed; skip cleanly if en_core_web_sm is not installed. These run the deriver/detector with
POSTGRES_DSN unset, so the cue maps resolve via the in-code DB-DOWN bootstrap (the same evidenced seed
the migrations write to public) — exactly the fail-safe contract, and enough to prove the binding +
metadata-resolution logic without a live DB.
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
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2026, 6, 27)


def _facts(text):
    """Derive (subject, rel_type, object) triples with the naming chain ON."""
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm", SPINE_NAMING_CHAIN="true")
    return {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(text, _REF)}


def _bindings(text):
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm", SPINE_NAMING_CHAIN="true")
    return m.analyze_name_type_bindings(text)


# ───────────────────────────── THE THREE CONNECTORS ─────────────────────────────

@requires_model
def test_connector_apposition_binds():
    b = _bindings("a son Alex")
    assert len(b) == 1
    assert b[0].name == "Alex" and b[0].type_noun == "son" and b[0].connector == "appos"


@requires_model
def test_connector_naming_verb_binds():
    b = _bindings("a dog named Fraggle")
    assert len(b) == 1
    assert b[0].name == "Fraggle" and b[0].type_noun == "dog" and b[0].connector == "named"


@requires_model
def test_connector_copula_binds():
    # "my friend is Sam" — copula connector (Sam may tag NOUN; the binding still resolves)
    facts = _facts("my friend is Sam")
    # the social tie + the classification, regardless of the exact connector tag
    assert ("sam", "friend_of", "user") in facts
    assert ("sam", "instance_of", "friend") in facts


# ───────────────────────── RELATION FROM METADATA (the only place domains differ) ─────────────

@requires_model
def test_kinship_relation_and_gender_from_metadata():
    facts = _facts("a son Alex")
    assert ("alex", "instance_of", "son") in facts
    assert ("alex", "child_of", "user") in facts          # kinship_noun → child_of (metadata)
    assert ("alex", "has_gender", "male") in facts         # kinship_gender → male (metadata)


@requires_model
def test_kinship_daughter_is_female():
    facts = _facts("a daughter Robin")
    assert ("robin", "child_of", "user") in facts
    assert ("robin", "has_gender", "female") in facts


@requires_model
def test_social_role_relation_from_metadata():
    facts = _facts("a friend Sam")
    assert ("sam", "instance_of", "friend") in facts
    assert ("sam", "friend_of", "user") in facts          # social_role → friend_of (metadata)
    # a person social role is NEVER ``owns``
    assert not any(r == "owns" and o == "sam" for (_, r, o) in facts)


@requires_model
def test_pet_possession_self_possessed():
    # self-possessed animal → a possession edge to the named instance (owns at minimum; the WGM gate
    # upgrades owns→has_pet when the object grounds Animal). The instance_of classifies the kind.
    facts = _facts("I have a dog named Fraggle")
    assert ("fraggle", "instance_of", "dog") in facts
    assert any(s == "user" and o == "fraggle" and r in ("owns", "has_pet") for (s, r, o) in facts)


@requires_model
def test_non_self_possessed_mints_no_possession():
    # "a server named Apollo" stated with NO possessor → only the classification, no fabricated rel.
    facts = _facts("a server named Apollo")
    assert ("apollo", "instance_of", "server") in facts
    assert not any(r in ("owns", "has_pet", "has_role") for (_, r, _o) in facts)


# ───────────────────────────── SCALAR + NICKNAME ─────────────────────────────

@requires_model
def test_age_scalar_in_binding_span():
    facts = _facts("a son Alex 19")
    assert ("alex", "age", "19") in facts


@requires_model
def test_nickname_binds_to_its_own_name_not_a_sibling():
    # THE SIBLING-BLEED REGRESSION: "Jay" belongs to Jamie, NEVER to Alex.
    facts = _facts("a son Alex 19, a son Jamie who goes by Jay 12, and a daughter Robin 10")
    # the deriver works lowercased (the alias is filed lowercased at ingest)
    assert ("jamie", "also_known_as", "jay") in facts
    assert ("alex", "also_known_as", "jay") not in facts
    # ages stay with their own instances
    assert ("alex", "age", "19") in facts
    assert ("jamie", "age", "12") in facts
    assert ("robin", "age", "10") in facts


# ───────────────────────── THE AGNOSTIC PROOF (mixed domains, one path) ─────────────────────

@requires_model
def test_mixed_domain_enumeration_all_bind_via_one_path():
    facts = _facts("I have a son Alex 19, a daughter Robin 10, "
                   "a dog named Fraggle, and a friend Sam.")
    # kid (male) + kid (female) + pet + friend — ALL via the ONE detector, each rel from metadata
    assert ("alex", "child_of", "user") in facts
    assert ("alex", "has_gender", "male") in facts
    assert ("alex", "age", "19") in facts
    assert ("robin", "child_of", "user") in facts
    assert ("robin", "has_gender", "female") in facts
    assert ("robin", "age", "10") in facts
    assert ("fraggle", "instance_of", "dog") in facts
    assert ("sam", "friend_of", "user") in facts
    assert ("sam", "instance_of", "friend") in facts


@requires_model
def test_no_bare_collective_or_type_entities():
    facts = _facts("We have three children together, a son Alex 19, "
                   "a son Jamie who goes by Jay 12, and a daughter Robin 10.")
    # NO bare role/collective entities, NO (user, have, children/son) junk, NO (who, has_state, go)
    subjects_objects = {s for (s, _r, _o) in facts} | {o for (_s, _r, o) in facts}
    assert "children" not in subjects_objects
    assert not any(r == "have" for (_s, r, _o) in facts)
    assert not any(r == "has_state" and o == "go" for (_s, r, o) in facts)
    assert not any(r == "has_role" and o in ("son", "daughter") for (_s, r, o) in facts)


# ───────────────────────────── DETERMINISM ─────────────────────────────

@requires_model
def test_determinism_3x_identical():
    agn = ("I have a son Alex 19, a daughter Robin 10, "
           "a dog named Fraggle, and a friend Sam.")
    runs = [frozenset(_facts(agn)) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


# ───────────────────────────── NO REGRESSION ─────────────────────────────

@requires_model
def test_pets_still_bind():
    facts = _facts("a dog named Fraggle, a cat named Goose")
    assert ("fraggle", "instance_of", "dog") in facts
    assert ("goose", "instance_of", "cat") in facts


@requires_model
def test_spouse_chain_intact():
    facts = _facts("My wife is Quinn")
    assert ("quinn", "spouse", "user") in facts


@requires_model
def test_mother_chain_intact():
    # the named-instance binding now binds the NAME (Sam) as the parent entity — a STRONGER capture
    # than the old role-only (mother, parent_of, user): Sam is a real named entity with the kin rel.
    facts = _facts("My mother is Sam")
    assert ("sam", "parent_of", "user") in facts
    assert ("sam", "has_gender", "female") in facts


@requires_model
def test_appositive_person_role_still_has_role():
    # "Rachel, a real estate agent" — a determiner-introduced TYPE apposition is NOT a name binding;
    # it stays owned by _chain_appositive (has_role), the binding detector never touches it.
    b = _bindings("Rachel, a real estate agent")
    assert b == []
    facts = _facts("Rachel, a real estate agent")
    assert any(r == "has_role" and s == "rachel" for (s, r, _o) in facts)


@requires_model
def test_flag_off_no_named_instance_edges():
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm", SPINE_NAMING_CHAIN="false")
    facts = {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts("a son Alex", _REF)}
    # flag OFF → the named-instance chain is inert (no instance_of/child_of/gender from it)
    assert not any(r in ("child_of", "has_gender") for (_s, r, _o) in facts)


# ─────────────── SELF-POSSESSION VERB-CLASS GATE (the box→cue-class fix) ───────────────
# `_type_is_self_possessed` clause (b) used to test a SINGLE hardcoded verb literal (`== "have"`), so
# "I own a motorcycle named Bolt" / "I possess a painting named Dawn" / "I acquired a server named
# Atlas" dropped the possession edge (only instance_of landed). It now reads the GROWABLE
# `possession_verb` cue class (resolved here via the in-code DB-DOWN bootstrap, DSN unset). These prove
# the box is gone: stative possession verbs beyond "have" mint the possession edge; the TYPE still
# drives the rel (animal→has_pet, object→owns); a non-possession verb mints NO possession edge.

@requires_model
def test_self_possession_verb_own():
    facts = _facts("I own a motorcycle named Bolt.")
    assert ("bolt", "instance_of", "motorcycle") in facts
    assert any(s == "user" and o == "bolt" and r in ("owns", "has_pet") for (s, r, o) in facts)


@requires_model
def test_self_possession_verb_possess():
    facts = _facts("I possess a painting named Dawn.")
    assert ("dawn", "instance_of", "painting") in facts
    assert any(s == "user" and o == "dawn" and r in ("owns", "has_pet") for (s, r, o) in facts)


@requires_model
def test_self_possession_verb_keep_animal_routes_has_pet_or_owns():
    # "keep" is in the possession class; the TYPE (hamster→Animal) drives the rel, not the verb.
    facts = _facts("I keep a hamster named Nibbles.")
    assert ("nibbles", "instance_of", "hamster") in facts
    assert any(s == "user" and o == "nibbles" and r in ("owns", "has_pet") for (s, r, o) in facts)


@requires_model
def test_self_possession_verb_have_no_regression():
    # "have" must STILL work — now via the cue class, not a code literal.
    facts = _facts("I have a dog named Rex.")
    assert ("rex", "instance_of", "dog") in facts
    assert any(s == "user" and o == "rex" and r in ("owns", "has_pet") for (s, r, o) in facts)


@requires_model
def test_self_possession_gate_unit_own_possess_keep_have_true():
    # Direct gate-unit test: the governing verb's lemma drives self-possession via the cue class.
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm", SPINE_NAMING_CHAIN="true")
    for verb in ("own", "possess", "keep", "have", "hold"):
        doc = m._parse(f"I {verb} a car named Zoom")
        type_tok = next((t for t in doc if t.pos_ == "NOUN" and t.lemma_ == "car"), None)
        assert type_tok is not None, f"no type token for verb={verb}"
        assert m._type_is_self_possessed(type_tok) is True, f"verb={verb} should be self-possessed"


@requires_model
def test_self_possession_gate_unit_non_possession_verb_false():
    # A NON-possession verb ("describe") governing the type must NOT register as self-possession.
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm", SPINE_NAMING_CHAIN="true")
    doc = m._parse("I described a car named Zoom")
    type_tok = next((t for t in doc if t.pos_ == "NOUN" and t.lemma_ == "car"), None)
    assert type_tok is not None
    assert m._type_is_self_possessed(type_tok) is False


@requires_model
def test_self_possession_verb_acquired_via_acquisition_class():
    # "acquired" is in the transfer-of-possession (acquisition_verb) class — the gate UNIONS it with the
    # stative possession class, so an acquired named instance is also self-possessed.
    facts = _facts("I acquired a server named Atlas.")
    assert ("atlas", "instance_of", "server") in facts
    assert any(s == "user" and o == "atlas" and r in ("owns", "has_pet") for (s, r, o) in facts)


# ─────────── COORDINATED-CONJUNCT DISTRIBUTION (general, rel-agnostic) ───────────
# The engine binds only the FIRST conjunct of a coordinated list and drops the rest. The general
# conj-distribution replicates the SAME resolved rel over EVERY coordinated member — kinship is just
# the first witness; the rel itself is whatever the chain resolved (child_of from the kinship_noun
# cue, an SVO verb, …). Each member is its own MEMORY entity, never classified into L4 as a type.

@requires_model
def test_kinship_enumeration_copula_binds_every_child():
    # "My kids are Cato, Dez, and Bianca" — ONE kinship head over a coordinated PROPN list; each
    # name is a distinct child (child_of → user), filed at the SINGULAR type place, none dropped.
    facts = _facts("My kids are Cato, Dez, and Bianca.")
    for name in ("cato", "dez", "bianca"):
        assert (name, "child_of", "user") in facts, f"{name} missing child_of"
    # each name is its OWN entity, filed at the singular type — never made a TYPE (no subclass_of).
    assert not any(r == "subclass_of" and s in ("cato", "dez", "bianca")
                   for (s, r, _o) in facts)


@requires_model
def test_kinship_enumeration_children_copula():
    facts = _facts("My children are Cato, Dez, and Bianca.")
    for name in ("cato", "dez", "bianca"):
        assert (name, "child_of", "user") in facts


@requires_model
def test_kinship_enumeration_named_apposition():
    # the "named" apposition form (plural collective head "children" → singular kin lemma "child").
    facts = _facts("I have three children named Cato, Dez, and Bianca.")
    for name in ("cato", "dez", "bianca"):
        assert (name, "child_of", "user") in facts, f"{name} missing child_of"
        assert (name, "instance_of", "child") in facts  # singular type place, not "children"


@requires_model
def test_plural_head_named_coordination_no_phantom_instance():
    # "I have two kids named Max and Lily" — the PLURAL head "kids" is the TYPE (kid), Max/Lily are
    # the named INSTANCES. THE HARD LINE: the plural head is a TYPE, never a name — it must NEVER be
    # minted as a named entity. The live gap minted the phantom ``(kids, also_known_as, max)`` = a
    # child literally named "Kids". Each real name is kept (instance_of kid + child_of user).
    facts = _facts("I have two kids named Max and Lily.")
    for name in ("max", "lily"):
        assert (name, "instance_of", "kid") in facts, f"{name} missing instance_of kid"
        assert (name, "child_of", "user") in facts, f"{name} missing child_of user"
    # NO edge files the plural head "kids" as a name/alias/instance subject (the phantom).
    assert not any(s == "kids" for (s, _r, _o) in facts), \
        f"phantom plural-head entity 'kids' minted: {[e for e in facts if e[0] == 'kids']}"
    assert not any(o == "kids" for (_s, r, o) in facts if r in ("instance_of", "also_known_as"))


@requires_model
def test_plural_head_named_coordination_sibling_generalizes_dogs():
    # SIBLING GENERALIZATION (subject-agnostic, any plural common noun): "two dogs named Rex and
    # Fido" must not mint a phantom "Dogs" instance; Rex/Fido are the named instances of dog.
    facts = _facts("I have two dogs named Rex and Fido.")
    for name in ("rex", "fido"):
        assert (name, "instance_of", "dog") in facts, f"{name} missing instance_of dog"
    assert not any(s == "dogs" for (s, _r, _o) in facts), \
        f"phantom plural-head entity 'dogs' minted: {[e for e in facts if e[0] == 'dogs']}"


@requires_model
def test_singular_named_instance_alias_unchanged_by_plural_firewall():
    # NEGATIVE (held): a SINGULAR head is Number=Sing → NOT declined → keeps its alias-on-referent
    # capture exactly as today. The plural firewall must not touch the single-instance naming case.
    facts = _facts("I have a dog named Rex.")
    assert ("dog", "also_known_as", "rex") in facts
    assert ("rex", "instance_of", "dog") in facts
    luna = _facts("a cat named Luna")
    assert ("cat", "also_known_as", "luna") in luna
    assert ("luna", "instance_of", "cat") in luna


@requires_model
def test_plural_head_no_names_today_behavior():
    # NEGATIVE (held): "two kids" with NO names → no naming construction → no name/alias edge, no
    # crash. The count-only plural stays on today's behavior.
    facts = _facts("I have two kids.")
    assert not any(r == "also_known_as" for (_s, r, _o) in facts)


@requires_model
def test_analyze_naming_declines_plural_head():
    # Pin the SEAM SOURCE (_detect_naming_states -> analyze_naming): a PLURAL head yields NO naming
    # analysis, so the alias-on-head phantom is never emitted; a SINGULAR head still resolves.
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm", SPINE_NAMING_CHAIN="true")
    assert m.analyze_naming("I have two kids named Max and Lily.") is None
    assert m.analyze_naming("I have two dogs named Rex and Fido.") is None
    na = m.analyze_naming("I have a dog named Rex.")
    assert na is not None and na.named == "dog"


@requires_model
def test_single_child_copula_still_binds_name():
    facts = _facts("My son is Cato.")
    assert ("cato", "child_of", "user") in facts
    assert ("cato", "has_gender", "male") in facts


@requires_model
def test_non_kinship_enumeration_is_not_children():
    # GUARDRAIL: a non-kinship enumeration must NEVER become child_of.
    facts = _facts("My favorite colors are red, blue, and green.")
    assert not any(r == "child_of" for (_s, r, _o) in facts)


@requires_model
def test_friends_enumeration_not_child_of():
    # GUARDRAIL: friends are a SOCIAL role, never kinship — no child_of for any name.
    facts = _facts("My friends are Alex and Sam.")
    assert not any(r == "child_of" for (_s, r, _o) in facts)


@requires_model
def test_object_side_distribution_is_rel_agnostic():
    # The general seam distributes ANY rel over coordinated OBJECTS — one edge per member, whatever
    # the verb resolves to (here the SVO verb lemma), each object a distinct entity.
    facts = _facts("I met Alice, Bob, and Carol.")
    for name in ("alice", "bob", "carol"):
        assert any(s == "user" and o == name and r in ("meet", "met")
                   for (s, r, o) in facts), f"{name} missing as a coordinated object"


@requires_model
def test_mixed_role_enumeration_no_role_bleed_after_distribution():
    # The distribution must NOT bleed a role across siblings that each carry their OWN binding.
    facts = _facts("a son Alex 19, a son Jamie who goes by Jay 12, and a daughter Robin 10")
    assert ("robin", "instance_of", "daughter") in facts
    assert ("robin", "instance_of", "son") not in facts          # no bleed of Alex/Jamie's role
    assert ("robin", "has_gender", "female") in facts
    assert ("alex", "has_gender", "male") in facts
