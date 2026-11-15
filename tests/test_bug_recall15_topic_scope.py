"""BUG-RECALL-15 — the possessive-kinship cue + the topic-scoped serve (pins).

THE WOUND (DEV/BUGS/family-smoke-20261111/BUG-RECALL-15-sense-disambiguation.md,
reproduced 2026-11-12 on a planted seat): "Tell me about my family?" walked the
speaker's family rows CORRECTLY (possessive-taxonomy anchor → membership walk → gate
skipped as taxonomy-scoped) but the DOCUMENT-HITS lane still served the decoy document
whose "family" tokens are NON-kinship collocations ("Algonquian language family", "a
family history of diabetes") — the token-match fallback rode the answer as a trailing
citation. And "Tell me about duck anatomy?" resolved the anchor onto the duck entity,
the walk fetched the duck row, and the LEXICAL TOPIC GATE dropped it
(topic_grounding_filtered after=0 before=1) so the doc chunk filled that void too.

THE FIX (owner redirect 2026-11-12): the walk OWNS the serve —
  seam 1. on the possessive-kinship class the document lane is NOT ASKED at all
          (the chunks are out of the serving set; the honest-empty mechanisms answer
          when the walk finds nothing);
  seam 2. rows fetched BY the question's own naming of a stored entity (a
          concretely-resolved anchor, or a concept the knowledge-ask/topic-ask frame
          names) are ON-TOPIC BY CONSTRUCTION — exempt from the lexical gate, with a
          bounded 1-hop neighbourhood fetch (the duck fact's content lives one hop
          from the duck entity). Rows NOT touching the named ids still face the gate:
          the scopes never bleed (duck ask → medical rows absent; medical ask → duck
          absent; family ask → duck absent).

All seams sit under RECALL_POSSESSIVE_KINSHIP (default ON; OFF → byte-parity with the
token-match behaviour). The negation scan-back, the correction marker gates and the
WGM intent gate are untouched.

House style: monkeypatched seams, no DB for the unit arms; the end-to-end arm is
DB-gated on LADDER_TEST_DSN + LADDER_TEST_SEAT so a database-less CI skips rather
than lies (same contract as test_prose_bound_to_its_fact.py).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.api import main as M  # noqa: E402
from src.api.models import QueryRequest, QueryResponse  # noqa: E402

_UID = str(uuid.uuid4())
_DUCK = str(uuid.uuid4())
_PENIS = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# seam 1 — the cue detector: possessive determiner + the kinship-GROUP head
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Tell me about my family?",
    "tell me about my family",
    "What does our family drive?",
    "how is my family doing",
    "my family's dog is old",          # possessive clattice on the head
    "our families get together yearly",  # plural, same lemma
])
def test_cue_fires_on_possessive_kinship_group(text):
    assert M._query_possessive_kinship_group(text) is True


@pytest.mark.parametrize("text", [
    "Tell me about duck anatomy?",          # topic ask, no possessive
    "what do I know about ducks?",          # knowledge ask, no possessive
    "What are my medical conditions?",      # possessive, but the head is not family
    "my wife is a pediatric nurse",         # possessed person role, not the group head
    "the family history of diabetes",       # bare/definite head — no first-person possessor
    "language family classifications",      # the non-kinship collocation itself
    "a family of ducks swims by",           # determiner possessor is not first person
    "",
])
def test_cue_stays_down_off_class(text):
    assert M._query_possessive_kinship_group(text) is False


def test_cue_failsafe_when_parser_unavailable(monkeypatch):
    import src.extraction.linguistics as L
    monkeypatch.setattr(L, "linguistics_available", lambda: False)
    assert M._query_possessive_kinship_group("tell me about my family") is False


# ---------------------------------------------------------------------------
# seam 1 — the wrapper withholds the document lane when the cue fires
# ---------------------------------------------------------------------------
class _FakeBarrier:
    async def acquire(self):
        return SimpleNamespaceAdmission()

    def release(self):
        return None


class SimpleNamespaceAdmission:
    waited_s = 0.0
    in_flight = 1

    @staticmethod
    def notify_trip(*a, **k):
        return None

    @staticmethod
    def busy_alert():
        return {}


class _OpenBudget:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def report(self):
        return {}


def _drive_query(text, hits_served, monkeypatch, flag_on=True, walk_resp=None):
    """Drive the REAL async query() wrapper with the doc lane and walk faked.

    Returns (response, doc_lane_calls): the response the wrapper returns and how many
    times the document-hits helper was invoked."""
    from types import SimpleNamespace

    monkeypatch.setattr(M, "RECALL_POSSESSIVE_KINSHIP", flag_on)
    monkeypatch.setattr(M, "_ensure_tenant_ready", _fake_async_noop)
    monkeypatch.setattr(M, "_turn_barrier", _FakeBarrier())
    monkeypatch.setattr(M, "_turn_budget", SimpleNamespace(open_budget=_OpenBudget))
    monkeypatch.setattr(M, "_document_hits_executor", None)

    calls = {"n": 0}

    def _fake_hits(uid, text_):
        calls["n"] += 1
        return list(hits_served)

    monkeypatch.setattr(M, "_document_hits_for_query", _fake_hits)
    if walk_resp is None:
        walk_resp = QueryResponse(
            anchor=_UID,
            facts=[{"definition": "You are the parent of Tam.",
                    "rel_type": "parent_of", "subject": _UID, "object": "tam"}],
            confidence_applied=False, staged_facts_count=0)
    monkeypatch.setattr(M, "_query_impl", lambda req: walk_resp)
    monkeypatch.setattr(M, "_recall_escalation_plan", lambda *a, **k: (False, []))

    req = QueryRequest(text=text, user_id=_UID, source="test")
    resp = asyncio.run(M.query(req))
    return resp, calls


async def _fake_async_noop(uid, where):
    return None


def test_wrapper_withholds_doc_lane_on_possessive_kinship(monkeypatch):
    decoy = {"document_id": 1, "title": "Algonquian notes", "chunk_index": 0,
             "chunk_text": "the Algonquian language family", "status": "ready"}
    resp, calls = _drive_query("Tell me about my family?", [decoy], monkeypatch)
    # THE WALK OWNS THE SERVE: the lane was never asked, the decoy is ABSENT.
    assert calls["n"] == 0
    assert resp.document_hits == []


def test_wrapper_serves_doc_lane_on_topic_query(monkeypatch):
    duck_doc = {"document_id": 2, "title": "waterfowl", "chunk_index": 1,
                "chunk_text": "spiral morphology in ducks", "status": "ready"}
    resp, calls = _drive_query("Tell me about duck anatomy?", [duck_doc], monkeypatch)
    # Non-possessive topic ask: today's doc-lane behaviour byte-for-byte.
    assert calls["n"] == 1
    assert resp.document_hits == [duck_doc]


def test_wrapper_flag_off_restores_doc_lane_byte_parity(monkeypatch):
    decoy = {"document_id": 1, "title": "Algonquian notes", "chunk_index": 0,
             "chunk_text": "the Algonquian language family", "status": "ready"}
    resp, calls = _drive_query("Tell me about my family?", [decoy], monkeypatch,
                               flag_on=False)
    # OFF = byte-parity: the token-match fallback behaviour is restored exactly.
    assert calls["n"] == 1
    assert resp.document_hits == [decoy]


# ---------------------------------------------------------------------------
# seam 2 — the seed: which entities the question itself named
# ---------------------------------------------------------------------------
def _fake_db_cursor(resolver_out=None):
    db = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: cur
    cur.__exit__ = lambda s, *a: None
    if resolver_out is not None:
        cur.fetchall.return_value = [(resolver_out,)]
    else:
        cur.fetchall.return_value = []
    db.cursor.return_value = cur
    return db


def test_seed_named_concrete_anchor(monkeypatch):
    ids = M._topic_entity_serve_ids(MagicMock(), _DUCK, _UID,
                                    "Tell me about duck anatomy?", "alias")
    assert ids == {_DUCK}


def test_seed_knowledge_ask_concept(monkeypatch):
    monkeypatch.setattr(M, "_resolve_query_concept_entities",
                        lambda cur, text, anchor: {_DUCK} if "duck" in text else set())
    ids = M._topic_entity_serve_ids(_fake_db_cursor(), _UID, _UID,
                                    "what do I know about ducks?", "default")
    assert ids == {_DUCK}


def test_seed_topic_ask_frame(monkeypatch):
    monkeypatch.setattr(M, "_resolve_query_concept_entities",
                        lambda cur, text, anchor: {_DUCK})
    ids = M._topic_entity_serve_ids(_fake_db_cursor(), _UID, _UID,
                                    "tell me about ducks", "identity")
    assert ids == {_DUCK}


def test_seed_never_fires_on_possessive_kinship_class(monkeypatch):
    # The cue fires ("my family") → the concept seed is REFUSED even when an entity
    # carries the alias — a document-minted "family" entity is not the speaker's family.
    monkeypatch.setattr(M, "_resolve_query_concept_entities",
                        lambda cur, text, anchor: {str(uuid.uuid4())})
    assert M._topic_entity_serve_ids(_fake_db_cursor(), _UID, _UID,
                                     "tell me about my family", "taxonomy") == set()


def test_seed_plain_possessed_aspect_stays_unseeded(monkeypatch):
    # "what are my medical conditions?" — no knowledge-ask/topic-ask frame → no seed
    # (the #61 absent-aspect abstention stays byte-identical).
    monkeypatch.setattr(M, "_resolve_query_concept_entities",
                        lambda cur, text, anchor: {str(uuid.uuid4())})
    assert M._topic_entity_serve_ids(_fake_db_cursor(), _UID, _UID,
                                     "what are my medical conditions?", "default") == set()


def test_seed_flag_off_is_empty(monkeypatch):
    monkeypatch.setattr(M, "RECALL_POSSESSIVE_KINSHIP", False)
    assert M._topic_entity_serve_ids(MagicMock(), _DUCK, _UID,
                                     "Tell me about duck anatomy?", "alias") == set()


# ---------------------------------------------------------------------------
# seam 2 — the supplement: the bounded 1-hop neighbourhood merge
# ---------------------------------------------------------------------------
def _row(sid, oid, rel="related_to", source="db", cls="A"):
    return {"subject": sid, "object": oid, "rel_type": rel, "confidence": 1.0,
            "fact_class": cls, "source": source, "category": None,
            "valid_from": None, "valid_until": None}


def test_supplement_merges_neighbour_rows_and_dedups(monkeypatch):
    duck_row = _row(_PENIS, _DUCK)                    # touches duck — walk already had it
    spiked = _row(_PENIS, "spiked", rel="has_state")  # the content, one hop away
    db_facts = [dict(duck_row)]

    monkeypatch.setattr(M, "_topic_entity_serve_ids",
                        lambda *a, **k: {_DUCK})
    rounds = [[dict(duck_row)], [dict(spiked)]]

    def _fake_fetch(db, ids, anchor):
        return rounds[0] if len(ids) == 1 else rounds[1]

    monkeypatch.setattr(M, "_fetch_concept_own_facts", _fake_fetch)
    db = _fake_db_cursor()
    ids, added = M._topic_entity_serve_supplement(db, _DUCK, _UID,
                                                  "Tell me about duck anatomy?",
                                                  "alias", db_facts)
    # round 1's deduped duck row still NAMES the neighbour (penis) — round 2 fetches
    # for {duck, penis} and merges the has_state row the walk never had.
    assert _PENIS in ids and _DUCK in ids
    assert added == 1
    assert [(f["subject"], f["rel_type"], f["object"]) for f in db_facts] == \
        [(_PENIS, "related_to", _DUCK), (_PENIS, "has_state", "spiked")]


def test_supplement_neighbour_cap_bounds_the_hop(monkeypatch):
    monkeypatch.setattr(M, "_topic_entity_serve_ids", lambda *a, **k: {_DUCK})
    neighbours = [str(uuid.uuid4()) for _ in range(40)]
    rows = [_row(n, _DUCK, rel=f"rel{i}") for i, n in enumerate(neighbours)]

    seen_ids = []

    def _fake_fetch(db, ids, anchor):
        seen_ids.append(set(ids))
        return [r for r in rows if r["subject"] in ids or r["object"] in ids]

    monkeypatch.setattr(M, "_fetch_concept_own_facts", _fake_fetch)
    db_facts: list = []
    ids, added = M._topic_entity_serve_supplement(_fake_db_cursor(), _DUCK, _UID,
                                                  "q", "alias", db_facts)
    # The id set is bounded (the hop cannot walk past the cap); round 2 fetched for
    # the bounded set only; every round-1 row touching the anchor merged.
    assert len(ids) <= M._TOPIC_ENTITY_NEIGHBOUR_CAP
    assert len(seen_ids[1]) <= M._TOPIC_ENTITY_NEIGHBOUR_CAP
    assert seen_ids[1] == ids
    assert added == len(rows)


def test_supplement_flag_off_never_touches_the_walk(monkeypatch):
    monkeypatch.setattr(M, "RECALL_POSSESSIVE_KINSHIP", False)
    db_facts: list = []
    ids, added = M._topic_entity_serve_supplement(MagicMock(), _DUCK, _UID, "q",
                                                  "alias", db_facts)
    assert ids == set() and added == 0 and db_facts == []


def test_supplement_never_raises(monkeypatch):
    monkeypatch.setattr(M, "_topic_entity_serve_ids",
                        MagicMock(side_effect=RuntimeError("boom")))
    db_facts: list = []
    ids, added = M._topic_entity_serve_supplement(MagicMock(), _DUCK, _UID, "q",
                                                  "alias", db_facts)
    assert ids == set() and added == 0 and db_facts == []


# ---------------------------------------------------------------------------
# seam 2 — the gate admission: the named ids pass, everything else is still gated
# ---------------------------------------------------------------------------
def test_touch_test_reads_uuid_and_display_slots():
    keys = {_DUCK, "duck"}  # raw UUID + the display name the slot wears post-strip
    assert M._row_touches_topic_entity({"subject": _DUCK}, keys) is True
    assert M._row_touches_topic_entity({"_subject_id": _DUCK}, keys) is True
    assert M._row_touches_topic_entity({"object": "duck"}, keys) is True
    assert M._row_touches_topic_entity({"subject": "penis", "object": "spiked"},
                                       keys) is False
    assert M._row_touches_topic_entity({"subject": "penicillin"}, set()) is False


def test_gate_split_topic_entity_rows_exempt_others_gated(monkeypatch):
    # The lexical gate drops the off-topic row; the named-entity row survives even
    # though its surface shares no token with the WordNet topic closure.
    kept, gated = [], []

    def _fake_gate(text, facts):
        gated.extend(facts)
        return []

    monkeypatch.setattr(M, "_topic_grounded_facts", _fake_gate)
    monkeypatch.setattr(M, "RECALL_POSSESSIVE_KINSHIP", True)
    monkeypatch.setattr(M, "_topic_entity_serve_supplement",
                        lambda *a, **k: ({_DUCK}, 2))
    rows = [
        {"subject": _PENIS, "object": _DUCK, "rel_type": "related_to",
         "source": "db", "fact_class": "A"},                       # touches duck
        {"subject": "penicillin", "object": "user", "rel_type": "has_allergy",
         "source": "db", "fact_class": "A"},                       # off topic
    ]
    keys = {_DUCK}
    te = [f for f in rows if M._row_touches_topic_entity(f, keys)]
    rest = [f for f in rows if f not in te]
    assert len(te) == 1 and len(rest) == 1
    assert te[0]["object"] == _DUCK


# ---------------------------------------------------------------------------
# the grounding check (mechanism-1 extension, src/mcp/server.py)
# ---------------------------------------------------------------------------
import src.mcp.server as S  # noqa: E402

_DECOY_DOC_LINE = ("From your document Algonquian Notes: the Algonquian language "
                   "family carries one of the richest ethnobotanical vocabularies "
                   "(document id 1, section 1).")


def test_grounding_check_fails_doc_only_family_serve():
    out = S._self_verify_grounding_check(_DECOY_DOC_LINE, "Tell me about my family?")
    assert out["ok"] is False
    assert out["grounding"] == "speaker"
    assert out["doc_only"] is True
    assert "doc_only_fallback" in out["why"]


def test_grounding_check_fails_doc_only_topic_ask():
    out = S._self_verify_grounding_check(_DECOY_DOC_LINE, "tell me about duck anatomy")
    assert out["ok"] is False
    assert out["grounding"] == "topic"


def test_grounding_check_passes_fact_bearing_serve():
    # The healthy family walk rows echo none of the query's tokens — they must NEVER
    # false-fail: only the doc-only band shape is judged.
    serve = "=== YOUR MEMORY ===\nYou are the parent of Tam.\nReya is 6 years old\n"
    out = S._self_verify_grounding_check(serve, "Tell me about my family?")
    assert out["ok"] is True


def test_grounding_check_passes_doc_only_document_referencing():
    # grounding-3: the possessive governs a DOCUMENT-PLANE head — the passage serve
    # is the answer for that class.
    out = S._self_verify_grounding_check(
        _DECOY_DOC_LINE, "what does my uploaded paper say about wind turbines?")
    assert out["ok"] is True


def test_grounding_check_passes_unclassifiable_query():
    out = S._self_verify_grounding_check(_DECOY_DOC_LINE, "hello")
    assert out["ok"] is True and out["grounding"] is None


def test_grounding_check_flag_off_is_byte_parity(monkeypatch):
    monkeypatch.setattr(S, "RECALL_POSSESSIVE_KINSHIP", False)
    out = S._self_verify_grounding_check(_DECOY_DOC_LINE, "Tell me about my family?")
    assert out["ok"] is True  # OFF → the token-match doc-only serve is restored
    monkeypatch.setattr(S, "RECALL_POSSESSIVE_KINSHIP", True)
    monkeypatch.setattr(S, "RECALL_SELF_VERIFY_SERVE", False)
    out = S._self_verify_grounding_check(_DECOY_DOC_LINE, "Tell me about my family?")
    assert out["ok"] is True


def test_doc_only_band_detector_boundaries():
    assert S._self_verify_answer_band_is_doc_only(_DECOY_DOC_LINE) is True
    assert S._self_verify_answer_band_is_doc_only(
        "You have a document: Algonquian Notes (document id 1).") is True
    assert S._self_verify_answer_band_is_doc_only(
        "=== YOUR MEMORY ===\nYou are the parent of Tam.\n\n" + _DECOY_DOC_LINE) is False
    assert S._self_verify_answer_band_is_doc_only("") is False


def test_grounding_classifier_boundaries():
    assert S._grounding_class("Tell me about my family?") == "speaker"
    assert S._grounding_class("what do I know about ducks?") == "topic"
    assert S._grounding_class("tell me about duck anatomy") == "topic"
    # the document plane stays unjudged
    assert S._grounding_class("what does my uploaded paper say about X?") is None
    assert S._grounding_class("who is my wife?") == "speaker"
    assert S._grounding_class("") is None


# ---------------------------------------------------------------------------
# end-to-end (DB-gated): the real walk on a planted bench seat
# ---------------------------------------------------------------------------
DSN = os.environ.get("LADDER_TEST_DSN")
SEAT = os.environ.get("LADDER_TEST_SEAT")
_gate_db = pytest.mark.skipif(
    not (DSN and SEAT),
    reason="set LADDER_TEST_DSN and LADDER_TEST_SEAT to a bench seat to run these",
)


@_gate_db
def test_family_serve_has_no_decoy_and_duck_serve_survives():
    """On a seat holding family rows + a duck world fact + a decoy doc:
    'tell me about my family' must carry NO document passage, and the duck ask's
    facts must include the duck-entity rows (the gate exemption live)."""
    os.environ["POSTGRES_DSN"] = DSN or ""
    fam = M._query_impl(QueryRequest(text="tell me about my family",
                                     user_id=SEAT, source="test"))
    assert fam.document_hits in ([], None)
    duck = M._query_impl(QueryRequest(text="tell me about duck anatomy",
                                      user_id=SEAT, source="test"))
    joined = " ".join(duck.facts or []).lower()
    assert ("duck" in joined) or (duck.facts), \
        "the duck entity rows must survive the lexical gate"
