"""CLIENT-NAME INDEPENDENCE — capability never varies by client name.

RECTIFIED 2026-08-15 (owner ruling). History: the 2026-08-14 authorship incident
(a build agent on a user's credentials polluted that user's memory through the automatic
write lanes) was first "fixed" by classifying client names against a hard-coded
list and closing store_context / recall's intent diverts / recall's turn harvest
for matches. That made the MCP capability depend on the ``mcp-name`` label —
wrong: an authorized bearer gets the full toolset identically whatever the
client calls itself. AUTH is the gate, and the only gate. The client name
survives as a LABEL (write-log traceability tag), never admission.

These tests pin both halves: (1) the label machinery (capture seams, per-request
isolation) and the ABSENCE of any classification predicate, and (2) the
independence itself — every formerly-gated lane behaves IDENTICALLY for a
formerly-blocklisted name ("opencode") and an unlabeled chat client, through the
real tool handlers AND the real ASGI transport.
"""

import asyncio
import contextvars
import json as _json
import os as _os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.mcp import client_class
from src.mcp.client_class import current_client_name, set_client_class
import src.mcp.server as _server_mod
from src.mcp.server import recall_memory_tool, remember_facts_tool, store_context_tool

# The two arms of every A/B below: a name that WAS on the deleted blocklist, and
# the unlabeled chat default. Independence means: no observable difference.
AGENT_NAME = "opencode"


# ── Fixtures (mirrors tests/mcp/test_server.py) ─────────────────────────────


@pytest.fixture
def mock_http_client():
    return AsyncMock()


@pytest.fixture(autouse=True)
def _clean_state():
    """Pin the backend URL (no live probing) and reset the client-label ContextVar.

    The ContextVar reset matters: a test that labels the client and leaks the set
    would mislabel every LATER test's write logs. Async tests set the var inside
    the pytest-asyncio task (a copied context that dies with the task), but the
    belt-and-braces reset keeps sync leakage impossible too.
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
    resp.status_code = 200  # a real httpx answer has an int status (the append classifier reads it)
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


# ── The rectification itself: no name classification exists ─────────────────


def test_no_classification_predicate_exists():
    """The hard-coded name list and every is-a-coding-agent predicate are GONE.

    This is the 2026-08-15 rectification pinned as an import contract: capability
    must never branch on the client name again. If this test fails because
    someone reintroduced a predicate/list, re-read the owner ruling before
    "fixing" the test.
    """
    for banned in ("is_coding_agent", "current_client_is_coding_agent",
                   "CODING_AGENT_CLIENTS"):
        assert not hasattr(client_class, banned), banned
    import inspect
    src = inspect.getsource(client_class)
    assert "CODING_AGENT_CLIENTS" not in src
    assert "MCP_CODING_AGENTS" not in src


def test_no_gate_reads_in_server_source():
    """No server.py / http_server.py code path consults the client label for admission."""
    import src.mcp.http_server as hs
    import inspect
    for mod in (_server_mod, hs):
        src = inspect.getsource(mod)
        assert "current_client_is_coding_agent" not in src
        assert "is_coding_agent" not in src


# ── Label machinery: capture seams + per-request isolation ──────────────────


def _fake_request(headers: dict) -> MagicMock:
    req = MagicMock()
    req.headers = headers
    return req


def test_capture_prefers_mcp_name_header():
    from src.mcp.http_server import _capture_client_class
    _capture_client_class(_fake_request({"mcp-name": "some-harness", "user-agent": "Mozilla/5.0"}))
    assert current_client_name() == "some-harness"


def test_capture_falls_back_to_user_agent_leading_token():
    from src.mcp.http_server import _capture_client_class
    _capture_client_class(_fake_request({"user-agent": f"{AGENT_NAME}/1.18 (linux)"}))
    assert current_client_name() == AGENT_NAME


def test_capture_no_headers_is_none_and_never_fails():
    from src.mcp.http_server import _capture_client_class
    _capture_client_class(_fake_request({}))
    assert current_client_name() is None


def test_contextvar_round_trip():
    set_client_class(AGENT_NAME)
    assert current_client_name() == AGENT_NAME
    set_client_class(None)
    assert current_client_name() is None


async def test_contextvar_set_in_one_task_does_not_leak():
    """A label set at one request's edge must not leak into another concurrent request.

    asyncio tasks copy the context at creation — exactly the per-request isolation
    the transport edge relies on: each HTTP request handler runs as its own task,
    so ONE request's client label can never label another request's writes.
    """
    set_client_class(None)

    async def _request_edge_task():
        set_client_class(AGENT_NAME)
        assert current_client_name() == AGENT_NAME

    await asyncio.create_task(_request_edge_task())
    assert current_client_name() is None


def test_contextvar_copy_context_isolation():
    """copy_context semantics: a label set inside a copied context stays inside it."""
    set_client_class(None)
    ctx = contextvars.copy_context()
    ctx.run(set_client_class, "cursor")
    assert ctx.run(current_client_name) == "cursor"
    assert current_client_name() is None


# ── Independence A/B: the formerly-gated lanes, agent name vs chat ───────────


async def test_store_context_identical_in_both_classes(mock_http_client):
    """store_context_tool: the 2026-08-14 incident lane. OLD: refused for the agent
    name (no POST, error directive). NOW (owner ruling): both arms POST and pass
    the response through byte-for-byte — auth is the gate, the name is not."""
    for client_name in (AGENT_NAME, None):
        set_client_class(client_name)
        stored = _json_response({"status": "stored", "point_id": "some-uuid"})
        mock_http_client.post = AsyncMock(return_value=stored)

        with patch("src.mcp.server._http_client", mock_http_client):
            result = await store_context_tool("some raw turn", "user-bob")

        assert result == {"status": "stored", "point_id": "some-uuid"}, client_name
        assert "/store_context" in _posted_urls(mock_http_client)[0], client_name
        mock_http_client.post.reset_mock()
        set_client_class(None)


async def test_recall_harvest_fires_in_both_classes(mock_http_client):
    """recall_memory's intent-independent harvest: the agent-name arm used to skip
    it. NOW: the harvest fires for BOTH arms — question-carried facts are stored
    whatever the client calls itself."""
    for client_name in (AGENT_NAME, None):
        set_client_class(client_name)
        mock_http_client.post = AsyncMock(side_effect=[
            _json_response({"intent": "QUERY", "confidence": 0.9}),   # classify
            _json_response({"edges": []}),                            # harvest
            _json_response({"facts": [], "attributes": {}}),          # query
        ])
        mock_http_client.get = AsyncMock(return_value=_json_response({"threshold": 0.70}))

        harvest_spy = AsyncMock(return_value=0)
        with patch("src.mcp.server._http_client", mock_http_client), \
             patch("src.mcp.server._harvest_turn_facts", harvest_spy):
            await recall_memory_tool("pets", "user-alice")

        # ⚠️ THE SOURCE ARGUMENT IS LOAD-BEARING AND WAS CARRIED HERE BY HAND. The suite this
        # file replaces pinned source="unattested" on this call, and deleting that file without
        # carrying the assertion across would have retired the AUTHORSHIP guarantee while
        # looking like it only removed a name-gating test. It is the reason de-gating is safe:
        # the harvest claims nobody said this, for every client, so there is nothing left for a
        # name check to protect against.
        harvest_spy.assert_awaited_once_with(
            "pets", "user-alice", source="unattested"), client_name
        urls = _posted_urls(mock_http_client)
        assert any("/query" in u for u in urls), (client_name, urls)
        mock_http_client.post.reset_mock()
        set_client_class(None)


async def test_recall_statement_divert_ingests_in_both_classes(mock_http_client):
    """THE INCIDENT LANE, INVERTED. A declarative sentence classifies STATEMENT;
    the recall divert ingests it. The old gate skipped this for the agent name —
    the exact pollution protection the owner ruled out. NOW: /ingest fires in
    BOTH arms identically."""
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

    for client_name in (AGENT_NAME, None):
        set_client_class(client_name)
        mock_http_client.post = AsyncMock(side_effect=_post)
        mock_http_client.get = _get_router()

        with patch("src.mcp.server._http_client", mock_http_client):
            await recall_memory_tool("my dog is Rex", "user-x")

        urls = _posted_urls(mock_http_client)
        assert any("/ingest" in u for u in urls), \
            f"{client_name!r} lost the statement divert: {urls}"
        mock_http_client.post.reset_mock()
        set_client_class(None)


async def test_remember_facts_write_and_authorship_note_in_both_classes(
        mock_http_client, capsys):
    """remember_facts was never gated and stays ungated: the write happens in both
    arms, the confirmation reinforces authorship, and the write log carries the
    client LABEL for traceability (the label's only write-side use)."""
    for client_name in (AGENT_NAME, None):
        set_client_class(client_name)
        mock_http_client.post = AsyncMock(side_effect=[
            _json_response({"intent": "STATEMENT", "confidence": 0.9}),  # classify
            # the REAL /episodic/append answer: the "Captured the user's own words" clause is a
            # retention claim gated on a `stored` answer (round 10); `{"ok": true}` is no answer
            # the server sends and classifies as a refusal → the honest not-retained clause.
            _json_response({"status": "stored", "id": 1}),               # episodic
            _json_response({"edges": [{"subject": "user", "rel_type": "has_pet",
                                       "object": "spot", "low_confidence": False}]}),
            _json_response({"stored": 1, "fact_class": "A"}),            # ingest
        ])
        mock_http_client.get = _get_router()

        with patch("src.mcp.server._http_client", mock_http_client):
            result = await remember_facts_tool("I have a dog named Spot", "user-alice")

        assert result["stored"] == 1
        urls = _posted_urls(mock_http_client)
        assert any("/ingest" in u for u in urls), (client_name, urls)
        assert "user's own words" in result["message"], client_name
        # Traceability: the write log line names WHICH client made the write.
        err = capsys.readouterr().err
        expected_tag = f"client={client_name or 'chat'}"
        assert expected_tag in err, (client_name, expected_tag, err)
        set_client_class(None)


# ── Independence through the REAL ASGI transport (the wiring, not the helper) ─


def _asgi_rpc(name, args, rpc_id):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


async def test_agent_ua_store_context_stores_through_real_asgi():
    """The old pin, INVERTED (round-2 wiring test). A real HTTP request with
    User-Agent 'opencode/1.18' through the actual ASGI app used to be REFUSED by
    the store_context gate. NOW it stores — byte-for-byte what a chat client gets."""
    _os.environ.setdefault("FAULTLINE_API_KEY", "test-secret-key")

    def _jr(payload):
        r = MagicMock(); r.raise_for_status = MagicMock(); r.json.return_value = payload; return r

    posts = []

    async def _post(url, *a, **kw):
        posts.append(url)
        return _jr({"status": "stored", "point_id": "uuid-x"})

    client = MagicMock(); client.post = AsyncMock(side_effect=_post)
    client.get = AsyncMock(return_value=_jr({"threshold": 0.70}))
    rpc = _asgi_rpc("store_context",
                    {"text": "agent brief prose",
                     "user_id": "11111111-1111-1111-1111-111111111111"}, 1)
    transport = httpx.ASGITransport(app=__import__(
        "src.mcp.http_server", fromlist=["app"]).app)
    with patch("src.mcp.server._http_client", client), \
         patch("src.mcp.server._ensure_provisioned", AsyncMock(return_value=True)), \
         patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/mcp", json=rpc,
                              headers={"Authorization": "Bearer test-secret-key",
                                       "User-Agent": f"{AGENT_NAME}/1.18 (linux)"})
    inner = _json.loads(r.json()["result"]["content"][0]["text"])
    assert inner.get("status") == "stored", inner
    assert posts, "agent-named client was silently refused: capability gated on name"


async def test_rest_seam_agent_ua_statement_diverts_through_real_asgi():
    """REST seam: /recall_memory with a formerly-blocklisted User-Agent and a
    STATEMENT-classified body must take the SAME divert a chat client takes —
    the auto-ingest fires for both (the old test asserted it did NOT for agents)."""
    _os.environ.setdefault("FAULTLINE_API_KEY", "test-secret-key")

    def _jr(payload):
        r = MagicMock(); r.raise_for_status = MagicMock(); r.json.return_value = payload; return r

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
        return _jr({"facts": [], "attributes": {}})

    posts = []

    async def _post(url, *a, **kw):
        posts.append(url)
        return _route_resp(url)

    c = MagicMock()
    c.post = AsyncMock(side_effect=_post)
    c.get = AsyncMock(return_value=_jr({"threshold": 0.70}))
    body = {"query": "the brain rate limit is 50 requests per hour",
            "user_id": "11111111-1111-1111-1111-111111111111"}
    transport = httpx.ASGITransport(app=__import__(
        "src.mcp.http_server", fromlist=["app"]).app)
    with patch("src.mcp.server._http_client", c), \
         patch("src.mcp.server._ensure_provisioned", AsyncMock(return_value=True)), \
         patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/recall_memory", json=body,
                              headers={"Authorization": "Bearer test-secret-key",
                                       "User-Agent": f"{AGENT_NAME}/1.18"})
    assert r.status_code == 200, r.text[:200]
    assert any("/extract/rewrite" in u for u in posts), \
        f"agent-named REST client lost the statement divert: {posts}"


async def test_initialize_clientinfo_does_not_carry_to_later_requests():
    """STATELESS LIMITATION, PINNED AS BEHAVIOR (unchanged by the rectification):
    initialize's clientInfo cannot label a LATER tools/call (the initialize
    request's context dies with it). A client identifying ONLY in the handshake,
    with a generic SDK User-Agent, is unlabeled on tools/call — which now changes
    nothing anyway: unlabeled and labeled clients get identical capability. The
    label rides per-request headers (mcp-name / product-name User-Agent) purely
    for traceability.
    """
    _os.environ.setdefault("FAULTLINE_API_KEY", "test-secret-key")

    def _jr(payload):
        r = MagicMock(); r.raise_for_status = MagicMock(); r.json.return_value = payload; return r

    posts = []

    async def _post(url, *a, **kw):
        posts.append(url)
        return _jr({"status": "stored", "point_id": "uuid-x"})

    client = MagicMock(); client.post = AsyncMock(side_effect=_post)
    client.get = AsyncMock(return_value=_jr({"threshold": 0.70}))
    rpc = _asgi_rpc("store_context",
                    {"text": "agent brief prose",
                     "user_id": "11111111-1111-1111-1111-111111111111"}, 2)
    transport = httpx.ASGITransport(app=__import__(
        "src.mcp.http_server", fromlist=["app"]).app)
    with patch("src.mcp.server._http_client", client), \
         patch("src.mcp.server._ensure_provisioned", AsyncMock(return_value=True)), \
         patch("src.mcp.http_server.MCP_API_KEY", "test-secret-key"):
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
    # stateless: the initialize identification did NOT carry — and since nothing
    # gates on the name, the generic-UA request stores like any authorized client.
    assert inner.get("status") == "stored", inner
    assert posts, "expected the store to happen"
