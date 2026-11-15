"""SIGNUP-FUNNEL POLISH (BUG-09 P1+P3) — styled activation pages + MFA-bypass CSRF fix.

Two defects, one lane:
  (P3) the email-activation pages ("activate your FaultLine account" confirm page + the
       activation outcome pages) rendered as BARE default HTML at the most delicate moment
       of the funnel. They now reuse the console's own stylesheet and login-card idiom.
  (P1) after activation, first login lands on the MFA-enrolment prompt; its SKIP button
       POSTs /me/mfa-prompt/defer, and a post-ACTIVATION session (cookie set by a
       script-free page, no body-carried token, nothing in sessionStorage) 403'd with
       "Your browser did not send a security token (CSRF)". The fix heals token DELIVERY
       in user.js::restoreSession via GET /me/csrf — the write-side check is untouched,
       so this opens NO CSRF hole (pinned below: tokenless POST still 403s).

Pinned here (the brief's gauntlet):
  (a) both activation pages render with the product styles — style hooks present, CSP-safe
      (no <script>, no inline event handlers), NO new static asset (reuses the already-served
      /dashboard/style.css, so the static-only Caddy root needs no sync);
  (b) MFA skip/bypass from a fresh post-activation session SUCCEEDS at the route level with
      the token threaded, and a POST WITHOUT the token is STILL REJECTED;
  (c) the real user.js heal, run in jsdom (tests/dashboard/render_activation_csrf_heal.js):
      restoreSession with an empty sessionStorage ends up holding a token, persists it, and
      the next unsafe POST carries x-csrf-token.

Run: python3 tools/fltest.py --bug SIGNUP-FUNNEL-POLISH --test tests/test_saas_signup_funnel_polish.py
"""

import json
import pathlib
import re
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import dashboard_auth as auth
from saas import mfa_prompt as _mfa_prompt
from saas import user_console as uc


HERE = pathlib.Path(__file__).resolve().parent
HARNESS = HERE / "dashboard" / "render_activation_csrf_heal.js"
USER_JS = HERE.parent / "saas" / "dashboard" / "user.js"

IND_ID = "aaa00000-0000-4000-8000-00000000000a"
USER_ID = "bbb00000-0000-4000-8000-00000000000b"
ACC_ID = "ccc00000-0000-4000-8000-00000000000c"
EMAIL = "funnel@example.com"


def _individual(**over):
    row = {
        "id": IND_ID, "user_id": USER_ID, "account_id": ACC_ID,
        "email": EMAIL, "password_hash": None, "totp_secret": None,
        "mfa_enabled": False, "display_name": "Funnel Person", "plan": "individual",
        "status": "active", "tenant_context": "none", "can_control_endpoint": True,
        "transfer_locked": False,
    }
    row.update(over)
    return row


@pytest.fixture
def funnel_client(monkeypatch) -> TestClient:
    """Router-level client: the auth/CSRF machinery stays REAL (signed sessions, signed
    double-submit tokens, require_user_session); only the Postgres control plane is stubbed."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "funnel-polish-secret-0123456789abcdef")
    app = FastAPI()
    app.include_router(uc.router)
    return TestClient(app)


def _login(funnel_client, monkeypatch, row=None) -> None:
    """Log the client in exactly the way a post-activation browser is: a signed session
    cookie and NO CSRF token anywhere (the activation page is script-free)."""
    row = row or _individual()
    monkeypatch.setattr(uc, "get_individual_by_id",
                        lambda i: row if str(i) == IND_ID else None)
    token = auth.issue_individual_session(individual_id=IND_ID, user_id=USER_ID, email=EMAIL)
    funnel_client.cookies.set(auth.INDIVIDUAL_SESSION_COOKIE_NAME, token)


# == (P3) STYLED ACTIVATION PAGES ==============================================

_STYLE_HOOKS = (
    '<link rel="stylesheet" href="/dashboard/style.css">',
    'class="login-wrap"',
    'class="section-label"',
    'class="panel"',
    'class="btn accent"',
)


def _assert_styled(html: str, *, button: bool = True) -> None:
    hooks = _STYLE_HOOKS if button else tuple(h for h in _STYLE_HOOKS if h != 'class="btn accent"')
    for hook in hooks:
        assert hook in html, "missing style hook " + repr(hook)
    # CSP-safe: the pages stay SCRIPT-FREE (the prefetch-safety argument for the GET door
    # depends on it), and no inline event handlers sneak in.
    assert "<script" not in html.lower()
    assert not re.search(r"\son\w+\s*=", html, re.I), "inline event handler on a CSP-safe page"


def test_confirm_page_is_styled_and_script_free(funnel_client):
    r = funnel_client.get("/me/verify-email", params={"token": "some-token"})
    assert r.status_code == 200
    _assert_styled(r.text)
    assert "activate my account" in r.text.lower()


def test_active_outcome_page_is_styled_and_sets_the_session(monkeypatch, funnel_client):
    """The happy outcome: full activation chain stubbed, form POST (browser=1) -> styled HTML
    carrying the one-time key, with the session cookie landing exactly as before."""
    monkeypatch.setattr(uc._ratelimit, "allow_request", lambda k: True)
    monkeypatch.setattr(uc._email_activation, "consume_activation", lambda t: IND_ID)
    monkeypatch.setattr(uc, "get_login_by_id_any_status",
                        lambda i: {"id": IND_ID, "user_id": USER_ID, "account_id": ACC_ID,
                                   "email": EMAIL, "status": uc.STATUS_PENDING_EMAIL,
                                   "display_name": "Funnel Person"})
    monkeypatch.setattr(uc, "_activate_pending_email_individual", lambda individual_id, decided_by=None: {
        "ok": True, "account_id": ACC_ID, "user_id": USER_ID, "email": EMAIL,
        "target_status": "active", "display_name": "Funnel Person"})
    monkeypatch.setattr(uc, "activate_approved_individual", lambda payload: None)
    monkeypatch.setattr(uc._auth, "issue_individual_session",
                        lambda **kw: "signed-session-stub")
    monkeypatch.setattr(uc._auth, "issue_individual_csrf", lambda **kw: "signed-csrf-stub")
    monkeypatch.setattr(uc, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(uc._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_onetime_key", "token_prefix": "flu_"})
    monkeypatch.setattr(uc, "send_welcome_to_individual", lambda i: {"ok": True})
    monkeypatch.setattr(uc, "_notify_operator_of_signup", lambda request, **kw: None)
    monkeypatch.setattr(uc, "_activation_origin_page",
                        lambda request, page: "https://console.example/dashboard/user.html")

    r = funnel_client.post("/me/verify-email",
                           data={"token": "some-token", "browser": "1"})
    assert r.status_code == 200
    _assert_styled(r.text)
    assert "flu_onetime_key" in r.text                       # the one-time key still shown
    assert auth.INDIVIDUAL_SESSION_COOKIE_NAME in r.cookies  # the session still lands
    assert "continue to your console" in r.text.lower()


def test_already_active_and_pending_approval_outcomes_are_styled(monkeypatch, funnel_client):
    monkeypatch.setattr(uc._ratelimit, "allow_request", lambda k: True)
    monkeypatch.setattr(uc, "_activation_origin_page", lambda request, page: "/dashboard/user.html")

    # already_active
    monkeypatch.setattr(uc._email_activation, "consume_activation", lambda t: IND_ID)
    monkeypatch.setattr(uc, "get_login_by_id_any_status",
                        lambda i: {"id": IND_ID, "user_id": USER_ID, "account_id": ACC_ID,
                                   "email": EMAIL, "status": "active", "display_name": None})
    r = funnel_client.post("/me/verify-email", data={"token": "t", "browser": "1"})
    assert r.status_code == 200
    _assert_styled(r.text)
    assert "already verified" in r.text.lower()

    # pending_approval (composed chain)
    monkeypatch.setattr(uc, "get_login_by_id_any_status",
                        lambda i: {"id": IND_ID, "user_id": USER_ID, "account_id": ACC_ID,
                                   "email": EMAIL, "status": uc.STATUS_PENDING_EMAIL,
                                   "display_name": None})
    monkeypatch.setattr(uc, "_activate_pending_email_individual", lambda individual_id, decided_by=None: {
        "ok": True, "account_id": ACC_ID, "user_id": USER_ID, "email": EMAIL,
        "target_status": uc._signup_approvals.STATUS_PENDING, "display_name": None})
    monkeypatch.setattr(uc, "_notify_operator_of_signup", lambda request, **kw: None)
    r = funnel_client.post("/me/verify-email", data={"token": "t", "browser": "1"})
    assert r.status_code == 200
    _assert_styled(r.text, button=False)   # no action to take: the operator owns the door
    assert "awaiting approval" in r.text.lower()


def test_no_new_static_asset_is_introduced():
    """The styling reuses the stylesheet the face ALREADY serves, so the static-only Caddy
    root for faultline.ca needs NO deploy-time sync for this lane. If this pin goes red, a
    NEW asset file crept in — flag it and update the deploy notes."""
    src = pathlib.Path(uc.__file__).read_text(encoding="utf-8")
    assert '_ACTIVATION_STYLESHEET = "/dashboard/style.css"' in src
    assert (HERE.parent / "saas" / "dashboard" / "style.css").is_file()


# == (P1) MFA SKIP: TOKEN THREADED, AND THE GATE STILL CLOSED ===================

def test_mfa_defer_without_csrf_is_still_403(monkeypatch, funnel_client):
    """THE SECURITY PIN: the fix must not open a CSRF hole. A POST to the MFA skip endpoint
    with a live session but NO token is rejected with the absent-token 403, verbatim the
    owner's reported error class."""
    _login(funnel_client, monkeypatch)
    r = funnel_client.post("/me/mfa-prompt/defer")
    assert r.status_code == 403
    assert "did not send a security token" in r.json()["detail"]


def test_mfa_defer_with_threaded_csrf_succeeds(monkeypatch, funnel_client):
    """The happy path the owner could not reach: fresh post-activation session + a properly
    threaded token (what GET /me/csrf mints and user.js now persists) -> the skip WORKS."""
    _login(funnel_client, monkeypatch)
    monkeypatch.setattr(_mfa_prompt, "defer", lambda realm, pid: {
        "ok": True, "status_code": 200, "counted": True,
        "state": {"required": False, "dismissible": True, "deferrals_used": 1,
                  "grace_logins": 10}})
    csrf = auth.issue_individual_csrf(individual_id=IND_ID)
    r = funnel_client.post("/me/mfa-prompt/defer", headers={"x-csrf-token": csrf})
    assert r.status_code == 200
    assert r.json()["status"] == "deferred"


def test_mfa_defer_with_a_foreign_csrf_is_still_403(monkeypatch, funnel_client):
    """A token minted for ANOTHER principal must not open this session's skip button."""
    _login(funnel_client, monkeypatch)
    csrf = auth.issue_individual_csrf(individual_id="ffffffff-ffff-4fff-8fff-ffffffffffff")
    r = funnel_client.post("/me/mfa-prompt/defer", headers={"x-csrf-token": csrf})
    assert r.status_code == 403


def test_csrf_reissue_route_needs_the_live_session(funnel_client):
    """GET /me/csrf is the delivery heal's server half: unauthenticated callers get 401,
    never a token (this is what keeps the heal from being a CSRF weakening)."""
    r = funnel_client.get("/me/csrf")
    assert r.status_code == 401


# == (P1) THE REAL CONSOLE HEAL, in jsdom =======================================

def _harness_ready() -> bool:
    return (HERE / "dashboard" / "node_modules").is_dir() and HARNESS.is_file()


def test_restore_session_heal_is_present_in_user_js():
    """Static pin (runs even without jsdom): the heal lives INSIDE restoreSession, before
    afterLogin — not in some OAuth-only branch the activation landing never takes."""
    src = USER_JS.read_text(encoding="utf-8")
    m = re.search(r"async function restoreSession\(\) \{.*?\n\}", src, re.S)
    assert m, "restoreSession not found in user.js"
    body = m.group(0)
    assert "/me/csrf" in body, "CSRF delivery heal missing from restoreSession"
    assert body.index("/me/csrf") < body.index("await afterLogin()")


@pytest.mark.skipif(not _harness_ready(), reason=(
    "jsdom harness deps not installed — run 'npm install --prefix tests/dashboard'; "
    "static assertions above still ran"))
def test_restore_session_heals_csrf_delivery_in_jsdom():
    res = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True,
                         timeout=120, cwd=str(HERE / "dashboard"))
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    # The session was live but sessionStorage was EMPTY (the activation page is script-free)
    # — restoreSession still ends up holding a freshly minted token, persists it, and the
    # MFA-skip POST carries the header instead of dead-ending on the 403.
    assert out["csrfFetched"] is True
    assert out["csrf"] == "healed-csrf-token-123"
    assert out["stored"] == "healed-csrf-token-123"
    assert out["deferHeader"] == "healed-csrf-token-123"
