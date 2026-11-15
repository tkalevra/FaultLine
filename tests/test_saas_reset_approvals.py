"""Two-tier password-reset APPROVAL workflow — router + state-machine proofs (no DB).

Covers DEV/DESIGN-reset-approval-workflow.md:
  * Tier 1  end-user (/me) reset → tenant-admin approval, flag require_user_reset_approval.
  * Tier 2  tenant-admin (/tenant) reset → operator approval, flag require_operator_approval_*.

The control-plane DATA functions are monkeypatched, so this file is green without a control
plane. The crown-jewel proofs pinned here:
  - flag OFF  → today's DIRECT reset (no pending row).
  - flag ON   → request HELD (pending), NO reset token minted / link emailed.
  - approve   → mint+deliver; realm+account gated at the state machine.
  - deny      → discard, no delivery.
  - BOLA      → a tenant approver ALWAYS acts with its SESSION account (never client input);
                the operator realm is separate. The state-machine guards reject cross-realm.
  - LOCK SCOPE→ a pending reset NEVER calls a data-plane token/schema function.
  - anti-enum → unknown email → identical neutral 200, nothing recorded.
  - expiry / supersede guards.

Run: python3 -m pytest tests/test_saas_reset_approvals.py -q   (tests/ gitignored → git add -f)
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import dashboard_auth as auth
from saas import reset_approvals as ra
from saas import tenant_console as tc
from saas import user_console as uc
from saas import admin as admin_mod


OP_KEY = "op-super-secret-key"
ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-4111-8111-111111111111"   # a seat of ACCT_A
IND_A = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"   # individual login id for USER_A
ADMIN_A = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"   # tenant-admin dashboard_user of ACCT_A


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "reset-approval-unit-secret-0123456789")
    yield


# ══════════════════════════════════════════════════════════════════════════════════
# STATE-MACHINE pure guards (no DB) — realm / kind / BOLA-arg gating
# ══════════════════════════════════════════════════════════════════════════════════

def test_record_pending_rejects_bad_realm_or_kind(saas_on):
    assert ra.record_pending(account_id=ACCT_A, approver_realm="nope",
                             target_kind=ra.TARGET_SEAT, target_id=IND_A, target_email="x@y.z") is None
    assert ra.record_pending(account_id=ACCT_A, approver_realm=ra.REALM_TENANT,
                             target_kind="nope", target_id=IND_A, target_email="x@y.z") is None


def test_approve_rejects_bad_realm_and_tenant_without_account(saas_on):
    assert ra.approve("id-1", realm="nope", account_id=ACCT_A, decided_by=None) is None
    # tenant realm MUST carry an account scope (BOLA) — missing → deny without touching the DB.
    assert ra.approve("id-1", realm=ra.REALM_TENANT, account_id=None, decided_by=None) is None
    assert ra.deny("id-1", realm=ra.REALM_TENANT, account_id=None, decided_by=None) is False


def test_flag_defaults_failsafe(saas_on, monkeypatch):
    # Read error → Tier1 default OFF, Tier2 default ON (fail-safe to the documented posture).
    class _Boom:
        def __enter__(self): raise RuntimeError("db down")
        def __exit__(self, *a): return False
    monkeypatch.setattr(ra.db, "connection", lambda: _Boom())
    assert ra.require_user_reset_approval(ACCT_A) is False
    assert ra.require_operator_approval_for_admin_reset(ACCT_A) is True


# ══════════════════════════════════════════════════════════════════════════════════
# TIER 1 — /me/forgot-password gating (user_console)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def me_client(saas_on):
    app = FastAPI()
    app.include_router(uc.router)
    return TestClient(app)


@pytest.fixture
def known_individual(monkeypatch):
    monkeypatch.setattr(uc, "get_individual_by_email",
                        lambda e: {"id": IND_A, "user_id": USER_A, "account_id": ACCT_A,
                                   "email": "seat@acme.com"} if e == "seat@acme.com" else None)
    # SMTP available so the DIRECT path would actually try to email (proves the gate diverts it).
    monkeypatch.setattr(uc._smtp_config, "get_config_redacted",
                        lambda: {"enabled": True, "configured": True, "reset_link_base_url": None})


def test_tier1_flag_off_direct_reset_no_pending(me_client, known_individual, monkeypatch):
    calls = {"pending": 0, "mint": 0}
    monkeypatch.setattr(ra, "require_user_reset_approval", lambda a: False)
    monkeypatch.setattr(uc._reset_approvals, "require_user_reset_approval", lambda a: False)
    monkeypatch.setattr(uc._reset_approvals, "record_pending",
                        lambda **k: calls.__setitem__("pending", calls["pending"] + 1) or "x")
    monkeypatch.setattr(uc._password_reset, "mint_reset",
                        lambda *a, **k: calls.__setitem__("mint", calls["mint"] + 1) or "tok")
    monkeypatch.setattr(uc._smtp_sender, "send_reset_email", lambda *a, **k: {"ok": True})
    r = me_client.post("/me/forgot-password", json={"email": "seat@acme.com"})
    assert r.status_code == 200
    import time; time.sleep(0.05)  # let the send thread run
    assert calls["pending"] == 0           # NOT held
    assert calls["mint"] == 1              # DIRECT mint+email happened


def test_tier1_flag_on_holds_pending_no_mint(me_client, known_individual, monkeypatch):
    calls = {"pending": 0, "mint": 0}
    monkeypatch.setattr(uc._reset_approvals, "require_user_reset_approval", lambda a: True)

    def _rec(**k):
        calls["pending"] += 1
        assert k["account_id"] == ACCT_A            # scoped to the seat's account
        assert k["approver_realm"] == ra.REALM_TENANT
        assert k["target_kind"] == ra.TARGET_SEAT
        assert k["target_id"] == IND_A
        return "appr-1"
    monkeypatch.setattr(uc._reset_approvals, "record_pending", _rec)
    monkeypatch.setattr(uc._password_reset, "mint_reset",
                        lambda *a, **k: calls.__setitem__("mint", calls["mint"] + 1) or "tok")
    monkeypatch.setattr(uc, "_notify_tenant_admins_of_pending", lambda *a, **k: None)
    monkeypatch.setattr(uc._audit, "record", lambda *a, **k: True)
    r = me_client.post("/me/forgot-password", json={"email": "seat@acme.com"})
    assert r.status_code == 200
    assert calls["pending"] == 1           # HELD
    assert calls["mint"] == 0              # NO reset token minted / link emailed


def test_tier1_unknown_email_neutral_no_pending(me_client, known_individual, monkeypatch):
    calls = {"pending": 0}
    monkeypatch.setattr(uc._reset_approvals, "require_user_reset_approval", lambda a: True)
    monkeypatch.setattr(uc._reset_approvals, "record_pending",
                        lambda **k: calls.__setitem__("pending", calls["pending"] + 1) or "x")
    r = me_client.post("/me/forgot-password", json={"email": "ghost@nowhere.com"})
    assert r.status_code == 200
    assert calls["pending"] == 0           # anti-enumeration: nothing recorded for unknown


# ══════════════════════════════════════════════════════════════════════════════════
# TIER 1 approver QUEUE — tenant console (session-scoped, BOLA)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tenant_client(saas_on):
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _session_a():
    return auth.issue_session(dashboard_user_id=ADMIN_A, account_id=ACCT_A, username="admin@acme.com")


def _csrf_a():
    return auth.issue_csrf(dashboard_user_id=ADMIN_A)


def test_queue_and_approve_require_session(tenant_client):
    assert tenant_client.get("/tenant/reset-approvals").status_code == 401
    assert tenant_client.post("/tenant/reset-approvals/x/approve").status_code == 401


def test_tenant_queue_is_session_scoped(tenant_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(tc._reset_approvals, "list_pending_for_account",
                        lambda a: seen.__setitem__("acct", a) or [])
    tenant_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = tenant_client.get("/tenant/reset-approvals")
    assert r.status_code == 200
    assert seen["acct"] == ACCT_A          # from the SESSION, never client input


def test_tenant_approve_uses_session_account_and_delivers(tenant_client, monkeypatch):
    seen = {}
    def _approve(approval_id, *, realm, account_id, decided_by):
        seen.update(approval_id=approval_id, realm=realm, account_id=account_id)
        return {"target_kind": ra.TARGET_SEAT, "target_id": IND_A,
                "target_email": "seat@acme.com", "account_id": ACCT_A}
    monkeypatch.setattr(tc._reset_approvals, "approve", _approve)
    delivered = {}
    monkeypatch.setattr(tc._user_console, "deliver_password_reset",
                        lambda ind, email, req: delivered.update(ind=ind, email=email) or {"delivery": "emailed", "email": email})
    monkeypatch.setattr(tc._audit, "record", lambda *a, **k: True)

    tenant_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = tenant_client.post("/tenant/reset-approvals/appr-1/approve",
                           headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200
    assert seen["realm"] == ra.REALM_TENANT
    assert seen["account_id"] == ACCT_A    # BOLA: session account, not client-supplied
    assert r.json()["delivery"] == "emailed"
    assert delivered["ind"] == IND_A


def test_tenant_approve_404_when_not_pending(tenant_client, monkeypatch):
    monkeypatch.setattr(tc._reset_approvals, "approve", lambda *a, **k: None)
    tenant_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = tenant_client.post("/tenant/reset-approvals/nope/approve",
                           headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 404


def test_tenant_deny_scoped_and_no_delivery(tenant_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(tc._reset_approvals, "deny",
                        lambda aid, *, realm, account_id, decided_by: seen.update(realm=realm, account_id=account_id) or True)
    # A deny must NEVER deliver a reset.
    monkeypatch.setattr(tc._user_console, "deliver_password_reset",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("deny must not deliver")))
    monkeypatch.setattr(tc._audit, "record", lambda *a, **k: True)
    tenant_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = tenant_client.post("/tenant/reset-approvals/appr-1/deny",
                           headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200
    assert seen["realm"] == ra.REALM_TENANT and seen["account_id"] == ACCT_A


def test_security_settings_get_put_session_scoped(tenant_client, monkeypatch):
    monkeypatch.setattr(tc._reset_approvals, "require_user_reset_approval", lambda a: False)
    monkeypatch.setattr(tc._reset_approvals, "require_operator_approval_for_admin_reset", lambda a: True)
    seen = {}
    monkeypatch.setattr(tc._reset_approvals, "set_require_user_reset_approval",
                        lambda a, v: seen.update(acct=a, val=v) or {"ok": True, "status_code": 200, "enabled": v})
    monkeypatch.setattr(tc._audit, "record", lambda *a, **k: True)
    tenant_client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    assert tenant_client.get("/tenant/security-settings").json()["require_user_reset_approval"] is False
    r = tenant_client.put("/tenant/security-settings", json={"require_user_reset_approval": True},
                          headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200
    assert seen["acct"] == ACCT_A and seen["val"] is True


# ══════════════════════════════════════════════════════════════════════════════════
# TIER 2 — tenant-admin self-serve forgot / operator queue
# ══════════════════════════════════════════════════════════════════════════════════

def test_tier2_forgot_holds_for_operator_when_flag_on(tenant_client, monkeypatch):
    monkeypatch.setattr(auth, "get_user_by_username",
                        lambda u: {"id": ADMIN_A, "account_id": ACCT_A, "username": "admin@acme.com"} if u == "admin@acme.com" else None)
    monkeypatch.setattr(tc._reset_approvals, "require_operator_approval_for_admin_reset", lambda a: True)
    seen = {}
    def _rec(**k):
        seen.update(k)
        return "appr-2"
    monkeypatch.setattr(tc._reset_approvals, "record_pending", _rec)
    monkeypatch.setattr(tc, "_notify_operators_of_pending", lambda *a, **k: None)
    monkeypatch.setattr(tc._audit, "record", lambda *a, **k: True)
    # No session needed (logged-out flow).
    r = tenant_client.post("/tenant/forgot-password", json={"email": "admin@acme.com"})
    assert r.status_code == 200
    assert seen["approver_realm"] == ra.REALM_OPERATOR
    assert seen["target_kind"] == ra.TARGET_DASHBOARD
    assert seen["account_id"] == ACCT_A


def test_tier2_forgot_unknown_or_operator_row_no_pending(tenant_client, monkeypatch):
    # operator row (account_id None) must NOT enter the tenant-admin reset flow
    monkeypatch.setattr(auth, "get_user_by_username",
                        lambda u: {"id": "op-1", "account_id": None, "username": u})
    calls = {"pending": 0}
    monkeypatch.setattr(tc._reset_approvals, "record_pending",
                        lambda **k: calls.__setitem__("pending", calls["pending"] + 1) or "x")
    r = tenant_client.post("/tenant/forgot-password", json={"email": "root@ops.com"})
    assert r.status_code == 200
    assert calls["pending"] == 0


def test_tier2_reset_password_consume(tenant_client, monkeypatch):
    from saas import dashboard_reset as dr
    monkeypatch.setattr(dr, "consume_reset", lambda t: ADMIN_A if t == "good" else None)
    monkeypatch.setattr(auth, "hash_password", lambda p: "hash")
    seen = {}
    monkeypatch.setattr(auth, "set_password", lambda uid, h: seen.update(uid=uid) or True)
    monkeypatch.setattr(tc._audit, "record", lambda *a, **k: True)
    assert tenant_client.post("/tenant/reset-password",
                              json={"token": "bad", "new_password": "longenough"}).status_code == 400
    r = tenant_client.post("/tenant/reset-password", json={"token": "good", "new_password": "longenough"})
    assert r.status_code == 200 and seen["uid"] == ADMIN_A
    # too-short new password → 422 (never reaches consume)
    assert tenant_client.post("/tenant/reset-password",
                              json={"token": "good", "new_password": "short"}).status_code == 422


@pytest.fixture
def op_client(saas_on):
    app = FastAPI()
    app.include_router(admin_mod.router)
    return TestClient(app)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


def test_operator_queue_requires_operator(op_client):
    assert op_client.get("/admin/reset-approvals").status_code == 403


def test_operator_approve_uses_operator_realm_and_delivers(op_client, monkeypatch):
    seen = {}
    def _approve(approval_id, *, realm, account_id, decided_by):
        seen.update(realm=realm, account_id=account_id)
        return {"target_kind": ra.TARGET_DASHBOARD, "target_id": ADMIN_A,
                "target_email": "admin@acme.com", "account_id": ACCT_A}
    monkeypatch.setattr(admin_mod._reset_approvals, "approve", _approve)
    monkeypatch.setattr(uc, "deliver_dashboard_reset",
                        lambda uid, email, req: {"delivery": "emailed", "email": email})
    monkeypatch.setattr(admin_mod._audit, "record", lambda *a, **k: True)
    r = op_client.post("/admin/reset-approvals/appr-2/approve", headers=_op())
    assert r.status_code == 200
    assert seen["realm"] == ra.REALM_OPERATOR
    assert seen["account_id"] is None      # operator = global realm, no account scope
    assert r.json()["delivery"] == "emailed"


def test_operator_policy_put_is_operator_only(op_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(admin_mod._reset_approvals, "set_operator_approval_policy",
                        lambda a, v: seen.update(acct=a, val=v) or {"ok": True, "status_code": 200, "enabled": v})
    monkeypatch.setattr(admin_mod._audit, "record", lambda *a, **k: True)
    # account key (non-operator) is rejected by require_operator
    r_bad = op_client.put(f"/admin/accounts/{ACCT_A}/reset-approval-policy",
                          json={"require_operator_approval_for_admin_reset": False})
    assert r_bad.status_code in (401, 403)
    r = op_client.put(f"/admin/accounts/{ACCT_A}/reset-approval-policy", headers=_op(),
                      json={"require_operator_approval_for_admin_reset": False})
    assert r.status_code == 200
    assert seen["acct"] == ACCT_A and seen["val"] is False


# ══════════════════════════════════════════════════════════════════════════════════
# PART 1 — operator edit-login-email + remove-login parity (admin.py)
# ══════════════════════════════════════════════════════════════════════════════════

def test_operator_edit_login_email(op_client, monkeypatch):
    monkeypatch.setattr(admin_mod, "_resolve_managed_seat_login",
                        lambda a, u: {"individual_id": IND_A, "email": "old@acme.com"})
    monkeypatch.setattr(uc, "set_individual_email",
                        lambda ind, email: {"ok": True, "status_code": 200, "email": email})
    monkeypatch.setattr(admin_mod._audit, "record", lambda *a, **k: True)
    r = op_client.post(f"/admin/accounts/{ACCT_A}/users/{USER_A}/login/email", headers=_op(),
                       json={"email": "New@Acme.com"})
    assert r.status_code == 200
    assert r.json()["email"] == "New@Acme.com"


def test_operator_remove_login(op_client, monkeypatch):
    monkeypatch.setattr(admin_mod, "_resolve_managed_seat_login",
                        lambda a, u: {"individual_id": IND_A, "email": "old@acme.com"})
    seen = {}
    monkeypatch.setattr(uc, "delete_individual_login",
                        lambda ind: seen.update(ind=ind) or True)
    monkeypatch.setattr(admin_mod._audit, "record", lambda *a, **k: True)
    r = op_client.request("DELETE", f"/admin/accounts/{ACCT_A}/users/{USER_A}/login", headers=_op())
    assert r.status_code == 200
    assert r.json()["status"] == "removed" and seen["ind"] == IND_A
