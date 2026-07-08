"""Regression gate for the SPINE deriver's genitive-name robustness + third-party alias capture.

Two deterministic capture gaps in ``derive_sentence_facts`` (src/extraction/linguistics.py),
both surfaced on the live spine (SENTENCE_PIPELINE ON) and fixed here. All inputs are
SUBJECT-AGNOSTIC (kin roles + names drawn from the DB cue classes, never a code literal) and use
NON-personal reference data.

── FAILURE 1 — apostrophe-stripped genitive minted a PHANTOM entity ───────────────────────────
The LLM atomizer / normalization sometimes DROPS the genitive apostrophe ("my sister's name" →
"my sisters name"). spaCy then re-parses the role noun as a ``compound`` dependent of "name" (not
``poss``), so ``_chain_genitive_name`` never matched → the role was not collapsed and
``_chain_possessive`` mis-minted (sisters, <kin>, user) off the surface "sisters" — a PHANTOM
entity — while a copula seam read "sisters name" as a type. FIX: the genitive-name chain now also
recognizes the compound frame (cue-gated to kinship/relational roles), the possessive chain steps
aside for it, and the proper NAME is bound as the kin with the role-noun collapsed.

── FAILURE 2 — third-party "prefers to be called / goes by / known as X" nickname DROPPED ──────
The naming capture handled a first-person self-name and "<role>'s name is Y", but a THIRD-PARTY
nickname stated with an alias predicate ("she goes by Dee") was dropped, and the intransitive/
copula-state chains mis-minted a junk (she, has_state, go) twin. FIX: ``_chain_alias_predicate``
captures (person, also_known_as, <Name>), resolving a 3rd-person pronoun subject to the nearest
preceding named person via ``_person_coref`` (recency/salience anaphora), routed to the SEEDED
``also_known_as`` rel (THE HARD LINE: a name is FILED via the alias registry, never classified into
L4). The phrasal alias vocabulary (go→by, know→as, refer→as) lives in the ``alias_predicate`` DB
cue class (migration 146), NOT in code.
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


# ── FAILURE 1: genitive-name, apostrophe present AND stripped, no phantom ───────────────────

@pytest.mark.parametrize("text,name,kin", [
    # apostrophe PRESENT (baseline)
    ("My sister's name is Dana.", "dana", "sibling_of"),
    ("My brother's name is Sam.", "sam", "sibling_of"),
    ("My mother's name is Priya.", "priya", "parent_of"),
    # apostrophe STRIPPED by the atomizer (the Failure-1 repro shape) — plural surface, singular lemma
    ("My sisters name is Dana.", "dana", "sibling_of"),
    ("My brothers name is Sam.", "sam", "sibling_of"),
    ("My mothers name is Priya.", "priya", "parent_of"),
])
def test_genitive_name_binds_proper_name_no_phantom(text, name, kin):
    facts = m.derive_sentence_facts(text, _REF)
    triples = _triples(facts)
    # the PROPER NAME is bound as the kin of the user
    assert (name, kin, "user") in triples, triples
    # NO phantom "sisters"/"brothers"/"sisters name" entity: nothing carries the role-noun surface
    for subj, rel, obj in triples:
        assert "sisters" not in subj and "sisters" not in obj, triples
        assert "brothers" not in subj and "brothers" not in obj, triples
        assert obj != "name" and not obj.endswith(" name"), triples
        # the kin rel must point at the NAME, never at the possessive role surface
        if rel == kin:
            assert subj == name, triples


def test_genitive_name_third_party_possessor():
    # "John's mother's name is Susan" → Susan is the parent of John (not the user)
    facts = m.derive_sentence_facts("John's mother's name is Susan.", _REF)
    assert ("susan", "parent_of", "john") in _triples(facts), _triples(facts)


def test_non_kin_compound_is_not_a_collapsed_genitive():
    # "my user name is Bob" is an ordinary noun-noun compound, NOT a kin genitive — the cue gate
    # must keep the compound-frame recovery from firing (no (user, sibling_of, ...) / phantom).
    facts = m.derive_sentence_facts("My user name is Bob.", _REF)
    for _subj, rel, _obj in _triples(facts):
        assert rel not in ("sibling_of", "parent_of", "spouse", "child_of"), _triples(facts)


# ── FAILURE 2: third-party alias / nickname capture with coref ──────────────────────────────

@pytest.mark.parametrize("text,name,alias,kin", [
    # naming-verb complement ("prefers to be called X") — passive xcomp, subject on the matrix verb
    ("My sister's name is Dana, she prefers to be called Dee.", "dana", "dee", "sibling_of"),
    # alias-PP idiom "goes by X" (go→by)
    ("My sister's name is Dana, she goes by Dee.", "dana", "dee", "sibling_of"),
    # alias-PP idiom "known as X" (know→as)
    ("My brother's name is Sam, he is known as Sammy.", "sam", "sammy", "sibling_of"),
])
def test_third_party_nickname_bound_to_corefd_person(text, name, alias, kin):
    facts = m.derive_sentence_facts(text, _REF)
    triples = _triples(facts)
    # kin binding still lands, role collapsed onto the proper name
    assert (name, kin, "user") in triples, triples
    # the nickname is filed as an alias ON the coref'd named person (THE HARD LINE)
    assert (name, "also_known_as", alias) in triples, triples
    # no junk has_state twin off the alias verb
    assert not any(rel == m._STATE_REL for _s, rel, _o in triples), triples


@pytest.mark.parametrize("text,tp,name,alias", [
    ("She goes by Dee.", ["dana"], "dana", "dee"),
    ("She prefers to be called Dee.", ["dana"], "dana", "dee"),
    ("He is known as Sammy.", ["sam"], "sam", "sammy"),
])
def test_alias_pronoun_resolves_via_turn_persons(text, tp, name, alias):
    # the atomizer splits a turn into per-atom sentences; the pronoun's antecedent arrives via the
    # turn's unambiguous person (PERSON NER threaded in as turn_persons) — _person_coref binds it.
    facts = m.derive_sentence_facts(text, _REF, turn_persons=tp)
    assert (name, "also_known_as", alias) in _triples(facts), _triples(facts)


def test_alias_named_subject_direct():
    # a NAMED (non-pronoun) subject binds directly, no coref needed
    facts = m.derive_sentence_facts("Dana goes by Dee.", _REF)
    assert ("dana", "also_known_as", "dee") in _triples(facts), _triples(facts)


def test_unresolved_alias_subject_suppresses_junk_but_emits_nothing():
    # a standalone alias construction with NO resolvable antecedent must NOT leak a has_state twin —
    # it owns the verb (suppress) even though it cannot emit the alias.
    facts = m.derive_sentence_facts("She goes by Dee.", _REF)  # no turn_persons, no antecedent
    triples = _triples(facts)
    assert not any(rel == m._STATE_REL for _s, rel, _o in triples), triples
    assert not any(rel == "also_known_as" for _s, rel, _o in triples), triples


@pytest.mark.parametrize("text", [
    "She goes to work.",     # go + "to" PP (motion) — not an alias idiom
    "She went to Paris.",    # go + "to" PP (motion)
])
def test_same_verb_non_alias_uses_untouched(text):
    facts = m.derive_sentence_facts(text, _REF)
    assert not any(rel == "also_known_as" for _s, rel, _o in _triples(facts)), _triples(facts)


def test_negated_alias_is_not_captured():
    facts = m.derive_sentence_facts("Dana is not known as Dee.", _REF)
    assert not any(rel == "also_known_as" for _s, rel, _o in _triples(facts)), _triples(facts)
