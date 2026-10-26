"""Shadow-memory routing fix (DEV/BUGS/foss-owui-notes-shadow-memory-20261024.md).

An OWUI user said "Faultline" three times; the model routed all three writes/reads to OWUI's
built-in Notes (`write_note`/`view_note`/`search_notes`) because nothing in FaultLine's tool
surface said "I AM the memory; those are not." Pins (2026-10-26):

- Both doors carry the differentiation. The native MCP door serves TOOLS verbatim; the
  OpenAPI door serves the FastAPI per-operation descriptions from /openapi.json. A fix on
  one door only leaves the other door's population shadow-routed.
- Descriptions stay within the 1024-char budget (exact-length pins below = budget guard:
  edit the text, re-measure, update the pin deliberately, never silently).
- The differentiation LEADS the description — hosts that truncate tool definitions
  (OWUI Tool Search defers >400-char defs and shows ~100 chars) must still see "FaultLine".
- `initialize` carries SERVER_INSTRUCTIONS on both transports — the one server-level
  routing notice native-MCP clients (OWUI ≥0.6.31) surface in the system prompt; the
  OpenAPI door drops it, which is why the README recommends the native connection type.
"""

import json
import os
import subprocess
import sys as _sys

from fastapi.testclient import TestClient

from src.mcp import premise
from src.mcp.http_server import app
from src.mcp.tools import TOOLS

# Measured 2026-10-26 — the budget pins. ≤1024 is the hard cap; the exact number is the guard.
_MCP_DOOR_DESC_LEN = {"recall_memory": 892, "remember_facts": 985}
_OPENAPI_DOOR_DESC_LEN = {"/recall_memory": 700, "/remember_facts": 985}
_DESC_CAP = 1024


def _tool(name):
    return next(t for t in TOOLS if t["name"] == name)


def test_remember_facts_differentiation_mcp_door():
    desc = _tool("remember_facts")["description"]
    assert desc
    assert len(desc) == _MCP_DOOR_DESC_LEN["remember_facts"]
    assert len(desc) <= _DESC_CAP
    assert "FaultLine" in desc
    assert "`write_note`" in desc
    assert "NOT FaultLine memory" in desc
    # LEADS: identity must survive a ~100-char truncation (OWUI Tool Search)
    assert "FaultLine" in desc[:100]


def test_recall_memory_differentiation_mcp_door():
    desc = _tool("recall_memory")["description"]
    assert desc
    assert len(desc) == _MCP_DOOR_DESC_LEN["recall_memory"]
    assert len(desc) <= _DESC_CAP
    assert "FaultLine" in desc
    assert "`search_notes`" in desc and "`view_note`" in desc
    assert "NOT FaultLine" in desc
    assert "FaultLine" in desc[:100]


def test_differentiation_on_the_openapi_door():
    """OWUI's OpenAPI tool servers read /openapi.json — the door the bug report came in on."""
    spec = app.openapi()
    markers = {
        "/remember_facts": "`write_note`",
        "/recall_memory": "`search_notes`",
    }
    for path, expected_len in _OPENAPI_DOOR_DESC_LEN.items():
        op = spec["paths"][path]["post"]
        desc = op.get("description", "")
        assert desc, f"{path}: OpenAPI door lost the description"
        assert len(desc) == expected_len
        assert len(desc) <= _DESC_CAP
        assert "FaultLine" in desc
        assert "NOT FaultLine" in desc
        assert markers[path] in desc
        # Same LEADS property as the MCP door — truncation must not erase the identity
        assert "FaultLine" in desc[:100]


def test_server_instructions_routing_notice():
    assert premise.SERVER_INSTRUCTIONS
    assert len(premise.SERVER_INSTRUCTIONS) <= _DESC_CAP
    for token in ("remember_facts", "recall_memory", "write_note", "FaultLine"):
        assert token in premise.SERVER_INSTRUCTIONS
    # Routing notices only — no SaaS premise text leaks into the public lineage
    assert "premise" not in premise.SERVER_INSTRUCTIONS.lower()


def test_http_initialize_carries_instructions():
    client = TestClient(app)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}})
    result = r.json()["result"]
    assert result["instructions"] == premise.SERVER_INSTRUCTIONS


def test_stdio_initialize_carries_instructions():
    """The stdio transport (Claude Desktop proxy) must carry the same routing notice."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ)
    env.pop("MCP_API_KEY", None)
    env.pop("FAULTLINE_USER_ID", None)
    env.update({"FAULTLINE_API_URL": "http://127.0.0.1:1", "_FAULTLINE_URL_DETECTED": "1",
                "PYTHONPATH": repo + os.pathsep + env.get("PYTHONPATH", "")})
    proc = subprocess.Popen(
        [_sys.executable, "tools/mcp_server.py", "--transport", "stdio"],
        cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        out, err = proc.communicate(
            input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}}) + "\n",
            timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    responses = [json.loads(x) for x in out.splitlines() if x.strip()]
    assert responses, f"no response from stdio server; stderr={err[:400]}"
    assert responses[0]["result"]["instructions"] == premise.SERVER_INSTRUCTIONS
