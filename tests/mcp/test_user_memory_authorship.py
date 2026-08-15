"""THE USER-MEMORY WRITE LANE IS DEFINED BY AUTHORSHIP, NOT CONTENT.

WHY THIS FILE EXISTS (production incident 2026-08-14)
-----------------------------------------------------
A build agent connected over MCP passed its OWN operating brief — engineering prose —
into ``remember_facts`` and ``recall_memory`` VERBATIM. It was OBEYING the contract as
written: "pass the user's message VERBATIM" with nothing separating a HUMAN's
conversational turn from the AGENT's own working text. The damage on a real user's
seat: dozens of durable Class-A facts at confidence 1.0 with junk entities (a provider
free-tier name typed Object, bare numerals as entities), plus a handful of engine-grown
rel_types minted from the junk.

THE FIX THESE TESTS PIN (FOSS port)
-----------------------------------
The write lane is defined by WHO authored the text, discriminated by WHO OPERATES THE
CLIENT (client_class, captured at the transport edge) — never by what the text looks
like. Content-based heuristics are forbidden by design: "looks technical" rules would
also filter a human engineer's genuine turn.

The upstream (private lineage) change also rewrote the model-facing tool/premise
descriptions to say "a HUMAN being's message" and added agent-memory tool routing.
The FOSS tree deliberately carries no model-facing premise text, and this port
deliberately does not touch tool descriptions — so those description-surface tests
were NOT ported. What the FOSS port carries, and what this file pins instead:

  * the schema/handler contract cannot silently drop a write (no ``evidence``-style
    property the handler never accepted; stray schema kwargs are tolerated, not
    TypeErrored away);
  * the store_context directive for agent clients names ONLY tools that exist here;
  * the advertised user-lane tool set stays free of unadvertised handler mismatches.

ONE THING THIS FILE REFUSES TO LET HAPPEN AGAIN: weakening human capture. The
authorship scoping must not soften the verbatim mandate or the no-permission default
on the user lane — a fix that filters genuine human turns would be worse than the
disease (the explicit remember_facts write is never client-class gated; pinned in
test_client_class_gates.py).
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.mcp.server as _server_mod
from src.mcp.server import remember_facts_tool, store_context_tool
from src.mcp.tools import TOOLS
from src.mcp.client_class import set_client_class


@pytest.fixture(autouse=True)
def _clean_state():
    """Pin the backend URL (no live probing) and reset the client-class ContextVar."""
    original_detected = _server_mod._FAULTLINE_URL_DETECTED
    _server_mod._FAULTLINE_URL_DETECTED = True
    set_client_class(None)
    yield
    set_client_class(None)
    _server_mod._FAULTLINE_URL_DETECTED = original_detected


def _json_response(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _get_router(**by_path):
    defaults = {
        "/confidence-gate": {"threshold": 0.70},
        "/internal/ingest-route": {"statement_extractor": "rewrite"},
    }
    defaults.update(by_path)

    async def _get(url, *_a, **_kw):
        for frag, payload in defaults.items():
            if frag in url:
                return _json_response(payload)
        return _json_response({})

    return AsyncMock(side_effect=_get)


def _tool(name):
    return next(t for t in TOOLS if t["name"] == name)


# ── The schema/handler contract must never silently drop a write ────────────


def test_remember_facts_schema_has_no_evidence_property():
    """The upstream incident fix removed an ``evidence`` property that was advertised on
    remember_facts while its handler took no such param — a model obeying the schema got
    a TypeError and the whole write was silently dropped. FOSS never carried the
    property; this pins that a schema/handler mismatch of that shape cannot return."""
    props = _tool("remember_facts")["inputSchema"]["properties"]
    assert "evidence" not in props, (
        "evidence is on remember_facts — its handler takes no such param (a model obeying "
        "the schema TypeErrors and the write is silently dropped)"
    )


async def test_remember_facts_tolerates_stray_schema_kwargs():
    """**_ignored (the FOSS half of the upstream swap-fix): a stale/cached tool schema
    carrying a field the handler never declared must not TypeError the write away — the
    human's words still land. A stray ``evidence`` kwarg rides through harmlessly."""
    set_client_class(None)
    classify_response = _json_response({"intent": "STATEMENT", "confidence": 0.9})
    episodic_response = _json_response({"ok": True})
    rewrite_response = _json_response({
        "edges": [{"subject": "user", "rel_type": "has_pet", "object": "rex",
                   "low_confidence": False}]
    })
    ingest_response = _json_response({"stored": 1, "fact_class": "A"})
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=[
        classify_response, episodic_response, rewrite_response, ingest_response])
    mock_client.get = _get_router()

    with patch("src.mcp.server._http_client", mock_client):
        result = await remember_facts_tool(
            "I have a dog named Rex", "user-carol",
            evidence={"sha": "abc123", "output": "red"},  # stray schema kwarg
        )

    assert result["stored"] == 1
    urls = [c[0][0] for c in mock_client.post.call_args_list]
    assert any("/ingest" in u for u in urls), urls


# ── The agent-client directive names ONLY tools that exist here ──────────────


async def test_store_context_directive_names_only_existing_tools():
    """The coding-agent store_context directive routes the caller to remember_facts. It
    must not name tools that do not exist on this server (the upstream lineage has a
    separate agent-memory lane; FOSS does not) — a directive naming a nonexistent tool
    strands the agent with nowhere to put the turn. And it must not store anything."""
    set_client_class("opencode")
    mock_client = MagicMock()
    mock_client.post = AsyncMock()

    with patch("src.mcp.server._http_client", mock_client):
        result = await store_context_tool("agent brief prose", "user-carol")

    mock_client.post.assert_not_awaited()
    assert result["status"] == "error"
    assert "remember_facts" in result["message"]
    # Every tool-shaped token the directive names must exist in the dispatch table.
    import re as _re
    for tok in _re.findall(r"[a-z_]+_[a-z_]+", result["message"]):
        assert tok in _server_mod.TOOL_DISPATCH, (
            f"directive names {tok!r} which is not a dispatchable tool — the agent is stranded"
        )


# ── The advertised user lane stays human-turn shaped ────────────────────────


def test_advertised_user_lane_tools_require_verbatim_human_turns():
    """The verbatim mandate on the user lane is the ENGINE'S raw material and predates
    the authorship fix — the FOSS port must not weaken it (a fix that filters genuine
    human turns would be worse than the disease). The descriptions still demand the
    user's message verbatim, in full."""
    for name in ("remember_facts", "recall_memory"):
        blob = str(_tool(name)).lower()
        assert "verbatim" in blob, (
            f"{name} lost the verbatim mandate — human capture is not negotiable"
        )
