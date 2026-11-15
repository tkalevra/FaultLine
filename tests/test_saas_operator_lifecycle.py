"""Operator account-lifecycle UX (SAAS_MODE) — first-run setup, self-service, break-glass.

Covers the operator lifecycle built on top of the migration-137 operator realm:

  * GET /operator/needs-setup — first-run detection (true iff ZERO operators).
  * POST /operator/bootstrap — creates the FIRST operator ONLY (409 once one exists).
  * Self-service (operator session + CSRF): change-password (wrong current → 401),
    MFA enroll → confirm → verify roundtrip (real pyotp secret), login gated by MFA once on.
  * Break-glass (operator KEY): reset-password lets the operator log in with the new temp;
    reset-mfa clears MFA; cannot delete the LAST operator (409).
  * BOLA: a TENANT session can never satisfy /operator/* nor /admin/operator-users.

No DB: the dashboard_auth control-plane helpers are faked over an in-memory operator store;
argon2 + pyotp stay REAL so the crypto roundtrips are genuine.

Run: python3 -m pytest tests/test_saas_operator_lifecycle.py -q   (tests/ gitignored → git add -f)
"""

import uuid

import pytest

# Control-plane singletons (the append-only ACTIVE contract chain, the platform settings row,
# the global operator realm) are shared by every suite in one database: the suites that
# write or depend on them run on one xdist worker (under --dist loadgroup, see tests/conftest.py).
pytestmark = pytest.mark.xdist_group("control_plane")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import dashboard_auth as auth
from saas import mfa as mfa_core
from saas import operator_console as opc

try:
    import pyotp
except Exception:  # pragma: no cover
    pyotp = None

OP_KEY = "op-super-secret-key"
ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UTENANT = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "operator-lifecycle-test-secret-0123456789")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    yield


# ── in-memory operator store faking the dashboard_auth control-plane helpers ─────────

@pytest.fixture
def store(monkeypatch):
    """{id: {id, account_id, username, password_hash, totp_secret, mfa_enabled, role, status}}"""
    db = {}

    def _mk(*, account_id, username, password_hash, totp_secret=None,
            mfa_enabled=False, role="admin"):
        if any(u["username"] == username for u in db.values()):
            return None
        uid = str(uuid.uuid4())
        db[uid] = {
            "id": uid, "account_id": account_id, "username": username,
            "password_hash": password_hash, "totp_secret": totp_secret,
            "mfa_enabled": bool(mfa_enabled), "role": role, "status": "active",
        }
        return uid

    def _by_username(u):
        return next((dict(x) for x in db.values()
                     if x["username"] == u and x["status"] == "active"), None)

    def _by_id(uid):
        row = db.get(uid)
        return dict(row) if row and row["status"] == "active" else None

    def _op_by_id(uid):
        row = db.get(uid)
        if row and row["role"] == "operator" and row["account_id"] is None:
            return dict(row)
        return None

    def _count_ops():
        return sum(1 for x in db.values()
                   if x["role"] == "operator" and x["account_id"] is None)

    def _list_ops():
        return [
            {"id": x["id"], "username": x["username"], "mfa_enabled": x["mfa_enabled"],
             "status": x["status"], "role": x["role"], "created_at": None,
             "last_login_at": None}
            for x in db.values() if x["role"] == "operator" and x["account_id"] is None
        ]

    def _set_pw(uid, ph):
        if uid in db:
            db[uid]["password_hash"] = ph
            return True
        return False

    def _set_totp(uid, *, totp_secret, mfa_enabled):
        if uid in db:
            db[uid]["totp_secret"] = totp_secret
            db[uid]["mfa_enabled"] = bool(mfa_enabled)
            return True
        return False

    def _delete(uid):
        return db.pop(uid, None) is not None

    def _principal_active(uid):
        """Backs the operator-session REVOCATION re-check against this same fake store.

        ``require_operator_session`` now re-validates the signed principal on every request, so
        a session for an id this store does not hold (or holds as non-'active') is refused —
        deleting an operator ends their live session instead of leaving it sliding until natural
        expiry. Wired to ``db`` rather than stubbed True so the suite keeps real teeth: pop the
        row and the cookie stops working, exactly as in production."""
        row = db.get(str(uid))
        return bool(row and row["role"] == "operator"
                    and row["account_id"] is None and row["status"] == "active")

    monkeypatch.setattr(auth, "operator_principal_active", _principal_active)
    monkeypatch.setattr(auth, "create_dashboard_user", _mk)
    monkeypatch.setattr(auth, "get_user_by_username", _by_username)
    monkeypatch.setattr(auth, "get_user_by_id", _by_id)
    monkeypatch.setattr(auth, "get_operator_by_id", _op_by_id)
    monkeypatch.setattr(auth, "count_operators", _count_ops)
    monkeypatch.setattr(auth, "list_operators", _list_ops)
    monkeypatch.setattr(auth, "set_password", _set_pw)
    monkeypatch.setattr(auth, "set_totp_secret", _set_totp)
    monkeypatch.setattr(auth, "delete_dashboard_user", _delete)
    monkeypatch.setattr(auth, "touch_last_login", lambda uid: None)
    return db


# ── in-memory multi-factor store faking saas.mfa (which otherwise hits public.mfa_factors) ──
# The operator MFA endpoints now call saas.mfa (M.*); this mock backs those calls with an
# in-memory factor dict so the ENDPOINT WIRING (login gate, challenge, verify routing, remove)
# is exercised without a DB. The real DB-backed core is covered by tests/test_mfa_core.py.
# pyotp stays REAL so the TOTP roundtrip is genuine.

@pytest.fixture(autouse=True)
def mfa_store(monkeypatch):
    factors = {}  # factor_id -> {realm, pid, factor_type, label, secret, enabled}

    def add_totp(realm, pid, *, label, account_label):
        fid = str(uuid.uuid4())
        secret = pyotp.random_base32() if pyotp else "JBSWY3DPEHPK3PXP"
        factors[fid] = {"realm": realm, "pid": str(pid), "factor_type": "totp",
                        "label": label or "Authenticator", "secret": secret, "enabled": False}
        uri = pyotp.totp.TOTP(secret).provisioning_uri("x", issuer_name="FaultLine") \
            if pyotp else "otpauth://totp/x"
        return {"ok": True, "factor_id": fid, "secret": secret,
                "provisioning_uri": uri, "qr": None}

    def _mine(realm, pid, fid):
        f = factors.get(fid)
        return f if f and f["realm"] == realm and f["pid"] == str(pid) else None

    def confirm_totp(realm, pid, fid, code):
        f = _mine(realm, pid, fid)
        if f and f["factor_type"] == "totp" and pyotp and pyotp.TOTP(f["secret"]).verify(code, valid_window=1):
            f["enabled"] = True
            return True
        return False

    def verify_totp_any(realm, pid, code):
        return any(f["enabled"] and f["factor_type"] == "totp" and f["realm"] == realm
                   and f["pid"] == str(pid) and pyotp and pyotp.TOTP(f["secret"]).verify(code, valid_window=1)
                   for f in factors.values())

    def has_enabled(realm, pid):
        return any(f["enabled"] and f["realm"] == realm and f["pid"] == str(pid) for f in factors.values())

    def enabled_for_login(realm, pid):
        return [{"id": k, "factor_type": f["factor_type"], "label": f["label"], "email_masked": None}
                for k, f in factors.items()
                if f["enabled"] and f["realm"] == realm and f["pid"] == str(pid)]

    def list_factors(realm, pid):
        return [{"id": k, "factor_type": f["factor_type"], "label": f["label"], "enabled": f["enabled"],
                 "email_masked": None, "created_at": None, "last_used_at": None}
                for k, f in factors.items() if f["realm"] == realm and f["pid"] == str(pid)]

    def remove(realm, pid, fid):
        return factors.pop(fid, None) is not None if _mine(realm, pid, fid) else False

    def remove_all(realm, pid):
        for k in [k for k, f in factors.items() if f["realm"] == realm and f["pid"] == str(pid)]:
            del factors[k]
        return True

    for name, fn in (("add_totp_factor", add_totp), ("confirm_totp_factor", confirm_totp),
                     ("verify_totp_any", verify_totp_any),
                     ("principal_has_enabled_factor", has_enabled),
                     ("require_mfa_decision", has_enabled),
                     ("enabled_factors_for_login", enabled_for_login),
                     ("list_factors", list_factors), ("remove_factor", remove),
                     ("remove_all_factors", remove_all)):
        monkeypatch.setattr(mfa_core, name, fn)
    return factors


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(opc.router)
    return TestClient(app)


def _mk_operator(store, username="root", password="op-pw-123", mfa=False, secret=None):
    uid = auth.create_dashboard_user(
        account_id=None, username=username,
        password_hash=auth.hash_password(password),
        totp_secret=secret, mfa_enabled=mfa, role="operator")
    return uid


def _op_session(client, uid, username="root"):
    """Attach an operator session cookie to the client; return the matching CSRF token."""
    client.cookies.set(auth.OPERATOR_SESSION_COOKIE_NAME,
                       auth.issue_operator_session(dashboard_user_id=uid, username=username))
    return auth.issue_operator_csrf(dashboard_user_id=uid)


# ════════════════════════════════════════════════════════════════════════════════
# needs-setup
# ════════════════════════════════════════════════════════════════════════════════

def test_needs_setup_true_when_zero_operators(store, client):
    r = client.get("/operator/needs-setup")
    assert r.status_code == 200
    assert r.json() == {"setup_required": True}


def test_needs_setup_false_once_an_operator_exists(store, client):
    _mk_operator(store)
    r = client.get("/operator/needs-setup")
    assert r.status_code == 200
    assert r.json()["setup_required"] is False


def test_needs_setup_is_unauthenticated(store, client):
    # No cookie / no key at all → still 200 (info-minimal, safe).
    r = client.get("/operator/needs-setup")
    assert r.status_code == 200


# ════════════════════════════════════════════════════════════════════════════════
# bootstrap — first operator ONLY
# ════════════════════════════════════════════════════════════════════════════════

def test_bootstrap_creates_first_operator(store, client):
    r = client.post("/operator/bootstrap",
                    headers={"Authorization": f"Bearer {OP_KEY}"},
                    json={"username": "root"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "operator" and body.get("temp_password")
    assert auth.count_operators() == 1


def test_bootstrap_refused_once_an_operator_exists(store, client):
    _mk_operator(store)
    r = client.post("/operator/bootstrap",
                    headers={"Authorization": f"Bearer {OP_KEY}"},
                    json={"username": "second"})
    assert r.status_code == 409
    assert auth.count_operators() == 1  # not created


def test_bootstrap_requires_the_key(store, client):
    r = client.post("/operator/bootstrap", json={"username": "root"})
    assert r.status_code == 403


# ════════════════════════════════════════════════════════════════════════════════
# self-service — change password
# ════════════════════════════════════════════════════════════════════════════════

def test_change_password_wrong_current_401(store, client):
    uid = _mk_operator(store, password="right-pw")
    csrf = _op_session(client, uid)
    r = client.post("/operator/change-password",
                    headers={"x-csrf-token": csrf},
                    json={"current_password": "WRONG", "new_password": "new-pw-1"})
    assert r.status_code == 401


def test_change_password_success_then_login_with_new(store, client):
    uid = _mk_operator(store, password="old-pw")
    csrf = _op_session(client, uid)
    r = client.post("/operator/change-password",
                    headers={"x-csrf-token": csrf},
                    json={"current_password": "old-pw", "new_password": "brand-new-pw"})
    assert r.status_code == 200
    # Old password no longer works; the new one authenticates.
    client.cookies.clear()
    bad = client.post("/operator/login", json={"username": "root", "password": "old-pw"})
    assert bad.status_code == 401
    ok = client.post("/operator/login", json={"username": "root", "password": "brand-new-pw"})
    assert ok.status_code == 200 and ok.json()["status"] == "authenticated"


def test_change_password_requires_csrf(store, client):
    uid = _mk_operator(store)
    _op_session(client, uid)  # cookie set, but no CSRF header
    r = client.post("/operator/change-password",
                    json={"current_password": "op-pw-123", "new_password": "x"})
    assert r.status_code == 403


# ════════════════════════════════════════════════════════════════════════════════
# self-service — MFA enroll → confirm → login gated
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(pyotp is None, reason="pyotp not installed")
def test_mfa_add_confirm_verify_roundtrip_and_login_gated(store, mfa_store, client):
    uid = _mk_operator(store, username="root", password="op-pw-123")
    csrf = _op_session(client, uid)

    # 1) add a TOTP factor → fresh secret + otpauth URI, pending (not yet enabled).
    r = client.post("/operator/mfa/factors/totp", headers={"x-csrf-token": csrf},
                    json={"label": "phone"})
    assert r.status_code == 200
    fid, secret = r.json()["factor_id"], r.json()["secret"]
    assert r.json()["provisioning_uri"].startswith("otpauth://")
    assert mfa_core.principal_has_enabled_factor("dashboard", uid) is False  # pending

    # 2) a WRONG code does NOT confirm the factor.
    bad = client.post(f"/operator/mfa/factors/{fid}/totp/confirm",
                      headers={"x-csrf-token": csrf}, json={"code": "000000"})
    assert bad.status_code == 401
    assert mfa_core.principal_has_enabled_factor("dashboard", uid) is False

    # 3) the real code confirms → the factor is enabled → MFA is on.
    good = client.post(f"/operator/mfa/factors/{fid}/totp/confirm",
                       headers={"x-csrf-token": csrf}, json={"code": pyotp.TOTP(secret).now()})
    assert good.status_code == 200
    assert mfa_core.principal_has_enabled_factor("dashboard", uid) is True

    # 4) login is now MFA-gated: password alone → challenge (+ the factor list), no session yet.
    client.cookies.clear()
    r1 = client.post("/operator/login", json={"username": "root", "password": "op-pw-123"})
    assert r1.status_code == 200 and r1.json()["status"] == "mfa_required"
    assert any(f["factor_type"] == "totp" for f in r1.json().get("factors", []))
    mfa_token = r1.json()["mfa_token"]
    assert auth.OPERATOR_SESSION_COOKIE_NAME not in r1.cookies

    # 5) TOTP verify (against ANY enabled authenticator) completes the login.
    r2 = client.post("/operator/mfa/verify",
                     json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()})
    assert r2.status_code == 200 and r2.json()["status"] == "authenticated"


@pytest.mark.skipif(pyotp is None, reason="pyotp not installed")
def test_mfa_remove_factor_turns_mfa_off(store, mfa_store, client):
    uid = _mk_operator(store, username="root", password="op-pw-123")
    csrf = _op_session(client, uid)
    # add + confirm a factor, then remove it → MFA back off.
    r = client.post("/operator/mfa/factors/totp", headers={"x-csrf-token": csrf},
                    json={"label": "phone"})
    fid, secret = r.json()["factor_id"], r.json()["secret"]
    client.post(f"/operator/mfa/factors/{fid}/totp/confirm", headers={"x-csrf-token": csrf},
                json={"code": pyotp.TOTP(secret).now()})
    assert mfa_core.principal_has_enabled_factor("dashboard", uid) is True
    d = client.delete(f"/operator/mfa/factors/{fid}", headers={"x-csrf-token": csrf})
    assert d.status_code == 200
    assert mfa_core.principal_has_enabled_factor("dashboard", uid) is False


# ════════════════════════════════════════════════════════════════════════════════
# break-glass (operator KEY) — reset password / reset mfa / delete-last guard
# ════════════════════════════════════════════════════════════════════════════════

def test_breakglass_reset_password_lets_operator_login(store, client):
    uid = _mk_operator(store, password="forgotten")
    r = client.post(f"/admin/operator-users/{uid}/reset-password",
                    headers={"Authorization": f"Bearer {OP_KEY}"})
    assert r.status_code == 200
    temp = r.json()["temp_password"]
    assert temp
    ok = client.post("/operator/login", json={"username": "root", "password": temp})
    assert ok.status_code == 200 and ok.json()["status"] == "authenticated"


def test_breakglass_reset_mfa_clears_it(store, client):
    secret = "JBSWY3DPEHPK3PXP"
    uid = _mk_operator(store, mfa=True, secret=secret)
    r = client.post(f"/admin/operator-users/{uid}/reset-mfa",
                    headers={"Authorization": f"Bearer {OP_KEY}"})
    assert r.status_code == 200
    assert store[uid]["mfa_enabled"] is False and store[uid]["totp_secret"] is None
    # Login no longer demands MFA.
    ok = client.post("/operator/login", json={"username": "root", "password": "op-pw-123"})
    assert ok.status_code == 200 and ok.json()["status"] == "authenticated"


def test_cannot_delete_the_last_operator(store, client):
    uid = _mk_operator(store)
    r = client.delete(f"/admin/operator-users/{uid}",
                      headers={"Authorization": f"Bearer {OP_KEY}"})
    assert r.status_code == 409
    assert auth.count_operators() == 1  # still there


def test_can_delete_when_more_than_one_operator(store, client):
    u1 = _mk_operator(store, username="root")
    u2 = _mk_operator(store, username="second")
    r = client.delete(f"/admin/operator-users/{u1}",
                      headers={"Authorization": f"Bearer {OP_KEY}"})
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert auth.count_operators() == 1 and u2 in store


def test_list_operators_never_leaks_secrets(store, client):
    _mk_operator(store, mfa=True, secret="JBSWY3DPEHPK3PXP")
    r = client.get("/admin/operator-users",
                   headers={"Authorization": f"Bearer {OP_KEY}"})
    assert r.status_code == 200
    for row in r.json()["operator_users"]:
        assert "password_hash" not in row and "totp_secret" not in row


# ════════════════════════════════════════════════════════════════════════════════
# BOLA — a TENANT session can never satisfy /operator/* nor /admin/operator-users
# ════════════════════════════════════════════════════════════════════════════════

def test_tenant_session_cannot_change_operator_password(store, client):
    # A valid TENANT session dropped into the operator cookie slot → 401 (salt mismatch).
    client.cookies.set(auth.OPERATOR_SESSION_COOKIE_NAME,
                       auth.issue_session(dashboard_user_id=UTENANT, account_id=ACCT_A,
                                          username="alice"))
    csrf = auth.issue_csrf(dashboard_user_id=UTENANT)
    r = client.post("/operator/change-password", headers={"x-csrf-token": csrf},
                    json={"current_password": "x", "new_password": "y"})
    assert r.status_code == 401


def test_tenant_session_cannot_list_operator_users(store, client):
    _mk_operator(store)
    client.cookies.set(auth.OPERATOR_SESSION_COOKIE_NAME,
                       auth.issue_session(dashboard_user_id=UTENANT, account_id=ACCT_A,
                                          username="alice"))
    r = client.get("/admin/operator-users")  # no key, tenant session only
    assert r.status_code == 403


def test_no_credential_cannot_reach_operator_management(store, client):
    uid = _mk_operator(store)
    assert client.get("/admin/operator-users").status_code == 403
    assert client.post(f"/admin/operator-users/{uid}/reset-password").status_code == 403
    assert client.get("/operator/mfa/factors").status_code == 401
