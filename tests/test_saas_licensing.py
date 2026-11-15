"""SaaS Phase 9 — per-account (per-SEAT) licensing model: entitlements + billing ledger.

Migration 139. Covers the product-owner decision:
  * seat_type is PER-SEAT (account_users.seat_type); the tenant holds a MIX of typed seats.
  * effective_ingest keys off the SEAT's own type (agent seat lockable; others always-on;
    tenant account_type IRRELEVANT).
  * upgrade-only ladder (standard<advanced<expert<agent) — a downgrade is refused.
  * base-5 Standard: included, immutable, not upgradeable.
  * per-type entitlement CAP (no capacity → clear "provision one first").
  * ledger is append-only + a SIGNED row per provisioning action (typed name authorization).
  * contract term/expiry surfaced; CAD currency; tax operator-settable (off).

Fake control-plane DB (no live server needed) exercises the REAL entitlements SQL branches.
Run: python3 -m pytest tests/test_saas_licensing.py -q  (tests/ gitignored → git add -f)
"""

import contextlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import accounts as saas_accounts
from saas import admin as saas_admin
from saas import agent_mode as saas_agent_mode
from saas import entitlements as ent
from saas import seats as saas_seats
from saas import tenant_console as tc
from saas import dashboard_auth as auth


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UA = "11111111-1111-4111-8111-111111111111"
UB = "22222222-2222-4222-8222-222222222222"
KEY_A = "flk_aaaaaaaa_secretA"
OP_KEY = "op-super-secret-key"
SIG = "Jane Operator"


# ════════════════════════════════════════════════════════════════════════════════
# PURE catalog / ladder / pricing (no DB)
# ════════════════════════════════════════════════════════════════════════════════

def test_ladder_and_rank():
    assert ent.LADDER == ("standard", "advanced", "expert", "agent")
    assert ent.rank("standard") < ent.rank("advanced") < ent.rank("expert") < ent.rank("agent")
    assert ent.rank("bogus") == 0


@pytest.mark.parametrize("cur,new,ok", [
    ("standard", "agent", True), ("advanced", "expert", True), ("expert", "agent", True),
    ("agent", "standard", False), ("expert", "advanced", False), ("agent", "agent", False),
    ("standard", "standard", False),
])
def test_can_upgrade_is_strictly_up(cur, new, ok):
    assert ent.can_upgrade(cur, new) is ok


def test_prices_and_env_override(monkeypatch):
    assert ent.price_for("standard") == 10.0
    assert ent.price_for("agent") == 80.0        # owner-ratified: $100 → $80
    monkeypatch.setenv("SAAS_PRICE_AGENT", "175")
    assert ent.price_for("agent") == 175.0


def test_bundle_catalog_and_totals():
    cat = {b["bundle_id"]: b for b in ent.bundle_catalog()}
    assert "agent_x1" in cat and "expert_x5" in cat
    # agent_x1 has no flat price → Σ(unit×qty) = 80 (agent seat now $80)
    assert cat["agent_x1"]["price"] == 80.0
    # expert_x5 flat override = 200
    assert cat["expert_x5"]["price"] == 200.0


def test_currency_is_cad():
    assert ent.CURRENCY == "CAD"


def test_attestation_string_has_signer_and_end():
    s = ent.build_attestation("Jane Doe", "jdoe", None)
    assert "Jane Doe" in s and "jdoe" in s and "valid until" in s


# ════════════════════════════════════════════════════════════════════════════════
# effective_ingest keys off the SEAT's own type (tenant type IRRELEVANT)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("seat_type", ["standard", "advanced", "expert"])
@pytest.mark.parametrize("seat_ing", [True, False])
def test_non_agent_seat_always_on(seat_type, seat_ing):
    # A non-agent SEAT is always ingest-on when active — regardless of its per-seat flag AND
    # regardless of the tenant account_type.
    assert saas_agent_mode.effective_ingest(
        {"status": "active", "account_type": "agent"},
        {"seat_type": seat_type, "ingest_enabled": seat_ing},
    ) is True


@pytest.mark.parametrize("seat_ing,expected", [(True, True), (False, False)])
def test_agent_seat_honors_its_own_flag(seat_ing, expected):
    assert saas_agent_mode.effective_ingest(
        {"status": "active", "account_type": "standard"},
        {"seat_type": "agent", "ingest_enabled": seat_ing},
    ) is expected


def test_missing_seat_type_reads_non_agent():
    assert saas_agent_mode.effective_ingest(
        {"status": "active"}, {"ingest_enabled": False}) is True


@pytest.mark.parametrize("status", ["suspended", "locked", "archived"])
def test_status_universal_blocks_even_agent_seat(status):
    assert saas_agent_mode.effective_ingest(
        {"status": status}, {"seat_type": "agent", "ingest_enabled": True}) is False


# ════════════════════════════════════════════════════════════════════════════════
# Signature-required + validation BEFORE any DB (fail fast, no control-plane touch)
# ════════════════════════════════════════════════════════════════════════════════

def _no_db(monkeypatch):
    def boom():
        raise AssertionError("db.connection reached — should validate before any DB")
    monkeypatch.setattr(ent.db, "connection", boom)


def test_provision_requires_signature_before_db(monkeypatch):
    _no_db(monkeypatch)
    out = ent.provision_capacity(ACCT_A, "agent", 1, signature="")
    assert out["ok"] is False and out["status_code"] == 400 and "signature" in out["reason"]


def test_provision_invalid_type_before_db(monkeypatch):
    _no_db(monkeypatch)
    out = ent.provision_capacity(ACCT_A, "bogus", 1, signature=SIG)
    assert out["status_code"] == 400


def test_upgrade_invalid_type_before_db(monkeypatch):
    _no_db(monkeypatch)
    assert ent.upgrade_seat(ACCT_A, UA, "bogus", signature=SIG)["status_code"] == 400


def test_move_seat_needs_no_signature(monkeypatch):
    # A tier move is license ASSIGNMENT, not a purchase — no signature gate. With the DB mocked
    # out it proceeds PAST validation to the connection (500), never the old 400-signature error.
    _no_db(monkeypatch)
    out = ent.upgrade_seat(ACCT_A, UA, "agent", signature="")
    assert out["status_code"] == 500 and "signature" not in out["reason"]


def test_bundle_requires_signature_before_db(monkeypatch):
    _no_db(monkeypatch)
    out = ent.provision_bundle(ACCT_A, "agent_x1", signature="")
    assert out["status_code"] == 400


# ════════════════════════════════════════════════════════════════════════════════
# A faithful fake control-plane DB — exercises the REAL entitlements SQL
# ════════════════════════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, state):
        self.st = state
        self._result = None
        self._rows = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._result = None
        self._rows = None
        acc = self.st["accounts"]
        ent_ = self.st["entitlements"]
        seats = self.st["seats"]
        ledger = self.st["ledger"]

        if s.startswith("SELECT term_months, expires_at FROM public.accounts") and "FOR UPDATE" in s:
            a = acc.get(p[0]); self._result = (a["term_months"], a["expires_at"]) if a else None
        elif s.startswith("SELECT term_months, expires_at FROM public.accounts"):
            a = acc.get(p[0]); self._result = (a["term_months"], a["expires_at"]) if a else None
        elif s.startswith("SELECT tax_rate FROM public.accounts"):
            a = acc.get(p[0]); self._result = (a.get("tax_rate", 0),) if a else None
        elif s.startswith("SELECT account_kind FROM public.accounts"):
            # The MAX-PROVISION CHECK (`entitlements.hard_cap_block`) resolves the account KIND
            # to get its hard cap. It fails CLOSED — an unreadable account refuses to provision,
            # because on a money path admitting on a broken read is the worse error. This fake
            # had no branch for the query, so it returned None and every provisioning test read
            # back "account not found". The fixture was wrong, NOT the guard: do not relax
            # hard_cap_block to make a mock pass.
            a = acc.get(p[0])
            self._result = (a.get("account_kind", "tenant"),) if a else None
        elif s.startswith("SELECT seat_type, quantity, base_quantity FROM public.account_entitlements"):
            self._rows = [(st, e["quantity"], e["base_quantity"])
                          for (a, st), e in ent_.items() if a == p[0]]
        elif s.startswith("SELECT seat_type, COUNT(*) FROM public.account_users"):
            counts = {}
            for r in seats:
                if r["account_id"] == p[0] and r["status"] == "active":
                    counts[r["seat_type"]] = counts.get(r["seat_type"], 0) + 1
            self._rows = list(counts.items())
        elif s.startswith("INSERT INTO public.account_entitlements") and "DO UPDATE" in s:
            # 🔴 THE REAL TABLE CARRIES ``CHECK (quantity >= base_quantity)`` AND PostgreSQL
            # EVALUATES IT ON THE TUPLE **PROPOSED FOR INSERTION**, BEFORE ON CONFLICT
            # ARBITRATION RUNS. This fake never modelled that, which is exactly how a green
            # suite coexisted with "a tenant cannot buy 1–4 Standard seats" (a bare HTTP 500
            # from ``provision_capacity``, byte-identical SQL at HEAD~1). It models it now, so a
            # revert to the old 4-parameter ``VALUES (%s,%s,%s,%s)`` shape goes RED here too and
            # not only against a real database.
            a, st = p[0], p[1]
            if len(p) == 6:                       # (account, seat_type, n, base, base, n)
                proposed = max(int(p[2]), int(p[3]))
                base, inc = int(p[3]), int(p[5])
            else:                                 # the legacy shape this defect lived in
                proposed, base = int(p[2]), int(p[3])
                inc = proposed
            if proposed < base:
                raise Exception(
                    'new row for relation "account_entitlements" violates check constraint '
                    '"account_entitlements_check"')
            key = (a, st)
            if key in ent_:
                ent_[key]["quantity"] += inc
            else:
                ent_[key] = {"quantity": proposed, "base_quantity": base}
            self._result = (ent_[key]["quantity"],)
        elif s.startswith("INSERT INTO public.account_entitlements"):  # ON CONFLICT DO NOTHING
            a, st, qty, base = p
            ent_.setdefault((a, st), {"quantity": qty, "base_quantity": base})
        elif s.startswith("SELECT quantity, base_quantity FROM public.account_entitlements"):
            e = ent_.get((p[0], p[1])); self._result = (e["quantity"], e["base_quantity"]) if e else None
        elif s.startswith("SELECT quantity FROM public.account_entitlements"):
            e = ent_.get((p[0], p[1])); self._result = (e["quantity"],) if e else None
        elif s.startswith("SELECT base_quantity FROM public.account_entitlements"):
            e = ent_.get((p[0], "standard")); self._result = (e["base_quantity"],) if e else None
        elif s.startswith("UPDATE public.account_entitlements SET quantity"):
            newq, a, st = p; ent_[(a, st)]["quantity"] = newq
        elif s.startswith("SELECT seat_type, is_base, status FROM public.account_users"):
            r = next((x for x in seats if x["account_id"] == p[0] and x["user_id"] == p[1]), None)
            self._result = (r["seat_type"], r["is_base"], r["status"]) if r else None
        elif s.startswith("SELECT COUNT(*) FROM public.account_users") and "is_base = TRUE" in s:
            # base-standard occupants of this account, EXCLUDING the seat being moved (user_id <> %s)
            a, u = p[0], p[-1]
            self._result = (sum(1 for r in seats if r["account_id"] == a and r["status"] == "active"
                                and r["seat_type"] == "standard" and r["is_base"] and r["user_id"] != u),)
        elif s.startswith("SELECT COUNT(*) FROM public.account_users") and "seat_type = %s" in s:
            a, st = p[0], p[-1]
            self._result = (sum(1 for r in seats if r["account_id"] == a
                                and r["status"] == "active" and r["seat_type"] == st),)
        elif s.startswith("SELECT COUNT(*) FROM public.account_users"):
            self._result = (sum(1 for r in seats if r["account_id"] == p[0]
                                and r["status"] == "active"),)
        elif s.startswith("UPDATE public.account_users SET seat_type = %s, is_base = %s WHERE"):
            st, ib, a, u = p
            for r in seats:
                if r["account_id"] == a and r["user_id"] == u:
                    r["seat_type"] = st; r["is_base"] = ib
        elif s.startswith("UPDATE public.account_users SET seat_type = %s WHERE"):
            st, a, u = p
            for r in seats:
                if r["account_id"] == a and r["user_id"] == u:
                    r["seat_type"] = st
        elif s.startswith("INSERT INTO public.account_ledger"):
            ledger.append({"action": p[1], "seat_type": p[2], "quantity": p[3],
                           "amount": p[5], "signature": p[15]})
            self._result = (len(ledger),)
        elif s.startswith("SELECT 1 FROM public.account_ledger"):
            self._result = None
        elif s.startswith("UPDATE public.accounts SET seat_limit"):
            pass
        elif s.startswith("UPDATE public.accounts"):
            pass

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._rows or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, state):
        self.st = state

    def cursor(self):
        return _FakeCursor(self.st)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def cp(monkeypatch):
    """A control plane: ACCT_A with the base-5 standard entitlement + one non-base agent seat."""
    end = datetime.now(timezone.utc) + timedelta(days=180)
    state = {
        "accounts": {ACCT_A: {"term_months": 6, "expires_at": end, "tax_rate": 0}},
        "entitlements": {
            (ACCT_A, "standard"): {"quantity": 5, "base_quantity": 5},
            (ACCT_A, "agent"): {"quantity": 1, "base_quantity": 0},
        },
        "seats": [
            {"account_id": ACCT_A, "user_id": UA, "status": "active",
             "seat_type": "standard", "is_base": True},    # a base-5 seat
            {"account_id": ACCT_A, "user_id": UB, "status": "active",
             "seat_type": "standard", "is_base": False},   # a provisioned (upgradeable) seat
        ],
        "ledger": [],
    }

    @contextlib.contextmanager
    def _conn():
        yield _FakeConn(state)
    monkeypatch.setattr(ent.db, "connection", _conn)
    monkeypatch.setattr(saas_seats, "invalidate_seat_cache", lambda: None)
    monkeypatch.setattr(saas_agent_mode, "invalidate_ingest_cache", lambda: None)
    return state


def test_provision_capacity_increments_and_ledgers(cp):
    # Enterprise Expert rate is $45 (tiered table) — 2 × 45 = 90 (the fake account resolves to the
    # default 'tenant'/Enterprise column). Old flat-$50 assumption is retired.
    out = ent.provision_capacity(ACCT_A, "expert", 2, signature=SIG, actor_user="jdoe")
    assert out["ok"] and out["new_quantity"] == 2 and out["amount"] == 90.0
    assert cp["entitlements"][(ACCT_A, "expert")]["quantity"] == 2
    assert cp["ledger"][-1]["action"] == "provision" and cp["ledger"][-1]["signature"] == SIG


def test_provision_bundle_writes_ledger(cp):
    out = ent.provision_bundle(ACCT_A, "agent_x3", signature=SIG)
    assert out["ok"] and out["amount"] == 250.0
    assert cp["entitlements"][(ACCT_A, "agent")]["quantity"] == 1 + 3
    assert cp["ledger"][-1]["action"] == "bundle"


# ── seat move: up OR down, base-5 movable, capacity-guarded, $0 audit ──

def test_move_up_the_ladder_ok(cp):
    # UB is a non-base standard seat; agent capacity = 1, used by 0 agent seats.
    out = ent.upgrade_seat(ACCT_A, UB, "agent", actor_user="jdoe")
    assert out["ok"] and out["seat_type"] == "agent" and out["previous_type"] == "standard"
    assert out["direction"] == "up"
    seat = next(r for r in cp["seats"] if r["user_id"] == UB)
    assert seat["seat_type"] == "agent"
    # $0 reassignment audit line (not a billed "upgrade").
    assert cp["ledger"][-1]["action"] == "reassign" and cp["ledger"][-1]["amount"] == 0.0


def test_move_down_within_capacity_ok(cp):
    # Lift UB→agent, then move it back DOWN to standard — allowed (standard has room: 1/5 used).
    ent.upgrade_seat(ACCT_A, UB, "agent")
    out = ent.upgrade_seat(ACCT_A, UB, "standard")
    assert out["ok"] and out["seat_type"] == "standard" and out["direction"] == "down"


def test_base5_seat_can_move_up_and_frees_slot(cp):
    # UA is a base-5 seat — it CAN move up now; doing so clears is_base (frees the included slot).
    out = ent.upgrade_seat(ACCT_A, UA, "agent")
    assert out["ok"] and out["seat_type"] == "agent" and out["direction"] == "up"
    seat = next(r for r in cp["seats"] if r["user_id"] == UA)
    assert seat["seat_type"] == "agent" and seat["is_base"] is False


def test_move_into_full_tier_refused_402(cp):
    # Fill agent (cap 1) with UB, then try to move UA into agent → no license left (402). This is
    # the capacity guard that also stops a DOWNGRADE into a tier that "doesn't cover the cost".
    ent.upgrade_seat(ACCT_A, UB, "agent")
    out = ent.upgrade_seat(ACCT_A, UA, "agent")
    assert out["ok"] is False and out["status_code"] == 402
    assert "license available" in out["reason"] and "provision one first" in out["reason"]


def test_no_capacity_402_clear_message(cp):
    # No expert entitlement exists → moving UB to expert has 0 capacity.
    out = ent.upgrade_seat(ACCT_A, UB, "expert")
    assert out["ok"] is False and out["status_code"] == 402
    assert "Expert license available" in out["reason"] and "provision one first" in out["reason"]


# ── removal never below base/usage ──

def test_remove_below_base_refused_409(cp):
    out = ent.remove_capacity(ACCT_A, "standard", 1, signature=SIG)
    assert out["ok"] is False and out["status_code"] == 409 and "base" in out["reason"].lower()


def test_remove_provisioned_ok(cp):
    ent.provision_capacity(ACCT_A, "expert", 3, signature=SIG)   # 0 used
    out = ent.remove_capacity(ACCT_A, "expert", 1, signature=SIG)
    assert out["ok"] and cp["entitlements"][(ACCT_A, "expert")]["quantity"] == 2


# ── capacity report + contract term surfaced ──

def test_capacity_report_shape_and_term(cp):
    rep = ent.capacity_report(ACCT_A)
    types = {t["seat_type"]: t for t in rep["types"]}
    assert types["standard"]["base"] == 5 and types["standard"]["licensed"] == 5
    assert types["agent"]["licensed"] == 1 and types["agent"]["used"] == 0
    assert rep["term_months"] == 6 and rep["expires_at"] is not None and rep["expired"] is False
    assert rep["ladder"] == list(ent.LADDER)
    # Base package is a flat CAD $1500/mo (5 Standard included) — NOT the $0/à-la-carte standard.
    assert rep["base_package_price"] == 1500.0


# ── scheduled downgrade (never mid-term) ──

def test_schedule_downgrade_requires_signature(monkeypatch):
    _no_db(monkeypatch)
    assert ent.schedule_downgrade(ACCT_A, UB, "standard", signature="")["status_code"] == 400


# ════════════════════════════════════════════════════════════════════════════════
# ingest_allowed selects the SEAT's own seat_type (fake JOIN row)
# ════════════════════════════════════════════════════════════════════════════════

class _IngestCursor:
    def __init__(self, row):
        self.row = row

    def execute(self, sql, params=None):
        assert "au.seat_type" in " ".join(sql.split())  # keys off the SEAT type now

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ingest_db(monkeypatch, row):
    class _C:
        def cursor(self_):
            return _IngestCursor(row)

        def commit(self_):
            pass

        def rollback(self_):
            pass

        def close(self_):
            pass

    @contextlib.contextmanager
    def _conn():
        yield _C()
    monkeypatch.setattr(saas_agent_mode.db, "connection", _conn)
    saas_agent_mode.invalidate_ingest_cache()


def test_ingest_allowed_agent_seat_off_blocks(monkeypatch):
    _ingest_db(monkeypatch, ("active", False, "agent"))
    assert saas_agent_mode.ingest_allowed(ACCT_A, UA) is False


def test_ingest_allowed_agent_seat_on_allows(monkeypatch):
    _ingest_db(monkeypatch, ("active", True, "agent"))
    assert saas_agent_mode.ingest_allowed(ACCT_A, UA) is True


def test_ingest_allowed_standard_seat_always_on(monkeypatch):
    _ingest_db(monkeypatch, ("active", False, "standard"))
    assert saas_agent_mode.ingest_allowed(ACCT_A, UA) is True


# ════════════════════════════════════════════════════════════════════════════════
# ROUTE wiring — admin (operator/account-key) surface
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account",
                        lambda k: {KEY_A: ACCT_A}.get(k))
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


def test_provision_route_passes_signature_and_actor(admin_client, monkeypatch):
    seen = {}

    def fake(account_id, seat_type, quantity, *, actor, note, signature, actor_user, actor_role):
        seen.update(account_id=account_id, seat_type=seat_type, quantity=quantity,
                    signature=signature, actor_role=actor_role)
        return {"ok": True, "status_code": 200, "seat_type": seat_type, "quantity": quantity,
                "amount": 50.0, "reason": "ok"}
    monkeypatch.setattr(saas_admin._entitlements, "provision_capacity", fake)
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/capacity",
                          json={"seat_type": "expert", "quantity": 1, "signature": SIG},
                          headers=_op())
    assert r.status_code == 200
    assert seen["signature"] == SIG and seen["actor_role"] == "operator"


def test_provision_route_missing_signature_422(admin_client):
    # signature is a required Field → 422 before the handler.
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/capacity",
                          json={"seat_type": "expert", "quantity": 1}, headers=_op())
    assert r.status_code == 422


def test_upgrade_route_wires_and_maps_status(admin_client, monkeypatch):
    monkeypatch.setattr(saas_admin._entitlements, "upgrade_seat",
                        lambda *a, **k: {"ok": False, "status_code": 409, "reason": "downgrade refused"})
    r = admin_client.put(f"/admin/accounts/{ACCT_A}/users/{UA}/type",
                         json={"seat_type": "standard", "signature": SIG}, headers=_op())
    assert r.status_code == 409


def test_capacity_and_ledger_read_routes(admin_client, monkeypatch):
    monkeypatch.setattr(saas_admin._entitlements, "capacity_report",
                        lambda a: {"account_id": a, "types": [], "total_licensed": 5})
    monkeypatch.setattr(saas_admin._entitlements, "ledger_summary",
                        lambda a, limit=200: {"account_id": a, "periods": [], "grand_total": 0.0})
    assert admin_client.get(f"/admin/accounts/{ACCT_A}/capacity", headers=_op()).status_code == 200
    assert admin_client.get(f"/admin/accounts/{ACCT_A}/ledger", headers=_op()).status_code == 200


def test_bundles_route_operator_only(admin_client):
    assert admin_client.get("/admin/bundles", headers=_op()).status_code == 200
    assert admin_client.get("/admin/bundles",
                            headers={"Authorization": f"Bearer {KEY_A}"}).status_code == 403


def test_tax_route_operator_only(admin_client, monkeypatch):
    monkeypatch.setattr(saas_admin._entitlements, "set_tax_rate",
                        lambda a, r: {"ok": True, "status_code": 200, "tax_rate": r, "reason": "ok"})
    # account key → 403
    assert admin_client.post(f"/admin/accounts/{ACCT_A}/tax", json={"tax_rate": 0.13},
                             headers={"Authorization": f"Bearer {KEY_A}"}).status_code == 403
    # operator → 200
    assert admin_client.post(f"/admin/accounts/{ACCT_A}/tax", json={"tax_rate": 0.13},
                             headers=_op()).status_code == 200


def test_new_licensing_routes_registered():
    paths = {r.path for r in saas_admin.router.routes}
    for p in ("/admin/accounts/{account_id}/capacity",
              "/admin/accounts/{account_id}/capacity/bundle",
              "/admin/accounts/{account_id}/users/{user_id}/type",
              "/admin/accounts/{account_id}/ledger",
              "/admin/accounts/{account_id}/billing",
              "/admin/accounts/{account_id}/users/{user_id}/schedule-downgrade"):
        assert p in paths


# ════════════════════════════════════════════════════════════════════════════════
# ROUTE wiring — tenant (session) surface, scope from the SESSION account (BOLA)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tenant_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "licensing-tenant-secret-0123456789abcd")
    monkeypatch.setattr(saas_seats, "is_active_seat", lambda a, u: (a, u) == (ACCT_A, UA))
    # These flows exercise the billing-authorized path (the session user is the account owner);
    # the can_view_billing GATE itself is proven in tests/test_saas_billing_access.py. Grant it
    # so the capacity/billing wiring here isn't 403'd by migration-159 role separation.
    monkeypatch.setattr(auth, "can_view_billing", lambda uid: True)
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _sess():
    return auth.issue_session(dashboard_user_id="dash-1", account_id=ACCT_A, username="alice")


def _csrf():
    return auth.issue_csrf(dashboard_user_id="dash-1")


def test_tenant_provision_scopes_to_session_account(tenant_client, monkeypatch):
    seen = {}

    def fake(account_id, seat_type, quantity, *, actor, note, signature, actor_user, actor_role):
        seen.update(account_id=account_id, actor_user=actor_user, actor_role=actor_role,
                    signature=signature)
        return {"ok": True, "status_code": 200, "seat_type": seat_type, "quantity": quantity,
                "amount": 25.0, "reason": "ok"}
    monkeypatch.setattr(tc._entitlements, "provision_capacity", fake)
    tenant_client.cookies.set(auth.SESSION_COOKIE_NAME, _sess())
    r = tenant_client.post("/tenant/capacity", headers={"x-csrf-token": _csrf()},
                           json={"seat_type": "advanced", "quantity": 1, "signature": SIG})
    assert r.status_code == 200
    # scope + actor come from the SESSION, never the client body
    assert seen["account_id"] == ACCT_A and seen["actor_role"] == "tenant"
    assert seen["actor_user"] == "alice" and seen["signature"] == SIG


def test_tenant_upgrade_seat_bola_and_wire(tenant_client, monkeypatch):
    monkeypatch.setattr(tc._entitlements, "upgrade_seat",
                        lambda *a, **k: {"ok": True, "status_code": 200, "seat_type": "agent",
                                         "previous_type": "advanced", "reason": "ok"})
    tenant_client.cookies.set(auth.SESSION_COOKIE_NAME, _sess())
    r = tenant_client.put(f"/tenant/seats/{UA}/type", headers={"x-csrf-token": _csrf()},
                          json={"seat_type": "agent", "signature": SIG})
    assert r.status_code == 200 and r.json()["seat_type"] == "agent"
    # a seat NOT of the session account → 403 (BOLA)
    r2 = tenant_client.put(f"/tenant/seats/{UB}/type", headers={"x-csrf-token": _csrf()},
                           json={"seat_type": "agent", "signature": SIG})
    assert r2.status_code == 403


def test_tenant_billing_read(tenant_client, monkeypatch):
    monkeypatch.setattr(tc._entitlements, "billing_view",
                        lambda a: {"account_id": a, "currency": "CAD", "lines": [],
                                   "monthly_total": 0.0})
    tenant_client.cookies.set(auth.SESSION_COOKIE_NAME, _sess())
    r = tenant_client.get("/tenant/billing")
    assert r.status_code == 200 and r.json()["currency"] == "CAD"


# ════════════════════════════════════════════════════════════════════════════════
# list_seats surfaces seat_type + is_base (migration 139)
# ════════════════════════════════════════════════════════════════════════════════

def test_list_seats_carries_seat_type_and_base(monkeypatch):
    class _C:
        def execute(self, sql, params=None):
            assert "seat_type" in " ".join(sql.split())

        def fetchall(self):
            # Full row shape as the real query returns it (migration 139 seat_type/is_base +
            # 148 response_type + 161 api_enabled + 210 owui_* + 212's read-through join).
            # Kept in step deliberately: a short mock exercises the defensive len() guards
            # instead of the real projection, which is how a stale fixture stops asserting
            # anything about the code that actually runs.
            return [(UA, "active", None, "lbl", True, "agent", False, None, False,
                     None, None, "tenant", "real", False, True)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _C()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    @contextlib.contextmanager
    def _conn():
        yield _Conn()
    monkeypatch.setattr(saas_seats.db, "connection", _conn)
    rows = saas_seats.list_seats(ACCT_A)
    assert rows[0]["seat_type"] == "agent" and rows[0]["is_base"] is False
