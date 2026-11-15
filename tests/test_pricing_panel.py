"""THE OPERATOR PRICING PANEL — base prices proven against the processor, seat rates gated.

WHERE IT LIVES (owner, 2026-08-12): *"the specific 'stripe' settings belong in 'PAYMENT
SETTINGS', and if stripe is selected it should have a top-menu for each aspect. I'd suggest the
first one is the pricing (how much we charge), followed by payment configurations, and lastly
under a 'CONFIG' menu at the top the abiliy to change/modify the payment provider."*

The panel first shipped on the PLATFORM view beside RETENTION and CONTRACT. That was the wrong
home — the owner went looking for a price under Payment settings and under Stripe billing and
found neither. Payment settings is now three tabs, ordered MOST-USED FIRST / MOST-DESTRUCTIVE
LAST: Pricing (default) · Payment configuration · Config. Changing the payment PROVIDER is the
rarest and most dangerous act on the page, so it is buried under Config rather than sitting
beside the currency box where it can be fat-fingered while checking a price. RETENTION and
CONTRACT did not move — they are not payment-provider settings.

Section 3 pins that placement in BOTH DIRECTIONS (absent from platform, present in payments),
because a one-directional assertion passes for a copy-paste that leaves the panel in two views
— and two seat-rate grids sharing one LOCKED_GROUPS entry is worse than the original misfiling.

WHAT WAS MISSING AND WHY IT MATTERS. The global admin console had no pricing surface at all. Six
live Stripe Prices (two tenant tiers + four individual retail plans) governed what every customer's
card is actually charged, and nothing in the product compared them to the figures our catalog, our
pricing cards and — critically — our APPEND-ONLY signed contracts state. A price edited in the
Stripe dashboard diverged silently, and the first symptom would have been a card charged an amount
the customer's permanent agreement contradicts.

THE OWNER'S DESIGN, AND THE TWO HALVES THIS FILE PINS SEPARATELY:

  HALF A — BASE TIER PRICES ARE READ-ONLY. *"Panel DISPLAYS the live Stripe price and flags any
  drift from our catalog. Changing a price is done in Stripe; the panel proves the two agree."*
  Stripe collects those, so Stripe is the system of record and there is deliberately NO write.

  HALF B — SEAT RATES ARE EDITABLE. Stripe never sees them (it collects a flat base with
  ``quantity: 1``); saas/invoicing.py assembles the per-seat money itself. Nothing to verify
  against and nowhere else to change them — so they are editable behind the SAME password
  re-authorization as the contractual figures.

WHAT IS PINNED HERE, as OUTCOMES rather than shapes:
  * a seat-rate write with NO password is REFUSED and the STORED RATES ARE UNCHANGED (re-read
    from the module afterwards — a 401 that still wrote would pass a status-code-only test);
  * with a valid password it SUCCEEDS and is AUDITED;
  * an unresolvable amount renders ``None`` → "unknown", NEVER 0 and never $0.00;
  * DRIFT between catalog and Stripe is REPORTED as drift, never silently reconciled;
  * a LIVE-CREDENTIAL build reports the REFUSAL REASON, not a bare "not configured";
  * 🔴 the INTERFACE GUARD: ``saas.platform_settings`` really exports ``seat_rates`` /
    ``seat_rate_cents`` / ``set_seat_rates`` and all three are callable. This exists because a
    consumer once looked up ``tenant_contract_term_months`` while the setting was
    ``contract_term_months``, a defensive ``getattr(..., None)`` swallowed the miss, and an
    operator field was decorative for hours. Neither side's own tests could see it; only a test
    that imports BOTH can.

⚠️ FALSE-GREEN GUARD. Every DB-backed test here would SKIP silently without a control plane, and
"PASS | 0 passed, N skipped" has lied in this repo before. ``_DB_RAN`` is set by the DB fixture and
``test_no_test_was_silently_skipped`` (LAST) turns a fully-skipped run RED.

⚠️ NO LIVE STRIPE CALL IS MADE. The verification path is driven with a STUB provider whose
``retrieve_price`` returns canned dicts, so match / drift / unresolvable / archived / one-time are
all exercised deterministically and offline.

Run (never raw pytest — owner rule):
  PYTHONPATH="src:.venv/lib64/python3.14/site-packages" \\
  POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_pricepanel_test \\
  python3 tools/fltest.py --bug PRICEPANEL --test tests/test_pricing_panel.py
"""

import os
import uuid

import pytest

# Control-plane singletons (the append-only ACTIVE contract chain, the platform settings row,
# the global operator realm) are shared by every suite in one database: the suites that
# write or depend on them run on one xdist worker (under --dist loadgroup, see tests/conftest.py).
pytestmark = pytest.mark.xdist_group("control_plane")
from fastapi.testclient import TestClient

import src.mcp.http_server as h
import saas.admin as admin
from saas import platform_settings as saas_platform
from saas import plans as saas_plans
from saas import tenant_checkout as saas_checkout

OP_KEY = "pricing-panel-op-super-secret-key"
OP_PASSWORD = "pricing-panel-operator-password"

# Set by the DB fixture. The final test asserts it — see the module docstring.
_DB_RAN = {"value": False}


def _dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or "").strip()


requires_db = pytest.mark.skipif(not _dsn(), reason="POSTGRES_DSN not set — control plane needed")


@pytest.fixture(autouse=True)
def _own_operator_key(monkeypatch):
    """Bind THIS module's operator key + session secret for ONE test, then restore.

    saas/admin.py reads SAAS_OPERATOR_KEY LIVE per request, so a function-scoped monkeypatch is
    the right lifetime and collection order can never decide this file's result."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "pricing-panel-session-secret-0123456789abcdef")


@pytest.fixture(autouse=True)
def _clear_cache():
    """platform_settings caches process-wide — reset around every test so state never leaks."""
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    yield
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


@pytest.fixture
def client():
    return TestClient(h.app, follow_redirects=False)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


_OPERATOR = {}


def _operator_session():
    """A real operator login + REAL argon2id hash + REAL signed session, once per process.

    Deliberately not a monkeypatch of ``reauthenticate_operator``: the thing under test is the
    gate, and a mocked gate proves nothing about whether the gate is wired in."""
    import psycopg2
    from saas import dashboard_auth as _A
    if _OPERATOR and _A.operator_principal_active(_OPERATOR["id"]):
        return _OPERATOR
    # (Re)create the login when the cached one is gone: test_operator_session_revocation_and_
    # perm_grant DELETEs every operator row in teardown, and the session gate re-validates the
    # principal against the row on every request, so a login cached "once per process" from
    # before that ran answered 403 "operator credential required" (x6 in the whole-suite floor,
    # green alone - #44, order dependence).
    _OPERATOR.clear()
    username = f"pricing-op-{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.dashboard_users (account_id, username, password_hash, role, "
                "status) VALUES (NULL, %s, %s, 'operator', 'active') RETURNING id::text",
                (username, _A.hash_password(OP_PASSWORD)),
            )
            op_id = cur.fetchone()[0]
    finally:
        conn.close()
    _OPERATOR.update({
        "id": op_id,
        "username": username,
        "session": _A.issue_operator_session(dashboard_user_id=op_id, username=username),
        "csrf": _A.issue_operator_csrf(dashboard_user_id=op_id),
        "cookie": _A.OPERATOR_SESSION_COOKIE_NAME,
    })
    return _OPERATOR


def _put_rates(client, rates, password=OP_PASSWORD):
    """PUT /admin/pricing/seat-rates through the SESSION door. ``password=None`` omits it."""
    op = _operator_session()
    body = {"rates": rates}
    if password is not None:
        body["confirm_password"] = password
    client.cookies.set(op["cookie"], op["session"])
    return client.put("/admin/pricing/seat-rates", json=body,
                      headers={"x-csrf-token": op["csrf"]})


@pytest.fixture
def db(monkeypatch):
    """A real control-plane connection; the seat-rate card is cleared after every test."""
    dsn = _dsn()
    if not dsn:
        pytest.skip("POSTGRES_DSN not set")
    import psycopg2
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    _DB_RAN["value"] = True
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    yield conn
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.platform_settings SET seat_rates_cents = NULL "
                        "WHERE singleton = TRUE")
    finally:
        conn.close()
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


def _stored_card(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT seat_rates_cents FROM public.platform_settings WHERE singleton = TRUE")
        row = cur.fetchone()
    return row[0] if row else None


# ══════════════════════════════════════════════════════════════════════════════════
# 0. 🔴 THE INTERFACE GUARD — the one test neither side's own suite could have had
# ══════════════════════════════════════════════════════════════════════════════════

def test_platform_settings_exports_the_pinned_seat_rate_interface():
    """``seat_rates`` / ``seat_rate_cents`` / ``set_seat_rates`` exist and are callable.

    🔴 THIS GUARDS THE NAME-MISMATCH CLASS, which has already cost a day on this branch. The
    producer's tests exercised the setting; the consumer's tests exercised its fallback; a
    ``getattr(mod, name, default)`` between them meant neither could see that the name was wrong
    and the operator's field was decorative. The only test that can catch it is one that imports
    BOTH sides and asserts the attribute is really there — no getattr, no default."""
    for name in ("seat_rates", "seat_rate_cents", "set_seat_rates"):
        assert hasattr(saas_platform, name), (
            f"saas.platform_settings.{name} is MISSING — saas/admin.py's pricing routes are "
            f"built against this name and will fail loudly rather than silently defaulting")
        assert callable(getattr(saas_platform, name)), f"{name} exists but is not callable"
    # And the axes the console grid is drawn from, read by name for the same reason.
    assert tuple(saas_platform.SEAT_RATE_KINDS) == ("tenant", "team")
    assert set(saas_platform.SEAT_RATE_TYPES) == {"standard", "advanced", "expert", "agent"}


def test_admin_reads_the_axes_from_platform_settings_not_a_local_copy():
    """The grid's kinds/types come from the module that VALIDATES the write, not a second copy.

    If admin.py kept its own tuple, the panel could offer a cell ``set_seat_rates`` rejects (or
    hide one it accepts) and the two would drift silently."""
    shape = admin._seat_rate_catalog_shape()
    assert [k["kind"] for k in shape["kinds"]] == list(saas_platform.SEAT_RATE_KINDS)
    assert shape["seat_types"] == list(saas_platform.SEAT_RATE_TYPES)
    # Labels are the shared kind→label map: 'tenant' is buyer-facing "Enterprise".
    assert dict((k["kind"], k["label"]) for k in shape["kinds"])["tenant"] == "Enterprise"
    assert shape["min_cents"] == saas_platform._MIN_SEAT_RATE_CENTS


# ══════════════════════════════════════════════════════════════════════════════════
# 1. HALF A — the read-only base-tier payload
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_pricing_payload_carries_both_products_and_no_zero_amounts(client, db):
    """Both the tenant tiers AND the individual retail plans, each with a real amount or None."""
    r = client.get("/admin/pricing", headers=_op())
    assert r.status_code == 200, r.text
    d = r.json()
    kinds = {row["kind"] for row in d["base_tiers"]}
    assert kinds == {"tenant", "individual"}, (
        "the panel must price BOTH products — the four individual retail plans have their own "
        "Stripe price ids and were outside every check before this")
    tiers = {row["tier"] for row in d["base_tiers"]}
    assert {"team", "enterprise"} <= tiers
    assert set(saas_plans.STRIPE_PRICE_IDS) <= tiers
    for row in d["base_tiers"]:
        # UNKNOWN is a legal answer; ZERO is not. A defaulted 0 is a confident wrong answer
        # about money and is exactly the CA$0.00 defect this codebase already shipped once.
        assert row["amount_cents"] is None or row["amount_cents"] > 0, (
            f"{row['tier']}: an amount of 0 must be reported as None/unknown, never as zero")


@requires_db
def test_pricing_route_is_operator_gated(client, db):
    """No operator credential, no pricing payload — it names live Stripe price ids."""
    assert client.get("/admin/pricing").status_code in (401, 403)


def test_unresolvable_individual_price_renders_unknown_not_zero(monkeypatch):
    """``individual_price_for`` returns 0 for a tier it cannot resolve. 0 must become UNKNOWN.

    THE TRAP: 0 is a perfectly ordinary float and flows through arithmetic without complaint, so
    a tier whose price could not be resolved would render a confident "CA$0.00" — and, worse,
    would be COMPARED against Stripe's real amount and reported as a drift that is really our own
    blind spot. Pinned on the real verifier with a stub provider."""
    monkeypatch.setattr(saas_plans, "individual_price_for", lambda t: 0.0)
    out = _verify_with_stub(monkeypatch, {
        pid: _price(pid, 1500, "CAD") for pid in saas_plans.STRIPE_PRICE_IDS.values()
    })
    indiv = [t for t in out["tiers"] if t["kind"] == "individual"]
    assert indiv, "the individual plans must be checked"
    for t in indiv:
        assert t["expected_cents"] is None, "an unresolved price must be None, never 0"
        assert not t["match"]
        assert any("UNKNOWN" in m for m in t.get("mismatch", [])), (
            "the row must say our catalog figure is unknown, not claim a drift against 0")


# ── the stub Stripe provider ─────────────────────────────────────────────────────────
# NO LIVE CALL IS EVER MADE. This returns exactly the dict shape
# ``StripePaymentProvider.retrieve_price`` produces, per
# https://docs.stripe.com/api/prices/object — unit_amount is "(integer, nullable) … Only set if
# billing_scheme=per_unit", livemode is "If the object exists in live mode, the value is true",
# active is "Whether the price can be used for new purchases", and the recurrence lives on
# recurring.interval / recurring.interval_count.

def _price(pid, cents, currency="CAD", interval="month", interval_count=1,
           active=True, livemode=False, recurring=True):
    return {"price_id": pid, "unit_amount_cents": cents, "currency": currency,
            "interval": interval if recurring else None, "interval_count": interval_count,
            "recurring": recurring, "active": active, "livemode": livemode}


class _StubProvider:
    name = "stripe"

    def __init__(self, prices, configured=True):
        self._prices = prices
        self._configured = configured

    def is_configured(self):
        return self._configured

    def retrieve_price(self, price_id):
        return self._prices.get(price_id)


def _verify_with_stub(monkeypatch, prices, configured=True):
    from saas import payments as _payments
    monkeypatch.setattr(_payments, "get_payment_provider",
                        lambda: _StubProvider(prices, configured))
    return saas_checkout.verify_catalog_prices()


def _all_expected_prices():
    """Every price id the verifier will ask for, answered with OUR catalog figure = a full match."""
    out = {}
    for t in saas_checkout.tier_catalog():
        if t.get("price_id"):
            out[t["price_id"]] = _price(t["price_id"], t["amount_cents"], t["currency"],
                                        t["interval"])
    cur = "CAD"
    for tier, pid in saas_plans.STRIPE_PRICE_IDS.items():
        cents = int(round(saas_plans.individual_price_for(tier) * 100))
        out[pid] = _price(pid, cents, cur, "month")
    return out


def test_all_match_reports_ok_across_both_products(monkeypatch):
    out = _verify_with_stub(monkeypatch, _all_expected_prices())
    assert out["configured"] is True
    assert out["ok"] is True, out["problems"]
    assert out["problems"] == []
    assert out["checked"] == len(saas_checkout.tier_catalog()) + len(saas_plans.STRIPE_PRICE_IDS)
    assert all(t["match"] for t in out["tiers"])


def test_drift_is_reported_as_drift_and_never_silently_reconciled(monkeypatch):
    """A Stripe amount that disagrees with the catalog must SURFACE, not be adopted.

    ⚠️ THE FAILURE THIS FORBIDS is the tempting one: 'Stripe is the system of record, so show
    Stripe's number'. That would make the panel agree with Stripe by construction and it could
    never flag anything — while the SIGNED, APPEND-ONLY contract still says the old figure. The
    catalog value must remain visible ALONGSIDE the live one, and the row must be marked."""
    prices = _all_expected_prices()
    team = next(t for t in saas_checkout.tier_catalog() if t["tier"] == "team")
    prices[team["price_id"]] = _price(team["price_id"], team["amount_cents"] + 12345,
                                      team["currency"], team["interval"])
    out = _verify_with_stub(monkeypatch, prices)
    assert out["ok"] is False
    row = next(t for t in out["tiers"] if t["price_id"] == team["price_id"])
    assert row["match"] is False
    assert row["expected_cents"] == team["amount_cents"], (
        "our catalog figure must survive the comparison — reconciling to Stripe's number is the "
        "one thing this check must never do")
    assert row["stripe"]["unit_amount_cents"] == team["amount_cents"] + 12345
    assert any("amount" in m for m in row["mismatch"])
    assert any("amount" in p for p in out["problems"])


def test_archived_one_time_and_wrong_currency_all_surface(monkeypatch):
    """The non-amount failure modes are findings too — an archived or one-time price cannot bill."""
    prices = _all_expected_prices()
    tiers = saas_checkout.tier_catalog()
    prices[tiers[0]["price_id"]] = _price(tiers[0]["price_id"], tiers[0]["amount_cents"],
                                          tiers[0]["currency"], tiers[0]["interval"],
                                          active=False)
    prices[tiers[1]["price_id"]] = _price(tiers[1]["price_id"], tiers[1]["amount_cents"],
                                          tiers[1]["currency"], recurring=False)
    pid = list(saas_plans.STRIPE_PRICE_IDS.values())[0]
    prices[pid] = _price(pid, prices[pid]["unit_amount_cents"], "USD")
    out = _verify_with_stub(monkeypatch, prices)
    assert out["ok"] is False
    joined = " | ".join(out["problems"])
    assert "ARCHIVED" in joined
    assert "ONE-TIME" in joined
    assert "currency" in joined


def test_a_missing_price_is_not_found_not_a_match(monkeypatch):
    out = _verify_with_stub(monkeypatch, {})     # provider answers None for everything
    assert out["ok"] is False
    assert all(t["match"] is False for t in out["tiers"])
    assert all("NOT FOUND" in p for p in out["problems"])


def test_an_unusable_provider_is_reported_as_unusable_and_blames_no_retired_guard(monkeypatch):
    """🔴 "CANNOT be verified" ≠ "not set up", and the panel must still say which — but it must
    not explain the symptom with a cause that no longer exists.

    THIS PAIR OF TESTS IS A WORKED EXAMPLE OF THE DEFECT THIS WHOLE BRANCH IS ABOUT. The panel
    used to report ``live_refusal_by_design: True`` and say "this build REFUSES live credentials
    by design (StripePaymentProvider.__init__ — sandbox-only owner rule)". That rule was LIFTED on
    2026-08-13 (d98b41ef, "the adapter may go live, loudly"): the constructor stopped raising and
    started logging at CRIT. The console copy did not follow — exactly the way the create-tenant
    chooser went on calling Team a zero-base tier after Team was given a base package.

    It was worse than merely stale. The flag was set for ANY unusable provider, so an operator
    running a half-configured Paddle was told about Stripe's sandbox rule, on the one card whose
    job is to tell the truth about prices. Explaining a real symptom with a retired cause is worse
    than saying nothing: it sends the reader to fix something that is not broken.

    The DISTINCTION is still drawn, because it is still real — ``provider_unavailable`` (no
    adapter at all) and ``provider_unusable`` (one resolved but reporting itself not configured)
    send an operator to different places. It is now drawn from what can be OBSERVED, and nothing
    is asserted about why."""
    out = _verify_with_stub(monkeypatch, {}, configured=False)
    assert out["configured"] is False
    assert out["reason"] == "provider_unusable"
    assert "live_refusal_by_design" not in out, (
        "the live-refusal flag must be GONE, not merely False — the guard it named was lifted on "
        "2026-08-13 and a console still reading it renders an explanation that is not true")
    problem = " ".join(out["problems"])
    assert "not usable" in problem.lower() or "not configured" in problem.lower()
    for retired in ("REFUSES", "sandbox-only", "by design"):
        assert retired not in problem, (
            f"the payload still blames {retired!r} — a guard this build does not enforce")
    # ...and it still points the operator at where the fix actually lives.
    assert "Payment settings" in problem


def test_the_live_credential_guard_was_LIFTED_and_now_announces_instead_of_refusing():
    """This test used to be ``test_the_live_credential_guard_itself_is_intact`` and it read:

        "The refusal above is only meaningful while the guard it describes still exists.
         A panel that explains a guard nobody enforces is worse than no panel."

    It was exactly right, it was the tripwire for precisely this, it fired the moment the guard
    was lifted — and it then sat RED and unread while the panel went on explaining the guard.
    A permanently-red test is indistinguishable from noise, and noise is where real reds go to
    hide. So it is not deleted: it is turned around to pin the behaviour that actually ships.

    The adapter now CONSTRUCTS against a live-shaped credential and announces itself at CRIT
    rather than raising (d98b41ef). Losing that announcement would be a silent transition from
    sandbox to real money, which is the thing the original guard existed to prevent — so the
    announcement is what gets pinned."""
    from saas import payments_stripe as ps
    if ps._stripe is None:
        pytest.skip("stripe library not installed — the adapter cannot be constructed")
    for cfg in ({"api_secret": "sk_live_" + "x" * 24, "webhook_secret": "whsec_test",
                 "mode": "test"},
                {"api_secret": "sk_test_" + "x" * 24, "webhook_secret": "whsec_test",
                 "mode": "live"}):
        # It must NOT raise. A refusal here is the retired guard coming back without the panel
        # copy coming back with it, which is how this pair got out of step in the first place.
        adapter = ps.StripePaymentProvider(config=cfg)
        assert adapter is not None
    # The live-credential DETECTION is what makes the announcement possible; it must survive.
    assert ps._is_live_secret("sk_live_" + "x" * 24) is True
    assert ps._is_live_secret("rk_live_" + "x" * 24) is True, (
        "a restricted live key is a live credential in every way that matters")
    assert ps._is_live_secret("sk_test_" + "x" * 24) is False


# ══════════════════════════════════════════════════════════════════════════════════
# 2. HALF B — the password-gated seat-rate write
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_a_seat_rate_write_with_no_password_is_refused_and_changes_nothing(client, db):
    """🔴 THE HEADLINE. Refused (401) AND the stored rates are re-read and found UNCHANGED.

    A status-code-only assertion would pass even if the write had gone through and the refusal
    were cosmetic — the same "verify the READ, not the write" trap that has bitten this repo. So
    the card is read back from the MODULE (the resolver everything else prices from) and from the
    raw DB column, both before and after."""
    before_module = saas_platform.seat_rates()
    before_row = _stored_card(db)

    r = _put_rates(client, {"tenant": {"expert": 9999}}, password=None)
    assert r.status_code == 401, r.text

    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    assert saas_platform.seat_rates() == before_module, "a REFUSED write must change nothing"
    assert _stored_card(db) == before_row
    assert saas_platform.seat_rates()["tenant"]["expert"] != 9999


@requires_db
def test_a_seat_rate_write_with_a_wrong_password_is_refused_and_changes_nothing(client, db):
    before = saas_platform.seat_rates()
    r = _put_rates(client, {"team": {"agent": 12345}}, password="not-the-password")
    assert r.status_code == 401, r.text
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    assert saas_platform.seat_rates() == before
    assert saas_platform.seat_rates()["team"]["agent"] != 12345


@requires_db
def test_the_break_glass_operator_key_cannot_change_a_seat_rate(client, db):
    """The static key has no password to re-prove, so it is refused — same rule as the
    contractual figures. It keeps every other operator power, including the offline switch."""
    before = saas_platform.seat_rates()
    r = client.put("/admin/pricing/seat-rates",
                   json={"rates": {"tenant": {"standard": 4242}},
                         "confirm_password": OP_PASSWORD},
                   headers=_op())
    assert r.status_code == 403, r.text
    assert "break-glass" in str(r.json().get("detail", ""))
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    assert saas_platform.seat_rates() == before


@requires_db
def test_a_valid_password_saves_the_rate_and_audits_it(client, db):
    """The positive path: it lands, it is readable through the resolver, and it is on the trail."""
    target = 4242
    r = _put_rates(client, {"tenant": {"expert": target}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    assert saas_platform.seat_rates()["tenant"]["expert"] == target
    assert saas_platform.seat_rate_cents("tenant", "expert") == target
    # The OTHER cells are untouched — a partial edit must not rewrite the whole card's values.
    shipped = saas_platform.shipped_seat_rates()
    assert saas_platform.seat_rates()["team"]["expert"] == shipped["team"]["expert"]

    with db.cursor() as cur:
        cur.execute("SELECT action, result, detail FROM public.saas_audit_log "
                    "WHERE action IN ('platform.seat_rates', 'platform_seat_rates_set') "
                    "ORDER BY id DESC LIMIT 5")
        rows = cur.fetchall() or []
    assert rows, "a seat-rate change must be on the append-only audit trail"
    ours = [x for x in rows if x[0] == "platform.seat_rates"]
    assert ours, "the route's own old→new audit line is missing"
    assert ours[0][1] == "success"
    assert "4242" in (ours[0][2] or ""), "the audit line must carry the NEW value"
    assert "→" in (ours[0][2] or ""), "the audit line must state old→new, not just new"


@requires_db
def test_a_refused_write_is_audited_as_denied(client, db):
    """A failed attempt on the billing configuration is exactly the event to find later."""
    with db.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM public.saas_audit_log")
        high = cur.fetchone()[0]
    _put_rates(client, {"tenant": {"expert": 7777}}, password="wrong")
    with db.cursor() as cur:
        cur.execute("SELECT action, result FROM public.saas_audit_log "
                    "WHERE id > %s AND action = 'platform.seat_rates_denied'", (high,))
        rows = cur.fetchall() or []
    assert rows, "a denied seat-rate write must be audited"
    assert all(x[1] == "denied" for x in rows)


@requires_db
def test_zero_is_refused_because_zero_is_free_service(client, db):
    """0 is not a low price, it is free service applied to every future purchase of that type."""
    before = saas_platform.seat_rates()
    r = _put_rates(client, {"tenant": {"standard": 0}})
    assert r.status_code == 400, r.text
    assert "free service" in str(r.json().get("detail", "")).lower()
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    assert saas_platform.seat_rates() == before


@requires_db
def test_a_float_rate_is_refused_rather_than_rounded(client, db):
    """Money is integer cents. Rounding a float is how 12.5 silently becomes 12 forever."""
    before = saas_platform.seat_rates()
    r = _put_rates(client, {"tenant": {"standard": 12.5}})
    assert r.status_code == 400, r.text
    assert "cents" in str(r.json().get("detail", "")).lower()
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    assert saas_platform.seat_rates() == before


@requires_db
def test_unknown_kind_or_seat_type_is_refused_not_silently_dropped(client, db):
    """A typo'd key that is quietly ignored is the 'saves, persists, nothing reads it' trap."""
    assert _put_rates(client, {"individual_house": {"standard": 1500}}).status_code == 400
    assert _put_rates(client, {"tenant": {"platinum": 1500}}).status_code == 400


@requires_db
def test_the_panel_reports_whether_existing_customers_are_re_priced(client, db):
    """The consequence statement is MEASURED from the schema, never asserted as console copy.

    An existing seat keeps its purchased rate only if ``account_entitlements`` actually carries a
    snapshot column. If that were console prose it would go on promising the protection long
    after anybody noticed it was never built."""
    d = client.get("/admin/pricing", headers=_op()).json()
    pr = d["historic_rate_protection"]
    assert pr["protected"] in (True, False, None)
    assert pr["reason"], "the panel must be able to say WHY, in either direction"
    with db.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='account_entitlements'")
        cols = {r[0] for r in (cur.fetchall() or [])}
    snapshot_present = any(m in c for c in cols for m in ("price", "rate", "amount", "cents"))
    assert pr["protected"] is snapshot_present, (
        "the claim must track the schema — reporting protection the table cannot provide is the "
        "failure this measurement exists to prevent")


# ══════════════════════════════════════════════════════════════════════════════════
# 3. The console surface — WHERE the panel lives, CSP, read-only rendering
# ══════════════════════════════════════════════════════════════════════════════════

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = open(os.path.join(_HERE, "saas", "dashboard", "index.html"), encoding="utf-8").read()
APPJS = open(os.path.join(_HERE, "saas", "dashboard", "app.js"), encoding="utf-8").read()


def _view_markup(name: str) -> str:
    """The markup of ONE operator view, sliced at its real element boundary.

    Sliced by BALANCED-DEPTH parse rather than "text between two data-view strings", because
    the latter silently returns the wrong span the moment a view is re-ordered — and the whole
    point of the tests below is to know which view a panel is really inside."""
    from html.parser import HTMLParser

    class _Slice(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.depth = None
            self.start = None
            self.end = None

        def handle_starttag(self, tag, attrs):
            if tag != "div":
                return
            if self.depth is None:
                if dict(attrs).get("data-view") == name:
                    self.depth = 1
                    self.start = self.getpos()
                return
            self.depth += 1

        def handle_endtag(self, tag):
            if tag != "div" or self.depth is None or self.end is not None:
                return
            self.depth -= 1
            if self.depth == 0:
                self.end = self.getpos()

    p = _Slice()
    p.feed(INDEX)
    assert p.start is not None, f'no view markup for data-view="{name}"'
    assert p.end is not None, f'the data-view="{name}" element never closes'
    lines = INDEX.splitlines(keepends=True)
    return "".join(lines[p.start[0] - 1:p.end[0]])


PLATFORM_VIEW = _view_markup("platform")
PAYMENTS_VIEW = _view_markup("payments")
# ⚠️ 2026-08-17 IA MOVE (owner). The two halves no longer share a view. The PROVIDER-COUPLED
# half (base tier prices + the verification) went to the processor's OWN destination, which
# also stopped being named "Stripe billing"; the PROVIDER-INDEPENDENT half (per-seat rates)
# stayed under the platform-wide knobs because the processor never sees a seat rate and the
# rates survive a swap. Both directions are still asserted for BOTH halves below.
PROVIDER_VIEW = _view_markup("provider-config")


# ── WHERE IT LIVES — asserted in BOTH DIRECTIONS ───────────────────────────────────
# 🔴 A one-directional "it is in Payment settings" assertion passes for a copy-paste that left
# the panel on Platform as well, and TWO pricing panels reading the same endpoint is worse than
# the misfiling this move exists to fix: the seat-rate grid would exist twice, both copies would
# push ids into the same LOCKED_GROUPS.seatrates.fields, and whichever rendered last would own
# the gate. Every test below asserts presence AND absence.

def test_the_pricing_panel_is_NOT_on_the_platform_view_any_more():
    """The owner went looking for pricing under Payment settings and Stripe billing. It was on
    Platform, beside RETENTION and CONTRACT, which is not where anyone looks for a price."""
    for ident in ('id="pricing-base-body"', 'id="btn-pricing-verify"', 'id="lockgrp-seatrates"',
                  'id="seatrates-confirm-password"', 'id="pricing-verify-banner"'):
        assert ident not in PLATFORM_VIEW, (
            f"{ident} is STILL on the platform view — a move that leaves a copy behind gives the "
            f"console two pricing panels sharing one LOCKED_GROUPS entry")


_PROVIDER_COUPLED_IDS = ('id="pricing-base-body"', 'id="btn-pricing-verify"',
                         'id="pricing-verify-banner"', 'id="pricing-provider-scope"',
                         'id="pricing-live-col"', 'id="pricing-priceid-col"')
_PROVIDER_INDEPENDENT_IDS = ('id="lockgrp-seatrates"', 'id="seatrates-confirm-password"',
                             'id="seatrates-protection-banner"', 'id="seatrates-body"')


def test_the_seat_rate_half_IS_on_payment_settings_and_the_base_tier_half_is_NOT():
    """PAYMENT SETTINGS = the platform-wide knobs, and ONLY those. A per-seat rate survives a
    processor swap untouched (the processor collects a flat base with quantity:1 and never sees
    one), which is the definition of a platform knob, so it stays. A base tier price is held by
    the SELECTED processor against its own price id, so it does not."""
    for ident in _PROVIDER_INDEPENDENT_IDS:
        assert ident in PAYMENTS_VIEW, f"{ident} left the payments view and should not have"
        assert ident not in PROVIDER_VIEW, (
            f"{ident} was dragged onto the processor's destination — a seat rate filed under one "
            f"provider's settings is orphaned the moment the provider changes")
    for ident in _PROVIDER_COUPLED_IDS:
        assert ident not in PAYMENTS_VIEW, (
            f"{ident} is STILL on the payments view — a move that leaves a copy behind gives the "
            f"console two base-tier tables reading one endpoint")
    assert 'data-locked-edit="seatrates"' in PAYMENTS_VIEW, (
        "the seat-rate group must use the SHARED locked-group mechanism, not a second one")


def test_the_base_tier_half_IS_on_the_processors_own_destination():
    """The owner chose the processor under Payment settings and then had to hunt for that
    processor's prices — which were filed under the platform knobs, while the destination
    actually named after a processor held none of them."""
    for ident in _PROVIDER_COUPLED_IDS:
        assert ident in PROVIDER_VIEW, f"{ident} did not arrive on the provider-config view"
    assert 'id="stripe-billing-panel"' in PROVIDER_VIEW, (
        "the processor's operational panel (health, endpoint/webhook control, MRR, dunning, "
        "refunds) belongs on the same destination as its prices — that is what makes it the "
        "processor's own surface rather than a second billing page")


def test_retention_and_contract_did_NOT_move():
    """Only PRICING moved. A data-retention window and a contract term are not payment-provider
    settings, and dragging them along would just relocate the same misfiling."""
    for ident in ('id="lockgrp-retention"', 'id="lockgrp-contract"',
                  'id="platform-contract_term_months"', 'id="platform-data_retention_days"'):
        assert ident in PLATFORM_VIEW, f"{ident} left the platform view and should not have"
        assert ident not in PAYMENTS_VIEW, f"{ident} was dragged onto the payments view"
        assert ident not in PROVIDER_VIEW, f"{ident} was dragged onto the provider-config view"


# ── THE TAB STRIP — order is the argument ──────────────────────────────────────────

def _pay_tabs():
    import re
    nav = re.search(r'<nav class="tabs[^"]*" id="pay-tabs">(.*?)</nav>', PAYMENTS_VIEW, re.S)
    assert nav, "the Payment settings view has no #pay-tabs strip"
    return re.findall(r'<button[^>]*data-ptab="([a-z-]+)"[^>]*>', nav.group(1)), nav.group(1)


def test_pricing_is_the_DEFAULT_tab_and_config_is_LAST():
    """Owner's ordering, and the ordering is the safety argument: most-used first, most-
    destructive last. Pricing is what somebody comes here for, so the view opens on it;
    changing the PROVIDER re-points every future checkout, so it is at the far end where it
    cannot be hit while looking up a price."""
    keys, nav = _pay_tabs()
    # Tab 1 was "pricing" and held BOTH halves. The base-tier half left for the processor's own
    # destination, so what remains is exactly the seat rates and the tab says so — a tab still
    # labelled "Pricing" that no longer holds the base prices is the misfiling all over again.
    assert keys == ["seat-rates", "configuration", "config"], (
        f"tab order is the owner's instruction, not an accident: {keys}")
    assert keys[0] == "seat-rates" and keys[-1] == "config"
    assert "pricing" not in keys, (
        "no tab may still be keyed 'pricing' — the base tier prices are not on this view")
    import re
    active = re.findall(r'<button[^>]*class="active"[^>]*data-ptab="([a-z-]+)"', nav)
    assert active == ["seat-rates"], (
        f"exactly ONE tab may be marked active in the markup: {active}")
    # ...and its panel is the only one not born hidden.
    import re as _re
    panels = _re.findall(r'<div class="tab-panel([^"]*)" data-panel="([a-z-]+)"', PAYMENTS_VIEW)
    shown = [k for cls, k in panels if "hidden" not in cls]
    assert shown == ["seat-rates"], f"Seat rates must be the panel that loads: {panels}"


def test_the_config_tab_is_the_ONLY_home_of_the_provider_control():
    """Both directions again: the selector is on Config, and it is not left behind on the
    configuration tab where it used to sit next to Mode and Currency."""
    import re
    panels = dict()
    for m in re.finditer(r'<div class="tab-panel[^"]*" data-panel="([a-z-]+)"', PAYMENTS_VIEW):
        panels[m.group(1)] = m.start()
    order = sorted(panels.items(), key=lambda kv: kv[1])
    bounds = {}
    for i, (k, pos) in enumerate(order):
        nxt = order[i + 1][1] if i + 1 < len(order) else len(PAYMENTS_VIEW)
        bounds[k] = PAYMENTS_VIEW[pos:nxt]
    assert 'id="pay-provider"' in bounds["config"], "the provider selector is not on Config"
    assert 'id="pay-provider"' not in bounds["configuration"], (
        "the provider selector is STILL on the Payment-configuration tab — the whole point of "
        "the move is that changing WHO takes the money is not a field beside the currency box")
    assert 'id="btn-pay-provider-save"' in bounds["config"]
    # the working credential surface stays on tab 2
    for ident in ('id="pay-mode"', 'id="pay-currency"', 'id="pay-api_key"',
                  'id="pay-webhook_secret"', 'id="pay-banner"'):
        assert ident in bounds["configuration"], f"{ident} should stay on Payment configuration"
        assert ident not in bounds["config"], f"{ident} leaked onto Config"


def test_the_tab_strip_reuses_the_consoles_own_tab_component():
    """Not a third tab implementation. `nav.tabs` + an `active` class is what Moderation and
    Users already use, and `.view-tabs` only zeroes the full-bleed padding the shared rule
    carries for the tenant console's page-level bar."""
    _, nav = _pay_tabs()
    assert '<nav class="tabs view-tabs" id="pay-tabs">' in PAYMENTS_VIEW
    css = open(os.path.join(_HERE, "saas", "dashboard", "style.css"), encoding="utf-8").read()
    assert "nav.tabs.view-tabs" in css, "the .view-tabs modifier is not styled"
    # Every class the moved markup uses must really be STYLED somewhere — an invented class (the
    # `table-wrap`/`tbl` mistake) renders a table that overruns its panel and pushes the page
    # wide. The console styles from two places and both are legitimate: style.css, and the inline
    # <style> block in index.html (CSP forbids inline SCRIPT, not inline style), which is where
    # .locked-group lives. Searching only one of them fails an honest class.
    styled = css + INDEX[:INDEX.index("</style>") + 8]
    for cls in ("table-scroll", "no-scroll", "sub-table", "empty", "tab-panel",
                "locked-group", "pay-banner", "help-strip"):
        assert cls in styled, f"class '{cls}' is used in the markup but is styled nowhere"
    assert "nav.tabs button" in css


# ── PROVIDER-AGNOSTIC SPLIT ────────────────────────────────────────────────────────

def test_the_two_halves_declare_whether_they_survive_a_provider_swap():
    """A second processor is expected. The split is marked in the markup so the next person does
    not have to re-derive it — and it is the same split the money makes: base tiers are held by
    the processor, seat rates are billed by us and it never sees them."""
    import re
    coupled = re.findall(r'id="pricing-processor-half" data-provider-coupled="(\w+)"',
                         PROVIDER_VIEW)
    assert coupled == ["true"], "the base-tier half must declare itself provider-coupled"
    indep = re.search(r'id="lockgrp-seatrates" data-provider-coupled="(\w+)"', PAYMENTS_VIEW)
    assert indep and indep.group(1) == "false", (
        "the seat-rate grid must declare itself provider-INDEPENDENT — Stripe never sees a seat "
        "rate (create_checkout sends a flat price with quantity:1), so a provider swap must not "
        "orphan it")


def test_no_provider_is_named_in_the_provider_coupled_labels():
    """The column header and the verify button name whichever processor is SELECTED, resolved
    from the catalog GET /admin/payment-config serves. Baking 'Stripe' into the markup is how a
    console ends up telling an operator it verified against a provider they switched away from."""
    for hardcoded in ("Live Stripe", "Verify against Stripe", "Stripe price id"):
        assert hardcoded not in PROVIDER_VIEW, (
            f'"{hardcoded}" is hardcoded in the markup — it must be rendered from the provider '
            f"catalog by renderPricingProviderScope()")
    assert 'id="pricing-live-col"' in PROVIDER_VIEW
    assert 'id="pricing-priceid-col"' in PROVIDER_VIEW
    assert 'id="pricing-provider-scope"' in PROVIDER_VIEW
    # and the renderer really reads the served catalog rather than a provider id in this file
    _a = APPJS.index("function pricingProvider(")
    _b = APPJS.index("// ── the base tiers, read-only", _a)
    scope = APPJS[_a:_b]
    assert "cfg.catalog" in scope, "the provider label must come from the served catalog"
    for pid in ("'stripe'", '"stripe"', "'paddle'", "'lemonsqueezy'", "'paypal'"):
        assert pid not in scope, (
            f"{pid} is a provider id written into the dashboard JS — the catalog is the source")


def test_the_pricing_panel_is_loaded_by_the_payment_settings_view_not_platform():
    """Where the markup lives and where the fetch is fired must agree, or the panel renders empty
    until somebody happens to open the other view first."""
    import re
    load_pay = APPJS[APPJS.index("async function loadPayments()"):]
    load_pay = load_pay[:load_pay.index("\n}")]
    assert "loadPricing()" in load_pay, "the Payment settings view must fetch the pricing payload"
    # ...and so must the processor's own destination, which now holds the OTHER half. A view that
    # renders a base-tier table it never fetched shows an empty table until the operator happens
    # to open some other page first.
    load_pc = APPJS[APPJS.index("async function loadProviderConfig()"):]
    load_pc = load_pc[:load_pc.index("\n}")]
    assert "loadPricing()" in load_pc, (
        "the Provider config view holds the base-tier half and must fetch the pricing payload")
    assert "loadPayCfg()" in load_pc, (
        "the provider LABEL and the verify button's target come from the payment config; without "
        "it this destination renders a neutral name for a processor that is in fact selected")
    assert "bindLockedGroups()" in load_pay, (
        "the seat-rate password gate is wired by the shared binder; without it here the Edit "
        "button only works if the operator visited Platform first")
    load_plat = APPJS[APPJS.index("async function loadPlatform()"):]
    load_plat = load_plat[:load_plat.index("\n}")]
    assert "loadPricing()" not in load_plat, (
        "loadPlatform must no longer render pricing — that is the move this change is")


def test_the_panel_adds_no_nav_item():
    """It rides the existing Payment settings view. A new nav entry would move a count another
    suite pins (tests/test_operator_console_ux.py::test_all_24_nav_items_survive_the_resection),
    and moving a panel BETWEEN views is precisely a change that must add none."""
    import re
    nav = re.findall(r'class="nav-item[^"]*"[^>]*data-view="([a-z-]+)"', INDEX)
    assert "pricing" not in nav
    assert "seatrates" not in nav


def test_no_inline_script_and_no_event_handler_attributes():
    """🔴 CSP is ``script-src 'self'`` with NO 'unsafe-inline'. An inline-script build renders
    BLANK, and an ``on*=`` attribute silently never fires."""
    import re
    inline = [t for t in re.findall(r"<script[^>]*>", INDEX) if "src=" not in t]
    assert inline == [], f"inline <script> blocks break the console under CSP: {inline}"
    handlers = re.findall(r"\son[a-z]+\s*=\s*[\"']", INDEX)
    assert handlers == [], f"on*= handlers never fire under this CSP: {handlers}"
    cdn = re.findall(r'(?:src|href)="https?://[^"]+"', INDEX)
    assert cdn == [], f"external/CDN references are blocked by CSP: {cdn}"


def test_the_javascript_carries_no_price_literal():
    """Every figure is served by GET /admin/pricing — metadata-driven, per the hard constraint."""
    import re
    block = APPJS[APPJS.index("// ══ PRICING ═"):APPJS.index("// Focused save for the MFA")]
    # The shipped rate card and the base package prices, in cents and in dollars.
    for forbidden in ("1000", "1250", "3000", "3500", "4500", "5000", "8000", "10000",
                      "75000", "150000", "400000", "750", "1500", "4000"):
        assert forbidden not in block, (
            f"the price literal {forbidden} is in app.js — every amount must come from the API")
    assert "'/admin/pricing'" in block
    assert "'/admin/billing-ops/verify-prices'" in block
    # UNKNOWN, never zero.
    assert "unknown" in block
    assert re.search(r"cents\s*==\s*null", block), (
        "a null amount must render 'unknown'; there must be no path that coerces it to 0")


def test_the_seat_rate_group_reuses_the_shared_lock_machinery():
    """Not a second gate, not a second lock: the same LOCKED_GROUPS map and the same password
    helper the contractual figures use.

    The group also keeps its ``onCancel`` hook. renderPlatform() restores the platform-settings
    singleton on cancel and the seat-rate grid is not part of it, so without this hook an
    abandoned edit leaves a half-typed rate sitting in a box. It mattered more after the move:
    the grid is no longer on the view renderPlatform is about."""
    assert "seatrates: { wrap: 'lockgrp-seatrates'" in APPJS
    assert "lockedGroupPassword('seatrates')" in APPJS
    assert "confirm_password: pw" in APPJS
    grp = APPJS[APPJS.index("seatrates: { wrap: 'lockgrp-seatrates'"):]
    grp = grp[:grp.index("\n};")]
    assert "onCancel" in grp and "renderSeatRates()" in grp, (
        "the seat-rate group must restore its own grid on cancel — renderPlatform() cannot, and "
        "the grid now lives on a different view entirely")


def test_the_seat_rate_save_REFUSES_before_sending_when_no_password_was_typed():
    """The client must not turn an empty password box into a request the server has to audit as
    a failed attempt — a blank field is a slip, not an attack (``lockedGroupPassword`` returns
    null precisely so the caller can refuse first).

    ⚠️ THIS IS A STRUCTURAL PIN AND IT IS THE WEAKER HALF. The load-bearing refusal is the
    SERVER's — ``PUT /admin/pricing/seat-rates`` refuses and writes nothing, proven behaviourally
    against a real control plane by
    ``test_a_seat_rate_write_with_no_password_is_refused_and_changes_nothing`` above. This one
    only guards the client-side early return, so it asserts the guard's SHAPE: the refusal is the
    whole ``if`` test and its body returns before any ``api(`` call. A grep for the text alone
    stays green under ``if (false && !pw)``, which is why the ordering is asserted too."""
    import re
    body = APPJS[APPJS.index("async function saveSeatRates()"):]
    body = body[:body.index("\n}\n")]
    m = re.search(r"\n  if \(!pw\) \{\n(?:[^\n]*\n)*?\s*return;\n  \}\n", body)
    assert m, ("saveSeatRates must refuse with a bare `if (!pw) { ... return; }` before it "
               "reaches the network — no extra conjunct, no fall-through")
    assert body.index("api('PUT'") > m.end(), (
        "the refusal must come BEFORE the request, not after it")


def test_admin_has_exactly_one_password_gate_implementation():
    """Both protected writes go through ``_require_operator_password``. Two copies is how one of
    them quietly stops refusing the break-glass key."""
    src = open(os.path.join(_HERE, "saas", "admin.py"), encoding="utf-8").read()
    assert src.count("def _require_operator_password") == 1
    assert src.count("reauthenticate_operator(") == 1, (
        "the re-authorization primitive must be called from exactly one place in admin.py")
    assert "_require_operator_password(request, fields=protected" in src   # settings PUT
    assert "_require_operator_password(request, fields=fields" in src      # seat-rate PUT


# ══════════════════════════════════════════════════════════════════════════════════
# LAST — the false-green guard
# ══════════════════════════════════════════════════════════════════════════════════

def test_no_test_was_silently_skipped():
    """A fully-skipped DB run must be RED, not a comfortable green. See the module docstring."""
    assert _DB_RAN["value"], (
        "no DB-backed test ran — set POSTGRES_DSN to a throwaway control plane with 'test' in "
        "its name. A green run that skipped every real assertion is the false green this repo "
        "has already been burned by.")
