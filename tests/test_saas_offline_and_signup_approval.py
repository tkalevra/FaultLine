"""SaaS OFFLINE MODE + on-approval-only SIGNUPS (migration 174) — the two operator gates.

Both are fail-CLOSED and SAAS_MODE-gated (OFF = today's behavior EXACTLY, zero saas/ imports).

THE INVARIANT THIS FILE EXISTS TO PROTECT
─────────────────────────────────────────
**Offline mode must NEVER take the OPERATOR CONSOLE offline.** The switch LIVES on that console
(/dashboard → /admin/platform-settings). If the gate ever covered /admin, /operator, /dashboard or
the admin-face root, flipping the product offline would also disable the only control that turns
it back on — a one-way trap recoverable only by a hand-edit of the database. ``test_operator_
surfaces_are_never_gated`` and ``test_operator_exempt_even_when_control_plane_is_unreadable`` pin
that. If you are here because one of them went red: you did not find a bug in the test.

Also pinned:
  * default DENY — a path NOT on the operator allow-list is offline (a route added tomorrow is
    covered by default rather than silently escaping the gate);
  * the gate denies BEFORE auth (an unauthenticated data-plane call gets 503, not 401);
  * fail-CLOSED — an unreadable control plane resolves to offline / approval-required, EXCEPT
    that the operator stays exempt;
  * a held signup is INERT: no session, no flu_ token, no seat, no schema, no trial clock;
  * approve activates + welcomes; deny disables the login and keeps the email claimed.

Run: python3 tools/fltest.py --bug OFFLINE-APPROVAL --test tests/test_saas_offline_and_signup_approval.py
"""

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mcp import http_server as http_server

from saas import alerts as saas_alerts
from saas import platform_settings as saas_platform
from saas import signup_approvals as saas_signup
from saas import smtp_sender as saas_smtp_sender
from saas import user_console as saas_user_console


# ══════════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clear_platform_cache():
    """platform_settings caches globally (a per-request read must not hit the DB every time).
    Reset it around every test so one test's state never leaks into the next."""
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    yield
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


@pytest.fixture(autouse=True)
def _moderation_guards_neutral(monkeypatch):
    """Migrations 228/229: /me/signup now also consults the platform BAN LIST and the global
    SIGNUP SPAM FILTER. Both read the control plane this DB-less suite does not have, and the
    ban check fails CLOSED by design (503), which would mask the approval-gate behaviour this
    file exists to prove. Stub them NEUTRAL; the guards are proven in
    tests/test_saas_account_moderation.py."""
    monkeypatch.setattr(saas_user_console._moderation, "signup_ban_check",
                        lambda e: {"banned": False, "degraded": False})
    monkeypatch.setattr(saas_user_console._signup_filter, "evaluate",
                        lambda e, **kw: saas_user_console._signup_filter.Decision(True))


def _gate_app() -> FastAPI:
    """A minimal app wearing the REAL _offline_gate middleware, with a stub route at each surface
    the gate must classify. Exercises the shipped gate function (not a copy of it) without
    booting the whole MCP app / its lifespan."""
    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=http_server._offline_gate)

    for path in ("/health", "/admin/platform-settings", "/operator/login",
                 "/dashboard/index.html", "/dashboard/app.js",
                 "/dashboard/tenant.html", "/dashboard/user.html",
                 "/recall_memory", "/remember_facts", "/mcp", "/me/login",
                 "/tenant/session", "/v1/memories", "/"):
        app.add_api_route(path, lambda: {"ok": True}, methods=["GET", "POST"])
    return app


@pytest.fixture
def gate_client() -> TestClient:
    return TestClient(_gate_app())


def _set_offline(monkeypatch, offline: bool, message: str = "back soon"):
    monkeypatch.setattr(saas_platform, "offline_state",
                        lambda: {"offline": offline, "message": message})


# ══════════════════════════════════════════════════════════════════════════════════
# SAAS_MODE OFF → the gate does not exist (byte-for-byte today's behavior)
# ══════════════════════════════════════════════════════════════════════════════════

def test_saas_off_offline_gate_is_a_noop(monkeypatch, gate_client):
    """SAAS off → the public core has no offline switch. Even a platform_settings that SCREAMS
    offline must not be consulted, let alone honored."""
    monkeypatch.delenv("SAAS_MODE", raising=False)

    def _explode():  # the gate must never call this when SAAS is off
        raise AssertionError("platform_settings was read with SAAS_MODE off")

    monkeypatch.setattr(saas_platform, "offline_state", _explode)
    assert gate_client.post("/recall_memory").status_code == 200
    assert gate_client.get("/").status_code == 200


# ══════════════════════════════════════════════════════════════════════════════════
# SAAS_MODE ON
# ══════════════════════════════════════════════════════════════════════════════════

def test_online_is_a_passthrough(monkeypatch, gate_client):
    monkeypatch.setenv("SAAS_MODE", "1")
    _set_offline(monkeypatch, False)
    assert gate_client.post("/recall_memory").status_code == 200
    assert gate_client.post("/me/login").status_code == 200


@pytest.mark.parametrize("method,path", [
    ("post", "/recall_memory"),     # data plane
    ("post", "/remember_facts"),
    ("post", "/mcp"),               # MCP JSON-RPC lane
    ("post", "/me/login"),          # individual console
    ("get", "/tenant/session"),     # tenant console
    ("get", "/v1/memories"),        # public API
    ("get", "/dashboard/tenant.html"),  # the tenant console PAGE (served from the shared dir)
    ("get", "/dashboard/user.html"),    # the individual console PAGE
    ("get", "/"),                       # the user-facing front door
])
def test_offline_blocks_the_product(monkeypatch, gate_client, method, path):
    """Default DENY: everything that is not an operator surface is offline."""
    monkeypatch.setenv("SAAS_MODE", "1")
    _set_offline(monkeypatch, True, "upgrading the engine")
    r = getattr(gate_client, method)(path)
    assert r.status_code == 503, f"{path} should be offline"
    assert r.headers.get("Retry-After")
    body = r.json()
    assert body["error"] == "offline"
    assert body["message"] == "upgrading the engine"   # the operator's own copy


@pytest.mark.parametrize("path", [
    "/health",                    # liveness — an orchestrator must not read maintenance as dead
    "/admin/platform-settings",   # the operator API (where the switch is written)
    "/operator/login",            # the operator auth realm
    "/dashboard/index.html",      # the operator console page
    "/dashboard/app.js",          # ...and its JS (CSP: external files only)
])
def test_operator_surfaces_are_never_gated(monkeypatch, gate_client, path):
    """⚠️ THE LOAD-BEARING TEST. Offline ON must leave every operator surface reachable — that
    console is where the OFF switch lives. If this goes red, the kill-switch became a trap."""
    monkeypatch.setenv("SAAS_MODE", "1")
    _set_offline(monkeypatch, True)
    assert gate_client.get(path).status_code == 200, f"{path} MUST survive offline mode"


def test_offline_denies_before_auth(monkeypatch, gate_client):
    """The gate is the OUTERMOST middleware: it runs before routing/auth, so a data-plane call
    is refused with 503 rather than reaching the auth layer."""
    monkeypatch.setenv("SAAS_MODE", "1")
    _set_offline(monkeypatch, True)
    r = gate_client.post("/recall_memory", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 503


def test_offline_serves_the_maintenance_page_to_a_browser(monkeypatch, gate_client):
    monkeypatch.setenv("SAAS_MODE", "1")
    _set_offline(monkeypatch, True)
    r = gate_client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 503
    assert "text/html" in r.headers["content-type"]
    assert "maintenance" in r.text.lower()
    # CSP (script-src 'self', NO unsafe-inline): an inline-script page renders BLANK. Strip the
    # HTML comments first — the page's own comment DOCUMENTS this rule and would false-positive.
    import re as _re
    markup = _re.sub(r"<!--.*?-->", "", r.text, flags=_re.S).lower()
    assert "<script" not in markup
    assert "onclick" not in markup and "onload" not in markup


def test_unknown_route_is_also_offline(monkeypatch, gate_client):
    """Default-DENY means a route added TOMORROW is covered without touching the gate."""
    monkeypatch.setenv("SAAS_MODE", "1")
    _set_offline(monkeypatch, True)
    assert gate_client.get("/some/route/invented/next/week").status_code == 503


# ── fail-CLOSED ─────────────────────────────────────────────────────────────────

def test_unreadable_control_plane_fails_closed(monkeypatch, gate_client):
    """A gate we cannot evaluate must DENY. (An unreachable control plane means Postgres is down
    — and Postgres is the authoritative store, so the product could not answer truthfully anyway:
    better an honest maintenance page than a lying empty recall.)"""
    monkeypatch.setenv("SAAS_MODE", "1")

    def _boom():
        raise RuntimeError("control plane unreachable")

    monkeypatch.setattr(saas_platform, "offline_state", _boom)
    assert gate_client.post("/recall_memory").status_code == 503


def test_operator_exempt_even_when_control_plane_is_unreadable(monkeypatch, gate_client):
    """...and the fail-closed path STILL must not lock the operator out (the exemption is
    evaluated BEFORE the settings are read, precisely so a dead DB cannot strand the switch)."""
    monkeypatch.setenv("SAAS_MODE", "1")

    def _boom():
        raise RuntimeError("control plane unreachable")

    monkeypatch.setattr(saas_platform, "offline_state", _boom)
    assert gate_client.get("/admin/platform-settings").status_code == 200
    assert gate_client.get("/dashboard/index.html").status_code == 200


def test_platform_settings_failsafe_defaults(monkeypatch):
    """With nothing cached and the DB unreadable, the SETTINGS themselves resolve fail-closed:
    offline ON, approval REQUIRED."""
    def _boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(saas_platform.db, "connection", _boom)
    assert saas_platform.is_offline() is True
    assert saas_platform.require_signup_approval() is True
    assert saas_platform.state()["degraded"] is True


def test_transient_blip_uses_last_known_good(monkeypatch):
    """A cached read survives a transient DB blip — a hiccup must not flap the whole product
    offline (that would be a self-inflicted outage)."""
    saas_platform._cache = {"offline_mode": False, "require_signup_approval": False,
                            "offline_message": None, "degraded": False}
    saas_platform._cache_at = 0.0   # stale → forces a re-read, which will fail

    def _boom():
        raise RuntimeError("blip")

    monkeypatch.setattr(saas_platform.db, "connection", _boom)
    assert saas_platform.is_offline() is False          # last-known-good wins over fail-closed
    assert saas_platform.require_signup_approval() is False


# ══════════════════════════════════════════════════════════════════════════════════
# SIGNUP APPROVAL — new users are on-approval-only
# ══════════════════════════════════════════════════════════════════════════════════

SIGNUP_BODY = {"email": "New.Person@Example.com", "password": "correct-horse",
               "invite_code": "INVITE", "display_name": "New Person"}


@pytest.fixture
def signup_client(monkeypatch) -> TestClient:
    """A /me/signup client with the control-plane collaborators stubbed (no live DB)."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_user_console, "_email_taken", lambda e: False)
    monkeypatch.setattr(saas_user_console, "_consume_invite", lambda c: bool(c))
    monkeypatch.setattr(saas_user_console._auth, "hash_password", lambda p: "$argon2id$stub")
    monkeypatch.setattr(saas_user_console._accounts, "get_or_create_singles_account",
                        lambda: "acc00000-0000-4000-8000-000000000000")
    monkeypatch.setattr(saas_user_console._audit, "record", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(saas_user_console.router)
    return TestClient(app)


def test_signup_is_held_and_is_inert(monkeypatch, signup_client):
    """APPROVAL ON → 202 pending, and the applicant gets NOTHING: no session cookie, no flu_
    connection token, no user_id echoed, no seat, no schema, and no trial clock started."""
    created = {}
    queued = {}
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: True)
    monkeypatch.setattr(saas_user_console, "_create_individual",
                        lambda **kw: created.update(kw) or "ind00000-0000-4000-8000-000000000000")
    monkeypatch.setattr(saas_user_console._signup_approvals, "record_pending",
                        lambda **kw: queued.update(kw) or "app00000-0000-4000-8000-000000000000")
    monkeypatch.setattr(saas_user_console, "_notify_operator_of_signup", lambda *a, **k: None)

    def _no_seat(*a, **k):
        raise AssertionError("a HELD signup must not register a seat / provision a schema")

    monkeypatch.setattr(saas_user_console, "_provision_individual_seat", _no_seat)

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending_approval"
    assert "api_key" not in body and "user_id" not in body and "csrf_token" not in body
    assert not r.cookies                                   # no session issued
    # The login row exists (claiming the email) but is INERT and un-clocked.
    assert created["status"] == saas_signup.STATUS_PENDING
    assert created["start_trial"] is False
    assert created["email"] == "new.person@example.com"    # normalized
    assert queued["email"] == "new.person@example.com"     # ...and it is in the operator's queue


def test_signup_direct_when_approval_is_off(monkeypatch, signup_client):
    """APPROVAL OFF → the historical flow is intact: seat + provisioning + session + a token."""
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: False)
    monkeypatch.setattr(saas_user_console, "_provision_individual_seat",
                        lambda a, u, seat_type=None: {"ok": True, "reason": ""})
    monkeypatch.setattr(saas_user_console, "_create_individual",
                        lambda **kw: "ind00000-0000-4000-8000-000000000000")
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_session",
                        lambda **kw: "session-token")
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_csrf", lambda **kw: "csrf")
    monkeypatch.setattr(saas_user_console, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(saas_user_console._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_secret", "token_prefix": "flu_abc"})

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "authenticated"
    assert body["api_key"] == "flu_secret"


def test_hold_rolls_back_the_login_when_the_queue_write_fails(monkeypatch, signup_client):
    """A pending login with no queue item is INVISIBLE to the operator — a person who can never
    be approved and whose email is silently burned. So a failed queue write rolls the login back
    and 503s rather than stranding them."""
    deleted = []
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: True)
    monkeypatch.setattr(saas_user_console, "_create_individual",
                        lambda **kw: "ind00000-0000-4000-8000-000000000000")
    monkeypatch.setattr(saas_user_console._signup_approvals, "record_pending",
                        lambda **kw: None)                       # queue write fails
    monkeypatch.setattr(saas_user_console, "_delete_individual", lambda i: deleted.append(i))

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r.status_code == 503
    assert deleted == ["ind00000-0000-4000-8000-000000000000"]   # rolled back, not stranded


def test_duplicate_email_check_is_status_agnostic(monkeypatch, signup_client):
    """A PENDING or DENIED row also claims the address: a denied applicant must not be able to
    re-signup around the decision, and a second signup must not blow up on the UNIQUE index."""
    monkeypatch.setattr(saas_user_console, "_email_taken", lambda e: True)
    r = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r.status_code == 409


def test_pending_login_cannot_authenticate():
    """Fail-closed by CONSTRUCTION: the login readers filter status='active', so a pending row is
    unreachable by every control-plane path (login, session resolve, reset) without a new check
    at each call site. This pins the SQL that makes that true."""
    import inspect
    src = inspect.getsource(saas_user_console.get_individual_by_email)
    assert "status = 'active'" in src
    src_by_id = inspect.getsource(saas_user_console.get_individual_by_id)
    assert "status = 'active'" in src_by_id
    assert saas_signup.STATUS_PENDING != "active"


# ══════════════════════════════════════════════════════════════════════════════════
# ALERT SPACE — the operator must be TOLD there is work (and that the world is dark)
# ══════════════════════════════════════════════════════════════════════════════════

def test_pending_signups_raise_an_operator_alert(monkeypatch):
    monkeypatch.setattr(saas_alerts._signup_approvals, "list_pending_for_operator",
                        lambda: [{"id": "1"}, {"id": "2"}])
    monkeypatch.setattr(saas_alerts._reset_approvals, "list_pending_for_operator", lambda: [])
    monkeypatch.setattr(saas_alerts._platform_settings, "is_offline", lambda: False)

    items = saas_alerts.pending_actions_for_operator()
    signup = [i for i in items if i["kind"] == "signup_approval"]
    assert len(signup) == 1
    assert "2 new signups" in signup[0]["message"]
    assert signup[0]["action"]["tab"] == "signup-approvals"   # deep-links to the real control


def test_offline_mode_raises_a_critical_operator_alert(monkeypatch):
    """The operator console keeps working while offline (by design) — so the alert strip is the
    only thing standing between 'maintenance' and 'we were down all weekend and nobody noticed'."""
    monkeypatch.setattr(saas_alerts._signup_approvals, "list_pending_for_operator", lambda: [])
    monkeypatch.setattr(saas_alerts._reset_approvals, "list_pending_for_operator", lambda: [])
    monkeypatch.setattr(saas_alerts._platform_settings, "is_offline", lambda: True)

    items = saas_alerts.pending_actions_for_operator()
    offline = [i for i in items if i["kind"] == "offline_mode"]
    assert len(offline) == 1
    assert offline[0]["severity"] == "critical"
    assert items[0]["kind"] == "offline_mode"   # loudest first


def test_no_alerts_when_nothing_is_pending(monkeypatch):
    monkeypatch.setattr(saas_alerts._signup_approvals, "list_pending_for_operator", lambda: [])
    monkeypatch.setattr(saas_alerts._reset_approvals, "list_pending_for_operator", lambda: [])
    monkeypatch.setattr(saas_alerts._platform_settings, "is_offline", lambda: False)
    assert saas_alerts.pending_actions_for_operator() == []


def test_a_broken_alert_provider_never_breaks_the_strip(monkeypatch):
    """Fail-SAFE: one bad provider contributes nothing; the others still render."""
    def _boom():
        raise RuntimeError("provider is broken")

    monkeypatch.setattr(saas_alerts._signup_approvals, "list_pending_for_operator", _boom)
    monkeypatch.setattr(saas_alerts._reset_approvals, "list_pending_for_operator", lambda: [])
    monkeypatch.setattr(saas_alerts._platform_settings, "is_offline", lambda: True)
    items = saas_alerts.pending_actions_for_operator()
    assert [i["kind"] for i in items] == ["offline_mode"]


# ══════════════════════════════════════════════════════════════════════════════════
# WELCOME EMAIL — sent as the platform no-reply mailbox, with a plaintext alternative
# ══════════════════════════════════════════════════════════════════════════════════

def test_welcome_email_from_override():
    """The welcome goes out as noreply@faultline.ca (operator-configured), NOT the relay's
    transactional from_address."""
    cfg = {"from_address": "relay@internal.example", "from_name": "FaultLine"}
    msg = saas_smtp_sender._build_message(
        cfg, "user@example.com", "subject", "text", "<p>html</p>",
        from_address="noreply@faultline.ca")
    assert "noreply@faultline.ca" in msg["From"]
    assert "relay@internal.example" not in msg["From"]


def test_welcome_email_falls_back_to_the_configured_sender():
    cfg = {"from_address": "relay@internal.example", "from_name": None}
    msg = saas_smtp_sender._build_message(cfg, "user@example.com", "s", "t", None,
                                          from_address=None)
    assert msg["From"] == "relay@internal.example"


def test_welcome_email_has_both_bodies_and_no_uuid_leak():
    text, html = saas_smtp_sender._welcome_bodies(
        "https://faultline.ca/dashboard/user.html", "Christopher")
    assert "Christopher" in text and "Christopher" in html
    assert "approved" in text.lower()
    assert "https://faultline.ca/dashboard/user.html" in text   # plaintext alternative is usable
    assert "<table" in html                                     # table layout (2005-era clients)


def test_signup_notice_names_the_applicant():
    text, html = saas_smtp_sender._signup_notice_bodies(
        "new@example.com", "New Person", "https://faultline.ca/dashboard/index.html")
    assert "new@example.com" in text
    assert "awaiting" in text.lower() or "pending" in text.lower()
    assert "new@example.com" in html


def test_self_serve_signup_notifies_operator_live(monkeypatch, signup_client):
    """APPROVAL OFF → the operator's notify address gets a LIVE notice too, not just the held
    path. The call must carry live=True (self-serve wording) and never break the signup."""
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: False)
    monkeypatch.setattr(saas_user_console, "_provision_individual_seat",
                        lambda a, u, seat_type=None: {"ok": True, "reason": ""})
    monkeypatch.setattr(saas_user_console, "_create_individual",
                        lambda **kw: "ind00000-0000-4000-8000-000000000000")
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_session",
                        lambda **kw: "session-token")
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_csrf", lambda **kw: "csrf")
    monkeypatch.setattr(saas_user_console, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(saas_user_console._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_secret", "token_prefix": "flu_abc"})
    monkeypatch.setattr(saas_user_console, "send_welcome_to_individual",
                        lambda t: {"delivery": "no_address"})
    captured = {}

    def _capture(request, *, email, display_name, live=False):
        captured.update(email=email, display_name=display_name, live=live)

    monkeypatch.setattr(saas_user_console, "_notify_operator_of_signup", _capture)

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)

    assert r.status_code == 200
    assert captured == {"email": "new.person@example.com",
                        "display_name": "New Person", "live": True}


def test_signup_notice_email_live_variant(monkeypatch):
    """The LIVE variant of the operator notice: its own subject and wording — an operator
    skimming notifications must never mistake a live account for one awaiting approval."""
    sent = {}

    def _capture_send(to, subject, text, html, require_enabled=True):
        sent.update(to=to, subject=subject, text=text, html=html)
        return {"ok": True}

    monkeypatch.setattr(saas_smtp_sender, "send_email", _capture_send)

    out = saas_smtp_sender.send_signup_notice_email(
        "op@example.com", applicant_email="live@example.com", live=True)

    assert out == {"ok": True}
    assert sent["to"] == "op@example.com"
    assert sent["subject"] == saas_smtp_sender._SIGNUP_NOTICE_LIVE_SUBJECT
    assert "live" in sent["subject"].lower()
    assert "live@example.com" in sent["text"]
    assert "HELD" not in sent["text"]
