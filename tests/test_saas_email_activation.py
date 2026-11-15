"""EMAIL-ACTIVATION signup gate (migration 302) — "Require email click to activate".

The operator valve (platform_settings.require_email_activation, default OFF = today's behavior
byte-for-byte) holds a new /me/signup as status='pending_email' until the mailbox clicks a
single-use hashed verification link. THE INVARIANT THIS FILE EXISTS TO PROTECT: a login that has
not answered its email can NEVER authenticate — the active-only login readers already guarantee
that by construction, and these pins hold them to it while the click path flips the hold.

Pinned here (the brief's gauntlet):
  (a) signup with the valve ON → 200 pending_email, link sent, NOT active, NO seat/session/key;
  (b) the click flips the hold — 'active' (approval OFF) or 'pending_approval' (approval ON);
      the emailed GET is a PREFETCH-SAFE confirmation (consumes nothing); the form POST (or a
      programmatic JSON POST) consumes once and answers HTML (cookie + one-time key) or JSON;
  (c) expired/invalid/already-used token → 400, single vocabulary, never resurrects a hold;
  (d) approval+email both ON → the full chain email_click → pending_approval → admin approves
      → active (the valves COMPOSE, they never compete);
  (e) valve OFF → today's behavior byte-for-byte (no email machinery touched);
  (f) SMTP dead → LOUD log, the hold is STILL created, operator can activate it manually;
  (g) resend → fresh token, rate-limited 1/hour/mailbox, neutral for unknown addresses.

Run: python3 tools/fltest.py --test tests/test_saas_email_activation.py
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import admin as saas_admin
from saas import email_activation as saas_activation
from saas import platform_settings as saas_platform
from saas import signup_approvals as saas_signup
from saas import smtp_sender as saas_smtp_sender
from saas import user_console as saas_user_console


IND_ID = "ind00000-0000-4000-8000-000000000001"
USER_ID = "usr00000-0000-4000-8000-000000000001"
ACC_ID = "acc00000-0000-4000-8000-000000000001"
SIGNUP_BODY = {"email": "New.Person@Example.com", "password": "correct-horse",
               "display_name": "New Person"}


# ══════════════════════════════════════════════════════════════════════════════════
# Fixtures — DB-less harness over the REAL router (mirrors the approval-gate suite)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clear_platform_cache():
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    yield
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


@pytest.fixture(autouse=True)
def _moderation_guards_neutral(monkeypatch):
    monkeypatch.setattr(saas_user_console._moderation, "signup_ban_check",
                        lambda e: {"banned": False, "degraded": False})
    monkeypatch.setattr(saas_user_console._signup_filter, "evaluate",
                        lambda e, **kw: saas_user_console._signup_filter.Decision(True))


@pytest.fixture
def signup_client(monkeypatch) -> TestClient:
    """/me/signup client with the control-plane collaborators stubbed (no live DB)."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_user_console, "_email_taken", lambda e: False)
    monkeypatch.setattr(saas_user_console._auth, "hash_password", lambda p: "$argon2id$stub")
    monkeypatch.setattr(saas_user_console._accounts, "get_or_create_singles_account",
                        lambda: ACC_ID)
    monkeypatch.setattr(saas_user_console._audit, "record", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(saas_user_console.router)
    return TestClient(app)


@pytest.fixture
def activation_world(monkeypatch):
    """The activation token world, in-memory: mint maps token→individual, consume consumes.
    The link builder is pinned to a deterministic origin (the served-origin guard is unit-pinned
    separately) so the outbox carries a parseable /me/verify-email link."""
    world = {"tokens": {}, "mints": [], "consumed": [], "resend_ok": True, "links": [],
             "cooldown": 3600}

    def _mint(individual_id, ttl_hours=None):
        world["mints"].append(individual_id)
        token = f"act-token-{len(world['mints'])}"
        world["tokens"][token] = individual_id
        return token

    def _consume(token):
        world["consumed"].append(token)
        return world["tokens"].pop(token, None)

    def _resend_ok(individual_id, min_interval_minutes=None):
        return world["resend_ok"]

    monkeypatch.setattr(saas_user_console._email_activation, "mint_activation", _mint)
    monkeypatch.setattr(saas_user_console._email_activation, "consume_activation", _consume)
    monkeypatch.setattr(saas_user_console._email_activation, "resend_allowed", _resend_ok)
    monkeypatch.setattr(saas_user_console._email_activation, "resend_cooldown_seconds",
                        lambda iid, min_interval_minutes=None: world["cooldown"])
    monkeypatch.setattr(saas_user_console._smtp_config, "get_config_redacted",
                        lambda: {"enabled": True, "configured": True,
                                 "reset_link_base_url": None})
    monkeypatch.setattr(saas_user_console, "_activation_link_for",
                        lambda request, token, base_override=None:
                        f"https://console.example/me/verify-email?token={token}")
    monkeypatch.setattr(saas_smtp_sender, "send_activation_email",
                        lambda to, link, expiry_hours=48, display_name=None:
                        world["links"].append({"to": to, "link": link,
                                               "display_name": display_name}) or {"ok": True})
    return world


# ══════════════════════════════════════════════════════════════════════════════════
# (1) THE SETTING — default OFF, pre-migration NULL → OFF, blind control plane → OFF
# ══════════════════════════════════════════════════════════════════════════════════

def test_setting_defaults_off_and_failsafe_off(monkeypatch):
    """Backward compatibility IS the default: the valve is OFF shipped, OFF on a pre-migration
    row (appended-column NULL), and OFF on a blind control plane — an unreadable policy never
    makes the product stricter than its operator set it."""
    assert saas_platform._FAILSAFE["require_email_activation"] is False
    # Pre-migration row: the appended expression yields NULL → the documented default.
    row20 = [False, None, None, None, True, None, None, None, None, 30, 7, True, 10,
             False, False, 30, 35, 5, 6, None]          # 20 cols — no 302 column yet
    assert saas_platform._row_to_state(row20)["require_email_activation"] is False
    # Post-migration: real booleans arrive as TEXT from to_jsonb(ps.*) ->>.
    row21 = row20 + ["true"]
    assert saas_platform._row_to_state(row21)["require_email_activation"] is True
    assert saas_platform._row_to_state(row20 + ["false"])["require_email_activation"] is False
    assert saas_platform._row_to_state(row20 + [None])["require_email_activation"] is False


def test_setting_is_settable_and_served(monkeypatch):
    """The console can read AND write it: GET serves it from state, PUT maps the typed model
    through the fixed _SETTABLE allow-list."""
    assert "require_email_activation" in saas_platform._SETTABLE
    assert "require_email_activation" in saas_platform._APPENDED_COLS
    assert "require_email_activation" not in saas_platform._COLS   # 🔴 deploy rule: never in _COLS
    assert "require_email_activation" in saas_admin.PlatformSettingsRequest.model_fields
    assert saas_platform.PROTECTED_SETTINGS == () or "require_email_activation" not in saas_platform.PROTECTED_SETTINGS


# ══════════════════════════════════════════════════════════════════════════════════
# (a) VALVE ON → held pending_email, link sent, INERT
# ══════════════════════════════════════════════════════════════════════════════════

def test_signup_held_pending_email_and_link_sent(monkeypatch, signup_client, activation_world):
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: False)
    created = {}
    monkeypatch.setattr(saas_user_console, "_create_individual",
                        lambda **kw: created.update(kw) or IND_ID)
    provisioned = []
    monkeypatch.setattr(saas_user_console, "_provision_individual_seat",
                        lambda a, u, seat_type=None: provisioned.append((a, u)) or {"ok": True})

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending_email"
    assert "Check your email" in body["message"]
    assert "api_key" not in body and "user_id" not in body and "csrf_token" not in body
    assert not r.cookies                                   # NO session issued
    # The login row exists, claims the email, is INERT and un-clocked.
    assert created["status"] == "pending_email"
    assert created["start_trial"] is False
    assert created["email"] == "new.person@example.com"
    assert created["password_hash"] == "$argon2id$stub"
    # NO seat was registered (that happens at the click), and exactly one link went out,
    # addressed to the signup address, carrying the /me/verify-email door.
    assert provisioned == []
    assert len(activation_world["links"]) == 1
    link = activation_world["links"][0]
    assert link["to"] == "new.person@example.com"
    assert link["link"].startswith("/me/verify-email?token=") or "/me/verify-email?token=" in link["link"]


def test_pending_email_login_cannot_authenticate():
    """Fail-closed BY CONSTRUCTION: every login reader filters status='active', so a
    pending_email hold can never sign in, keep a session, or reset — no per-call-site check
    to forget. This pins the SQL that makes that true, plus the honeypot-equality of modes."""
    import inspect
    assert "status = 'active'" in inspect.getsource(saas_user_console.get_individual_by_email)
    assert "status = 'active'" in inspect.getsource(saas_user_console.get_individual_by_id)
    assert saas_user_console.STATUS_PENDING_EMAIL != "active"


def test_honeypot_trip_in_email_mode_matches_the_genuine_answer(monkeypatch, signup_client,
                                                                activation_world):
    """The trap must be indistinguishable from the genuine hold in THIS mode too: same 200,
    same builder, and — the point — NO link minted and NO login created for a bot."""
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    created = []
    monkeypatch.setattr(saas_user_console, "_create_individual",
                        lambda **kw: created.append(kw) or IND_ID)

    r = signup_client.post("/me/signup", json={**SIGNUP_BODY, "website": "https://bot.example"})

    assert r.status_code == 200
    assert r.json()["status"] == "pending_email"
    assert not r.cookies
    assert created == []                       # nothing written
    assert activation_world["mints"] == []     # no link minted
    assert activation_world["links"] == []     # no email sent


# ══════════════════════════════════════════════════════════════════════════════════
# (b)+(d) THE CLICK — active (approval OFF), pending_approval (approval ON)
# ══════════════════════════════════════════════════════════════════════════════════

def _fake_conn_factory(monkeypatch, user_console, row):
    """A minimal fake connection serving the guarded flip UPDATE; records commit/rollback."""
    calls = {"commit": 0, "rollback": 0, "queries": []}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=None): calls["queries"].append((" ".join(q.split()), p))
        def fetchone(self): return row

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def commit(self): calls["commit"] += 1
        def rollback(self): calls["rollback"] += 1

    monkeypatch.setattr(user_console.db, "connection", lambda: _Conn())
    return calls


def test_click_flips_to_active_when_approval_off(monkeypatch, signup_client, activation_world):
    """(b) approval OFF: the click IS the activation — 200 authenticated, session cookie, a
    one-time flu_ token, seat + trial provisioned, welcome AFTER activation."""
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: False)
    held = {}
    monkeypatch.setattr(saas_user_console, "_create_individual",
                        lambda **kw: held.update(kw) or IND_ID)
    monkeypatch.setattr(saas_user_console, "get_login_by_id_any_status",
                        lambda i: {"id": IND_ID, "user_id": USER_ID, "account_id": ACC_ID,
                                   "email": "new.person@example.com",
                                   "status": "pending_email", "display_name": "New Person"})
    calls = _fake_conn_factory(monkeypatch, saas_user_console,
                               (USER_ID, ACC_ID, "new.person@example.com", "New Person"))
    queued = {}
    monkeypatch.setattr(saas_user_console._signup_approvals, "record_pending",
                        lambda **kw: queued.update(kw) or "should-not-be-called")
    finished = []
    monkeypatch.setattr(saas_user_console, "activate_approved_individual",
                        lambda t: finished.append(t) or {"seat": "ok", "trial": "started"})
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_session",
                        lambda **kw: "session-token")
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_csrf", lambda **kw: "csrf")
    monkeypatch.setattr(saas_user_console, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(saas_user_console._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_secret", "token_prefix": "flu_abc"})
    monkeypatch.setattr(saas_user_console, "send_welcome_to_individual",
                        lambda t: finished.append({"welcome": t["email"]}) or {"delivery": "emailed"})
    monkeypatch.setattr(saas_user_console, "_notify_operator_of_signup", lambda *a, **k: None)
    # Sign up (valve ON) → take the link from the outbox.
    r0 = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r0.json()["status"] == "pending_email"
    token = activation_world["links"][0]["link"].split("token=")[1]

    r = signup_client.post("/me/verify-email", json={"token": token})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "authenticated"
    assert body["api_key"] == "flu_secret"
    assert body["csrf_token"] == "csrf"
    assert "set-cookie" in r.headers
    assert any(finished and f.get("welcome") == "new.person@example.com" for f in finished)
    assert finished[0]["user_id"] == USER_ID            # seat + trial ran BEFORE the welcome
    # The flip UPDATE guarded on the hold status.
    assert any("status = %s" in q and "pending_email" in str(p) for q, p in calls["queries"])
    assert queued == {}                                 # approval queue untouched
    # The token is SPENT: a second click of the same link can never re-fire.
    r2 = signup_client.post("/me/verify-email", json={"token": token})
    assert r2.status_code == 400


def test_full_chain_email_click_then_approval_then_active(monkeypatch, signup_client,
                                                          activation_world):
    """(d) BOTH valves ON → the chain composes: signup held pending_email → click flips to
    pending_approval + queues for the operator (NO session) → the operator's approve (the REAL
    signup_approvals machinery's contract) is what activates."""
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: True)
    monkeypatch.setattr(saas_user_console, "_create_individual", lambda **kw: IND_ID)
    monkeypatch.setattr(saas_user_console, "get_login_by_id_any_status",
                        lambda i: {"id": IND_ID, "user_id": USER_ID, "account_id": ACC_ID,
                                   "email": "new.person@example.com",
                                   "status": "pending_email", "display_name": "New Person"})
    calls = _fake_conn_factory(monkeypatch, saas_user_console,
                               (USER_ID, ACC_ID, "new.person@example.com", "New Person"))
    queued = {}
    monkeypatch.setattr(saas_user_console._signup_approvals, "record_pending",
                        lambda **kw: queued.update(kw) or "app00000-0000-4000-8000-000000000009")
    monkeypatch.setattr(saas_user_console, "_notify_operator_of_signup", lambda *a, **k: None)
    monkeypatch.setattr(saas_user_console, "_provision_individual_seat",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("approval chain must NOT provision at the click")))
    r0 = signup_client.post("/me/signup", json=SIGNUP_BODY)
    token = activation_world["links"][0]["link"].split("token=")[1]

    r = signup_client.post("/me/verify-email", json={"token": token})

    # The click does NOT sign them in — the operator still owns the door.
    assert r.status_code == 202
    assert r.json()["status"] == "pending_approval"
    assert not r.cookies
    assert queued["individual_id"] == IND_ID and queued["email"] == "new.person@example.com"
    # The flip targeted the approval hold, guarded on pending_email.
    assert any("pending_approval" in str(p) for q, p in calls["queries"])
    # And the operator's half of the chain is the EXISTING machinery: approve() flips
    # pending_approval → active in ONE guarded transaction (queue row + login together).
    approve_calls = {"queries": [], "rows": iter([
        (IND_ID, ACC_ID, "new.person@example.com", "New Person"),           # queue row
        (USER_ID, ACC_ID),                                                  # login flip
    ])}

    class _ACur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=None):
            approve_calls["queries"].append((" ".join(q.split()), p))
        def fetchone(self): return next(approve_calls["rows"], None)

    class _AConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _ACur()
        def commit(self): pass
        def rollback(self): pass

    monkeypatch.setattr(saas_signup.db, "connection", lambda: _AConn())
    target = saas_signup.approve("app0", decided_by="operator:test")
    assert target and target["user_id"] == USER_ID and target["email"] == "new.person@example.com"
    # The login flip is guarded on the approval hold: WHERE ... status = 'pending_approval'.
    assert any("individual_users" in q and "pending_approval" in str(p)
               for q, p in approve_calls["queries"])


def test_browser_click_door_confirms_then_serves_the_one_time_key(monkeypatch, signup_client,
                                                                  activation_world):
    """GET /me/verify-email is a CONFIRMATION, not a consumption: mail-scanner prefetch
    (SafeLinks et al.) fires GETs, so consuming the credential on the GET would let a fetcher
    activate the account and take the session + one-time key. The GET renders a script-free
    form and changes NOTHING; the form's POST is what consumes — and that POST answers with
    the same script-free HTML (session cookie + flu_ token shown once)."""
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: False)
    monkeypatch.setattr(saas_user_console, "_create_individual", lambda **kw: IND_ID)
    monkeypatch.setattr(saas_user_console, "get_login_by_id_any_status",
                        lambda i: {"id": IND_ID, "user_id": USER_ID, "account_id": ACC_ID,
                                   "email": "new.person@example.com",
                                   "status": "pending_email", "display_name": "New Person"})
    _fake_conn_factory(monkeypatch, saas_user_console,
                       (USER_ID, ACC_ID, "new.person@example.com", "New Person"))
    monkeypatch.setattr(saas_user_console, "activate_approved_individual",
                        lambda t: {"seat": "ok", "trial": "started"})
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_session",
                        lambda **kw: "session-token")
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_csrf", lambda **kw: "csrf")
    monkeypatch.setattr(saas_user_console, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(saas_user_console._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_secret", "token_prefix": "flu_abc"})
    monkeypatch.setattr(saas_user_console, "send_welcome_to_individual",
                        lambda t: {"delivery": "emailed"})
    monkeypatch.setattr(saas_user_console, "_notify_operator_of_signup", lambda *a, **k: None)
    signup_client.post("/me/signup", json=SIGNUP_BODY)
    token = activation_world["links"][0]["link"].split("token=")[1]

    # THE PREFETCH PIN: two scanner GETs consume NOTHING, mint NOTHING, set NO cookie —
    # and the human's POST still works afterward.
    for _ in range(2):
        g = signup_client.get("/me/verify-email", params={"token": token})
        assert g.status_code == 200
        assert "text/html" in g.headers["content-type"]
        assert "<form" in g.text and 'name="token"' in g.text
        assert "<script" not in g.text.lower()          # CSP-safe: no inline script
        assert "set-cookie" not in g.headers
    assert activation_world["consumed"] == []           # GET consumed NOTHING

    # THE FORM TRANSPORT PIN (round-2 critic): the confirmation form submits
    # application/x-www-form-urlencoded — the browser door MUST answer THAT, not only JSON.
    # A dict-annotated body 422s here while a JSON-posting suite stays green.
    r = signup_client.post("/me/verify-email", data={"token": token, "browser": "1"},
                           headers={"Content-Type": "application/x-www-form-urlencoded"})

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "flu_secret" in r.text                       # shown once, in the page body
    # BUG-09 (P3): the outcome page now carries the console styling (login-card idiom,
    # /dashboard/style.css) and stays script-free; copy is the button's lowercase label.
    assert "continue to your console" in r.text
    assert 'href="/dashboard/style.css"' in r.text and 'class="panel"' in r.text
    assert "<script" not in r.text.lower()
    assert "set-cookie" in r.headers


def test_verify_email_get_404s_without_saas_mode(monkeypatch):
    """The GET door is a /me surface: on a non-SaaS deployment it does not exist, like every
    other /me door."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    app = FastAPI()
    app.include_router(saas_user_console.router)
    client = TestClient(app)
    assert client.get("/me/verify-email", params={"token": "x"}).status_code == 404


def test_activation_link_never_uses_an_untrusted_host(monkeypatch):
    """The emailed link is a credential carrier: operator base else SERVED origin only — a
    spoofed Host must never anchor it (same discipline as the reset link, WSTG-INPV-17)."""
    captured = []
    monkeypatch.setattr(saas_user_console, "_log_crit", lambda m: captured.append(m))
    # (1) operator-configured base wins, reduced to its origin.
    link = saas_user_console._activation_link_for(
        None, "T0K", "https://faultline.ca/dashboard/user.html")
    assert link == "https://faultline.ca/me/verify-email?token=T0K"
    # (2) no base → the served origin (monkeypatched for determinism).
    monkeypatch.setattr(saas_user_console._acl, "served_origin",
                        lambda request: "https://served.example")
    assert saas_user_console._activation_link_for(object(), "T0K", "") == \
        "https://served.example/me/verify-email?token=T0K"
    # (3) neither → NO link is built and the refusal is LOUD.
    monkeypatch.setattr(saas_user_console._acl, "served_origin", lambda request: "")
    assert saas_user_console._activation_link_for(object(), "T0K", "") == ""
    assert captured and "NOT built" in captured[0]


# ══════════════════════════════════════════════════════════════════════════════════
# (c) BAD TOKENS — 400, single vocabulary
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("token", ["", "garbage", "act-token-spent", "../../etc/passwd"])
def test_bad_token_is_a_400(monkeypatch, signup_client, activation_world, token):
    monkeypatch.setenv("SAAS_MODE", "1")
    r = signup_client.post("/me/verify-email", json={"token": token})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid, expired, or already-used activation link"


def test_expired_token_cannot_claim(monkeypatch):
    """The DB contract: an expired row matches zero rows in the guarded UPDATE → None. Proven
    against the REAL consumer with a fake connection whose UPDATE claims nothing."""
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=None): self._q = q
        def fetchone(self): return None     # expired / used / unknown → no row
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def commit(self): pass
    monkeypatch.setattr(saas_activation.db, "connection", lambda: _Conn())
    assert saas_activation.consume_activation("expired-or-used") is None


def test_token_is_stored_hashed_never_plaintext(monkeypatch):
    """The 163 contract holds here too: only the sha256 HASH lands in the INSERT; the plaintext
    exists only in the returned link."""
    seen = {}
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=None): seen["q"] = " ".join(q.split()); seen["p"] = p
        def fetchone(self): return ("row",)
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def commit(self): pass
    monkeypatch.setattr(saas_activation.db, "connection", lambda: _Conn())
    token = saas_activation.mint_activation(IND_ID)
    assert token and token not in str(seen["p"])          # plaintext never stored
    import hashlib
    assert seen["p"][1] == hashlib.sha256(token.encode()).hexdigest()
    assert "individual_email_activations" in seen["q"]


def test_resend_interval_is_db_backed(monkeypatch):
    """The 1/hour valve reads the NEWEST row's created_at from the DB — no Redis dependency."""
    import datetime as _dt
    seen = {}
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=None): seen["q"] = q; seen["p"] = p
        def fetchone(self): return seen.get("row")
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def commit(self): pass
    monkeypatch.setattr(saas_activation.db, "connection", lambda: _Conn())
    seen["row"] = None                                    # first mint ever → allowed
    assert saas_activation.resend_allowed(IND_ID) is True
    seen["row"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5),)
    assert saas_activation.resend_allowed(IND_ID) is False
    seen["row"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=61),)
    assert saas_activation.resend_allowed(IND_ID) is True


# ══════════════════════════════════════════════════════════════════════════════════
# (e) VALVE OFF — today's behavior byte-for-byte
# ══════════════════════════════════════════════════════════════════════════════════

def test_valve_off_is_yesterdays_flow_exactly(monkeypatch, signup_client, activation_world):
    """OFF (the shipped default) must not even LOOK at the email-activation machinery: no mint,
    no link, no hold — the ungated flow returns 200 authenticated with a session + key."""
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: False)
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: False)
    monkeypatch.setattr(saas_user_console, "_provision_individual_seat",
                        lambda a, u, seat_type=None: {"ok": True, "reason": ""})
    monkeypatch.setattr(saas_user_console, "_create_individual",
                        lambda **kw: IND_ID)
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_session",
                        lambda **kw: "session-token")
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_csrf", lambda **kw: "csrf")
    monkeypatch.setattr(saas_user_console, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(saas_user_console._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_secret", "token_prefix": "flu_abc"})
    monkeypatch.setattr(saas_user_console, "send_welcome_to_individual",
                        lambda t: {"delivery": "no_address"})
    monkeypatch.setattr(saas_user_console, "_notify_operator_of_signup", lambda *a, **k: None)

    def _explode(*a, **k):
        raise AssertionError("valve OFF must not touch the activation machinery")

    monkeypatch.setattr(saas_user_console, "_hold_signup_for_email_activation", _explode)
    monkeypatch.setattr(saas_user_console, "_send_activation_email", _explode)
    monkeypatch.setattr(saas_user_console._email_activation, "mint_activation", _explode)

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "authenticated"
    assert body["api_key"] == "flu_secret"
    assert "set-cookie" in r.headers
    assert activation_world["links"] == []


# ══════════════════════════════════════════════════════════════════════════════════
# (f) SMTP DEAD — loud log, hold STILL created, operator can activate manually
# ══════════════════════════════════════════════════════════════════════════════════

def test_dead_smtp_never_blocks_the_signup(monkeypatch, signup_client, activation_world):
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    monkeypatch.setattr(saas_user_console, "_create_individual", lambda **kw: IND_ID)
    monkeypatch.setattr(saas_smtp_sender, "send_activation_email",
                        lambda *a, **k: {"ok": False, "error": "SMTP send failed (TimeoutError)"})
    crits = []
    monkeypatch.setattr(saas_user_console, "_log_crit", lambda m: crits.append(m))

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)

    # The signup SUCCEEDS into the hold — a dead relay must not 5xx the person.
    assert r.status_code == 200
    assert r.json()["status"] == "pending_email"
    assert any("NOT DELIVERED" in c and IND_ID in c for c in crits)   # LOUD, with the id


def test_operator_manual_activate_from_console(monkeypatch):
    """(f) the escape hatch: POST /admin/awaiting-email/{id}/activate flips the hold to ACTIVE
    (the operator's click IS the decision — the approval valve is bypassed), provisions the
    seat + trial, and attempts the welcome. Guarded on status='pending_email'."""
    monkeypatch.setenv("SAAS_MODE", "1")
    calls = _fake_conn_factory(
        monkeypatch, saas_user_console,
        (USER_ID, ACC_ID, "new.person@example.com", "New Person"))
    monkeypatch.setattr(saas_user_console._platform_settings, "require_signup_approval",
                        lambda: True)   # even with approval ON, the operator bypasses it here
    monkeypatch.setattr(saas_user_console._signup_approvals, "record_pending",
                        lambda **kw: (_ for _ in ()).throw(
                            AssertionError("manual activate must NOT queue an approval")))
    monkeypatch.setattr(saas_user_console, "activate_approved_individual",
                        lambda t: (_ for _ in ()).throw(
                            AssertionError("the CORE only flips; the route finishes the "
                                           "seat/provision/welcome (pinned by the route test)")))

    res = saas_user_console._activate_pending_email_individual(
        IND_ID, decided_by="operator:test", force_active=True)

    assert res["ok"] is True
    assert res["target_status"] == "active"
    # The guard is the status: a second call finds no pending_email row → 400.
    calls2 = _fake_conn_factory(monkeypatch, saas_user_console, None)
    res2 = saas_user_console._activate_pending_email_individual(
        IND_ID, decided_by="operator:test", force_active=True)
    assert res2["ok"] is False and res2["status_code"] == 400


def _operator_app() -> TestClient:
    """The REAL admin router with the operator gate overridden (the gate itself is proven
    elsewhere); we are pinning the wiring, not the auth."""
    app = FastAPI()
    app.include_router(saas_admin.router)
    app.dependency_overrides[saas_admin.require_operator] = lambda: True
    return TestClient(app)


def test_operator_activate_route_wiring(monkeypatch):
    """The operator route exists, is operator-gated, and finishes like an activation."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_user_console, "_activate_pending_email_individual",
                        lambda i, *, decided_by, force_active=False:
                        {"ok": True, "status_code": 200, "target_status": "active",
                         "individual_id": i, "user_id": USER_ID, "account_id": ACC_ID,
                         "email": "new.person@example.com", "display_name": "New Person"}
                        if force_active else {"ok": False})
    monkeypatch.setattr(saas_user_console, "activate_approved_individual",
                        lambda t: {"seat": "ok", "trial": "started"})
    monkeypatch.setattr(saas_admin, "_send_welcome", lambda t: {"delivery": "no_address"})
    monkeypatch.setattr(saas_admin, "_operator_principal", lambda r: "op-test")
    recorded = []
    monkeypatch.setattr(saas_admin._audit, "record", lambda *a, **k: recorded.append(a))

    r = _operator_app().post(f"/admin/awaiting-email/{IND_ID}/activate")
    assert r.status_code == 200
    assert r.json()["status"] == "activated"
    assert recorded and "awaiting_email_activated" in str(recorded[0])


def test_queue_read_carries_the_awaiting_email_list(monkeypatch):
    """GET /admin/signup-approvals serves both work lists: approvals AND awaiting-email."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_user_console, "list_awaiting_email_signups",
                        lambda: [{"individual_id": IND_ID, "email": "p@example.com",
                                  "status": "pending_email"}])
    monkeypatch.setattr(saas_signup, "list_pending_for_operator", lambda: [])
    r = _operator_app().get("/admin/signup-approvals")
    assert r.status_code == 200
    body = r.json()
    assert body["awaiting_email"] == [{"individual_id": IND_ID, "email": "p@example.com",
                                       "status": "pending_email"}]


# ══════════════════════════════════════════════════════════════════════════════════
# (g) RESEND — fresh token, rate-limited, anti-enumeration
# ══════════════════════════════════════════════════════════════════════════════════

def _world_with_pending_hold(monkeypatch):
    monkeypatch.setattr(saas_user_console, "get_login_by_email_any_status",
                        lambda e: {"id": IND_ID, "user_id": USER_ID, "account_id": ACC_ID,
                                   "email": e, "status": "pending_email",
                                   "display_name": "New Person"})


def test_resend_mints_and_sends_a_fresh_link(monkeypatch, signup_client, activation_world):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    _world_with_pending_hold(monkeypatch)
    r = signup_client.post("/me/signup", json={**SIGNUP_BODY, "resend_activation": True})
    assert r.status_code == 200
    assert r.json()["status"] == "pending_email"
    assert len(activation_world["mints"]) == 1             # a FRESH token
    assert len(activation_world["links"]) == 1
    # Nothing was provisioned and no login was created for a resend.
    assert activation_world["mints"] == [IND_ID]


def test_resend_is_rate_limited_to_one_per_hour(monkeypatch, signup_client, activation_world):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    _world_with_pending_hold(monkeypatch)
    activation_world["resend_ok"] = False                  # the newest link is <1h old
    activation_world["cooldown"] = 599                      # …and 599s of it remain
    r = signup_client.post("/me/signup", json={**SIGNUP_BODY, "resend_activation": True})
    assert r.status_code == 429
    # BUG-08: Retry-After reports the REMAINING cooldown (the activate screen's button
    # re-arms from it) — not a fresh full hour at minute 59
    assert r.headers.get("Retry-After") == "599"
    assert activation_world["mints"] == []                 # NO fresh token under the valve


def test_resend_is_neutral_for_unknown_or_active_addresses(monkeypatch, signup_client,
                                                           activation_world):
    """No pending hold → the SAME neutral 200, no mint, no enumeration oracle."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    monkeypatch.setattr(saas_user_console, "get_login_by_email_any_status", lambda e: None)
    r = signup_client.post("/me/signup", json={**SIGNUP_BODY, "resend_activation": True})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending_email"               # indistinguishable from the real one
    assert activation_world["mints"] == [] and activation_world["links"] == []


def test_resend_honeypot_trip_gets_the_neutral_answer(monkeypatch, signup_client,
                                                      activation_world):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    _world_with_pending_hold(monkeypatch)
    r = signup_client.post("/me/signup", json={**SIGNUP_BODY, "resend_activation": True,
                                               "website": "https://bot.example"})
    assert r.status_code == 200
    assert r.json()["status"] == "pending_email"
    assert activation_world["mints"] == []                 # a bot learns nothing, gets no email


# ══════════════════════════════════════════════════════════════════════════════════
# UI — the console surfaces cannot silently unwire
# ══════════════════════════════════════════════════════════════════════════════════

def test_console_checkbox_and_queue_surface_wiring():
    """The New-users checkbox exists immediately after Require approval; the awaiting-email
    table exists on the approvals view; app.js loads/saves the valve and renders the holds."""
    import re
    from pathlib import Path
    dash = Path(saas_user_console.__file__).parent / "dashboard"
    html = (dash / "index.html").read_text(encoding="utf-8")
    approval_pos = html.find('id="platform-require_signup_approval"')
    email_pos = html.find('id="platform-require_email_activation"')
    assert approval_pos != -1 and email_pos != -1
    assert approval_pos < email_pos                        # immediately after, same panel
    assert "Require email click to activate" in html
    assert 'id="op-awaiting-email-body"' in html
    assert 'id="op-awaiting-email-empty"' in html
    js = (dash / "app.js").read_text(encoding="utf-8")
    assert "chk('platform-require_email_activation', cfg.require_email_activation)" in js
    assert "require_email_activation: !!(($('platform-require_email_activation') || {}).checked)" in js
    assert "renderAwaitingEmail" in js and "activateAwaitingEmail" in js
    assert "awaiting email activation" in js               # the badge copy
    # The awaiting-email renderer reads the SAME field the API serves.
    assert re.search(r"r\.awaiting_email \|\| \[\]", js)
