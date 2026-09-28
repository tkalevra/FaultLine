"""Regression gate for the SPINE deriver's PASSIVE-NAMING role binding (``_chain_named_role``).

GAP (validated on an empty seat): "My mother is named Sarah" recalled "Mother", not "Sarah".
``_chain_genitive_name`` handled the "my mother's NAME is Sarah" genitive surface, but NOT the
"my mother IS NAMED Sarah" / "is called Sarah" PASSIVE-naming surface — the name was dropped and
only ``(mother, parent_of, user)`` was minted, so the entity's primary surface stayed the role
noun. The paired 3rd-person "and she is 62" then bound its age to a bare "sarah" via coref while the
kin relation hung off a separate "mother" node — two entities, name lost.

FIX: ``_chain_named_role`` (sibling of ``_chain_genitive_name`` for the passive surface) binds the
PROPER NAME as the entity, attaches the kin relation to THAT person, collapses the role noun, and
(flag-gated) files the role as an ``also_known_as`` alias. THE HARD LINE: the name is filed via its
subject surface (the alias registry), NEVER classified into L4. Subject-agnostic — naming verb from
the DB ``naming_verb`` cue class, kin rel from the ``kinship_noun`` cue map, possessor/person from
grammar (Person=1 poss morphology / PROPN). All reference data is NON-personal.
"""
import datetime

import pytest

from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine deriver no-ops",
)

_REF = datetime.date(2023, 6, 1)


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


@pytest.mark.parametrize("text,name,kin", [
    ("My mother is named Sarah.", "sarah", "parent_of"),
    ("My mother is called Sarah.", "sarah", "parent_of"),
    ("My sister is named Kate.", "kate", "sibling_of"),
    ("My brother is called Sam.", "sam", "sibling_of"),
])
def test_passive_named_role_binds_proper_name_no_role_entity(text, name, kin):
    triples = _triples(m.derive_sentence_facts(text, _REF))
    # the PROPER NAME is the entity carrying the kin relation to the user
    assert (name, kin, "user") in triples, triples
    # the ROLE noun never becomes a standalone entity, and the kin rel points at the NAME
    for subj, rel, obj in triples:
        if rel == kin:
            assert subj == name, triples
        assert obj != "mother" and subj != "mother", triples


def test_passive_named_role_then_thirdperson_age_lands_on_named_person():
    # "My mother is named Sarah and she is 62" — the age must land on the NAMED person (sarah),
    # unified with the kin binding, not a bare/parallel entity.
    triples = _triples(m.derive_sentence_facts("My mother is named Sarah and she is 62.", _REF))
    assert ("sarah", "parent_of", "user") in triples, triples
    assert ("sarah", "age", "62") in triples, triples


def test_passive_named_relational_nonkin_role():
    # a relational (non-kin) role → generic related_to, name still bound as the entity
    triples = _triples(m.derive_sentence_facts("My manager is named Bob.", _REF))
    assert ("bob", "related_to", "user") in triples, triples
    assert not any(subj == "manager" or obj == "manager" for subj, _r, obj in triples), triples


def test_passive_named_role_alias_leg_when_flag_on(monkeypatch):
    # with SPINE_NAMING_CHAIN on, the role surface is filed as an also_known_as of the named person
    monkeypatch.setattr(m, "SPINE_NAMING_CHAIN", True)
    triples = _triples(m.derive_sentence_facts("My mother is named Sarah.", _REF))
    assert ("sarah", "parent_of", "user") in triples, triples
    assert ("sarah", "also_known_as", "mother") in triples, triples


def test_genitive_name_surface_still_works():
    # sibling genitive surface must be untouched (no regression)
    triples = _triples(m.derive_sentence_facts("My mother's name is Carol.", _REF))
    assert ("carol", "parent_of", "user") in triples, triples


def test_passive_named_type_not_hijacked_as_kin():
    # "My dog is named Rex" — dog is a TYPE, not a kin/relational role → the kin chain must NOT fire
    # (no (rex, parent_of/related_to, user)); left to the named-instance/naming seams.
    triples = _triples(m.derive_sentence_facts("My dog is named Rex.", _REF))
    assert not any(rel in ("parent_of", "child_of", "sibling_of", "spouse", "related_to")
                   and subj == "rex" for subj, rel, _o in triples), triples


def test_negated_passive_named_role_not_captured():
    triples = _triples(m.derive_sentence_facts("My mother is not named Sarah.", _REF))
    assert not any(rel == "parent_of" and subj == "sarah" for subj, rel, _o in triples), triples


# ── CAP2DUP: cross-atom role-noun collapse (the shipped-fix residual) ────────────────────────────
#
# The LLM atomizer splits "My mother is named Sarah and she is 62." into "My mother is named Sarah"
# + a PRONOUN-RESOLVED "My mother is 62." — the second atom carries the possessed KIN ROLE but NO
# naming verb, so ``_chain_named_role``'s in-atom collapse can't reach it: the possessive kin-bind
# and the copula-measure chain both read the bare surface "mother" and mint a PARALLEL standalone
# "mother" entity carrying (mother, parent_of, user) + (mother, age, 62) ALONGSIDE the named "sarah"
# — the duplicate seen in pre-prod validation. ``build_turn_role_name_map`` computes the whole-turn
# {role → name} binding ONCE (order-independent, like the turn-person pool); threaded as
# ``turn_role_names`` it resolves the bare role subject to the named person, so ONLY "sarah" carries
# the kin edge + age. Fail-safe: a role the turn never named stays on its own surface (no fabrication).


def test_turn_role_name_map_binds_passive_and_genitive():
    assert m.build_turn_role_name_map(
        "My mother is named Sarah and she is 62.") == {"mother": "sarah"}
    assert m.build_turn_role_name_map(
        "My mother's name is Priya and she is 62.") == {"mother": "priya"}


def test_turn_role_name_map_empty_when_role_never_named():
    # a measurement-only turn (no naming construction) binds nothing → the deriver keeps "mother".
    assert m.build_turn_role_name_map("My mother is 62.") == {}
    # non-kin sortal role ("user name") is never a kin/relational binding.
    assert m.build_turn_role_name_map("My user name is Bob.") == {}
    # negated naming never binds.
    assert m.build_turn_role_name_map("My mother is not named Sarah.") == {}


def test_split_measure_atom_collapses_role_onto_named_person():
    # The atomizer's pronoun-resolved split atom: "My mother is 62." with the whole-turn binding.
    role_map = m.build_turn_role_name_map("My mother is named Sarah and she is 62.")
    triples = _triples(m.derive_sentence_facts("My mother is 62.", _REF, turn_role_names=role_map))
    # BOTH the kin edge and the age land on the NAMED person, unified with the naming atom …
    assert ("sarah", "parent_of", "user") in triples, triples
    assert ("sarah", "age", "62") in triples, triples
    # … and NO parallel "mother" role entity carries the kin edge or the scalar.
    assert not any(subj == "mother" for subj, _r, _o in triples), triples


def test_assembled_turn_has_single_named_person_no_role_dup():
    # Simulate the assembled spine path: the naming atom + the pronoun-resolved measurement atom,
    # both threaded with the whole-turn role map (+ turn-person pool for the "she" coref variant).
    turn = "My mother is named Sarah and she is 62."
    role_map = m.build_turn_role_name_map(turn)
    assert role_map == {"mother": "sarah"}
    union = []
    for atom, tp in (("My mother is named Sarah.", []),
                     ("My mother is 62.", []),          # she→my mother (atomizer coref)
                     ("she is 62.", ["sarah"])):        # she→sarah (turn-person coref) — same target
        union += _triples(m.derive_sentence_facts(
            atom, _REF, turn_persons=tp, turn_role_names=role_map))
    # exactly ONE entity ("sarah") holds parent_of and age; NO "mother" entity holds either.
    parent_subjs = {s for s, r, _o in union if r == "parent_of"}
    age_subjs = {s for s, r, _o in union if r == "age"}
    assert parent_subjs == {"sarah"}, union
    assert age_subjs == {"sarah"}, union
    assert not any(s == "mother" for s, _r, _o in union), union


def test_role_name_map_none_is_byte_identical_no_regression():
    # Threading nothing (harvest unwired) must leave the per-atom reading unchanged (fail-safe).
    assert _triples(m.derive_sentence_facts("My mother is 62.", _REF, turn_role_names=None)) == \
        _triples(m.derive_sentence_facts("My mother is 62.", _REF))
