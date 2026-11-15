"""Tenant-console HUMAN auth realm — unit + seam tests (fast, no DB).

Proves the argon2id/TOTP/session/CSRF primitives and the BOLA-safe session→account
binding at ``require_dashboard_session``. Live-DB paths (public.dashboard_users) are
monkeypatched, so this file is always green without a control plane.

Run: python3 -m pytest tests/test_saas_tenant_auth.py -q   (tests/ is gitignored → git add -f)
"""

import time

import pytest

from saas import dashboard_auth as auth
from saas import keys as saas_keys
from saas import tenant_console as tc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"
KNOWN_TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # RFC test vector base32 secret


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
    yield


# ════════════════════════════════════════════════════════════════════════════════
# argon2id passwords — the REALM SEPARATION invariant
# ════════════════════════════════════════════════════════════════════════════════

def test_password_hash_is_argon2id_not_sha256():
    h = auth.hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$"), "human passwords MUST use argon2id"
    # And it must NOT look like the SHA-256 used for M2M API keys (different realm).
    sha = saas_keys.hash_key("correct horse battery staple")
    assert not sha.startswith("$argon2id$")
    assert len(sha) == 64 and h != sha


def test_password_salted_hashes_differ():
    a = auth.hash_password("same-password")
    b = auth.hash_password("same-password")
    assert a != b  # per-hash random salt


def test_correct_password_verifies_wrong_fails():
    h = auth.hash_password("s3cret-pw")
    assert auth.verify_password(h, "s3cret-pw") is True
    assert auth.verify_password(h, "wrong-pw") is False
    assert auth.verify_password(h, "") is False


# ════════════════════════════════════════════════════════════════════════════════
# TOTP
# ════════════════════════════════════════════════════════════════════════════════

def test_totp_known_secret_roundtrip():
    import pyotp
    code = pyotp.TOTP(KNOWN_TOTP_SECRET).now()
    assert auth.verify_totp(KNOWN_TOTP_SECRET, code) is True
    assert auth.verify_totp(KNOWN_TOTP_SECRET, "000000") is False
    uri = auth.totp_provisioning_uri(KNOWN_TOTP_SECRET, "alice")
    assert uri.startswith("otpauth://totp/") and "FaultLine" in uri


# ════════════════════════════════════════════════════════════════════════════════
# Session + CSRF primitives
# ════════════════════════════════════════════════════════════════════════════════

def test_session_roundtrip_and_scope(saas_on):
    tok = auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")
    payload = auth.verify_session(tok)
    assert payload and payload["acct"] == ACCT_A and payload["sub"] == USER_A


def test_tampered_session_rejected(saas_on):
    tok = auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")
    tampered = tok[:-2] + ("aa" if not tok.endswith("aa") else "bb")
    assert auth.verify_session(tampered) is None


def test_expired_session_rejected(saas_on, monkeypatch):
    tok = auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")
    monkeypatch.setattr(auth, "SESSION_TTL_SECONDS", -1)  # force expiry
    assert auth.verify_session(tok) is None


def test_session_secret_unset_fails_closed(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.delenv("SAAS_SESSION_SECRET", raising=False)
    with pytest.raises(Exception):
        auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")


def test_csrf_bound_to_session_user(saas_on):
    csrf = auth.issue_csrf(dashboard_user_id=USER_A)
    assert auth.verify_csrf(csrf, dashboard_user_id=USER_A) is True
    # A CSRF minted for user A cannot authorize a mutation under user B's session.
    assert auth.verify_csrf(csrf, dashboard_user_id=USER_B) is False
    assert auth.verify_csrf("garbage", dashboard_user_id=USER_A) is False


def test_mfa_challenge_is_not_a_session(saas_on):
    ch = auth.issue_mfa_challenge(dashboard_user_id=USER_A)
    assert auth.verify_mfa_challenge(ch) == USER_A
    # A challenge token must NOT validate as a full session (namespaced salts).
    assert auth.verify_session(ch) is None


# ════════════════════════════════════════════════════════════════════════════════
# require_dashboard_session — the BOLA-safe session→account binding
# ════════════════════════════════════════════════════════════════════════════════

class _FakeRequest:
    def __init__(self, method="GET", cookies=None, headers=None):
        self.method = method
        self.cookies = cookies or {}
        self.headers = headers or {}


def test_session_resolves_its_own_account_and_only_that(saas_on):
    sess_a = auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")
    req = _FakeRequest(cookies={auth.SESSION_COOKIE_NAME: sess_a})
    assert tc.require_dashboard_session(req) == ACCT_A

    sess_b = auth.issue_session(dashboard_user_id=USER_B, account_id=ACCT_B, username="bob")
    req_b = _FakeRequest(cookies={auth.SESSION_COOKIE_NAME: sess_b})
    resolved_b = tc.require_dashboard_session(req_b)
    assert resolved_b == ACCT_B and resolved_b != ACCT_A


def test_no_cookie_is_401(saas_on):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        tc.require_dashboard_session(_FakeRequest())
    assert exc.value.status_code == 401


def test_tampered_cookie_is_401(saas_on):
    from fastapi import HTTPException
    sess = auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")
    req = _FakeRequest(cookies={auth.SESSION_COOKIE_NAME: sess[:-3] + "xxx"})
    with pytest.raises(HTTPException) as exc:
        tc.require_dashboard_session(req)
    assert exc.value.status_code == 401


def test_csrf_required_on_unsafe_methods(saas_on):
    from fastapi import HTTPException
    sess = auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")

    # POST without CSRF → 403.
    req = _FakeRequest(method="POST", cookies={auth.SESSION_COOKIE_NAME: sess})
    with pytest.raises(HTTPException) as exc:
        tc.require_dashboard_session(req)
    assert exc.value.status_code == 403

    # POST with a CSRF bound to a DIFFERENT user → 403 (cross-session CSRF blocked).
    wrong_csrf = auth.issue_csrf(dashboard_user_id=USER_B)
    req2 = _FakeRequest(method="POST", cookies={auth.SESSION_COOKIE_NAME: sess},
                        headers={"x-csrf-token": wrong_csrf})
    with pytest.raises(HTTPException) as exc2:
        tc.require_dashboard_session(req2)
    assert exc2.value.status_code == 403

    # POST with the correct session-bound CSRF → allowed, resolves the session's account.
    good_csrf = auth.issue_csrf(dashboard_user_id=USER_A)
    req3 = _FakeRequest(method="POST", cookies={auth.SESSION_COOKIE_NAME: sess},
                        headers={"x-csrf-token": good_csrf})
    assert tc.require_dashboard_session(req3) == ACCT_A


# ════════════════════════════════════════════════════════════════════════════════
# SAAS-off → helpers no-op / deny
# ════════════════════════════════════════════════════════════════════════════════

def test_saas_off_session_dependency_denies(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "irrelevant")
    # Even with a would-be cookie, saas off → the dependency 404s (no-op realm).
    req = _FakeRequest(cookies={auth.SESSION_COOKIE_NAME: "whatever"})
    with pytest.raises(HTTPException) as exc:
        tc.require_dashboard_session(req)
    assert exc.value.status_code == 404


def test_router_paths_present():
    paths = {r.path for r in tc.router.routes}
    assert {"/tenant/login", "/tenant/mfa/verify", "/tenant/logout", "/tenant/me",
            "/admin/dashboard-users"}.issubset(paths)


# ════════════════════════════════════════════════════════════════════════════════
# Endpoint flow: login → (MFA gate) → session, via FastAPI TestClient
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def client(saas_on):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _fake_user(mfa=False, secret=None, pw="pw-123456"):
    return {
        "id": USER_A, "account_id": ACCT_A, "username": "alice",
        "password_hash": auth.hash_password(pw), "totp_secret": secret,
        "mfa_enabled": mfa, "role": "admin", "status": "active",
    }


def test_login_no_mfa_issues_session_cookie(client, monkeypatch):
    user = _fake_user(mfa=False)
    monkeypatch.setattr(auth, "get_user_by_username", lambda u: user if u == "alice" else None)
    monkeypatch.setattr(auth, "touch_last_login", lambda uid: None)
    from saas import mfa as _mfa
    monkeypatch.setattr(_mfa, "require_mfa_decision", lambda realm, pid: False)

    r = client.post("/tenant/login", json={"username": "alice", "password": "pw-123456"})
    assert r.status_code == 200
    assert r.json()["status"] == "authenticated"
    assert r.json().get("csrf_token")
    assert auth.SESSION_COOKIE_NAME in r.cookies

    bad = client.post("/tenant/login", json={"username": "alice", "password": "nope"})
    assert bad.status_code == 401


def test_mfa_enabled_gates_session_until_totp(client, monkeypatch):
    import pyotp
    user = _fake_user(mfa=True, secret=KNOWN_TOTP_SECRET)
    monkeypatch.setattr(auth, "get_user_by_username", lambda u: user if u == "alice" else None)
    monkeypatch.setattr(auth, "get_user_by_id", lambda i: user if i == USER_A else None)
    monkeypatch.setattr(auth, "touch_last_login", lambda uid: None)
    # Login now gates on the multi-factor core (public.mfa_factors), not the legacy column — mock it
    # so this DB-less test exercises the tenant login/verify wiring: one enabled TOTP factor whose
    # code is KNOWN_TOTP_SECRET. (Real DB-backed core coverage lives in tests/test_mfa_core.py.)
    from saas import mfa as _mfa
    monkeypatch.setattr(_mfa, "principal_has_enabled_factor", lambda realm, pid: True)
    monkeypatch.setattr(_mfa, "require_mfa_decision", lambda realm, pid: True)
    monkeypatch.setattr(_mfa, "enabled_factors_for_login",
                        lambda realm, pid: [{"id": "f1", "factor_type": "totp",
                                             "label": "Authenticator", "email_masked": None}])
    monkeypatch.setattr(_mfa, "verify_totp_any",
                        lambda realm, pid, code: pyotp.TOTP(KNOWN_TOTP_SECRET).verify(code, valid_window=1))

    # Step 1: password correct but MFA on → NO session cookie, a challenge instead.
    r = client.post("/tenant/login", json={"username": "alice", "password": "pw-123456"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "mfa_required" and body.get("mfa_token")
    assert auth.SESSION_COOKIE_NAME not in r.cookies  # session withheld

    # Step 2: wrong TOTP → 401 (still no session).
    bad = client.post("/tenant/mfa/verify",
                      json={"mfa_token": body["mfa_token"], "code": "000000"})
    assert bad.status_code == 401

    # Step 3: correct TOTP → session issued.
    code = pyotp.TOTP(KNOWN_TOTP_SECRET).now()
    ok = client.post("/tenant/mfa/verify",
                     json={"mfa_token": body["mfa_token"], "code": code})
    assert ok.status_code == 200 and ok.json()["status"] == "authenticated"
    assert auth.SESSION_COOKIE_NAME in ok.cookies


def test_me_returns_only_session_scope(client, monkeypatch):
    sess = auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")
    client.cookies.set(auth.SESSION_COOKIE_NAME, sess)
    r = client.get("/tenant/me")
    assert r.status_code == 200
    data = r.json()
    assert data["account_id"] == ACCT_A and data["username"] == "alice"
    assert ACCT_B not in data.values()
