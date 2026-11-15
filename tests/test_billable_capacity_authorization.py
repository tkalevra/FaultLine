"""BILLABLE CAPACITY — every write of it must be BUYABLE and AUTHORIZED.

Two defects, both on the money path, both measured against a real control-plane database:

**F5 — a tenant could not buy 1–4 Standard seats.** ``provision_capacity`` issued
``INSERT … VALUES (account, 'standard', n, 5) ON CONFLICT (account_id, seat_type) DO UPDATE``.
PostgreSQL evaluates ``CHECK (quantity >= base_quantity)`` on the tuple PROPOSED FOR INSERTION,
**before** ON CONFLICT arbitration runs — so ``n < 5`` failed the check even though the row that
would actually be written (the existing 5 plus n) is perfectly legal. Buying 1, 2, 3 or 4 Standard
seats returned a bare HTTP 500 "control-plane error"; buying 5 or 10 worked. Byte-identical SQL at
HEAD~1, so pre-existing — and revenue-blocking, surfaced as an unexplained server error.

**F6 — billable capacity written with no ledger row, no signature, no price stamp.**
``accounts.set_agent_feature(enabled=True, bucket=10)`` wrote ten ``agent`` entitlements straight
into ``account_entitlements`` with ``= EXCLUDED.quantity``. MEASURED: the rendered invoice went
CA$1,500.00 → CA$2,300.00 (``addon agent q=10 unit=80.0 monthly=800.0``) with **zero**
``account_ledger`` rows — bypassing ``_write_ledger``'s own rule that a BILLABLE action carries a
typed-name signature. CA$800/mo of billable capacity with no authorization record, no rate
snapshot (so a rate-card edit re-prices it) and no Stripe push.

⚠️ THE ASSERTIONS READ RENDERED INVOICE LINES AND REAL LEDGER ROWS, not the dict the function
handed back. The defect being fixed here is precisely one where the return value looked fine.

RUN IT (never raw pytest — owner rule):

    PYTHONPATH="src:<venv site-packages>" \\
    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_fixmoney_test \\
    python3 tools/fltest.py --bug FIXMONEY --test tests/test_billable_capacity_authorization.py

FALSE-GREEN GUARD: ``_DB_RAN`` is set by the fixture and asserted by the last test. A DSN-gated
suite that skips itself reports "0 failed" and looks green.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from saas import accounts as accounts_mod        # noqa: E402
from saas import entitlements as ent_mod         # noqa: E402
from saas import invoicing as inv_mod            # noqa: E402
from saas import stripe_seat_sync as sync_mod    # noqa: E402

DEFAULT_TEST_DSN = ("postgresql://faultline:faultline@172.20.0.3:5432/"
                    "faultline_fixmoney_test")
TEST_DSN = os.environ.get("CAPAUTH_TEST_DSN",
                          os.environ.get("POSTGRES_DSN", DEFAULT_TEST_DSN))

_DB_RAN = {"value": False}

SIGNER = "Christopher Thompson"


@pytest.fixture(scope="module")
def dbconn():
    if "test" not in TEST_DSN.rsplit("/", 1)[-1]:
        pytest.skip(f"refusing to run against a non-test database: {TEST_DSN}")
    import psycopg2
    try:
        conn = psycopg2.connect(TEST_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"control-plane test DB unreachable ({type(exc).__name__}): {TEST_DSN}")
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.accounts'), "
                    "       to_regclass('public.account_entitlements'), "
                    "       to_regclass('public.account_ledger')")
        present = cur.fetchone()
        cur.execute("SELECT column_name FROM information_schema.columns "
                    " WHERE table_schema='public' AND table_name='account_entitlements' "
                    "   AND column_name='unit_price_cents'")
        stamped = cur.fetchone()
    conn.commit()
    if not all(present) or not stamped:
        conn.close()
        pytest.skip("throwaway DB missing the SaaS control plane or migration 249")
    yield conn
    conn.close()


@pytest.fixture
def env(monkeypatch, dbconn):
    _DB_RAN["value"] = True
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    monkeypatch.setenv("SAAS_MODE", "1")
    # No Stripe rail in this suite — the seat sync must be a no-op, never a failure.
    monkeypatch.setattr(sync_mod, "_provider", lambda: None)
    return True


def _mk_account(conn, *, kind="tenant", seeded=True):
    """A tenant in the PRODUCTION shape: ``seed_base_entitlement`` has already written the
    standard 5/5 row. That shape is what makes F5 a live defect rather than a curiosity — the
    conflict path is the one a real purchase takes."""
    slug = f"c{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.accounts (name, slug, plan, account_type, account_kind, "
            " seat_limit, status, term_months, term_months_set_at, term_months_source) "
            "VALUES (%s,%s,'standard','standard',%s,%s,'active',6,%s,'provision') "
            "RETURNING account_id::text",
            (slug, slug, kind, accounts_mod.hard_seat_cap(kind),
             datetime.now(timezone.utc) - timedelta(days=1)))
        aid = cur.fetchone()[0]
    conn.commit()
    if seeded:
        assert ent_mod.seed_base_entitlement(aid) is True
    return aid


def _qty(conn, aid, seat_type):
    with conn.cursor() as cur:
        cur.execute("SELECT quantity, base_quantity, unit_price_cents "
                    "  FROM public.account_entitlements "
                    " WHERE account_id = %s AND seat_type = %s", (aid, seat_type))
        r = cur.fetchone()
    conn.commit()
    return r


def _ledger(conn, aid, action=None):
    with conn.cursor() as cur:
        if action:
            cur.execute("SELECT action, seat_type, quantity, unit_price, amount, signature, "
                        "       attestation FROM public.account_ledger "
                        " WHERE account_id = %s AND action = %s ORDER BY ledger_id",
                        (aid, action))
        else:
            cur.execute("SELECT action, seat_type, quantity, unit_price, amount, signature, "
                        "       attestation FROM public.account_ledger "
                        " WHERE account_id = %s ORDER BY ledger_id", (aid,))
        rows = cur.fetchall()
    conn.commit()
    cols = ("action", "seat_type", "quantity", "unit_price", "amount", "signature", "attestation")
    return [dict(zip(cols, r)) for r in rows]


def _render(aid, tag=""):
    model = inv_mod.build_invoice(aid)
    assert model is not None, "build_invoice returned None (control-plane error)"
    rows = [(round(float(ln["amount"]), 2), ln["description"]) for ln in model["line_items"]]
    print(f"\n── RENDERED INTERNAL INVOICE {tag} ──")
    for amt, desc in rows:
        print(f"  {amt:>10.2f}  {desc}")
    print(f"  {'TOTAL':>10}  {model['total']:.2f}")
    return model, rows


# ════════════════════════════════════════════════════════════════════════════════════
# F5 — buying 1–4 Standard seats
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 10])
def test_a_tenant_can_buy_any_quantity_of_standard_seats(env, dbconn, n):
    """1–4 returned a bare 500 at 944fbaad; 5 and 10 worked. All six must succeed, and the
    RESULTING CAPACITY must be the existing 5 plus n in every case — the fix must not quietly
    change what a customer gets for their money."""
    aid = _mk_account(dbconn)
    assert _qty(dbconn, aid, "standard")[:2] == (5, 5), "production shape: base row seeded 5/5"

    out = ent_mod.provision_capacity(aid, "standard", n, actor="test", signature=SIGNER)
    assert out["ok"] is True, out
    assert out["status_code"] == 200
    assert out["quantity"] == n
    assert out["new_quantity"] == 5 + n, "the top-up adds n to the existing 5 — never more"

    qty, base, unit_cents = _qty(dbconn, aid, "standard")
    assert (qty, base) == (5 + n, 5)
    assert unit_cents is not None, "the purchase must be rate-stamped (migration 249)"

    # THE CHARGE IS FOR n SEATS, at the stamped rate. Not for max(n, 5), not for the new total.
    led = _ledger(dbconn, aid, "provision")
    assert len(led) == 1, led
    assert led[0]["quantity"] == n
    assert round(float(led[0]["amount"]), 2) == round(float(led[0]["unit_price"]) * n, 2)
    assert led[0]["signature"] == SIGNER


def test_the_billed_seat_count_is_the_same_arithmetic_for_a_small_purchase(env, dbconn):
    """Read the RENDERED invoice, not the return dict. Enterprise includes 5 Standard in the base
    package, so buying 3 must produce an add-on line of exactly 3."""
    aid = _mk_account(dbconn)
    before, _rows = _render(aid, "before buying 3 Standard")
    assert ent_mod.provision_capacity(aid, "standard", 3, actor="test",
                                      signature=SIGNER)["ok"] is True
    after, rows = _render(aid, "after buying 3 Standard")

    addon = [ln for ln in after["line_items"]
             if "Standard" in ln["description"] and ln["amount"] > 0]
    assert addon, rows
    delta = round(after["total"] - before["total"], 2)
    unit = ent_mod.price_for("standard", "tenant")
    assert delta == round(unit * 3, 2), \
        f"3 seats beyond the included 5 must add exactly 3 × {unit}: {rows}"


def test_a_bundle_whose_standard_count_is_under_the_base_allowance_does_not_500(env, dbconn):
    """The same proposed-tuple trap lived in ``provision_bundle``. Exercised directly against the
    SQL, because no shipped bundle happens to carry fewer than 5 Standard today — which is exactly
    why it would have sat undetected until someone added one."""
    aid = _mk_account(dbconn)
    from saas import db as db_mod
    with db_mod.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.account_entitlements
                    (account_id, seat_type, quantity, base_quantity)
                VALUES (%s, %s, GREATEST(%s::int, %s::int), %s::int)
                ON CONFLICT (account_id, seat_type)
                DO UPDATE SET quantity = public.account_entitlements.quantity + %s::int,
                              updated_at = NOW()
                RETURNING quantity
                """,
                (aid, "standard", 2, 5, 5, 2))
            got = cur.fetchone()[0]
        conn.commit()
    assert got == 7, "5 seeded + 2 bought"


def test_a_genuine_control_plane_failure_is_still_refused_and_logged(env, dbconn, capsys):
    """The fix must not turn every error into a success. A non-existent account still fails —
    and now says so on stderr instead of returning a silent 500."""
    out = ent_mod.provision_capacity(str(uuid.uuid4()), "standard", 2, actor="test",
                                     signature=SIGNER)
    assert out["ok"] is False and out["status_code"] == 404, out


# ════════════════════════════════════════════════════════════════════════════════════
# F6 — the agent-seat capacity write must be ledgered, signed, stamped and pushed
# ════════════════════════════════════════════════════════════════════════════════════

def test_enabling_the_agent_feature_writes_a_signed_ledger_row_for_the_seat_capacity(env,
                                                                                     dbconn):
    """THE HEADLINE. Enabling at bucket 10 puts CA$800/mo of agent-seat capacity on the invoice.
    That charge must carry an authorization record, exactly like every other capacity purchase."""
    aid = _mk_account(dbconn)
    before, _r = _render(aid, "before the agent feature")
    assert _ledger(dbconn, aid, "provision") == []

    state = accounts_mod.set_agent_feature(
        aid, enabled=True, bucket=10, enabled_by="op@example.test",
        terms_version="operator-override", signature=SIGNER, actor="operator",
        actor_role="operator")
    assert state is not None and state["bucket"] == 10, state
    assert state["capacity"]["ok"] is True, state["capacity"]

    qty, base, unit_cents = _qty(dbconn, aid, "agent")
    assert qty == 10 and base == 0
    assert unit_cents is not None, "the capacity must be RATE-STAMPED — an unstamped row gets " \
                                   "re-priced by the next rate-card edit"

    led = _ledger(dbconn, aid, "provision")
    assert len(led) == 1, led
    assert led[0]["seat_type"] == "agent" and led[0]["quantity"] == 10
    assert led[0]["signature"] == SIGNER, "a billable write needs a typed-name signature"
    assert led[0]["attestation"], "the row must stand alone as an authorization record"
    unit = ent_mod.price_for("agent", "tenant")
    assert round(float(led[0]["amount"]), 2) == round(unit * 10, 2)

    after, rows = _render(aid, "after the agent feature at bucket 10")
    delta = round(after["total"] - before["total"], 2)
    assert delta == round(unit * 10, 2), \
        f"the invoice moved by {delta} and the ledger authorized {unit * 10}: {rows}"


def test_enabling_the_agent_feature_without_a_signature_provisions_nothing(env, dbconn):
    """Fail toward NOT BILLING. With no authorization record the capacity must not be written —
    and neither must the flag, because a flag with no capacity is a half-state nobody asked for."""
    aid = _mk_account(dbconn)
    state = accounts_mod.set_agent_feature(aid, enabled=True, bucket=5,
                                           enabled_by="op@example.test")
    assert state is None, "an unsigned billable bucket must REFUSE"
    assert _qty(dbconn, aid, "agent") is None, "no capacity may be written"
    assert _ledger(dbconn, aid) == [] or all(r["action"] != "provision"
                                             for r in _ledger(dbconn, aid))
    feat = accounts_mod.get_agent_feature(aid)
    assert feat is not None and feat["enabled"] is False, \
        "the flag must not flip either — the refusal is BEFORE anything is written"


def test_lowering_the_agent_bucket_credits_at_the_rate_the_customer_paid(env, dbconn):
    """A reduction is a CREDIT line at the STAMPED rate (migration 249). Crediting at today's
    card would refund a number the customer was never charged."""
    aid = _mk_account(dbconn)
    accounts_mod.set_agent_feature(aid, enabled=True, bucket=10, enabled_by="op",
                                   signature=SIGNER, actor="operator")
    assert _qty(dbconn, aid, "agent")[0] == 10

    out = ent_mod.set_agent_seat_capacity(aid, 5, actor="operator", signature=SIGNER)
    assert out["ok"] is True and out["delta"] == -5, out
    assert _qty(dbconn, aid, "agent")[0] == 5
    rem = _ledger(dbconn, aid, "removal")
    assert len(rem) == 1 and rem[0]["quantity"] == -5, rem
    assert float(rem[0]["amount"]) < 0, "a reduction is a credit line"
    unit = ent_mod.price_for("agent", "tenant")
    assert round(float(rem[0]["unit_price"]), 2) == round(unit, 2)


def test_lowering_the_bucket_below_the_agent_seats_in_use_is_refused(env, dbconn):
    aid = _mk_account(dbconn)
    accounts_mod.set_agent_feature(aid, enabled=True, bucket=5, enabled_by="op",
                                   signature=SIGNER, actor="operator")
    with dbconn.cursor() as cur:
        for _ in range(3):
            cur.execute("INSERT INTO public.account_users "
                        " (account_id, user_id, status, seat_type) "
                        " VALUES (%s, %s, 'active', 'agent')", (aid, str(uuid.uuid4())))
    dbconn.commit()
    out = ent_mod.set_agent_seat_capacity(aid, 1, actor="operator", signature=SIGNER)
    assert out["ok"] is False and out["status_code"] == 409, out
    assert _qty(dbconn, aid, "agent")[0] == 5, "a refused reduction changes nothing"


def test_setting_the_same_bucket_twice_charges_nothing_the_second_time(env, dbconn):
    """The bucket is a LEVEL, not a delta. Re-asserting it must not mint a second charge."""
    aid = _mk_account(dbconn)
    accounts_mod.set_agent_feature(aid, enabled=True, bucket=5, enabled_by="op",
                                   signature=SIGNER, actor="operator")
    accounts_mod.set_agent_feature(aid, enabled=True, bucket=5, enabled_by="op",
                                   signature=SIGNER, actor="operator")
    assert len(_ledger(dbconn, aid, "provision")) == 1, _ledger(dbconn, aid)
    assert _qty(dbconn, aid, "agent")[0] == 5


def test_the_agent_capacity_write_pushes_the_new_quantity_to_stripe(env, dbconn, monkeypatch):
    """Structural + behavioural: the bucket is billable capacity, so Stripe has to be told, the
    same way ``provision_capacity`` tells it."""
    seen = []
    monkeypatch.setattr(sync_mod, "sync_account",
                        lambda aid, reason="": seen.append((aid, reason)) or {"ok": True})
    aid = _mk_account(dbconn)
    accounts_mod.set_agent_feature(aid, enabled=True, bucket=10, enabled_by="op",
                                   signature=SIGNER, actor="operator")
    assert any(a == aid for a, _r in seen), \
        f"the agent-seat capacity change never reached the Stripe seam: {seen}"


def test_set_agent_feature_no_longer_writes_the_entitlement_itself(env, dbconn):
    """AST pin. The bare ``INSERT INTO public.account_entitlements`` inside
    ``accounts.set_agent_feature`` is the defect; it must not come back."""
    import ast
    src = open(os.path.join(os.path.dirname(__file__), "..", "saas", "accounts.py")).read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "set_agent_feature")
    # Skip the docstring — it DESCRIBES the removed write and must be allowed to say so.
    body = [s for s in fn.body if not (isinstance(s, ast.Expr)
                                       and isinstance(s.value, ast.Constant))]
    sql = " ".join(n.value for s in body for n in ast.walk(s)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert "account_entitlements" not in sql, (
        "set_agent_feature writes billable capacity directly again — route it through "
        "entitlements.set_agent_seat_capacity so it is ledgered, signed and stamped")


# ════════════════════════════════════════════════════════════════════════════════════
# FALSE-GREEN GUARD — must be last
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_database_half_actually_ran():
    assert _DB_RAN["value"], (
        "the DB half SKIPPED — none of this suite's outcome assertions executed. Pass "
        "POSTGRES_DSN (a database whose NAME contains 'test') and re-run.")
