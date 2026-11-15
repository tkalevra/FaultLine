"""GLOBAL ADMIN BILLING DASHBOARD — saas/billing_dashboard_routes.py + billing.js/billing.html.

Pins the operator-console Stripe billing dashboard contract:

  * every /admin/billing/* route is require_operator-gated (no credential → 403).
  * /admin/billing/whoami returns EVERY permission field the JS consults (affordance parity,
    canary #2) — the UI must never render a control it cannot use.
  * a non-refund-permissioned operator's whoami surfaces can_issue_refunds=False (the JS hides
    the refund control).
  * MRR drift is SHOWN, not hidden: stripe_total != local_total ⇒ drift_cents + has_drift.
  * stale webhook (> threshold minutes, configured provider) ⇒ provider_health.stale = True.
  * ENUM COVERAGE (canary #3): every subscription status in the 7-value set renders a row.
  * capture-email BODIES are stripped for a non-superadmin viewer.
  * CSP (canary): billing.html + index.html carry NO inline <script>; billing.js has NO
    on*= HTML-attribute handlers.

Run: python3 tools/fltest.py --bug BILLING-DASHBOARD --test tests/test_billing_dashboard.py
"""

import datetime as _dt
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import billing_dashboard_routes as bdr
from saas import dashboard_auth


OP_KEY = "op-billing-dashboard-secret-key"
_HERE = Path(__file__).resolve().parent
_DASH = Path(bdr.__file__).resolve().parent / "dashboard"


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "billing-dashboard-test-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    app = FastAPI()
    app.include_router(bdr.router)
    return TestClient(app)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


# ── fake DB (a context manager yielding a conn with a no-op cursor) ───────────────

class _FakeCur:
    def __init__(self, fetchone=None, fetchall=None):
        self._one = fetchone
        self._all = fetchall
        self.rowcount = 0
        self.last = None

    def execute(self, q, params=None):
        self.last = (q, params)
        return None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur=None):
        self._cur = cur or _FakeCur()

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_db(cur=None):
    class _DB:
        connection = staticmethod(lambda: _FakeConn(cur))
    return _DB()


# The full route set advertised by the dashboard. Every one MUST be operator-gated.
ALL_ROUTES = [
    ("GET", "/admin/billing/whoami"),
    ("GET", "/admin/billing/state"),
    ("GET", "/admin/billing/mrr"),
    ("GET", "/admin/billing/dunning"),
    ("GET", "/admin/billing/lifecycle"),
    ("GET", "/admin/billing/refunds"),
    ("POST", "/admin/billing/refunds"),
    ("GET", "/admin/billing/provider-health"),
    ("POST", "/admin/billing/provider-health/test"),
    ("GET", "/admin/billing/branding"),
    ("PUT", "/admin/billing/branding"),
    ("GET", "/admin/billing/email-controls"),
    ("PUT", "/admin/billing/email-controls"),
    ("POST", "/admin/billing/email-controls/clear-outbox"),
    ("POST", "/admin/billing/email-controls/test-capture"),
    ("GET", "/admin/billing/email-outbox"),
    ("POST", "/admin/billing/register-webhook"),
]


# ══════════════════════════════════════════════════════════════════════════════════
# 1. every route requires an operator credential
# ══════════════════════════════════════════════════════════════════════════════════

def test_every_route_requires_operator(client):
    # No Authorization header, no session cookie → require_operator denies every route.
    # (require_operator returns 403 "operator credential required"; 401 is also accepted per
    # the task's "401/403" wording — the contract is "no access without a credential".)
    bodies = {
        "POST": {"charge_id": "ch_x"},
        "PUT": {"support_email": "x@y.z"},
    }
    for method, path in ALL_ROUTES:
        r = client.request(method, path, json=bodies.get(method))
        assert r.status_code in (401, 403), f"{method} {path} returned {r.status_code} (expected 401/403)"


# ══════════════════════════════════════════════════════════════════════════════════
# 2. whoami returns every permission field the JS consults (affordance parity, canary #2)
# ══════════════════════════════════════════════════════════════════════════════════

REQUIRED_PERM_FIELDS = {
    "is_operator_superadmin", "can_view_billing", "can_issue_refunds",
    "can_manage_branding", "can_manage_email_controls", "can_register_webhook",
}


def test_whoami_returns_all_permission_fields(client):
    r = client.get("/admin/billing/whoami", headers=_op())
    assert r.status_code == 200
    body = r.json()
    # The break-glass KEY path carries every grant.
    for f in REQUIRED_PERM_FIELDS:
        assert f in body, f"whoami missing required perm field {f!r}"
        assert body[f] is True, f"KEY path should grant {f}=True, got {body[f]}"
    assert body.get("via_key") is True


# ══════════════════════════════════════════════════════════════════════════════════
# 3. a non-refund-permissioned operator's whoami surfaces can_issue_refunds=False
#    (the JS hides the refund control on this signal)
# ══════════════════════════════════════════════════════════════════════════════════

def test_refund_hidden_without_can_issue_refunds(client, monkeypatch):
    # Drive the SESSION path (not the KEY path, which is all-True). Mint a real operator
    # session cookie, then make the row read return a stripped permission set.
    #
    # ``require_operator`` now re-validates the signed principal against public.dashboard_users
    # on every request (deleting an operator must end their live session). This is a pure-mock
    # suite with no control plane and a synthetic operator id, so the lookup is stubbed LIVE
    # alongside the perms row it already stubs — the revocation contract itself is proved
    # against real rows in tests/test_operator_session_revocation_and_perm_grant.py.
    monkeypatch.setattr(dashboard_auth, "operator_principal_active", lambda uid: True)
    cookie = dashboard_auth.issue_operator_session(
        dashboard_user_id="00000000-0000-0000-0000-000000000001", username="ops-limited")
    stripped = {
        "is_operator_superadmin": True, "can_view_billing": True,
        "can_issue_refunds": False, "can_manage_branding": False,
        "can_manage_email_controls": False, "can_register_webhook": False, "via_key": False,
        "is_platform_owner": False, "can_grant_operator_perms": False,
    }
    monkeypatch.setattr(bdr, "_operator_perms_row", lambda uid: dict(stripped))
    r = client.get("/admin/billing/whoami",
                   cookies={dashboard_auth.OPERATOR_SESSION_COOKIE_NAME: cookie})
    assert r.status_code == 200
    body = r.json()
    assert body["can_issue_refunds"] is False
    assert body["can_view_billing"] is True   # the read half stays available


def test_refund_post_denied_without_can_issue_refunds(client, monkeypatch):
    # See the note above: the principal re-check is stubbed LIVE for this mock-only suite so the
    # test still exercises the PERMISSION gate rather than the (separately proved) session gate.
    monkeypatch.setattr(dashboard_auth, "operator_principal_active", lambda uid: True)
    cookie = dashboard_auth.issue_operator_session(
        dashboard_user_id="00000000-0000-0000-0000-000000000002", username="ops-limited2")
    monkeypatch.setattr(bdr, "_operator_perms_row", lambda uid: {
        "is_operator_superadmin": True, "can_view_billing": True, "can_issue_refunds": False,
        "can_manage_branding": False, "can_manage_email_controls": False,
        "can_register_webhook": False, "via_key": False,
        "is_platform_owner": False, "can_grant_operator_perms": False})
    # POST /admin/billing/refunds must 403 — the destructive verb is gated.
    r = client.post("/admin/billing/refunds", json={"charge_id": "ch_x"},
                    cookies={dashboard_auth.OPERATOR_SESSION_COOKIE_NAME: cookie})
    assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════════
# 4. MRR drift is SHOWN, not hidden
# ══════════════════════════════════════════════════════════════════════════════════

def _fake_composition(total_cents, **over):
    """A complete _mrr_composition() payload. `_stripe_mrr_total_cents` (which returned a bare
    int and did `COALESCE(SUM(amount), 0)` over a column that did not exist) was replaced by
    `_mrr_composition`, which reports the total TOGETHER WITH what it had to leave out."""
    out = {"stripe_total_cents": total_cents, "currency": "CAD",
           "totals_by_currency": ({"CAD": total_cents} if total_cents is not None else {}),
           "counted_subscriptions": 1, "unknown_subscriptions": 0,
           "non_monthly_subscriptions": 0, "complete": True,
           "amount_sources": {"snapshot": 1, "catalog": 0, "unknown": 0}}
    out.update(over)
    return out


def test_mrr_drift_shown_not_hidden(client, monkeypatch):
    monkeypatch.setattr(bdr, "db", _fake_db())
    monkeypatch.setattr(bdr, "_mrr_composition", lambda cur: _fake_composition(10000))  # $100.00
    monkeypatch.setattr(bdr, "_local_ledger_total_cents", lambda cur: 8000)             # $80.00
    r = client.get("/admin/billing/mrr", headers=_op())
    assert r.status_code == 200
    body = r.json()
    assert body["stripe_total_cents"] == 10000
    assert body["local_total_cents"] == 8000
    assert body["drift_cents"] == 2000          # the delta is surfaced
    assert body["has_drift"] is True
    assert body["drift_pct"] == 25.0


def test_mrr_in_sync_reports_no_drift(client, monkeypatch):
    monkeypatch.setattr(bdr, "db", _fake_db())
    monkeypatch.setattr(bdr, "_mrr_composition", lambda cur: _fake_composition(5000))
    monkeypatch.setattr(bdr, "_local_ledger_total_cents", lambda cur: 5000)
    r = client.get("/admin/billing/mrr", headers=_op())
    body = r.json()
    assert body["has_drift"] is False
    assert body["drift_cents"] == 0


def test_an_unknown_mrr_never_reports_zero_and_never_fakes_a_drift(client, monkeypatch):
    """The failure the whole task turns on: when the total cannot be obtained, the payload must
    say UNKNOWN (None) — not CA$0.00 — and it must not compute a drift against that placeholder."""
    monkeypatch.setattr(bdr, "db", _fake_db())
    monkeypatch.setattr(bdr, "_mrr_composition",
                        lambda cur: _fake_composition(None, complete=False,
                                                      unknown_subscriptions=2,
                                                      counted_subscriptions=0))
    monkeypatch.setattr(bdr, "_local_ledger_total_cents", lambda cur: 8000)
    body = client.get("/admin/billing/mrr", headers=_op()).json()
    assert body["stripe_total_cents"] is None, "an unobtainable MRR was reported as a number"
    assert body["stripe_total_cents"] != 0
    assert body["complete"] is False
    assert body["drift_cents"] is None, "drift was computed against an unknown total"
    assert body["has_drift"] is False


def test_mrr_reports_unknown_when_the_control_plane_is_unreadable(client, monkeypatch):
    """A DB failure must surface as an error with an UNKNOWN total, never a confident zero."""
    class _Boom:
        def connection(self):
            raise RuntimeError("control plane down")
    monkeypatch.setattr(bdr, "db", _Boom())
    body = client.get("/admin/billing/mrr", headers=_op()).json()
    assert body["error"]
    assert body["stripe_total_cents"] is None
    assert body["complete"] is False


# ══════════════════════════════════════════════════════════════════════════════════
# 5. stale-webhook alarm
# ══════════════════════════════════════════════════════════════════════════════════

def test_stale_webhook_alarm(client, monkeypatch):
    ninety_min_ago = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=90)).isoformat()
    monkeypatch.setattr(bdr, "db", _fake_db())
    monkeypatch.setattr(bdr, "_provider_health", lambda cur: {
        "provider": "stripe", "mode": "test", "configured": True, "adapter_live": True,
        "last_webhook_received_at": ninety_min_ago, "stale": True,
        "stale_threshold_minutes": 60})
    r = client.get("/admin/billing/provider-health", headers=_op())
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True
    assert body["configured"] is True


# ══════════════════════════════════════════════════════════════════════════════════
# 6. ENUM COVERAGE — every subscription status renders a row (canary #3)
# ══════════════════════════════════════════════════════════════════════════════════

def test_subscription_status_enum_coverage(client, monkeypatch):
    expected = ["trialing", "active", "past_due", "canceled",
                "unpaid", "incomplete", "incomplete_expired"]
    seeded = [
        {"account_id": f"acct_{s}", "customer_id": f"cus_{s}", "livemode": False,
         "subscription_id": f"sub_{s}", "status": s, "current_period_end": None,
         "plan_id": "plan_x", "price_id": "price_x", "amount_cents": 1000, "currency": "CAD"}
        for s in expected
    ]
    monkeypatch.setattr(bdr, "db", _fake_db())
    monkeypatch.setattr(bdr, "_read_stripe_state", lambda cur: seeded)
    r = client.get("/admin/billing/state", headers=_op())
    assert r.status_code == 200
    rows = r.json()["accounts"]
    got = [row["status"] for row in rows]
    for s in expected:
        assert s in got, f"status {s!r} was silently dropped — no row rendered"


def test_dunning_covers_each_dunning_status(client, monkeypatch):
    # The dunning queue MUST include each of the 4 dunning statuses when present.
    dunning = ["past_due", "unpaid", "incomplete", "incomplete_expired"]
    seeded = [
        {"customer_id": f"cus_{s}", "subscription_id": f"sub_{s}", "status": s,
         "amount_cents": 1500, "currency": "CAD", "current_period_end": None,
         "days_in_dunning": 3}
        for s in dunning
    ]
    monkeypatch.setattr(bdr, "db", _fake_db())
    monkeypatch.setattr(bdr, "_read_dunning", lambda cur: seeded)
    r = client.get("/admin/billing/dunning", headers=_op())
    rows = r.json()["entries"]
    got = {row["status"] for row in rows}
    for s in dunning:
        assert s in got, f"dunning status {s!r} missing"


# ══════════════════════════════════════════════════════════════════════════════════
# 7. capture-email BODIES are stripped for a non-superadmin viewer
# ══════════════════════════════════════════════════════════════════════════════════

def test_capture_bodies_not_leaked_to_non_superadmin(client, monkeypatch):
    # Simulate a non-superadmin caller (e.g. a future limited-operator role): the perm
    # resolver returns is_operator_superadmin=False. The outbox read returns rows that WOULD
    # carry bodies if asked — the guard must strip them regardless of ?with_bodies=true.
    monkeypatch.setattr(bdr, "resolve_operator_perms",
                        lambda request, authorization=None: {
                            "is_operator_superadmin": False, "can_view_billing": True,
                            "can_issue_refunds": False, "can_manage_branding": False,
                            "can_manage_email_controls": False, "can_register_webhook": False,
                            "via_key": False})
    monkeypatch.setattr(bdr, "db", _fake_db())

    def _outbox(cur, include_bodies, limit=50):
        # If the guard worked, include_bodies is False even though ?with_bodies=true was sent.
        item = {"id": 1, "to_address": "x@y.z", "subject": "s", "captured_at": "2026-01-01T00:00:00+00:00",
                "email_type": "retention"}
        if include_bodies:
            item["html_body"] = "<secret>"; item["text_body"] = "secret"
        return [item]
    monkeypatch.setattr(bdr, "_read_outbox", _outbox)

    r = client.get("/admin/billing/email-outbox?with_bodies=true", headers=_op())
    assert r.status_code == 200
    body = r.json()
    assert body["bodies_included"] is False
    for row in body["outbox"]:
        assert "html_body" not in row
        assert "text_body" not in row


def test_capture_bodies_shown_to_superadmin(client, monkeypatch):
    # The KEY path IS super-admin → with_bodies=true returns bodies (operator console only).
    monkeypatch.setattr(bdr, "db", _fake_db())

    def _outbox(cur, include_bodies, limit=50):
        item = {"id": 1, "to_address": "x@y.z", "subject": "s",
                "captured_at": "2026-01-01T00:00:00+00:00", "email_type": "retention"}
        if include_bodies:
            item["html_body"] = "<b>hi</b>"; item["text_body"] = "hi"
        return [item]
    monkeypatch.setattr(bdr, "_read_outbox", _outbox)
    r = client.get("/admin/billing/email-outbox?with_bodies=true", headers=_op())
    body = r.json()
    assert body["bodies_included"] is True
    assert body["outbox"][0]["html_body"] == "<b>hi</b>"


# ══════════════════════════════════════════════════════════════════════════════════
# 8. CSP canary — no inline <script> in billing.html or index.html
# ══════════════════════════════════════════════════════════════════════════════════

def test_no_inline_script_in_billing_html():
    # Every <script> tag MUST carry a src= attribute (CSP: script-src 'self', no 'unsafe-inline').
    script_tag = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
    for name in ("billing.html", "index.html"):
        path = _DASH / name
        text = path.read_text(encoding="utf-8")
        for m in script_tag.finditer(text):
            attrs = m.group(1)
            assert "src=" in attrs.lower(), (
                f"{name} has an inline <script> (no src=) — CSP violation:\n{m.group(0)}")


def test_no_on_handlers_in_billing_js():
    # No inline HTML on*= event handlers anywhere in billing.js (CSP canary). addEventListener
    # only. The regex targets the inline-handler attribute form inside HTML string literals.
    text = (_DASH / "billing.js").read_text(encoding="utf-8")
    inline_handler = re.compile(r"\bon(click|load|error|change|submit|mouseover|mouseenter|"
                                r"mouseleave|mousedown|mouseup|mousemove|keydown|keyup|keypress|"
                                r"input|focus|blur|dblclick|wheel|scroll|resize|toggle)\s*=")
    hits = inline_handler.findall(text)
    assert hits == [], f"billing.js contains inline on*= handlers: {hits}"


def test_billing_js_is_external_loaded_by_index():
    # index.html MUST load billing.js as an external script (so the dashboard is CSP-served).
    text = (_DASH / "index.html").read_text(encoding="utf-8")
    assert '<script src="/dashboard/billing.js"' in text


# ══════════════════════════════════════════════════════════════════════════════════
# 9. defensive reads — absent tables return empty-shaped (fail-safe, never 500)
# ══════════════════════════════════════════════════════════════════════════════════

def test_state_fail_safe_when_tables_absent(client, monkeypatch):
    # to_regclass returns None for every table ⇒ empty-but-shaped (the other builder's
    # migrations 235–239 have not run yet).
    cur = _FakeCur(fetchone=(None,))  # to_regclass → NULL
    monkeypatch.setattr(bdr, "db", _fake_db(cur))
    r = client.get("/admin/billing/state", headers=_op())
    assert r.status_code == 200
    body = r.json()
    assert body["accounts"] == []
    assert body["error"] is None


def test_lifecycle_includes_expiry_queue(client, monkeypatch):
    monkeypatch.setattr(bdr, "db", _fake_db())
    monkeypatch.setattr(bdr, "_read_lifecycle_events", lambda cur, et, lim: [])
    monkeypatch.setattr(bdr, "_individual_expiry_queue", lambda cur: [
        {"user_id": "u1", "email": "a@b.c", "trial_ends_at": "2026-08-20T00:00:00+00:00",
         "expires_at": "2026-08-27T00:00:00+00:00", "warning_sent": True, "farewell_sent": False}])
    r = client.get("/admin/billing/lifecycle", headers=_op())
    body = r.json()
    assert body["expiry_queue"][0]["email"] == "a@b.c"
    assert body["expiry_queue"][0]["warning_sent"] is True


# ══════════════════════════════════════════════════════════════════════════════════
# 10. THE REAL TABLE — these tests execute the ACTUAL SQL against a REAL migrated DB
# ══════════════════════════════════════════════════════════════════════════════════
#
# 🔴 WHY THIS SECTION EXISTS, AND WHY IT MUST NEVER BE REPLACED BY FIXTURES.
#
# Every test above this line feeds a DICT to a monkeypatched reader. That is fine for shape and
# permission contracts — and it is USELESS for schema truth. For the whole life of this module
# THREE reads selected `s.amount`, `s.currency` and `s.first_past_due_at` from
# public.stripe_subscriptions; none of those columns has ever existed, so the STRIPE BILLING view
# raised `UndefinedColumn: column s.amount does not exist` on EVERY load — while this suite sat
# at 16/16 green, because a dict fixture containing "amount_cents": 1000 cannot notice a missing
# column. Two further reads (`stripe_events.created_at/account_id/summary`,
# `stripe_refunds.amount/payment_intent_id/invoice_id`) and an `ORDER BY c.created_at` were broken
# the same way and had never been executed either.
#
# So: the headline test below runs EVERY reader in the module against a database built from the
# migrations. If a column drifts again — in either direction — it goes RED with the real
# UndefinedColumn text instead of staying quietly green.
#
# Set POSTGRES_DSN to a THROWAWAY database with every migration >= 131 applied:
#   PYTHONPATH=... POSTGRES_DSN=postgresql://faultline:faultline@host:5432/throwaway \
#     python3 tools/fltest.py --bug BILLDASH --test tests/test_billing_dashboard.py

import os as _os
import uuid as _uuid

from saas import db as _db
from saas import stripe_state as _stripe_state

# _DB_RAN GUARD: if the DB half is skipped, test_zzz_no_db_test_was_silently_skipped turns the
# run RED. A schema suite that quietly no-ops is how the original defect survived.
_DB_RAN = {"n": 0}

_DSN = _os.environ.get("POSTGRES_DSN")
_TAG = _uuid.uuid4().hex[:8]

# The three columns that NEVER existed and whose absence caused the outage, and the real columns
# that answer the same questions. Pinned so a future edit re-introducing a phantom is caught here.
_PHANTOM_SUB_COLUMNS = ("amount", "first_past_due_at")
_REQUIRED_SUB_COLUMNS = ("amount_cents", "currency", "billing_interval", "billing_interval_count")


def _columns(cur, table: str) -> set:
    cur.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) "
        "AND attnum > 0 AND NOT attisdropped",
        (f"public.{table}",),
    )
    return {r[0] for r in cur.fetchall()}


@pytest.fixture(scope="module")
def real_db():
    """A real cursor against POSTGRES_DSN, with this module's Stripe fixtures seeded.

    Skips ONLY when no DSN is configured or the Stripe control-plane tables are absent — and the
    skip is counted so it cannot pass unnoticed."""
    if not _DSN:
        pytest.skip("POSTGRES_DSN not set — pass a throwaway DSN to exercise the real schema")
    try:
        conn = _db.psycopg2.connect(_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"control plane unreachable: {type(exc).__name__}")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('public.stripe_subscriptions')")
    if not cur.fetchone()[0]:
        pytest.skip("public.stripe_subscriptions absent — apply migrations >= 131")
    have = _columns(cur, "stripe_subscriptions")
    missing = [c for c in _REQUIRED_SUB_COLUMNS if c not in have]
    if missing:
        pytest.skip(f"migration 247 not applied — missing {missing}")

    cus = lambda k: f"cus_{_TAG}_{k}"      # noqa: E731 — terse local id builders
    sub = lambda k: f"sub_{_TAG}_{k}"      # noqa: E731
    cur.execute(
        "INSERT INTO public.stripe_customers (customer_id, livemode, first_seen_at) "
        "VALUES (%s,false,NOW()-INTERVAL '5 days'),(%s,false,NOW()-INTERVAL '4 days'),"
        "       (%s,false,NOW()-INTERVAL '3 days'),(%s,false,NOW()-INTERVAL '2 days'),"
        "       (%s,false,NOW()-INTERVAL '1 day')",
        (cus("snap"), cus("cat"), cus("unk"), cus("year"), cus("dun")),
    )
    cur.execute(
        "INSERT INTO public.stripe_subscriptions (subscription_id, customer_id, status, plan_id,"
        " price_id, amount_cents, currency, billing_interval, billing_interval_count, livemode,"
        " current_period_end) VALUES "
        # OBSERVED on Stripe — a negotiated price that DIFFERS from the catalog's 'enterprise'
        # rate, so "snapshot beats catalog" is provable rather than coincidental.
        " (%s,%s,'active','enterprise',%s,99900,'cad','month',1,false,"
        "  NOW()+INTERVAL '10 days'),"
        # No snapshot; resolvable from the shipped catalog by EXACT plan_id equality.
        " (%s,%s,'active','team',NULL,NULL,NULL,NULL,NULL,false,NOW()+INTERVAL '20 days'),"
        # Resolvable by NEITHER lane — must read UNKNOWN, never 0.
        " (%s,%s,'active',%s,%s,NULL,NULL,NULL,NULL,false,NULL),"
        # ANNUAL: resolvable, but must NOT be summed into a MONTHLY total.
        " (%s,%s,'active',%s,%s,1200000,'cad','year',1,false,"
        "  NOW()+INTERVAL '300 days'),"
        # Dunning, for the event-ledger age derivation.
        " (%s,%s,'past_due','team',NULL,NULL,NULL,NULL,NULL,false,NOW()-INTERVAL '9 days')",
        (sub("snap"), cus("snap"), f"price_negotiated_{_TAG}",
         sub("cat"), cus("cat"),
         sub("unk"), cus("unk"), f"legacy_bespoke_{_TAG}", f"price_absent_{_TAG}",
         sub("year"), cus("year"), f"annual_{_TAG}", f"price_annual_{_TAG}",
         sub("dun"), cus("dun")),
    )
    # public.stripe_events is APPEND-ONLY (migration 235 blocks DELETE), so rows are tagged and
    # never cleaned up. The earlier of the two failures must win the dunning age.
    cur.execute(
        "INSERT INTO public.stripe_events (event_id,event_type,livemode,received_at,payload,"
        " processing_status,processing_error) VALUES "
        " (%s,'invoice.payment_failed',false,NOW()-INTERVAL '9 days',%s::jsonb,'processed',NULL),"
        " (%s,'invoice.payment_failed',false,NOW()-INTERVAL '2 days',%s::jsonb,'processed',NULL),"
        " (%s,'customer.subscription.updated',false,NOW()-INTERVAL '5 minutes',%s::jsonb,'error',%s)",
        (f"evt_{_TAG}_old",
         '{"data":{"object":{"object":"invoice","id":"in_a","subscription":"%s"}}}' % sub("dun"),
         f"evt_{_TAG}_new",
         '{"data":{"object":{"object":"invoice","id":"in_b","subscription":"%s"}}}' % sub("dun"),
         f"evt_{_TAG}_meta",
         '{"data":{"object":{"object":"subscription","id":"%s","status":"active",'
         '"metadata":{"account_id":"acct-%s"}}}}' % (sub("snap"), _TAG),
         "boom: adapter timeout"),
    )
    cur.execute(
        "INSERT INTO public.stripe_charges (charge_id,customer_id,subscription_id,invoice_id,"
        " amount_cents,currency,refunded_amount_cents,status,livemode,created_at) "
        "VALUES (%s,%s,%s,%s,99900,'cad',5000,'succeeded',false,NOW()-INTERVAL '2 days')",
        (f"ch_{_TAG}", cus("snap"), sub("snap"), f"in_paid_{_TAG}"),
    )
    cur.execute(
        "INSERT INTO public.stripe_refunds (refund_id,charge_id,amount_cents,currency,reason,"
        " status,livemode,created_at) "
        "VALUES (%s,%s,5000,'cad','requested_by_customer','succeeded',false,NOW()-INTERVAL '1 day')",
        (f"re_{_TAG}", f"ch_{_TAG}"),
    )
    try:
        yield {"cur": cur, "cus": cus, "sub": sub}
    finally:
        try:
            cur.execute("DELETE FROM public.stripe_refunds WHERE refund_id = %s", (f"re_{_TAG}",))
            cur.execute("DELETE FROM public.stripe_charges WHERE charge_id = %s", (f"ch_{_TAG}",))
            cur.execute("DELETE FROM public.stripe_subscriptions WHERE subscription_id LIKE %s",
                        (f"sub_{_TAG}_%",))
            cur.execute("DELETE FROM public.stripe_customers WHERE customer_id LIKE %s",
                        (f"cus_{_TAG}_%",))
        except Exception:
            pass
        cur.close()
        conn.close()


def _row_for(rows, key, sub_id):
    for r in rows:
        if r.get(key) == sub_id:
            return r
    raise AssertionError(f"{sub_id} not found in {len(rows)} rows")


# ── the headline: every read runs against the real schema ────────────────────────

def test_every_dashboard_read_executes_against_the_real_schema(real_db):
    """THE regression that the fixture-only suite could never catch.

    Runs EVERY reader in the module against the REAL migrated tables. Revert any of the column
    fixes and this goes RED with the genuine UndefinedColumn — e.g.
    `column s.amount does not exist`. It is deliberately one test over all readers: a partial
    repair (fixing `s.amount` but leaving `ORDER BY c.created_at`) still fails."""
    _DB_RAN["n"] += 1
    cur = real_db["cur"]
    reads = {
        "_read_stripe_state": lambda: bdr._read_stripe_state(cur),
        "_mrr_composition": lambda: bdr._mrr_composition(cur),
        "_local_ledger_total_cents": lambda: bdr._local_ledger_total_cents(cur),
        "_read_dunning": lambda: bdr._read_dunning(cur),
        "_dunning_since": lambda: bdr._dunning_since(cur, [real_db["sub"]("dun")]),
        "_read_lifecycle_events": lambda: bdr._read_lifecycle_events(cur, None, 25),
        "_read_lifecycle_events(filtered)":
            lambda: bdr._read_lifecycle_events(cur, "invoice.payment_failed", 25),
        "_individual_expiry_queue": lambda: bdr._individual_expiry_queue(cur),
        "_read_refunds": lambda: bdr._read_refunds(cur),
        "_provider_health": lambda: bdr._provider_health(cur),
        "_read_branding": lambda: bdr._read_branding(cur),
        "_read_email_controls_state": lambda: bdr._read_email_controls_state(cur),
        "_read_outbox": lambda: bdr._read_outbox(cur, False),
        "_read_outbox(bodies)": lambda: bdr._read_outbox(cur, True),
        "_read_retention_log": lambda: bdr._read_retention_log(cur),
    }
    broken = {}
    for name, fn in reads.items():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            broken[name] = f"{type(exc).__name__}: {str(exc).strip().splitlines()[0]}"
    assert not broken, "reads that do NOT run against the real schema:\n" + "\n".join(
        f"  {k}: {v}" for k, v in sorted(broken.items()))


def test_the_phantom_columns_are_still_absent_and_the_real_ones_present(real_db):
    """Pins the schema fact the outage turned on, in BOTH directions.

    If someone 'fixes' a future report by adding an `amount` column, this goes red and points at
    migration 247's `amount_cents` instead — one name for one thing."""
    _DB_RAN["n"] += 1
    have = _columns(real_db["cur"], "stripe_subscriptions")
    for phantom in _PHANTOM_SUB_COLUMNS:
        assert phantom not in have, (
            f"public.stripe_subscriptions.{phantom} was added — the dashboard reads "
            f"amount_cents (migration 247); do not reintroduce a second name for it")
    for real in _REQUIRED_SUB_COLUMNS:
        assert real in have, f"migration 247 column {real} missing"
    events = _columns(real_db["cur"], "stripe_events")
    assert "received_at" in events and "created_at" not in events
    refunds = _columns(real_db["cur"], "stripe_refunds")
    assert "amount_cents" in refunds and "amount" not in refunds
    customers = _columns(real_db["cur"], "stripe_customers")
    assert "first_seen_at" in customers and "created_at" not in customers


# ── authority order + the UNKNOWN floor ──────────────────────────────────────────

def test_state_amount_prefers_the_stripe_snapshot_over_the_catalog(real_db):
    """A NEGOTIATED price observed on Stripe must beat the catalog rate for the same tier."""
    _DB_RAN["n"] += 1
    rows = bdr._read_stripe_state(real_db["cur"])
    row = _row_for(rows, "subscription_id", real_db["sub"]("snap"))
    assert row["amount_cents"] == 99900, "the catalog overrode an observed Stripe amount"
    assert row["amount_source"] == "snapshot"
    assert row["currency"] == "CAD"


def test_catalog_resolves_by_plan_id_when_price_id_is_null(real_db):
    """The real post-checkout state: price_id NULL (line_items is not on the webhook payload).

    Without the plan_id lane this row would read UNKNOWN for a whole billing cycle."""
    _DB_RAN["n"] += 1
    rows = bdr._read_stripe_state(real_db["cur"])
    row = _row_for(rows, "subscription_id", real_db["sub"]("cat"))
    assert row["amount_source"] == "catalog"
    assert row["amount_cents"] and row["amount_cents"] > 0
    assert row["currency"] == "CAD"


def test_unresolvable_amount_reads_unknown_never_zero(real_db):
    """THE non-negotiable. An amount nobody can substantiate is None — not 0, and no currency."""
    _DB_RAN["n"] += 1
    rows = bdr._read_stripe_state(real_db["cur"])
    row = _row_for(rows, "subscription_id", real_db["sub"]("unk"))
    assert row["amount_cents"] is None, "an unknown amount was reported as a number"
    assert row["amount_cents"] != 0
    assert row["currency"] is None, "a currency was invented for an unknown amount"
    assert row["amount_source"] == "unknown"


# ── MRR: the total must declare what it left out ─────────────────────────────────

def test_mrr_declares_the_subscriptions_it_could_not_count(real_db):
    _DB_RAN["n"] += 1
    comp = bdr._mrr_composition(real_db["cur"])
    assert comp["unknown_subscriptions"] >= 1
    assert comp["non_monthly_subscriptions"] >= 1
    assert comp["complete"] is False, (
        "MRR reported itself COMPLETE while excluding subscriptions — a partial total that "
        "looks whole is the defect this panel was fixed for")
    assert comp["amount_sources"]["snapshot"] >= 1
    assert comp["amount_sources"]["catalog"] >= 1
    assert comp["amount_sources"]["unknown"] >= 1


def test_mrr_excludes_an_annual_price_from_a_monthly_total(real_db):
    """A CA$12,000/year price must not be added to a MONTHLY figure as if it were monthly."""
    _DB_RAN["n"] += 1
    comp = bdr._mrr_composition(real_db["cur"])
    assert comp["stripe_total_cents"] is not None
    assert comp["stripe_total_cents"] < 1200000, (
        "an annual price was summed into MRR — the total overstates monthly revenue")
    assert comp["non_monthly_subscriptions"] >= 1


def test_mrr_counts_the_snapshot_and_catalog_rows_exactly(real_db):
    _DB_RAN["n"] += 1
    comp = bdr._mrr_composition(real_db["cur"])
    rows = bdr._read_stripe_state(real_db["cur"])
    expected = sum(
        r["amount_cents"] for r in rows
        if r["status"] in ("active", "trialing")
        and r["amount_cents"] is not None and r["billing_interval"] == "month"
    )
    assert comp["totals_by_currency"].get("CAD") == expected


def test_mrr_snapshot_returns_the_shape_the_renderer_reads(real_db, monkeypatch):
    """The deferral to stripe_state.mrr_snapshot() used to return {total_cents,…} while
    billing.js reads stripe_total_cents/local_total_cents/has_drift — every key absent, so the
    MRR card rendered CA$NaN and the DB fallback was unreachable dead code."""
    _DB_RAN["n"] += 1
    monkeypatch.setenv("POSTGRES_DSN", _DSN)
    snap = bdr.mrr_snapshot()
    for key in ("stripe_total_cents", "local_total_cents", "has_drift", "drift_cents",
                "complete", "unknown_subscriptions", "amount_sources", "collected_net_cents"):
        assert key in snap, f"renderer key {key!r} missing from /admin/billing/mrr"
    assert snap.get("error") is None
    # The COLLECTED figure (net settled charges) must not be the MRR total — they are different
    # measurements and conflating them is what the old deferral did.
    assert snap["collected_net_cents"] != snap["stripe_total_cents"]


# ── dunning age: derived from real evidence, not a missing column ────────────────

def test_dunning_age_comes_from_the_append_only_event_ledger(real_db):
    """`first_past_due_at` never existed. The age is read from public.stripe_events, and the
    EARLIEST failure must win — a later redelivery must not reset the clock to '0 days'."""
    _DB_RAN["n"] += 1
    rows = bdr._read_dunning(real_db["cur"])
    row = _row_for(rows, "subscription_id", real_db["sub"]("dun"))
    assert row["days_in_dunning"] == 9, (
        f"expected the EARLIEST failure (9d) to set the age, got {row['days_in_dunning']}")
    assert row["dunning_since"] is not None


def test_dunning_age_is_unknown_when_the_ledger_has_no_evidence(real_db):
    """No event on record ⇒ None ⇒ the renderer shows '—'. Never a fabricated 0 days."""
    _DB_RAN["n"] += 1
    assert bdr._dunning_since(real_db["cur"], [f"sub_{_TAG}_no_such_subscription"]) == {}


# ── the other four broken reads ──────────────────────────────────────────────────

def test_lifecycle_reads_received_at_and_lifts_account_id_from_the_payload(real_db):
    _DB_RAN["n"] += 1
    rows = bdr._read_lifecycle_events(real_db["cur"], "customer.subscription.updated", 50)
    mine = [r for r in rows if r["event_id"] == f"evt_{_TAG}_meta"]
    assert mine, "the seeded event did not come back — ORDER BY/received_at is wrong"
    row = mine[0]
    assert row["received_at"] and row["created_at"] == row["received_at"]
    assert row["account_id"] == f"acct-{_TAG}", "account_id was not lifted from payload metadata"
    assert row["processing_status"] == "error"
    # `summary` is not a column; it must carry the ledger's own facts, not invented prose.
    assert "boom: adapter timeout" in row["summary"]


def test_refund_invoice_id_is_recovered_from_the_parent_charge(real_db):
    """stripe_refunds has no invoice_id; the parent charge does. billing.js renders it as the
    Stripe invoice link, so losing it would silently drop a working control."""
    _DB_RAN["n"] += 1
    rows = bdr._read_refunds(real_db["cur"])
    row = _row_for(rows, "refund_id", f"re_{_TAG}")
    assert row["amount_cents"] == 5000
    assert row["currency"] == "CAD"
    assert row["invoice_id"] == f"in_paid_{_TAG}"


def test_provider_health_last_webhook_is_populated_not_permanently_none(real_db):
    """`MAX(created_at)` raised UndefinedColumn into a bare except, so last_webhook_received_at
    was permanently None and `stale` permanently True — a health card that could only cry wolf."""
    _DB_RAN["n"] += 1
    health = bdr._provider_health(real_db["cur"])
    assert health["last_webhook_received_at"] is not None
    assert health["stale"] is False, "a webhook received minutes ago still reported stale"


# ── the WRITE side: stripe_state records what Stripe charges ─────────────────────

def test_upsert_subscription_records_the_observed_recurring_amount(real_db, monkeypatch):
    _DB_RAN["n"] += 1
    monkeypatch.setenv("POSTGRES_DSN", _DSN)
    cur, sub_id = real_db["cur"], real_db["sub"]("write1")
    obj = {"id": sub_id, "items": {"data": [
        {"quantity": 3, "price": {"unit_amount": 2500, "currency": "cad",
                                  "billing_scheme": "per_unit",
                                  "recurring": {"interval": "month", "interval_count": 1,
                                                "usage_type": "licensed"}}}]}}
    rec = _stripe_state.recurring_from_subscription_object(obj)
    assert rec == {"amount_cents": 7500, "currency": "cad",
                   "billing_interval": "month", "billing_interval_count": 1}
    assert _stripe_state.upsert_subscription({
        "subscription_id": sub_id, "customer_id": real_db["cus"]("snap"),
        "status": "active", "recurring": rec}) is True
    cur.execute("SELECT amount_cents, currency, billing_interval FROM "
                "public.stripe_subscriptions WHERE subscription_id = %s", (sub_id,))
    assert cur.fetchone() == (7500, "cad", "month")


def test_a_tiered_price_records_unknown_not_zero(real_db):
    """Stripe: `unit_amount` is "Only set if billing_scheme=per_unit". A tiered price therefore
    has no unit amount, and the honest snapshot is NULL — a 0 would invent a free subscription."""
    _DB_RAN["n"] += 1
    for obj in (
        {"items": {"data": [{"quantity": 1, "price": {
            "unit_amount": None, "currency": "cad", "billing_scheme": "tiered",
            "recurring": {"interval": "month", "usage_type": "licensed"}}}]}},
        {"items": {"data": [{"quantity": None, "price": {
            "unit_amount": 100, "currency": "cad", "billing_scheme": "per_unit",
            "recurring": {"interval": "month", "usage_type": "metered"}}}]}},
        # items disagreeing on currency: the sum would be arithmetic on incomparable units.
        {"items": {"data": [
            {"quantity": 1, "price": {"unit_amount": 100, "currency": "cad",
                                      "billing_scheme": "per_unit",
                                      "recurring": {"interval": "month", "usage_type": "licensed"}}},
            {"quantity": 1, "price": {"unit_amount": 100, "currency": "usd",
                                      "billing_scheme": "per_unit",
                                      "recurring": {"interval": "month", "usage_type": "licensed"}}}]}},
    ):
        rec = _stripe_state.recurring_from_subscription_object(obj)
        assert rec is not None and rec["amount_cents"] is None, f"expected UNKNOWN for {obj}"


def test_an_event_that_cannot_state_the_amount_preserves_the_known_one(real_db, monkeypatch):
    """invoice.paid carries an Invoice, not a Subscription — it cannot restate the price, so it
    must PRESERVE the snapshot rather than blank it (recurring=None ⇒ COALESCE)."""
    _DB_RAN["n"] += 1
    monkeypatch.setenv("POSTGRES_DSN", _DSN)
    cur, sub_id = real_db["cur"], real_db["sub"]("write2")
    assert _stripe_state.upsert_subscription({
        "subscription_id": sub_id, "customer_id": real_db["cus"]("snap"), "status": "active",
        "recurring": {"amount_cents": 4200, "currency": "cad",
                      "billing_interval": "month", "billing_interval_count": 1}}) is True
    # An Invoice object has no `items` envelope at all → None → "did not state it".
    assert _stripe_state.recurring_from_subscription_object({"object": "invoice"}) is None
    assert _stripe_state.upsert_subscription({
        "subscription_id": sub_id, "customer_id": real_db["cus"]("snap"),
        "status": "active", "recurring": None}) is True
    cur.execute("SELECT amount_cents FROM public.stripe_subscriptions WHERE subscription_id = %s",
                (sub_id,))
    assert cur.fetchone()[0] == 4200, "a silent event blanked a known amount"


# ── the skip guard ───────────────────────────────────────────────────────────────

def test_zzz_no_db_test_was_silently_skipped():
    """RED if the real-schema half did not run. The whole lesson of this bug is that a suite
    which quietly stops exercising the database keeps reporting green over a broken query."""
    assert _DB_RAN["n"] >= 17, (
        f"only {_DB_RAN['n']} of 17 real-schema tests ran — the DB half was skipped, which is "
        f"exactly how `column s.amount does not exist` shipped under a 16/16 green suite. "
        f"Pass POSTGRES_DSN pointing at a throwaway DB with migrations >= 131 applied.")
