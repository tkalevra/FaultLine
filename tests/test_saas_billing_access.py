"""Billing ROLE SEPARATION (migration 159) — the can_view_billing gate + owner-only grant.

Owner-ratified decision (CLAUDE.md SaaS §12): a seat/security admin does NOT see spend by
default. These proofs cover the crown-jewel invariants, all server-derived (never client input):

  1. can_view_billing gate  → a login WITHOUT it gets 403 on the billing/ledger/plan-cost/
                              billing-contact reads; WITH it → 200.
  2. plan-cost redaction    → GET /tenant/capacity keeps capacity COUNTS for everyone but strips
                              every price for a non-billing admin.
  3. spend is gated         → POST /tenant/capacity (incurring charge) needs can_view_billing.
  4. billing_contact_email  → round-trips through the (billing-gated) get/set endpoints.
  5. owner-only grant       → a NON-owner (even one WITH billing) cannot grant billing access
                              (can't self-grant); the OWNER can; the owner can't revoke their own.
  6. no session             → 401 (fail-closed) on the gated endpoints.

The control-plane data functions are monkeypatched, so this file is green without a control
plane. Run: python3 -m pytest tests/test_saas_billing_access.py -q  (tests/ is gitignored →
git add -f).
"""

import pytest

from saas import accounts as saas_accounts
from saas import dashboard_auth as auth
from saas import tenant_console as tc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OWNER = "11111111-1111-4111-8111-111111111111"   # owner of ACCT_A, can_view_billing
ADMIN_NB = "22222222-2222-4222-8222-222222222222"  # a seat/security admin — NO billing
BILL_NONOWNER = "33333333-3333-4333-8333-333333333333"  # billing, but NOT the owner


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
    # _audit.record hits the DB — no-op it so the gate's denial audit never touches a control plane.
    monkeypatch.setattr(tc._audit, "record", lambda *a, **k: None)
    yield


@pytest.fixture
def client(saas_on):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


# WHO can see billing / WHO is owner — keyed by the session's dashboard_user_id (sub).
_BILLING = {OWNER: True, ADMIN_NB: False, BILL_NONOWNER: True}
_OWNER = {OWNER: True, ADMIN_NB: False, BILL_NONOWNER: False}


@pytest.fixture
def roles(monkeypatch):
    monkeypatch.setattr(auth, "can_view_billing", lambda uid: _BILLING.get(str(uid), False))
    monkeypatch.setattr(auth, "is_account_owner",
                        lambda acct, uid: acct == ACCT_A and _OWNER.get(str(uid), False))


def _sess(sub):
    return auth.issue_session(dashboard_user_id=sub, account_id=ACCT_A, username=sub[:5])


def _csrf(sub):
    return auth.issue_csrf(dashboard_user_id=sub)


def _login(client, sub):
    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE_NAME, _sess(sub))


# ════════════════════════════════════════════════════════════════════════════════
# 1. no session → 401 on every gated endpoint (fail-closed, before any role check)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method,path", [
    ("get", "/tenant/billing"),
    ("get", "/tenant/ledger"),
    ("get", "/tenant/billing-contact"),
    ("get", "/tenant/billing-access"),
])
def test_no_session_is_401(client, roles, method, path):
    client.cookies.clear()
    assert getattr(client, method)(path).status_code == 401


# ════════════════════════════════════════════════════════════════════════════════
# 2. can_view_billing gate on the money READS
# ════════════════════════════════════════════════════════════════════════════════

def test_billing_read_denied_without_permission(client, roles, monkeypatch):
    monkeypatch.setattr(tc._entitlements, "billing_view", lambda a: {"monthly_total": 42})
    _login(client, ADMIN_NB)
    r = client.get("/tenant/billing")
    assert r.status_code == 403
    assert "billing contact" in r.json()["detail"].lower()


def test_billing_read_allowed_with_permission(client, roles, monkeypatch):
    monkeypatch.setattr(tc._entitlements, "billing_view", lambda a: {"monthly_total": 42})
    _login(client, OWNER)
    r = client.get("/tenant/billing")
    assert r.status_code == 200 and r.json()["monthly_total"] == 42


def test_ledger_gated(client, roles, monkeypatch):
    monkeypatch.setattr(tc._entitlements, "ledger_summary",
                        lambda a, limit=200: {"periods": [], "grand_total": 0.0})
    _login(client, ADMIN_NB)
    assert client.get("/tenant/ledger").status_code == 403
    _login(client, OWNER)
    assert client.get("/tenant/ledger").status_code == 200


# ════════════════════════════════════════════════════════════════════════════════
# 3. plan-cost redaction: capacity COUNTS for all, PRICES only for billing
# ════════════════════════════════════════════════════════════════════════════════

def _cap_report():
    return {
        "account_id": ACCT_A, "account_kind": "tenant",
        "types": [{"seat_type": "standard", "licensed": 5, "base": 5,
                   "provisioned": 0, "used": 2, "available": 3, "unit_price": 30.0}],
        "prices": {"standard": 30.0}, "base_package_price": 1500,
        "bundles": [{"bundle_id": "b1", "label": "starter", "items": [], "price": 100}],
        "total_licensed": 5, "total_used": 2,
    }


def test_capacity_prices_redacted_for_non_billing(client, roles, monkeypatch):
    monkeypatch.setattr(tc._entitlements, "capacity_report", lambda a: _cap_report())
    _login(client, ADMIN_NB)
    r = client.get("/tenant/capacity")
    assert r.status_code == 200
    body = r.json()
    # counts survive (Plan tab still works) …
    assert body["types"][0]["licensed"] == 5 and body["types"][0]["used"] == 2
    # … but every price is stripped.
    assert "unit_price" not in body["types"][0]
    assert "prices" not in body and "base_package_price" not in body
    assert body["bundles"][0].get("price") is None
    assert body["billing_visible"] is False


def test_capacity_prices_present_for_billing(client, roles, monkeypatch):
    monkeypatch.setattr(tc._entitlements, "capacity_report", lambda a: _cap_report())
    _login(client, OWNER)
    body = client.get("/tenant/capacity").json()
    assert body["types"][0]["unit_price"] == 30.0
    assert body["base_package_price"] == 1500


def test_capacity_spend_is_gated(client, roles):
    # POST /tenant/capacity incurs a charge → needs can_view_billing (403 without).
    _login(client, ADMIN_NB)
    r = client.post("/tenant/capacity", headers={"x-csrf-token": _csrf(ADMIN_NB)},
                    json={"seat_type": "standard", "quantity": 1, "signature": "Sam"})
    assert r.status_code == 403


# ════════════════════════════════════════════════════════════════════════════════
# 4. billing_contact_email round-trips (billing-gated)
# ════════════════════════════════════════════════════════════════════════════════

def test_billing_contact_round_trip(client, roles, monkeypatch):
    store = {"email": None}
    monkeypatch.setattr(saas_accounts, "get_billing_contact_email", lambda a: store["email"])

    def _set(a, email):
        norm = (email or "").strip().lower() or None
        store["email"] = norm
        return {"ok": True, "status_code": 200, "billing_contact_email": norm}
    monkeypatch.setattr(saas_accounts, "set_billing_contact_email", _set)

    _login(client, OWNER)
    # set
    r = client.put("/tenant/billing-contact", headers={"x-csrf-token": _csrf(OWNER)},
                   json={"billing_contact_email": "Finance@Acme.example"})
    assert r.status_code == 200 and r.json()["billing_contact_email"] == "finance@acme.example"
    # get reads it back
    assert client.get("/tenant/billing-contact").json()["billing_contact_email"] == "finance@acme.example"


def test_billing_contact_denied_without_permission(client, roles):
    _login(client, ADMIN_NB)
    assert client.get("/tenant/billing-contact").status_code == 403
    assert client.put("/tenant/billing-contact", headers={"x-csrf-token": _csrf(ADMIN_NB)},
                      json={"billing_contact_email": "x@y.com"}).status_code == 403


# ════════════════════════════════════════════════════════════════════════════════
# 5. owner-only grant/revoke — a non-owner cannot self-grant
# ════════════════════════════════════════════════════════════════════════════════

def test_non_owner_cannot_grant_even_with_billing(client, roles):
    # BILL_NONOWNER HAS billing but is NOT the owner → 403 on the grant control.
    _login(client, BILL_NONOWNER)
    r = client.put("/tenant/billing-access", headers={"x-csrf-token": _csrf(BILL_NONOWNER)},
                   json={"dashboard_user_id": ADMIN_NB, "can_view_billing": True})
    assert r.status_code == 403


def test_owner_can_grant(client, roles, monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "set_can_view_billing",
                        lambda acct, uid, val: (calls.append((acct, uid, val)) or True))
    _login(client, OWNER)
    r = client.put("/tenant/billing-access", headers={"x-csrf-token": _csrf(OWNER)},
                   json={"dashboard_user_id": ADMIN_NB, "can_view_billing": True})
    assert r.status_code == 200 and r.json()["can_view_billing"] is True
    # UPDATE was hard-scoped to the SESSION account (BOLA) — never a client-supplied one.
    assert calls == [(ACCT_A, ADMIN_NB, True)]


def test_owner_cannot_revoke_own_billing(client, roles, monkeypatch):
    monkeypatch.setattr(auth, "set_can_view_billing", lambda acct, uid, val: True)
    _login(client, OWNER)
    r = client.put("/tenant/billing-access", headers={"x-csrf-token": _csrf(OWNER)},
                   json={"dashboard_user_id": OWNER, "can_view_billing": False})
    assert r.status_code == 400


def test_grant_requires_csrf(client, roles):
    _login(client, OWNER)
    r = client.put("/tenant/billing-access",
                   json={"dashboard_user_id": ADMIN_NB, "can_view_billing": True})
    assert r.status_code == 403  # missing CSRF (unsafe method)
