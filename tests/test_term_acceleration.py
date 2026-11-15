"""TERM ACCELERATION on cancellation — strip the seats, accelerate only the base, two ways out.

WHAT THIS SUITE PINS (migration 253 §3, "If your subscription ends before your term does"):
  * the ELASTIC ITEMS ARE STRIPPED FIRST and the strip is VERIFIED by re-reading Stripe. This is
    the safety property: if the seats are not on the subscription, no arithmetic downstream can
    accelerate them. A test must go red if the strip is skipped.
  * the amount is ``remaining_committed_months x SIGNED base fee`` — the amount on the signed
    contract, never the live card rate. A test must go red if the live rate is substituted.
  * TWO resolutions, both reachable: SETTLE zeroes the balance; RESUME voids the invoice IN FULL,
    reverses the receivable, and clears the scheduled cancellation. A test must go red if the
    invoice is left open on resume.

🔴 THE ACCOUNTING ASSERTIONS READ THE REAL BILLING VIEW, NOT THE TABLE. The failure this feature
could most easily cause is a figure the operator reads as "charged" that we never charged. So the
four states are asserted through ``entitlements.ledger_summary`` and ``entitlements.billing_view``
— the same functions ``saas/dashboard/app.js`` renders as "Ledger — charged … paid … balance …" —
rather than by SELECTing the rows back. A previous defect in this exact area survived a green
suite precisely because the tests asserted the dict the code had just written.

⚠️ THE LIVE LANE IS REAL. PART C creates real TEST-MODE Stripe customers, subscriptions, prices
and invoices, and drives the real adapter. Only Stripe's own billing engine can testify that
stripping items mints no credit note, that a one-off invoice does not contaminate the subscription
invoice, and that clearing ``cancel_at`` does not re-anchor the cycle. It NEVER touches live mode —
the adapter refuses a live credential at construction and PART A asserts that guard is intact.

RUN IT (never raw pytest — owner rule):

    PYTHONPATH="src:<venv site-packages>" \\
    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_cancelaccel_test \\
    ACCEL_STRIPE_KEY=sk_test_... \\
    python3 tools/fltest.py --bug CANCELACCEL --test tests/test_term_acceleration.py

FALSE-GREEN GUARDS: ``_DB_RAN`` / ``_STRIPE_RAN`` are set by their fixtures and asserted by the
last two tests. A DSN-gated suite that silently skips half of itself reports "N passed, M skipped"
and looks green; that has produced a false green in this repo repeatedly.
"""

import inspect
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from saas import entitlements as ent_mod              # noqa: E402
from saas import payments_stripe as ps_mod            # noqa: E402
from saas import plans as plans_mod                   # noqa: E402
from saas import stripe_seat_sync as sync_mod         # noqa: E402
from saas import term_acceleration as accel_mod       # noqa: E402

DEFAULT_TEST_DSN = ("postgresql://faultline:faultline@172.20.0.3:5432/"
                    "faultline_cancelaccel_test")
TEST_DSN = os.environ.get("ACCEL_TEST_DSN", os.environ.get("POSTGRES_DSN", DEFAULT_TEST_DSN))
STRIPE_KEY = os.environ.get("ACCEL_STRIPE_KEY") or ""

_DB_RAN = {"value": False}
_STRIPE_RAN = {"value": False}

ENTERPRISE_BASE_CENTS = 150000          # faultline_enterprise_base_monthly = CA$1,500.00
TENANT_STD_SEAT_CENTS = 1000            # faultline_seat_tenant_standard_monthly = CA$10.00
TENANT_EXPERT_SEAT_CENTS = 4500         # faultline_seat_tenant_expert_monthly = CA$45.00
AGENT_ADDON_5_CENTS = 16000             # faultline_agent_addon_5 = CA$160.00


def _source_without_docstring(fn) -> str:
    """Function source with its docstring REMOVED.

    A plain ``inspect.getsource`` grep sees the prose too, so a test that greps for a forbidden
    token passes on the docstring that EXPLAINS why the token is forbidden while the code happily
    uses it. (Recorded lesson; paid for more than once in this repo.) ``inspect.getdoc`` cannot be
    used to subtract it — that dedents, so it no longer matches the indented source."""
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]
    return ast.unparse(tree)


def _test_provider_config(secret: str) -> dict:
    """The adapter resolves ``api_secret`` / ``api_key`` / ``webhook_secret`` — NOT
    ``secret_key``. A wrong key name silently yields ``is_configured() == False``, which reads as
    "Stripe is down" rather than "the fixture is wrong", so it is built in one place."""
    return {"api_secret": secret, "api_key": "pk_test_suite",
            "webhook_secret": "whsec_test_suite", "mode": "test", "currency": "CAD"}


# ════════════════════════════════════════════════════════════════════════════════════
# PART A — pure decisions. No DB, no network.
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_live_credential_guard_is_still_intact():
    """Everything below drives a REAL Stripe account. The one thing that must never be true is
    that it could be the LIVE one. Asserted structurally, not by reading the docstring."""
    src = inspect.getsource(ps_mod.StripePaymentProvider.__init__)
    assert "_is_live_secret" in src
    assert ps_mod._is_live_secret("sk_live_abc") is True
    assert ps_mod._is_live_secret("sk_test_abc") is False


def test_months_remaining_counts_whole_calendar_months_and_never_a_partial_one():
    """A partial trailing month must NOT be billed as a full one — on a collections path the
    rounding belongs to the customer. And the arithmetic is CALENDAR, not 30-day: a 6-month term
    starting the 31st has to land on a real date."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    assert accel_mod.months_remaining(now, None) == 0
    assert accel_mod.months_remaining(now, now) == 0
    assert accel_mod.months_remaining(now, sync_mod._add_months(now, 6)) == 6
    # one day short of the 6-month boundary is FIVE whole months, not six
    assert accel_mod.months_remaining(
        now, sync_mod._add_months(now, 6) - timedelta(days=1)) == 5
    # a term end in the PAST owes nothing
    assert accel_mod.months_remaining(now, now - timedelta(days=90)) == 0
    # Jan 31 + 1 month clamps into February rather than overflowing
    jan31 = datetime(2026, 1, 31, tzinfo=timezone.utc)
    assert sync_mod._add_months(jan31, 1).month == 2


def test_the_month_count_is_ONE_counter_shared_with_the_broken_commitment_ledger():
    """The arrears recorded in the 'commitment_broken' ledger row and the amount demanded on the
    customer's final invoice are two documents about ONE debt. They must be computed by the same
    function or they will eventually quote different numbers."""
    src = inspect.getsource(sync_mod.check_commitment)
    assert "months_remaining" in src, "check_commitment must defer to the shared counter"
    assert "while months_unpaid < 121" not in src, (
        "check_commitment has re-grown its own inline month loop — that is the second copy this "
        "test exists to prevent")


def test_the_three_acceleration_actions_are_registered_and_none_is_billable():
    """A billable action demands a typed-name signature. The authorization for an acceleration is
    the SIGNED CONTRACT, not a fresh typed name — and demanding one would make the non-payment
    lane unreachable, because no human is present when dunning ends a subscription."""
    for a in ("term_acceleration", "term_acceleration_settled", "term_acceleration_voided"):
        assert a in ent_mod.LEDGER_ACTIONS
        assert a not in ent_mod.BILLABLE_ACTIONS


def test_only_the_settlement_is_a_payment_action():
    """The charge and the void must move ``grand_total`` (the figure rendered as "charged"); the
    settlement must move ``paid_total`` instead. Getting this wrong reports the acceleration as
    having been charged twice, or as never charged at all."""
    assert "term_acceleration_settled" in ent_mod.PAYMENT_ACTIONS
    assert "term_acceleration" not in ent_mod.PAYMENT_ACTIONS
    assert "term_acceleration_voided" not in ent_mod.PAYMENT_ACTIONS
    # and the pre-existing split is untouched
    assert "payment" in ent_mod.PAYMENT_ACTIONS


def test_commitment_broken_still_carries_amount_zero():
    """The state BEFORE this feature runs is 'owed but not invoiced', and that is NOT a receivable.
    This suite must not have quietly turned it into one."""
    src = inspect.getsource(sync_mod.check_commitment)
    assert "amount 0 by design" in src
    assert "commitment_broken" not in ent_mod.PAYMENT_ACTIONS
    assert "commitment_broken" not in ent_mod.BILLABLE_ACTIONS


def test_the_invoice_line_states_the_months_the_amount_and_the_exclusion():
    """The contract promises an invoice "stating the months and the amount". A customer looking at
    a five-figure final invoice must not have to INFER that their seats are excluded."""
    d = accel_mod._invoice_description(4, ENTERPRISE_BASE_CENTS, "CAD", "2027-02-12")
    assert "4 month(s)" in d
    assert "1,500.00" in d and "6,000.00" in d
    assert "Seats and add-ons are NOT included" in d
    assert "resume" in d.lower()


def test_the_grace_floor_is_the_thirty_days_the_contract_promises():
    assert accel_mod.GRACE_DAYS == 30
    # and the invoice may not fall due before the customer's window to choose has closed
    assert accel_mod.INVOICE_DUE_DAYS >= accel_mod.GRACE_DAYS


def test_the_rollback_lever_disables_only_the_write_path():
    """A kill switch that strands an already-open final invoice is not a kill switch — the two
    resolutions must stay reachable under the flag."""
    old = os.environ.get("SAAS_TERM_ACCELERATION")
    try:
        os.environ["SAAS_TERM_ACCELERATION"] = "false"
        assert accel_mod.acceleration_enabled() is False
        r = accel_mod.accelerate(str(uuid.uuid4()))
        assert r["status"] == "disabled"
        # settle/resume do NOT consult the flag
        assert "acceleration_enabled" not in inspect.getsource(accel_mod.settle)
        assert "acceleration_enabled" not in inspect.getsource(accel_mod.resume)
    finally:
        if old is None:
            os.environ.pop("SAAS_TERM_ACCELERATION", None)
        else:
            os.environ["SAAS_TERM_ACCELERATION"] = old
    assert accel_mod.acceleration_enabled() is True


def test_the_strip_routes_through_the_single_write_seam_so_the_base_guard_applies():
    """Deleting items on a live subscription is only safe because the base-item immutability guard
    sits in the seam. A strip that called the adapter directly would bypass it."""
    src = inspect.getsource(accel_mod.strip_elastic_items)
    assert "_apply_item_mutation" in src
    assert "delete_subscription_item" not in src, (
        "the strip is calling the adapter directly and has bypassed the base-item guard")


def test_resume_voids_before_it_clears_the_cancellation():
    """Reversed, a failed void leaves the customer subscribed AND holding a live invoice for money
    they no longer owe. Pinned on the ORDER, because both calls being present is not enough."""
    src = inspect.getsource(accel_mod.resume)
    assert src.index("void_invoice") < src.index("clear_scheduled_cancellation")


def test_accelerate_books_the_receivable_only_after_stripe_confirms_the_invoice():
    """The opposite order books money as charged that was never invoiced — the exact
    active-misstatement class this feature is written around."""
    src = inspect.getsource(accel_mod.accelerate)
    assert src.index("create_one_off_invoice") < src.index('"term_acceleration"')


def test_nothing_derives_resolution_from_an_invoice_amount():
    """MEASURED: a VOIDED Stripe invoice still reports total / amount_due / amount_remaining at
    their FULL original values. Any check shaped ``amount_due == 0`` reads a voided invoice as
    still owed, and a void as unresolved."""
    for fn in (accel_mod.settle, accel_mod.resume):
        # STRIP THE DOCSTRING FIRST. A source grep sees the prose too, and both of these
        # docstrings explain the amount_due trap by name — so the naive grep would pass on the
        # explanation while the code did the wrong thing. (Recorded lesson, paid for twice.)
        src = _source_without_docstring(fn)
        assert "amount_due" not in src, f"{fn.__name__} is reading an amount to decide resolution"
    assert '!= "paid"' in inspect.getsource(accel_mod.settle)


def test_the_adapter_voids_rather_than_marking_uncollectible():
    """A resumed customer owes nothing and is not a bad debt. Marking the invoice uncollectible
    would book phantom bad debt against a paying customer."""
    src = inspect.getsource(ps_mod.StripePaymentProvider.void_invoice)
    assert "void_invoice(iid)" in src
    assert "mark_uncollectible(" not in src
    # and a DRAFT is deleted, not voided — measured: void on a draft is refused by Stripe
    assert "Invoice.delete(iid)" in src
    # a PAID invoice is refused outright rather than erased
    assert 'status == "paid"' in src


def test_the_one_off_invoice_is_created_before_its_item():
    """Item-first sweeps the accelerated base fee onto the customer's next SUBSCRIPTION invoice
    and leaves the standalone invoice empty."""
    src = inspect.getsource(ps_mod.StripePaymentProvider.create_one_off_invoice)
    assert src.index("Invoice.create(") < src.index("InvoiceItem.create(")
    assert "invoice=inv_id" in src, "the item must be pinned to OUR invoice explicitly"
    assert 'collection_method="send_invoice"' in src, (
        "charge_automatically would take the customer's choice away by pulling the full "
        "accelerated amount off their card")


def test_a_non_positive_acceleration_is_refused_not_credited():
    p = ps_mod.StripePaymentProvider(config=_test_provider_config("sk_test_x"))
    for bad in (0, -1, -150000):
        r = p.create_one_off_invoice(customer_id="cus_x", amount_cents=bad, currency="cad",
                                     description="x", days_until_due=30, idempotency_key="k")
        assert r["ok"] is False and "must be > 0" in r["reason"]


# ════════════════════════════════════════════════════════════════════════════════════
# PART B — the DB lane: the four accounting states, on the REAL billing view.
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def db_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    assert "test" in TEST_DSN, "refusing to run against a database whose name lacks 'test'"
    import psycopg2
    conn = psycopg2.connect(TEST_DSN)
    conn.autocommit = True
    _DB_RAN["value"] = True
    yield conn
    conn.close()


def _mk_account(conn, *, kind="tenant", term_months=6, months_into_term=1,
                base_cents=ENTERPRISE_BASE_CENTS, sub_id=None, with_contract=True,
                interval="month", currency="CAD"):
    """A tenant account mid-term with a signed contract bound to a Stripe subscription."""
    aid = str(uuid.uuid4())
    sub_id = sub_id or f"sub_test_{uuid.uuid4().hex[:16]}"
    # CALENDAR-anchored, not 31-day: term_end is computed in PostgreSQL as
    # set_at + make_interval(months => term_months), so a 31-day offset lands just PAST the
    # month boundary and the floor'd month count comes out one short. A real account's billing
    # anchor sits on the month grid; the fixture must too.
    # ...and a DAY inside the boundary. ``months_remaining`` floors, and ``_add_months``
    # preserves the time of day, so a set_at landing exactly N months ago makes term_end land
    # exactly 5 months from the fixture's ``now`` — which the assertion's own ``now``, a few
    # milliseconds later, has already passed. The count then flickers between 4 and 5 on a clock
    # tick. One day of slack makes the intended answer unambiguous.
    set_at = (sync_mod._add_months(datetime.now(timezone.utc), -months_into_term)
              + timedelta(days=1))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.accounts (account_id, name, slug, account_kind, term_months, "
            "  term_months_set_at, term_months_source, externally_billed, "
            "  external_billing_processor, external_billing_ref, external_billing_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'operator',TRUE,'stripe',%s,NOW())",
            (aid, f"Accel {aid[:8]}", f"accel-{aid[:8]}", kind, term_months, set_at, sub_id))
        if with_contract:
            cur.execute("SELECT id FROM public.contract_templates ORDER BY id LIMIT 1")
            tpl = cur.fetchone()
            cur.execute(
                "INSERT INTO public.signed_contracts (account_id, contract_template_id, "
                "  signer_email, tier, contract_snapshot, agreed_amount_cents, agreed_currency, "
                "  agreed_interval, stripe_subscription_id, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'accepted')",
                (aid, (tpl[0] if tpl else 1), "signer@example.invalid", "enterprise",
                 "SNAPSHOT", base_cents, currency, interval, sub_id))
    return aid, sub_id


def _ledger(conn, aid):
    with conn.cursor() as cur:
        cur.execute("SELECT action, amount, quantity, unit_price, reverses_ledger_id "
                    "  FROM public.account_ledger WHERE account_id = %s ORDER BY ledger_id", (aid,))
        return cur.fetchall()


def test_the_signed_base_fee_is_read_from_the_contract(db_env):
    aid, sub = _mk_account(db_env)
    fee = accel_mod.signed_base_fee(sub)
    assert fee["ok"] is True
    assert fee["cents"] == ENTERPRISE_BASE_CENTS
    assert fee["currency"] == "CAD"
    assert fee["contract_id"] is not None


@pytest.mark.parametrize("mutate,expect", [
    ("no_contract", "no_contract"),
    ("null_amount", "no_amount"),
    ("zero_amount", "non_positive_amount"),
    ("annual", "interval_mismatch"),
])
def test_an_unresolvable_base_fee_REFUSES_and_never_falls_back_to_the_card_rate(
        db_env, mutate, expect):
    """On a collections path a plausible-but-wrong number is worse than no number. The live rate
    card must never be substituted for the signed amount."""
    if mutate == "no_contract":
        aid, sub = _mk_account(db_env, with_contract=False)
    elif mutate == "annual":
        aid, sub = _mk_account(db_env, interval="year")
    else:
        aid, sub = _mk_account(db_env,
                               base_cents=(0 if mutate == "zero_amount" else None))
    fee = accel_mod.signed_base_fee(sub)
    assert fee["ok"] is False
    assert fee["reason"] == expect
    assert fee["cents"] in (None, 0)
    # and the live card rate is a real, different, tempting number that must NOT appear
    assert ent_mod.base_package_price_for("tenant") > 0


def test_the_open_acceleration_index_makes_a_double_submit_a_refusal_not_two_invoices(db_env):
    aid, sub = _mk_account(db_env)
    import psycopg2
    with db_env.cursor() as cur:
        cur.execute("INSERT INTO public.term_accelerations (account_id, subscription_id, "
                    "  base_fee_cents, months_remaining, amount_cents, status) "
                    "VALUES (%s,%s,%s,%s,%s,'invoiced')",
                    (aid, sub, ENTERPRISE_BASE_CENTS, 5, ENTERPRISE_BASE_CENTS * 5))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        with db_env.cursor() as cur:
            cur.execute("INSERT INTO public.term_accelerations (account_id, subscription_id, "
                        "  base_fee_cents, months_remaining, amount_cents, status) "
                        "VALUES (%s,%s,%s,%s,%s,'pending')",
                        (aid, sub, ENTERPRISE_BASE_CENTS, 5, ENTERPRISE_BASE_CENTS * 5))
    # ...but a RESOLVED history does not block a later, legitimate acceleration
    with db_env.cursor() as cur:
        cur.execute("UPDATE public.term_accelerations SET status='voided' WHERE account_id=%s",
                    (aid,))
        cur.execute("INSERT INTO public.term_accelerations (account_id, subscription_id, "
                    "  base_fee_cents, months_remaining, amount_cents, status) "
                    "VALUES (%s,%s,%s,%s,%s,'pending')",
                    (aid, sub, ENTERPRISE_BASE_CENTS, 3, ENTERPRISE_BASE_CENTS * 3))


def test_a_negative_acceleration_cannot_be_stored(db_env):
    """An acceleration of a negative number of months is an upstream arithmetic failure. The one
    thing worse than refusing to invoice is inventing a credit nobody is owed."""
    import psycopg2
    aid, sub = _mk_account(db_env)
    with pytest.raises(psycopg2.errors.CheckViolation):
        with db_env.cursor() as cur:
            cur.execute("INSERT INTO public.term_accelerations (account_id, subscription_id, "
                        "  base_fee_cents, months_remaining, amount_cents, status) "
                        "VALUES (%s,%s,%s,%s,%s,'pending')",
                        (aid, sub, ENTERPRISE_BASE_CENTS, -2, -ENTERPRISE_BASE_CENTS * 2))


# ── 🔴 THE FOUR ACCOUNTING STATES, ON THE REAL BILLING VIEW ─────────────────────────

def _write_state(conn, aid, state, *, months=5, base=ENTERPRISE_BASE_CENTS, term_end=None):
    """Drive the ledger to one of the four states through the REAL ``_write_ledger``."""
    from saas import db as saas_db
    amount = round(base * months / 100.0, 2)
    unit = round(base / 100.0, 2)
    term_end = term_end or (datetime.now(timezone.utc) + timedelta(days=150))
    with saas_db.connection() as c:
        with c.cursor() as cur:
            if state == "owed_uninvoiced":
                ent_mod._write_ledger(cur, aid, "commitment_broken", None, months, unit, 0,
                                      6, "stripe-dunning", "commitment broken",
                                      contract_end=term_end,
                                      billing_note=f"ARREARS (not invoiced): {amount}")
                c.commit()
                return None
            lid = ent_mod._write_ledger(cur, aid, "term_acceleration", None, months, unit, amount,
                                        6, "system", "accelerated", contract_end=term_end)
            if state == "settled":
                ent_mod._write_ledger(cur, aid, "term_acceleration_settled", None, months, unit,
                                      amount, None, "stripe", "settled")
            elif state == "voided":
                ent_mod._write_ledger(cur, aid, "term_acceleration_voided", None, -months, unit,
                                      -amount, None, "system", "resumed — voided",
                                      reverses_ledger_id=lid)
        c.commit()
    return amount


def test_STATE_1_owed_but_not_invoiced_moves_no_money_on_the_billing_view(db_env, monkeypatch):
    """An obligation nobody has billed is NOT a receivable. It must not appear in the figure the
    operator reads as "charged", and it must not create a balance."""
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env)
    _write_state(db_env, aid, "owed_uninvoiced")
    s = ent_mod.ledger_summary(aid)
    assert s["grand_total"] == 0.0, "an un-invoiced obligation was booked as CHARGED"
    assert s["balance"] == 0.0
    assert s["paid_total"] == 0.0
    # ...but it is VISIBLE and quantified, not lost
    assert s["accelerated_owed_uninvoiced"] == 7500.0
    assert s["accelerated_invoiced"] == 0.0
    assert s["accelerated_outstanding"] == 0.0
    actions = [r[0] for r in _ledger(db_env, aid)]
    assert actions == ["commitment_broken"]


def test_the_SAME_obligation_cannot_be_owed_uninvoiced_and_invoiced_and_settled_at_once(
        db_env, monkeypatch):
    """🔴 THE LIFECYCLE, WHICH THE FOUR STATE TESTS NEVER DRIVE.

    Each of those writes ONE state in isolation, so nothing exercised the sequence a real broken
    commitment follows: recorded → invoiced → settled, on ONE obligation. ``account_ledger`` is
    append-only and ``accelerated_owed_uninvoiced`` was reconstructed from every
    ``commitment_broken`` row unconditionally, so the summary reported the same CA$7,500 as
    owed-uninvoiced AND invoiced AND settled simultaneously. The module docstring promises the
    four states are "distinguishable IN THE DATA … never by inference"; three of them were
    mutually contradictory.

    Retirement is keyed on ``contract_end`` — the term end the obligation is FOR, carried by both
    the ``commitment_broken`` row and the ``term_acceleration`` invoice row."""
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env)
    term_end = datetime.now(timezone.utc) + timedelta(days=150)

    _write_state(db_env, aid, "owed_uninvoiced", term_end=term_end)
    s = ent_mod.ledger_summary(aid)
    assert s["accelerated_owed_uninvoiced"] == 7500.0, "recorded but not billed — correctly owed"
    assert s["accelerated_invoiced"] == 0.0

    # The operator now INVOICES that same obligation (same term end) and the customer pays it.
    _write_state(db_env, aid, "settled", term_end=term_end)
    s = ent_mod.ledger_summary(aid)
    assert s["accelerated_invoiced"] == 7500.0
    assert s["accelerated_settled"] == 7500.0
    assert s["accelerated_outstanding"] == 0.0
    assert s["accelerated_owed_uninvoiced"] == 0.0, (
        "the obligation has been invoiced AND paid — reporting it as still un-invoiced makes "
        "three of the four states true at once")


def test_a_second_unrelated_obligation_still_reads_as_owed(db_env, monkeypatch):
    """The retirement must key on the OBLIGATION, not merely on 'some invoice exists'. Two broken
    commitments at different term ends retire independently."""
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env)
    end_a = datetime.now(timezone.utc) + timedelta(days=150)
    end_b = datetime.now(timezone.utc) + timedelta(days=400)
    _write_state(db_env, aid, "owed_uninvoiced", term_end=end_a)
    _write_state(db_env, aid, "owed_uninvoiced", term_end=end_b)
    assert ent_mod.ledger_summary(aid)["accelerated_owed_uninvoiced"] == 15000.0

    _write_state(db_env, aid, "invoiced", term_end=end_a)
    s = ent_mod.ledger_summary(aid)
    assert s["accelerated_owed_uninvoiced"] == 7500.0, \
        "only the invoiced obligation retires — the other is still owed"
    assert s["accelerated_invoiced"] == 7500.0


def test_STATE_2_invoiced_and_unpaid_IS_a_receivable(db_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env)
    amount = _write_state(db_env, aid, "invoiced")
    s = ent_mod.ledger_summary(aid)
    assert s["grand_total"] == amount == 7500.0, "an ISSUED invoice must move the charged total"
    assert s["paid_total"] == 0.0
    assert s["balance"] == 7500.0
    assert s["accelerated_invoiced"] == 7500.0
    assert s["accelerated_outstanding"] == 7500.0
    # and the two states are distinguishable IN THE DATA, by action — not inferred
    assert s["accelerated_owed_uninvoiced"] == 0.0


def test_STATE_3_settled_zeroes_the_balance_without_re_inflating_charged(db_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env)
    amount = _write_state(db_env, aid, "settled")
    s = ent_mod.ledger_summary(aid)
    assert s["grand_total"] == 7500.0, "settlement must not be summed with the charge"
    assert s["paid_total"] == 7500.0
    assert s["balance"] == 0.0
    assert s["accelerated_settled"] == 7500.0
    assert s["accelerated_outstanding"] == 0.0


def test_STATE_4_voided_on_resume_is_neither_revenue_nor_a_write_off(db_env, monkeypatch):
    """The receivable must disappear CLEANLY: charged returns to where it was, nothing lands in
    paid (nobody paid), and the balance is zero."""
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env)
    _write_state(db_env, aid, "voided")
    s = ent_mod.ledger_summary(aid)
    assert s["grand_total"] == 0.0, "a voided invoice is NOT revenue — charged must fall back"
    assert s["paid_total"] == 0.0, "a void is not a payment — nobody paid"
    assert s["balance"] == 0.0
    assert s["accelerated_voided"] == 7500.0
    assert s["accelerated_outstanding"] == 0.0
    # the reversal is LINKED to the charge it reverses, not a free-floating negative
    rows = _ledger(db_env, aid)
    charge = [r for r in rows if r[0] == "term_acceleration"][0]
    void = [r for r in rows if r[0] == "term_acceleration_voided"][0]
    assert void[4] is not None, "the reversal must carry reverses_ledger_id"
    assert float(void[1]) == -float(charge[1])


def test_the_four_states_render_on_the_REAL_operator_billing_view(db_env, monkeypatch):
    """``billing_view`` is what ``saas/dashboard/app.js`` renders as
    "Ledger — charged X · paid Y · balance Z". Asserting the summary alone would not prove the
    figures survive the view that actually reaches an operator's screen."""
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    expected = {
        "owed_uninvoiced": (0.0, 0.0, 0.0),
        "invoiced":        (7500.0, 0.0, 7500.0),
        "settled":         (7500.0, 7500.0, 0.0),
        "voided":          (0.0, 0.0, 0.0),
    }
    for state, (charged, paid, balance) in expected.items():
        aid, sub = _mk_account(db_env)
        _write_state(db_env, aid, state)
        v = ent_mod.billing_view(aid)
        assert v is not None, f"billing_view returned None for {state}"
        assert v["history_total"] == charged, f"{state}: charged"
        assert v["paid_total"] == paid, f"{state}: paid"
        assert v["balance"] == balance, f"{state}: balance"


def test_the_grace_deadline_is_computable_from_the_stored_notice_date(db_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env)
    now = datetime.now(timezone.utc)
    with db_env.cursor() as cur:
        cur.execute("INSERT INTO public.term_accelerations (account_id, subscription_id, "
                    "  base_fee_cents, months_remaining, amount_cents, status, notice_sent_at, "
                    "  grace_until) VALUES (%s,%s,%s,%s,%s,'invoiced',%s,%s)",
                    (aid, sub, ENTERPRISE_BASE_CENTS, 5, ENTERPRISE_BASE_CENTS * 5,
                     now - timedelta(days=10), now + timedelta(days=20)))
    st = accel_mod.state(aid)
    assert st["found"] is True
    assert st["grace_active"] is True
    assert 19 <= st["grace_days_left"] <= 20
    # expired
    with db_env.cursor() as cur:
        cur.execute("UPDATE public.term_accelerations SET grace_until = %s WHERE account_id = %s",
                    (now - timedelta(days=1), aid))
    assert accel_mod.state(aid)["grace_active"] is False


def test_an_unknown_notice_date_leaves_the_grace_ACTIVE(db_env, monkeypatch):
    """If we cannot prove the customer was given their 30 days, they get them."""
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env)
    with db_env.cursor() as cur:
        cur.execute("INSERT INTO public.term_accelerations (account_id, subscription_id, "
                    "  base_fee_cents, months_remaining, amount_cents, status) "
                    "VALUES (%s,%s,%s,%s,%s,'invoiced')",
                    (aid, sub, ENTERPRISE_BASE_CENTS, 5, ENTERPRISE_BASE_CENTS * 5))
    st = accel_mod.state(aid)
    assert st["grace_active"] is True and st["grace_days_left"] is None


def test_an_out_of_term_account_is_not_accelerated(db_env, monkeypatch):
    """The commitment has been served. Cancelling now owes nothing, and inventing a charge would
    be the worst possible defect on this path."""
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env, term_months=6, months_into_term=9)
    r = accel_mod.accelerate(aid)
    assert r["ok"] is True and r["status"] == "term_served"
    assert _ledger(db_env, aid) == []


def test_no_term_of_record_refuses_rather_than_guessing(db_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env)
    with db_env.cursor() as cur:
        cur.execute("UPDATE public.accounts SET term_months_set_at = NULL WHERE account_id = %s",
                    (aid,))
    r = accel_mod.accelerate(aid)
    assert r["ok"] is False and r["status"] == "refused"
    assert _ledger(db_env, aid) == []


def test_every_reader_runs_against_the_REAL_migrated_tables(db_env, monkeypatch):
    """A green suite over dict fixtures cannot see a missing column. This calls every DB reader in
    the module against the real schema and asserts none raised — deliberately ONE test over ALL
    readers, so a partial repair still fails."""
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    aid, sub = _mk_account(db_env)
    assert accel_mod.signed_base_fee(sub)["ok"] is True
    st = accel_mod.state(aid)
    assert st["ok"] is True and st["found"] is False
    assert accel_mod.settle(aid)["status"] == "no_open_acceleration"
    assert accel_mod.resume(aid)["status"] == "no_open_acceleration"
    assert ent_mod.ledger_summary(aid) is not None
    from saas import db as saas_db
    with saas_db.connection() as c:
        with c.cursor() as cur:
            row = accel_mod._open_row(cur, aid)
        c.commit()
    assert row is None


# ════════════════════════════════════════════════════════════════════════════════════
# PART C — THE LIVE LANE. Real test-mode Stripe, end to end, both resolutions.
# ════════════════════════════════════════════════════════════════════════════════════

def _require_stripe():
    if not STRIPE_KEY:
        pytest.skip("ACCEL_STRIPE_KEY not set — the live lane cannot run")
    assert STRIPE_KEY.startswith("sk_test"), "REFUSING: this suite is TEST MODE ONLY"


@pytest.fixture()
def live(db_env, monkeypatch):
    """A real test-mode subscription: base + 2 seat items + the agent-chat add-on, paid, mid-term,
    with a signed contract bound to it and the account marked externally billed against it."""
    _require_stripe()
    import stripe
    stripe.api_key = STRIPE_KEY

    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    monkeypatch.setenv("SAAS_STRIPE_SECRET_KEY", STRIPE_KEY)
    monkeypatch.setenv("SAAS_PAYMENT_PROVIDER", "stripe")

    provider = ps_mod.StripePaymentProvider(config=_test_provider_config(STRIPE_KEY))
    assert provider.is_configured()
    monkeypatch.setattr(sync_mod, "_provider", lambda: provider)
    # plans.resolve_price_id reaches for payments.get_payment_provider (NOT the seat-sync seam)
    # to turn a lookup_key into a Price id. Without this the canonical keys silently fall back to
    # the hardcoded ids, which belong to a DIFFERENT Stripe account — the subscription would then
    # be built from prices this account does not own.
    from saas import payments as _payments_mod
    monkeypatch.setattr(_payments_mod, "get_payment_provider", lambda *a, **k: provider)
    monkeypatch.setattr(plans_mod._payments, "get_payment_provider", lambda *a, **k: provider)
    plans_mod._PRICE_ID_CACHE.clear()

    base = plans_mod.tenant_price_id("enterprise")
    seat_std = plans_mod.seat_item_price_id("tenant", "standard")
    seat_exp = plans_mod.seat_item_price_id("tenant", "expert")
    addon = plans_mod.agent_addon_price_id(5)
    assert base and seat_std and seat_exp and addon, "canonical Prices did not resolve"

    cust = stripe.Customer.create(name="CANCELACCEL suite", email="accel@example.invalid")
    pm = stripe.PaymentMethod.attach("pm_card_visa", customer=cust.id)
    stripe.Customer.modify(cust.id, invoice_settings={"default_payment_method": pm.id})
    sub = stripe.Subscription.create(
        customer=cust.id,
        items=[{"price": base, "quantity": 1}, {"price": seat_std, "quantity": 10},
               {"price": seat_exp, "quantity": 3}, {"price": addon, "quantity": 1}],
        payment_behavior="default_incomplete",
        payment_settings={"save_default_payment_method": "on_subscription"},
        expand=["latest_invoice"])
    stripe.Invoice.pay(sub.latest_invoice.id)
    sub = stripe.Subscription.retrieve(sub.id)
    assert sub.status == "active" and len(sub["items"].data) == 4

    aid, _ = _mk_account(db_env, sub_id=sub.id, term_months=6, months_into_term=1)
    _STRIPE_RAN["value"] = True
    yield {"account_id": aid, "subscription_id": sub.id, "customer_id": cust.id,
           "provider": provider, "stripe": stripe, "base_price": base}
    try:
        stripe.Subscription.cancel(sub.id)
    except Exception:
        pass


def _live_items(stripe, sub_id):
    return stripe.Subscription.retrieve(sub_id)["items"].data


def test_LIVE_accelerate_strips_every_elastic_item_and_bills_only_the_signed_base(live, db_env):
    """THE CORE CLAIM, end to end against Stripe's own billing engine."""
    s, sub_id, aid = live["stripe"], live["subscription_id"], live["account_id"]

    before = _live_items(s, sub_id)
    assert len(before) == 4

    r = accel_mod.accelerate(aid, trigger="cancellation", actor="suite")
    assert r["ok"] is True, f"acceleration failed: reason={r.get('reason')} strip={r.get('strip')} full={r}"
    assert r["status"] == "invoiced"

    # 1. EXACTLY the base item remains.
    after = _live_items(s, sub_id)
    assert len(after) == 1, f"expected 1 item after the strip, got {len(after)}"
    assert after[0].price.id == live["base_price"]
    assert r["items_stripped"] == 3

    # 2. Stripping cost the customer nothing — no credit note, no extra invoice.
    notes = [c for c in s.CreditNote.list(limit=100).data
             if getattr(c, "customer", None) == live["customer_id"]]
    assert notes == [], "stripping the seats minted a CREDIT NOTE"

    # 3. The final invoice = months x SIGNED base fee, and carries NO seat lines.
    months = r["months_remaining"]
    assert months == 5, f"expected 5 whole committed months, got {months}"
    assert r["amount"] == round(ENTERPRISE_BASE_CENTS * months / 100.0, 2) == 7500.0
    inv = s.Invoice.retrieve(r["invoice_id"])
    assert inv.status == "open"
    assert inv.total == ENTERPRISE_BASE_CENTS * months == 750000
    assert len(inv.lines.data) == 1, f"the final invoice has {len(inv.lines.data)} lines"
    line = inv.lines.data[0]
    for forbidden in ("seat", "Seat", "add-on ×", "Agent"):
        assert forbidden not in (line.description or "").replace("Seats and add-ons", ""), (
            f"a seat/add-on line reached the final invoice: {line.description!r}")
    assert "5 month(s)" in line.description
    print(f"\n[LIVE] final invoice {inv.number} total CA${inv.total / 100:,.2f} "
          f"({months} x CA${ENTERPRISE_BASE_CENTS / 100:,.2f}), {len(inv.lines.data)} line")

    # 4. The receivable is on the books, exactly once, and renders as CHARGED.
    summary = ent_mod.ledger_summary(aid)
    assert summary["grand_total"] == 7500.0
    assert summary["balance"] == 7500.0
    assert summary["accelerated_outstanding"] == 7500.0
    assert [row[0] for row in _ledger(db_env, aid)] == ["term_acceleration"]

    # 5. Re-running does not raise a SECOND final invoice.
    again = accel_mod.accelerate(aid)
    assert again["status"] == "already_invoiced"
    assert len([row for row in _ledger(db_env, aid) if row[0] == "term_acceleration"]) == 1


def test_LIVE_a_NEGOTIATED_signed_price_beats_the_live_card_rate(live, db_env):
    """The amount must come from the SIGNED contract, and this is the test that can PROVE it.

    🔴 WITHOUT A NEGOTIATED PRICE THIS CLAIM IS UNTESTABLE. Enterprise's card rate and the usual
    signed amount are BOTH CA$1,500, so a mutant that substitutes ``base_package_price_for`` for
    ``signed_base_fee`` produces an identical invoice and every other test in this file stays
    green — a mutation passing for the wrong reason. Here the contract is re-signed at a
    negotiated CA$1,200/month while the Stripe subscription still carries the CA$1,500 base
    Price, so the two sources give visibly different answers and only one of them is correct.

    It is also the real commercial case: migration 253 §3 says "the base fee recorded with your
    signature is the base fee for that whole term"."""
    s, aid = live["stripe"], live["account_id"]
    negotiated = 120000
    with db_env.cursor() as cur:
        # signed_contracts is append-only (migration 243), so a NEW signature supersedes rather
        # than an UPDATE — which is also how a renegotiation actually happens.
        cur.execute("SELECT contract_template_id FROM public.signed_contracts "
                    " WHERE account_id = %s LIMIT 1", (aid,))
        tpl = cur.fetchone()[0]
        cur.execute("SELECT external_billing_ref FROM public.accounts WHERE account_id = %s",
                    (aid,))
        sub_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO public.signed_contracts (account_id, contract_template_id, signer_email,"
            "  tier, contract_snapshot, agreed_amount_cents, agreed_currency, agreed_interval, "
            "  stripe_subscription_id, status) "
            "VALUES (%s,%s,'renegotiated@example.invalid','enterprise','SNAPSHOT',%s,'CAD',"
            "        'month',%s,'accepted')",
            (aid, tpl, negotiated, sub_id))

    assert accel_mod.signed_base_fee(sub_id)["cents"] == negotiated
    card_rate_cents = int(round(ent_mod.base_package_price_for("tenant") * 100))
    assert card_rate_cents != negotiated, "the fixture no longer separates the two sources"

    r = accel_mod.accelerate(aid, actor="suite")
    assert r["ok"] is True, r
    assert r["base_fee"] == 1200.0
    assert r["amount"] == round(negotiated * r["months_remaining"] / 100.0, 2) == 6000.0
    inv = s.Invoice.retrieve(r["invoice_id"])
    assert inv.total == negotiated * r["months_remaining"] == 600000
    assert inv.total != card_rate_cents * r["months_remaining"], (
        "the final invoice was computed from the LIVE CARD RATE, not the signed contract")
    assert "1,200.00" in inv.lines.data[0].description
    print(f"\n[LIVE] negotiated contract CA$1,200/mo (card rate CA$1,500/mo) -> final invoice "
          f"{inv.number} CA${inv.total / 100:,.2f} for {r['months_remaining']} months")


def test_LIVE_resolution_A_SETTLE_zeroes_the_balance(live, db_env):
    s, aid = live["stripe"], live["account_id"]
    r = accel_mod.accelerate(aid, actor="suite")
    assert r["ok"] is True

    s.Invoice.pay(r["invoice_id"])
    assert s.Invoice.retrieve(r["invoice_id"]).status == "paid"

    out = accel_mod.settle(aid)
    assert out["ok"] is True and out["status"] == "settled", out
    assert out["amount"] == 7500.0

    summary = ent_mod.ledger_summary(aid)
    assert summary["grand_total"] == 7500.0
    assert summary["paid_total"] == 7500.0
    assert summary["balance"] == 0.0, "a settled acceleration must leave a zero balance"
    assert summary["accelerated_outstanding"] == 0.0
    view = ent_mod.billing_view(aid)
    assert view["balance"] == 0.0
    print(f"\n[LIVE] SETTLE — charged CA${summary['grand_total']:,.2f} · "
          f"paid CA${summary['paid_total']:,.2f} · balance CA${summary['balance']:,.2f}")

    st = accel_mod.state(aid)
    assert st["acceleration"]["status"] == "settled"


def test_LIVE_resolution_B_RESUME_voids_the_invoice_and_restores_monthly_billing(live, db_env):
    s, sub_id, aid = live["stripe"], live["subscription_id"], live["account_id"]
    r = accel_mod.accelerate(aid, actor="suite")
    assert r["ok"] is True

    # the cancellation is scheduled, as it would be on a real cancel
    sched = sync_mod.request_cancellation(aid, actor="suite")
    assert sched["ok"] is True, sched
    assert s.Subscription.retrieve(sub_id).cancel_at is not None

    out = accel_mod.resume(aid, actor="suite")
    assert out["ok"] is True and out["status"] == "voided", out

    # 1. the invoice is VOIDED IN FULL — and status, never the amount, is the evidence
    inv = s.Invoice.retrieve(r["invoice_id"])
    assert inv.status == "void"
    assert inv.total == 750000, ("Stripe keeps the amount on a voided invoice — this assertion "
                                 "exists so nobody 'fixes' the settle check to read amount_due")

    # 2. the subscription is LIVE again with no scheduled cancellation
    sub = s.Subscription.retrieve(sub_id)
    assert sub.cancel_at is None, "the scheduled cancellation was not cleared"
    assert sub.status == "active"

    # 3. normal monthly billing continues, un-prorated and un-re-anchored
    preview = s.Invoice.create_preview(customer=live["customer_id"], subscription=sub_id)
    assert preview.total == ENTERPRISE_BASE_CENTS, (
        f"the next monthly invoice is {preview.total}, not the plain base {ENTERPRISE_BASE_CENTS}")
    assert [ln for ln in preview.lines.data if getattr(ln, "proration", False)] == [], (
        "clearing cancel_at generated prorations")

    # 4. the receivable disappeared CLEANLY — not revenue, not a write-off
    summary = ent_mod.ledger_summary(aid)
    assert summary["grand_total"] == 0.0, "a voided invoice is not revenue"
    assert summary["paid_total"] == 0.0, "a void is not a payment"
    assert summary["balance"] == 0.0
    assert summary["accelerated_voided"] == 7500.0
    assert summary["accelerated_outstanding"] == 0.0
    view = ent_mod.billing_view(aid)
    assert view["history_total"] == 0.0 and view["balance"] == 0.0
    print(f"\n[LIVE] RESUME — invoice {inv.number} status={inv.status}; next monthly preview "
          f"CA${preview.total / 100:,.2f}, 0 prorations; charged "
          f"CA${summary['grand_total']:,.2f} · balance CA${summary['balance']:,.2f}")


# ════════════════════════════════════════════════════════════════════════════════════
# FALSE-GREEN TRIPWIRES — last, so they see the whole run.
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_database_lane_actually_ran():
    assert _DB_RAN["value"], (
        "NO DB test executed. This suite's accounting assertions are its entire point and they "
        "all live behind the DSN. Point POSTGRES_DSN at a database whose NAME contains 'test' "
        "and re-run.")


def test_the_live_stripe_lane_actually_ran():
    assert _STRIPE_RAN["value"], (
        "NO live Stripe test executed. A fake cannot testify that stripping items mints no credit "
        "note, that the one-off invoice does not contaminate the subscription invoice, or that "
        "clearing cancel_at re-anchors nothing. Set ACCEL_STRIPE_KEY (sk_test_...) and re-run.")
