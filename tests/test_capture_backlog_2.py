"""Lane I (capture backlog 2) — rendered-answer pins, phrase level, on fresh seats.

#111  A possessive-role question ("Where does my brother live?") that resolves to a third party
      gets the same third-party answer scoping as the named question ("Where does Pablo live?").

Each pin carries a fresh-domain control (different role / place vocabulary) so the fix is shown to
be grammar-driven, not a word list.
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

import src.api.main as M  # noqa: E402
from tests.laneb_e2e_support import (  # noqa: E402
    e2e_fixture, ingest_edges, module_flags_fixture, state,
)

_flags = module_flags_fixture()
e2e_111a = e2e_fixture()
e2e_111b = e2e_fixture()
e2e_111c = e2e_fixture()


def _prose(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return " / ".join(str(f.get("definition") or "") for f in (r.get("facts") or [])).lower()


# ── #111 ──────────────────────────────────────────────────────────────────────
def test_111_unit_possessive_role_question_is_third_party():
    me, a = "u-1", "a" * 36
    facts = [
        {"_subject_id": a, "_object_id": "b" * 36, "rel_type": "lives_in"},
        {"_subject_id": me, "_object_id": "t" * 36, "rel_type": "lives_in"},
        {"_subject_id": me, "_object_id": a, "rel_type": "sibling_of"},
    ]
    assert M._third_party_answer_strays(a, me, "Where does my brother live?", facts) == {1}
    # the speaker as an argument of the question keeps today's behaviour
    assert M._third_party_answer_strays(a, me, "Does my brother live near me?", facts) == set()
    assert M._third_party_answer_strays(a, me, "Do I live with my brother?", facts) == set()


def test_111_unit_speaker_anchored_possessed_role():
    """Two brothers → the anchor cannot narrow and stays the speaker; the speaker's own rows that
    connect to no role-holder still leave the answer."""
    me, p, j = "u-1", "p" * 36, "j" * 36
    facts = [
        {"_subject_id": me, "_object_id": "r" * 36, "rel_type": "lives_in"},
        {"_subject_id": me, "_object_id": p, "rel_type": "sibling_of"},
        {"_subject_id": me, "_object_id": j, "rel_type": "sibling_of"},
        {"_subject_id": p, "_object_id": "b" * 36, "rel_type": "lives_in"},
        {"_subject_id": j, "_object_id": "m" * 36, "rel_type": "lives_in"},
    ]
    assert M._third_party_answer_strays(me, me, "Where does my brother live?", facts) == {0}
    assert M._third_party_answer_strays(me, me, "Where do I live?", facts) == set()
    assert M._third_party_answer_strays(me, me, "Where does my car live?", facts) == set()
    # nothing but speaker rows → today's answer stands (never an empty reply by projection)
    assert M._third_party_answer_strays(me, me, "Where does my brother live?", facts[:1]) == set()


def _seat(env, sents):
    for t in sents:
        if isinstance(t, dict):
            ingest_edges(env, "Where does my brother live?", [t], source="unattested")
        else:
            state(env, t)


_HISTORY = ("I live in Regina.", "I used to live in Hamilton.", "I lived in Leeds.")


def test_111_e2e_my_brother_with_a_junk_type_sibling(e2e_111a):
    """The pre-prod seat shape: the speaker's residence history + the type-node sibling row a
    recall harvest once filed."""
    _seat(e2e_111a, _HISTORY + ("My brother Pablo lives in Bilbao.",
                                {"subject": "brother", "rel_type": "sibling_of", "object": "user",
                                 "fact_provenance": "user_stated", "confidence": 0.8}))
    p = _prose(e2e_111a, "Where does my brother live?")
    assert "bilbao" in p, p
    for w in ("regina", "hamilton", "leeds"):
        assert w not in p, p


def test_111_e2e_two_brothers(e2e_111c):
    _seat(e2e_111c, _HISTORY[:2] + ("My brother Pablo lives in Bilbao.",
                                    "My brother Juan lives in Madrid."))
    p = _prose(e2e_111c, "Where does my brother live?")
    assert "bilbao" in p and "madrid" in p, p
    assert "regina" not in p and "hamilton" not in p, p


def test_111_e2e_fresh_domain_genitive_sister(e2e_111b):
    _seat(e2e_111b, ("I live in Halifax.", "I used to live in Perth.",
                     "My sister's name is Iris.", "Iris lives in Tromso."))
    p = _prose(e2e_111b, "Where does my sister live?")
    assert "tromso" in p, p
    assert "halifax" not in p and "perth" not in p, p
