"""DRAIN WATCHDOG — a document sitting non-terminal past the threshold must SCREAM.

Founding wound: prod doc 14 sat status='pending' for FIFTEEN DAYS producing zero
signal (owner: 'this has been ongoing now for literal weeks'). The sweep in
_drain_watchdog_once closes that class. These pins hold its contract:

1. a never-claimed pending doc older than the threshold fires a CRITICAL (class a)
2. fresh docs are silent                                              (anti-spam)
3. a claimed-but-stalled processing doc fires                         (class b)
4. terminal states (ready/partial/error) are never flagged            (retry lane's turf)
5. DOC_WATCHDOG_HOURS=0 disables the sweep entirely                   (operator lever)
6. one bad schema does not silence the others                         (per-schema fail-safe)
7. the watchdog only calls REAL logging.Logger API                    (fake-divergence pin:
   the first cut called log.crit() — a method only the hand-rolled test double had —
   so 6 pins stayed green while production raised AttributeError and the CRITICAL
   degraded to a cycle_error warning; the capture fixture IS a stock Logger now, so
   any invented API fails here first, and this pin fails at review time)
8. garbage DOC_WATCHDOG_HOURS warns and falls back to 24, still sweeps (no silent guess)
9. a schema name outside the tenant contract is skipped LOUDLY, never interpolated
   into SET search_path                                               (injection guard)

Fast, DB-free: a faked psycopg2-style connection answers the schema listing and
per-schema starved-row SELECTs; a REAL logging.Logger subclass captures every record.
"""

import inspect
import logging
import re

import pytest

from src.re_embedder import embedder


class _CaptureHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self._sink = sink

    def emit(self, record):
        self._sink.append((record.levelno, record.getMessage()))


class CaptureLog(logging.Logger):
    """A REAL logging.Logger — not a duck-typed stand-in.

    The founding defect this class pins: the first watchdog cut was tested against a
    hand-rolled fake whose .crit() method does not exist on logging.Logger, so the
    production call log.crit(...) raised AttributeError, every CRITICAL degraded to a
    cycle_error warning, and the six original pins could not see it. This capture IS
    a stock Logger (records via a real Handler), so any method the implementation
    invents now fails loudly in the test run instead of silently in production."""

    def __init__(self, name="drain_watchdog_test_capture"):
        super().__init__(name, logging.INFO)
        self.records = []
        self.addHandler(_CaptureHandler(self.records))
        self.propagate = False  # keep test output clean; we assert on self.records

    @property
    def crits(self):
        return [m for lvl, m in self.records if lvl >= logging.CRITICAL]

    @property
    def warnings(self):
        return [m for lvl, m in self.records if lvl == logging.WARNING]

    @property
    def infos(self):
        return [m for lvl, m in self.records if lvl == logging.INFO]


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split()).lower()
        if "from pg_namespace" in s:
            self._result = [(name,) for name in self._conn.schemas]
        elif "set search_path" in s:
            self._conn.current = sql.rsplit("TO ", 1)[1].strip()
            self._conn.visited.append(self._conn.current)
            self._result = []
        elif "from documents where status in" in s:
            self._result = self._conn.starved.get(self._conn.current, [])
            if self._conn.explode_on and self._conn.current in self._conn.explode_on:
                raise RuntimeError("simulated schema fault")
        else:
            self._result = []

    def fetchall(self):
        return list(self._result)


class FakeConn:
    def __init__(self, schemas, starved=None, explode_on=None):
        self.schemas = schemas
        self.starved = starved or {}
        self.explode_on = explode_on or set()
        self.visited = []
        self.current = None
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def capture(monkeypatch):
    log = CaptureLog()
    monkeypatch.setattr(embedder, "log", log)
    monkeypatch.setenv("DOC_WATCHDOG_HOURS", "24")
    monkeypatch.delenv("DOC_WATCHDOG_INTERVAL_S", raising=False)
    return log


def test_starved_never_claimed_pending_fires_crit(capture):
    conn = FakeConn(["faultline_a"], starved={"faultline_a": [(14, "pending", 360.0)]})
    n = embedder._drain_watchdog_once(conn)
    assert n == 1
    assert any("drain_watchdog.starved" in c and "doc_id=14" in c for c in capture.crits)
    assert any("drain_watchdog.board starved=1" in c for c in capture.crits)


def test_fresh_docs_are_silent(capture):
    conn = FakeConn(["faultline_a"], starved={"faultline_a": []})
    n = embedder._drain_watchdog_once(conn)
    assert n == 0
    assert not [c for c in capture.crits if "starved" in c]


def test_claimed_stalled_processing_fires(capture):
    conn = FakeConn(["faultline_a"], starved={"faultline_a": [(17, "processing", 30.0)]})
    n = embedder._drain_watchdog_once(conn)
    assert n == 1
    assert any("status=processing" in c for c in capture.crits)


def test_multiple_starved_rows_one_board_line(capture):
    conn = FakeConn(["faultline_a"], starved={
        "faultline_a": [(14, "pending", 360.0), (15, "processing", 30.0)]})
    n = embedder._drain_watchdog_once(conn)
    assert n == 2
    assert sum(1 for c in capture.crits if "drain_watchdog.starved " in c) == 2
    assert any("drain_watchdog.board starved=2" in c for c in capture.crits)


def test_disabled_flag_noop(capture, monkeypatch):
    monkeypatch.setenv("DOC_WATCHDOG_HOURS", "0")
    conn = FakeConn(["faultline_a"])
    n = embedder._drain_watchdog_once(conn)
    assert n == 0
    assert conn.visited == []


def test_disabled_inf_noop(capture, monkeypatch):
    # float("inf") parses fine and would silently never fire; it must resolve to
    # DISABLED like any other non-usable threshold, not lurk as a zero-signal sweep.
    monkeypatch.setenv("DOC_WATCHDOG_HOURS", "inf")
    conn = FakeConn(["faultline_a"])
    n = embedder._drain_watchdog_once(conn)
    assert n == 0
    assert conn.visited == []


def test_bad_schema_does_not_silence_others(capture):
    conn = FakeConn(
        ["faultline_broken", "faultline_good"],
        starved={"faultline_good": [(22, "pending", 100.0)]},
        explode_on={"faultline_broken"},
    )
    n = embedder._drain_watchdog_once(conn)
    assert n == 1
    assert any("drain_watchdog.schema_error" in w for w in capture.warnings)
    assert any("doc_id=22" in c for c in capture.crits)
    assert conn.rollbacks >= 1


def test_watchdog_uses_only_real_logger_api():
    """FAKE-DIVERGENCE PIN (pin 7): every log.<method>( call inside the watchdog
    must resolve on a stock logging.Logger. The first cut called log.crit() — real
    in the old test double only — and production degraded every CRITICAL to a
    cycle_error warning. If this goes red, someone invented logger API again."""
    src = inspect.getsource(embedder._drain_watchdog_once) + \
        inspect.getsource(embedder._drain_watchdog_loop) + \
        inspect.getsource(embedder._drain_watchdog_hours)
    called = set(re.findall(r"log\.(\w+)\(", src))
    assert called, "sanity: the watchdog must call the logger at all"
    for meth in sorted(called):
        assert callable(getattr(logging.Logger, meth, None)), (
            f"log.{meth}() is not a real logging.Logger method — the CRITICAL would "
            f"raise AttributeError in production and degrade to cycle_error warnings"
        )


def test_invalid_threshold_falls_back_to_24_and_still_sweeps(capture, monkeypatch):
    monkeypatch.setenv("DOC_WATCHDOG_HOURS", "garbage")
    conn = FakeConn(["faultline_a"], starved={"faultline_a": [(31, "pending", 30.0)]})
    n = embedder._drain_watchdog_once(conn)
    assert n == 1
    assert any("drain_watchdog.bad_threshold" in w for w in capture.warnings)
    assert any("doc_id=31" in c for c in capture.crits)


def test_schema_outside_contract_skipped_loudly_never_interpolated(capture):
    hostile = "faultline_x; drop table x"
    conn = FakeConn(
        ["faultline_good", hostile],
        starved={"faultline_good": [(41, "pending", 50.0)]},
    )
    n = embedder._drain_watchdog_once(conn)
    assert n == 1
    assert any("drain_watchdog.schema_skipped" in w for w in capture.warnings)
    # the hostile name must never reach SET search_path
    assert conn.visited == ["faultline_good"]
