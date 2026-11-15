"""Agent-delegate MFA PARITY — every factor the console supports must be able to connect.

THE DEFECT UNDER TEST. ``POST /v1/auth/tenant-delegate`` mints the short-lived, account-scoped
token the agent appliance uses to list and bind seats, and it did so by reimplementing the tenant
login as ONE request (email + password + TOTP). A single request can only ever carry a factor that
fits one round trip, so that path is TOTP-only — and WebAuthn, being challenge/response, never
will. Measured in production: a tenant admin whose ONLY enabled factor is a passkey could not mint
a delegate at all, so the entire "Connect to FaultLine" flow was unreachable for them, while the
MFA-enrolment prompt was actively steering users toward exactly that factor.

THE FIX UNDER TEST. Stop duplicating authentication. The appliance opens a pop-up at the REAL
console login (which already handles authenticator codes, emailed codes and passkeys), and the
resulting authenticated SESSION is exchanged for the delegate. What this file pins:

  1. a WEBAUTHN-ONLY login can obtain a delegate through the new path — end to end, driving the
     console's own passkey login, not a shortcut;
  2. a TOTP login still works through BOTH paths (the headless one is not regressed);
  3. MFA required but NOT satisfied yields NO delegate, on either path;
  4. an unresolvable MFA state still fails CLOSED (503), never a password-only delegate;
  5. operator-realm rows are still refused (403) on both doors;
  6. the rate-limit / lockout on the credential path still applies;
  7. the exchange endpoint refuses an unauthenticated, CSRF-less or FOREIGN session, and a
     pairing can only ever be minted into the account of the human who approved it.

Fast + no DB: the pairing STORE is replaced with an in-memory double that mirrors the SQL state
machine (conditional transitions, single-use), exactly as the existing delegate tests replace
``get_user_by_username`` / ``verify_password``. The real ``saas/agent_pairing.py`` guards that need
no database are asserted directly at the bottom.

Run: python3 -m pytest tests/test_saas_agent_delegate_mfa.py -q   (tests/ is gitignored → git add -f)
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"

PASSKEY_FACTOR = [{"id": "f-pk", "factor_type": "webauthn", "label": "Optimus-FaultLine",
                   "email_masked": None, "is_default": True}]
TOTP_FACTOR = [{"id": "f-totp", "factor_type": "totp", "label": "Authenticator",
                "email_masked": None, "is_default": True}]
EMAIL_FACTOR = [{"id": "f-mail", "factor_type": "email", "label": "Email",
                 "email_masked": "c***@seedtenant.example", "is_default": True}]


# ════════════════════════════════════════════════════════════════════════════════
# In-memory double for the pairing store (mirrors migration 233's state machine)
# ════════════════════════════════════════════════════════════════════════════════

class _FakeStore:
    """Same contract as saas/agent_pairing, same guards, a dict instead of Postgres.

    The conditional transitions are reproduced faithfully, because they are the security
    property: pending→approved and approved→claimed each happen at most once, so a pairing is
    single-use and cannot be re-pointed at a second account."""

    def __init__(self):
        self.rows = {}
        self._n = 0

    def start(self, *, label=None, requester_ip=None):
        self._n += 1
        pid = f"pair-{self._n}"
        code = f"device-secret-{self._n}"
        self.rows[pid] = {"code": code, "label": label, "ip": requester_ip,
                          "status": "pending", "sub": None, "acct": None, "usr": None}
        return {"pairing_id": pid, "device_code": code, "expires_in": 300,
                "connect_path": f"/dashboard/tenant.html#agent-connect={pid}",
                "poll_interval": 2}

    def describe(self, pairing_id):
        r = self.rows.get(str(pairing_id))
        if not r:
            return None
        return {"pairing_id": str(pairing_id), "status": r["status"], "label": r["label"],
                "requester_ip": r["ip"], "created_at": None, "expires_at": None}

    def approve(self, pairing_id, *, dashboard_user_id, account_id, username):
        acct = "" if account_id is None else str(account_id).strip()
        sub = "" if dashboard_user_id is None else str(dashboard_user_id).strip()
        if not acct or acct.lower() in ("none", "null") or not sub:
            raise self.PairingError("refusing to approve a pairing with no account scope")
        r = self.rows.get(str(pairing_id))
        if not r or r["status"] != "pending":
            return False
        r.update(status="approved", sub=sub, acct=acct, usr=username)
        return True

    def claim(self, pairing_id, device_code):
        r = self.rows.get(str(pairing_id or ""))
        if not r or str(device_code or "") != r["code"]:
            return {"status": "unknown"}
        if r["status"] == "pending":
            return {"status": "pending"}
        if r["status"] != "approved":
            return {"status": "unknown"}
        r["status"] = "claimed"
        return {"status": "approved", "dashboard_user_id": r["sub"],
                "account_id": r["acct"], "username": r["usr"]}


@pytest.fixture
def env(monkeypatch):
    """SAAS on, a signing secret, and every DB-backed primitive replaced.

    ``state`` is the dial the tests turn: which login exists, whether MFA is required, which
    FACTORS it has, and whether the second factor verifies."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-for-delegate-parity")
    monkeypatch.setenv("SAAS_COOKIE_SECURE", "0")   # TestClient speaks http

    from saas import agent_pairing as pairing
    from saas import audit, dashboard_auth as auth, mfa, ratelimit
    from saas import tenant_console as tc

    users = {
        "passkey@seedtenant.example": {
            "id": USER_A, "account_id": ACCT_A, "username": "passkey@seedtenant.example",
            "password_hash": "HASH", "role": "admin", "status": "active",
        },
        "totp@example.test": {
            "id": USER_B, "account_id": ACCT_B, "username": "totp@example.test",
            "password_hash": "HASH", "role": "admin", "status": "active",
        },
        "operator@example.test": {
            "id": "33333333-3333-4333-8333-333333333333", "account_id": None,
            "username": "operator@example.test", "password_hash": "HASH",
            "role": "operator", "status": "active",
        },
    }
    state = {
        "users": users,
        "mfa_required": {},        # username → bool
        "factors": {},             # username → list
        "totp_ok": False,
        "webauthn_ok": True,
        "mfa_raises": False,
        "login_locked": False,
    }

    store = _FakeStore()
    store.PairingError = pairing.PairingError

    def _by_name(name):
        u = users.get((name or "").strip().lower())
        return dict(u) if u else None

    def _by_id(uid):
        for u in users.values():
            if u["id"] == uid:
                return dict(u)
        return None

    def _uname(uid):
        u = _by_id(uid)
        return u["username"] if u else ""

    def _require_mfa(realm, pid):
        if state["mfa_raises"]:
            raise mfa.MfaError("simulated control-plane fault")
        return bool(state["mfa_required"].get(_uname(pid), False))

    monkeypatch.setattr(auth, "get_user_by_username", _by_name)
    monkeypatch.setattr(auth, "get_user_by_id", _by_id)
    monkeypatch.setattr(auth, "verify_password", lambda h, pw: pw == "good-pw")
    monkeypatch.setattr(auth, "touch_last_login", lambda uid: None)
    monkeypatch.setattr(auth, "role_for", lambda uid: "admin")
    monkeypatch.setattr(auth, "can_view_billing", lambda uid: True)
    monkeypatch.setattr(auth, "is_account_owner", lambda a, u: True)
    monkeypatch.setattr(mfa, "require_mfa_decision", _require_mfa)
    monkeypatch.setattr(mfa, "verify_totp_any", lambda r, p, c: bool(state["totp_ok"]))
    monkeypatch.setattr(mfa, "enabled_factors_for_login",
                        lambda r, p: list(state["factors"].get(_uname(p), [])))
    monkeypatch.setattr(mfa, "resolve_webauthn_rp", lambda origin: ("rp.test", "https://rp.test"))
    monkeypatch.setattr(mfa, "webauthn_authentication_options",
                        lambda r, p, rp_id=None: {"ok": True, "options": "{}", "challenge": "ch"})
    monkeypatch.setattr(mfa, "verify_webauthn_assertion",
                        lambda r, p, **kw: bool(state["webauthn_ok"]))
    monkeypatch.setattr(audit, "record", lambda *a, **k: True)
    monkeypatch.setattr(ratelimit, "login_locked",
                        lambda ident: ((True, 30) if state["login_locked"] else (False, 0)))
    monkeypatch.setattr(ratelimit, "record_login_failure", lambda ident: None)
    monkeypatch.setattr(ratelimit, "record_login_success", lambda ident: None)
    monkeypatch.setattr(ratelimit, "mfa_verify_locked", lambda realm, pid: (False, 0))
    monkeypatch.setattr(ratelimit, "record_mfa_failure", lambda realm, pid: None)
    monkeypatch.setattr(ratelimit, "record_mfa_success", lambda realm, pid: None)
    monkeypatch.setattr(tc, "_seat_management_allowed", lambda acct: True, raising=False)

    # Both routers on ONE app so a single cookie jar carries the console session while the
    # appliance-facing /v1 doors are exercised against the same process — the real deployment.
    for fn in ("start", "describe", "approve", "claim"):
        monkeypatch.setattr(pairing, fn, getattr(store, fn))

    return state, store


def _app():
    from saas import agent_proxy
    from saas import tenant_console as tc
    app = FastAPI()
    app.include_router(agent_proxy.build_agent_proxy_router(
        require_auth=lambda *a, **k: "noop",
        resolve_user_id=lambda *a, **k: "u",
        account_for=lambda *a, **k: None,
        log=lambda *a, **k: None,
    ))
    app.include_router(tc.router)
    return app


def _console_login_passkey(c, email="passkey@seedtenant.example"):
    """Drive the console's REAL passkey login: password → mfa_required → webauthn options →
    webauthn verify → session cookie. Returns the CSRF token."""
    r = c.post("/tenant/login", json={"username": email, "password": "good-pw"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "mfa_required", body
    assert [f["factor_type"] for f in body["factors"]] == ["webauthn"]
    mfa_token = body["mfa_token"]
    o = c.post("/tenant/mfa/webauthn/options", json={"mfa_token": mfa_token},
               headers={"origin": "https://rp.test"})
    assert o.status_code == 200, o.text
    v = c.post("/tenant/mfa/webauthn/verify",
               json={"mfa_token": mfa_token, "webauthn_token": o.json()["webauthn_token"],
                     "credential": {"id": "cred"}},
               headers={"origin": "https://rp.test"})
    assert v.status_code == 200, v.text
    assert v.json()["status"] == "authenticated"
    return v.json()["csrf_token"]


def _console_login_totp(c, email="totp@example.test"):
    r = c.post("/tenant/login", json={"username": email, "password": "good-pw"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "mfa_required"
    v = c.post("/tenant/mfa/verify", json={"mfa_token": r.json()["mfa_token"], "code": "123456"})
    assert v.status_code == 200, v.text
    return v.json()["csrf_token"]


# ════════════════════════════════════════════════════════════════════════════════
# 1 — THE HEADLINE: a passkey-only login CAN obtain a delegate
# ════════════════════════════════════════════════════════════════════════════════

def test_webauthn_only_login_obtains_a_delegate_through_the_pairing_flow(env):
    """The exact production account shape: one enabled factor, factor_type='webauthn', no TOTP
    secret to supply. Before this change that login could not mint a delegate at all."""
    state, store = env
    state["mfa_required"]["passkey@seedtenant.example"] = True
    state["factors"]["passkey@seedtenant.example"] = PASSKEY_FACTOR
    state["totp_ok"] = False        # there is NO totp secret on this account, ever

    from saas import dashboard_auth as auth
    with TestClient(_app()) as c:
        # 1. the appliance registers a pairing (unauthenticated; grants nothing on its own)
        s = c.post("/v1/auth/agent-pairing/start", json={"label": "Optimus front-desk agent"})
        assert s.status_code == 200, s.text
        pid, device = s.json()["pairing_id"], s.json()["device_code"]
        assert s.json()["connect_path"].endswith(pid)

        # 2. nothing approved yet → 202, and NO token
        p = c.post("/v1/auth/agent-delegate/claim",
                   json={"pairing_id": pid, "device_code": device})
        assert p.status_code == 202 and "delegate_token" not in p.json()

        # 3. the human signs in IN THE POP-UP with their passkey, then approves
        csrf = _console_login_passkey(c)
        info = c.get(f"/tenant/agent-pairing/{pid}")
        assert info.status_code == 200
        assert info.json()["label"] == "Optimus front-desk agent"
        assert info.json()["status"] == "pending"
        a = c.post(f"/tenant/agent-pairing/{pid}/approve", json={},
                   headers={"x-csrf-token": csrf})
        assert a.status_code == 200, a.text

        # 4. the appliance collects the delegate over ITS OWN connection
        r = c.post("/v1/auth/agent-delegate/claim",
                   json={"pairing_id": pid, "device_code": device})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "agent_delegate"
    assert body["account_id"] == ACCT_A
    assert body["expires_in"] == auth.AGENT_DELEGATE_TTL_SECONDS
    payload = auth.verify_agent_delegate(body["delegate_token"])
    assert payload is not None
    assert payload["acct"] == ACCT_A and payload["sub"] == USER_A
    assert payload["role"] == "agent_delegate"


def test_webauthn_only_login_is_told_precisely_why_the_headless_path_cannot_work(env):
    """The old message ("a current totp code is required") was simply FALSE for this account and
    left the appliance nothing to try. It must now name the real factors and the other door."""
    state, _ = env
    state["mfa_required"]["passkey@seedtenant.example"] = True
    state["factors"]["passkey@seedtenant.example"] = PASSKEY_FACTOR
    with TestClient(_app()) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "passkey@seedtenant.example", "password": "good-pw"})
    assert r.status_code == 401
    d = r.json()["detail"]
    assert d["error"] == "mfa_factor_unsupported"
    assert d["enabled_factors"] == ["webauthn"]
    assert d["browser_flow"] == "/v1/auth/agent-pairing/start"
    assert "passkey" in d["message"]
    assert "totp code is required" not in d["message"]


def test_email_only_login_is_also_pointed_at_the_pop_up(env):
    """Email codes need a send step, so they never fit the single request either — same answer,
    no longer 'totp or nothing'."""
    state, _ = env
    state["mfa_required"]["passkey@seedtenant.example"] = True
    state["factors"]["passkey@seedtenant.example"] = EMAIL_FACTOR
    with TestClient(_app()) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "passkey@seedtenant.example", "password": "good-pw"})
    assert r.status_code == 401
    assert r.json()["detail"]["enabled_factors"] == ["email"]
    assert r.json()["detail"]["error"] == "mfa_factor_unsupported"


# ════════════════════════════════════════════════════════════════════════════════
# 2 — a TOTP login still works through BOTH paths
# ════════════════════════════════════════════════════════════════════════════════

def test_totp_login_still_works_through_the_headless_single_request_path(env):
    """Headless/API integrators with no browser must not have been traded away for the pop-up."""
    state, _ = env
    state["mfa_required"]["totp@example.test"] = True
    state["factors"]["totp@example.test"] = TOTP_FACTOR
    state["totp_ok"] = True

    from saas import dashboard_auth as auth
    with TestClient(_app()) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "totp@example.test", "password": "good-pw", "totp": "123456"})
    assert r.status_code == 200, r.text
    assert auth.verify_agent_delegate(r.json()["delegate_token"])["acct"] == ACCT_B


def test_totp_login_also_works_through_the_new_pairing_path(env):
    state, _ = env
    state["mfa_required"]["totp@example.test"] = True
    state["factors"]["totp@example.test"] = TOTP_FACTOR
    state["totp_ok"] = True

    from saas import dashboard_auth as auth
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={"label": "headless-too"})
        pid, device = s.json()["pairing_id"], s.json()["device_code"]
        csrf = _console_login_totp(c)
        a = c.post(f"/tenant/agent-pairing/{pid}/approve", json={},
                   headers={"x-csrf-token": csrf})
        assert a.status_code == 200, a.text
        r = c.post("/v1/auth/agent-delegate/claim",
                   json={"pairing_id": pid, "device_code": device})
    assert r.status_code == 200, r.text
    assert auth.verify_agent_delegate(r.json()["delegate_token"])["acct"] == ACCT_B


def test_no_mfa_login_still_works_on_both_paths(env):
    """A login with no second factor at all is unchanged on the headless path and works on the
    new one (the console issues a session directly)."""
    from saas import dashboard_auth as auth
    with TestClient(_app()) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "totp@example.test", "password": "good-pw"})
        assert r.status_code == 200, r.text

        s = c.post("/v1/auth/agent-pairing/start", json={})
        pid, device = s.json()["pairing_id"], s.json()["device_code"]
        lg = c.post("/tenant/login", json={"username": "totp@example.test",
                                           "password": "good-pw"})
        assert lg.json()["status"] == "authenticated"
        a = c.post(f"/tenant/agent-pairing/{pid}/approve", json={},
                   headers={"x-csrf-token": lg.json()["csrf_token"]})
        assert a.status_code == 200
        cl = c.post("/v1/auth/agent-delegate/claim",
                    json={"pairing_id": pid, "device_code": device})
    assert cl.status_code == 200
    assert auth.verify_agent_delegate(cl.json()["delegate_token"])["acct"] == ACCT_B


# ════════════════════════════════════════════════════════════════════════════════
# 3 — MFA required but NOT satisfied → NO delegate, on either path
# ════════════════════════════════════════════════════════════════════════════════

def test_wrong_totp_gets_no_delegate_and_stays_generic(env):
    state, _ = env
    state["mfa_required"]["totp@example.test"] = True
    state["factors"]["totp@example.test"] = TOTP_FACTOR
    state["totp_ok"] = False
    with TestClient(_app()) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "totp@example.test", "password": "good-pw", "totp": "000000"})
    assert r.status_code == 401
    # A wrong SECOND factor must not be distinguishable from a wrong password.
    assert r.json()["detail"] == "invalid credentials"
    assert "delegate_token" not in r.text


def test_failed_passkey_assertion_yields_no_session_and_therefore_no_delegate(env):
    """The pop-up path's MFA gate is the CONSOLE's. Fail the assertion and there is no session,
    so the approval 401s and the pairing can never be claimed."""
    state, _ = env
    state["mfa_required"]["passkey@seedtenant.example"] = True
    state["factors"]["passkey@seedtenant.example"] = PASSKEY_FACTOR
    state["webauthn_ok"] = False
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={})
        pid, device = s.json()["pairing_id"], s.json()["device_code"]
        lg = c.post("/tenant/login", json={"username": "passkey@seedtenant.example",
                                           "password": "good-pw"})
        assert lg.json()["status"] == "mfa_required"
        o = c.post("/tenant/mfa/webauthn/options", json={"mfa_token": lg.json()["mfa_token"]},
                   headers={"origin": "https://rp.test"})
        v = c.post("/tenant/mfa/webauthn/verify",
                   json={"mfa_token": lg.json()["mfa_token"],
                         "webauthn_token": o.json()["webauthn_token"],
                         "credential": {"id": "cred"}},
                   headers={"origin": "https://rp.test"})
        assert v.status_code == 401
        # No session → cannot approve, with or without a guessed CSRF token.
        a = c.post(f"/tenant/agent-pairing/{pid}/approve", json={},
                   headers={"x-csrf-token": "anything"})
        assert a.status_code == 401
        cl = c.post("/v1/auth/agent-delegate/claim",
                    json={"pairing_id": pid, "device_code": device})
    assert cl.status_code == 202          # still merely pending — nothing was ever approved
    assert "delegate_token" not in cl.text


def test_mfa_required_and_no_code_supplied_never_mints(env):
    state, _ = env
    state["mfa_required"]["totp@example.test"] = True
    state["factors"]["totp@example.test"] = TOTP_FACTOR
    state["totp_ok"] = True     # would verify — but nothing was supplied to verify
    with TestClient(_app()) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "totp@example.test", "password": "good-pw"})
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "mfa_code_required"
    assert "delegate_token" not in r.text


# ════════════════════════════════════════════════════════════════════════════════
# 4 — fail-CLOSED on an unresolvable MFA state (503, never a password-only delegate)
# ════════════════════════════════════════════════════════════════════════════════

def test_unresolvable_mfa_state_denies_on_the_headless_path(env):
    state, _ = env
    state["mfa_raises"] = True
    with TestClient(_app()) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "totp@example.test", "password": "good-pw"})
    assert r.status_code == 503
    assert "delegate_token" not in r.text


def test_unresolvable_mfa_state_denies_on_the_pop_up_path_too(env):
    """The pop-up inherits the console login's fail-closed gate — a DB blip cannot buy a
    password-only session and therefore cannot buy a delegate."""
    state, _ = env
    state["mfa_raises"] = True
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={})
        pid, device = s.json()["pairing_id"], s.json()["device_code"]
        lg = c.post("/tenant/login", json={"username": "totp@example.test",
                                           "password": "good-pw"})
        assert lg.status_code == 503
        a = c.post(f"/tenant/agent-pairing/{pid}/approve", json={},
                   headers={"x-csrf-token": "anything"})
        assert a.status_code == 401
        cl = c.post("/v1/auth/agent-delegate/claim",
                    json={"pairing_id": pid, "device_code": device})
    assert cl.status_code == 202 and "delegate_token" not in cl.text


# ════════════════════════════════════════════════════════════════════════════════
# 5 — the REALM gate: operator rows carry no account and are refused
# ════════════════════════════════════════════════════════════════════════════════

def test_operator_realm_refused_on_the_headless_path(env):
    with TestClient(_app()) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "operator@example.test", "password": "good-pw"})
    assert r.status_code == 403


def test_operator_realm_cannot_reach_the_pop_up_path_at_all(env):
    """tenant_login refuses the operator realm BEFORE issuing a session, so there is no session
    to exchange and no pairing to approve."""
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={})
        pid = s.json()["pairing_id"]
        lg = c.post("/tenant/login", json={"username": "operator@example.test",
                                           "password": "good-pw"})
        assert lg.status_code == 403
        a = c.post(f"/tenant/agent-pairing/{pid}/approve", json={},
                   headers={"x-csrf-token": "anything"})
    assert a.status_code == 401


def test_exchange_refuses_a_login_row_that_lost_its_account_scope(env, monkeypatch):
    """Belt-and-braces: even holding a valid session, a login row with no account_id is refused
    rather than minting a delegate scoped to the string "None"."""
    from saas import dashboard_auth as auth
    with TestClient(_app()) as c:
        csrf = _console_login_totp_nomfa(c)
        monkeypatch.setattr(auth, "get_user_by_id",
                            lambda uid: {"id": USER_B, "account_id": None,
                                         "username": "totp@example.test"})
        r = c.post("/tenant/agent-delegate", json={}, headers={"x-csrf-token": csrf})
    assert r.status_code == 403


def _console_login_totp_nomfa(c, email="totp@example.test"):
    r = c.post("/tenant/login", json={"username": email, "password": "good-pw"})
    assert r.status_code == 200 and r.json()["status"] == "authenticated"
    return r.json()["csrf_token"]


# ════════════════════════════════════════════════════════════════════════════════
# 6 — rate limiting / lockout still guards the credential path
# ════════════════════════════════════════════════════════════════════════════════

def test_lockout_still_applies_to_the_headless_delegate(env):
    state, _ = env
    state["login_locked"] = True
    with TestClient(_app()) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "totp@example.test", "password": "good-pw"})
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "30"


def test_lockout_still_applies_to_the_pop_up_path(env):
    """The pop-up's credential step IS /tenant/login, so it inherits the same lockout — and the
    MFA-verify throttle the headless path never had."""
    state, _ = env
    state["login_locked"] = True
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={})
        pid, device = s.json()["pairing_id"], s.json()["device_code"]
        lg = c.post("/tenant/login", json={"username": "totp@example.test",
                                           "password": "good-pw"})
        assert lg.status_code == 429
        cl = c.post("/v1/auth/agent-delegate/claim",
                    json={"pairing_id": pid, "device_code": device})
    assert cl.status_code == 202 and "delegate_token" not in cl.text


def test_bad_password_still_401s_on_both_doors(env):
    with TestClient(_app()) as c:
        r = c.post("/v1/auth/tenant-delegate",
                   json={"email": "totp@example.test", "password": "wrong"})
        assert r.status_code == 401
        lg = c.post("/tenant/login", json={"username": "totp@example.test", "password": "wrong"})
    assert lg.status_code == 401


# ════════════════════════════════════════════════════════════════════════════════
# 7 — the exchange endpoint: unauthenticated / CSRF-less / FOREIGN sessions
# ════════════════════════════════════════════════════════════════════════════════

def test_exchange_requires_a_session(env):
    with TestClient(_app()) as c:
        r = c.post("/tenant/agent-delegate", json={})
    assert r.status_code == 401
    assert "delegate_token" not in r.text


def test_exchange_requires_csrf(env):
    with TestClient(_app()) as c:
        _console_login_totp_nomfa(c)
        r = c.post("/tenant/agent-delegate", json={})          # cookie present, no CSRF header
    assert r.status_code == 403


def test_exchange_rejects_a_csrf_bound_to_another_login(env):
    from saas import dashboard_auth as auth
    with TestClient(_app()) as c:
        _console_login_totp_nomfa(c)
        foreign = auth.issue_csrf(dashboard_user_id=USER_A)   # a DIFFERENT login's token
        r = c.post("/tenant/agent-delegate", json={}, headers={"x-csrf-token": foreign})
    assert r.status_code == 403


def test_exchange_mints_only_for_the_sessions_own_account(env):
    """Identity + scope come from the SIGNED session, never the request. Two different logins get
    two different account scopes, and neither can ask for the other's."""
    state, _ = env
    state["mfa_required"]["passkey@seedtenant.example"] = True
    state["factors"]["passkey@seedtenant.example"] = PASSKEY_FACTOR
    from saas import dashboard_auth as auth
    with TestClient(_app()) as c:
        csrf = _console_login_passkey(c)
        # A body that TRIES to name another account changes nothing — it is not read.
        r = c.post("/tenant/agent-delegate", json={"account_id": ACCT_B, "sub": USER_B},
                   headers={"x-csrf-token": csrf})
    assert r.status_code == 200, r.text
    payload = auth.verify_agent_delegate(r.json()["delegate_token"])
    assert payload["acct"] == ACCT_A and payload["sub"] == USER_A
    assert r.json()["expires_in"] == auth.AGENT_DELEGATE_TTL_SECONDS


def test_pairing_is_minted_into_the_approvers_account_not_the_appliances_choice(env):
    """The appliance supplies a pairing id and a device code — never an identity. Whoever
    approves decides the scope."""
    from saas import dashboard_auth as auth
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={"label": "who am i"})
        pid, device = s.json()["pairing_id"], s.json()["device_code"]
        csrf = _console_login_totp_nomfa(c)                 # signs in as ACCT_B's admin
        c.post(f"/tenant/agent-pairing/{pid}/approve", json={"account_id": ACCT_A},
               headers={"x-csrf-token": csrf})
        r = c.post("/v1/auth/agent-delegate/claim",
                   json={"pairing_id": pid, "device_code": device})
    assert r.status_code == 200
    assert auth.verify_agent_delegate(r.json()["delegate_token"])["acct"] == ACCT_B


def test_claim_requires_the_device_code_the_browser_never_saw(env):
    """A page that observes the pairing id in the pop-up URL still cannot claim the token."""
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={})
        pid = s.json()["pairing_id"]
        csrf = _console_login_totp_nomfa(c)
        c.post(f"/tenant/agent-pairing/{pid}/approve", json={}, headers={"x-csrf-token": csrf})
        r = c.post("/v1/auth/agent-delegate/claim",
                   json={"pairing_id": pid, "device_code": "guessed"})
    assert r.status_code == 404
    assert "delegate_token" not in r.text


def test_a_pairing_is_single_use(env):
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={})
        pid, device = s.json()["pairing_id"], s.json()["device_code"]
        csrf = _console_login_totp_nomfa(c)
        c.post(f"/tenant/agent-pairing/{pid}/approve", json={}, headers={"x-csrf-token": csrf})
        first = c.post("/v1/auth/agent-delegate/claim",
                       json={"pairing_id": pid, "device_code": device})
        second = c.post("/v1/auth/agent-delegate/claim",
                        json={"pairing_id": pid, "device_code": device})
    assert first.status_code == 200
    assert second.status_code == 404


def test_approving_twice_cannot_repoint_a_pairing(env):
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={})
        pid = s.json()["pairing_id"]
        csrf = _console_login_totp_nomfa(c)
        first = c.post(f"/tenant/agent-pairing/{pid}/approve", json={},
                       headers={"x-csrf-token": csrf})
        second = c.post(f"/tenant/agent-pairing/{pid}/approve", json={},
                        headers={"x-csrf-token": csrf})
    assert first.status_code == 200
    assert second.status_code == 409


def test_pairing_info_needs_a_session(env):
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={})
        r = c.get(f"/tenant/agent-pairing/{s.json()['pairing_id']}")
    assert r.status_code == 401


def test_unknown_pairing_claim_is_indistinguishable_from_a_wrong_device_code(env):
    with TestClient(_app()) as c:
        a = c.post("/v1/auth/agent-delegate/claim",
                   json={"pairing_id": "pair-does-not-exist", "device_code": "x"})
        s = c.post("/v1/auth/agent-pairing/start", json={})
        b = c.post("/v1/auth/agent-delegate/claim",
                   json={"pairing_id": s.json()["pairing_id"], "device_code": "wrong"})
    assert a.status_code == b.status_code == 404
    assert a.json() == b.json()


# ════════════════════════════════════════════════════════════════════════════════
# fail-closed off the SaaS core + the real store's non-DB guards
# ════════════════════════════════════════════════════════════════════════════════

def test_every_new_door_404s_when_saas_is_off(env, monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    with TestClient(_app()) as c:
        assert c.post("/v1/auth/agent-pairing/start", json={}).status_code == 404
        assert c.post("/v1/auth/agent-delegate/claim",
                      json={"pairing_id": "p", "device_code": "d"}).status_code == 404
        assert c.post("/tenant/agent-delegate", json={}).status_code == 404


def test_routes_are_registered():
    from saas import agent_proxy
    from saas import tenant_console as tc
    r = agent_proxy.build_agent_proxy_router(
        require_auth=lambda *a, **k: "noop", resolve_user_id=lambda *a, **k: "u",
        account_for=lambda *a, **k: None, log=lambda *a, **k: None)
    v1 = {route.path for route in r.routes}
    assert {"/v1/auth/tenant-delegate", "/v1/auth/agent-pairing/start",
            "/v1/auth/agent-delegate/claim"}.issubset(v1)
    console = {route.path for route in tc.router.routes}
    assert {"/tenant/agent-delegate", "/tenant/agent-pairing/{pairing_id}",
            "/tenant/agent-pairing/{pairing_id}/approve"}.issubset(console)


def test_real_store_refuses_an_unscoped_approval(monkeypatch):
    """The one state that could mint an unscoped delegate is refused before it reaches SQL (and
    the migration's CHECK refuses it again)."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-for-delegate-parity")
    from saas import agent_pairing
    for bad in (None, "", "  ", "None", "null"):
        with pytest.raises(agent_pairing.PairingError):
            agent_pairing.approve("p", dashboard_user_id=USER_A, account_id=bad, username="x")
    with pytest.raises(agent_pairing.PairingError):
        agent_pairing.approve("p", dashboard_user_id="", account_id=ACCT_A, username="x")


def test_real_store_device_hash_is_domain_separated(monkeypatch):
    """A hash captured for one pairing must not validate another."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-for-delegate-parity")
    from saas import agent_pairing
    a = agent_pairing._device_hash("pair-1", "same-code")
    b = agent_pairing._device_hash("pair-2", "same-code")
    assert a and b and a != b
    assert agent_pairing._device_hash("pair-1", "same-code") == a


def test_real_store_claim_rejects_empty_input(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-for-delegate-parity")
    from saas import agent_pairing
    assert agent_pairing.claim("", "") == {"status": "unknown"}
    assert agent_pairing.claim("p", "") == {"status": "unknown"}


# ════════════════════════════════════════════════════════════════════════════════
# the POP-UP page itself — static assertions on the shipped console assets
# ════════════════════════════════════════════════════════════════════════════════

def _dash(name):
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    return (root / "saas" / "dashboard" / name).read_text(encoding="utf-8")


def _dash_code(name):
    """The shipped asset with whole-line comments removed — so a test that forbids an API can be
    written about the CODE without tripping over the comment explaining why it is forbidden."""
    import re
    return "\n".join(ln for ln in _dash(name).splitlines()
                     if not re.match(r"^\s*(//|\*|/\*)", ln))


def test_console_serves_the_approval_screen_and_reads_the_pairing_from_the_fragment():
    html, js = _dash("tenant.html"), _dash("tenant.js")
    assert 'id="view-agent-connect"' in html
    assert 'id="btn-ac-approve"' in html
    # The FRAGMENT, not the query string: a fragment is never sent to the server, so the pairing
    # id stays out of access logs and Referer headers.
    assert "agent-connect=" in js and "location.hash" in js
    assert "/tenant/agent-pairing/" in js


def test_the_pop_up_never_hands_anything_to_its_OPENER():
    """THE security property of this design, pinned so a later 'convenience' cannot undo it.

    Handing the delegate back through ``window.opener.postMessage`` would give a bearer token to
    whoever opened the pop-up — and any web page can open a pop-up. The token is instead fetched
    by the appliance over its own connection, so the console page must never touch opener,
    postMessage, or the delegate token at all."""
    js = _dash_code("tenant.js")
    assert "window.opener" not in js
    assert "postMessage" not in js
    assert "delegate_token" not in js
    # …and the approval response carries no token to leak in the first place.
    assert "/tenant/agent-delegate" not in js


def test_the_appliance_supplied_label_is_escaped_before_it_is_rendered():
    """The label is attacker-controllable text from an UNAUTHENTICATED endpoint, rendered on a
    page that is one click from approving account access. It must go through esc()."""
    js = _dash("tenant.js")
    assert "esc(info.label" in js
    assert "esc(info.requester_ip" in js


def test_delegate_realm_stays_isolated_from_the_session_realm(env):
    """A delegate obtained through the new path is still a DELEGATE — it cannot be replayed as a
    console session, and a session cannot be replayed as a delegate."""
    from saas import dashboard_auth as auth
    with TestClient(_app()) as c:
        s = c.post("/v1/auth/agent-pairing/start", json={})
        pid, device = s.json()["pairing_id"], s.json()["device_code"]
        csrf = _console_login_totp_nomfa(c)
        c.post(f"/tenant/agent-pairing/{pid}/approve", json={}, headers={"x-csrf-token": csrf})
        tok = c.post("/v1/auth/agent-delegate/claim",
                     json={"pairing_id": pid, "device_code": device}).json()["delegate_token"]
    assert auth.verify_session(tok) is None
    assert auth.verify_csrf(tok, dashboard_user_id=USER_B) is False
    assert auth.verify_agent_delegate(tok) is not None
