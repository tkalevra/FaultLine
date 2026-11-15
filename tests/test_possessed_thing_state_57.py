"""#57 — "My car is red" filed a junk relation (user, car, red) named after the possessed noun.

Root cause: linguistics.derive_sentence_facts::_chain_attr_scalar (V6 single-ADJ arm) reads every
"my <noun> is <ADJ>" as an attribute scalar keyed by the noun. That is right for an ATTRIBUTE noun
("my mood is good", a coined "my vantick is teal") and wrong for a concrete THING. WordNet's
supersense partition (_noun_denotes_concrete_thing: a concrete sense, no attribute-like sense) now
routes the thing reading to (user, owns, car) + (car, has_state, red).

Fresh-domain control: "My kayak is green" (thing) vs "My mood is good" / "My vantick is teal"
(attribute / undecidable → unchanged).
"""
from __future__ import annotations

import datetime

import pytest

pytest.importorskip("spacy")

from src.extraction import linguistics as L  # noqa: E402
from tests.laneb_e2e_support import (  # noqa: E402
    attr_rows, e2e_fixture, fact_rows, module_flags_fixture, state,
)

_flags = module_flags_fixture()
e2e = e2e_fixture()
_REF = datetime.datetime(2026, 9, 29)


def _facts(s):
    return {(f.subject, f.rel_type, (f.object or "").lower()) for f in L.derive_sentence_facts(s, _REF)}


@pytest.mark.parametrize("text,thing,value", [
    ("My car is red.", "car", "red"),
    ("My kayak is green.", "kayak", "green"),               # fresh domain
])
def test_quality_lands_on_the_thing(text, thing, value):
    got = _facts(text)
    assert ("user", thing, value) not in got, f"#57: junk rel named after the noun: {got}"
    assert (thing, "has_state", value) in got, got
    assert ("user", "owns", thing) in got, got


@pytest.mark.parametrize("text,rel,value", [
    ("My mood is good.", "mood", "good"),
    ("My vantick is teal.", "vantick", "teal"),
])
def test_attribute_nouns_keep_the_scalar_reading(text, rel, value):
    assert ("user", rel, value) in _facts(text), _facts(text)


def test_e2e_car_state_lands_on_the_car(e2e):
    state(e2e, "My car is red.")
    state(e2e, "My kayak is green.")
    facts = fact_rows(e2e)
    attrs = {(a, v) for (_e, a, v) in attr_rows(e2e)}
    assert ("car", "red") not in attrs and ("kayak", "green") not in attrs, f"#57: {attrs}"
    assert not any(r in ("car", "kayak") for (_s, r, _o) in facts), f"#57: {facts}"
    assert ("car", "has_state", "red") in facts, sorted(facts)
    assert ("kayak", "has_state", "green") in facts, sorted(facts)
