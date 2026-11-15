"""TWO SEAT-MOVEMENT PATHS THAT WROTE NOTHING TO THE RAIL THE CUSTOMER IS BILLED ON.

────────────────────────────────────────────────────────────────────────────────────
FINDING 1 — ``entitlements.schedule_downgrade`` WROTE THREE COLUMNS NOBODY EVER APPLIED
────────────────────────────────────────────────────────────────────────────────────
It set ``scheduled_seat_type`` / ``scheduled_effective`` / ``scheduled_by`` on
``public.account_users``, demanded a typed-name signature, wrote a ledger row, and the operator
console rendered "⚠ scheduled downgrade — expert → standard … effective <date>" with a cancel
button beside it. No applier existed anywhere in ``saas/``, ``src/`` or ``migrations/``. The
effective date arrived and the seat never moved: a signed, ledgered, rendered promise that could
not come true.

``entitlements.apply_due_downgrades()`` is the applier, and this file pins it as OUTCOMES read
back out of the DATABASE — never as a return value. A function that reports ``applied: [...]``
while ``account_users.seat_type`` still says ``expert`` is exactly the "success shape for a write
that did not happen" this codebase keeps paying for.

WHAT IT DELIBERATELY DOES **NOT** DO, pinned here so nobody "completes" it by accident: it moves
the seat ASSIGNMENT and NOT the licensed capacity. ``billing_view`` bills the tenant tiers on
``account_entitlements.quantity``, so the surplus licence is reported (``surplus_capacity``) and
left for a human to remove under signature. Auto-removing it would be an unauthorised money
movement in the customer's favour that also costs them their rate snapshot (migration 249) when
they re-buy.

────────────────────────────────────────────────────────────────────────────────────
FINDING 2 — A TRANSFER MOVED A SEAT INTO A TENANT WITHOUT TELLING STRIPE
────────────────────────────────────────────────────────────────────────────────────
``transfers.approve_and_execute`` re-associates an individual's ``account_users`` row onto a
corporate tenant. ``_team_billing_view`` bills Standard on ``max(licensed, OCCUPIED)``, so that
is a change in BILLABLE OCCUPANCY — the same class of drift that was already measured on
hiring/firing (revoke 4 seats → true bill CA$900, Stripe still collecting CA$950). The transfer
path had no push at all. Direction is UNDER-billing.

The fix routes through ``entitlements.sync_billable_seats`` — the SAME single seam
``saas/seats.py`` register/revoke/auto-provision uses. This file proves the transfer reaches
THAT function, not a second copy of the ordering/failure/logging logic.

────────────────────────────────────────────────────────────────────────────────────
HOW THIS SUITE IS WIRED
────────────────────────────────────────────────────────────────────────────────────
Real modules, real PostgreSQL, no cursor proxy: point ``DOWNGRADE_TEST_DSN`` (or
``POSTGRES_DSN``) at a throwaway database built by applying ``migrations/*.sql`` numbered ≥ 131.
The DB name must contain ``test`` — the same guard the sibling suites use, so a stray DSN cannot
have a suite writing into anything real.

⚠️ THE SKIP TRIPWIRE AT THE BOTTOM IS DELIBERATE. Without a DSN the entire DB lane skips and a
bare run looks like a clean green over an untested applier. ``test_the_db_lane_actually_ran``
goes RED in that case. A red tripwire is the correct outcome, not a defect — supply the DSN.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("_saas_env")

DSN = (os.environ.get("DOWNGRADE_TEST_DSN") or os.environ.get("POSTGRES_DSN") or "").strip()
_DB_RAN = {"yes": False}


def _dsn_usable() -> bool:
    return bool(DSN) and "test" in DSN.rsplit("/", 1)[-1].lower()


requires_db = pytest.mark.skipif(
    not _dsn_usable(),
    reason="set DOWNGRADE_TEST_DSN (or POSTGRES_DSN) to a throwaway database whose NAME contains "
           "'test'; see the tripwire at the bottom of this file",
)


@pytest.fixture(scope="module", autouse=True)
def _saas_env():
    old = {k: os.environ.get(k) for k in ("SAAS_MODE", "POSTGRES_DSN")}
    os.environ["SAAS_MODE"] = "1"
    if DSN:
        os.environ["POSTGRES_DSN"] = DSN
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture()
def ent():
    from saas import entitlements as _e
    return _e


@pytest.fixture()
def conn():
    import psycopg2
    c = psycopg2.connect(DSN)
    yield c
    c.close()


# ── fixture helpers ──────────────────────────────────────────────────────────────────

def _q(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall() if cur.description else None
    conn.commit()
    return rows


def _make_account(conn, *, kind: str = "tenant", slug_hint: str = "dg") -> str:
    aid = str(uuid.uuid4())
    slug = f"{slug_hint}-{aid[:8]}"
    _q(conn,
       "INSERT INTO public.accounts (account_id, name, slug, account_kind, seat_limit, "
       "term_months, expires_at) VALUES (%s, %s, %s, %s, 1000, 6, NOW() + INTERVAL '90 days')",
       (aid, f"Test {slug}", slug, kind))
    return aid


def _seat(conn, account_id: str, seat_type: str, *, is_base: bool = False) -> str:
    uid = str(uuid.uuid4())
    _q(conn,
       "INSERT INTO public.account_users (account_id, user_id, seat_type, is_base, status) "
       "VALUES (%s, %s, %s, %s, 'active')",
       (account_id, uid, seat_type, is_base))
    return uid


def _licence(conn, account_id: str, seat_type: str, qty: int, base: int = 0) -> None:
    _q(conn,
       "INSERT INTO public.account_entitlements (account_id, seat_type, quantity, base_quantity) "
       "VALUES (%s, %s, %s, %s) ON CONFLICT (account_id, seat_type) DO UPDATE "
       "SET quantity = EXCLUDED.quantity, base_quantity = EXCLUDED.base_quantity",
       (account_id, seat_type, qty, base))


def _park_schedule(conn, account_id: str, user_id: str, target: str, effective) -> None:
    """Write the schedule DIRECTLY. Deliberately not via schedule_downgrade(): that function's
    own guards are not what is under test here, and going through it would couple every case to
    a signature/ladder check that has nothing to do with whether the APPLIER works."""
    _q(conn,
       "UPDATE public.account_users SET scheduled_seat_type = %s, scheduled_effective = %s, "
       "scheduled_by = 'test-admin' WHERE account_id = %s AND user_id = %s",
       (target, effective, account_id, user_id))


def _seat_row(conn, account_id: str, user_id: str):
    return _q(conn,
              "SELECT seat_type, is_base, scheduled_seat_type, scheduled_effective, scheduled_by "
              "FROM public.account_users WHERE account_id = %s AND user_id = %s",
              (account_id, user_id))[0]


def _ledger(conn, account_id: str, action: str):
    return _q(conn,
              "SELECT seat_type, quantity, amount, note, signature FROM public.account_ledger "
              "WHERE account_id = %s AND action = %s", (account_id, action))


YESTERDAY = datetime.now(timezone.utc) - timedelta(days=1)
NEXT_YEAR = datetime.now(timezone.utc) + timedelta(days=365)


# ════════════════════════════════════════════════════════════════════════════════════
# 1 — THE APPLIER ACTUALLY MOVES THE SEAT
# ════════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_a_due_downgrade_moves_the_seat_and_clears_the_promise(conn, ent, monkeypatch):
    _DB_RAN["yes"] = True
    pushes = []
    monkeypatch.setattr(ent, "sync_billable_seats",
                        lambda aid, *, reason: pushes.append((aid, reason)))
    aid = _make_account(conn)
    _licence(conn, aid, "expert", 1)
    _licence(conn, aid, "standard", 5, base=5)
    uid = _seat(conn, aid, "expert")
    _park_schedule(conn, aid, uid, "standard", YESTERDAY)

    out = ent.apply_due_downgrades()

    # THE OUTCOME IS THE ROW, NOT THE RETURN VALUE.
    seat_type, is_base, sched, sched_eff, sched_by = _seat_row(conn, aid, uid)
    assert seat_type == "standard", (
        f"the seat is still {seat_type!r} — the applier reported {out} but did not move it")
    assert sched is None and sched_eff is None and sched_by is None, (
        "the schedule columns must be cleared in the SAME statement that moves the seat, or the "
        "next sweep applies it again")
    assert any(a["user_id"] == uid and a["ok"] for a in out["applied"])

    rows = _ledger(conn, aid, "apply_downgrade")
    assert len(rows) == 1, f"expected exactly one apply_downgrade ledger row, got {rows}"
    seat_t, qty, amount, note, signature = rows[0]
    assert float(amount) == 0.0, "applying a scheduled assignment is not a new charge"
    assert signature is None, (
        "no human typed a name here — the authorisation is the ORIGINAL signed "
        "schedule_downgrade row. Forging a signature on a machine-applied row would make the "
        "ledger lie about who authorised what.")
    assert "expert" in note and "standard" in note

    assert pushes == [(aid, "apply_downgrade")], (
        f"the applier must push the new billable counts through the ONE seam "
        f"(entitlements.sync_billable_seats); got {pushes}")


@requires_db
def test_the_sweep_is_idempotent_a_second_run_finds_nothing(conn, ent, monkeypatch):
    _DB_RAN["yes"] = True
    monkeypatch.setattr(ent, "sync_billable_seats", lambda aid, *, reason: None)
    aid = _make_account(conn)
    _licence(conn, aid, "expert", 1)
    _licence(conn, aid, "standard", 5, base=5)
    uid = _seat(conn, aid, "expert")
    _park_schedule(conn, aid, uid, "standard", YESTERDAY)

    first = ent.apply_due_downgrades()
    assert any(a["user_id"] == uid for a in first["applied"])
    second = ent.apply_due_downgrades()
    assert not any(a.get("user_id") == uid for a in second["applied"])
    assert not any(r.get("user_id") == uid for r in second["refused"])
    assert _seat_row(conn, aid, uid)[0] == "standard"


@requires_db
def test_a_schedule_that_is_not_yet_due_is_left_completely_alone(conn, ent, monkeypatch):
    _DB_RAN["yes"] = True
    monkeypatch.setattr(ent, "sync_billable_seats", lambda aid, *, reason: None)
    aid = _make_account(conn)
    _licence(conn, aid, "expert", 1)
    _licence(conn, aid, "standard", 5, base=5)
    uid = _seat(conn, aid, "expert")
    _park_schedule(conn, aid, uid, "standard", NEXT_YEAR)

    ent.apply_due_downgrades()

    seat_type, _b, sched, _e, _by = _seat_row(conn, aid, uid)
    assert seat_type == "expert", "a future-dated downgrade must NOT apply early"
    assert sched == "standard", "and its promise must survive intact"


# ════════════════════════════════════════════════════════════════════════════════════
# 2 — REFUSALS ARE LOUD AND NEVER SILENTLY CLEAR A CUSTOMER'S SIGNED SCHEDULE
# ════════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_no_licence_in_the_target_tier_refuses_and_leaves_the_promise_pending(conn, ent,
                                                                             monkeypatch):
    """Provisioning capacity to satisfy the move would be an unauthorised charge; clearing the
    schedule would delete a signed promise. The only honest third option is to refuse, leave it
    visible, and retry."""
    _DB_RAN["yes"] = True
    pushes = []
    monkeypatch.setattr(ent, "sync_billable_seats",
                        lambda aid, *, reason: pushes.append((aid, reason)))
    aid = _make_account(conn)                     # tenant kind: standard IS licence-counted
    _licence(conn, aid, "expert", 1)
    _licence(conn, aid, "standard", 0)            # nothing free to move into
    uid = _seat(conn, aid, "expert")
    _park_schedule(conn, aid, uid, "standard", YESTERDAY)

    out = ent.apply_due_downgrades()

    seat_type, _b, sched, sched_eff, _by = _seat_row(conn, aid, uid)
    assert seat_type == "expert", "an unlicensed move must not happen"
    assert sched == "standard" and sched_eff is not None, (
        "the schedule must SURVIVE a refusal — a customer's signed downgrade vanishing with no "
        "trace is the same silent loss the applier exists to end")
    assert any(r["user_id"] == uid and not r["ok"] for r in out["refused"])
    assert not _ledger(conn, aid, "apply_downgrade"), "a refusal writes no apply row"
    assert pushes == [], "nothing moved, so nothing is pushed to Stripe"

    # …and the entitlement was NOT topped up behind the customer's back.
    q = _q(conn, "SELECT quantity FROM public.account_entitlements "
                 "WHERE account_id = %s AND seat_type = 'standard'", (aid,))
    assert int(q[0][0]) == 0


@requires_db
def test_a_seat_revoked_after_scheduling_is_refused_not_resurrected(conn, ent, monkeypatch):
    _DB_RAN["yes"] = True
    monkeypatch.setattr(ent, "sync_billable_seats", lambda aid, *, reason: None)
    aid = _make_account(conn)
    _licence(conn, aid, "expert", 1)
    _licence(conn, aid, "standard", 5, base=5)
    uid = _seat(conn, aid, "expert")
    _park_schedule(conn, aid, uid, "standard", YESTERDAY)
    _q(conn, "UPDATE public.account_users SET status = 'revoked' "
             "WHERE account_id = %s AND user_id = %s", (aid, uid))

    out = ent.apply_due_downgrades()

    assert not any(a.get("user_id") == uid for a in out["applied"])
    assert _seat_row(conn, aid, uid)[0] == "expert"


@requires_db
def test_a_base_seat_is_refused_because_base_seats_are_fixed(conn, ent, monkeypatch):
    _DB_RAN["yes"] = True
    monkeypatch.setattr(ent, "sync_billable_seats", lambda aid, *, reason: None)
    aid = _make_account(conn)
    _licence(conn, aid, "standard", 5, base=5)
    _licence(conn, aid, "advanced", 5)
    uid = _seat(conn, aid, "expert", is_base=True)   # contrived, but is_base is the gate
    _park_schedule(conn, aid, uid, "advanced", YESTERDAY)

    out = ent.apply_due_downgrades()

    assert _seat_row(conn, aid, uid)[0] == "expert"
    assert any(r["user_id"] == uid and "base" in r["reason"] for r in out["refused"])


@requires_db
def test_a_schedule_that_is_no_longer_a_downgrade_is_refused_not_applied(conn, ent, monkeypatch):
    """Months can pass between scheduling and the effective date. If the seat was moved DOWN in
    the meantime, applying the old target could be an UPGRADE — a charge nobody authorised."""
    _DB_RAN["yes"] = True
    monkeypatch.setattr(ent, "sync_billable_seats", lambda aid, *, reason: None)
    aid = _make_account(conn)
    _licence(conn, aid, "standard", 5, base=5)
    _licence(conn, aid, "advanced", 5)
    uid = _seat(conn, aid, "standard")               # already at the bottom
    _park_schedule(conn, aid, uid, "advanced", YESTERDAY)

    out = ent.apply_due_downgrades()

    assert _seat_row(conn, aid, uid)[0] == "standard", "must never move a seat UP"
    assert any(r["user_id"] == uid and "no longer a downgrade" in r["reason"]
               for r in out["refused"])


@requires_db
def test_an_undated_schedule_is_counted_and_never_guessed_at(conn, ent, monkeypatch):
    """``scheduled_effective`` is copied from ``accounts.expires_at``, which is NULLABLE — so an
    account with no contract end banks a promise with no date. Inventing one would be inventing
    a money event. It is COUNTED and reported instead."""
    _DB_RAN["yes"] = True
    monkeypatch.setattr(ent, "sync_billable_seats", lambda aid, *, reason: None)
    aid = _make_account(conn)
    _licence(conn, aid, "expert", 1)
    _licence(conn, aid, "standard", 5, base=5)
    uid = _seat(conn, aid, "expert")
    _park_schedule(conn, aid, uid, "standard", None)

    out = ent.apply_due_downgrades()

    assert out["undated"] >= 1, "an undated schedule must be surfaced, not silently ignored"
    assert _seat_row(conn, aid, uid)[0] == "expert"
    assert _seat_row(conn, aid, uid)[2] == "standard"


# ════════════════════════════════════════════════════════════════════════════════════
# 3 — THE APPLIER IS ACTUALLY SCHEDULED (a function nobody calls is the original defect)
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_applier_is_wired_into_the_background_worker():
    """The whole finding was "the columns are written and nothing applies them". Shipping an
    applier that nothing CALLS reproduces it one layer up. It rides the existing
    ``_retention_worker`` loop (daily-grain policy, same as the retention sweep) rather than a
    second scheduler."""
    main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "src", "api", "main.py")
    with open(main_py, encoding="utf-8") as fh:
        src = fh.read()
    assert "apply_due_downgrades()" in src, (
        "src/api/main.py never calls entitlements.apply_due_downgrades — the applier is dead "
        "code and the scheduled downgrade still never happens")
    worker = src.split("async def _retention_worker", 1)
    assert len(worker) == 2, "the _retention_worker loop moved; re-point this assertion"
    body = worker[1].split("asyncio.create_task(_retention_worker())", 1)[0]
    assert "apply_due_downgrades()" in body, (
        "the applier call is in main.py but NOT inside the _retention_worker loop, so it runs "
        "once at import (or not at all) instead of on the poll")


# ════════════════════════════════════════════════════════════════════════════════════
# 4 — A TRANSFER PUSHES THE MOVED OCCUPANCY TO STRIPE, THROUGH THE ONE SEAM
# ════════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_executing_a_transfer_pushes_billable_seats_for_both_accounts(conn, monkeypatch):
    """A seat landing on a tenant is a change in billable occupancy. Before this fix the transfer
    path had ZERO Stripe pushes; the target went on being billed for the occupancy it had before
    the seat arrived."""
    _DB_RAN["yes"] = True
    from saas import entitlements as _ent
    from saas import transfers as _transfers

    pushes = []
    monkeypatch.setattr(_ent, "sync_billable_seats",
                        lambda aid, *, reason: pushes.append((aid, reason)))

    house = _make_account(conn, kind="individual_house", slug_hint="house")
    target = _make_account(conn, kind="team", slug_hint="tgt")
    uid = _seat(conn, house, "standard")
    ind_id = str(uuid.uuid4())
    _q(conn,
       "INSERT INTO public.individual_users (id, user_id, account_id, email, password_hash, "
       "plan, status, tenant_context) VALUES (%s, %s, %s, %s, 'x', 'individual', 'active', "
       "'house')", (ind_id, uid, house, f"t{uid[:8]}@example.test"))
    req_id = str(uuid.uuid4())
    _q(conn,
       "INSERT INTO public.transfer_requests (id, individual_id, user_id, from_account_id, "
       "target_identifier, target_account_id, target_account_name, status, consent_ack) "
       "VALUES (%s, %s, %s, %s, %s, %s, 'Target', 'pending', TRUE)",
       (req_id, ind_id, uid, house, "target", target))

    out = _transfers.approve_and_execute(req_id, operator_id="op-test")
    assert out.get("ok"), f"the transfer itself failed, so this proves nothing: {out}"

    # THE SEAT REALLY MOVED (verify the READ, not the return value)…
    moved = _q(conn, "SELECT account_id::text FROM public.account_users WHERE user_id = %s",
               (uid,))
    assert moved and moved[0][0] == target

    # …AND BOTH SIDES WERE PUSHED, THROUGH sync_billable_seats.
    pushed = {aid for aid, _r in pushes}
    assert target in pushed, (
        "the TARGET tenant gained an active seat and Stripe was not told — this is the "
        "under-billing direction the seat-sync seam exists to close")
    assert house in pushed, "the source account lost an occupant and was not pushed either"
    assert all(r.startswith("transfer_execute") for _a, r in pushes), (
        f"every push must carry a reason naming the transfer, for reconcile(); got {pushes}")


def test_the_transfer_path_uses_the_shared_seam_and_not_a_second_copy():
    """``sync_billable_seats`` exists so the ordering (DB first, Stripe second), the failure
    posture and the logging live in ONE place. A transfer reaching ``stripe_seat_sync`` directly
    would be a second implementation that drifts."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "saas", "transfers.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "sync_billable_seats" in src
    assert "stripe_seat_sync" not in src.replace(
        "stripe_seat_sync.reconcile()", ""), (
        "saas/transfers.py reaches into stripe_seat_sync directly instead of going through "
        "entitlements.sync_billable_seats (a bare mention inside the CRIT log's 'run "
        "stripe_seat_sync.reconcile()' hint is allowed and is excluded above)")


# ════════════════════════════════════════════════════════════════════════════════════
# 5 — SKIP TRIPWIRE
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_db_lane_actually_ran():
    """RED when the DB lane silently skipped. A suite that reports "all passed" over an applier
    it never executed is worse than one that fails."""
    assert _DB_RAN["yes"], (
        "the DATABASE lane of this suite did not run — every applier and transfer test was "
        "SKIPPED. Set DOWNGRADE_TEST_DSN (or POSTGRES_DSN) to a throwaway database whose name "
        "contains 'test', built by applying migrations/*.sql numbered >= 131. This tripwire is "
        "the intended behaviour, not a defect."
    )
