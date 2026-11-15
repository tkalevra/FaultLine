"""BUG-10 defect 1 (P2, owner-directed, 2026-10-31): a completed signup must notify the
CONFIGURED global-admin contact, and the individual welcome must actually fire.

Pinned here (the brief's gauntlet):
  (a) activation-consumed (the /me/verify-email POST door) → the operator notice IS sent
      to platform_settings.notify_email() with live=True wording;
  (b) the welcome email fires for the individual at the SAME transition (the owner's
      "no email on signup" could have been EITHER half — both are pinned);
  (c) DEDUPE: the notice+welcome ride the one-shot activation transition — a replayed /
      re-clicked token 400s and can never re-fire either send (the single-use token IS
      the rate/dedupe mechanism; no notify-on-hold, so no spam magnet);
  (d) the self-serve /me/signup door (activation valve OFF) notifies live=True too;
  (e) UNSET notify_email → the send is SKIPPED but now LOGGED loudly (BUG-10: silence
      was indistinguishable from "nobody signed up"), and never raises.

Run: python3 -m pytest tests/test_signup_admin_notify_pins.py -q
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import platform_settings as saas_platform
from saas import smtp_sender as saas_smtp_sender
from saas import user_console as saas_user_console


IND_ID = "ind00000-0000-4000-8000-0000000000a1"
USER_ID = "usr00000-0000-4000-8000-0000000000a1"
ACC_ID = "acc00000-0000-4000-8000-0000000000a1"
EMAIL = "new.person@example.com"
SIGNUP_BODY = {"email": EMAIL, "password": "correct-horse", "display_name": "New Person"}
LOGIN_ROW = {"id": IND_ID, "user_id": USER_ID, "account_id": ACC_ID, "email": EMAIL,
             "status": "pending_email", "display_name": "New Person"}


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
def notify_world(monkeypatch):
    """Captures the operator notice + the individual welcome, with notify_email SET."""
    world = {"notices": [], "welcomes": []}

    def _mint(individual_id, ttl_hours=None):
        return "act-token-1"

    world["tokens"] = {"act-token-1": IND_ID}

    def _consume(token):
        return world["tokens"].pop(token, None)

    monkeypatch.setattr(saas_platform, "notify_email", lambda: "owner@example.com")
    monkeypatch.setattr(saas_user_console._email_activation, "mint_activation", _mint)
    monkeypatch.setattr(saas_user_console._email_activation, "consume_activation", _consume)
    monkeypatch.setattr(saas_user_console._email_activation, "resend_allowed",
                        lambda i, min_interval_minutes=None: True)
    monkeypatch.setattr(saas_user_console._smtp_config, "get_config_redacted",
                        lambda: {"enabled": True, "configured": True,
                                 "reset_link_base_url": None})
    monkeypatch.setattr(saas_user_console._smtp_config, "resolve_config",
                        lambda: {"reset_link_base_url":
                                 "https://console.example/dashboard/index.html"})
    monkeypatch.setattr(saas_user_console, "_activation_link_for",
                        lambda request, token, base_override=None:
                        f"https://console.example/me/verify-email?token={token}")
    monkeypatch.setattr(saas_smtp_sender, "send_activation_email",
                        lambda to, link, expiry_hours=48, display_name=None: {"ok": True})
    # THE two sends this gauntlet exists for — capture at the SMTP sender seam.
    monkeypatch.setattr(saas_smtp_sender, "send_signup_notice_email",
                        lambda to, **kw: world["notices"].append({"to": to, **kw})
                        or {"ok": True})
    monkeypatch.setattr(saas_user_console, "send_welcome_to_individual",
                        lambda t: world["welcomes"].append(t["email"]) or {"delivery": "emailed"})
    return world


@pytest.fixture
def signup_client(monkeypatch) -> TestClient:
    """/me door client with the control-plane collaborators stubbed (no live DB)."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_user_console, "_email_taken", lambda e: False)
    monkeypatch.setattr(saas_user_console._auth, "hash_password", lambda p: "$argon2id$stub")
    monkeypatch.setattr(saas_user_console._accounts, "get_or_create_singles_account",
                        lambda: ACC_ID)
    monkeypatch.setattr(saas_user_console._audit, "record", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(saas_user_console.router)
    return TestClient(app)


def _fake_conn_factory(monkeypatch, module, flip_row):
    """The guarded-UPDATE connection the activation flip needs (mirrors the activation
    suite's harness so the REAL _activate_pending_email_individual runs)."""
    calls = {"queries": []}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=None):
            calls["queries"].append((" ".join(q.split()), p))
        def fetchone(self): return flip_row

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def commit(self): pass
        def rollback(self): pass

    monkeypatch.setattr(module.db, "connection", lambda: _Conn())
    return calls


def _arm_activation_flip(monkeypatch):
    monkeypatch.setattr(saas_user_console, "get_login_by_id_any_status",
                        lambda i: dict(LOGIN_ROW))
    monkeypatch.setattr(saas_user_console, "activate_approved_individual",
                        lambda t: {"seat": "ok", "trial": "started"})
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_session",
                        lambda **kw: "session-token")
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_csrf", lambda **kw: "csrf")
    monkeypatch.setattr(saas_user_console, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(saas_user_console._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_secret", "token_prefix": "flu_abc"})


# ══════════════════════════════════════════════════════════════════════════════════
# (a)+(b)+(c) THE ACTIVATION-CONSUMED TRANSITION: notice + welcome + dedupe
# ══════════════════════════════════════════════════════════════════════════════════

def test_activation_consume_notifies_operator_and_welcomes_individual(
        monkeypatch, signup_client, notify_world):
    """(a)+(b) The owner's complaint had two possible absent halves. On the activation
    click BOTH must fire: the operator notice to the CONFIGURED address with live=True,
    and the individual welcome — and (c) a replay of the spent token 400s and fires
    NEITHER again."""
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: False)
    monkeypatch.setattr(saas_user_console, "_create_individual", lambda **kw: IND_ID)
    _arm_activation_flip(monkeypatch)
    _fake_conn_factory(monkeypatch, saas_user_console,
                       (USER_ID, ACC_ID, EMAIL, "New Person"))

    r0 = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r0.json()["status"] == "pending_email"

    r = signup_client.post("/me/verify-email", json={"token": "act-token-1"})
    assert r.status_code == 200 and r.json()["status"] == "authenticated"

    # (a) operator notice — to the CONFIGURED contact, live wording (account IS active)
    assert len(notify_world["notices"]) == 1, \
        f"expected exactly one operator notice, got {notify_world['notices']}"
    n = notify_world["notices"][0]
    assert n["to"] == "owner@example.com"
    assert n["live"] is True
    assert n["applicant_email"] == EMAIL

    # (b) the individual welcome fired too
    assert notify_world["welcomes"] == [EMAIL]

    # (c) DEDUPE — the token is spent; a replay 400s and re-fires NOTHING
    r2 = signup_client.post("/me/verify-email", json={"token": "act-token-1"})
    assert r2.status_code == 400
    assert len(notify_world["notices"]) == 1
    assert notify_world["welcomes"] == [EMAIL]


def test_signup_hold_does_not_spam_operator(monkeypatch, signup_client, notify_world):
    """(c, the other half) The notice rides the one-shot transition only — a signup held
    at pending_email fires the operator notice ZERO times (no notify-on-hold spam magnet;
    the notice belongs to the activation CONSUMPTION, per the brief)."""
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: True)
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: False)
    monkeypatch.setattr(saas_user_console, "_create_individual", lambda **kw: IND_ID)
    _arm_activation_flip(monkeypatch)
    _fake_conn_factory(monkeypatch, saas_user_console,
                       (USER_ID, ACC_ID, EMAIL, "New Person"))

    r0 = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r0.json()["status"] == "pending_email"
    assert notify_world["notices"] == [], "a held signup must not notify the operator"
    assert notify_world["welcomes"] == [], "a held signup must not send the welcome"


# ══════════════════════════════════════════════════════════════════════════════════
# (d) THE UNGATED SELF-SERVE DOOR notifies too
# ══════════════════════════════════════════════════════════════════════════════════

def test_self_serve_signup_notifies_live(monkeypatch, signup_client, notify_world):
    """(d) Activation valve OFF: /me/signup that goes straight to active sends the SAME
    live=True operator notice + welcome (both doors, one contract)."""
    monkeypatch.setattr(saas_platform, "require_email_activation", lambda: False)
    monkeypatch.setattr(saas_platform, "require_signup_approval", lambda: False)
    monkeypatch.setattr(saas_user_console, "_create_individual",
                        lambda **kw: IND_ID)
    monkeypatch.setattr(saas_user_console, "_provision_individual_seat",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_session",
                        lambda **kw: "session-token")
    monkeypatch.setattr(saas_user_console._auth, "issue_individual_csrf", lambda **kw: "csrf")
    monkeypatch.setattr(saas_user_console, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(saas_user_console._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_secret", "token_prefix": "flu_abc"})

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r.status_code == 200 and r.json()["status"] == "authenticated"
    assert len(notify_world["notices"]) == 1
    assert notify_world["notices"][0]["live"] is True
    assert notify_world["welcomes"] == [EMAIL]


# ══════════════════════════════════════════════════════════════════════════════════
# (e) UNSET ADDRESS — skipped, LOUDLY, never raising
# ══════════════════════════════════════════════════════════════════════════════════

def test_unset_notify_email_is_loud_not_silent(monkeypatch, caplog):
    """(e) The owner not receiving notices was indistinguishable from nobody signing up.
    With notify_email UNSET the send is skipped — but a log line now says so, and the
    call still never raises (a notify can never break a signup)."""
    monkeypatch.setattr(saas_platform, "notify_email", lambda: None)
    logs = []
    monkeypatch.setattr(saas_user_console, "_log", lambda m: logs.append(m))
    # a dead sender would also be swallowed; the unset-address arm must not even reach it
    monkeypatch.setattr(saas_smtp_sender, "send_signup_notice_email",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("unset address must not reach the sender")))

    from fastapi import Request
    req = Request({"type": "http", "method": "GET", "url": "https://x/",
                   "headers": [], "query_string": b""})
    saas_user_console._notify_operator_of_signup(req, email=EMAIL,
                                                 display_name="New Person", live=True)
    assert any("notify_email" in m and "SKIP" in m.upper() for m in logs), \
        f"the unset-address skip must be logged loudly, got {logs}"
