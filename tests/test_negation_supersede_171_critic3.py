"""Critic round-3 pins for fix/negation-supersede-171 (568430d4).

1. LEMMA TYPE ROOT COLLIDES WITH A PET NAME (#171 shape, data loss). `_find_denied_same_slot_facts`
   adds EVERY entity whose alias equals the denied head's lemma to the type roots. Under #177 the
   TYPE entity 'cat' carries its pets' names as aliases ('cat' aliases mouse, luna), so
   "I don't have mice anymore." resolves the root 'mouse' to the CAT type entity, and every
   `instance_of cat` row on has_pet is retired. The cat named Mouse is not a mouse.
2. UNMARKED QUANTIFIER DETERMINER READ AS "ALL". `_denied_np_reading` treats any det without
   Definite/Dem morphology as the universal reading. "another" carries no morphology in spaCy, so
   "I don't have another cat." (= I have no additional cat; it presupposes the cats the user has)
   retires every cat.
3. (follow-up, #197; superseded by the #206 verification rule, see below) A POSSESSIVE-DEFINITE restricted denial over a single instance.
   "I don't have my old dog anymore." presupposes the dog exists; with exactly one dog on the slot
   it names that dog, but the restrictive-dependent rule retires nothing, and the render then
   contradicts itself ("you have a dog named Quillon" next to "You do not have a pet that is old
   dog"). Fail-safe direction (under-retirement), so it does not block shipping.

E2E on fresh *_test seats, production flags (laneb_e2e_support; module-scoped, never module-level
os.environ — #63). Each wound has a fresh-domain control.
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")
from tests.laneb_e2e_support import e2e_fixture, rows, state  # noqa: E402

e2e_mice = e2e_fixture()
e2e_foxes = e2e_fixture()
e2e_another = e2e_fixture()
e2e_anotherbank = e2e_fixture()
e2e_olddog = e2e_fixture()
e2e_oldbank = e2e_fixture()


def _slot(env):
    rs = rows(env["schema"], """
        SELECT f.rel_type, (SELECT string_agg(lower(alias), '|') FROM entity_aliases
                             WHERE entity_id = f.object_id),
               f.polarity, f.superseded_at IS NULL
          FROM facts f WHERE f.subject_id = %s
        UNION ALL
        SELECT s.rel_type, (SELECT string_agg(lower(alias), '|') FROM entity_aliases
                             WHERE entity_id = s.object_id),
               s.polarity, (s.deleted_at IS NULL AND s.expires_at > now())
          FROM staged_facts s WHERE s.subject_id = %s""", (env["user_id"], env["user_id"]))
    return [(r, set((o or "").split("|")), p, live) for r, o, p, live in rs]


def _live_instance(env, name):
    return [r for r in _slot(env) if r[1] == {name} and r[2] == "affirmed" and r[3]
            and r[0] != "instance_of"]


# ── 1. a lemma root must not resolve to a type entity through a pet-name alias ─────────────────
def test_denying_mice_keeps_the_cat_named_mouse_and_her_sister(e2e_mice):
    state(e2e_mice, "My cat Mouse is 2 years old.")
    state(e2e_mice, "My cat Luna is 4 years old.")
    assert _live_instance(e2e_mice, "mouse") and _live_instance(e2e_mice, "luna"), _slot(e2e_mice)
    state(e2e_mice, "I don't have mice anymore.")
    assert _live_instance(e2e_mice, "mouse"), _slot(e2e_mice)
    assert _live_instance(e2e_mice, "luna"), _slot(e2e_mice)


def test_fresh_domain_denying_foxes_keeps_the_dog_named_fox(e2e_foxes):
    """Fresh-domain control: a regular plural, a different species."""
    state(e2e_foxes, "My dog Fox is 3 years old.")
    state(e2e_foxes, "My dog Quillon is 4 years old.")
    assert _live_instance(e2e_foxes, "fox") and _live_instance(e2e_foxes, "quillon"), \
        _slot(e2e_foxes)
    state(e2e_foxes, "I don't have foxes anymore.")
    assert _live_instance(e2e_foxes, "fox"), _slot(e2e_foxes)
    assert _live_instance(e2e_foxes, "quillon"), _slot(e2e_foxes)


# ── 2. "another" is not a universal ───────────────────────────────────────────────────────────
def test_denying_another_cat_keeps_both_cats(e2e_another):
    state(e2e_another, "My cat Mango is 2 years old.")
    state(e2e_another, "My cat Luna is 4 years old.")
    assert _live_instance(e2e_another, "mango") and _live_instance(e2e_another, "luna"), \
        _slot(e2e_another)
    state(e2e_another, "I don't have another cat.")
    assert _live_instance(e2e_another, "mango"), _slot(e2e_another)
    assert _live_instance(e2e_another, "luna"), _slot(e2e_another)


def test_fresh_domain_denying_another_bank_keeps_both_employers(e2e_anotherbank):
    state(e2e_anotherbank, "I work at Acme.")
    state(e2e_anotherbank, "Acme is a bank.")
    state(e2e_anotherbank, "I work at Zenith.")
    state(e2e_anotherbank, "Zenith is a bank.")
    assert _live_instance(e2e_anotherbank, "acme") and _live_instance(e2e_anotherbank, "zenith"), \
        _slot(e2e_anotherbank)
    state(e2e_anotherbank, "I don't work at another bank.")
    assert _live_instance(e2e_anotherbank, "acme"), _slot(e2e_anotherbank)
    assert _live_instance(e2e_anotherbank, "zenith"), _slot(e2e_anotherbank)


# ── 3. follow-up: possessive-definite restricted denial over the only instance ────────────────
# #197 → #206 (lane M2 round 3, coordinator decision): a restricted NP — possessive included —
# retires the single match ONLY when stored facts verify every modifier. Nothing stores "old" here,
# so the dog / the employer is KEPT: accepted under-retirement (visible, recoverable), after three
# rounds in which the descriptive-adjective convenience lost data. The verified twin ("my brown
# dog" with a stored brown dog) lives in tests/test_negation_followup_195.py.
def test_my_old_dog_unverified_keeps_the_only_dog(e2e_olddog):
    state(e2e_olddog, "My dog Quillon is 4 years old.")
    assert _live_instance(e2e_olddog, "quillon"), _slot(e2e_olddog)
    state(e2e_olddog, "I don't have my old dog anymore.")
    assert _live_instance(e2e_olddog, "quillon"), _slot(e2e_olddog)


def test_fresh_domain_my_old_bank_unverified_keeps_the_only_employer(e2e_oldbank):
    state(e2e_oldbank, "I work at Acme.")
    state(e2e_oldbank, "Acme is a bank.")
    assert _live_instance(e2e_oldbank, "acme"), _slot(e2e_oldbank)
    state(e2e_oldbank, "I don't work at my old bank anymore.")
    assert _live_instance(e2e_oldbank, "acme"), _slot(e2e_oldbank)
