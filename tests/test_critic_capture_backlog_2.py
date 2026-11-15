"""Critic pins for fix/capture-backlog-2 (lane I). Each pin is RED on e5759f8e and names its issue.

Fresh-domain controls ride alongside so a fix that special-cases the probe word does not pass.

Issue map (tkalevra/FaultLine-dev):
  #127 test_111_role_holder_link_without_other_rows_is_kept            (new in lane I)
  #128 test_102_4_retag_skips_conjunct_of_gerund_object                 (new in lane I)
  #129 test_92_determined_year_modifier_is_not_a_count                  (pre-existing)
  #130 test_71_exemption_keeps_counted_plural_type_word_as_a_name       (new in lane I)
  #131 test_83_possessed_role_possessor_mints_a_bare_role_entity        (#83 residual)
  #132 test_71_genitive_name_correction_leaves_dog_unnamed              (#71 residual)
  #133 test_appositive_role_with_predicate_nominal_ladders_occupation_under_role (pre-existing)
  #134 test_102_5_possessed_role_move_lands_on_a_phantom_role_entity     (#102.5 not fixed e2e)
  #135 test_92_count_correction_keeps_the_corrected_value               (#92 correction)
  #137 test_132_repair_rename_swallows_a_second_dog                     (round 2, 5b014b06)
  #138 test_134_role_mover_in_a_compound_turn_stays_a_phantom           (round 2, 5b014b06)
  #139 test_negated_possession_files_a_pet                              (pre-existing)
  #140 test_kin_correction_not_my_sister_is_ignored                     (pre-existing)
  #141 test_speaker_age_question_leaks_a_relative_age                   (pre-existing)
  #160 test_role_rebind_moves_the_users_sister_for_a_third_partys_sister (round 3, 98edd68c)
  #161 test_misattached_clause_moves_the_wrong_holder                   (round 3, 98edd68c)
  #129 (reopened shape) possessive-determiner params of test_92_determined_year_modifier_...
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

import src.api.main as M  # noqa: E402
from tests.laneb_e2e_support import module_flags_fixture  # noqa: E402

_flags = module_flags_fixture()


# ── #111 residual: the speaker's link to a role-holder is dropped when that holder has no other row
@pytest.mark.parametrize("q,rel", [
    ("Who are my brothers?", "sibling_of"),
    ("Who are my daughters?", "child_of"),          # fresh domain
])
def test_111_role_holder_link_without_other_rows_is_kept(q, rel):
    """_possessed_role_answer_strays' own contract: "the speaker's link to a role-holder stays".
    It decides by CONNECTIVITY (does the other end recur in another row?), so a role-holder with no
    row of their own — Juan, a brother with no residence on file — is dropped as a stray from
    "Who are my brothers?"; only Pablo (who has a residence) survives."""
    me, p, j = "u-1", "p" * 36, "j" * 36
    facts = [
        {"_subject_id": p, "_object_id": me, "rel_type": rel},
        {"_subject_id": j, "_object_id": me, "rel_type": rel},
        {"_subject_id": p, "_object_id": "b" * 36, "rel_type": "lives_in"},
    ]
    strays = M._third_party_answer_strays(me, me, q, facts)
    assert 1 not in strays, strays


# ── #102.4 retag over-reach: a nominal conjunct of a GERUND object is re-tagged a verb ──────
def _derive(s):
    import datetime
    import src.extraction.linguistics as L
    return {(f.subject, f.rel_type, f.object)
            for f in (L.derive_sentence_facts(s, reference=datetime.datetime(2026, 10, 1)) or [])}


def test_102_4_retag_skips_conjunct_of_gerund_object():
    """`_repair_coordinated_verb_mistag` re-tags any NOUN conj of a VERB-tagged head. In "I enjoy
    hiking and travel in Peru." the head 'hiking' is a gerund OBJECT of 'enjoy' (a nominal), so
    'travel' is a noun too — re-tagged, it files a travel claim the speaker never made: (user,
    travel_in, peru). The finite-predicate control must stay captured."""
    assert ("user", "travel_in", "peru") not in _derive("I enjoy hiking and travel in Peru.")
    assert any(s == "user" and o == "waterloo"
               for s, r, o in _derive("I live in Kitchener and work in Waterloo."))


# ── #92 adjacency (PRE-EXISTING, red on base b45ea233 too): a determined year modifier is a count ──
@pytest.mark.parametrize("sent,rel", [
    ("I watched the 2018 playoffs.", "watch_playoffs"),
    ("I survived the 2004 storms.", "survive_storms"),     # fresh domain
    # round 2 (5b014b06) gates only a `det`; a possessive determiner is the same classifier slot
    ("I kept my 2019 photos.", "photos"),
    ("I filed my 2020 returns.", "file_returns"),
])
def test_92_determined_year_modifier_is_not_a_count(sent, rel):
    """"the 2018 playoffs" — a cardinal after a definite determiner names WHICH playoffs (a year
    classifier), not how many; the count lane files (user, watch_playoffs, 2018). The lane-I
    comment "a year is never a nummod of a plural count noun" is contradicted by this parse."""
    assert ("user", rel, sent.split()[3]) not in _derive(sent)


# ── E2E pins (fresh *_test seat per pin) ────────────────────────────────────────
from tests.laneb_e2e_support import e2e_fixture, rows, state  # noqa: E402

e2e_books = e2e_fixture()
e2e_nephew = e2e_fixture()
e2e_rename = e2e_fixture()


def _prose(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return " / ".join(str(f.get("definition") or "") for f in (r.get("facts") or [])).lower()


def _preferred(env):
    return {a.lower() for (a,) in rows(env["schema"],
                                       "SELECT alias FROM entity_aliases WHERE is_preferred")}


def test_71_exemption_keeps_counted_plural_type_word_as_a_name(e2e_books):
    """The #71 /ingest exemption spares EVERY subject of a P31 edge this turn from the bare-type-
    word demotion. The count lane files (books, instance_of, book) for "I read 12 books." — a
    counted plural of the type, not a named individual — so 'books' / 'countries' stand as
    preferred NAMES (on base b45ea233 they are demoted). The rendered "you read a book named
    Books" is pre-existing (base renders it too) and is not asserted here."""
    state(e2e_books, "I read 12 books.")
    state(e2e_books, "I visited 3 countries.")              # fresh domain
    pref = _preferred(e2e_books)
    assert "books" not in pref and "countries" not in pref, pref


def test_83_possessed_role_possessor_mints_a_bare_role_entity(e2e_nephew):
    """#83's `_kin_possessor_surface` returns the possessor NOUN's surface for "My brother's son
    Jake": the kin edge lands on a phantom entity whose only name is the type word 'brother'
    beside the named brother Pablo — "Who is Jake?" → "Jake is the child of Brother". A possessed
    role possessor must bind to the role-holder (or file no kin edge), never mint the role word as
    a person. (Base b45ea233 filed the nephew as the SPEAKER's child — "Jake is the child of you"
    — which this pin also refuses.)"""
    state(e2e_nephew, "My brother Pablo lives in Bilbao.")
    state(e2e_nephew, "My brother's son Jake lives in Ottawa.")
    state(e2e_nephew, "My sister Ana lives in Lima.")              # fresh domain
    state(e2e_nephew, "My sister's daughter Mia lives in Cusco.")
    p = _prose(e2e_nephew, "Who is Jake?") + " / " + _prose(e2e_nephew, "Who is Mia?")
    assert "of brother" not in p and "of sister" not in p, p
    assert "child of you" not in p, p


e2e_moved = e2e_fixture()
e2e_recount = e2e_fixture()


def test_102_5_possessed_role_move_lands_on_a_phantom_role_entity(e2e_moved):
    """#102.5 claims "the possessive-role resolver binds 'sister' to the named sister at ingest".
    It does not: with Ana on file, "My sister moved to Cusco." files (sister, lives_in, cusco) on a
    SEPARATE entity named only 'sister'; Ana keeps Lima. "Where does my sister live?" → "Ana lives
    in Lima / Sister lives in Cusco". (Lane I's own e2e pin asserts only "cusco" in the prose, which
    the phantom row satisfies.)"""
    state(e2e_moved, "My sister Ana lives in Lima.")
    state(e2e_moved, "My sister moved to Cusco.")
    p = _prose(e2e_moved, "Where does my sister live?")
    assert "sister lives in" not in p and "ana lives in lima" not in p, p
    state(e2e_moved, "My brother Leo lives in Porto.")              # fresh domain
    state(e2e_moved, "My brother moved to Faro.")
    p = _prose(e2e_moved, "Where does my brother live?")
    assert "brother lives in" not in p and "leo lives in porto" not in p, p


def test_92_count_correction_keeps_the_corrected_value(e2e_recount):
    """Correction end-to-end for #92: "I scored 1999 points." then "No, I scored 2001 points." —
    the seat ends with only (user, has_state, score); neither count survives and the answer is
    "you are score"."""
    state(e2e_recount, "I scored 1999 points.")
    state(e2e_recount, "No, I scored 2001 points.")
    assert "2001" in _prose(e2e_recount, "How many points did I score?")
    state(e2e_recount, "I walked 2000 steps.")                      # fresh domain
    state(e2e_recount, "Actually, I walked 2500 steps.")
    assert "2500" in _prose(e2e_recount, "How many steps did I walk?")


e2e_dog2 = e2e_fixture()
e2e_sis2 = e2e_fixture()


def test_132_repair_rename_swallows_a_second_dog(e2e_dog2):
    """Round 2 (5b014b06) #132 arm: a turn read as a repair re-names the referent its naming clause
    walks to. "Actually, my other dog's name is Rex." introduces a SECOND dog, but the walk lands on
    Biscuit and renames it — the seat ends with one dog, Rex (Biscuit kept only as a non-preferred
    alias of Rex). A true name is destroyed."""
    state(e2e_dog2, "My dog's name is Biscuit.")
    state(e2e_dog2, "Actually, my other dog's name is Rex.")
    pref = _preferred(e2e_dog2)
    assert "biscuit" in pref and "rex" in pref, pref
    state(e2e_dog2, "My boat's name is Osprey.")                   # fresh domain
    state(e2e_dog2, "Sorry, my second boat's name is Heron.")
    pref = _preferred(e2e_dog2)
    assert "osprey" in pref and "heron" in pref, pref


def test_134_role_mover_in_a_compound_turn_stays_a_phantom(e2e_sis2):
    """Round 2 (5b014b06) `_possessed_role_holder_rebinds` only fires when the turn ALSO carries a
    (role, <kin rel>, user) edge. "My sister moved to Cusco and my other sister Eva lives in Quito."
    carries none for 'sister', so both residences land on a phantom entity named 'sister' (Cusco
    AND Quito); Ana keeps Lima and Eva gets no residence."""
    state(e2e_sis2, "My sister Ana lives in Lima.")
    state(e2e_sis2, "My sister moved to Cusco and my other sister Eva lives in Quito.")
    p = _prose(e2e_sis2, "Where does my sister live?")
    assert "sister lives in" not in p, p
    assert "eva lives in quito" in p, p


e2e_neg = e2e_fixture()
e2e_kincorr = e2e_fixture()
e2e_agelk = e2e_fixture()


def test_negated_possession_files_a_pet(e2e_neg):
    """PRE-EXISTING (base b45ea233 identical). "No, I don't have a dog." files
    (user, has_pet, dog) — the clausal negation is ignored and a denial becomes an ownership."""
    state(e2e_neg, "No, I don't have a dog.")
    state(e2e_neg, "I do not own a car.")                          # fresh domain
    rs = rows(e2e_neg["schema"], """
        SELECT f.rel_type, a.alias FROM facts f JOIN entity_aliases a ON a.entity_id = f.object_id
         WHERE f.superseded_at IS NULL AND f.subject_id = %s
        UNION ALL
        SELECT s.rel_type, a.alias FROM staged_facts s JOIN entity_aliases a ON a.entity_id = s.object_id
         WHERE s.deleted_at IS NULL AND s.subject_id = %s""",
        (e2e_neg["user_id"], e2e_neg["user_id"]))
    assert not any(o in ("dog", "car") for r, o in rs), rs


def test_kin_correction_not_my_sister_is_ignored(e2e_kincorr):
    """PRE-EXISTING. "Actually, Ana is my cousin, not my sister." leaves (user, sibling_of, ana) and
    (ana, instance_of, sister) live and mints a bare 'cousin' entity; round 2's role-holder bind then
    moves Ana on "My sister moved to Cusco." because she is still the only sister on file."""
    state(e2e_kincorr, "My sister Ana lives in Lima.")
    state(e2e_kincorr, "Actually, Ana is my cousin, not my sister.")
    p = _prose(e2e_kincorr, "Who is Ana?") + " / " + _prose(e2e_kincorr, "Where does Ana live?")
    assert "your sister" not in p and "siblings" not in p, p
    assert "cousin" in p, p


def test_speaker_age_question_leaks_a_relative_age(e2e_agelk):
    """PRE-EXISTING. "How old am I?" answers "You are 40 years old / Sister is 38 years old" — a
    third party's age rendered in answer to a question about the speaker."""
    state(e2e_agelk, "I am 40 years old.")
    state(e2e_agelk, "My sister is 38.")
    state(e2e_agelk, "My cousin is 25 years old.")                  # fresh domain
    p = _prose(e2e_agelk, "How old am I?")
    assert "40" in p and "38" not in p and "25" not in p, p


e2e_tomsis = e2e_fixture()
e2e_conjsub = e2e_fixture()


def test_role_rebind_moves_the_users_sister_for_a_third_partys_sister(e2e_tomsis):
    """Round 3 (98edd68c) `_role_rebinds`: one bare "my sister" mention in the turn rebinds EVERY
    edge whose surface is 'sister' — including the edges of "Tom's sister". Ana (the user's
    sister) gains (ana, move_to, oslo), (ana, sibling_of, tom), (ana, has_state, stay)."""
    state(e2e_tomsis, "My sister Ana lives in Lima.")
    state(e2e_tomsis, "Tom's sister moved to Oslo and my sister stayed.")
    p = _prose(e2e_tomsis, "Who is Ana?") + " / " + _prose(e2e_tomsis, "Where does Ana live?")
    assert "tom" not in p and "oslo" not in p, p
    state(e2e_tomsis, "My brother Leo lives in Porto.")              # fresh domain
    state(e2e_tomsis, "Maria's brother moved to Faro and my brother stayed.")
    p = _prose(e2e_tomsis, "Who is Leo?") + " / " + _prose(e2e_tomsis, "Where does Leo live?")
    assert "maria" not in p and "faro" not in p, p


def test_misattached_clause_moves_the_wrong_holder(e2e_conjsub):
    """Round 3 (98edd68c): "I visited my brother and my sister lives in Rome." still parses
    (brother, live_in, rome); with the role-holder bind it now lands on LEO — his Porto residence
    is ended and he "currently lives in Rome". `_repair_misattached_clause_subject` covers the
    'moved to X and my other sister Eva lives' shape only."""
    state(e2e_conjsub, "My sister Ana lives in Lima.")
    state(e2e_conjsub, "My brother Leo lives in Porto.")
    state(e2e_conjsub, "I visited my brother and my sister lives in Rome.")
    p = _prose(e2e_conjsub, "Where does Leo live?")
    assert "rome" not in p, p
    state(e2e_conjsub, "My cousin Mara lives in Lyon.")              # fresh domain
    state(e2e_conjsub, "I called my cousin and my uncle works in Nice.")
    assert "nice" not in _prose(e2e_conjsub, "Where does Mara work?")


e2e_pilot = e2e_fixture()


def test_appositive_role_with_predicate_nominal_ladders_occupation_under_role(e2e_pilot):
    """PRE-EXISTING (base b45ea233 identical). "My brother Juan is a pilot." files
    (pilot, subclass_of, brother) — the OCCUPATION laddered as a subclass of the KIN ROLE — and
    Juan gets no preferred name and no occupation: "Who is Juan?" answers nothing."""
    state(e2e_pilot, "My brother Juan is a pilot.")
    state(e2e_pilot, "My cousin Mara is a nurse.")                 # fresh domain
    p = _prose(e2e_pilot, "Who is Juan?")
    assert "pilot" in p, p
    assert "nurse" in _prose(e2e_pilot, "Who is Mara?")


def test_71_genitive_name_correction_leaves_dog_unnamed(e2e_rename):
    """Correction end-to-end for #71: after "My dog's name is Biscuit." → "Actually, my dog's name
    is Rex." the seat keeps (user, has_pet, biscuit), Biscuit's preferred alias is gone, Rex has no
    row, and "What's my dog's name?" answers nothing at all."""
    state(e2e_rename, "My dog's name is Biscuit.")
    state(e2e_rename, "Actually, my dog's name is Rex.")
    assert "rex" in _prose(e2e_rename, "What's my dog's name?")
    state(e2e_rename, "My boat's name is Osprey.")                 # fresh domain
    state(e2e_rename, "Actually, my boat's name is Heron.")
    assert "heron" in _prose(e2e_rename, "What is my boat's name?")
