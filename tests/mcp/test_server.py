"""Tests for FaultLine MCP server — mock-based, no live API needed.

Tests cover: tool schemas, success responses, error handling, user_id isolation.
"""

import json
import sys
import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.mcp.tools import TOOLS, validate_text, validate_user_id, validate_edges
from src.mcp.server import (
    extract_tool,
    ingest_tool,
    query_tool,
    retract_tool,
    store_context_tool,
    _call_tool,
)


# ── Schema validation ────────────────────────────────────────────────────────


def test_tools_list_has_five_tools():
    assert len(TOOLS) == 5
    names = {t["name"] for t in TOOLS}
    assert names == {"extract", "ingest", "query", "retract", "store_context"}


def test_tools_have_required_schema_fields():
    for tool in TOOLS:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema


def test_extract_schema_requires_text_and_user_id():
    extract = next(t for t in TOOLS if t["name"] == "extract")
    assert set(extract["inputSchema"]["required"]) == {"text", "user_id"}


def test_query_schema_requires_text_and_user_id():
    query = next(t for t in TOOLS if t["name"] == "query")
    assert set(query["inputSchema"]["required"]) == {"text", "user_id"}


def test_ingest_schema_requires_text_user_id_and_edges():
    ingest = next(t for t in TOOLS if t["name"] == "ingest")
    assert set(ingest["inputSchema"]["required"]) == {"text", "user_id", "edges"}


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


# ── Tool handler tests (mocked httpx) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_tool_success():
    mock_response = MagicMock()
    mock_response.json.return_value = {"facts": [], "preferred_names": {}}
    mock_response.raise_for_status = MagicMock()

    with patch("src.mcp.server.httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result = await query_tool("tell me about family", "user-alice")
        assert result == {"facts": [], "preferred_names": {}}
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_extract_tool_success():
    mock_response = MagicMock()
    mock_response.json.return_value = {"entities": []}
    mock_response.raise_for_status = MagicMock()

    with patch("src.mcp.server.httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result = await extract_tool("some text", "user-bob")
        assert result == {"entities": []}


@pytest.mark.asyncio
async def test_ingest_tool_success():
    mock_response = MagicMock()
    mock_response.json.return_value = {"stored": 2}
    mock_response.raise_for_status = MagicMock()

    edges = [{"subject": "user", "object": "paris", "rel_type": "lives_in"}]
    with patch("src.mcp.server.httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result = await ingest_tool("I live in Paris", "user-bob", edges)
        assert result == {"stored": 2}


@pytest.mark.asyncio
async def test_retract_tool_success():
    mock_response = MagicMock()
    mock_response.json.return_value = {"retracted": True}
    mock_response.raise_for_status = MagicMock()

    with patch("src.mcp.server.httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result = await retract_tool("user-alice", "alice", rel_type="pref_name")
        assert result == {"retracted": True}


@pytest.mark.asyncio
async def test_store_context_tool_success():
    mock_response = MagicMock()
    mock_response.json.return_value = {"stored": True}
    mock_response.raise_for_status = MagicMock()

    with patch("src.mcp.server.httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result = await store_context_tool("some raw text", "user-bob")
        assert result == {"stored": True}


# ── Error handling ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_timeout():
    import httpx

    with patch("src.mcp.server.httpx.AsyncClient.post", side_effect=httpx.TimeoutException("timeout")):
        result = await _call_tool("query", {"text": "hello", "user_id": "alice"})
        assert "content" in result
        text = result["content"][0]["text"]
        assert "timeout" in json.loads(text)["error"].lower()


@pytest.mark.asyncio
async def test_call_tool_http_500():
    import httpx

    error_response = MagicMock()
    error_response.status_code = 500
    http_error = httpx.HTTPStatusError("error", request=MagicMock(), response=error_response)

    with patch("src.mcp.server.httpx.AsyncClient.post", side_effect=http_error):
        result = await _call_tool("query", {"text": "hello", "user_id": "alice"})
        text = result["content"][0]["text"]
        assert "500" in json.loads(text)["error"]


@pytest.mark.asyncio
async def test_call_tool_invalid_user_id():
    result = await _call_tool("query", {"text": "hello", "user_id": ""})
    text = result["content"][0]["text"]
    assert "Invalid user_id" in json.loads(text)["error"]


@pytest.mark.asyncio
async def test_call_tool_unknown_tool():
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

    with patch("src.mcp.server.httpx.AsyncClient.post", side_effect=capture_post):
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
