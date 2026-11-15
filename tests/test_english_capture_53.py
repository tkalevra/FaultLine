"""#53 — English capture: three wrong readings in the spine deriver.

1. "My mother calls Anna every day." → (anna, parent_of, user). linguistics._chain_named_role took
   an ACTIVE clause's plain dobj PROPN as the assigned name. Voice now decides: passive
   (nsubjpass/auxpass) may name through oprd/attr/dobj; active only through oprd/attr.
2. "When I was 20 years old I lived in Rome." → (user, age, 20) as the CURRENT age.
   _chain_copula_measure now files nothing for a copula heading a subordinated adverbial clause
   (a frame), and stamps temporal_status='past' on a Tense=Past copula measure; /ingest's scalar
   upsert never lets a past value retire the live one.
3. "Your sister is 30 years old." → (sister, age, 30), the key the user's own sister uses.
   A non-speaker possessor now files nothing.

Fresh-domain controls: "My uncle phones Ravi on Sundays" (active) vs "My uncle is called Ravi";
"If I were 30 …"; "Her cousin is 40 years old"; "I am 45 years old" still current.
"""
from __future__ import annotations

import datetime

import pytest

pytest.importorskip("spacy")

from src.extraction import linguistics as L  # noqa: E402
from tests.laneb_e2e_support import (  # noqa: E402
    attr_rows, e2e_fixture, module_flags_fixture, state,
)

_flags = module_flags_fixture()
e2e = e2e_fixture()
_REF = datetime.datetime(2026, 9, 29)


def _facts(s):
    return [(f.subject, f.rel_type, (f.object or "").lower(), getattr(f, "temporal_status", None))
            for f in L.derive_sentence_facts(s, _REF)]


@pytest.mark.parametrize("text,name", [
    ("My mother calls Anna every day.", "anna"),
    ("My uncle phones Ravi on Sundays.", "ravi"),            # fresh domain (naming-verb-free)
    ("My uncle calls Ravi on Sundays.", "ravi"),
])
def test_active_call_is_not_naming(text, name):
    got = _facts(text)
    assert not any(s == name and r in ("parent_of", "also_known_as", "sibling_of")
                   for (s, r, _o, _t) in got), f"#53.1: {got}"


@pytest.mark.parametrize("text,name,rel", [
    ("My mother is called Anna.", "anna", "parent_of"),
    ("My uncle is called Ravi.", "ravi", None),
])
def test_passive_naming_still_binds(text, name, rel):
    got = _facts(text)
    assert any(s == name and (rel is None or r == rel) for (s, r, _o, _t) in got), got


@pytest.mark.parametrize("text", [
    "When I was 20 years old I lived in Rome.",
    "If I were 30 years old I would retire.",
])
def test_subordinate_frame_files_no_age(text):
    assert not any(r == "age" for (_s, r, _o, _t) in _facts(text)), _facts(text)


def test_past_tense_age_is_stamped_past_and_present_is_not():
    past = [t for t in _facts("I was 20 years old.") if t[1] == "age"]
    assert past and all(t[3] == "past" for t in past), past
    now = [t for t in _facts("I am 45 years old.") if t[1] == "age"]
    assert now and all(t[3] != "past" for t in now), now


@pytest.mark.parametrize("text", [
    "Your sister is 30 years old.",
    "His sister is 30 years old.",
    "Her cousin is 40 years old.",                           # fresh domain
])
def test_non_speaker_possessor_files_nothing(text):
    assert not any(r == "age" for (_s, r, _o, _t) in _facts(text)), _facts(text)


def test_speaker_possessor_still_files():
    assert ("sister", "age", "30", None) in _facts("My sister is 30 years old.")


def test_e2e_past_age_never_overwrites_current(e2e):
    state(e2e, "I am 45 years old.")
    state(e2e, "When I was 20 years old I lived in Rome.")
    state(e2e, "I was 20 years old.")
    ages = {v for (e, a, v) in attr_rows(e2e) if e == e2e["user_id"] and a == "age"}
    assert ages == {"45"}, f"#53.2: {ages}"


# ═════════════════════════════════════════════════════════════════════════════
# #95 — a past-stamped scalar never writes (or locks) the live slot; each case on its own tenant
# ═════════════════════════════════════════════════════════════════════════════
e2e95a = e2e_fixture()
e2e95b = e2e_fixture()


def _ages(env):
    return {v for (e, a, v) in attr_rows(env) if e == env["user_id"] and a == "age"}


def test_e2e_95_past_age_then_present_age(e2e95a):
    from tests.laneb_e2e_support import ask, rows
    state(e2e95a, "I was 30 when I married.")
    assert "30" not in _ages(e2e95a), f"#95: past age written live: {_ages(e2e95a)}"
    hist = rows(e2e95a["schema"], "SELECT prior_value_text, cause FROM entity_attributes_history "
                                  "WHERE attribute = 'age'")
    assert ("30", "past_stated") in hist, f"#95: past age not kept as history: {hist}"
    state(e2e95a, "I am 34.")
    assert _ages(e2e95a) == {"34"}, f"#95: present age dropped/locked: {_ages(e2e95a)}"
    got = ask(e2e95a, "How old am I?")
    vals = {str(o) for (_s, r, o) in got if r == "age"}
    assert vals == {"34"}, f"#95: How old am I? → {got}"


def test_e2e_95_last_year_alone_leaves_no_live_age(e2e95b):
    state(e2e95b, "Last year I was 33.")
    assert not _ages(e2e95b), f"#95: a past-only statement became the live age: {_ages(e2e95b)}"
