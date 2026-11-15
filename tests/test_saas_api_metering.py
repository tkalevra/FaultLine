"""SaaS /v1 transport metering — api_calls is metered DISTINCTLY from MCP tool volume.

Proves the seam wired for the seat-card "API CALLS" meter:
  1. bump_for_tool(..., api=False)  — the MCP/REST-shim path — bumps ONLY the tool's flow meter,
     NEVER api_calls (MCP metering byte-for-byte unchanged).
  2. bump_for_tool(..., api=True)   — the /v1 door — bumps the tool's flow meter AND api_calls.
  3. api_calls IS an accepted FLOW metric (bump() no longer drops it).
  4. usage_for_account carries a per-seat {used, limit} api_calls cell (the seat-card feed).
  5. _api_calls_entry: limit is None with no seeded cap (unbounded); reads a plan_limits cap if one
     is later seeded for the api_calls metric.

Run: python3 -m pytest tests/test_saas_api_metering.py -q   (tests/ is gitignored → git add -f)
"""

import asyncio
import time

import pytest

from saas import metering as m


class _Cur:
    def __init__(self, responses):
        self._responses = responses
        self._last = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
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
    def __init__(self, responses):
        self.cur = _Cur(responses)
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


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


ACCT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UID = "11111111-1111-4111-8111-111111111111"


# ── 1/2 — the transport flag decides whether api_calls is bumped ──────────────────────

def test_mcp_path_does_not_bump_api_calls(saas_on, monkeypatch):
    seen = []

    async def _fake_bump(a, u, metric, n=1):
        seen.append(metric)
    monkeypatch.setattr(m, "bump", _fake_bump)
    # MCP/REST-shim: api defaults False → ONLY the tool's flow meter.
    asyncio.run(m.bump_for_tool(ACCT, UID, "recall_memory"))
    assert seen == ["questions"]
    assert "api_calls" not in seen


def test_v1_path_bumps_api_calls_in_addition(saas_on, monkeypatch):
    seen = []

    async def _fake_bump(a, u, metric, n=1):
        seen.append(metric)
    monkeypatch.setattr(m, "bump", _fake_bump)
    # /v1 door: api=True → the tool meter AND the transport-scoped api_calls.
    asyncio.run(m.bump_for_tool(ACCT, UID, "remember_facts", api=True))
    assert seen == ["conversations", "api_calls"]


# ── 3 — api_calls is an accepted flow metric (bump no longer drops it) ────────────────

def test_bump_api_calls_issues_upsert(saas_on, monkeypatch):
    conn = _Conn([])
    monkeypatch.setattr(m.db, "connection", lambda: _CM(conn))
    asyncio.run(m.bump(ACCT, UID, m.API_METRIC))
    sql, params = conn.cur.executed[-1]
    assert "INSERT INTO public.usage_counters" in sql
    assert params[3] == "api_calls"
    assert conn.committed


# ── 4 — usage_for_account carries the per-seat api_calls cell ─────────────────────────

def test_usage_for_account_includes_api_calls_cell(saas_on, monkeypatch):
    # No caps primed → caps_for_plan returns {} → api_calls limit is None (unbounded).
    m._caps_cache["by_plan"] = {}
    m._caps_cache["at"] = time.monotonic()
    try:
        responses = [
            ("FROM public.account_users", [(UID, "advanced")]),
            ("FROM public.usage_gauges", []),
            ("FROM public.usage_counters", [(UID, "api_calls", 173), (UID, "questions", 40)]),
        ]
        monkeypatch.setattr(m.db, "connection", lambda: _CM(_Conn(responses)))
        out = m.usage_for_account(ACCT)
        assert UID in out
        assert out[UID]["api_calls"] == {"used": 173, "limit": None}
    finally:
        m._caps_cache["by_plan"] = None
        m._caps_cache["at"] = 0.0


# ── 5 — _api_calls_entry limit resolution (unbounded by default, cap-aware if seeded) ──

def test_api_calls_entry_unbounded_without_cap():
    m._caps_cache["by_plan"] = {}
    m._caps_cache["at"] = time.monotonic()
    try:
        assert m._api_calls_entry("standard", 7) == {"used": 7, "limit": None}
    finally:
        m._caps_cache["by_plan"] = None
        m._caps_cache["at"] = 0.0


def test_api_calls_entry_reads_seeded_cap():
    m._caps_cache["by_plan"] = {
        "expert": {"api_calls": {"limit": 100000, "enforce": "soft", "per_seat": True}}
    }
    m._caps_cache["at"] = time.monotonic()
    try:
        assert m._api_calls_entry("expert", 250) == {"used": 250, "limit": 100000}
    finally:
        m._caps_cache["by_plan"] = None
        m._caps_cache["at"] = 0.0
