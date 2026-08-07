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
    """recall_memory + remember_facts declare outputSchema -> envelope carries structuredContent,
    and the text ``content`` mirror is retained for back-compat with text-only clients."""
    cases = [
        ("recall_memory", {"query": "x"}, {"memory": "No relevant facts found."}),
        ("remember_facts", {"text": "my favourite colour is cerulean"},
         {"status": "stored", "committed": 1, "message": "ok"}),
        # A status the OLD hard-coded enum ["stored","valid","no_ingest","corrected","failed"]
        # would have REJECTED -> proves the open-string schema accepts the full return surface.
        ("remember_facts", {"text": "hi"}, {"status": "query_detected", "message": "use recall_memory"}),
    ]
    for tool_name, args, result in cases:
        with patch.dict(_server_mod.TOOL_DISPATCH, {tool_name: _fake(result)}, clear=False):
            env = await _call_tool(tool_name, {"user_id": "struct-pin", **args})
        assert "structuredContent" in env, f"{tool_name}: outputSchema declared but no structuredContent"
        assert env["structuredContent"] == result
        assert json.loads(env["content"][0]["text"]) == result  # text mirror retained


@pytest.mark.asyncio
async def test_tools_without_output_schema_stay_text_only():
    """A tool with no outputSchema entry must NOT emit structuredContent."""
    name = "noop_untyped_tool"
    assert name not in _OUTPUT_SCHEMAS
    result = {"anything": "text-only"}
    with patch.dict(_server_mod.TOOL_DISPATCH, {name: _fake(result)}, clear=False):
        env = await _call_tool(name, {"user_id": "struct-pin", "query": "x"})
    assert "structuredContent" not in env
    assert json.loads(env["content"][0]["text"]) == result


def test_remember_facts_status_is_open_string_not_enum():
    """status is an open string spanning the full return surface, NOT a closed enum —
    a closed enum here is the landmine that shipped -32600 to spec-strict clients."""
    schema = _OUTPUT_SCHEMAS["remember_facts"]["properties"]["status"]
    assert "enum" not in schema
    assert schema["type"] == "string"


def test_advertised_tools_with_schema_are_a_subset_of_tools():
    """Guard: the only tools that carry structuredContent are exactly those in _OUTPUT_SCHEMAS."""
    advertised = {t["name"] for t in _server_mod.TOOLS}
    assert _OUTPUT_SCHEMAS.keys() <= advertised
