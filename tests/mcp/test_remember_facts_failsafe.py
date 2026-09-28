"""remember_facts fail-safe on a transient LLM/transport failure (robustness).

BUG: a flaky brain (~14% ConnectTimeouts to a tenant BYO endpoint) makes the backend LLM
extractor slow-or-erroring. The MCP `remember_facts_tool` STATEMENT path POSTed /extract/rewrite
and /ingest — and the CORRECTION/RETRACTION divert POSTed /retract/correct — with UNWRAPPED
`raise_for_status()`. A backend 5xx (or an httpx transport timeout) therefore raised out of
`remember_facts_tool` → `rest_remember_facts` (no try) → a FastAPI **500**, dropping the whole turn.
Because a turns-mode benchmark question ingests ~45 turns, ONE transient failure 500'd the question.

The fix wraps ONLY the failing calls, catching the httpx.HTTPError CLASS (transport + 5xx), and:
  - STATEMENT (updated, first-touch-cold-path round 2): retried ONCE, then QUEUED for
    re-extraction + a loud degraded envelope with the cause (no substituted extractor) — the
    fact is retained verbatim; still a graceful non-500 result.
  - /ingest and /retract/correct: return a graceful `degraded` result — never a 500.
A genuine programming error (NOT an httpx.HTTPError) still surfaces — we degrade on the LLM/transport
failure class, we do not blanket-swallow real bugs. The success path (backend healthy) is unchanged.

Mock-based, no live API. Follows tests/mcp/test_server.py conventions.
"""

import os
import sys

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.mcp.server as S
from src.mcp.server import remember_facts_tool, retract_fact_tool


_UID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _resp(json_body, status=200):
    """A MagicMock httpx.Response whose raise_for_status() raises a real httpx.HTTPStatusError on 5xx."""
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=json_body)
    if status >= 400:
        r.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(str(status), request=MagicMock(), response=r)
        )
    else:
        r.raise_for_status = MagicMock()
    return r


@pytest.fixture(autouse=True)
def _pin_ready():
    """Skip the URL-probe + provisioning gate so every request stays inside the mock."""
    S._FAULTLINE_URL_DETECTED = True
    S._provisioned_users.add(_UID)
    yield
    S._provisioned_users.discard(_UID)


def _emit(x):
    """A configured route value is either a response mock (has .status_code) or a plain
    side-effect fn (raises). NOTE: a MagicMock response is ITSELF callable, so discriminate on
    the response shape (.status_code), not on callable()."""
    if x is not None and not hasattr(x, "status_code"):
        return x()   # plain function → invoke it (may raise a transport error)
    return x


def _router(rewrite=None, ingest=None, harvest=None, retract=None, route="rewrite"):
    """Build (post, get) async mocks that route by URL. `rewrite`/`ingest`/`harvest`/`retract` are
    either a response mock OR a plain side-effect fn (e.g. raise). Unmatched URLs → 200 {}."""
    async def post(url, *a, **k):
        if "/classify-intent" in url:
            return _resp({"intent": "STATEMENT", "confidence": 0.9})
        if "/episodic/append" in url:
            return _resp({"ok": True})
        if "/extract/rewrite" in url and rewrite is not None:
            return _emit(rewrite)
        if "/harvest-spans" in url and harvest is not None:
            return harvest
        if "/ingest" in url and ingest is not None:
            return ingest
        if "/retract/correct" in url and retract is not None:
            return _emit(retract)
        return _resp({})

    async def get(url, *a, **k):
        if "/confidence-gate" in url:
            return _resp({"threshold": 0.70})
        if "/internal/ingest-route" in url:
            return _resp({"statement_extractor": route, "ingest_enabled": True})
        return _resp({})

    mc = MagicMock()
    mc.post = AsyncMock(side_effect=post)
    mc.get = AsyncMock(side_effect=get)
    return mc


# ── (a) transient rewrite failure → NO raise, NO 500, and NO substituted extractor ──────────
# first-touch-cold-path round 2 (gap A): the deterministic-harvest SUBSTITUTION this file used to
# pin is retired — the brain chose /extract/rewrite; a transport failure there is retried once
# (budget MCP_BRAIN_TIMEOUT_EXTRACT_REWRITE) and then the turn is QUEUED for re-extraction and the
# caller told loudly with the cause. Still no raise, still no 500 — but never "stored" via a lane
# the brain did not pick.

@pytest.mark.asyncio
async def test_rewrite_5xx_is_retried_then_queued_no_500():
    """Backend /extract/rewrite returns 500 (transient LLM failure) → remember_facts_tool must NOT
    raise; the call is retried once, then the turn is queued and reported degraded with the cause."""
    mc = _router(
        rewrite=_resp({"error": "boom"}, status=500),
        harvest=_resp({"edges": [{"subject": "user", "rel_type": "owns", "object": "corolla"}], "spans": 1}),
        ingest=_resp({"status": "ok", "committed": 1}),
    )
    with patch("src.mcp.server._http_client", mc), \
         patch("src.mcp.server._defer_statement_extraction", AsyncMock(return_value=True)):
        res = await remember_facts_tool("I drive a Corolla and fixed the fence last week", _UID)
    assert res["status"] == "degraded" and res["isError"] is True
    assert res["brain_unavailable"] == "/extract/rewrite" and "HTTP 500" in res["cause"]
    assert res["queued"] is True and res["retry_pending"] is True
    called = [c.args[0] for c in mc.post.call_args_list]
    assert sum("/extract/rewrite" in u for u in called) == 2   # attempt + one retry
    assert not any("/harvest-spans" in u for u in called)     # no substituted extractor
    assert not any("/ingest" in u for u in called)


@pytest.mark.asyncio
async def test_rewrite_connect_timeout_transport_error_no_500():
    """A pure httpx TRANSPORT error (ConnectTimeout, no HTTP response at all) must also degrade,
    not raise — this is the exact ~14% dashscope ConnectTimeout failure mode from prod."""
    def _raise():
        raise httpx.ConnectTimeout("connect timed out")
    mc = _router(
        rewrite=_raise,
        harvest=_resp({"edges": [{"subject": "user", "rel_type": "owns", "object": "corolla"}], "spans": 1}),
        ingest=_resp({"status": "ok", "committed": 1}),
    )
    with patch("src.mcp.server._http_client", mc), \
         patch("src.mcp.server._defer_statement_extraction", AsyncMock(return_value=True)):
        res = await remember_facts_tool("I drive a Corolla", _UID)
    assert res["status"] == "degraded" and "ConnectTimeout" in res["cause"]
    assert res["queued"] is True
    called = [c.args[0] for c in mc.post.call_args_list]
    assert not any("/harvest-spans" in u for u in called)


@pytest.mark.asyncio
async def test_rewrite_failure_and_deterministic_empty_degrades_no_500():
    """Rewrite fails AND the deterministic harvest also produces nothing (brain fully down) →
    a graceful `degraded` result, still NO raise / NO 500."""
    mc = _router(
        rewrite=_resp({"error": "boom"}, status=503),
        harvest=_resp({"edges": [], "spans": 0}),
    )
    with patch("src.mcp.server._http_client", mc):
        res = await remember_facts_tool("I drive a Corolla", _UID)
    assert res["status"] == "degraded"
    assert res.get("committed") == 0


# ── (b) transient /ingest failure → NO raise ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_5xx_degrades_no_500():
    """Extraction succeeds but the store commit hits a transient 5xx → graceful degrade, no raise."""
    mc = _router(
        rewrite=_resp({"status": "success", "edges": [{"subject": "user", "rel_type": "owns", "object": "corolla"}]}),
        ingest=_resp({"error": "db blip"}, status=500),
    )
    with patch("src.mcp.server._http_client", mc):
        res = await remember_facts_tool("I drive a Corolla", _UID)
    assert res["status"] == "degraded"
    assert res.get("committed") == 0


# ── (b') transient /retract/correct failure (CORRECTION/RETRACTION divert) → NO raise ────────────

class _FakeAsyncClientCM:
    """Stand-in for `async with httpx.AsyncClient(...) as client:` — retract_fact_tool builds its
    OWN client (90s timeout) rather than using _http_client, so we patch the constructor."""
    def __init__(self, resp):
        self._resp = resp
    def __call__(self, *a, **k):
        return self
    async def __aenter__(self):
        client = MagicMock()
        client.post = AsyncMock(return_value=self._resp)
        return client
    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_retract_correct_5xx_degrades_no_500():
    """A CORRECTION/RETRACTION turn whose LLM-heavy /retract/correct call 5xx's must degrade,
    not 500 — remember_facts_tool diverts here unwrapped."""
    fake_client = _FakeAsyncClientCM(_resp({"error": "boom"}, status=500))
    with patch("src.mcp.server.httpx.AsyncClient", fake_client):
        res = await retract_fact_tool("actually forget my car", _UID, classified_intent="RETRACTION")
    assert res["status"] == "degraded"


# ── (c) SUCCESS PATH UNCHANGED — the wrapper is transparent on a healthy backend ────────────────

@pytest.mark.asyncio
async def test_success_path_unchanged_when_backend_healthy():
    """With a healthy backend, the STATEMENT path is byte-for-byte today's behavior: /extract/rewrite
    → /ingest, returning the /ingest response. The fail-safe try only ever wraps the two calls; a 200
    flows straight through."""
    ingest_body = {"status": "ok", "committed": 2, "staged": 0}
    mc = _router(
        rewrite=_resp({"status": "success",
                       "edges": [{"subject": "user", "rel_type": "owns", "object": "corolla"}]}),
        ingest=_resp(dict(ingest_body)),
    )
    with patch("src.mcp.server._http_client", mc):
        res = await remember_facts_tool("I drive a Corolla and it is red", _UID)
    # Same shape today's code returns: the ingest response + the remainder_stored key.
    assert res["status"] == "ok"
    assert res["committed"] == 2
    called = [c.args[0] for c in mc.post.call_args_list]
    assert any("/extract/rewrite" in u for u in called)
    assert any("/ingest" in u for u in called)
    # The deterministic fallback must NOT fire on the success path.
    assert not any("/harvest-spans" in u for u in called)


# ── (d) a non-httpx error at the client CALL is surfaced with its name — never silently swallowed ──

@pytest.mark.asyncio
async def test_non_http_error_is_surfaced_in_the_cause():
    """httpx raises a bare RuntimeError for a request on a CLOSED client, so the brain-call seam
    treats ANY exception at the call as "no settled answer" (first-touch-cold-path round 2). It is
    not blanket-swallowed: the exception NAME rides the degraded envelope's `cause` and the
    `brain_call.transport_error` log lines, and the turn is queued rather than 500'd."""
    def _raise_bug():
        raise ValueError("genuine programming bug")
    mc = _router(rewrite=_raise_bug)
    with patch("src.mcp.server._http_client", mc), \
         patch("src.mcp.server._defer_statement_extraction", AsyncMock(return_value=True)):
        res = await remember_facts_tool("I drive a Corolla", _UID)
    assert res["status"] == "degraded" and res["isError"] is True
    # The exception CLASS rides the envelope; the message itself is redacted to a `(ref …)`
    # by src/api/errors.py::public_detail (a sibling lane's contract — the full text is logged
    # under that ref, never shown to the model).
    assert "ValueError" in res["cause"], res["cause"]
