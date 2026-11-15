"""Mark as paid — a human assertion about money, so it must be signed, noted, and non-distorting.

Payment capture is still a stub (`TODO(lemon-squeezy)`), so "this invoice is settled" carries no
machine evidence. Recording it is therefore held to a higher bar than the charge levers: BOTH a
typed-name signature AND a written note are required, and the identity that lands on the row comes
from the signed OPERATOR SESSION, never the request body.

THE NON-OBVIOUS HALF — and the reason this file exists at all. `ledger_summary` totalled EVERY
row's `amount` into a period total and a `grand_total` the Billing view labels as what the account
was CHARGED. A payment carries a real amount (so it stays queryable and reconcilable instead of
buried in free text), so summing it there would report the account as having been charged MORE the
moment someone recorded that they PAID. The write would look perfect and a different number would
go wrong — verify the read, not the write.
"""
from unittest.mock import MagicMock, patch

import pytest

from saas import entitlements as E


# ── the guard rails on the write ──────────────────────────────────────────────────

def test_a_payment_without_a_signature_is_refused():
    """Who asserted this? Without that, the record has no value."""
    out = E.record_payment("acct-1", 100.0, signature="", note="cash in hand")
    assert out["status_code"] == 400 and "signature" in out["reason"]


def test_a_payment_without_a_note_is_refused():
    """On what basis? A signature alone records WHO, never WHY — and 'why' is the audit trail."""
    out = E.record_payment("acct-1", 100.0, signature="Jane Operator", note="  ")
    assert out["status_code"] == 400 and "note" in out["reason"]


@pytest.mark.parametrize("bad", [0, -5, "abc", None])
def test_a_non_positive_or_unparseable_amount_is_refused(bad):
    out = E.record_payment("acct-1", bad, signature="Jane Operator", note="e-transfer")
    assert out["status_code"] == 400


def test_payment_is_a_billable_action_so_the_ledger_itself_enforces_the_signature():
    """Defence in depth. `record_payment` validates, but a FUTURE call site could write the row
    directly — `_write_ledger` must refuse an unsigned payment regardless of who calls it."""
    assert "payment" in E.BILLABLE_ACTIONS
    with pytest.raises(E.SignatureRequired):
        E._write_ledger(MagicMock(), "acct-1", "payment", None, 0, 0.0, 100.0,
                        12, "operator", "note", signature=None)


# ── the aggregation: a credit must never read as a charge ─────────────────────────

def _entries(*rows):
    """rows: (action, amount, period)"""
    return [{"action": a, "amount": amt, "period": p} for a, amt, p in rows]


def test_a_payment_does_not_inflate_what_the_account_was_charged():
    """THE regression. Before the split, recording a $1500 payment made grand_total read $1500
    HIGHER — i.e. paying your bill looked like being billed again."""
    with patch.object(E, "ledger_entries", lambda *a, **k: _entries(
            ("provision", 1500.0, "2026-08"), ("payment", 1500.0, "2026-08"))):
        s = E.ledger_summary("acct-1")
    assert s["grand_total"] == 1500.0, "charges only"
    assert s["paid_total"] == 1500.0
    assert s["balance"] == 0.0


def test_the_balance_is_charges_minus_payments():
    with patch.object(E, "ledger_entries", lambda *a, **k: _entries(
            ("provision", 1000.0, "2026-07"), ("upgrade", 500.0, "2026-08"),
            ("payment", 600.0, "2026-08"))):
        s = E.ledger_summary("acct-1")
    assert (s["grand_total"], s["paid_total"], s["balance"]) == (1500.0, 600.0, 900.0)


def test_per_period_totals_are_split_too_not_just_the_grand_total():
    """The Billing view renders PER PERIOD. Splitting only the grand total would leave each
    period row still overstating what was charged that month."""
    with patch.object(E, "ledger_entries", lambda *a, **k: _entries(
            ("provision", 200.0, "2026-08"), ("payment", 50.0, "2026-08"))):
        s = E.ledger_summary("acct-1")
    aug = [p for p in s["periods"] if p["period"] == "2026-08"][0]
    assert aug["total"] == 200.0 and aug["paid"] == 50.0
    assert len(aug["entries"]) == 2, "the payment still APPEARS in the period's entries"


def test_an_account_with_no_payments_is_unchanged():
    """The control: existing accounts must read exactly as before, balance == charged."""
    with patch.object(E, "ledger_entries", lambda *a, **k: _entries(
            ("provision", 300.0, "2026-08"), ("base_grant", 0.0, "2026-08"))):
        s = E.ledger_summary("acct-1")
    assert s["grand_total"] == 300.0 and s["paid_total"] == 0.0 and s["balance"] == 300.0
