"""Critic pins for fix/negation-followup-195 (397ed2e1): denial over-retirement that survives or is
introduced by the #195/#196/#197 fix. The worst failure is silent data loss, so each pin seeds the
user's own instance(s), states a denial that does NOT deny them, and asserts they stay live.

  #201 (regression of the #197 fix) — a possessive NP with a restricting modifier reads
       "restricted_definite" and `_gate_restricted_denial` retires the single type match WITHOUT
       checking the restriction: "my other dog", "my second dog", "my dog in Toronto" retire the one
       dog on file (base 568430d4 keeps it).
  #202 (pre-existing, #196 residual) — "all my cats" / "both my cats": the possessor marks the NP
       definite, the existential-determiner check is skipped, the plural reads "all".
  #203 (pre-existing) — "my sister's dog": a third-party possessor reads as the user's referent.
  #204 (pre-existing, capture) — "I have no cats anymore." files an AFFIRMED has_pet.

Fresh-domain twins use a different type (horse) so a fix keyed to "dog" cannot pass. E2E: fresh
*_test seats, production flags applied module-scoped by e2e_fixture (#63).
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

from tests.laneb_e2e_support import e2e_fixture, rows, state  # noqa: E402

e2e_other = e2e_fixture()
e2e_other_horse = e2e_fixture()
e2e_second = e2e_fixture()
e2e_located = e2e_fixture()
e2e_allmy = e2e_fixture()
e2e_bothmy = e2e_fixture()
e2e_sister = e2e_fixture()
e2e_nocats = e2e_fixture()


def _live(env, name):
    rs = rows(env["schema"], """
        SELECT f.rel_type FROM facts f
         WHERE f.subject_id = %s AND f.superseded_at IS NULL
           AND COALESCE(f.polarity, 'affirmed') = 'affirmed'
           AND (SELECT string_agg(lower(alias), '|') FROM entity_aliases
                 WHERE entity_id = f.object_id) = %s""", (env["user_id"], name))
    return [r for (r,) in rs if r != "instance_of"]


# ── #201: possessive + restriction over the ONLY instance ────────────────────────────────────
def test_my_other_dog_keeps_the_only_dog(e2e_other):
    """'my other dog' presupposes a dog that is NOT the one on file."""
    state(e2e_other, "My dog Quillon is 4 years old.")
    assert _live(e2e_other, "quillon")
    state(e2e_other, "I don't have my other dog anymore.")
    assert _live(e2e_other, "quillon"), "#201: 'my other dog' retired the only dog on file"


def test_my_other_horse_keeps_the_only_horse(e2e_other_horse):
    """Fresh-domain twin of the above."""
    state(e2e_other_horse, "My horse Comet is 9 years old.")
    assert _live(e2e_other_horse, "comet")
    state(e2e_other_horse, "I don't have my other horse anymore.")
    assert _live(e2e_other_horse, "comet"), "#201: 'my other horse' retired the only horse"


def test_my_second_dog_keeps_the_only_dog(e2e_second):
    state(e2e_second, "My dog Quillon is 4 years old.")
    assert _live(e2e_second, "quillon")
    state(e2e_second, "I don't have my second dog anymore.")
    assert _live(e2e_second, "quillon"), "#201: 'my second dog' retired the only dog on file"


def test_my_dog_in_a_place_keeps_the_dog(e2e_located):
    """A locative PP on the possessed head locates the dog; it does not deny owning it."""
    state(e2e_located, "My dog Quillon is 4 years old.")
    assert _live(e2e_located, "quillon")
    state(e2e_located, "I no longer have my dog in Toronto.")
    assert _live(e2e_located, "quillon"), "#201: 'my dog in Toronto' retired the dog"


# ── #202: non-universal quantifier under a possessor ─────────────────────────────────────────
def test_not_all_my_cats_keeps_the_cats(e2e_allmy):
    state(e2e_allmy, "My cat Mango is 2 years old.")
    state(e2e_allmy, "My cat Luna is 4 years old.")
    assert _live(e2e_allmy, "mango") and _live(e2e_allmy, "luna")
    state(e2e_allmy, "I don't have all my cats anymore.")
    assert _live(e2e_allmy, "mango") and _live(e2e_allmy, "luna"), \
        "#202: 'not all my cats' retired every cat"


def test_not_both_my_cats_keeps_the_cats(e2e_bothmy):
    state(e2e_bothmy, "My cat Mango is 2 years old.")
    state(e2e_bothmy, "My cat Luna is 4 years old.")
    assert _live(e2e_bothmy, "mango") and _live(e2e_bothmy, "luna")
    state(e2e_bothmy, "I don't have both my cats anymore.")
    assert _live(e2e_bothmy, "mango") and _live(e2e_bothmy, "luna"), \
        "#202: 'not both my cats' retired every cat"


# ── #203: third-party possessor ──────────────────────────────────────────────────────────────
def test_my_sisters_dog_keeps_my_dog(e2e_sister):
    state(e2e_sister, "My dog Quillon is 4 years old.")
    assert _live(e2e_sister, "quillon")
    state(e2e_sister, "I don't have my sister's dog anymore.")
    assert _live(e2e_sister, "quillon"), "#203: denying the sister's dog retired the user's dog"


# ── #204: object-NP negation lands affirmed ──────────────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="#204: object-NP negation ('no' det) is not read as clause "
                   "polarity on capture — out of lane M2 scope (not trivial); open")
def test_i_have_no_cats_files_no_affirmed_pet(e2e_nocats):
    state(e2e_nocats, "My cat Mango is 2 years old.")
    assert _live(e2e_nocats, "mango")
    state(e2e_nocats, "I have no cats anymore.")
    assert not _live(e2e_nocats, "cats"), "#204: 'I have no cats' filed an AFFIRMED has_pet cats"
