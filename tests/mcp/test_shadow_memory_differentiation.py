"""Shadow-memory routing differentiation (merge-upward gauntlet B1, port of foss 5bf9e382).

THE FAILURE (foss user report, 2026-10-24, live FOSS stack): a model routed "save to FaultLine"
into the HOST's built-in note tools (write_note / search_notes / view_note). The data landed in
ephemeral host notes — never in the knowledge graph. The model had never been told, at connect
time, that the host's own note tools are NOT FaultLine memory.

THE PORT: the routing rule rides SERVER_INSTRUCTIONS — the MCP-spec initialize.instructions text
that OpenWebUI >= 0.6.31 surfaces in the system prompt on native MCP connections (the connection
type the report is about). Both local doors serve it (http_server initialize + stdio run_mcp_server,
both read premise.SERVER_INSTRUCTIONS).

ADJUDICATED RESIDUAL (measured, not hand-waved): foss also led the canonical recall/remember
tool DESCRIPTIONS with differentiation prose. On this line the served descriptions are
prefix (TOOL_PREMISE 242) + body, at 1009/1011 chars against the 1024 OpenAI function-calling
limit — 15/13 chars of headroom. A differentiation sentence does not fit; growing TOOL_PREMISE
past 1024 triggers _within_budget's clamp, which SILENTLY DROPS the description tail (the
document_status wound of 2026-10). So the description-lead surface needs an owner-approved copy
pass (trims of defended authorship-contract text) and is NOT silently widened here.
"""
import sys

sys.path.insert(0, ".")

from src.mcp import premise


def test_server_instructions_names_the_host_note_tools():
    p = premise.SERVER_INSTRUCTIONS
    assert "HOST NOTE TOOLS ARE NOT THIS MEMORY" in p
    for note_tool in ("write_note", "search_notes", "view_note"):
        assert note_tool in p, f"the routing notice must name the host built-in {note_tool}"
    assert "NOT FaultLine memory" in p
    # The rule must end at a ACTION, not a warning: name the FaultLine tools to route through.
    assert "remember_facts" in p and "recall_memory" in p


def test_discover_result_carries_the_routing_notice():
    """server/discover is the second transport — same instructions, same rule (transport parity)."""
    d = premise.discover_result()
    assert "HOST NOTE TOOLS ARE NOT THIS MEMORY" in d["instructions"]


def test_initialize_on_the_http_door_serves_the_routing_notice():
    """END-TO-END smoke: a real POST /mcp initialize through the TestClient must come back
    with the routing notice in result.instructions — this is the exact surface OpenWebUI
    >= 0.6.31 injects into the system prompt (the shadow-memory report's connection type)."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from src.mcp import http_server as h

    with patch("src.mcp.http_server.MCP_API_KEY", "smoke-key"):
        client = TestClient(h.app)
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
            },
            headers={"Authorization": "Bearer smoke-key"},
        )
    assert resp.status_code == 200, resp.text
    instructions = resp.json()["result"]["instructions"]
    assert "HOST NOTE TOOLS ARE NOT THIS MEMORY" in instructions
    assert "write_note" in instructions and "view_note" in instructions


def test_served_descriptions_stay_under_the_openai_limit():
    """The residual guard: recall/remember descriptions are at 1009/1011 chars with 15/13 chars
    of headroom. If a future copy pass pushes them past 1024, _within_budget clamps and SILENTLY
    DROPS the description tail on the OpenAPI door — the exact silent-drop wound document_status
    took in 2026-10. Keep the served copies whole; assert the REAL builder's output."""
    from src.mcp import http_server as h

    spec = h._build_tool_openapi()
    served = {i["post"]["operationId"]: len(i["post"]["description"]) for i in spec["paths"].values()}
    assert served["recall_memory"] <= premise.OPENAI_DESCRIPTION_LIMIT
    assert served["remember_facts"] <= premise.OPENAI_DESCRIPTION_LIMIT
    # FOSS PORT (2026-11-15 uplift): the foss host-note naming block is re-woven into the
    # canonical descriptions (source rewrite had dropped it), so the served lengths moved
    # from the private line's 1009/1011 to 1006/991. Vehicle-swap with the MECHANISM byte-
    # identical: <= limit + the exact host-note phrases. Re-pin evidence: measured on the
    # foss uplift tip with MEMORY_TEACHES on (production condition).
    assert served["recall_memory"] == 1006, "recall_memory description moved — re-pin WITH evidence"
    assert served["remember_facts"] == 991, "remember_facts description moved — re-pin WITH evidence"
