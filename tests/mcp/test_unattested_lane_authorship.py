"""AUTHORSHIP-HONEST PROVENANCE - the unattested write lanes (2026-08-15).

Three AUTOMATIC lanes used to stamp whatever they captured as
fact_provenance="user_stated" at Class A - a machine side-effect claiming the human
said something they never (explicitly) said:

  1. recall intent-independent turn harvest,
  2. recall STATEMENT and CORRECTION intent diverts,
  3. the (dispatchable) store_context tool, whose captures return through the
     episodic re-mine.

Every one of those lanes is UNATTESTED: the model called recall_memory (or the
low-level store_context), not remember_facts / retract_fact - nothing attested the
capture. They now carry source="unattested" / attested=False end-to-end, and the
backend /ingest provenance router lands them llm_inferred at staged Class B - NEVER
user_stated, NEVER Class A, NEVER the Class-C 30-day clock.

WHAT MUST NOT CHANGE (pinned here on purpose):
  * an EXPLICIT remember_facts keeps source="mcp" -> user_stated -> Class A,
  * an EXPLICIT retract_fact keeps attested=True -> user_stated/Class-A supersede,
  * the discrimination is LANE attestation only - never client name, never text.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.mcp.server as srv
from src.mcp.server import (
    recall_memory_tool,
    remember_facts_tool,
    retract_fact_tool,
    _harvest_turn_facts,
)


def _empty_query_client():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"facts": [], "attributes": {}})
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    return client


def _harvest_spans_client(edges):
    def _post(url, json=None, **kwargs):
        r = MagicMock()
        r.raise_for_status = MagicMock()
        payload = {"edges": edges} if "harvest-spans" in url else {"facts": [], "attributes": {}}
        r.json = MagicMock(return_value=payload)
        return r
    client = AsyncMock()
    client.post = AsyncMock(side_effect=_post)
    return client


def _chat_client_patch():
    return patch.object(srv, "_client_class")


@pytest.mark.asyncio
async def test_recall_harvest_ingests_unattested():
    fake_ingest = AsyncMock(return_value={"status": "stored", "committed": 0})
    with patch.object(srv, "_maybe_intercept_slash", AsyncMock(return_value=None)), \
         patch.object(srv, "RECALL_INTENT_ROUTING", True), \
         patch.object(srv, "_classify_and_gate", AsyncMock(return_value=("QUERY", 0.9, {}))), \
         patch.object(srv, "_http_client", _harvest_spans_client(
             [{"subject": "user", "rel_type": "lives_in", "object": "fitzroy"}])), \
         patch.object(srv, "ingest_tool", fake_ingest), \
         _chat_client_patch() as fake_cc:
        fake_cc.current_client_is_coding_agent.return_value = False
        fake_cc.current_client_name.return_value = None
        await recall_memory_tool("what is my suburb? by the way I live in Fitzroy", "u1")

    fake_ingest.assert_awaited_once()
    assert fake_ingest.await_args.kwargs.get("source") == "unattested", (
        "the recall harvest is an automatic write - it must never claim user_stated")


@pytest.mark.asyncio
async def test_recall_harvest_is_not_gated_on_the_connection_name():
    """INVERTED 2026-08-17 (owner ruling). This asserted the harvest was SKIPPED when the client
    called itself a coding agent. That gate is gone and asserting it kept it alive.

    The name is chosen by the END USER when they configure the MCP connection, so a hard-coded
    dictionary of recognised names is a denylist against strings the user picked — anyone naming
    their connection something we did not anticipate got silently degraded capability. Owner:
    "I can call my mcp tool fuckhead and it doesn't matter, it just gets recorded as that's
    where it came from." The bearer decides what may be done; the name only labels the log.

    The authorship protection this file exists to pin is UNCHANGED and is what makes de-gating
    safe: the harvest lands source="unattested" for EVERY client, so nothing claims the human
    said it. That guarantee is structural and applies equally to every caller, rather than
    resting on recognising a name — which is strictly stronger than the gate it replaces.
    """
    for client_name in ("opencode", "fuckhead", None):
        fake_harvest = AsyncMock(return_value=0)
        with patch.object(srv, "_maybe_intercept_slash", AsyncMock(return_value=None)), \
             patch.object(srv, "_http_client", _empty_query_client()), \
             patch.object(srv, "_harvest_turn_facts", fake_harvest), \
             _chat_client_patch() as fake_cc:
            fake_cc.current_client_name.return_value = client_name
            await recall_memory_tool("anything", "u1")

        # Fires for every name, and always on the unattested lane.
        fake_harvest.assert_awaited_once_with(
            "anything", "u1", source="unattested"), client_name


@pytest.mark.asyncio
async def test_recall_statement_divert_marks_unattested():
    fake_remember = AsyncMock(return_value={"status": "stored", "committed": 1})
    with patch.object(srv, "_maybe_intercept_slash", AsyncMock(return_value=None)), \
         patch.object(srv, "RECALL_INTENT_ROUTING", True), \
         patch.object(srv, "_classify_and_gate", AsyncMock(return_value=("STATEMENT", 0.9, {}))), \
         patch.object(srv, "remember_facts_tool", fake_remember), \
         patch.object(srv, "_harvest_turn_facts", AsyncMock(return_value=0)), \
         patch.object(srv, "_http_client", _empty_query_client()), \
         _chat_client_patch() as fake_cc:
        fake_cc.current_client_is_coding_agent.return_value = False
        fake_cc.current_client_name.return_value = None
        await recall_memory_tool("my dog is Rex", "u1")

    fake_remember.assert_awaited_once()
    assert fake_remember.await_args.kwargs.get("attested") is False


@pytest.mark.asyncio
async def test_recall_correction_divert_marks_unattested():
    fake_retract = AsyncMock(return_value={"memory": "corrected"})
    with patch.object(srv, "_maybe_intercept_slash", AsyncMock(return_value=None)), \
         patch.object(srv, "RECALL_INTENT_ROUTING", True), \
         patch.object(srv, "_classify_and_gate", AsyncMock(return_value=("CORRECTION", 0.9, {}))), \
         patch.object(srv, "retract_fact_tool", fake_retract), \
         _chat_client_patch() as fake_cc:
        fake_cc.current_client_is_coding_agent.return_value = False
        fake_cc.current_client_name.return_value = None
        await recall_memory_tool("actually my dog is Rex not Rexx", "u1")

    fake_retract.assert_awaited_once()
    assert fake_retract.await_args.kwargs.get("attested") is False


@pytest.mark.asyncio
async def test_explicit_remember_facts_stays_mcp():
    captured = {}

    async def fake_retry(body, **kwargs):
        captured.update(body)
        return {"status": "stored", "committed": 1}, None

    with patch.object(srv, "_classify_and_gate", AsyncMock(return_value=("STATEMENT", 0.9, {}))), \
         patch.object(srv, "_episodic_capture", AsyncMock()) as fake_epi, \
         patch.object(srv, "_statement_extractor_route", AsyncMock(return_value="spine")), \
         patch.object(srv, "_http_client", _harvest_spans_client(
             [{"subject": "user", "rel_type": "lives_in", "object": "fitzroy"}])), \
         patch.object(srv, "_ingest_with_retry", fake_retry):
        await remember_facts_tool("I live in Fitzroy", "u1")

    assert captured.get("source") == "mcp"
    assert fake_epi.await_args.kwargs.get("source") == "mcp"


@pytest.mark.asyncio
async def test_unattested_remember_facts_threads_unattested():
    captured = {}

    async def fake_retry(body, **kwargs):
        captured.update(body)
        return {"status": "stored", "staged": 1}, None

    with patch.object(srv, "_classify_and_gate", AsyncMock(return_value=("STATEMENT", 0.9, {}))), \
         patch.object(srv, "_episodic_capture", AsyncMock()) as fake_epi, \
         patch.object(srv, "_statement_extractor_route", AsyncMock(return_value="spine")), \
         patch.object(srv, "_http_client", _harvest_spans_client(
             [{"subject": "user", "rel_type": "lives_in", "object": "fitzroy"}])), \
         patch.object(srv, "_ingest_with_retry", fake_retry):
        await remember_facts_tool("I live in Fitzroy", "u1", attested=False)

    assert captured.get("source") == "unattested"
    assert fake_epi.await_args.kwargs.get("source") == "unattested"


@pytest.mark.asyncio
async def test_unattested_defer_carries_source_through_queue():
    captured = {}

    async def fake_enqueue(text, user_id, *, ingest_source="mcp"):
        captured["ingest_source"] = ingest_source
        return True

    with patch.object(srv, "_classify_and_gate", AsyncMock(return_value=("STATEMENT", 0.9, {}))), \
         patch.object(srv, "_episodic_capture", AsyncMock()), \
         patch.object(srv, "_defer_statement_extraction", fake_enqueue), \
         patch.object(srv, "_statement_extractor_route", AsyncMock(
             side_effect=srv.BrainUnavailable("/internal/ingest-route", "ConnectError", 2))):
        # The defer point in the open core: the brain cannot say WHICH extractor to run, so the
        # raw turn is handed to the deferred drain — with the turn's authorship intact.
        result = await remember_facts_tool("I live in Fitzroy and it is lovely", "u1", attested=False)

    assert captured["ingest_source"] == "unattested"
    assert result.get("deferred") is True


class _FakeAsyncClient:
    captured = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **k):
        _FakeAsyncClient.captured = (url, json)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"status": "corrected"})
        return resp


@pytest.mark.asyncio
async def test_explicit_retract_sends_attested_true():
    orig = srv.httpx.AsyncClient
    srv.httpx.AsyncClient = _FakeAsyncClient
    try:
        await retract_fact_tool("actually my dog is Rex not Rexx", "u1",
                                classified_intent="CORRECTION")
    finally:
        srv.httpx.AsyncClient = orig
    url, body = _FakeAsyncClient.captured
    assert "/retract/correct" in url
    assert body.get("attested") is True


@pytest.mark.asyncio
async def test_unattested_retract_sends_attested_false():
    orig = srv.httpx.AsyncClient
    srv.httpx.AsyncClient = _FakeAsyncClient
    try:
        await retract_fact_tool("actually my dog is Rex not Rexx", "u1",
                                classified_intent="CORRECTION", attested=False)
    finally:
        srv.httpx.AsyncClient = orig
    url, body = _FakeAsyncClient.captured
    assert "/retract/correct" in url
    assert body.get("attested") is False


@pytest.mark.asyncio
async def test_harvest_helper_default_is_mcp():
    fake_ingest = AsyncMock(return_value={"status": "stored"})
    with patch.object(srv, "_http_client", _harvest_spans_client(
            [{"subject": "user", "rel_type": "lives_in", "object": "fitzroy"}])), \
         patch.object(srv, "ingest_tool", fake_ingest):
        await _harvest_turn_facts("I live in Fitzroy", "u1")

    assert fake_ingest.await_args.kwargs.get("source") == "mcp"
