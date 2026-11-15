"""Löbner FUNCTIONAL-NOUN arm — the attribute SLOT is not a possessum and not a referent.

Löbner (1985), "Definites", Journal of Semantics 4(4):279-326, splits common nouns into SORTAL,
RELATIONAL and FUNCTIONAL; Löbner (2011), "Concept Types and Determination", J. Semantics
28(3):279-333, gives the 4-way grid in which a FUNCTIONAL noun is type <e,e> — a function from an
argument (the possessor) to a UNIQUE value.

``_chain_possessive`` shipped a person-role (relational) arm and a sortal arm and NO functional
arm, so a functional head fell through to the sortal/ownership leg: the SLOT was minted as an
entity and the VALUE was destroyed. Two defects, one class:

  D1  "My favourite colour is Blue."   -> (user, owns, 'favourite colour')  [+ has_pet upgrade
                                          downstream: the user's colour became their PET]
  D2  "Alicia is the user's name" -> (name, related_to, user), 'Alicia' DROPPED

Every assertion below is on the string the LIVE deriver emits, printed and eyeballed first — not
on the value we wish it emitted.
"""
import os

import pytest

# The deriver silently NO-OPs to [] without a loadable spaCy model, which reads as a product
# failure. Skip loudly rather than assert green on an empty rig.
from src.extraction import linguistics as L

pytestmark = pytest.mark.skipif(
    L._get_nlp() is None,
    reason="spaCy model unavailable — the deriver NO-OPs to [] and every assertion would be "
           "vacuous. Point SPACY_MODEL at the model directory.",
)


def _edges(sentence):
    """(subject, rel_type, object) triples the live deriver emits, lowercased."""
    return {
        ((f.subject or "").strip().lower(),
         (f.rel_type or "").strip().lower(),
         (f.object or "").strip().lower())
        for f in (L.derive_sentence_facts(sentence, None, None) or [])
    }


# ── CONTROLS — must be identical with the arm ON or OFF ────────────────────────────────────────
# A uniform empty result is an ENV failure until proven otherwise; these prove the rig is live
# AND that the functional arm does not touch a sortal possessive or a relational one.

def test_control_sortal_possessive_scalar_is_untouched():
    """"My car's mileage is 40000." — 'car' is SORTAL, 'mileage' its scalar. Not a slot."""
    e = _edges("My car's mileage is 40000.")
    assert ("user", "owns", "car") in e, e
    assert ("car", "mileage", "40000") in e, e


def test_control_relational_kin_possessive_is_untouched():
    """"My mother's name is Diane." — the RELATIONAL role arm keeps binding the named person."""
    assert ("diane", "parent_of", "user") in _edges("My mother's name is Diane."), \
        _edges("My mother's name is Diane.")


def test_control_named_instance_still_owns_its_type():
    """"My dog Rex is a poodle." — a named instance is a possessum, NOT a functional slot."""
    e = _edges("My dog Rex is a poodle.")
    assert ("dog rex", "instance_of", "poodle") in e, e


# ── DEFECT 1 — a preference SELECTOR makes an otherwise sortal head FUNCTIONAL ─────────────────
# "my favourite colour" denotes a slot with one value per possessor. (user, owns, <slot>) files
# the ADDRESS of a value as though it were the thing.

def test_preference_slot_is_never_owned_declarative():
    e = _edges("My favourite colour is Blue.")
    assert not [t for t in e if t[1] == "owns"], \
        f"the attribute SLOT was minted as a possessum: {e}"


def test_preference_slot_is_never_owned_interrogative():
    """The path the downstream twin-suppression cannot reach.

    main.py's SPINE_AFFECT_PREFERENCE suppression only drops the (user, owns, <phrase>) twin when
    the preference seam captured a matching rel IN THE SAME BATCH — but that seam runs on
    DECLARATIVES only, so on a question it is structurally silent and has nothing to key on, while
    HARVEST_POSSESSIVE_PRESUPPOSITION re-derives the dropped clause and keeps the junk edge. A
    suppression conditioned on another lane's output cannot fire where that lane is silent, so
    this one is keyed on the CONSTRUCTION instead.
    """
    e = _edges("What is my favourite colour?")
    assert not [t for t in e if t[1] == "owns"], \
        f"a QUESTION committed an ownership fact: {e}"


def test_preference_value_capture_is_not_collateral_damage():
    """Suppressing the slot must not cost the VALUE — the slot edge never carried it anyway."""
    assert ("user", "favourite_car", "volvo") in _edges("My favourite car is a Volvo."), \
        _edges("My favourite car is a Volvo.")


# ── DEFECT 2 — a naming noun is inherently functional; both copular orders must converge ───────
# Higgins (1973); Mikkelsen (2005: ch. 1, exx. 1.1/1.2). The order with the naming noun in `nsubj`
# is the SPECIFICATIONAL one; the order with it in `attr` is the predicational/canonical one. The
# chain knew the construction in ONE direction only.

@pytest.mark.parametrize("sentence", [
    "My name is Alicia.",              # naming noun = nsubj, 1st-person possessor
    "The user's name is Alicia.",      # naming noun = nsubj, bare nominal possessor
    "Alicia is the user's name.",      # naming noun = attr,  bare nominal possessor
    "Alicia is my name.",              # naming noun = attr,  1st-person possessor
])
def test_all_four_naming_orders_converge_on_the_alias(sentence):
    """A specificational pair must specify the SAME value whichever way it is linearized."""
    assert ("user", "also_known_as", "alicia") in _edges(sentence), \
        f"{sentence!r} -> {_edges(sentence)}"


def test_the_name_is_never_dropped_as_uncovered_residue():
    """'Alicia' was logged derive_residue_uncovered — captured nowhere at all."""
    e = _edges("Alicia is the user's name.")
    assert any("alicia" in t[0] or "alicia" in t[2] for t in e), \
        f"the NAME itself was dropped: {e}"


def test_the_naming_noun_never_becomes_an_entity():
    """(name, related_to, user) files the SLOT as the referent — THE HARD LINE, inverted."""
    e = _edges("Alicia is the user's name.")
    assert not [t for t in e if t[0] in L._naming_nouns()], \
        f"a naming noun was minted as an entity: {e}"


def test_the_naming_rail_is_asked_not_the_literal_lemma_name():
    """~10 sites gated on the literal lemma "name" while the DB-grown naming_noun rail already
    carried alias/byname/epithet/moniker/nickname/surname — so "… is the user's NICKNAME" failed
    identically to a construction "… NAME" handled fine. That is a subject-agnostic violation:
    the class lives on the per-tenant rail and the code must ASK the rail."""
    assert "nickname" in L._naming_nouns(), L._naming_nouns()
    assert ("user", "also_known_as", "alicia") in \
        _edges("Alicia is the user's nickname."), \
        _edges("Alicia is the user's nickname.")


def test_spacy_token_identity_is_compared_by_offset_not_by_is():
    """spaCy builds a NEW Token wrapper on every Doc access, so `c is not role` is ALWAYS True and
    the "exclude the role itself" filter excluded nothing. Latent until the predicational arm
    arrived: in the bare-possessor frame the naming noun's only `poss` child IS the role, so the
    filter re-selected it and the possessor collapsed onto the role surface — (alicia,
    related_to, user) instead of the alias edge. Same defect class as commit 53545151.

    This asserts the PROPERTY rather than the code: a token compared to itself is `is`-distinct
    but offset-identical.
    """
    doc = L._get_nlp()("Alicia is the user's name.")
    name = [t for t in doc if t.text == "name"][0]
    role = next(c for c in name.children if c.dep_ == "poss")
    twin = doc[role.i]
    assert twin is not role, "spaCy started reusing Token objects — re-check the filter"
    assert twin.i == role.i, "offset identity is what must be compared"


# ── FIX A — the ladder is a CHECK, not a fallback ──────────────────────────────────────────────
# RDF Schema 1.1 (W3C Recommendation, 25 February 2014) §3.3 / §3.4: rdf:type and rdfs:subClassOf
# are used "to state that" — they are ASSERTIONS. A coarse six-label NER tag is a heuristic
# ANNOTATION. An annotation must not silently outrank an assertion when the two disagree.

def test_a_contradicted_ner_stamp_declines_the_structural_upgrade():
    """The measured production case: entity_type='Animal' (NER) vs instance_of -> colour
    (Concept), written in the SAME commit one hop apart. The disproof was already in the
    transaction and was never asked for, so owns was upgraded to has_pet."""
    from src.api.main import _possession_upgrade_type
    resolved, decline = _possession_upgrade_type("Animal", "Concept")
    assert decline is True, "a contradicted stamp still drove the upgrade"
    assert resolved is None


def test_agreement_and_single_sided_evidence_keep_todays_behaviour():
    from src.api.main import _possession_upgrade_type
    assert _possession_upgrade_type("Animal", "Animal") == ("Animal", False)
    assert _possession_upgrade_type("Animal", None) == ("Animal", False)     # ladder silent
    assert _possession_upgrade_type(None, "Concept") == ("Concept", False)   # stamp missing
    assert _possession_upgrade_type("unknown", "Concept") == ("Concept", False)
    assert _possession_upgrade_type(None, None) == (None, False)             # no material


# ── FIX B — presupposed content is not asserted content ────────────────────────────────────────
# SEP "Presupposition" (Beaver, Geurts & Denlinger 2021, §2 "The Frege-Strawson tradition";
# Strawson 1950, "On Referring", Mind 59(235):320-344): presupposed information is "taken for
# granted, rather than being part of the main propositional content of a speech act", and
# possessives are listed triggers.

def test_the_deriver_marks_the_possessive_presupposition():
    """The signal has always been computed correctly — it was dropped at the emitter."""
    facts = L.derive_sentence_facts("My car's mileage is 40000.", None, None)
    owns = [f for f in facts if f.rel_type == "owns"]
    assert owns and owns[0].presupposed is True, [(f.rel_type, f.presupposed) for f in facts]


def test_the_transport_now_carries_presupposed():
    """It reached NOTHING outside linguistics.py: EdgeInput had no field, so the flag died at the
    HTTP hop and backgrounded content committed identically to asserted content. Same "signal
    computed, then dropped at the emitter" class as commit 7ddfb169."""
    from src.api.models import EdgeInput
    e = EdgeInput(subject="user", rel_type="owns", object="favourite colour",
                  fact_provenance="user_stated", presupposed=True)
    assert e.presupposed is True
    assert EdgeInput(subject="a", rel_type="owns", object="b").presupposed is False


def test_presupposed_tiers_to_durable_B_not_asserted_A(monkeypatch):
    """Class B, NOT C: the speaker IS committed to a presupposition, so this is a tier
    correction, not a downgrade toward forgetting. B is durable (only C is reaped) and is
    query-visible from staged_facts. Nothing is lost; it stops claiming asserted-truth status.

    NOTE the fixture: outside request scope _rel_meta misses and defined_class silently defaults
    to "C", which routes BOTH arms to ("B", 0.8) and proves nothing. Feed a defined-A rel so the
    user_stated path actually reaches ("A", 1.0).
    """
    import src.api.main as M
    monkeypatch.setitem(M._REL_TYPE_CACHE, "owns", {"fact_class": "A"})

    monkeypatch.setenv("PRESUPPOSED_TIER_B", "false")
    assert M.assign_class_and_confidence(
        {}, fact_provenance="user_stated", rel_type="owns", presupposed=True) == ("A", 1.0)

    monkeypatch.setenv("PRESUPPOSED_TIER_B", "true")
    assert M.assign_class_and_confidence(
        {}, fact_provenance="user_stated", rel_type="owns", presupposed=True) == ("B", 0.8)
    # asserted content is untouched
    assert M.assign_class_and_confidence(
        {}, fact_provenance="user_stated", rel_type="owns", presupposed=False) == ("A", 1.0)
    # a CORRECTION is user authority and still overrides everything
    assert M.assign_class_and_confidence(
        {}, fact_provenance="user_stated", rel_type="owns", presupposed=True,
        is_correction=True) == ("A", 1.0)
