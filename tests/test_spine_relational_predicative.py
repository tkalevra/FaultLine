"""Regression gate for the NOMINAL RELATIONAL PREDICATIVE frame — "X is a <RELNOUN> of Y".

THE DEFECT (measured on the live spine, GLiNER2-typed Doc, before this lane existed):

    "Kai is a member of the hockey team."  ->  [('kai', 'instance_of', 'member')]
    "The GPS is a part of the car."        ->  [('gps', 'instance_of', 'part')]

TWO failures per clause. (i) The relational noun's INTERNAL ARGUMENT — the group, the whole — is
ANNIHILATED, so the membership/mereology edge is never captured at all; graph-wide across 8 tenants
that showed up as `member_of` = 2 rows and `part_of` = 4 rows, and `max_subjects_per_group = 1` in
every local tenant, which is exactly the predicate `_mint_groupings_from_membership`
(src/api/main.py) needs to clear (`COUNT(DISTINCT subject_id) >= 2`) before a grouping taxonomy can
be minted. (ii) A two-place RELATIONAL noun is filed as a one-place sortal TYPE, minting "member" /
"part" as an L4 PLACE — THE HARD LINE: "member" is not a kind of thing anything IS, it is half of a
relation.

THE GRAMMAR. Löbner, "Definites", *Journal of Semantics* 4(4):279-326, 1985 (doi:10.1093/jos/4.4.279)
classifies nouns as sortal (conceptually one-place) vs relational (conceptually two-place); Glass,
"Quantifying relational nouns in corpora", *English Language and Linguistics* 26(4):833-859, 2022
restates it ("conceptually one-place 'sortal' nouns such as tree ... conceptually two-place
'relational' nouns such as cousin") and names the `of`-genitive as the standard relationality
diagnostic while cautioning that "the availability and interpretation of _of_-phrases actually
depends on many factors above and beyond the head noun" — which is precisely why this lane ships NO
lexical relational-noun classifier and instead reads the frames the tenant's OWN ontology declares in
`rel_types.natural_language` ("X is a member of Y", "X is a part of Y", ...).

The bare-plural admission ("Kai and Rowan are members of the hockey team" — no determiner, so the
det-gated type-complement selector never saw it and the clause emitted ZERO facts) rests on English
having no plural indefinite article: Carlson, "A unified analysis of the English bare plural",
*Linguistics and Philosophy* 1(3):413-457, 1977 (doi:10.1007/BF00353456) defines the bare plural as
"an NP with plural head that lacks a determiner" and names this reading the "indefinite plural …
the semantic plural of the NP's determined by article a(n)".
"""
import datetime
import os

import pytest

from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine deriver no-ops",
)

_REF = datetime.date(2023, 6, 1)

# The frames the SEEDED ontology declares (verified against public.rel_types + every local tenant).
# Injected so the grammar pins run without a live DB; the DB-backed resolver is pinned separately.
_SEEDED_FRAMES = {
    ("member", "of"): "member_of",
    ("part", "of"): "part_of",
    ("child", "of"): "child_of",
    ("parent", "of"): "parent_of",
    ("friend", "of"): "friend_of",
    ("instance", "of"): "instance_of",
    ("subclass", "of"): "subclass_of",
}


@pytest.fixture()
def frames(monkeypatch):
    monkeypatch.setattr(m, "_relational_predicative_frames", lambda: dict(_SEEDED_FRAMES))
    return _SEEDED_FRAMES


def _triples(text):
    return [(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(text, _REF)]


# ── 1. the declared frame is emitted, and the instance_of twin is NOT ────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Kai is a member of the hockey team.", ("kai", "member_of", "hockey team")),
    ("The GPS is a part of the car.", ("gps", "part_of", "car")),
    ("Marlow is a friend of Devon.", ("marlow", "friend_of", "devon")),
    ("Eli is the child of Dana.", ("eli", "child_of", "dana")),
])
def test_declared_relational_frame_replaces_the_instance_of_reading(frames, text, expected):
    t = _triples(text)
    assert expected in t, t
    # THE HARD LINE: the relational noun is never filed as a TYPE.
    relnoun = expected[1].rsplit("_", 1)[0]
    assert not [x for x in t if x[1] == "instance_of" and x[2] == relnoun], t


def test_flag_off_restores_the_legacy_instance_of_reading(frames, monkeypatch):
    """ABLATION of SPINE_RELATIONAL_PREDICATIVE — OFF must reproduce the defect byte-for-byte."""
    monkeypatch.setattr(m, "SPINE_RELATIONAL_PREDICATIVE", False)
    t = _triples("Kai is a member of the hockey team.")
    assert ("kai", "instance_of", "member") in t, t
    assert not [x for x in t if x[1] == "member_of"], t


# ── 2. the bare plural (no determiner) is admitted — the multi-member shape ──────────────────────

def test_bare_plural_predicative_is_admitted(frames):
    """"...are members of..." has NO det child; the det-gated type-complement selector misses it.

    ABLATION: restore the determiner requirement in the relational lane's own complement selection
    and this goes RED with ZERO facts — which is exactly what the clause produced before the fix."""
    t = _triples("Kai and Rowan are members of the hockey team.")
    assert ("kai", "member_of", "hockey team") in t, t
    assert ("rowan", "member_of", "hockey team") in t, t


def test_subject_coordination_puts_two_members_on_one_group(frames):
    """THE MINT PREDICATE. `_mint_groupings_from_membership` (src/api/main.py) requires
    COUNT(DISTINCT subject_id) >= 2 for one (object, rel) pair before it mints a grouping taxonomy.
    A coordinated subject over a declared relational frame is the ONLY construction that satisfies
    that from a SINGLE utterance.

    ABLATION: delete the `_np_conjuncts(tok)` subject loop and only "kai" survives -> RED."""
    t = _triples("Kai and Rowan are members of the hockey team.")
    subjects = {s for s, r, o in t if r == "member_of" and o == "hockey team"}
    assert len(subjects) >= 2, t


def test_object_side_is_not_distributed_so_a_clausal_coordination_cannot_spray(frames):
    """MEASURED, not assumed. Distributing over the pobj's conjuncts captures "a part of the car and
    the truck" — and also FABRICATES: "Kai is a member of Wellington Rugby and Rowan is happy."
    parses ``Rowan`` as a ``conj`` of the pobj, and the object loop emitted
    ``(kai, member_of, rowan)``. Same ruling the sibling `_emit_folded_locative_containment` already
    reached. A fabricated edge is worse than a missed one.

    ABLATION: re-add the `_np_conjuncts(_rp_arg)` object loop and this goes RED."""
    t = _triples("Kai is a member of Wellington Rugby and Rowan is happy.")
    assert ("kai", "member_of", "wellington rugby") in t, t
    assert not [x for x in t if x[2] == "rowan"], t
    # the stated residual, pinned so it is never mistaken for a silent success
    t2 = _triples("The engine is a part of the car and the truck.")
    assert ("engine", "part_of", "car") in t2, t2
    assert ("engine", "part_of", "truck") not in t2, t2


# ── 2b. the FIRST-PERSON arm — the most common membership statement a user makes ────────────────

@pytest.mark.parametrize("text,expected", [
    ("I am a member of the gym.", ("user", "member_of", "gym")),
    ("I'm a member of the book club.", ("user", "member_of", "book club")),
    ("We are members of the hockey team.", ("user", "member_of", "hockey team")),
    ("I am a friend of Devon.", ("user", "friend_of", "devon")),
])
def test_first_person_membership_is_captured(frames, text, expected):
    """The classification arm skips a first-person subject ("I am ..." is the self/feeling/identity
    lane, never a geo classification). Correct for THAT reading — and it swallowed these whole: on
    the parent all four emitted ZERO facts and the deriver logged `derive_residue_uncovered`, i.e.
    no chain owned the construction at all. First person resolves to `user` by the one language
    hook, detected GRAMMATICALLY (`_is_first_person_personal_pronoun`, Person=1 morphology).

    ABLATION: put the relational lane back BELOW the first-person guard and all four go RED."""
    assert expected in _triples(text), _triples(text)


@pytest.mark.parametrize("text", ["I am happy.", "I am a teacher."])
def test_the_first_person_relaxation_claims_nothing_else(frames, text):
    """The relaxation is scoped by the FRAME, not by the subject: a first-person copula with no
    declared relational complement is left exactly as the parent had it."""
    t = _triples(text)
    assert not [x for x in t if x[0] == "user" and x[1] in _SEEDED_FRAMES.values()], t


# ── 3. the NAME-CONGRUENCE gate on the frame inventory ──────────────────────────────────────────

def test_name_congruence_admits_declared_frames_and_rejects_the_growth_autogloss(monkeypatch):
    """The growth engine auto-generates "X is the <rel> of Y" for EVERY noun-named rel it mints
    (measured across all 20 local tenant schemas: book, car, event, order, plan, tour, plus the
    truncation artifacts del/el/es/goe). Admitting those lets any grown noun-named rel seize the
    copular type complement and DISPLACE a correct classification. The genuine relational rels name
    themselves after the noun AND its preposition (member+of -> member_of).

    ABLATION: drop the `f"{noun}_{prep}" != rel` gate and ("book","of") -> "book" is admitted -> RED."""
    monkeypatch.setattr(m, "_rel_overlay_meta_map", lambda: {
        "member_of": {"natural_language": "X is a member of Y"},
        "part_of": {"natural_language": "X is a part of Y"},
        "instance_of": {"natural_language": "X is an instance of Y (type)"},
        "book": {"natural_language": "X is the book of Y"},          # growth auto-gloss
        "first_time": {"natural_language": "X is the time of Y"},    # growth auto-gloss
        "is_a": {"natural_language": "X is a type of Y"},            # classifier construction
        "located_in": {"natural_language": "X is located in Y"},     # participial — not this family
        "also_known_as": {"natural_language": "X is also known as Y"},  # no determiner
    })
    f = m._relational_predicative_frames()
    assert f == {("member", "of"): "member_of",
                 ("part", "of"): "part_of",
                 ("instance", "of"): "instance_of"}, f


def test_autoglossed_noun_rel_cannot_steal_a_sortal_classification(monkeypatch):
    """The consequence of the gate, at the DERIVER: "Dune is a book of science fiction" must stay
    `instance_of(dune, book)` and must NOT become `(dune, book, science fiction)`.

    This drives the REAL resolver (only the overlay is faked) so the congruence gate is on the path.
    ABLATION: drop the gate and `("book","of") -> "book"` is admitted and this goes RED."""
    monkeypatch.setattr(m, "_rel_overlay_meta_map", lambda: {
        "member_of": {"natural_language": "X is a member of Y"},
        "book": {"natural_language": "X is the book of Y"},   # growth auto-gloss
    })
    t = _triples("Dune is a book of science fiction.")
    assert ("dune", "instance_of", "book") in t, t
    assert not [x for x in t if x[1] == "book"], t


# ── 4. the constructions this lane must NOT touch ───────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("A poodle is a breed of dog.", ("poodle", "subclass_of", "dog")),
    ("A tomato is a kind of fruit.", ("tomato", "subclass_of", "fruit")),
])
def test_taxonomic_classifier_collapse_is_untouched(frames, text, expected):
    assert expected in _triples(text), _triples(text)


def test_sortal_classification_and_geo_containment_are_untouched(frames):
    t = _triples("Hamilton is a city in Ontario.")
    assert ("hamilton", "instance_of", "city") in t, t
    assert ("hamilton", "located_in", "ontario") in t, t


def test_a_relational_noun_with_no_argument_is_still_a_type(frames):
    """No of-PP -> no second argument -> no relation to state; the sortal reading stands."""
    t = _triples("Kai is a member.")
    assert ("kai", "instance_of", "member") in t, t
    assert not [x for x in t if x[1] == "member_of"], t


@pytest.mark.parametrize("text,expected", [
    # a PRONOUN internal argument names no resolvable second entity -> no relation to state
    ("Devon is a friend of mine.", ("devon", "instance_of", "friend")),
    ("Kai is a member of it.", ("kai", "instance_of", "member")),
])
def test_a_pronoun_argument_does_not_bind(frames, text, expected):
    """ABLATION: drop the `c.pos_ in ("NOUN","PROPN")` pobj test in
    `_relational_predicative_binding` and `(devon, friend_of, mine)` / `(kai, member_of, it)` are
    minted — a function word as an entity, THE HARD LINE."""
    t = _triples(text)
    assert expected in t, t
    # the RELATIONAL reading must not fire (the sortal `instance_of` reading is the correct one)
    relnoun = expected[2]
    assert not [x for x in t if x[1] == _SEEDED_FRAMES[(relnoun, "of")]], t


def test_non_referential_pronoun_subject_is_still_refused(frames):
    """The lane routes through `_emit` WITH `subj_tok`, so the chokepoint's HARD-LINE pronoun guards
    still run. ABLATION: withhold `subj_tok` (the taxonomic-classifier arm's discipline) and
    `(this, subclass_of, vehicle)` is minted — a function word as an entity -> RED."""
    t = _triples("This is a subclass of vehicle.")
    assert not [x for x in t if x[0] == "this"], t
