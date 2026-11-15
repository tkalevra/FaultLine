"""THE EPISODIC DRAIN MUST NOT BE ABLE TO EAT THE CYCLE.

MEASURED 2026-08-27 on the local rig (faultline-api up 02:46, one re_embedder cycle observed):
`reextract_episodic` spent 403.4 s on ONE tenant — 45% of the whole elapsed PHASE-2b pass —
while the other 1,212 tenants in the same pass cost a MEDIAN of 0.03 s each. The 403 s is two
serial LLM timeouts on a SINGLE row:

    02:58:01  llm_call_async.attempt_start operation=REFRAME timeout_seconds=180.0
    03:01:01  re_embedder.reextract_spine_failed (falling back to /extract/rewrite): timed out
    03:04:01  re_embedder.reextract_row_failed episodic_id=1: timed out

Everything ordered after the drain is what it starves: for that tenant the document drain and
the Class-C promotion/decay/strike jobs (all BELOW it in the same per-tenant block), and for
every other tenant the whole rest of the serial pass, including the ontology-growth sweeps that
only begin once the pass is done.

⛔ WHY THE EXISTING STORM BOUND DID NOT CATCH IT — AND WHY ITS OWN TEST STAYED GREEN.
`REEXTRACT_FAIL_ABORT` was built for exactly this shape (63cdd1cf: "a sick brain fails SLOW").
It arms off `consecutive_failures`. 28215456 then added `_exempt` — correctly, to stop a lane
outage stamping `reextracted_at` and freezing turns out of eligibility forever — and wired it to
that SAME counter: `if not _exempt: consecutive_failures += 1`.

A REAL timeout is `httpx.ReadTimeout`, a subclass of `httpx.TransportError` → `_backend_down` →
`_exempt` → the counter never moves. The abort's designed trigger became its exclusion.

`tests/test_ctier_retained_turn_drain.py::test_storm_abort_stops_the_batch_after_n_consecutive_failures`
passes anyway, because its fake raises `RuntimeError("timed out")` — a shape the real lane never
produces, and one that is NOT exempt. A green test over an exception the pipeline does not emit.
Every failure pin in THIS file therefore raises the exception types the lane actually raises.

WHAT IS PINNED
  1. LANE-DOWN ABORT (unflagged) — the scheduling half of `_exempt`, split back out from the
     stamping half. Ablation: delete `if _lane_down: break` and 1-4 go red.
  2. THE STAMPING HALF IS UNCHANGED — aborting must not stamp, or the abort re-opens the very
     data-loss bug the exemption was written to close.
  3. PER-TENANT WALL-CLOCK BUDGET — bounds the healthy-but-slow case a row count cannot.
     Ablation: set `_REEXTRACT_TENANT_BUDGET_S = 0` and 7-9 go red.
  4. CYCLE BUDGET + ROTATION CURSOR — bounds the aggregate, and rotates so capping the budget
     does not simply move the starvation to the tail. Ablation: return `(armed, armed)` from
     `_reextract_cycle_gate` and 10-15 go red.

PURE tests — no DB, no network, no LLM, no clock (monotonic is faked where it matters).
"""
import httpx
import pytest

import src.re_embedder.embedder as E


# ── Fakes (same shape as tests/test_ctier_retained_turn_drain.py) ─────────────

class _FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.owner.executed.append((sql, params))

    def fetchall(self):
        return self.owner.rows

    def fetchone(self):
        return (self.owner.backlog,)

    @property
    def rowcount(self):
        return 0


class _FakeConn:
    def __init__(self, rows=None, backlog=0):
        self.rows = rows if rows is not None else []
        self.backlog = backlog
        self.executed = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def _rows(n, start=1, source=None):
    return [(start + i, f"turn number {start + i}", None, "STATEMENT", None, source)
            for i in range(n)]


def _stamp_sqls(conn):
    """Every UPDATE that writes `reextracted_at` — the stamping half."""
    return [s for s, _ in conn.executed if "reextracted_at" in s and "UPDATE" in s]


# THE EXCEPTIONS THE LANE ACTUALLY RAISES.
# A read timeout against the brain and a 5xx from /ingest are the two shapes observed live;
# both classify as `_backend_down` in `reextract_episodic`'s handler. Constructing them here
# rather than a RuntimeError is the whole point of this file.
def _real_timeout():
    return httpx.ReadTimeout("timed out")


def _real_backend_5xx():
    req = httpx.Request("POST", "http://localhost:8000/ingest")
    return httpx.HTTPStatusError(
        "Server error '500 Internal Server Error'",
        request=req, response=httpx.Response(500, request=req))


def _raiser(monkeypatch, exc_factory, calls):
    def _boom(raw_text, user_id, backend_url, statement_route, preserve_source=None):
        calls.append(raw_text)
        raise exc_factory()
    monkeypatch.setattr(E, "_reextract_row_edges", _boom)


# ── 0. THE REGRESSION WITNESS ─────────────────────────────────────────────────

def test_the_real_timeout_is_exempt_from_the_storm_counter():
    """Not a behaviour pin — a statement of the defect, so nobody re-derives it.

    The storm bound counts `consecutive_failures`, and the exemption clears it for exactly the
    exception the lane really raises. If this ever goes red the exemption changed shape and the
    lane-down abort below should be re-read against it.
    """
    assert isinstance(_real_timeout(), httpx.TransportError)
    assert _real_backend_5xx().response.status_code >= 500


# ── 1. LANE-DOWN ABORTS THE BATCH (unflagged) ─────────────────────────────────

def test_lane_down_aborts_after_the_first_real_timeout(monkeypatch):
    """One 360 s answer is enough. Ablation: delete `if _lane_down: break` → 20 calls."""
    calls = []
    _raiser(monkeypatch, _real_timeout, calls)
    conn = _FakeConn(rows=_rows(20), backlog=20)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=20)
    assert len(calls) == 1, (
        f"a proven-down lane must cost ONE row, not {len(calls)} — that is the 403 s wedge")


def test_lane_down_aborts_on_a_backend_5xx(monkeypatch):
    """The backend is a lane too: a 500 from /ingest answers the same for every row."""
    calls = []
    _raiser(monkeypatch, _real_backend_5xx, calls)
    conn = _FakeConn(rows=_rows(20), backlog=20)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=20)
    assert len(calls) == 1


def test_lane_down_abort_is_not_flag_gated(monkeypatch):
    """The storm defence has been dark since it shipped because its flag defaults OFF. The
    scheduling half must not inherit that — with REEXTRACT_BACKLOG_DRAIN OFF it still aborts."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", False)
    calls = []
    _raiser(monkeypatch, _real_timeout, calls)
    conn = _FakeConn(rows=_rows(12))
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=12)
    assert len(calls) == 1


def test_a_paced_deferral_is_not_an_outage(monkeypatch):
    """`_paced` shares the exemption but NOT the abort: pacing is a healthy lane waiting out a
    budget window, and aborting on it would back off from a brain that is fine."""
    calls = []

    def _paced(raw_text, user_id, backend_url, statement_route, preserve_source=None):
        calls.append(raw_text)
        raise RuntimeError("extraction degraded (brain unavailable) — rate_deferred")

    monkeypatch.setattr(E, "_reextract_row_edges", _paced)
    conn = _FakeConn(rows=_rows(6))
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=6)
    assert len(calls) == 6, "a paced lane must keep its whole batch"


# ── 2. THE STAMPING HALF IS UNCHANGED ─────────────────────────────────────────

def test_lane_down_abort_stamps_nothing(monkeypatch):
    """The exemption exists because stamping a lane-outage row froze it out of
    `reextracted_at IS NULL` FOREVER. Aborting must not re-open that from the other side:
    the attempted row and every un-attempted row stay NULL and are re-selected next cycle."""
    calls = []
    _raiser(monkeypatch, _real_timeout, calls)
    conn = _FakeConn(rows=_rows(20), backlog=20)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=20)
    assert _stamp_sqls(conn) == [], "a lane-down abort must never stamp a row"


def test_lane_down_abort_reports_zero_processed(monkeypatch):
    """No success shape for work that did not happen — the return is the count STAMPED."""
    calls = []
    _raiser(monkeypatch, _real_timeout, calls)
    conn = _FakeConn(rows=_rows(20), backlog=20)
    assert E.reextract_episodic(
        conn, "http://backend", user_id="u" * 36, batch_size=20) == 0


# ── 3. PER-TENANT WALL-CLOCK BUDGET ───────────────────────────────────────────

def test_both_budgets_ship_ON():
    """THE DEFAULT IS THE TERM. Every other test here monkeypatches the constant, so without
    this pin the shipped default could be silently set to 0 and the whole file stays green —
    exactly how `REEXTRACT_FAIL_ABORT` came to be a storm defence nobody was running. The
    remedy for a live wedge must not itself default to OFF."""
    assert E._REEXTRACT_TENANT_BUDGET_S > 0, "the per-tenant drain budget must ship ON"
    assert E._REEXTRACT_CYCLE_BUDGET_S > 0, "the cycle-wide drain budget must ship ON"


def test_budgets_are_env_overridable_to_zero(monkeypatch):
    """`=0` on either knob is the documented byte-for-byte rollback lever, so an operator can
    reproduce the legacy behaviour without a deploy."""
    monkeypatch.setenv("REEXTRACT_TENANT_BUDGET_S", "0")
    monkeypatch.setenv("REEXTRACT_CYCLE_BUDGET_S", "0")
    import os as _os
    assert max(0.0, float(_os.getenv("REEXTRACT_TENANT_BUDGET_S", "90"))) == 0.0
    assert max(0.0, float(_os.getenv("REEXTRACT_CYCLE_BUDGET_S", "600"))) == 0.0



def _clock(monkeypatch, per_row_seconds):
    """A monotonic clock that advances `per_row_seconds` on every read after the first."""
    state = {"t": 0.0, "n": 0}

    def _mono():
        state["n"] += 1
        if state["n"] > 1:
            state["t"] += per_row_seconds
        return state["t"]

    monkeypatch.setattr(E.time, "monotonic", _mono)


def test_tenant_budget_stops_a_slow_batch_between_rows(monkeypatch):
    """The healthy-but-slow case a row count cannot bound: 20 rows at 40 s each under a 90 s
    budget. Ablation: `_REEXTRACT_TENANT_BUDGET_S = 0` → all 20 run."""
    monkeypatch.setattr(E, "_REEXTRACT_TENANT_BUDGET_S", 90.0)
    calls = []
    monkeypatch.setattr(
        E, "_reextract_row_edges",
        lambda raw_text, *a, **k: calls.append(raw_text) or [])
    _clock(monkeypatch, 40.0)
    conn = _FakeConn(rows=_rows(20), backlog=20)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=20)
    assert 0 < len(calls) < 20, f"budget must truncate the batch, ran {len(calls)}/20"


def test_tenant_budget_always_grants_the_first_row(monkeypatch):
    """A budget that can deny a tenant its FIRST row lets a slow neighbour starve it outright —
    the same wedge, one level up. Even a budget already blown grants row one."""
    monkeypatch.setattr(E, "_REEXTRACT_TENANT_BUDGET_S", 0.001)
    calls = []
    monkeypatch.setattr(
        E, "_reextract_row_edges",
        lambda raw_text, *a, **k: calls.append(raw_text) or [])
    _clock(monkeypatch, 10_000.0)
    conn = _FakeConn(rows=_rows(20), backlog=20)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=20)
    assert len(calls) == 1


def test_tenant_budget_zero_is_the_legacy_unbounded_batch(monkeypatch):
    """`REEXTRACT_TENANT_BUDGET_S=0` is the byte-for-byte rollback lever."""
    monkeypatch.setattr(E, "_REEXTRACT_TENANT_BUDGET_S", 0.0)
    calls = []
    monkeypatch.setattr(
        E, "_reextract_row_edges",
        lambda raw_text, *a, **k: calls.append(raw_text) or [])
    _clock(monkeypatch, 10_000.0)
    conn = _FakeConn(rows=_rows(9), backlog=9)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=9)
    assert len(calls) == 9


def test_a_fast_healthy_batch_is_untouched(monkeypatch):
    """NOT A DRAIN THROTTLE. A tenant that finishes inside the budget drains in full, so the
    steady-state rate on a healthy brain is unchanged."""
    monkeypatch.setattr(E, "_REEXTRACT_TENANT_BUDGET_S", 90.0)
    calls = []
    monkeypatch.setattr(
        E, "_reextract_row_edges",
        lambda raw_text, *a, **k: calls.append(raw_text) or [])
    _clock(monkeypatch, 0.5)
    conn = _FakeConn(rows=_rows(25), backlog=25)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=25)
    assert len(calls) == 25


# ── 4. CYCLE BUDGET + ROTATION CURSOR ─────────────────────────────────────────

def test_cycle_gate_arms_immediately_with_no_cursor():
    armed, may = E._reextract_cycle_gate(False, None, "seat-a", spent=0.0, budget=600.0)
    assert (armed, may) == (True, True)


def test_cycle_gate_denies_until_the_cursor_is_reached():
    """The rotation half. Ablation: return `(armed, armed)` → this goes red."""
    armed, may = E._reextract_cycle_gate(False, "seat-c", "seat-a", spent=0.0, budget=600.0)
    assert (armed, may) == (False, False)
    armed, may = E._reextract_cycle_gate(armed, "seat-c", "seat-b", spent=0.0, budget=600.0)
    assert (armed, may) == (False, False)
    armed, may = E._reextract_cycle_gate(armed, "seat-c", "seat-c", spent=0.0, budget=600.0)
    assert (armed, may) == (True, True), "the cursor seat must be the one that arms the cycle"


def test_cycle_gate_stays_armed_once_reached():
    """Arming is a latch — the seats AFTER the cursor are exactly the starved tail."""
    armed = True
    for seat in ("seat-d", "seat-e", "seat-f"):
        armed, may = E._reextract_cycle_gate(armed, "seat-c", seat, spent=0.0, budget=600.0)
        assert (armed, may) == (True, True)


def test_cycle_gate_denies_once_the_budget_is_spent():
    armed, may = E._reextract_cycle_gate(True, None, "seat-a", spent=600.0, budget=600.0)
    assert (armed, may) == (True, False)
    armed, may = E._reextract_cycle_gate(True, None, "seat-a", spent=599.9, budget=600.0)
    assert (armed, may) == (True, True)


def test_cycle_gate_budget_zero_is_the_legacy_uncapped_pass():
    """`REEXTRACT_CYCLE_BUDGET_S=0` is the rollback lever: every armed seat drains."""
    armed, may = E._reextract_cycle_gate(True, None, "seat-a", spent=99_999.0, budget=0.0)
    assert (armed, may) == (True, True)


def test_cycle_gate_matches_the_cursor_on_string_form():
    """`ready_schemas` yields psycopg2 UUID objects, not str. The cursor is stored as `str(...)`,
    so the comparison must be on the string form or the cursor NEVER arms and every seat is
    denied forever — the fail-safe in the loop catches that, but it must not happen at all."""
    import uuid
    u = uuid.uuid4()
    armed, may = E._reextract_cycle_gate(False, str(u), u, spent=0.0, budget=600.0)
    assert (armed, may) == (True, True)


@pytest.mark.parametrize("budget", [0.0, 600.0])
def test_cycle_gate_never_denies_an_armed_seat_under_its_own_budget(budget):
    """Fail-safe direction is RUN (sweep_ledger invariant 1): armed + under budget → drain."""
    armed, may = E._reextract_cycle_gate(True, None, "seat", spent=0.0, budget=budget)
    assert may is True
