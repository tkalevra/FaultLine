"""Unit tests for the NAME-REPAIR lane — replacive/corrective negation ("… is Caryl, NOT Carol").

PURE tests — no DB, no network, no GLiNER2, no LLM. spaCy only (SPACY_MODEL=en_core_web_sm).

THE BUG THEY PIN (live pre-prod, spine ON):
    turn 1  "My mother's name is Carol and she is 62 years old."
    turn 2  "Actually my mother's name is Caryl, not Carol."
  → the deriver read turn 2 as a FRESH assertion and emitted ``(caryl, parent_of, user)`` plus the
    junk appositive ``(caryl, has_role, carol)``. At ingest ``EntityRegistry.resolve`` minted a
    SECOND person (uuid5 of "caryl" ≠ uuid5 of "carol"), so the user ended up with TWO Class-A
    ``parent_of`` mothers, neither superseded, and the old label still flagged preferred.

THE CONTRACT:
  A repair is a LABEL CHANGE ON ONE REFERENT, never a second referent.
    * SKOS Reference §5, integrity conditions S13/S14 (https://www.w3.org/TR/skos-reference/#L1567):
      a resource has AT MOST ONE ``skos:prefLabel`` and prefLabel/altLabel are pairwise disjoint —
      renaming demotes the old label to an altLabel on the SAME resource.
    * Replacive/corrective negation: Horn, *A Natural History of Negation* (1989), ch. 6; McCawley
      (1991) on "not X but Y". The negated conjunct is a REJECTED ALTERNATIVE — not asserted, and
      co-referent with the asserted one.
    * Conversational repair: Schegloff, Jefferson & Sacks (1977), *Language* 53(2).
  So the deriver anchors the repair on the REJECTED surface (the surface the existing entity is
  already registered under) and emits the change as ``pref_name``.

  Subject-agnostic: pure UD dependency + linear order. No name list, no kin-role list in code (the
  kin rel comes from the ``kinship_noun`` DB cue class), no repair-marker vocabulary.

Run: python3 tools/fltest.py --bug NAMEREPAIR --test tests/test_name_repair_corrective_negation.py
     (tests/ is gitignored → git add -f)
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
# NOTE: deliberately does NOT set SPINE_NAMING_CHAIN. linguistics.py reads its flags at MODULE
# IMPORT, so a setdefault here LEAKS into every other test file collected in the same session
# (it flipped 3 unrelated kinship-apposition cases red in a combined run). The repair lane itself
# is flag-independent; only the role-slot alias leg is gated, and that test reads the live flag.

from src.extraction import linguistics as L  # noqa: E402


pytestmark = pytest.mark.skipif(
    not L.linguistics_available(), reason="spaCy model unavailable (SPACY_MODEL=en_core_web_sm)"
)


def _edges(text):
    return [(f.subject, f.rel_type, f.object) for f in (L.derive_sentence_facts(text, None, None) or [])]


# ── the structural detector ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,asserted,rejected", [
    ("Actually my mother's name is Caryl, not Carol.", "caryl", "carol"),
    ("Actually my sister's name is Ann, not Anne.", "ann", "anne"),
    ("Actually my dog is called Luna, not Bella.", "luna", "bella"),
])
def test_detects_replacive_negation_alternative(text, asserted, rejected):
    doc = L._parse(text)
    hits = {
        (t.text.lower(), L.corrective_negation_alternative(t).text.lower())
        for t in doc if L.corrective_negation_alternative(t) is not None
    }
    assert (asserted, rejected) in hits


@pytest.mark.parametrize("text", [
    "Rachel, a real estate agent, called me.",   # ordinary apposition — no negation
    "Her name is not Carol.",                    # plain predicate negation — no alternative
    "My mother's name is Carol.",                # plain assertion
])
def test_no_false_positive_without_the_contrast(text):
    doc = L._parse(text)
    assert all(L.corrective_negation_alternative(t) is None for t in doc)


# ── the deriver contract ─────────────────────────────────────────────────────────────────────────

def test_name_repair_anchors_on_the_existing_entity_not_a_rival():
    """THE REGRESSION. The repair must re-use the OLD surface as the subject, so the kin edge is
    idempotent with turn 1 (exactly ONE parent_of after ingest) and the new name arrives as a
    ``pref_name`` LABEL CHANGE on that same entity."""
    first = _edges("My mother's name is Carol and she is 62 years old.")
    assert ("carol", "parent_of", "user") in first

    repair = _edges("Actually my mother's name is Caryl, not Carol.")

    # 1. the kin edge is re-asserted on the EXISTING surface — same triple as turn 1 → idempotent.
    assert ("carol", "parent_of", "user") in repair
    # 2. NO rival person: the newly asserted name never becomes a second kin subject.
    assert not [e for e in repair if e[0] == "caryl" and e[1] == "parent_of"]
    # 3. the change rides the naming layer as a prefLabel flip on the existing entity.
    assert ("carol", "pref_name", "caryl") in repair
    # 4. the rejected alternative is never asserted as a relation (the old has_role junk).
    assert not [e for e in repair if e[1] == "has_role"]
    # 5. THE HARD LINE — a name is never classified into L4.
    assert not [e for e in repair if e[1] in ("instance_of", "subclass_of")
                and e[0] in ("caryl", "carol")]


@pytest.mark.parametrize("text,kin_rel,anchor,new_name", [
    ("Actually my mother's name is Caryl, not Carol.", "parent_of", "carol", "caryl"),
    ("Actually my sister's name is Ann, not Anne.", "sibling_of", "anne", "ann"),
    ("Actually my brother's name is Bob, not Rob.", "sibling_of", "rob", "bob"),
])
def test_holds_for_any_kin_role_and_any_name(text, kin_rel, anchor, new_name):
    """Subject-agnostic: no "mother"/"carol" special case anywhere — the kin rel is resolved from
    the ``kinship_noun`` cue class and the anchor from the parse."""
    edges = _edges(text)
    assert (anchor, kin_rel, "user") in edges
    assert (anchor, "pref_name", new_name) in edges
    assert not [e for e in edges if e[0] == new_name and e[1] == kin_rel]


@pytest.mark.skipif(not L.SPINE_NAMING_CHAIN,
                    reason="role-slot alias leg is gated by SPINE_NAMING_CHAIN")
def test_role_slot_alias_follows_the_anchor():
    """The role→person alias ("mother") must land on the SAME entity the repair anchored on, or a
    later split atom ("My mother is 63") re-mints a parallel role entity."""
    edges = _edges("Actually my mother's name is Caryl, not Carol.")
    assert ("carol", "also_known_as", "mother") in edges
    assert ("caryl", "also_known_as", "mother") not in edges


def test_role_slot_alias_never_lands_on_the_rejected_rival():
    """Flag-independent half of the above: whatever the role-alias flag, the role must never be
    aliased onto the newly asserted name (that is the rival entity the bug created)."""
    edges = _edges("Actually my mother's name is Caryl, not Carol.")
    assert ("caryl", "also_known_as", "mother") not in edges


# ── no collateral damage on the non-repair paths ────────────────────────────────────────────────

def test_plain_naming_assertion_unchanged():
    edges = _edges("My wife's name is Nora.")
    assert ("nora", "spouse", "user") in edges
    assert not [e for e in edges if e[1] == "pref_name"]


def test_third_party_possessor_unchanged():
    assert ("susan", "parent_of", "john") in _edges("John's mother's name is Susan.")


def test_ordinary_appositive_role_still_captured():
    assert ("rachel", "has_role", "estate agent") in _edges("Rachel, a real estate agent, called me.")


def test_rejected_alternative_never_becomes_a_role_in_any_domain():
    """The appositive lane must drop a replacive-negation alternative regardless of subject —
    "I want tea, not coffee" must not assert (tea, has_role, coffee)."""
    assert not [e for e in _edges("I want tea, not coffee.") if e[1] == "has_role"]


# ── DB-level assertion (described — needs a live tenant; not run here) ───────────────────────────
#
# After ingesting the two turns through the MCP into a fresh tenant schema, the tenant DB MUST hold:
#
#   SELECT count(*) FROM facts
#    WHERE rel_type='parent_of' AND superseded_at IS NULL AND archived_at IS NULL;   -- == 1
#
#   SELECT alias, is_preferred FROM entity_aliases WHERE entity_id = <that fact's subject_id>;
#     -- 'caryl' is_preferred = true      (skos:prefLabel, S14: exactly one)
#     -- 'carol' is_preferred = false     (demoted to skos:altLabel — preserved, not deleted)
#     -- 'mother' is_preferred = false    (role slot)
#
#   SELECT count(*) FROM entity_aliases WHERE alias='caryl' AND is_preferred = true;  -- == 1
#
# and NO row in entity_name_conflicts for alias='caryl' (there is no genuine collision — one
# referent, one prefLabel). The alias flip is performed by EntityRegistry.register_alias() on the
# pref_name edge, which demotes the incumbent by _PREFERENCE_RANK; the old label is retained
# non-preferred, so recall by the OLD name still resolves to the same person.
