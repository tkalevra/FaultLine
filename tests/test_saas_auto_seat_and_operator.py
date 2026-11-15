"""Migration-137 SaaS build — auto-seat provisioning + operator auth realm + tenant panic.

Three surfaces, tests-first on the security-critical ones:

  PART A — AUTO-SEAT PROVISIONING (modifies the crown-jewel seat check in bind_tenant):
    * bind_tenant SEAM: unknown UUID under cap → provisioned + allowed; at cap → HARD
      "seat limit reached" 403 (distinct message); cross-account conflict / auto-off → today's
      403. (auto_provision_seat monkeypatched → proves the WIRING at the chokepoint.)
    * auto_provision_seat FAKE-DB: provisioned / at_cap / conflict / disabled / already-seat,
      + a best-effort atomic-cap check (two sequential first-touches cannot exceed the cap).
    * LIVE-DB (auto-skips without POSTGRES_DSN): real end-to-end cap + cross-account conflict.

  PART B — OPERATOR AUTH REALM (a human super-admin login, salt-separated from tenants):
    * operator session satisfies require_operator_session; a TENANT session does NOT (401/None).
    * operator endpoints accept operator-KEY OR operator-SESSION (require_operator / _account).
    * a tenant/account key CANNOT mint a role='operator' user (403); bootstrap works from the KEY.

  PART C — TENANT PANIC:
    * session panic → locked + tenant_panic scoped to the SESSION account; the SAME
      session/account credential canNOT clear it (403); the OPERATOR can; slug mismatch → 400.

Run: python3 -m pytest tests/test_saas_auto_seat_and_operator.py -q   (tests/ gitignored → git add -f)
"""

import contextlib
import os
import uuid

import pytest

# Control-plane singletons (the append-only ACTIVE contract chain, the platform settings row,
# the global operator realm) are shared by every suite in one database: the suites that
# write or depend on them run on one xdist worker (under --dist loadgroup, see tests/conftest.py).
pytestmark = pytest.mark.xdist_group("control_plane")

from src.mcp import server as mcp_server

from saas import accounts as saas_accounts
from saas import admin as saas_admin
from saas import dashboard_auth as auth
from saas import operator_console as opc
from saas import seats as saas_seats
from saas import tenant_console as tc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UA = "11111111-1111-4111-8111-111111111111"
UB = "22222222-2222-4222-8222-222222222222"
UNEW = "44444444-4444-4444-8444-444444444444"
OP_KEY = "op-super-secret-key"


class _FakeRequest:
    def __init__(self, method="GET", cookies=None, headers=None):
        self.method = method
        self.cookies = cookies or {}
        self.headers = headers or {}


# ════════════════════════════════════════════════════════════════════════════════
# PART A — bind_tenant auto-seat WIRING (the crown-jewel chokepoint)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def not_a_seat(monkeypatch):
    """The claimed UUID is NOT an active seat → bind_tenant reaches the auto-provision branch."""
    monkeypatch.setattr(saas_seats, "is_active_seat", lambda a, u: False)


def test_bind_tenant_auto_provision_under_cap_allows(not_a_seat, monkeypatch):
    seen = {}

    def fake_ap(account_id, user_id):
        seen["args"] = (account_id, user_id)
        return {"result": "provisioned", "seat_limit": 5, "active_seats": 3}
    monkeypatch.setattr(saas_seats, "auto_provision_seat", fake_ap)

    resolved = mcp_server.bind_tenant(f"account:{ACCT_A}", UNEW)
    assert resolved == UNEW                      # provisioned → request proceeds
    assert seen["args"] == (ACCT_A, UNEW)        # reused the cap-enforcing path


def test_bind_tenant_at_cap_hard_rejects_with_distinct_message(not_a_seat, monkeypatch):
    monkeypatch.setattr(saas_seats, "auto_provision_seat",
                        lambda a, u: {"result": "at_cap", "seat_limit": 5, "active_seats": 5})
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", UNEW)
    assert exc.value.status_code == 403
    # HARD ceiling: a clear, distinct message (N/N licensed) — not the generic not-a-seat text.
    assert "seat limit reached (5/5 licensed)" in exc.value.message
    assert "administrator" in exc.value.message


def test_bind_tenant_cross_account_conflict_denied_todays_403(not_a_seat, monkeypatch):
    """A UUID owned by ANOTHER account → conflict → today's 403 (never reassigned)."""
    monkeypatch.setattr(saas_seats, "auto_provision_seat",
                        lambda a, u: {"result": "conflict", "seat_limit": 5, "active_seats": 2})
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", UB)
    assert exc.value.status_code == 403
    assert "not a licensed seat" in exc.value.message


def test_bind_tenant_auto_provision_disabled_denied_todays_403(not_a_seat, monkeypatch):
    """auto_provision_seats=false → unknown UUID rejected exactly as today (manual/token only)."""
    monkeypatch.setattr(saas_seats, "auto_provision_seat",
                        lambda a, u: {"result": "disabled", "seat_limit": 5, "active_seats": 1})
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", UNEW)
    assert exc.value.status_code == 403
    assert "not a licensed seat" in exc.value.message


def test_bind_tenant_auto_provision_error_fails_closed(not_a_seat, monkeypatch):
    def boom(a, u):
        raise RuntimeError("control plane down")
    monkeypatch.setattr(saas_seats, "auto_provision_seat", boom)
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", UNEW)
    assert exc.value.status_code == 403


def test_bind_tenant_active_seat_never_reaches_auto_provision(monkeypatch):
    """An already-active seat is allowed WITHOUT touching auto_provision_seat."""
    monkeypatch.setattr(saas_seats, "is_active_seat", lambda a, u: True)

    def poison(a, u):
        raise AssertionError("auto_provision_seat must not run for an existing seat")
    monkeypatch.setattr(saas_seats, "auto_provision_seat", poison)
    assert mcp_server.bind_tenant(f"account:{ACCT_A}", UA) == UA


def test_bind_tenant_malformed_uuid_still_400_before_provision(not_a_seat, monkeypatch):
    monkeypatch.setattr(saas_seats, "auto_provision_seat",
                        lambda a, u: (_ for _ in ()).throw(AssertionError("must not run")))
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", "not-a-uuid")
    assert exc.value.status_code == 400


# ════════════════════════════════════════════════════════════════════════════════
# PART A — auto_provision_seat itself, over a faithful fake control-plane DB
# ════════════════════════════════════════════════════════════════════════════════

class _APFakeCursor:
    """Models public.accounts (cap/flag/status) + public.account_users (roster)."""

    def __init__(self, state):
        self.state = state          # {"accounts": {id:{...}}, "seats": [ {..} ]}
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._result = None
        if s.startswith("SELECT seat_limit, status, auto_provision_seats FROM public.accounts"):
            acct = self.state["accounts"].get(p[0])
            self._result = (
                (acct["seat_limit"], acct["status"], acct["auto"]) if acct else None)
        elif s.startswith("SELECT account_id::text, status FROM public.account_users WHERE user_id"):
            row = next((r for r in self.state["seats"] if r["user_id"] == p[0]), None)
            self._result = (row["account_id"], row["status"]) if row else None
        elif s.startswith("SELECT COUNT(*) FROM public.account_users"):
            acct = p[0]
            n = sum(1 for r in self.state["seats"]
                    if r["account_id"] == acct and r["status"] == "active")
            self._result = (n,)
        elif s.startswith("INSERT INTO public.account_users"):
            acct, user, status = p[0], p[1], "active"
            if any(r["user_id"] == user for r in self.state["seats"]):
                raise Exception("duplicate key value violates unique constraint account_users")
            self.state["seats"].append(
                {"account_id": acct, "user_id": user, "status": status})
        elif s.startswith("UPDATE public.account_users SET status = 'active'"):
            acct, user = p[0], p[1]
            for r in self.state["seats"]:
                if r["account_id"] == acct and r["user_id"] == user:
                    r["status"] = "active"

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _APFakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _APFakeCursor(self.state)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def ap_db(monkeypatch):
    state = {
        "accounts": {
            ACCT_A: {"seat_limit": 2, "status": "active", "auto": True},
            ACCT_B: {"seat_limit": 5, "status": "active", "auto": False},
        },
        "seats": [
            {"account_id": ACCT_A, "user_id": UA, "status": "active"},   # 1/2 used
            {"account_id": ACCT_B, "user_id": UB, "status": "active"},
        ],
    }

    @contextlib.contextmanager
    def _connection():
        yield _APFakeConn(state)
    monkeypatch.setattr(saas_seats.db, "connection", _connection)
    monkeypatch.setattr(saas_seats, "invalidate_seat_cache", lambda: None)
    return state


def test_ap_under_cap_provisions(ap_db):
    out = saas_seats.auto_provision_seat(ACCT_A, UNEW)   # 1/2 → 2/2
    assert out["result"] == "provisioned"
    assert any(r["user_id"] == UNEW and r["status"] == "active" for r in ap_db["seats"])


def test_ap_at_cap_rejects_no_seat_created(ap_db):
    saas_seats.auto_provision_seat(ACCT_A, UNEW)          # fills 2/2
    before = len(ap_db["seats"])
    out = saas_seats.auto_provision_seat(ACCT_A, "55555555-5555-4555-8555-555555555555")
    assert out["result"] == "at_cap"
    assert out["seat_limit"] == 2 and out["active_seats"] == 2
    assert len(ap_db["seats"]) == before                 # no seat created at cap


def test_ap_cross_account_conflict_not_reassigned(ap_db):
    """UB is B's seat; A auto-provisioning UB → conflict, B's seat untouched."""
    out = saas_seats.auto_provision_seat(ACCT_A, UB)
    assert out["result"] == "conflict"
    ub = next(r for r in ap_db["seats"] if r["user_id"] == UB)
    assert ub["account_id"] == ACCT_B and ub["status"] == "active"   # never stolen


def test_ap_disabled_rejects(ap_db):
    out = saas_seats.auto_provision_seat(ACCT_B, UNEW)    # B has auto=False
    assert out["result"] == "disabled"
    assert not any(r["user_id"] == UNEW for r in ap_db["seats"])


def test_ap_already_active_seat_is_provisioned(ap_db):
    out = saas_seats.auto_provision_seat(ACCT_A, UA)      # already A's active seat
    assert out["result"] == "provisioned"


def test_ap_missing_account_errors(ap_db):
    out = saas_seats.auto_provision_seat("ffffffff-ffff-4fff-8fff-ffffffffffff", UNEW)
    assert out["result"] == "error"


def test_ap_atomic_cap_two_sequential_first_touches(ap_db):
    """Best-effort atomicity proof: with 1 free seat, two distinct first-touches → exactly
    ONE provisioned, the other at_cap (the row-locked count→insert cannot over-issue)."""
    u1 = "66666666-6666-4666-8666-666666666666"
    u2 = "77777777-7777-4777-8777-777777777777"
    r1 = saas_seats.auto_provision_seat(ACCT_A, u1)       # 1/2 → 2/2
    r2 = saas_seats.auto_provision_seat(ACCT_A, u2)       # 2/2 → rejected
    results = {r1["result"], r2["result"]}
    assert results == {"provisioned", "at_cap"}
    active = sum(1 for r in ap_db["seats"]
                 if r["account_id"] == ACCT_A and r["status"] == "active")
    assert active == 2                                   # never exceeded the cap of 2


# ════════════════════════════════════════════════════════════════════════════════
# PART B — OPERATOR AUTH REALM
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "operator-unit-test-signing-secret-0123456789")
    yield


def test_operator_session_salt_separation(saas_on):
    """A tenant session can NEVER verify as an operator session and vice-versa (distinct salts)."""
    op = auth.issue_operator_session(dashboard_user_id=UA, username="root")
    tn = auth.issue_session(dashboard_user_id=UA, account_id=ACCT_A, username="alice")
    assert auth.verify_operator_session(op) is not None
    assert auth.verify_operator_session(tn) is None      # tenant token != operator
    assert auth.verify_session(op) is None               # operator token != tenant


@pytest.fixture
def live_operator_principal(monkeypatch):
    """The synthetic operator id ``UA`` still EXISTS and is ACTIVE in the control plane.

    The operator gates now re-validate the signed principal against public.dashboard_users on
    every request — a signature proves the token was minted by us, not that the operator still
    works here (deleting one used to leave their session sliding forever). These are pure gate
    UNIT tests over a synthetic id with no control-plane row, so the lookup is stubbed to the
    LIVE answer; the revocation contract itself is proved against real rows in
    tests/test_operator_session_revocation_and_perm_grant.py."""
    monkeypatch.setattr(auth, "operator_principal_active", lambda uid: True)
    yield


def test_require_operator_session_accepts_operator_denies_tenant(saas_on, live_operator_principal):
    from fastapi import HTTPException
    op = auth.issue_operator_session(dashboard_user_id=UA, username="root")
    req_op = _FakeRequest(cookies={auth.OPERATOR_SESSION_COOKIE_NAME: op})
    assert opc.require_operator_session(req_op) is True

    # A tenant session dropped into the operator cookie slot → 401 (salt mismatch).
    tn = auth.issue_session(dashboard_user_id=UB, account_id=ACCT_B, username="bob")
    req_tn = _FakeRequest(cookies={auth.OPERATOR_SESSION_COOKIE_NAME: tn})
    with pytest.raises(HTTPException) as exc:
        opc.require_operator_session(req_tn)
    assert exc.value.status_code == 401


def test_require_operator_session_csrf_on_unsafe(saas_on, live_operator_principal):
    from fastapi import HTTPException
    op = auth.issue_operator_session(dashboard_user_id=UA, username="root")
    # POST without CSRF → 403.
    req = _FakeRequest(method="POST", cookies={auth.OPERATOR_SESSION_COOKIE_NAME: op})
    with pytest.raises(HTTPException) as exc:
        opc.require_operator_session(req)
    assert exc.value.status_code == 403
    # POST with the session-bound operator CSRF → allowed.
    csrf = auth.issue_operator_csrf(dashboard_user_id=UA)
    ok = _FakeRequest(method="POST", cookies={auth.OPERATOR_SESSION_COOKIE_NAME: op},
                      headers={"x-csrf-token": csrf})
    assert opc.require_operator_session(ok) is True


def test_require_operator_accepts_key_or_session(saas_on, monkeypatch, live_operator_principal):
    from fastapi import HTTPException
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)

    # (1) operator KEY (break-glass) → True.
    assert saas_admin.require_operator(_FakeRequest(), f"Bearer {OP_KEY}") is True

    # (2) operator SESSION → True.
    op = auth.issue_operator_session(dashboard_user_id=UA, username="root")
    req = _FakeRequest(cookies={auth.OPERATOR_SESSION_COOKIE_NAME: op})
    assert saas_admin.require_operator(req, None) is True

    # (3) neither → 403.
    with pytest.raises(HTTPException) as exc:
        saas_admin.require_operator(_FakeRequest(), "Bearer wrong")
    assert exc.value.status_code == 403


def test_require_account_operator_session_is_operator_sentinel(saas_on, monkeypatch, live_operator_principal):
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    op = auth.issue_operator_session(dashboard_user_id=UA, username="root")
    req = _FakeRequest(cookies={auth.OPERATOR_SESSION_COOKIE_NAME: op})
    assert saas_admin.require_account(req, None) == "__operator__"
    # operator KEY too.
    assert saas_admin.require_account(_FakeRequest(), f"Bearer {OP_KEY}") == "__operator__"


def test_tenant_key_cannot_mint_operator_via_dashboard_users(saas_on, monkeypatch):
    """A role='operator' create through /admin/dashboard-users is HARD-rejected (403)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    # Even an ACCOUNT key that resolves fine cannot escalate to operator via this endpoint.
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: ACCT_A)
    # Guard must fire BEFORE any DB write — poison create to prove it never runs.
    monkeypatch.setattr(auth, "create_dashboard_user",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("must not create")))
    app = FastAPI()
    app.include_router(tc.router)
    client = TestClient(app)
    r = client.post("/admin/dashboard-users",
                    headers={"Authorization": "Bearer flk_acctA_key"},
                    json={"account_id": ACCT_A, "username": "sneaky", "role": "operator"})
    assert r.status_code == 403


def test_operator_bootstrap_from_key_creates_login(saas_on, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return "op-user-id-1"
    monkeypatch.setattr(auth, "create_dashboard_user", fake_create)
    app = FastAPI()
    app.include_router(opc.router)
    client = TestClient(app)

    # No key → 403.
    bad = client.post("/operator/bootstrap", json={"username": "root"})
    assert bad.status_code == 403

    # Operator KEY → creates the first operator login (account_id NULL, role='operator').
    ok = client.post("/operator/bootstrap",
                     headers={"Authorization": f"Bearer {OP_KEY}"},
                     json={"username": "root"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["role"] == "operator" and body["id"] == "op-user-id-1"
    assert body.get("temp_password")                     # temp password shown ONCE
    assert captured["account_id"] is None and captured["role"] == "operator"


def test_operator_login_issues_operator_session(saas_on, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    op_user = {
        "id": UA, "account_id": None, "username": "root",
        "password_hash": auth.hash_password("op-pw-123"), "totp_secret": None,
        "mfa_enabled": False, "role": "operator", "status": "active",
    }
    monkeypatch.setattr(auth, "get_user_by_username",
                        lambda u: op_user if u == "root" else None)
    monkeypatch.setattr(auth, "touch_last_login", lambda uid: None)
    app = FastAPI()
    app.include_router(opc.router)
    client = TestClient(app)

    r = client.post("/operator/login", json={"username": "root", "password": "op-pw-123"})
    assert r.status_code == 200 and r.json()["status"] == "authenticated"
    assert auth.OPERATOR_SESSION_COOKIE_NAME in r.cookies

    # A non-operator (tenant) row must NOT be able to log in via /operator/login.
    tenant_row = dict(op_user, role="admin", account_id=ACCT_A)
    monkeypatch.setattr(auth, "get_user_by_username",
                        lambda u: tenant_row if u == "root" else None)
    bad = client.post("/operator/login", json={"username": "root", "password": "op-pw-123"})
    assert bad.status_code == 401


# ════════════════════════════════════════════════════════════════════════════════
# PART C — TENANT PANIC (session-authed, one-way latch)
# ════════════════════════════════════════════════════════════════════════════════

class _PanicFakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._result = None
        if s.startswith("SELECT slug FROM public.accounts"):
            row = self.store.get(p[0])
            self._result = (row["slug"],) if row else None
        elif s.startswith("SELECT status, lock_origin FROM public.accounts"):
            row = self.store.get(p[0])
            self._result = (row["status"], row["lock_origin"]) if row else None
        elif s.startswith("UPDATE public.accounts SET status = 'locked', lock_origin = 'tenant_panic'"):
            row = self.store.get(p[0])
            if row:
                row["status"], row["lock_origin"] = "locked", "tenant_panic"
        elif s.startswith("UPDATE public.accounts SET status = %s, lock_origin = %s"):
            status, origin, acct = p
            row = self.store.get(acct)
            if row:
                row["status"], row["lock_origin"] = status, origin

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def panic_store(monkeypatch, saas_on):
    store = {ACCT_A: {"status": "active", "lock_origin": None, "slug": "acct-a"}}

    @contextlib.contextmanager
    def _connection():
        yield type("C", (), {
            "cursor": lambda self: _PanicFakeCursor(store),
            "commit": lambda self: None, "rollback": lambda self: None,
            "close": lambda self: None})()
    monkeypatch.setattr(saas_accounts.db, "connection", _connection)
    monkeypatch.setattr(saas_accounts, "invalidate_account_cache", lambda a: None)
    return store


def _session_req(account_id, user_id, *, method="POST", csrf=True):
    cookies = {auth.SESSION_COOKIE_NAME: auth.issue_session(
        dashboard_user_id=user_id, account_id=account_id, username="owner")}
    headers = {}
    if csrf:
        headers["x-csrf-token"] = auth.issue_csrf(dashboard_user_id=user_id)
    return _FakeRequest(method=method, cookies=cookies, headers=headers)


def test_tenant_panic_locks_scoped_to_session_account(panic_store):
    acct = tc.require_dashboard_session(_session_req(ACCT_A, UA))
    out = tc.tenant_panic(saas_admin.PanicRequest(confirm="acct-a"), account_id=acct)
    assert out["status"] == "locked" and out["lock_origin"] == "tenant_panic"
    assert panic_store[ACCT_A]["status"] == "locked"
    assert panic_store[ACCT_A]["lock_origin"] == "tenant_panic"


def test_tenant_panic_slug_mismatch_400(panic_store):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        tc.tenant_panic(saas_admin.PanicRequest(confirm="WRONG"), account_id=ACCT_A)
    assert exc.value.status_code == 400
    assert panic_store[ACCT_A]["status"] == "active"    # not locked


def test_tenant_panic_owner_cannot_clear_operator_can(panic_store):
    # Owner panics.
    tc.tenant_panic(saas_admin.PanicRequest(confirm="acct-a"), account_id=ACCT_A)
    # Owner (is_operator=False) tries to clear → 403, latch holds.
    res = saas_accounts.set_account_status(ACCT_A, "active", is_operator=False)
    assert res["ok"] is False and res["status_code"] == 403
    assert panic_store[ACCT_A]["lock_origin"] == "tenant_panic"
    # Operator clears → active, latch reset.
    res_op = saas_accounts.set_account_status(ACCT_A, "active", is_operator=True)
    assert res_op["ok"] is True and res_op["status"] == "active"
    assert panic_store[ACCT_A]["lock_origin"] is None


def test_tenant_panic_route_present():
    paths = {r.path for r in tc.router.routes}
    assert "/tenant/panic" in paths


# ════════════════════════════════════════════════════════════════════════════════
# SAAS-off preservation
# ════════════════════════════════════════════════════════════════════════════════

def test_bind_tenant_account_branch_only_on_account_principal(monkeypatch):
    """SAAS off / shared principal → bind_tenant NEVER hits the account/auto-provision branch."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setattr(mcp_server, "FAULTLINE_USER_ID", "")

    def poison(a, u):
        raise AssertionError("auto_provision must not run off the account branch")
    monkeypatch.setattr(saas_seats, "auto_provision_seat", poison)
    assert mcp_server.bind_tenant("shared", UA) == UA


def test_operator_endpoints_404_when_saas_off(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.delenv("SAAS_MODE", raising=False)
    with pytest.raises(HTTPException) as exc:
        opc.require_operator_session(_FakeRequest(cookies={auth.OPERATOR_SESSION_COOKIE_NAME: "x"}))
    assert exc.value.status_code in (401, 404)


# ════════════════════════════════════════════════════════════════════════════════
# LIVE-DB integration — real auto_provision_seat cap + conflict (auto-skip w/o DSN)
# ════════════════════════════════════════════════════════════════════════════════

def _control_plane_available() -> bool:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=2)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.account_users'), "
                        "to_regclass('public.accounts')")
            r = cur.fetchone()
            # migration 137 column present?
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='accounts' "
                        "AND column_name='auto_provision_seats'")
            has_col = cur.fetchone() is not None
        conn.close()
        return all(r) and has_col
    except Exception:
        return False


live_db = pytest.mark.skipif(
    not _control_plane_available(),
    reason="no reachable POSTGRES_DSN control plane w/ migration 137 (auto_provision_seats)",
)


@live_db
def test_live_auto_provision_cap_and_conflict():
    import psycopg2
    dsn = os.environ["POSTGRES_DSN"]
    a_slug = f"ap-a-{uuid.uuid4().hex[:8]}"
    b_slug = f"ap-b-{uuid.uuid4().hex[:8]}"
    ub = str(uuid.uuid4())
    unew = str(uuid.uuid4())
    unew2 = str(uuid.uuid4())
    conn = psycopg2.connect(dsn)
    acct_a = acct_b = None
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO public.accounts (name, slug, seat_limit, "
                        "auto_provision_seats) VALUES (%s,%s,%s,true) RETURNING account_id::text",
                        ("ap A", a_slug, 1))
            acct_a = cur.fetchone()[0]
            cur.execute("INSERT INTO public.accounts (name, slug, seat_limit, "
                        "auto_provision_seats) VALUES (%s,%s,%s,true) RETURNING account_id::text",
                        ("ap B", b_slug, 5))
            acct_b = cur.fetchone()[0]
            cur.execute("INSERT INTO public.account_users (account_id,user_id,status) "
                        "VALUES (%s,%s,'active')", (acct_b, ub))
        conn.commit()
        saas_seats.invalidate_seat_cache()

        # under cap (A: 0/1) → provisioned
        assert saas_seats.auto_provision_seat(acct_a, unew)["result"] == "provisioned"
        # now A is 1/1 → at cap
        assert saas_seats.auto_provision_seat(acct_a, unew2)["result"] == "at_cap"
        # cross-account: A tries B's user → conflict, never reassigned
        assert saas_seats.auto_provision_seat(acct_a, ub)["result"] == "conflict"
        with conn.cursor() as cur:
            cur.execute("SELECT account_id::text FROM public.account_users WHERE user_id=%s", (ub,))
            assert cur.fetchone()[0] == acct_b          # B's seat untouched
    finally:
        with conn.cursor() as cur:
            for acct in (acct_a, acct_b):
                if acct:
                    cur.execute("DELETE FROM public.account_users WHERE account_id=%s", (acct,))
                    cur.execute("DELETE FROM public.accounts WHERE account_id=%s", (acct,))
        conn.commit()
        conn.close()
        saas_seats.invalidate_seat_cache()
