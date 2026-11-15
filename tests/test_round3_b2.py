"""Round 3 (lane B, b2) — the two rendered failures from the pre-prod smoke on seat 843d39dc.

1. "My brother Pablo lives in Bilbao." → "Where does my brother live?" rendered the sibling rows but
   never Bilbao. Root cause (pre-prod logs): recall's possessive-presupposition harvest of the QUESTION
   kept (brother, sibling_of, user), /ingest bound the bare role word to the alias holder — the
   instance_of TYPE node "brother" — so the user gained a second "sibling" (the shelf), the
   "my brother" walk saw two fillers and deferred, and the answer fell back to the speaker. Same lane
   minted "Dad" from "Who is my dad?" ("You are the child of Dad").
   Fix: (a) the presupposition lane drops an unnamed person-role endpoint (a reference, never a person
   to mint); (b) the possessive-rel resolver drops a TYPE-node filler while a member filler remains and
   narrows several fillers by their instance_of rung on the asked role word (the read the #96 alias
   leg used to give, without making the role the display name).
2. "Where does Pablo live?" rendered the answer and then the speaker's own residences, age and a
   sister's birth date: the family-taxonomy walk reached every member and the aspect/measure topic-gate
   exemptions admitted their rows. Fix: a named third-party question (anchor ≠ speaker, no first person)
   keeps only rows touching the anchor among the speaker's rows and other entities' scalars.

Pins are on the RENDERED prose (each fact's ``definition``), phrase level.
"""
from __future__ import annotations

import types

import pytest

pytest.importorskip("spacy")

import src.api.main as M  # noqa: E402
from tests.laneb_e2e_support import (  # noqa: E402
    e2e_fixture, fact_rows, ingest_edges, module_flags_fixture, state,
)

_flags = module_flags_fixture()
e2e_a = e2e_fixture()
e2e_b = e2e_fixture()
e2e_c = e2e_fixture()
e2e_d = e2e_fixture()
e2e_e = e2e_fixture()


def _prose(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return " / ".join(str(f.get("definition") or "") for f in (r.get("facts") or [])).lower()


def _recall_harvest(env, q):
    """What the MCP recall lane does with the user's question: harvest it UNATTESTED."""
    return state(env, q, source="unattested")


# ── unit: the two predicates ──────────────────────────────────────────────────
def test_unnamed_person_role_endpoint_detected():
    f = types.SimpleNamespace
    assert M._presupposed_endpoint_is_unnamed_role(f(subject="brother", object="user"))
    assert M._presupposed_endpoint_is_unnamed_role(f(subject="dad", object="user"))
    assert not M._presupposed_endpoint_is_unnamed_role(f(subject="user", object="sony a7r iv"))
    assert not M._presupposed_endpoint_is_unnamed_role(f(subject="user", object="camera"))
    assert not M._presupposed_endpoint_is_unnamed_role(f(subject="pablo", object="user"))


def test_third_party_strays_only_for_named_third_party_questions():
    me, a, h = "u-1", "a" * 36, "h" * 36
    facts = [
        {"_subject_id": a, "_object_id": "b" * 36, "rel_type": "lives_in"},
        {"_subject_id": me, "_object_id": "t" * 36, "rel_type": "lives_in"},
        {"_subject_id": me, "rel_type": "age", "source": "attributes"},
        {"_subject_id": h, "rel_type": "born_on", "source": "attributes"},
        {"_subject_id": me, "_object_id": a, "rel_type": "sibling_of"},
        {"_subject_id": h, "_object_id": me, "rel_type": "parent_of"},
        # a descended node's own measure, reached through the asked entity (rs13 octopus): kept
        {"_subject_id": "x" * 36, "rel_type": "measure", "source": "attributes"},
    ]
    assert M._third_party_answer_strays(a, me, "Where does Pablo live?", facts) == {1, 2, 3, 5}
    # #111: a possessive-role question is third-party too ("my" only names whose brother)
    assert M._third_party_answer_strays(a, me, "Where does my brother live?", facts) == {1, 2, 3, 5}
    assert M._third_party_answer_strays(a, me, "Do I live near Pablo?", facts) == set()
    assert M._third_party_answer_strays(me, me, "Where does Pablo live?", facts) == set()


# ── 1 + 2: the smoke sequence ─────────────────────────────────────────────────
def test_e2e_my_brother_reaches_bilbao_after_the_recall_harvest(e2e_a):
    for t in ("I live in Toronto.", "I am 34.", "My brother Pablo lives in Bilbao.",
              "Helen lives in Paris."):
        state(e2e_a, t)
    _recall_harvest(e2e_a, "Where does my brother live?")
    assert ("user", "sibling_of", "brother") not in fact_rows(e2e_a), fact_rows(e2e_a)
    p = _prose(e2e_a, "Where does my brother live?")
    assert "bilbao" in p, p
    p = _prose(e2e_a, "Where does Pablo live?")
    assert "pablo lives in bilbao" in p, p
    assert "brother lives in" not in p, p
    assert "you live in" not in p, p
    assert "you are 34" not in p, p
    assert "paris" not in p, p


def test_e2e_type_node_already_a_sibling_does_not_steal_my_brother(e2e_b):
    """Seats that already carry the junk row (pre-prod 843d39dc does): the walk must still reach
    the named brother."""
    state(e2e_b, "My brother Pablo lives in Bilbao.")
    ingest_edges(e2e_b, "Where does my brother live?",
                 [{"subject": "brother", "rel_type": "sibling_of", "object": "user",
                   "fact_provenance": "user_stated", "confidence": 0.8}], source="unattested")
    p = _prose(e2e_b, "Where does my brother live?")
    assert "bilbao" in p, p
    assert "pablo" in p, p


def test_e2e_who_is_my_dad_mints_no_dad(e2e_c):
    state(e2e_c, "My dad calls me Champ.")
    _recall_harvest(e2e_c, "Who is my dad?")
    rs = fact_rows(e2e_c)
    assert not any(s in ("dad", "my dad") and r == "parent_of" for s, r, o in rs), rs
    p = _prose(e2e_c, "Who is my dad?")
    assert "child of dad" not in p, p


def test_e2e_two_brothers_after_the_recall_harvest(e2e_d):
    state(e2e_d, "My brother Pablo lives in Bilbao.")
    state(e2e_d, "My brother Juan lives in Madrid.")
    _recall_harvest(e2e_d, "Where does my brother live?")
    p = _prose(e2e_d, "Where does my brother live?")
    assert "pablo lives in bilbao" in p and "juan lives in madrid" in p, p
    assert "brother lives in" not in p, p
    p = _prose(e2e_d, "Where does Pablo live?")
    assert "pablo lives in bilbao" in p and "madrid" not in p, p


def test_e2e_brother_and_sister_after_the_recall_harvest(e2e_e):
    state(e2e_e, "My brother Pablo lives in Bilbao.")
    state(e2e_e, "My sister Ana lives in Lyon.")
    _recall_harvest(e2e_e, "Where does my brother live?")
    _recall_harvest(e2e_e, "Where does my sister live?")
    p = _prose(e2e_e, "Where does my brother live?")
    assert "pablo lives in bilbao" in p and "lyon" not in p, p
    p = _prose(e2e_e, "Where does my sister live?")
    assert "ana lives in lyon" in p and "bilbao" not in p, p
    p = _prose(e2e_e, "Where does Ana live?")
    assert "ana lives in lyon" in p and "sister lives in" not in p, p
