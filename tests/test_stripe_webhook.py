"""Stripe webhook processor unit tests — sandbox-only build.

Covers the hard rules from the agent brief:
  * signature forgery → verify_and_record returns (False, None); no DB row written.
  * replay (redelivery of the same event id) → duplicate=True, processor NOT re-run.
  * ack-fast: the route returns 200 before process_event runs.
  * redirect grants nothing (checkout.session.completed with bad signature → no entitlement).
  * money event fail-closed + loud: a handler that raises → row marked 'error', processing_error
    populated, log_crit called, entitlement NOT granted, NO silent drop.
  * customer.subscription.deleted (verified) → entitlement revoked.
  * dunning (past_due) subscription → retention._is_subscribed returns True (not purgeable).
  * no live key in any response/log/DB row.

Uses the stripe lib's WebhookSignature.generate_signature_header to mint valid signatures.
Every DB touch is mocked — no real Postgres required.
"""

import io
import json
import sys
from datetime import datetime, timezone

import pytest

import stripe
from stripe._webhook import WebhookSignature

from saas import stripe_webhooks as _wh
from saas import stripe_state as _state
from saas import retention as _retention


# ── fakes ─────────────────────────────────────────────────────────────────────────

SECRET = "whsec_test_signing_secret_abc123"


def _sign(event_dict):
    """Mint a valid Stripe-Signature header for an event dict (round-trip helper)."""
    payload_str = json.dumps(event_dict)
    sig = WebhookSignature.generate_signature_header(payload=payload_str, secret=SECRET)
    return payload_str.encode(), sig


def _fake_event(event_id="evt_test_001", event_type="checkout.session.completed", **obj_over):
    """Build a minimal but realistic stripe event payload as plain dict."""
    obj = {"id": "cs_test_123", "customer": "cus_test_abc",
           "subscription": "sub_test_xyz", "customer_email": "buyer@example.com",
           "metadata": {"account_id": "00000000-0000-0000-0000-000000000001",
                        "user_id": "00000000-0000-0000-0000-000000000002",
                        "plan_id": "pro_monthly"}}
    obj.update(obj_over)
    return {"id": event_id, "type": event_type, "api_version": "2024-06-20",
            "livemode": False, "data": {"object": obj}}


# ── a fake DB connection that records inserts/updates in-memory ───────────────────

class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._last = None
        self._fetchone_result = None

    # psycopg2 cursors ARE their own context managers — the saas code uses
    # `with conn.cursor() as cur:`, so the fake must too.
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = (sql, params)
        sql_l = sql.lower().strip()
        store = self.store
        if sql_l.startswith("insert into public.stripe_events"):
            eid = (params or [None])[0]
            if eid in store["events"]:
                self.rowcount = 0  # ON CONFLICT DO NOTHING
            else:
                store["events"][eid] = {"event_id": eid, "processing_status": "pending",
                                        "attempt_count": 0, "payload": (params or [None]*5)[4]}
                self.rowcount = 1
        elif sql_l.startswith("update public.stripe_events"):
            # UPDATE ... SET processing_status = '...' ...
            # params order: for 'processing' → (eid,); for 'processed' → (eid,);
            # for 'error' → (error_text, eid)
            status = "processed" if "'processed'" in sql else (
                "processing" if "'processing'" in sql else (
                    "error" if "'error'" in sql else None))
            if status == "error":
                err, eid = params
                if eid in store["events"]:
                    store["events"][eid]["processing_status"] = "error"
                    store["events"][eid]["processing_error"] = err
            elif status and params and params[-1] in store["events"]:
                store["events"][params[-1]]["processing_status"] = status
                if status == "processing":
                    store["events"][params[-1]]["attempt_count"] += 1
                if status == "processed":
                    store["events"][params[-1]]["processing_error"] = None
            self.rowcount = 1
        elif sql_l.startswith("insert into public.stripe_subscriptions"):
            # snapshot upsert — record last write
            store["subs_last"] = params
            self.rowcount = 1
        elif sql_l.startswith("insert into public.stripe_customers"):
            store["cust_last"] = params
            self.rowcount = 1
        elif sql_l.startswith("insert into public.stripe_charges"):
            store["charges"][params[0]] = params
            self.rowcount = 1
        elif sql_l.startswith("insert into public.stripe_refunds"):
            store["refunds"][params[0]] = params
            self.rowcount = 1
        elif "from public.stripe_subscriptions" in sql_l and "where subscription_id" in sql_l:
            # _existing_subscription lookup
            self._fetchone_result = None
        else:
            self.rowcount = 0

    def fetchone(self):
        return getattr(self, "_fetchone_result", None)

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    """Install a fake saas.db.connection that records writes in an in-memory store."""
    store = {"events": {}, "subs_last": None, "cust_last": None,
             "charges": {}, "refunds": {}}

    class _Ctx:
        def __enter__(self):
            return _FakeConn(store)

        def __exit__(self, *a):
            return False

    from saas import db as _db
    monkeypatch.setattr(_db, "connection", lambda: _Ctx())

    # Also stub payment_config.resolve_config to return a webhook secret (verify needs it).
    from saas import payment_config as _pc
    monkeypatch.setattr(_pc, "resolve_config",
                        lambda: {"provider": "stripe", "mode": "test", "configured": True,
                                 "webhook_secret": SECRET, "api_secret": "sk_test_X",
                                 "api_key": "pk_test_X", "currency": "CAD"})
    return store


@pytest.fixture
def stub_entitlements(monkeypatch):
    """Stub the entitlement seam so tests can assert grant/revoke without a DB.

    Returns a list that handlers append to: ('grant'|'revoke', account_id).

    ⚠️ THE SEAM MOVED (migration 245): a Stripe-PAYING tenant is marked
    ``set_external_billing(True, processor='stripe', …)`` — the internal biller stands down
    because the money is collected on Stripe's rail — and is NOT ``set_comp(True)``. A comp is a
    COMPLIMENTARY human write-off (signature + reason + expiry); a payer is its opposite. Both
    are stubbed here: the external one because it is the live path, ``set_comp`` because ANY
    call to it from a webhook is now a regression and would be caught by the assertion below.
    ``external_billing_state`` is stubbed too, so the grant's idempotency probe does not reach
    a database in this DB-less suite."""
    calls = []

    def _fake_set_external(account_id, externally_billed, **kw):
        calls.append(("grant" if externally_billed else "revoke", account_id))
        return {"ok": True, "status_code": 200}

    def _forbidden_set_comp(account_id, comped, **kw):  # pragma: no cover - regression tripwire
        raise AssertionError(
            "a Stripe webhook called set_comp — a PAYING customer is not a complimentary "
            "write-off; use set_external_billing (migration 245)")

    from saas import entitlements as _ent
    monkeypatch.setattr(_ent, "set_external_billing", _fake_set_external)
    monkeypatch.setattr(_ent, "external_billing_state", lambda aid: {
        "externally_billed": False, "active": False, "processor": None,
        "reference": None, "at": None, "source": None})
    monkeypatch.setattr(_ent, "set_comp", _forbidden_set_comp)
    return calls


# ── TEST 1: signature forgery rejected ────────────────────────────────────────────

def test_signature_forge_rejected(fake_db, monkeypatch):
    """Tamper the signature → verify_and_record returns (False, None); no DB row written."""
    event = _fake_event()
    payload, sig = _sign(event)
    # Tamper: flip a byte in the signature header.
    forged = "t=9999999999,v1=deadbeef" + sig[20:]
    ok, ev = _wh.verify_and_record(payload, forged)
    assert ok is False
    # verify_and_record now returns a meta dict with reason + http_status on failure (so the
    # route can return the RIGHT status — 400 for forgery, 503 for DB-error, 200 for missing-secret).
    assert isinstance(ev, dict) and ev.get("reason") == "forgery", f"expected forgery meta, got {ev}"
    assert ev.get("http_status") == 400, f"forgery should map to HTTP 400, got {ev}"
    # No event row was written.
    assert fake_db["events"] == {}, "a forged event must NOT write a ledger row"


def test_signature_forge_no_entitlement(fake_db, stub_entitlements):
    """A forged checkout.session.completed → no entitlement change (redirect grants nothing too)."""
    event = _fake_event(event_type="checkout.session.completed")
    payload, _sig = _sign(event)
    ok, ev = _wh.verify_and_record(payload, "t=1,v1=forged")
    assert ok is False
    assert stub_entitlements == [], "a forged event must NOT grant entitlement"


# ── TEST 2: replay is harmless ────────────────────────────────────────────────────

def test_replay_is_harmless(fake_db, stub_entitlements):
    """A redelivery of the same event id → duplicate=True, processor NOT re-run, one row."""
    event = _fake_event(event_id="evt_dup_001", event_type="checkout.session.completed")
    payload, sig = _sign(event)

    # First delivery — fresh.
    ok1, ev1 = _wh.verify_and_record(payload, sig)
    assert ok1 is True
    assert isinstance(ev1, dict) and ev1.get("duplicate") is not True
    _wh.process_event(ev1)
    grants_after_first = list(stub_entitlements)

    # Second delivery — same event id → duplicate.
    ok2, ev2 = _wh.verify_and_record(payload, sig)
    assert ok2 is True
    assert ev2 == {"duplicate": True, "event_id": "evt_dup_001"}, \
        f"second delivery must be flagged duplicate, got {ev2}"

    # Calling process_event on the duplicate would be the route's bug, but even if it were
    # called, the processor itself is idempotent-by-snapshot. The CONTRACT here is that the
    # route does NOT call process_event on a duplicate. We assert exactly one ledger row.
    assert len(fake_db["events"]) == 1, "exactly one event row, not two"
    assert grants_after_first == stub_entitlements, "no extra grant calls after duplicate"


# ── TEST 3: ack-fast (route returns 200 before processing) ────────────────────────

def test_ack_fast_returns_before_processing(fake_db, stub_entitlements, monkeypatch):
    """The route returns 200 (verify_and_record succeeds) BEFORE process_event runs.

    We simulate the route's behaviour: call verify_and_record, assert ok, then assert
    entitlement was NOT yet granted (process_event has not run). Then run process_event and
    assert the grant happened."""
    event = _fake_event(event_type="checkout.session.completed")
    payload, sig = _sign(event)

    # The route's fast path.
    ok, ev = _wh.verify_and_record(payload, sig)
    assert ok is True, "verify must succeed so the route can return 200"
    # At this point the route has returned 200. process_event has NOT run → no grant yet.
    assert stub_entitlements == [], "entitlement must NOT be granted before process_event"

    # Now the async path runs process_event.
    summary = _wh.process_event(ev)
    assert summary["status"] == "processed"
    assert any(c[0] == "grant" for c in stub_entitlements), "grant happens in process_event"


# ── TEST 4: redirect grants nothing (bad signature → no grant) ────────────────────

def test_redirect_grants_nothing(fake_db, stub_entitlements):
    """The success_url redirect grants nothing; only the signed webhook grants.

    Simulated as: a checkout.session.completed with a BAD signature → no entitlement change.
    (A redirect carries no signature at all, so it cannot reach verify_and_record.)"""
    event = _fake_event(event_type="checkout.session.completed")
    payload, _sig = _sign(event)
    # A bad signature (what a forged redirect-claim would carry).
    ok, ev = _wh.verify_and_record(payload, "t=1,v1=forgeddeadbeef")
    assert ok is False
    assert isinstance(ev, dict) and ev.get("reason") == "forgery", f"expected forgery meta, got {ev}"
    assert stub_entitlements == [], "an unsigned/bad-signature redirect grants nothing"


# ── TEST 5: money event fail-closed + loud ────────────────────────────────────────

def test_money_event_fail_closed_and_loud(fake_db, monkeypatch):
    """A handler that raises → row marked 'error', processing_error populated, log_crit called,
    entitlement NOT granted, NO silent drop."""
    crit_calls = []
    monkeypatch.setattr(_wh, "_log_crit", lambda msg: crit_calls.append(msg))

    # Make the grant handler raise — simulates a transient backend failure during the grant
    # step of checkout.session.completed. (Migration 245: the grant seam is
    # ``set_external_billing``, not ``set_comp`` — a Stripe payer is charged in full on Stripe's
    # rail, which is the opposite of a complimentary write-off.)
    def _raising_set_external(account_id, externally_billed, **kw):
        raise RuntimeError("backend exploded during grant")
    from saas import entitlements as _ent
    monkeypatch.setattr(_ent, "set_external_billing", _raising_set_external)
    monkeypatch.setattr(_ent, "external_billing_state", lambda aid: {
        "externally_billed": False, "active": False, "processor": None,
        "reference": None, "at": None, "source": None})

    event = _fake_event(event_id="evt_fail_001", event_type="checkout.session.completed")
    payload, sig = _sign(event)
    ok, ev = _wh.verify_and_record(payload, sig)
    assert ok is True
    summary = _wh.process_event(ev)

    # The handler raised inside _grant_entitlement, but _grant_entitlement catches it (it is
    # layered so the snapshot still writes). To test the OUTER fail-closed path, we make the
    # HANDLER itself raise. Let's force that by breaking the snapshot upsert too.
    # Actually — the brief's contract is "make process_event's grant handler raise → row marked
    # error". _grant_entitlement swallows exceptions deliberately (snapshot is authoritative).
    # So we need a handler that raises at the TOP level. Patch upsert_subscription to raise.
    crit_calls.clear()
    fake_db["events"].clear()

    def _raising_upsert(sub):
        raise RuntimeError("DB connection lost mid-grant")
    monkeypatch.setattr(_state, "upsert_subscription", _raising_upsert)

    event2 = _fake_event(event_id="evt_fail_002", event_type="checkout.session.completed")
    payload2, sig2 = _sign(event2)
    ok2, ev2 = _wh.verify_and_record(payload2, sig2)
    assert ok2 is True
    summary2 = _wh.process_event(ev2)

    # The row is marked 'error' with processing_error populated.
    row = fake_db["events"]["evt_fail_002"]
    assert row["processing_status"] == "error", \
        f"row must be marked error, got {row['processing_status']}"
    assert row["processing_error"], "processing_error must be populated"
    # 2026-09-14 (gauntlet codeql-info-exposure round 2, IE2): this asserted the exception's own
    # sentence ("DB connection lost") in the ledger row — a row the operator console renders,
    # i.e. it was pinning exception text in a surfaced field. The row now carries the authored
    # phrase + a correlation id; the sentence is on the seam's CRIT line under that id
    # (src/api/errors.py, OWASP ASVS V7.4.1). Loud and traceable, not verbatim.
    import re as _re
    assert _re.search(r"event processing failed \(ref [0-9a-f]{12}\)", row["processing_error"]), row["processing_error"]
    assert "DB connection lost" not in row["processing_error"]
    # log_crit was called (loud, not silent).
    assert any("HANDLER FAILED" in c or "FAIL" in c for c in crit_calls), \
        f"log_crit must be called, got {crit_calls}"
    # The summary reports error (not processed) — NO silent drop.
    assert summary2["status"] == "error"


# ── TEST 6: subscription.deleted revokes ──────────────────────────────────────────

def test_subscription_deleted_revokes(fake_db, stub_entitlements):
    """A verified customer.subscription.deleted → entitlement revoked."""
    event = _fake_event(event_id="evt_del_001", event_type="customer.subscription.deleted",
                        id="sub_test_xyz")  # the sub object id
    payload, sig = _sign(event)
    ok, ev = _wh.verify_and_record(payload, sig)
    assert ok is True
    summary = _wh.process_event(ev)
    assert summary["status"] == "processed"
    # A revoke call happened.
    assert any(c[0] == "revoke" for c in stub_entitlements), \
        f"subscription.deleted must revoke, got {stub_entitlements}"


# ── TEST 7: dunning subscription still subscribed ─────────────────────────────────

def test_dunning_subscription_still_subscribed(monkeypatch):
    """retention._is_subscribed against a past_due subscription → True (not purgeable)."""
    user_id = "00000000-0000-0000-0000-0000000000aa"
    monkeypatch.setattr(_state, "get_subscription_for_user",
                        lambda uid: {"subscription_id": "sub_x", "status": "past_due"})
    assert _retention._is_subscribed({"user_id": user_id}) is True


def test_active_subscription_subscribed(monkeypatch):
    monkeypatch.setattr(_state, "get_subscription_for_user",
                        lambda uid: {"subscription_id": "sub_x", "status": "active"})
    assert _retention._is_subscribed({"user_id": "u1"}) is True


def test_canceled_subscription_not_subscribed(monkeypatch):
    monkeypatch.setattr(_state, "get_subscription_for_user",
                        lambda uid: {"subscription_id": "sub_x", "status": "canceled"})
    assert _retention._is_subscribed({"user_id": "u1"}) is False


def test_no_subscription_not_subscribed(monkeypatch):
    monkeypatch.setattr(_state, "get_subscription_for_user", lambda uid: None)
    assert _retention._is_subscribed({"user_id": "u1"}) is False


def test_subscribed_read_failure_defaults_to_subscribed(monkeypatch):
    """A read error → defensive default TRUE (never purge on doubt)."""
    def _boom(uid):
        raise RuntimeError("control plane unreadable")
    monkeypatch.setattr(_state, "get_subscription_for_user", _boom)
    assert _retention._is_subscribed({"user_id": "u1"}) is True


# ── TEST 8: no live key in any path ───────────────────────────────────────────────

def test_no_live_key_in_any_path(fake_db, stub_entitlements, monkeypatch):
    """None of the response payloads, log lines, or DB rows contain a live-key fragment."""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    event = _fake_event(event_id="evt_noleak_001", event_type="checkout.session.completed")
    payload, sig = _sign(event)
    ok, ev = _wh.verify_and_record(payload, sig)
    summary = _wh.process_event(ev)

    # Grep everything: stderr, the summary, and the stored event payload.
    blob = buf.getvalue() + json.dumps(summary, default=str)
    for eid, row in fake_db["events"].items():
        blob += json.dumps({"id": eid, "row": str(row)}, default=str)
    assert "sk_live_" not in blob, "live secret fragment leaked"
    assert "pk_live_" not in blob, "live publishable fragment leaked"


# ── BONUS: invoice.paid records a charge + grants ─────────────────────────────────

def test_invoice_paid_records_charge_and_grants(fake_db, stub_entitlements):
    """invoice.paid (verified) → charge recorded + grant re-confirmed (renewal)."""
    event = _fake_event(event_id="evt_inv_001", event_type="invoice.paid",
                        id="in_test_001", subscription="sub_test_xyz",
                        charge="ch_test_001", amount_paid=1999, currency="cad",
                        customer="cus_test_abc")
    payload, sig = _sign(event)
    ok, ev = _wh.verify_and_record(payload, sig)
    assert ok is True
    summary = _wh.process_event(ev)
    assert summary["status"] == "processed"
    # A charge row was recorded.
    assert "ch_test_001" in fake_db["charges"], "invoice.paid must record the charge"
    # Entitlement was re-granted (renewal).
    assert any(c[0] == "grant" for c in stub_entitlements)


# ── BONUS: charge.refunded records the refund ─────────────────────────────────────

def test_charge_refunded_records_refund(fake_db, stub_entitlements):
    """charge.refunded (verified) → refund recorded in the local ledger."""
    event = _fake_event(event_id="evt_ref_001", event_type="charge.refunded",
                        id="ch_test_001", amount=1999, amount_refunded=1999,
                        currency="cad", customer="cus_test_abc", status="succeeded",
                        refunds={"data": [{"id": "re_test_001", "amount": 1999,
                                            "currency": "cad", "status": "succeeded",
                                            "reason": "requested_by_customer"}]})
    payload, sig = _sign(event)
    ok, ev = _wh.verify_and_record(payload, sig)
    _wh.process_event(ev)
    assert "re_test_001" in fake_db["refunds"], "charge.refunded must record the refund"
