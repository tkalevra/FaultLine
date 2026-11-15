"""COMP and TRIAL-EXTENSION must survive the PRICING-IA refactor. CLOSED-SOURCE SaaS layer.

WHY THIS SUITE EXISTS
─────────────────────
The pricing refactor is moving every displayed price off template literals and onto the billing
engine, and it enforces a new rule on that surface:

    🔴 ZERO IS NEVER A PRICE (saas/admin.py:1866, saas/entitlements.py::account_kind_catalog)
       — an amount that cannot be RESOLVED renders "unknown", never a currency zero.

That rule and the COMP lever look identical on screen and are exact opposites underneath:

    UNRESOLVED  → we do not KNOW the amount → ``None`` → renders "unknown".
    COMPED      → we know the amount EXACTLY, and a human DELIBERATELY gave it away →
                  a real ``0.0`` bill, with a signature, a reason, and the gross still visible.

Collapse the two in either direction and you get one of two shipped-grade defects: a comped
account that starts rendering "unknown" (the operator can no longer tell a give-away from a
broken resolver), or an unresolvable price that renders "$0.00" (the console quotes free to an
operator who is about to quote it to a customer). The comp lever is the ONLY sanctioned path to
a zero bill and it must come out of this refactor untouched. Same for the trial lever.

WHAT IS PINNED HERE, AND WHAT WAS ALREADY PINNED ELSEWHERE
──────────────────────────────────────────────────────────
Existing coverage that this file deliberately does NOT duplicate:
  * tests/test_saas_billing_quantity_and_comp.py — the comp waiver's SHAPE as seen through
    ``billing_view`` (gross preserved, one credit line, who/when/why in the label), comp input
    validation, comp fail-toward-billing, ``extend_trial`` argument validation and the exact SQL
    the writer issues. 30 passed.
  * tests/test_external_billing.py — comp vs external billing against a REAL database: the
    genuine comp's byte-identical invoice line, the comp surviving a subscription cancellation,
    the comp winning the credit line over the external overlay, and the operator report not
    counting a Stripe payer as a give-away. 28 passed.

What is NEW here — the gaps those two leave open:
  A. ``_apply_billing_overlays`` pinned AT ITS OWN BOUNDARY. Every existing comp assertion goes
     through ``billing_view``. A refactor that recomputes a money view anywhere else, or that
     reorders/short-circuits the overlay pair, keeps those tests green while a comped account
     silently bills in full on the new path. So: the overlay contract is pinned directly, AND
     ``billing_view`` is pinned to still route through it on BOTH account paths.
  B. THE OPPOSITES, asserted side by side in one place: the comped zero is a real ``0.0`` and is
     self-describing (``comped: True``); the unresolvable price is ``None`` and never 0.
  C. ``billing_overview`` with a comped account and an externally-billed account IN THE SAME
     ROSTER — the two roll-ups measured by DELTA, so a future ``comped += external`` fails loudly.
     The standing rule is at saas/admin.py:3184; they were conflated once and the console
     reported COLLECTED revenue as money GIVEN AWAY.
  D. The trial extension landing IN THE DATABASE, re-read afterwards through the reader the
     serving path uses. A write that returns 200 and does not land is exactly the failure this
     exists to catch — the existing coverage asserts the SQL TEXT, which cannot catch it.
  E. THE THREE TRIAL CLOCKS STAY DISTINCT. They are trivially cross-wired:
       a. ``individual_users.trial_ends_at``   — the individual first-month-free (plans.py:257,
          moved by saas/admin.py:3137 ``extend_individual_trial``). The one that moves MONEY.
       b. ``accounts.trial_ends_at``           — the TENANT account trial (saas/accounts.py:2289,
          migration 226). A BILLING MARKER ONLY: nothing reads it to grant capability, and there
          is no operator lever that extends it — the whole-account lever is the COMP
          (see the deliberate note at saas/plans.py:252).
       c. ``accounts.chatbot_trial_*``         — the chat-agent FEATURE trial (saas/accounts.py
          :2153 / :2186), which DOES gate /v1/agent/chat.

RUN IT (never bare pytest — owner rule, 2026-07-13):

    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.6:5432/faultline_priceia_test \\
      python3 tools/fltest.py --bug PRICEIA \\
        --test tests/test_comp_and_trial_survive_pricing_ia.py --no-publish

FALSE-GREEN GUARD: ``_DB_RAN`` is set by the DB fixture and asserted by the LAST test in this
file. A DSN-gated suite that skips its DB half reports "PASS | N passed, M skipped" and looks
green; that has produced a false green in this repo repeatedly. If the DB half did not run,
this suite is RED.

CLEANUP: every row this suite inserts is torn down in the module fixture — see ``_purge_accounts``
for why that is not a plain DELETE (``account_ledger`` / ``saas_audit_log`` / ``signed_contracts``
are append-only by trigger, and a cascade from ``accounts`` fires those triggers too). The target
database is a SHARED throwaway control plane and a previous worker left rows behind and broke an
unrelated MRR test, so the teardown raises rather than swallowing a failure to clean up.

⚠️ ONE TEST IN HERE WAS AN ``xfail(strict=True)`` FOR ABOUT AN HOUR. It recorded a real,
PRE-EXISTING defect: ``/admin/billing/overview`` accumulated ``individuals.trialing`` and
``individuals.trial_monthly_value`` and then dropped both when building its response, so the
"N on first-month-free trial" pill saas/dashboard/app.js reads them for could never render and an
operator who extended a trial saw no trace of it anywhere on the overview. Confirmed present at
bf209cdb, i.e. it did not arrive with the pricing-IA work — but the pricing-IA bar requires that a
trialing individual keeps SHOWING its trial state, and a field the endpoint never emits does not
show anything. Fixed here rather than recorded: the two keys are emitted and the test is a normal
passing test. Left in place because the write-up is the reason it must stay emitted.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from saas import accounts as accounts_mod        # noqa: E402
from saas import admin as admin_mod              # noqa: E402
from saas import entitlements as ent             # noqa: E402
from saas import plans as plans_mod              # noqa: E402


DEFAULT_TEST_DSN = ("postgresql://faultline:faultline@172.20.0.6:5432/"
                    "faultline_priceia_test")
TEST_DSN = os.environ.get("PRICEIA_TEST_DSN",
                          os.environ.get("POSTGRES_DSN", DEFAULT_TEST_DSN))

_DB_RAN = {"value": False}

COMP_REASON = "PRICEIA regression — owner's own workspace, indefinite"


# ════════════════════════════════════════════════════════════════════════════════════
# PART A — THE OVERLAY CONTRACT, pinned at its own boundary. No database.
# ════════════════════════════════════════════════════════════════════════════════════

_ACTIVE_COMP = {"comped": True, "active": True, "reason": COMP_REASON,
                "by": "owner@example-operator.ca", "at": "2026-08-03T10:00:00+00:00",
                "until": None, "expired": False}
_NO_COMP = {"comped": False, "active": False, "reason": None, "by": None,
            "at": None, "until": None, "expired": False}
_NO_EXTERNAL = {"externally_billed": False, "active": False, "processor": None,
                "reference": None, "source": None}


def _view(subtotal=850.0):
    """A computed billing view as ``billing_view`` hands one to the overlays: real charge lines,
    a real subtotal, nothing zeroed yet."""
    return {
        "monthly_subtotal": subtotal,
        "monthly_total": subtotal,
        "tax_rate": 0.0,
        "tax_amount": 0.0,
        "currency": ent.CURRENCY,
        "lines": [
            {"seat_type": "standard", "kind": "base", "quantity": 2, "licensed": 2,
             "occupied": 2, "unit_price": 750.0, "monthly": 750.0, "label": "Team base"},
            {"seat_type": "expert", "kind": "addon", "quantity": 2, "licensed": 2,
             "occupied": 1, "unit_price": 50.0, "monthly": 100.0, "label": "Expert x2"},
        ],
    }


def _overlay_stub(monkeypatch, *, comp=None, external=None):
    monkeypatch.setattr(ent, "comp_state", lambda a: dict(comp or _NO_COMP))
    monkeypatch.setattr(ent, "external_billing_state",
                        lambda a: dict(external or _NO_EXTERNAL))


def test_the_overlay_preserves_the_gross_and_zeroes_only_the_payable(monkeypatch):
    """THE COMP CONTRACT, asserted on ``_apply_billing_overlays`` ITSELF rather than through
    ``billing_view``.

    Four things must all still be true at once, and each one is a separate way to lose money or
    lose the audit trail:
      * ``gross_subtotal`` survives — it is what the give-away is COSTING, and the operator has
        to keep seeing it. Zero it and the comped roll-up reports every give-away as worth $0.
      * ``comped_value`` equals that gross — the figure ``billing_overview`` sums.
      * exactly ONE explicit credit line — two credit lines render a NEGATIVE subtotal, none is
        the silent $0 the whole feature exists to prevent.
      * only the TOTAL is zeroed.
    """
    _overlay_stub(monkeypatch, comp=_ACTIVE_COMP)
    out = ent._apply_billing_overlays("acct", _view(850.0))

    assert out["comped"] is True
    assert out["gross_subtotal"] == 850.0, "the overlay destroyed the operator's view of the cost"
    assert out["comped_value"] == 850.0
    assert out["monthly_subtotal"] == 0.0 and out["monthly_total"] == 0.0

    credits = [ln for ln in out["lines"] if ln["kind"] == "comp"]
    assert len(credits) == 1, f"a comped bill must carry exactly ONE credit line: {out['lines']}"
    assert credits[0]["monthly"] == -850.0
    # The reason must still be READABLE — on the line and in the structured state.
    assert COMP_REASON in credits[0]["label"]
    assert out["comp"]["reason"] == COMP_REASON
    assert out["comp"]["by"] == "owner@example-operator.ca"
    # A comp WAIVES; it does not HIDE. Every charge line survives the waiver.
    assert [ln["seat_type"] for ln in out["lines"] if ln["kind"] in ("base", "addon")] == \
        ["standard", "expert"]


def test_the_comped_zero_is_a_REAL_zero_and_is_self_describing(monkeypatch):
    """HALF ONE OF THE OPPOSITES. A comped bill is a KNOWN amount, deliberately given away, so
    every money field it produces must be a genuine float — never ``None``, never a sentinel
    that a "zero is never a price" renderer would translate into "unknown".

    The failure this blocks: the pricing surface learns to render ``None``/falsey money as
    "unknown", somebody makes the comp path emit one, and a comped invoice starts telling the
    customer their bill is *unknown* rather than *waived*. The comp must be distinguishable from
    an unresolved amount WITHOUT reading the number, which is what ``comped: True`` is for."""
    _overlay_stub(monkeypatch, comp=_ACTIVE_COMP)
    out = ent._apply_billing_overlays("acct", _view(850.0))

    for key in ("monthly_subtotal", "monthly_total", "tax_amount", "comped_value",
                "gross_subtotal"):
        assert out[key] is not None, f"{key} is None — a comp is a KNOWN amount, not an unknown"
        assert isinstance(out[key], float), f"{key} is {type(out[key]).__name__}, not a float"
    assert out["monthly_total"] == 0.0
    # The flag, not the number, is what tells a renderer this zero is deliberate.
    assert out["comped"] is True
    comp_line = next(ln for ln in out["lines"] if ln["kind"] == "comp")
    assert isinstance(comp_line["monthly"], float) and comp_line["monthly"] == -850.0


def test_an_unresolvable_price_is_None_and_never_a_currency_zero(monkeypatch, capsys):
    """HALF TWO OF THE OPPOSITES, on the surface the refactor added. A tier that HAS a base
    package but whose resolver blows up is ``base_price: None`` — UNKNOWN — while
    ``has_base_package`` stays True so the console still renders a base row and shows it as
    unknown. Falling back to 0 here is how "we could not read the price" becomes "this tier is
    free"; that is the same zero a comp produces, and only the comp is allowed to produce it."""
    monkeypatch.setattr(ent, "base_package_price_for",
                        lambda kind: (_ for _ in ()).throw(RuntimeError("resolver down")))
    rows = {r["kind"]: r for r in ent.account_kind_catalog()}

    for kind in ("tenant", "team", "clinical"):
        row = rows[kind]
        assert row["has_base_package"] is True
        assert row["base_price"] is None, f"{kind} defaulted an unresolvable price to a number"
        assert row["base_price_cents"] is None
        assert row["base_price"] != 0 and row["base_price"] is not False
    # Singles is the OTHER kind of None: a fact, not a failure — no base package to price.
    assert rows["individual_house"]["has_base_package"] is False
    assert rows["individual_house"]["base_price"] is None
    # An unresolvable PRICE is loud. A comp is not an error and must never be logged as one.
    assert "UNKNOWN, not 0" in capsys.readouterr().err


def test_billing_view_still_routes_BOTH_account_paths_through_the_overlays(monkeypatch):
    """🔴 THE REFACTOR GUARD. Every other comp assertion in this repo is written against
    ``billing_view``'s OUTPUT. That proves the overlay works; it does not prove the overlay is
    still WIRED IN on the path a given account kind takes.

    ``billing_view`` has TWO returns that must each go through ``_apply_billing_overlays``
    (saas/entitlements.py:2480 — the enterprise/clinical path, and :2577 — the team path). A
    refactor that adds a third money view, or that inlines the comp on only one of them, ships a
    tenancy kind whose comped accounts BILL IN FULL — with the operator console still showing the
    comp flag, because that is read from the account row. Pin the call, not just the result."""
    seen = []
    real = ent._apply_billing_overlays
    monkeypatch.setattr(ent, "_apply_billing_overlays",
                        lambda aid, view: seen.append(aid) or real(aid, view))
    _overlay_stub(monkeypatch, comp=_ACTIVE_COMP)
    monkeypatch.setattr(ent, "seat_type_usage", lambda a: {"standard": 2})
    monkeypatch.setattr(ent, "get_entitlements", lambda a: {})
    monkeypatch.setattr(ent, "_contract_term",
                        lambda a: {"term_months": 6, "expires_at": None, "expired": False})
    monkeypatch.setattr(ent, "ledger_summary", lambda a, **k: {"periods": [], "grand_total": 0.0})
    monkeypatch.setattr(ent, "_account_tax", lambda a: {"tax_rate": 0.0, "tax_amount": 0.0})
    monkeypatch.setattr(ent, "scheduled_downgrades", lambda a: [])

    for kind in ("team", "tenant"):
        seen.clear()
        monkeypatch.setattr(ent, "_account_kind", lambda a, _k=kind: _k)
        view = ent.billing_view(f"acct-{kind}")
        assert seen == [f"acct-{kind}"], \
            f"the {kind} billing path no longer calls _apply_billing_overlays"
        assert view["comped"] is True and view["monthly_total"] == 0.0, \
            f"a comped {kind} account was billed in full"
        assert view["gross_subtotal"] > 0.0


def test_the_overlay_pair_keeps_its_order_so_the_human_decision_owns_the_credit(monkeypatch):
    """The ordering argument lives in ``_apply_billing_overlays``'s docstring; this pins the
    OUTCOME of it at the same boundary, for the case where both overlays fire. The comp (a signed
    human decision) owns the single credit line; the external mark (a webhook's observation)
    degrades to a ZERO-AMOUNT advisory. Reverse it and a comped Stripe payer's give-away vanishes
    from the one report built to find give-aways."""
    _overlay_stub(monkeypatch, comp=_ACTIVE_COMP,
                  external={"externally_billed": True, "active": True, "processor": "stripe",
                            "reference": "sub_priceia_order", "source": "stripe-webhook"})
    out = ent._apply_billing_overlays("acct", _view(850.0))

    assert out["comped"] is True and out["externally_billed"] is True
    assert out["comped_value"] == 850.0, "the comp must own the credit"
    assert out["externally_collected_value"] == 0.0, "the deferring overlay must not double-count"
    assert len([ln for ln in out["lines"] if float(ln["monthly"]) < 0]) == 1
    note = next(ln for ln in out["lines"] if ln["kind"] == "external_billing_note")
    assert float(note["monthly"]) == 0.0
    assert "sub_priceia_order" in note["label"]
    assert out["monthly_total"] == 0.0


# ════════════════════════════════════════════════════════════════════════════════════
# PART B — THE OUTCOME, against a REAL control-plane database
# ════════════════════════════════════════════════════════════════════════════════════

def _connect():
    import psycopg2
    return psycopg2.connect(TEST_DSN)


def _mk_account(conn, *, kind="tenant", seat_limit=5, name=None):
    slug = f"priceia{uuid.uuid4().hex[:12]}"
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


def _mk_individual(conn, account_id, *, seat_type="standard", trial_ends_at=None):
    """A REAL self-serve individual: the ``individual_users`` row (which owns the trial clock)
    plus the ``account_users`` seat (which is what ``individual_revenue`` prices)."""
    uid = str(uuid.uuid4())
    email = f"priceia-{uuid.uuid4().hex[:10]}@example.invalid"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.individual_users (user_id, account_id, email, password_hash, "
            " plan, status, trial_ends_at) VALUES (%s, %s, %s, 'x', 'individual', 'active', %s)",
            (uid, account_id, email, trial_ends_at),
        )
        cur.execute(
            "INSERT INTO public.account_users (account_id, user_id, status, seat_type) "
            "VALUES (%s, %s, 'active', %s)",
            (account_id, uid, seat_type),
        )
    conn.commit()
    return uid


def _sign_contract(conn, aid, sub, *, cents, currency="CAD", interval="month"):
    """A REAL ``public.signed_contracts`` row — THE only source ``external_collected_monthly``
    will accept for "how much does the rail actually collect". Without one the account is
    externally MARKED but its collected amount is UNRESOLVED, which correctly bills in full and
    would give this suite nothing to measure the roll-up against.

    ``contract_templates`` is itself append-only, so an EXISTING template is reused and a new one
    is only minted if the database has none at all."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM public.contract_templates ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO public.contract_templates (version, title, body_markdown) "
                        "VALUES (%s, 'PRICEIA terms', 'body') RETURNING id",
                        (f"v-priceia-{uuid.uuid4().hex[:10]}",))
            tid = cur.fetchone()[0]
        else:
            tid = row[0]
        cur.execute(
            "INSERT INTO public.signed_contracts (contract_template_id, signer_email, tier, "
            " account_id, stripe_subscription_id, agreed_amount_cents, agreed_currency, "
            " agreed_interval, contract_snapshot, status) "
            "VALUES (%s, %s, 'enterprise', %s, %s, %s, %s, %s, 'snapshot', 'completed')",
            (tid, f"priceia-{uuid.uuid4().hex[:8]}@example.invalid", aid, sub, cents,
             currency, interval))
    conn.commit()


def _account_row(conn, aid, *cols):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM public.accounts WHERE account_id = %s", (aid,))
        row = cur.fetchone()
    conn.commit()
    return dict(zip(cols, row))


def _individual_trial_ends_at(conn, uid):
    """The value the DATABASE holds — not the dict the writer returned."""
    with conn.cursor() as cur:
        cur.execute("SELECT trial_ends_at FROM public.individual_users WHERE user_id = %s", (uid,))
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def _purge_accounts(conn, account_ids, user_ids):
    """Remove every row this suite inserted. The target DSN is a SHARED throwaway control plane —
    a previous worker left rows behind and broke an unrelated MRR test — so teardown is not
    optional and it is not best-effort.

    IT IS ALSO NOT A PLAIN DELETE, for two reasons that both come from the production design:
      * ``account_ledger``, ``saas_audit_log`` and ``signed_contracts`` are APPEND-ONLY. Each
        carries a BEFORE DELETE trigger that RAISEs — they are permanent financial / security
        records and there is deliberately no application path that removes one. A cascade from
        ``accounts`` fires those triggers too, so even deleting the parent fails.
      * ``session_replication_role = 'replica'`` (superuser) suspends those triggers, but it
        suspends the FK CASCADE along with them — so every child is deleted EXPLICITLY, walked
        out of ``pg_constraint`` rather than hardcoded, so a child table this suite never touched
        by name still goes.
    """
    if not account_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.conrelid::regclass::text, a.attname "
            "FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "WHERE c.contype = 'f' AND c.confrelid = 'public.accounts'::regclass")
        children = cur.fetchall()
        cur.execute("SET session_replication_role = 'replica'")
        try:
            # No FK on these two, so the walk below cannot find them.
            cur.execute("DELETE FROM public.signed_contracts "
                        "WHERE account_id = ANY(%s::uuid[])", (account_ids,))
            cur.execute("DELETE FROM public.saas_audit_log "
                        "WHERE account_id = ANY(%s::uuid[])", (account_ids,))
            if user_ids:
                # saas_audit_log.target_id is TEXT — no uuid cast here, deliberately.
                cur.execute("DELETE FROM public.saas_audit_log WHERE target_id = ANY(%s)",
                            (user_ids,))
            for table, col in children:
                cur.execute(f'DELETE FROM {table} WHERE "{col}" = ANY(%s::uuid[])',
                            (account_ids,))
            cur.execute("DELETE FROM public.accounts WHERE account_id = ANY(%s::uuid[])",
                        (account_ids,))
        finally:
            cur.execute("SET session_replication_role = 'origin'")
    conn.commit()


def _fake_request():
    from fastapi import Request
    return Request({"type": "http", "method": "POST", "headers": [], "path": "/x",
                    "query_string": b""})


@pytest.fixture(scope="module")
def world():
    """Build the whole roster ONCE, snapshot ``/admin/billing/overview`` before and after, then
    tear every inserted row back out.

    Snapshotting by DELTA rather than by absolute is deliberate: the target DSN is a SHARED
    throwaway that already holds 100+ accounts from other suites, so "the comped count is 1" is
    unrunnable and "the comped count went up by exactly 1, and its value went up by exactly this
    account's gross" is both runnable and stricter — it proves nothing ELSE moved either.
    ``billing_overview`` walks every account and prices each one, so it is called exactly twice.
    """
    if "test" not in TEST_DSN.rsplit("/", 1)[-1]:
        pytest.skip(f"refusing to run against a non-test database: {TEST_DSN}")
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PRICEIA test DB unreachable ({type(exc).__name__}): {TEST_DSN}")
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.accounts'), "
                    "       to_regclass('public.individual_users'), "
                    "       to_regclass('public.account_users'), "
                    "       to_regclass('public.signed_contracts')")
        present = cur.fetchone()
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'accounts' "
                    "AND column_name IN ('externally_billed', 'comped', 'trial_ends_at', "
                    "                    'chatbot_trial_expires_at')")
        cols = {r[0] for r in cur.fetchall()}
    conn.commit()
    if not all(present) or len(cols) < 4:
        conn.close()
        pytest.skip("throwaway DB is missing the SaaS control plane (accounts/individual_users/"
                    "signed_contracts, migrations 211 / 220 / 226 / 245) — see this file's header")

    mp = pytest.MonkeyPatch()
    mp.setenv("POSTGRES_DSN", TEST_DSN)
    mp.setenv("SAAS_MODE", "1")

    made = {"accounts": [], "individuals": []}
    state = {}
    try:
        _DB_RAN["value"] = True
        state["before"] = admin_mod.billing_overview(_op=True)

        # ── 1. the COMPED account: a real, non-zero gross, deliberately given away ──────
        comped_aid = _mk_account(conn, kind="tenant", name=f"priceia-comped-{uuid.uuid4().hex[:6]}")
        made["accounts"].append(comped_aid)
        state["comped_gross"] = float(ent.billing_view(comped_aid)["monthly_total"])
        assert state["comped_gross"] > 0.0, "the comp fixture needs a NON-ZERO bill to give away"
        out = ent.set_comp(comped_aid, True, reason=COMP_REASON, actor="operator",
                           actor_user="owner@example-operator.ca", actor_role="operator")
        assert out["ok"] is True, out
        state["comped_aid"] = comped_aid

        # ── 2. the EXTERNALLY BILLED account: money COLLECTED, on someone else's rail ───
        ext_aid = _mk_account(conn, kind="tenant", name=f"priceia-payer-{uuid.uuid4().hex[:6]}")
        made["accounts"].append(ext_aid)
        sub = f"sub_{uuid.uuid4().hex[:18]}"
        base_cents = int(round(float(ent.base_package_price_for("tenant")) * 100))
        _sign_contract(conn, ext_aid, sub, cents=base_cents)
        out = ent.set_external_billing(ext_aid, True, processor="stripe", reference=sub,
                                       source="stripe-webhook", actor="stripe-webhook")
        assert out["ok"] is True, out
        state["ext_aid"] = ext_aid
        state["ext_collected"] = float(
            ent.billing_view(ext_aid)["externally_collected_value"])
        assert state["ext_collected"] > 0.0, "the external fixture needs a COLLECTED amount"

        # ── 3. the self-serve INDIVIDUAL, on a LAPSED trial, then extended by the lever ─
        house_aid = _mk_account(conn, kind="individual_house", seat_limit=25,
                                name=f"priceia-singles-{uuid.uuid4().hex[:6]}")
        made["accounts"].append(house_aid)
        lapsed = datetime.now(timezone.utc) - timedelta(days=9)
        uid = _mk_individual(conn, house_aid, seat_type="standard", trial_ends_at=lapsed)
        made["individuals"].append(uid)
        state["house_aid"] = house_aid
        state["individual_uid"] = uid
        state["trial_before"] = _individual_trial_ends_at(conn, uid)

        # The OTHER two clocks, stamped BEFORE the extension so cross-wiring is detectable.
        accounts_mod.start_account_trial(house_aid, days=30, source="priceia-regression")
        accounts_mod.set_chatbot_trial(house_aid, kind="grant", days=14,
                                       granted_by="priceia-regression")
        state["account_trial_before"] = accounts_mod.get_account_trial(house_aid)
        state["chatbot_trial_before"] = accounts_mod.get_chatbot_trial(house_aid)

        # THE LEVER, through the REAL operator route function (admin.py:3137).
        state["extend_response"] = admin_mod.extend_individual_trial(
            uid, admin_mod.ExtendTrialRequest(days=30, reason="PRICEIA regression — pilot"),
            _fake_request(), _op=True)

        state["after"] = admin_mod.billing_overview(_op=True)
        state["conn"] = conn
        yield state
    finally:
        mp.undo()
        try:
            _purge_accounts(conn, made["accounts"], made["individuals"])
        except Exception:                       # noqa: BLE001
            conn.rollback()
            raise
        finally:
            conn.close()


# ── B1. THE ROLL-UPS STAY SEPARATE ────────────────────────────────────────────────

def test_the_comped_and_externally_billed_rollups_never_leak_into_each_other(world):
    """🔴 THE STANDING RULE, at saas/admin.py:3184: "EXTERNALLY BILLED IS NOT A GIVE-AWAY AND
    MUST NEVER BE SUMMED INTO ``comped``". They were conflated once — the Stripe flow called
    ``set_comp(True)`` on every PAYING tenant — and the console reported COLLECTED revenue as
    money GIVEN AWAY, while "who is on a freebie?" could not be answered at all.

    Both states produce a ZERO internal bill, which is exactly why they look alike and exactly
    why this must fail loudly. One comped account and one externally-billed account are added to
    the SAME roster and each roll-up is measured by DELTA: each moves by exactly its own account,
    and by NOTHING of the other's."""
    before, after = world["before"], world["after"]

    d_comp_count = after["comped"]["count"] - before["comped"]["count"]
    d_comp_value = round(after["comped"]["monthly_value"] - before["comped"]["monthly_value"], 2)
    d_ext_count = after["externally_billed"]["count"] - before["externally_billed"]["count"]
    d_ext_value = round(after["externally_billed"]["monthly_value"]
                        - before["externally_billed"]["monthly_value"], 2)

    assert d_comp_count == 1, "the comped COUNT did not move by exactly the one comped account"
    assert d_comp_value == round(world["comped_gross"], 2), (
        f"the comped VALUE moved by {d_comp_value}, not by the comped account's gross "
        f"{world['comped_gross']} — if it moved by more, collected revenue is being reported "
        f"as a give-away")
    assert d_ext_count == 1
    assert d_ext_value == round(world["ext_collected"], 2)

    # The load-bearing negative: the two figures are DIFFERENT numbers, and neither absorbed the
    # other. A ``comped += external`` regression makes d_comp_value == gross + collected.
    assert d_comp_value != d_comp_value + d_ext_value, "the two roll-ups collapsed into one"
    assert after["comped"]["currency"] == after["externally_billed"]["currency"]
    assert after["comped"] is not after["externally_billed"]


def test_a_comped_account_is_visible_in_the_roster_and_never_reported_as_unknown(world):
    """A comp WAIVES; it does not HIDE, and it is not an unresolved amount. The per-account row
    must still be present, still flagged, still carry its reason and its cost, and must NOT be
    dressed up as the externally-billed axis.

    "Not hidden" matters more than it sounds: the give-away roster is the ONLY report built to
    answer "who is not paying us, and why". An account that renders as blank/unknown there is
    indistinguishable from a broken price resolver."""
    row = next(x for x in world["after"]["accounts"] if x["account_id"] == world["comped_aid"])

    assert row["comped"] is True
    assert row["comp_reason"] == COMP_REASON, "the comp reason was lost on the roster row"
    assert row["comp_until"] is None, "an indefinite comp must stay indefinite"
    assert row["comped_value"] == round(world["comped_gross"], 2), \
        "the roster no longer shows what this give-away COSTS"
    # The zero it renders is a DELIBERATE zero, not an unresolved one.
    assert row["monthly_total"] == 0.0 and isinstance(row["monthly_total"], float)
    assert row["exempt"] is False
    # ...and it is NOT the other axis.
    assert row["externally_billed"] is False
    assert row["externally_collected_value"] == 0.0
    assert row["external_processor"] is None and row["external_reference"] is None


def test_the_externally_billed_row_carries_no_comp_signature(world):
    """The mirror of the above, on the row that gets mistaken for a comp. A webhook has no
    authority to sign a write-off, so a collected account must carry NO comp reason at all —
    an empty ``comp_reason`` is what makes "who is on a freebie" answerable."""
    row = next(x for x in world["after"]["accounts"] if x["account_id"] == world["ext_aid"])

    assert row["externally_billed"] is True
    assert row["external_processor"] == "stripe"
    assert row["externally_collected_value"] == round(world["ext_collected"], 2)
    assert row["comped"] is False
    assert row["comped_value"] == 0.0
    assert row["comp_reason"] is None, "a PAYING customer was given a comp signature"


# ── B2. THE TRIAL LEVER LANDS IN THE DATABASE ─────────────────────────────────────

def test_extend_individual_trial_moves_the_date_the_DATABASE_holds(world):
    """🔴 A WRITE THAT RETURNS 200 AND DOES NOT LAND is exactly the failure this test exists to
    catch, and it is the one an endpoint-level assertion cannot see. The existing coverage
    (tests/test_saas_billing_quantity_and_comp.py::test_extend_trial_extends_from_the_later_of_
    now_and_current_end) asserts the SQL TEXT against a fake cursor — correct, and blind to a
    transaction that never commits, a column that moved, or a route that swallowed the result.

    So: drive the REAL operator route, then re-read ``individual_users.trial_ends_at`` from the
    database, and separately through ``plans.trial_status`` — the reader the serving path uses.

    The individual starts on a LAPSED trial (9 days ago). The documented contract is "extend from
    the LATER of now and the current end", so a lapsed trial is REVIVED for the full window
    rather than handed 30 days that are already in the past."""
    conn, uid = world["conn"], world["individual_uid"]
    resp = world["extend_response"]
    assert resp["ok"] is True and resp["status_code"] == 200, resp

    was = world["trial_before"]
    now_db = _individual_trial_ends_at(conn, uid)
    assert now_db is not None, "the trial date is NULL in the database — the write never landed"
    assert now_db > was, f"the stored trial did not move: was {was}, now {now_db}"

    # REVIVED, not back-dated: a lapsed trial extended by 30 days lands ~30 days in the FUTURE.
    now = datetime.now(timezone.utc)
    assert now_db > now, "a lapsed trial was 'extended' to a date still in the past"
    assert timedelta(days=29) < (now_db - now) < timedelta(days=31), \
        f"expected ~30 days of runway from NOW, got {now_db - now}"

    # ...and the READER the serving path uses agrees. The route's echo is not evidence.
    status = plans_mod.trial_status(uid)
    assert status["in_trial"] is True
    assert status["trial_ends_at"][:10] == now_db.isoformat()[:10]
    assert status["converts_on"] is not None
    assert resp["trial_ends_at"][:10] == now_db.isoformat()[:10], \
        "the route reported a date different from the one it stored"


def test_an_extended_individual_still_bills_NOTHING_and_is_counted_as_TRIALING(world):
    """A trialing individual must not start reporting a real bill, and must not report a
    zero-that-means-unknown either. ``plans.individual_revenue`` is the roll-up
    ``billing_overview`` consumes, so this is the money consequence of the lever:

      * ``monthly_total`` EXCLUDES the seat — nothing is owed during the trial;
      * ``trialing`` COUNTS it — it is present and accounted for, not missing;
      * ``trial_monthly_value`` carries the POST-trial price, so the $0 is explained by a real
        number sitting next to it rather than by an absence. That is the same discipline the comp
        uses: the zero is always accompanied by what it is costing."""
    rev = plans_mod.individual_revenue(world["house_aid"])

    price = plans_mod.individual_price_for("standard")
    assert price > 0.0, "the fixture needs a PRICED tier for the $0 to mean anything"
    assert rev["count"] == 1
    assert rev["monthly_total"] == 0.0, "a trialing individual was billed"
    assert rev["trialing"] == 1, "the extended individual stopped being counted as trialing"
    assert rev["trial_monthly_value"] == round(price, 2), \
        "the post-trial value vanished — the $0 is now unexplained"
    assert rev["tiers"] == {"standard": 1}
    assert rev["currency"] == ent.CURRENCY


def test_the_billing_overview_does_not_invent_a_bill_for_a_trialing_individual(world):
    """The same fact at the roster level. The Singles house is EXEMPT from tenant costing, so the
    trialing individual must add a HEAD to ``individuals.count`` and ZERO to
    ``individuals.monthly_total`` — and no phantom tenant line either."""
    before, after = world["before"], world["after"]
    assert after["individuals"]["count"] - before["individuals"]["count"] == 1
    assert round(after["individuals"]["monthly_total"]
                 - before["individuals"]["monthly_total"], 2) == 0.0, \
        "a trialing individual is being billed in the operator roll-up"

    row = next(x for x in after["accounts"] if x["account_id"] == world["house_aid"])
    assert row["exempt"] is True
    assert row["monthly_total"] is None, "the Singles house grew a phantom tenant bill"
    assert row["individual_monthly_total"] == 0.0
    assert row["in_mrr"] is False


def test_the_billing_overview_reports_how_many_individuals_are_trialing(world):
    """The trial roll-up the operator console asks for.

    THIS WAS RED WHEN IT WAS WRITTEN, and the defect was PRE-EXISTING (present at bf209cdb, so it
    did not arrive with the pricing-IA work). ``billing_overview`` accumulated both figures and
    then dropped them when it assembled the response; the console has always read
    ``individuals.trialing`` to render the "N on first-month-free trial" pill, so the pill could
    never render. An operator extended a trial and the revenue surface reported nothing.

    A trialing individual is a THIRD state — not a paying customer, and NOT an unresolved amount
    that should read "unknown". Losing it makes a trial cohort look like flat revenue on the one
    screen whose job is to say where the money is. Both keys are emitted now."""
    before, after = world["before"], world["after"]
    assert "trialing" in after["individuals"], \
        "individuals.trialing is missing from /admin/billing/overview"
    assert "trial_monthly_value" in after["individuals"]
    assert after["individuals"]["trialing"] - before["individuals"].get("trialing", 0) == 1
    assert round(after["individuals"]["trial_monthly_value"]
                 - before["individuals"].get("trial_monthly_value", 0.0), 2) == \
        round(plans_mod.individual_price_for("standard"), 2)


# ── B3. THE THREE CLOCKS STAY DISTINCT ────────────────────────────────────────────

def test_moving_the_individual_trial_moved_NEITHER_of_the_other_two_clocks(world):
    """Three trial clocks, all called "trial", two of them on the SAME ``accounts`` row:

      a. ``individual_users.trial_ends_at``  — the individual first-month-free. MOVES MONEY
         (``individual_revenue``) and POSTPONES DELETION (``saas/retention.py``).
      b. ``accounts.trial_ends_at``          — the TENANT account trial (migration 226). A
         BILLING MARKER ONLY; nothing reads it to grant capability.
      c. ``accounts.chatbot_trial_expires_at`` — the chat-agent FEATURE trial (migration 220),
         which DOES gate /v1/agent/chat.

    Cross-wiring (b) or (c) to the individual lever is a one-line mistake with two very different
    blast radii: extending (b) silently misreports the account's billing state, extending (c)
    HANDS OUT A GATED FEATURE from a button labelled "extend trial". The operator route moved (a)
    and only (a)."""
    conn = world["conn"]
    house = world["house_aid"]

    acct_after = accounts_mod.get_account_trial(house)
    chat_after = accounts_mod.get_chatbot_trial(house)

    assert acct_after["ends_at"] == world["account_trial_before"]["ends_at"], \
        "extending an INDIVIDUAL's trial moved the TENANT account trial (accounts.trial_ends_at)"
    assert acct_after["started_at"] == world["account_trial_before"]["started_at"]
    assert chat_after["expires_at"] == world["chatbot_trial_before"]["expires_at"], \
        "extending an INDIVIDUAL's trial moved the CHATBOT feature trial — that grants a gated " \
        "feature from the wrong lever"
    assert chat_after["active"] == world["chatbot_trial_before"]["active"]

    # Belt and braces, straight off the row: the two account-level columns are genuinely distinct
    # from each other as well, so nothing is aliasing one onto the other.
    row = _account_row(conn, house, "trial_ends_at", "chatbot_trial_expires_at", "trial_source")
    assert row["trial_ends_at"] is not None and row["chatbot_trial_expires_at"] is not None
    assert row["trial_ends_at"] != row["chatbot_trial_expires_at"]
    assert row["trial_source"] == "priceia-regression"


def test_the_individual_lever_refuses_an_ACCOUNT_id_outright(world):
    """The clocks are kept apart at the WRITER too, not only by luck of which id gets passed.
    ``plans.extend_trial`` looks the id up in ``public.individual_users``; a tenant/team account
    id is simply not there, so it is a 404 and NOTHING is written.

    This is the enforcement behind the deliberate design note at saas/plans.py:252 — there is no
    operator lever that extends a whole account's trial, because the whole-account give-away lever
    is the COMP, which is explicit, signed and separately audited. Passing an account id here must
    never be quietly absorbed into ``accounts.trial_ends_at``."""
    conn = world["conn"]
    aid = world["comped_aid"]
    before = _account_row(conn, aid, "trial_ends_at", "chatbot_trial_expires_at")

    out = plans_mod.extend_trial(aid, 30, reason="PRICEIA — must not be honoured",
                                 actor="operator", actor_user="op@example.invalid")
    assert out["ok"] is False and out["status_code"] == 404, out
    assert "individual" in out["reason"].lower()

    after = _account_row(conn, aid, "trial_ends_at", "chatbot_trial_expires_at")
    assert after == before, "the individual trial lever wrote to an ACCOUNT-level clock"


def test_the_comp_survived_the_whole_run_on_the_account_row(world):
    """Closing the loop on the account row itself, AFTER two full ``billing_overview`` passes, a
    trial extension and a refused extend against the same account. The comp is a stored human
    decision and nothing in a read path may erode it."""
    row = _account_row(world["conn"], world["comped_aid"],
                       "comped", "comp_reason", "comp_by", "comp_until", "externally_billed")
    assert row["comped"] is True
    assert row["comp_reason"] == COMP_REASON
    assert row["comp_by"] == "owner@example-operator.ca"
    assert row["comp_until"] is None
    assert row["externally_billed"] is False

    state = ent.comp_state(world["comped_aid"])
    assert state["active"] is True and state["expired"] is False
    view = ent.billing_view(world["comped_aid"])
    assert view["comped"] is True
    assert view["monthly_total"] == 0.0
    assert view["comped_value"] == view["gross_subtotal"] > 0.0
    assert len([ln for ln in view["lines"] if ln["kind"] == "comp"]) == 1


# ── FALSE-GREEN GUARD (must be last) ──────────────────────────────────────────────

def test_no_test_was_silently_skipped():
    """A DSN-gated suite that skips its DB half looks green. It is not. If this fails, the DB
    fixture never ran and NOTHING above about a real row, a real roll-up or a real trial write
    was actually verified."""
    assert _DB_RAN["value"] is True, (
        f"the DATABASE half of this suite never ran (DSN={TEST_DSN}) — every row / roll-up / "
        f"trial assertion was skipped and this run proves nothing")
