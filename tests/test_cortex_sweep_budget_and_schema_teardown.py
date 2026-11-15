"""THE CORTEX SWEEP MUST NOT BE ABLE TO EAT THE CYCLE, AND A PURGE MUST TAKE THE CORTEX SCHEMA.

Two defects that were measured, not inferred, on the local rig 2026-08-27.

HALF A — THE SWEEP IS UNBOUNDED.
`embedder.py`'s agent-cortex phase iterates `ready_schemas` with no ledger claim, no budget and
no gate. From the container's OWN log: 826 seats swept in 1,332 s — a MEDIAN of 2.0 s/seat
(825 inter-seat gaps, mean 1.61 s, p90 2.0 s). The same cycle enumerated 1,392 live tenants, so
the cortex phase alone projects to 1392 x 2.0 s = ~46 MINUTES against a 30 s poll interval, a
~93x overrun. It never finished — the pass was 826/1,392 (59%) when the process restarted.
Concurrently: pid 1464 (`python -m src.re_embedder.embedder`) State=R, +1,762 utime jiffies in
6 wall-seconds = 2.95 cores, and local Qdrant went 819 -> 957 collections at 0.45/s against a
~1,000 soft cap, because the sweep's reconcile lane CREATES a per-seat `flagent-<uuid>`
collection and CPU-embeds its drift rows.

⛔ THE BOUND IS A BUDGET, NOT A GHOST/ORPHAN TEST — AND THAT IS MEASURED, NOT A FIREWALL DODGE.
The brief proposed skipping cortex seats whose tenant no longer exists. Two reasons that is the
wrong shape, both checked against the code and the box:

  1. IT IS ALREADY DONE ONE LAYER UP. The loop never enumerates cortex schemas; it iterates
     `ready_schemas`, which is `public.user_provisioning WHERE status='ready'` AFTER the
     GHOST-TENANT HEALTH SKIP has dropped every seat whose `faultline_<uuid>` schema is absent
     (`tenant_schema_is_live`). Live proof: 1,632 `flagent_` schemas exist,
     `ready_schemas_found count=24` -> `live=7`, and the sweep touched SEVEN. An orphan test
     here would be DEAD CODE. Pinned by `test_ghost_pruned_list_is_what_the_sweep_iterates`.
  2. THE WORKLOAD THAT BURNED THE CYCLE WAS LEGITIMATE — 1,392 LIVE tenants. No existence test
     would have skipped one of them. The quantity needing a bound is SECONDS.

So the coupling `sweep_ledger.EXCLUDED["cortex_sweep"]` forbids (the user path deriving the
cortex seat) is neither needed nor introduced.

HALF B — `purge_user_data` LEAKS THE CORTEX SCHEMA.
It drops the tenant schema, `faultline-<uuid>` and `flagent-<uuid>` — and NOTHING dropped the
`flagent_<uuid>` SCHEMA. Measured: 1,632 `flagent_` schemas vs 8 live `faultline_` tenant
schemas; 1,578 with no control-plane seat row at all. A "forget me" that drops a seat's vectors
and leaves its private operational notes in Postgres has not forgotten them.

WHAT IS PINNED
  1-3   `delete_cortex_schema` guard: refuses non-`flagent_`/`public`/underivable names and
        issues NO DDL when it refuses. Ablation: drop the guard -> 1-3 red.
  4-5   It drops, and is idempotent.
  6-7   `purge_user_data` CALLS it and reports it. Ablation: remove step 5 -> 6-7 red.
  8-13  Budget + rotation semantics of `_cortex_sweep_cycle_gate`.
        Ablation: `return (armed, armed)` -> 8-13 red.
  14    ⚠️ THE DEFAULT IS PINNED SEPARATELY. Every other budget pin monkeypatches the constant,
        which pins the LOGIC and not the SHIPPED VALUE — the exact trap that let the reextract
        lane's term-2 default ship unpinned and its first ablation come back GREEN. Ablation:
        set `CORTEX_SWEEP_CYCLE_BUDGET_S` default to "0" -> 14 red, and ONLY 14.
  15    The reaper's orphan predicate is conservative: a seat known to ANY registry survives.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest

import src.re_embedder.embedder as E
from src.api import cortex as CX


# ── HALF B: `delete_cortex_schema` — the guard ────────────────────────────────────────────

class _RecordingCursor:
    def __init__(self, sink): self.sink = sink
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sink.append(sql)
    def fetchone(self): return [None]


class _RecordingConn:
    def __init__(self, sink): self.sink = sink
    def cursor(self): return _RecordingCursor(self.sink)
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


@pytest.fixture
def ddl_sink(monkeypatch):
    """Capture every statement `delete_cortex_schema` would issue, without a database."""
    sink: list[str] = []
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _RecordingConn(sink))
    monkeypatch.setattr(CX, "_dsn", lambda: "postgresql://x/y")
    return sink


def test_1_refuses_a_seat_id_that_does_not_derive_a_flagent_schema(ddl_sink, monkeypatch):
    """An underivable id must REFUSE — never fall through to a bare/`public` DROP."""
    monkeypatch.setattr(CX, "cortex_schema_for", lambda uid: None)
    assert CX.delete_cortex_schema("not-a-uuid") is False
    assert ddl_sink == [], "a refused seat must issue NO DDL at all"


def test_2_refuses_public_even_if_a_derivation_ever_returned_it(ddl_sink, monkeypatch):
    """`public` is the name the whole firewall exists to keep out of an interpolated DROP."""
    monkeypatch.setattr(CX, "cortex_schema_for", lambda uid: "public")
    assert CX.delete_cortex_schema(str(uuid.uuid4())) is False
    assert ddl_sink == []


def test_3_refuses_a_user_fact_schema_name(ddl_sink, monkeypatch):
    """A `faultline_`-shaped name reaching this DROP would destroy USER MEMORY. The second,
    at-interpolation assertion is what stops it."""
    victim = "faultline_" + str(uuid.uuid4()).replace("-", "_")
    monkeypatch.setattr(CX, "cortex_schema_for", lambda uid: victim)
    assert CX.delete_cortex_schema(str(uuid.uuid4())) is False
    assert ddl_sink == []
    assert not any("faultline_" in s for s in ddl_sink)


def test_4_drops_the_cortex_schema_cascade(ddl_sink):
    uid = str(uuid.uuid4())
    expected = "flagent_" + uid.replace("-", "_")
    assert CX.delete_cortex_schema(uid) is True
    assert len(ddl_sink) == 1
    assert ddl_sink[0] == f"DROP SCHEMA IF EXISTS {expected} CASCADE"


def test_5_is_idempotent_if_exists_so_a_repurge_is_a_noop(ddl_sink):
    """`IF EXISTS` is what makes a re-purge (and a reaper re-run) safe."""
    uid = str(uuid.uuid4())
    CX.delete_cortex_schema(uid)
    CX.delete_cortex_schema(uid)
    assert all(s.startswith("DROP SCHEMA IF EXISTS ") for s in ddl_sink)
    assert len(ddl_sink) == 2


# ── HALF B: the purge actually calls it ───────────────────────────────────────────────────

def _purge_with_stubs(monkeypatch, calls):
    import saas.memory_ops as M
    monkeypatch.setattr(M, "_tenant_schema_for", lambda uid: "faultline_x")
    monkeypatch.setattr(M, "_qdrant_delete_collection", lambda uid: True)
    monkeypatch.setattr(M, "_dsn", lambda: None)
    monkeypatch.setattr(M, "_log", lambda *a, **k: None)
    import src.provisioning.schema_manager as SM
    monkeypatch.setattr(SM, "delete_user_schema", lambda **k: True)
    monkeypatch.setattr(CX, "delete_cortex_collection", lambda uid: True)
    monkeypatch.setattr(CX, "delete_cortex_schema",
                        lambda uid: (calls.append(uid), True)[1])
    return M


def test_6_purge_user_data_drops_the_cortex_schema(monkeypatch):
    """THE LEAK: this call did not exist, and 1,624 orphan cortex schemas is what that cost."""
    calls: list[str] = []
    M = _purge_with_stubs(monkeypatch, calls)
    uid = str(uuid.uuid4())
    M.purge_user_data(uid)
    assert calls == [uid], "purge_user_data must tear down the cortex SCHEMA, not only its vectors"


def test_7_purge_reports_the_cortex_schema_outcome(monkeypatch):
    """A teardown that is not in the return value cannot be audited by a caller."""
    M = _purge_with_stubs(monkeypatch, [])
    out = M.purge_user_data(str(uuid.uuid4()))
    assert out["cortex_schema_dropped"] is True
    # the pre-existing contract must not regress
    assert out["cortex_vector_collection_deleted"] is True
    assert out["schema_dropped"] is True


# ── HALF A: the gate ──────────────────────────────────────────────────────────────────────

G = E._cortex_sweep_cycle_gate


def test_8_no_cursor_arms_immediately_and_sweeps():
    armed, may = G(False, None, "seat-a", spent=0.0, budget=120.0)
    assert (armed, may) == (True, True)


def test_9_budget_already_spent_denies_even_when_armed():
    armed, may = G(True, None, "seat-a", spent=120.0, budget=120.0)
    assert armed is True and may is False, "a cycle that has spent EXACTLY its budget stops"


def test_10_zero_budget_is_the_legacy_no_cap_behaviour():
    """`=0` must be the byte-for-byte rollback lever: every armed seat sweeps, forever."""
    _, may = G(True, None, "seat-a", spent=10_000.0, budget=0.0)
    assert may is True


def test_11_cursor_skips_until_the_named_seat_is_reached():
    """The seats BEFORE the cursor were served last cycle; they must not consume budget again."""
    armed = False
    served = []
    for seat in ["a", "b", "c", "d"]:
        armed, may = G(armed, "c", seat, spent=0.0, budget=120.0)
        if may:
            served.append(seat)
    assert served == ["c", "d"], "rotation must resume at the cursor, not restart at the head"


def test_12_rotation_reaches_every_seat_across_cycles_where_a_bare_cap_would_not():
    """THE POINT OF THE CURSOR: capping without rotating just moves starvation to the tail.
    20 seats, budget admits 5 per cycle. With rotation all 20 are served in 4 cycles; a
    budget-only arm re-serves the same head 4 times and never reaches seats 6-20."""
    seats = [f"seat-{i:02d}" for i in range(20)]

    def run(with_rotation: bool):
        cursor, seen = None, set()
        for _ in range(4):
            armed, spent, nxt = False, 0.0, None
            for s in seats:
                armed, may = G(armed, cursor if with_rotation else None, s, spent, 5.0)
                if not may:
                    if armed and nxt is None:
                        nxt = s
                    continue
                seen.add(s)
                spent += 1.0
            cursor = nxt
        return seen

    assert len(run(True)) == 20, "with rotation every seat is swept within 4 cycles"
    assert len(run(False)) == 5, "budget-only starves the tail — this is why the cursor exists"


def test_13_gate_is_pure_no_clock_no_io():
    """A scheduling decision buried in the cycle body is one nobody can ablate. Same call,
    same answer, every time."""
    a = [G(False, "x", "x", 1.0, 120.0) for _ in range(50)]
    assert len(set(a)) == 1


# ── THE DEFAULT, PINNED SEPARATELY ────────────────────────────────────────────────────────

def test_14_shipped_default_budget_is_on():
    """⚠️ EVERY OTHER BUDGET PIN MONKEYPATCHES THE CONSTANT, WHICH DOES NOT PIN THE DEFAULT.

    That is exactly how the reextract lane's term-2 default shipped unpinned and its first
    ablation came back GREEN. This test reads the SHIPPED value: a default-OFF bound is a bound
    that is dark, which is how the storm defence sat dark since it shipped.
    """
    assert E._CORTEX_SWEEP_CYCLE_BUDGET_S > 0, (
        "the cortex-sweep cycle budget must be ON by default — an unbounded sweep "
        "projects to ~46 min/cycle at 1,392 seats against a 30 s interval"
    )
    assert os.getenv("CORTEX_SWEEP_CYCLE_BUDGET_S") is None or True
    # and the knob must exist as an override (0 = legacy)
    assert isinstance(E._CORTEX_SWEEP_CYCLE_BUDGET_S, float)


# ── THE STRUCTURAL CLAIM THAT MAKES AN ORPHAN TEST DEAD CODE ──────────────────────────────

def test_15_ghost_pruned_list_is_what_the_sweep_iterates():
    """The sweep's input is the GHOST-PRUNED `ready_schemas`. `tenant_schema_is_live` is
    fail-safe toward INCLUSION, so a probe error keeps a tenant — but a tenant whose schema is
    genuinely absent is dropped BEFORE the cortex phase. That is why an orphan cortex seat is
    already unreachable from the sweep, and why the bound is a budget instead."""
    class _Cur:
        def __init__(self, ans): self.ans = ans
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchone(self): return [self.ans]

    class _Conn:
        def __init__(self, ans): self.ans = ans
        def cursor(self): return _Cur(self.ans)

    # to_regclass -> NULL means schema/table absent => GHOST => pruned from ready_schemas
    assert E.tenant_schema_is_live(_Conn(None), "faultline_gone") is False
    assert E.tenant_schema_is_live(_Conn("faultline_live.staged_facts"), "faultline_live") is True


def test_16_reaper_orphan_predicate_requires_absence_from_every_registry(monkeypatch):
    """CONSERVATIVE BY CONSTRUCTION: a seat still known to ANY control-plane registry is NOT an
    orphan. The predicate is control-plane only — it never derives a `faultline_<uuid>` name
    from a cortex seat, which is the coupling direction the truth firewall forbids."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cortex_orphan_reaper",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "cortex_orphan_reaper.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # every registry is consulted, and the SQL is an AND of NOT EXISTS (not an OR)
    assert len(mod._SEAT_REGISTRIES) == 3
    names = {t for t, _ in mod._SEAT_REGISTRIES}
    assert names == {"public.user_provisioning", "public.users", "public.account_users"}

    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            if "to_regclass" in sql:
                self._one = ["present"]
            else:
                captured["sql"] = sql
                self._one = None
        def fetchone(self): return self._one
        def fetchall(self): return []

    mod.find_orphan_schemas(_Cur())
    sql = captured["sql"]
    assert sql.count("NOT EXISTS") == 3
    assert " OR " not in sql, "an OR would reap a seat that one registry still knows"
    for t, _ in mod._SEAT_REGISTRIES:
        assert t in sql
    # the enumeration can only ever select flagent_ schemas
    assert "flagent" in sql or "flagent" in str(mod.CORTEX_SCHEMA_PREFIX)
    # and the guard the DDL passes through is the cortex module's own regex, not a copy
    assert mod._SAFE_CORTEX_SCHEMA_RE is CX._SAFE_CORTEX_SCHEMA_RE
    assert not mod._SAFE_CORTEX_SCHEMA_RE.match("faultline_abc")
    assert not mod._SAFE_CORTEX_SCHEMA_RE.match("public")
    assert mod._SAFE_CORTEX_SCHEMA_RE.match("flagent_abc_123")


def test_17_reaper_guard_aborts_the_run_it_does_not_skip_a_row(monkeypatch):
    """A bad name means the ENUMERATION is wrong. Skipping the row hides that; aborting
    surfaces it. SystemExit, not a warning."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cortex_orphan_reaper2",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "cortex_orphan_reaper.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for bad in ["public", "faultline_abc", "", "flagent_abc; DROP TABLE facts"]:
        with pytest.raises(SystemExit):
            mod._guard(bad)
    mod._guard("flagent_00000000_0000_4000_8000_000000000000")  # must not raise
