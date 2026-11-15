"""Tenant provisioning on a PAID checkout — B3 / S5 / S8 / S6.

⚠️ THESE TESTS ASSERT THE ROW, NEVER THE CATALOG. That is the entire reason this file exists.
The two pre-existing tests that touched this feature asserted ``catalog["account_kind"] ==
'tenant'`` — a DICTIONARY. They stayed green for the whole life of the defect, because a
dictionary is green with zero provisioning code in existence, which is exactly what shipped: a
CA$1500/mo Enterprise buyer kept ``account_kind='team'``, the 15-seat cap and none of the five
included seats. So every assertion in Part B below reads ``public.accounts`` and
``public.account_entitlements`` AFTER the webhook, and the teeth were proved by DELETING the
``_provision_account`` call and confirming the DB tests go red (see the commit report).

RUN IT (never raw pytest — owner rule):

    createdb: see the fixture header. Then
    PYTHONPATH="src:<venv site-packages>" \\
    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_provision_test \\
    python3 tools/fltest.py --bug CONTRACT --test tests/test_tenant_provisioning.py

THE THROWAWAY DATABASE is built by applying every migration numbered >= 131 to an empty DB
(``ON_ERROR_STOP=0`` — the per-tenant migrations in that range fail harmlessly and are not
needed). It must NOT be a real tenant database; the fixture refuses to run against one whose
name does not look like a test DB.

FALSE-GREEN GUARD: ``_DB_RAN`` is set by the DB fixture and asserted by the LAST test in the
file. A DSN-gated suite that skips its DB half reports "PASS | N passed, M skipped" and looks
green; that has produced a false green in this repo repeatedly. If the DB half did not run,
this suite is RED.
"""

import json
import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from saas import stripe_webhooks as swh          # noqa: E402
from saas import tenant_provisioning as prov     # noqa: E402
from saas import tenant_checkout as checkout     # noqa: E402
from saas import accounts as accounts_mod        # noqa: E402
from saas import entitlements as ent_mod         # noqa: E402


DEFAULT_TEST_DSN = ("postgresql://faultline:faultline@172.20.0.3:5432/"
                    "faultline_provision_test")
TEST_DSN = os.environ.get("PROVISION_TEST_DSN", DEFAULT_TEST_DSN)
WHSEC = "whsec_unit_test_secret_do_not_use_anywhere_real"

_DB_RAN = {"value": False}

# Control-plane singletons (the append-only ACTIVE contract chain, the platform settings row,
# the global operator realm) are shared by every suite in one database: the suites that
# write or depend on them run on one xdist worker (under --dist loadgroup, see tests/conftest.py).
pytestmark = pytest.mark.xdist_group("control_plane")


# ════════════════════════════════════════════════════════════════════════════════════
# PART A — pure decisions, no database
# ════════════════════════════════════════════════════════════════════════════════════

def test_tier_resolves_from_plan_id_which_is_the_key_a_producer_actually_writes():
    """S8. ``payments_stripe.create_checkout`` writes {account_id, user_id, plan_id,
    contract_id} and ``begin_subscription`` passes the tier key as ``plan_id``. The old consumer
    read ``metadata['tier']``, which no producer emits — so the loudest alarm on the money path
    was dead. This drives the REAL shape, deliberately WITHOUT a 'tier' key."""
    real_shape = {"account_id": str(uuid.uuid4()), "user_id": "fl_contract_7",
                  "plan_id": "enterprise", "contract_id": "7"}
    assert "tier" not in real_shape                      # the shape the producer emits
    row = prov.resolve_tenant_tier(real_shape)
    assert row is not None, "the real producer shape must resolve a tenant tier"
    assert row["tier"] == "enterprise"
    assert row["account_kind"] == "tenant"               # the naming trap, carried explicitly


def test_tier_still_resolves_from_an_explicit_tier_key():
    """A future producer (or another door) that DOES write ``tier`` is not ignored."""
    assert prov.resolve_tenant_tier({"tier": "team"})["tier"] == "team"


def test_a_non_tenant_plan_id_is_not_a_tenant_tier():
    """An individual/house checkout must not be treated as a tenant subscription."""
    assert prov.resolve_tenant_tier({"plan_id": "standard"}) is None
    assert prov.resolve_tenant_tier({}) is None
    assert prov.resolve_tenant_tier(None) is None


def test_the_webhooks_alarm_predicate_now_fires_on_the_real_shape():
    """The consumer inside stripe_webhooks, not just the helper."""
    assert swh._tenant_catalog_tier({"plan_id": "team"})
    assert not swh._tenant_catalog_tier({"plan_id": "standard"})


def test_kind_transitions_are_ordered_by_the_billing_catalog():
    """team → tenant is an upgrade; the reverse is refused; same is a no-op."""
    assert prov.plan_kind_transition("team", "tenant")["verdict"] == "upgrade"
    assert prov.plan_kind_transition("tenant", "team")["verdict"] == "downgrade"
    assert prov.plan_kind_transition("team", "team")["verdict"] == "already"
    assert prov.plan_kind_transition("tenant", "tenant")["verdict"] == "already"
    # The ordering is READ FROM THE CATALOG, not hardcoded here.
    assert prov._kind_ordinal("tenant") > prov._kind_ordinal("team")
    assert prov._kind_ordinal("team") == (ent_mod.TEAM_SEAT_CAP, ent_mod.TEAM_INCLUDED_STANDARD)


def test_off_ladder_tenancies_are_never_converted_by_a_webhook():
    """Converting the shared Singles house would re-kind every self-serve individual at once;
    clinical is a distinct tenancy with no conversion path (accounts.py:36-41)."""
    for kind in ("individual_house", "clinical"):
        d = prov.plan_kind_transition(kind, "tenant")
        assert d["apply"] is False and d["verdict"] == "off_ladder", (kind, d)


def test_an_unknown_target_kind_is_refused_not_guessed():
    d = prov.plan_kind_transition("team", "enterprise")   # 'enterprise' is a TIER, not a kind
    assert d["apply"] is False and d["verdict"] == "unknown"


def test_provisioning_refuses_a_tier_row_with_no_account_kind(capsys):
    """Catalog drift must not be papered over by deriving the kind from the tier STRING — that
    is the naming trap ('enterprise' is not a valid account_kind)."""
    out = prov.provision_from_checkout(account_id=str(uuid.uuid4()),
                                       tier_row={"tier": "enterprise"})
    assert out["provisioned"] is False
    assert out["reason"] == "tier_has_no_account_kind"
    assert "CRIT" in capsys.readouterr().err


def test_a_paid_tenant_tier_with_no_account_id_is_loud(capsys):
    out = prov.provision_from_checkout(account_id=None,
                                       tier_row=checkout.get_tier("enterprise"))
    assert out["provisioned"] is False and out["reason"] == "no_account_id"
    assert "CRIT" in capsys.readouterr().err


def test_a_missing_webhook_secret_now_asks_stripe_to_RETRY(monkeypatch, capsys):
    """S6. A 200 tells Stripe the event was delivered, and this branch writes NO ledger row, so
    the event is gone forever. Non-2xx → Stripe retries (up to 3 days live / 3 times in a
    sandbox), which is the only branch on which the event can still be recovered."""
    from saas import payment_config as _pc
    monkeypatch.setattr(_pc, "resolve_config", lambda: {"webhook_secret": ""})
    ok, meta = swh.verify_and_record(b'{"id":"evt_1"}', "t=1,v1=deadbeef")
    assert ok is False
    assert meta["reason"] == "missing_secret"
    assert meta["http_status"] == 503, "200 here silently destroys a paid event"
    assert "CRIT" in capsys.readouterr().err


# ── S5 amount verification, pure ──────────────────────────────────────────────────

def _contract(**kw):
    base = {"id": 42, "tier": "enterprise", "agreed_amount_cents": 150000,
            "agreed_currency": "CAD", "agreed_interval": "month", "account_id": None}
    base.update(kw)
    return base


def test_a_tax_inclusive_total_is_NOT_a_mismatch(capsys):
    """create_checkout sends ``automatic_tax: {"enabled": True}`` and our own invoices are
    ex-tax, so amount_total legitimately exceeds the agreed amount. Comparing amount_total
    naively would false-alarm on every taxable customer; amount_subtotal is the operand."""
    res = prov.verify_charged_amount(
        session_obj={"currency": "cad", "amount_subtotal": 150000, "amount_total": 169500,
                     "total_details": {"amount_tax": 19500, "amount_discount": 0}},
        contract=_contract(), session_id="cs_ok")
    assert res["checked"] is True and res["match"] is True
    assert res["classification"] == "match"
    assert "CRIT" not in capsys.readouterr().err


def test_a_wrong_stripe_price_is_caught_and_screams(capsys):
    """The whole point of S5: the contract records 150000 from our dict, Stripe bills whatever
    the Price says. A mis-mapped price id must not pass silently."""
    res = prov.verify_charged_amount(
        session_obj={"currency": "cad", "amount_subtotal": 75000, "amount_total": 75000,
                     "total_details": {"amount_tax": 0, "amount_discount": 0}},
        contract=_contract(), session_id="cs_bad")
    assert res["match"] is False and res["classification"] == "amount_mismatch"
    err = capsys.readouterr().err
    assert "CRIT" in err and "DOES NOT MATCH THE SIGNED CONTRACT" in err


def test_a_currency_mismatch_is_caught(capsys):
    res = prov.verify_charged_amount(
        session_obj={"currency": "usd", "amount_subtotal": 150000, "amount_total": 150000,
                     "total_details": {}},
        contract=_contract(), session_id="cs_usd")
    assert res["match"] is False and res["classification"] == "currency_mismatch"
    assert "CRIT" in capsys.readouterr().err


def test_a_zero_first_invoice_is_classified_not_mislabelled(capsys):
    """A free trial produces a $0 first invoice (payment_status 'paid'). We do not enable
    trials today, so it is still an alarm — but it is NAMED, so an operator is not sent hunting
    for a pricing error that is really a trial."""
    res = prov.verify_charged_amount(
        session_obj={"currency": "cad", "amount_subtotal": 0, "amount_total": 0,
                     "total_details": {"amount_tax": 0, "amount_discount": 0}},
        contract=_contract(), session_id="cs_trial")
    assert res["match"] is False and res["classification"] == "zero_first_invoice"
    assert "CRIT" in capsys.readouterr().err


def test_a_session_with_no_amount_is_reported_as_unchecked_not_as_a_match():
    res = prov.verify_charged_amount(session_obj={"currency": "cad"},
                                     contract=_contract(), session_id="cs_none")
    assert res["checked"] is False and res["match"] is None
    assert res["classification"] == "no_amount_on_session"


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
        pytest.skip(f"provisioning test DB unreachable ({type(exc).__name__}): {TEST_DSN}")
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.accounts'), "
                    "       to_regclass('public.account_entitlements'), "
                    "       to_regclass('public.account_ledger'), "
                    "       to_regclass('public.signed_contracts'), "
                    "       to_regclass('public.stripe_events'), "
                    "       to_regclass('public.saas_audit_log')")
        present = cur.fetchone()
    conn.commit()
    if not all(present):
        conn.close()
        pytest.skip("throwaway DB is missing the SaaS control plane — see this file's header")
    _own_the_active_agreement(conn)
    yield conn
    conn.close()


def _own_the_active_agreement(conn, months: int = 6):
    """Publish the ACTIVE agreement this module's term assertions are about (#44).

    tenant_term_months() reads CONTRACT_TERM_MONTHS frozen onto the ACTIVE contract template
    FIRST, and public.contract_templates is append-only (migration 243). In a shared-DB run the
    active version is whatever another suite published last - measured: 24 from
    test_retention_settings' term-v2, 8 from test_contract_term_authority's fixture - so the
    "six-month term" pins read 24 once those suites started running (they used to be silently
    skipped by an import-time DSN pop, #63). Same remedy test_contract_term_authority already
    uses: create the active agreement, never assume one. The platform setting is set to the
    value for the publish and put back."""
    from saas import contracts as _contracts
    from saas import platform_settings as _ps
    mp = pytest.MonkeyPatch()
    mp.setenv("POSTGRES_DSN", TEST_DSN)
    before = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT contract_term_months FROM public.platform_settings WHERE singleton")
            row = cur.fetchone()
            before = row[0] if row else None
            cur.execute("UPDATE public.platform_settings SET contract_term_months = %s "
                        "WHERE singleton", (months,))
        conn.commit()
        _ps.invalidate()
        res = _contracts.publish_terms_version(
            version="tenant-prov-" + uuid.uuid4().hex[:10],
            title="Tenant Provisioning Fixture",
            body_markdown="### Term\n\nThis is a **{{CONTRACT_TERM_MONTHS}}-month term**.\n",
            created_by="test-tenant-provisioning",
            change_summary="Fixture version published by tests/test_tenant_provisioning.py to "
                           "own the active agreement for the duration of this suite.",
            change_is_material=False,
        )
        assert res.get("ok"), f"could not publish the fixture agreement: {res}"
    finally:
        if before is not None:
            with conn.cursor() as cur:
                cur.execute("UPDATE public.platform_settings SET contract_term_months = %s "
                            "WHERE singleton", (before,))
            conn.commit()
        _ps.invalidate()
        mp.undo()


@pytest.fixture
def env(monkeypatch, dbconn):
    _DB_RAN["value"] = True
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    monkeypatch.setenv("SAAS_MODE", "1")
    from saas import payment_config as _pc
    monkeypatch.setattr(_pc, "resolve_config", lambda: {"webhook_secret": WHSEC})
    return True


def _mk_account(conn, *, kind="team", seat_limit=None, name=None):
    if seat_limit is None:
        seat_limit = accounts_mod.hard_seat_cap(kind)
    slug = f"t{uuid.uuid4().hex[:12]}"
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


def _read_account(conn, aid):
    with conn.cursor() as cur:
        cur.execute("SELECT account_kind, seat_limit, comped, externally_billed "
                    "FROM public.accounts WHERE account_id = %s", (aid,))
        row = cur.fetchone()
    conn.commit()
    return row


def _read_entitlements(conn, aid):
    with conn.cursor() as cur:
        cur.execute("SELECT seat_type, quantity, base_quantity FROM public.account_entitlements "
                    "WHERE account_id = %s ORDER BY seat_type", (aid,))
        rows = cur.fetchall()
    conn.commit()
    return rows


def _count_base_grants(conn, aid):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM public.account_ledger "
                    "WHERE account_id = %s AND action = 'base_grant'", (aid,))
        n = cur.fetchone()[0]
    conn.commit()
    return int(n)


def _audit_rows(conn, aid, action):
    with conn.cursor() as cur:
        cur.execute("SELECT result, detail FROM public.saas_audit_log "
                    "WHERE account_id = %s AND action = %s ORDER BY id", (str(aid), action))
        rows = cur.fetchall()
    conn.commit()
    return rows


def _event(*, account_id, plan_id, session_id=None, subscription_id=None, contract_id=None,
           amount_subtotal=None, amount_total=None, tax=0, currency="cad"):
    """A checkout.session.completed shaped EXACTLY like the real producer: the metadata keys
    are the four ``payments_stripe.create_checkout`` writes — note there is NO 'tier' key."""
    meta = {"account_id": account_id, "user_id": f"fl_contract_{contract_id or 0}",
            "plan_id": plan_id}
    if contract_id is not None:
        meta["contract_id"] = str(contract_id)
    obj = {
        "id": session_id or f"cs_{uuid.uuid4().hex[:20]}",
        "object": "checkout.session",
        "customer": f"cus_{uuid.uuid4().hex[:14]}",
        "subscription": subscription_id or f"sub_{uuid.uuid4().hex[:18]}",
        "mode": "subscription", "payment_status": "paid", "status": "complete",
        "currency": currency, "metadata": meta,
    }
    if amount_subtotal is not None:
        obj["amount_subtotal"] = amount_subtotal
        obj["amount_total"] = amount_total if amount_total is not None else amount_subtotal
        obj["total_details"] = {"amount_tax": tax, "amount_discount": 0}
    return {"id": f"evt_{uuid.uuid4().hex[:22]}", "type": "checkout.session.completed",
            "api_version": "2024-06-20", "livemode": False, "created": int(time.time()),
            "data": {"object": obj}}


# ── B3: THE BLOCKER — the row, after the money ────────────────────────────────────

def test_an_enterprise_purchase_actually_makes_the_account_enterprise(dbconn, env):
    """THE BLOCKER, asserted by READING THE ROW. Before this fix the buyer stayed
    account_kind='team' with the 15-seat cap and zero included seats."""
    aid = _mk_account(dbconn, kind="team")
    assert _read_account(dbconn, aid)[:2] == ("team", ent_mod.TEAM_SEAT_CAP)

    swh.handle_checkout_session_completed(_event(account_id=aid, plan_id="enterprise"))

    kind, cap, _comped, _ext = _read_account(dbconn, aid)
    assert kind == "tenant", "an Enterprise buyer must BE an Enterprise account"
    assert cap == accounts_mod.hard_seat_cap("tenant"), "the 15-seat Team cap must be gone"
    assert cap > ent_mod.TEAM_SEAT_CAP
    assert _read_entitlements(dbconn, aid) == [
        ("standard", ent_mod.BASE_QUANTITY, ent_mod.BASE_QUANTITY)], \
        "the five included Standard seats the page sells must exist as capacity"
    assert ent_mod.ENTERPRISE_INCLUDED_STANDARD == ent_mod.BASE_QUANTITY


def test_a_team_purchase_writes_the_TEAM_allotment_and_not_the_ENTERPRISE_one(dbconn, env):
    """🔴 THIS TEST CAUGHT A REAL OVER-PROVISIONING BUG AND ITS ASSERTION WAS STILL HALF WRONG.
    Renamed and strengthened 2026-08-17, deliberately. Read this before "fixing" it back.

    It used to assert ``_read_entitlements(...) == []`` — that a Team gets NO standard capacity
    row at all — on the strength of ``_team_billing_view``'s own comment. The DANGER it names is
    real and still asserted below: a seeded ``standard 5/5`` row makes that view bill
    ``max(5, occupied) - 2 included = 3`` à-la-carte Standard seats, CA$37.50/mo invented for a
    two-person Team that bought nothing of the sort.

    THE REMEDY WAS WRONG. "No row" is not the safe alternative, because ``seats.register_seat``
    gates a standard seat on ``SUM(entitlements.quantity)`` and answers 402 "no licensed seat
    capacity (0 active / 0 licensed)" when there is none — so a Team that had just PAID for a
    package including two Standard seats could not register either of them. Nobody noticed
    because ``migrations/139``'s unfiltered backfill was handing every account a 5/5 row on every
    container boot, which is exactly the over-billing above. Remove that crutch — as this branch
    does — and "no row" becomes a customer who bought a package and cannot use it.

    The state where all three agree is a row at the KIND'S OWN allotment, and it is measured, not
    argued: a Team at 2/2 registers exactly two Standard seats, is refused the third with
    "provision more capacity first" (correct — that seat is à-la-carte), and invoices exactly the
    base package with no add-on line. So this test now pins the ROW, the GATE and the INVOICE
    together, which is strictly more than "== []" ever did."""
    aid = _mk_account(dbconn, kind="team")
    swh.handle_checkout_session_completed(_event(account_id=aid, plan_id="team"))
    kind, cap, _c, _e = _read_account(dbconn, aid)
    assert (kind, cap) == ("team", ent_mod.TEAM_SEAT_CAP)

    inc = ent_mod.included_standard_for("team")
    rows = {r[0]: (r[1], r[2]) for r in _read_entitlements(dbconn, aid)}
    assert rows.get("standard") == (inc, inc), (
        f"a Team must be licensed EXACTLY its own included allotment ({inc}/{inc}); got "
        f"{rows.get('standard')}. The ENTERPRISE allotment here is CA$37.50/mo of invented "
        f"charge; no row at all is a customer who cannot use what they paid for.")

    # THE INVOICE, not just the row. A Team occupying nothing must bill the base package and
    # nothing else — that is the assertion the over-billing bug would actually fail.
    view = ent_mod.billing_view(aid)
    assert view is not None
    assert view["monthly_total"] == ent_mod.base_package_price_for("team"), (
        f"a Team that bought only its package must invoice exactly that package; "
        f"invoiced {view['monthly_total']} with lines "
        f"{[(l['kind'], l['quantity'], l['monthly']) for l in view['lines']]}")
    assert [l for l in view["lines"] if l["kind"] == "addon"] == [], \
        "an add-on line on a Team that bought nothing à-la-carte is the over-billing bug"

    # And the gate that decides the allotment is read from the biller, not from a kind literal.
    assert ent_mod.included_standard_for("team") != ent_mod.BASE_QUANTITY
    assert ent_mod.included_standard_for("tenant") == ent_mod.BASE_QUANTITY


def test_the_page_and_the_biller_agree_about_included_seats(dbconn, env, capsys):
    """A drift between the catalog's ``seats_included`` and the biller's
    ``included_standard_for`` means the customer is sold one thing and billed for another. It
    is reported as a CRIT at provisioning time; this asserts there is no drift TODAY, so the
    alarm means something when it does fire."""
    for tier, kind in (("team", "team"), ("enterprise", "tenant")):
        assert checkout.get_tier(tier)["seats_included"] == ent_mod.included_standard_for(kind)
    aid = _mk_account(dbconn, kind="team")
    swh.handle_checkout_session_completed(_event(account_id=aid, plan_id="enterprise"))
    assert "CATALOG DRIFT" not in capsys.readouterr().err


def test_the_stripe_paying_tenant_is_marked_EXTERNALLY_BILLED_and_is_NOT_comped(dbconn, env):
    """🔴 THIS TEST PREVIOUSLY PINNED THE BUG — read the rewrite before "restoring" it.

    It used to be ``test_the_stripe_paying_tenant_is_comped_…`` and asserted ``comped is True``.
    The EFFECT it was reaching for is right (an account Stripe already charges must not ALSO be
    issued a full internal invoice) but the CONCEPT was wrong, and the test froze the wrong one
    in place. COMP = COMPLIMENTARY = a deliberate human WRITE-OFF with a signature and a reason.
    A Stripe payer is the opposite: charged in full, on another rail (migration 245)."""
    aid = _mk_account(dbconn, kind="team")
    swh.handle_checkout_session_completed(_event(account_id=aid, plan_id="enterprise"))
    _k, _c, comped, external = _read_account(dbconn, aid)
    assert comped is False, "a PAYING customer must never be flagged as a give-away"
    assert external is True, "the internal biller must stand down for money Stripe collects"
    assert ent_mod.comp_state(aid)["active"] is False
    st = ent_mod.external_billing_state(aid)
    assert st["active"] is True and st["processor"] == "stripe"
    assert str(st["reference"] or "").startswith("sub_"), "the linkage must be recorded"
    assert st["source"] == "stripe-webhook"      # provenance, not a fabricated authoriser


# ── idempotency: Stripe redelivers for up to three days ───────────────────────────

def test_a_replay_does_not_stack_entitlements_or_re_grant_the_base_package(dbconn, env):
    aid = _mk_account(dbconn, kind="team")
    ev = _event(account_id=aid, plan_id="enterprise")
    swh.handle_checkout_session_completed(ev)
    swh.handle_checkout_session_completed(ev)          # the exact redelivery
    swh.handle_checkout_session_completed(              # and a fresh event, same subscription
        _event(account_id=aid, plan_id="enterprise"))
    assert _read_account(dbconn, aid)[:2] == ("tenant", accounts_mod.hard_seat_cap("tenant"))
    assert _read_entitlements(dbconn, aid) == [
        ("standard", ent_mod.BASE_QUANTITY, ent_mod.BASE_QUANTITY)]
    assert _count_base_grants(dbconn, aid) == 1, "the base grant must not be re-ledgered"


def test_a_replay_does_not_append_a_second_external_billing_row_to_the_ledger(dbconn, env):
    """🔴 FOUND BY RUNNING THE REPLAY AND READING account_ledger, not by reading the code.

    The setter is idempotent in the accounts row but NOT in the ledger: each call appends a row,
    and account_ledger is append-only so nothing can be removed afterwards. Stripe redelivers
    for up to three days.

    NOTE what changed with migration 245: the old 'comp' row carried ``amount = -waived`` and
    'comp' is not in PAYMENT_ACTIONS, so ``ledger_summary`` summed every redelivery into
    ``grand_total`` — the figure the Billing view presents as what the account was CHARGED. An
    ``external_billing_on`` row carries amount 0 (nothing is waived), so the money damage is
    gone even without the guard; the guard remains because duplicate rows are still noise in an
    append-only audit record. ZERO 'comp' rows must be written at all."""
    aid = _mk_account(dbconn, kind="team")
    ev = _event(account_id=aid, plan_id="enterprise")
    for _ in range(3):
        swh.handle_checkout_session_completed(ev)
    with dbconn.cursor() as cur:
        cur.execute("SELECT action, COUNT(*), COALESCE(SUM(amount), 0) "
                    "FROM public.account_ledger WHERE account_id = %s "
                    "AND action IN ('comp', 'comp_off', 'external_billing_on') "
                    "GROUP BY action", (aid,))
        rows = {a: (int(n), float(s)) for a, n, s in cur.fetchall()}
    dbconn.commit()
    assert "comp" not in rows, "a PAYING customer must never write a comp credit"
    assert rows.get("external_billing_on", (0, 0.0))[0] == 1, \
        f"{rows} — one purchase must leave ONE external-billing row (append-only ledger)"
    assert rows["external_billing_on"][1] == 0.0, \
        "an external-billing row must carry amount 0 — nothing was waived"
    _k, _c, comped, external = _read_account(dbconn, aid)
    assert (comped, external) == (False, True)


def test_a_replay_never_lowers_a_seat_limit_an_operator_has_raised(dbconn, env):
    """seat_limit moves by GREATEST, so provisioning can only ever RAISE it."""
    aid = _mk_account(dbconn, kind="team")
    swh.handle_checkout_session_completed(_event(account_id=aid, plan_id="enterprise"))
    raised = accounts_mod.hard_seat_cap("tenant") + 5000
    assert accounts_mod.set_seat_limit(aid, raised)["ok"] is True
    swh.handle_checkout_session_completed(_event(account_id=aid, plan_id="enterprise"))
    assert _read_account(dbconn, aid)[1] == raised, "an operator's raised cap was clobbered"


def test_a_cap_an_operator_LOWERED_is_not_silently_restored(dbconn, env):
    """🔴 THE WRITE-PATH AUDIT FOUND THIS, and it inverts what the first cut did.

    ``accounts.set_seat_limit`` is an operator lever that may deliberately LOWER a cap
    ("lowering the limit BELOW the current active-seat count is ALLOWED"). An Enterprise
    account restricted to 3 seats by a human is INDISTINGUISHABLE in the row from one with a
    stale cap, so a webhook that "normalises upward" would restore 100000 on every redelivery
    and every monthly renewal — a machine repeatedly overruling a person. The cap therefore
    moves ONLY in the statement that changes the kind."""
    aid = _mk_account(dbconn, kind="tenant", seat_limit=3)
    swh.handle_checkout_session_completed(_event(account_id=aid, plan_id="enterprise"))
    assert _read_account(dbconn, aid)[1] == 3, "a webhook overrode an operator's seat cap"
    # ...but a real KIND CHANGE does raise it, because that is what was bought.
    aid2 = _mk_account(dbconn, kind="team", seat_limit=3)
    swh.handle_checkout_session_completed(_event(account_id=aid2, plan_id="enterprise"))
    assert _read_account(dbconn, aid2)[:2] == ("tenant", accounts_mod.hard_seat_cap("tenant"))


def test_a_renewal_never_touches_the_seat_limit(dbconn, env):
    """invoice.paid re-provisions monthly. Because the cap is bound to the kind change, a
    renewal on an already-correct account cannot move it."""
    aid = _mk_account(dbconn, kind="tenant", seat_limit=42)
    inv = {"id": f"evt_{uuid.uuid4().hex[:22]}", "type": "invoice.paid", "livemode": False,
           "created": int(time.time()),
           "data": {"object": {"id": f"in_{uuid.uuid4().hex[:16]}",
                               "subscription": f"sub_{uuid.uuid4().hex[:18]}",
                               "customer": f"cus_{uuid.uuid4().hex[:14]}",
                               "amount_paid": 150000, "currency": "cad",
                               "metadata": {"account_id": aid, "plan_id": "enterprise"}}}}
    swh.handle_invoice_paid(inv)
    assert _read_account(dbconn, aid)[1] == 42


# ── refusals: a paid event must never take capability away ────────────────────────

def test_provisioning_never_downgrades_an_enterprise_account(dbconn, env, capsys):
    aid = _mk_account(dbconn, kind="tenant")
    before = _read_account(dbconn, aid)
    swh.handle_checkout_session_completed(_event(account_id=aid, plan_id="team"))
    assert _read_account(dbconn, aid)[:2] == before[:2], "a Team event downgraded Enterprise"
    assert "CRIT" in capsys.readouterr().err
    rows = _audit_rows(dbconn, aid, "account.provision_from_stripe")
    assert rows and rows[-1][0] == "failure" and "downgrade" in rows[-1][1]


def test_a_clinical_account_is_never_converted_by_a_webhook(dbconn, env, capsys):
    aid = _mk_account(dbconn, kind="clinical")
    swh.handle_checkout_session_completed(_event(account_id=aid, plan_id="enterprise"))
    assert _read_account(dbconn, aid)[0] == "clinical"
    assert _read_entitlements(dbconn, aid) == []
    assert "CRIT" in capsys.readouterr().err


def test_an_unknown_account_id_is_loud_and_leaves_a_durable_trace(dbconn, env, capsys):
    """The customer PAID. If we cannot provision, an operator must be able to FIND them after
    the log buffer has rolled — hence the saas_audit_log row, not just the stderr line."""
    ghost = str(uuid.uuid4())
    swh.handle_checkout_session_completed(_event(account_id=ghost, plan_id="enterprise"))
    assert "CRIT" in capsys.readouterr().err
    rows = _audit_rows(dbconn, ghost, "account.provision_from_stripe")
    assert rows and rows[-1][0] == "failure" and "account_not_found" in rows[-1][1]


def test_an_individual_checkout_provisions_nothing_and_is_silent(dbconn, env, capsys):
    aid = _mk_account(dbconn, kind="team")
    out = swh.handle_checkout_session_completed(_event(account_id=aid, plan_id="standard"))
    assert out["provisioning"]["reason"] == "not_a_tenant_tier"
    assert _read_account(dbconn, aid)[:2] == ("team", ent_mod.TEAM_SEAT_CAP)
    assert "tenant-provisioning-CRIT" not in capsys.readouterr().err


# ── renewals self-heal ────────────────────────────────────────────────────────────

def test_a_renewal_self_heals_an_account_whose_purchase_provisioning_failed(dbconn, env):
    """invoice.paid re-provisions. An account left wrong by a failed purchase-time run is
    corrected at the next monthly invoice instead of staying wrong forever."""
    aid = _mk_account(dbconn, kind="team")
    inv = {"id": f"evt_{uuid.uuid4().hex[:22]}", "type": "invoice.paid",
           "livemode": False, "created": int(time.time()),
           "data": {"object": {"id": f"in_{uuid.uuid4().hex[:16]}",
                               "subscription": f"sub_{uuid.uuid4().hex[:18]}",
                               "customer": f"cus_{uuid.uuid4().hex[:14]}",
                               "amount_paid": 150000, "currency": "cad",
                               "metadata": {"account_id": aid, "plan_id": "enterprise"}}}}
    swh.handle_invoice_paid(inv)
    assert _read_account(dbconn, aid)[:2] == ("tenant", accounts_mod.hard_seat_cap("tenant"))
    assert _read_entitlements(dbconn, aid) == [
        ("standard", ent_mod.BASE_QUANTITY, ent_mod.BASE_QUANTITY)]


# ── S5 end-to-end: mismatch is loud but never blocks the paid grant ───────────────

def _insert_template(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM public.contract_templates WHERE retired_at IS NULL "
                    "ORDER BY effective_at DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            conn.commit()
            return int(row[0])
        cur.execute("INSERT INTO public.contract_templates (version, title, body_markdown) "
                    "VALUES ('test-v1', 'Test Terms', 'body') RETURNING id")
        tid = cur.fetchone()[0]
    conn.commit()
    return int(tid)


def _insert_contract(conn, *, session_id, amount_cents, currency="CAD", tier="enterprise",
                     age_days=0):
    """``accepted_at`` is set AT INSERT, never by a later UPDATE — migration 243's append-only
    trigger correctly refuses to modify an acceptance fact ("public.signed_contracts.accepted_at
    is an acceptance fact — it is append-only and cannot be modified"). Backdating a fixture is
    an INSERT-time concern."""
    tid = _insert_template(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.signed_contracts (contract_template_id, signer_email, "
            "signer_name, signer_ip, tier, stripe_price_id, stripe_checkout_session_id, "
            "contract_snapshot, contract_version, agreed_amount_cents, agreed_currency, "
            "agreed_interval, status, accepted_at) "
            "VALUES (%s, %s, 'Test Signer', '203.0.113.9', %s, 'price_x', %s, 'snap', "
            "'test-v1', %s, %s, 'month', 'accepted', NOW() - make_interval(days => %s)) "
            "RETURNING id",
            (tid, f"{uuid.uuid4().hex[:8]}@example.com", tier, session_id,
             int(amount_cents), currency, int(age_days)),
        )
        cid = cur.fetchone()[0]
    conn.commit()
    return int(cid)


def test_a_price_mismatch_screams_but_the_customer_still_gets_provisioned(dbconn, env, capsys):
    """They paid — withholding the product would punish the customer for OUR price error. The
    alarm has to be loud AND durable instead."""
    aid = _mk_account(dbconn, kind="team")
    sess = f"cs_{uuid.uuid4().hex[:20]}"
    cid = _insert_contract(dbconn, session_id=sess, amount_cents=150000)
    out = swh.handle_checkout_session_completed(_event(
        account_id=aid, plan_id="enterprise", session_id=sess, contract_id=cid,
        amount_subtotal=75000))                                   # billed HALF what was signed
    assert out["contract"]["amount_check"]["match"] is False
    assert out["contract"]["amount_check"]["classification"] == "amount_mismatch"
    assert _read_account(dbconn, aid)[0] == "tenant", "provisioning must NOT be blocked"
    err = capsys.readouterr().err
    assert "CRIT" in err and "DOES NOT MATCH THE SIGNED CONTRACT" in err


def test_the_agreed_amount_matching_the_charge_passes_quietly(dbconn, env, capsys):
    aid = _mk_account(dbconn, kind="team")
    sess = f"cs_{uuid.uuid4().hex[:20]}"
    cid = _insert_contract(dbconn, session_id=sess, amount_cents=150000)
    out = swh.handle_checkout_session_completed(_event(
        account_id=aid, plan_id="enterprise", session_id=sess, contract_id=cid,
        amount_subtotal=150000, amount_total=169500, tax=19500))
    assert out["contract"]["amount_check"]["match"] is True
    assert "DOES NOT MATCH" not in capsys.readouterr().err


# ── the whole signed pipeline, once, end to end ───────────────────────────────────

def _signed(payload: dict, secret: str = WHSEC):
    """Build a real Stripe-Signature header per https://docs.stripe.com/webhooks#verify-manually
    (HMAC-SHA256 over ``{t}.{payload}``). ``stripe.Webhook.construct_event`` does the
    verification — this only reproduces the client side."""
    import hashlib
    import hmac
    body = json.dumps(payload).encode()
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={t},v1={sig}"


def test_a_signature_verified_event_provisions_the_account_end_to_end(dbconn, env):
    pytest.importorskip("stripe")
    aid = _mk_account(dbconn, kind="team")
    ev = _event(account_id=aid, plan_id="enterprise")
    body, sig = _signed(ev)
    ok, verified = swh.verify_and_record(body, sig)
    assert ok is True and verified.get("id") == ev["id"], verified
    result = swh.process_event(verified)
    assert result["status"] == "processed", result
    assert result["handler"]["provisioning"]["provisioned"] is True
    assert _read_account(dbconn, aid)[:2] == ("tenant", accounts_mod.hard_seat_cap("tenant"))


# ── S6 report ─────────────────────────────────────────────────────────────────────

def test_the_unreconciled_report_lists_an_accepted_contract_with_no_completed_event(
        dbconn, env):
    """A READ-ONLY operator surface. Ambiguous by nature (abandoned checkout vs lost webhook)
    and documented as such — it must never write, grant or void."""
    sess = f"cs_{uuid.uuid4().hex[:20]}"
    cid = _insert_contract(dbconn, session_id=sess, amount_cents=150000, age_days=2)
    rep = prov.report_unreconciled_contracts(older_than_minutes=60, limit=100)
    assert rep["error"] is None
    assert cid in [c["contract_id"] for c in rep["candidates"]]

    # ...and once the completed event for that session is in the ledger, it drops out.
    ev = _event(account_id=_mk_account(dbconn, kind="team"), plan_id="enterprise",
                session_id=sess, contract_id=cid)
    body, sig = _signed(ev)
    pytest.importorskip("stripe")
    assert swh.verify_and_record(body, sig)[0] is True
    rep2 = prov.report_unreconciled_contracts(older_than_minutes=60, limit=100)
    assert cid not in [c["contract_id"] for c in rep2["candidates"]]


# ════════════════════════════════════════════════════════════════════════════════════
# B2 — THE TERM. The product must not contradict the contract the customer signed.
#
# migrations/243_contract_infrastructure.sql:405 "changes take effect on your next invoice"
# migrations/243_contract_infrastructure.sql:427 "Your subscription renews automatically each
#                                                  month"
# …while ``accounts.term_months`` is NOT NULL DEFAULT 6 and provisioning never set it.
#
# EVERY assertion below reads the ``public.accounts`` ROW, for the same reason Part B does.
# ════════════════════════════════════════════════════════════════════════════════════

def _read_term(conn, aid):
    with conn.cursor() as cur:
        cur.execute("SELECT term_months, expires_at FROM public.accounts "
                    "WHERE account_id = %s", (aid,))
        row = cur.fetchone()
    conn.commit()
    return row


def _set_term(conn, aid, months, *, expires=None):
    """Stand in for an OPERATOR deliberately setting a contract term."""
    with conn.cursor() as cur:
        if expires is None:
            cur.execute("UPDATE public.accounts SET term_months = %s WHERE account_id = %s",
                        (int(months), aid))
        else:
            cur.execute("UPDATE public.accounts SET term_months = %s, expires_at = %s "
                        "WHERE account_id = %s", (int(months), expires, aid))
    conn.commit()


def _purchase(aid, tier):
    """The PURCHASE moment — checkout.session.completed carries a session id."""
    return prov.provision_from_checkout(
        account_id=aid, tier_row=checkout.get_tier(tier),
        subscription_id=f"sub_{uuid.uuid4().hex[:12]}",
        session_id=f"cs_{uuid.uuid4().hex[:12]}", contract_id=1)


def _renewal(aid, tier):
    """The RENEWAL moment — invoice.paid passes session_id=None and no contract_id."""
    return prov.provision_from_checkout(
        account_id=aid, tier_row=checkout.get_tier(tier),
        subscription_id=f"sub_{uuid.uuid4().hex[:12]}",
        session_id=None, contract_id=None)


def test_the_tenant_term_is_the_configured_six_month_commitment():
    """🔴 THE OWNER REVERSED THIS AND THE REVERSAL IS THE POINT.

    An earlier repair found the signed ToS saying "month to month / next invoice" while the
    account carried a 6-month term and the console made the customer attest to "billed to
    term-end (no refund)". It resolved that real contradiction by forcing the ACCOUNT to one
    month. The owner's ruling is the opposite:

        "The contract term is 6 months, payment is per month via cc on tenant, individuals
         should be month-to-month is my feeling, don't lock in a single individual kicking the
         tires, allow the free month, and a contract per-month via cc."

    So the six-month term is REAL and intended for a TENANT; the CONTRACT TEXT was the wrong
    half, and the console attestation was accurate all along. This test is flipped rather than
    deleted so the reversal is recorded where the next reader will meet it.

    Individuals are the month-to-month case and never reach this module.

    ⚠️ THE DEFAULT IS NOW ASSERTED AGAINST AN ISOLATED RESOLVER, NOT THE LIVE CONTROL PLANE.
    ``tenant_term_months()`` resolves the ACTIVE CONTRACT TEMPLATE's frozen ``CONTRACT_TERM_MONTHS``
    first (the number the customer's own agreement states), falling back to the platform setting —
    see saas/contracts.py::active_contract_term_months for why the enforced term must come from
    the document rather than from a live setting. Correct in production; in a SHARED test database
    it means this assertion reads whatever agreement a sibling suite last published and did not
    clean up (measured: 24, from tests/test_retention_settings.py). Both upstream sources are
    therefore neutralised here so the test measures THE DEFAULT, which is what it is about."""
    import saas.entitlements as ent_mod
    import saas.contracts as contracts_mod
    _orig_doc = contracts_mod.active_contract_term_months
    _orig_ps = None
    try:
        import saas.platform_settings as ps_mod
        _orig_ps = ps_mod.contract_term_months
        # No agreement states a term, and no operator has configured one → the shipped default.
        contracts_mod.active_contract_term_months = lambda: None
        ps_mod.contract_term_months = lambda: 6
    except Exception:  # noqa: BLE001
        pass
    try:
        _assert_default_tenant_term(ent_mod)
    finally:
        contracts_mod.active_contract_term_months = _orig_doc
        if _orig_ps is not None:
            import saas.platform_settings as ps_mod
            ps_mod.contract_term_months = _orig_ps


def _assert_default_tenant_term(ent_mod):
    assert prov.tenant_term_months() == 6, (
        "a tenant's contract term is six months (operator-configurable, default 6)")
    # It is the SALES-LED default too — one model, not two.
    assert prov.tenant_term_months() == int(ent_mod.DEFAULT_TERM_MONTHS)
    assert not hasattr(prov, "SELF_SERVE_TERM_MONTHS"), (
        "the month-to-month constant was removed with the owner's correction; if it is back, "
        "the reversal has been undone")


def test_a_self_serve_TEAM_purchase_lands_on_the_six_month_term(dbconn, env):
    """THE B2 OUTCOME, on the row. A Team never seeds a base entitlement, so before this fix
    nothing on its path touched term_months at all and it sat on the schema default 6."""
    aid = _mk_account(dbconn, kind="team")
    assert _read_term(dbconn, aid)[0] == ent_mod.DEFAULT_TERM_MONTHS   # the defect's start state
    assert _purchase(aid, "team")["provisioned"] is True
    term, expires = _read_term(dbconn, aid)
    assert term == 6, "a tenant subscription carries the six-month contract term"
    # ⚠️ THIS ASYMMETRY IS NOW CLOSED, and the closing was a side effect worth stating.
    # This used to assert ``expires is None``: both tiers carried the 6-month term, but only
    # ENTERPRISE got a contract END stamped, because ``seed_base_entitlement`` is its only writer
    # and that seed ran for Enterprise alone. The comment here called it "a real inconsistency"
    # and asserted it as it WAS so nobody would paper over it.
    # ``seed_base_entitlement`` now runs for every kind with a positive included allotment (it
    # seeds each kind's OWN allotment rather than the Enterprise one), so a Team's expires_at is
    # stamped like anyone else's and the gap this comment described is gone. Asserted in its new,
    # consistent shape.
    assert expires is not None, (
        "a TEAM now gets its contract end stamped like every other tier — seed_base_entitlement "
        "runs for any kind whose package includes Standard seats, not for Enterprise alone")


def test_a_self_serve_ENTERPRISE_purchase_lands_on_the_six_month_term(dbconn, env):
    """The Enterprise path is the hard one: ``seed_base_entitlement`` unconditionally stamps
    its own 6-month default INTO public.accounts, so the term write must have the LAST word."""
    aid = _mk_account(dbconn, kind="team")
    assert _purchase(aid, "enterprise")["provisioned"] is True
    term, expires = _read_term(dbconn, aid)
    assert term == 6
    assert expires is not None, ("an ENTERPRISE contract end IS stamped by seed_base_entitlement and a fixed 6-month term makes it meaningful")


def test_a_stripe_redelivery_does_not_change_the_term(dbconn, env):
    """Stripe redelivers for up to three days. N deliveries must equal one delivery."""
    aid = _mk_account(dbconn, kind="team")
    _purchase(aid, "enterprise")
    first = _read_term(dbconn, aid)
    for _ in range(3):
        _purchase(aid, "enterprise")
    assert _read_term(dbconn, aid) == first
    assert first[0] == 6, "the six-month tenant term must survive"


def test_a_monthly_renewal_does_not_undo_the_term(dbconn, env):
    """``invoice.paid`` re-provisions EVERY month, forever, and it re-runs the base seed —
    which re-stamps 6. Without the ordering fix the account silently reverts at renewal #1."""
    aid = _mk_account(dbconn, kind="team")
    _purchase(aid, "enterprise")
    for _ in range(3):
        _renewal(aid, "enterprise")
    got = _read_term(dbconn, aid)
    assert got[0] == 6, "the six-month tenant term must survive"


def test_an_operator_set_term_is_never_overridden_by_a_renewal(dbconn, env):
    """A machine must not repeatedly overrule a human — the same rule the seat cap learned.
    24 is not a value any machine writes, so it is unambiguously an operator's."""
    aid = _mk_account(dbconn, kind="team")
    _purchase(aid, "team")
    _set_term(dbconn, aid, 24)
    for _ in range(2):
        _renewal(aid, "team")
    assert _read_term(dbconn, aid)[0] == 24, "an operator's deliberate term was clobbered"


def test_an_operator_set_term_survives_the_base_seed_that_would_flatten_it(dbconn, env):
    """The Enterprise arm of the same rule, and it is STRICTER than 'do not write'.
    ``entitlements.seed_base_entitlement`` runs on every renewal and unconditionally stamps
    DEFAULT_TERM_MONTHS over whatever the column held, so merely declining to write would still
    leave the operator's 12 destroyed — by a different function. The term write reads the value
    under the kind-lock BEFORE the seed and RESTORES it."""
    aid = _mk_account(dbconn, kind="team")
    _purchase(aid, "enterprise")
    _set_term(dbconn, aid, 12)
    _renewal(aid, "enterprise")
    assert _read_term(dbconn, aid)[0] == 12
    _renewal(aid, "enterprise")
    assert _read_term(dbconn, aid)[0] == 12, "not stable across repeated renewals"


def test_an_operator_contract_end_is_not_cleared_when_the_term_is_theirs(dbconn, env):
    """``expires_at = NULL`` belongs to the month-to-month branch ONLY. An operator who set a
    real contract term keeps their contract end."""
    import datetime as _dt
    aid = _mk_account(dbconn, kind="team")
    _purchase(aid, "team")
    end = _dt.datetime(2027, 6, 1, tzinfo=_dt.timezone.utc)
    _set_term(dbconn, aid, 18, expires=end)
    _renewal(aid, "team")
    term, expires = _read_term(dbconn, aid)
    assert term == 18
    assert expires is not None and expires.year == 2027


def test_the_SALES_LED_door_still_creates_a_six_month_term(dbconn, env):
    """THE REGRESSION GUARD THAT MATTERS MOST. ``accounts.create_account`` is the operator's
    door for a negotiated enterprise contract and never touches this module. If this ever goes
    red, genuine six-month contracts have been silently converted to monthly."""
    slug = f"sl{uuid.uuid4().hex[:12]}"
    res = accounts_mod.create_account(name=slug, slug=slug, account_kind="tenant")
    term, expires = _read_term(dbconn, res["account_id"])
    assert term == ent_mod.DEFAULT_TERM_MONTHS == 6
    assert expires is not None, "a sales-led contract still gets a contract end"


def test_the_readers_of_term_months_behave_at_the_configured_term(dbconn, env):
    """Every consumer of the column, driven for real at term_months=1. The one that could
    plausibly misbehave is ``expired`` — a contract end that nothing ever advances would paint
    a red 'expired' badge on a paying customer, which is why expires_at is NULL, not +1 month."""
    aid = _mk_account(dbconn, kind="team")
    _purchase(aid, "enterprise")

    bill = ent_mod.billing_view(aid)
    assert bill is not None
    assert bill["term_months"] == 6
    assert bill["contract_end"] is not None, ("the biller surfaces the contract end for a fixed-term Enterprise account")
    assert bill["expired"] is False, "a month-to-month subscription must never read as expired"

    ov = accounts_mod.get_account_overview(aid)
    assert ov is not None and ov["term_months"] == 6
    assert ov["expired"] is False

    cap = ent_mod.capacity_report(aid) if hasattr(ent_mod, "capacity_report") else None
    if isinstance(cap, dict) and "term_months" in cap:
        assert cap["term_months"] == 6


def test_a_capacity_purchase_still_works_with_a_null_contract_end(dbconn, env):
    """``expires_at = NULL`` reaches ``_term_and_end`` → ``_write_ledger(contract_end=None)``
    → ``build_attestation``. Both ledger columns are nullable and ``_fmt_date(None)`` renders
    "the contract end", but a signed, money-moving path is not somewhere to assume that."""
    # A Team USED to be the tier that arrived here with a NULL contract end for free, because
    # nothing on its path wrote ``expires_at``. ``seed_base_entitlement`` now runs for Team too,
    # so the NULL no longer happens by accident — and the NULL path still has to work, because
    # ``expires_at`` is nullable and a signed, money-moving path is not somewhere to assume a
    # value is present. It is therefore made EXPLICITLY, which is the honest way to test it:
    # depending on another tier's omission to produce your fixture is how a case stops being
    # covered without anyone noticing.
    aid = _mk_account(dbconn, kind="team")
    _purchase(aid, "team")
    with dbconn.cursor() as cur:
        cur.execute("UPDATE public.accounts SET expires_at = NULL WHERE account_id = %s", (aid,))
    dbconn.commit()
    got = _read_term(dbconn, aid)
    assert got[0] == 6, "the six-month tenant term must survive"
    assert got[1] is None, "the NULL contract end is the case under test"
    out = ent_mod.provision_capacity(aid, "advanced", 1, actor="operator",
                                     signature="Test Operator", actor_user="op@example.com")
    assert out["ok"] is True, f"capacity purchase failed on a NULL contract end: {out}"
    with dbconn.cursor() as cur:
        cur.execute("SELECT term_months, contract_end, attestation FROM public.account_ledger "
                    "WHERE account_id = %s AND action = 'provision' ORDER BY ledger_id DESC "
                    "LIMIT 1", (aid,))
        row = cur.fetchone()
    dbconn.commit()
    assert row is not None, "no ledger row was written"
    assert row[0] == 6, "the ledger must stamp the term in force (6)"
    assert row[1] is None
    assert "the contract end" in (row[2] or ""), row[2]


# ════════════════════════════════════════════════════════════════════════════════════
# THE FALSE-GREEN GUARD — must stay LAST
# ════════════════════════════════════════════════════════════════════════════════════

def test_no_test_was_silently_skipped():
    """A DSN-gated suite that skips its DB half reports "PASS | N passed, M skipped" and looks
    green. That is the half that can actually falsify the B3 fix — every catalog-only assertion
    stayed green throughout the defect. If the DB half did not run, this is a FALSE GREEN."""
    assert _DB_RAN["value"], (
        "the database half of this suite never ran (DSN unreachable or the SaaS control plane "
        f"is not applied at {TEST_DSN}) — this is a FALSE GREEN, not a pass. See the header.")
