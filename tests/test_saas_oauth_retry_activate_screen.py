"""SAAS-OAUTH-RETRY-ACTIVATE-SCREEN — BUG-08 (P2, owner-directed).

A repeat OAuth attempt on a held (pending_email) mailbox must land on the SAME
check-your-inbox screen as the first attempt — with the resend lever and its cooldown —
NOT the generic error:account_not_available card. Pinned here:

  * login-door retry on a pending_email individual + LIVE activation row →
    #oauth=pending_email~<cooldown>, no session, no fresh email while the link lives
  * retry with an EXPIRED/CONSUMED row and the valve OPEN → the resend path fires
    (fresh link minted+sent server-side), fragment carries the fresh interval
  * retry with a dead row but the valve CLOSED (cooldown>0) → no send, cooldown relayed
  * the TRUE identity-taken race (a live non-active, non-pending row claims the
    mailbox) stays error:account_not_available
  * NO mailbox-existence oracle without OAuth context: the pending branch sits AFTER
    the verified provider round trip — a tampered state never reaches it

No network, no DB: every external seam monkeypatched at the module the route calls.
Run: python3 tools/fltest.py --bug SAAS-OAUTH-RETRY-ACTIVATE-SCREEN --test tests/test_saas_oauth_retry_activate_screen.py
"""

import json
import pathlib
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import user_console as uc
from saas import oauth_login as ol

BASE = "https://app.example.test"
HERE = pathlib.Path(__file__).resolve().parent
DASH_HARNESS = HERE / "dashboard" / "render_activate_screen.js"
USER_JS = HERE.parent / "saas" / "dashboard" / "user.js"
USER_HTML = HERE.parent / "saas" / "dashboard" / "user.html"


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


def _ident(email="held@example.com", name="Held Person"):
    return {"email": email, "name": name, "provider_account": "sub-1"}


def _pending_login(email="held@example.com"):
    return {"id": "ind-pend", "user_id": "u-pend", "account_id": "a-pend",
            "email": email, "status": "pending_email", "display_name": "Held Person"}


def _retry_surface(monkeypatch, *, cooldown=1800, live=True, sends=None, lookups=None):
    """Patch every seam the pending-retry branch touches. sends/lookups are
    lists the branch effects are recorded into."""
    monkeypatch.setattr(ol, "exchange_code", lambda p, **kw: "fake-access-token")
    monkeypatch.setattr(ol, "fetch_identity", lambda p, t: _ident())
    # the ACTIVE-only login reader sees nothing for a pending_email row
    monkeypatch.setattr(uc, "get_individual_by_email", lambda e: None)
    monkeypatch.setattr(uc, "get_login_by_email_any_status",
                        lambda e: (lookups.append(e) or _pending_login(e))
                        if lookups is not None else _pending_login(e))
    monkeypatch.setattr(uc._email_activation, "resend_cooldown_seconds", lambda iid: cooldown)
    monkeypatch.setattr(uc._email_activation, "has_live_activation", lambda iid: live)
    monkeypatch.setattr(uc, "_send_activation_email",
                        lambda iid, email, request, display_name=None:
                        (sends.append((iid, email)) if sends is not None else None)
                        or {"sent": True})
    monkeypatch.setattr(uc, "_email_taken", lambda e: True)


def _start(client, provider="google"):
    r = client.get("/me/oauth/" + provider + "/start")
    assert r.status_code == 303
    from urllib.parse import urlparse, parse_qs
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    value = [c for c in r.headers["set-cookie"].split(";")
             if c.strip().startswith("fl_oauth_state=")][0].split("=", 1)[1].strip('"')
    return state, value


def _cb(client, state, cookie_value, code="authz-code"):
    client.cookies.clear()
    return client.get(
        "/me/oauth/google/callback",
        params={"state": state, "code": code},
        headers={"Cookie": "fl_oauth_state=" + cookie_value} if cookie_value else {},
    )


def _frag(r):
    return r.headers["location"].split("#oauth=")[1]


# ── the retry lands on the activate/resend screen ─────────────────────────────────

def test_retry_with_live_row_lands_on_pending_email_screen(client, google, monkeypatch):
    sends = []
    _retry_surface(monkeypatch, cooldown=1800, live=True, sends=sends)
    state, value = _start(client)
    r = _cb(client, state, value)
    assert r.status_code == 303
    frag = _frag(r)
    # the cooldown rides the fragment so the client button can disable itself
    assert frag.startswith("pending_email~")
    assert frag.split("~")[1].isdigit() and int(frag.split("~")[1]) == 1800
    assert "account_not_available" not in frag
    assert uc._auth.INDIVIDUAL_SESSION_COOKIE_NAME not in r.headers.get("set-cookie", "")
    # a LIVE link is still sitting in the inbox — no fresh email may be machine-gunned
    assert sends == []


def test_retry_with_live_row_and_open_valve_does_not_remail(client, google, monkeypatch):
    # cooldown elapsed but the inbox link is still clickable → still no fresh send
    sends = []
    _retry_surface(monkeypatch, cooldown=0, live=True, sends=sends)
    state, value = _start(client)
    r = _cb(client, state, value)
    assert _frag(r) == "pending_email~0"
    assert sends == []


def test_retry_with_dead_row_and_open_valve_re_arms_via_resend(client, google, monkeypatch):
    # every prior link expired/consumed AND the 1/hour valve is open → mint+send NOW
    sends = []
    side = {"cooldown": 0}
    _retry_surface(monkeypatch, cooldown=0, live=False, sends=sends)
    monkeypatch.setattr(uc._email_activation, "resend_cooldown_seconds",
                        lambda iid: side["cooldown"])
    def _send(iid, email, request, display_name=None):
        sends.append((iid, email))
        side["cooldown"] = 3600      # a fresh mint resets the valve
        return {"sent": True}
    monkeypatch.setattr(uc, "_send_activation_email", _send)
    state, value = _start(client)
    r = _cb(client, state, value)
    assert sends == [("ind-pend", "held@example.com")]
    assert _frag(r) == "pending_email~3600"


def test_retry_with_dead_row_and_closed_valve_relays_cooldown(client, google, monkeypatch):
    sends = []
    _retry_surface(monkeypatch, cooldown=2400, live=False, sends=sends)
    state, value = _start(client)
    r = _cb(client, state, value)
    assert sends == []                          # the valve refuses; nothing is sent
    assert _frag(r) == "pending_email~2400"     # and the screen says so honestly


# ── the TRUE race keeps the distinct error fragment ────────────────────────────────

def test_true_identity_taken_race_stays_account_not_available(client, google, monkeypatch):
    monkeypatch.setattr(ol, "exchange_code", lambda p, **kw: "fake-access-token")
    monkeypatch.setattr(ol, "fetch_identity", lambda p, t: _ident(email="denied@example.com"))
    monkeypatch.setattr(uc, "get_individual_by_email", lambda e: None)
    monkeypatch.setattr(uc, "get_login_by_email_any_status",
                        lambda e: {"id": "ind-denied", "user_id": "u", "account_id": "a",
                                   "email": e, "status": "denied", "display_name": None})
    monkeypatch.setattr(uc, "_email_taken", lambda e: True)
    state, value = _start(client)
    r = _cb(client, state, value)
    # ONLY the true race (a DIFFERENT live individual holds the mailbox) lands here
    assert _frag(r) == "error:account_not_available"


# ── no mailbox-existence oracle without the OAuth context ──────────────────────────

def test_pending_state_never_surfaces_without_verified_round_trip(client, google, monkeypatch):
    lookups, sends = [], []
    _retry_surface(monkeypatch, cooldown=1800, live=True, sends=sends, lookups=lookups)
    state, value = _start(client)
    r = _cb(client, state[:-4] + "AAAA", state[:-4] + "AAAA")   # tampered state pair
    assert _frag(r) == "error:state"
    # the pending lookup sits AFTER the verified provider round trip: a refused state
    # binding never consulted the mailbox — no oracle for an arbitrary visitor
    assert lookups == [] and sends == []


# ── client renders the screen with a working, honestly-disabled resend ─────────────

def _harness_ready() -> bool:
    return (HERE / "dashboard" / "node_modules").is_dir() and DASH_HARNESS.is_file()


@pytest.mark.skipif(not _harness_ready(), reason=(
    "jsdom harness deps not installed — run npm install --prefix tests/dashboard; "
    "static assertions below still ran"))
def test_client_renders_activate_screen_with_cooldown():
    res = subprocess.run(["node", str(DASH_HARNESS)], capture_output=True, text=True,
                         timeout=120, cwd=str(HERE / "dashboard"))
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)

    # cooldown fragment → the activate step is the visible card, button DISABLED with
    # countdown copy (not the generic error card, not a dead end)
    assert out["cooldown"]["step_visible"] is True
    assert out["cooldown"]["button_disabled"] is True
    assert "resend available in" in out["cooldown"]["button_text"]
    # a disabled click must not fire the resend POST
    assert out["cooldown"]["disabled_click_fetches"] == 0

    # bare fragment (first attempt) → same screen, button ENABLED
    assert out["bare"]["step_visible"] is True
    assert out["bare"]["button_disabled"] is False
    assert out["bare"]["button_text"] == "▸ resend activation link"

    # an enabled click POSTs the REAL resend lever shape to the REAL signup door
    assert out["resend_click"]["method"] == "POST"
    assert out["resend_click"]["path"] == "/me/signup"
    body = out["resend_click"]["body"]
    assert body["resend_activation"] is True
    assert body["email"] == "held@example.com"
    # the shared schema password is IGNORED on the resend path — placeholder only
    assert isinstance(body["password"], str) and len(body["password"]) >= 8

    # the error map race copy is the TRUE-race wording, not the old generic shrug
    assert "different account" in out["race_copy"]


# ── static pins (run even without jsdom) ──────────────────────────────────────────

def test_user_js_activate_screen_shape():
    src = USER_JS.read_text(encoding="utf-8")
    html = USER_HTML.read_text(encoding="utf-8")
    # the retry fragment grammar is parsed before the generic error fall-through
    assert "code.startsWith('pending_email~')" in src
    assert src.index("pending_email~") < src.index("const ec = code.startsWith('error:')")
    # the step is a first-class login-card step (hidden by the shared hider)
    assert "login-step-activate" in src.split("_hideAllLoginSteps")[1][:400]
    assert 'id="login-step-activate"' in html
    assert 'id="btn-activate-resend"' in html
    # the resend lever is the password door valve
    assert "resend_activation: true" in src
    # the cooldown honestly disables the button and the 429 re-arms it
    assert "setActivateCooldown" in src and "e.status === 429" in src
    # the true-race copy names the race
    assert "different account" in src


def test_server_fragment_carries_cooldown_only_from_retry():
    src = (HERE.parent / "saas" / "user_console.py").read_text(encoding="utf-8")
    # the retry branch emits the cooldown fragment…
    assert "pending_email~" in src
    # …and sits AFTER the verified identity resolution, BEFORE the signup door claim
    cb_at = src.index("def me_oauth_callback")
    assert src.index("identity = _oauth.fetch_identity") < src.index("PENDING-EMAIL RETRY")
    assert cb_at < src.index("PENDING-EMAIL RETRY") < src.index("_email_taken(email)", cb_at)

def test_first_attempt_fragment_recomputes_cooldown_not_constant(client, google, monkeypatch):
    # BUG-08 critic round 2: when the hold's MINT failed there is no activation row,
    # the valve is genuinely OPEN, and the first-attempt fragment must say ~0 so the
    # resend button stays usable — a hardcoded full interval would disable it for an
    # hour with no live link ever sent.
    monkeypatch.setattr(ol, "exchange_code", lambda p, **kw: "fake-access-token")
    monkeypatch.setattr(ol, "fetch_identity", lambda p, t: _ident(email="mintfail@example.com"))
    monkeypatch.setattr(uc, "get_individual_by_email", lambda e: None)
    monkeypatch.setattr(uc, "get_login_by_email_any_status",
                        lambda e: {"id": "ind-mf", "user_id": "u", "account_id": "a",
                                   "email": e, "status": "pending_email",
                                   "display_name": None})
    monkeypatch.setattr(uc._email_activation, "resend_cooldown_seconds", lambda iid: 0)
    monkeypatch.setattr(uc._email_activation, "has_live_activation", lambda iid: False)
    monkeypatch.setattr(uc, "_hold_signup_for_email_activation", lambda **kw: None)
    monkeypatch.setattr(uc._platform_settings, "require_email_activation", lambda: True)
    state, value = _start(client)
    r = _cb(client, state, value)
    assert _frag(r) == "pending_email~0"

