"""Lane I — #83: the possessor decides whose kin. "His sister Kate …" / "Tom's son Jake …" filed the
person as the USER's kin. Deriver pins + rendered-answer pin on a fresh seat; fresh-domain control
is the PROPN genitive possessor ("Tom's son Jake").
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

from tests.laneb_e2e_support import (  # noqa: E402
    e2e_fixture, fact_rows, module_flags_fixture, state,
)

_flags = module_flags_fixture()
e2e_83 = e2e_fixture()


def _derive(s):
    import src.extraction.linguistics as L
    import datetime
    return {(f.subject, f.rel_type, f.object)
            for f in (L.derive_sentence_facts(s, reference=datetime.datetime(2026, 10, 1)) or [])}


def _prose(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return " / ".join(str(f.get("definition") or "") for f in (r.get("facts") or [])).lower()


# ── #83 deriver ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("sent,name", [
    ("His sister Kate works at Google.", "kate"),
    ("Your brother Pablo lives in Bilbao.", "pablo"),
    ("Tom's son Jake plays hockey.", "jake"),        # fresh domain: a PROPN genitive possessor
])
def test_83_third_party_kin_is_not_the_users(sent, name):
    fs = _derive(sent)
    assert not any(s == name and o == "user" for s, r, o in fs), fs


def test_83_genitive_possessor_gets_the_kin_edge():
    assert ("jake", "child_of", "tom") in _derive("Tom's son Jake plays hockey.")


def test_83_speaker_kin_unchanged():
    assert ("pablo", "sibling_of", "user") in _derive("My brother Pablo lives in Bilbao.")


# ── rendered answer ─────────────────────────────────────────────────────────
def test_83_e2e_third_party_sister_is_not_mine(e2e_83):
    state(e2e_83, "My sister Ana lives in Lima.")
    state(e2e_83, "His sister Kate works at Google.")
    state(e2e_83, "Tom's son Jake plays hockey.")         # fresh domain
    rs = fact_rows(e2e_83)
    assert ("kate", "sibling_of", "user") not in rs and ("user", "sibling_of", "kate") not in rs, rs
    assert ("jake", "child_of", "user") not in rs and ("user", "parent_of", "jake") not in rs, rs
    p = _prose(e2e_83, "Who is my sister?")
    assert "ana" in p and "kate" not in p, p
    p = _prose(e2e_83, "Who is my son?")
    assert "jake" not in p, p
