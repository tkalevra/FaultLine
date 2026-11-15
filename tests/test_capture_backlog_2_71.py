"""Lane I — #71: a named possessed thing. "My dog's name is Biscuit." filed (biscuit, related_to,
user) and laddered the NAME as a class; "My dog Biscuit is 4 years old." landed but "How old is my
dog?" answered nothing. Deriver pins + rendered-answer pins; fresh-domain controls boat/cat.
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

from tests.laneb_e2e_support import (  # noqa: E402
    e2e_fixture, fact_rows, module_flags_fixture, state,
)

_flags = module_flags_fixture()
e2e_71a = e2e_fixture()
e2e_71b = e2e_fixture()


def _derive(s):
    import src.extraction.linguistics as L
    import datetime
    return {(f.subject, f.rel_type, f.object)
            for f in (L.derive_sentence_facts(s, reference=datetime.datetime(2026, 10, 1)) or [])}


def _prose(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return " / ".join(str(f.get("definition") or "") for f in (r.get("facts") or [])).lower()


# ── #71 deriver ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("sent,name,noun", [
    ("My dog's name is Biscuit.", "biscuit", "dog"),
    ("My boat's name is Osprey.", "osprey", "boat"),   # fresh domain
])
def test_71_genitive_name_files_an_instance(sent, name, noun):
    fs = _derive(sent)
    assert (name, "instance_of", noun) in fs, fs
    assert any(s == "user" and o == name and r in ("owns", "has_pet") for s, r, o in fs), fs
    assert (name, "related_to", "user") not in fs, fs


def test_71_kin_genitive_name_unchanged():
    assert ("diane", "parent_of", "user") in _derive("My mother's name is Diane.")


# ── rendered answers ──────────────────────────────────────────────────────────
def test_71_e2e_whats_my_dogs_name(e2e_71a):
    state(e2e_71a, "My dog's name is Biscuit.")
    rs = fact_rows(e2e_71a)
    assert not any(s == "biscuit" and r == "subclass_of" for s, r, o in rs), rs
    assert "biscuit" in _prose(e2e_71a, "What's my dog's name?")
    state(e2e_71a, "My boat's name is Osprey.")          # fresh domain
    assert "osprey" in _prose(e2e_71a, "What is my boat's name?")


def test_71_e2e_how_old_is_my_dog(e2e_71b):
    state(e2e_71b, "My dog Biscuit is 4 years old.")
    p = _prose(e2e_71b, "How old is my dog?")
    assert "biscuit is 4" in p, p
    assert "biscuit" in _prose(e2e_71b, "What's my dog's name?")
    state(e2e_71b, "My cat Mochi is 7 years old.")        # fresh domain (split-subject parse)
    p = _prose(e2e_71b, "How old is my cat?")
    assert "mochi is 7" in p, p


def test_71_named_pet_appositive_binds_the_name():
    for sent, name, noun in (("My dog Biscuit is 4 years old.", "biscuit", "dog"),
                             ("My cat Mochi is 7 years old.", "mochi", "cat")):
        fs = _derive(sent)
        assert (name, "instance_of", noun) in fs and (name, "age", "4" if noun == "dog" else "7") in fs, fs




e2e_71c = e2e_fixture()


def test_137_another_pet_is_minted_not_a_rename(e2e_71c):
    """#137: a modifier on the possessed noun ("my OTHER dog", "my SECOND boat") introduces a
    distinct referent — both names survive and "What are my dogs' names?" lists both."""
    state(e2e_71c, "My dog's name is Biscuit.")
    state(e2e_71c, "Actually, my other dog's name is Rex.")
    p = _prose(e2e_71c, "What are my dogs' names?") + " / " + _prose(e2e_71c, "Tell me about my dogs.")
    assert "biscuit" in p and "rex" in p, p
    state(e2e_71c, "My boat's name is Osprey.")                    # fresh domain
    state(e2e_71c, "Sorry, my second boat's name is Heron.")
    p = _prose(e2e_71c, "Tell me about my boats.")
    assert "osprey" in p and "heron" in p, p
