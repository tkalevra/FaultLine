"""Migration RUNNER contract tests — idempotency, resumability, failure isolation, checksum
guard, dry-run. Maps to DEV/DESIGN-per-account-grain-and-api-subject.md §5.1.

Proves the runner's contract WITHOUT a live database via a lightweight in-memory fake that
models exactly the two things the runner touches: (1) the ``public.schema_migration_status``
ledger, and (2) whether a given migration's DDL succeeds or raises per schema. The fake honors
transaction commit/rollback so the "atomic apply + ledger" and "record failed in its own txn"
semantics are actually exercised, not mocked away.

Run: python3 -m pytest tests/test_saas_migration_runner.py -q   (tests/ is gitignored → git add -f)
"""

import re

import pytest

from saas import migration_runner as mr
from saas.migration_runner import (
    ChecksumMismatchError,
    Migration,
    SchemaNameError,
    apply_migration,
    apply_all_pending,
    load_migrations,
    status,
)


# ─────────────────────────────────────────────────────────────────────────────────────
# In-memory fake DB — models the ledger + per-(schema,migration) DDL success/failure.
# ─────────────────────────────────────────────────────────────────────────────────────
class FakeDB:
    """Shared backing store across all connections a run opens (like one real Postgres).

    ledger: {(schema, migration_id): {status, checksum, error, applied_at}}
    fail_ddl: a callable (schema, migration_id) -> bool; True means the DDL raises.
    ddl_calls: list of (schema, migration_id) whose DDL actually executed (committed OR rolled back).
    ddl_committed: list of (schema, migration_id) whose DDL COMMITTED (proves no-op on re-run).
    """

    def __init__(self, fail_ddl=None):
        self.ledger: dict = {}
        self.fail_ddl = fail_ddl or (lambda s, m: False)
        self.ddl_calls: list = []
        self.ddl_committed: list = []

    def connection(self):
        return _FakeConnCtx(self)


class _FakeConnCtx:
    def __init__(self, dbase):
        self._db = dbase

    def __enter__(self):
        return _FakeConn(self._db)

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, dbase):
        self._db = dbase
        self.autocommit = True
        # pending mutations staged in the current txn (applied on commit, dropped on rollback)
        self._staged_ledger: dict = {}
        self._staged_ddl: list = []
        self._pending_search_path = None

    def cursor(self):
        return _FakeCursorCtx(self)

    def commit(self):
        for k, v in self._staged_ledger.items():
            self._db.ledger[k] = v
        for entry in self._staged_ddl:
            self._db.ddl_committed.append(entry)
        self._staged_ledger = {}
        self._staged_ddl = []

    def rollback(self):
        self._staged_ledger = {}
        self._staged_ddl = []


class _FakeCursorCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return _FakeCursor(self._conn)

    def __exit__(self, *a):
        return False


# Recognizers for the small SQL surface the runner issues.
_RE_CREATE_LEDGER = re.compile(r"CREATE TABLE IF NOT EXISTS public\.schema_migration_status", re.I)
_RE_SET_PATH = re.compile(r"SET LOCAL search_path TO (\S+)", re.I)
_RE_SELECT_LEDGER = re.compile(r"SELECT status, checksum FROM public\.schema_migration_status", re.I)
_RE_SELECT_STATUS = re.compile(r"SELECT schema_name, migration_id, status", re.I)
_RE_INSERT_APPLIED = re.compile(r"INSERT INTO public\.schema_migration_status.*'applied'", re.I | re.S)
_RE_INSERT_FAILED = re.compile(r"INSERT INTO public\.schema_migration_status.*'failed'", re.I | re.S)


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._db = conn._db
        self._result = []

    def execute(self, sql, params=None):
        self._result = []

        if _RE_CREATE_LEDGER.search(sql):
            return  # ledger table always "exists" in the fake

        mpath = _RE_SET_PATH.search(sql)
        if mpath:
            self._conn._pending_search_path = mpath.group(1)
            return

        if _RE_SELECT_LEDGER.search(sql):
            schema, mid = params
            row = self._conn._staged_ledger.get((schema, mid)) or self._db.ledger.get((schema, mid))
            self._result = [(row["status"], row["checksum"])] if row else []
            return

        if _RE_SELECT_STATUS.search(sql):
            # status() view read
            items = sorted(self._db.ledger.items())
            if params:  # filtered to one schema
                (schema,) = params
                items = [(k, v) for k, v in items if k[0] == schema]
            self._result = [
                (k[0], k[1], v["status"], v["checksum"], None, v.get("applied_at"), v.get("error"))
                for k, v in items
            ]
            return

        if _RE_INSERT_APPLIED.search(sql):
            schema, mid, checksum = params
            self._conn._staged_ledger[(schema, mid)] = {
                "status": "applied",
                "checksum": checksum,
                "error": None,
                "applied_at": "now",
            }
            return

        if _RE_INSERT_FAILED.search(sql):
            schema, mid, checksum, err = params
            self._conn._staged_ledger[(schema, mid)] = {
                "status": "failed",
                "checksum": checksum,
                "error": err,
                "applied_at": None,
            }
            return

        # Anything else is a migration DDL body. Record + maybe raise.
        schema = self._conn._pending_search_path
        # recover migration_id from the staged 'applied' insert that follows? No — the DDL runs
        # BEFORE the insert. We only know the schema here; the caller stages the id via the insert.
        # Track by schema+sql so fail_ddl can key on the sql body if needed.
        self._db.ddl_calls.append((schema, sql))
        self._conn._staged_ddl.append((schema, sql))
        if self._db.fail_ddl(schema, sql):
            raise RuntimeError(f"simulated DDL failure for schema={schema}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


# ─────────────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────────────
DDL = "CREATE TABLE IF NOT EXISTS demo (id int);"
MIG_ID = "158_demo"


def _applied(db, schema, mid=MIG_ID):
    return db.ledger.get((schema, mid), {}).get("status")


# ─────────────────────────────────────────────────────────────────────────────────────
# 1. IDEMPOTENCY — apply twice → second run is a no-op (ledger already applied, DDL not re-run)
# ─────────────────────────────────────────────────────────────────────────────────────
def test_idempotent_second_run_is_noop():
    db = FakeDB()
    schemas = ["faultline_acct_aaa", "faultline_acct_bbb"]

    s1 = apply_migration(MIG_ID, DDL, schemas, conn_factory=db.connection)
    assert s1.applied == 2 and s1.skipped == 0 and s1.ok
    assert all(_applied(db, s) == "applied" for s in schemas)
    committed_after_first = list(db.ddl_committed)
    assert len(committed_after_first) == 2  # DDL committed once per schema

    s2 = apply_migration(MIG_ID, DDL, schemas, conn_factory=db.connection)
    assert s2.applied == 0 and s2.skipped == 2 and s2.ok
    # No NEW committed DDL on the second run — proven no-op.
    assert db.ddl_committed == committed_after_first


# ─────────────────────────────────────────────────────────────────────────────────────
# 2. RESUMABILITY — mid-fanout failure on schema K → K 'failed', others 'applied'; re-run retries only K
# ─────────────────────────────────────────────────────────────────────────────────────
def test_resumable_retries_only_failed_schema():
    schemas = ["faultline_acct_a", "faultline_acct_k", "faultline_acct_c"]
    fail_first = {"on": True}

    def fail_ddl(schema, sql):
        return schema == "faultline_acct_k" and fail_first["on"]

    db = FakeDB(fail_ddl=fail_ddl)

    s1 = apply_migration(MIG_ID, DDL, schemas, conn_factory=db.connection)
    assert s1.applied == 2 and s1.failed == 1 and not s1.ok
    assert _applied(db, "faultline_acct_a") == "applied"
    assert _applied(db, "faultline_acct_c") == "applied"
    assert _applied(db, "faultline_acct_k") == "failed"
    committed_after_first = list(db.ddl_committed)

    # Heal the fault; re-run. Only K should be retried (a/c are skipped no-ops).
    fail_first["on"] = False
    s2 = apply_migration(MIG_ID, DDL, schemas, conn_factory=db.connection)
    assert s2.applied == 1 and s2.skipped == 2 and s2.ok
    assert _applied(db, "faultline_acct_k") == "applied"
    # Exactly ONE new committed DDL (K); a and c were skipped.
    assert len(db.ddl_committed) == len(committed_after_first) + 1


# ─────────────────────────────────────────────────────────────────────────────────────
# 3. FAILURE ISOLATION — one schema raises → recorded 'failed', the rest still applied, summary returned
# ─────────────────────────────────────────────────────────────────────────────────────
def test_failure_isolation_one_bad_schema_does_not_abort_fanout():
    schemas = [f"faultline_acct_{c}" for c in "abcde"]

    def fail_ddl(schema, sql):
        return schema == "faultline_acct_c"

    db = FakeDB(fail_ddl=fail_ddl)
    s = apply_migration(MIG_ID, DDL, schemas, conn_factory=db.connection)

    # Not an aborted run — a full summary with 4 applied + 1 failed.
    assert s.applied == 4
    assert s.failed == 1
    assert not s.ok
    assert _applied(db, "faultline_acct_c") == "failed"
    for good in ["faultline_acct_a", "faultline_acct_b", "faultline_acct_d", "faultline_acct_e"]:
        assert _applied(db, good) == "applied"


# ─────────────────────────────────────────────────────────────────────────────────────
# 4. CHECKSUM GUARD — different sql under an already-applied migration_id is detected (not silent)
# ─────────────────────────────────────────────────────────────────────────────────────
def test_checksum_mismatch_detected_not_silent():
    db = FakeDB()
    schema = ["faultline_acct_x"]

    s1 = apply_migration(MIG_ID, DDL, schema, conn_factory=db.connection)
    assert s1.applied == 1

    # Same migration_id, DIFFERENT body → guard trips (recorded, not silently re-applied).
    s2 = apply_migration(MIG_ID, "CREATE TABLE demo2 (x int);", schema, conn_factory=db.connection)
    assert s2.failed == 1 and not s2.ok
    assert s2.results[0].action == "checksum_mismatch"
    # The original applied row is untouched (still the ORIGINAL checksum).
    assert db.ledger[(schema[0], MIG_ID)]["checksum"] == Migration.create(MIG_ID, DDL).checksum


def test_checksum_mismatch_raises_at_apply_one_level():
    """The low-level guard raises loudly; the fanout catches it into the summary (above)."""
    db = FakeDB()
    with db.connection() as conn:
        mr._ensure_ledger(conn)
        mr._apply_one(conn, "faultline_acct_x", Migration.create(MIG_ID, DDL))
        with pytest.raises(ChecksumMismatchError):
            mr._apply_one(conn, "faultline_acct_x", Migration.create(MIG_ID, "DIFFERENT;"))


# ─────────────────────────────────────────────────────────────────────────────────────
# 5. DRY-RUN — touches nothing (no DDL committed, no ledger row written)
# ─────────────────────────────────────────────────────────────────────────────────────
def test_dry_run_touches_nothing():
    db = FakeDB()
    schemas = ["faultline_acct_a", "faultline_acct_b"]

    s = apply_migration(MIG_ID, DDL, schemas, dry_run=True, conn_factory=db.connection)
    assert s.would_apply == 2
    assert s.applied == 0
    # Nothing written.
    assert db.ledger == {}
    assert db.ddl_committed == []


def test_dry_run_reports_would_skip_for_already_applied():
    db = FakeDB()
    schema = ["faultline_acct_a"]
    apply_migration(MIG_ID, DDL, schema, conn_factory=db.connection)

    s = apply_migration(MIG_ID, DDL, schema, dry_run=True, conn_factory=db.connection)
    assert s.would_apply == 0
    assert s.results[0].action == "would_skip"


def test_dry_run_flags_checksum_mismatch():
    db = FakeDB()
    schema = ["faultline_acct_a"]
    apply_migration(MIG_ID, DDL, schema, conn_factory=db.connection)

    s = apply_migration(MIG_ID, "DIFFERENT;", schema, dry_run=True, conn_factory=db.connection)
    assert s.results[0].action == "checksum_mismatch"
    # Still no writes.
    assert db.ledger[(schema[0], MIG_ID)]["checksum"] == Migration.create(MIG_ID, DDL).checksum


# ─────────────────────────────────────────────────────────────────────────────────────
# 6. SAFETY — unsafe schema names throw before any DB touch
# ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["public; DROP TABLE x", "Faultline", "acct-1", "1abc", "", "a b"])
def test_unsafe_schema_name_rejected(bad):
    db = FakeDB()
    with pytest.raises(SchemaNameError):
        apply_migration(MIG_ID, DDL, [bad], conn_factory=db.connection)
    # Fail-loud BEFORE touching the DB.
    assert db.ddl_calls == []


# ─────────────────────────────────────────────────────────────────────────────────────
# 7. status() view
# ─────────────────────────────────────────────────────────────────────────────────────
def test_status_view_reflects_ledger():
    db = FakeDB()
    apply_migration(MIG_ID, DDL, ["faultline_acct_a", "faultline_acct_b"], conn_factory=db.connection)
    rows = status(conn_factory=db.connection)
    assert {r["schema_name"] for r in rows} == {"faultline_acct_a", "faultline_acct_b"}
    assert all(r["status"] == "applied" for r in rows)

    filtered = status("faultline_acct_a", conn_factory=db.connection)
    assert len(filtered) == 1 and filtered[0]["schema_name"] == "faultline_acct_a"


# ─────────────────────────────────────────────────────────────────────────────────────
# 8. apply_all_pending — discovers real numbered migrations + applies in order
# ─────────────────────────────────────────────────────────────────────────────────────
def test_apply_all_pending_discovers_and_applies(tmp_path):
    (tmp_path / "010_first.sql").write_text("CREATE TABLE a (i int);")
    (tmp_path / "002_early.sql").write_text("CREATE TABLE b (i int);")
    (tmp_path / "notamigration.txt").write_text("ignore me")
    (tmp_path / "003_migrate.py").write_text("# python migration, ignored")

    db = FakeDB()
    s = apply_all_pending(
        ["faultline_acct_a"], migrations_dir=str(tmp_path), conn_factory=db.connection
    )
    assert s.applied == 2  # only the two .sql files, .txt and .py ignored
    assert _applied(db, "faultline_acct_a", "002_early") == "applied"
    assert _applied(db, "faultline_acct_a", "010_first") == "applied"

    # Re-run is fully idempotent.
    s2 = apply_all_pending(
        ["faultline_acct_a"], migrations_dir=str(tmp_path), conn_factory=db.connection
    )
    assert s2.applied == 0 and s2.skipped == 2


def test_load_migrations_sorted_by_numeric_prefix(tmp_path):
    (tmp_path / "100_c.sql").write_text("--c")
    (tmp_path / "9_a.sql").write_text("--a")
    (tmp_path / "20_b.sql").write_text("--b")
    migs = load_migrations(str(tmp_path))
    assert [m.migration_id for m in migs] == ["9_a", "20_b", "100_c"]


def test_real_migrations_dir_loads_and_checksums():
    """The real migrations/ dir loads without error and every file gets a checksum (well-formed)."""
    migs = load_migrations()
    assert len(migs) > 100  # the repo has 150+ numbered migrations
    assert all(m.checksum and len(m.checksum) == 64 for m in migs)  # sha256 hex
    ids = [m.migration_id for m in migs]
    assert "158_saas_schema_migration_status" in ids
