"""FOG-FAMILY F5 — pet possession gap: "We have a cat named Turnip." must file has_pet.

THE WOUND (bench lab, DEV/FINDINGS-fog-family-lab-20261107.md F5): the turn committed ONE valid
fact and NO has_pet row — the always-on object-named-value seam filed only the alias-only edge
((cat, also_known_as, Turnip)) while the raw SVO have-frame carries a rel the seed does not
define, so the gate dropped it. Every pets query then starved and the doc chunk filled the void
(F2 interaction).

THE FIX (SPINE_PET_NAMED_POSSESSION, default ON; migration 308 seeds the pet_noun cue class):
the seam's PET arm files the ownership edge AT THE NAMED INSTANCE for a SELF-POSSESSED binding
(same _type_is_self_possessed grammar the unified named-instance chain uses):
    (user, has_pet, <name>) + (<name>, instance_of, <type>) — the species as the name's TYPE place.
The alias-only emit is skipped for that binding (its direction is inverted for an instance).

PRECISELY SCOPED (the fix contract, pinned below):
  - non-pet heads keep the alias-only edge byte-for-byte ("a playlist called Summer Vibes");
  - a THIRD-PARTY possessive ("My friend has a dog named Rex") is not self-possessed -> unchanged;
  - the possession guard's count-lane territory ("My dog has four legs") is untouched — no
    numeral is consulted, nothing is admitted as a measured amount (L4 9/12 regression class);
  - no name -> no pet edge (the detector requires a ProperName-Type binding);
  - person heads (kin/social/role) were already skipped above the arm.

PURE tests — no DB, no network, no GLiNER2, no LLM; the deriver DIRECT (the cue classes resolve
via the in-code DB-DOWN bootstrap). Run: python3 -m pytest tests/test_f5_pet_named_possession.py -q
"""
import datetime
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics as L  # noqa: E402
from src.extraction.linguistics import derive_sentence_facts, linguistics_available  # noqa: E402

requires_model = pytest.mark.skipif(
    not linguistics_available(), reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2026, 4, 15)


def _facts(sentence):
    return {(f.subject, f.rel_type, f.object) for f in derive_sentence_facts(sentence, reference=_REF)}


# ── THE WOUND, CLOSED: fresh species, fresh names ──────────────────────────────────────────
@requires_model
@pytest.mark.parametrize("sentence,name,species", [
    # THE lab wound itself — first-person PLURAL subject, possessive 'have'.
    ("We have a cat named Turnip.", "turnip", "cat"),
    # fresh material: turtle / ferret / parrot across we/I subjects (the self-possession gate
    # is the CERTIFIED 1st-person grammar — a 2nd-person 'you have' is a different speaker
    # relation and stays out of the fix contract, same as the unified chain's 3b arm).
    ("We have a turtle named Shelly.", "shelly", "turtle"),
    ("I have a ferret named Biscuit.", "biscuit", "ferret"),
    ("We have a parrot named Pepper.", "pepper", "parrot"),
])
def test_pet_possession_files_has_pet_at_the_named_instance(sentence, name, species):
    facts = _facts(sentence)
    assert ("user", "has_pet", name) in facts, facts
    assert (name, "instance_of", species) in facts, facts


@requires_model
def test_compound_head_places_the_species_phrase():
    """A PHRASE cue ("guinea pig") must place the full species phrase in L4, not the bare head
    ("pig" alone under-specifies the species — and bare "pig" is deliberately NOT a pet cue)."""
    facts = _facts("We have a guinea pig named Truffle.")
    assert ("user", "has_pet", "truffle") in facts, facts
    assert ("truffle", "instance_of", "guinea pig") in facts, facts


@requires_model
def test_flag_off_restores_the_alias_only_shape_byte_for_byte(monkeypatch):
    """SPINE_PET_NAMED_POSSESSION=OFF -> the seam is skipped entirely: the pre-fix shape
    ((user, have, cat) + the inverted alias) is restored exactly."""
    monkeypatch.setattr(L, "SPINE_PET_NAMED_POSSESSION", False)
    facts = _facts("We have a cat named Turnip.")
    assert ("user", "have", "cat") in facts, facts
    assert ("cat", "also_known_as", "turnip") in facts, facts
    assert not any(r == "has_pet" for _s, r, _o in facts), facts


# ── PRECISE SCOPING: every lane the fix must NOT touch ─────────────────────────────────────
@requires_model
def test_non_pet_head_keeps_the_alias_only_edge(monkeypatch):
    """Non-pet named objects keep the pre-fix shape byte-for-byte (the arm never reads them).

    Flag context made explicit (entity-centric filing, 2026-11-15): with ENTITY_CENTRIC_FILING
    ON (the new default) a THING head like `playlist` is minted as an entity by the thing arm
    (tests/test_entity_centric_filing.py pins that shape) — this pin holds the F5 contract's
    own shape, the alias-only edge, under the legacy flag state. The PET arm itself is
    untouched in both states (its contract pins above run at defaults)."""
    monkeypatch.setattr(L, "ENTITY_CENTRIC_FILING", False)
    facts = _facts("I created a playlist called Summer Vibes.")
    assert ("playlist", "also_known_as", "summer vibes") in facts, facts
    assert not any(r == "has_pet" for _s, r, _o in facts), facts


@requires_model
def test_third_party_possessive_never_files_user_pet():
    '''"My friend has a dog named Rex." — the dog is the FRIEND's, not the speaker's: the
    self-possession gate refuses, so NO (user, has_pet, ...) is minted (the alias-only
    pre-fix shape too — the F5 arm adds nothing for a third party's pet).'''
    facts = _facts("My friend has a dog named Rex.")
    assert not any(r == "has_pet" for _s, r, _o in facts), facts
    assert ("dog", "also_known_as", "rex") in facts, facts  # alias-only, unchanged


@requires_model
def test_count_lane_untouched_by_the_pet_arm():
    '''"My dog has four legs." — the certified possession guard's count-lane territory
    (the L4 9/12 hijack class: legs/bedrooms/pages). The pet arm reads NAME bindings only —
    no numeral is consulted, nothing is admitted as a measured amount, no has_pet-of-name.'''
    facts = _facts("My dog has four legs.")
    assert not any(r == "has_pet" and o != "dog" for _s, r, o in facts), facts
    assert not any(r == "instance_of" for _s, r, _o in facts), facts
    # the count capture itself stands (the scalar lane's own business — pinned as "not regressed")
    assert any(o in ("four", "4") and r == "legs" for _s, r, o in facts), facts


@requires_model
def test_nameless_possession_files_no_pet_instance_edges():
    """No ProperName-Type binding -> the arm cannot fire (it requires a NAME to file AT)."""
    facts = _facts("We have a cat.")
    assert not any(r == "has_pet" for _s, r, _o in facts), facts
    assert not any(r == "instance_of" for _s, r, _o in facts), facts


@requires_model
def test_pet_noun_class_resolves_db_down():
    """The cue class must NEVER resolve empty (an empty floor silently disables the arm)."""
    pets = L._pet_nouns()
    assert {"cat", "dog", "rabbit", "hamster", "fish", "pet", "turtle", "guinea pig"} <= pets
