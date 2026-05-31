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
    yield
    _mcp._provisioned_users = original_provisioned
    _mcp.FAULTLINE_USER_ID = original_user_id
    _mcp._http_client = original_client


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
    assert result["protocolVersion"] == "2025-03-26"
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
    assert len(tools) == 3
    names = {t["name"] for t in tools}
    assert names == {"recall_memory", "remember_facts", "retract_fact"}


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
                "arguments": {"query": "family", "user_id": "alice"},
            },
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 4
    assert data["result"] == expected_result
    mock_call.assert_called_once_with("recall_memory", {"query": "family", "user_id": "alice"})


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
                "arguments": {"text": "My dog is called Spot.", "user_id": "alice"},
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
                "arguments": {"text": "forget that I have a dog", "user_id": "alice"},
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


def test_cors_headers(client):
    resp = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers


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
    assert "WWW-Authenticate" in resp.headers


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
