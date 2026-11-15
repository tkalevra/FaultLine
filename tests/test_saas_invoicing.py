"""SaaS — itemised invoice PDF + monthly invoice scheduler (migration 171).

Covers:
  * invoice MODEL build from entitlements (tenant / clinical), stable invoice number, tax
    placeholder (rate 0 → $0, MoR note), line items + total match entitlements;
  * dependency-free PDF renderer produces a valid, non-empty %PDF with the total in it;
  * scheduler: skips no-billing-email / exempt / zero-total accounts, is idempotent (never
    double-sends a period), fail-safe when SMTP is off;
  * billing-contact / send-day setters (real DB) are can_view_billing-scoped at the router.

Pure/mocked tests run with no server. The DB-integration tests use the LOCAL control plane and
SKIP when POSTGRES_DSN is unreachable. Run:
    POSTGRES_DSN=postgresql://faultline:faultline@<pg-ip>:5432/faultline \
      python3 -m pytest tests/test_saas_invoicing.py -q   (tests/ gitignored → git add -f)
"""

import os
import uuid

import pytest

from saas import invoice_pdf as pdf
from saas import invoicing
from saas import invoice_scheduler as sched


# ════════════════════════════════════════════════════════════════════════════════════
# PURE — invoice number, PDF renderer (no DB)
# ════════════════════════════════════════════════════════════════════════════════════

def test_invoice_number_is_stable_and_derived():
    a = "abcd1234-0000-4000-8000-000000000000"
    n1 = invoicing.invoice_number(a, "2026-07")
    n2 = invoicing.invoice_number(a, "2026-07")
    assert n1 == n2 == "INV-ABCD1234-202607"
    assert invoicing.invoice_number(a, "2026-08") != n1          # period-scoped


def test_valid_period_defaults_and_normalises():
    assert invoicing._valid_period("2026-03") == "2026-03"
    assert invoicing._valid_period("garbage").count("-") == 1     # falls back to a real period
    assert invoicing._valid_period("2026-13") != "2026-13"        # invalid month → current


def test_pdf_is_valid_and_contains_total():
    inv = {
        "invoice_number": "INV-TEST0001-202607", "issue_date": "2026-07-12",
        "period": "2026-07", "period_label": "July 2026", "due_date": "2026-08-11",
        "terms": "Net 30", "currency": "CAD",
        "seller": {"legal_name": "Volenti", "product": "FaultLine", "address": ["Canada"]},
        "bill_to": {"account_id": "x", "account_name": "Acme", "billing_contact": "f@acme.test"},
        "line_items": [
            {"description": "Base package - 5 Standard (included)", "quantity": 5,
             "unit_price": 1500.0, "amount": 1500.0},
            {"description": "Expert add-on x 3", "quantity": 3, "unit_price": 45.0,
             "amount": 135.0},
        ],
        "subtotal": 1635.0,
        "tax": {"label": "Tax (GST/HST)", "rate": 0.0, "amount": 0.0, "note": "MoR."},
        "total": 1635.0, "footer": ["Volenti - FaultLine."],
    }
    b = pdf.render_invoice_pdf(inv)
    assert b.startswith(b"%PDF-1.") and b.rstrip().endswith(b"%%EOF")
    assert len(b) > 800
    # the money strings are drawn as literal PDF text — present verbatim in the content stream
    assert b"$1,635.00" in b and b"INVOICE" in b and b"Volenti" in b


def test_text_width_bold_wider_than_regular_for_caps():
    assert pdf.text_width("MEMORY", 10, bold=True) > 0
    assert pdf.text_width("W", 10, bold=True) >= pdf.text_width("i", 10, bold=True)


# ════════════════════════════════════════════════════════════════════════════════════
# invoice MODEL from a mocked control plane (no live DB)
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_cp(monkeypatch):
    """Feed invoicing a known billing_view + account identity without a DB."""
    ent = invoicing._entitlements
    monkeypatch.setattr(ent, "_account_kind", lambda a: "tenant")
    monkeypatch.setattr(ent, "_account_tax", lambda a: {"tax_rate": 0.0, "tax_amount": 0.0})
    monkeypatch.setattr(ent, "billing_view", lambda a: {
        "exempt": False, "monthly_subtotal": 1715.0,
        "lines": [
            {"seat_type": "standard", "kind": "base", "quantity": 5, "unit_price": 1500.0,
             "monthly": 1500.0, "label": "Base package - 5 Standard (included)"},
            {"seat_type": "expert", "kind": "addon", "quantity": 3, "unit_price": 45.0,
             "monthly": 135.0, "label": "Expert add-on x 3"},
            {"seat_type": "agent", "kind": "addon", "quantity": 1, "unit_price": 80.0,
             "monthly": 80.0, "label": "Agent add-on x 1"},
        ],
    })
    monkeypatch.setattr(invoicing._accounts, "get_account_name", lambda a: "HelpDeskPro Inc.")
    monkeypatch.setattr(invoicing._accounts, "get_billing_contact_email",
                        lambda a: "finance@hdp.example")
    monkeypatch.delenv("SAAS_INVOICE_TAX_RATE", raising=False)
    return "aaaa1111-0000-4000-8000-000000000000"


def test_build_invoice_tenant_lines_and_total(mock_cp):
    inv = invoicing.build_invoice(mock_cp, "2026-07")
    assert inv is not None
    assert inv["invoice_number"] == "INV-AAAA1111-202607"
    assert inv["seller"]["legal_name"] == "Volenti"
    assert inv["seller"]["product"] == "FaultLine"
    assert inv["bill_to"]["account_name"] == "HelpDeskPro Inc."
    assert inv["bill_to"]["billing_contact"] == "finance@hdp.example"
    assert len(inv["line_items"]) == 3
    assert inv["subtotal"] == 1715.0
    # tax placeholder: rate 0 → $0, MoR note present
    assert inv["tax"]["rate"] == 0.0 and inv["tax"]["amount"] == 0.0
    assert "merchant of record" in inv["tax"]["note"].lower()
    assert inv["total"] == 1715.0
    assert inv["terms"].startswith("Net")
    # renders
    assert pdf.render_invoice_pdf(inv).startswith(b"%PDF")


def test_build_invoice_tax_rate_env_placeholder(monkeypatch, mock_cp):
    monkeypatch.setenv("SAAS_INVOICE_TAX_RATE", "0.13")     # placeholder GST/HST
    inv = invoicing.build_invoice(mock_cp, "2026-07")
    assert inv["tax"]["rate"] == 0.13
    assert inv["tax"]["amount"] == pytest.approx(1715.0 * 0.13, rel=1e-6)
    assert inv["total"] == pytest.approx(round(1715.0 + 1715.0 * 0.13, 2))
    assert "placeholder" in inv["tax"]["note"].lower()


def test_build_invoice_clinical_itemises_base_and_overage(monkeypatch):
    ent = invoicing._entitlements
    monkeypatch.setattr(ent, "_account_kind", lambda a: "clinical")
    monkeypatch.setattr(ent, "_account_tax", lambda a: {"tax_rate": 0.0, "tax_amount": 0.0})
    monkeypatch.setattr(ent, "clinical_base_price", lambda: 4000.0)
    monkeypatch.setattr(ent, "clinical_included_patients", lambda: 250)
    monkeypatch.setattr(ent, "patient_unit_price", lambda n: 8.0)
    monkeypatch.setattr(ent, "get_entitlements", lambda a: {})
    import saas.clinical as _clin
    # ⚠️ THIS STUB USED TO BE ``{"licensed": 300}`` — no ``included_patients`` KEY AT ALL, which
    # is the field the builder is being tested on. With it absent the builder fell through to the
    # global default, so this test passed BYTE-IDENTICALLY whether the per-account allotment was
    # honoured or ignored: it could never have failed on the defect it appears to cover, and did
    # not, while a clinic seeded at 400 was invoiced CA$900/mo of overage it did not owe. A stub
    # that omits the field under test cannot fail on it. The key is now present and equal to the
    # default, so the arithmetic below is unchanged and the assertion means something; the
    # per-account cases where it DIFFERS from the default are covered against a real database in
    # tests/test_console_prices_trace_to_the_engine.py, because that is the only way to prove the
    # builder reads the row rather than a stub somebody wrote.
    monkeypatch.setattr(_clin, "clinical_plan",
                        lambda a: {"licensed": 300, "included_patients": 250})
    monkeypatch.setattr(invoicing._accounts, "get_account_name", lambda a: "Clinic")
    monkeypatch.setattr(invoicing._accounts, "get_billing_contact_email", lambda a: "b@clinic.test")
    monkeypatch.delenv("SAAS_INVOICE_TAX_RATE", raising=False)

    inv = invoicing.build_invoice("cccc2222-0000-4000-8000-000000000000", "2026-07")
    assert inv["account_kind"] == "clinical"
    descs = [li["description"] for li in inv["line_items"]]
    assert any("base package" in d.lower() for d in descs)
    assert any("overage" in d.lower() for d in descs)
    # base 4000 + 50 overage patients * $8 = 4400
    assert inv["subtotal"] == 4400.0 and inv["total"] == 4400.0


# ════════════════════════════════════════════════════════════════════════════════════
# SCHEDULER — skip rules + idempotency (mocked send path)
# ════════════════════════════════════════════════════════════════════════════════════

def test_scheduler_skips_when_no_billing_email(monkeypatch):
    monkeypatch.setattr(sched._invoicing, "build_invoice", lambda a, p=None: {
        "period": "2026-07", "exempt": False, "total": 1500.0,
        "invoice_number": "INV-X-202607", "currency": "CAD",
        "bill_to": {"billing_contact": None},
    })
    r = sched.send_account_invoice("acct", "2026-07")
    assert r["status"] == "skipped" and "billing_contact" in r["reason"]


def test_scheduler_skips_exempt_and_zero_total(monkeypatch):
    monkeypatch.setattr(sched._invoicing, "build_invoice", lambda a, p=None: {
        "period": "2026-07", "exempt": True, "total": 0.0,
        "invoice_number": "INV-X-202607", "currency": "CAD",
        "bill_to": {"billing_contact": "x@y.test"}})
    assert sched.send_account_invoice("acct", "2026-07")["status"] == "skipped"


def test_scheduler_idempotent_second_run_skips(monkeypatch):
    """Simulate the claim ledger: first claim 'claimed' → send; second claim 'already_sent' → skip.
    smtp is stubbed OK; render is real."""
    inv = {
        "period": "2026-07", "exempt": False, "total": 1500.0,
        "invoice_number": "INV-X-202607", "currency": "CAD",
        "issue_date": "2026-07-12", "period_label": "July 2026", "due_date": "2026-08-11",
        "terms": "Net 30", "seller": {"legal_name": "Volenti", "address": []},
        "bill_to": {"billing_contact": "x@y.test", "account_name": "Y", "account_id": "acct"},
        "line_items": [{"description": "Base", "quantity": 1, "unit_price": 1500.0,
                        "amount": 1500.0}],
        "subtotal": 1500.0, "tax": {"label": "Tax", "amount": 0.0, "note": ""}, "footer": [],
    }
    monkeypatch.setattr(sched._invoicing, "build_invoice", lambda a, p=None: dict(inv))
    state = {"claimed_once": False, "sends": 0}

    def fake_claim(aid, period, num, to, total, cur):
        if not state["claimed_once"]:
            state["claimed_once"] = True
            return "claimed"
        return "already_sent"

    monkeypatch.setattr(sched, "_claim", fake_claim)
    monkeypatch.setattr(sched, "_mark", lambda *a, **k: None)

    def fake_send(to, invoice, pdf_bytes):
        state["sends"] += 1
        assert pdf_bytes.startswith(b"%PDF")
        return {"ok": True}

    monkeypatch.setattr(sched._smtp, "send_invoice_email", fake_send)

    r1 = sched.send_account_invoice("acct", "2026-07")
    r2 = sched.send_account_invoice("acct", "2026-07")
    assert r1["status"] == "sent"
    assert r2["status"] == "skipped" and "already sent" in r2["reason"]
    assert state["sends"] == 1                                 # emailed exactly ONCE


def test_scheduler_failsafe_when_smtp_off(monkeypatch):
    inv = {"period": "2026-07", "exempt": False, "total": 1500.0,
           "invoice_number": "INV-X-202607", "currency": "CAD",
           "issue_date": "d", "period_label": "July 2026", "due_date": "d", "terms": "Net 30",
           "seller": {"legal_name": "Volenti", "address": []},
           "bill_to": {"billing_contact": "x@y.test", "account_name": "Y", "account_id": "a"},
           "line_items": [], "subtotal": 1500.0,
           "tax": {"label": "Tax", "amount": 0.0, "note": ""}, "footer": []}
    monkeypatch.setattr(sched._invoicing, "build_invoice", lambda a, p=None: dict(inv))
    monkeypatch.setattr(sched, "_claim", lambda *a, **k: "claimed")
    monkeypatch.setattr(sched, "_mark", lambda *a, **k: None)
    monkeypatch.setattr(sched._smtp, "send_invoice_email",
                        lambda *a, **k: {"ok": False, "error": "SMTP is not configured"})
    r = sched.send_account_invoice("a", "2026-07")
    assert r["status"] == "failed" and "SMTP" in r["reason"]  # captured, no crash


# ════════════════════════════════════════════════════════════════════════════════════
# DB-INTEGRATION — real local control plane (skips if POSTGRES_DSN unreachable)
# ════════════════════════════════════════════════════════════════════════════════════

def _db_available() -> bool:
    if not os.environ.get("POSTGRES_DSN"):
        return False
    try:
        from saas import db as _db
        with _db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.invoice_sends')")
                return cur.fetchone()[0] is not None
    except Exception:
        return False


dbonly = pytest.mark.skipif(not _db_available(),
                            reason="POSTGRES_DSN control plane / migration 171 not reachable")


def _purge(aid: str) -> None:
    """Remove a throwaway test account. account_ledger is append-only (a BEFORE DELETE trigger
    blocks the cascade), so we disable session triggers for the delete (bootstrap DB user is
    superuser locally)."""
    from saas import db as _db
    with _db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET session_replication_role = replica")
            cur.execute("DELETE FROM public.invoice_sends WHERE account_id = %s", (aid,))
            cur.execute("DELETE FROM public.accounts WHERE account_id = %s", (aid,))
            cur.execute("SET session_replication_role = origin")
        conn.commit()


@dbonly
def test_db_end_to_end_invoice_matches_entitlements():
    from saas import accounts as acc
    from saas import entitlements as ent
    from saas import db as _db

    slug = "invtest-" + uuid.uuid4().hex[:8]
    created = acc.create_account("Invoice Test Co", slug, account_kind="tenant")
    aid = created["account_id"]
    try:
        ent.seed_base_entitlement(aid)                        # base-5 standard + $1500 base
        ent.provision_capacity(aid, "expert", 2, signature="Test Op", actor_user="op")
        acc.set_billing_contact_email(aid, "finance@invtest.example")

        inv = invoicing.build_invoice(aid, "2026-07")
        assert inv is not None and inv["account_kind"] == "tenant"
        # base $1500 + 2 expert @ $45 = $1590 (Enterprise expert rate)
        assert inv["subtotal"] == 1590.0
        assert inv["bill_to"]["billing_contact"] == "finance@invtest.example"
        assert inv["bill_to"]["account_name"] == "Invoice Test Co"
        b = pdf.render_invoice_pdf(inv)
        assert b.startswith(b"%PDF") and b"$1,590.00" in b

        # scheduler dry-run reports it would send; no ledger row written
        dry = sched.send_account_invoice(aid, "2026-07", dry_run=True)
        assert dry["status"] == "dry_run" and dry["total"] == 1590.0
    finally:
        _purge(aid)


@dbonly
def test_db_send_day_setter_roundtrip():
    from saas import accounts as acc
    from saas import db as _db
    slug = "invday-" + uuid.uuid4().hex[:8]
    aid = acc.create_account("Send Day Co", slug)["account_id"]
    try:
        assert acc.get_invoice_send_day(aid) == 1              # default
        assert acc.set_invoice_send_day(aid, 15)["ok"]
        assert acc.get_invoice_send_day(aid) == 15
        assert acc.set_invoice_send_day(aid, 31)["status_code"] == 400   # capped at 28
    finally:
        _purge(aid)


@dbonly
def test_db_scheduler_idempotency_ledger():
    """A real claim → mark sent → re-claim returns already_sent (the UNIQUE firewall)."""
    from saas import accounts as acc
    from saas import db as _db
    slug = "invidem-" + uuid.uuid4().hex[:8]
    aid = acc.create_account("Idem Co", slug)["account_id"]
    try:
        c1 = sched._claim(aid, "2026-07", "INV-1", "x@y.test", 1500.0, "CAD")
        assert c1 == "claimed"
        sched._mark(aid, "2026-07", "sent")
        assert sched.already_sent(aid, "2026-07") is True
        c2 = sched._claim(aid, "2026-07", "INV-1", "x@y.test", 1500.0, "CAD")
        assert c2 == "already_sent"
    finally:
        _purge(aid)
