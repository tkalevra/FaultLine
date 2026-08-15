"""CLIENT-CLASS GATES — the mechanical half of the 2026-08-14 authorship fix.

Incident: a build agent (coding assistant over MCP, on a user's credentials) polluted
a real user's seat through the AUTOMATIC write lanes — recall_memory's per-turn harvest
ingested the agent's query text (source="mcp" → user_stated), and the unadvertised-but-
dispatchable store_context tool landed the agent's brief sentences in episodic_log
(source="store_context_deferred") where they were re-mined into durable facts.

The fix's discriminator is WHO IS OPERATING THE CLIENT (identity, captured at the
transport edge into a ContextVar), never what the text looks like. For coding-agent
clients the AUTO-write lanes close; the EXPLICIT remember_facts of a human turn is never
gated — a human talking THROUGH a coding agent must still be remembered.

These tests pin the three halves: the pure classifier (truth table), the ContextVar
(per-request isolation), and the gates themselves (harvest skipped / store_context
directive / remember_facts unchanged in BOTH classes).
"""

import asyncio
import contextvars
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.mcp import client_class
from src.mcp.client_class import (
    CODING_AGENT_CLIENTS,
    current_client_is_coding_agent,
    current_client_name,
    is_coding_agent,
    set_client_class,
)
import src.mcp.server as _server_mod
from src.mcp.server import recall_memory_tool, remember_facts_tool, store_context_tool


# ── Fixtures (mirrors tests/mcp/test_server.py) ─────────────────────────────


@pytest.fixture
def mock_http_client():
    return AsyncMock()


@pytest.fixture(autouse=True)
def _clean_state():
    """Pin the backend URL (no live probing) and reset the client-class ContextVar.

    The ContextVar reset matters as much as the URL pin: a test that classifies the
    client and leaks the set would silently flip every LATER test's gates. Async tests
    set the var inside the pytest-asyncio task (a copied context that dies with the
    task), but the belt-and-braces reset keeps sync leakage impossible too.
    """
    original_detected = _server_mod._FAULTLINE_URL_DETECTED
    _server_mod._FAULTLINE_URL_DETECTED = True
    set_client_class(None)
    yield
    set_client_class(None)
    _server_mod._FAULTLINE_URL_DETECTED = original_detected


def _json_response(payload):
    """A MagicMock httpx response whose .json() returns `payload`."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _get_router(**by_path):
    """Route the tool's GETs by URL substring (the sync sidecars of the ingest path)."""
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


def _posted_urls(mock_client):
    return [c[0][0] for c in mock_client.post.call_args_list]


# ── Unit: is_coding_agent truth table ───────────────────────────────────────


def test_every_registered_coding_agent_name_classifies():
    """Every name in CODING_AGENT_CLIENTS must classify True — the set IS the contract."""
    for name in CODING_AGENT_CLIENTS:
        assert is_coding_agent(name) is True, name


def test_is_coding_agent_normalizes_case_and_whitespace():
    assert is_coding_agent("OpenCode") is True
    assert is_coding_agent("  opencode  ") is True
    assert is_coding_agent("Claude Code") is True
    assert is_coding_agent("CLAUDE-CODE") is True
    assert is_coding_agent("Gemini CLI") is True


def test_is_coding_agent_unknown_none_empty_false():
    """Unknown/None/empty → False: fail toward today's behavior (chat default)."""
    assert is_coding_agent(None) is False
    assert is_coding_agent("") is False
    assert is_coding_agent("   ") is False
    assert is_coding_agent("some-unknown-frontend") is False


def test_is_coding_agent_chat_frontends_false():
    """Browsers and chat frontends are NOT coding agents — their auto-captures are human turns."""
    assert is_coding_agent("OpenWebUI") is False
    assert is_coding_agent("Mozilla/5.0") is False
    assert is_coding_agent("Mozilla") is False
    assert is_coding_agent("faultline-agent") is False


# ── ContextVar: set/read round-trip + per-request isolation ─────────────────


def test_contextvar_round_trip():
    set_client_class("opencode")
    assert current_client_is_coding_agent() is True
    assert current_client_name() == "opencode"
    set_client_class("OpenWebUI")
    assert current_client_is_coding_agent() is False
    assert current_client_name() == "OpenWebUI"


def test_contextvar_unset_reads_false():
    set_client_class(None)
    assert current_client_is_coding_agent() is False
    assert current_client_name() is None


async def test_contextvar_set_in_one_task_does_not_leak():
    """A set at one request's edge must not leak into another concurrent request.

    asyncio tasks copy the context at creation — exactly the per-request isolation the
    transport edge relies on: each HTTP request handler runs as its own task, so the
    coding-agent classification of ONE request can never close another chat request's
    auto-write lanes.
    """
    set_client_class(None)

    async def _request_edge_task():
        # Mimics the transport edge: classify + run a "handler" inside this task only.
        set_client_class("opencode")
        assert current_client_is_coding_agent() is True

    await asyncio.create_task(_request_edge_task())

    # The sibling/outer context was never touched.
    assert current_client_is_coding_agent() is False


def test_contextvar_copy_context_isolation():
    """copy_context semantics: a set inside a copied context stays inside it."""
    set_client_class(None)
    ctx = contextvars.copy_context()
    ctx.run(set_client_class, "cursor")
    assert ctx.run(current_client_is_coding_agent) is True
    assert current_client_is_coding_agent() is False


# ── Transport-edge capture helper (http_server seam) ────────────────────────


def _fake_request(headers: dict) -> MagicMock:
    req = MagicMock()
    req.headers = headers
    return req


def test_capture_prefers_mcp_name_header():
    from src.mcp.http_server import _capture_client_class
    _capture_client_class(_fake_request({"mcp-name": "opencode", "user-agent": "Mozilla/5.0"}))
    assert current_client_name() == "opencode"
    assert current_client_is_coding_agent() is True


def test_capture_falls_back_to_user_agent_leading_token():
    from src.mcp.http_server import _capture_client_class
    _capture_client_class(_fake_request({"user-agent": "opencode/1.18 (linux)"}))
    assert current_client_name() == "opencode"
    assert current_client_is_coding_agent() is True


def test_capture_user_agent_browser_is_chat():
    from src.mcp.http_server import _capture_client_class
    _capture_client_class(_fake_request({"user-agent": "Mozilla/5.0 (X11; Linux x86_64)"}))
    # Leading token "Mozilla" is not a coding agent — chat default, harvest stays open.
    assert current_client_is_coding_agent() is False


def test_capture_no_headers_is_chat_and_never_fails():
    from src.mcp.http_server import _capture_client_class
    _capture_client_class(_fake_request({}))
    assert current_client_name() is None
    assert current_client_is_coding_agent() is False


# ── Gate: recall_memory harvest skipped for coding-agent clients ─────────────


async def test_recall_harvest_skipped_for_coding_agent(mock_http_client):
    """The AUTO harvest must not fire for a coding-agent client — its query is the agent's
    own working text (2026-08-14: harvest of an agent brief wrote it into a real user's
    seat as user_stated). The recall READ itself is untouched."""
    set_client_class("opencode")
    query_response = _json_response({"facts": [], "attributes": {}})

    mock_http_client.post = AsyncMock(side_effect=[query_response])
    mock_http_client.get = AsyncMock(return_value=_json_response({"threshold": 0.70}))

    harvest_spy = AsyncMock(return_value=0)
    with patch("src.mcp.server._http_client", mock_http_client), \
         patch("src.mcp.server._harvest_turn_facts", harvest_spy):
        result = await recall_memory_tool("pets", "user-alice")

    harvest_spy.assert_not_awaited()
    assert not any("/harvest-spans" in u for u in _posted_urls(mock_http_client))
    # The recall read still ran and still returned the byte-identical empty sentinel
    # (benchmark contract — see recall_memory_tool).
    assert any("/query" in u for u in _posted_urls(mock_http_client))
    assert result["memory"] == "No relevant facts found."


async def test_recall_harvest_still_fires_for_chat(mock_http_client):
    """Chat frontend (class unset): the harvest lane is unchanged — a chat query is a
    human turn and question-carried facts must still be captured."""
    classify_response = _json_response({"intent": "QUERY", "confidence": 0.9})
    harvest_response = _json_response({"edges": []})
    query_response = _json_response({"facts": [], "attributes": {}})

    mock_http_client.post = AsyncMock(
        side_effect=[classify_response, harvest_response, query_response])
    mock_http_client.get = AsyncMock(return_value=_json_response({"threshold": 0.70}))

    harvest_spy = AsyncMock(return_value=0)
    with patch("src.mcp.server._http_client", mock_http_client), \
         patch("src.mcp.server._harvest_turn_facts", harvest_spy):
        await recall_memory_tool("pets", "user-alice")

    harvest_spy.assert_awaited_once_with("pets", "user-alice")


# ── Gate: store_context refuses agent-authored text ─────────────────────────


async def test_store_context_agent_client_stores_nothing(mock_http_client):
    """The incident lane itself: a coding agent calling the unadvertised store_context
    tool must store NOTHING (no HTTP POST — nothing reaches episodic_log's deferred
    lane) and get the directive routing the human-statement lane to remember_facts.
    The directive must name ONLY tools that exist on this server."""
    set_client_class("claude-code")
    mock_http_client.post = AsyncMock()

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await store_context_tool("agent operating brief sentences", "user-alice")

    mock_http_client.post.assert_not_awaited()
    assert result["status"] == "error"
    assert result["point_id"] == ""
    assert "HUMAN" in result["message"]
    assert "remember_facts" in result["message"]


async def test_store_context_chat_client_stores_as_before(mock_http_client):
    """Chat class (unset): byte-for-byte today's behavior — POST fires, response passes through."""
    stored = _json_response({"status": "stored", "point_id": "some-uuid"})
    mock_http_client.post = AsyncMock(return_value=stored)

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await store_context_tool("some raw human turn", "user-bob")

    assert result == {"status": "stored", "point_id": "some-uuid"}
    assert "/store_context" in _posted_urls(mock_http_client)[0]


# ── Non-gate: remember_facts explicit human-turn writes in BOTH classes ─────


async def test_remember_facts_write_in_both_classes(mock_http_client, capsys):
    """remember_facts is NEVER client-class gated: a human talking THROUGH a coding agent
    must still be remembered. In both classes the write happens (classify → episodic →
    extract → ingest). The write log carries the client tag for traceability."""
    for client_name in ("opencode", None):
        set_client_class(client_name)
        classify_response = _json_response({"intent": "STATEMENT", "confidence": 0.9})
        episodic_response = _json_response({"ok": True})
        rewrite_response = _json_response({
            "edges": [
                {"subject": "user", "rel_type": "has_pet", "object": "spot",
                 "low_confidence": False}
            ]
        })
        ingest_response = _json_response({"stored": 1, "fact_class": "A"})

        mock_http_client.post = AsyncMock(side_effect=[
            classify_response, episodic_response, rewrite_response, ingest_response,
        ])
        mock_http_client.get = _get_router()

        with patch("src.mcp.server._http_client", mock_http_client):
            result = await remember_facts_tool("I have a dog named Spot", "user-alice")

        # THE POINT: the explicit write happened in BOTH classes.
        assert result["stored"] == 1
        urls = _posted_urls(mock_http_client)
        assert any("/ingest" in u for u in urls), (client_name, urls)
        # Traceability: the write log line names WHICH class made the write.
        err = capsys.readouterr().err
        expected_tag = f"client={client_name or 'chat'}"
        assert expected_tag in err, (client_name, expected_tag, err)
        set_client_class(None)


# ── ROUND-2 CRITIC CATCHES: the intent DIVERTS and the transport-edge wiring ──────────
# A fresh critic live-proved (a) a coding-agent STATEMENT query still auto-wrote through
# recall_memory's intent divert (a BIGGER write than the gated harvest — full LLM triple
# extraction → durable Class A) and (b) deleting the http-edge capture seams broke NO test
# — the wiring had zero coverage. These tests pin both.


async def test_recall_statement_divert_skipped_for_coding_agent(mock_http_client):
    """THE INCIDENT LANE, REOPENED AND CLOSED. A declarative agent sentence (the exact
    shape of the 2026-08-14 brief pollution) classifies STATEMENT; the old code ingested
    it via the divert BEFORE the harvest gate ever ran. Now: coding-agent class → the
    intent diverts (STATEMENT ingest AND CORRECTION retract) are skipped, plain recall
    still runs, and NOT ONE backend write fires."""
    set_client_class("opencode")
    query = _json_response({"facts": [], "attributes": {}})
    mock_http_client.post = AsyncMock(side_effect=[query])
    mock_http_client.get = _get_router()

    with patch("src.mcp.server._http_client", mock_http_client):
        result = await recall_memory_tool(
            "the brain rate limit is 50 requests per hour on the free tier",
            "user-agent-1")

    urls = _posted_urls(mock_http_client)
    writes = [u for u in urls if any(w in u for w in ("/ingest", "/retract", "/store_context", "/episodic"))]
    assert not writes, f"coding-agent recall auto-wrote through: {writes}"
    # and the recall itself still happened
    assert any("/query" in u for u in urls), urls
    assert result  # a renderable recall result came back


async def test_recall_correction_divert_skipped_for_coding_agent(mock_http_client):
    """Same gate, destructive sibling: a CORRECTION-classified agent query must NOT
    supersede facts through the retract divert; chat keeps it."""
    set_client_class("opencode")
    query = _json_response({"facts": [], "attributes": {}})
    mock_http_client.post = AsyncMock(side_effect=[query])
    mock_http_client.get = _get_router()

    with patch("src.mcp.server._http_client", mock_http_client):
        await recall_memory_tool("actually the limit is 1000 not 50", "user-agent-1")

    urls = _posted_urls(mock_http_client)
    assert not any("/retract" in u or "/ingest" in u for u in urls), urls


async def test_recall_statement_divert_still_fires_for_chat(mock_http_client):
    """Chat class: today's behavior — the STATEMENT divert ingests (a human statement
    in a recall query still lands). The gates must close the agent lane ONLY."""
    # URL-routed responses: classify (x2: recall + remember_facts flows), episodic,
    # rewrite, ingest, harvest (chat keeps it), query.
    def _route(url, *a, **kw):
        if "/classify" in url:
            return _json_response({"intent": "STATEMENT", "confidence": 0.95})
        if "/episodic" in url:
            return _json_response({"ok": True})
        if "/extract/rewrite" in url:
            return _json_response({"edges": [{"subject": "user", "rel_type": "has_pet",
                                              "object": "rex", "low_confidence": False}]})
        if "/ingest" in url:
            return _json_response({"stored": 1, "fact_class": "A"})
        if "/harvest" in url:
            return _json_response({"edges": []})
        return _json_response({"facts": [], "attributes": {}})

    async def _post(url, *a, **kw):
        return _route(url, *a, **kw)

    mock_http_client.post = AsyncMock(side_effect=_post)
    mock_http_client.get = _get_router()

    with patch("src.mcp.server._http_client", mock_http_client):
        await recall_memory_tool("my dog is Rex", "user-chat-1")

    urls = _posted_urls(mock_http_client)
    assert any("/ingest" in u for u in urls), f"chat lost the statement divert: {urls}"


# ── The WIRING: real ASGI requests through the actual HTTP transport ────────


def _asgi_auth_patches(hs):
    """Deterministic auth for the ASGI tests: pin the env key (so the Bearer matches)
    and pin the dashboard DSN empty (so no DB credential can suppress env auth)."""
    return (
        patch.object(hs, "MCP_API_KEY", "test-secret-key"),
        patch.object(hs, "_dashboard_dsn", lambda: ""),
    )


async def test_transport_edge_capture_reaches_gate_through_real_asgi():
    """The WIRING, not the helper: a real HTTP request carrying User-Agent
    'opencode/1.18' through the actual ASGI app must close the store_context gate.
    (Round-2 critic: deleting both _capture_client_class seams broke no test.)"""
    import json as _json
    import httpx
    from src.mcp import http_server as hs

    def _jr(payload):
        r = MagicMock(); r.raise_for_status = MagicMock(); r.json.return_value = payload; return r

    posts = []

    async def _post(url, *a, **kw):
        posts.append(url)
        return _jr({"status": "stored", "point_id": "uuid-x"})

    client = MagicMock(); client.post = AsyncMock(side_effect=_post)
    client.get = AsyncMock(return_value=_jr({"threshold": 0.70}))
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "store_context",
                      "arguments": {"text": "agent brief prose",
                                    "user_id": "11111111-1111-1111-1111-111111111111"}}}
    transport = httpx.ASGITransport(app=hs.app)
    p1, p2 = _asgi_auth_patches(hs)
    with p1, p2, \
         patch("src.mcp.server._http_client", client), \
         patch("src.mcp.server._ensure_provisioned", AsyncMock(return_value=True)):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/mcp", json=rpc,
                              headers={"Authorization": "Bearer test-secret-key",
                                       "User-Agent": "opencode/1.18 (linux)"})
    inner = _json.loads(r.json()["result"]["content"][0]["text"])
    assert inner.get("status") == "error" and "HUMAN" in inner.get("message", ""), inner
    assert not posts, f"agent text reached backend despite UA-classified gate: {posts}"


async def test_initialize_clientinfo_does_not_carry_to_later_requests():
    """STATELESS LIMITATION, PINNED AS BEHAVIOR: initialize's clientInfo cannot classify
    a LATER tools/call (the initialize request's context dies with it). A client
    identifying ONLY in the handshake, with a generic SDK User-Agent, is chat-classified
    on tools/call. This is the documented boundary: HTTP classification rides per-request
    headers (mcp-name / product-name User-Agent), and agent deployments should send one."""
    import json as _json
    import httpx
    from src.mcp import http_server as hs

    def _jr(payload):
        r = MagicMock(); r.raise_for_status = MagicMock(); r.json.return_value = payload; return r

    posts = []

    async def _post(url, *a, **kw):
        posts.append(url)
        return _jr({"status": "stored", "point_id": "uuid-x"})

    client = MagicMock(); client.post = AsyncMock(side_effect=_post)
    client.get = AsyncMock(return_value=_jr({"threshold": 0.70}))
    rpc = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
           "params": {"name": "store_context",
                      "arguments": {"text": "agent brief prose",
                                    "user_id": "11111111-1111-1111-1111-111111111111"}}}
    transport = httpx.ASGITransport(app=hs.app)
    p1, p2 = _asgi_auth_patches(hs)
    with p1, p2, \
         patch("src.mcp.server._http_client", client), \
         patch("src.mcp.server._ensure_provisioned", AsyncMock(return_value=True)):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"clientInfo": {"name": "claude-code", "version": "1.0"},
                               "protocolVersion": "2025-03-26"}}
            await ac.post("/mcp", json=init,
                          headers={"Authorization": "Bearer test-secret-key",
                                   "User-Agent": "python-mcp/1.9"})
            r = await ac.post("/mcp", json=rpc,
                              headers={"Authorization": "Bearer test-secret-key",
                                       "User-Agent": "python-mcp/1.9"})
    inner = _json.loads(r.json()["result"]["content"][0]["text"])
    # stateless: the initialize identification did NOT carry — generic UA = chat = stored
    assert inner.get("status") == "stored", inner
    assert posts, "expected the chat-default path to store"


async def test_rest_seam_capture_reaches_gates_through_real_asgi():
    """The REST seam: _capture_client_class inside _resolve_rest_user_id is the ONLY
    classifier for every REST tool route — and no test drove it. A REST POST
    /recall_memory with a coding-agent User-Agent and a STATEMENT-classified body must
    NOT auto-ingest; the same request as chat still diverts."""
    import httpx
    from src.mcp import http_server as hs

    def _jr(payload):
        r = MagicMock(); r.raise_for_status = MagicMock(); r.json.return_value = payload; return r

    async def _make_client(route):
        posts = []

        def _route_resp(url, *a, **kw):
            if "/classify" in url:
                return _json_response({"intent": "STATEMENT", "confidence": 0.95})
            if "/ingest" in url:
                return _json_response({"stored": 1, "fact_class": "A"})
            if "/harvest" in url:
                return _json_response({"edges": []})
            if "/episodic" in url:
                return _json_response({"ok": True})
            if "/extract" in url:
                return _json_response({"edges": []})
            return route(url)

        async def _post(url, *a, **kw):
            posts.append(url)
            return _route_resp(url)

        c = MagicMock()
        c.post = AsyncMock(side_effect=_post)
        c.get = AsyncMock(return_value=_jr({"threshold": 0.70}))
        return c, posts

    body_agent = {"query": "the brain rate limit is 50 requests per hour",
                  "user_id": "11111111-1111-1111-1111-111111111111"}
    transport = httpx.ASGITransport(app=hs.app)

    # coding-agent UA: no ingest may fire
    c, posts = await _make_client(lambda u: _jr({"facts": [], "attributes": {}}))
    p1, p2 = _asgi_auth_patches(hs)
    with p1, p2, \
         patch("src.mcp.server._http_client", c), \
         patch("src.mcp.server._ensure_provisioned", AsyncMock(return_value=True)):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/recall_memory", json=body_agent,
                              headers={"Authorization": "Bearer test-secret-key",
                                       "User-Agent": "opencode/1.18"})
    assert r.status_code == 200, r.text[:200]
    assert not any("/ingest" in u for u in posts), \
        f"REST seam failed to classify the coding agent — auto-ingest fired: {posts}"

    # chat UA: the STATEMENT divert still ingests (today's behavior)
    c2, posts2 = await _make_client(lambda u: _jr({"facts": [], "attributes": {}}))
    p1, p2 = _asgi_auth_patches(hs)
    with p1, p2, \
         patch("src.mcp.server._http_client", c2), \
         patch("src.mcp.server._ensure_provisioned", AsyncMock(return_value=True)):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            r2 = await ac.post("/recall_memory", json=body_agent,
                               headers={"Authorization": "Bearer test-secret-key",
                                        "User-Agent": "OpenWebUI/0.6"})
    assert r2.status_code == 200
    assert any("/extract/rewrite" in u for u in posts2), \
        f"chat lost the statement divert through the REST seam: {posts2}"


# ── The stdio seam: initialize clientInfo classifies the process ────────────


async def test_stdio_initialize_clientinfo_sets_client_class(monkeypatch):
    """stdio is single-client for the process lifetime: the handshake's clientInfo.name
    is captured ONCE by the initialize branch of the REAL run_mcp_server loop and every
    later tools/call on the process reads it. Drives the actual loop (fake stdin: one
    initialize line, then EOF) — not a re-implementation of the capture."""
    import src.mcp.server as srv

    lines = [
        '{"jsonrpc":"2.0","id":0,"method":"initialize","params":'
        '{"clientInfo":{"name":"claude-code","version":"1.0"},'
        '"protocolVersion":"2025-03-26"}}\n',
    ]

    class _FakeStdin:
        def readline(self):
            return lines.pop(0) if lines else ""

    sent = []
    monkeypatch.setattr(srv.sys, "stdin", _FakeStdin())
    monkeypatch.setattr(srv, "_send", lambda resp: sent.append(resp))

    set_client_class(None)
    await srv.run_mcp_server()

    # The initialize was answered (the loop ran the real branch)…
    assert sent and sent[0]["id"] == 0
    assert sent[0]["result"]["serverInfo"]["name"] == "faultline-mcp"
    # …and the handshake's clientInfo classified the process for its lifetime.
    assert current_client_is_coding_agent() is True
    assert current_client_name() == "claude-code"
