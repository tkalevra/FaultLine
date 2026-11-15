"""#195 / #196 / #197: the #171 denial follow-ups.

#195 (data loss) — a common-noun denial's lemma resolved its TYPE roots through ANY alias, and under
#177 the type entity 'cat' carries its pets' names as aliases, so "I don't have mice anymore." with a
cat named Mouse retired every cat. A lemma root must be a TYPE (it has members in the stored ladder),
and an alias that one of the type's own INSTANCES carries is that instance's name, not the type's.

#196 (data loss) — an unmarked quantifier determiner read as the universal: "I don't have another
cat." retired every cat. The universal reading of an unmarked determiner is admitted only for the
tenant's grown universal-denial determiner class (migration 288: any, no); any other ("another",
"some") and a partitive ("one of the cats") retire nothing.

#197 — a restricted denial over the ONLY instance: a possessive/definite restricted NP ("my old
dog") presupposes its referent and retires the single type match; an indefinite restricted NP ("a
brown dog") retires it only when stored attributes verify the restriction; a modifier equal to the
slot's own head noun ("a pet dog" on has_pet) restricts nothing. "I don't have a dog that sheds."
over one dog still retires nothing (#190).

The critic's E2E pins live in tests/test_negation_supersede_171_critic3.py. Here: the grammar reading
(pure, with the cue class stubbed so it runs without a DSN) and the extra E2E controls, each with a
fresh-domain twin. E2E: fresh *_test seats, production flags (module-scoped — #63).
"""
from __future__ import annotations

import pytest

import src.api.main as M

pytest.importorskip("spacy")


@pytest.fixture
def _cue_class(monkeypatch):
    """The tenant's grown cue classes as migrations 288 / 289 seed them."""
    monkeypatch.setattr(M, "_universal_denial_determiners", lambda: frozenset({"any", "no"}))
    monkeypatch.setattr(M, "_distinct_referent_modifiers",
                        lambda: frozenset({"other", "another", "new", "different", "additional"}))


# ── #196: quantifier determiners (pure grammar + the grown cue class) ─────────────────────────
@pytest.mark.parametrize("sent,surf,want", [
    ("I don't have another cat.", "cat", "ambiguous"),
    ("I don't have some cats anymore.", "cat", "ambiguous"),
    ("I don't have one of the cats anymore.", "cat", "ambiguous"),
    ("I don't have any cats anymore.", "cat", "all"),
    ("I don't have any of the cats anymore.", "cat", "all"),
    ("I don't have a cat anymore.", "cat", "all"),
    ("I don't have cats anymore.", "cat", "all"),
    ("I don't work at another bank.", "bank", "ambiguous"),          # fresh domain
    ("I don't work at any bank anymore.", "bank", "all"),
])
def test_quantifier_determiner_reading(_cue_class, sent, surf, want):
    reading, _ = M._denied_np_reading(sent, [surf])
    assert reading == want, (sent, reading)


def test_an_empty_cue_class_reads_no_quantifier_as_universal(monkeypatch):
    """Fail-safe: with the class unavailable (DB down), 'any' is not universal either — nothing is
    retired — while the article / bare / plural readings stay grammar-only."""
    monkeypatch.setattr(M, "_universal_denial_determiners", lambda: frozenset())
    assert M._denied_np_reading("I don't have any cats anymore.", ["cat"])[0] == "ambiguous"
    assert M._denied_np_reading("I don't have a cat anymore.", ["cat"])[0] == "all"
    assert M._denied_np_reading("I don't have cats anymore.", ["cat"])[0] == "all"


# ── #197: restricted readings ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("sent,surf,slot,want,mods,verifiable", [
    ("I don't have my old dog anymore.", "dog", {"pet"}, "restricted_definite", ["old"], True),
    ("I don't work at my old bank anymore.", "bank", set(), "restricted_definite", ["old"], True),
    ("I don't have a brown dog anymore.", "dog", {"pet"}, "restricted", ["brown"], True),
    ("I don't have a dog that sheds.", "dog", {"pet"}, "restricted", ["shed"], False),
    ("I don't have a pet dog anymore.", "dog", {"pet"}, "all", None, None),
])
def test_restricted_reading(_cue_class, sent, surf, slot, want, mods, verifiable):
    detail: dict = {}
    reading, _ = M._denied_np_reading(sent, [surf], slot_nouns=slot, detail=detail)
    assert reading == want, (sent, reading)
    if mods is not None:
        assert detail.get("modifiers") == mods and detail.get("verifiable") is verifiable, detail


# ── round 2: #201 distinct referent, #202 not-all quantifier, #203 third-party possessor ──────
@pytest.mark.parametrize("sent,surf,want", [
    ("I don't have my other dog anymore.", "dog", "restricted_distinct"),       # #201
    ("I don't have my new dog anymore.", "dog", "restricted_distinct"),
    ("I don't have my second dog anymore.", "dog", "restricted_distinct"),      # ORDINAL
    ("I don't have my third horse anymore.", "horse", "restricted_distinct"),
    ("I no longer have my dog in Toronto.", "dog", "restricted_distinct"),
    ("I don't have my dog that sheds anymore.", "dog", "restricted_distinct"),
    ("I don't have my two dogs anymore.", "dog", "restricted_distinct"),
    ("I don't have my old dog anymore.", "dog", "restricted_definite"),
    ("I don't have all my cats anymore.", "cat", "ambiguous"),                  # #202
    ("I don't have both my cats anymore.", "cat", "ambiguous"),
    ("I don't have all the horses anymore.", "horse", "ambiguous"),
    ("I don't have my cats anymore.", "cat", "all"),
    ("I don't have my sister's dog anymore.", "dog", "other_possessor"),        # #203
    ("I don't have Tom's horse anymore.", "horse", "other_possessor"),
    ("I don't have his dog anymore.", "dog", "other_possessor"),
    ("I don't have our dog anymore.", "dog", "one"),
    ("I don't have my dog anymore.", "dog", "one"),
])
def test_round2_reading(_cue_class, sent, surf, want):
    reading, _ = M._denied_np_reading(sent, [surf], slot_nouns={"pet"})
    assert reading == want, (sent, reading)


@pytest.mark.parametrize("sent,surf,want", [
    ("I don't have my younger dog anymore.", "dog", "restricted_distinct"),     # #206 Degree=Cmp
    ("I don't have my oldest horse anymore.", "horse", "restricted_distinct"),  # Degree=Sup
    ("I don't have my 2nd dog anymore.", "dog", "restricted_distinct"),         # numeric ordinal
    ("I don't have my 21st horse anymore.", "horse", "restricted_distinct"),
    ("I don't have my next dog anymore.", "dog", "restricted_definite"),        # gate: unverifiable
    ("I don't have my brown dog anymore.", "dog", "restricted_definite"),       # gate verifies
])
def test_round3_reading(_cue_class, sent, surf, want):
    reading, _ = M._denied_np_reading(sent, [surf], slot_nouns={"pet"})
    assert reading == want, (sent, reading)


def test_an_empty_distinct_class_still_needs_verification(monkeypatch):
    """#206: with migration 289's class unavailable, 'my other dog' reads restricted_definite —
    harmless, because the gate retires only a stored-fact-verified match."""
    monkeypatch.setattr(M, "_universal_denial_determiners", lambda: frozenset({"any", "no"}))
    monkeypatch.setattr(M, "_distinct_referent_modifiers", lambda: frozenset())
    assert M._denied_np_reading("I don't have my other dog anymore.", ["dog"])[0] == \
        "restricted_definite"


# ── E2E ──────────────────────────────────────────────────────────────────────────────────────
from tests.laneb_e2e_support import e2e_fixture, rows, state  # noqa: E402

e2e_amouse = e2e_fixture()
e2e_anycats = e2e_fixture()
e2e_sheds = e2e_fixture()
e2e_brown = e2e_fixture()
e2e_petdog = e2e_fixture()
e2e_twodogs_old = e2e_fixture()
e2e_mydogsheds = e2e_fixture()
e2e_mytwodogs = e2e_fixture()
e2e_mybrown = e2e_fixture()
e2e_mygrey_horse = e2e_fixture()
e2e_twocolours = e2e_fixture()


def _live(env, name):
    rs = rows(env["schema"], """
        SELECT f.rel_type FROM facts f
         WHERE f.subject_id = %s AND f.superseded_at IS NULL
           AND COALESCE(f.polarity, 'affirmed') = 'affirmed'
           AND (SELECT string_agg(lower(alias), '|') FROM entity_aliases
                 WHERE entity_id = f.object_id) = %s""", (env["user_id"], name))
    return [r for (r,) in rs if r != "instance_of"]


def test_denying_a_mouse_keeps_the_cat_named_mouse(e2e_amouse):
    """#195, singular: the denied object itself must not resolve through a pet's name either."""
    state(e2e_amouse, "My cat Mouse is 2 years old.")
    state(e2e_amouse, "My cat Luna is 4 years old.")
    assert _live(e2e_amouse, "mouse") and _live(e2e_amouse, "luna")
    state(e2e_amouse, "I don't have a mouse anymore.")
    assert _live(e2e_amouse, "luna")


def test_any_cats_still_retires_every_cat(e2e_anycats):
    """#196 control: the universal determiner (grown class, migration 288) still denies all."""
    state(e2e_anycats, "My cat Mango is 2 years old.")
    state(e2e_anycats, "My cat Luna is 4 years old.")
    assert _live(e2e_anycats, "mango") and _live(e2e_anycats, "luna")
    state(e2e_anycats, "I don't have any cats anymore.")
    assert not _live(e2e_anycats, "mango") and not _live(e2e_anycats, "luna")


def test_a_dog_that_sheds_keeps_the_only_dog(e2e_sheds):
    """#197 / #190 at n=1: an unverifiable restriction (relative clause) retires nothing."""
    state(e2e_sheds, "My dog Quillon is 4 years old.")
    assert _live(e2e_sheds, "quillon")
    state(e2e_sheds, "I don't have a dog that sheds.")
    assert _live(e2e_sheds, "quillon")


def test_a_brown_dog_retires_the_brown_dog_only_when_verified(e2e_brown):
    """#197: an indefinite restricted NP retires the single match when its stored attributes
    verify the restriction."""
    state(e2e_brown, "My dog Quillon is 4 years old.")
    state(e2e_brown, "Quillon is brown.")
    assert _live(e2e_brown, "quillon")
    state(e2e_brown, "I don't have a black dog anymore.")
    assert _live(e2e_brown, "quillon"), "an unverified restriction must retire nothing"
    state(e2e_brown, "I don't have a brown dog anymore.")
    assert not _live(e2e_brown, "quillon")


def test_a_pet_dog_retires_the_only_dog(e2e_petdog):
    """#197: the slot's own head noun as a modifier ('pet dog' on has_pet) is not a restriction."""
    state(e2e_petdog, "My dog Quillon is 4 years old.")
    assert _live(e2e_petdog, "quillon")
    state(e2e_petdog, "I don't have a pet dog anymore.")
    assert not _live(e2e_petdog, "quillon")


def test_my_old_dog_over_two_dogs_retires_nothing(e2e_twodogs_old):
    """#197: the presupposed referent is unidentified among several matches → nothing retired."""
    state(e2e_twodogs_old, "My dog Quillon is 4 years old.")
    state(e2e_twodogs_old, "My dog Biscuit is 6 years old.")
    assert _live(e2e_twodogs_old, "quillon") and _live(e2e_twodogs_old, "biscuit")
    state(e2e_twodogs_old, "I don't have my old dog anymore.")
    assert _live(e2e_twodogs_old, "quillon") and _live(e2e_twodogs_old, "biscuit")


def test_my_dog_that_sheds_keeps_the_only_dog(e2e_mydogsheds):
    """#201 (critic's debatable case): a relative clause on a possessed head restricts; nothing
    retired."""
    state(e2e_mydogsheds, "My dog Quillon is 4 years old.")
    assert _live(e2e_mydogsheds, "quillon")
    state(e2e_mydogsheds, "I don't have my dog that sheds anymore.")
    assert _live(e2e_mydogsheds, "quillon")


def test_my_two_dogs_keeps_the_only_dog(e2e_mytwodogs):
    """#201 (critic's debatable case): a numeral on a possessed head counts referents the slot does
    not hold; nothing retired."""
    state(e2e_mytwodogs, "My dog Quillon is 4 years old.")
    assert _live(e2e_mytwodogs, "quillon")
    state(e2e_mytwodogs, "I don't have my two dogs anymore.")
    assert _live(e2e_mytwodogs, "quillon")


def test_my_brown_dog_retires_the_verified_brown_dog(e2e_mybrown):
    """#206: a possessive restricted NP retires the single match when a stored fact verifies the
    modifier (Quillon has_state brown)."""
    state(e2e_mybrown, "My dog Quillon is 4 years old.")
    state(e2e_mybrown, "Quillon is brown.")
    assert _live(e2e_mybrown, "quillon")
    state(e2e_mybrown, "I don't have my brown dog anymore.")
    assert not _live(e2e_mybrown, "quillon")


def test_my_grey_horse_retires_the_verified_grey_horse(e2e_mygrey_horse):
    """Fresh-domain twin of the verified possessive."""
    state(e2e_mygrey_horse, "My horse Comet is 9 years old.")
    state(e2e_mygrey_horse, "Comet is grey.")
    assert _live(e2e_mygrey_horse, "comet")
    state(e2e_mygrey_horse, "I don't have my grey horse anymore.")
    assert not _live(e2e_mygrey_horse, "comet")


def test_two_live_colours_do_not_verify_the_denial(e2e_twocolours):
    """#208: the verifying value must be the instance's ONLY live value on that rel/attribute.
    Two live has_state colours make "brown" uncertain → nothing retired. (Control: a single live
    brown value retires — test_my_brown_dog_retires_the_verified_brown_dog.)"""
    state(e2e_twocolours, "My dog Quillon is 4 years old.")
    state(e2e_twocolours, "Quillon is brown.")
    state(e2e_twocolours, "Quillon is black.")
    assert _live(e2e_twocolours, "quillon")
    state(e2e_twocolours, "I don't have my brown dog anymore.")
    assert _live(e2e_twocolours, "quillon")
