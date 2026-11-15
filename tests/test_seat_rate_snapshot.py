"""SaaS — the SEAT RATE SNAPSHOT (migration 249) + the operator-configurable rate card.

THE OWNER'S RULE, verbatim:
    "any changes to those amounts is ONLY for new sign-ups, historic must be maintained
     as it's accounting!"

THE DEFECT. ``public.account_entitlements`` carried no price, so ``invoicing.build_invoice``
-> ``entitlements.billing_view`` -> ``price_for()`` resolved the rate LIVE at every invoice
build. Editing a seat rate re-priced every existing customer on their next invoice.

WHAT IS PINNED HERE, and every one of these is an OUTCOME on a real row or a real rendered
invoice — never "the column exists" or "the setting saved":

  * 🔴 HEADLINE: buy a seat, CHANGE the rate, and the same account's next ``build_invoice``
    still charges the ORIGINAL rate — while a seat bought AFTER the change is charged the new
    one. Then revert the rate and confirm the old figure flips back, which proves the test has
    teeth (it is reading a live resolution, not a frozen fixture).
  * an UNSTAMPED (legacy) row falls back to the CONFIGURED rate and LOGS that it did.
  * a rate of 0 or negative is REFUSED by the setter.
  * the backfill is IDEMPOTENT and marks rows ``legacy_backfill``.
  * the PINNED INTERFACE: this file imports platform_settings AND its consumer (entitlements)
    and asserts each contract name exists and is callable — the check that would have caught
    the ``tenant_contract_term_months`` wiring bug that made an operator field decorative.

Run (never raw pytest — owner rule):
    PYTHONPATH="src:<venv site-packages>" \
    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_seatrate_test \
      python3 tools/fltest.py --bug SEATRATE --test tests/test_seat_rate_snapshot.py

FALSE-GREEN GUARD: ``_DB_RAN`` is set by the DB fixture and asserted by the LAST test, so a
run with no reachable throwaway DB comes back RED rather than a comfortable all-skipped green.
"""

import io
import os
import uuid
from contextlib import redirect_stderr

import pytest

from saas import accounts as accounts_mod
from saas import entitlements as ent_mod
from saas import invoicing as inv_mod
from saas import platform_settings as ps_mod

DEFAULT_TEST_DSN = ("postgresql://faultline:faultline@172.20.0.3:5432/"
                    "faultline_seatrate_test")
TEST_DSN = os.environ.get("SEATRATE_TEST_DSN",
                          os.environ.get("POSTGRES_DSN", DEFAULT_TEST_DSN))

_DB_RAN = {"value": False}

SIG = "Test Operator"


# ════════════════════════════════════════════════════════════════════════════════════
# PART A — THE PINNED INTERFACE (no DB). A name mismatch here has already cost a day.
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_three_contract_names_exist_and_are_callable():
    """🔴 THE WIRING PIN. A sibling console panel is built against these exact names.

    The precedent: ``tenant_provisioning`` looked up ``platform_settings.
    tenant_contract_term_months`` while the setting was ``contract_term_months``; a defensive
    ``getattr(..., None)`` swallowed the miss and the operator's field was decorative for
    hours. Neither side's own tests could see it — the producer tested the setting, the
    consumer tested the fallback. This test imports BOTH sides so a rename cannot pass."""
    for name in ("seat_rates", "seat_rate_cents", "set_seat_rates"):
        assert hasattr(ps_mod, name), f"platform_settings.{name} is the PINNED contract name"
        assert callable(getattr(ps_mod, name)), f"platform_settings.{name} must be callable"

    # The CONSUMER side: entitlements must have imported the symbol DIRECTLY (so a rename is an
    # ImportError at import time, not a plausible wrong price at invoice time).
    assert ent_mod.seat_rate_cents is ps_mod.seat_rate_cents, (
        "entitlements must bind platform_settings.seat_rate_cents by direct import — a "
        "getattr probe would turn a rename into a silently wrong rate")
    for name in ("entitlement_unit_price", "stamp_entitlement_price", "rate_card_kind"):
        assert callable(getattr(ent_mod, name)), f"entitlements.{name} must be callable"


def test_the_shipped_card_is_the_single_source_of_the_rate_numbers():
    """``entitlements._RATE_TABLE`` is DERIVED, not a second hand-maintained copy."""
    for kind, block in ps_mod._SHIPPED_SEAT_RATES_CENTS.items():
        for st, cents in block.items():
            assert ent_mod._RATE_TABLE[kind][st] == cents / 100.0
            assert isinstance(cents, int), f"{kind}.{st} must be integer CENTS, not a float"
    # The documented card, spelled out so a silent edit to the constants is visible here too.
    assert ps_mod._SHIPPED_SEAT_RATES_CENTS["tenant"] == {
        "standard": 1000, "advanced": 3000, "expert": 4500, "agent": 8000}
    assert ps_mod._SHIPPED_SEAT_RATES_CENTS["team"] == {
        "standard": 1250, "advanced": 3500, "expert": 5000, "agent": 10000}


def test_rate_card_kind_excludes_the_individual_house():
    """A self-serve individual is billed by plans.individual_price_for, never by this card.
    Stamping a tenant rate onto those rows would record a price no invoice reads."""
    assert ent_mod.rate_card_kind("tenant") == "tenant"
    assert ent_mod.rate_card_kind("clinical") == "tenant"     # human seats price at Enterprise
    assert ent_mod.rate_card_kind("team") == "team"
    assert ent_mod.rate_card_kind("individual_house") is None
    assert ent_mod.rate_card_kind(None) is None
    assert ent_mod.rate_card_kind("something_new") is None


def test_a_seat_type_off_the_card_is_never_given_a_card_price():
    """'patient' is priced by its own volume ladder. Inventing a rate-card number for it would
    attach a price nothing bills at."""
    assert ps_mod.seat_rate_cents("tenant", "patient") == 0
    assert ent_mod.price_for("patient", "tenant") == 0.0
    assert ent_mod.stamp_entitlement_price(None, "acct", "patient") is None


def test_seat_rates_resolution_is_per_leaf_and_never_falls_toward_zero(monkeypatch):
    """A junk leaf must not discard seven good ones, and must never resolve to 0."""
    monkeypatch.setattr(ps_mod, "state", lambda **k: {"seat_rates_cents": {
        "tenant": {"expert": 9900, "agent": 0, "advanced": "lots", "standard": -5},
        "team": "not-an-object",
    }})
    got = ps_mod.seat_rates()
    assert got["tenant"]["expert"] == 9900                       # the one trusted override
    assert got["tenant"]["agent"] == 8000                        # 0 REFUSED -> shipped
    assert got["tenant"]["advanced"] == 3000                     # junk -> shipped
    assert got["tenant"]["standard"] == 3000 or True             # negative -> shipped (below)
    assert got["tenant"]["standard"] == 1000
    assert got["team"] == ps_mod._SHIPPED_SEAT_RATES_CENTS["team"]   # bad block -> all shipped
    # Complete + positive for every cell: a caller may index without a guard.
    for kind in ps_mod.SEAT_RATE_KINDS:
        for st in ps_mod.SEAT_RATE_TYPES:
            assert isinstance(got[kind][st], int) and got[kind][st] > 0


def test_an_unreadable_control_plane_resolves_to_the_shipped_card_not_zero(monkeypatch):
    """FAIL TOWARD THE CONFIGURED RATE AND SCREAM. Never toward 0 (= free service)."""
    monkeypatch.setattr(ps_mod, "state", lambda **k: dict(ps_mod._FAILSAFE))
    assert ps_mod.seat_rates() == ps_mod.shipped_seat_rates()
    assert ps_mod.seat_rate_cents("tenant", "expert") == 4500


def test_shipped_seat_rates_is_returned_by_value():
    a = ps_mod.shipped_seat_rates()
    a["tenant"]["expert"] = 1
    assert ps_mod._SHIPPED_SEAT_RATES_CENTS["tenant"]["expert"] == 4500


# ════════════════════════════════════════════════════════════════════════════════════
# PART B — the resolver, as a pure decision (no DB)
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_stamp_wins_over_the_live_card():
    row = {"unit_price_cents": 3300, "unit_price_currency": "CAD", "price_source": "snapshot"}
    assert ent_mod.entitlement_unit_price(row, "expert", "tenant") == 33.0


def test_an_unstamped_row_falls_back_to_the_configured_rate_AND_LOGS():
    """The fallback is honest, not silent: an operator must be able to see which rows still
    price at the live card, because those are the ones a rate edit would move."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        got = ent_mod.entitlement_unit_price(
            {"unit_price_cents": None, "price_source": None}, "expert", "tenant",
            account_id="acct-legacy")
    assert got == ent_mod.price_for("expert", "tenant")
    out = buf.getvalue()
    assert "NO rate snapshot" in out and "acct-legacy" in out and "expert" in out


def test_the_quiet_fallback_is_available_and_still_reports_a_real_anomaly():
    """loud=False silences ONLY the documented normal case (a Team standard row that does not
    exist). A row that EXISTS but carries junk is reported regardless."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        ent_mod.entitlement_unit_price(None, "standard", "team", account_id="t", loud=False)
    assert buf.getvalue() == ""
    buf = io.StringIO()
    with redirect_stderr(buf):
        ent_mod.entitlement_unit_price({"unit_price_cents": "junk"}, "standard", "team",
                                       account_id="t", loud=False)
    assert "unusable" in buf.getvalue()


# ════════════════════════════════════════════════════════════════════════════════════
# PART C — the SETTER refuses what would silently give service away
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad,needle", [
    ({"tenant": {"expert": 0}}, "free service"),
    ({"tenant": {"expert": -100}}, "free service"),
    ({"tenant": {"expert": 45.0}}, "integer number of CENTS"),
    ({"tenant": {"expert": True}}, "integer number of CENTS"),
    ({"tenant": {"expert": 10_000_000}}, "outside the permitted range"),
    ({"enterprise": {"expert": 4500}}, "unknown account kind"),
    ({"tenant": {"wizard": 4500}}, "unknown seat type"),
    ({"tenant": 4500}, "must be an object"),
    ({}, "no rates supplied"),
    (None, "no rates supplied"),
])
def test_the_setter_refuses_bad_rates_before_any_write(bad, needle, monkeypatch):
    """Every refusal is BEFORE the DB call, so a bad payload writes nothing. The 0 case is the
    important one: 0 is free service applied silently to every future purchase."""
    def _explode(*a, **k):
        raise AssertionError("the setter must refuse before touching the database")
    monkeypatch.setattr(ps_mod.db, "connection", _explode)
    out = ps_mod.set_seat_rates(bad, actor="operator", actor_user="op@test")
    assert out["ok"] is False
    assert needle in out["reason"], f"{out['reason']!r} should mention {needle!r}"


def test_an_unknown_key_is_refused_rather_than_silently_dropped(monkeypatch):
    """A typo'd key that is quietly ignored is the "operator saves it, watches it persist,
    nothing reads it" trap — the exact failure this whole interface is defended against."""
    monkeypatch.setattr(ps_mod.db, "connection",
                        lambda *a, **k: pytest.fail("must not reach the DB"))
    out = ps_mod.set_seat_rates({"tenant": {"expertt": 4500}},
                                actor="operator", actor_user="op@test")
    assert out["ok"] is False and "unknown seat type" in out["reason"]


# ════════════════════════════════════════════════════════════════════════════════════
# PART D — the OUTCOME, against a REAL control-plane database
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
        pytest.skip(f"seat-rate test DB unreachable ({type(exc).__name__}): {TEST_DSN}")
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.accounts'), "
                    "       to_regclass('public.account_entitlements'), "
                    "       to_regclass('public.platform_settings')")
        present = cur.fetchone()
        cur.execute("SELECT count(*) FROM pg_attribute "
                    "WHERE attrelid = to_regclass('public.account_entitlements') "
                    "AND attname IN ('unit_price_cents','unit_price_currency',"
                    "'price_source','price_set_at') AND NOT attisdropped")
        n_cols = cur.fetchone()[0]
    conn.commit()
    if not all(present) or n_cols != 4:
        conn.close()
        pytest.skip("throwaway DB is missing the SaaS control plane or migration 249 — "
                    "see this file's header")
    yield conn
    conn.close()


@pytest.fixture
def env(monkeypatch, dbconn):
    _DB_RAN["value"] = True
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    monkeypatch.setenv("SAAS_MODE", "1")
    # The env rate override is FIRST in price_for's resolution order by design; a stray one in
    # the ambient environment would mask the whole configured-card lane under test.
    for kind in ("TENANT", "TEAM"):
        for st in ("STANDARD", "ADVANCED", "EXPERT", "AGENT"):
            monkeypatch.delenv(f"SAAS_PRICE_{kind}_{st}", raising=False)
            monkeypatch.delenv(f"SAAS_PRICE_{st}", raising=False)
    ps_mod.invalidate()
    yield True
    ps_mod.invalidate()


@pytest.fixture
def clean_card(dbconn):
    """Leave the singleton's rate card exactly as it was found — this row is shared with every
    other suite that reads platform settings."""
    with dbconn.cursor() as cur:
        cur.execute("SELECT seat_rates_cents FROM public.platform_settings WHERE singleton")
        before = cur.fetchone()[0]
    dbconn.commit()
    yield
    with dbconn.cursor() as cur:
        import json
        cur.execute("UPDATE public.platform_settings SET seat_rates_cents = %s::jsonb "
                    "WHERE singleton", (json.dumps(before) if before is not None else None,))
    dbconn.commit()
    ps_mod.invalidate()


def _mk_account(conn, *, kind="tenant", name=None):
    slug = f"sr{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.accounts (name, slug, plan, account_type, account_kind, "
            "seat_limit, status) VALUES (%s, %s, 'standard', 'standard', %s, %s, 'active') "
            "RETURNING account_id::text",
            (name or slug, slug, kind, int(accounts_mod.hard_seat_cap(kind))),
        )
        aid = cur.fetchone()[0]
    conn.commit()
    return aid


def _ent_rows(conn, aid):
    with conn.cursor() as cur:
        cur.execute("SELECT seat_type, quantity, unit_price_cents, unit_price_currency, "
                    "price_source FROM public.account_entitlements WHERE account_id = %s "
                    "ORDER BY seat_type", (aid,))
        rows = cur.fetchall()
    conn.commit()
    return {r[0]: {"quantity": r[1], "cents": r[2], "currency": r[3], "source": r[4]}
            for r in rows}


def _invoice_line(aid, needle):
    """The REAL rendered invoice line — what the customer actually reads."""
    model = inv_mod.build_invoice(aid)
    assert model is not None, "build_invoice returned None (control-plane error)"
    for ln in model["line_items"]:
        if needle.lower() in str(ln["description"]).lower():
            return ln
    raise AssertionError(f"no invoice line matching {needle!r} in "
                         f"{[l['description'] for l in model['line_items']]}")


def _set_rate(kind, seat_type, cents):
    out = ps_mod.set_seat_rates({kind: {seat_type: cents}},
                                actor="operator", actor_user="op@test")
    assert out["ok"] is True, out
    ps_mod.invalidate()
    return out


# ── 🔴 THE HEADLINE ──────────────────────────────────────────────────────────────────

def test_HEADLINE_a_rate_change_never_reprices_an_existing_customer(env, dbconn, clean_card):
    """🔴 THE OWNER'S RULE, END TO END, ON REAL RENDERED INVOICES.

    Old customer buys Expert at CAD 45. Operator raises the card to CAD 99. The OLD customer's
    next invoice still says 45; a NEW customer's says 99. Then the rate is put back and the new
    customer's invoice flips to 45 — which is what proves this test has teeth rather than
    reading a frozen fixture.
    """
    _set_rate("tenant", "expert", 4500)                     # the card in force at purchase

    old = _mk_account(dbconn, kind="tenant")
    ent_mod.seed_base_entitlement(old)
    r = ent_mod.provision_capacity(old, "expert", 2, signature=SIG, actor_user="op@test")
    assert r["ok"] is True, r

    rows = _ent_rows(dbconn, old)
    assert rows["expert"]["cents"] == 4500
    assert rows["expert"]["source"] == "snapshot"
    assert rows["expert"]["currency"] == "CAD"

    before = _invoice_line(old, "Expert add-on")
    assert before["unit_price"] == 45.0 and before["amount"] == 90.0

    # ── the operator raises the rate ────────────────────────────────────────────────
    _set_rate("tenant", "expert", 9900)
    assert ps_mod.seat_rate_cents("tenant", "expert") == 9900
    assert ent_mod.price_for("expert", "tenant") == 99.0     # the CARD moved…

    after = _invoice_line(old, "Expert add-on")
    assert after["unit_price"] == 45.0, (
        "THE ACCOUNTING VIOLATION: an existing customer was re-priced by a rate-card edit")
    assert after["amount"] == 90.0

    # ── a seat purchased AFTER the change pays the NEW rate ─────────────────────────
    new = _mk_account(dbconn, kind="tenant")
    ent_mod.seed_base_entitlement(new)
    assert ent_mod.provision_capacity(new, "expert", 1, signature=SIG,
                                      actor_user="op@test")["ok"] is True
    assert _ent_rows(dbconn, new)["expert"]["cents"] == 9900
    fresh = _invoice_line(new, "Expert add-on")
    assert fresh["unit_price"] == 99.0 and fresh["amount"] == 99.0

    # A NEW seat TYPE on the OLD account also takes today's card — "new purchases use the
    # current rate" holds wherever it is unambiguous.
    assert ent_mod.provision_capacity(old, "agent", 1, signature=SIG,
                                      actor_user="op@test")["ok"] is True
    assert _invoice_line(old, "Agent add-on")["unit_price"] == \
        ps_mod.seat_rate_cents("tenant", "agent") / 100.0

    # ── TEETH: put the rate back and the NEW account's invoice must move ────────────
    _set_rate("tenant", "expert", 4500)
    assert _invoice_line(old, "Expert add-on")["unit_price"] == 45.0    # still its own rate
    assert ent_mod.price_for("expert", "tenant") == 45.0
    reverted = _mk_account(dbconn, kind="tenant")
    ent_mod.seed_base_entitlement(reverted)
    ent_mod.provision_capacity(reverted, "expert", 1, signature=SIG, actor_user="op@test")
    assert _invoice_line(reverted, "Expert add-on")["unit_price"] == 45.0, (
        "the resolution is not live — this test would pass against a frozen fixture")


def test_a_team_account_snapshots_at_the_team_column(env, dbconn, clean_card):
    """The card is per-tier. A Team must stamp the Team rate, not the Enterprise one."""
    _set_rate("team", "advanced", 3500)
    aid = _mk_account(dbconn, kind="team")
    assert ent_mod.provision_capacity(aid, "advanced", 2, signature=SIG,
                                      actor_user="op@test")["ok"] is True
    assert _ent_rows(dbconn, aid)["advanced"]["cents"] == 3500
    assert _invoice_line(aid, "Advanced")["unit_price"] == 35.0
    _set_rate("team", "advanced", 6000)
    assert _invoice_line(aid, "Advanced")["unit_price"] == 35.0


def test_an_unstamped_legacy_row_bills_at_the_configured_rate_and_says_so(env, dbconn,
                                                                          clean_card):
    """A row written by a path that does not stamp yet must still BILL — at the configured
    rate, never 0 — and must be VISIBLE, so an operator can find it and fix the writer."""
    _set_rate("tenant", "expert", 4500)
    aid = _mk_account(dbconn, kind="tenant")
    ent_mod.seed_base_entitlement(aid)
    ent_mod.provision_capacity(aid, "expert", 2, signature=SIG, actor_user="op@test")
    # Simulate a legacy / non-stamping writer.
    with dbconn.cursor() as cur:
        cur.execute("UPDATE public.account_entitlements SET unit_price_cents = NULL, "
                    "unit_price_currency = NULL, price_source = NULL, price_set_at = NULL "
                    "WHERE account_id = %s AND seat_type = 'expert'", (aid,))
    dbconn.commit()

    _set_rate("tenant", "expert", 7700)
    buf = io.StringIO()
    with redirect_stderr(buf):
        line = _invoice_line(aid, "Expert add-on")
    assert line["unit_price"] == 77.0, "an unstamped row must bill at the CONFIGURED rate"
    assert line["amount"] == 154.0
    assert line["unit_price"] != 0.0, "🔴 a rate must NEVER fail toward 0 (= free service)"
    out = buf.getvalue()
    assert "NO rate snapshot" in out and aid in out, \
        f"the fallback must be LOUD so the unstamped row is findable; stderr was: {out!r}"


def test_a_top_up_keeps_the_rate_the_account_bought_at_and_logs(env, dbconn, clean_card):
    """STAMP-ONCE. One row, one price column, one invoice unit_price — the only safe choice is
    the customer's recorded rate. The divergence is logged, never silent."""
    _set_rate("tenant", "advanced", 3000)
    aid = _mk_account(dbconn, kind="tenant")
    ent_mod.seed_base_entitlement(aid)
    ent_mod.provision_capacity(aid, "advanced", 1, signature=SIG, actor_user="op@test")
    _set_rate("tenant", "advanced", 8800)

    buf = io.StringIO()
    with redirect_stderr(buf):
        r = ent_mod.provision_capacity(aid, "advanced", 2, signature=SIG, actor_user="op@test")
    assert r["ok"] is True
    assert "rate-snapshot top-up" in buf.getvalue()
    rows = _ent_rows(dbconn, aid)
    assert rows["advanced"]["quantity"] == 3
    assert rows["advanced"]["cents"] == 3000, "a top-up must not re-price the seats they own"
    line = _invoice_line(aid, "Advanced add-on")
    assert line["unit_price"] == 30.0 and line["amount"] == 90.0
    # The ledger line records what is ACTUALLY billed, not the card the customer will not pay.
    assert r["unit_price"] == 30.0 and r["amount"] == 60.0


def test_the_base_seed_stamps_and_a_reprovision_cannot_move_it(env, dbconn, clean_card):
    """``invoice.paid`` re-runs seed_base_entitlement every month forever. It must be unable to
    re-price a customer, no matter how many times it fires."""
    _set_rate("tenant", "standard", 1000)
    aid = _mk_account(dbconn, kind="tenant")
    assert ent_mod.seed_base_entitlement(aid) is True
    assert _ent_rows(dbconn, aid)["standard"]["cents"] == 1000
    _set_rate("tenant", "standard", 2500)
    for _ in range(3):                                   # three "renewals"
        assert ent_mod.seed_base_entitlement(aid) is True
    assert _ent_rows(dbconn, aid)["standard"]["cents"] == 1000
    # And Standard seats beyond the included 5 bill at the rate they signed up on.
    #
    # ⚠️ WHY 6 AND NOT 2 — A PRE-EXISTING BUG, NOT A CHOICE OF CONVENIENCE. For the base seat
    # type ``provision_capacity`` issues ``INSERT ... VALUES (aid,'standard',n,5) ON CONFLICT
    # DO UPDATE``, and PostgreSQL evaluates CHECK constraints on the tuple PROPOSED for
    # insertion, before conflict arbitration. So ``quantity >= base_quantity`` fails for any
    # n < 5 and the call returns a 500 "control-plane error" — on the CURRENT code, with or
    # without migration 249 (reproduced against HEAD's own SQL). Reported, deliberately NOT
    # fixed here: it changes what a Standard top-up costs, which is a billing decision and a
    # different owner's call.
    r6 = ent_mod.provision_capacity(aid, "standard", 6, signature=SIG, actor_user="op@test")
    assert r6["ok"] is True, r6
    assert _invoice_line(aid, "Standard add-on")["unit_price"] == 10.0


def test_an_individual_house_row_is_never_stamped(env, dbconn, clean_card):
    """Individuals are billed by plans.individual_price_for. A tenant-rate stamp on those rows
    would record a price nothing reads and actively mislead whoever reads the row later."""
    aid = _mk_account(dbconn, kind="individual_house")
    ent_mod.provision_capacity(aid, "expert", 1, signature=SIG, actor_user="op@test")
    assert _ent_rows(dbconn, aid)["expert"]["cents"] is None
    assert _ent_rows(dbconn, aid)["expert"]["source"] is None


def test_capacity_report_shows_what_they_pay_and_what_a_new_seat_costs(env, dbconn,
                                                                       clean_card):
    """Two distinct numbers. Collapsing them is how a customer is shown a price they are not
    being charged."""
    _set_rate("tenant", "expert", 4500)
    aid = _mk_account(dbconn, kind="tenant")
    ent_mod.seed_base_entitlement(aid)
    ent_mod.provision_capacity(aid, "expert", 1, signature=SIG, actor_user="op@test")
    _set_rate("tenant", "expert", 9900)
    rep = ent_mod.capacity_report(aid)
    row = [t for t in rep["types"] if t["seat_type"] == "expert"][0]
    assert row["unit_price"] == 45.0, "the Plan tab must show what the invoice charges"
    assert row["card_unit_price"] == 99.0, "…and what another seat would cost today"


def test_a_removal_credits_the_rate_they_paid_not_todays_card(env, dbconn, clean_card):
    """A credit at the live rate would refund money the customer was never charged — the same
    violation as re-pricing, in the other direction."""
    _set_rate("tenant", "expert", 4500)
    aid = _mk_account(dbconn, kind="tenant")
    ent_mod.seed_base_entitlement(aid)
    ent_mod.provision_capacity(aid, "expert", 2, signature=SIG, actor_user="op@test")
    _set_rate("tenant", "expert", 9900)
    assert ent_mod.remove_capacity(aid, "expert", 1, signature=SIG,
                                   actor_user="op@test")["ok"] is True
    with dbconn.cursor() as cur:
        cur.execute("SELECT unit_price, amount FROM public.account_ledger "
                    "WHERE account_id = %s AND action = 'removal'", (aid,))
        unit, amount = cur.fetchone()
    dbconn.commit()
    assert float(unit) == 45.0 and float(amount) == -45.0


def test_the_setter_persists_a_complete_card_and_never_touches_an_entitlement(env, dbconn,
                                                                              clean_card):
    """A partial edit is legal; what is STORED is the full resolved card, and not one existing
    entitlement row moves."""
    _set_rate("tenant", "expert", 4500)
    aid = _mk_account(dbconn, kind="tenant")
    ent_mod.seed_base_entitlement(aid)
    ent_mod.provision_capacity(aid, "expert", 1, signature=SIG, actor_user="op@test")
    before = _ent_rows(dbconn, aid)

    out = ps_mod.set_seat_rates({"tenant": {"expert": 5555}},
                                actor="operator", actor_user="op@test")
    assert out["ok"] is True and out["reason"] is None
    with dbconn.cursor() as cur:
        cur.execute("SELECT seat_rates_cents FROM public.platform_settings WHERE singleton")
        stored = cur.fetchone()[0]
    dbconn.commit()
    for kind in ps_mod.SEAT_RATE_KINDS:                  # complete, self-describing row
        for st in ps_mod.SEAT_RATE_TYPES:
            assert isinstance(stored[kind][st], int) and stored[kind][st] > 0
    assert stored["tenant"]["expert"] == 5555
    assert _ent_rows(dbconn, aid) == before, \
        "🔴 a rate-card edit must not touch a single existing entitlement row"


# ── the BACKFILL ─────────────────────────────────────────────────────────────────────

def _apply_backfill(conn):
    """Re-run migration 249's backfill statement verbatim from the migration file, so this
    test cannot drift from what actually ships."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sql = open(os.path.join(here, "migrations",
                            "249_seat_rate_snapshot.sql")).read()
    start = sql.index("WITH card(kind, seat_type, cents) AS (")
    end = sql.index("individual_house';", start) + len("individual_house';")
    with conn.cursor() as cur:
        cur.execute(sql[start:end])
        n = cur.rowcount
    conn.commit()
    return n


def test_the_backfill_is_idempotent_and_marks_rows_legacy_backfill(env, dbconn, clean_card):
    """The backfill is an ASSUMPTION (today's card applied to rows with no purchase record).
    ``legacy_backfill`` is what lets a future reader tell it apart from real evidence — and a
    second run must be a no-op, not a re-stamp."""
    aid = _mk_account(dbconn, kind="tenant")
    team = _mk_account(dbconn, kind="team")
    solo = _mk_account(dbconn, kind="individual_house")
    with dbconn.cursor() as cur:
        for a, st in ((aid, "expert"), (team, "expert"), (solo, "expert"),
                      (aid, "patient")):
            cur.execute("INSERT INTO public.account_entitlements "
                        "(account_id, seat_type, quantity) VALUES (%s, %s, 3)", (a, st))
    dbconn.commit()

    first = _apply_backfill(dbconn)
    assert first >= 2
    rows = _ent_rows(dbconn, aid)
    assert rows["expert"]["cents"] == 4500 and rows["expert"]["source"] == "legacy_backfill"
    assert rows["expert"]["currency"] == "CAD"
    assert _ent_rows(dbconn, team)["expert"]["cents"] == 5000        # the TEAM column
    assert _ent_rows(dbconn, solo)["expert"]["cents"] is None        # individuals excluded
    assert rows["patient"]["cents"] is None                          # own volume ladder

    snapshot = _ent_rows(dbconn, aid)
    second = _apply_backfill(dbconn)                                 # IDEMPOTENT
    assert second == 0
    assert _ent_rows(dbconn, aid) == snapshot


def test_a_real_snapshot_survives_the_backfill(env, dbconn, clean_card):
    """The backfill must never overwrite EVIDENCE with an assumption."""
    _set_rate("tenant", "expert", 4500)
    aid = _mk_account(dbconn, kind="tenant")
    ent_mod.provision_capacity(aid, "expert", 1, signature=SIG, actor_user="op@test")
    _set_rate("tenant", "expert", 9900)
    _apply_backfill(dbconn)
    row = _ent_rows(dbconn, aid)["expert"]
    assert row["cents"] == 4500 and row["source"] == "snapshot"


def test_the_column_carries_no_default_so_an_unstamped_row_is_never_free(env, dbconn):
    """🔴 ``DEFAULT 0`` would make every un-stamped row look like a CONFIRMED free seat. NULL
    is the only value that means "no snapshot" and routes to the loud fallback."""
    with dbconn.cursor() as cur:
        cur.execute("SELECT column_default, is_nullable FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='account_entitlements' "
                    "AND column_name='unit_price_cents'")
        default, nullable = cur.fetchone()
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = to_regclass('public.account_entitlements') "
                    "AND conname = 'account_entitlements_price_source_check'")
        check = cur.fetchone()
    dbconn.commit()
    assert default is None and nullable == "YES"
    assert check is not None and "snapshot" in check[0] and "legacy_backfill" in check[0]


def test_zzz_no_test_was_silently_skipped():
    """FALSE-GREEN GUARD — see the module docstring."""
    assert _DB_RAN["value"] is True, (
        "the DB fixture never ran: every outcome test in PART D was skipped. Point "
        "POSTGRES_DSN (or SEATRATE_TEST_DSN) at a throwaway control-plane DB with "
        "migration 249 applied — a green run with no DB proves nothing.")
