"""Tests for FaultLine MCP HTTP transport (http_server.py).

Mock-based — does NOT hit a live API or live FaultLine backend.
All tool calls are intercepted at the _mcp._call_tool level.
"""

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.mcp.server as _mcp
from src.mcp.http_server import app


# A well-formed tenant UUID. bind_tenant() (src/mcp/server.py) is FAIL-LOUD: a malformed
# (non-UUID) claimed identity is REJECTED with a 400 — that is the multi-tenant spoof guard
# (DEV/SECURITY-multiuser-tenant-isolation.md; pinned by
# tests/test_saas_isolation.py::test_saof_bind_tenant_malformed_still_400). Tests that drive a
# tool through the transport must therefore claim a REAL, well-formed (lowercase) UUID; the old
# "alice" placeholder now legitimately never reaches the tool.
TEST_USER_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_mcp_state():
    """Prevent module-level state from leaking between tests."""
    original_provisioned = _mcp._provisioned_users.copy()
    original_user_id = _mcp.FAULTLINE_USER_ID
    # Patch _http_client so _ensure_provisioned never makes a real HTTP call.
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=Exception("no real provisioning in tests"))
    original_client = _mcp._http_client
    _mcp._http_client = mock_client
    # ...but _http_client is NOT the only thing that touches the network. `_detect_faultline_url`
    # PROBES candidate backend URLs with its own client, so a unit test on a machine with no local
    # backend burned ~55s of connection retries PER TEST that reached the provisioning gate — two
    # of them, which is ~110s of the suite's 115s runtime, and both went red for purely
    # environmental reasons. tests/mcp/test_server.py already pins this; this file never did.
    # A unit test must not depend on a service being up.
    original_detected = _mcp._FAULTLINE_URL_DETECTED
    _mcp._FAULTLINE_URL_DETECTED = True   # no live probing from a unit test
    yield
    _mcp._provisioned_users = original_provisioned
    _mcp.FAULTLINE_USER_ID = original_user_id
    _mcp._http_client = original_client
    _mcp._FAULTLINE_URL_DETECTED = original_detected


@pytest.fixture
def client():
    """TestClient wraps the FastAPI app. Lifespan runs automatically."""
    with TestClient(app) as c:
        yield c


# ── Health ────────────────────────────────────────────────────────────────────


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["transport"] == "http"


# ── Initialize handshake ──────────────────────────────────────────────────────


def test_initialize_handshake(client):
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 1
    result = data["result"]
    # protocolVersion is NEGOTIATED, not hardcoded. This used to assert the literal
    # "2025-03-26" — but that only pinned the fact that the value was hardcoded and the
    # client's request ignored. The MCP lifecycle spec: echo the requested version if we
    # support it, otherwise respond with a version we do support, and that "SHOULD be the
    # latest". This request names NO version, so our latest is the correct answer.
    # (The echo cases are covered in test_premise_and_tool_parity.py.)
    from src.mcp.premise import LATEST_PROTOCOL_VERSION
    assert result["protocolVersion"] == LATEST_PROTOCOL_VERSION
    assert "capabilities" in result
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "faultline-mcp"


# ── tools/list ────────────────────────────────────────────────────────────────


def test_tools_list(client):
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    })
    assert resp.status_code == 200
    data = resp.json()
    tools = data["result"]["tools"]
    names = {t["name"] for t in tools}
    # learn_facts is now a registered tool alongside the original three.
    assert {"recall_memory", "remember_facts", "retract_fact"} <= names


# ── ping ──────────────────────────────────────────────────────────────────────


def test_ping(client):
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 3,
        "method": "ping",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 3
    assert data["result"] == {}


# ── notifications/initialized ─────────────────────────────────────────────────


def test_notifications_initialized(client):
    """HTTP transport acknowledges notifications/initialized with empty result."""
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": None,
        "method": "notifications/initialized",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == {}


# ── tools/call ────────────────────────────────────────────────────────────────


def test_tools_call_recall_memory(client):
    """tools/call recall_memory invokes _call_tool with correct args."""
    expected_result = {
        "content": [{"type": "text", "text": json.dumps({"facts": ["You have a cat."], "preferred_names": {}})}]
    }

    with patch("src.mcp.server._call_tool", new=AsyncMock(return_value=expected_result)) as mock_call:
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "recall_memory",
                "arguments": {"query": "family", "user_id": TEST_USER_ID},
            },
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 4
    assert data["result"] == expected_result
    mock_call.assert_called_once_with(
        "recall_memory", {"query": "family", "user_id": TEST_USER_ID}
    )


def test_tools_call_remember_facts(client):
    expected_result = {
        "content": [{"type": "text", "text": json.dumps({"stored": 1, "fact_class": "A"})}]
    }
    with patch("src.mcp.server._call_tool", new=AsyncMock(return_value=expected_result)):
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "remember_facts",
                "arguments": {"text": "My dog is called Spot.", "user_id": TEST_USER_ID},
            },
        })
    assert resp.status_code == 200
    assert resp.json()["result"] == expected_result


def test_tools_call_retract_fact(client):
    expected_result = {
        "content": [{"type": "text", "text": json.dumps({"retracted": True})}]
    }
    with patch("src.mcp.server._call_tool", new=AsyncMock(return_value=expected_result)):
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "retract_fact",
                "arguments": {"text": "forget that I have a dog", "user_id": TEST_USER_ID},
            },
        })
    assert resp.status_code == 200
    assert resp.json()["result"] == expected_result


# ── Unknown method ────────────────────────────────────────────────────────────


def test_unknown_method(client):
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 7,
        "method": "no_such_method",
    })
    assert resp.status_code == 404
    data = resp.json()
    assert data["id"] == 7
    assert "error" in data
    assert data["error"]["code"] == -32601
    assert "no_such_method" in data["error"]["message"]


# ── Malformed JSON ────────────────────────────────────────────────────────────


def test_malformed_json(client):
    resp = client.post(
        "/mcp",
        content=b"this is not json{{",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == -32700
    assert "parse" in data["error"]["message"].lower()


# ── CORS headers ──────────────────────────────────────────────────────────────


def test_cors_unlisted_origin_not_allowed(client):
    """CORS is now an env-configurable allowlist (default empty). An unlisted
    browser origin must NOT receive an access-control-allow-origin header."""
    resp = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


# ── JSON-RPC id pass-through ──────────────────────────────────────────────────


def test_jsonrpc_id_passthrough_string(client):
    """String IDs must be echoed back unchanged."""
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": "req-abc",
        "method": "ping",
    })
    assert resp.json()["id"] == "req-abc"


def test_jsonrpc_id_passthrough_null(client):
    """Null ID (notification-style) must be echoed back as null."""
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": None,
        "method": "ping",
    })
    assert resp.json()["id"] is None


# ── Bearer token auth ─────────────────────────────────────────────────────────


def test_auth_bypassed_when_no_api_key_set(client):
    """When MCP_API_KEY is not set, all requests are allowed through."""
    with patch("src.mcp.http_server.MCP_API_KEY", ""):
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 200


def test_auth_valid_bearer_token_accepted(client):
    """Correct Bearer token returns 200."""
    with patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer test-secret-key"},
        )
    assert resp.status_code == 200
    assert resp.json()["result"] == {}


def test_auth_missing_header_returns_401(client):
    """No Authorization header → 401."""
    with patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert resp.status_code == 401
    # No challenge header (port of foss 595d084c): a bare WWW-Authenticate: Bearer triggers
    # Claude Desktop's OAuth 2.1 DCR discovery loop that 401s every request. Flip pinned 2026-11-04.
    assert "WWW-Authenticate" not in resp.headers


def test_auth_wrong_token_returns_401(client):
    """Wrong Bearer token → 401."""
    with patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer wrong-key"},
        )
    assert resp.status_code == 401


def test_auth_health_endpoint_always_open(client):
    """/health does not require auth — needed for Docker healthchecks."""
    with patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"):
        resp = client.get("/health")
    assert resp.status_code == 200


# ── OpenAPI advertises the bearer scheme ──────────────────────────────────────


def test_openapi_advertises_bearer_scheme(client):
    """/openapi.json is the minimal CLIENT tool spec (compatibility waterfall): OpenAPI 3.0.3,
    ONLY the 6 tools, bearer security (global + per-op). It deliberately excludes /mcp (the
    JSON-RPC lane), /health, and the entire control-plane surface (info-disclosure win)."""
    data = client.get("/openapi.json").json()

    assert data["openapi"] == "3.0.3"   # broadly-compatible dialect (OWUI etc. can't parse 3.1)
    scheme = data["components"]["securitySchemes"]["HTTPBearer"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert data.get("security") == [{"HTTPBearer": []}]

    # The 6 tool operations carry the requirement (per-op).
    for path in ("/recall_memory", "/remember_facts", "/ingest_document",
                 "/learn_facts", "/retract_fact", "/forget_fact"):
        assert data["paths"][path]["post"]["security"] == [{"HTTPBearer": []}]

    # Tools-only: the JSON-RPC lane, health, and the control-plane are NOT in the client spec.
    assert "/mcp" not in data["paths"]
    assert "/health" not in data["paths"]
    assert not any(p.startswith("/admin") or p.startswith("/tenant") for p in data["paths"])


# ── REST endpoint auth (per-route) ────────────────────────────────────────────


def test_rest_remember_facts_401_without_bearer(client):
    """REST /remember_facts → 401, no challenge header (port of foss 595d084c)."""
    with patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"):
        resp = client.post("/remember_facts", json={"text": "x", "user_id": "alice"})
    assert resp.status_code == 401
    # No challenge: a bare WWW-Authenticate: Bearer triggers Claude Desktop's OAuth 2.1 DCR
    # discovery loop that 401s every request. Flip pinned 2026-11-04.
    assert "WWW-Authenticate" not in resp.headers


def test_rest_remember_facts_200_with_bearer(client):
    """REST /remember_facts → 200 with the correct bearer token."""
    expected = {"content": [{"type": "text", "text": json.dumps({"stored": 1})}]}
    # Every REST route gates on _ensure_provisioned BEFORE dispatching to the tool
    # (http_server.py:1285ff). Without this the gate short-circuits and returns its provisioning
    # payload, the patched tool is never called, and the test reads as a transport bug when it is
    # really the gate. We are testing the BEARER path here, not the provisioning gate.
    _mcp._provisioned_users.add(TEST_USER_ID)
    with patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"), \
            patch("src.mcp.server.remember_facts_tool", new=AsyncMock(return_value=expected)):
        resp = client.post(
            "/remember_facts",
            json={"text": "My dog is Spot.", "user_id": TEST_USER_ID},
            headers={"Authorization": "Bearer test-secret-key"},
        )
    assert resp.status_code == 200
    assert resp.json() == expected


def test_rest_recall_memory_401_without_bearer(client):
    """REST /recall_memory → 401 when no bearer sent."""
    with patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"):
        resp = client.post("/recall_memory", json={"query": "family", "user_id": "alice"})
    assert resp.status_code == 401
    # No challenge header (port of foss 595d084c) — flip pinned 2026-11-04.
    assert "WWW-Authenticate" not in resp.headers


def test_rest_recall_memory_200_with_bearer(client):
    """REST /recall_memory → 200 with the correct bearer token."""
    expected = {"content": [{"type": "text", "text": json.dumps({"facts": []})}]}
    _mcp._provisioned_users.add(TEST_USER_ID)   # testing the bearer path, not the gate
    with patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"), \
            patch("src.mcp.server.recall_memory_tool", new=AsyncMock(return_value=expected)):
        resp = client.post(
            "/recall_memory",
            json={"query": "family", "user_id": TEST_USER_ID},
            headers={"Authorization": "Bearer test-secret-key"},
        )
    assert resp.status_code == 200
    assert resp.json() == expected


def test_rest_wrong_token_returns_401(client):
    """REST endpoint with a wrong bearer token → 401 (constant-time mismatch)."""
    with patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"):
        resp = client.post(
            "/remember_facts",
            json={"text": "x", "user_id": "alice"},
            headers={"Authorization": "Bearer wrong-key"},
        )
    assert resp.status_code == 401
