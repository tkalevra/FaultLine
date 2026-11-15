"""SaaS Phase 6 — the 4-value account_type enum + the ingest-lock CAPABILITY GATE.

Migration 138: account_type ∈ {standard, advanced, expert, agent}. ONLY 'agent' can lock
ingest (the train->serve freeze). A NON-agent account is ALWAYS ingest-enabled (the
ingest_enabled flags are ignored), and the agent-mode TOGGLE endpoints reject for non-agent
with a clear 4xx. Status lock/suspend/archive/panic stay UNIVERSAL (NOT gated).

Everything is SAAS_MODE-gated and fail-closed. Run:
  python3 -m pytest tests/test_saas_account_type.py -q
"""

import contextlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import accounts as saas_accounts
from saas import admin as saas_admin
from saas import agent_mode as saas_agent_mode
from saas import seats as saas_seats
from saas import tenant_console as saas_tenant


UA = "11111111-1111-4111-8111-111111111111"
UB = "22222222-2222-4222-8222-222222222222"
ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
KEY_A = "flk_aaaaaaaa_secretA"
OP_KEY = "op_super_secret"


# ════════════════════════════════════════════════════════════════════════════════
# effective_ingest — non-agent ALWAYS on; agent honors flags; status UNIVERSAL
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("acct_type", ["standard", "advanced", "expert"])
@pytest.mark.parametrize("acct_ing,seat_ing", [
    (True, True), (True, False), (False, True), (False, False),
])
def test_non_agent_active_is_always_ingest_enabled(acct_type, acct_ing, seat_ing):
    # A non-agent ACTIVE account ignores BOTH ingest flags → always True.
    assert saas_agent_mode.effective_ingest(
        {"status": "active", "account_type": acct_type, "ingest_enabled": acct_ing},
        {"ingest_enabled": seat_ing},
    ) is True


def test_missing_account_type_reads_as_non_agent_always_on():
    # No account_type at all → non-agent default → always on when active.
    assert saas_agent_mode.effective_ingest(
        {"status": "active", "ingest_enabled": False}, {"ingest_enabled": False}) is True


@pytest.mark.parametrize("acct_ing,seat_ing,expected", [
    # Migration 139: an AGENT SEAT honors ONLY its per-seat flag; the tenant account flags are
    # irrelevant (both rows below with seat=True are True regardless of the account flag).
    (True, True, True),
    (True, False, False),
    (False, True, True),    # account flag ignored → seat ON is allowed
    (False, False, False),
])
def test_agent_seat_honors_seat_flag_only(acct_ing, seat_ing, expected):
    assert saas_agent_mode.effective_ingest(
        {"status": "active", "account_type": "standard", "ingest_enabled": acct_ing},
        {"seat_type": "agent", "ingest_enabled": seat_ing},
    ) is expected


def test_agent_seat_ignores_tenant_type_and_flags():
    # Same AGENT seat flag, opposite tenant account_type/flag → identical result (tenant inert).
    on = {"seat_type": "agent", "ingest_enabled": True}
    assert saas_agent_mode.effective_ingest(
        {"status": "active", "account_type": "standard", "ingest_enabled": True}, on
    ) is saas_agent_mode.effective_ingest(
        {"status": "active", "account_type": "agent", "ingest_enabled": False}, on
    ) is True


def test_tenant_agent_type_does_not_lock_a_non_agent_seat():
    # A tenant whose (legacy) account_type='agent' does NOT lock a STANDARD seat — tier is
    # per-SEAT now: a non-agent seat is always ingest-on when active.
    assert saas_agent_mode.effective_ingest(
        {"status": "active", "account_type": "agent"},
        {"seat_type": "standard", "ingest_enabled": False},
    ) is True


@pytest.mark.parametrize("acct_type", ["standard", "advanced", "expert", "agent"])
@pytest.mark.parametrize("status", ["suspended", "locked", "archived"])
def test_status_lock_is_universal_across_all_types(acct_type, status):
    # Non-'active' status blocks ingest for EVERY tier (status stays universal).
    assert saas_agent_mode.effective_ingest(
        {"status": status, "account_type": acct_type, "ingest_enabled": True},
        {"ingest_enabled": True},
    ) is False


# ════════════════════════════════════════════════════════════════════════════════
# enum validation + default (create_account) — ValueError before any DB touch
# ════════════════════════════════════════════════════════════════════════════════

def test_create_account_rejects_bad_type_before_db(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("db.connection reached — validation should fail first")
    monkeypatch.setattr(saas_accounts.db, "connection", boom)
    with pytest.raises(ValueError):
        saas_accounts.create_account("n", "s", 1, account_type="bogus")


def test_account_types_enum_and_default():
    assert saas_accounts.ACCOUNT_TYPES == ("standard", "advanced", "expert", "agent")
    assert saas_accounts.DEFAULT_ACCOUNT_TYPE == "standard"


# ════════════════════════════════════════════════════════════════════════════════
# create_account default seat_limit=5 — a tenant is "just a name" (operator no longer
# prompts for seats/type at create). The INSERT must carry seat_limit=5 when omitted.
# ════════════════════════════════════════════════════════════════════════════════

class _CaptureCursor:
    """Captures the accounts-INSERT params; returns fixed ids for the two RETURNINGs."""
    def __init__(self, sink):
        self.sink = sink
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO public.accounts"):
            self.sink["accounts_params"] = params
            self._result = ("acct-generated-id",)
        elif s.startswith("INSERT INTO public.account_api_keys"):
            self._result = ("key-generated-id",)
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _CaptureConn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self):
        return _CaptureCursor(self.sink)

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_create_account_defaults_seat_limit_to_5(monkeypatch):
    sink = {}
    monkeypatch.setattr(saas_accounts._keys, "generate_key",
                        lambda: ("flk_full_secret", "flk_pref", "hash_abc"))
    monkeypatch.setattr(saas_accounts.db, "connection", lambda: _CaptureConn(sink))
    monkeypatch.setattr(saas_accounts, "invalidate_key_cache", lambda: None)

    # seat_limit omitted → the "just a name" path.
    result = saas_accounts.create_account("Acme Research", "acme-research")

    # INSERT params order: (name, slug, plan, account_type, account_kind, seat_limit)
    name, slug, plan, account_type, account_kind, seat_limit = sink["accounts_params"]
    assert name == "Acme Research" and slug == "acme-research"
    assert account_type == "standard"          # defaulted tier
    assert account_kind == "tenant"            # defaulted kind (Enterprise)
    assert int(seat_limit) == 5                 # defaulted seat cap
    assert result["api_key"] == "flk_full_secret"  # shown ONCE


def test_create_account_request_seat_limit_defaults_to_5():
    # The route model no longer REQUIRES seat_limit — it defaults to 5.
    req = saas_admin.CreateAccountRequest(name="Acme", slug="acme")
    assert req.seat_limit == 5
    assert req.account_type == "standard"


# ════════════════════════════════════════════════════════════════════════════════
# set_account_type + account_type() + is_agent_tier() — REAL SQL vs a fake control plane
# ════════════════════════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._result = None
        if s.startswith("UPDATE public.accounts SET account_type = %s"):
            new_type, acct = p
            row = self.store.get(acct)
            if row:
                row["account_type"] = new_type
                self._result = (new_type,)
        elif s.startswith("SELECT account_type FROM public.accounts"):
            row = self.store.get(p[0])
            self._result = (row["account_type"],) if row else None

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
def acct_store(monkeypatch):
    store = {ACCT_A: {"account_type": "standard"}}

    @contextlib.contextmanager
    def _conn():
        yield _FakeConn(store)
    monkeypatch.setattr(saas_accounts.db, "connection", _conn)
    monkeypatch.setattr(saas_accounts, "invalidate_account_cache", lambda a: None)
    return store


def test_set_account_type_valid(acct_store):
    out = saas_accounts.set_account_type(ACCT_A, "agent")
    assert out["ok"] is True and out["account_type"] == "agent"
    assert acct_store[ACCT_A]["account_type"] == "agent"


def test_set_account_type_invalid_400(acct_store):
    out = saas_accounts.set_account_type(ACCT_A, "bogus")
    assert out["ok"] is False and out["status_code"] == 400
    assert acct_store[ACCT_A]["account_type"] == "standard"     # untouched


def test_set_account_type_not_found_404(acct_store):
    out = saas_accounts.set_account_type("ffffffff-ffff-4fff-8fff-ffffffffffff", "agent")
    assert out["ok"] is False and out["status_code"] == 404


def test_account_type_and_is_agent_tier(acct_store):
    assert saas_accounts.account_type(ACCT_A) == "standard"
    assert saas_accounts.is_agent_tier(ACCT_A) is False
    acct_store[ACCT_A]["account_type"] = "agent"
    assert saas_accounts.is_agent_tier(ACCT_A) is True


# ════════════════════════════════════════════════════════════════════════════════
# ROUTE tests — the capability gate on the admin (M2M) surface
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account",
                        lambda k: {KEY_A: ACCT_A}.get(k))
    monkeypatch.setattr(saas_seats, "is_active_seat",
                        lambda a, u: (a, u) == (ACCT_A, UA))
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _set_tier(monkeypatch, is_agent):
    # Migration 139: the ingest-lock capability gate is PER-SEAT — it keys off the SEAT's own
    # type (saas_seats.is_agent_seat), NOT the tenant's account_type.
    monkeypatch.setattr(saas_seats, "is_agent_seat", lambda a, u: is_agent)


# ── PUT /admin/agent-mode (whole tenant) — REMOVED (per-seat only now) ──

def test_admin_agent_mode_route_removed(admin_client):
    # The whole-tenant agent-mode toggle was deleted — train->serve is per-SEAT only. The
    # route no longer exists on the admin router.
    r = admin_client.put("/admin/agent-mode", json={"enabled": False}, headers=_auth(KEY_A))
    assert r.status_code in (404, 405)


# ── PUT /admin/users/{id} — ingest_enabled gated; label allowed for all ──

def test_admin_seat_ingest_toggle_rejected_for_non_agent(admin_client, monkeypatch):
    _set_tier(monkeypatch, False)
    monkeypatch.setattr(saas_seats, "update_seat",
                        lambda *a, **k: pytest.fail("update_seat reached for non-agent ingest toggle"))
    r = admin_client.put(f"/admin/users/{UA}", json={"ingest_enabled": False},
                         headers=_auth(KEY_A))
    assert r.status_code == 403 and "Agent-tier" in r.json()["detail"]


def test_admin_seat_ingest_toggle_ok_for_agent(admin_client, monkeypatch):
    _set_tier(monkeypatch, True)
    monkeypatch.setattr(saas_seats, "update_seat",
                        lambda account_id, user_id, *, ingest_enabled, label: {
                            "user_id": user_id, "status": "active",
                            "ingest_enabled": bool(ingest_enabled), "label": label})
    r = admin_client.put(f"/admin/users/{UA}", json={"ingest_enabled": False},
                         headers=_auth(KEY_A))
    assert r.status_code == 200 and r.json()["ingest_enabled"] is False


def test_admin_seat_label_allowed_for_non_agent(admin_client, monkeypatch):
    # Setting a LABEL only (no ingest_enabled) is allowed for every tier.
    _set_tier(monkeypatch, False)
    captured = {}

    def fake_update(account_id, user_id, *, ingest_enabled, label):
        captured["args"] = (account_id, user_id, ingest_enabled, label)
        return {"user_id": user_id, "status": "active", "ingest_enabled": True, "label": label}
    monkeypatch.setattr(saas_seats, "update_seat", fake_update)
    r = admin_client.put(f"/admin/users/{UA}", json={"label": "net-agent"}, headers=_auth(KEY_A))
    assert r.status_code == 200 and r.json()["label"] == "net-agent"
    assert captured["args"] == (ACCT_A, UA, None, "net-agent")


# ── POST /admin/accounts/{id}/type (operator-only) ──

def test_set_type_endpoint_operator_only(admin_client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "set_account_type",
                        lambda a, t: {"ok": True, "status_code": 200,
                                      "account_type": t, "reason": "ok"})
    # account key → 403 (require_operator)
    assert admin_client.post(f"/admin/accounts/{ACCT_A}/type",
                             json={"account_type": "agent"},
                             headers=_auth(KEY_A)).status_code == 403
    # operator key → 200
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/type",
                          json={"account_type": "agent"}, headers=_auth(OP_KEY))
    assert r.status_code == 200 and r.json()["account_type"] == "agent"


def test_set_type_endpoint_rejects_bad_enum(admin_client):
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/type",
                          json={"account_type": "on_fire"}, headers=_auth(OP_KEY))
    assert r.status_code == 400


# ── POST /admin/accounts create — account_type validation + default + passthrough ──

def test_create_account_endpoint_default_and_passthrough(admin_client, monkeypatch):
    captured = {}

    def fake_create(name, slug, seat_limit, plan, *, account_type, account_kind="tenant"):
        captured["account_type"] = account_type
        captured["account_kind"] = account_kind
        return {"account_id": ACCT_A, "account_type": account_type,
                "account_kind": account_kind,
                "key_id": "k", "key_prefix": "flk_x", "api_key": "flk_x_secret"}
    monkeypatch.setattr(saas_accounts, "create_account", fake_create)
    # default → 'standard'
    r = admin_client.post("/admin/accounts",
                          json={"name": "n", "slug": "s1", "seat_limit": 1},
                          headers=_auth(OP_KEY))
    assert r.status_code == 200 and captured["account_type"] == "standard"
    # explicit agent passes through
    r2 = admin_client.post("/admin/accounts",
                           json={"name": "n", "slug": "s2", "seat_limit": 1,
                                 "account_type": "agent"}, headers=_auth(OP_KEY))
    assert r2.status_code == 200 and captured["account_type"] == "agent"


def test_create_account_endpoint_rejects_bad_type(admin_client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "create_account",
                        lambda *a, **k: pytest.fail("create reached with a bad type"))
    r = admin_client.post("/admin/accounts",
                          json={"name": "n", "slug": "s", "seat_limit": 1,
                                "account_type": "bogus"}, headers=_auth(OP_KEY))
    assert r.status_code == 400


# ════════════════════════════════════════════════════════════════════════════════
# ROUTE tests — the capability gate on the TENANT (session) surface
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tenant_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_seats, "is_active_seat",
                        lambda a, u: (a, u) == (ACCT_A, UA))
    app = FastAPI()
    app.include_router(saas_tenant.router)
    # bypass the session cookie/CSRF — the gate under test is the tier check, not auth.
    app.dependency_overrides[saas_tenant.require_dashboard_session] = lambda: ACCT_A
    return TestClient(app)


def test_tenant_agent_mode_route_removed(tenant_client):
    # The whole-tenant PUT /tenant/agent-mode toggle was removed (per-seat only). GET stays
    # as the snapshot source; the PUT is gone.
    r = tenant_client.put("/tenant/agent-mode", json={"enabled": False})
    assert r.status_code in (404, 405)


def test_tenant_seat_ingest_toggle_rejected_for_non_agent(tenant_client, monkeypatch):
    _set_tier(monkeypatch, False)
    monkeypatch.setattr(saas_seats, "update_seat",
                        lambda *a, **k: pytest.fail("update_seat reached for non-agent ingest toggle"))
    r = tenant_client.put(f"/tenant/seats/{UA}", json={"ingest_enabled": False})
    assert r.status_code == 403 and "Agent-tier" in r.json()["detail"]


def test_tenant_seat_label_allowed_for_non_agent(tenant_client, monkeypatch):
    _set_tier(monkeypatch, False)
    monkeypatch.setattr(saas_seats, "update_seat",
                        lambda account_id, user_id, *, ingest_enabled, label, response_type=None: {
                            "user_id": user_id, "status": "active",
                            "ingest_enabled": True, "label": label})
    r = tenant_client.put(f"/tenant/seats/{UA}", json={"label": "contracts"})
    assert r.status_code == 200 and r.json()["label"] == "contracts"


# ════════════════════════════════════════════════════════════════════════════════
# Dashboard-login MANAGEMENT endpoints (list + reset-password) — operator-authed
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def dash_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account",
                        lambda k: {KEY_A: ACCT_A}.get(k))
    app = FastAPI()
    app.include_router(saas_tenant.router)
    return TestClient(app)


def test_list_dashboard_users(dash_client, monkeypatch):
    monkeypatch.setattr(saas_tenant._auth, "list_dashboard_users",
                        lambda a: [{"id": "du-1", "username": "acme-admin",
                                    "mfa_enabled": True, "status": "active", "role": "admin",
                                    "created_at": None, "last_login_at": None}])
    r = dash_client.get(f"/admin/dashboard-users?account_id={ACCT_A}", headers=_auth(OP_KEY))
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == ACCT_A and len(body["dashboard_users"]) == 1
    # never leaks a hash
    assert "password_hash" not in body["dashboard_users"][0]


def test_list_dashboard_users_cross_account_denied(dash_client, monkeypatch):
    # account key A asking for account B's logins → 403 (scope check)
    monkeypatch.setattr(saas_tenant._auth, "list_dashboard_users",
                        lambda a: pytest.fail("listed cross-account"))
    r = dash_client.get(f"/admin/dashboard-users?account_id={ACCT_B}", headers=_auth(KEY_A))
    assert r.status_code == 403


def test_reset_password_returns_temp_once(dash_client, monkeypatch):
    monkeypatch.setattr(saas_tenant._auth, "get_user_by_id",
                        lambda uid: {"id": uid, "account_id": ACCT_A, "username": "acme-admin"})
    monkeypatch.setattr(saas_tenant._auth, "generate_temp_password", lambda: "Tmp-SECRET-123")
    monkeypatch.setattr(saas_tenant._auth, "hash_password", lambda p: "hash:" + p)
    seen = {}
    monkeypatch.setattr(saas_tenant._auth, "set_password",
                        lambda uid, h: seen.setdefault("hash", h) or True)
    r = dash_client.post("/admin/dashboard-users/du-1/reset-password", headers=_auth(OP_KEY))
    assert r.status_code == 200 and r.json()["temp_password"] == "Tmp-SECRET-123"
    assert seen["hash"] == "hash:Tmp-SECRET-123"     # stored hashed, not plaintext


def test_reset_password_not_found_404(dash_client, monkeypatch):
    monkeypatch.setattr(saas_tenant._auth, "get_user_by_id", lambda uid: None)
    r = dash_client.post("/admin/dashboard-users/nope/reset-password", headers=_auth(OP_KEY))
    assert r.status_code == 404
