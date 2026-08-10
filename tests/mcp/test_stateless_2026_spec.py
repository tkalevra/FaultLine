"""Pin the 2026-07-28 stateless MCP surface on the HTTP transport.

The 2026-07-28 revision makes MCP stateless: no initialize/notifications/initialized handshake
required, no Mcp-Session-Id, every request self-contained with version + capabilities in `_meta`.
It adds `server/discover` (MUST), `resultType` on every result (MUST), CacheableResult
(`ttlMs`/`cacheScope`) on list results, `Mcp-Method`/`Mcp-Name` routing headers, and
`UnsupportedProtocolVersionError`. FaultLine is DUAL-ERA: legacy clients (2025-11-25 and earlier)
still use the initialize handshake untouched; modern clients (2026-07-28) are served statelessly.

These tests pin the ADDITIVE surface only — every legacy behaviour is unchanged, which is the
"no breaking changes" constraint made testable.
"""

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.mcp import premise
from src.mcp.http_server import app

# A well-formed tenant UUID (bind_tenant requires one even in tests).
TEST_USER_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _open_core_env(monkeypatch):
    """Auth off for every test in this package (mirrors tests/mcp/conftest)."""
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    import src.mcp.http_server as h
    monkeypatch.setattr(h, "MCP_API_KEY", "")


# ── server/discover ──────────────────────────────────────────────────────────────
# 2026-07-28: servers MUST implement server/discover (SEP-2575). Lets a modern client learn
# supported protocol versions + capabilities + identity in one request, before any other RPC —
# and is the stdio backward-compatibility probe.

def test_server_discover_advertises_versions_capabilities_and_identity(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": "d1",
                                  "method": "server/discover", "params": {}})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["resultType"] == "complete"
    # supportedVersions includes the modern revision FIRST.
    assert result["supportedVersions"][0] == "2026-07-28"
    assert "2025-06-18" in result["supportedVersions"]  # legacy still supported
    assert "tools" in result["capabilities"]
    # Identity (self-reported).
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "faultline-mcp"
    # CacheableResult hints (SEP-2549).
    assert result["ttlMs"] > 0
    assert result["cacheScope"] in ("public", "private")


def test_server_discover_works_before_any_initialize(client):
    """Stateless: discover is served WITHOUT a prior initialize handshake (a modern client
    sends it first thing; gating it behind the legacy handshake would break the probe)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "server/discover"})
    assert r.status_code == 200
    assert r.json()["result"]["resultType"] == "complete"


# ── resultType on every result ───────────────────────────────────────────────────
# SEP-2322: all results carry a required resultType; clients MUST treat missing as "complete",
# so adding it is non-breaking AND makes responses predictably parsable.

def test_tools_list_result_carries_resultType_and_cache_hints(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                  "method": "tools/list", "params": {}})
    result = r.json()["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] > 0
    assert result["cacheScope"] in ("public", "private")
    # Server identity on the envelope (SHOULD, SEP-2575).
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "faultline-mcp"


def test_tools_call_result_carries_resultType(client):
    expected = {"content": [{"type": "text", "text": json.dumps({"memory": "no facts"})}]}
    with patch("src.mcp.server._call_tool", new=AsyncMock(return_value=expected)):
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": "recall_memory",
                                                 "arguments": {"query": "x",
                                                               "user_id": TEST_USER_ID}}})
    result = r.json()["result"]
    assert result["resultType"] == "complete"
    # content payload unchanged (text mirror retained).
    assert result["content"] == expected["content"]


def test_empty_ack_results_stay_byte_identical(client):
    """ping / notifications/initialized return `{}` — an empty result must NOT gain resultType
    (legacy ack semantics untouched; the spec's clients treat missing as complete anyway)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert r.json()["result"] == {}


# ── Mcp-Method / Mcp-Name headers (SEP-2243) ─────────────────────────────────────
# Modern Streamable HTTP POSTs carry Mcp-Method/Mcp-Name so gateways route without parsing the
# body. We ACCEPT them; absence (legacy) falls back to the body method.

def test_mcp_method_header_routes_the_request(client):
    """A self-consistent Mcp-Method header is honored (routing without body parsing)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                    headers={"Mcp-Method": "tools/list"})
    assert r.status_code == 200
    assert r.json()["result"]["resultType"] == "complete"


def test_mcp_method_header_mismatch_is_rejected(client):
    """A header that disagrees with the body method is a mis-routed request → HeaderMismatch."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 6, "method": "tools/list"},
                    headers={"Mcp-Method": "tools/call"})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == premise.ERROR_HEADER_MISMATCH  # -32020 (renumbered per SEP-2575)
    assert "disagrees" in err["message"]


def test_mcp_method_header_routes_without_body_method(client):
    """A modern request may omit the body method entirely and route on the header alone."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7},
                    headers={"Mcp-Method": "ping"})
    assert r.status_code == 200
    assert r.json()["result"] == {}


# ── Protocol version negotiation (SEP-2575) ──────────────────────────────────────
# Modern requests carry `_meta.io.modelcontextprotocol/protocolVersion` (or MCP-Protocol-Version
# header). An unsupported version → UnsupportedProtocolVersionError listing what we DO support.

def test_unsupported_protocol_version_returns_actionable_error(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 8, "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}},
    })
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == premise.ERROR_UNSUPPORTED_PROTOCOL_VERSION  # -32022
    # ACTIONABLE: the data carries the supported list + the requested value, so the client can
    # pick a mutually-supported version and retry (spec-mandated shape).
    assert "2026-07-28" in err["data"]["supported"]
    assert err["data"]["requested"] == "1900-01-01"


def test_supported_protocol_version_in_meta_is_accepted(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
    })
    assert r.status_code == 200


def test_legacy_requests_without_version_are_unaffected(client):
    """A legacy request with no _meta version is served exactly as before (dual-era)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
    assert r.status_code == 200


def test_mcp_protocol_version_header_negotiates(client):
    """The MCP-Protocol-Version header is the HTTP carrier of the same negotiation."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 11, "method": "tools/list"},
                    headers={"MCP-Protocol-Version": "1999-01-01"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == premise.ERROR_UNSUPPORTED_PROTOCOL_VERSION


# ── Initialize still echoes the modern version when asked (dual-era) ─────────────
def test_initialize_echoes_2026_version(client):
    """A legacy client that initializes with the 2026-07-28 version gets it echoed."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 12, "method": "initialize",
                                  "params": {"protocolVersion": "2026-07-28"}})
    assert r.json()["result"]["protocolVersion"] == "2026-07-28"


# ── stock SDK regression (2026-08-10): client SDKs propose 2025-11-25 ──────────
# The official MCP client SDKs' LATEST_PROTOCOL_VERSION is "2025-11-25" and their supported
# whitelist rejects any echoed revision it has never seen. We were not listing 2025-11-25, so
# negotiation replied with 2026-07-28, the client threw "protocol version is not supported",
# and the connection surfaced the breakage as a misleading transport error.

def test_initialize_echoes_2025_11_25_for_stock_sdk(client):
    """The SDK's proposed version must be echoed verbatim, not clobbered with 2026-07-28."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 30, "method": "initialize",
                                  "params": {"protocolVersion": "2025-11-25"}})
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"


def test_2025_11_25_header_on_subsequent_requests_is_accepted(client):
    """The SDK re-sends the echoed value in `mcp-protocol-version` on every request."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 31, "method": "tools/list"},
                    headers={"MCP-Protocol-Version": "2025-11-25"})
    assert r.status_code == 200
    assert "error" not in r.json()


def test_negotiate_never_offers_newer_than_requested(client):
    """An unknown proposed revision gets the newest revision we serve that is <= it."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 32, "method": "initialize",
                                  "params": {"protocolVersion": "2025-12-01"}})
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"
    assert r.json()["result"]["protocolVersion"] != premise.LATEST_PROTOCOL_VERSION


def test_negotiate_unknown_old_revision_falls_back_to_newest_known(client):
    """A proposal older than everything we list still yields something parseable."""
    assert premise.negotiate_protocol_version("2024-08-01") == "2024-10-07"
    assert premise.negotiate_protocol_version("2025-11-25") == "2025-11-25"
    assert premise.negotiate_protocol_version(None) == premise.LATEST_PROTOCOL_VERSION


# ── Discoverability: canonical order ─────────────────────────────────────────────
# 2026-07-28 §tools: deterministic order enables caching + stable prompt-cache hits, and ORDER
# IS DISCOVERABILITY — the user-memory entry points must precede the document/learn lanes so a
# model meets "recall/remember your own knowledge" before "store a document".

def test_tools_list_orders_user_memory_pair_first(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 13, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    # The user-memory pair leads, immediately followed by the document lane.
    assert names[0] == "recall_memory"
    assert names[1] == "remember_facts"
    assert names.index("ingest_document") > names.index("remember_facts")
    assert names.index("learn_facts") > names.index("ingest_document")
    # Deterministic: same order across calls.
    r2 = client.post("/mcp", json={"jsonrpc": "2.0", "id": 14, "method": "tools/list"})
    assert [t["name"] for t in r2.json()["result"]["tools"]] == names


def test_openapi_door_orders_user_memory_pair_first(client):
    """The door OpenWebUI actually reads (per-operation iteration order) must match."""
    spec = client.get("/openapi.json").json()
    ops = [i["post"]["operationId"] for i in spec["paths"].values() if "post" in i]
    assert ops.index("rest_recall_memory_recall_memory_post") < \
        ops.index("rest_ingest_document_ingest_document_post")
    assert ops.index("rest_remember_facts_remember_facts_post") < \
        ops.index("rest_ingest_document_ingest_document_post")


# ── Every advertised tool carries outputSchema → predictably parsable results ────
def test_all_advertised_tools_declare_output_schema():
    from src.mcp.tools import TOOLS, _OUTPUT_SCHEMAS
    missing = [t["name"] for t in TOOLS if t["name"] not in _OUTPUT_SCHEMAS]
    assert not missing, f"advertised tools missing outputSchema: {missing}"


# ── stdio dual-era gate ──────────────────────────────────────────────────────────
# The stdio loop gates tools/list + tools/call on `_initialized` for LEGACY clients only. A
# modern (2026-07-28) request is STATELESS — gating it behind a handshake the protocol abolished
# would break every modern client on the stdio transport. These run the server as a subprocess
# and drive the wire protocol: server/discover and tools/list need no backend, so the gate
# (not the tool) is what is under test.

import subprocess
import sys as _sys


def _stdio_roundtrip(lines, env_extra=None):
    """Run the stdio MCP server and exchange one JSON-RPC message per line. Returns responses."""
    env = dict(os.environ)
    # A subprocess cannot reuse this test's monkeypatched env (that is process-wide here, but
    # the child starts fresh) — so unset the auth switch explicitly and pin the URL-detection
    # fast path so a tool that never runs cannot probe the network anyway.
    env.pop("MCP_API_KEY", None)
    env.pop("FAULTLINE_USER_ID", None)
    env.update({"FAULTLINE_API_URL": "http://127.0.0.1:1", "_FAULTLINE_URL_DETECTED": "1"})
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # tools/mcp_server.py imports `src.*` — the repo root must be on the child's path.
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [_sys.executable, "tools/mcp_server.py", "--transport", "stdio"],
        cwd=repo,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    try:
        out, err = proc.communicate(input="\n".join(json.dumps(l) for l in lines) + "\n",
                                    timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    responses = [json.loads(x) for x in out.splitlines() if x.strip()]
    return responses, err


def test_stdio_server_discover_is_stateless():
    """A modern client may send server/discover as its FIRST message — no initialize, no gate."""
    responses, err = _stdio_roundtrip([
        {"jsonrpc": "2.0", "id": "sd1", "method": "server/discover", "params": {}},
    ])
    assert responses, f"no response from stdio server; stderr={err[:400]}"
    result = responses[0]["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"][0] == "2026-07-28"


def test_stdio_tools_list_bypasses_the_initialize_gate_for_modern_clients():
    """2026-07-28: a request carrying `_meta` protocolVersion is self-contained and MUST NOT be
    gated on the legacy initialize handshake."""
    responses, err = _stdio_roundtrip([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
         "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}},
    ])
    assert responses, f"no response from stdio server; stderr={err[:400]}"
    result = responses[0]["result"]
    assert result["resultType"] == "complete"
    names = [t["name"] for t in result["tools"]]
    assert names[0] == "recall_memory"
    assert names.index("ingest_document") > names.index("remember_facts")


def test_stdio_legacy_gate_still_holds():
    """DUAL-ERA, legacy half unchanged: a legacy client (no _meta) that calls tools/list before
    initialize still gets the -32002 gate, exactly as before."""
    responses, err = _stdio_roundtrip([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])
    assert responses, f"no response from stdio server; stderr={err[:400]}"
    assert responses[0]["error"]["code"] == -32002, "legacy gate must hold for non-modern requests"


def test_stdio_unsupported_modern_version_is_actionable():
    responses, err = _stdio_roundtrip([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
         "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1999-01-01"}}},
    ])
    assert responses, f"no response from stdio server; stderr={err[:400]}"
    err_body = responses[0]["error"]
    assert err_body["code"] == premise.ERROR_UNSUPPORTED_PROTOCOL_VERSION
    assert "2026-07-28" in err_body["data"]["supported"]
