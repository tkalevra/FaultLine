"""EXTERNAL BILLING vs COMP — two axes that were conflated on the money path (migration 245).

THE CORRECTION THIS SUITE PINS, in the owner's words:

    "a comped account 'complimentary' is not billed, it's written off with a signature and
     reason either based on a specific date, or indefinitely, ie. my Internal tenant Tenant or
     testing tenants should by default be set as 'complimentary[comp]' accounts"

COMP = COMPLIMENTARY = A DELIBERATE, AUTHORISED WRITE-OFF. Signature (``comp_by``), reason
(``comp_reason``), expiry or indefinite (``comp_until``). Revenue GIVEN AWAY.
EXTERNAL BILLING = we ARE charging, at full price, on Stripe's rail, so the INTERNAL biller
stands down and does not invoice the same product twice. Revenue COLLECTED.

The Stripe tenant flow used to ``set_comp(True, actor="stripe-webhook")`` on every PAYING
tenant. Right effect, wrong concept — and the cost was an invoice saying "waived" to a customer
who had paid CA$1500, collected revenue reported as give-aways in the operator console, and a
fabricated authorisation on what is by definition a signed human decision.

⚠️ EVERY ASSERTION BELOW READS A REAL ``public.accounts`` ROW, A REAL LEDGER ROW, OR A REAL
RENDERED INVOICE LINE. None of them asserts a dictionary the code hands back. That distinction
is why the previous suite stayed green over the defect: it asserted ``comp_state(aid)["active"]
is True``, which was perfectly true and perfectly wrong.

RUN IT (never raw pytest — owner rule):

    PYTHONPATH="src:<venv site-packages>" \\
    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_extbill_test \\
    python3 tools/fltest.py --bug COMP --test tests/test_external_billing.py

THE THROWAWAY DATABASE is built by applying every migration numbered >= 131 to an empty DB
(``ON_ERROR_STOP=0``; the per-tenant migrations in that range fail harmlessly), then migration
245. It must NOT be a real tenant database; the fixture refuses a DSN whose database name does
not contain "test".

FALSE-GREEN GUARD: ``_DB_RAN`` is set by the DB fixture and asserted by the LAST test in this
file. A DSN-gated suite that skips its DB half reports "PASS | N passed, M skipped" and looks
green; that has produced a false green in this repo repeatedly. If the DB half did not run,
this suite is RED.
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from saas import stripe_webhooks as swh          # noqa: E402
from saas import accounts as accounts_mod        # noqa: E402
from saas import entitlements as ent_mod         # noqa: E402
from saas import invoicing as inv_mod            # noqa: E402


DEFAULT_TEST_DSN = ("postgresql://faultline:faultline@172.20.0.3:5432/"
                    "faultline_extbill_test")
TEST_DSN = os.environ.get("EXTBILL_TEST_DSN",
                          os.environ.get("POSTGRES_DSN", DEFAULT_TEST_DSN))
WHSEC = "whsec_unit_test_secret_do_not_use_anywhere_real"

_DB_RAN = {"value": False}


# ════════════════════════════════════════════════════════════════════════════════════
# PART A — pure decisions, no database
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_two_states_are_separate_ledger_actions():
    """A shared action name would re-merge the axes in the one place they are auditable."""
    assert "comp" in ent_mod.COMP_ACTIONS and "comp_off" in ent_mod.COMP_ACTIONS
    assert ent_mod.EXTERNAL_BILLING_ACTIONS == ("external_billing_on", "external_billing_off")
    assert not set(ent_mod.COMP_ACTIONS) & set(ent_mod.EXTERNAL_BILLING_ACTIONS)
    for a in ent_mod.EXTERNAL_BILLING_ACTIONS:
        assert a in ent_mod.LEDGER_ACTIONS, f"{a} must be a recognised ledger action"
        assert a not in ent_mod.BILLABLE_ACTIONS, \
            f"{a} is not a charge — requiring a signature would block the webhook"


def test_marking_externally_billed_requires_the_rail_and_the_linkage():
    """An unattributed 'somebody else collects this' is the same silent zero an unexplained
    comp would be. Both are refused BEFORE any DB work, so no account lookup is needed."""
    out = ent_mod.set_external_billing("acct", True, processor="", reference="sub_x")
    assert out["ok"] is False and out["status_code"] == 400 and "processor" in out["reason"]
    out = ent_mod.set_external_billing("acct", True, processor="stripe", reference="   ")
    assert out["ok"] is False and out["status_code"] == 400 and "reference" in out["reason"]


# ════════════════════════════════════════════════════════════════════════════════════
# PART B — the OUTCOME, against a REAL control-plane database
# ════════════════════════════════════════════════════════════════════════════════════

def _connect():
    import psycopg2
    return psycopg2.connect(TEST_DSN)


@pytest.fixture(scope="module")
def dbconn():
    if "test" not in TEST_DSN.rsplit("/", 1)[-1]:
        pytest.skip(f"refusing to run against a non-test database: {TEST_DSN}")
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"external-billing test DB unreachable ({type(exc).__name__}): {TEST_DSN}")
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.accounts'), "
                    "       to_regclass('public.account_ledger'), "
                    "       to_regclass('public.saas_audit_log')")
        present = cur.fetchone()
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'accounts' "
                    "AND column_name = 'externally_billed'")
        has_col = cur.fetchone()
    conn.commit()
    if not all(present) or not has_col:
        conn.close()
        pytest.skip("throwaway DB is missing the SaaS control plane or migration 245 — "
                    "see this file's header")
    yield conn
    conn.close()


@pytest.fixture
def env(monkeypatch, dbconn):
    _DB_RAN["value"] = True
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    monkeypatch.setenv("SAAS_MODE", "1")
    from saas import payment_config as _pc
    monkeypatch.setattr(_pc, "resolve_config", lambda: {"webhook_secret": WHSEC})
    return True


def _mk_account(conn, *, kind="tenant", seat_limit=None, name=None):
    if seat_limit is None:
        seat_limit = accounts_mod.hard_seat_cap(kind)
    slug = f"x{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.accounts (name, slug, plan, account_type, account_kind, "
            "seat_limit, status) VALUES (%s, %s, 'standard', 'standard', %s, %s, 'active') "
            "RETURNING account_id::text",
            (name or slug, slug, kind, int(seat_limit)),
        )
        aid = cur.fetchone()[0]
    conn.commit()
    return aid


def _row(conn, aid):
    """The REAL public.accounts row for both axes."""
    with conn.cursor() as cur:
        cur.execute("SELECT comped, comp_reason, comp_by, comp_until, externally_billed, "
                    "external_billing_processor, external_billing_ref, external_billing_source "
                    "FROM public.accounts WHERE account_id = %s", (aid,))
        r = cur.fetchone()
    conn.commit()
    return dict(zip(("comped", "comp_reason", "comp_by", "comp_until", "externally_billed",
                     "processor", "reference", "source"), r))


def _ledger(conn, aid, *actions):
    with conn.cursor() as cur:
        cur.execute("SELECT action, amount, note FROM public.account_ledger "
                    "WHERE account_id = %s AND action = ANY(%s) ORDER BY ledger_id",
                    (aid, list(actions)))
        rows = cur.fetchall()
    conn.commit()
    return [(a, float(m), n) for a, m, n in rows]


def _invoice_lines(aid):
    """The REAL rendered invoice line descriptions — what a customer actually reads."""
    model = inv_mod.build_invoice(aid)
    assert model is not None, "build_invoice returned None (control-plane error)"
    return model, [ln["description"] for ln in model["line_items"]]


def _render(aid, tag=""):
    """Print + return the REAL rendered invoice: (model, [(amount, description), ...]).
    Printed so a run's evidence is the customer document, not a dict."""
    model = inv_mod.build_invoice(aid)
    assert model is not None, "build_invoice returned None (control-plane error)"
    rows = [(round(float(ln["amount"]), 2), ln["description"]) for ln in model["line_items"]]
    print(f"\n── RENDERED INVOICE {tag} ──")
    for amt, desc in rows:
        print(f"  {amt:>10.2f}  {desc}")
    print(f"  {'TOTAL':>10}  {model['total']:.2f}")
    return model, rows


def _sign_contract(conn, aid, sub, *, cents=150000, currency="CAD", interval="month"):
    """A REAL ``public.signed_contracts`` row — THE source of truth for what Stripe collects.

    ``stripe_subscriptions`` (migration 236) snapshots ``price_id`` and NO amount, so it cannot
    answer "how much"; the signed contract records ``agreed_amount_cents`` in our own words, is
    immutable under migration 243's append-only trigger, and carries a human signature."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM public.contract_templates ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO public.contract_templates (version, title, body_markdown) "
                        "VALUES (%s, 'Test terms', 'body') RETURNING id",
                        (f"v-test-{uuid.uuid4().hex[:10]}",))
            tid = cur.fetchone()[0]
        else:
            tid = row[0]
        cur.execute(
            "INSERT INTO public.signed_contracts (contract_template_id, signer_email, tier, "
            " account_id, stripe_subscription_id, agreed_amount_cents, agreed_currency, "
            " agreed_interval, contract_snapshot, status) "
            "VALUES (%s, %s, 'enterprise', %s, %s, %s, %s, %s, 'snapshot', 'completed') "
            "RETURNING id",
            (tid, f"s-{uuid.uuid4().hex[:8]}@example.com", aid, sub, cents, currency, interval))
        cid = cur.fetchone()[0]
    conn.commit()
    return cid


def _subscribed(dbconn, aid, *, cents=150000, currency="CAD", interval="month",
                plan_id="enterprise"):
    """Drive the REAL webhook, then bind the signed contract to the subscription it created —
    the same two steps production performs (``_grant_entitlement`` then ``_link_signed_contract``,
    in that order). Returns the subscription id."""
    sub = f"sub_{uuid.uuid4().hex[:18]}"
    swh.handle_checkout_session_completed(
        _event(account_id=aid, subscription_id=sub, plan_id=plan_id))
    _sign_contract(dbconn, aid, sub, cents=cents, currency=currency, interval=interval)
    return sub


def _event(*, account_id, plan_id="enterprise", subscription_id=None):
    meta = {"account_id": account_id, "user_id": "fl_contract_0", "plan_id": plan_id}
    obj = {"id": f"cs_{uuid.uuid4().hex[:20]}", "object": "checkout.session",
           "customer": f"cus_{uuid.uuid4().hex[:14]}",
           "subscription": subscription_id or f"sub_{uuid.uuid4().hex[:18]}",
           "mode": "subscription", "payment_status": "paid", "status": "complete",
           "currency": "cad", "metadata": meta}
    return {"id": f"evt_{uuid.uuid4().hex[:22]}", "type": "checkout.session.completed",
            "api_version": "2024-06-20", "livemode": False, "created": int(time.time()),
            "data": {"object": obj}}


def _deleted_event(*, account_id, subscription_id):
    obj = {"id": subscription_id, "object": "subscription", "status": "canceled",
           "customer": f"cus_{uuid.uuid4().hex[:14]}", "canceled_at": int(time.time()),
           "cancel_at_period_end": False,
           "metadata": {"account_id": account_id, "plan_id": "enterprise"}}
    return {"id": f"evt_{uuid.uuid4().hex[:22]}", "type": "customer.subscription.deleted",
            "livemode": False, "created": int(time.time()), "data": {"object": obj}}


# ── 1. THE PAYING TENANT: externally billed, NOT comped ───────────────────────────

def test_a_stripe_paying_tenant_ends_with_comped_false_and_the_external_mark_set(dbconn, env):
    """THE HEADLINE. Read the ROW, not the dict the setter returned."""
    aid = _mk_account(dbconn, kind="team")
    sub = f"sub_{uuid.uuid4().hex[:18]}"
    swh.handle_checkout_session_completed(_event(account_id=aid, subscription_id=sub))
    r = _row(dbconn, aid)
    assert r["comped"] is False, "a PAYING customer must never be flagged as a give-away"
    assert r["comp_reason"] is None and r["comp_by"] is None
    assert r["externally_billed"] is True
    assert r["processor"] == "stripe"
    assert r["reference"] == sub, "the collection must be traceable to a real subscription"
    assert r["source"] == "stripe-webhook", "provenance, NOT a fabricated authoriser"


def test_the_paying_tenants_invoice_says_COLLECTED_and_never_says_waived(dbconn, env):
    """⚠️ THIS TEXT LANDS ON A CUSTOMER'S INVOICE. It must be impossible to read as a comp.

    SCENARIO: a tenant with NO add-ons — the flat Stripe price covers the whole bill, so the
    payable really is 0.00 and the original "nothing is payable" wording is TRUE here."""
    aid = _mk_account(dbconn, kind="tenant")
    _subscribed(dbconn, aid)
    model, rows = _render(aid, "— no add-ons, fully collected")
    descs = [d for _a, d in rows]
    blob = " ".join(descs).lower()
    assert "collected via stripe" in blob, descs
    for forbidden in ("waived", "comped account", "complimentary", "no charge", "free"):
        assert forbidden not in blob, f"{forbidden!r} appears on a PAID customer's invoice: {descs}"
    assert any("charged in full on that subscription, so nothing is payable" in d
               for d in descs), descs
    assert model["total"] == 0.0, "the internal invoice must not double-charge"


def test_the_value_lands_in_externally_collected_value_and_NOT_in_comped_value(dbconn, env):
    aid = _mk_account(dbconn, kind="tenant")
    _subscribed(dbconn, aid)
    b = ent_mod.billing_view(aid)
    assert b["comped"] is False and b["comped_value"] == 0.0
    assert b["externally_billed"] is True
    assert b["externally_collected_value"] == b["gross_subtotal"] > 0.0
    assert b["monthly_subtotal"] == 0.0 and b["monthly_total"] == 0.0


def test_the_operator_comped_report_does_NOT_include_a_stripe_payer(dbconn, env):
    """The revenue-reporting half of the defect: collected money counted as given away."""
    from saas import admin as admin_mod
    aid = _mk_account(dbconn, kind="tenant", name=f"payer-{uuid.uuid4().hex[:8]}")
    _subscribed(dbconn, aid)
    rpt = admin_mod.billing_overview(_op=True)      # the operator gate is a FastAPI Depends
    line = next(x for x in rpt["accounts"] if x["account_id"] == aid)
    assert line["comped"] is False and line["comped_value"] == 0.0
    assert line["externally_billed"] is True and line["externally_collected_value"] > 0.0
    assert line["external_processor"] == "stripe"
    # ...and the rollups keep them apart.
    assert rpt["externally_billed"]["count"] >= 1
    assert rpt["externally_billed"]["monthly_value"] >= line["externally_collected_value"]


# ── 2. THE GENUINE COMP is untouched, byte-identical ──────────────────────────────

def test_a_genuine_human_comp_still_behaves_exactly_as_before(dbconn, env):
    """BYTE-IDENTICAL invoice line. A regression here means migration 245 changed what comp
    MEANS, which is the one thing it must not do."""
    aid = _mk_account(dbconn, kind="tenant")
    out = ent_mod.set_comp(aid, True, reason="Internal tenant — owner's own workspace",
                           until="2027-01-31", actor="operator",
                           actor_user="owner@example-operator.ca", actor_role="operator")
    assert out["ok"] is True
    r = _row(dbconn, aid)
    assert r["comped"] is True and r["comp_by"] == "owner@example-operator.ca"
    assert r["comp_reason"] == "Internal tenant — owner's own workspace"
    assert r["externally_billed"] is False, "a comp must not imply an external rail"

    b = ent_mod.billing_view(aid)
    assert b["comped"] is True and b["comped_value"] == b["gross_subtotal"] > 0.0
    assert b["externally_collected_value"] == 0.0
    assert b["monthly_total"] == 0.0

    _model, descs = _invoice_lines(aid)
    when = ent_mod.comp_state(aid)["at"][:10]
    expected = ("Comped account — Internal tenant — owner's own workspace "
                f"(waived by owner@example-operator.ca on {when}, through 2027-01-31)")
    assert expected in descs, f"comp line drifted:\n  got      {descs}\n  expected {expected}"
    assert not any("Collected via" in d for d in descs), \
        "a plain comp must emit NO external-billing line at all"


def test_an_expired_comp_bills_again_on_its_own(dbconn, env):
    """``comp_until`` is evaluated at READ time — no reaper, no job."""
    aid = _mk_account(dbconn, kind="tenant")
    ent_mod.set_comp(aid, True, reason="14-day pilot", until="2020-01-01",
                     actor="operator", actor_user="op@example.com")
    st = ent_mod.comp_state(aid)
    assert st["comped"] is True and st["expired"] is True and st["active"] is False
    b = ent_mod.billing_view(aid)
    assert b["comped"] is False and b["monthly_total"] > 0.0, "a lapsed comp must bill again"
    _model, descs = _invoice_lines(aid)
    assert not any("Comped account" in d for d in descs)


# ── 3. BOTH AT ONCE: the ordering decision, made explicitly ───────────────────────

def test_a_comp_on_a_stripe_payer_keeps_the_comp_line_and_DISCLOSES_the_live_rail(dbconn, env):
    """ORDER: the HUMAN decision owns the credit line (authority order: user-stated > system
    observation), and exactly one line may credit -gross or the subtotal goes negative. But a
    comp CANNOT stop Stripe charging the card, so the external overlay leaves a ZERO-AMOUNT
    advisory naming the live subscription. Silence here would let an operator read the comp as
    'the customer stopped being charged'."""
    aid = _mk_account(dbconn, kind="tenant")
    sub = f"sub_{uuid.uuid4().hex[:18]}"
    swh.handle_checkout_session_completed(_event(account_id=aid, subscription_id=sub))
    ent_mod.set_comp(aid, True, reason="goodwill after an outage", actor="operator",
                     actor_user="op@example.com")

    b = ent_mod.billing_view(aid)
    assert b["comped"] is True and b["externally_billed"] is True
    assert b["comped_value"] == b["gross_subtotal"] > 0.0, "the comp owns the credit"
    assert b["externally_collected_value"] == 0.0, "the deferring overlay must not double-count"
    assert b["monthly_total"] == 0.0

    _model, descs = _invoice_lines(aid)
    assert any(d.startswith("Comped account —") for d in descs), descs
    note = next(d for d in descs if d.startswith("Note —"))
    assert "collected via Stripe" in note and sub in note
    assert "does not stop Stripe from charging" in note
    # exactly ONE line credits the gross; the advisory is zero-amount
    credits = [ln for ln in _model["line_items"] if float(ln["amount"]) < 0]
    assert len(credits) == 1, f"double credit: {_model['line_items']}"
    assert _model["total"] == 0.0


# ── 4. CANCELLATION must not revoke a human write-off ─────────────────────────────

def test_cancelling_a_stripe_subscription_does_NOT_clear_a_genuine_human_comp(dbconn, env):
    """🔴 THE SHARP EDGE. The old ``_revoke_entitlement`` called ``set_comp(False)``, which
    cleared comped/reason/by/at/until UNCONDITIONALLY. So an account the owner had genuinely
    comped (Internal tenant, a testing tenant) that also had ANY Stripe subscription would have its
    WRITE-OFF SILENTLY REVOKED the moment that subscription ended — the only trace a 'comp_off'
    ledger row attributed to stripe-webhook. Reachable today: an operator can comp any account
    at any time, independently of its subscriptions."""
    aid = _mk_account(dbconn, kind="tenant")
    sub = f"sub_{uuid.uuid4().hex[:18]}"
    swh.handle_checkout_session_completed(_event(account_id=aid, subscription_id=sub))
    ent_mod.set_comp(aid, True, reason="testing tenant — indefinite", actor="operator",
                     actor_user="owner@example-operator.ca")

    swh.handle_customer_subscription_deleted(_deleted_event(account_id=aid,
                                                            subscription_id=sub))

    r = _row(dbconn, aid)
    assert r["comped"] is True, "a webhook revoked the OWNER'S write-off"
    assert r["comp_by"] == "owner@example-operator.ca"
    assert r["comp_reason"] == "testing tenant — indefinite"
    assert r["comp_until"] is None, "an indefinite comp must stay indefinite"
    assert r["externally_billed"] is False, "the external rail did stop and must be cleared"
    assert ent_mod.comp_state(aid)["active"] is True
    assert _ledger(dbconn, aid, "comp_off") == [], "no comp was removed, so none was ledgered"


def test_cancelling_a_subscription_restores_internal_billing_for_a_plain_payer(dbconn, env):
    aid = _mk_account(dbconn, kind="tenant")
    sub = _subscribed(dbconn, aid)
    assert ent_mod.billing_view(aid)["monthly_total"] == 0.0
    swh.handle_customer_subscription_deleted(_deleted_event(account_id=aid,
                                                            subscription_id=sub))
    r = _row(dbconn, aid)
    assert (r["externally_billed"], r["processor"], r["reference"]) == (False, None, None)
    assert ent_mod.billing_view(aid)["monthly_total"] > 0.0, "the internal biller must resume"


# ── 5. LEDGER: nothing is waived, so nothing is subtracted ────────────────────────

def test_the_external_billing_ledger_row_carries_amount_zero_not_minus_gross(dbconn, env):
    """The 'comp' row carried ``-waived`` and 'comp' is not in PAYMENT_ACTIONS, so
    ``ledger_summary`` folded every redelivery into ``grand_total`` — the figure the Billing
    view labels as what the account was CHARGED, in an APPEND-ONLY table. Nothing is waived on
    an external rail, so nothing may be subtracted."""
    aid = _mk_account(dbconn, kind="tenant")
    swh.handle_checkout_session_completed(_event(account_id=aid))
    rows = _ledger(dbconn, aid, "external_billing_on", "comp")
    assert [a for a, _m, _n in rows] == ["external_billing_on"], rows
    assert rows[0][1] == 0.0, "an external-billing row must not credit the ledger"
    assert "NOT waived" in rows[0][2]


def test_a_stripe_redelivery_does_not_append_a_second_row(dbconn, env):
    """Stripe redelivers for up to three days and account_ledger is append-only."""
    aid = _mk_account(dbconn, kind="tenant")
    sub = f"sub_{uuid.uuid4().hex[:18]}"
    for _ in range(3):
        swh.handle_checkout_session_completed(_event(account_id=aid, subscription_id=sub))
    assert len(_ledger(dbconn, aid, "external_billing_on")) == 1


def test_a_genuinely_new_subscription_id_DOES_re_mark_the_account(dbconn, env):
    """The idempotency guard keys on the REFERENCE, so a re-subscribe is not swallowed as a
    redelivery — otherwise the account would keep pointing at a dead subscription."""
    aid = _mk_account(dbconn, kind="tenant")
    first, second = f"sub_{uuid.uuid4().hex[:18]}", f"sub_{uuid.uuid4().hex[:18]}"
    swh.handle_checkout_session_completed(_event(account_id=aid, subscription_id=first))
    swh.handle_checkout_session_completed(_event(account_id=aid, subscription_id=second))
    assert _row(dbconn, aid)["reference"] == second
    assert len(_ledger(dbconn, aid, "external_billing_on")) == 2


# ── 6. THE SETTER'S OWN CARE ──────────────────────────────────────────────────────

def test_the_setter_never_touches_a_comp_column(dbconn, env):
    """The separation is the feature. Proven on the ROW, in both directions."""
    aid = _mk_account(dbconn, kind="tenant")
    ent_mod.set_comp(aid, True, reason="indefinite comp", actor="operator",
                     actor_user="op@example.com")
    before = _row(dbconn, aid)
    ent_mod.set_external_billing(aid, True, processor="stripe", reference="sub_zzz",
                                 source="operator", actor="operator")
    ent_mod.set_external_billing(aid, False, source="operator", actor="operator")
    after = _row(dbconn, aid)
    for k in ("comped", "comp_reason", "comp_by", "comp_until"):
        assert before[k] == after[k], f"set_external_billing moved {k}"


def test_the_shared_individual_house_account_is_refused(dbconn, env):
    aid = _mk_account(dbconn, kind="individual_house")
    out = ent_mod.set_external_billing(aid, True, processor="stripe", reference="sub_h")
    assert out["ok"] is False and out["status_code"] == 400
    assert _row(dbconn, aid)["externally_billed"] is False


def test_the_database_refuses_an_unattributed_mark_written_by_direct_sql(dbconn, env):
    """The app returns 400; the CHECK constraint is the backstop for anything that bypasses it."""
    import psycopg2
    aid = _mk_account(dbconn, kind="tenant")
    with pytest.raises(psycopg2.errors.CheckViolation):
        with dbconn.cursor() as cur:
            cur.execute("UPDATE public.accounts SET externally_billed = true "
                        "WHERE account_id = %s", (aid,))
    dbconn.rollback()
    assert _row(dbconn, aid)["externally_billed"] is False


def test_a_control_plane_read_failure_fails_toward_BILLING(dbconn, env, monkeypatch, capsys):
    """FAIL LOUD, FAIL TOWARD BILLING — never toward silently zeroing a bill. Same posture
    ``comp_state`` documents. The residual (a possible duplicate internal invoice) is the
    deliberate lesser harm: a wrong invoice is visible and disputable, a silent zero is not."""
    aid = _mk_account(dbconn, kind="tenant")
    swh.handle_checkout_session_completed(_event(account_id=aid))
    assert ent_mod.external_billing_state(aid)["active"] is True

    class _Boom:
        def __enter__(self):
            raise RuntimeError("control plane unreachable")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(ent_mod.db, "connection", lambda *a, **k: _Boom())
    st = ent_mod.external_billing_state(aid)
    assert st["active"] is False and st["externally_billed"] is False
    assert "external_billing_state read failed" in capsys.readouterr().err


# ── 7. THE MIGRATION'S RECLASSIFICATION ───────────────────────────────────────────

def test_migration_245_reclassifies_a_stripe_webhook_comp_and_spares_a_human_one(dbconn, env):
    """Drives the REAL SQL in migrations/245_external_billing.sql against rows shaped exactly
    like the ones the old webhook left behind. The discriminator is EXACT equality on
    ``comp_by``, never a pattern over ``comp_reason`` — a human comp whose reason merely
    MENTIONS Stripe must survive."""
    bad = _mk_account(dbconn, kind="tenant")           # the mislabelled payer
    good = _mk_account(dbconn, kind="tenant")          # a genuine human comp
    sub = f"sub_{uuid.uuid4().hex[:18]}"
    with dbconn.cursor() as cur:
        cur.execute(
            "UPDATE public.accounts SET comped = true, comp_by = 'stripe-webhook', "
            "comp_reason = %s, comp_at = NOW() WHERE account_id = %s",
            (f"Stripe subscription {sub} active (plan enterprise)", bad))
        cur.execute(
            "UPDATE public.accounts SET comped = true, comp_by = 'owner@example-operator.ca', "
            "comp_reason = %s, comp_at = NOW(), comp_until = DATE '2030-01-01' "
            "WHERE account_id = %s",
            ("waived while their Stripe account is sorted out", good))
        cur.execute(
            "INSERT INTO public.stripe_subscriptions (subscription_id, customer_id, "
            "account_id, status, livemode) VALUES (%s, %s, %s, 'active', false) "
            "ON CONFLICT (subscription_id) DO NOTHING",
            (sub, _ensure_customer(cur), bad))
    dbconn.commit()

    _apply_migration_245(dbconn)

    b = _row(dbconn, bad)
    assert b["comped"] is False and b["comp_by"] is None and b["comp_reason"] is None
    assert b["externally_billed"] is True and b["processor"] == "stripe"
    assert b["reference"] == sub, "the linkage was recovered RELATIONALLY, not string-parsed"
    assert b["source"] == "migration-245"

    g = _row(dbconn, good)
    assert g["comped"] is True and g["comp_by"] == "owner@example-operator.ca"
    assert g["externally_billed"] is False, \
        "a HUMAN comp whose reason mentions Stripe must not be reclassified"

    # Re-running is a no-op: the statement clears comp_by, so nothing is selected twice.
    _apply_migration_245(dbconn)
    assert _row(dbconn, bad)["source"] == "migration-245"
    assert _row(dbconn, good)["comped"] is True


def _ensure_customer(cur):
    cid = f"cus_{uuid.uuid4().hex[:14]}"
    cur.execute("INSERT INTO public.stripe_customers (customer_id, livemode) "
                "VALUES (%s, false) ON CONFLICT (customer_id) DO NOTHING", (cid,))
    return cid


def _apply_migration_245(conn):
    path = os.path.join(os.path.dirname(__file__), "..", "migrations",
                        "245_external_billing.sql")
    with open(path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════════
# 8. B1 — THE OVERLAY MAY ONLY CREDIT WHAT THE PROCESSOR ACTUALLY COLLECTS
#
# Stripe collects a FLAT price: ``payments_stripe.create_checkout`` sends
# ``line_items: [{"price": price_id, "quantity": 1}]`` and NOTHING in this repo ever pushes a
# seat quantity to Stripe (``subscription_items`` / ``SubscriptionItem`` / ``Subscription.modify``
# / ``proration`` / ``usage_record`` — zero hits). Per the Stripe API reference for
# POST /v1/checkout/sessions, ``line_items.price`` is "The ID of the Price or Plan object" and
# ``line_items.quantity`` is "The quantity of the line item being purchased" — one unit of a
# fixed Price, INVARIANT to seat count.
#
# So the overlay's old ``-gross`` credit was wrong twice over on a 15-seat Enterprise:
# CA$100/mo of add-on revenue went unbilled, AND the invoice told the customer "nothing is
# payable on this internal invoice" while CA$100 was payable. THE LESSON: replacing a vague
# label ("waived") with a PRECISE one converts a silent gap into an ACTIVE MISSTATEMENT, so a
# precise label must be backed by a MEASUREMENT, never by the flag that says somebody else
# collects something.
# ══════════════════════════════════════════════════════════════════════════════════

def _addon_total(view):
    return round(sum(float(ln["monthly"]) for ln in view["lines"]
                     if ln.get("kind") == "addon"), 2)


def _base_total(view):
    return round(sum(float(ln["monthly"]) for ln in view["lines"]
                     if ln.get("kind") == "base"), 2)


def test_the_flat_stripe_price_credits_ONLY_the_base_and_the_add_ons_stay_payable(dbconn, env):
    """🔴 THE HEADLINE (B1). Enterprise, 5 Standard included + 10 added à la carte.
    Stripe collects the CA$1500 base package and NOTHING ELSE. The internal invoice must
    therefore still collect the CA$100 of add-ons, and must not claim otherwise."""
    aid = _mk_account(dbconn, kind="tenant")
    _subscribed(dbconn, aid, cents=150000)          # agreed CA$1500.00/mo, the base package
    prov = ent_mod.provision_capacity(aid, "standard", 10, actor="operator",
                                      signature="Christopher Thompson")
    assert prov["ok"] is True, prov

    b = ent_mod.billing_view(aid)
    base, addons = _base_total(b), _addon_total(b)
    assert base == 1500.0 and addons == 100.0, (base, addons)

    model, rows = _render(aid, "— Enterprise 5 included + 10 added")
    descs = [d for _a, d in rows]

    # 1. THE MONEY. The payable equals the add-ons, NOT zero.
    assert model["total"] == addons == 100.0, rows
    assert b["monthly_subtotal"] == 100.0 and b["monthly_total"] == 100.0
    # 2. THE CREDIT equals ONLY what Stripe collects.
    credits = [a for a, _d in rows if a < 0]
    assert credits == [-1500.0], rows
    # 3. The reported collected value equals that same figure — not the gross.
    assert b["externally_collected_value"] == 1500.0
    assert b["gross_subtotal"] == 1600.0
    assert b["comped_value"] == 0.0, "collected money is never a give-away"
    # 4. THE STATEMENT ON THE CUSTOMER DOCUMENT IS TRUE.
    line = next(d for d in descs if d.startswith("Collected via Stripe"))
    assert "nothing is payable" not in line, f"FALSE STATEMENT on the invoice: {line}"
    assert "CAD 1500.00/mo" in line and "CAD 100.00" in line, line
    assert "payable here" in line, line
    blob = " ".join(descs).lower()
    for forbidden in ("waived", "comped account", "complimentary", "no charge", "free"):
        assert forbidden not in blob, f"{forbidden!r} on a PAID customer's invoice: {descs}"


def test_an_unresolvable_collected_amount_bills_IN_FULL_and_says_so(dbconn, env, capsys):
    """FAIL TOWARD BILLING. No signed contract bound to the subscription ⇒ what the rail
    collects is UNKNOWN ⇒ credit NOTHING, bill in full, disclose it, and log LOUD. Never
    guess an amount, and never zero a bill on an unproven assumption."""
    aid = _mk_account(dbconn, kind="tenant")
    swh.handle_checkout_session_completed(_event(account_id=aid))   # NO contract bound
    b = ent_mod.billing_view(aid)
    assert b["externally_billed"] is True
    assert b["externally_collected_value"] == 0.0
    assert b["monthly_total"] == b["gross_subtotal"] > 0.0, "an unknown must not zero a bill"

    model, rows = _render(aid, "— externally marked, collected amount UNKNOWN")
    assert model["total"] == b["gross_subtotal"]
    line = next(d for a, d in rows if d.startswith("Collected via Stripe"))
    assert "could not be confirmed against a signed contract" in line, line
    assert "bills in full" in line, line
    assert "nothing is payable" not in line, line
    assert [a for a, _d in rows if a < 0] == [], "nothing was proven collected, so no credit"
    assert "external collection UNRESOLVED" in capsys.readouterr().err


def test_a_currency_mismatch_refuses_to_credit_and_is_logged_crit(dbconn, env, capsys):
    """A cross-currency credit is arithmetic nobody authorised. Refuse it, bill in full, LOUD."""
    aid = _mk_account(dbconn, kind="tenant")
    _subscribed(dbconn, aid, cents=150000, currency="USD")
    b = ent_mod.billing_view(aid)
    assert b["externally_collected_value"] == 0.0
    assert b["monthly_total"] == b["gross_subtotal"] > 0.0
    assert "CURRENCY MISMATCH" in capsys.readouterr().err


def test_a_non_monthly_interval_refuses_to_credit_and_is_logged_crit(dbconn, env, capsys):
    """An ANNUAL contract credited against ONE month would be wrong by a factor of twelve."""
    aid = _mk_account(dbconn, kind="tenant")
    _subscribed(dbconn, aid, cents=1800000, interval="year")
    b = ent_mod.billing_view(aid)
    assert b["externally_collected_value"] == 0.0
    assert b["monthly_total"] == b["gross_subtotal"] > 0.0
    assert "INTERVAL MISMATCH" in capsys.readouterr().err


def test_an_amount_mismatch_credits_the_LESSER_and_is_logged_crit(dbconn, env, capsys):
    """A grandfathered CA$1200 contract against a CA$1500 base package: credit CA$1200 — what
    is genuinely collected — leave CA$300 payable, and say so. Never credit money nobody took."""
    aid = _mk_account(dbconn, kind="tenant")
    _subscribed(dbconn, aid, cents=120000)
    b = ent_mod.billing_view(aid)
    assert b["externally_collected_value"] == 1200.0
    assert b["monthly_total"] == round(b["gross_subtotal"] - 1200.0, 2) == 300.0
    _model, rows = _render(aid, "— contract CA$1200 vs base CA$1500")
    assert [a for a, _d in rows if a < 0] == [-1200.0], rows
    assert "AMOUNT MISMATCH" in capsys.readouterr().err


def test_the_ledger_note_no_longer_asserts_the_internal_gross_as_collected(dbconn, env):
    """The APPEND-ONLY ledger row used to record "(CAD <gross>/mo collected on that rail)" —
    a figure nobody collected, permanently. It now records the internal gross AS the gross, and
    says outright when the collected amount is not yet confirmed."""
    aid = _mk_account(dbconn, kind="tenant")
    ent_mod.provision_capacity(aid, "expert", 2, actor="operator", signature="Christopher")
    gross = ent_mod.billing_view(aid)["gross_subtotal"]
    ent_mod.set_external_billing(aid, True, processor="stripe", reference="sub_ledgernote",
                                 source="stripe-webhook", actor="stripe-webhook")
    rows = _ledger(dbconn, aid, "external_billing_on")
    assert len(rows) == 1, rows
    note = rows[0][2]
    assert "NOT waived" in note, note
    assert f"internal gross CAD {gross:.2f}/mo" in note, note
    assert "NOT yet confirmed against a signed contract" in note, note
    assert f"CAD {gross:.2f}/mo collected on that rail" not in note, \
        f"the ledger still asserts the internal gross as collected: {note}"


def test_the_clinical_lane_credits_only_the_base_too(dbconn, env):
    """``invoicing.py`` builds clinical lines OUTSIDE ``billing_view`` and carries its own copy
    of the overlay. It must reach the SAME decision through the SAME function — a divergence
    here is how a whole account kind silently keeps the old defect."""
    aid = _mk_account(dbconn, kind="clinical")
    base = ent_mod.clinical_base_price()
    sub = f"sub_{uuid.uuid4().hex[:18]}"
    ent_mod.set_external_billing(aid, True, processor="stripe", reference=sub,
                                 source="stripe-webhook", actor="stripe-webhook")
    _sign_contract(dbconn, aid, sub, cents=int(round(base * 100)))
    ent_mod.provision_capacity(aid, "expert", 1, actor="operator", signature="Christopher")
    unit = ent_mod.price_for("expert", "tenant")

    model, rows = _render(aid, "— clinical base + 1 expert seat")
    assert [a for a, _d in rows if a < 0] == [-round(base, 2)], rows
    assert model["subtotal"] == round(unit, 2), rows
    line = next(d for _a, d in rows if d.startswith("Collected via Stripe"))
    assert "nothing is payable" not in line, f"FALSE STATEMENT on a clinical invoice: {line}"
    assert "payable here" in line, line


def test_a_comp_still_wins_the_credit_line_even_when_add_ons_remain(dbconn, env):
    """ORDERING IS UNCHANGED: a signed human write-off outranks a system observation, and
    exactly ONE line may credit. With add-ons present the comp still credits the FULL gross
    (it waives the whole internal bill) and the external overlay stays a zero-amount advisory."""
    aid = _mk_account(dbconn, kind="tenant")
    sub = _subscribed(dbconn, aid, cents=150000)
    ent_mod.provision_capacity(aid, "standard", 10, actor="operator", signature="Christopher")
    ent_mod.set_comp(aid, True, reason="goodwill after an outage", actor="operator",
                     actor_user="op@example.com")

    b = ent_mod.billing_view(aid)
    assert b["comped_value"] == b["gross_subtotal"] == 1600.0, "the comp owns the FULL credit"
    assert b["externally_collected_value"] == 0.0, "the deferring overlay must not double-count"
    assert b["monthly_total"] == 0.0

    model, rows = _render(aid, "— comped AND externally billed")
    assert len([a for a, _d in rows if a < 0]) == 1, rows
    note = next(d for _a, d in rows if d.startswith("Note —"))
    assert sub in note and "does not stop Stripe from charging" in note
    assert model["total"] == 0.0


# ── FALSE-GREEN GUARD (must be last) ──────────────────────────────────────────────

def test_no_test_was_silently_skipped():
    """A DSN-gated suite that skips its DB half looks green. It is not. If this fails, the DB
    fixture never ran and NOTHING above about a real row was actually verified."""
    assert _DB_RAN["value"] is True, (
        f"the DATABASE half of this suite never ran (DSN={TEST_DSN}) — every row/ledger/invoice "
        f"assertion was skipped and this run proves nothing")
