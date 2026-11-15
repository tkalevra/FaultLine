"""Critic r3 pins for fix/negation-followup-195 @dca6a296 — #208.

A restricted denial now retires its single match only when a stored fact VERIFIES each modifier.
The match is exact, subject-only and skips negated/superseded rows — but a pre-existing capture gap
leaves a corrected value live: "Actually, Quillon is black, not brown." files has_state black and
keeps has_state brown. "I don't have my brown dog anymore." is then verified by the stale row and
retires the dog (base 568430d4 keeps it). Conflicting live values on one property must not verify.

Fresh-domain twin on a horse. E2E: fresh *_test seats, production flags module-scoped (#63).
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

from tests.laneb_e2e_support import e2e_fixture, rows, state  # noqa: E402

e2e_corrected = e2e_fixture()
e2e_corrected_horse = e2e_fixture()


def _live(env, name):
    rs = rows(env["schema"], """
        SELECT f.rel_type FROM facts f
         WHERE f.subject_id = %s AND f.superseded_at IS NULL
           AND COALESCE(f.polarity, 'affirmed') = 'affirmed'
           AND (SELECT string_agg(lower(alias), '|') FROM entity_aliases
                 WHERE entity_id = f.object_id) = %s""", (env["user_id"], name))
    return [r for (r,) in rs if r != "instance_of"]


def test_corrected_colour_does_not_verify_the_denial(e2e_corrected):
    state(e2e_corrected, "My dog Quillon is 4 years old.")
    state(e2e_corrected, "Quillon is brown.")
    state(e2e_corrected, "Actually, Quillon is black, not brown.")
    assert _live(e2e_corrected, "quillon")
    state(e2e_corrected, "I don't have my brown dog anymore.")
    assert _live(e2e_corrected, "quillon"), \
        "#208: a corrected-away colour verified 'my brown dog' and retired the dog"


def test_corrected_colour_does_not_verify_the_denial_horse(e2e_corrected_horse):
    state(e2e_corrected_horse, "My horse Comet is 9 years old.")
    state(e2e_corrected_horse, "Comet is grey.")
    state(e2e_corrected_horse, "Actually, Comet is white, not grey.")
    assert _live(e2e_corrected_horse, "comet")
    state(e2e_corrected_horse, "I don't have my grey horse anymore.")
    assert _live(e2e_corrected_horse, "comet"), \
        "#208: a corrected-away colour verified 'my grey horse' and retired the horse"
