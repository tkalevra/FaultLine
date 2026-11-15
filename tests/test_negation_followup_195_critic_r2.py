"""Critic r2 pins for fix/negation-followup-195 @cf427397 — #206 (residual of #201).

A possessive head with only "descriptive" amod modifiers reads restricted_definite and retires the
single type match. Data loss remains when the modifier presupposes a comparison set (comparative /
superlative morphology: "my younger dog", "my oldest dog"), is a numeric ordinal the NER misses
("my 2nd dog"), or is CONTRADICTED by what is stored on the single match ("my brown dog" when the
dog is black). Base 568430d4 keeps the dog in every case; cf427397 retires it.

Fresh-domain twins on a horse. E2E: fresh *_test seats, production flags module-scoped (#63).
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

from tests.laneb_e2e_support import e2e_fixture, rows, state  # noqa: E402

e2e_younger = e2e_fixture()
e2e_oldest_horse = e2e_fixture()
e2e_2nd = e2e_fixture()
e2e_contradicted = e2e_fixture()
e2e_contradicted_horse = e2e_fixture()


def _live(env, name):
    rs = rows(env["schema"], """
        SELECT f.rel_type FROM facts f
         WHERE f.subject_id = %s AND f.superseded_at IS NULL
           AND COALESCE(f.polarity, 'affirmed') = 'affirmed'
           AND (SELECT string_agg(lower(alias), '|') FROM entity_aliases
                 WHERE entity_id = f.object_id) = %s""", (env["user_id"], name))
    return [r for (r,) in rs if r != "instance_of"]


def test_my_younger_dog_keeps_the_only_dog(e2e_younger):
    """Degree=Cmp presupposes two or more dogs: the one on file is not identified."""
    state(e2e_younger, "My dog Quillon is 4 years old.")
    assert _live(e2e_younger, "quillon")
    state(e2e_younger, "I don't have my younger dog anymore.")
    assert _live(e2e_younger, "quillon"), "#206: 'my younger dog' retired the only dog"


def test_my_oldest_horse_keeps_the_only_horse(e2e_oldest_horse):
    """Fresh domain, Degree=Sup."""
    state(e2e_oldest_horse, "My horse Comet is 9 years old.")
    assert _live(e2e_oldest_horse, "comet")
    state(e2e_oldest_horse, "I don't have my oldest horse anymore.")
    assert _live(e2e_oldest_horse, "comet"), "#206: 'my oldest horse' retired the only horse"


def test_my_2nd_dog_keeps_the_only_dog(e2e_2nd):
    """A numeric ordinal is an ordinal: NER does not tag '2nd', the parse says amod ADJ."""
    state(e2e_2nd, "My dog Quillon is 4 years old.")
    assert _live(e2e_2nd, "quillon")
    state(e2e_2nd, "I don't have my 2nd dog anymore.")
    assert _live(e2e_2nd, "quillon"), "#206: 'my 2nd dog' retired the only dog"


def test_my_brown_dog_keeps_the_black_dog(e2e_contradicted):
    """The stored state contradicts the modifier: the one dog on file is not 'my brown dog'."""
    state(e2e_contradicted, "My dog Quillon is 4 years old.")
    state(e2e_contradicted, "Quillon is black.")
    assert _live(e2e_contradicted, "quillon")
    state(e2e_contradicted, "I don't have my brown dog anymore.")
    assert _live(e2e_contradicted, "quillon"), "#206: 'my brown dog' retired the black dog"


def test_my_grey_horse_keeps_the_white_horse(e2e_contradicted_horse):
    """Fresh-domain twin of the contradicted modifier."""
    state(e2e_contradicted_horse, "My horse Comet is 9 years old.")
    state(e2e_contradicted_horse, "Comet is white.")
    assert _live(e2e_contradicted_horse, "comet")
    state(e2e_contradicted_horse, "I don't have my grey horse anymore.")
    assert _live(e2e_contradicted_horse, "comet"), "#206: 'my grey horse' retired the white horse"
