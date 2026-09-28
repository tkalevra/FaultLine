"""PLACEMEM — label–description anaphora + named-instance L4 placement (THE HARD LINE).

THE DEFECT these pin (measured live on the pre-prod TEST seat, 2026-07-31):
  1. "Calista Tea House: This elegant tea house serves a wide variety of teas." bound the clause's
     facts to the anaphoric COMMON NOUN — the TYPE NODE ("tea house") — not to the named instance.
     The engine then ladders that type node in L4 (WordNet restaurant→eating house|eatery), so recall
     answered "Eatery serves great nasi goreng": THE PLACE INSTEAD OF THE MEMORY.
  2. "Miss Bee Providore: This restaurant serves …" captured five relational edges on the name and
     ZERO ``instance_of`` — the named instance had no PLACE in L4, so no description-based query
     ("that restaurant that serves …") had a ladder to descend to it.

Grounding: OntoNotes English coreference guidelines §2 (IDENT links proper-nominal ↔ definite-nominal
mentions of one referent), §2.1 (all demonstratives link to their referents), §2.5/2.6 (in a copular
structure only the leftmost element — the referent — is linked; the complement is the ATTRIBUTE, i.e.
the type); Prince (1981) given/new (indefinite = brand-new entity, definite = evoked).

PRECISION IS THE POINT: a WRONG bind files one entity's facts onto another and corrupts user truth.
The decline tests below are as load-bearing as the resolve tests.
"""
import datetime
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics as L  # noqa: E402

REF = datetime.datetime(2023, 5, 22)


def _facts(sentence):
    return [(f.subject, f.rel_type, f.object)
            for f in (L.derive_sentence_facts(sentence, REF) or [])]


# ── RESOLVE: the named instance owns the clause, and it gets a PLACE in L4 ───────────────────────

@pytest.mark.parametrize("sentence,name,type_noun,rel,obj", [
    ("Miss Bee Providore: This restaurant serves a mix of western cuisine.",
     "miss bee providore", "restaurant", "serve", "mix of western cuisine"),
    ("Calista Tea House: This elegant tea house features a cozy atmosphere "
     "and serves a wide variety of teas.",
     "calista tea house", "tea house", "serve", "wide variety of teas"),
    ("Rumah Mode: The factory outlet is famous for its architecture.",
     "rumah mode", "factory outlet", "has_state", "famous"),
])
def test_definite_np_anaphor_binds_to_the_named_instance(sentence, name, type_noun, rel, obj):
    facts = _facts(sentence)
    assert (name, rel, obj) in facts, facts
    # the MEMORY is filed AT a PLACE — the name is instance_of its type node
    assert (name, "instance_of", type_noun) in facts, facts
    # and NOTHING is filed on the type node itself (that is the HARD LINE inversion)
    assert not [f for f in facts if f[0] == type_noun and f[1] != "instance_of"], facts


def test_bare_demonstrative_never_becomes_an_entity():
    """"Kampung Daun: This is a popular tea house that features X" — "this" was minted as an entity."""
    facts = _facts("Kampung Daun: This is a popular and picturesque tea house "
                   "that features traditional Sundanese architecture.")
    assert not [f for f in facts if f[0] in ("this", "that")], facts
    assert ("kampung daun", "instance_of", "tea house") in facts, facts
    # the copular attribute's relative clause predicates of the REFERENT, not of the type node
    assert ("kampung daun", "feature", "traditional sundanese architecture") in facts, facts


def test_premodified_type_twin_is_not_minted_as_a_second_place():
    """One L4 node per type — never one per adjective ("popular tea house"/"elegant tea house")."""
    facts = _facts("Kampung Daun: This is a popular and picturesque tea house.")
    types = {o for s, r, o in facts if r == "instance_of" and s == "kampung daun"}
    assert types == {"tea house"}, facts


# ── DECLINE: leaving the anaphor unresolved beats binding it to the wrong referent ───────────────

@pytest.mark.parametrize("sentence,reason", [
    ("Miss Bee Providore: A restaurant serves a great meal.", "indefinite introduces a NEW entity"),
    ("Miss Bee Providore: My restaurant serves a great meal.", "possessive anchors to the possessor"),
    ("Miss Bee Providore: Restaurants serve nasi goreng.", "bare NP is not a definite anaphor"),
    ("Miss Bee Providore: I love the nasi goreng.", "1st-person subject is not an anaphor"),
    ("Miss Bee Providore: Chef Anton says this restaurant is great.", "competing PROPN = ambiguous"),
    ("This restaurant serves a great meal.", "no label in the window → nothing to bind to"),
    ("The place: This restaurant serves a great meal.", "label must be a PROPER nominal"),
])
def test_declines_to_resolve(sentence, reason):
    assert L.analyze_label_anaphora(L._parse(sentence)) == [], reason


# ── G4: a PP-EMBEDDED label is a post-modifier, NOT the antecedent ──────────────────────────────
# This is the highest-value decline gate. Measured over the whole LongMemEval corpus (59,139
# colon-bearing lines), the "<Title> by <Author>:" shape is ~half of every hit whose label the NER
# calls PERSON. Binding it files the BOOK's facts onto its AUTHOR — cross-referent corruption.
# These run on a PLAIN parse: the gate is purely structural (dep + ancestors), so it behaves
# identically whether or not the Doc carries entity types. If it ever goes inert, these go red.

@pytest.mark.parametrize("sentence,wrong_referent", [
    ("Those Who Save Us by Jenna Blum: This novel explores a complex relationship.", "jenna blum"),
    ("The Kite Runner by Khaled Hosseini: This novel tells the story of a boy.", "khaled hosseini"),
    ("The Printmaking Council of New Jersey: This website offers tutorials.", "new jersey"),
])
def test_declines_a_pp_embedded_label(sentence, wrong_referent):
    binds = L.analyze_label_anaphora(L._parse(sentence))
    assert binds == [], f"would have filed facts onto {wrong_referent!r}: {binds}"
    # and end-to-end: nothing may be filed on the post-modifier
    assert not [f for f in _facts(sentence) if f[0] == wrong_referent], _facts(sentence)


def test_pp_gate_is_not_inert_the_bare_twin_still_resolves():
    """Guards the gate against being satisfied by declining EVERYTHING.

    Same type noun, same anaphor — only the label's PP embedding differs.
    """
    assert L.analyze_label_anaphora(
        L._parse("Jenna Blum: This novel explores a complex relationship.")) != []


# ── G5: the anaphor must head the MAIN clause ───────────────────────────────────────────────────

@pytest.mark.parametrize("sentence", [
    "Personalization Mall: As the name suggests, they specialize in gifts.",
    "Snorkeling or Scuba Diving: If the beach has a coral reef you will enjoy it.",
])
def test_declines_a_subordinate_clause_subject(sentence):
    assert L.analyze_label_anaphora(L._parse(sentence)) == []


def test_markdown_emphasis_is_not_a_label():
    """"**Name**:" tokenizes to bare "*" runs the tagger calls PROPN — markup is not a name."""
    binds = L.analyze_label_anaphora(L._parse("**Vortex Solo**: This monocular is compact."))
    assert all(any(ch.isalnum() for ch in b.antecedent) and "*" not in b.antecedent
               for b in binds), binds


def test_a_person_named_venue_still_resolves():
    """DOCUMENTED RESIDUAL — this is a deliberate accept, not an oversight.

    There is no available signal separating "Sarah Chen: This restaurant serves noodles." from
    "Kartika Sari: This bakery is famous for its brownies." — the entity typer calls BOTH PERSON,
    and the second is a real bakery. Measured: after the structural gates, 9 of 27 corpus hits carry
    a PERSON-typed label and ALL NINE are venues/products the NER mislabels; ZERO are actual people.
    A person/thing veto would have scored 0 true positives and 9 false negatives on this corpus, so
    it was removed. If this test ever starts failing, a type gate was re-added — re-measure first.
    """
    assert [(b.antecedent, b.type_noun)
            for b in L.analyze_label_anaphora(
                L._parse("Kartika Sari: This bakery is famous for its brownies."))] == \
        [("kartika sari", "bakery")]


# ── FLAG PARITY: OFF is byte-for-byte today's behaviour ─────────────────────────────────────────

@pytest.mark.parametrize("sentence,legacy_fact,name", [
    ("Calista Tea House: This elegant tea house features a cozy atmosphere "
     "and serves a wide variety of teas.",
     ("elegant tea house", "serve", "wide variety of teas"), "calista tea house"),
    ("Kampung Daun: This is a popular and picturesque tea house.",
     ("this", "instance_of", "popular tea house"), "kampung daun"),
])
def test_flag_off_is_byte_for_byte_legacy(monkeypatch, sentence, legacy_fact, name):
    """"OFF is byte-for-byte legacy" — for THESE flags, against the baseline THEY were measured on.

    ⚠️ WHY A THIRD FLAG IS DISABLED HERE (added 2026-08-12). `SPINE_NONREFERENTIAL_SUBJECT_GATE`
    landed at the SAME ``_emit`` chokepoint these two flags route through, and it refuses a
    non-referential pronoun subject. One of the legacy facts pinned below is
    ``('this','instance_of','popular tea house')`` — a PRONOUN AS SUBJECT, i.e. exactly the junk the
    new gate exists to refuse. So with the gate at its shipped default this test went RED, and it
    went red for a GOOD reason: the thing it pins is a defect, and the defect is now fixed.

    That does NOT make the parity contract worthless — it makes it RELATIVE. "Legacy" means "what
    this flag's OFF path produced against the engine as it stood when the flag shipped", so the
    baseline must hold every LATER chokepoint guard off too. Disabling the gate here restores that
    comparison honestly; it does not weaken the assertion, because the gate has its own dedicated
    suite (``tests/test_spine_nonreferential_subject.py``) pinning its behaviour.

    🔴 THE GENERAL TRAP, worth reading before adding any new chokepoint guard: a guard at a shared
    chokepoint silently invalidates EVERY other flag's "OFF = byte-for-byte legacy" test that routes
    through it. Those tests are the least likely to be re-run — they "only test the rollback lever" —
    so grep the tree for ``byte_for_byte`` / ``flag_off`` / ``is_legacy`` and run them BOTH ways
    before shipping the guard.
    """
    monkeypatch.setattr(L, "SPINE_DEFINITE_ANAPHORA", False)
    monkeypatch.setattr(L, "SPINE_INSTANCE_PLACEMENT", False)
    monkeypatch.setattr(L, "SPINE_NONREFERENTIAL_SUBJECT_GATE", False)
    legacy = _facts(sentence)
    assert legacy_fact in legacy, legacy
    assert not [f for f in legacy if f[0] == name], legacy


def test_the_nonreferential_gate_refuses_the_pronoun_subject_this_test_used_to_pin(monkeypatch):
    """The other half of the correction above: with the gate at its SHIPPED default, the junk
    "legacy fact" must NOT come back. Pins the fix so the parity test above can never quietly
    re-acquire it by someone flipping the gate default rather than fixing the construction."""
    monkeypatch.setattr(L, "SPINE_DEFINITE_ANAPHORA", False)
    monkeypatch.setattr(L, "SPINE_INSTANCE_PLACEMENT", False)
    facts = _facts("Kampung Daun: This is a popular and picturesque tea house.")
    assert ("this", "instance_of", "popular tea house") not in facts, facts
    assert not [f for f in facts if f[0] == "this"], facts
