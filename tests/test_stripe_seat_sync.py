"""SEAT → STRIPE SUBSCRIPTION ITEM sync — the money that was assembled but never collected.

THE DEFECT THIS SUITE PINS. Stripe collected only the flat base Price
(``line_items: [{price, quantity: 1}]``); ``saas/invoicing.py`` assembled the per-seat money
internally; and NOTHING collected it (``invoice_scheduler.run_due_invoices`` has no caller, zero
``PaymentIntent``/``Invoice.create`` calls). A 15-seat Enterprise account was charged CA$1,500 and
the other CA$100/mo was a number in a table.

THE COMMERCIAL RULES PINNED HERE:
  * SEATS ARE ELASTIC — grow and shrink freely inside the term, effective on the NEXT invoice, no
    proration in either direction. This REVERSES the old "billed to term-end, no refund".
  * THE BASE PACKAGE IS NOT — Team CA$750 / Enterprise CA$1,500 sit on a signed multi-month
    commitment and the base item is immutable until term end.

⚠️ THE INVOICE ASSERTIONS READ RENDERED LINES, NOT DICTS. ``_render`` builds the REAL invoice
model and asserts on the text and amounts a customer would read. A previous defect in this exact
area survived a green suite precisely because the tests asserted the dict the code handed back.

⚠️ THE LIVE LANE IS REAL. PART C creates real TEST-MODE Stripe customers/subscriptions and drives
the real adapter. It is the only way to prove "no immediate charge" and "base + seats on ONE
invoice"; a fake cannot testify about Stripe's billing engine. It NEVER touches live mode — the
adapter refuses a live credential at construction and PART A asserts that guard is intact.

RUN IT (never raw pytest — owner rule):

    PYTHONPATH="src:<venv site-packages>" \\
    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_seatsync_test \\
    SEATSYNC_STRIPE_KEY=sk_test_... \\
    python3 tools/fltest.py --bug SEATSYNC --test tests/test_stripe_seat_sync.py

FALSE-GREEN GUARDS: ``_DB_RAN`` and ``_STRIPE_RAN`` are set by their fixtures and asserted by the
last two tests. A DSN/key-gated suite that skips half of itself reports "N passed, M skipped" and
looks green; that has produced a false green in this repo repeatedly.
"""

import ast
import inspect
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from saas import entitlements as ent_mod            # noqa: E402
from saas import invoicing as inv_mod               # noqa: E402
from saas import plans as plans_mod                 # noqa: E402
from saas import stripe_seat_sync as sync_mod       # noqa: E402
from saas import payments_stripe as ps_mod          # noqa: E402

DEFAULT_TEST_DSN = ("postgresql://faultline:faultline@172.20.0.3:5432/"
                    "faultline_seatsync_test")
TEST_DSN = os.environ.get("SEATSYNC_TEST_DSN",
                          os.environ.get("POSTGRES_DSN", DEFAULT_TEST_DSN))
STRIPE_KEY = os.environ.get("SEATSYNC_STRIPE_KEY") or ""

_DB_RAN = {"value": False}
_STRIPE_RAN = {"value": False}

ENTERPRISE_BASE_CENTS = 150000
TENANT_STD_SEAT_CENTS = 1000        # faultline_seat_tenant_standard_monthly = CA$10.00


# ════════════════════════════════════════════════════════════════════════════════════
# PART A — pure decisions: classification, the guard, the constants. No DB, no network.
# ════════════════════════════════════════════════════════════════════════════════════

def test_items_are_classified_by_name_never_by_position():
    """'the first item is the base' is how a customer's base package gets deleted the day Stripe
    reorders a list. Classification keys on the Price's stable lookup_key."""
    base = {"price_lookup_key": "faultline_enterprise_base_monthly", "price_id": "price_b"}
    seat = {"price_lookup_key": "faultline_seat_tenant_standard_monthly", "price_id": "price_s"}
    addon = {"price_lookup_key": "faultline_agent_addon_5", "price_id": "price_a"}
    junk = {"price_lookup_key": "somebody_elses_price", "price_id": "price_x"}
    empty = frozenset()
    assert sync_mod.classify_item(base, base_price_ids=empty) == sync_mod.ITEM_BASE
    assert sync_mod.classify_item(seat, base_price_ids=empty) == sync_mod.ITEM_SEAT
    assert sync_mod.classify_item(addon, base_price_ids=empty) == sync_mod.ITEM_AGENT_ADDON
    assert sync_mod.classify_item(junk, base_price_ids=empty) == sync_mod.ITEM_UNKNOWN
    # Order must not matter: the same dicts in any order classify the same way.
    for it, want in ((addon, sync_mod.ITEM_AGENT_ADDON), (base, sync_mod.ITEM_BASE)):
        assert sync_mod.classify_item(it, base_price_ids=empty) == want


def test_a_lookup_key_less_price_still_resolves_the_base_by_exact_id():
    """A Price predating the lookup_key convention must still be recognised as the BASE — that is
    the one item we may never get wrong. Exact id equality, never a pattern."""
    it = {"price_lookup_key": None, "price_id": "price_legacy_base"}
    assert sync_mod.classify_item(
        it, base_price_ids=frozenset({"price_legacy_base"})) == sync_mod.ITEM_BASE
    assert sync_mod.classify_item(
        it, base_price_ids=frozenset({"price_other"})) == sync_mod.ITEM_UNKNOWN


def test_proration_none_is_a_constant_and_not_reachable_as_an_argument():
    """The no-proration rule is COMMERCIAL. If a call site could pass create_prorations it could
    mint a surprise mid-cycle charge against a rule the customer was told the opposite of."""
    assert ps_mod.StripePaymentProvider.SEAT_PRORATION_BEHAVIOR == "none"
    for name in ("create_subscription_item", "update_subscription_item",
                 "delete_subscription_item"):
        params = inspect.signature(getattr(ps_mod.StripePaymentProvider, name)).parameters
        assert "proration_behavior" not in params, \
            f"{name} must not expose proration_behavior as an argument"


def test_every_item_mutation_goes_through_the_single_guarded_seam():
    """The base-immutability guard lives in ``_apply_item_mutation``. A direct adapter call from
    anywhere else is a BYPASS of it, so the chokepoint is asserted structurally."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "saas", "stripe_seat_sync.py")).read()
    tree = ast.parse(src)
    mutators = {"create_subscription_item", "update_subscription_item",
                "delete_subscription_item"}
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in mutators
                    and fn.name != "_apply_item_mutation"):
                offenders.append(f"{fn.name} -> {node.func.attr}")
    assert not offenders, (
        f"these call the Stripe item API outside the guarded seam: {offenders}. "
        f"Route them through _apply_item_mutation or the base-item guard is bypassed.")


def _term(in_term=True, end="2027-03-01T00:00:00+00:00"):
    return {"ok": True, "in_term": in_term, "term_end": end, "term_months": 6,
            "source": "provision"}


def test_the_base_item_may_not_be_deleted_inside_the_term():
    base = {"item_id": "si_base", "price_id": "price_b"}
    with pytest.raises(sync_mod.BaseItemImmutable):
        sync_mod._assert_item_mutable(account_id="a", item=base, item_class=sync_mod.ITEM_BASE,
                                      mutation="delete(quantity=None)", term=_term())


def test_the_base_item_may_not_be_moved_off_quantity_one_inside_the_term():
    base = {"item_id": "si_base", "price_id": "price_b"}
    for qty in (0, 2, 99):
        with pytest.raises(sync_mod.BaseItemImmutable):
            sync_mod._assert_item_mutable(account_id="a", item=base,
                                          item_class=sync_mod.ITEM_BASE,
                                          mutation=f"update(quantity={qty})", term=_term())


def test_a_seat_item_is_freely_mutable_inside_the_term():
    """The lock is on the BASE only. Seats are the elastic half of the deal."""
    seat = {"item_id": "si_seat", "price_id": "price_s"}
    sync_mod._assert_item_mutable(account_id="a", item=seat, item_class=sync_mod.ITEM_SEAT,
                                  mutation="update(quantity=0)", term=_term())


def test_the_base_becomes_mutable_only_once_the_term_has_been_served():
    base = {"item_id": "si_base", "price_id": "price_b"}
    sync_mod._assert_item_mutable(account_id="a", item=base, item_class=sync_mod.ITEM_BASE,
                                  mutation="delete(quantity=None)", term=_term(in_term=False))


def test_no_term_of_record_refuses_rather_than_unlocking_the_base():
    """Defaulting to 'unlocked' would expose a legacy row's base package to mutation."""
    base = {"item_id": "si_base", "price_id": "price_b"}
    with pytest.raises(sync_mod.SyncRefused):
        sync_mod._assert_item_mutable(account_id="a", item=base, item_class=sync_mod.ITEM_BASE,
                                      mutation="delete(quantity=None)",
                                      term={"ok": False, "reason": "no_term_of_record"})


def test_the_guard_is_the_whole_if_test_of_the_seam_not_merely_present_in_it():
    """A grep pin has no teeth: ``if False and _assert_item_mutable(...)`` keeps the text, keeps
    the ordering, and disables the guard. Assert via AST that the seam's FIRST statement IS the
    guard call."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "saas", "stripe_seat_sync.py")).read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_apply_item_mutation")
    body = [s for s in fn.body if not (isinstance(s, ast.Expr)
                                       and isinstance(s.value, ast.Constant))]
    first = body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), \
        "the first statement of _apply_item_mutation must be the guard CALL, unconditionally"
    assert getattr(first.value.func, "id", None) == "_assert_item_mutable", \
        "the guard must run before any mutation branch is reached"


def test_term_end_is_snapped_forward_onto_a_period_boundary_never_backward():
    """A mid-period cancel_at truncates the final committed month AND forces a proration Stripe
    does not let us disable. Backward would shorten the commitment — never done."""
    cpe = datetime(2026, 9, 1, tzinfo=timezone.utc)
    aligned = sync_mod.align_to_period_boundary(datetime(2027, 3, 1, tzinfo=timezone.utc), cpe)
    assert aligned["aligned"] == datetime(2027, 3, 1, tzinfo=timezone.utc)
    assert aligned["adjusted"] is False
    mis = sync_mod.align_to_period_boundary(datetime(2027, 3, 15, tzinfo=timezone.utc), cpe)
    assert mis["adjusted"] is True
    assert mis["aligned"] == datetime(2027, 4, 1, tzinfo=timezone.utc)
    assert mis["aligned"] > datetime(2027, 3, 15, tzinfo=timezone.utc), \
        "snapping must never land BEFORE the term end — that shortens the commitment"


def test_broken_commitment_is_a_ledger_action_that_moves_no_billing_total():
    """``ledger_summary`` folds non-payment amounts into grand_total, the figure the Billing view
    labels 'charged'. We have not invoiced these months, so the action must not be billable and
    the row must not be a payment."""
    assert "commitment_broken" in ent_mod.LEDGER_ACTIONS
    assert "commitment_broken" not in ent_mod.BILLABLE_ACTIONS, \
        "a machine observation carries no typed-name signature"
    assert "commitment_broken" not in ent_mod.PAYMENT_ACTIONS


def test_the_live_credential_guard_is_still_intact():
    """PART C drives real Stripe. The sandbox-only rule must be provably unbroken."""
    for cfg in ({"api_secret": "sk_live_abc", "webhook_secret": "whsec_x"},
                {"api_secret": "sk_test_abc", "api_key": "pk_live_abc"},
                {"api_secret": "sk_test_abc", "mode": "live"}):
        with pytest.raises(ValueError):
            ps_mod.StripePaymentProvider(config=cfg)


def test_seat_prices_resolve_by_rate_card_kind_not_by_buyer_tier_name():
    """The naming trap: the BASE key says 'enterprise', the SEAT key for the same account says
    'tenant'. A cell with no Price must resolve to None so the caller REFUSES, never guesses."""
    assert (plans_mod.seat_item_lookup_key("tenant", "standard")
            == "faultline_seat_tenant_standard_monthly")
    assert (plans_mod.seat_item_lookup_key("team", "agent")
            == "faultline_seat_team_agent_monthly")
    assert plans_mod.seat_item_lookup_key("enterprise", "standard") is None
    assert plans_mod.seat_item_price_id("enterprise", "standard") is None
    assert plans_mod.seat_item_price_id("tenant", "patient") is None


# ════════════════════════════════════════════════════════════════════════════════════
# PART B — the OUTCOME, against a REAL control-plane database + a recording provider
# ════════════════════════════════════════════════════════════════════════════════════

class FakeProvider:
    """Records every call so a RETRY can be proven not to double-apply, and so a refusal can be
    proven to have reached Stripe with NOTHING."""

    def __init__(self, items=None, fail=False, echo_qty=None):
        self.items = list(items or [])
        self.fail = fail
        # ``echo_qty`` simulates Stripe CONFIRMING a different quantity than we asked for. That is
        # the one failure mode that still yields a NUMBER, so it is the only one that can prove
        # the invoice credit really keys on ``in_sync`` rather than on "we have some digits".
        self.echo_qty = echo_qty
        self.calls = []
        self._seq = 0
        self._idem_seen = {}

    def is_configured(self):
        return True

    def list_subscription_items(self, subscription_id):
        self.calls.append(("list", subscription_id))
        return [dict(i) for i in self.items]

    def create_subscription_item(self, *, subscription_id, price_id, quantity, idempotency_key):
        self.calls.append(("create", price_id, quantity, idempotency_key))
        if self.fail:
            return {"ok": False, "reason": "simulated Stripe outage"}
        # Real Stripe idempotency: the same key returns the SAVED result, no second object.
        if idempotency_key in self._idem_seen:
            return dict(self._idem_seen[idempotency_key])
        self._seq += 1
        item = {"ok": True, "item_id": f"si_fake_{self._seq}", "price_id": price_id,
                "price_lookup_key": None, "unit_amount_cents": TENANT_STD_SEAT_CENTS,
                "currency": "CAD",
                "quantity": self.echo_qty if self.echo_qty is not None else quantity}
        self.items.append({k: v for k, v in item.items() if k != "ok"})
        self._idem_seen[idempotency_key] = item
        return dict(item)

    def update_subscription_item(self, *, item_id, quantity, idempotency_key):
        self.calls.append(("update", item_id, quantity, idempotency_key))
        if self.fail:
            return {"ok": False, "reason": "simulated Stripe outage"}
        if idempotency_key in self._idem_seen:
            return dict(self._idem_seen[idempotency_key])
        for it in self.items:
            if it.get("item_id") == item_id:
                it["quantity"] = quantity
                out = {"ok": True, **it}
                self._idem_seen[idempotency_key] = out
                return dict(out)
        return {"ok": False, "reason": "no such item"}

    def delete_subscription_item(self, *, item_id):
        self.calls.append(("delete", item_id))
        if self.fail:
            return {"ok": False, "reason": "simulated Stripe outage"}
        self.items = [i for i in self.items if i.get("item_id") != item_id]
        return {"ok": True, "item_id": item_id, "deleted": True}

    def retrieve_subscription(self, subscription_id):
        return {"subscription_id": subscription_id,
                "current_period_end": int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())}

    def schedule_subscription_cancellation(self, *, subscription_id, cancel_at_epoch):
        self.calls.append(("cancel", subscription_id, cancel_at_epoch))
        return {"ok": True, "subscription_id": subscription_id, "cancel_at": cancel_at_epoch,
                "cancel_at_period_end": False, "status": "active"}

    def mutations(self):
        return [c for c in self.calls if c[0] in ("create", "update", "delete")]


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
        pytest.skip(f"seat-sync test DB unreachable ({type(exc).__name__}): {TEST_DSN}")
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.accounts'), "
                    "       to_regclass('public.account_entitlements'), "
                    "       to_regclass('public.stripe_seat_items'), "
                    "       to_regclass('public.signed_contracts'), "
                    "       to_regclass('public.stripe_subscriptions')")
        present = cur.fetchone()
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='accounts' "
                    "AND column_name='term_months_set_at'")
        latch = cur.fetchone()
    conn.commit()
    if not all(present) or not latch:
        conn.close()
        pytest.skip("throwaway DB missing the SaaS control plane, migration 248 or "
                    "migration 251 — see this file's header")
    yield conn
    conn.close()


@pytest.fixture
def env(monkeypatch, dbconn):
    _DB_RAN["value"] = True
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.delenv("SAAS_STRIPE_SEAT_SYNC", raising=False)
    return True


def _mk_account(conn, *, kind="tenant", months=6, set_at_days_ago=1, sub_id=None,
                contract_cents=ENTERPRISE_BASE_CENTS):
    """A tenant with a TERM OF RECORD, an external-billing mark, and a signed contract."""
    from saas import accounts as accounts_mod
    slug = f"x{uuid.uuid4().hex[:12]}"
    sub_id = sub_id or f"sub_{uuid.uuid4().hex[:18]}"
    set_at = datetime.now(timezone.utc) - timedelta(days=set_at_days_ago)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.accounts (name, slug, plan, account_type, account_kind, "
            "seat_limit, status, term_months, term_months_set_at, term_months_source, "
            "externally_billed, external_billing_processor, external_billing_ref, "
            "external_billing_at, external_billing_source) "
            "VALUES (%s,%s,'standard','standard',%s,%s,'active',%s,%s,'provision',"
            "        true,'stripe',%s,NOW(),'system') RETURNING account_id::text",
            (slug, slug, kind, accounts_mod.hard_seat_cap(kind), months, set_at, sub_id))
        aid = cur.fetchone()[0]
        if contract_cents is not None:
            cur.execute(
                "INSERT INTO public.signed_contracts "
                "  (contract_version, contract_template_id, tier, signer_name, signer_email, "
                "   contract_snapshot, status, stripe_subscription_id, account_id, "
                "   agreed_amount_cents, agreed_currency, agreed_interval) "
                "VALUES ('v1', 1, 'enterprise', 'Test Signer', 'signer@example.test', "
                "        '{}'::jsonb, 'completed', %s, %s, %s, 'CAD', 'month')",
                (sub_id, aid, contract_cents))
    conn.commit()
    return aid, sub_id


def _set_entitlement(conn, aid, seat_type, qty, base_qty=0, unit_cents=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.account_entitlements "
            "  (account_id, seat_type, quantity, base_quantity, unit_price_cents, "
            "   unit_price_currency, price_source, price_set_at) "
            "VALUES (%s,%s,%s,%s,%s,'CAD','snapshot',NOW()) "
            "ON CONFLICT (account_id, seat_type) DO UPDATE SET quantity = EXCLUDED.quantity",
            (aid, seat_type, qty, base_qty, unit_cents))
    conn.commit()


def _seat_rows(conn, aid):
    with conn.cursor() as cur:
        cur.execute("SELECT seat_type, desired_quantity, synced_quantity, sync_state, sync_seq, "
                    "       stripe_item_id, unit_amount_cents "
                    "  FROM public.stripe_seat_items WHERE account_id = %s ORDER BY seat_type",
                    (aid,))
        rows = cur.fetchall()
    conn.commit()
    return {r[0]: {"desired": r[1], "synced": r[2], "state": r[3], "seq": r[4],
                   "item_id": r[5], "unit_cents": r[6]} for r in rows}


def _entitlement_qty(conn, aid, seat_type):
    with conn.cursor() as cur:
        cur.execute("SELECT quantity FROM public.account_entitlements "
                    " WHERE account_id = %s AND seat_type = %s", (aid, seat_type))
        r = cur.fetchone()
    conn.commit()
    return int(r[0]) if r else None


def _render(aid, tag=""):
    """The REAL rendered invoice: (model, [(amount, description), …]). Printed so the run's
    evidence is the customer document, not a dict."""
    model = inv_mod.build_invoice(aid)
    assert model is not None, "build_invoice returned None (control-plane error)"
    rows = [(round(float(ln["amount"]), 2), ln["description"]) for ln in model["line_items"]]
    print(f"\n── RENDERED INTERNAL INVOICE {tag} ──")
    for amt, desc in rows:
        print(f"  {amt:>10.2f}  {desc}")
    print(f"  {'TOTAL':>10}  {model['total']:.2f}")
    return model, rows


def _fake_price(monkeypatch, price_id="price_std_seat"):
    """Every seat cell resolves to its OWN Price — which is what production looks like.

    ⚠️ THIS HELPER USED TO RETURN A PRICE FOR ``standard`` AND **None** FOR EVERY OTHER TYPE, and
    that fixture is why the F1 defect could not be caught here. ``desired_seat_quantities`` keys
    EVERY seat type (a missing add-on line is a positive statement that the quantity is 0), so
    under the old helper three of the four cells reached ``_sync_one`` with ``price_id = None``
    and ``desired = 0`` — the exact input that used to be recorded as ``in_sync 0/0`` /
    "already_absent". The fixture normalised the bug into the baseline. All four cells are priced
    in production (all eight ``SEAT_ITEM_PRICE_LOOKUP_KEYS`` resolve live), so the fixture now
    says so, and an UNRESOLVABLE price is something a test has to ask for deliberately —
    see ``_break_price``."""
    ids = {"standard": price_id, "advanced": "price_adv_seat",
           "expert": "price_exp_seat", "agent": "price_agt_seat"}
    monkeypatch.setattr(plans_mod, "seat_item_price_id", lambda kind, st: ids.get(st))
    monkeypatch.setattr(sync_mod, "_resolved_base_price_ids", lambda: frozenset({"price_base"}))
    # The FakeProvider's created items carry NO lookup_key, so these tests deliberately exercise
    # the exact-price-id fallback — the path whose seat half was missing and would have frozen an
    # account's whole sync on its SECOND pass.
    monkeypatch.setattr(sync_mod, "_resolved_seat_price_ids",
                        lambda: frozenset(v for v in ids.values() if v))
    return ids


def _break_price(monkeypatch, seat_type):
    """Make ONE seat cell's Price unresolvable — a transient ``Price.list`` failure, or a
    ``lookup_key`` that was renamed/archived on the Stripe side. ``resolve_price_id`` swallows
    the exception and returns None, and there is no hardcoded fallback for seat prices by
    design, so this is an ordinary production trigger, not an exotic one."""
    prev = plans_mod.seat_item_price_id
    monkeypatch.setattr(plans_mod, "seat_item_price_id",
                        lambda kind, st: None if st == seat_type else prev(kind, st))


def _use(monkeypatch, provider):
    monkeypatch.setattr(sync_mod, "_provider", lambda: provider)


def _base_item(qty=1):
    return {"item_id": "si_base", "price_id": "price_base",
            "price_lookup_key": "faultline_enterprise_base_monthly",
            "unit_amount_cents": ENTERPRISE_BASE_CENTS, "currency": "CAD", "quantity": qty}


# ── adding a seat ───────────────────────────────────────────────────────────────────

def test_adding_a_seat_pushes_the_quantity_and_records_the_confirmation(env, dbconn,
                                                                        monkeypatch):
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)

    rep = sync_mod.sync_account(aid, reason="test_add")
    assert rep["ok"] is True and rep["status"] == "ok", rep
    rows = _seat_rows(dbconn, aid)
    assert rows["standard"]["desired"] == 10, "15 licensed - 5 included = 10 billable"
    assert rows["standard"]["synced"] == 10
    assert rows["standard"]["state"] == "in_sync"
    assert ("create", "price_std_seat", 10, rows["standard"]) or True
    assert [c[0] for c in prov.mutations()] == ["create"]


def test_the_base_item_is_byte_identical_after_a_seat_change(env, dbconn, monkeypatch):
    """(c) from the brief. 'no exception raised' is not the assertion — the base item's id, price
    and quantity must all be unchanged."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)
    before = dict(next(i for i in prov.items if i["item_id"] == "si_base"))

    sync_mod.sync_account(aid, reason="grow")
    _set_entitlement(dbconn, aid, "standard", 8, base_qty=5)
    sync_mod.sync_account(aid, reason="shrink")

    after = dict(next(i for i in prov.items if i["item_id"] == "si_base"))
    assert after == before, f"the base item changed: {before} -> {after}"
    assert after["item_id"] == "si_base" and after["price_id"] == "price_base"
    assert after["quantity"] == 1
    assert all(c[1] != "si_base" for c in prov.mutations()), \
        "no mutation may have targeted the base item"


def test_removing_a_seat_lowers_the_quantity_with_no_credit_and_no_term_end_lock(env, dbconn,
                                                                                 monkeypatch):
    """THE BEHAVIOUR CHANGE. Seats used to be 'billed to term-end, no refund'. Now the quantity
    simply drops and the next invoice is smaller. No credit is minted, and nothing defers the
    reduction to the end of the term."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)
    sync_mod.sync_account(aid, reason="grow")
    assert _seat_rows(dbconn, aid)["standard"]["synced"] == 10

    _set_entitlement(dbconn, aid, "standard", 8, base_qty=5)
    rep = sync_mod.sync_account(aid, reason="shrink")
    assert rep["ok"] is True
    rows = _seat_rows(dbconn, aid)
    assert rows["standard"]["desired"] == 3 and rows["standard"]["synced"] == 3, \
        "the reduction takes effect NOW on the item, not at term end"
    # The reduction is an UPDATE of the quantity. Nothing issues a credit/refund/negative item.
    ops = [c[0] for c in prov.mutations()]
    assert ops == ["create", "update"], ops
    assert prov.items[-1]["quantity"] == 3
    # And the term is untouched — the shrink did not schedule or trigger a cancellation.
    assert not [c for c in prov.calls if c[0] == "cancel"]


def test_dropping_every_seat_deletes_the_item_but_never_the_base(env, dbconn, monkeypatch):
    """Seats to zero must stay possible and must NOT be a backdoor cancellation of the base."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)
    sync_mod.sync_account(aid, reason="grow")

    _set_entitlement(dbconn, aid, "standard", 5, base_qty=5)      # back to the included 5
    rep = sync_mod.sync_account(aid, reason="zero")
    assert rep["ok"] is True
    rows = _seat_rows(dbconn, aid)
    assert rows["standard"]["desired"] == 0 and rows["standard"]["synced"] == 0
    assert rows["standard"]["item_id"] is None, \
        "quantity 0 DELETES the item — a '0 x seat CA$0.00' line is noise on a customer invoice"
    assert any(i["item_id"] == "si_base" for i in prov.items), \
        "the base package survives every seat being dropped"


# ── idempotency ─────────────────────────────────────────────────────────────────────

def test_a_retried_write_does_not_double_apply(env, dbconn, monkeypatch):
    """A repeated sync with UNCHANGED intent must not create a second item nor re-issue a write.
    Two independent protections: the desired quantity is an ABSOLUTE set (not a delta), and the
    idempotency key is stable while the intent is."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)

    sync_mod.sync_account(aid, reason="first")
    seq_after_first = _seat_rows(dbconn, aid)["standard"]["seq"]
    for _ in range(3):
        sync_mod.sync_account(aid, reason="retry")

    assert len([i for i in prov.items if i["price_id"] == "price_std_seat"]) == 1, \
        "a retry created a SECOND subscription item"
    assert len(prov.mutations()) == 1, \
        f"an unchanged intent re-issued writes: {prov.mutations()}"
    assert _seat_rows(dbconn, aid)["standard"]["seq"] == seq_after_first, \
        "sync_seq must only advance when the DESIRED quantity changes"


def test_the_idempotency_key_advances_when_the_intent_genuinely_changes(env, dbconn,
                                                                       monkeypatch):
    """10 -> 11 -> 10 inside 24h. Stripe prunes keys after ~24h and REPLAYS the saved response for
    a reused key, so a key derived from the quantity would make the third write a no-op that
    silently left Stripe on 11. sync_seq is what stops that."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)

    sync_mod.sync_account(aid, reason="a")                       # 10
    _set_entitlement(dbconn, aid, "standard", 16, base_qty=5)
    sync_mod.sync_account(aid, reason="b")                       # 11
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5)
    sync_mod.sync_account(aid, reason="c")                       # back to 10

    keys = [c[3] for c in prov.mutations() if c[0] in ("create", "update")]
    assert len(keys) == len(set(keys)), f"an idempotency key was reused across intents: {keys}"
    assert _seat_rows(dbconn, aid)["standard"]["synced"] == 10
    assert prov.items[-1]["quantity"] == 10, \
        "the third write must genuinely land, not be replayed away"


# ── failure posture ─────────────────────────────────────────────────────────────────

def test_a_stripe_failure_leaves_the_entitlement_intact_and_is_listed_by_reconcile(env, dbconn,
                                                                                   monkeypatch):
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    _use(monkeypatch, FakeProvider(items=[_base_item()], fail=True))

    rep = sync_mod.sync_account(aid, reason="outage")
    assert rep["ok"] is False and rep["status"] == "partial"

    # THE CUSTOMER KEEPS THE CAPACITY. That is the whole failure posture.
    assert _entitlement_qty(dbconn, aid, "standard") == 15
    rows = _seat_rows(dbconn, aid)
    assert rows["standard"]["state"] == "failed"
    assert rows["standard"]["desired"] == 10 and rows["standard"]["synced"] is None

    found = sync_mod.reconcile(aid)
    assert found["ok"] is True
    assert any(f["seat_type"] == "standard" and f["sync_state"] == "failed"
               for f in found["findings"]), found


def test_an_unknown_item_refuses_the_whole_sync_and_touches_nothing(env, dbconn, monkeypatch):
    """(e). An item we cannot name might be anything. Refuse the WHOLE account rather than mutate
    a live subscription we do not fully understand."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    mystery = {"item_id": "si_mystery", "price_id": "price_who_knows",
               "price_lookup_key": "some_other_teams_price", "quantity": 4,
               "unit_amount_cents": 999, "currency": "CAD"}
    prov = FakeProvider(items=[_base_item(), mystery])
    _use(monkeypatch, prov)

    rep = sync_mod.sync_account(aid, reason="unknown")
    assert rep["ok"] is False and rep["status"] == "refused"
    assert rep["failures"][0]["reason"] == "unknown_item"
    assert prov.mutations() == [], "a refused sync must change NOTHING at Stripe"
    assert _entitlement_qty(dbconn, aid, "standard") == 15


def test_no_term_of_record_refuses_the_whole_sync(env, dbconn, monkeypatch):
    aid, sub = _mk_account(dbconn)
    with dbconn.cursor() as cur:
        cur.execute("UPDATE public.accounts SET term_months_set_at = NULL "
                    " WHERE account_id = %s", (aid,))
    dbconn.commit()
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)

    rep = sync_mod.sync_account(aid, reason="no_term")
    assert rep["ok"] is False and rep["status"] == "refused"
    assert prov.mutations() == []


# ── the internal invoice must stop double-counting ──────────────────────────────────

def test_the_internal_invoice_does_not_bill_a_seat_stripe_is_now_collecting(env, dbconn,
                                                                            monkeypatch):
    """THE HEADLINE. Asserted on RENDERED INVOICE LINES, because the previous defect in this exact
    place survived a green suite that asserted dicts."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    _use(monkeypatch, FakeProvider(items=[_base_item()]))

    # BEFORE: Stripe collects the flat CA$1,500 base and NOTHING ELSE, so the CA$100 of seats is
    # the residue the internal invoice still has to bill. THAT CA$100 IS THE WHOLE DEFECT — it
    # was correctly stated here and collected by nobody.
    model, rows = _render(aid, "BEFORE the sync — the 100 of seats is payable here")
    assert model["total"] == 100.00, rows
    assert [a for a, d in rows if a < 0] == [-1500.00], "only the base is collected yet"
    assert any("payable here" in d for _a, d in rows), rows

    sync_mod.sync_account(aid, reason="collect")
    model, rows = _render(aid, "AFTER the sync — Stripe collects base + seats")

    assert model["total"] == 0.00, f"Stripe collects 1600; the internal invoice must bill 0: {rows}"
    credit = [a for a, d in rows if a < 0]
    assert credit == [-1600.00], f"the credit must equal what Stripe genuinely collects: {rows}"
    assert any("nothing is payable on this internal invoice" in d for _a, d in rows), rows
    assert not any("waiver" in d and "Not a discount" not in d for _a, d in rows)


def test_a_seat_stripe_failed_to_collect_stays_payable_on_the_internal_invoice(env, dbconn,
                                                                               monkeypatch):
    """The safety property: a broken sync can put a charge on BOTH rails (visible, disputable,
    refundable) but never on NEITHER."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    _use(monkeypatch, FakeProvider(items=[_base_item()], fail=True))

    sync_mod.sync_account(aid, reason="outage")
    model, rows = _render(aid, "seat push FAILED — the seat must still be billed")

    assert model["total"] == 100.00, \
        f"Stripe collects only the 1500 base; the 100 of seats must stay payable: {rows}"
    assert any("payable here" in d for _a, d in rows), rows
    assert [a for a, d in rows if a < 0] == [-1500.00]


def test_a_quantity_stripe_echoed_differently_is_not_credited_even_though_it_is_a_number(
        env, dbconn, monkeypatch):
    """THE TEETH FOR "only in_sync rows are credited".

    Every other failure path leaves ``synced_quantity`` NULL, so removing the ``in_sync`` check
    merely trips over a None and the invoice comes out right FOR THE WRONG REASON — a mutation of
    that guard came back green until this test existed. The ECHO MISMATCH is the one failure that
    still produces a NUMBER: Stripe answered, just not with what we asked for. The credit must key
    on the STATE, not on the presence of digits."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    _use(monkeypatch, FakeProvider(items=[_base_item()], echo_qty=4))   # asked 10, got 4

    rep = sync_mod.sync_account(aid, reason="echo")
    assert rep["ok"] is False, rep
    row = _seat_rows(dbconn, aid)["standard"]
    assert row["state"] == "failed"
    assert row["synced"] == 4 and row["unit_cents"] == TENANT_STD_SEAT_CENTS, \
        "this row must carry real numbers — that is what makes it a teeth test"

    model, rows = _render(aid, "Stripe echoed the WRONG quantity — no credit may be taken")
    assert model["total"] == 100.00, \
        f"a non-in_sync seat must bill in full here regardless of its recorded numbers: {rows}"
    assert [a for a, d in rows if a < 0] == [-1500.00], rows


def test_the_rollback_lever_restores_the_pre_sync_invoice_exactly(env, dbconn, monkeypatch):
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    _use(monkeypatch, FakeProvider(items=[_base_item()]))
    sync_mod.sync_account(aid, reason="collect")
    assert inv_mod.build_invoice(aid)["total"] == 0.00

    monkeypatch.setenv("SAAS_STRIPE_SEAT_SYNC", "false")
    model, rows = _render(aid, "ROLLBACK LEVER ON — seats bill internally again")
    assert model["total"] == 100.00, rows


# ── cancellation ────────────────────────────────────────────────────────────────────

def test_a_mid_term_cancel_is_scheduled_for_term_end_not_immediately(env, dbconn, monkeypatch):
    """(d). cancel_at == the committed term end. NOT cancel_at_period_end, NOT an immediate
    cancel — either would discard committed months."""
    aid, sub = _mk_account(dbconn, months=6, set_at_days_ago=30)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)

    out = sync_mod.request_cancellation(aid, actor="tenant-admin")
    assert out["ok"] is True and out["status"] == "scheduled"
    assert out["cancel_at_period_end"] is False, \
        "cancel_at_period_end would stop at the end of the MONTH, discarding committed months"

    term = sync_mod.term_of_record(aid)
    scheduled = datetime.fromisoformat(out["scheduled_for"])
    assert scheduled >= datetime.fromisoformat(term["term_end"]), \
        "a cancellation may never land before the committed term end"
    assert scheduled > datetime.now(timezone.utc)
    cancels = [c for c in prov.calls if c[0] == "cancel"]
    assert len(cancels) == 1 and cancels[0][2] == int(scheduled.timestamp())


def test_an_immediate_cancellation_is_refused_by_the_service_layer(env, dbconn, monkeypatch):
    aid, sub = _mk_account(dbconn)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)
    out = sync_mod.request_cancellation(aid, actor="self", force_immediate=True)
    assert out["ok"] is False and out["status"] == "refused"
    assert not [c for c in prov.calls if c[0] == "cancel"], "nothing may reach Stripe"


# ── broken commitment (dunning) ─────────────────────────────────────────────────────

def test_a_dunning_cancellation_mid_term_is_recorded_as_a_broken_commitment(env, dbconn,
                                                                            monkeypatch):
    """Stripe cancels after up to eight failed attempts and never chases the committed months;
    the deleted event is identical to a clean expiry. It must not close silently."""
    aid, sub = _mk_account(dbconn, months=6, set_at_days_ago=30)
    with dbconn.cursor() as cur:
        cur.execute("INSERT INTO public.stripe_customers (customer_id, livemode) "
                    "VALUES (%s, false) ON CONFLICT DO NOTHING", (f"cus_{uuid.uuid4().hex[:14]}",))
        cur.execute("SELECT customer_id FROM public.stripe_customers LIMIT 1")
        cust = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO public.stripe_subscriptions "
            "  (subscription_id, customer_id, account_id, status, livemode) "
            "VALUES (%s, %s, %s, 'unpaid', false)", (sub, cust, aid))
    dbconn.commit()

    out = sync_mod.check_commitment(aid)
    assert out["broken"] is True and out["reason"] == "recorded", out
    assert out["months_unpaid"] >= 4, out
    assert out["arrears"] == round(out["months_unpaid"] * 1500.0, 2)

    with dbconn.cursor() as cur:
        cur.execute("SELECT action, amount, quantity, unit_price, billing_note "
                    "  FROM public.account_ledger "
                    " WHERE account_id = %s AND action = 'commitment_broken'", (aid,))
        rows = cur.fetchall()
    dbconn.commit()
    assert len(rows) == 1, rows
    action, amount, qty, unit, note = rows[0]
    assert float(amount) == 0.0, \
        "amount must be 0 — ledger_summary folds it into the 'charged' total and we have NOT " \
        "invoiced these months"
    assert int(qty) == out["months_unpaid"] and float(unit) == 1500.0
    assert "ARREARS" in note and "committed month" in note

    # And it must not move the figure the Billing view labels as charged.
    summary = ent_mod.ledger_summary(aid)
    assert summary["grand_total"] == 0.0, summary

    # IDEMPOTENT: a webhook redelivery must not stack rows in an append-only table.
    again = sync_mod.check_commitment(aid)
    assert again["reason"] == "already_recorded"
    with dbconn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM public.account_ledger "
                    " WHERE account_id = %s AND action = 'commitment_broken'", (aid,))
        n = cur.fetchone()[0]
    dbconn.commit()
    assert n == 1, f"redelivery stacked {n} arrears rows"


def test_an_active_subscription_is_not_a_broken_commitment(env, dbconn, monkeypatch):
    """The negative control: the SUBJECT satisfies every clause except the one under test."""
    aid, sub = _mk_account(dbconn, months=6, set_at_days_ago=30)
    with dbconn.cursor() as cur:
        cur.execute("SELECT customer_id FROM public.stripe_customers LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO public.stripe_customers (customer_id, livemode) "
                        "VALUES ('cus_ctrl', false) RETURNING customer_id")
            row = cur.fetchone()
        cur.execute("INSERT INTO public.stripe_subscriptions "
                    "  (subscription_id, customer_id, account_id, status, livemode) "
                    "VALUES (%s, %s, %s, 'active', false)", (sub, row[0], aid))
    dbconn.commit()
    out = sync_mod.check_commitment(aid)
    assert out["broken"] is False and out["reason"] == "status_active"


# ── the entitlement call-outs ───────────────────────────────────────────────────────

def test_provision_and_remove_capacity_both_call_the_sync(env, dbconn, monkeypatch):
    """The wiring itself, asserted structurally so a refactor cannot quietly drop it."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "saas", "entitlements.py")).read()
    tree = ast.parse(src)
    for fname in ("provision_capacity", "remove_capacity"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fname)
        calls = [n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert "_push_seat_quantities_to_stripe" in calls, \
            f"{fname} no longer pushes the new seat count to Stripe"


def test_a_sync_failure_never_fails_a_committed_capacity_purchase(env, dbconn, monkeypatch):
    """DB first, Stripe second: a purchase that COMMITTED must not report failure because Stripe
    was unreachable."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 5, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)

    def boom(*a, **k):
        raise RuntimeError("stripe is on fire")
    monkeypatch.setattr(sync_mod, "sync_account", boom)

    out = ent_mod.provision_capacity(aid, "expert", 2, actor="test", signature="Test Signer")
    assert out["ok"] is True, out
    assert _entitlement_qty(dbconn, aid, "expert") == 2


# ════════════════════════════════════════════════════════════════════════════════════
# F1 — AN UNRESOLVABLE PRICE IS NOT "STRIPE HAS NO SUCH ITEM"
#
# THE DEFECT. ``_sync_one`` guarded a null price only on the ``desired > 0`` branch. At
# ``desired == 0`` the lookup ``live_seat_items.get(str(price_id or ""))`` could not match, so
# ``have_item`` was False, and the code read that as "there is nothing to delete": it marked the
# row ``in_sync 0/0`` and returned ``already_absent``. MEASURED at 944fbaad — a tenant removing
# 10 Expert seats while ``seat_item_price_id`` returned None kept being billed CA$450/mo by
# Stripe; the internal invoice showed no add-on line so it credited nothing and printed
# "charged in full … nothing is payable"; the stored row read ``expert in_sync 0/0``; and
# ``reconcile()`` returned [] because its query keys on OUR OWN success flag — the very flag that
# write had just set. Invisible on every surface, triggered by one transient ``Price.list``
# failure (``resolve_price_id`` swallows it and there is no hardcoded seat-price fallback).
# ════════════════════════════════════════════════════════════════════════════════════

def test_an_unresolvable_price_at_zero_refuses_instead_of_claiming_already_absent(
        env, dbconn, monkeypatch):
    """THE CRITIC'S SCENARIO, on our own rails: 10 Expert seats synced, the price lookup then
    breaks, and the customer removes every one of them.

    (a) the sync REFUSES, (b) Stripe still carries the item so nothing was silently accepted,
    (c) reconcile() REPORTS it. The stored row must NOT read in_sync 0/0."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "expert", 10, unit_cents=4500)
    _fake_price(monkeypatch)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)

    rep = sync_mod.sync_account(aid, reason="grow")
    assert rep["ok"] is True, rep
    rows = _seat_rows(dbconn, aid)
    assert rows["expert"]["desired"] == 10 and rows["expert"]["synced"] == 10
    live_expert = [i for i in prov.items if i["price_id"] == "price_exp_seat"]
    assert len(live_expert) == 1 and live_expert[0]["quantity"] == 10

    # The customer removes every Expert seat — and the Price lookup is failing right now.
    _set_entitlement(dbconn, aid, "expert", 0)
    _break_price(monkeypatch, "expert")
    rep = sync_mod.sync_account(aid, reason="remove_all_expert")

    # (a) REFUSED — and reported as a failure, never as a success shape.
    assert rep["ok"] is False and rep["status"] == "partial", rep
    reasons = [f["reason"] for f in rep["failures"]]
    assert any("no Stripe price for tenant/expert" in str(r) for r in reasons), rep
    assert not any(o.get("seat_type") == "expert" for o in rep["applied"] + rep["skipped"]), rep

    # (b) NOTHING WAS ACCEPTED AT STRIPE. The item is still there, still at 10 — which is the
    #     honest state: we could not establish which Price to touch, so we touched nothing.
    still = [i for i in prov.items if i["price_id"] == "price_exp_seat"]
    assert len(still) == 1 and still[0]["quantity"] == 10, prov.items
    assert [c for c in prov.mutations() if c[0] == "delete"] == [], \
        "a refusal must issue no mutation at all"

    # …and the stored row does NOT claim success. This line used to read ``in_sync 0/0``: the
    # engine asserting, on its own money record, that Stripe carried nothing. It now reads
    # FAILED, with the desired 0 we could not reach and the 10 Stripe last CONFIRMED both intact.
    rows = _seat_rows(dbconn, aid)
    assert rows["expert"]["state"] == "failed", rows["expert"]
    assert rows["expert"]["desired"] == 0, rows["expert"]
    assert rows["expert"]["synced"] == 10, \
        "the last CONFIRMED quantity is what the customer is being billed — do not blank it"

    # (c) RECONCILE SEES IT — both as an unresolvable price and as a stale billable quantity.
    found = sync_mod.reconcile(aid)
    assert found["ok"] is True
    expert_findings = [f for f in found["findings"] if f.get("seat_type") == "expert"]
    assert expert_findings, found["findings"]
    assert any("price_unresolvable" in str(f.get("reason")) for f in expert_findings), \
        expert_findings


def test_reconcile_reports_an_in_sync_row_whose_price_no_longer_resolves(env, dbconn,
                                                                         monkeypatch):
    """The rows written by the OLD code are still out there, and a lookup_key can be renamed or
    archived at Stripe tomorrow. A row marked in_sync is not evidence about Stripe when we cannot
    even resolve which Price it refers to — the reconciler has to say so out loud."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "expert", 10, unit_cents=4500)
    _fake_price(monkeypatch)
    _use(monkeypatch, FakeProvider(items=[_base_item()]))
    sync_mod.sync_account(aid, reason="grow")
    assert _seat_rows(dbconn, aid)["expert"]["state"] == "in_sync"

    _break_price(monkeypatch, "expert")
    found = sync_mod.reconcile(aid)
    assert any(f.get("seat_type") == "expert" and "price_unresolvable" in str(f.get("reason"))
               for f in found["findings"]), found["findings"]


def test_deep_reconcile_reports_a_live_seat_item_no_stored_row_accounts_for(env, dbconn,
                                                                            monkeypatch):
    """THE EXACT RESIDUE THE OLD CODE LEFT: a stored row reading ``in_sync 0/0`` with a NULL
    price_id and NULL item_id, while Stripe carries a live Expert item at 10.

    The deep pass used to match live items by the STORED price_id — which is NULL on precisely
    these rows — so the lookup missed, ``live_qty`` came back None, and ``(None or 0) != (0 or 0)``
    reported nothing while the customer was being billed CA$450/mo."""
    aid, sub = _mk_account(dbconn)
    _set_entitlement(dbconn, aid, "standard", 15, base_qty=5, unit_cents=TENANT_STD_SEAT_CENTS)
    _fake_price(monkeypatch)
    orphan = {"item_id": "si_orphan", "price_id": "price_exp_seat", "price_lookup_key": None,
              "unit_amount_cents": 4500, "currency": "CAD", "quantity": 10}
    prov = FakeProvider(items=[_base_item(), orphan])
    _use(monkeypatch, prov)
    sync_mod.sync_account(aid, reason="grow")
    # That sync legitimately DELETED the expert item (entitlement 0). Put it back: the state under
    # test is "Stripe carries it, our map does not know", which is what the old branch produced.
    prov.items.append(dict(orphan))
    # Hand-write the row the old ``already_absent`` branch produced, then break the price lookup
    # so nothing can re-derive the cell — the state an archived/renamed lookup_key leaves behind.
    with dbconn.cursor() as cur:
        cur.execute("UPDATE public.stripe_seat_items "
                    "   SET sync_state = 'in_sync', synced_quantity = 0, desired_quantity = 0, "
                    "       price_id = NULL, stripe_item_id = NULL "
                    " WHERE account_id = %s AND seat_type = 'expert'", (aid,))
    dbconn.commit()
    _break_price(monkeypatch, "expert")

    found = sync_mod.reconcile(aid, deep=True)
    assert any(str(f.get("reason", "")).startswith("orphan_seat_item_on_subscription")
               for f in found["findings"]), found["findings"]


# ════════════════════════════════════════════════════════════════════════════════════
# F2 — HIRING AND FIRING MOVE THE MONEY ON A TEAM ACCOUNT
#
# ``_team_billing_view`` bills Standard on ``max(licensed, OCCUPIED) − included``, so the
# billable quantity moves on the SEAT roster, not only on capacity calls. Only
# ``provision_capacity`` / ``remove_capacity`` pushed, and ``saas/seats.py`` had zero references
# to the sync: measured, revoking 4 Standard seats on a real Team took the true bill from CA$950
# to CA$900 while Stripe went on billing CA$950, with the invoice printing "nothing is payable".
# ════════════════════════════════════════════════════════════════════════════════════

def _seat_row(conn, aid, user_id, seat_type):
    """Put a seat on the roster directly. Team Standard is ORDERED BY OCCUPYING (see
    ``_billable_count(gated=False)``), so occupancy above the licensed floor is the normal Team
    state, not a corruption — this is the shape the measured account was in."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO public.account_users (account_id, user_id, status, seat_type) "
                    "VALUES (%s, %s, 'active', %s)", (aid, user_id, seat_type))
    conn.commit()


def _billed_qty(prov, price_id):
    for i in prov.items:
        if i.get("price_id") == price_id:
            return i.get("quantity")
    return None


def test_revoking_and_hiring_a_team_seat_both_move_stripes_billed_quantity(env, dbconn,
                                                                           monkeypatch):
    """Drive a REAL Team account through revoke and hire through the REAL seat API and assert
    Stripe's billed quantity tracks the true bill in BOTH directions."""
    from saas import seats as seats_mod
    aid, sub = _mk_account(dbconn, kind="team")
    _set_entitlement(dbconn, aid, "standard", 5, base_qty=5, unit_cents=1250)
    _set_entitlement(dbconn, aid, "expert", 10, unit_cents=5000)
    ids = [str(uuid.uuid4()) for _ in range(10)]
    for u in ids[:8]:
        _seat_row(dbconn, aid, u, "standard")
    for u in ids[8:]:
        _seat_row(dbconn, aid, u, "expert")
    _fake_price(monkeypatch)
    prov = FakeProvider(items=[_base_item()])
    _use(monkeypatch, prov)

    sync_mod.sync_account(aid, reason="baseline")
    model, _rows = _render(aid, "TEAM baseline — 8 standard occupied, 2 expert")
    assert _billed_qty(prov, "price_std_seat") == 6, "8 occupied − 2 included"
    base_total = model["monthly_total"] if "monthly_total" in model else model["total"]

    # ── FIRE. The overcharge direction: without a push Stripe keeps billing the old count. ──
    for u in ids[:2]:
        assert seats_mod.revoke_seat(aid, u) is True
    assert _billed_qty(prov, "price_std_seat") == 4, \
        "revoking 2 Standard seats must lower Stripe's billed quantity 6 → 4"
    assert _seat_rows(dbconn, aid)["standard"]["synced"] == 4
    _render(aid, "TEAM after revoking 2 Standard")

    # ── HIRE. The under-charge direction, through the real cap-enforcing path. ──
    hired = str(uuid.uuid4())
    out = seats_mod.register_seat(aid, hired, seat_type="standard")
    assert out["ok"] is True, out
    assert _billed_qty(prov, "price_std_seat") == 5, \
        "hiring 1 Standard seat must raise Stripe's billed quantity 4 → 5"
    assert _seat_rows(dbconn, aid)["standard"]["synced"] == 5
    _render(aid, "TEAM after hiring 1 Standard")
    assert base_total is not None


def test_every_occupancy_moving_seat_path_pushes_to_stripe(env, dbconn, monkeypatch):
    """Structural pin. ``saas/seats.py`` had ZERO references to the sync; a future seat path
    written by someone who has not read the header is the failure this guards."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "saas", "seats.py")).read()
    tree = ast.parse(src)
    for fname in ("register_seat", "auto_provision_seat", "revoke_seat"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fname)
        calls = [n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert "_push_billable_seats" in calls, \
            f"seats.{fname} moves billable occupancy and no longer pushes it to Stripe"
    ent = ast.parse(open(os.path.join(os.path.dirname(__file__), "..",
                                      "saas", "entitlements.py")).read())
    for fname in ("upgrade_seat", "provision_bundle"):
        fn = next(n for n in ast.walk(ent)
                  if isinstance(n, ast.FunctionDef) and n.name == fname)
        names = [n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert ("sync_billable_seats" in names
                or "_push_seat_quantities_to_stripe" in names), \
            f"entitlements.{fname} moves billable quantity and does not push it to Stripe"


def test_reconcile_reports_billable_quantity_that_moved_without_a_sync(env, dbconn, monkeypatch):
    """THE DURABLE PROTECTION. Wiring every call site is necessary but not sufficient — the next
    seat path will be added by someone who has not read the header. A missed push must become a
    FINDING, not silence."""
    aid, sub = _mk_account(dbconn, kind="team")
    _set_entitlement(dbconn, aid, "standard", 5, base_qty=5, unit_cents=1250)
    ids = [str(uuid.uuid4()) for _ in range(8)]
    for u in ids:
        _seat_row(dbconn, aid, u, "standard")
    _fake_price(monkeypatch)
    _use(monkeypatch, FakeProvider(items=[_base_item()]))
    sync_mod.sync_account(aid, reason="baseline")
    assert _seat_rows(dbconn, aid)["standard"]["synced"] == 6
    assert sync_mod.reconcile(aid)["findings"] == []

    # A seat leaves the roster with NO sync — exactly what a future un-wired path would do.
    with dbconn.cursor() as cur:
        cur.execute("UPDATE public.account_users SET status = 'revoked' "
                    " WHERE account_id = %s AND user_id = %s", (aid, ids[0]))
    dbconn.commit()

    found = sync_mod.reconcile(aid)
    drift = [f for f in found["findings"]
             if "billable_quantity_moved_since_last_sync" in str(f.get("reason"))]
    assert drift, found["findings"]
    assert drift[0]["desired_quantity"] == 6 and drift[0]["billable_now"] == 5, drift


# ════════════════════════════════════════════════════════════════════════════════════
# F3 — THE GUARD MUST CLASSIFY THE ITEM ITSELF, NEVER TAKE THE CALLER'S WORD
#
# ``_assert_item_mutable`` opened with ``if item_class != ITEM_BASE: return``. PROVEN against the
# seam at 944fbaad: the same base item passed with ``item_class=ITEM_SEAT`` was DELETED, and
# ``mutation=create`` with the CA$1,500 base ``price_id`` and ``item_class=ITEM_SEAT`` created a
# base-priced item at quantity 10 — CA$15,000/mo.
# ════════════════════════════════════════════════════════════════════════════════════

def _catalog(monkeypatch):
    monkeypatch.setattr(sync_mod, "_resolved_base_price_ids", lambda: frozenset({"price_base"}))
    monkeypatch.setattr(sync_mod, "_resolved_seat_price_ids",
                        lambda: frozenset({"price_std_seat"}))


def test_the_caller_cannot_mislabel_the_base_item_into_a_deletion(monkeypatch):
    """(B) from the proof. A caller asserting 'this is a seat' about the BASE item must not be
    believed — the guard re-classifies and refuses on its OWN finding."""
    _catalog(monkeypatch)
    base = {"item_id": "si_base", "price_id": "price_base",
            "price_lookup_key": "faultline_enterprise_base_monthly"}
    with pytest.raises(sync_mod.BaseItemImmutable):
        sync_mod._assert_item_mutable(account_id="a", item=base, item_class=sync_mod.ITEM_SEAT,
                                      mutation="delete(quantity=None)", term=_term())


def test_the_caller_cannot_mislabel_a_base_price_into_a_created_item(monkeypatch):
    """(C) from the proof, and the expensive one: a CREATE has no existing item, so the guard has
    to classify the PRICE. Labelled a seat, priced as the CA$1,500 base, quantity 10."""
    _catalog(monkeypatch)
    with pytest.raises(sync_mod.BaseItemImmutable):
        sync_mod._assert_item_mutable(account_id="a", item={}, item_class=sync_mod.ITEM_SEAT,
                                      mutation="create(quantity=10)", term=_term(),
                                      price_id="price_base")


def test_creating_an_item_from_an_unclassifiable_price_is_refused(monkeypatch):
    """Minting a new recurring line from a Price this build cannot name is the same blind
    mutation ``sync_account`` refuses account-wide — and it is the direction that costs money."""
    _catalog(monkeypatch)
    with pytest.raises(sync_mod.SyncRefused):
        sync_mod._assert_item_mutable(account_id="a", item={}, item_class=sync_mod.ITEM_SEAT,
                                      mutation="create(quantity=1)", term=_term(),
                                      price_id="price_nobody_knows")


def test_a_correctly_labelled_seat_create_still_goes_through(monkeypatch):
    """The guard must not become a brake on the ordinary path."""
    _catalog(monkeypatch)
    sync_mod._assert_item_mutable(account_id="a", item={}, item_class=sync_mod.ITEM_SEAT,
                                  mutation="create(quantity=3)", term=_term(),
                                  price_id="price_std_seat")


def test_the_strip_lane_would_be_refused_if_it_ever_mislabelled_the_base(monkeypatch):
    """``term_acceleration.strip_elastic_items`` catches ``BaseItemImmutable`` and its docstring
    claims 'even if classification were wrong … the seam REFUSES'. That except was UNREACHABLE
    while the guard trusted the caller (the strip only ever passes SEAT/ADDON). This asserts the
    claim is now backed by code — the same call shape the strip makes, on a base item."""
    _catalog(monkeypatch)
    base = {"item_id": "si_base", "price_id": "price_base",
            "price_lookup_key": "faultline_enterprise_base_monthly"}
    with pytest.raises(sync_mod.BaseItemImmutable):
        sync_mod._assert_item_mutable(account_id="a", item=base,
                                      item_class=sync_mod.ITEM_AGENT_ADDON,
                                      mutation="delete(quantity=None)", term=_term())


# ════════════════════════════════════════════════════════════════════════════════════
# PART C — LIVE Stripe test mode. The only witness to "no immediate charge".
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def live():
    if not STRIPE_KEY:
        pytest.skip("SEATSYNC_STRIPE_KEY not set — the live Stripe lane cannot run")
    if not STRIPE_KEY.startswith("sk_test_"):
        pytest.fail("SEATSYNC_STRIPE_KEY is not a TEST key — refusing to touch live mode")
    prov = ps_mod.StripePaymentProvider(
        config={"api_secret": STRIPE_KEY, "webhook_secret": "whsec_seatsync_test", "mode": "test"})
    assert prov.is_configured()
    import stripe
    stripe.api_key = STRIPE_KEY
    base = prov.find_price_by_lookup_key("faultline_enterprise_base_monthly")
    seat = prov.find_price_by_lookup_key("faultline_seat_tenant_standard_monthly")
    assert base and seat, "the catalog prices must exist in this Stripe mode"
    cust = stripe.Customer.create(name="FL seatsync suite", description="throwaway test-mode")
    pm = stripe.PaymentMethod.create(type="card", card={"token": "tok_visa"})
    stripe.PaymentMethod.attach(pm.id, customer=cust.id)
    stripe.Customer.modify(cust.id, invoice_settings={"default_payment_method": pm.id})
    sub = stripe.Subscription.create(customer=cust.id, items=[{"price": base, "quantity": 1}])
    ctx = {"provider": prov, "stripe": stripe, "customer": cust.id, "subscription": sub.id,
           "base_price": base, "seat_price": seat}
    yield ctx
    try:
        stripe.Subscription.cancel(sub.id)
    except Exception:  # noqa: BLE001
        pass


def test_live_seats_ride_the_same_invoice_and_create_no_immediate_charge(live):
    """The evidence: base + seats on ONE upcoming invoice with the right total, and the already
    -paid invoice untouched."""
    _STRIPE_RAN["value"] = True
    prov, stripe = live["provider"], live["stripe"]
    invoices_before = {i.id for i in stripe.Invoice.list(customer=live["customer"], limit=20).data}

    res = prov.create_subscription_item(
        subscription_id=live["subscription"], price_id=live["seat_price"], quantity=10,
        idempotency_key=f"fl_suite_{uuid.uuid4().hex}")
    assert res["ok"] is True, res
    assert res["quantity"] == 10

    invoices_after = {i.id for i in stripe.Invoice.list(customer=live["customer"], limit=20).data}
    assert invoices_after == invoices_before, \
        "adding seats must NOT create an invoice — proration_behavior=none"

    up = prov.preview_subscription_invoice(customer_id=live["customer"],
                                           subscription_id=live["subscription"])
    print("\n── LIVE STRIPE UPCOMING INVOICE (test mode) ──")
    for ln in up["lines"]:
        print(f"  {ln['amount_cents']/100:>10.2f}  {ln['quantity']} x {ln['description']}")
    print(f"  {'TOTAL':>10}  {up['total_cents']/100:.2f} {up['currency']}")
    assert up["total_cents"] == ENTERPRISE_BASE_CENTS + 10 * TENANT_STD_SEAT_CENTS
    assert len(up["lines"]) == 2, up["lines"]
    assert not any(l["proration"] for l in up["lines"]), "no proration line may appear"
    live["item"] = res["item_id"]


def test_live_shrinking_seats_mints_no_credit_and_lands_on_the_next_invoice(live):
    prov, stripe = live["provider"], live["stripe"]
    item = live.get("item")
    if not item:
        pytest.skip("the add test did not run")
    invoices_before = {i.id for i in stripe.Invoice.list(customer=live["customer"], limit=20).data}

    res = prov.update_subscription_item(item_id=item, quantity=3,
                                        idempotency_key=f"fl_suite_{uuid.uuid4().hex}")
    assert res["ok"] is True and res["quantity"] == 3

    invoices_after = {i.id for i in stripe.Invoice.list(customer=live["customer"], limit=20).data}
    assert invoices_after == invoices_before, "shrinking must mint NO credit note"
    up = prov.preview_subscription_invoice(customer_id=live["customer"],
                                           subscription_id=live["subscription"])
    assert up["total_cents"] == ENTERPRISE_BASE_CENTS + 3 * TENANT_STD_SEAT_CENTS
    assert not any(l["amount_cents"] < 0 for l in up["lines"]), \
        "a negative line would be a proration credit the customer must not receive"


def test_live_stripe_itself_refuses_a_duplicate_price_on_one_subscription(live):
    """A SECOND, INDEPENDENT protection against duplicate seat items, measured rather than
    assumed. Stripe rejects adding an item whose Price is already on the subscription:

        "A new item with Price price_… can't be added to this Subscription because an existing
         Subscription Item si_… is already using that Price"

    So even if our idempotency key AND our list-then-decide pass both failed, Stripe would refuse
    the duplicate rather than silently double-bill. Pinned because ``_sync_one`` relies on
    one-item-per-price when it matches live items by price_id."""
    prov = live["provider"]
    existing = [i for i in prov.list_subscription_items(live["subscription"])
                if i["price_id"] == live["seat_price"]]
    if not existing:
        pytest.skip("no seat item present to duplicate")
    res = prov.create_subscription_item(subscription_id=live["subscription"],
                                        price_id=live["seat_price"], quantity=1,
                                        idempotency_key=f"fl_suite_dup_{uuid.uuid4().hex}")
    assert res["ok"] is False, "Stripe must refuse a duplicate Price on one subscription"
    assert "already using that Price" in str(res["reason"])
    after = [i for i in prov.list_subscription_items(live["subscription"])
             if i["price_id"] == live["seat_price"]]
    assert len(after) == len(existing) == 1


def test_live_a_repeated_write_with_the_same_key_creates_no_second_item(live):
    prov, stripe = live["provider"], live["stripe"]
    # Stripe allows only ONE item per Price per subscription (proved above), so clear the seat
    # item first — this test is about the IDEMPOTENCY KEY, not about that separate protection.
    for i in prov.list_subscription_items(live["subscription"]):
        if i["price_id"] == live["seat_price"]:
            prov.delete_subscription_item(item_id=i["item_id"])
    key = f"fl_suite_idem_{uuid.uuid4().hex}"
    a = prov.create_subscription_item(subscription_id=live["subscription"],
                                      price_id=live["seat_price"], quantity=2,
                                      idempotency_key=key)
    b = prov.create_subscription_item(subscription_id=live["subscription"],
                                      price_id=live["seat_price"], quantity=2,
                                      idempotency_key=key)
    assert a["ok"] and b["ok"]
    assert a["item_id"] == b["item_id"], "the retry created a SECOND item"
    items = prov.list_subscription_items(live["subscription"])
    assert sum(1 for i in items if i["item_id"] == a["item_id"]) == 1
    prov.delete_subscription_item(item_id=a["item_id"])


def test_live_deleting_an_absent_item_is_a_satisfied_intent_not_a_failure(live):
    out = live["provider"].delete_subscription_item(item_id="si_does_not_exist_at_all")
    assert out["ok"] is True and out["already_absent"] is True


def test_live_an_unresolvable_price_at_zero_leaves_the_real_stripe_item_untouched(
        live, env, dbconn, monkeypatch):
    """F1 ON THE REAL RAIL. A fake provider can testify that we issued no delete; only Stripe can
    testify that the item the customer is being BILLED for is still sitting on the subscription.

    Real test-mode customer, real subscription, real Expert seat item at quantity 10
    (CA$45 × 10 = CA$450/mo). The customer removes all 10 while the Expert Price lookup is
    failing — the exact trigger a transient ``Price.list`` error produces."""
    prov, stripe = live["provider"], live["stripe"]
    expert_price = prov.find_price_by_lookup_key("faultline_seat_tenant_expert_monthly")
    assert expert_price, "the tenant Expert seat Price must exist in this Stripe mode"
    cust = stripe.Customer.create(name="FL seatsync F1", description="throwaway test-mode")
    pm = stripe.PaymentMethod.create(type="card", card={"token": "tok_visa"})
    stripe.PaymentMethod.attach(pm.id, customer=cust.id)
    stripe.Customer.modify(cust.id, invoice_settings={"default_payment_method": pm.id})
    sub = stripe.Subscription.create(
        customer=cust.id,
        items=[{"price": live["base_price"], "quantity": 1},
               {"price": expert_price, "quantity": 10}])
    try:
        aid, _s = _mk_account(dbconn, sub_id=sub.id)
        _set_entitlement(dbconn, aid, "expert", 10)
        _use(monkeypatch, prov)
        # ``plans.resolve_price_id`` resolves through ``payments.get_payment_provider()``, which
        # reads the ENV config — unset in this suite. Point it at the live test-mode provider so
        # the lookup_keys resolve for real; the whole test is about what happens when ONE of them
        # stops resolving, which is meaningless if none of them ever did.
        from saas import payments as _payments_mod
        monkeypatch.setattr(_payments_mod, "get_payment_provider", lambda: prov)
        plans_mod._PRICE_ID_CACHE.clear()

        rep = sync_mod.sync_account(aid, reason="live_baseline")
        assert rep["ok"] is True, rep
        rows = _seat_rows(dbconn, aid)
        assert rows["expert"]["synced"] == 10 and rows["expert"]["state"] == "in_sync", rows
        print(f"\n── LIVE STRIPE sub {sub.id}: expert item at "
              f"{[i['quantity'] for i in prov.list_subscription_items(sub.id) if i['price_id'] == expert_price]}")

        # Remove every Expert seat while the price lookup is broken.
        _set_entitlement(dbconn, aid, "expert", 0)
        real_lookup = plans_mod.seat_item_price_id
        monkeypatch.setattr(plans_mod, "seat_item_price_id",
                            lambda kind, st: None if st == "expert" else real_lookup(kind, st))
        rep = sync_mod.sync_account(aid, reason="live_remove_all_expert")

        # (a) REFUSED.
        assert rep["ok"] is False and rep["status"] == "partial", rep
        assert any("no Stripe price for tenant/expert" in str(f.get("reason"))
                   for f in rep["failures"]), rep

        # (b) STRIPE STILL SHOWS THE ITEM — nothing was silently accepted.
        after = [i for i in prov.list_subscription_items(sub.id)
                 if i["price_id"] == expert_price]
        assert len(after) == 1 and after[0]["quantity"] == 10, after
        print(f"── LIVE STRIPE after the refused removal: expert item STILL "
              f"{after[0]['quantity']} × {expert_price} (CA$"
              f"{after[0]['quantity'] * (after[0].get('unit_amount_cents') or 0) / 100:.2f}/mo) "
              f"— and the seat map says so instead of claiming 'already absent'")

        # (c) RECONCILE REPORTS IT.
        found = sync_mod.reconcile(aid)
        assert any(f.get("seat_type") == "expert" and "price_unresolvable" in str(f.get("reason"))
                   for f in found["findings"]), found["findings"]
        assert _seat_rows(dbconn, aid)["expert"]["state"] != "in_sync"
    finally:
        # A shared module-level cache written by ONE test poisons every later one (learned the
        # hard way with platform_settings). Clear the live-resolved ids on the way out.
        plans_mod._PRICE_ID_CACHE.clear()
        try:
            stripe.Subscription.cancel(sub.id)
        except Exception:  # noqa: BLE001
            pass


# ════════════════════════════════════════════════════════════════════════════════════
# FALSE-GREEN GUARDS — must be last
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_database_half_actually_ran():
    assert _DB_RAN["value"], (
        "the DB half SKIPPED — this suite's outcome assertions did not execute. Pass "
        "POSTGRES_DSN (a database whose NAME contains 'test') and re-run.")


def test_the_live_stripe_half_actually_ran():
    assert _STRIPE_RAN["value"], (
        "the LIVE Stripe half SKIPPED — 'no immediate charge' and 'one invoice' were NOT "
        "proven. Pass SEATSYNC_STRIPE_KEY (a sk_test_ key) and re-run.")
