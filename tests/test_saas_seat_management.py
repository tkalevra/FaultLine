"""Live account-capacity + seat management — set the seat LIMIT, ASSIGN a seat, EDIT a
seat's label; all AFTER provisioning.

Three new operator surfaces on ``saas.admin`` + a label on the tenant create path:

  * POST /admin/accounts/{id}/seat-limit          — operator-only licensed-capacity lever
  * POST /admin/accounts/{id}/users               — operator ASSIGN a seat (+label +flu_ token)
  * PUT  /admin/accounts/{id}/users/{uid}         — operator EDIT a seat's label
  * POST /tenant/seats {label?}                    — tenant create accepts a label

Auth contract:
  - seat-limit → ``require_operator`` (operator key/session ONLY; an account key → 403), like
    the sibling /type + /status business levers. Independent of account_type.
  - assign + edit-label → ``require_account`` + ``_authorize_account_path`` (operator ANY
    account; an account key ONLY its OWN — a mismatching path account_id → 403 BOLA), matching
    the sibling GET/DELETE on /admin/accounts/{id}/users.
  - tenant create → session-scoped (account from the signed session) + CSRF.

Cap-enforced (assign → 402 at the licensed ceiling), fail-closed, secrets never logged. The
control-plane data functions are monkeypatched so this file is always green without a DB.

Run: python3 -m pytest tests/test_saas_seat_management.py -q  (tests/ gitignored → git add -f)
"""

import contextlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import accounts as saas_accounts
from saas import admin as saas_admin
from saas import dashboard_auth as auth
from saas import seats as saas_seats
from saas import tenant_console as tc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UA = "11111111-1111-4111-8111-111111111111"
UB = "22222222-2222-4222-8222-222222222222"
KEY_A = "flk_aaaaaaaa_secretA"
OP_KEY = "op-super-secret-key"


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


def _acct_a_key():
    return {"Authorization": f"Bearer {KEY_A}"}


# ════════════════════════════════════════════════════════════════════════════════
# accounts.set_seat_limit — REAL SQL against a fake control plane
# ════════════════════════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._result = None
        if s.startswith("UPDATE public.accounts SET seat_limit = %s"):
            n, acct = p
            row = self.store.get(acct)
            if row is not None:
                row["seat_limit"] = n
                self._result = (n,)
        elif s.startswith("SELECT COUNT(*) FROM public.account_users"):
            row = self.store.get(p[0])
            self._result = (row["active"] if row else 0,)

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def seatlimit_store(monkeypatch):
    store = {ACCT_A: {"seat_limit": 5, "active": 3}}
    busted = {"count": 0, "last": None}

    @contextlib.contextmanager
    def _conn():
        yield _FakeConn(store)
    monkeypatch.setattr(saas_accounts.db, "connection", _conn)
    monkeypatch.setattr(saas_accounts, "invalidate_account_cache",
                        lambda a: busted.update(count=busted["count"] + 1, last=a))
    store["_busted"] = busted
    return store


def test_set_seat_limit_valid_and_cache_busted(seatlimit_store):
    out = saas_accounts.set_seat_limit(ACCT_A, 25)
    assert out["ok"] is True and out["status_code"] == 200
    assert out["seat_limit"] == 25 and out["active_seats"] == 3
    assert out["warning"] is None
    assert seatlimit_store[ACCT_A]["seat_limit"] == 25
    # the seat/account cache MUST be busted so register re-reads the new cap immediately
    assert seatlimit_store["_busted"]["count"] == 1
    assert seatlimit_store["_busted"]["last"] == ACCT_A


def test_set_seat_limit_below_active_warns_but_allows(seatlimit_store):
    # 3 active seats, new limit 2 → allowed, existing seats keep working, warning returned.
    out = saas_accounts.set_seat_limit(ACCT_A, 2)
    assert out["ok"] is True and out["seat_limit"] == 2 and out["active_seats"] == 3
    assert out["warning"] and "below the current active seats" in out["warning"]
    assert seatlimit_store[ACCT_A]["seat_limit"] == 2   # still written


def test_set_seat_limit_zero_ok(seatlimit_store):
    out = saas_accounts.set_seat_limit(ACCT_A, 0)
    assert out["ok"] is True and out["seat_limit"] == 0
    assert out["warning"]           # 3 active > 0 → warning


def test_set_seat_limit_negative_400_no_db(monkeypatch):
    def boom():
        raise AssertionError("db.connection reached — negative should fail before any DB")
    monkeypatch.setattr(saas_accounts.db, "connection", boom)
    out = saas_accounts.set_seat_limit(ACCT_A, -1)
    assert out["ok"] is False and out["status_code"] == 400


def test_set_seat_limit_not_found_404(seatlimit_store):
    out = saas_accounts.set_seat_limit("ffffffff-ffff-4fff-8fff-ffffffffffff", 10)
    assert out["ok"] is False and out["status_code"] == 404


# ════════════════════════════════════════════════════════════════════════════════
# ROUTE fixtures
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "seat-mgmt-secret-0123456789abcdef")
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account",
                        lambda k: {KEY_A: ACCT_A}.get(k))
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


# ── POST /admin/accounts/{id}/seat-limit — operator-only ──────────────────────────

def test_seat_limit_route_operator_ok(admin_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(saas_admin._accounts, "set_seat_limit",
                        lambda a, n: seen.update(args=(a, n)) or {
                            "ok": True, "status_code": 200, "seat_limit": n,
                            "active_seats": 2, "warning": None, "reason": "ok"})
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/seat-limit",
                          json={"seat_limit": 50}, headers=_op())
    assert r.status_code == 200
    body = r.json()
    assert seen["args"] == (ACCT_A, 50)
    assert body["seat_limit"] == 50 and body["active_seats"] == 2
    assert "warning" not in body


def test_seat_limit_route_surfaces_warning(admin_client, monkeypatch):
    monkeypatch.setattr(saas_admin._accounts, "set_seat_limit",
                        lambda a, n: {"ok": True, "status_code": 200, "seat_limit": n,
                                      "active_seats": 6, "warning": "below the current active seats",
                                      "reason": "ok"})
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/seat-limit",
                          json={"seat_limit": 3}, headers=_op())
    assert r.status_code == 200 and r.json()["warning"]


def test_seat_limit_route_account_key_is_403(admin_client, monkeypatch):
    monkeypatch.setattr(saas_admin._accounts, "set_seat_limit",
                        lambda *a, **k: pytest.fail("set_seat_limit reached without operator cred"))
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/seat-limit",
                          json={"seat_limit": 10}, headers=_acct_a_key())
    assert r.status_code == 403


def test_seat_limit_route_negative_is_422(admin_client):
    # Pydantic ge=0 rejects a negative body before the handler runs.
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/seat-limit",
                          json={"seat_limit": -5}, headers=_op())
    assert r.status_code == 422


def test_seat_limit_route_saas_off_404(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    app = FastAPI()
    app.include_router(saas_admin.router)
    c = TestClient(app)
    r = c.post(f"/admin/accounts/{ACCT_A}/seat-limit", json={"seat_limit": 10}, headers=_op())
    assert r.status_code == 404


# ── POST /admin/accounts/{id}/users — operator ASSIGN a seat ──────────────────────

@pytest.fixture
def assign_mocks(monkeypatch):
    calls = {}
    monkeypatch.setattr(saas_admin._seats, "register_seat",
                        lambda a, u: calls.update(register=(a, u)) or {
                            "ok": True, "status_code": 200, "reason": "seat registered"})
    monkeypatch.setattr(saas_admin._seats, "update_seat",
                        lambda a, u, **k: calls.update(update=(a, u, k)) or {
                            "user_id": u, "status": "active",
                            "ingest_enabled": True, "label": k.get("label")})
    monkeypatch.setattr(saas_admin._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_pfx_THESECRET", "token_id": "tok-1",
                                      "token_prefix": "flu_pfx"})
    monkeypatch.setattr(saas_admin._onboarding, "public_mcp_url",
                        lambda: "https://mcp.example:8002")
    calls["_reset"] = True
    return calls


def test_assign_seat_operator_creates_seat_label_and_token(admin_client, assign_mocks):
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/users",
                          json={"label": "net-agent"}, headers=_op())
    assert r.status_code == 200
    body = r.json()
    # a uuid4 was generated + registered under the target account
    acct, uid = assign_mocks["register"]
    assert acct == ACCT_A and body["user_id"] == uid
    # the label was set on the new seat
    assert assign_mocks["update"][2].get("label") == "net-agent"
    assert body["label"] == "net-agent"
    # the flu_ token + a paste-ready config are returned ONCE
    assert body["token"] == "flu_pfx_THESECRET"
    assert "X-OpenWebUI-User-Id" in body["config"] and body["user_id"] in body["config"]


def test_assign_seat_accepts_explicit_uuid(admin_client, assign_mocks):
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/users",
                          json={"user_id": UA}, headers=_op())
    assert r.status_code == 200 and r.json()["user_id"] == UA
    assert assign_mocks["register"] == (ACCT_A, UA)


def test_assign_seat_malformed_uuid_is_400(admin_client, monkeypatch):
    monkeypatch.setattr(saas_admin._seats, "register_seat",
                        lambda *a, **k: pytest.fail("register reached with a bad uuid"))
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/users",
                          json={"user_id": "not-a-uuid"}, headers=_op())
    assert r.status_code == 400


def test_assign_seat_at_cap_is_402_clear_message(admin_client, monkeypatch):
    monkeypatch.setattr(saas_admin._seats, "register_seat",
                        lambda a, u: {"ok": False, "status_code": 402,
                                      "reason": "seat cap reached (5/5)"})
    monkeypatch.setattr(saas_admin._tokens, "mint_user_token",
                        lambda *a, **k: pytest.fail("token minted despite seat cap"))
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/users", json={}, headers=_op())
    assert r.status_code == 402
    assert "raise the seat limit" in r.json()["detail"]


def test_assign_seat_cross_account_is_403_and_never_registers(admin_client, monkeypatch):
    called = {"hit": False}
    monkeypatch.setattr(saas_admin._seats, "register_seat",
                        lambda a, u: called.__setitem__("hit", True) or {"ok": True, "status_code": 200})
    # ACCT_A's key names ACCT_B in the path → 403 before any control-plane write.
    r = admin_client.post(f"/admin/accounts/{ACCT_B}/users", json={}, headers=_acct_a_key())
    assert r.status_code == 403
    assert called["hit"] is False


def test_assign_seat_own_account_key_ok(admin_client, assign_mocks):
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/users",
                          json={"label": "own"}, headers=_acct_a_key())
    assert r.status_code == 200 and r.json()["label"] == "own"


# ── PUT /admin/accounts/{id}/users/{uid} — operator EDIT label ────────────────────

def test_edit_label_operator_ok(admin_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(saas_admin._seats, "update_seat",
                        lambda a, u, **k: seen.update(args=(a, u, k)) or {
                            "user_id": u, "status": "active",
                            "ingest_enabled": True, "label": k.get("label")})
    r = admin_client.put(f"/admin/accounts/{ACCT_A}/users/{UA}",
                         json={"label": "renamed"}, headers=_op())
    assert r.status_code == 200 and r.json()["label"] == "renamed"
    # the seat-edit endpoint forwards BOTH label and response_type to update_seat
    # (response_type=None on a label-only edit → update_seat leaves the voice unchanged)
    assert seen["args"] == (ACCT_A, UA, {"label": "renamed", "response_type": None})


def test_edit_label_not_a_seat_is_404(admin_client, monkeypatch):
    monkeypatch.setattr(saas_admin._seats, "update_seat", lambda a, u, **k: None)
    r = admin_client.put(f"/admin/accounts/{ACCT_A}/users/{UB}",
                         json={"label": "x"}, headers=_op())
    assert r.status_code == 404


def test_edit_label_malformed_uuid_is_400(admin_client, monkeypatch):
    monkeypatch.setattr(saas_admin._seats, "update_seat",
                        lambda *a, **k: pytest.fail("update reached with a bad uuid"))
    r = admin_client.put(f"/admin/accounts/{ACCT_A}/users/not-a-uuid",
                         json={"label": "x"}, headers=_op())
    assert r.status_code == 400


def test_edit_label_cross_account_is_403_and_never_writes(admin_client, monkeypatch):
    called = {"hit": False}
    monkeypatch.setattr(saas_admin._seats, "update_seat",
                        lambda a, u, **k: called.__setitem__("hit", True) or {"label": "x"})
    r = admin_client.put(f"/admin/accounts/{ACCT_B}/users/{UB}",
                         json={"label": "x"}, headers=_acct_a_key())
    assert r.status_code == 403
    assert called["hit"] is False


# ════════════════════════════════════════════════════════════════════════════════
# tenant create accepts a label — POST /tenant/seats {label}
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tenant_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "seat-mgmt-tenant-secret-0123456789")
    # Migration 226: POST /tenant/seats is now also gated on the OPERATOR's per-tenant
    # ``accounts.seat_management_enabled``, which fails CLOSED on an unreadable control plane —
    # i.e. always, in this DB-less suite. Stub it ON so these tests keep proving the LABEL
    # contract they are about. The flag itself is proven in test_saas_seat_management_flag.py.
    monkeypatch.setattr(tc._accounts, "get_seat_management",
                        lambda account_id: {"enabled": True, "changed_at": None,
                                            "changed_by": None})
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _session_a():
    return auth.issue_session(dashboard_user_id=UA, account_id=ACCT_A, username="alice")


def _csrf_a():
    return auth.issue_csrf(dashboard_user_id=UA)


def test_tenant_create_seat_accepts_label(tenant_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(tc._seats, "register_seat",
                        lambda a, u: {"ok": True, "status_code": 200, "reason": "ok"})
    monkeypatch.setattr(tc._seats, "update_seat",
                        lambda a, u, **k: seen.update(label=k.get("label"), account=a) or {
                            "user_id": u, "status": "active", "ingest_enabled": True,
                            "label": k.get("label")})
    monkeypatch.setattr(tc._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_x_y", "token_id": "t", "token_prefix": "flu_x"})
    monkeypatch.setattr(tc._onboarding, "public_mcp_url", lambda: "https://mcp.example:8002")

    tenant_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = tenant_client.post("/tenant/seats", headers={"x-csrf-token": _csrf_a()},
                           json={"label": "claude-desktop"})
    assert r.status_code == 200
    body = r.json()
    # label set on the SESSION account's new seat, and echoed back in the payload
    assert seen["label"] == "claude-desktop" and seen["account"] == ACCT_A
    assert body["label"] == "claude-desktop"


def test_tenant_create_seat_no_label_skips_update(tenant_client, monkeypatch):
    monkeypatch.setattr(tc._seats, "register_seat",
                        lambda a, u: {"ok": True, "status_code": 200, "reason": "ok"})
    monkeypatch.setattr(tc._seats, "update_seat",
                        lambda *a, **k: pytest.fail("update_seat called with no label"))
    monkeypatch.setattr(tc._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_x_y", "token_id": "t", "token_prefix": "flu_x"})
    monkeypatch.setattr(tc._onboarding, "public_mcp_url", lambda: "https://mcp.example:8002")
    tenant_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = tenant_client.post("/tenant/seats", headers={"x-csrf-token": _csrf_a()}, json={})
    assert r.status_code == 200 and "label" not in r.json()


# ════════════════════════════════════════════════════════════════════════════════
# routes registered
# ════════════════════════════════════════════════════════════════════════════════

def test_new_routes_present():
    admin_paths = {(r.path, tuple(sorted(getattr(r, "methods", []) or [])))
                   for r in saas_admin.router.routes}
    paths = {p for p, _ in admin_paths}
    assert "/admin/accounts/{account_id}/seat-limit" in paths
    # POST assign + PUT edit-label share the /users + /users/{id} paths with GET/DELETE
    methods_users = {m for p, ms in admin_paths if p == "/admin/accounts/{account_id}/users"
                     for m in ms}
    assert {"GET", "POST"} <= methods_users
    methods_user = {m for p, ms in admin_paths
                    if p == "/admin/accounts/{account_id}/users/{user_id}" for m in ms}
    assert {"DELETE", "PUT"} <= methods_user
