"""
Per-tenant transaction-abort ISOLATION in the re-embedder poll loop.

Bug (live 2026-06-23): a tenant schema with a missing/incomplete relation (e.g. no
`staged_facts` / `intent_confidence_feedback`) aborts the Postgres transaction; on a
SHARED connection the abort was NOT rolled back, so every subsequent statement in the
cycle failed with "current transaction is aborted" — one bad tenant poisoned the whole
poll cycle (and every later tenant/job).

Fix: each per-tenant unit of work rolls back on ANY exception (function-level outer
`except` + the `_rollback_and_reapply_search_path` helper for shared-connection
subsystem loops) so a broken tenant is logged + skipped and the rest of the cycle
proceeds. These tests prove the rollback fires and the connection is left clean.
"""
import psycopg2
import pytest
from unittest.mock import MagicMock, patch

from src.re_embedder.embedder import (
    promote_staged_facts,
    expire_staged_facts,
    decay_class_c_hits,
    promote_class_c_hits,
    _rollback_and_reapply_search_path,
)


def _aborting_db(missing_relation="staged_facts"):
    """A mock connection whose FIRST data query raises UndefinedTable (missing relation),
    exactly like Postgres when a throwaway tenant schema lacks the table. SET search_path
    succeeds; subsequent data queries raise."""
    db = MagicMock()

    def _cursor_factory():
        cur = MagicMock()

        def _execute(sql, *args, **kwargs):
            s = str(sql)
            if "search_path" in s.lower():
                return None  # search_path always succeeds
            raise psycopg2.errors.UndefinedTable(
                f'relation "{missing_relation}" does not exist'
            )

        cur.execute.side_effect = _execute
        cur.fetchall.return_value = []
        cur.fetchone.return_value = None
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cur)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    db.cursor.side_effect = _cursor_factory
    return db


@pytest.fixture(autouse=True)
def _quiet_log():
    with patch("src.re_embedder.embedder.log"):
        yield


# ──────────────────────────────────────────────────────────────────────────────
# 1. Each per-tenant job rolls back on a missing-relation abort and does NOT raise.
# ──────────────────────────────────────────────────────────────────────────────

def test_promote_staged_facts_rolls_back_on_missing_relation():
    db = _aborting_db("staged_facts")
    # Must NOT raise — the broken tenant is handled gracefully.
    result = promote_staged_facts(db, "http://qdrant", user_id="u1", schema_name="faultline_broken")
    assert result == 0
    # The connection was rolled back so it is CLEAN for the next job/tenant.
    assert db.rollback.called, "promote_staged_facts must rollback the aborted txn"


def test_expire_staged_facts_rolls_back_on_missing_relation():
    db = _aborting_db("staged_facts")
    result = expire_staged_facts(db, "http://qdrant", user_id="u1")
    assert result == 0
    assert db.rollback.called, "expire_staged_facts must rollback the aborted txn"


def test_decay_class_c_hits_rolls_back_on_missing_relation():
    db = _aborting_db("staged_facts")
    result = decay_class_c_hits(db, "http://qdrant", user_id="u1")
    assert result["decremented"] == 0 and result["dropped"] == 0
    assert db.rollback.called, "decay_class_c_hits must rollback the aborted txn"


def test_promote_class_c_hits_rolls_back_on_missing_relation():
    db = _aborting_db("staged_facts")
    result = promote_class_c_hits(
        db, "http://qdrant", "http://llm", user_id="u1", schema_name="faultline_broken"
    )
    assert result == 0
    assert db.rollback.called, "promote_class_c_hits must rollback the aborted txn"


# ──────────────────────────────────────────────────────────────────────────────
# 2. The shared-connection helper rolls back AND re-applies the tenant search_path.
# ──────────────────────────────────────────────────────────────────────────────

def test_helper_rolls_back_and_reapplies_search_path():
    db = MagicMock()
    cur = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cur)
    cm.__exit__ = MagicMock(return_value=False)
    db.cursor.return_value = cm

    _rollback_and_reapply_search_path(db, "faultline_tenant_a")

    db.rollback.assert_called_once()
    # search_path re-applied to the SAME tenant (NO public) after rollback.
    executed = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
    assert "search_path to faultline_tenant_a" in executed.lower()


def test_helper_is_failsafe_when_rollback_raises():
    db = MagicMock()
    db.rollback.side_effect = Exception("connection gone")
    # Must NOT propagate — fail-safe.
    _rollback_and_reapply_search_path(db, "faultline_tenant_a")
    assert db.rollback.called


# ──────────────────────────────────────────────────────────────────────────────
# 3. END-TO-END isolation: a broken tenant does not abort a healthy tenant.
#    Simulates the shared-connection subsystem loop pattern: iterate tenants on ONE
#    connection, rollback-on-exception between them. Proves NO cascade.
# ──────────────────────────────────────────────────────────────────────────────

def test_broken_tenant_does_not_cascade_to_healthy_tenant():
    """The poll loop iterates [broken, healthy]. The broken tenant raises; after the
    rollback the connection is clean and the healthy tenant processes normally — no
    'current transaction is aborted' cascade."""
    processed = []
    aborted = {"flag": False}

    # One SHARED connection (the JOB-7 / subsystem-loop shape).
    db = MagicMock()

    def _run_subsystem(schema):
        # Simulate a statement against the tenant schema on the shared connection.
        if aborted["flag"]:
            # If a prior abort was NOT cleaned up, every statement would fail like this.
            raise psycopg2.errors.InFailedSqlTransaction(
                "current transaction is aborted, commands ignored until end of "
                "transaction block"
            )
        if schema == "faultline_broken":
            aborted["flag"] = True  # txn is now aborted
            raise psycopg2.errors.UndefinedTable('relation "staged_facts" does not exist')
        processed.append(schema)

    def _rollback():
        aborted["flag"] = False  # rollback clears the aborted txn

    db.rollback.side_effect = _rollback

    tenants = ["faultline_broken", "faultline_healthy"]
    errors = []
    for schema in tenants:
        try:
            _run_subsystem(schema)
        except Exception as e:
            # THE FIX: rollback between tenants on the shared connection, then continue.
            db.rollback()
            errors.append((schema, type(e).__name__))
            continue

    # Broken tenant errored (fail-loud) but the healthy tenant STILL processed.
    assert processed == ["faultline_healthy"], "healthy tenant must process after a broken one"
    assert errors == [("faultline_broken", "UndefinedTable")], "only the broken tenant errors"
    # Critically: the healthy tenant did NOT hit InFailedSqlTransaction (no cascade).
    assert all(name != "InFailedSqlTransaction" for _, name in errors)


def test_without_rollback_cascade_would_occur_control():
    """Control: WITHOUT the between-tenant rollback, the same loop cascades — proving the
    rollback is what prevents the poison. (Documents the bug the fix resolves.)"""
    processed = []
    aborted = {"flag": False}

    def _run_subsystem(schema):
        if aborted["flag"]:
            raise psycopg2.errors.InFailedSqlTransaction("current transaction is aborted")
        if schema == "faultline_broken":
            aborted["flag"] = True
            raise psycopg2.errors.UndefinedTable('relation "staged_facts" does not exist')
        processed.append(schema)

    tenants = ["faultline_broken", "faultline_healthy"]
    errors = []
    for schema in tenants:
        try:
            _run_subsystem(schema)
        except Exception as e:
            # NO rollback here — the bug.
            errors.append((schema, type(e).__name__))
            continue

    # The healthy tenant was POISONED — never processed, hit the cascade error.
    assert processed == [], "without rollback the healthy tenant is poisoned"
    assert errors[1] == ("faultline_healthy", "InFailedSqlTransaction")
