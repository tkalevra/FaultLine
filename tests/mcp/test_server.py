"""Tests for FaultLine MCP server — mock-based, no live API needed.

Tests cover: tool schemas, success responses, error handling, user_id isolation.
"""

import json
import sys
import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.mcp.tools import TOOLS, validate_text, validate_user_id, validate_edges, validate_query
from src.mcp.server import (
    extract_tool,
    ingest_tool,
    query_tool,
    retract_tool,
    store_context_tool,
    recall_memory_tool,
    remember_facts_tool,
    retract_fact_tool,
    _call_tool,
)
import src.mcp.server as _server_mod


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_http_client():
    mock = AsyncMock()
    return mock


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


# ── Schema validation ────────────────────────────────────────────────────────


def test_tools_list_has_three_tools():
    assert len(TOOLS) == 3
    names = {t["name"] for t in TOOLS}
    assert names == {"recall_memory", "remember_facts", "retract_fact"}


def test_tools_have_required_schema_fields():
    for tool in TOOLS:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema


def test_recall_memory_schema_requires_query():
    recall = next(t for t in TOOLS if t["name"] == "recall_memory")
    assert "query" in recall["inputSchema"]["required"]
    assert "user_id" not in recall["inputSchema"]["required"]


def test_remember_facts_schema_requires_text():
    remember = next(t for t in TOOLS if t["name"] == "remember_facts")
    assert "text" in remember["inputSchema"]["required"]
    assert "user_id" not in remember["inputSchema"]["required"]


def test_retract_fact_schema_requires_text():
    retract = next(t for t in TOOLS if t["name"] == "retract_fact")
    assert "text" in retract["inputSchema"]["required"]
    assert "user_id" not in retract["inputSchema"]["required"]


# ── Input validation ─────────────────────────────────────────────────────────


def test_validate_text_valid():
    assert validate_text("hello world") is None


def test_validate_text_empty():
    assert validate_text("") is not None
    assert "empty" in validate_text("").lower()


def test_validate_text_not_string():
    assert validate_text(123) is not None  # type: ignore


def test_validate_user_id_valid():
    assert validate_user_id("user-123") is None


def test_validate_user_id_empty():
    assert validate_user_id("") is not None


def test_validate_edges_valid():
    edges = [{"subject": "alice", "object": "engineer", "rel_type": "works_for"}]
    assert validate_edges(edges) is None


def test_validate_edges_missing_field():
    edges = [{"subject": "alice"}]  # missing object and rel_type
    err = validate_edges(edges)
    assert err is not None
    assert "missing" in err.lower()


def test_validate_edges_empty():
    assert validate_edges([]) is not None


def test_validate_query_valid():
    assert validate_query("tell me about family") is None


def test_validate_query_empty():
    err = validate_query("")
    assert err is not None
    assert "empty" in err.lower()


def test_validate_query_not_string():
    assert validate_query(42) is not None  # type: ignore


# ── Tool handler tests (mocked _http_client) ────────────────────────────────


@pytest.mark.asyncio
async def test_query_tool_success(mock_http_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"facts": [], "preferred_names": {}}
    mock_response.raise_for_status = MagicMock()
    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await query_tool("tell me about family", "user-alice")
        assert result == {"facts": [], "preferred_names": {}}
        mock_http_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_extract_tool_success(mock_http_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"entities": []}
    mock_response.raise_for_status = MagicMock()
    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await extract_tool("some text", "user-bob")
        assert result == {"entities": []}


@pytest.mark.asyncio
async def test_ingest_tool_success(mock_http_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"stored": 2}
    mock_response.raise_for_status = MagicMock()
    mock_http_client.post = AsyncMock(return_value=mock_response)

    edges = [{"subject": "user", "object": "paris", "rel_type": "lives_in"}]
    with patch("src.mcp.server._http_client", mock_http_client):
        result = await ingest_tool("I live in Paris", "user-bob", edges)
        assert result == {"stored": 2}


@pytest.mark.asyncio
async def test_retract_tool_success(mock_http_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"retracted": True}
    mock_response.raise_for_status = MagicMock()
    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await retract_tool("user-alice", "alice", rel_type="pref_name")
        assert result == {"retracted": True}


@pytest.mark.asyncio
async def test_store_context_tool_success(mock_http_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"stored": True}
    mock_response.raise_for_status = MagicMock()
    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await store_context_tool("some raw text", "user-bob")
        assert result == {"stored": True}


# ── New high-level tool tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_memory_tool_success(mock_http_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"facts": ["You have a dog named Spot."], "preferred_names": {}}
    mock_response.raise_for_status = MagicMock()
    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await recall_memory_tool("pets", "user-alice")
        assert result == {"facts": ["You have a dog named Spot."], "preferred_names": {}}
        mock_http_client.post.assert_called_once()
        call_kwargs = mock_http_client.post.call_args
        assert "/query" in call_kwargs[0][0]
        assert call_kwargs[1]["json"]["text"] == "pets"
        assert call_kwargs[1]["json"]["user_id"] == "user-alice"


@pytest.mark.asyncio
async def test_remember_facts_tool_success(mock_http_client):
    rewrite_response = MagicMock()
    rewrite_response.raise_for_status = MagicMock()
    rewrite_response.json.return_value = {
        "edges": [
            {"subject": "user", "rel_type": "has_pet", "object": "spot", "low_confidence": False}
        ]
    }
    ingest_response = MagicMock()
    ingest_response.raise_for_status = MagicMock()
    ingest_response.json.return_value = {"stored": 1, "fact_class": "A"}

    mock_http_client.post = AsyncMock(side_effect=[rewrite_response, ingest_response])

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await remember_facts_tool("I have a dog named Spot", "user-alice")
        assert result == {"stored": 1, "fact_class": "A"}
        assert mock_http_client.post.call_count == 2
        first_call_url = mock_http_client.post.call_args_list[0][0][0]
        second_call_url = mock_http_client.post.call_args_list[1][0][0]
        assert "/extract/rewrite" in first_call_url
        assert "/ingest" in second_call_url


@pytest.mark.asyncio
async def test_remember_facts_tool_no_edges(mock_http_client):
    rewrite_response = MagicMock()
    rewrite_response.raise_for_status = MagicMock()
    rewrite_response.json.return_value = {
        "edges": [
            {"subject": "user", "rel_type": "has_pet", "object": "spot", "low_confidence": True}
        ]
    }
    mock_http_client.post = AsyncMock(return_value=rewrite_response)

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await remember_facts_tool("maybe I have a dog?", "user-alice")
        assert result["status"] == "no_facts"
        assert "message" in result
        # Should NOT have called /ingest
        assert mock_http_client.post.call_count == 1


@pytest.mark.asyncio
async def test_retract_fact_tool_success(mock_http_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"retracted": True, "fact": "aurora instance_of computer"}
    mock_response.raise_for_status = MagicMock()
    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await retract_fact_tool("forget that Aurora is a computer", "user-alice")
        assert result == {"retracted": True, "fact": "aurora instance_of computer"}
        call_kwargs = mock_http_client.post.call_args
        assert "/retract/correct" in call_kwargs[0][0]
        assert call_kwargs[1]["json"]["text"] == "forget that Aurora is a computer"


# ── Error handling ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_timeout():
    import httpx

    with patch("src.mcp.server._http_client") as mock_client:
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.get = AsyncMock(side_effect=Exception("no provisioning"))
        result = await _call_tool("recall_memory", {"query": "hello", "user_id": "alice"})
        assert "content" in result
        text = result["content"][0]["text"]
        assert "timeout" in json.loads(text)["error"].lower()


@pytest.mark.asyncio
async def test_call_tool_http_500():
    import httpx

    error_response = MagicMock()
    error_response.status_code = 500
    http_error = httpx.HTTPStatusError("error", request=MagicMock(), response=error_response)

    with patch("src.mcp.server._http_client") as mock_client:
        mock_client.post = AsyncMock(side_effect=http_error)
        mock_client.get = AsyncMock(side_effect=Exception("no provisioning"))
        result = await _call_tool("recall_memory", {"query": "hello", "user_id": "alice"})
        text = result["content"][0]["text"]
        assert "500" in json.loads(text)["error"]


@pytest.mark.asyncio
async def test_call_tool_invalid_user_id():
    # Force FAULTLINE_USER_ID to empty so user_id arg validation is exercised
    with patch("src.mcp.server.FAULTLINE_USER_ID", ""):
        result = await _call_tool("query", {"text": "hello", "user_id": ""})
    text = result["content"][0]["text"]
    assert "Invalid user_id" in json.loads(text)["error"]


@pytest.mark.asyncio
async def test_call_tool_unknown_tool():
    with patch("src.mcp.server._http_client") as mock_client:
        mock_client.get = AsyncMock(side_effect=Exception("no provisioning"))
        result = await _call_tool("nonexistent", {"text": "hello", "user_id": "alice"})
        text = result["content"][0]["text"]
        assert "Unknown tool" in json.loads(text)["error"]


# ── User_id isolation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_id_isolation_different_users():
    """Verify different user_ids produce different API call parameters."""
    captured_bodies = []

    async def capture_post(url, **kwargs):
        captured_bodies.append(kwargs.get("json", {}))
        mock = MagicMock()
        mock.json.return_value = {"facts": []}
        mock.raise_for_status = MagicMock()
        return mock

    with patch("src.mcp.server._http_client") as mock_client:
        mock_client.post = AsyncMock(side_effect=capture_post)
        await query_tool("family", "user-alice")
        await query_tool("family", "user-bob")

    assert captured_bodies[0]["user_id"] == "user-alice"
    assert captured_bodies[1]["user_id"] == "user-bob"
    assert captured_bodies[0]["user_id"] != captured_bodies[1]["user_id"]


@pytest.mark.asyncio
async def test_call_tool_invalid_edges():
    result = await _call_tool("ingest", {
        "text": "hello",
        "user_id": "alice",
        "edges": [],  # empty edges
    })
    text = result["content"][0]["text"]
    assert "Invalid edges" in json.loads(text)["error"]


@pytest.mark.asyncio
async def test_call_tool_invalid_retract_subject():
    result = await _call_tool("retract", {
        "user_id": "alice",
        "subject": "",  # empty subject
    })
    text = result["content"][0]["text"]
    assert "subject" in json.loads(text)["error"].lower()


# ── FAULTLINE_USER_ID env override ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_faultline_user_id_env_overrides_argument():
    """Env var FAULTLINE_USER_ID overrides whatever user_id is passed as an argument."""
    captured_bodies = []

    async def capture_post(url, **kwargs):
        captured_bodies.append(kwargs.get("json", {}))
        mock = MagicMock()
        mock.json.return_value = {"facts": []}
        mock.raise_for_status = MagicMock()
        return mock

    with patch("src.mcp.server._http_client") as mock_client:
        mock_client.post = AsyncMock(side_effect=capture_post)
        mock_client.get = AsyncMock(side_effect=Exception("no provisioning"))
        with patch("src.mcp.server.FAULTLINE_USER_ID", "fixed-user-123"):
            result = await _call_tool(
                "recall_memory",
                {"query": "family", "user_id": "should-be-overridden"},
            )

    assert len(captured_bodies) == 1
    assert captured_bodies[0]["user_id"] == "fixed-user-123"
    assert captured_bodies[0]["user_id"] != "should-be-overridden"
