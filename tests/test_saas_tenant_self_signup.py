"""PUBLIC tenant self-signup (POST /signup/team) — provisioning, gating + anti-abuse proofs.

The door a STRANGER uses to create a TENANT (saas/tenant_signup.py). Follows the
tests/test_saas_tenant_control.py conventions: every control-plane function is monkeypatched, so
this file is always green without a control plane or a Redis.

The proofs here are the ones that would hurt if they regressed:
  1. approval OFF   → an account + an OWNER login + BOTH trials + a session, and the first API
                      key returned exactly once.
  2. can_view_billing=TRUE on the owner. The column defaults FALSE (migration 159) and the grant
                      panel is OWNER-gated — a self-signed-up owner without it is the paying
                      customer permanently locked out of their own billing, with nobody above
                      them able to grant it back. This is THE regression to guard.
  3. approval ON    → the application is HELD: NO create_account, NO login, NO key, NO seat, NO
                      session, NO trial. Only a queue row.
  4. anti-abuse     → rate-limit / honeypot / format / disposable-email / name-guard / duplicate
                      / 3-strikes each reject BEFORE any DB write (nothing is created).
  5. collisions     → a duplicate email is a 409 (pre-check AND the create-race), and a losing
                      race rolls the orphan account back rather than stranding it.
  6. SAAS off       → 404, fail-closed, on the public door and the operator queue alike.
  7. the operator queue is behind require_operator, and approve() returns a failed application
                      to the queue instead of losing it.

Run: python3 -m pytest tests/test_saas_tenant_self_signup.py -q   (tests/ is gitignored →
git add -f).
"""

import pytest

from saas import tenant_signup as ts


ACCOUNT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OWNER_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
REQUEST_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

GOOD = {
    "company": "Acme Robotics",
    "email": "Owner@AcmeRobotics.com",
    "password": "correct horse battery",
    "display_name": "Wile E. Coyote",
}


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(ts.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def moderation_guards_neutral(monkeypatch):
    """Migrations 228/229: the anti-abuse chain now also consults the platform BAN LIST and the
    global SIGNUP SPAM FILTER. Both read the control plane, which this DB-less suite does not
    have — and the ban check fails CLOSED by design (503 "temporarily unavailable"), which would
    mask everything this file is actually about. Stub both NEUTRAL so these tests keep proving
    what they are for. The guards' OWN behaviour — matching, canonical folding, fail-open vs
    fail-closed, both doors — is proven in tests/test_saas_account_moderation.py."""
    monkeypatch.setattr(ts._moderation, "signup_ban_check",
                        lambda e: {"banned": False, "degraded": False})
    monkeypatch.setattr(ts._signup_filter, "evaluate",
                        lambda e, **kw: ts._signup_filter.Decision(True))


class Spy:
    """Records every control-plane call the router makes, so a test can assert not just the
    response but that NOTHING was written on a rejection path."""

    def __init__(self):
        self.calls = []

    def note(self, _name, **kw):
        # first arg is positional-ish on purpose: callers pass note("create_account", name=...)
        self.calls.append((_name, kw))

    def names(self):
        return [c[0] for c in self.calls]

    def kw(self, name):
        for n, kw in self.calls:
            if n == name:
                return kw
        return None

    def wrote_anything(self):
        """True iff any DB-mutating primitive was reached."""
        writes = {"create_account", "create_dashboard_user", "record_pending",
                  "start_account_trial", "set_chatbot_trial", "set_display_name"}
        return bool(writes.intersection(self.names()))


@pytest.fixture
def spy(monkeypatch):
    """Neutral, permissive stubs for every side-effecting dependency. A test tightens only the
    one it is about."""
    s = Spy()

    # ── anti-abuse: all PASS by default ──
    monkeypatch.setattr(ts._ratelimit, "allow_signup_attempt", lambda ip: True)
    monkeypatch.setattr(ts._name_guard, "is_disposable_email", lambda e: False)
    monkeypatch.setattr(ts._name_guard, "looks_gibberish", lambda n: False)
    monkeypatch.setattr(ts._signup_ledger, "decide", lambda e: ts._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(ts._signup_ledger, "record_attempt",
                        lambda e, **kw: s.note("record_attempt", email=e, **kw) or True)

    # ── duplicate checks: address is free ──
    monkeypatch.setattr(ts._auth, "get_user_by_username", lambda u: None)
    monkeypatch.setattr(ts, "pending_request_for_email", lambda e: False)

    # ── approval valve OFF by default (the provisioning path); flipped per-test ──
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: False)
    monkeypatch.setattr(ts._platform_settings, "notify_email", lambda: None)
    monkeypatch.setattr(ts._platform_settings, "welcome_from_address", lambda: None)
    monkeypatch.setattr(ts._platform_settings, "trial_days", lambda: 30)

    # ── argon2 may not be installed in CI; the hash itself is not what this file proves ──
    monkeypatch.setattr(ts._auth, "hash_password", lambda p: "$argon2id$stub$" + str(len(p)))

    # ── control-plane writes ──
    def _create_account(name, slug, seat_limit, plan, account_type="standard",
                        account_kind="tenant"):
        s.note("create_account", name=name, slug=slug, seat_limit=seat_limit, plan=plan,
               account_type=account_type, account_kind=account_kind)
        return {"account_id": ACCOUNT_ID, "slug": slug, "account_type": account_type,
                "account_kind": account_kind, "key_id": "k1", "key_prefix": "fl_abc",
                "api_key": "fl_abcSECRETSECRET"}
    monkeypatch.setattr(ts._accounts, "create_account", _create_account)

    def _create_dashboard_user(**kw):
        s.note("create_dashboard_user", **kw)
        return OWNER_ID
    monkeypatch.setattr(ts._auth, "create_dashboard_user", _create_dashboard_user)

    monkeypatch.setattr(ts._auth, "set_display_name",
                        lambda uid, dn: s.note("set_display_name", uid=uid, dn=dn) or True)
    monkeypatch.setattr(ts._auth, "touch_last_login", lambda uid: None)
    monkeypatch.setattr(ts._auth, "set_can_view_billing",
                        lambda a, u, v: s.note("set_can_view_billing", account=a, user=u, value=v)
                        or True)

    def _start_trial(account_id, **kw):
        s.note("start_account_trial", account_id=account_id, **kw)
        return {"active": True, "started_at": "2026-08-07T00:00:00+00:00",
                "ends_at": "2026-09-06T00:00:00+00:00", "source": kw.get("source")}
    monkeypatch.setattr(ts._accounts, "start_account_trial", _start_trial)

    def _set_chatbot_trial(account_id, **kw):
        s.note("set_chatbot_trial", account_id=account_id, **kw)
        return {"status": "active", "active": True}
    monkeypatch.setattr(ts._accounts, "set_chatbot_trial", _set_chatbot_trial)

    monkeypatch.setattr(ts, "record_pending",
                        lambda **kw: s.note("record_pending", **kw) or REQUEST_ID)

    # ── rollback path (only reached when the login create loses a race) ──
    monkeypatch.setattr(ts._accounts, "set_account_status",
                        lambda a, st, **kw: s.note("set_account_status", account=a, status=st)
                        or {"ok": True})
    monkeypatch.setattr(ts._accounts, "delete_account",
                        lambda a, slug, **kw: s.note("delete_account", account=a, slug=slug)
                        or {"ok": True})

    # ── outbound / audit: never touch a relay or the log table in a unit test ──
    monkeypatch.setattr(ts._audit, "record", lambda *a, **kw: True)
    monkeypatch.setattr(ts._smtp_sender, "send_welcome_email",
                        lambda to, **kw: s.note("welcome_email", to=to) or {"ok": True})
    monkeypatch.setattr(ts._smtp_sender, "send_signup_notice_email",
                        lambda to, **kw: s.note("notice_email", to=to) or {"ok": True})
    monkeypatch.setattr(ts._smtp_sender, "send_final_rejection_email",
                        lambda to, **kw: s.note("rejection_email", to=to) or {"ok": True})
    monkeypatch.setattr(ts._smtp_sender, "send_cheeky_repeat_email",
                        lambda to, **kw: s.note("cheeky_email", to=to) or {"ok": True})
    monkeypatch.setattr(ts._smtp_config, "resolve_config",
                        lambda: {"reset_link_base_url": "https://app.example.test/dashboard/"})
    return s


# ════════════════════════════════════════════════════════════════════════════════
# 1. APPROVAL OFF → the tenant is fully provisioned
# ════════════════════════════════════════════════════════════════════════════════

def test_signup_creates_account_and_owner_login(client, spy):
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "authenticated"
    assert body["account_id"] == ACCOUNT_ID
    assert body["csrf_token"]

    # the account was created through the EXISTING primitive, as a TEAM (never Enterprise —
    # create_account seeds the immutable $1500 base package for kind='tenant').
    acct = spy.kw("create_account")
    assert acct is not None
    assert acct["account_kind"] == ts.SELF_SERVE_ACCOUNT_KIND == "team"
    assert acct["name"] == "Acme Robotics"
    assert acct["slug"] == "acme-robotics"      # derived, DNS-safe, brand-guarded

    # the OWNER login was created through the EXISTING primitive, scoped to the new account,
    # with the EMAIL as the username (lowercased) and the admin role.
    login = spy.kw("create_dashboard_user")
    assert login is not None
    assert login["account_id"] == ACCOUNT_ID
    assert login["username"] == "owner@acmerobotics.com"
    assert login["role"] == "admin"
    assert login["password_hash"].startswith("$argon2id$")


def test_first_api_key_returned_exactly_once(client, spy):
    body = client.post("/signup/team", json=GOOD).json()
    assert body["api_key"] == "fl_abcSECRETSECRET"
    assert body["key_prefix"] == "fl_abc"


def test_session_cookie_is_set_so_the_owner_lands_signed_in(client, spy):
    from saas import dashboard_auth as auth
    r = client.post("/signup/team", json=GOOD)
    assert auth.SESSION_COOKIE_NAME in r.cookies
    # Starlette DQUOTE-wraps a cookie value containing '=' (base64 padding); a browser strips
    # those on send. Same shape the tenant login already produces — unwrap before verifying.
    raw = r.cookies[auth.SESSION_COOKIE_NAME].strip('"')
    session = auth.verify_session(raw)
    assert session and session["acct"] == ACCOUNT_ID and session["sub"] == OWNER_ID


def test_display_name_is_applied_to_the_owner_login(client, spy):
    client.post("/signup/team", json=GOOD)
    assert spy.kw("set_display_name")["dn"] == "Wile E. Coyote"


# ════════════════════════════════════════════════════════════════════════════════
# 2. THE TRAP: the owner MUST land with billing access
# ════════════════════════════════════════════════════════════════════════════════

def test_owner_login_lands_with_can_view_billing_true(client, spy):
    """dashboard_users.can_view_billing defaults FALSE (migration 159) and the grant panel is
    owner-gated. A self-signed-up owner without it can never get it back."""
    client.post("/signup/team", json=GOOD)
    login = spy.kw("create_dashboard_user")
    assert login["can_view_billing"] is True


def test_billing_flag_is_written_in_the_insert_not_a_follow_up_update(client, spy):
    """Set IN the INSERT, so there is no window in which the owner exists without billing —
    and so a failed follow-up UPDATE cannot silently leave them locked out."""
    client.post("/signup/team", json=GOOD)
    assert "set_can_view_billing" not in spy.names()


def test_create_dashboard_user_default_is_still_false_for_every_other_caller():
    """The new parameter must not widen any EXISTING login-minting path."""
    import inspect
    from saas import dashboard_auth as auth
    sig = inspect.signature(auth.create_dashboard_user)
    assert sig.parameters["can_view_billing"].default is False


# ════════════════════════════════════════════════════════════════════════════════
# 3. TRIALS — the account trial + the chat-agent feature trial
# ════════════════════════════════════════════════════════════════════════════════

def test_signup_starts_the_account_trial(client, spy):
    body = client.post("/signup/team", json=GOOD).json()
    trial = spy.kw("start_account_trial")
    assert trial["account_id"] == ACCOUNT_ID
    assert trial["source"] == ts.TRIAL_SOURCE
    assert body["trial"]["active"] is True


def test_signup_grants_the_chatbot_feature_trial_via_the_existing_setter(client, spy):
    client.post("/signup/team", json=GOOD)
    ct = spy.kw("set_chatbot_trial")
    assert ct["kind"] == "grant" and ct["granted_by"] == ts.TRIAL_SOURCE


def test_provisioning_survives_a_trial_stamp_failure(client, spy, monkeypatch):
    """The tenant IS usable without the free-time marker; failing a working signup over it would
    be strictly worse. It must be reported, not raised."""
    monkeypatch.setattr(ts._accounts, "start_account_trial", lambda a, **kw: None)
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 200
    assert r.json()["trial"] is None
    assert spy.kw("create_dashboard_user") is not None


# ════════════════════════════════════════════════════════════════════════════════
# 4. APPROVAL ON → HELD. Nothing is created.
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def approval_on(monkeypatch):
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: True)


def test_approval_on_holds_the_signup(client, spy, approval_on):
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending_approval"
    # NO account, NO login, NO key, NO trial, NO session, NO api_key echoed back.
    assert "create_account" not in spy.names()
    assert "create_dashboard_user" not in spy.names()
    assert "start_account_trial" not in spy.names()
    assert "api_key" not in body and "account_id" not in body and "csrf_token" not in body
    from saas import dashboard_auth as auth
    assert auth.SESSION_COOKIE_NAME not in r.cookies


def test_approval_on_queues_the_application_with_the_password_hash(client, spy, approval_on):
    client.post("/signup/team", json=GOOD)
    held = spy.kw("record_pending")
    assert held["company_name"] == "Acme Robotics"
    assert held["email"] == "owner@acmerobotics.com"
    assert held["password_hash"].startswith("$argon2id$")   # so they are never asked twice
    assert held["account_kind"] == "team"


def test_approval_on_with_an_unwritable_queue_is_503_not_a_silent_drop(
        client, spy, approval_on, monkeypatch):
    """A pending application with no queue row is invisible to the operator — an applicant who
    can never be approved and whose address is silently burned."""
    monkeypatch.setattr(ts, "record_pending", lambda **kw: None)
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 503


# ════════════════════════════════════════════════════════════════════════════════
# 5. ANTI-ABUSE — every guard rejects BEFORE any DB write
# ════════════════════════════════════════════════════════════════════════════════

def test_rate_limit_rejects_before_any_write(client, spy, monkeypatch):
    monkeypatch.setattr(ts._ratelimit, "allow_signup_attempt", lambda ip: False)
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 429
    assert not spy.wrote_anything()


def test_honeypot_is_dropped_bot_opaquely_and_writes_nothing(client, spy):
    """A trip must be indistinguishable from a GENUINE outcome in whichever approval mode is
    active — so with the valve OFF (this fixture's default, and how the owner runs it) the answer
    is the ordinary 200 "authenticated", not a 202 that only a trapped bot ever sees. The full
    channel-by-channel proof lives in tests/test_saas_open_signup_hardening.py; what this file
    pins is the half that belongs to it: absolutely nothing is created."""
    payload = dict(GOOD, website="http://spam.example")
    r = client.post("/signup/team", json=payload)
    assert r.status_code == 200
    assert r.json()["status"] == "authenticated"
    assert not spy.wrote_anything()


def test_honeypot_under_approval_on_still_looks_like_an_ordinary_held_application(
        client, spy, approval_on):
    r = client.post("/signup/team", json=dict(GOOD, website="http://spam.example"))
    assert r.status_code == 202
    assert r.json()["status"] == "pending_approval"
    assert not spy.wrote_anything()


def test_bad_email_rejects_before_any_write(client, spy):
    r = client.post("/signup/team", json=dict(GOOD, email="not-an-email"))
    assert r.status_code == 400
    assert not spy.wrote_anything()


def test_short_password_rejects_before_any_write(client, spy):
    r = client.post("/signup/team", json=dict(GOOD, password="short"))
    assert r.status_code == 400
    assert not spy.wrote_anything()


def test_missing_company_rejects_before_any_write(client, spy):
    r = client.post("/signup/team", json=dict(GOOD, company="   "))
    assert r.status_code == 400
    assert not spy.wrote_anything()


def test_disposable_email_rejects_before_any_write(client, spy, monkeypatch):
    monkeypatch.setattr(ts._name_guard, "is_disposable_email", lambda e: True)
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 400
    assert "permanent email" in r.json()["detail"]
    assert not spy.wrote_anything()


def test_company_name_guard_rejects_before_any_write(client, spy, monkeypatch):
    """The COMPANY name is the impersonation surface here: it names the account AND derives the
    slug (→ a subdomain)."""
    class _Verdict:
        ok = False
        code = "brand"
        message = "Please choose a different name."
    monkeypatch.setattr(ts._name_guard, "check_display_name", lambda n: _Verdict())
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 400
    assert not spy.wrote_anything()


def test_three_strikes_ladder_rejects_before_any_write(client, spy, monkeypatch):
    monkeypatch.setattr(ts._signup_ledger, "decide",
                        lambda e: ts._signup_ledger.DECISION_REJECT)
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 202
    assert r.json()["status"] == "not_eligible"
    assert not spy.wrote_anything()
    assert "rejection_email" in spy.names()


def test_second_attempt_is_allowed_with_the_cheeky_note(client, spy, monkeypatch):
    monkeypatch.setattr(ts._signup_ledger, "decide",
                        lambda e: ts._signup_ledger.DECISION_ALLOW_CHEEKY)
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 200
    assert "cheeky_email" in spy.names()


def test_the_dead_invite_code_gate_is_not_copied(client, spy):
    """/me/signup carries a legacy, UI-less invite path. It must not be reproduced here — an
    invite requirement with no way to issue one is a closed door wearing an open sign."""
    import inspect
    src = inspect.getsource(ts)
    assert "invite_code" not in src
    assert "_consume_invite" not in src


# ════════════════════════════════════════════════════════════════════════════════
# 6. COLLISIONS — duplicate address, and the create-race
# ════════════════════════════════════════════════════════════════════════════════

def test_existing_tenant_login_is_409_before_any_write(client, spy, monkeypatch):
    monkeypatch.setattr(ts._auth, "get_user_by_username",
                        lambda u: {"id": OWNER_ID, "account_id": ACCOUNT_ID})
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 409
    assert not spy.wrote_anything()


def test_pending_application_for_the_same_address_is_409(client, spy, monkeypatch):
    monkeypatch.setattr(ts, "pending_request_for_email", lambda e: True)
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 409
    assert not spy.wrote_anything()


def test_lost_login_race_rolls_the_orphan_account_back(client, spy, monkeypatch):
    """An account with no login is unreachable by anyone but the operator and holds its slug
    forever. Losing the UNIQUE(username) race must not strand one."""
    monkeypatch.setattr(ts._auth, "create_dashboard_user", lambda **kw: None)
    r = client.post("/signup/team", json=GOOD)
    assert r.status_code == 409
    assert spy.kw("set_account_status")["status"] == "archived"
    assert spy.kw("delete_account")["account"] == ACCOUNT_ID
    # no trial was minted for an account that no longer exists
    assert "start_account_trial" not in spy.names()


def test_slug_collision_is_left_to_create_account_auto_uniquify(client, spy, monkeypatch):
    """create_account already retries base → base-2 → … . The signup must NOT reimplement that;
    it just surfaces whichever slug came back."""
    def _collide(name, slug, seat_limit, plan, account_type="standard", account_kind="tenant"):
        return {"account_id": ACCOUNT_ID, "slug": slug + "-3", "account_kind": account_kind,
                "key_prefix": "fl_abc", "api_key": "fl_abcSECRET"}
    monkeypatch.setattr(ts._accounts, "create_account", _collide)
    body = client.post("/signup/team", json=GOOD).json()
    assert body["slug"] == "acme-robotics-3"


def test_a_reserved_company_name_cannot_become_a_reserved_subdomain(client, spy):
    """The slug derivation is REUSED from the operator route (admin._safe_slug_from_name), which
    prefixes a reserved/brand/profanity label with 't-'."""
    client.post("/signup/team", json=dict(GOOD, company="www"))
    assert spy.kw("create_account")["slug"] == "t-www"


# ════════════════════════════════════════════════════════════════════════════════
# 7. SAAS OFF → 404 everywhere (fail-closed)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def saas_off_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SAAS_MODE", "0")
    monkeypatch.delenv("SAAS_SESSION_SECRET", raising=False)
    app = FastAPI()
    app.include_router(ts.router)
    return TestClient(app)


def test_public_signup_is_404_with_saas_off(saas_off_client, spy):
    r = saas_off_client.post("/signup/team", json=GOOD)
    assert r.status_code == 404
    assert not spy.wrote_anything()


@pytest.mark.parametrize("method,path", [
    ("get", "/admin/tenant-signups"),
    ("post", f"/admin/tenant-signups/{REQUEST_ID}/approve"),
    ("post", f"/admin/tenant-signups/{REQUEST_ID}/deny"),
])
def test_operator_queue_is_404_with_saas_off(saas_off_client, method, path):
    r = getattr(saas_off_client, method)(path)
    assert r.status_code == 404


# ════════════════════════════════════════════════════════════════════════════════
# 8. THE OPERATOR QUEUE — auth + approve/deny behaviour
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method,path", [
    ("get", "/admin/tenant-signups"),
    ("post", f"/admin/tenant-signups/{REQUEST_ID}/approve"),
    ("post", f"/admin/tenant-signups/{REQUEST_ID}/deny"),
])
def test_operator_queue_requires_an_operator_credential(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 403


@pytest.fixture
def as_operator(monkeypatch):
    """Satisfy the SAME require_operator dependency the rest of /admin uses (never bypass it —
    the point is that the gate is present and is that one)."""
    from saas import admin as _admin
    monkeypatch.setattr(_admin, "_operator_key_ok", lambda auth: True)


def test_operator_approve_provisions_the_held_tenant(client, spy, as_operator, monkeypatch):
    monkeypatch.setattr(ts, "claim", lambda rid, **kw: {
        "id": rid, "company_name": "Acme Robotics", "requested_slug": "acme-robotics",
        "email": "owner@acmerobotics.com", "password_hash": "$argon2id$stub$1",
        "display_name": "Wile E. Coyote", "account_kind": "team"})
    monkeypatch.setattr(ts, "attach_account",
                        lambda rid, aid: spy.note("attach_account", rid=rid, aid=aid) or True)
    r = client.post(f"/admin/tenant-signups/{REQUEST_ID}/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["account_id"] == ACCOUNT_ID
    assert spy.kw("create_dashboard_user")["can_view_billing"] is True
    assert spy.kw("start_account_trial")["source"] == ts.TRIAL_SOURCE
    assert spy.kw("attach_account")["aid"] == ACCOUNT_ID
    # An operator must never be handed the customer's plaintext credential.
    assert "api_key" not in body


def test_operator_approve_of_an_undecidable_application_is_404(client, spy, as_operator,
                                                               monkeypatch):
    monkeypatch.setattr(ts, "claim", lambda rid, **kw: None)
    r = client.post(f"/admin/tenant-signups/{REQUEST_ID}/approve")
    assert r.status_code == 404
    assert not spy.wrote_anything()


def test_failed_provisioning_returns_the_application_to_the_queue(client, spy, as_operator,
                                                                  monkeypatch):
    """Otherwise the operator watches the item vanish with no tenant to show for it."""
    monkeypatch.setattr(ts, "claim", lambda rid, **kw: {
        "id": rid, "company_name": "Acme Robotics", "requested_slug": "acme-robotics",
        "email": "owner@acmerobotics.com", "password_hash": "$argon2id$stub$1",
        "display_name": None, "account_kind": "team"})
    monkeypatch.setattr(ts._accounts, "create_account",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("control plane down")))
    released = []
    monkeypatch.setattr(ts, "release", lambda rid: released.append(rid) or True)
    r = client.post(f"/admin/tenant-signups/{REQUEST_ID}/approve")
    assert r.status_code == 500
    assert released == [REQUEST_ID]


def test_operator_deny_creates_nothing(client, spy, as_operator, monkeypatch):
    monkeypatch.setattr(ts, "deny", lambda rid, **kw: True)
    r = client.post(f"/admin/tenant-signups/{REQUEST_ID}/deny")
    assert r.status_code == 200 and r.json()["status"] == "denied"
    assert not spy.wrote_anything()


def test_operator_list_never_leaks_a_password_hash(client, as_operator, monkeypatch):
    monkeypatch.setattr(ts, "list_pending", lambda: [
        {"id": REQUEST_ID, "company_name": "Acme Robotics", "email": "owner@acmerobotics.com"}])
    r = client.get("/admin/tenant-signups")
    assert r.status_code == 200
    assert "password" not in r.text.lower()
    # the column is not in the queue projection at all
    assert "password_hash" not in ts._REQ_COLS


# ════════════════════════════════════════════════════════════════════════════════
# 9. The operator-only account route was NOT loosened
# ════════════════════════════════════════════════════════════════════════════════

def test_admin_create_account_still_requires_an_operator():
    """This whole feature exists so POST /admin/accounts never had to be opened up."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from saas import admin as _admin
    app = FastAPI()
    app.include_router(_admin.router)
    r = TestClient(app).post("/admin/accounts", json={"name": "Nope", "slug": "nope"})
    assert r.status_code in (403, 404)      # 403 operator required / 404 SAAS off
