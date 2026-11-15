"""
Pin: DB Decimals must not meet float math in the retraction-outcome lane.

evaluate_retraction_outcomes() (src/re_embedder/embedder.py) reads per-pattern
aggregates from retraction_outcomes. Its success_rate column expression is

    AVG(CASE WHEN was_correct=true THEN 1.0 ELSE 0.0 END)

The CASE literals are NUMERIC, so the AVG is numeric, and psycopg2 returns
decimal.Decimal for numeric. Downstream the lane is float math (1.0 - rate,
rate * 100, :.2f formats). Before the boundary coercion, 1.0 - Decimal raised

    TypeError: unsupported operand type(s) for -: 'float' and 'decimal.Decimal'

logged as retraction_outcome_processing_failed — measured on production
2026-08-20 with pattern 'forget that they have a gender named F', recurring
every cycle because the per-pattern handler died BEFORE the signal row was
inserted/updated, so the outcome never reached a terminal state.

These tests feed the function Decimal aggregates EXACTLY as psycopg2 delivers
them (fake responses are keyed on the SQL text — see the Step-1/Step-2a
queries — never one constant tuple for every question) and pin:
  - the outcome row reaches its terminal state (INSERT/UPDATE on
    retraction_signals actually executes; stats reflect it),
  - the values written across the boundary are plain float/int (ONE coercion
    direction: to float — a Decimal surviving into the write params means the
    boundary drifted back),
  - no retraction_outcome_processing_failed error line is emitted.
"""
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.re_embedder.embedder import evaluate_retraction_outcomes


# The exact production pattern that crashed (faultline-api logs, 2026-08-20).
PROD_PATTERN = "forget that they have a gender named F"


class _LaneCursor:
    """Fake psycopg2 cursor whose fetch results are KEYED ON THE SQL TEXT.

    A stub returning one fixed tuple for every fetchone()/fetchall() will
    silently mis-answer any NEW query a later commit adds against the same
    connection (burned before: a schema-capability probe answered through a
    stale fake and flipped a test's fixture). This cursor asserts which query
    it is answering instead of guessing.
    """

    def __init__(self, conn):
        self._conn = conn
        self.executed = []  # (sql, params) in order

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        sql = self.executed[-1][0] if self.executed else ""
        assert "FROM retraction_outcomes" in sql, (
            f"fetchall() answers the Step-1 aggregate over retraction_outcomes; "
            f"got: {sql[:100]!r}"
        )
        return self._conn.outcome_rows

    def fetchone(self):
        sql = self.executed[-1][0] if self.executed else ""
        assert "FROM retraction_signals" in sql, (
            f"fetchone() answers the Step-2a existence probe over "
            f"retraction_signals; got: {sql[:100]!r}"
        )
        return self._conn.existing_signal

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _LaneConn:
    """Fake db_conn: one fresh cursor per context manager, shared row fixtures."""

    def __init__(self, outcome_rows, existing_signal=None):
        self.outcome_rows = outcome_rows
        self.existing_signal = existing_signal
        self.cursors = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        c = _LaneCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _prod_shaped_row(success_rate, retraction_method="pattern"):
    """One Step-1 aggregate row, shaped exactly as the SQL returns it.

    success_rate: AVG(numeric) -> Decimal as psycopg2 delivers it.
    correct_count: CAST(... AS FLOAT) -> float. freq/total_count: bigint -> int.
    """
    return (
        PROD_PATTERN,
        3,                                   # freq  (bigint -> int)
        1.0,                                 # correct_count (CAST AS FLOAT -> float)
        3,                                   # total_count (bigint -> int)
        0.87,                                # avg_confidence (AVG over FLOAT col -> float)
        success_rate,                        # AVG(numeric CASE) -> Decimal
        retraction_method,
        datetime(2026, 8, 20, tzinfo=timezone.utc),  # last_seen
    )


def _statements(db, keyword):
    return [(sql, params) for c in db.cursors for (sql, params) in c.executed
            if keyword in sql]


def test_decimal_success_rate_reaches_terminal_state_new_pattern():
    """The prod crash shape: Decimal aggregate, pattern not yet learned.

    Pre-fix this raised TypeError inside the per-pattern handler, was logged
    as retraction_outcome_processing_failed, and the signal row was NEVER
    written — the outcome row could not reach a terminal state, so the crash
    recurred every cycle. Now: discovered==1, the INSERT into retraction_signals
    executes, and the numeric params crossing the write boundary are plain
    int/float.
    """
    # 1 of 3 outcomes correct — the repeating-decimal case (0.333...), exactly
    # what numeric AVG returns for real GROUP BY data.
    db = _LaneConn([_prod_shaped_row(Decimal("0.3333333333333333"))])

    with patch("src.re_embedder.embedder.log") as mlog:
        stats = evaluate_retraction_outcomes(db, frequency_threshold=3)

    assert stats["discovered"] == 1, f"outcome did not reach terminal state: {stats}"
    assert stats["errors"] == 0

    inserts = _statements(db, "INSERT INTO retraction_signals")
    assert inserts, "retraction_signals INSERT never executed (pattern not learned)"
    params = inserts[0][1]
    # Params: (signal, signal_category, priority, false_positive_rate, notes)
    assert params[0] == PROD_PATTERN.lower()
    assert type(params[2]) is int and params[2] == 33      # int(0.3333... * 100)
    # ONE coercion direction, to float: a Decimal here means the boundary
    # coercion was removed or flipped back to Decimal math.
    assert type(params[3]) is float, (
        f"false_positive_rate must cross the boundary as float, got "
        f"{type(params[3]).__name__}: {params[3]!r}"
    )
    assert abs(params[3] - (1.0 - 0.3333333333333333)) < 1e-9

    # The intent-classification twin table is grown in the same transaction.
    assert _statements(db, "INSERT INTO negation_patterns"), (
        "negation_patterns INSERT never executed"
    )
    assert db.commits >= 1

    failed = [c for c in mlog.error.call_args_list
              if "retraction_outcome_processing_failed" in str(c)]
    assert not failed, f"the pinned crash recurred: {failed}"


def test_decimal_success_rate_updates_existing_signal_mixed_types():
    """Sibling site in the same lane: Step 2c delta math.

    existing row comes from retraction_signals (priority INT -> int,
    false_positive_rate FLOAT -> float); the freshly computed side derives
    from a Decimal aggregate. Pre-fix the mixed arithmetic died even earlier
    (at 1.0 - Decimal); the pin here is that BOTH float-vs-float sites
    (priority_delta, fpr_delta) run clean when one side began life as Decimal.
    """
    # 2.5/3 correct -> 0.8333... -> priority 83 vs existing 50: delta 33 > 5
    # forces the UPDATE branch.
    db = _LaneConn(
        [_prod_shaped_row(Decimal("0.8333333333333333"))],
        existing_signal=(7, "implicit_negation", 50, 0.9),  # int, float — as psycopg2 returns INT/FLOAT columns
    )

    with patch("src.re_embedder.embedder.log") as mlog:
        stats = evaluate_retraction_outcomes(db, frequency_threshold=3)

    assert stats["updated"] == 1, f"existing-signal update did not land: {stats}"
    assert stats["errors"] == 0

    updates = _statements(db, "UPDATE retraction_signals")
    assert updates, "retraction_signals UPDATE never executed"
    params = updates[0][1]
    assert type(params[0]) is int and params[0] == 83
    assert type(params[1]) is float, (
        f"false_positive_rate must be float at the write boundary, got "
        f"{type(params[1]).__name__}"
    )
    assert _statements(db, "UPDATE negation_patterns")

    failed = [c for c in mlog.error.call_args_list
              if "retraction_outcome_processing_failed" in str(c)]
    assert not failed, f"the pinned crash recurred: {failed}"


def test_none_success_rate_guard_survives_the_coercion():
    """The None-guard must stay intact: float(None) would raise, so the
    coercion is conditional and the legacy 0.5/50 defaults still apply."""
    db = _LaneConn([_prod_shaped_row(None)])

    stats = evaluate_retraction_outcomes(db, frequency_threshold=3)

    assert stats["discovered"] == 1 and stats["errors"] == 0
    params = _statements(db, "INSERT INTO retraction_signals")[0][1]
    assert params[2] == 50        # priority default when success_rate falsy
    assert params[3] == 0.5       # fpr default when success_rate is None


def test_each_decimal_scale_processes_cleanly():
    """Every aggregate the lane can really see: 0, 1/3, 1/2, 2/3, 1.
    Guards against a coercion that only survives some scales (e.g. an
    accidental str()/repr() round-trip)."""
    for rate in (Decimal("0.0000000000000000"),
                 Decimal("0.3333333333333333"),
                 Decimal("0.5000000000000000"),
                 Decimal("0.6666666666666667"),
                 Decimal("1.0000000000000000")):
        db = _LaneConn([_prod_shaped_row(rate)])
        stats = evaluate_retraction_outcomes(db, frequency_threshold=3)
        assert stats["discovered"] == 1, f"rate={rate} did not process: {stats}"
        assert stats["errors"] == 0, f"rate={rate} errored: {stats}"
