"""MCP server regression tests — mock-based, no live API needed.

Scope: the MCP safety + UX contract in `src/mcp/server.py`.

  * `retract_fact` must fail toward DATA PRESERVATION. A model that mis-picks
    `retract_fact` for a plain STATEMENT (or a QUERY) must be redirected to
    `remember_facts`, never force-routed into a destructive delete. A genuine
    RETRACTION must still delete; a CORRECTION must still reach the
    NON-DESTRUCTIVE supersede — including at low confidence.
  * `no_ingest` returns are DIRECTIVES to the model, not status reports.
  * `recall_memory` appends a re-query hint on a NON-EMPTY recall only. The empty
    sentinel is a benchmark contract and must stay byte-identical.
"""

import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.mcp.tools import TOOLS
from src.mcp.server import (
    recall_memory_tool,
    remember_facts_tool,
    retract_fact_tool,
)
import src.mcp.server as _server_mod


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_http_client():
    return AsyncMock()


@pytest.fixture(autouse=True)
def reset_server_state():
    """Reset module-level state between tests to prevent leakage."""
    original_initialized = _server_mod._initialized
    original_provisioned = _server_mod._provisioned_users.copy()
    original_user_id = _server_mod.FAULTLINE_USER_ID
    yield
    _server_mod._initialized = original_initialized
    _server_mod._provisioned_users = original_provisioned
    _server_mod.FAULTLINE_USER_ID = original_user_id


# ── Mock helpers (the STATEMENT-redirect triangle) ───────────────────────────


def _fake_classify_http_client(intent: str, confidence: float = 0.95, gate: float = 0.70):
    """Build a mock `_http_client` whose /classify-intent + /confidence-gate return canned values.

    `retract_fact_tool` routes through the shared `_classify_and_gate` helper, which
    POSTs /classify-intent and GETs /confidence-gate on `_http_client` — so mocking
    that one seam covers the whole classification step.
    """
    client = MagicMock()
    classify_resp = MagicMock()
    classify_resp.raise_for_status = MagicMock()
    classify_resp.json.return_value = {"intent": intent, "confidence": confidence}
    gate_resp = MagicMock()
    gate_resp.raise_for_status = MagicMock()
    gate_resp.json.return_value = {"threshold": gate}
    client.post = AsyncMock(return_value=classify_resp)
    client.get = AsyncMock(return_value=gate_resp)
    return client


def _fake_retract_httpx_client(captured: dict):
    """Mock the standalone `httpx.AsyncClient` the /retract/correct path creates.

    `captured` is populated with the request json when post() fires, so a test can
    assert the destructive path was — or crucially was NOT — reached, and with what intent.
    """
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"retracted": True}
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    async def _capture(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return fake_resp

    fake_client.post = AsyncMock(side_effect=_capture)
    return fake_client


# ── Advertised tool set ──────────────────────────────────────────────────────


def test_tools_list_advertised_set():
    """The advertised set is 6 — distinct from the larger internal dispatch table."""
    assert len(TOOLS) == 6
    assert {t["name"] for t in TOOLS} == {
        "recall_memory",
        "remember_facts",
        "ingest_document",
        "learn_facts",
        "retract_fact",
        "forget_fact",
    }


# ── retract_fact: data preservation ──────────────────────────────────────────


async def test_retract_fact_statement_mispick_redirects_to_remember():
    """A STATEMENT mis-picked as retract_fact must redirect to remember_facts
    (data preservation), NOT fall back to RETRACTION (delete).

    Guards the destructive-data-loss case: the pre-fix behavior forced
    `intent = "RETRACTION"` for any STATEMENT, deleting data the user meant to
    store — measured on "my name is alice and I am 42", which destroyed real facts.
    """
    fake_remember = AsyncMock(return_value={"status": "stored", "committed": 1})
    retract_captured: dict = {}

    with patch("src.mcp.server._http_client",
               _fake_classify_http_client("STATEMENT", 0.92, 0.70)), \
         patch("src.mcp.server.remember_facts_tool", fake_remember), \
         patch("httpx.AsyncClient",
               return_value=_fake_retract_httpx_client(retract_captured)):
        result = await retract_fact_tool("my name is alice and I am 42", "user-alice")

    assert result == {"status": "stored", "committed": 1}
    assert fake_remember.await_count == 1
    fake_remember.assert_awaited_with("my name is alice and I am 42", "user-alice")
    # /retract/correct was NEVER reached — no destructive delete.
    assert "url" not in retract_captured


async def test_retract_fact_query_mispick_redirects_to_remember():
    """A QUERY mis-picked as retract_fact is the same tool-selection error as a
    STATEMENT and must likewise be redirected, never deleted."""
    fake_remember = AsyncMock(return_value={"status": "stored", "committed": 1})
    retract_captured: dict = {}

    with patch("src.mcp.server._http_client",
               _fake_classify_http_client("QUERY", 0.91, 0.70)), \
         patch("src.mcp.server.remember_facts_tool", fake_remember), \
         patch("httpx.AsyncClient",
               return_value=_fake_retract_httpx_client(retract_captured)):
        result = await retract_fact_tool("what is my name", "user-alice")

    assert result == {"status": "stored", "committed": 1}
    assert fake_remember.await_count == 1
    assert "url" not in retract_captured


async def test_retract_fact_retraction_still_routes_to_correct():
    """Regression guard on the OTHER arm: a genuine RETRACTION must still reach
    /retract/correct with intent=RETRACTION. The redirect must not break deletes."""
    fake_remember = AsyncMock(return_value={"status": "should_not_be_called"})
    retract_captured: dict = {}

    with patch("src.mcp.server._http_client",
               _fake_classify_http_client("RETRACTION", 0.96, 0.70)), \
         patch("src.mcp.server.remember_facts_tool", fake_remember), \
         patch("httpx.AsyncClient",
               return_value=_fake_retract_httpx_client(retract_captured)):
        result = await retract_fact_tool("forget that aurora is a computer", "user-alice")

    assert result == {"retracted": True}
    assert "/retract/correct" in retract_captured["url"]
    assert retract_captured["json"]["intent"] == "RETRACTION"
    # remember_facts must not be reached on a genuine RETRACTION.
    assert fake_remember.await_count == 0


async def test_retract_fact_low_confidence_retraction_still_deletes():
    """A LOW-confidence RETRACTION still deletes — the model explicitly asked to
    forget, and the gate is diagnostic-only here (the brain's intent drives the route)."""
    fake_remember = AsyncMock(return_value={"status": "should_not_be_called"})
    retract_captured: dict = {}

    with patch("src.mcp.server._http_client",
               _fake_classify_http_client("RETRACTION", 0.31, 0.70)), \
         patch("src.mcp.server.remember_facts_tool", fake_remember), \
         patch("httpx.AsyncClient",
               return_value=_fake_retract_httpx_client(retract_captured)):
        result = await retract_fact_tool("forget that aurora is a computer", "user-alice")

    assert result == {"retracted": True}
    assert retract_captured["json"]["intent"] == "RETRACTION"
    assert fake_remember.await_count == 0


async def test_retract_fact_correction_preserved_non_destructive():
    """A high-confidence CORRECTION must reach /retract/correct with intent=CORRECTION
    (non-destructive supersede), NOT be downgraded to RETRACTION and NOT be redirected
    to remember_facts. Preserves the is_correction pickup path."""
    fake_remember = AsyncMock(return_value={"status": "should_not_be_called"})
    retract_captured: dict = {}

    with patch("src.mcp.server._http_client",
               _fake_classify_http_client("CORRECTION", 0.95, 0.70)), \
         patch("src.mcp.server.remember_facts_tool", fake_remember), \
         patch("httpx.AsyncClient",
               return_value=_fake_retract_httpx_client(retract_captured)):
        result = await retract_fact_tool("my age is 45 not 42", "user-alice")

    assert result == {"retracted": True}
    assert "/retract/correct" in retract_captured["url"]
    assert retract_captured["json"]["intent"] == "CORRECTION"
    assert fake_remember.await_count == 0


async def test_retract_fact_low_confidence_correction_not_downgraded():
    """A LOW-confidence CORRECTION must reach /retract/correct with intent=CORRECTION,
    NOT be force-downgraded to RETRACTION (destructive delete).

    Data preservation wins: the brain said "supersede", and RETRACTION deletes. The old
    `if confidence < gate: intent = "RETRACTION"` line forced exactly that data loss.
    """
    fake_remember = AsyncMock(return_value={"status": "should_not_be_called"})
    retract_captured: dict = {}

    with patch("src.mcp.server._http_client",
               _fake_classify_http_client("CORRECTION", 0.40, 0.70)), \
         patch("src.mcp.server.remember_facts_tool", fake_remember), \
         patch("httpx.AsyncClient",
               return_value=_fake_retract_httpx_client(retract_captured)):
        result = await retract_fact_tool("my age is 45 not 42", "user-alice")

    assert result == {"retracted": True}
    assert "/retract/correct" in retract_captured["url"]
    # MUST be CORRECTION (non-destructive supersede), NOT RETRACTION (destructive delete).
    assert retract_captured["json"]["intent"] == "CORRECTION"
    assert fake_remember.await_count == 0


async def test_retract_fact_classify_failure_defaults_to_retraction():
    """RETRACT-SPECIFIC fail-safe: when classification fails outright, the default is
    RETRACTION — the model EXPLICITLY chose this tool — not the STATEMENT default
    `_classify_and_gate` uses for remember/recall."""
    fake_remember = AsyncMock(return_value={"status": "should_not_be_called"})
    retract_captured: dict = {}

    with patch("src.mcp.server._classify_and_gate",
               AsyncMock(side_effect=RuntimeError("classify down"))), \
         patch("src.mcp.server.remember_facts_tool", fake_remember), \
         patch("httpx.AsyncClient",
               return_value=_fake_retract_httpx_client(retract_captured)):
        result = await retract_fact_tool("forget that aurora is a computer", "user-alice")

    assert result == {"retracted": True}
    assert retract_captured["json"]["intent"] == "RETRACTION"
    assert fake_remember.await_count == 0


async def test_retract_fact_preclassified_intent_skips_the_redirect():
    """No redirect loop: when remember_facts_tool re-routes here it passes
    `classified_intent=...`, which skips the whole classification/redirect block."""
    fake_remember = AsyncMock(return_value={"status": "should_not_be_called"})
    retract_captured: dict = {}

    with patch("src.mcp.server.remember_facts_tool", fake_remember), \
         patch("httpx.AsyncClient",
               return_value=_fake_retract_httpx_client(retract_captured)):
        result = await retract_fact_tool(
            "aurora is not my dog", "user-alice", classified_intent="CORRECTION"
        )

    assert result == {"retracted": True}
    assert retract_captured["json"]["intent"] == "CORRECTION"
    assert fake_remember.await_count == 0


# ── no_ingest returns are directives, not status reports ─────────────────────


async def test_no_ingest_return_is_directive_not_a_status_report():
    """A sub-gate turn must tell the MODEL what to do, not report system state.

    Models parrot tool output verbatim, so a descriptive message made weak models
    announce "I couldn't store that" — breaking the silence-is-the-feature design
    (docs/MCP-SYSTEM-PROMPT.md). The CAUSE is logged for us; the return is a directive.

    Drives the REAL gate: a 2-word text fails `_passes_ingest_gate` (>= 3 words OR the
    self-identity regex), so no successful HTTP call is needed to reach the branch.
    """
    result = await remember_facts_tool("ok thanks", "user-alice")
    assert result["status"] == "no_ingest"
    assert result["message"] == "Respond normally; do not mention memory or storage."
    # It must NOT read as a failure the model can repeat back at the user.
    lowered = result["message"].lower()
    assert "error" not in lowered and "fail" not in lowered and "no confident" not in lowered


# ── recall re-query hint + the empty-sentinel benchmark contract ─────────────


async def test_non_empty_recall_appends_the_requery_hint(mock_http_client):
    """A compound message needs a second lookup; the hint is model-facing GUIDANCE.

    Deliberately NOT a backend-state claim — there is no `more_available` boolean, and
    inventing one at the transport would be unsound (brain-not-transport).

    ⚠️ FIXTURE SHAPE MATTERS: the field is `definition`, not `prose`. The render loop
    builds each line from fact["definition"] and SKIPS a fact whose definition is empty —
    a wrong fixture produces a test that asserts nothing while looking green.
    """
    classify_response = MagicMock()
    classify_response.raise_for_status = MagicMock()
    classify_response.json.return_value = {"intent": "QUERY", "confidence": 0.9}
    gate_response = MagicMock()
    gate_response.raise_for_status = MagicMock()
    gate_response.json.return_value = {"threshold": 0.70}
    harvest_response = MagicMock()
    harvest_response.raise_for_status = MagicMock()
    harvest_response.json.return_value = {"edges": []}
    query_response = MagicMock()
    query_response.raise_for_status = MagicMock()
    query_response.json.return_value = {
        "facts": [{"definition": "You have a pet that is Fraggle",
                   "fact_class": "A", "rel_type": "has_pet"}],
        "attributes": {},
    }

    mock_http_client.post = AsyncMock(
        side_effect=[classify_response, harvest_response, query_response]
    )
    mock_http_client.get = AsyncMock(return_value=gate_response)

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await recall_memory_tool("what do you know", "user-alice")

    assert "If their message touched on other distinct topics" in result["memory"]
    assert result["memory"].rstrip().endswith("recall each before responding.")


async def test_empty_recall_sentinel_never_gains_the_hint(mock_http_client):
    """The other half of the contract, asserted separately so a regression names itself.

    ⚠️ BENCHMARK CONTRACT: the abstention scorer keys off the EXACT string
    "No relevant facts found.". Appending the hint to an EMPTY recall would silently
    corrupt every abstention score.
    """
    classify_response = MagicMock()
    classify_response.raise_for_status = MagicMock()
    classify_response.json.return_value = {"intent": "QUERY", "confidence": 0.9}
    gate_response = MagicMock()
    gate_response.raise_for_status = MagicMock()
    gate_response.json.return_value = {"threshold": 0.70}
    harvest_response = MagicMock()
    harvest_response.raise_for_status = MagicMock()
    harvest_response.json.return_value = {"edges": []}
    query_response = MagicMock()
    query_response.raise_for_status = MagicMock()
    query_response.json.return_value = {"facts": [], "attributes": {}}

    mock_http_client.post = AsyncMock(
        side_effect=[classify_response, harvest_response, query_response]
    )
    mock_http_client.get = AsyncMock(return_value=gate_response)

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await recall_memory_tool("anything", "user-alice")

    assert result == {"memory": "No relevant facts found."}
    assert "distinct topics" not in result["memory"]
