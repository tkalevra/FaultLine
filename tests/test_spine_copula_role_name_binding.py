"""MGRCAP — the "my <role> is <PROPN>" COPULA construction binds the ROLE relation to the NAMED
person (the analogue of the kinship copula-name binding that HARDLINE left half-done).

Before this fix, "My manager is Priya Sharma." bound NO role→person edge: ``_chain_copula_name``
emitted only a generic ``(sharma, has_role, manager)`` off the LAST name, while ``_chain_attr_scalar``
minted ``(user, manager, "priya sharma")`` (a novel rel on the USER anchor — the exact edge that
queued the self as a concept) and ``_chain_named_instance`` degraded to ``(priya, related_to, user)``
+ a spurious ``(user, owns, priya)``. The role noun "manager" collapsed and Priya was never bound as
the manager.

FIX (all in ``src/extraction/linguistics.py``, SPINE_NAMING_CHAIN-gated, metadata-driven off the
per-tenant ``role_noun`` ∪ ``social_role`` cue classes — NO domain literal):
  • ``_chain_copula_name`` binds the FULL name via a role-DERIVED ``<role>_of`` relation to the user
    (manager → (priya sharma, manager_of, user)) + files the role as an ``also_known_as`` alias and
    collapses the role noun + the named-person span (so no later chain re-reads them as junk).
  • ``_chain_named_instance`` emits the SAME ``<role>_of`` rel for a ``role_noun`` type (dedup).
  • ``_chain_attr_scalar`` defers a person-role head with a PROPER-NAME complement.

THE HARD LINE: the derived rel is a RELATION (never ``subclass_of``); the name is filed via its
subject surface (alias) and is ``instance_of`` its role type at most — never classified INTO L4.
The ``<role>_of`` rel is NOVEL → it GROWS per-tenant via the WGM gate / ontology_evaluations; it is
NOT pre-seeded. A non-role head ("my car is a Tesla") is untouched. Subject-agnostic; kin/role from
DB cue classes, possessor/person from grammar (Person=1 poss morphology / PROPN). Reference data is
NON-personal.
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


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    # The copula-name binding rides SPINE_NAMING_CHAIN (prod runs it ON).
    monkeypatch.setattr(m, "SPINE_NAMING_CHAIN", True)


@pytest.mark.parametrize("text,name,role", [
    ("My manager is Priya Sharma.", "priya sharma", "manager"),
    ("My boss is Dana Wells.", "dana wells", "boss"),
    ("My supervisor is Alex Kim.", "alex kim", "supervisor"),
])
def test_role_copula_binds_role_relation_to_named_person(text, name, role):
    triples = _triples(m.derive_sentence_facts(text, _REF))
    # the FULL NAME is the entity, carrying the role-DERIVED <role>_of relation to the user
    assert (name, f"{role}_of", "user") in triples, triples
    # the role noun is filed as an ALIAS on the named person (so "my manager" resolves to Priya)
    assert (name, "also_known_as", role) in triples, triples


@pytest.mark.parametrize("text,name,role", [
    ("My manager is Priya Sharma.", "priya sharma", "manager"),
    ("My boss is Dana Wells.", "dana wells", "boss"),
])
def test_role_copula_no_user_anchor_junk(text, name, role):
    # the HARDLINE incident edge: a novel rel on the USER anchor, and false ownership of the person.
    triples = _triples(m.derive_sentence_facts(text, _REF))
    assert ("user", role, name) not in triples, triples          # (user, manager, priya) — gone
    assert ("user", "owns", name) not in triples, triples          # (user, owns, priya) — gone
    # the role noun never becomes a STANDALONE entity subject/object (other than as the alias/type
    # of the named person).
    assert not any(s == role for s, _r, _o in triples), triples
    assert not any(o == role and r not in ("also_known_as", "instance_of")
                   for s, r, o in triples), triples


def test_role_copula_hard_line_name_never_subclassed():
    # THE HARD LINE: the NAME is filed at the place (instance_of its role type at most), NEVER
    # classified INTO L4 via subclass_of.
    triples = _triples(m.derive_sentence_facts("My manager is Priya Sharma.", _REF))
    assert not any(r == "subclass_of" and s == "priya sharma"
                   for s, r, _o in triples), triples


def test_non_role_head_is_untouched():
    # "my car is a Tesla" — car is a SORTAL noun, NOT a person-role → no <role>_of, no role alias,
    # no manager-style binding. The sortal/naming seams keep today's reading.
    triples = _triples(m.derive_sentence_facts("My car is a Tesla Model 3.", _REF))
    assert not any(r.endswith("_of") and o == "user" for _s, r, o in triples), triples
    assert not any(r == "car_of" for _s, r, _o in triples), triples
    # the value still reads as the car's attribute (existing behavior, unchanged)
    assert any(o == "tesla model 3" for _s, _r, o in triples), triples


def test_kinship_copula_unchanged():
    # kinship still binds the SPECIFIC kin rel (no regression from the role branch).
    triples = _triples(m.derive_sentence_facts("My sister is Sarah.", _REF))
    assert ("sarah", "sibling_of", "user") in triples, triples
    assert ("sarah", "also_known_as", "sister") in triples, triples


def test_negated_role_copula_not_captured():
    # "My manager is not Priya" → absence; no role binding.
    triples = _triples(m.derive_sentence_facts("My manager is not Priya.", _REF))
    assert not any(r == "manager_of" for _s, r, _o in triples), triples
