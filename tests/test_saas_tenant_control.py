"""Session-authed /tenant/* CONTROL endpoints — BOLA + session-scope proofs (no DB).

These are the tenant-console control surfaces (agent-mode, seat label/toggle, onboard,
llm-config, scoped logs) added in ``saas.tenant_console``. Every one derives its account
scope from the SIGNED SESSION (``require_dashboard_session``), never from client input.

The proofs here are the crown-jewel ones:
  1. no session            → 401 (fail-closed).
  2. session scope         → the underlying control-plane call is ALWAYS made with the
                             SESSION's account_id, never a client-supplied one.
  3. cross-account (BOLA)  → session A cannot act on account B's seat (is_active_seat is
                             evaluated against the SESSION account → 403), and a scoped log
                             read is force-scoped to the session account.
  4. CSRF                  → unsafe methods require the session-bound CSRF token.

The control-plane data functions are monkeypatched, so this file is always green without a
control plane. Run: python3 -m pytest tests/test_saas_tenant_control.py -q (tests/ is
gitignored → git add -f).
"""

import pytest

from saas import dashboard_auth as auth
from saas import tenant_console as tc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-4111-8111-111111111111"   # a seat of ACCT_A
USER_B = "22222222-2222-4222-8222-222222222222"   # a seat of ACCT_B (NOT of A)


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _session_a():
    return auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")


def _csrf_a():
    return auth.issue_csrf(dashboard_user_id=USER_A)


@pytest.fixture(autouse=True)
def seat_management_on(monkeypatch):
    """Migration 226: the four add/remove-seat endpoints are gated on the OPERATOR's per-tenant
    ``accounts.seat_management_enabled``, and that gate fails CLOSED when the control plane is
    unreadable — which, in this DB-less suite, it always is. Stub it ON so these tests keep
    proving what they are about (session scope, BOLA, CSRF, cap handling). The flag's OWN
    behaviour — ON/OFF/unreadable, the audited denial, operator-only flipping — is proven in
    tests/test_saas_seat_management_flag.py."""
    monkeypatch.setattr(tc._accounts, "get_seat_management",
                        lambda account_id: {"enabled": True, "changed_at": None,
                                            "changed_by": None})


@pytest.fixture
def seat_ownership(monkeypatch):
    """USER_A is a seat of ACCT_A only; USER_B is a seat of ACCT_B only (never of A)."""
    def _is_active(account_id, user_id):
        return (account_id, user_id) in {(ACCT_A, USER_A), (ACCT_B, USER_B)}
    monkeypatch.setattr(tc._seats, "is_active_seat", _is_active)


# ════════════════════════════════════════════════════════════════════════════════
# 1. no session → 401 (fail-closed) on every control endpoint
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method,path", [
    ("get", "/tenant/seats"),
    ("post", "/tenant/seats"),
    ("get", "/tenant/agent-mode"),
    ("put", f"/tenant/seats/{USER_A}"),
    ("delete", f"/tenant/seats/{USER_A}"),
    ("post", f"/tenant/seats/{USER_A}/token"),
    ("delete", f"/tenant/seats/{USER_A}/token"),
    ("post", f"/tenant/seats/{USER_A}/onboard"),
    ("get", "/tenant/llm-config"),
    ("put", "/tenant/llm-config"),
    ("post", "/tenant/llm-config/test"),
    ("post", "/tenant/llm-config/models"),
    ("get", "/tenant/llm-config/saved"),
    ("post", "/tenant/llm-config/saved"),
    ("post", "/tenant/llm-config/saved/00000000-0000-0000-0000-000000000000/apply"),
    ("delete", "/tenant/llm-config/saved/00000000-0000-0000-0000-000000000000"),
    ("get", "/tenant/logs"),
])
def test_no_session_is_401(client, method, path):
    kw = {"json": {}} if method in ("put", "post") else {}
    resp = getattr(client, method)(path, **kw)
    assert resp.status_code == 401


def test_whole_tenant_agent_mode_put_route_removed(client):
    # The whole-tenant PUT /tenant/agent-mode toggle was removed (per-seat only). With no
    # session it is 401 only if the route exists; the route is gone → 405 (GET still exists).
    client.cookies.clear()
    assert client.put("/tenant/agent-mode", json={"enabled": True}).status_code in (404, 405)


# ════════════════════════════════════════════════════════════════════════════════
# 2. session scope — the underlying call uses the SESSION account, never client input
# ════════════════════════════════════════════════════════════════════════════════

def test_list_seats_uses_session_account(client, monkeypatch):
    seen = {}
    # ``include_upstream_identity`` (migration 210) is keyword-only and OPT-IN; this surface
    # passes True so the seat card can render the OpenWebUI-supplied name/email beside the
    # UUID. Captured here so the session-scoping assertion below still tests only scoping.
    def _list(account_id, *, include_upstream_identity=False):
        seen["account_id"] = account_id
        seen["include_upstream_identity"] = include_upstream_identity
        return [{"user_id": USER_A, "status": "active", "created_at": None}]
    monkeypatch.setattr(tc._seats, "list_seats", _list)
    monkeypatch.setattr(tc._tokens, "active_token_map", lambda a: {USER_A: 1})

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.get("/tenant/seats")
    assert r.status_code == 200
    assert seen["account_id"] == ACCT_A
    body = r.json()
    assert body["account_id"] == ACCT_A
    # Token status is folded into each seat (issued / none) for the control-surface UI.
    assert body["seats"][0]["has_token"] is True and body["seats"][0]["token_count"] == 1


# ════════════════════════════════════════════════════════════════════════════════
# POST /tenant/seats — create a per-user CONNECTION (seat + token), session-scoped,
# cap-enforced, CSRF-required. This is the non-OWUI "add user connection" control.
# ════════════════════════════════════════════════════════════════════════════════

def test_create_seat_uses_session_account_and_returns_token_once(client, monkeypatch):
    seen = {}
    def _register(account_id, user_id):
        seen["account_id"] = account_id
        seen["user_id"] = user_id
        return {"ok": True, "status_code": 200, "reason": "seat registered"}
    monkeypatch.setattr(tc._seats, "register_seat", _register)
    monkeypatch.setattr(tc._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_pfx_THESECRET", "token_id": "tok-1",
                                      "token_prefix": "flu_pfx"})
    monkeypatch.setattr(tc._onboarding, "public_mcp_url", lambda: "https://mcp.example:8002")

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    # Client tries to smuggle account B — scope MUST come from the session.
    r = client.post("/tenant/seats", headers={"x-csrf-token": _csrf_a()},
                    json={"account_id": ACCT_B})
    assert r.status_code == 200
    body = r.json()
    assert seen["account_id"] == ACCT_A            # session-scoped, not client input
    # A fresh uuid4 was generated and registered.
    assert body["user_id"] == seen["user_id"]
    # The token + a paste-ready config are returned ONCE.
    assert body["token"] == "flu_pfx_THESECRET"
    assert "X-OpenWebUI-User-Id" in body["config"] and body["user_id"] in body["config"]


def test_create_seat_accepts_explicit_uuid(client, monkeypatch):
    seen = {}
    def _register(account_id, user_id):
        seen["user_id"] = user_id
        return {"ok": True, "status_code": 200, "reason": "ok"}
    monkeypatch.setattr(tc._seats, "register_seat", _register)
    monkeypatch.setattr(tc._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_x_y", "token_id": "t", "token_prefix": "flu_x"})
    monkeypatch.setattr(tc._onboarding, "public_mcp_url", lambda: "https://mcp.example:8002")
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    explicit = "44444444-4444-4444-8444-444444444444"
    r = client.post("/tenant/seats", headers={"x-csrf-token": _csrf_a()},
                    json={"user_id": explicit})
    assert r.status_code == 200 and r.json()["user_id"] == explicit
    assert seen["user_id"] == explicit


def test_create_seat_malformed_uuid_is_400(client, monkeypatch):
    monkeypatch.setattr(tc._seats, "register_seat",
                        lambda *a, **k: pytest.fail("register reached with a bad uuid"))
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post("/tenant/seats", headers={"x-csrf-token": _csrf_a()},
                    json={"user_id": "not-a-uuid"})
    assert r.status_code == 400


def test_create_seat_cap_enforced_402(client, monkeypatch):
    # register_seat returns 402 at the licensed ceiling → the route surfaces it, no token minted.
    monkeypatch.setattr(tc._seats, "register_seat",
                        lambda a, u: {"ok": False, "status_code": 402,
                                      "reason": "seat cap reached (5/5)"})
    monkeypatch.setattr(tc._tokens, "mint_user_token",
                        lambda *a, **k: pytest.fail("token minted despite seat cap"))
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post("/tenant/seats", headers={"x-csrf-token": _csrf_a()}, json={})
    assert r.status_code == 402


def test_create_seat_requires_csrf(client, monkeypatch):
    monkeypatch.setattr(tc._seats, "register_seat",
                        lambda *a, **k: pytest.fail("register reached without CSRF"))
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post("/tenant/seats", json={})   # no x-csrf-token
    assert r.status_code == 403


# ════════════════════════════════════════════════════════════════════════════════
# per-seat token issue / revoke — session-scoped + BOLA (foreign seat 403)
# ════════════════════════════════════════════════════════════════════════════════

def test_issue_seat_token_own_seat_ok(client, seat_ownership, monkeypatch):
    monkeypatch.setattr(tc._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_a_b", "token_id": "t", "token_prefix": "flu_a"})
    monkeypatch.setattr(tc._onboarding, "public_mcp_url", lambda: "https://mcp.example:8002")
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post(f"/tenant/seats/{USER_A}/token", headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200 and r.json()["token"] == "flu_a_b"


def test_issue_seat_token_foreign_seat_is_403(client, seat_ownership, monkeypatch):
    monkeypatch.setattr(tc._tokens, "mint_user_token",
                        lambda *a, **k: pytest.fail("token minted for a foreign seat"))
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post(f"/tenant/seats/{USER_B}/token", headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 403


def test_revoke_seat_token_own_seat_ok(client, seat_ownership, monkeypatch):
    seen = {}
    monkeypatch.setattr(tc._tokens, "revoke_all_user_tokens",
                        lambda a, u: seen.update(account_id=a, user_id=u) or 2)
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.delete(f"/tenant/seats/{USER_A}/token", headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200 and r.json()["revoked"] == 2
    assert seen == {"account_id": ACCT_A, "user_id": USER_A}


def test_deprovision_seat_own_seat_ok(client, seat_ownership, monkeypatch):
    monkeypatch.setattr(tc._seats, "revoke_seat", lambda a, u: True)
    monkeypatch.setattr(tc._tokens, "revoke_all_user_tokens", lambda a, u: 1)
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.delete(f"/tenant/seats/{USER_A}", headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200 and r.json()["status"] == "deprovisioned"


def test_deprovision_foreign_seat_is_403(client, seat_ownership, monkeypatch):
    monkeypatch.setattr(tc._seats, "revoke_seat",
                        lambda *a, **k: pytest.fail("revoke reached a foreign seat"))
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.delete(f"/tenant/seats/{USER_B}", headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 403


def test_logs_are_force_scoped_to_session_account(client, monkeypatch):
    seen = {}
    def _tsq(account_id, **kw):
        seen["account_id"] = account_id
        return [{"id": 1, "level": "INFO", "message": "ok", "redacted": True}]
    monkeypatch.setattr(tc._eventlog, "tenant_safe_query", _tsq)

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.get("/tenant/logs?levels=WARN,CRIT")
    assert r.status_code == 200
    # tenant_safe_query is called with the SESSION account (positional #1), never a param.
    assert seen["account_id"] == ACCT_A
    assert r.json()["account_id"] == ACCT_A


# ════════════════════════════════════════════════════════════════════════════════
# 3. cross-account BOLA — session A cannot touch account B's seat
# ════════════════════════════════════════════════════════════════════════════════

def test_seat_update_own_seat_ok(client, seat_ownership, monkeypatch):
    monkeypatch.setattr(tc._seats, "is_agent_seat", lambda a, u: True)  # migration 139 per-seat gate
    seen = {}
    def _upd(account_id, user_id, *, ingest_enabled=None, label=None, response_type=None):
        seen["account_id"] = account_id
        seen["user_id"] = user_id
        return {"user_id": user_id, "status": "active",
                "ingest_enabled": bool(ingest_enabled), "label": label}
    monkeypatch.setattr(tc._seats, "update_seat", _upd)

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.put(f"/tenant/seats/{USER_A}",
                   headers={"x-csrf-token": _csrf_a()},
                   json={"ingest_enabled": False, "label": "networking agent"})
    assert r.status_code == 200
    assert seen["account_id"] == ACCT_A and seen["user_id"] == USER_A


def test_seat_update_foreign_seat_is_403(client, seat_ownership, monkeypatch):
    # Session A tries to toggle USER_B — a seat of account B, NOT of A. Must be 403 and the
    # update function must NEVER be reached (no cross-account write).
    called = {"hit": False}
    def _upd(*a, **k):
        called["hit"] = True
        return {"user_id": USER_B, "status": "active", "ingest_enabled": False, "label": None}
    monkeypatch.setattr(tc._seats, "update_seat", _upd)

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.put(f"/tenant/seats/{USER_B}",
                   headers={"x-csrf-token": _csrf_a()},
                   json={"ingest_enabled": False})
    assert r.status_code == 403
    assert called["hit"] is False


def test_onboard_foreign_seat_is_403(client, seat_ownership, monkeypatch):
    called = {"hit": False}
    monkeypatch.setattr(tc._tokens, "mint_user_token",
                        lambda a, u: called.__setitem__("hit", True))
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post(f"/tenant/seats/{USER_B}/onboard", headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 403
    assert called["hit"] is False


def test_logs_user_filter_foreign_seat_is_403(client, seat_ownership, monkeypatch):
    called = {"hit": False}
    monkeypatch.setattr(tc._eventlog, "tenant_safe_query",
                        lambda *a, **k: called.__setitem__("hit", True) or [])
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.get(f"/tenant/logs?user_id={USER_B}")
    assert r.status_code == 403
    assert called["hit"] is False


# ════════════════════════════════════════════════════════════════════════════════
# 4. CSRF required on unsafe methods
# ════════════════════════════════════════════════════════════════════════════════

def test_unsafe_method_requires_csrf(client, monkeypatch):
    # Use POST /tenant/seats as the representative unsafe method (the whole-tenant agent-mode
    # PUT was removed). register/mint must never be reached without a valid, bound CSRF token.
    monkeypatch.setattr(tc._seats, "register_seat",
                        lambda *a, **k: pytest.fail("register reached without valid CSRF"))
    monkeypatch.setattr(tc._tokens, "mint_user_token",
                        lambda *a, **k: pytest.fail("mint reached without valid CSRF"))
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    # No CSRF header → 403.
    assert client.post("/tenant/seats", json={}).status_code == 403
    # CSRF bound to a DIFFERENT user → 403.
    wrong = auth.issue_csrf(dashboard_user_id=USER_B)
    assert client.post("/tenant/seats", headers={"x-csrf-token": wrong},
                       json={}).status_code == 403


# ════════════════════════════════════════════════════════════════════════════════
# 5. llm-config read is redacted + session-scoped
# ════════════════════════════════════════════════════════════════════════════════

def test_llm_models_uses_session_account_and_returns_list(client, monkeypatch):
    seen = {}

    def _list(backend_type, base_url, api_key):
        seen["backend_type"] = backend_type
        seen["base_url"] = base_url
        seen["api_key"] = api_key
        return {"models": ["m-a", "m-b"]}
    monkeypatch.setattr(tc._llm_config, "list_remote_models", _list)
    # An explicit key is supplied → the stored-key fallback must NOT be consulted.
    called = {"resolve": False}
    monkeypatch.setattr(tc._llm_config, "resolve_account_llm_config",
                        lambda a: called.__setitem__("resolve", True) or None)

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post("/tenant/llm-config/models", headers={"x-csrf-token": _csrf_a()},
                    json={"backend_type": "openai", "base_url": "https://1.1.1.1",
                          "api_key": "explicit-key"})
    assert r.status_code == 200
    assert r.json()["models"] == ["m-a", "m-b"]
    assert seen["api_key"] == "explicit-key"
    assert called["resolve"] is False  # explicit key wins, no stored-key lookup


def test_llm_models_falls_back_to_stored_key_when_omitted(client, monkeypatch):
    seen = {}

    def _list(backend_type, base_url, api_key):
        seen["api_key"] = api_key
        return {"models": ["m-x"]}
    monkeypatch.setattr(tc._llm_config, "list_remote_models", _list)
    # No key in the body → resolve the STORED decrypted key for THIS session account.
    def _resolve(account_id):
        seen["resolved_account"] = account_id
        return {"api_key": "STORED-DECRYPTED"}
    monkeypatch.setattr(tc._llm_config, "resolve_account_llm_config", _resolve)

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post("/tenant/llm-config/models", headers={"x-csrf-token": _csrf_a()},
                    json={"backend_type": "openai", "base_url": "https://1.1.1.1"})
    assert r.status_code == 200
    assert seen["api_key"] == "STORED-DECRYPTED"
    assert seen["resolved_account"] == ACCT_A  # scoped to the SESSION account only


def test_llm_models_requires_csrf(client, monkeypatch):
    monkeypatch.setattr(tc._llm_config, "list_remote_models",
                        lambda *a, **k: {"models": []})
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post("/tenant/llm-config/models",
                    json={"backend_type": "openai", "base_url": "https://1.1.1.1"})
    assert r.status_code == 403  # unsafe method w/o CSRF


def test_llm_models_error_shape_is_200_graceful(client, monkeypatch):
    # A fetch failure returns the error shape (200 + {"error": ...}) — never a raise/5xx —
    # so the UI can fall back to free-text entry.
    monkeypatch.setattr(tc._llm_config, "list_remote_models",
                        lambda *a, **k: {"error": "could not list models (ConnectError)"})
    monkeypatch.setattr(tc._llm_config, "resolve_account_llm_config", lambda a: None)
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post("/tenant/llm-config/models", headers={"x-csrf-token": _csrf_a()},
                    json={"backend_type": "openai", "base_url": "https://1.1.1.1"})
    assert r.status_code == 200
    assert "error" in r.json() and "models" not in r.json()


def test_llm_models_missing_fields_is_graceful_error(client, monkeypatch):
    called = {"hit": False}
    monkeypatch.setattr(tc._llm_config, "list_remote_models",
                        lambda *a, **k: called.__setitem__("hit", True) or {"models": []})
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post("/tenant/llm-config/models", headers={"x-csrf-token": _csrf_a()},
                    json={})
    assert r.status_code == 200
    assert "error" in r.json()
    assert called["hit"] is False  # no fetch attempted without a backend + url


def test_llm_config_get_is_session_scoped(client, monkeypatch):
    seen = {}
    def _redacted(account_id):
        seen["account_id"] = account_id
        return {"account_id": account_id, "configured": True, "api_key_set": True,
                "model": "qwen3.5-9b"}
    monkeypatch.setattr(tc._llm_config, "get_config_redacted", _redacted)

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.get("/tenant/llm-config")
    assert r.status_code == 200
    body = r.json()
    assert seen["account_id"] == ACCT_A
    # Never an actual key field in the payload (redacted contract).
    assert "api_key" not in body and body.get("api_key_set") is True


# ════════════════════════════════════════════════════════════════════════════════
# 6. /tenant/keys — the tenant now OWNS connection keys (moved off the operator).
#    Session-scoped, CSRF-gated, BOLA-safe, and the full key is NEVER in the LIST.
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method,path", [
    ("get", "/tenant/keys"),
    ("post", "/tenant/keys"),
    ("delete", "/tenant/keys/key-1"),
])
def test_keys_no_session_is_401(client, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code == 401


def test_keys_list_uses_session_account_and_never_returns_full_key(client, monkeypatch):
    seen = {}
    def _overview(account_id):
        seen["account_id"] = account_id
        # get_account_overview is prefix-only by contract — NO api_key field.
        return {"account_id": account_id, "keys": [
            {"key_id": "key-1", "key_prefix": "flk_a1b2c3", "status": "active",
             "created_at": None, "last_used_at": None},
        ]}
    monkeypatch.setattr(tc._accounts, "get_account_overview", _overview)

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.get("/tenant/keys")
    assert r.status_code == 200
    body = r.json()
    # Scope is the SESSION account — never a client-supplied id.
    assert seen["account_id"] == ACCT_A and body["account_id"] == ACCT_A
    # The LIST carries prefixes ONLY — never a full/hashed key.
    assert body["keys"] and all("api_key" not in k and "key_hash" not in k
                                for k in body["keys"])
    assert body["keys"][0]["key_prefix"] == "flk_a1b2c3"


def test_keys_mint_uses_session_account_and_shows_full_key_once(client, monkeypatch):
    seen = {}
    def _mint(account_id, label=None):
        seen["account_id"] = account_id
        seen["label"] = label
        return {"key_id": "key-new", "key_prefix": "flk_new123",
                "api_key": "flk_new123_THEFULLSECRET", "label": label}
    monkeypatch.setattr(tc._accounts, "mint_key", _mint)

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post("/tenant/keys", headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200
    # mint_key was invoked with the SESSION account, never a client id.
    assert seen["account_id"] == ACCT_A
    # The full key is returned exactly here (shown ONCE), by design.
    assert r.json()["api_key"] == "flk_new123_THEFULLSECRET"


def test_keys_mint_requires_csrf(client, monkeypatch):
    monkeypatch.setattr(tc._accounts, "mint_key",
                        lambda a: {"key_id": "k", "key_prefix": "flk_x", "api_key": "flk_x_secret"})
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    # No CSRF header → 403 (unsafe method).
    r = client.post("/tenant/keys")
    assert r.status_code == 403


def test_keys_revoke_is_scoped_to_session_account(client, monkeypatch):
    seen = {}
    def _revoke(account_id, key_id):
        seen["account_id"] = account_id
        seen["key_id"] = key_id
        return True
    monkeypatch.setattr(tc._accounts, "revoke_key", _revoke)

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.delete("/tenant/keys/key-own", headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200
    # revoke_key is scoped to (SESSION account, key_id) — the WHERE binds account_id server-side.
    assert seen["account_id"] == ACCT_A and seen["key_id"] == "key-own"


def test_keys_revoke_foreign_key_is_404_bola(client, monkeypatch):
    # BOLA: session A tries to revoke a key that belongs to account B. accounts.revoke_key
    # scopes its UPDATE by (account_id=ACCT_A, key_id) → a B-owned key never matches → False
    # → the route returns 404. The key is never touched under A's authority.
    def _revoke(account_id, key_id):
        # Only ACCT_A's own key would match; a foreign key_id under ACCT_A resolves to nothing.
        return account_id == ACCT_A and key_id == "key-A-own"
    monkeypatch.setattr(tc._accounts, "revoke_key", _revoke)

    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.delete("/tenant/keys/key-B-foreign", headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 404


def test_keys_list_404_when_account_missing(client, monkeypatch):
    monkeypatch.setattr(tc._accounts, "get_account_overview", lambda a: None)
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.get("/tenant/keys")
    assert r.status_code == 404


# ════════════════════════════════════════════════════════════════════════════════
# 7. /tenant/connection-info — the PRESENTER: we HAND the tenant our MCP endpoint +
#    per-client config templates. Session-scoped; MCP endpoint from SERVER config;
#    falls back to the request origin when the public URL env is unset.
# ════════════════════════════════════════════════════════════════════════════════

def test_connection_info_no_session_is_401(client):
    assert client.get("/tenant/connection-info").status_code == 401


def test_connection_info_returns_endpoint_and_templates(client, monkeypatch):
    # Public URL configured server-side (env) → advertised as-is, no fallback note.
    monkeypatch.setenv("SAAS_MCP_PUBLIC_URL", "https://mcp.faultline.example:8002")
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.get("/tenant/connection-info")
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == ACCT_A
    assert body["public_url_configured"] is True
    assert "public_url_note" not in body
    assert body["base_url"] == "https://mcp.faultline.example:8002"
    assert body["mcp_endpoint"] == "https://mcp.faultline.example:8002/mcp"
    clients = body["clients"]
    # All three client presenters are present, each with instructions + a config snippet.
    assert set(clients) == {"openwebui", "claude_desktop", "generic"}
    for c in clients.values():
        assert c["instructions"] and isinstance(c["instructions"], list)
        assert c["config_snippet"]
        assert c["endpoint"] == body["mcp_endpoint"]
    # OpenWebUI uses the account connection key (flk_) and carries its placeholder.
    assert clients["openwebui"]["credential"] == "connection_key"
    assert clients["openwebui"]["credential_placeholder"] in clients["openwebui"]["config_snippet"]
    # Claude Desktop is one-user-per-token (flu_) and is onboarded via Seats.
    assert clients["claude_desktop"]["credential"] == "user_token"
    assert clients["claude_desktop"].get("onboard_via_seats") is True
    assert "X-OpenWebUI-User-Id" in clients["claude_desktop"]["config_snippet"]
    # Generic surfaces the raw endpoint + Bearer + the explicit user-id header.
    assert "X-OpenWebUI-User-Id" in clients["generic"]["config_snippet"]


def test_connection_info_falls_back_when_public_url_unset(client, monkeypatch):
    # Neither SAAS_MCP_PUBLIC_URL nor MCP_PUBLIC_URL set → best-effort request origin + a note.
    # Since checkout-origin-authority round 4 the request origin is honoured ONLY when the host
    # is a served face (acl.served_origin) — never the raw Host — so the test lists the
    # TestClient host the way a deployment lists its faces. An unlisted host gets the
    # placeholder instead (pinned in tests/test_served_origin.py).
    monkeypatch.delenv("SAAS_MCP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("SAAS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("SAAS_TENANT_HOSTS", "testserver")
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.get("/tenant/connection-info")
    assert r.status_code == 200
    body = r.json()
    assert body["public_url_configured"] is False
    assert "public_url_note" in body and body["public_url_note"]
    # The placeholder host is NEVER advertised — the endpoint is derived from the request.
    assert "your-faultline-host" not in body["mcp_endpoint"]
    assert body["mcp_endpoint"].endswith("/mcp")
    assert body["base_url"] and body["mcp_endpoint"].startswith(body["base_url"])
    assert set(body["clients"]) == {"openwebui", "claude_desktop", "generic"}
