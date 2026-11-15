"""TENANT-OAUTH-SIGNIN — Google/GitHub sign-in for the TENANT console (BUG-01).

Driven pins for the /tenant/oauth/* lane (saas/tenant_console.py + saas/oauth_login.py),
per BUGS/tenant-oauth-login-20261031/BUG-01-tenant-oauth-login.md:
  * STATE SCOPING — the signed state carries scope='tenant' ('me' on the individual door);
    each callback refuses a state whose scope is not its own door (crossdoor refusal,
    BOTH directions), on top of the cookie<->param binding.
  * BIND, FAIL-CLOSED — subject match first; verified email only as a LOUD fallback that
    binds; unknown email REFUSED with zero rows written (OAuth never creates a dashboard
    login); operator-realm rows refused.
  * MFA NEVER BYPASSED — mfa_enabled login -> #oauth=mfa:<challenge> and NO session
    anywhere between OAuth success and /tenant/mfa/verify; the callback itself NEVER sets
    a session cookie (the no-MFA path hands the page a one-time GRANT, exchanged at
    POST /tenant/oauth/session — the only session mint on this path).
  * Buttons render server-driven only (pinned in test_tenant_oauth_signin_buttons.py).

No network, no DB: every external seam is monkeypatched at the module the route calls.
Run: python3 -m pytest tests/test_tenant_oauth_signin.py -q
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import tenant_console as tc
from saas import oauth_login as ol

BASE = "https://app.example.test"


@pytest.fixture()
def client(monkeypatch):
    app = FastAPI()
    app.include_router(tc.router)
    monkeypatch.setattr(ol, "redirect_base", lambda: BASE)
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def google(monkeypatch):
    monkeypatch.setenv("SAAS_OAUTH_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("SAAS_OAUTH_GOOGLE_CLIENT_SECRET", "gsec")
    prov = ol.get_provider("google")
    assert prov is not None
    return prov


def _ident(email="owner@acme.test", subject="sub-123"):
    return {"email": email, "name": "Owner", "provider_account": subject}


def _user(email="owner@acme.test", mfa=False, account_id="a-1"):
    return {"id": "du-1", "account_id": account_id, "username": email,
            "password_hash": "x", "totp_secret": None, "mfa_enabled": mfa,
            "role": "admin", "status": "active", "display_name": "Owner"}


class _Store:
    """In-memory dashboard_users stand-in; counts binds so 'zero rows written' is assertable."""

    def __init__(self, *users):
        self.users = list(users)
        self.binds = []
        self.logs = []

    def wire(self, monkeypatch):
        store = self

        def by_username(email):
            return next((u for u in store.users
                         if u["username"] == (email or "").strip().lower()), None)

        def by_oauth(provider, subject):
            return next((u for u in store.users
                         if u.get("oauth_provider") == provider
                         and u.get("oauth_subject") == subject), None)

        def bind(user, provider, subject):
            store.binds.append((user["id"], provider, subject))
            user["oauth_provider"], user["oauth_subject"] = provider, subject
            return True

        monkeypatch.setattr(tc._auth, "get_user_by_username", by_username)
        monkeypatch.setattr(tc, "_dashboard_user_by_oauth_subject", by_oauth)
        monkeypatch.setattr(tc, "_bind_oauth_identity", bind)
        monkeypatch.setattr(tc, "_log", lambda m: store.logs.append(m))
        monkeypatch.setattr(tc.M, "require_mfa_decision", lambda realm, pid: False)
        monkeypatch.setattr(tc._ratelimit, "login_locked", lambda email: (False, 0))
        monkeypatch.setattr(tc._ratelimit, "record_login_success", lambda email: None)
        monkeypatch.setattr(tc._audit, "record", lambda *a, **k: None)
        monkeypatch.setattr(tc._auth, "touch_last_login", lambda pid: None)

    def bind_count(self, user_id="du-1"):
        return len([b for b in self.binds if b[0] == user_id])


def _happy_provider(monkeypatch, identity):
    monkeypatch.setattr(ol, "exchange_code", lambda p, **kw: "fake-access-token")
    monkeypatch.setattr(ol, "fetch_identity", lambda p, t: identity)


def _start(client, provider="google"):
    r = client.get("/tenant/oauth/" + provider + "/start")
    assert r.status_code == 303
    from urllib.parse import urlparse, parse_qs
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    cookie = [c for c in r.headers["set-cookie"].split(";")
              if c.strip().startswith("fl_oauth_state=")][0].split("=", 1)[1]
    cookie = cookie.strip('"')   # starlette quotes base64 padding; a browser echoes it back
    return state, cookie


def _callback(client, state, cookie, provider="google"):
    return client.get("/tenant/oauth/" + provider + "/callback",
                      params={"code": "auth-code", "state": state},
                      cookies={"fl_oauth_state": cookie})


# -- provider surface --------------------------------------------------------------

def test_providers_listing_is_server_driven(client, google, monkeypatch):
    monkeypatch.delenv("SAAS_OAUTH_GITHUB_CLIENT_ID", raising=False)
    r = client.get("/tenant/oauth/providers")
    assert r.status_code == 200
    d = r.json()
    assert d["providers"] == ["google"]          # the registry decides, never the page
    assert d["start_base"] == BASE               # origin-bound start door


def test_providers_hidden_when_nothing_configured(client, monkeypatch):
    monkeypatch.delenv("SAAS_OAUTH_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("SAAS_OAUTH_GITHUB_CLIENT_ID", raising=False)
    assert client.get("/tenant/oauth/providers").json()["providers"] == []


def test_start_carries_tenant_scope_and_scoped_cookie(client, google):
    r = client.get("/tenant/oauth/google/start")
    assert r.status_code == 303
    from urllib.parse import urlparse, parse_qs
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert ol.verify_state_token(state)["scope"] == "tenant"
    cookie_line = [c for c in r.headers.get("set-cookie", "").split("\n")
                   if "fl_oauth_state=" in c][0]
    assert "Path=/tenant/oauth" in cookie_line


# -- crossdoor state-scope refusal (BOTH directions) -------------------------------

def test_tenant_state_refused_at_individual_door(client, google, monkeypatch):
    import saas.user_console as uc
    app = FastAPI()
    app.include_router(uc.router)
    uc_client = TestClient(app, follow_redirects=False)
    monkeypatch.setattr(uc, "_require_saas", lambda: None)
    state, cookie = _start(client)                      # scope='tenant'
    r = uc_client.get("/me/oauth/google/callback",
                      params={"code": "auth-code", "state": state},
                      cookies={"fl_oauth_state": cookie})
    assert r.status_code == 303
    assert r.headers["location"].endswith("#oauth=error:state")


def test_individual_state_refused_at_tenant_door(client, google, monkeypatch):
    verifier, challenge = ol.make_verifier_and_challenge()
    state = ol.issue_state_token(provider="google", verifier=verifier,
                                 return_origin=BASE, scope="me")
    r = _callback(client, state, state)
    assert r.status_code == 303
    assert r.headers["location"].endswith("#oauth=error:state")


def test_state_binding_param_cookie_mismatch_refused(client, google):
    state, cookie = _start(client)
    r = client.get("/tenant/oauth/google/callback",
                   params={"code": "auth-code", "state": state},
                   cookies={"fl_oauth_state": cookie + "tampered"})
    assert r.status_code == 303
    assert r.headers["location"].endswith("#oauth=error:state")


# -- binding, fail-closed ----------------------------------------------------------

def test_unknown_email_refused_zero_rows_written(client, google, monkeypatch):
    _happy_provider(monkeypatch, _ident())
    store = _Store()          # EMPTY roster — nobody to bind
    store.wire(monkeypatch)
    r = _callback(client, *_start(client))
    assert r.status_code == 303
    assert r.headers["location"].endswith("#oauth=error:no_console_account")
    assert store.binds == []                      # ZERO rows written — never auto-create


def test_first_login_binds_by_verified_email(client, google, monkeypatch):
    _happy_provider(monkeypatch, _ident())
    store = _Store(_user())
    store.wire(monkeypatch)
    r = _callback(client, *_start(client))
    assert r.status_code == 303
    frag = r.headers["location"].split("#oauth=")[1]
    assert frag.startswith("session:")            # no-MFA: a grant, NEVER a session cookie
    assert "fl_dash_session" not in (r.headers.get("set-cookie") or "")
    assert store.bind_count() == 1


def test_email_fallback_binds_with_loud_log(client, google, monkeypatch):
    _happy_provider(monkeypatch, _ident())
    store = _Store(_user())
    store.wire(monkeypatch)
    _callback(client, *_start(client))
    assert any("EMAIL-FALLBACK" in m for m in store.logs)


def test_repeat_login_matches_subject_no_rebind(client, google, monkeypatch):
    _happy_provider(monkeypatch, _ident())
    u = _user()
    u["oauth_provider"], u["oauth_subject"] = "google", "sub-123"   # already bound
    store = _Store(u)
    store.wire(monkeypatch)

    class _Seen:
        fallback = False

    def by_username(email):
        _Seen.fallback = True                     # any email lookup means subject MISS
        return next((x for x in store.users
                     if x["username"] == (email or "").lower()), None)

    monkeypatch.setattr(tc._auth, "get_user_by_username", by_username)
    r = _callback(client, *_start(client))
    assert r.status_code == 303
    assert r.headers["location"].startswith(BASE + "/dashboard/tenant.html#oauth=session:")
    assert _Seen.fallback is False                # subject matched FIRST, email never tried
    assert store.bind_count() == 0                # idempotent — no second bind written


def test_operator_realm_login_refused(client, google, monkeypatch):
    _happy_provider(monkeypatch, _ident())
    store = _Store(_user(account_id=None))
    store.wire(monkeypatch)
    r = _callback(client, *_start(client))
    assert r.headers["location"].endswith("#oauth=error:operator_realm")
    assert "fl_dash_session" not in (r.headers.get("set-cookie") or "")


# -- MFA is NEVER bypassed ---------------------------------------------------------

def test_mfa_enabled_gets_challenge_and_no_session(client, google, monkeypatch):
    _happy_provider(monkeypatch, _ident())
    store = _Store(_user(mfa=True))
    store.wire(monkeypatch)
    monkeypatch.setattr(tc.M, "require_mfa_decision", lambda realm, pid: True)
    monkeypatch.setattr(tc._auth, "issue_mfa_challenge",
                        lambda *, dashboard_user_id: "mfa-challenge-token")
    r = _callback(client, *_start(client))
    assert r.status_code == 303
    assert r.headers["location"].endswith("#oauth=mfa:mfa-challenge-token")
    # NO session exists between OAuth success and MFA verify — the callback sets NOTHING.
    setc = r.headers.get("set-cookie") or ""
    assert "fl_dash_session" not in setc
    assert "session:" not in r.headers["location"]


def test_mfa_state_unresolved_fails_closed(client, google, monkeypatch):
    _happy_provider(monkeypatch, _ident())
    store = _Store(_user())
    store.wire(monkeypatch)

    def _boom(realm, pid):
        raise tc.M.MfaError("x")

    monkeypatch.setattr(tc.M, "require_mfa_decision", _boom)
    r = _callback(client, *_start(client))
    assert r.headers["location"].endswith("#oauth=error:mfa_unavailable")
    assert "fl_dash_session" not in (r.headers.get("set-cookie") or "")


def test_rate_limited_login_refused(client, google, monkeypatch):
    _happy_provider(monkeypatch, _ident())
    store = _Store(_user())
    store.wire(monkeypatch)
    monkeypatch.setattr(tc._ratelimit, "login_locked", lambda email: (True, 42))
    r = _callback(client, *_start(client))
    assert r.headers["location"].endswith("#oauth=error:rate_limited")
    assert "fl_dash_session" not in (r.headers.get("set-cookie") or "")


# -- the grant exchange: the ONLY session mint on the OAuth path -------------------

def test_grant_exchanges_for_session(client, monkeypatch):
    u = _user()
    monkeypatch.setattr(tc._auth, "verify_oauth_session_grant",
                        lambda g: u["id"] if g == "g-1" else None)
    monkeypatch.setattr(tc._auth, "get_user_by_id",
                        lambda pid: u if pid == u["id"] else None)
    monkeypatch.setattr(tc._auth, "touch_last_login", lambda pid: None)
    monkeypatch.setattr(tc._auth, "issue_csrf", lambda *, dashboard_user_id: "csrf-1")
    monkeypatch.setattr(tc._audit, "record", lambda *a, **k: None)
    r = client.post("/tenant/oauth/session", json={"grant": "g-1"})
    assert r.status_code == 200
    assert r.json()["csrf_token"] == "csrf-1"
    assert "fl_dash_session" in (r.headers.get("set-cookie") or "")


def test_grant_exchange_rejects_bad_grant(client, monkeypatch):
    monkeypatch.setattr(tc._auth, "verify_oauth_session_grant", lambda g: None)
    r = client.post("/tenant/oauth/session", json={"grant": "junk"})
    assert r.status_code == 401


def test_grant_exchange_refuses_operator_realm(client, monkeypatch):
    u = _user(account_id=None)
    monkeypatch.setattr(tc._auth, "verify_oauth_session_grant", lambda g: u["id"])
    monkeypatch.setattr(tc._auth, "get_user_by_id", lambda pid: u)
    r = client.post("/tenant/oauth/session", json={"grant": "g-1"})
    assert r.status_code == 403
    assert "fl_dash_session" not in (r.headers.get("set-cookie") or "")
