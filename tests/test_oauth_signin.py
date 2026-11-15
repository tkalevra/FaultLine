"""OAUTH-SIGNIN — standard provider sign-up/log-in gates (Google, GitHub doors).

Driven pins for the /me/oauth/* lane (saas/oauth_login.py + user_console routes):
  * state cookie<->param binding is the CSRF defence (mismatch/tamper/absent -> refused)
  * the LOG-IN door runs the password door's gates: rate limit, seat status, fail-closed
    MFA decision (MFA required -> challenge fragment, NEVER a session)
  * the SIGN-UP door runs the password door's anti-abuse chain (disposable / spam-filter
    / fail-closed ban / one-mailbox) and the SAME approval valve
  * verified email only; no open redirect (allowlisted return origins only)

No network, no DB: every external seam is monkeypatched at the module the route calls.
Run: python3 tools/fltest.py --bug OAUTH-SIGNIN --test tests/test_oauth_signin.py
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from saas import user_console as uc
from saas import oauth_login as ol

BASE = "https://app.example.test"


@pytest.fixture()
def client(monkeypatch):
    app = FastAPI()
    app.include_router(uc.router)
    monkeypatch.setattr(ol, "redirect_base", lambda: BASE)
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def google(monkeypatch):
    monkeypatch.setenv("SAAS_OAUTH_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("SAAS_OAUTH_GOOGLE_CLIENT_SECRET", "gsec")
    prov = ol.get_provider("google")
    assert prov is not None
    return prov


def _ident(email="pat@example.com", name="Pat Doe"):
    return {"email": email, "name": name, "provider_account": "sub-1"}


def _individual(email="pat@example.com", mfa=False):
    return {"id": "ind-1", "user_id": "u-1", "account_id": "a-1", "email": email,
            "mfa_enabled": mfa, "status": "active"}


def _happy_provider(monkeypatch, prov, identity):
    monkeypatch.setattr(ol, "exchange_code", lambda p, **kw: "fake-access-token")
    monkeypatch.setattr(ol, "fetch_identity", lambda p, t: identity)
    return prov


def _start(client, provider="google"):
    r = client.get(f"/me/oauth/{provider}/start")
    assert r.status_code == 303
    from urllib.parse import urlparse, parse_qs
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]  # proper %-decode
    cookie = r.headers["set-cookie"].split(";")
    value = [c for c in cookie if c.strip().startswith("fl_oauth_state=")][0].split("=", 1)[1]
    # starlette QUOTES cookie values whose base64 padding trips RFC 6265 special-chars;
    # a real browser echoes the quotes and starlette unquotes on parse — mirror that here.
    value = value.strip('"')
    return state, value, r.headers["set-cookie"]


# ── provider surface ──────────────────────────────────────────────────────────────

def test_providers_listing_reflects_configuration(client, google, monkeypatch):
    monkeypatch.delenv("SAAS_OAUTH_GITHUB_CLIENT_ID", raising=False)
    r = client.get("/me/oauth/providers")
    assert r.status_code == 200
    d = r.json()
    assert d["providers"] == ["google"]
    # the ORIGIN-BOUND fix: the payload must advertise where /start lives, so a page on
    # another origin (direct port vs public base) sends the user to the right door
    assert d["start_base"] == BASE


def test_start_for_unconfigured_provider_is_404(client, monkeypatch):
    monkeypatch.delenv("SAAS_OAUTH_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("SAAS_OAUTH_GITHUB_CLIENT_ID", raising=False)
    assert client.get("/me/oauth/google/start").status_code == 404


def test_start_redirects_with_pkce_and_state_cookie(client, google):
    state, value, setcookie = _start(client)
    assert value == state                                   # cookie == param
    assert "httponly" in setcookie.lower()
    assert "samesite=lax" in setcookie.lower()              # Lax: provider redirect is cross-site GET
    assert "path=/me/oauth" in setcookie.lower()            # scoped: no other route sees it
    loc = client.get("/me/oauth/google/start").headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "code_challenge_method=S256" in loc


# ── state binding (the CSRF defence) ──────────────────────────────────────────────

def _cb(client, state, cookie_value, code="authz-code"):
    # The TestClient jar ALSO captured the state cookie from /start and would merge a
    # duplicate; clear it so the EXPLICIT header below is the cookie the server sees
    # (lets the mismatch/tamper tests control it precisely).
    client.cookies.clear()
    return client.get(
        "/me/oauth/google/callback",
        params={"state": state, "code": code},
        headers={"Cookie": f"fl_oauth_state={cookie_value}"} if cookie_value else {},
    )


def test_callback_refuses_state_param_cookie_mismatch(client, google, monkeypatch):
    monkeypatch.setattr(ol, "exchange_code", lambda p, **kw: pytest.fail("must not exchange"))
    state, _, _ = _start(client)
    r = _cb(client, state, "different-value")
    assert r.status_code == 303 and r.headers["location"].endswith("#oauth=error:state")


def test_callback_refuses_tampered_state(client, google, monkeypatch):
    monkeypatch.setattr(ol, "exchange_code", lambda p, **kw: pytest.fail("must not exchange"))
    state, _, _ = _start(client)
    r = _cb(client, state[:-4] + "AAAA", state[:-4] + "AAAA")   # signature-broken pair
    assert r.headers["location"].endswith("#oauth=error:state")


def test_callback_without_code_is_denied(client, google):
    state, value, _ = _start(client)
    r = _cb(client, state, value, code="")
    assert r.headers["location"].endswith("#oauth=error:denied")


# ── LOG-IN door ───────────────────────────────────────────────────────────────────

def test_login_door_issues_session_when_no_mfa(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident())
    monkeypatch.setattr(uc, "get_individual_by_email", lambda e: _individual())
    monkeypatch.setattr(uc._ratelimit, "login_locked", lambda e: (False, 0))
    monkeypatch.setattr(uc._ratelimit, "record_login_success", lambda e: None)
    monkeypatch.setattr(uc._ratelimit, "record_login_failure", lambda e: None)
    monkeypatch.setattr(uc, "_seat_gate_status", lambda ind: None)
    monkeypatch.setattr(uc.M, "require_mfa_decision", lambda realm, iid: False)
    monkeypatch.setattr(uc, "_touch_individual_login", lambda iid: None)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=ok")
    assert uc._auth.INDIVIDUAL_SESSION_COOKIE_NAME in r.headers["set-cookie"]


def test_login_door_mfa_required_redirects_to_challenge_not_session(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident())
    monkeypatch.setattr(uc, "get_individual_by_email", lambda e: _individual(mfa=True))
    monkeypatch.setattr(uc._ratelimit, "login_locked", lambda e: (False, 0))
    monkeypatch.setattr(uc._ratelimit, "record_login_success", lambda e: None)
    monkeypatch.setattr(uc, "_seat_gate_status", lambda ind: None)
    monkeypatch.setattr(uc.M, "require_mfa_decision", lambda realm, iid: True)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    loc = r.headers["location"]
    assert "#oauth=mfa:" in loc
    challenge = loc.split("#oauth=mfa:")[1]
    assert uc._auth.verify_individual_mfa_challenge(challenge) == "ind-1"
    assert uc._auth.INDIVIDUAL_SESSION_COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_login_door_mfa_state_unreadable_fails_closed(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident())
    monkeypatch.setattr(uc, "get_individual_by_email", lambda e: _individual())
    monkeypatch.setattr(uc._ratelimit, "login_locked", lambda e: (False, 0))
    monkeypatch.setattr(uc._ratelimit, "record_login_success", lambda e: None)
    def _boom(realm, iid):
        raise uc.M.MfaError("unreadable")
    monkeypatch.setattr(uc.M, "require_mfa_decision", _boom)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=error:mfa_unavailable")


def test_login_door_blocked_seat_is_refused(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident())
    monkeypatch.setattr(uc, "get_individual_by_email", lambda e: _individual())
    monkeypatch.setattr(uc._ratelimit, "login_locked", lambda e: (False, 0))
    monkeypatch.setattr(uc._ratelimit, "record_login_failure", lambda e: None)
    monkeypatch.setattr(uc, "_seat_gate_status", lambda ind: "revoked")
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=error:disabled")


# ── SIGN-UP door ──────────────────────────────────────────────────────────────────

def _signup_surface(monkeypatch, *, approval=False, activation=False, taken=False, banned=False,
                    disposable=False, spam=False, ladder_reject=False):
    # argon2 is not installed on the bare test box (it is in the container); the HASH is
    # not what these tests measure — the DOORS are.
    monkeypatch.setattr(uc._auth, "hash_password", lambda pw: "argon2-fake-hash")
    monkeypatch.setattr(uc, "get_individual_by_email", lambda e: None)
    monkeypatch.setattr(uc, "_email_taken", lambda e: taken)
    monkeypatch.setattr(uc._name_guard, "is_disposable_email", lambda e: disposable)
    monkeypatch.setattr(uc._name_guard, "check_display_name",
                        lambda dn: type("D", (), {"ok": True})())
    monkeypatch.setattr(uc._signup_filter, "evaluate",
                        lambda e: type("F", (), {"allowed": not spam})())
    monkeypatch.setattr(uc._moderation, "signup_ban_check",
                        lambda e: {"degraded": False, "banned": banned})
    monkeypatch.setattr(uc._signup_ledger, "decide",
                        lambda e: uc._signup_ledger.DECISION_REJECT if ladder_reject
                        else uc._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(uc._signup_ledger, "record_attempt",
                        lambda e, outcome, ip: None)
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval",
                        lambda: approval)
    monkeypatch.setattr(uc._platform_settings, "require_email_activation",
                        lambda: activation)
    monkeypatch.setattr(uc._accounts, "get_or_create_singles_account",
                        lambda: "singles-1")
    monkeypatch.setattr(uc, "_provision_individual_seat",
                        lambda acct, uid, seat_type=None: {"ok": True})
    monkeypatch.setattr(uc, "_create_individual",
                        lambda **kw: "new-ind-1")
    monkeypatch.setattr(uc, "_touch_individual_login", lambda iid: None)
    monkeypatch.setattr(uc, "send_welcome_to_individual", lambda t: {"delivery": "none"})


def test_signup_door_provisions_when_approval_off(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident(email="new@example.com"))
    _signup_surface(monkeypatch, approval=False)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=ok")
    assert uc._auth.INDIVIDUAL_SESSION_COOKIE_NAME in r.headers["set-cookie"]


def test_signup_door_holds_when_approval_on_no_session(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident(email="new@example.com"))
    _signup_surface(monkeypatch, approval=True)
    held = {}
    def _hold(**kw):
        held.update(kw)
    monkeypatch.setattr(uc, "_hold_signup_for_approval", _hold)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=pending")
    assert held.get("email") == "new@example.com"           # the SAME hold path as /me/signup
    assert uc._auth.INDIVIDUAL_SESSION_COOKIE_NAME not in r.headers.get("set-cookie", "")


# ── SIGN-UP door: email-activation valve (BUG-01 seam 2 — OAuth twin of /me/signup) ─

def test_signup_door_holds_pending_email_when_activation_on(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident(email="fresh@example.com", name="Fresh Name"))
    _signup_surface(monkeypatch, activation=True)
    held = {}
    provisioned, welcomed = [], []
    monkeypatch.setattr(uc, "_hold_signup_for_email_activation", lambda **kw: held.update(kw))
    monkeypatch.setattr(uc, "_provision_individual_seat",
                        lambda acct, uid, seat_type=None: provisioned.append((acct, uid)) or {"ok": True})
    monkeypatch.setattr(uc, "send_welcome_to_individual",
                        lambda t: welcomed.append(t) or {"delivery": "none"})
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    # BUG-08: the fragment carries the fresh valve's cooldown so the FIRST attempt's
    # resend button is honestly disabled too (a bare click would slam into the 429)
    assert r.headers["location"].endswith(
        "#oauth=pending_email~" + str(uc._email_activation.RESEND_INTERVAL_MINUTES * 60))
    assert uc._auth.INDIVIDUAL_SESSION_COOKIE_NAME not in r.headers.get("set-cookie", "")
    # the SAME hold path as the password door, with the SAME payload shape
    assert held.get("email") == "fresh@example.com"
    assert held.get("display_name") == "Fresh Name"
    assert held.get("password_hash") == "argon2-fake-hash"
    assert held.get("user_id") and held.get("account_id") == "singles-1"
    # and NOTHING past the hold: no provisioning, no welcome email
    assert provisioned == [] and welcomed == []


def test_signup_door_activation_gate_precedes_approval_valve(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident(email="both@example.com"))
    _signup_surface(monkeypatch, activation=True, approval=True)
    calls = {"activation": 0, "approval": 0}
    monkeypatch.setattr(uc, "_hold_signup_for_email_activation",
                        lambda **kw: calls.__setitem__("activation", calls["activation"] + 1))
    monkeypatch.setattr(uc, "_hold_signup_for_approval",
                        lambda **kw: calls.__setitem__("approval", calls["approval"] + 1))
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert "#oauth=pending_email" in r.headers["location"]          # NOT 'pending'
    assert calls == {"activation": 1, "approval": 0}                # email click comes first


def test_signup_door_activation_hold_identity_race_maps_to_fragment(client, google, monkeypatch):
    # the REAL helper converts the _IdentityTaken race to HTTPException(409) itself — pin
    # the helper's actual contract, not an exception it can never emit
    _happy_provider(monkeypatch, google, _ident(email="race@example.com"))
    _signup_surface(monkeypatch, activation=True)
    def _hold(**kw):
        raise HTTPException(status_code=409, detail="identity taken")
    monkeypatch.setattr(uc, "_hold_signup_for_email_activation", _hold)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    # an HTTPException must NEVER escape a GET navigation as a JSON error body
    assert r.status_code == 303
    assert r.headers["location"].endswith("#oauth=error:account_not_available")


def test_signup_door_activation_hold_persist_failure_maps_to_fragment(client, google, monkeypatch):
    from fastapi import HTTPException
    _happy_provider(monkeypatch, google, _ident(email="persist@example.com"))
    _signup_surface(monkeypatch, activation=True)
    def _hold(**kw):
        raise HTTPException(status_code=500, detail="could not persist the login")
    monkeypatch.setattr(uc, "_hold_signup_for_email_activation", _hold)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.status_code == 303
    assert r.headers["location"].endswith("#oauth=error:unavailable")


def test_signup_door_provisions_when_activation_off(client, google, monkeypatch):
    # explicit OFF (not just unpatched): the pre-BUG-01 behavior is byte-identical
    _happy_provider(monkeypatch, google, _ident(email="new@example.com"))
    _signup_surface(monkeypatch, activation=False)
    held = []
    monkeypatch.setattr(uc, "_hold_signup_for_email_activation", lambda **kw: held.append(kw))
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=ok")
    assert not held
    assert uc._auth.INDIVIDUAL_SESSION_COOKIE_NAME in r.headers["set-cookie"]


def test_login_door_ignores_activation_gate(client, google, monkeypatch):
    # the valve is a SIGN-UP door gate only: an existing active login signs straight in
    _happy_provider(monkeypatch, google, _ident())
    _signup_surface(monkeypatch, activation=True)
    monkeypatch.setattr(uc, "get_individual_by_email", lambda e: _individual())
    monkeypatch.setattr(uc._ratelimit, "login_locked", lambda e: (False, 0))
    monkeypatch.setattr(uc._ratelimit, "record_login_success", lambda e: None)
    monkeypatch.setattr(uc, "_seat_gate_status", lambda ind: None)
    monkeypatch.setattr(uc.M, "require_mfa_decision", lambda realm, iid: False)
    monkeypatch.setattr(uc, "_touch_individual_login", lambda iid: None)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=ok")


def test_signup_door_refuses_taken_mailbox_without_enumeration(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident(email="claimed@example.com"))
    _signup_surface(monkeypatch, taken=True)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=error:account_not_available")


def test_signup_door_refuses_banned_identity(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident(email="banned@example.com"))
    _signup_surface(monkeypatch, banned=True)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=error:email_rejected")


def test_signup_door_refuses_disposable_domain(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident(email="x@mailinator.com"))
    _signup_surface(monkeypatch, disposable=True)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=error:email_rejected")


def test_signup_door_three_strikes_reject(client, google, monkeypatch):
    _happy_provider(monkeypatch, google, _ident(email="again@example.com"))
    _signup_surface(monkeypatch, ladder_reject=True)
    state, value, _ = _start(client)
    r = _cb(client, state, value)
    assert r.headers["location"].endswith("#oauth=error:not_eligible")


# ── verified email + open redirect (unit level) ───────────────────────────────────

def test_google_userinfo_requires_verified_email(monkeypatch):
    class _R:
        def raise_for_status(self): pass
        def json(self): return {"email": "a@b.co", "email_verified": False}
    monkeypatch.setattr(ol.httpx, "get", lambda *a, **kw: _R())
    with pytest.raises(ol.OAuthFlowError) as ei:
        ol._google_userinfo(None, "tok")
    assert ei.value.code == "no_verified_email"


def test_github_userinfo_requires_primary_verified_email(monkeypatch):
    class _R:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p
    emails = [{"email": "x@y.co", "primary": True, "verified": False},
              {"email": "z@y.co", "primary": False, "verified": True}]
    monkeypatch.setattr(ol.httpx, "get",
                        lambda url, **kw: _R({"id": 7, "login": "pat"}
                                             if url.endswith("/user") else emails))
    with pytest.raises(ol.OAuthFlowError) as ei:
        ol._github_userinfo(None, "tok")
    assert ei.value.code == "no_verified_email"


def test_return_origin_allowlist_blocks_open_redirect(monkeypatch):
    monkeypatch.delenv("SAAS_TENANT_HOSTS", raising=False)
    monkeypatch.delenv("SAAS_ADMIN_HOSTS", raising=False)
    monkeypatch.setattr(ol, "redirect_base", lambda: "https://app.example.test")
    assert ol.allowed_return_origin("https://app.example.test/user.html") == \
        "https://app.example.test"
    assert ol.allowed_return_origin("https://evil.example.net/user.html") is None
    assert ol.allowed_return_origin("javascript:alert(1)") is None
    assert ol.allowed_return_origin("") is None


def test_return_origin_marketing_host_falls_back_to_base(monkeypatch):
    """LIVE-DEFECT PIN (2026-11-01): a MARKETING host can be operator-allowlisted
    (SAAS_TENANT_HOSTS names faultline.ca/www) while serving NO /dashboard/* — an OAuth
    round trip started from such a page bounced its callback back to a 404 wearing the
    outcome fragment (#oauth=error:disabled 404), three times in a row for the owner.
    A return origin that is not the redirect base can never render the outcome, so it is
    refused: the callback falls back to the base, which serves the dashboard."""
    monkeypatch.setenv("SAAS_TENANT_HOSTS", "marketing.example.test,app.example.test")
    monkeypatch.delenv("SAAS_ADMIN_HOSTS", raising=False)
    monkeypatch.setattr(ol, "redirect_base", lambda: "https://app.example.test")
    # allowlisted AND the base → honored
    assert ol.allowed_return_origin("https://app.example.test/user.html") == \
        "https://app.example.test"
    # allowlisted but NOT the base → refused (falls back to the base at the callback)
    assert ol.allowed_return_origin("https://marketing.example.test/user.html") is None
    # not allowlisted at all → refused
    assert ol.allowed_return_origin("https://evil.example.net/user.html") is None
