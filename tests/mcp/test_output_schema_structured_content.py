"""Pin the MCP outputSchema -> structuredContent contract (2025-06-18 spec).

A tool that declares ``outputSchema`` MUST return ``structuredContent``; spec-strict
clients (opencode / Claude Desktop / Cline) reject a text-only result with
``-32600 "Tool X has an output schema but did not return structured content"``.

The tools/call dispatch envelope (``_call_tool``) emits ``structuredContent`` exactly
for tools present in ``_OUTPUT_SCHEMAS``; every other tool stays text-only. These tests
pin both halves plus the open-string ``status`` schema so the contract cannot regress.
"""

import json
import os
import sys

import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.mcp.tools import _OUTPUT_SCHEMAS
import src.mcp.server as _server_mod
from src.mcp.server import _call_tool


@pytest.fixture(autouse=True)
def _bypass_provisioning_and_validation():
    """Skip the provisioning gate and input validation so the test isolates the envelope.

    ``_call_tool`` blocks on ``_ensure_provisioned()`` and runs ``_validate_tool_input``
    before dispatch; neither is what this test is pinning, so both are short-circuited.
    """
    _server_mod._provisioned_users.add("struct-pin")
    _server_mod._FAULTLINE_URL_DETECTED = True
    with patch.object(_server_mod, "_validate_tool_input", return_value=None):
        yield
    _server_mod._provisioned_users.discard("struct-pin")


def _fake(returning):
    async def _h(**kwargs):
        return dict(returning)
    return _h


@pytest.mark.asyncio
async def test_tools_with_output_schema_emit_structured_content():
    """Every ADVERTISED tool declares outputSchema -> envelope carries structuredContent, and the
    text ``content`` mirror is retained for back-compat with text-only clients.

    The cortex pair (cortex_recall / cortex_remember) were added to _OUTPUT_SCHEMAS so the
    agent-cortex lane is as predictably parsable as the user-memory lane: they return the same
    ``{"memory": ...}`` / ``{"status": ...}`` dict shapes recall_memory and remember_facts
    already declared, so a text-only client sees the identical ``content`` string while a
    spec-strict client gains a structured view. This is ADDITIVE — declaring outputSchema never
    removes the text mirror, so no client regresses.
    """
    cases = [
        ("recall_memory", {"query": "x"}, {"memory": "No relevant facts found."}),
        ("remember_facts", {"text": "my favourite colour is cerulean"},
         {"status": "stored", "committed": 1, "message": "ok"}),
        # A status the OLD hard-coded enum ["stored","valid","no_ingest","corrected","failed"]
        # would have REJECTED -> proves the open-string schema accepts the full delegated surface.
        ("remember_facts", {"text": "hi"}, {"status": "query_detected", "message": "use recall_memory"}),
        # Agent-cortex lane: same dict shapes, now structured.
        ("cortex_recall", {"query": "deploy"},
         {"memory": "My own operational notes for this:..."}),
        ("cortex_remember", {"text": "being corrected IS the trigger"},
         {"status": "noted", "note": "Noted for myself (operational memory) — my own working note on how to operate better, not something recorded as your fact."}),
    ]
    for tool_name, args, result in cases:
        with patch.dict(_server_mod.TOOL_DISPATCH, {tool_name: _fake(result)}, clear=False):
            env = await _call_tool(tool_name, {"user_id": "struct-pin", **args})
        assert "structuredContent" in env, f"{tool_name}: outputSchema declared but no structuredContent"
        assert env["structuredContent"] == result
        assert json.loads(env["content"][0]["text"]) == result  # text mirror retained


@pytest.mark.asyncio
async def test_tools_without_output_schema_stay_text_only():
    """A tool with no outputSchema entry (the low-level, unadvertised backend tools) must NOT emit
    structuredContent."""
    assert "query" not in _OUTPUT_SCHEMAS  # low-level tools are not part of the advertised surface
    result = {"notes": []}
    with patch.dict(_server_mod.TOOL_DISPATCH, {"query": _fake(result)}, clear=False):
        env = await _call_tool("query", {"user_id": "struct-pin", "query": "x"})
    assert "structuredContent" not in env
    assert json.loads(env["content"][0]["text"]) == result


def test_remember_facts_status_is_open_string_not_enum():
    """status is an open string spanning a delegated/retract surface, NOT a closed enum —
    a closed enum here is the landmine that shipped -32600 to spec-strict clients."""
    schema = _OUTPUT_SCHEMAS["remember_facts"]["properties"]["status"]
    assert "enum" not in schema
    assert schema["type"] == "string"


def test_recall_memory_status_is_open_string_not_enum():
    """Same landmine as remember_facts: recall_memory diverts to learn/retract/ingest paths,
    so its status is an open set (ingest_disabled, read_only, corrected, query_detected, …).
    A closed enum here shipped -32602 to spec-strict clients (opencode) when a diverted
    recall returned a status outside ["ok","no_ingest","error"]."""
    schema = _OUTPUT_SCHEMAS["recall_memory"]["properties"]["status"]
    assert "enum" not in schema
    assert schema["type"] == "string"


def test_advertised_tools_with_schema_are_a_subset_of_dispatch():
    """Guard: the only tools that carry structuredContent are exactly those in _OUTPUT_SCHEMAS."""
    advertised = {t["name"] for t in _server_mod.TOOLS}
    assert _OUTPUT_SCHEMAS.keys() <= advertised
