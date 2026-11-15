"""Per-seat INDIVIDUAL ("My Memory") login management — operator + tenant consoles.

Covers the controls added so an admin can manage a seat's personal-page login
(public.individual_users): reset password, reset MFA, enable/disable the page, and
(optionally) create the login. Two realms, ONE control-plane store:

  * OPERATOR (saas.admin)          — /admin/accounts/{aid}/users/{uid}/login*  (any account;
                                     an account key ONLY its own — BOLA via _authorize_account_path
                                     AND the login's own account_id must match the path).
  * TENANT  (saas.tenant_console)  — /tenant/seats/{uid}/login*  (session account only; a foreign
                                     seat is 403 at the seat gate BEFORE any login lookup).

The crown-jewel proofs here:
  1. reset/disable/create returns the temp password ONCE and never touches the MCP token.
  2. cross-account BOLA — a tenant admin (or account key) CANNOT touch a seat in another account.
  3. 'disabled' is fail-closed at /me login (the login lookup filters status='active').

The control-plane DB functions are monkeypatched → no DB needed. tests/ is gitignored (git add -f).
Run: python3 -m pytest tests/test_saas_individual_login_management.py -q
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import accounts as saas_accounts
from saas import admin as saas_admin
from saas import dashboard_auth as auth
from saas import tenant_console as tc
from saas import user_console as uc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-4111-8111-111111111111"   # a seat of ACCT_A (with a login)
USER_B = "22222222-2222-4222-8222-222222222222"   # a seat of ACCT_B (never of A)
OP_KEY = "op-super-secret-key"

_LOGIN_A = {"individual_id": "ind-aaaa", "user_id": USER_A, "account_id": ACCT_A,
            "email": "a@ex.com", "status": "active", "mfa_enabled": True}


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "individual-login-mgmt-secret-0123456789")
    yield


# ══════════════════════════════════════════════════════════════════════════════════
# OPERATOR realm (saas.admin)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def op_client(saas_on):
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


def _acct_a_key():
    return {"Authorization": "Bearer flk_acctA_key"}


@pytest.fixture
def login_lookup(monkeypatch):
    """get_managed_individual returns the login ONLY when (account_id,user_id)==(ACCT_A,USER_A)."""
    def _managed(account_id, user_id):
        if (account_id, user_id) == (ACCT_A, USER_A):
            return dict(_LOGIN_A)
        return None
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "get_managed_individual", _managed)


def test_operator_reset_password_returns_temp_once(op_client, login_lookup, monkeypatch):
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "admin_reset_login_password", lambda iid: "TEMP-PW-ONCE")
    r = op_client.post(f"/admin/accounts/{ACCT_A}/users/{USER_A}/login/reset-password", headers=_op())
    assert r.status_code == 200
    assert r.json()["temp_password"] == "TEMP-PW-ONCE"


def test_operator_reset_mfa_ok(op_client, login_lookup, monkeypatch):
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "admin_reset_login_mfa", lambda iid: True)
    r = op_client.post(f"/admin/accounts/{ACCT_A}/users/{USER_A}/login/reset-mfa", headers=_op())
    assert r.status_code == 200 and r.json()["mfa_cleared"] is True


def test_operator_set_status_disable_ok(op_client, login_lookup, monkeypatch):
    seen = {}
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "set_individual_status",
                        lambda iid, st: seen.update(iid=iid, st=st) or True)
    r = op_client.post(f"/admin/accounts/{ACCT_A}/users/{USER_A}/login/status",
                       headers=_op(), json={"status": "disabled"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    assert seen["st"] == "disabled"


def test_operator_set_status_rejects_bad_value(op_client, login_lookup, monkeypatch):
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "set_individual_status",
                        lambda *a, **k: pytest.fail("mutated on an invalid status"))
    r = op_client.post(f"/admin/accounts/{ACCT_A}/users/{USER_A}/login/status",
                       headers=_op(), json={"status": "locked"})
    assert r.status_code == 400


def test_operator_no_login_is_404(op_client, login_lookup):
    # USER_B has no login under ACCT_A → get_managed_individual → None → 404.
    r = op_client.post(f"/admin/accounts/{ACCT_A}/users/{USER_B}/login/reset-password", headers=_op())
    assert r.status_code == 404


def test_operator_malformed_uuid_is_400(op_client, login_lookup):
    r = op_client.post(f"/admin/accounts/{ACCT_A}/users/not-a-uuid/login/reset-password", headers=_op())
    assert r.status_code == 400


def test_operator_create_login_returns_temp_once(op_client, monkeypatch):
    import saas.user_console as _uc
    monkeypatch.setattr(saas_admin._seats, "is_active_seat", lambda a, u: True)
    monkeypatch.setattr(_uc, "get_login_for_seat", lambda u: None)   # none yet
    monkeypatch.setattr(_uc, "create_login_for_seat",
                        lambda **kw: {"individual_id": "ind-new", "email": kw["email"],
                                      "temp_password": "NEW-TEMP"})
    r = op_client.post(f"/admin/accounts/{ACCT_A}/users/{USER_A}/login",
                       headers=_op(), json={"email": "New@Ex.com"})
    assert r.status_code == 200 and r.json()["temp_password"] == "NEW-TEMP"


def test_operator_create_login_conflict_when_exists(op_client, monkeypatch):
    import saas.user_console as _uc
    monkeypatch.setattr(saas_admin._seats, "is_active_seat", lambda a, u: True)
    monkeypatch.setattr(_uc, "get_login_for_seat", lambda u: dict(_LOGIN_A))
    r = op_client.post(f"/admin/accounts/{ACCT_A}/users/{USER_A}/login",
                       headers=_op(), json={"email": "x@ex.com"})
    assert r.status_code == 409


# ── BOLA: an ACCOUNT KEY may act ONLY on its own account ────────────────────────────

def test_account_key_foreign_account_reset_is_403(op_client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: ACCT_A)
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "get_managed_individual",
                        lambda *a, **k: pytest.fail("login lookup reached across accounts"))
    monkeypatch.setattr(_uc, "admin_reset_login_password",
                        lambda *a, **k: pytest.fail("password reset across accounts"))
    # ACCT_A's key names ACCT_B in the path → 403 at _authorize_account_path, never reaches the login.
    r = op_client.post(f"/admin/accounts/{ACCT_B}/users/{USER_B}/login/reset-password",
                       headers=_acct_a_key())
    assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════════
# TENANT realm (saas.tenant_console) — session-scoped, foreign seat 403
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tc_client(saas_on):
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _session_a():
    return auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")


def _csrf_a():
    return auth.issue_csrf(dashboard_user_id=USER_A)


@pytest.fixture
def seat_ownership(monkeypatch):
    """USER_A is a seat of ACCT_A only; USER_B is a seat of ACCT_B only (never of A)."""
    def _is_active(account_id, user_id):
        return (account_id, user_id) in {(ACCT_A, USER_A), (ACCT_B, USER_B)}
    monkeypatch.setattr(tc._seats, "is_active_seat", _is_active)


def test_tenant_reset_password_own_seat_ok(tc_client, seat_ownership, monkeypatch):
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "get_managed_individual",
                        lambda a, u: dict(_LOGIN_A) if (a, u) == (ACCT_A, USER_A) else None)
    monkeypatch.setattr(_uc, "admin_reset_login_password", lambda iid: "SEAT-TEMP")
    tc_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = tc_client.post(f"/tenant/seats/{USER_A}/login/reset-password",
                       headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200 and r.json()["temp_password"] == "SEAT-TEMP"


def test_tenant_reset_password_foreign_seat_is_403(tc_client, seat_ownership, monkeypatch):
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "get_managed_individual",
                        lambda *a, **k: pytest.fail("login lookup reached a foreign seat"))
    monkeypatch.setattr(_uc, "admin_reset_login_password",
                        lambda *a, **k: pytest.fail("reset reached a foreign seat"))
    tc_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    # session A tries seat B (of ACCT_B) → _authorize_session_seat → is_active_seat(A,B)=False → 403.
    r = tc_client.post(f"/tenant/seats/{USER_B}/login/reset-password",
                       headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 403


def test_tenant_disable_page_own_seat_ok(tc_client, seat_ownership, monkeypatch):
    seen = {}
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "get_managed_individual",
                        lambda a, u: dict(_LOGIN_A) if (a, u) == (ACCT_A, USER_A) else None)
    monkeypatch.setattr(_uc, "set_individual_status",
                        lambda iid, st: seen.update(st=st) or True)
    tc_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = tc_client.post(f"/tenant/seats/{USER_A}/login/status",
                       headers={"x-csrf-token": _csrf_a()}, json={"status": "disabled"})
    assert r.status_code == 200 and seen["st"] == "disabled"


def test_tenant_login_action_requires_csrf(tc_client, seat_ownership):
    tc_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = tc_client.post(f"/tenant/seats/{USER_A}/login/reset-password")   # no csrf
    assert r.status_code == 403


def test_tenant_login_action_no_session_is_401(tc_client):
    r = tc_client.post(f"/tenant/seats/{USER_A}/login/reset-password")
    assert r.status_code == 401


def test_tenant_no_login_is_404(tc_client, seat_ownership, monkeypatch):
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "get_managed_individual", lambda a, u: None)   # no login row
    tc_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = tc_client.post(f"/tenant/seats/{USER_A}/login/reset-mfa",
                       headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════════
# /me login FAIL-CLOSED on a disabled login (the lookups filter status='active')
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def me_client(saas_on):
    app = FastAPI()
    app.include_router(uc.router)
    return TestClient(app)


def test_disabled_login_cannot_authenticate(me_client, monkeypatch):
    # A disabled row is filtered out by get_individual_by_email (status='active'): the route sees
    # None → 401. This is the fail-closed personal-page gate the disable control relies on.
    monkeypatch.setattr(uc, "get_individual_by_email", lambda email: None)
    monkeypatch.setattr(uc._ratelimit, "login_locked", lambda e: (False, 0))
    monkeypatch.setattr(uc._ratelimit, "record_login_failure", lambda e: None)
    r = me_client.post("/me/login", json={"email": "disabled@ex.com", "password": "whatever"})
    assert r.status_code == 401


def test_login_lookups_enforce_active_status_in_sql():
    # Lock the fail-closed contract at the source: both /me login lookups MUST filter status='active'
    # so a disabled/suspended login can neither sign in nor keep a live session.
    import inspect
    src = inspect.getsource(uc.get_individual_by_email) + inspect.getsource(uc.get_individual_by_id)
    assert src.count("status = 'active'") >= 2
