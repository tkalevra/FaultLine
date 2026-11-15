"""SaaS Phase 5 — account STATUS lock + AGENT-MODE ingest gate + PANIC one-way latch.

The two fail-closed control gates (DEV/DESIGN-saas-dashboards-and-observability.md §2/§2.1/§3):

  §2  operator STATUS lock — a non-'active' status kills every key + token at the chokepoint
       and the re_embedder skips the account's tenants. Cache-busted immediately.
  §2.1 break-glass PANIC — account owner one-way latch: can PANIC, can never un-panic; only
       the operator clears a tenant_panic lock.
  §3  tenant AGENT MODE — effective_ingest = account active ∧ account.ingest_enabled ∧
       seat.ingest_enabled; enforced at the MCP WRITE tools (remember/learn/retract), NEVER
       at recall_memory. OFF = READ-ONLY (writes rejected, HTTP 200 read_only).

Everything is fail-CLOSED and SAAS_MODE-gated (OFF = today's behavior EXACTLY).
Live-DB roundtrips auto-skip when no control plane is reachable.

Run: python3 -m pytest tests/test_saas_status_and_agent_mode.py -q
"""

import contextlib
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.mcp import http_server as http_server

from saas import admin as saas_admin
from saas import accounts as saas_accounts
from saas import seats as saas_seats
from saas import tokens as saas_tokens
from saas import agent_mode as saas_agent_mode


UA = "11111111-1111-4111-8111-111111111111"   # AcctA's seat
UB = "22222222-2222-4222-8222-222222222222"   # AcctB's seat
UNOWNED = "33333333-3333-4333-8333-333333333333"
ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

KEY_A = "flk_aaaaaaaa_secretA"
KEY_B = "flk_bbbbbbbb_secretB"
OP_KEY = "op_super_secret"


# ════════════════════════════════════════════════════════════════════════════════
# A minimal fake control-plane DB (exercises the REAL accounts.py SQL branches)
# ════════════════════════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, store):
        self.store = store          # account_id -> {status, lock_origin, slug, ingest_enabled}
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._result = None
        if s.startswith("SELECT status, lock_origin FROM public.accounts"):
            row = self.store.get(p[0])
            self._result = (row["status"], row["lock_origin"]) if row else None
        elif s.startswith("SELECT slug FROM public.accounts"):
            row = self.store.get(p[0])
            self._result = (row["slug"],) if row else None
        elif s.startswith("SELECT status FROM public.accounts"):
            row = self.store.get(p[0])
            self._result = (row["status"],) if row else None
        elif s.startswith("UPDATE public.accounts SET status = 'locked', lock_origin = 'tenant_panic'"):
            row = self.store.get(p[0])
            if row:
                row["status"], row["lock_origin"] = "locked", "tenant_panic"
        elif s.startswith("UPDATE public.accounts SET status = %s, lock_origin = %s"):
            status, origin, acct = p
            row = self.store.get(acct)
            if row:
                row["status"], row["lock_origin"] = status, origin
        elif s.startswith("UPDATE public.accounts SET ingest_enabled"):
            enabled, acct = p
            row = self.store.get(acct)
            if row:
                row["ingest_enabled"] = enabled
                self._result = (enabled, row["status"])

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _fake_db(store):
    @contextlib.contextmanager
    def _connection():
        yield _FakeConn(store)
    return _connection


@pytest.fixture
def acct_store(monkeypatch):
    store = {
        ACCT_A: {"status": "active", "lock_origin": None, "slug": "acct-a",
                 "ingest_enabled": True},
    }
    monkeypatch.setattr(saas_accounts.db, "connection", _fake_db(store))
    return store


# ════════════════════════════════════════════════════════════════════════════════
# §3 — effective_ingest truth table (pure, no DB) — all 3 flags
# ════════════════════════════════════════════════════════════════════════════════

# NOTE (per-seat-only): the whole-tenant account.ingest_enabled term was REMOVED — for the
# 'agent' tier effective ingest depends ONLY on the per-SEAT flag (and the universal status).
# The acct_ing column below is deliberately varied to PROVE it no longer affects the result
# (rows with the same status+seat but different acct_ing yield the same expected value).
@pytest.mark.parametrize("status,acct_ing,seat_ing,expected", [
    ("active",    True,  True,  True),
    ("active",    False, True,  True),     # account flag ignored → seat ON wins
    ("active",    True,  False, False),
    ("active",    False, False, False),
    ("suspended", True,  True,  False),
    ("locked",    True,  True,  False),
    ("archived",  True,  True,  False),
    ("suspended", False, False, False),
])
def test_effective_ingest_truth_table(status, acct_ing, seat_ing, expected):
    # Migration 139: the AGENT SEAT keys off its OWN seat_type + per-seat flag; the tenant
    # account_type/ingest_enabled are irrelevant (acct_ing varied to prove it).
    assert saas_agent_mode.effective_ingest(
        {"status": status, "ingest_enabled": acct_ing, "account_type": "standard"},
        {"ingest_enabled": seat_ing, "seat_type": "agent"},
    ) is expected


def test_effective_ingest_missing_flags_fail_closed():
    # No status at all → blocked regardless of tier.
    assert saas_agent_mode.effective_ingest({}, {}) is False
    # AGENT SEAT with the per-seat ingest flag missing → fail-closed (blocked).
    assert saas_agent_mode.effective_ingest(
        {"status": "active"}, {"seat_type": "agent"}) is False


def test_account_statuses_enum():
    assert saas_accounts.ACCOUNT_STATUSES == ("active", "suspended", "locked", "archived")


# ════════════════════════════════════════════════════════════════════════════════
# §3 — account_id resolution for a resolved principal (server-side, fail-closed)
# ════════════════════════════════════════════════════════════════════════════════

def test_account_id_for_flk_principal_is_pure():
    assert saas_agent_mode.account_id_for_principal(f"account:{ACCT_A}", UA) == ACCT_A


def test_account_id_for_flu_principal_db_error_fails_closed(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(saas_agent_mode.db, "connection", boom)
    # a bare-UUID (flu_ token) principal that can't resolve its account → None
    assert saas_agent_mode.account_id_for_principal(UA, UA) is None


def test_account_id_for_flu_principal_looks_up_account(monkeypatch):
    store = {ACCT_A: {"status": "active", "lock_origin": None, "slug": "a",
                      "ingest_enabled": True}}

    class _C(_FakeCursor):
        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("SELECT account_id::text FROM public.account_users"):
                self._result = (ACCT_A,)
            else:
                super().execute(sql, params)

    @contextlib.contextmanager
    def _conn():
        c = _FakeConn(store)
        c.cursor = lambda: _C(store)
        yield c
    monkeypatch.setattr(saas_agent_mode.db, "connection", _conn)
    assert saas_agent_mode.account_id_for_principal(UA, UA) == ACCT_A


# ════════════════════════════════════════════════════════════════════════════════
# §3 — write_gate_block: blocks writes when off, ALLOWS recall_memory always
# ════════════════════════════════════════════════════════════════════════════════

def _gate_env(monkeypatch, *, saas_on, allowed, account="acct"):
    monkeypatch.setattr(saas_agent_mode, "saas_mode_enabled", lambda: saas_on)
    monkeypatch.setattr(saas_agent_mode, "account_id_for_principal",
                        lambda p, u: account)
    monkeypatch.setattr(saas_agent_mode, "ingest_allowed", lambda a, u: allowed)


def test_gate_saas_off_never_blocks(monkeypatch):
    _gate_env(monkeypatch, saas_on=False, allowed=False)
    assert saas_agent_mode.write_gate_block("account:x", UA, "remember_facts") is None


def test_gate_recall_memory_never_consulted(monkeypatch):
    _gate_env(monkeypatch, saas_on=True, allowed=False)
    # recall is not a write tool → passes through even when ingest is disabled
    assert saas_agent_mode.write_gate_block("account:x", UA, "recall_memory") is None


@pytest.mark.parametrize("tool", ["remember_facts", "learn_facts", "retract_fact"])
def test_gate_blocks_all_write_tools_when_off(monkeypatch, tool):
    _gate_env(monkeypatch, saas_on=True, allowed=False)
    blocked = saas_agent_mode.write_gate_block("account:x", UA, tool)
    assert blocked == saas_agent_mode.READ_ONLY_RESPONSE
    assert blocked["status"] == "read_only"


@pytest.mark.parametrize("tool", ["remember_facts", "learn_facts", "retract_fact"])
def test_gate_allows_write_tools_when_on(monkeypatch, tool):
    _gate_env(monkeypatch, saas_on=True, allowed=True)
    assert saas_agent_mode.write_gate_block("account:x", UA, tool) is None


def test_gate_unresolvable_account_fails_closed(monkeypatch):
    monkeypatch.setattr(saas_agent_mode, "saas_mode_enabled", lambda: True)
    monkeypatch.setattr(saas_agent_mode, "account_id_for_principal", lambda p, u: None)
    monkeypatch.setattr(saas_agent_mode, "ingest_allowed",
                        lambda a, u: pytest.fail("ingest_allowed reached with no account"))
    assert saas_agent_mode.write_gate_block(None, UA, "remember_facts") == \
        saas_agent_mode.READ_ONLY_RESPONSE


def test_ingest_allowed_empty_args_fail_closed():
    assert saas_agent_mode.ingest_allowed("", UA) is False
    assert saas_agent_mode.ingest_allowed(ACCT_A, "") is False


def test_ingest_allowed_db_error_fails_closed(monkeypatch):
    saas_agent_mode.invalidate_ingest_cache()

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(saas_agent_mode.db, "connection", boom)
    assert saas_agent_mode.ingest_allowed(ACCT_A, UA) is False


# ════════════════════════════════════════════════════════════════════════════════
# transport chokepoint — http_server._saas_write_block (SAAS-off unchanged)
# ════════════════════════════════════════════════════════════════════════════════

def test_transport_gate_saas_off_returns_none(monkeypatch):
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: False)
    # SAAS off → the gate is ABSENT; every write flows exactly as today.
    assert http_server._saas_write_block("account:x", UA, "remember_facts") is None


def test_transport_gate_blocks_when_disabled(monkeypatch):
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)
    monkeypatch.setattr("saas.agent_mode.write_gate_block",
                        lambda p, u, t: saas_agent_mode.READ_ONLY_RESPONSE)
    out = http_server._saas_write_block("account:x", UA, "remember_facts")
    assert out["status"] == "read_only"


def test_transport_gate_allows_when_enabled(monkeypatch):
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)
    monkeypatch.setattr("saas.agent_mode.write_gate_block", lambda p, u, t: None)
    assert http_server._saas_write_block("account:x", UA, "remember_facts") is None


def test_transport_gate_error_in_saas_mode_fails_closed(monkeypatch):
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)

    def boom(p, u, t):
        raise RuntimeError("gate module broke")
    monkeypatch.setattr("saas.agent_mode.write_gate_block", boom)
    out = http_server._saas_write_block("account:x", UA, "remember_facts")
    assert out["status"] == "read_only"   # fail-closed block, not a pass-through


# ════════════════════════════════════════════════════════════════════════════════
# §2 — status set + cache-bust (REAL set_account_status against the fake DB)
# ════════════════════════════════════════════════════════════════════════════════

def test_set_status_invalid_enum_400(acct_store):
    out = saas_accounts.set_account_status(ACCT_A, "bogus", is_operator=True)
    assert out["ok"] is False and out["status_code"] == 400


def test_set_status_not_found_404(acct_store):
    out = saas_accounts.set_account_status("ffffffff-ffff-4fff-8fff-ffffffffffff",
                                           "suspended", is_operator=True)
    assert out["ok"] is False and out["status_code"] == 404


def test_set_status_valid_transition(acct_store):
    out = saas_accounts.set_account_status(ACCT_A, "suspended", is_operator=True)
    assert out["ok"] is True and out["status"] == "suspended"
    assert acct_store[ACCT_A]["status"] == "suspended"


def test_status_change_busts_caches(acct_store):
    # seed all sibling caches; a status change must clear them immediately (§2 req 1).
    kh = "deadbeefhash"
    saas_accounts._key_cache[kh] = (10 ** 12, ACCT_A)     # never-expiring entry
    saas_seats._seat_cache[(ACCT_A, UA)] = (10 ** 12, True)
    saas_tokens._token_cache["thash"] = (10 ** 12, UA)
    saas_agent_mode._ingest_cache[(ACCT_A, UA)] = (10 ** 12, True)

    saas_accounts.set_account_status(ACCT_A, "locked", is_operator=True)

    assert kh not in saas_accounts._key_cache            # this account's key entry dropped
    assert saas_seats._seat_cache == {}                  # sibling caches wholesale-cleared
    assert saas_tokens._token_cache == {}
    assert saas_agent_mode._ingest_cache == {}


# ════════════════════════════════════════════════════════════════════════════════
# §2.1 — PANIC one-way latch (REAL panic_lock + set_account_status, fake DB)
# ════════════════════════════════════════════════════════════════════════════════

def test_panic_sets_locked_and_tenant_panic(acct_store):
    out = saas_accounts.panic_lock(ACCT_A, "acct-a")
    assert out["ok"] is True and out["status"] == "locked"
    assert acct_store[ACCT_A]["status"] == "locked"
    assert acct_store[ACCT_A]["lock_origin"] == "tenant_panic"


def test_panic_slug_mismatch_400(acct_store):
    out = saas_accounts.panic_lock(ACCT_A, "wrong-slug")
    assert out["ok"] is False and out["status_code"] == 400
    assert acct_store[ACCT_A]["status"] == "active"       # untouched


def test_panic_not_found_404(acct_store):
    out = saas_accounts.panic_lock("ffffffff-ffff-4fff-8fff-ffffffffffff", "x")
    assert out["ok"] is False and out["status_code"] == 404


def test_account_key_cannot_clear_tenant_panic(acct_store):
    saas_accounts.panic_lock(ACCT_A, "acct-a")            # → locked/tenant_panic
    # account owner (is_operator=False) tries to un-panic → 403, latch holds
    out = saas_accounts.set_account_status(ACCT_A, "active", is_operator=False)
    assert out["ok"] is False and out["status_code"] == 403
    assert acct_store[ACCT_A]["status"] == "locked"
    assert acct_store[ACCT_A]["lock_origin"] == "tenant_panic"


def test_operator_can_clear_tenant_panic(acct_store):
    saas_accounts.panic_lock(ACCT_A, "acct-a")
    out = saas_accounts.set_account_status(ACCT_A, "active", is_operator=True)
    assert out["ok"] is True and out["status"] == "active"
    assert acct_store[ACCT_A]["status"] == "active"
    assert acct_store[ACCT_A]["lock_origin"] is None      # latch reset on clear


def test_operator_lock_is_operator_clearable(acct_store):
    # a normal operator lock (lock_origin='operator')…
    acct_store[ACCT_A]["status"] = "locked"
    acct_store[ACCT_A]["lock_origin"] = "operator"
    # non-operator still cannot clear a lock (fail-closed operator-only)…
    denied = saas_accounts.set_account_status(ACCT_A, "active", is_operator=False)
    assert denied["status_code"] == 403
    # …but the operator clears it and the origin resets.
    ok = saas_accounts.set_account_status(ACCT_A, "active", is_operator=True)
    assert ok["ok"] is True and acct_store[ACCT_A]["lock_origin"] is None


# ════════════════════════════════════════════════════════════════════════════════
# §3 — agent-mode whole-tenant switch (REAL set_account_ingest_enabled, fake DB)
# ════════════════════════════════════════════════════════════════════════════════

def test_set_account_ingest_enabled_toggles(acct_store):
    off = saas_accounts.set_account_ingest_enabled(ACCT_A, False)
    assert off["ingest_enabled"] is False
    assert acct_store[ACCT_A]["ingest_enabled"] is False
    on = saas_accounts.set_account_ingest_enabled(ACCT_A, True)
    assert on["ingest_enabled"] is True


def test_set_account_ingest_enabled_not_found(acct_store):
    assert saas_accounts.set_account_ingest_enabled(
        "ffffffff-ffff-4fff-8fff-ffffffffffff", False) is None


# ════════════════════════════════════════════════════════════════════════════════
# ROUTE tests — admin endpoints (auth, precedence, wiring)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    keymap = {KEY_A: ACCT_A, KEY_B: ACCT_B}
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: keymap.get(k))
    roster = {(ACCT_A, UA): True, (ACCT_B, UB): True}
    monkeypatch.setattr(saas_seats, "is_active_seat", lambda a, u: roster.get((a, u), False))
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_status_endpoint_operator_only(client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "set_account_status",
                        lambda a, s, is_operator: {"ok": True, "status_code": 200,
                                                   "status": s, "reason": "ok"})
    # account key → 403 (require_operator)
    r_acct = client.post(f"/admin/accounts/{ACCT_A}/status",
                         json={"status": "suspended"}, headers=_auth(KEY_A))
    assert r_acct.status_code == 403
    # operator key → 200
    r_op = client.post(f"/admin/accounts/{ACCT_A}/status",
                       json={"status": "suspended"}, headers=_auth(OP_KEY))
    assert r_op.status_code == 200 and r_op.json()["status"] == "suspended"


def test_status_endpoint_rejects_unknown_status(client):
    r = client.post(f"/admin/accounts/{ACCT_A}/status",
                    json={"status": "on_fire"}, headers=_auth(OP_KEY))
    assert r.status_code == 400


def test_panic_endpoint_account_key(client, monkeypatch):
    seen = {}

    def fake_panic(account_id, confirm):
        seen["args"] = (account_id, confirm)
        return {"ok": True, "status_code": 200, "status": "locked"}
    monkeypatch.setattr(saas_accounts, "panic_lock", fake_panic)
    r = client.post("/admin/panic", json={"confirm": "acct-a"}, headers=_auth(KEY_A))
    assert r.status_code == 200
    assert r.json()["lock_origin"] == "tenant_panic"
    assert seen["args"] == (ACCT_A, "acct-a")   # account server-derived, not client-supplied


def test_panic_endpoint_slug_mismatch_400(client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "panic_lock",
                        lambda a, c: {"ok": False, "status_code": 400,
                                      "reason": "confirmation does not match account slug"})
    r = client.post("/admin/panic", json={"confirm": "nope"}, headers=_auth(KEY_A))
    assert r.status_code == 400


def test_panic_endpoint_operator_rejected(client):
    # operator sentinel has no concrete account → 400 (panic is the owner's break-glass)
    r = client.post("/admin/panic", json={"confirm": "x"}, headers=_auth(OP_KEY))
    assert r.status_code == 400


def test_agent_mode_route_removed(client):
    # The whole-tenant PUT /admin/agent-mode toggle was deleted (train->serve is per-seat).
    r = client.put("/admin/agent-mode", json={"enabled": False}, headers=_auth(KEY_A))
    assert r.status_code in (404, 405)


def test_update_seat_own_seat(client, monkeypatch):
    monkeypatch.setattr(saas_seats, "is_agent_seat", lambda a, u: True)  # migration 139 per-seat gate
    captured = {}

    def fake_update(account_id, user_id, *, ingest_enabled, label):
        captured["args"] = (account_id, user_id, ingest_enabled, label)
        return {"user_id": user_id, "status": "active",
                "ingest_enabled": bool(ingest_enabled), "label": label}
    monkeypatch.setattr(saas_seats, "update_seat", fake_update)
    r = client.put(f"/admin/users/{UA}",
                   json={"ingest_enabled": False, "label": "net-agent"},
                   headers=_auth(KEY_A))
    assert r.status_code == 200
    body = r.json()
    assert body["ingest_enabled"] is False and body["label"] == "net-agent"
    assert captured["args"] == (ACCT_A, UA, False, "net-agent")


def test_update_seat_cross_account_denied(client, monkeypatch):
    monkeypatch.setattr(saas_seats, "update_seat",
                        lambda *a, **k: pytest.fail("update_seat reached cross-account"))
    r = client.put(f"/admin/users/{UB}", json={"label": "x"}, headers=_auth(KEY_A))
    assert r.status_code == 403


def test_saas_off_status_endpoint_404(client, monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    r = client.post(f"/admin/accounts/{ACCT_A}/status",
                    json={"status": "suspended"}, headers=_auth(OP_KEY))
    assert r.status_code == 404


def test_saas_off_status_endpoint_still_404_for_removed_agent_mode(client, monkeypatch):
    # The agent-mode route is gone entirely now (removed, per-seat only) — 404/405 regardless
    # of SAAS_MODE.
    monkeypatch.delenv("SAAS_MODE", raising=False)
    r = client.put("/admin/agent-mode", json={"enabled": False}, headers=_auth(KEY_A))
    assert r.status_code in (404, 405)


# ════════════════════════════════════════════════════════════════════════════════
# LIVE-DB integration — real SQL roundtrips (auto-skip when no control plane)
# ════════════════════════════════════════════════════════════════════════════════

def _control_plane_available() -> bool:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=2)
        with conn.cursor() as cur:
            # migration 133 columns present?
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='accounts' "
                        "AND column_name IN ('lock_origin','ingest_enabled','account_type')")
            ok = len(cur.fetchall()) == 3
        conn.close()
        return ok
    except Exception:
        return False


live_db = pytest.mark.skipif(
    not _control_plane_available(),
    reason="no reachable POSTGRES_DSN control plane w/ migration 133 — live path proven via curl",
)


@live_db
def test_live_status_panic_latch_and_agent_mode_roundtrip():
    import psycopg2
    dsn = os.environ["POSTGRES_DSN"]
    slug = f"test-p5-{uuid.uuid4().hex[:8]}"
    ua = str(uuid.uuid4())
    conn = psycopg2.connect(dsn)
    acct = None
    try:
        with conn.cursor() as cur:
            # agent tier so the ingest_enabled toggles bind (migration 138 semantics).
            cur.execute("INSERT INTO public.accounts (name, slug, seat_limit, account_type) "
                        "VALUES (%s,%s,%s,'agent') RETURNING account_id::text",
                        ("p5 test", slug, 1))
            acct = cur.fetchone()[0]
            cur.execute("INSERT INTO public.account_users (account_id,user_id,status) "
                        "VALUES (%s,%s,'active')", (acct, ua))
        conn.commit()
        saas_accounts.invalidate_key_cache()
        saas_agent_mode.invalidate_ingest_cache()

        # agent mode: ingest allowed by default, blocked when tenant switch flips
        assert saas_agent_mode.ingest_allowed(acct, ua) is True
        saas_accounts.set_account_ingest_enabled(acct, False)
        assert saas_agent_mode.ingest_allowed(acct, ua) is False
        saas_accounts.set_account_ingest_enabled(acct, True)

        # per-seat flip also blocks
        saas_seats.update_seat(acct, ua, ingest_enabled=False, label="agent-x")
        assert saas_agent_mode.ingest_allowed(acct, ua) is False
        saas_seats.update_seat(acct, ua, ingest_enabled=True)

        # status lock kills ingest + resolution
        saas_accounts.set_account_status(acct, "suspended", is_operator=True)
        assert saas_agent_mode.ingest_allowed(acct, ua) is False
        saas_accounts.set_account_status(acct, "active", is_operator=True)

        # PANIC one-way latch
        assert saas_accounts.panic_lock(acct, slug)["ok"] is True
        blocked = saas_accounts.set_account_status(acct, "active", is_operator=False)
        assert blocked["status_code"] == 403                       # owner cannot un-panic
        cleared = saas_accounts.set_account_status(acct, "active", is_operator=True)
        assert cleared["ok"] is True                               # operator clears it
        with conn.cursor() as cur:
            cur.execute("SELECT status, lock_origin FROM public.accounts WHERE account_id=%s",
                        (acct,))
            st, origin = cur.fetchone()
        assert st == "active" and origin is None                   # latch reset
    finally:
        with conn.cursor() as cur:
            if acct:
                cur.execute("DELETE FROM public.account_users WHERE account_id=%s", (acct,))
                cur.execute("DELETE FROM public.accounts WHERE account_id=%s", (acct,))
        conn.commit()
        conn.close()
        saas_accounts.invalidate_key_cache()
        saas_agent_mode.invalidate_ingest_cache()
