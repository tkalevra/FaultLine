"""SaaS per-seat usage metering — behavior + safety PROOF (no live DB required).

Coverage:
  1. bump is a NO-OP when SAAS is off (never touches the control plane) and NEVER raises.
  2. bump issues the atomic monotonic UPSERT when SAAS is on.
  3. caps ladder loads per seat_type; caps_for_plan resolves the right tier.
  4. _assemble_meters computes %-of-cap, unlimited (∞), over, ordering, memories-primary.
  5. hard_cap_block: no-op SAAS off; blocks memories_kept/topics_trained at cap; never blocks
     recall/soft/under-cap; FAIL-OPEN on a read error (returns None = allow).
  6. usage_for_account joins gauge + counters vs caps per active seat.
  7. _count_memories sums facts+staged+attributes and survives a missing table.
  8. meter_catalog exposes the 5 meters + the cap table.

Run: python3 -m pytest tests/test_saas_metering.py -q   (tests/ is gitignored → git add -f)
"""

import asyncio
import time

import pytest

from saas import metering as m


# ════════════════════════════════════════════════════════════════════════════════
# Fake control-plane connection — dispatches rows by a SQL substring needle
# ════════════════════════════════════════════════════════════════════════════════

class _Cur:
    def __init__(self, responses, fail=False):
        self._responses = responses          # list[(needle, rows)]
        self._last = []
        self._fail = fail
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._fail:
            raise RuntimeError("forced execute failure")
        self._last = []
        for needle, rows in self._responses:
            if needle in sql:
                self._last = rows
                break

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, responses, fail=False):
        self.cur = _Cur(responses, fail=fail)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _CM:
    def __init__(self, conn):
        self._c = conn

    def __enter__(self):
        return self._c

    def __exit__(self, *a):
        return False


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")


@pytest.fixture
def saas_off(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)


# The laddered caps as migration 143 seeds them, in the shape _load_caps returns.
_CAPS = {
    "standard": {"memories_kept": {"limit": 25000, "enforce": "hard", "per_seat": True},
                 "topics_trained": {"limit": 3, "enforce": "hard", "per_seat": True},
                 "conversations": {"limit": 5000, "enforce": "soft", "per_seat": True},
                 "documents": {"limit": 50, "enforce": "soft", "per_seat": True},
                 "questions": {"limit": 25000, "enforce": "soft", "per_seat": True}},
    "advanced": {"memories_kept": {"limit": 100000, "enforce": "hard", "per_seat": True},
                 "topics_trained": {"limit": 15, "enforce": "hard", "per_seat": True},
                 "conversations": {"limit": 25000, "enforce": "soft", "per_seat": True},
                 "documents": {"limit": 300, "enforce": "soft", "per_seat": True},
                 "questions": {"limit": 150000, "enforce": "soft", "per_seat": True}},
    "agent": {"memories_kept": {"limit": 1000000, "enforce": "hard", "per_seat": True},
              "topics_trained": {"limit": 50, "enforce": "hard", "per_seat": True},
              "conversations": {"limit": 250000, "enforce": "soft", "per_seat": True},
              "documents": {"limit": 5000, "enforce": "soft", "per_seat": True},
              "questions": {"limit": -1, "enforce": "soft", "per_seat": True}},
}


@pytest.fixture
def caps(monkeypatch):
    """Prime the caps cache so no DB read is needed for cap lookups."""
    m._caps_cache["by_plan"] = _CAPS
    m._caps_cache["at"] = time.monotonic()
    yield _CAPS
    m._caps_cache["by_plan"] = None
    m._caps_cache["at"] = 0.0


ACCT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UID = "11111111-1111-4111-8111-111111111111"


# ════════════════════════════════════════════════════════════════════════════════
# 1 — bump: no-op when SAAS off, never raises
# ════════════════════════════════════════════════════════════════════════════════

def test_bump_noop_when_saas_off(saas_off, monkeypatch):
    monkeypatch.setattr(m.db, "connection",
                        lambda: (_ for _ in ()).throw(AssertionError("DB touched while SAAS off")))
    asyncio.run(m.bump(ACCT, UID, "conversations"))   # must not raise / not touch DB


def test_bump_swallows_connection_failure(saas_on, monkeypatch):
    def _boom():
        raise RuntimeError("no control plane")
    monkeypatch.setattr(m.db, "connection", _boom)
    asyncio.run(m.bump(ACCT, UID, "conversations"))   # swallowed, no raise


def test_bump_ignores_unknown_metric(saas_on, monkeypatch):
    monkeypatch.setattr(m.db, "connection",
                        lambda: (_ for _ in ()).throw(AssertionError("DB touched for bad metric")))
    asyncio.run(m.bump(ACCT, UID, "memories_kept"))   # gauge metric is not a flow bump
    asyncio.run(m.bump(ACCT, UID, "nonsense"))


def test_bump_issues_upsert_when_on(saas_on, monkeypatch):
    conn = _Conn([])
    monkeypatch.setattr(m.db, "connection", lambda: _CM(conn))
    asyncio.run(m.bump(ACCT, UID, "conversations"))
    sql, params = conn.cur.executed[-1]
    assert "INSERT INTO public.usage_counters" in sql
    assert "ON CONFLICT" in sql and "count = usage_counters.count + EXCLUDED.count" in sql
    assert params[0] == ACCT and params[3] == "conversations"
    assert conn.committed


def test_bump_for_tool_maps_recall_to_questions(saas_on, monkeypatch):
    seen = {}

    async def _fake_bump(a, u, metric, n=1):
        seen["metric"] = metric
    monkeypatch.setattr(m, "bump", _fake_bump)
    asyncio.run(m.bump_for_tool(ACCT, UID, "recall_memory"))
    assert seen["metric"] == "questions"


# ════════════════════════════════════════════════════════════════════════════════
# 2 — caps ladder
# ════════════════════════════════════════════════════════════════════════════════

def test_caps_for_plan_resolves_tier(caps):
    assert m.caps_for_plan("advanced")["memories_kept"]["limit"] == 100000
    assert m.caps_for_plan("agent")["questions"]["limit"] == -1
    assert m.caps_for_plan("nonexistent") == {}


def test_caps_ladder_is_monotonic(caps):
    order = ["standard", "advanced", "expert", "agent"]  # expert absent here → skip gracefully
    seq = [t for t in order if t in _CAPS]
    mem = [_CAPS[t]["memories_kept"]["limit"] for t in seq]
    assert mem == sorted(mem) and mem[0] < mem[-1]


# ════════════════════════════════════════════════════════════════════════════════
# 3 — _assemble_meters math
# ════════════════════════════════════════════════════════════════════════════════

def test_assemble_meters_pct_and_order(caps):
    rows = m._assemble_meters("advanced", {"memories_kept": 50000}, {"conversations": 2500})
    assert [r["metric"] for r in rows] == list(m.METRIC_ORDER)
    mem = rows[0]
    assert mem["metric"] == "memories_kept" and mem["pct"] == 50 and mem["over"] is False
    conv = next(r for r in rows if r["metric"] == "conversations")
    assert conv["used"] == 2500 and conv["pct"] == 10


def test_assemble_meters_unlimited_and_over(caps):
    rows = m._assemble_meters("agent", {"memories_kept": 1000000}, {"questions": 999999})
    q = next(r for r in rows if r["metric"] == "questions")
    assert q["unlimited"] is True and q["pct"] is None and q["over"] is False
    mem = rows[0]
    assert mem["over"] is True and mem["pct"] == 100


# ════════════════════════════════════════════════════════════════════════════════
# 4 — hard_cap_block enforcement (fail-OPEN)
# ════════════════════════════════════════════════════════════════════════════════

def test_hard_cap_noop_when_saas_off(saas_off, caps):
    assert m.hard_cap_block(ACCT, UID, "standard", "remember_facts") is None


def test_hard_cap_never_blocks_recall(saas_on, caps, monkeypatch):
    monkeypatch.setattr(m.db, "connection",
                        lambda: (_ for _ in ()).throw(AssertionError("recall must not read a cap")))
    assert m.hard_cap_block(ACCT, UID, "standard", "recall_memory") is None


def test_hard_cap_blocks_memories_at_limit(saas_on, caps, monkeypatch):
    conn = _Conn([("FROM public.usage_gauges", [(25000,)])])
    monkeypatch.setattr(m.db, "connection", lambda: _CM(conn))
    out = m.hard_cap_block(ACCT, UID, "standard", "remember_facts")
    assert out and out["status"] == "quota_exceeded" and out["metric"] == "memories_kept"
    assert out["limit"] == 25000 and out["used"] == 25000


def test_hard_cap_allows_memories_under_limit(saas_on, caps, monkeypatch):
    conn = _Conn([("FROM public.usage_gauges", [(24999,)])])
    monkeypatch.setattr(m.db, "connection", lambda: _CM(conn))
    assert m.hard_cap_block(ACCT, UID, "standard", "ingest_document") is None


def test_hard_cap_topics_uses_monthly_counter(saas_on, caps, monkeypatch):
    conn = _Conn([("FROM public.usage_counters", [(3,)])])
    monkeypatch.setattr(m.db, "connection", lambda: _CM(conn))
    out = m.hard_cap_block(ACCT, UID, "standard", "learn_facts")
    assert out and out["metric"] == "topics_trained" and out["limit"] == 3


def test_hard_cap_fail_open_on_read_error(saas_on, caps, monkeypatch):
    conn = _Conn([], fail=True)
    monkeypatch.setattr(m.db, "connection", lambda: _CM(conn))
    # A control-plane read failure must ALLOW (fail-open), never deny a paying customer.
    assert m.hard_cap_block(ACCT, UID, "standard", "remember_facts") is None


def test_hard_cap_soft_meter_never_blocks(saas_on, monkeypatch):
    # conversations is soft even though it is a write meter → _HARD_GATE has no entry beyond
    # the two hard meters; a soft tool is not gated regardless of usage.
    monkeypatch.setattr(m.db, "connection",
                        lambda: (_ for _ in ()).throw(AssertionError("soft meter must not read a cap")))
    assert m.hard_cap_block(ACCT, UID, "standard", "retract_fact") is None


# ════════════════════════════════════════════════════════════════════════════════
# 5 — usage_for_account
# ════════════════════════════════════════════════════════════════════════════════

def test_usage_for_account_joins_gauge_and_counters(saas_on, caps, monkeypatch):
    responses = [
        ("FROM public.account_users", [(UID, "advanced")]),
        ("FROM public.usage_gauges", [(UID, "memories_kept", 40000)]),
        ("FROM public.usage_counters", [(UID, "conversations", 5000)]),
    ]
    monkeypatch.setattr(m.db, "connection", lambda: _CM(_Conn(responses)))
    out = m.usage_for_account(ACCT)
    assert UID in out and out[UID]["seat_type"] == "advanced"
    mem = out[UID]["meters"][0]
    assert mem["metric"] == "memories_kept" and mem["used"] == 40000 and mem["pct"] == 40


def test_usage_for_account_empty_when_saas_off(saas_off):
    assert m.usage_for_account(ACCT) == {}


# ════════════════════════════════════════════════════════════════════════════════
# 6 — _count_memories (gauge sampling) survives a missing table
# ════════════════════════════════════════════════════════════════════════════════

def test_count_memories_sums_three_counts(monkeypatch):
    responses = [
        ("FROM facts", [(100,)]),
        ("FROM staged_facts", [(30,)]),
        ("FROM entity_attributes", [(20,)]),
    ]
    conn = _Conn(responses)
    assert m._count_memories(conn, "faultline_test") == 150


def test_count_memories_partial_on_missing_table(monkeypatch):
    class _PartialCur(_Cur):
        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            if "SET search_path" in sql:
                self._last = []
                return
            if "FROM staged_facts" in sql:
                raise RuntimeError("relation does not exist")
            self._last = [(100,)] if "FROM facts" in sql else [(20,)]

    class _PartialConn(_Conn):
        def __init__(self):
            super().__init__([])
            self.cur = _PartialCur([])
    conn = _PartialConn()
    # facts(100) + attributes(20); staged missing → still returns the partial sum (not None)
    assert m._count_memories(conn, "faultline_test") == 120


# ════════════════════════════════════════════════════════════════════════════════
# 7 — meter_catalog shape
# ════════════════════════════════════════════════════════════════════════════════

def test_meter_catalog_shape(caps):
    cat = m.meter_catalog()
    metrics = [x["metric"] for x in cat["meters"]]
    assert metrics == list(m.METRIC_ORDER)
    assert cat["caps"]["agent"]["memories_kept"]["limit"] == 1000000
    hard = {x["metric"] for x in cat["meters"] if x["hardness"] == "hard"}
    assert hard == {"memories_kept", "topics_trained"}
