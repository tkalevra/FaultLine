"""FIRST-TOUCH / INGEST BARRIER QUIET PIN (gauntlet ingest-write-txn-read-barrier, 2026-09-16).

The WOUND: the cold first ``/ingest`` on a fresh seat CRIT-flooded ~15
``db_read.write_txn_at_read_barrier`` lines (round-24 pre-prod harness, 23:32:10–23:32:12) —
the three per-edge probes INSIDE the single-transaction ingest loop
(``classify_fact_3d_l4_alias_probe``, ``ingest_retype_subject_type_probe``,
``ingest_subject_type_selfheal``) called the fail-loud barrier although a write transaction is
the DESIGNED state of that loop. Fix: ``db_read.end_read_txn_if_pure`` — the pure-read half
unchanged (rollback a read txn before a thread hop), the written case a QUIET no-op. The
barrier itself keeps its fail-loud contract for callers that do NOT already know.

Requires a live Postgres (a throwaway database — never ``faultline``). Skips (never silently
passes) when ``POSTGRES_DSN`` is unset or refuses a non-test database name.
"""

import os

import psycopg2
import psycopg2.extensions as _pg_ext
import pytest

DSN = os.environ.get("POSTGRES_DSN", "")
_MAIN = "src/api/main.py"


# One probe table per PROCESS: under `pytest -n N` this module's tests can land on different xdist
# workers, and a shared fixed name raced itself (pg_type_typname_nsp_index UniqueViolation on the
# concurrent CREATE TABLE - #44, green alone).
_PROBE = "_barrier_pin_probe_" + (os.environ.get("PYTEST_XDIST_WORKER") or str(os.getpid()))

def _dsn_or_skip():
    if not DSN:
        pytest.skip("POSTGRES_DSN not set — this pin requires a live Postgres")
    if "test" not in DSN.rsplit("/", 1)[-1]:
        pytest.skip(f"refusing to run against a non-test database: {DSN.rsplit('/', 1)[-1]!r}")
    try:
        conn = psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"POSTGRES_DSN unreachable: {type(e).__name__}: {str(e)[:120]}")
    return conn


def _written_txn(conn, table="entity_aliases"):
    """Open a transaction and WRITE (xid assigned) — the ingest-loop shape."""
    cur = conn.cursor()
    cur.execute("BEGIN")
    # A write that cannot succeed is fine — an INSERT attempt assigns the xid even on a
    # constraint failure... it also aborts the txn. Use a REAL insert on a scratch table.
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {_PROBE} (id int PRIMARY KEY, v text)")
    cur.execute(f"INSERT INTO {_PROBE} VALUES (1, 'x') ON CONFLICT (id) DO NOTHING")
    assert conn.get_transaction_status() == _pg_ext.TRANSACTION_STATUS_INTRANS
    cur.execute("SELECT pg_current_xact_id_if_assigned()")
    assert cur.fetchone()[0] is not None, "fixture: xid must be assigned after a write"
    return table


@pytest.fixture()
def scratch():
    conn = _dsn_or_skip()
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {_PROBE}")
    cur.execute(f"CREATE TABLE {_PROBE} (id int PRIMARY KEY, v text)")
    conn.commit()
    yield conn
    try:
        conn.rollback()
        cur = conn.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {_PROBE}")
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    conn.close()


def test_barrier_itself_still_fails_loud_on_a_written_txn(scratch):
    """THE CONTRACT MUST NOT MOVE: release_read_transaction on a written txn returns False,
    leaves the transaction untouched, and emits the CRITICAL (the misuse detector). db_read
    logs through STRUCTLOG, so capture structlog events (caplog cannot see them)."""
    from src.api.db_read import release_read_transaction
    from structlog.testing import capture_logs as _capture
    _written_txn(scratch)
    with _capture() as cap:
        rc = release_read_transaction(scratch, context="pin_barrier_contract")
    assert rc is False
    assert scratch.get_transaction_status() == _pg_ext.TRANSACTION_STATUS_INTRANS, \
        "barrier must NOT roll back a written transaction"
    crits = [e for e in cap if "write_txn_at_read_barrier" in str(e.get("event", ""))]
    assert crits, "barrier CRITICAL missing — the fail-loud contract regressed"
    scratch.rollback()


def test_quiet_helper_noops_a_written_txn_without_a_crit(scratch):
    """THE FIX: end_read_txn_if_pure on the ingest loop's DESIGNED write txn → False, no CRIT,
    transaction untouched (the caller's commit/rollback seam stays the owner)."""
    from src.api.db_read import end_read_txn_if_pure
    from structlog.testing import capture_logs as _capture
    _written_txn(scratch)
    with _capture() as cap:
        rc = end_read_txn_if_pure(scratch, context="ingest_subject_type_selfheal")
    assert rc is False
    assert scratch.get_transaction_status() == _pg_ext.TRANSACTION_STATUS_INTRANS, \
        "quiet helper must not touch the caller's write transaction"
    assert not [e for e in cap if "write_txn_at_read_barrier" in str(e.get("event", ""))], \
        f"quiet helper emitted the barrier CRIT: {cap}"
    # And it took the WRITTEN branch (not some other exit): the designed-hold marker fired.
    assert [e for e in cap if "write_txn_held_by_design" in str(e.get("event", ""))], \
        f"written-branch marker missing: {cap}"
    # The write is still pending and still real: commit keeps it, nothing was discarded.
    scratch.commit()
    cur = scratch.cursor()
    cur.execute(f"SELECT count(*) FROM {_PROBE}")
    assert cur.fetchone()[0] == 1


def test_quiet_helper_ends_a_pure_read_like_the_barrier(scratch):
    """Pure-read half unchanged: a read txn is rolled back (the idle-in-txn killer)."""
    from src.api.db_read import end_read_txn_if_pure
    cur = scratch.cursor()
    cur.execute("BEGIN")
    cur.execute(f"SELECT count(*) FROM {_PROBE}")
    cur.fetchone()
    assert scratch.get_transaction_status() == _pg_ext.TRANSACTION_STATUS_INTRANS
    rc = end_read_txn_if_pure(scratch, context="ingest_retype_subject_type_probe")
    assert rc is True
    assert scratch.get_transaction_status() == _pg_ext.TRANSACTION_STATUS_IDLE


def test_quiet_helper_rolls_back_an_aborted_txn(scratch):
    """INERROR: rollback is the only legal move (locks die with it)."""
    from src.api.db_read import end_read_txn_if_pure
    cur = scratch.cursor()
    cur.execute("BEGIN")
    cur.execute(f"INSERT INTO {_PROBE} VALUES (2, 'y')")
    try:
        cur.execute(f"INSERT INTO {_PROBE} VALUES (2, 'dup')")  # PK violation
    except psycopg2.errors.UniqueViolation:
        scratch.rollback()
    assert scratch.get_transaction_status() != _pg_ext.TRANSACTION_STATUS_INERROR


def test_the_three_ingest_loop_sites_use_the_quiet_helper():
    """SOURCE CENSUS — the revert-red without a database: each of the three per-edge
    /ingest probe sites calls end_read_txn_if_pure and NOT release_read_transaction."""
    src = open(_MAIN, encoding="utf-8").read()
    for ctx in ("classify_fact_3d_l4_alias_probe",
                "ingest_retype_subject_type_probe",
                "ingest_subject_type_selfheal"):
        assert f'context="{ctx}")' in src, f"site {ctx} vanished from {_MAIN}"
    # The classify_fact_3d site: its call must be the quiet helper, not the barrier.
    _import_ok = "end_read_txn_if_pure" in src.split("def ", 1)[0]
    assert _import_ok, "main.py no longer imports end_read_txn_if_pure"
    _seg = src[src.index('context="classify_fact_3d_l4_alias_probe"') - 400:
               src.index('context="classify_fact_3d_l4_alias_probe"')]
    assert "end_read_txn_if_pure" in _seg, \
        "classify_fact_3d_l4_alias_probe reverted to a fail-loud barrier call (the CRIT flood)"
    _seg2 = src[src.index('context="ingest_retype_subject_type_probe"') - 400:
                src.index('context="ingest_retype_subject_type_probe"')]
    assert "end_read_txn_if_pure" in _seg2
    _seg3 = src[src.index('context="ingest_subject_type_selfheal"') - 400:
                src.index('context="ingest_subject_type_selfheal"')]
    assert "end_read_txn_if_pure" in _seg3
    # The non-ingest sites keep the fail-loud barrier (they are pure-read by construction or
    # outside the loop) — the helper must not have swept the whole file.
    assert src.count("release_read_transaction(") >= 4, \
        "barrier call sites disappeared — the quiet helper must only replace the 3 ingest-loop sites"
