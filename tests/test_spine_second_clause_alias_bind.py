"""The production two-clause naming turn: a second-clause alias binds to the FIRST
clause's already-resolved person, never mints the alias surface as its own entity.

THE DEFECT (measured live, pre-prod scratch seat, and on the production seat the
replica was loaded from): the turn "My wifes name is Nora, she prefers to be called
Mars" committed (user spouse Nora) and (Nora also_known_as wife) correctly, but the
alias 'mars' landed on a BRAND-NEW standalone entity. The mechanism, reproduced live at
HEAD: the LLM atomizer splits the turn into two atoms; when the deriver fails to capture
the SECOND clause (an unresolvable pronoun subject, or a "her name is Mars" possessive
copula the naming chains did not own), the sentence yields no alias edge and the
GLiNER2-relation fallback proposes (mars, related_to, nora) — 'mars' grounds in SUBJECT
position, mints a phantom entity, and recall "who is Mars" resolves to the phantom and
answers NOTHING.

The fix is at the deriver (capture correct → the fallback is starved and the alias weld
at ingest files 'mars' on Nora's entity — validated live). These tests pin the bar at
the deriver level with the HARVEST'S OWN threading shape:

  * atom 1 establishes the person ("My wifes name is Nora" → (nora, spouse, user));
  * atom 2 is the second clause in every surface the atomizer produces, threaded with
    the whole-turn PERSON pool (BOTH NER readings — the nickname typed PERSON, which is
    the ambiguous-pool production condition, and untyped) and the prior-atom NP
    accumulator built from atom 1's edge objects (the harvest's Rule-1 accumulator).

GROUND TRUTH independence (the lesson from the _prose pin): the expected subject is
atom 1's OWN captured person surface — the entity the first clause resolved — read from
atom 1's output, which the second-clause fix cannot influence. The alias edge of atom 2
must carry THAT surface; and 'mars' must appear in NO subject slot (subject-position
grounding is exactly how the phantom minted).

Subject-agnostic by construction: kin roles come from the seeded cue classes (wife,
mother), every decision keyed on grammar (Person/Poss/PronType morphology, dep_
relations, PROPN) and the whole-turn machinery — no name or role literals.
"""
import datetime

import pytest

from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine deriver no-ops",
)

_REF = datetime.date(2026, 8, 20)
_TURN = "My wifes name is Nora, she prefers to be called Mars"
_ATOM1 = "My wifes name is Nora"

# second-clause surfaces: the alias-predicate shapes and the possessive-name shapes
_SECOND_CLAUSES = [
    "she prefers to be called Mars",
    "She prefers to be called Mars",
    "she likes to be called Mars",
    "she wants to be called Mars",
    "she goes by Mars",
    "she is known as Mars",
    "her name is Mars",
    "Her name is Mars",
    "her preferred name is Mars",
    "My wife likes to be called Mars",
]

# threading states: (label, prior_nps after atom 1, whole-turn PERSON pool)
_THREADINGS = [
    # spaCy's own NER reading (Mars -> LOC): unambiguous pool
    ("ner-loc", ["user", "wife"], ["nora"]),
    # the production condition: the nickname NER-typed PERSON -> AMBIGUOUS pool
    ("ner-person", ["user", "wife"], ["nora", "mars"]),
    # atomizer reordered so the naming atom emitted the name object FIRST
    ("reordered", ["nora", "user"], ["nora", "mars"]),
    # no prior-atom accumulator at all (atom split lost atom 1's edges)
    ("no-prior", [], ["nora", "mars"]),
]


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


def _atom1_person():
    """The surface atom 1 bound the kin relation to — the FIRST clause's resolved person.

    Read from atom 1's own capture (independent of the second-clause fix).
    """
    facts = m.derive_sentence_facts(_ATOM1, _REF)
    for s, rel, o in _triples(facts):
        if o == "user" and rel in ("spouse", "parent_of", "child_of", "sibling_of"):
            return s
    raise AssertionError(f"atom 1 did not bind the kin relation: {_triples(facts)}")


def _turn_role_names():
    return m.build_turn_role_name_map(_TURN) or {}


@pytest.mark.parametrize("clause", _SECOND_CLAUSES)
@pytest.mark.parametrize("threading", _THREADINGS, ids=[t[0] for t in _THREADINGS])
def test_second_clause_alias_binds_to_first_clause_person(clause, threading):
    _, prior, pool = threading
    person = _atom1_person()
    facts = m.derive_sentence_facts(
        clause, _REF, prior_nps=list(prior),
        turn_persons=list(pool), turn_role_names=_turn_role_names())
    triples = _triples(facts)

    # THE BIND: the alias edge files 'mars' on the FIRST clause's person surface
    # (the surface ingest grounds to Nora's entity — validated live).
    assert (person, "also_known_as", "mars") in triples, triples

    # NO NEW ENTITY: 'mars' never grounds — it appears in NO subject slot (subject-
    # position grounding is the phantom-mint mechanism) and in no compound object
    # surface; its only legal place is the alias VALUE slot.
    for s, rel, o in triples:
        assert s != "mars", triples
        assert o == "mars" or "mars" not in (o or ""), triples


@pytest.mark.parametrize("pool", [["nora"], ["nora", "mars"]], ids=["unambiguous", "ambiguous"])
def test_exact_turn_single_sentence(pool):
    """The EXACT production turn as one atom (the atomizer sometimes keeps it whole)."""
    facts = m.derive_sentence_facts(
        _TURN, _REF, turn_persons=list(pool), turn_role_names=_turn_role_names())
    triples = _triples(facts)
    person = _atom1_person()
    assert (person, "also_known_as", "mars") in triples, triples
    assert (person, "spouse", "user") in triples, triples
    for s, _rel, _o in triples:
        assert s != "mars", triples


def test_named_possessor_direct():
    """A PROPN possessor is used directly: "Kate's name is Katherine" -> (kate, aka, katherine)."""
    facts = m.derive_sentence_facts("Kate's name is Katherine.", _REF)
    assert ("kate", "also_known_as", "katherine") in _triples(facts), _triples(facts)


def test_non_person_possessor_is_not_a_person_bind():
    """A common-noun possessor outside the kin/relational cue classes never becomes a
    person bind: the pool rescue is ANAPHORA resolution and must not fire for "my dog"."""
    facts = m.derive_sentence_facts(
        "My dog likes to be called Rex", _REF,
        prior_nps=["user"], turn_persons=["nora"], turn_role_names=_turn_role_names())
    triples = _triples(facts)
    for s, rel, o in triples:
        if o == "rex":
            assert s not in ("nora", "user"), triples  # never welded onto a person
            assert rel != "also_known_as" or s in ("dog", "rex"), triples


def test_first_person_self_naming_still_defers():
    """The self case stays owned by the preference/naming seam — the alias chain must
    not double-capture "I prefer to be called Jon"."""
    facts = m.derive_sentence_facts(
        "I prefer to be called Jon", _REF,
        prior_nps=[], turn_persons=["jon"], turn_role_names={})
    for s, rel, o in _triples(facts):
        assert not (o == "jon" and rel == "also_known_as"), _triples(facts)


def test_negated_alias_is_absence():
    facts = m.derive_sentence_facts(
        "she does not go by Mars", _REF,
        prior_nps=["user", "wife"], turn_persons=["nora"], turn_role_names=_turn_role_names())
    for s, rel, o in _triples(facts):
        assert not (o == "mars" and rel == "also_known_as"), _triples(facts)


# ── SUBJECT-AGNOSTICISM PROOF — the fix is keyed on GRAMMAR, not on the defect's lexicon ──
#
# The tests above pin the production turn itself. But a fix keyed on wife/Nora/Mars
# (a name list, a role literal, a gendered-pronoun lexicon) would pass every one of
# them. This section proves grammar-keying: the SAME grammatical constructions driven
# through DIFFERENT kin roles (from the seeded cue classes), different names, and
# BOTH genders' pronouns. Every case must bind exactly as the defect turn does. If
# any case here goes red while the defect-turn tests stay green, the fix has leaked
# a lexical key. (Roles/names drawn per the sibling suite's parametrization:
# test_spine_genitive_name_and_alias.py uses sister/Dana, brother/Sam, mother/Priya.)

_AGNOSTIC_CASES = [
    # (kin role, proper name, nickname, subject pronoun, possessive pronoun)
    ("mother", "Priya", "Pri", "she", "her"),
    ("brother", "Sam", "Sammy", "he", "his"),
    ("sister", "Dana", "Dee", "she", "her"),
    ("father", "David", "Dave", "he", "his"),
    # G1 (round-2 critic refinement): singular-they row — a RELATIONAL (non-kin) cue-class
    # role with they/their, the exact pronoun+role shape of the live G2 phantom turn
    # ('My friends name is Alex, they prefer to be called Al'). The section previously
    # parametrized she/her and he/his only, so removing 'their' from _person_coref's
    # pronoun surface set left every row green while the deriver provably NOBOUND the
    # possessive shape.
    ("friend", "Alex", "Al", "they", "their"),
]


@pytest.mark.parametrize("role,name,nick,subj_pron,poss_pron", _AGNOSTIC_CASES)
def test_alias_predicate_binds_across_roles_names_and_genders(role, name, nick, subj_pron, poss_pron):
    # "<pron> prefers to be called <Nick>" binds to the first clause's person for
    # every role/name/gender — including the he/his rows (no gendered-pronoun lexicon).
    turn = f"My {role}s name is {name}, {subj_pron} prefers to be called {nick}"
    trn = m.build_turn_role_name_map(turn) or {}
    atom1 = f"My {role}s name is {name}"
    facts1 = m.derive_sentence_facts(atom1, _REF, turn_role_names=trn)
    t1 = _triples(facts1)
    person = next((s for s, r, o in t1 if o == "user" and r != "also_known_as"), None)
    assert person, t1  # atom 1 bound the kin to the named person
    for pool in ([], [name.lower()], [name.lower(), nick.lower()]):  # empty+prior-role (G1), unambiguous, ambiguous
        facts2 = m.derive_sentence_facts(
            f"{subj_pron} prefers to be called {nick}", _REF,
            prior_nps=["user", role], turn_persons=list(pool), turn_role_names=trn)
        t2 = _triples(facts2)
        assert (person, "also_known_as", nick.lower()) in t2, (pool, t2)
        for s, _r, _o in t2:
            assert s != nick.lower(), t2  # the alias surface never grounds


@pytest.mark.parametrize("role,name,nick,subj_pron,poss_pron", _AGNOSTIC_CASES)
def test_possessive_name_copula_binds_across_roles_and_genders(role, name, nick, subj_pron, poss_pron):
    # "<poss> name is <Nick>" (the possessive naming copula) binds for he/his as for
    # she/her — the possessor resolution is morphology-keyed, not lexicon-keyed.
    turn = f"My {role}s name is {name}, {poss_pron} name is {nick}"
    trn = m.build_turn_role_name_map(turn) or {}
    atom1 = f"My {role}s name is {name}"
    facts1 = m.derive_sentence_facts(atom1, _REF, turn_role_names=trn)
    person = next((s for s, r, o in _triples(facts1) if o == "user" and r != "also_known_as"), None)
    assert person, _triples(facts1)
    for pool in ([], [name.lower()], [name.lower(), nick.lower()]):
        facts2 = m.derive_sentence_facts(
            f"{poss_pron} name is {nick}", _REF,
            prior_nps=["user", role], turn_persons=list(pool), turn_role_names=trn)
        t2 = _triples(facts2)
        assert (person, "also_known_as", nick.lower()) in t2, (pool, t2)
        for s, _r, _o in t2:
            assert s != nick.lower(), t2


@pytest.mark.parametrize("role,name,nick,subj_pron,poss_pron", _AGNOSTIC_CASES)
def test_whole_turn_binds_across_roles_and_genders(role, name, nick, subj_pron, poss_pron):
    # The full two-clause turn (alias-predicate shape) as ONE atom, both pool readings.
    turn = f"My {role}s name is {name}, {subj_pron} prefers to be called {nick}"
    trn = m.build_turn_role_name_map(turn) or {}
    for pool in ([], [name.lower()], [name.lower(), nick.lower()]):
        facts = m.derive_sentence_facts(turn, _REF, turn_persons=list(pool), turn_role_names=trn)
        triples = _triples(facts)
        assert (name.lower(), "also_known_as", nick.lower()) in triples, (pool, triples)
        for s, _r, _o in triples:
            assert s != nick.lower(), triples


def test_singular_they_possessive_empty_pool_pins_the_pronoun_surface():
    # G1 (round-2 critic mutation): 'their' in the pronoun surface set is LOAD-BEARING but
    # unpinned — removing it keeps every other test green yet NOBINDs exactly this shape
    # (empty pool, prior-NP threading: 'My friends name is Alex, they ...' split so the
    # possessive arrives with only prior_nps to resolve against). Subject-agnostic: the
    # pin is about the MORPHOLOGY (3rd-person Prs+Poss), and 'friend' here is just the
    # nearest non-speaker prior NP the guarded fallback legitimately resolves to.
    facts = m.derive_sentence_facts(
        "their name is Al", _REF,
        prior_nps=["user", "friend"], turn_persons=[], turn_role_names={})
    t = _triples(facts)
    assert ("friend", "also_known_as", "al") in t, (
        "the 3rd-person possessive pronoun no longer resolves: the empty-pool+prior-role "
        f"shape NOBINDs (got {t})")

# ── ROUND-2 G2/G3: possessive-construction topics + cross-atom role welds ──────────────
# Live defects (pre-prod, instrumented build 2026-08-21): the atomizer splits
# "My friends name is Alex, they prefer to be called Al" into atoms whose FIRST
# sentence established discourse_topic surface='friends name' — a possessive
# ROLE-CONSTRUCTION NP, not a referent — so atom 2's pronoun alias clause rebound to
# the construction surface and minted a phantom person that stole the preferred 'al'.
# And "My mothers name is Katherine, her name is Kate" atomizes into TWO genitive
# namings of the same role, each minting its own person (recall double-answers).


def _topic_of(sent):
    return m.discourse_topic_from_doc(sent)


def test_possessive_construction_subject_never_becomes_the_discourse_topic():
    # The construction "<poss> <role> name" describes an attribute-of-a-relation, never
    # a referent. The topic must re-anchor on the naming copula's PROPER complement.
    for sent, want in [
        ("My friends name is Alex.", "alex"),
        ("My mothers name is Katherine.", "katherine"),
        ("My wifes name is Nora.", "nora"),
        ("My brothers name is Peter.", "peter"),
    ]:
        t = _topic_of(sent)
        assert t is not None and t.surface == want, (
            f"{sent!r}: topic surface should re-anchor on the proper name {want!r}, "
            f"got {getattr(t, 'surface', None)!r}")


def test_construction_guard_leaves_plain_subject_topics_alone():
    # Anti-overfire: a plain possessed subject whose head is NOT a kin/relational
    # cue-class role keeps today's topic behavior.
    t = _topic_of("My server is slow.")
    assert t is not None and t.surface == "server"


def test_second_atom_alias_binds_the_named_person_not_the_construction_surface():
    # THE G2 LIVE SHAPE (guardrail-rejected atom replaced by its source span): atom 2's
    # pronoun alias clause must bind the turn's named person, never the construction NP.
    turn = "My friends name is Alex, they prefer to be called Al"
    topic = _topic_of("My friends name is Alex.")
    role_names = m.build_turn_role_name_map(turn)
    f1 = m.derive_sentence_facts("My friends name is Alex.", None, prior_nps=[],
                                 turn_persons=["alex"], turn_role_names=role_names)
    f2 = m.derive_sentence_facts("they prefer to be called Al", None,
                                 prior_nps=["user"], discourse_topic=topic,
                                 turn_persons=["alex"], turn_role_names=role_names)
    triples = _triples(f1) + _triples(f2)
    assert ("alex", "also_known_as", "al") in triples, triples
    assert not any(s == "friends name" for (s, _r, _o) in triples), triples


def test_original_prod_turn_through_the_topic_lane():
    # The original production turn, driven through the same cross-atom threading the
    # spine uses (topic + role map + person pool): the nickname lands on Nora.
    turn = "My wifes name is Nora, she prefers to be called Mars"
    topic = _topic_of("My wifes name is Nora.")
    assert topic is not None and topic.surface == "nora"
    role_names = m.build_turn_role_name_map(turn)
    f2 = m.derive_sentence_facts("she prefers to be called Mars", None,
                                 prior_nps=["user"], discourse_topic=topic,
                                 turn_persons=["nora"], turn_role_names=role_names)
    assert ("nora", "also_known_as", "mars") in _triples(f2), _triples(f2)


def test_second_genitive_naming_of_the_same_role_welds_onto_the_first_person():
    # THE G3 LIVE SHAPE: the atomizer pronoun-resolves "her name is Kate" into a SECOND
    # genitive naming of the same role. The second name is an ALIAS of the person the
    # first atom bound — never a second entity carrying a second parent_of.
    turn = "My mothers name is Katherine, her name is Kate"
    role_names = m.build_turn_role_name_map(turn)
    assert role_names.get("mother") == "katherine"
    f1 = m.derive_sentence_facts("My mothers name is Katherine.", None, prior_nps=[],
                                 turn_persons=["katherine", "kate"],
                                 turn_role_names=role_names)
    f2 = m.derive_sentence_facts("My mothers name is Kate.", None, prior_nps=["user"],
                                 turn_persons=["katherine", "kate"],
                                 turn_role_names=role_names)
    triples = _triples(f1) + _triples(f2)
    assert ("katherine", "parent_of", "user") in triples, triples
    assert ("katherine", "also_known_as", "kate") in triples, triples
    assert ("kate", "parent_of", "user") not in triples, (
        f"the second genitive naming minted a parallel person: {triples}")
    assert ("kate", "also_known_as", "mother") not in triples, triples


def test_role_weld_never_crosses_possessors():
    # A THIRD-PERSON possessor's role-holder is a different person: no weld, and the
    # speaker's own later naming is not collapsed onto the third person's binding.
    turn = "John's mother's name is Katherine, my mother's name is Kate"
    role_names = m.build_turn_role_name_map(turn)
    assert "mother" not in role_names or role_names["mother"] == "kate", role_names
    f1 = m.derive_sentence_facts("John's mother's name is Katherine.", None, prior_nps=[],
                                 turn_persons=["katherine", "kate"],
                                 turn_role_names=role_names)
    f2 = m.derive_sentence_facts("my mother's name is Kate.", None, prior_nps=[],
                                 turn_persons=["katherine", "kate"],
                                 turn_role_names=role_names)
    triples = _triples(f1) + _triples(f2)
    assert ("katherine", "parent_of", "john") in triples, triples
    assert ("kate", "parent_of", "user") in triples, triples
    assert ("katherine", "also_known_as", "kate") not in triples, (
        f"a third-person possessor's binding welded onto the speaker's person: {triples}")


def test_third_possessor_role_binding_is_not_recorded_for_the_speaker():
    # CAP2DUP possessor scoping: "John's mother is named Katherine" is NOT a binding of
    # the SPEAKER'S mother — it must never collapse a sibling atom's bare "my mother".
    assert m.build_turn_role_name_map("Johns mother is named Katherine, my mother is 62") == {}
    assert m.build_turn_role_name_map("my mother is named Katherine") == {
        "mother": "katherine"}


# ── ROUND-3 ITEM 1: pin the guard's SCOPE CONJUNCTION (the possessor requirement) ─────
#
# The construction-subject guard above is a CONJUNCTION: a kin/relational cue-class role
# dependent AND an actual possessor. Mutating away the possessor requirement leaves every
# other test green while LIVE-CHANGING behavior: a genuine NAME head carrying a
# role-modifier ("Mother Teresa is a saint" — 'Mother' is a PROPN compound of the name
# 'Teresa') would flip topic->None (the copula complement 'saint' is a NOUN, so the
# re-anchor finds no PROPN and fails safe), suppressing a legitimate referent. A
# name-with-role-modifier head must NOT be suppressed: only possessive/role-CONSTRUCTION
# subjects ('friends name' / 'johns mother') are the guard's scope. Grammar-keyed (PROPN
# name head + compound modifier whose lemma is a seeded cue-class role), subject-agnostic
# (any cue-class role word, any name), no lexicons.

_NAME_WITH_ROLE_MODIFIER = [
    # (role modifier, name head, copula predicate)
    ("Mother", "Teresa", "a saint"),
    ("Sister", "Mary", "a nurse"),
    ("Father", "Brown", "a detective"),
    ("Brother", "Jonathan", "a teacher"),
]


@pytest.mark.parametrize("role,name,pred", _NAME_WITH_ROLE_MODIFIER)
def test_name_with_role_modifier_head_is_never_suppressed_by_the_guard(role, name, pred):
    # The scope conjunction is LOAD-BEARING: without the possessor requirement this
    # flips to None (no PROPN copula complement to re-anchor on). The full compound
    # NAME surface is the topic.
    sent = f"{role} {name} is {pred}."
    t = _topic_of(sent)
    assert t is not None and t.surface == f"{role} {name}".lower(), (
        f"{sent!r}: a genuine name-with-role-modifier head must keep its subject topic, "
        f"got {getattr(t, 'surface', None)!r}")


def test_name_with_role_modifier_keeps_topic_under_a_content_verb():
    # The same shape under a CONTENT-VERB root (no copula complement at all): the
    # guard's scope must not reach here either — the dobj is not an attr/oprd.
    t = _topic_of("Mother Teresa visited Calcutta.")
    assert t is not None and t.surface == "mother teresa", (
        f"got {getattr(t, 'surface', None)!r}")


# ── ROUND-3 ITEM 2: the GENITIVE ROLE-HEAD topic lane (the G2 sibling) ────────────────
#
# 'Johns mother is named Katherine' — the atomizer's apostrophe-stripped compound form
# parses as PROPN+role-noun compound ('Johns'/compound -> 'mother'/nsubjpass), i.e. the
# subject HEAD is itself the kin/relational role noun and its genitive is a PROPN
# dependent. The turn-role-name-map half is already fixed (possessor-blocks returns {});
# the TOPIC lane needs the same treatment: discourse_topic_from_doc used to return the
# construction NP 'johns mother' at BOTH arms, so a threaded atom-2 minted junk
# ('johns mother', 'live_in', 'toronto'). The guard now re-anchors on the naming
# copula's PROPN complement — or fails safe — and NEVER adopts the construction surface.

_GENITIVE_ROLE_HEAD_CASES = [
    # (sentence, expected re-anchored surface)
    ("Johns mother is named Katherine.", "katherine"),    # apostrophe-stripped compound
    ("John's mother is named Katherine.", "katherine"),   # explicit PROPN poss genitive
    ("Kates brother is named Sam.", "sam"),               # other role, other name
    ("Alexs wife is called Priya.", "priya"),             # 'called' naming variant
    # RESIDUAL (honest): genitive names the tagger reads as NOUN ("Sams"/"Rachels wife
    # is called Priya") do not trip the PROPN gate and keep today's subject reading —
    # a spaCy vocab quirk, not a guard defect; keyed on the tag, never the name.
]


@pytest.mark.parametrize("sent,want", _GENITIVE_ROLE_HEAD_CASES)
def test_genitive_role_head_subject_never_becomes_the_discourse_topic(sent, want):
    t = _topic_of(sent)
    assert t is not None and t.surface == want, (
        f"{sent!r}: the genitive role-head subject must re-anchor on the naming "
        f"complement {want!r}, got {getattr(t, 'surface', None)!r}")


def test_genitive_role_head_without_propn_complement_fails_safe():
    # No PROPN complement to re-anchor on ('62' is NUM) → NO topic — never the
    # construction surface 'johns mother'.
    assert _topic_of("Johns mother is 62.") is None


def test_first_person_role_head_topic_is_unchanged():
    # ANTI-OVERFIRE: the speaker's own kin-role subject keeps today's role-surface
    # reading — its turn role-name map DOES bind ({'mother': 'katherine'}), so the
    # role surface stays a legitimate consolidation anchor. The possessor 'My' is a
    # PRON, not a PROPN genitive: a different construction.
    t = _topic_of("My mother is named Katherine.")
    assert t is not None and t.surface == "mother", getattr(t, "surface", None)
    t2 = _topic_of("My wife is named Nora.")
    assert t2 is not None and t2.surface == "wife", getattr(t2, "surface", None)


def test_genitive_role_head_topic_threads_the_named_person():
    # THE G2-SIBLING LIVE SHAPE end-to-end at the deriver: atom 2 threaded with the
    # topic binds the NAMED PERSON — the construction surface appears in no slot.
    # (Ground truth independent of the guard: with the defect topic 'johns mother'
    # the same threading provably mints ('johns mother', 'live_in', 'toronto').)
    turn = "Johns mother is named Katherine, she lives in Toronto"
    topic = _topic_of("Johns mother is named Katherine.")
    assert topic is not None and topic.surface == "katherine"
    role_names = m.build_turn_role_name_map(turn)  # third-person possessor -> {}
    assert role_names == {}
    f2 = m.derive_sentence_facts("she lives in Toronto", None, prior_nps=["user"],
                                 discourse_topic=topic, turn_persons=["katherine"],
                                 turn_role_names=role_names)
    t2 = _triples(f2)
    assert any(s == "katherine" and o == "toronto" for (s, _r, o) in t2), t2
    assert not any(s == "johns mother" for (s, _r, _o) in t2), t2
