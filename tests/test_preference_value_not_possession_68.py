"""#68 — "My favorite fruit is mango" also filed (user, owns, mango).

Root cause: linguistics.derive_sentence_facts::_chain_named_instance read the copula as a
name↔type binding (mango ↔ fruit), and its self-possessed arm minted the possession rel for the
TYPE onto the complement — while the favorite/preference pre-pass had already recorded the
possessed head as a functional slot (_pref_suppress / _fn_slot_suppress) that _chain_possessive
honours. The named-instance chain never consulted it.

Fix: the named-instance possession arm declines when the binding's type token is the possessed
head of a recorded preference frame (token index, not word). The (value, instance_of, type)
placement and the preference value edge stand.

Revert-red: base emits (user, owns, mango). Fresh-domain control: "My favourite cheese is gouda"
(no owns gouda) while a plain possessed naming copula ("my cat is Luna") keeps its
possession edge.
"""
from __future__ import annotations

import datetime

import pytest

pytest.importorskip("spacy")

from src.extraction import linguistics as L  # noqa: E402
from tests.laneb_e2e_support import (  # noqa: E402
    e2e_fixture, fact_rows, module_flags_fixture, state,
)

_flags = module_flags_fixture()
e2e = e2e_fixture()
_REF = datetime.datetime(2026, 9, 29)


def _facts(s):
    return {(f.subject, f.rel_type, (f.object or "").lower()) for f in L.derive_sentence_facts(s, _REF)}


_POSSESSION = ("owns", "has_pet")


@pytest.mark.parametrize("text,value,pref_rel", [
    ("My favorite fruit is mango.", "mango", "favorite_fruit"),
    ("My favorite fruit is mango and I live in Hamilton.", "mango", "favorite_fruit"),
    # fresh domain
    ("My favorite vegetable is broccoli.", "broccoli", "favorite_vegetable"),
    ("My favourite cheese is gouda.", "gouda", "favourite_cheese"),
])
def test_preference_value_is_not_possessed(text, value, pref_rel):
    got = _facts(text)
    assert not any(s == "user" and r in _POSSESSION and o == value for (s, r, o) in got), \
        f"#68: preference value filed as a possession: {sorted(got)}"
    assert ("user", pref_rel, value) in got, sorted(got)


def test_plain_possessed_naming_keeps_its_possession():
    # control: no preference selector → the named instance is still the user's
    got = _facts("My cat is Luna.")
    assert any(s == "user" and r in _POSSESSION and o == "luna" for (s, r, o) in got), sorted(got)


def test_e2e_no_possession_row_for_the_preference_value(e2e):
    state(e2e, "My favorite fruit is mango and I live in Hamilton.")
    state(e2e, "My favourite cheese is gouda.")                # fresh domain
    got = fact_rows(e2e)
    bad = {t for t in got if t[0] == "user" and t[1] in _POSSESSION and t[2] in ("mango", "gouda")}
    assert not bad, f"#68: {sorted(bad)}"
    assert ("user", "favorite_fruit", "mango") in got, sorted(got)
    assert ("user", "lives_in", "hamilton") in got, sorted(got)
