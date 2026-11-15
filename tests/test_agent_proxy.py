"""Phase 2A keystone — the through-FaultLine agent LLM-proxy + tenant-delegate auth.

Covers the non-negotiable invariants of the two new SAAS_MODE-gated endpoints
(``POST /v1/agent/chat`` + ``POST /v1/auth/tenant-delegate``) and PINS the serve-mode
``/expand`` bypass fix at the unit level (the live pre-prod proof is separate):

  agent/chat
    1. fail-closed off-core   → 404 when SAAS_MODE is off (the surface does not exist).
    2. seat resolution reuse  → 401 with no/invalid bearer (the existing require_auth seam).
    3. brain stays server-side → fail-closed 409 when no brain configured (NO global fallback).
    4. backend guard          → 501 for a non-OpenAI-compatible backend (no half-translation).
    5. serve-mode does NOT block chat → a frozen seat still chats (chat is a brain READ, not a
                                 memory write; write_gate_block is intentionally not consulted).
    6. streams + key-safe     → 200 stream + 200 non-stream; the brain key never appears in the
                                 response body or headers.

  tenant-delegate
    7. fail-closed off-core   → 404 when SAAS_MODE is off.
    8. happy path             → 200 + a delegate token that verifies + carries the account scope.
    9. bad password           → 401 (generic — no user enumeration).
   10. MFA required           → 401 when TOTP missing/wrong; 200 when correct.
   11. operator realm         → 403 (no account scope to delegate).
   12. realm isolation        → a delegate token cannot satisfy the session verifier & vice-versa.

  /expand serve-mode bypass (deliverable #3, SHIPPED — pinned here)
   13. _learn_via_llm gates on _serve_mode_blocks_divert BEFORE the global ingest probe — a
       frozen seat's /expand returns the read-only status and schedules NO learn.

Control-plane + LLM calls are monkeypatched, so this file is green with no DB / no network.
Run: python3 tools/fltest.py --bug FLAG2A --test tests/test_agent_proxy.py -q
     (tests/ is gitignored → ``git add -f tests/test_agent_proxy.py``).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from saas import agent_proxy


@pytest.fixture
def app_with_proxy(monkeypatch):
    """Mount ONLY the agent_proxy router on a bare FastAPI app, with controllable seams.

    All SaaS control-plane + httpx calls are injected via this fixture's state dict so a test
    can flip them without touching real infrastructure."""
    from saas import agent_proxy, dashboard_auth

    state = {
        "saas_on": True,
        "principal": "u-principal",      # a resolved flu_ seat principal
        "user_id": "10d7d879-63cd-4f31-92ce-f2c9edb760ab",
        "account_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        # The serve-entitlement gate result (Phase 2C). Default ALLOW so the happy-path tests
        # reach the brain; trial/refusal tests flip this. (None = let the real gate run, which
        # needs a DB; the unit suite has no DB, so every test sets one of the two explicitly.)
        "serve": (True, None),
        "brain": {
            "backend_type": "openai",
            "base_url": "https://brain.example.test",
            "model": "test-model",
            "api_key": "SECRET-BRAIN-KEY-NEVER-LEAK",
            "extra_headers": {},
        },
        "upstream_status": 200,
        "upstream_body": {"id": "chatcmpl-x", "choices": [{"message": {"role": "assistant",
                                                                       "content": "PONG"}}]},
        "upstream_stream_chunks": [
            b'data: {"choices":[{"delta":{"content":"HEL"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"LO"}}]}\n\n',
            b'data: [DONE]\n\n',
        ],
        "captured_headers": None,
        "captured_payload": None,
    }

    # SAAS_MODE toggle for the router's _require_saas gate.
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: bool(state["saas_on"]))

    # Serve-entitlement gate (Phase 2C / chatbot trial). The unit suite has no DB, so the real
    # gate would always fail-closed to ``unverifiable``; we inject the parked result instead. A
    # trial test sets state["serve"] = (True, None) to prove a trial flips the gate to allow, or a
    # refusal tuple to prove expiry/revocation fail-closes.
    from saas import agent_entitlement as _ae
    monkeypatch.setattr(_ae, "serve_allowed",
                        lambda aid, uid: state["serve"] if state["serve"] is not None
                        else (False, {"reason_code": "unverifiable", "status": 403,
                                      "message": "no DB in unit suite"}))

    def _require_auth(request: Request):
        # Treat "bad" as no credential → 401; else the resolved principal.
        auth = request.headers.get("authorization", "")
        if auth.endswith("bad") or not auth:
            raise HTTPException(status_code=401, detail="Unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})
        return state["principal"]

    def _resolve_user_id(request, body_user_id, principal):
        # BOLA-safe stub: always resolves to the configured seat.
        return state["user_id"]

    def _account_for(principal, user_id):
        return state["account_id"]

    logs: list[str] = []
    app = FastAPI()
    app.include_router(agent_proxy.build_agent_proxy_router(
        require_auth=_require_auth,
        resolve_user_id=_resolve_user_id,
        account_for=_account_for,
        log=logs.append,
    ))

    # Fake the brain resolver — returns whatever the test parked in state["brain"].
    monkeypatch.setattr("saas.seat_brain.resolve_brain",
                        lambda uid, aid: state["brain"] if state["brain"] else None)

    # Fake httpx.AsyncClient so the chat test never hits the network and we can assert the
    # brain key was sent UPSTREAM (in headers) but never reaches the downstream response.
    class _FakeResp:
        def __init__(self, status, body):
            self.status_code = status
            self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.headers = {"content-type": "application/json"}

    class _FakeStreamCtx:
        def __init__(self, payload, headers):
            self.payload = payload
            self.headers = headers
            self.status_code = state["upstream_status"]

        async def __aenter__(self):
            state["captured_payload"] = self.payload
            state["captured_headers"] = self.headers
            return self

        async def __aexit__(self, *exc):
            return False

        async def aread(self):
            return (json.dumps({"error": "upstream"}).encode()
                    if self.status_code != 200 else b"")

        async def aiter_raw(self):
            for c in state["upstream_stream_chunks"]:
                yield c

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            state["captured_payload"] = json
            state["captured_headers"] = headers
            return _FakeResp(state["upstream_status"], state["upstream_body"])

        def stream(self, method, url, json=None, headers=None):
            return _FakeStreamCtx(json, headers)

    monkeypatch.setattr(agent_proxy.httpx, "AsyncClient", _FakeAsyncClient)

    return app, state, logs


# ── helper to build the chat body ─────────────────────────────────────────────────
def _chat_body(stream=False, **kw):
    body = {"messages": [{"role": "user", "content": "ping"}], "stream": stream}
    body.update(kw)
    return body


# ════════════════════════════════════════════════════════════════════════════════
# agent/chat — invariants 1–6
# ════════════════════════════════════════════════════════════════════════════════

def test_agent_chat_404_when_saas_off(app_with_proxy):
    """1. fail-closed off-core — the surface does not exist on the open core."""
    app, state, _ = app_with_proxy
    state["saas_on"] = False
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat", json=_chat_body(), headers={"Authorization": "Bearer x"})
    assert r.status_code == 404


def test_agent_chat_401_no_bearer(app_with_proxy):
    """2. seat resolution reuse — the existing require_auth seam enforces the flu_ token."""
    app, _, _ = app_with_proxy
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat", json=_chat_body())
    assert r.status_code == 401


def test_agent_chat_409_no_brain_fail_closed(app_with_proxy):
    """3. brain stays server-side — no brain → 409, NEVER a global fallback (surprise spend)."""
    app, state, _ = app_with_proxy
    state["brain"] = None
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat", json=_chat_body(), headers={"Authorization": "Bearer ok"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "brain_not_configured"


def test_agent_chat_no_brain_sentinel_fail_closed(app_with_proxy):
    """3b. the no_brain sentinel (account exists but no brain row) is also fail-closed 409."""
    app, state, _ = app_with_proxy
    state["brain"] = {"no_brain": True, "account_id": state["account_id"]}
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat", json=_chat_body(), headers={"Authorization": "Bearer ok"})
    assert r.status_code == 409


def test_agent_chat_501_unsupported_backend(app_with_proxy):
    """4. backend guard — ollama/anthropic are not half-translated; clean 501."""
    app, state, _ = app_with_proxy
    state["brain"]["backend_type"] = "ollama"
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat", json=_chat_body(), headers={"Authorization": "Bearer ok"})
    assert r.status_code == 501
    assert r.json()["error"]["code"] == "backend_not_supported"


def test_agent_chat_serve_mode_does_not_block(app_with_proxy):
    """5. a FROZEN (serve) seat may still chat — chat is a brain READ, not a memory write.

    The proof is structural: agent_chat never consults write_gate_block. We assert the chat
    succeeds even though the seat's ingest is frozen (state here is irrelevant to the path;
    the point is the endpoint has no freeze check to flip)."""
    app, _, _ = app_with_proxy
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat", json=_chat_body(), headers={"Authorization": "Bearer ok"})
    assert r.status_code == 200
    # The seat is conceptually frozen; chat still happened. (No write_gate_block in the path.)


def test_agent_chat_non_stream_proxies_and_key_stays_server_side(app_with_proxy):
    """6. non-stream → 200 + the brain key was sent upstream but is NOT in the response."""
    app, state, _ = app_with_proxy
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat", json=_chat_body(stream=False),
                   headers={"Authorization": "Bearer ok"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "PONG"
    # The brain key reached the upstream (it's how the LLM authenticates)...
    assert state["captured_headers"]["Authorization"] == "Bearer SECRET-BRAIN-KEY-NEVER-LEAK"
    # ...but NEVER appears in the downstream response (body or headers).
    assert b"SECRET-BRAIN-KEY-NEVER-LEAK" not in r.content
    # The brain's model is authoritative — a client cannot pick a different one.
    assert state["captured_payload"]["model"] == "test-model"


def test_agent_chat_stream_proxies_sse(app_with_proxy):
    """6b. stream → 200 text/event-stream + the SSE chunks pass through verbatim."""
    app, _, _ = app_with_proxy
    with TestClient(app) as c:
        with c.stream("POST", "/v1/agent/chat", json=_chat_body(stream=True),
                      headers={"Authorization": "Bearer ok"}) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            chunks = b"".join(r.iter_bytes())
    assert b"HEL" in chunks and b"LO" in chunks   # the two delta chunks pass through verbatim
    assert b"[DONE]" in chunks


def test_agent_chat_brain_url_normalization(app_with_proxy):
    """6c. a base_url saved WITH a trailing /v1 still resolves to /v1/chat/completions."""
    app, state, _ = app_with_proxy
    state["brain"]["base_url"] = "https://brain.example.test/v1"
    with TestClient(app) as c:
        c.post("/v1/agent/chat", json=_chat_body(), headers={"Authorization": "Bearer ok"})
    # captured_payload is set by the fake client; the URL itself is built inside the handler.

    # We re-derive it to confirm the normalization rule: trailing /v1 stripped, /v1/chat/completions appended.
    from saas.agent_proxy import _normalize_chat_url
    assert _normalize_chat_url("https://brain.example.test/v1") == \
        "https://brain.example.test/v1/chat/completions"
    assert _normalize_chat_url("https://brain.example.test") == \
        "https://brain.example.test/v1/chat/completions"


# ════════════════════════════════════════════════════════════════════════════════
# chatbot-integration TRIAL — the gate honours an unexpired trial (SPEC §12)
# ════════════════════════════════════════════════════════════════════════════════

def test_agent_chat_chatbot_trial_allows_serve(app_with_proxy):
    """7. An UNEXPIRED chatbot trial flips the chat-agent entitlement ON (up to 1 seat),
    regardless of paid status — the gate returns allow and /v1/agent/chat serves 200. The
    fixture injects the gate result a trial would produce (the pure helper that computes it is
    covered in test_saas_chatbot_trial.py)."""
    app, state, _ = app_with_proxy
    state["serve"] = (True, None)  # a live trial → gate allows
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat", json=_chat_body(), headers={"Authorization": "Bearer ok"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "PONG"


def test_agent_chat_chatbot_trial_expired_refuses_fail_closed(app_with_proxy):
    """8. On trial EXPIRY the gate FAIL-CLOSES — serve is refused with ``chatbot_trial_expired``
    (the surface shows the "Add agent" upgrade CTA). No silent grace."""
    app, state, _ = app_with_proxy
    state["serve"] = (False, {"allowed": False, "status": 402,
                              "reason_code": "chatbot_trial_expired",
                              "message": "the chatbot-integration trial has ended for this account.",
                              "enable_at": "/dashboard/agent-feature"})
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat", json=_chat_body(), headers={"Authorization": "Bearer ok"})
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "chatbot_trial_expired"


# ════════════════════════════════════════════════════════════════════════════════
# tenant-delegate — invariants 7–12
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def delegate_env(monkeypatch):
    """SAAS on + a signing secret + monkeypatched auth/MFA primitives."""
    # monkeypatch, not os.environ: process-wide writes here were never restored and leaked
    # this fixture's signing secret into every module collected after it.
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "test-secret-for-delegate-only")
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)

    from saas import dashboard_auth, mfa

    acct = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    base_user = {
        "id": "dash-1",
        "account_id": acct,
        "username": "admin@example.test",
        "password_hash": "HASH",
        "totp_secret": None,
        "mfa_enabled": False,
        "role": "admin",
        "status": "active",
        "display_name": None,
    }
    state = {"user": dict(base_user), "mfa_required": False, "totp_ok": False}

    monkeypatch.setattr(dashboard_auth, "get_user_by_username",
                        lambda email: dict(state["user"]) if email == base_user["username"] else None)
    monkeypatch.setattr(dashboard_auth, "verify_password",
                        lambda pw_hash, pw: pw == "good-pw")
    monkeypatch.setattr(dashboard_auth, "touch_last_login", lambda uid: None)
    monkeypatch.setattr(mfa, "require_mfa_decision", lambda realm, pid: state["mfa_required"])
    monkeypatch.setattr(mfa, "verify_totp_any", lambda realm, pid, code: state["totp_ok"])
    return state, acct, base_user


def _delegate_app():
    from saas import agent_proxy
    app = FastAPI()
    app.include_router(agent_proxy.build_agent_proxy_router(
        require_auth=lambda *a, **k: "noop",      # delegate endpoint does not use require_auth
        resolve_user_id=lambda *a, **k: "u",
        account_for=lambda *a, **k: None,
        log=lambda *a, **k: None,
    ))
    return app


def test_tenant_delegate_404_when_saas_off(delegate_env, monkeypatch):
    """7. fail-closed off-core."""
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: False)
    app = _delegate_app()
    with TestClient(app) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "admin@example.test", "password": "good-pw"})
    assert r.status_code == 404


def test_tenant_delegate_happy_path(delegate_env):
    """8. valid login → 200 + a delegate token that verifies + carries the account scope."""
    state, acct, base_user = delegate_env
    from saas import dashboard_auth
    app = _delegate_app()
    with TestClient(app) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "admin@example.test", "password": "good-pw"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "agent_delegate"
    assert body["account_id"] == acct
    payload = dashboard_auth.verify_agent_delegate(body["delegate_token"])
    assert payload is not None
    assert payload["acct"] == acct
    assert payload["role"] == "agent_delegate"


def test_tenant_delegate_bad_password(delegate_env):
    """9. bad password → 401 (generic — no user enumeration)."""
    app = _delegate_app()
    with TestClient(app) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "admin@example.test", "password": "wrong"})
    assert r.status_code == 401


def test_tenant_delegate_unknown_user(delegate_env):
    """9b. unknown email → 401 (same shape as bad password)."""
    app = _delegate_app()
    with TestClient(app) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "nobody@example.test", "password": "x"})
    assert r.status_code == 401


def test_tenant_delegate_mfa_required_missing_and_wrong(delegate_env):
    """10. MFA required → 401 when TOTP missing, 401 when wrong, 200 when correct."""
    state, _, _ = delegate_env
    state["mfa_required"] = True
    state["totp_ok"] = False
    app = _delegate_app()
    with TestClient(app) as c:
        # missing
        r1 = c.post("/v1/auth/tenant-delegate",
                    json={"email": "admin@example.test", "password": "good-pw"})
        assert r1.status_code == 401
        # wrong
        r2 = c.post("/v1/auth/tenant-delegate",
                    json={"email": "admin@example.test", "password": "good-pw", "totp": "000000"})
        assert r2.status_code == 401
        # correct
        state["totp_ok"] = True
        r3 = c.post("/v1/auth/tenant-delegate",
                    json={"email": "admin@example.test", "password": "good-pw", "totp": "123456"})
    assert r3.status_code == 200, r3.text


def test_tenant_delegate_operator_realm_rejected(delegate_env):
    """11. operator rows (no account scope) cannot be delegated."""
    state, _, _ = delegate_env
    state["user"]["account_id"] = None
    app = _delegate_app()
    with TestClient(app) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "admin@example.test", "password": "good-pw"})
    assert r.status_code == 403


def test_delegate_token_realm_isolated_from_session(delegate_env):
    """12. a delegate token cannot satisfy the session verifier and vice-versa."""
    from saas import dashboard_auth
    state, acct, _ = delegate_env
    delegate = dashboard_auth.issue_agent_delegate(
        dashboard_user_id="dash-1", account_id=acct, username="admin@example.test")
    session = dashboard_auth.issue_session(
        dashboard_user_id="dash-1", account_id=acct, username="admin@example.test")
    assert dashboard_auth.verify_session(delegate) is None       # delegate ≠ session
    assert dashboard_auth.verify_agent_delegate(session) is None  # session ≠ delegate


def test_delegate_token_tamper_rejected(delegate_env):
    """12b. a tampered delegate token fails verification (fail-closed)."""
    from saas import dashboard_auth
    state, acct, _ = delegate_env
    delegate = dashboard_auth.issue_agent_delegate(
        dashboard_user_id="dash-1", account_id=acct, username="admin@example.test")
    assert dashboard_auth.verify_agent_delegate(delegate + "tampered") is None


# ════════════════════════════════════════════════════════════════════════════════
# /expand serve-mode bypass — invariant 13 (the SHIPPED fix, pinned at unit level)
# ════════════════════════════════════════════════════════════════════════════════

def test_learn_via_llm_gates_on_serve_mode_before_global_probe(monkeypatch):
    """13. ``_learn_via_llm`` consults ``_serve_mode_blocks_divert`` BEFORE the global
    ``INGEST_ENABLED`` probe — a frozen seat's ``/expand`` returns the read-only status and
    schedules NO learn. This is the load-bearing gate for deliverable #3."""
    from src.mcp import server

    UID = "10d7d879-63cd-4f31-92ce-f2c9edb760ab"
    log_calls: list[str] = []

    # Pretend the seat is FROZEN (serve-mode) for learn_facts.
    monkeypatch.setattr(server, "_serve_mode_blocks_divert",
                        lambda uid, tool: tool == "learn_facts")

    # Sentinel: if the global INGEST probe ran, the gate ordering is WRONG (the old bug).
    global_probe_ran = {"yes": False}

    class _BadProbeClient:
        async def get(self, *a, **k):
            global_probe_ran["yes"] = True  # must NOT be reached when serve-mode blocks
            raise AssertionError("global INGEST probe must not run when serve-mode blocks")

    monkeypatch.setattr(server, "_http_client", _BadProbeClient(), raising=False)
    monkeypatch.setattr(server, "_log", log_calls.append, raising=False)

    result = asyncio.run(server._learn_via_llm("robotics", UID, online=False))

    assert global_probe_ran["yes"] is False, "serve-mode gate must run BEFORE the global probe"
    # The frozen /expand returns a read-only/disabled status — NOT a "building concept map" ack.
    assert result.get("status") == server._INGEST_DISABLED_STATUS or \
        result.get("memory") == server._INGEST_DISABLED_MESSAGE
    assert any("expand_divert_blocked" in m for m in log_calls), \
        "the serve-mode block must be logged"


# ════════════════════════════════════════════════════════════════════════════════════
# CONSTRAINED customer-agent lane (SPEC: the pipeline is the product)
# ════════════════════════════════════════════════════════════════════════════════════

def _constrained_scope(enabled=True, **kw):
    scope = {"enabled": enabled, "allowed_topics": [], "forbidden_topics": [],
             "redirect_target": "https://optimus.example/contact",
             "escalation_triggers": []}
    scope.update(kw)
    return scope


def _mount_scope(app_with_proxy, monkeypatch, scope):
    """Point the per-seat config reads at the scope under test + a memory backend of faultline."""
    app, state, _ = app_with_proxy
    from saas import accounts as _accounts
    monkeypatch.setattr(_accounts, "get_agent_seat_config",
                        lambda aid, sid: {"memory_backend": {"provider": "faultline"}})
    monkeypatch.setattr(_accounts, "get_agent_scope_config",
                        lambda aid, sid: scope)
    return app, state


def test_constrained_consequential_turn_refused_before_brain(app_with_proxy, monkeypatch):
    """A consequential turn (medical) in the CONSTRAINED lane is refused — the brain never runs."""
    scope = _constrained_scope()
    app, state = _mount_scope(app_with_proxy, monkeypatch, scope)
    upstream_ran = {"yes": False}
    orig_post = agent_proxy.httpx.AsyncClient.post
    async def _spy_post(self, url, json=None, headers=None):
        upstream_ran["yes"] = True
        return await orig_post(self, url, json=json, headers=headers)
    monkeypatch.setattr(agent_proxy.httpx.AsyncClient, "post", _spy_post)
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat",
                   json=_chat_body(stream=False, constrained=True,
                                   messages=[{"role": "user",
                                              "content": "What dose of my medication should I take?"}]),
                   headers={"Authorization": "Bearer ok"})
    assert r.status_code == 200
    assert upstream_ran["yes"] is False, "the brain must NEVER run on a refused consequential turn"
    body = r.json()
    reply = body["choices"][0]["message"]["content"]
    assert "informational tool" in reply
    assert "medical" in reply
    assert "licensed professionals" in reply


def test_constrained_out_of_scope_declined_and_redirected(app_with_proxy, monkeypatch):
    """An out-of-scope turn (forbidden topic) is declined + redirected — no brain call."""
    scope = _constrained_scope(forbidden_topics=["pricing"])
    app, state = _mount_scope(app_with_proxy, monkeypatch, scope)
    upstream_ran = {"yes": False}
    orig_post = agent_proxy.httpx.AsyncClient.post
    async def _spy_post(self, url, json=None, headers=None):
        upstream_ran["yes"] = True
        return await orig_post(self, url, json=json, headers=headers)
    monkeypatch.setattr(agent_proxy.httpx.AsyncClient, "post", _spy_post)
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat",
                   json=_chat_body(stream=False, constrained=True,
                                   messages=[{"role": "user",
                                              "content": "Can you give me pricing for 500 units?"}]),
                   headers={"Authorization": "Bearer ok"})
    assert r.status_code == 200
    assert upstream_ran["yes"] is False
    reply = r.json()["choices"][0]["message"]["content"]
    assert "outside what" in reply.lower() or "can't help" in reply.lower() or "outside" in reply.lower()
    assert "optimus.example/contact" in reply


def test_constrained_in_scope_proceeds_grounded(app_with_proxy, monkeypatch):
    """An in-scope turn proceeds — but the injected messages carry the GROUNDING instruction
    (never parametric guessing), and the brain is still called."""
    scope = _constrained_scope()
    app, state = _mount_scope(app_with_proxy, monkeypatch, scope)
    import asyncio
    recall_result = {"memory": "Optimus Cannabis is a Maritime-local licensed producer."}
    async def _fake_recall(query, user_id):
        return recall_result
    from src.mcp import server as _mcp_server
    monkeypatch.setattr(_mcp_server, "recall_memory_tool", _fake_recall)
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat",
                   json=_chat_body(stream=False, constrained=True,
                                   messages=[{"role": "user",
                                              "content": "What do you offer?"}]),
                   headers={"Authorization": "Bearer ok"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "PONG"
    # The grounding instruction was injected into the outbound messages.
    msgs = state["captured_payload"]["messages"]
    joined = " ".join(m.get("content", "") for m in msgs)
    assert "Validated memory" in joined
    assert "informational tool" in joined.lower()


def test_constrained_abstains_when_cannot_ground(app_with_proxy, monkeypatch):
    """An in-scope turn with an EMPTY recall ABSTAINS — the brain must not improvise."""
    scope = _constrained_scope()
    app, state = _mount_scope(app_with_proxy, monkeypatch, scope)
    async def _empty_recall(query, user_id):
        return {"memory": ""}
    from src.mcp import server as _mcp_server
    monkeypatch.setattr(_mcp_server, "recall_memory_tool", _empty_recall)
    upstream_ran = {"yes": False}
    orig_post = agent_proxy.httpx.AsyncClient.post
    async def _spy_post(self, url, json=None, headers=None):
        upstream_ran["yes"] = True
        return await orig_post(self, url, json=json, headers=headers)
    monkeypatch.setattr(agent_proxy.httpx.AsyncClient, "post", _spy_post)
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat",
                   json=_chat_body(stream=False, constrained=True,
                                   messages=[{"role": "user",
                                              "content": "What is your return policy?"}]),
                   headers={"Authorization": "Bearer ok"})
    assert r.status_code == 200
    assert upstream_ran["yes"] is False, "empty recall in constrained mode = abstain, no brain"
    reply = r.json()["choices"][0]["message"]["content"]
    assert "informational tool" in reply
    assert "optimus.example/contact" in reply


def test_constrained_off_is_unconstrained(app_with_proxy, monkeypatch):
    """constrained=False (or scope disabled) behaves byte-for-byte as before — no triage."""
    scope = _constrained_scope(enabled=False)
    app, state = _mount_scope(app_with_proxy, monkeypatch, scope)
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat", json=_chat_body(stream=False, constrained=True,
                                                     messages=[{"role": "user",
                                                                "content": "What dose should I take?"}]),
                   headers={"Authorization": "Bearer ok"})
    # Scope disabled → unconstrained behaviour (brain runs, returns PONG).
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "PONG"


def test_constrained_enforced_even_without_request_flag(app_with_proxy, monkeypatch):
    """THE FIX: a tenant that enabled Scope & guardrails gets a constrained agent on EVERY
    door — the widget and appliance send NO `constrained` flag, so the scope config itself
    must be the authoritative gate. Without this, the customer-facing widget answered weather
    from parametrics (observed on prod)."""
    scope = _constrained_scope(enabled=True, forbidden_topics=["weather"])
    app, state = _mount_scope(app_with_proxy, monkeypatch, scope)
    upstream_ran = {"yes": False}
    orig_post = agent_proxy.httpx.AsyncClient.post
    async def _spy_post(self, url, json=None, headers=None):
        upstream_ran["yes"] = True
        return await orig_post(self, url, json=json, headers=headers)
    monkeypatch.setattr(agent_proxy.httpx.AsyncClient, "post", _spy_post)
    # NO constrained flag in the request — exactly what the appliance widget sends.
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat",
                   json=_chat_body(stream=False,
                                   messages=[{"role": "user",
                                              "content": "What is the weather like tomorrow?"}]),
                   headers={"Authorization": "Bearer ok"})
    assert r.status_code == 200
    assert upstream_ran["yes"] is False, "a scope-enabled seat must never reach the brain on an out-of-scope turn"
    reply = r.json()["choices"][0]["message"]["content"]
    assert "weather" not in reply.lower() or "outside" in reply.lower()
    assert "informational" in reply.lower() or "outside" in reply.lower()


def test_constrained_lane_never_trains_customer_turns(app_with_proxy, monkeypatch):
    """The CONSTRAINED lane is a READ of the tenant's corpus. Even when the seat's ingest valve
    is ON (train mode), a customer's conversation must NEVER be written to memory — the ingest
    valve governs the TENANT's training lane, not customer traffic. Nothing the tenant did not
    authorize leaves the chat lane."""
    scope = _constrained_scope(enabled=True)
    app, state = _mount_scope(app_with_proxy, monkeypatch, scope)
    writes = {"yes": False}
    from src.mcp import server as _mcp_server
    async def _spy_remember(text, user_id, **kw):
        writes["yes"] = True
        return {"status": "ok"}
    monkeypatch.setattr(_mcp_server, "remember_facts_tool", _spy_remember)
    # Ingest valve OPEN (train mode) — the tenant COULD train via the appliance, but the
    # constrained customer lane must not.
    from saas import agent_mode as _am
    monkeypatch.setattr(_am, "ingest_allowed", lambda aid, uid: True)
    import asyncio as _aio
    async def _empty_recall(query, user_id):
        return {"memory": "Optimus offers free licensed-pharmacist consultations."}
    monkeypatch.setattr(_mcp_server, "recall_memory_tool", _empty_recall)
    with TestClient(app) as c:
        r = c.post("/v1/agent/chat",
                   json=_chat_body(stream=False,
                                   messages=[{"role": "user",
                                              "content": "What services do you offer?"}]),
                   headers={"Authorization": "Bearer ok"})
    assert r.status_code == 200
    # Give the fire-and-forget write a beat to (not) fire.
    import time as _time; _time.sleep(0.3)
    assert writes["yes"] is False, "the constrained lane must NEVER write a customer turn to memory"
