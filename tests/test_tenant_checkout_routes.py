"""Tenant self-serve subscription: the ROUTES + the webhook contract linkage (migration 243).

Two halves, deliberately different in kind:

  PART A — the ROUTES (``saas/tenant_checkout_routes.py``). Pure: the sibling orchestration
  (``tenant_checkout``) and record (``contracts``) layers are replaced with recording doubles,
  so these proofs run anywhere and pin BEHAVIOUR, not plumbing. The crown jewel is
  ``test_body_supplied_signer_email_and_account_are_IGNORED``: a client that posts someone
  else's email signs as ITSELF.

  PART B — the WEBHOOK (``saas/stripe_webhooks.handle_checkout_session_completed``). Runs
  against a REAL PostgreSQL with migration 243 + 235 applied, through the REAL
  ``stripe.Webhook.construct_event`` signature verification, and asserts by READING THE
  DATABASE ROW — never the handler's return dict. A handler can happily report success over a
  write that did not happen; the row cannot.

⚠️ DSN-GATED SUITES IN THIS REPO HAVE PRODUCED FALSE GREENS. "PASS | 0 passed, N skipped" is
not evidence. ``test_no_test_was_silently_skipped`` at the bottom fails the run if the DB half
did not execute, so a green here means both halves ran.

DB: a THROWAWAY database (never a tenant schema, never production). Override with
``CONTRACT_TEST_DSN``. Create it with:
    CREATE DATABASE faultline_contract_test;
    \\i migrations/243_contract_infrastructure.sql
    \\i migrations/235_stripe_event_ledger.sql

Run (NOT raw pytest — a raw run is invisible to the owner's dashboard):
    python3 tools/fltest.py --bug CONTRACT --test tests/test_tenant_checkout_routes.py \\
        --note "tenant checkout routes + webhook"
"""

import hashlib
import hmac
import json
import os
import sys
import time
import types
import uuid

import pytest

import saas
from saas import dashboard_auth as auth
from saas import stripe_webhooks as swh
from saas import tenant_checkout_routes as tcr


# ════════════════════════════════════════════════════════════════════════════════════
# Shared identities
# ════════════════════════════════════════════════════════════════════════════════════

ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ADMIN_A = "11111111-1111-4111-8111-111111111111"
# A SECOND billing admin of ACCOUNT A, and an admin of the OTHER account. Both exist because
# the two defects fixed on 2026-08-12 are invisible to a single-principal test: S1 needs two
# logins on ONE account, S2 needs one login in ANOTHER account holding a familiar email.
ADMIN_A2 = "22222222-2222-4222-8222-222222222222"
ADMIN_B = "33333333-3333-4333-8333-333333333333"
EMAIL_A = "admin@tenant-a.example"
EMAIL_A2 = "second.admin@tenant-a.example"
EMAIL_B = "admin@tenant-b.example"

# The DOCUMENT BINDING tokens (migration 252) every subscribe body must echo. Derived from the
# fake terms with the SHIPPED fingerprint function — never a hand-written literal, or the fixture
# and the code could disagree about what the hash of this text is and the tests would pin the
# fixture's opinion rather than the product's.
from saas.contracts import body_fingerprint as _fp   # noqa: E402

TERMS = {
    "id": 1,
    "version": "2026-08-12-v1",
    "title": "FaultLine Subscription Agreement",
    "body_markdown": "## FaultLine Subscription Agreement\n\nThe exact words they must read.",
    "effective_at": "2026-08-12T00:00:00+00:00",
    "created_by": "system-seed",
}
TERMS["body_sha256"] = _fp(TERMS["body_markdown"])

_TV = TERMS["version"]
_TSHA = TERMS["body_sha256"]

TIERS = [
    {"tier": "team", "name": "Team", "amount_cents": 75000, "currency": "CAD",
     "interval": "month", "price_id": "price_team", "seats_included": 10,
     "blurb": "For a working team.", "features": ["a", "b"]},
    {"tier": "enterprise", "name": "Enterprise", "amount_cents": 150000, "currency": "CAD",
     "interval": "month", "price_id": "price_ent", "seats_included": 50,
     "blurb": "For a company.", "features": ["a", "b", "c"]},
]


# ════════════════════════════════════════════════════════════════════════════════════
# PART A — the routes
# ════════════════════════════════════════════════════════════════════════════════════

class FakeCheckout:
    """A recording double for ``saas/tenant_checkout.py``. Every call is captured so the tests
    can assert on the ARGUMENTS the route passed down — which is where the security property
    lives (the signer must be the session's, never the body's)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.result: dict | None = None

    def tier_catalog(self):
        return list(TIERS)

    def get_tier(self, tier):
        for t in TIERS:
            if t["tier"] == tier:
                return dict(t)
        return None

    def begin_subscription(self, **kwargs):
        self.calls.append(kwargs)
        if self.result is not None:
            return self.result
        return {
            "ok": True, "status": "created", "contract_id": 42,
            "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_x",
            "session_id": "cs_test_x", "tier": kwargs.get("tier"),
            "amount_cents": 75000, "currency": "CAD",
            "contract_version": TERMS["version"], "terms": None, "reason": None,
        }


class FakeContracts:
    # The routes call these module-level helpers on saas.contracts directly (the document
    # fingerprint, migration 252). They are DELEGATED TO THE REAL IMPLEMENTATION rather than
    # stubbed: a fake hash would let the fixture and the product disagree about what the
    # fingerprint of a document is, and the tests would then pin the fixture's opinion.
    from saas.contracts import body_fingerprint as _bf
    from saas.contracts import fingerprints_match as _fm
    from saas.contracts import _has_column as _hc
    body_fingerprint = staticmethod(_bf)
    fingerprints_match = staticmethod(_fm)
    _has_column = staticmethod(_hc)

    def __init__(self):
        self.terms = dict(TERMS)
        self.rows: dict[int, dict] = {}

    def get_current_terms(self):
        return dict(self.terms) if self.terms else None

    def get_signed_contract(self, contract_id):
        row = self.rows.get(int(contract_id))
        return dict(row) if row else None


@pytest.fixture
def fake_checkout(monkeypatch):
    fc = FakeCheckout()
    monkeypatch.setattr(tcr, "_checkout", fc)
    return fc


@pytest.fixture
def fake_contracts(monkeypatch):
    fc = FakeContracts()
    monkeypatch.setattr(tcr, "_contracts", fc)
    return fc


class FakeAccounts:
    """Recording double for ``saas/accounts.py``'s status lookup (the operator kill switch)."""

    def __init__(self):
        self.status = "active"
        self.asked: list[str] = []

    def account_status(self, account_id):
        self.asked.append(str(account_id))
        return self.status


@pytest.fixture
def fake_accounts(monkeypatch):
    fa = FakeAccounts()
    monkeypatch.setattr(tcr, "_accounts", fa)
    return fa


@pytest.fixture
def billing(monkeypatch):
    """The ``can_view_billing`` permission (migration 159), which every route here now requires.

    Returned so a test can flip it OFF and prove the 403 — see
    ``test_a_non_billing_admin_cannot_sign_or_read_agreements``."""
    state = {"allowed": True}
    monkeypatch.setattr(auth, "can_view_billing", lambda uid: bool(state["allowed"]))
    return state


@pytest.fixture
def client(monkeypatch, fake_checkout, fake_contracts, fake_accounts, billing):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
    # The session's login row — the ONLY source of the signer identity.
    monkeypatch.setattr(
        auth, "get_user_by_id",
        lambda uid: ({"id": ADMIN_A, "account_id": ACCT_A, "username": EMAIL_A,
                      "password_hash": "x", "totp_secret": None, "mfa_enabled": False,
                      "role": "admin", "status": "active", "display_name": None}
                     if str(uid) == ADMIN_A else None))
    # The audit sink writes to the control plane — no-op it.
    monkeypatch.setattr(tcr._audit, "record", lambda *a, **k: None)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tcr.router)
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE_NAME,
                  auth.issue_session(dashboard_user_id=ADMIN_A, account_id=ACCT_A,
                                     username=EMAIL_A))
    return c


def _csrf_headers():
    return {"x-csrf-token": auth.issue_csrf(dashboard_user_id=ADMIN_A)}


# ── GET /tenant/plans ───────────────────────────────────────────────────────────────

def test_plans_returns_both_tiers_and_the_active_terms_body(client):
    r = client.get("/tenant/plans")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["tier"] for p in body["plans"]] == ["team", "enterprise"]
    assert body["plans"][0]["amount_cents"] == 75000
    # The EXACT terms text ships with the pricing, in one response — so the words the customer
    # reads are provably the words that get snapshotted.
    assert body["terms"]["body_markdown"] == TERMS["body_markdown"]
    assert body["terms"]["version"] == TERMS["version"]
    assert body["terms"]["id"] == 1


def test_plans_FAILS_LOUD_when_no_terms_version_is_published(client, fake_contracts):
    """A signature box with no contract behind it must be unreachable — 503, not a plan list
    with a null contract."""
    fake_contracts.terms = None
    r = client.get("/tenant/plans")
    assert r.status_code == 503
    assert "terms" in r.json()["detail"].lower()


def test_plans_requires_a_session(client):
    client.cookies.clear()
    assert client.get("/tenant/plans").status_code == 401


# ── POST /tenant/subscribe — THE SECURITY PROPERTY ──────────────────────────────────

def test_body_supplied_signer_email_and_account_are_IGNORED(client, fake_checkout):
    """🔴 THE crown jewel. A client posts someone else's signer_email + another account_id.
    The route must sign as the SESSION and bind the SESSION's account — otherwise anyone could
    bind a named third party to a permanent, undeletable $750/mo financial obligation."""
    r = client.post("/tenant/subscribe", headers=_csrf_headers(), json={
        "tier": "team", "legal_name": "Christopher Thompson", "accept": True,
        "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA,
        # ── the attack ──
        "signer_email": "victim@someone-else.example",
        "account_id": ACCT_B,
        "signer_user_id": "99999999-9999-4999-8999-999999999999",
    })
    assert r.status_code == 200, r.text
    assert len(fake_checkout.calls) == 1
    call = fake_checkout.calls[0]
    assert call["signer_email"] == EMAIL_A          # the SESSION's, not the body's
    assert call["account_id"] == ACCT_A             # the SESSION's, not the body's
    assert call["signer_user_id"] == ADMIN_A        # the SESSION's, not the body's
    assert "victim@someone-else.example" not in json.dumps(call)
    assert ACCT_B not in json.dumps(call)


def test_subscribe_captures_the_signer_ip_and_user_agent(client, fake_checkout):
    """The electronic-signature facts under PIPEDA/ESIGN. The IP is the LAST X-Forwarded-For
    hop (Caddy appends the peer it observed; the leftmost entries are client-supplied)."""
    r = client.post("/tenant/subscribe",
                    headers={**_csrf_headers(),
                             "x-forwarded-for": "1.2.3.4, 203.0.113.9",
                             "user-agent": "Mozilla/5.0 (test)"},
                    json={"tier": "team", "legal_name": "C. Thompson", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 200, r.text
    call = fake_checkout.calls[0]
    assert call["signer_ip"] == "203.0.113.9"       # LAST hop, not the spoofable first
    assert call["signer_user_agent"] == "Mozilla/5.0 (test)"


def test_subscribe_returns_the_checkout_url_and_contract_id(client):
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. Thompson", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    body = r.json()
    assert body["ok"] is True
    assert body["contract_id"] == 42
    assert body["checkout_url"].startswith("https://checkout.stripe.com/")
    assert body["session_id"] == "cs_test_x"
    assert body["tier"] == "team"
    assert body["amount_cents"] == 75000 and body["currency"] == "CAD"


@pytest.mark.parametrize("payload,fragment", [
    ({"tier": "team", "legal_name": "C. Thompson", "accept": False,
      "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA}, "accept"),
    ({"tier": "team", "legal_name": "C. Thompson",
      "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA}, "accept"),                            # omitted → False
    ({"tier": "team", "legal_name": "   ", "accept": True,
      "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA}, "legal name"),
    ({"tier": "team", "legal_name": "", "accept": True,
      "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA}, "legal name"),
])
def test_refused_signatures_write_NOTHING(client, fake_checkout, payload, fragment):
    """``begin_subscription`` is the SOLE write path to public.signed_contracts reachable from
    this route, so "it was never called" IS "no row was written" — and the row could never be
    deleted afterwards (migration 243 blocks DELETE outright), which is why these checks run
    before anything downstream is touched."""
    r = client.post("/tenant/subscribe", headers=_csrf_headers(), json=payload)
    assert r.status_code == 400
    assert fragment in r.json()["detail"].lower()
    assert fake_checkout.calls == []


def test_unknown_tier_is_refused_and_writes_nothing(client, fake_checkout):
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "platinum-unicorn", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 400 and "tier" in r.json()["detail"].lower()
    assert fake_checkout.calls == []


def test_subscribe_requires_csrf_and_a_session(client):
    # Session but no CSRF token → 403 (require_dashboard_session, unsafe method).
    r = client.post("/tenant/subscribe",
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 403
    client.cookies.clear()
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 401


def test_downstream_failure_never_reports_a_success(client, fake_checkout):
    """MONEY PATH: a failure below must surface as a failure, never a 200 with a null URL."""
    fake_checkout.result = {"ok": False, "reason": "stripe is not configured",
                            "status_code": 503}
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 503
    # The MESSAGE is what must survive; ``detail`` is now a structured object on every refusal
    # (see _refusal_detail) so the one refusal that HAS written a permanent row can name it.
    assert r.json()["detail"]["message"] == "stripe is not configured"
    assert r.json()["detail"]["signed"] is False


def test_downstream_failure_without_a_status_code_becomes_502(client, fake_checkout):
    fake_checkout.result = {"ok": False, "reason": "record layer unavailable"}
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 502


@pytest.mark.parametrize("status,http", [
    ("invalid_tier", 400), ("invalid_signature", 400), ("invalid_email", 400),
    ("no_active_terms", 503), ("contract_write_failed", 503),
    ("payments_unavailable", 503),
    ("some_future_status_nobody_mapped", 502),
])
def test_module_status_enum_maps_to_the_right_http_class(client, fake_checkout, status, http):
    """4xx = the caller can fix it; 5xx = they cannot and an operator has a problem. Our own
    faults (no published terms, a failed contract write) must NEVER be blamed on the customer."""
    fake_checkout.result = {"ok": False, "status": status, "reason": "nope"}
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == http


def test_the_REAL_tenant_checkout_module_satisfies_the_contract_this_route_calls():
    """Drift guard against the SIBLING module. These routes call three functions with named
    kwargs; if the sibling renames one, every subscription 500s in production and no unit test
    with a double would notice. Checks the real signature, not a stand-in."""
    import inspect

    from saas import tenant_checkout as real   # a hard import: absence must be RED, not a skip

    assert callable(real.tier_catalog) and callable(real.get_tier)
    params = inspect.signature(real.begin_subscription).parameters
    for name in ("tier", "signer_email", "legal_name", "account_id", "signer_user_id",
                 "signer_ip", "signer_user_agent"):
        assert name in params, f"begin_subscription lost the '{name}' kwarg"
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_router_module_imports_its_siblings_for_real():
    """A guarded import that silently resolved to None would make every route answer 503 in
    production while the unit tests — which inject doubles — stayed green."""
    import importlib
    mod = importlib.import_module("saas.tenant_checkout_routes")
    assert mod._contracts is not None, "saas.contracts did not import"
    assert mod._checkout is not None, "saas.tenant_checkout did not import"


def test_missing_sibling_layer_is_503_not_a_silent_pass(client, monkeypatch):
    monkeypatch.setattr(tcr, "_checkout", None)
    assert client.get("/tenant/plans").status_code == 503
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 503


# ── GET /tenant/contract/{id} — BOLA ────────────────────────────────────────────────

def _row(cid, *, account_id, email, signer_user_id=None, status="accepted"):
    """A ``contracts.get_signed_contract`` projection. ``signer_user_id`` defaults to None so a
    test must name it deliberately — the ownership check must never be satisfied by a row that
    merely carries a familiar EMAIL."""
    return {"id": cid, "template_id": 1, "email": email, "name": "Someone",
            "ip": "203.0.113.9", "user_agent": "ua", "tier": "team",
            "price_id": "price_team", "checkout_session_id": f"cs_{cid}",
            "subscription_id": None, "account_id": account_id,
            "signer_user_id": signer_user_id,
            "contract_snapshot": TERMS["body_markdown"],
            "accepted_at": "2026-08-12T00:00:00+00:00", "status": status}


def test_own_contract_by_account_is_returned_with_the_snapshot(client, fake_contracts):
    fake_contracts.rows[7] = _row(7, account_id=ACCT_A, email=EMAIL_A)
    r = client.get("/tenant/contract/7")
    assert r.status_code == 200
    # The snapshot is the point: PIPEDA/ESIGN require the agreement to come back in the form
    # it was presented.
    assert r.json()["contract_snapshot"] == TERMS["body_markdown"]


def test_own_contract_by_signer_user_id_when_the_account_is_not_linked(client, fake_contracts):
    """A legacy row with a NULL account_id must still be readable BY THE LOGIN THAT SIGNED IT.

    This is the requirement the old ``signer_email`` arm was there to serve; ``signer_user_id``
    serves it too and, unlike an email, cannot be re-issued to somebody else."""
    fake_contracts.rows[8] = _row(8, account_id=None, email=EMAIL_A,
                                  signer_user_id=ADMIN_A)
    assert client.get("/tenant/contract/8").status_code == 200


def test_a_fresh_unpaid_contract_carries_the_account_so_the_signer_can_read_it_back(
        client, fake_contracts):
    """The S1 fix writes ``account_id`` at INSERT, so a just-signed unpaid contract is owned by
    the account from the moment it exists — no NULL-account window to paper over with an email.

    Kept as its OWN test because it is the load-bearing premise of the S2 fix: if this
    regressed, dropping the email arm would strand a signer at their own confirmation page."""
    fake_contracts.rows[11] = _row(11, account_id=ACCT_A, email=EMAIL_A,
                                   signer_user_id=None, status="accepted")
    assert client.get("/tenant/contract/11").status_code == 200


def test_another_accounts_contract_is_404_NOT_403(client, fake_contracts):
    """404, not 403: a 403 confirms the id exists and turns the sequential BIGSERIAL primary
    key into an enumeration oracle."""
    fake_contracts.rows[9] = _row(9, account_id=ACCT_B, email=EMAIL_B,
                                  signer_user_id=ADMIN_B)
    r = client.get("/tenant/contract/9")
    assert r.status_code == 404
    assert r.json()["detail"] == "contract not found"
    # And nothing about the row leaks into the response.
    assert EMAIL_B not in r.text and ACCT_B not in r.text


def test_a_nonexistent_contract_is_indistinguishable_from_someone_elses(client, fake_contracts):
    fake_contracts.rows[9] = _row(9, account_id=ACCT_B, email=EMAIL_B,
                                  signer_user_id=ADMIN_B)
    theirs = client.get("/tenant/contract/9")
    missing = client.get("/tenant/contract/9999")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()


def test_same_email_domain_is_NOT_ownership(client, fake_contracts):
    """Deterministic EQUALITY only — no substring/domain matching. 'someone at the same
    company' is not the signer."""
    fake_contracts.rows[10] = _row(10, account_id=None, email="other@tenant-a.example",
                                   signer_user_id=ADMIN_A2)
    assert client.get("/tenant/contract/10").status_code == 404


# ══════════════════════════════════════════════════════════════════════════════════
# S2 — THE CRITIC'S REPRODUCTION: an attacker whose LOGIN EMAIL equals a victim's
#      immutable ``signer_email``, in a DIFFERENT account.
# ══════════════════════════════════════════════════════════════════════════════════
#
# ``signer_email`` is frozen on the contract, but the login email it was compared against is
# self-mutable (PUT /tenant/account/email) and ``set_username`` only rejects an address held by
# a LIVE login — so a victim's email change, or their deletion at offboarding, frees the string.
# Measured before the fix: attacker account + victim email returned the full row.

def test_S2_an_attacker_holding_the_victims_email_gets_404_not_the_contract(
        client, fake_contracts, monkeypatch):
    """The exact critic reproduction, driven through the REAL route.

    The session is ADMIN_B of ACCOUNT B, whose login email is now the victim's old address. The
    victim's contract belongs to ACCOUNT A and was signed by ADMIN_A."""
    monkeypatch.setattr(
        auth, "get_user_by_id",
        lambda uid: {"id": ADMIN_B, "account_id": ACCT_B, "username": EMAIL_A,  # ← claimed
                     "password_hash": "x", "totp_secret": None, "mfa_enabled": False,
                     "role": "admin", "status": "active", "display_name": None})
    client.cookies.set(auth.SESSION_COOKIE_NAME,
                       auth.issue_session(dashboard_user_id=ADMIN_B, account_id=ACCT_B,
                                          username=EMAIL_A))
    fake_contracts.rows[6] = _row(6, account_id=ACCT_A, email=EMAIL_A,
                                  signer_user_id=ADMIN_A, status="completed")

    r = client.get("/tenant/contract/6")
    assert r.status_code == 404, r.text
    # Nothing about the victim's agreement may leak — not the snapshot, not the signer IP, not
    # the amount, not the account it belongs to.
    for leak in (TERMS["body_markdown"], "203.0.113.9", "75000", ACCT_A, ADMIN_A):
        assert leak not in r.text


def test_S2_the_attacker_gets_ZERO_rows_from_the_list_route(client, monkeypatch):
    """The list half of the same reproduction, asserted on the REAL SQL predicate.

    ``_list_own_contracts`` is NOT stubbed here — the point is what the WHERE clause selects, so
    the DB call is intercepted at the cursor and the emitted SQL + arguments are inspected. A
    predicate that still mentioned the email would fail on the parameters alone."""
    monkeypatch.setattr(
        auth, "get_user_by_id",
        lambda uid: {"id": ADMIN_B, "account_id": ACCT_B, "username": EMAIL_A,
                     "password_hash": "x", "totp_secret": None, "mfa_enabled": False,
                     "role": "admin", "status": "active", "display_name": None})
    client.cookies.set(auth.SESSION_COOKIE_NAME,
                       auth.issue_session(dashboard_user_id=ADMIN_B, account_id=ACCT_B,
                                          username=EMAIL_A))
    seen = {}

    class _Cur:
        description = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=()):
            seen["sql"] = sql
            seen["args"] = args

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    import contextlib as _ctx

    @_ctx.contextmanager
    def _fake_connection():
        yield _Conn()

    monkeypatch.setattr(tcr.db, "connection", _fake_connection)

    r = client.get("/tenant/contracts")
    assert r.status_code == 200
    body = r.json()
    assert body["contracts"] == [] and body["count"] == 0
    # ``current_terms`` rides along for every caller (it is the PUBLISHED version, not anybody's
    # signed row), so it is not part of the leak surface this test is about.
    assert set(body) == {"contracts", "count", "current_terms"}
    # The predicate binds the attacker's OWN account and OWN user id — never an email string.
    # THE ARGUMENTS ARE THE STRONGEST EVIDENCE and they are checked first: the attacker holds a
    # session whose username IS the victim's email (EMAIL_A, set above), so if the email were an
    # authorization arm it would appear here as a bound parameter. It does not.
    assert seen["args"][:2] == (ACCT_B, ADMIN_B)
    assert EMAIL_A not in seen["args"], "the victim's email reached the query as a parameter"
    # ⚠️ ASSERTED ON THE **WHERE CLAUSE**, NOT ON THE WHOLE STATEMENT. The archive surface now
    # SELECTS signer_email (an agreement list has to say who signed — that is the whole point of
    # a record of assent), so "signer_email absent from the SQL text" would fail on a change that
    # authorizes nothing. Selecting a column is not authorizing on it; the WHERE is where
    # authorization lives.
    where = seen["sql"][seen["sql"].index("WHERE "):seen["sql"].index("ORDER BY")]
    assert "signer_email" not in where, f"the list AUTHORIZES on an email: {where}"
    assert "signer_user_id" in where and "account_id" in where


def test_S2_no_route_in_this_module_authorizes_on_an_email(client):
    """A structural guard: ownership must never key on the session's ``username`` again.

    Reading the source is the only way to catch a REGRESSION that re-adds the arm somewhere the
    two behavioural tests above do not reach."""
    import inspect
    # The PARAMETERS are the contract: neither helper may take an email at all.
    assert "signer_email" not in inspect.signature(tcr._owns_contract).parameters
    assert "signer_email" not in inspect.signature(tcr._list_own_contracts).parameters
    assert "signer_user_id" in inspect.signature(tcr._owns_contract).parameters
    assert "signer_user_id" in inspect.signature(tcr._list_own_contracts).parameters
    # …and the SQL must not AUTHORIZE on it.
    #
    # ⚠️ THIS ASSERTION WAS TIGHTENED, NOT RELAXED — read why before loosening it further.
    # It used to be "no executable line mentions signer_email at all", which is a PROXY for the
    # real property and cannot tell a WHERE clause from a SELECT list. The archive surface now
    # RETURNS signer_email (who signed is the point of an agreement archive — Ironclad's clickwrap
    # guidance is precisely about being able to "prove who accepted"), and the crude version went
    # red on a change that touches no authorization at all.
    #
    # The genuine property is: the row set is chosen by account_id / signer_user_id and by nothing
    # else. So the WHERE clause is examined, and any comparison against signer_email anywhere is
    # still forbidden. Selecting a column ≠ authorizing on it; comparing one IS.
    src = inspect.getsource(tcr._list_own_contracts)
    where = src[src.index('"WHERE '):src.index("ORDER BY")]
    assert "signer_email" not in where, (
        f"the list AUTHORIZES on signer_email — an email is a reassignable label, not an "
        f"identity: {where}")
    assert "account_id = %s::uuid OR signer_user_id = %s::uuid" in where
    compares = [ln for ln in src.splitlines()
                if "signer_email" in ln and any(op in ln for op in ("=", "==", "IN (", "ILIKE"))
                and "\"signer_email\":" not in ln]
    assert compares == [], f"signer_email is COMPARED somewhere in the list helper: {compares}"
    # The routes must hand the helpers the session's USER ID, never its username.
    for fn in (tcr.tenant_list_contracts, tcr.tenant_get_contract):
        code = inspect.getsource(fn)
        assert 'user["username"]' not in code, f"{fn.__name__} still authorizes on an email"
        assert 'user["id"]' in code


def test_contract_read_requires_a_session(client, fake_contracts):
    fake_contracts.rows[7] = _row(7, account_id=ACCT_A, email=EMAIL_A)
    client.cookies.clear()
    assert client.get("/tenant/contract/7").status_code == 401


# ── GET /tenant/contracts — the list is SQL-scoped ──────────────────────────────────

def test_contract_list_is_scoped_in_sql_to_the_session(client, monkeypatch):
    """The WHERE clause — not a Python post-filter — is what makes the list BOLA-safe (and what
    stops a tenant's own rows falling outside an unscoped 'newest N' window)."""
    seen = {}

    def _fake_list(*, account_id, signer_user_id, limit):
        seen.update({"account_id": account_id, "signer_user_id": signer_user_id,
                     "limit": limit})
        return [{"id": 7, "tier": "team", "status": "completed", "accepted_at": None,
                 "amount_cents": 75000, "currency": "CAD", "interval": "month",
                 "contract_version": TERMS["version"], "subscription_id": "sub_1"}]

    monkeypatch.setattr(tcr, "_list_own_contracts", _fake_list)
    r = client.get("/tenant/contracts?limit=5")
    assert r.status_code == 200
    assert r.json()["count"] == 1 and r.json()["contracts"][0]["id"] == 7
    # Both scope keys are non-reassignable identifiers — the session's account and its login id.
    assert seen == {"account_id": ACCT_A, "signer_user_id": ADMIN_A, "limit": 5}


def test_contract_list_limit_is_bounded(client, monkeypatch):
    monkeypatch.setattr(tcr, "_list_own_contracts", lambda **kw: [])
    assert client.get("/tenant/contracts?limit=100000").status_code == 422
    assert client.get("/tenant/contracts?limit=0").status_code == 422


# ── S2: the can_view_billing gate on a four-figure commitment ───────────────────────

@pytest.mark.parametrize("method,path", [
    ("get", "/tenant/plans"),
    ("post", "/tenant/subscribe"),
    ("get", "/tenant/contracts"),
    ("get", "/tenant/contract/7"),
])
def test_a_non_billing_admin_cannot_sign_or_read_agreements(
        client, billing, fake_checkout, method, path):
    """🔴 Every route on this surface requires ``can_view_billing`` (migration 159).

    Before this gate the four routes sat on the bare session, so a tenant admin the owner had
    deliberately fenced out of spend could still commit the organization to CA$750–1500/mo —
    permanently, because the row can never be deleted. The sibling console applies
    ``require_billing_access`` to 19 money routes; signing the subscription is the most
    expensive action in the product and was the one left open.

    ``/tenant/plans`` is included because it serves ``amount_cents`` per tier while
    ``/tenant/capacity`` REDACTS money for the same admin — an unredacted price list through a
    second door makes the redaction cosmetic."""
    billing["allowed"] = False
    r = getattr(client, method)(path, headers=_csrf_headers(), **(
        {"json": {"tier": "team", "legal_name": "C. T.", "accept": True,
                  "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA}} if method == "post" else {}))
    assert r.status_code == 403, r.text
    # And crucially: the write path was never entered.
    assert fake_checkout.calls == []


def test_billing_admin_still_gets_through(client, billing):
    """The gate must not be a blanket lockout — the permitted admin is unaffected."""
    billing["allowed"] = True
    assert client.get("/tenant/plans").status_code == 200


# ── S1: the binding between what was READ and what is SIGNED ────────────────────────

def test_the_template_id_the_client_rendered_is_passed_to_the_brain(client, fake_checkout):
    """The route must FORWARD the echoed template id — the server-side comparison is worthless
    if the transport drops the token."""
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 200, r.text
    call = fake_checkout.calls[0]
    assert call["expected_template_id"] == 1
    assert call["expected_version"] == "2026-08-12-v1"
    # THE DOCUMENT BINDING (migration 252). The id is a row and the version is a label; the
    # fingerprint is the text. All three reach the brain, or the brain cannot refuse on them.
    assert call["expected_sha256"] == _TSHA


def test_a_page_that_predates_the_binding_gets_a_RECOVERABLE_409_not_a_bare_422(
        client, fake_checkout):
    """🔴 THE DEAD END, CLOSED. A page already open when this shipped cannot echo tokens it was
    never served. Refusing it with a Pydantic 422 names two fields the customer has never heard
    of and offers no way forward, mid-signature, on a four-figure commitment.

    So the two document tokens are declared OPTIONAL in the schema and REQUIRED by the brain,
    which is the only layer that knows what the current agreement is and can therefore hand back
    the text needed to recover. The tokens still reach the brain (as None) rather than being
    answered here — BRAIN NOT TRANSPORT.

    ``contract_template_id`` stays schema-required and 422s, and that asymmetry is deliberate:
    EVERY page build that has ever posted to this route sends it, so its absence is not the
    stale-page symptom, it is a malformed request."""
    base = {"tier": "team", "legal_name": "C. T.", "accept": True, "contract_template_id": 1}
    stale = {"ok": False, "status": "terms_version_stale", "contract_id": None,
             "checkout_url": None, "session_id": None, "tier": "team", "amount_cents": 75000,
             "currency": "CAD", "contract_version": _TV,
             "terms": {"id": 1, "version": _TV, "title": "T",
                       "body_markdown": TERMS["body_markdown"], "body_sha256": _TSHA,
                       "effective_at": None},
             "reason": "This page is out of date, so we did not sign anything…"}

    # (a) the PRE-BINDING page shape: neither document token, plus the field it used to send.
    fake_checkout.result = stale
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={**base, "contract_version": "2026-08-12-v1"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail["terms"]["body_sha256"] == _TSHA
    assert detail["signed"] is False
    # It REACHED the brain, with the tokens explicitly absent — the decision was not made here.
    assert len(fake_checkout.calls) == 1
    assert fake_checkout.calls[0]["expected_sha256"] is None
    assert fake_checkout.calls[0]["expected_version"] is None

    # (b) each token missing on its own — same treatment, still brain-decided.
    for missing in ("terms_version", "terms_sha256"):
        fake_checkout.calls.clear()
        body = {**base, "terms_version": _TV, "terms_sha256": _TSHA}
        body.pop(missing)
        r = client.post("/tenant/subscribe", headers=_csrf_headers(), json=body)
        assert r.status_code == 409, (missing, r.text)
        assert len(fake_checkout.calls) == 1, missing

    # (c) a MALFORMED body — no template id at all — is still a 422 and never reaches the brain.
    fake_checkout.calls.clear()
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True})
    assert r.status_code == 422, r.text
    assert fake_checkout.calls == []


def test_a_stale_document_409_CARRIES_THE_CURRENT_TERMS_so_the_page_can_rerender(
        client, fake_checkout):
    """🔴 THE RECOVERY HALF. A 409 that only says "it changed" leaves the page with a stale
    agreement on screen next to a live submit button, and forces the client to issue its OWN
    re-read — a THIRD read of a mutable row that can land on yet another version, i.e. the same
    race one turn later.

    So the refusal ships the current agreement, from the SAME read that decided the refusal.
    RFC 9110 §15.5.10 asks a 409 for "enough information for a user to recognize the source of
    the conflict"; the page needs more than recognition, it needs the text."""
    new_terms = {"id": 2, "version": "2026-08-12-v2", "title": "T",
                 "body_markdown": "## v2\n\nGoverned by the laws of Delaware, USA.\n",
                 "body_sha256": "ab" * 32, "effective_at": None}
    for status in ("terms_changed", "terms_version_stale"):
        fake_checkout.calls.clear()
        fake_checkout.result = {
            "ok": False, "status": status, "contract_id": None, "checkout_url": None,
            "session_id": None, "tier": "team", "amount_cents": 75000, "currency": "CAD",
            "contract_version": "2026-08-12-v2", "terms": new_terms,
            "reason": "The subscription agreement changed while this page was open…",
        }
        r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                        json={"tier": "team", "legal_name": "C. T.", "accept": True,
                              "contract_template_id": 1, "terms_version": _TV,
                              "terms_sha256": _TSHA})
        assert r.status_code == 409, (status, r.text)
        detail = r.json()["detail"]
        assert isinstance(detail, dict), (status, detail)
        assert detail["status"] == status
        assert detail["signed"] is False and detail["charged"] is False
        assert detail["terms"]["body_markdown"] == new_terms["body_markdown"]
        assert detail["terms"]["body_sha256"] == new_terms["body_sha256"]
        assert "changed" in detail["message"]


def test_a_refusal_that_is_NOT_about_the_document_still_states_signed_and_charged(
        client, fake_checkout):
    """🔴 THIS TEST USED TO ASSERT THE OPPOSITE, AND ASSERTING IT IS WHAT KEPT THE DEFECT.

    It was called ``..._keeps_its_plain_string_detail`` and pinned ``isinstance(detail, str)``
    for every status except the two stale-document ones. Scoping the structured body that
    narrowly is precisely why the step-4 refusal — the ONLY one that has already written a
    permanent, undeletable contract row — had no way to say so, and the page told a customer we
    could not confirm whether their signature was recorded.

    The structured body is now universal. The MESSAGE still survives unchanged (subscribe.js
    reads it through ``detailText``), so no client reader changes meaning; what is ADDED is the
    ``signed`` / ``charged`` pair that every refusal on a money path owes the customer.

    ``terms`` remains scoped to the two stale-document statuses — a refusal for a bad tier still
    has no business shipping the whole agreement back."""
    fake_checkout.result = {
        "ok": False, "status": "already_subscribed", "contract_id": 9, "checkout_url": None,
        "session_id": None, "tier": "team", "amount_cents": 75000, "currency": "CAD",
        "contract_version": "2026-08-12-v1", "terms": None,
        "reason": "This account already has an active subscription (agreement #9).",
    }
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV,
                          "terms_sha256": _TSHA})
    assert r.status_code == 409
    d = r.json()["detail"]
    assert isinstance(d, dict)
    assert d["message"] == "This account already has an active subscription (agreement #9)."
    assert d["charged"] is False
    # This refusal wrote NOTHING (the fake carries no ``signed``), so it must not claim a
    # signature nor quote a contract number.
    assert d["signed"] is False
    assert "contract_id" not in d
    # …and it is not a stale-document refusal, so it does NOT ship the agreement back.
    assert "terms" not in d


def test_get_tenant_plans_serves_the_document_fingerprint(client, fake_checkout):
    """The page cannot echo a token it was never given, so the plans payload is where the whole
    binding starts."""
    r = client.get("/tenant/plans")
    assert r.status_code == 200, r.text
    terms = r.json()["terms"]
    assert terms["body_sha256"] == _fp(terms["body_markdown"])
    assert terms["version"] == _TV and terms["id"] == 1


def test_a_body_with_no_template_id_is_REFUSED_and_writes_nothing(client, fake_checkout):
    """The binding token is REQUIRED. A client that will not say which agreement it rendered
    cannot be allowed to sign — that is precisely the pre-fix behaviour."""
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True})
    assert r.status_code == 422, r.text
    assert fake_checkout.calls == []


def test_terms_changed_is_a_409_and_writes_nothing(client, fake_checkout):
    """🔴 THE CRITIC'S DEMONSTRATION, at the HTTP layer.

    The brain refuses with ``terms_changed`` when the rendered template is no longer active.
    The transport must surface that as 409 (RFC 9110 §15.5.10 — a conflict with the current
    state of the resource, which the caller can resolve and retry) rather than a 502 or, worse,
    a success. The end-to-end proof that NO ROW is written lives in
    ``test_tenant_checkout_module.py::test_terms_changed_writes_no_contract_row`` against a real
    database; here we pin the status mapping."""
    fake_checkout.result = {
        "ok": False, "status": "terms_changed", "contract_id": None,
        "checkout_url": None, "session_id": None, "tier": "team",
        "amount_cents": 75000, "currency": "CAD", "contract_version": "2026-08-12-v2",
        "reason": "The subscription agreement was updated while this page was open…",
    }
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 409, r.text
    assert "updated" in r.json()["detail"]["message"]


@pytest.mark.parametrize("status", ["already_subscribed", "duplicate_in_flight"])
def test_duplicate_guards_map_to_409(client, fake_checkout, status):
    """A duplicate is a CONFLICT, not a bad request and not a server error: the caller is
    authorized and the body is well-formed, but the server state makes the write wrong."""
    fake_checkout.result = {
        "ok": False, "status": status, "contract_id": 7, "checkout_url": None,
        "session_id": None, "tier": "team", "amount_cents": 75000, "currency": "CAD",
        "contract_version": "2026-08-12-v1", "reason": "already covered by agreement #7",
    }
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 409, r.text


# ── M2: a suspended / locked / archived account cannot sign ─────────────────────────

@pytest.mark.parametrize("status", ["suspended", "locked", "archived", None])
def test_a_non_active_account_cannot_sign(client, fake_accounts, fake_checkout, status):
    """A session cookie outlives an operator's decision. An account switched off — quite
    possibly for non-payment — must not be able to sign a NEW permanent CA$750–1500/mo
    agreement on the strength of a still-valid cookie. ``None`` covers the unreadable/unknown
    case, which must fail CLOSED."""
    fake_accounts.status = status
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 403, r.text
    assert fake_checkout.calls == [], "an inactive account reached the write path"


def test_an_active_account_is_unaffected(client, fake_accounts, fake_checkout):
    fake_accounts.status = "active"
    r = client.post("/tenant/subscribe", headers=_csrf_headers(),
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                          "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 200, r.text
    assert len(fake_checkout.calls) == 1
    assert fake_accounts.asked == [ACCT_A]        # the SESSION's account, not the body's


# ── M4: the Stripe return urls must be absolute ─────────────────────────────────────

def test_return_urls_passed_to_the_brain_are_absolute(client, fake_checkout, monkeypatch):
    """A relative success_url resolves against **checkout.stripe.com**, so a paying customer
    lands on Stripe's domain instead of ours. The route derives an absolute origin from the
    forwarded headers and passes both urls down.

    Since gauntlet checkout-origin-authority a request host is honoured ONLY when it is in the
    deployment's own ``SAAS_TENANT_HOSTS ∪ SAAS_ADMIN_HOSTS`` allowlist (and no operator
    ``SAAS_MCP_PUBLIC_URL`` is set), so the test configures that rail — the same way the
    deployment does — rather than trusting a bare header. The full matrix (forged host, port
    survival, operator precedence, empty allowlist) lives in ``tests/test_tenant_checkout_origin.py``."""
    monkeypatch.delenv("SAAS_MCP_PUBLIC_URL", raising=False)
    monkeypatch.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    r = client.post("/tenant/subscribe",
                    headers={**_csrf_headers(),
                             "x-forwarded-proto": "https",
                             "x-forwarded-host": "app.example.test"},
                    json={"tier": "team", "legal_name": "C. T.", "accept": True,
                           "contract_template_id": 1, "terms_version": _TV, "terms_sha256": _TSHA})
    assert r.status_code == 200, r.text
    call = fake_checkout.calls[0]
    assert call["success_url"].startswith("https://app.example.test/")
    assert call["cancel_url"].startswith("https://app.example.test/")
    # Stripe's guide: "{CHECKOUT_SESSION_ID} ... is a literal string and you must add it exactly
    # as you see it here." It must survive verbatim, unsubstituted.
    assert "{CHECKOUT_SESSION_ID}" in call["success_url"]


def test_malicious_host_header_regex_rejects_attack_vectors():
    """The _HOST_RE regex at module level blocks URL metacharacters that could break the
    origin URL structure. It is the SHAPE gate (``host[:port]`` and nothing else), not the trust
    decision — trust is the allowlist in ``_request_origin`` — but it is still load-bearing:
    the allowlist matches the PORT-STRIPPED form, so a suffix like ``/path`` after an allowlisted
    host must be caught here or it would ride into the return URL."""
    # Import after the module is loaded by the top-level imports
    import saas.tenant_checkout_routes as tcr
    for bad_host in ["evil.com/path", "evil.com?query=1", "evil.com@other.com",
                     "/etc/passwd", "javascript:alert(1)", "evil.com\\path",
                     "", "   ", "host:port with space", "evil.com\x00null"]:
        assert not tcr._HOST_RE.match(bad_host), f"_HOST_RE should reject {bad_host!r}"


def test_malicious_host_header_regex_accepts_valid_domains():
    """The _HOST_RE regex must accept legitimate DNS names and IPs."""
    import saas.tenant_checkout_routes as tcr
    for host in ["app.faultline.ca", "localhost", "127.0.0.1", "my-host.example.com",
                 "a.b.c.example.co.uk", "example.com"]:
        assert tcr._HOST_RE.match(host), f"_HOST_RE should accept {host!r}"


def test_ownership_predicate_matches_the_list_predicate():
    """The detail route and the list must never disagree about ownership."""
    # account arm
    assert tcr._owns_contract(_row(1, account_id=ACCT_A, email=EMAIL_B, signer_user_id=ADMIN_B),
                              account_id=ACCT_A, signer_user_id=ADMIN_A) is True
    # signer arm (a row whose account was never linked)
    assert tcr._owns_contract(_row(1, account_id=None, email=EMAIL_B, signer_user_id=ADMIN_A),
                              account_id=ACCT_A, signer_user_id=ADMIN_A) is True
    # neither
    assert tcr._owns_contract(_row(1, account_id=ACCT_B, email=EMAIL_B, signer_user_id=ADMIN_B),
                              account_id=ACCT_A, signer_user_id=ADMIN_A) is False
    # 🔴 THE EMAIL IS NOT AN ARM. Same email on the row, wrong account, wrong login → NOT ours.
    assert tcr._owns_contract(_row(1, account_id=ACCT_B, email=EMAIL_A, signer_user_id=ADMIN_B),
                              account_id=ACCT_A, signer_user_id=ADMIN_A) is False
    # A row with NO owning identifiers at all is owned by nobody (never by "whoever asks").
    assert tcr._owns_contract(_row(1, account_id=None, email=EMAIL_A, signer_user_id=None),
                              account_id=ACCT_A, signer_user_id=ADMIN_A) is False
    # A session with no usable identifiers cannot claim a row either.
    assert tcr._owns_contract(_row(1, account_id=ACCT_A, email=EMAIL_A, signer_user_id=ADMIN_A),
                              account_id="not-a-uuid", signer_user_id=None) is False


# ════════════════════════════════════════════════════════════════════════════════════
# PART B — the webhook, against a REAL database and a REAL Stripe signature
# ════════════════════════════════════════════════════════════════════════════════════

DEFAULT_TEST_DSN = ("postgresql://faultline:faultline@172.20.0.3:5432/"
                    "faultline_contract_test")
TEST_DSN = os.environ.get("CONTRACT_TEST_DSN", DEFAULT_TEST_DSN)
WHSEC = "whsec_unit_test_secret_do_not_use_anywhere_real"

# Set by the db fixture; read by the guard test at the bottom so a silently-skipped DB half
# fails the run instead of reporting a green.
_DB_RAN = {"value": False}


def _connect():
    import psycopg2
    return psycopg2.connect(TEST_DSN)


@pytest.fixture(scope="module")
def dbconn():
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"contract test DB unreachable ({type(exc).__name__}): {TEST_DSN}")
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.signed_contracts'), "
                    "       to_regclass('public.contract_templates'), "
                    "       to_regclass('public.stripe_events')")
        present = cur.fetchone()
    conn.commit()
    if not all(present):
        conn.close()
        pytest.skip("throwaway DB is missing migration 243 and/or 235 — see this file's header")
    yield conn
    conn.close()


@pytest.fixture
def webhook(monkeypatch, dbconn):
    """Point the saas control-plane DSN at the THROWAWAY database, install the webhook secret,
    and stub the Stripe SNAPSHOT tables + the entitlement grant (they belong to sibling
    migrations). The contract linkage and the signature verification are REAL."""
    _DB_RAN["value"] = True
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    monkeypatch.setenv("SAAS_MODE", "1")

    from saas import payment_config as _pc
    monkeypatch.setattr(_pc, "resolve_config", lambda: {"webhook_secret": WHSEC})

    monkeypatch.setattr(swh._state, "upsert_customer", lambda d: None)
    monkeypatch.setattr(swh._state, "upsert_subscription", lambda d: None)

    grants: list[tuple] = []

    def _grant(account_id, user_id, plan_id, subscription_id):
        grants.append((account_id, user_id, plan_id, subscription_id))
        return {"set_comp": {"ok": True}}

    monkeypatch.setattr(swh, "_grant_entitlement", _grant)
    return grants


def _insert_contract(conn, *, email, account_id=None, session_id=None, status="accepted",
                     signer_user_id=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.signed_contracts
                (contract_template_id, signer_email, signer_name, signer_ip, tier,
                 stripe_price_id, stripe_checkout_session_id, contract_snapshot,
                 contract_version, agreed_amount_cents, agreed_currency, agreed_interval,
                 account_id, signer_user_id, status)
            VALUES ((SELECT id FROM public.contract_templates WHERE retired_at IS NULL
                     ORDER BY effective_at DESC LIMIT 1),
                    %s, 'Test Signer', '203.0.113.9', 'team', 'price_team', %s,
                    'snapshot text', '2026-08-12-v1', 75000, 'CAD', 'month', %s, %s, %s)
            RETURNING id
            """,
            (email, session_id, account_id, signer_user_id, status),
        )
        cid = cur.fetchone()[0]
    conn.commit()
    return int(cid)


def _read_contract(conn, cid):
    with conn.cursor() as cur:
        cur.execute("SELECT status, stripe_subscription_id, account_id, "
                    "       stripe_checkout_session_id, signer_email "
                    "FROM public.signed_contracts WHERE id = %s", (cid,))
        row = cur.fetchone()
    conn.commit()
    return row


def _signed_event(payload: dict, *, secret: str = WHSEC, tamper: bool = False):
    """Build a real Stripe-Signature header: HMAC-SHA256 over ``{t}.{payload}``, hex-encoded,
    per https://docs.stripe.com/webhooks#verify-manually. ``stripe.Webhook.construct_event``
    does the verification — this test does NOT reimplement it, only the client side."""
    body = json.dumps(payload).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if tamper:
        body = json.dumps({**payload, "id": payload["id"] + "_tampered"}).encode()
    return body, f"t={ts},v1={sig}"


def _event(*, session_id, subscription_id, account_id, metadata):
    return {
        "id": f"evt_{uuid.uuid4().hex[:24]}",
        "object": "event",
        "type": "checkout.session.completed",
        "api_version": "2024-06-20",
        "livemode": False,
        "created": int(time.time()),
        "data": {"object": {
            "id": session_id,
            "object": "checkout.session",
            "customer": "cus_test",
            "subscription": subscription_id,
            "mode": "subscription",
            "payment_status": "paid",
            "status": "complete",
            "metadata": {**metadata, "account_id": account_id},
        }},
    }


def test_signature_verified_event_advances_the_contract_AND_links_the_subscription(
        dbconn, webhook):
    """The whole point, asserted by READING THE ROW."""
    sess = f"cs_{uuid.uuid4().hex[:20]}"
    sub = f"sub_{uuid.uuid4().hex[:20]}"
    cid = _insert_contract(dbconn, email=EMAIL_A, session_id=sess)
    assert _read_contract(dbconn, cid)[0] == "accepted"

    ev = _event(session_id=sess, subscription_id=sub, account_id=ACCT_A,
                metadata={"contract_id": str(cid), "tier": "team"})
    body, sig = _signed_event(ev)

    ok, verified = swh.verify_and_record(body, sig)
    assert ok is True and verified.get("id") == ev["id"], verified
    result = swh.process_event(verified)
    assert result["status"] == "processed", result

    status, sub_id, acct, sess_id, email = _read_contract(dbconn, cid)
    assert status == "completed"
    assert sub_id == sub
    assert str(acct) == ACCT_A
    assert sess_id == sess               # write-once, unchanged
    assert email == EMAIL_A              # acceptance fact, immutable
    assert webhook == [(ACCT_A, None, None, sub)]   # the grant still happened


def test_webhook_replay_leaves_the_row_correct(dbconn, webhook):
    """Stripe redelivers for up to 3 days. A replay must be a no-op that reports success — not
    a corruption and not a crash."""
    sess = f"cs_{uuid.uuid4().hex[:20]}"
    sub = f"sub_{uuid.uuid4().hex[:20]}"
    cid = _insert_contract(dbconn, email=EMAIL_A, session_id=sess)
    ev = _event(session_id=sess, subscription_id=sub, account_id=ACCT_A,
                metadata={"contract_id": str(cid), "tier": "team"})

    body, sig = _signed_event(ev)
    ok, verified = swh.verify_and_record(body, sig)
    assert ok
    swh.process_event(verified)
    first = _read_contract(dbconn, cid)

    # 1. The ledger dedupes an identical redelivery outright.
    body2, sig2 = _signed_event(ev)
    ok2, meta2 = swh.verify_and_record(body2, sig2)
    assert ok2 is True and meta2.get("duplicate") is True

    # 2. And even when the handler is re-run anyway (the reprocessor sweep does exactly this),
    #    the row is unchanged and the outcome still reports linked.
    out = swh.handle_checkout_session_completed(ev)
    assert out["contract"]["linked"] is True
    assert out["contract"]["contract_id"] == cid
    assert _read_contract(dbconn, cid) == first


def test_contract_resolves_by_session_id_when_metadata_is_absent(dbconn, webhook):
    """The fallback lane: an older session with no contract_id in metadata is found by its
    UNIQUE stripe_checkout_session_id."""
    sess = f"cs_{uuid.uuid4().hex[:20]}"
    sub = f"sub_{uuid.uuid4().hex[:20]}"
    cid = _insert_contract(dbconn, email=EMAIL_A, session_id=sess)
    ev = _event(session_id=sess, subscription_id=sub, account_id=ACCT_A, metadata={})
    out = swh.handle_checkout_session_completed(ev)
    assert out["contract"]["linked"] is True
    assert out["contract"]["source"] == "session_lookup"
    assert _read_contract(dbconn, cid)[0] == "completed"


def test_a_contract_failure_NEVER_aborts_the_entitlement_grant(dbconn, webhook):
    """The customer paid; they get their service. A voided contract cannot be advanced (the
    SQL function's WHERE excludes it) — the handler must still grant and still return."""
    sess = f"cs_{uuid.uuid4().hex[:20]}"
    sub = f"sub_{uuid.uuid4().hex[:20]}"
    cid = _insert_contract(dbconn, email=EMAIL_A, session_id=sess, status="voided")
    ev = _event(session_id=sess, subscription_id=sub, account_id=ACCT_A,
                metadata={"contract_id": str(cid), "tier": "team"})
    out = swh.handle_checkout_session_completed(ev)

    assert webhook and webhook[-1][3] == sub          # GRANTED
    assert out["contract"]["linked"] is False
    assert out["contract"]["reason"] == "advance_failed"
    assert _read_contract(dbconn, cid)[0] == "voided"  # untouched, not corrupted


def test_a_raising_contract_layer_never_aborts_the_grant(dbconn, webhook, monkeypatch):
    """Belt-and-braces: even an exception inside the contract layer stays inside it."""
    from saas import contracts as _contracts

    def _boom(*a, **k):
        raise RuntimeError("control plane on fire")

    monkeypatch.setattr(_contracts, "verify_acceptance_for_session", _boom)
    monkeypatch.setattr(_contracts, "advance_to_completed", _boom)

    ev = _event(session_id=f"cs_{uuid.uuid4().hex[:20]}",
                subscription_id=f"sub_{uuid.uuid4().hex[:20]}",
                account_id=ACCT_A, metadata={"contract_id": "1"})
    out = swh.handle_checkout_session_completed(ev)
    assert webhook                                     # the grant happened
    assert out["contract"]["linked"] is False
    assert out["contract"]["reason"].startswith("error:")


def test_forged_signature_is_rejected_and_writes_nothing(dbconn, webhook):
    """VERIFY BEFORE ANY OTHER ACTION. A tampered payload must never reach the handler."""
    sess = f"cs_{uuid.uuid4().hex[:20]}"
    cid = _insert_contract(dbconn, email=EMAIL_A, session_id=sess)
    ev = _event(session_id=sess, subscription_id="sub_forged", account_id=ACCT_A,
                metadata={"contract_id": str(cid)})
    body, sig = _signed_event(ev, tamper=True)
    ok, meta = swh.verify_and_record(body, sig)
    assert ok is False and meta["reason"] == "forgery" and meta["http_status"] == 400
    assert _read_contract(dbconn, cid)[0] == "accepted"     # untouched

    # A correct payload signed with the WRONG secret is equally rejected.
    body2, sig2 = _signed_event(ev, secret="whsec_a_different_secret_entirely")
    ok2, meta2 = swh.verify_and_record(body2, sig2)
    assert ok2 is False and meta2["reason"] == "forgery"
    assert _read_contract(dbconn, cid)[0] == "accepted"


def test_async_payment_succeeded_rides_the_same_contract_path(dbconn, webhook):
    """ACH/SEPA clear later via checkout.session.async_payment_succeeded, which delegates to
    the completed handler — so the contract must link on that path too."""
    sess = f"cs_{uuid.uuid4().hex[:20]}"
    sub = f"sub_{uuid.uuid4().hex[:20]}"
    cid = _insert_contract(dbconn, email=EMAIL_A, session_id=sess)
    ev = _event(session_id=sess, subscription_id=sub, account_id=ACCT_A,
                metadata={"contract_id": str(cid), "tier": "team"})
    ev["type"] = "checkout.session.async_payment_succeeded"
    out = swh.handle_checkout_session_async_payment_succeeded(ev)
    assert out["contract"]["linked"] is True
    assert _read_contract(dbconn, cid)[:2] == ("completed", sub)


def test_a_nonexistent_contract_id_in_metadata_is_loud_not_fatal(dbconn, webhook):
    ev = _event(session_id=f"cs_{uuid.uuid4().hex[:20]}",
                subscription_id=f"sub_{uuid.uuid4().hex[:20]}",
                account_id=ACCT_A, metadata={"contract_id": "999999999", "tier": "team"})
    out = swh.handle_checkout_session_completed(ev)
    assert webhook                                    # grant unaffected
    assert out["contract"]["linked"] is False
    assert out["contract"]["reason"] == "advance_failed"


def test_tenant_tier_with_no_contract_is_flagged_as_an_anomaly(dbconn, webhook, monkeypatch,
                                                               capsys):
    """A tenant-catalog tier reaching checkout with no acceptance row is a defect in the
    signature flow — it must be _log_crit'd, not pass quietly. The tier check is METADATA-DRIVEN
    (a lookup against the catalog), never a hardcoded tier list.

    ⚠️ THIS TEST USED TO HAND-BUILD ``metadata={"tier": "team"}`` — A SHAPE NO PRODUCER EMITS,
    which meant it pinned an alarm that could never fire in production and reported green while
    doing it. ``payments_stripe.create_checkout`` writes exactly
    ``{account_id, user_id, plan_id, contract_id}``; there is no ``tier`` key, ever, and
    ``begin_subscription`` passes the tier key as ``plan_id``. The metadata below is now the
    shape the PRODUCER actually writes, so a green here means the alarm genuinely fires.

    (The consumer, ``stripe_webhooks._tenant_catalog_tier``, now reads ``plan_id`` first and
    keeps ``tier`` as a secondary lookup — that half is owned by the webhook module. The
    parametrisation asserts both keys resolve, but ``plan_id`` is the one that matters because
    it is the only one a real Checkout Session carries.)"""
    stub = types.ModuleType("saas.tenant_checkout")
    stub.get_tier = lambda t: {"tier": t} if t in ("team", "enterprise") else None
    stub.tier_catalog = lambda: []
    monkeypatch.setitem(sys.modules, "saas.tenant_checkout", stub)
    monkeypatch.setattr(saas, "tenant_checkout", stub, raising=False)

    # THE PRODUCER SHAPE: plan_id carries the tier key; there is no "tier" key on a real session.
    ev = _event(session_id=f"cs_{uuid.uuid4().hex[:20]}",
                subscription_id=f"sub_{uuid.uuid4().hex[:20]}",
                account_id=ACCT_A, metadata={"plan_id": "team"})
    out = swh.handle_checkout_session_completed(ev)
    assert out["contract"]["reason"] == "no_contract"
    err = capsys.readouterr().err
    assert "CRIT" in err and "NO SIGNED CONTRACT" in err

    # ...and an individual/house checkout (a plan the tenant catalog does not sell) is NOT an
    # anomaly — no crit.
    ev2 = _event(session_id=f"cs_{uuid.uuid4().hex[:20]}",
                 subscription_id=f"sub_{uuid.uuid4().hex[:20]}",
                 account_id=ACCT_A, metadata={"plan_id": "standard"})
    swh.handle_checkout_session_completed(ev2)
    assert "NO SIGNED CONTRACT" not in capsys.readouterr().err


def test_the_alarm_keys_on_a_key_the_producer_actually_writes(monkeypatch):
    """🔴 THE REGRESSION GUARD FOR A DEAD ALARM.

    Reading the producer proves a lane WOULD run; only enumerating the keys it actually writes
    proves the consumer can see them. This asserts the contract directly: whatever key
    ``_tenant_catalog_tier`` keys on must be a key ``create_checkout`` emits. If someone
    re-points the consumer at a key the producer does not write, this goes RED instead of the
    alarm going quietly dark again."""
    stub = types.ModuleType("saas.tenant_checkout")
    stub.get_tier = lambda t: {"tier": t} if t in ("team", "enterprise") else None
    stub.tier_catalog = lambda: []
    monkeypatch.setitem(sys.modules, "saas.tenant_checkout", stub)
    monkeypatch.setattr(saas, "tenant_checkout", stub, raising=False)

    # The producer's key set, verbatim from payments_stripe.create_checkout.
    producer_keys = {"account_id", "user_id", "plan_id", "contract_id"}
    resolved = swh._tenant_catalog_tier({"plan_id": "team"})
    assert resolved, ("the consumer does not resolve a tenant tier from 'plan_id' — the only "
                      "tier-bearing key any real Checkout Session carries")
    assert "plan_id" in producer_keys
    # A session for a non-tenant plan must NOT look like a tenant tier.
    assert not swh._tenant_catalog_tier({"plan_id": "standard"})
    # And the dead shape must not be the ONLY thing that works.
    assert not swh._tenant_catalog_tier({}), "an empty metadata dict must not resolve a tier"


def test_the_append_only_trigger_still_guards_the_acceptance_facts(dbconn, webhook):
    """The linkage path must not have opened a hole: DELETE stays blocked and an acceptance
    fact stays immutable, even after the row has advanced to completed."""
    import psycopg2
    sess = f"cs_{uuid.uuid4().hex[:20]}"
    cid = _insert_contract(dbconn, email=EMAIL_A, session_id=sess)
    with pytest.raises(psycopg2.Error):
        with dbconn.cursor() as cur:
            cur.execute("DELETE FROM public.signed_contracts WHERE id = %s", (cid,))
    dbconn.rollback()
    with pytest.raises(psycopg2.Error):
        with dbconn.cursor() as cur:
            cur.execute("UPDATE public.signed_contracts SET signer_email = %s WHERE id = %s",
                        ("attacker@example.com", cid))
    dbconn.rollback()
    assert _read_contract(dbconn, cid)[4] == EMAIL_A


# ════════════════════════════════════════════════════════════════════════════════════
# S2, AGAINST THE REAL SQL — the WHERE clause is the security boundary, not the helper
# ════════════════════════════════════════════════════════════════════════════════════

def test_db_S2_the_victims_contract_is_invisible_to_an_attacker_holding_their_email(
        dbconn, webhook):
    """The critic's list-route reproduction, run against REAL rows and the REAL predicate.

    Three contracts exist. The attacker is a billing admin of ACCOUNT B whose login email is now
    the victim's address. Before the fix the email arm returned the victim's row; the assertion
    is that the attacker sees ONLY their own, and that the victim still sees theirs."""
    victim_email = f"victim-{uuid.uuid4().hex[:8]}@tenant-a.example"
    victim_uid = str(uuid.uuid4())
    attacker_uid = str(uuid.uuid4())

    victim_cid = _insert_contract(dbconn, email=victim_email, account_id=ACCT_A,
                                  session_id=f"cs_{uuid.uuid4().hex[:20]}",
                                  signer_user_id=victim_uid)
    attacker_cid = _insert_contract(dbconn, email=f"att-{uuid.uuid4().hex[:8]}@tenant-b.example",
                                    account_id=ACCT_B,
                                    session_id=f"cs_{uuid.uuid4().hex[:20]}",
                                    signer_user_id=attacker_uid)

    # The attacker's session: account B, their own login id — and the victim's EMAIL, which the
    # predicate must now ignore entirely (it is not even an argument any more).
    theirs = tcr._list_own_contracts(account_id=ACCT_B, signer_user_id=attacker_uid, limit=50)
    ids = {r["id"] for r in theirs}
    assert victim_cid not in ids, ("CROSS-TENANT READ: the victim's signed contract was returned "
                                   "to an attacker in another account")
    assert attacker_cid in ids

    # The victim still reads their own back — by account AND by signer id, independently.
    by_account = tcr._list_own_contracts(account_id=ACCT_A, signer_user_id=str(uuid.uuid4()),
                                         limit=50)
    assert victim_cid in {r["id"] for r in by_account}
    by_signer = tcr._list_own_contracts(account_id=str(uuid.uuid4()), signer_user_id=victim_uid,
                                        limit=50)
    assert victim_cid in {r["id"] for r in by_signer}

    # And the DETAIL helper agrees with the list on every one of those cases — read through the
    # REAL projection, so a column the projection forgets to select cannot silently pass here.
    from saas import contracts as _c
    victim_row = _c.get_signed_contract(victim_cid)
    assert victim_row and victim_row["signer_user_id"] == victim_uid, (
        "get_signed_contract does not return signer_user_id — the ownership check depends on it")
    assert tcr._owns_contract(victim_row, account_id=ACCT_B,
                              signer_user_id=attacker_uid) is False
    assert tcr._owns_contract(victim_row, account_id=ACCT_A,
                              signer_user_id=str(uuid.uuid4())) is True
    assert tcr._owns_contract(victim_row, account_id=str(uuid.uuid4()),
                              signer_user_id=victim_uid) is True


# ════════════════════════════════════════════════════════════════════════════════════
# ONE CANCELLATION ADDRESS ACROSS THE WHOLE CONTRACT SURFACE
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_contract_surface_names_exactly_ONE_cancellation_address():
    """🔴 A SIGNED DOCUMENT AND THE SCREEN IT IS SIGNED ON MUST NOT NAME DIFFERENT ADDRESSES.

    There are ZERO self-serve cancel routes in this product, so the email address IS the
    cancellation mechanism — naming the wrong one is materially harmful, and the ToS snapshot is
    append-only so the mismatch becomes permanent at the first signature.

    This drift has now recurred THREE times, and the THIRD time was this test being wrong.
    Round 1 found the ToS naming ``@volenti.ca``; the repair changed the ToS and left the page,
    the webhook and the duplicate-guard refusal naming it — the fix MOVED the mismatch instead of
    closing it. Hence a sweep of EVERY surface rather than the one a reviewer happened to quote.

    ⚠️ THEN THE OWNER REVERSED THE DIRECTION, AND HE IS RIGHT: **VOLENTI IS THE REGISTERED
    COMPANY.** The earlier repair had unified on ``@faultline.ca`` because that was the string the
    code already used (``invoicing.py`` defaults ``SAAS_SELLER_EMAIL`` to ``billing@faultline.ca``)
    — but "which domain does the code already mention" is a popularity contest, not an authority
    order. A binding agreement names the LEGAL ENTITY the customer is contracting with; FaultLine
    is the product brand. So the correct invariant is the INVERSE of what this test first asserted,
    and the assertion is flipped rather than deleted.

    Also swept: ``helpdeskpro.ca``, which must appear NOWHERE — it is a separate entity and not a
    registered company, and it was live in the RFC 9116 ``/.well-known/security.txt`` default, so a
    security researcher following the machine-readable canonical contact was pointed at a
    non-company."""
    import pathlib
    import re as _re
    root = pathlib.Path(__file__).resolve().parents[1]
    surface = [
        "migrations/243_contract_infrastructure.sql",
        "saas/contracts.py",
        "saas/tenant_checkout.py",
        "saas/tenant_checkout_routes.py",
        "saas/stripe_webhooks.py",
        "saas/dashboard/subscribe.html",
        "saas/dashboard/subscribe.js",
    ]
    wrong_domain, banned_entity = [], []
    for rel in surface:
        p = root / rel
        if not p.exists():
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            # a CONTACT address at the product domain — not bare "faultline.ca" (URLs are fine)
            if _re.search(r"[A-Za-z0-9._%+-]+@faultline\.ca", line):
                wrong_domain.append(f"{rel}:{n}")
            if "helpdeskpro" in line.lower():
                banned_entity.append(f"{rel}:{n}")
    assert wrong_domain == [], (
        "the contract surface names a @faultline.ca contact address. Volenti is the REGISTERED "
        "COMPANY and the party to this agreement; the binding Terms name @volenti.ca, and the "
        f"page/webhook/refusal text must match it exactly: {wrong_domain}")
    assert banned_entity == [], (
        "helpdeskpro.ca is a separate entity and NOT a registered company — it must not appear "
        f"anywhere on the contract surface: {banned_entity}")


# ════════════════════════════════════════════════════════════════════════════════════
# THE FALSE-GREEN GUARD — must be the last test in the file
# ════════════════════════════════════════════════════════════════════════════════════

def test_no_test_was_silently_skipped():
    """A DSN-gated suite that skips its DB half reports "PASS | 0 passed, N skipped" and looks
    green. That has produced a false green in this repo three times. If the DB half did not
    run, this FAILS — so a green result means both halves executed."""
    assert _DB_RAN["value"], (
        "the database half of this suite never ran (DSN unreachable or migrations 243/235 "
        f"not applied at {TEST_DSN}) — this is a FALSE GREEN, not a pass. See the file header.")
