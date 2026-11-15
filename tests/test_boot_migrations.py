"""Tests for the boot migration gate (src/provisioning/boot_migrations.py).

WHAT IS BEING PINNED, AND WHY EACH TEST EXISTS
----------------------------------------------
The gate stops the boot from re-applying all 275 migrations into every tenant schema on every
container start (measured: 3,978 row-writes into 9 existing tenants per boot, including 134
DELETEs against tenant metadata). The risk of a gate is the opposite failure — silently SKIPPING
a migration that was genuinely needed — so the tests below pin the skip conditions tightly and
pin the DEFAULT separately from the BEHAVIOUR, so a flag flip cannot be mistaken for a fix.
"""

import hashlib
import os

import pytest

# Applies a FAN-OUT migration (ALTER TABLE on every faultline_% schema) to the shared suite
# database: run with the other migration-appliers on one xdist worker (#44 suite hang).
pytestmark = pytest.mark.xdist_group("shared_migrations")

from src.provisioning import boot_migrations as bm


# ── checksum / change detection ───────────────────────────────────────────────────────
def test_checksum_is_content_addressed():
    assert bm._checksum(b"SELECT 1;") == hashlib.sha256(b"SELECT 1;").hexdigest()


def test_checksum_changes_when_file_changes():
    """A MODIFIED migration must be detected, not silently skipped under its old identity.

    This is the whole reason the ledger stores a checksum rather than just a name.
    """
    assert bm._checksum(b"SELECT 1;") != bm._checksum(b"SELECT 1; -- edited")


def test_discover_migrations_is_ordered_and_hashed(tmp_path):
    """Order MUST match the shell glob — several migrations depend on their predecessors."""
    (tmp_path / "002_b.sql").write_text("SELECT 2;")
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "010_c.sql").write_text("SELECT 3;")
    (tmp_path / "notes.txt").write_text("ignored")

    found = bm.discover_migrations(str(tmp_path))
    assert [m[0] for m in found] == ["001_a", "002_b", "010_c"]
    assert found[0][2] == bm._checksum(b"SELECT 1;")


# ── error attribution: the rule that decides what may be stamped ──────────────────────
_SCHEMAS = ["public", "faultline_alpha", "faultline_rectest"]


def test_clean_run_blames_nobody():
    lines, blamed, unattributed = bm.attribute_errors("CREATE TABLE\nINSERT 0 3\n", _SCHEMAS)
    assert lines == [] and blamed == set() and unattributed is False


def test_error_naming_a_schema_blames_only_that_schema():
    """~20 migrations error every boot against one partial schema. The healthy schemas must
    still be stampable, or those files never converge and the ledger delivers nothing."""
    out = 'psql:m.sql:60: ERROR:  relation "faultline_rectest.staged_facts" does not exist'
    lines, blamed, unattributed = bm.attribute_errors(out, _SCHEMAS)
    assert len(lines) == 1
    assert blamed == {"faultline_rectest"}
    assert unattributed is False


def test_unattributed_error_blocks_all_stamping():
    """An error naming no schema (e.g. `column "fact_provenance" does not exist`) is
    UNATTRIBUTABLE. We must refuse to stamp anything for that file, so it re-runs next boot
    exactly as it does today — never guess that an unexplained failure was harmless."""
    out = 'psql:m.sql:45: ERROR:  column "fact_provenance" does not exist'
    lines, blamed, unattributed = bm.attribute_errors(out, _SCHEMAS)
    assert unattributed is True
    assert blamed == set()


def test_longer_schema_name_is_not_shadowed_by_a_prefix():
    """`faultline_rectest` must not absorb the blame belonging to `faultline_rectest_two`."""
    schemas = ["public", "faultline_rectest", "faultline_rectest_two"]
    out = 'ERROR:  relation "faultline_rectest_two.facts" does not exist'
    _lines, blamed, unattributed = bm.attribute_errors(out, schemas)
    assert blamed == {"faultline_rectest_two"}
    assert unattributed is False


# ── flag: DEFAULT pinned separately from BEHAVIOUR ────────────────────────────────────
def test_ledger_default_is_on(monkeypatch):
    monkeypatch.delenv("FAULTLINE_MIGRATION_LEDGER", raising=False)
    assert bm._ledger_enabled() is True


@pytest.mark.parametrize("raw", ["false", "FALSE", " False "])
def test_ledger_flag_off_is_the_rollback_lever(monkeypatch, raw):
    """FAULTLINE_MIGRATION_LEDGER=false restores the legacy every-file sweep."""
    monkeypatch.setenv("FAULTLINE_MIGRATION_LEDGER", raw)
    assert bm._ledger_enabled() is False


@pytest.mark.parametrize("raw", ["true", "1", "yes", "anything-else"])
def test_only_the_literal_false_disables_the_ledger(monkeypatch, raw):
    """A typo'd value must NOT silently disable the gate."""
    monkeypatch.setenv("FAULTLINE_MIGRATION_LEDGER", raw)
    assert bm._ledger_enabled() is True


# ── fail-safe direction ───────────────────────────────────────────────────────────────
def test_unreachable_ledger_runs_every_migration_rather_than_skipping(tmp_path, monkeypatch):
    """THE FAIL-SAFE. An unreadable ledger must fall back to RUNNING EVERYTHING.

    Over-application is bounded and is what every boot has done for 275 migrations. A silently
    skipped migration is unbounded: a missing column raises UndefinedColumn, aborts the caller's
    transaction, and surfaces as a wrong answer somewhere else entirely. A skip is only safe when
    the ledger is trusted; an unreachable ledger is not trusted, so it buys no skips.
    """
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "002_b.sql").write_text("SELECT 2;")

    ran = []
    monkeypatch.setattr(bm, "_run_psql", lambda dsn, path: ran.append(path) or "")
    monkeypatch.setattr(
        bm, "_connect",
        lambda dsn: (_ for _ in ()).throw(bm.LedgerUnavailable("simulated: ledger unreachable")),
    )

    summary = bm.run_boot_migrations(dsn="postgresql://unused", migrations_dir=str(tmp_path),
                                     echo=False)

    assert summary.ledger_active is False
    assert summary.ledger_error is not None
    assert summary.ran_count == 2, "an unreachable ledger must not cause skips"
    assert summary.skipped_count == 0
    assert len(ran) == 2


def test_flag_off_runs_everything_and_writes_no_ledger(tmp_path, monkeypatch):
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    monkeypatch.setenv("FAULTLINE_MIGRATION_LEDGER", "false")

    ran = []
    monkeypatch.setattr(bm, "_run_psql", lambda dsn, path: ran.append(path) or "")
    monkeypatch.setattr(
        bm, "_connect",
        lambda dsn: pytest.fail("ledger must not be contacted when the flag is off"),
    )

    summary = bm.run_boot_migrations(dsn="postgresql://unused", migrations_dir=str(tmp_path),
                                     echo=False)
    assert summary.ran_count == 1 and summary.skipped_count == 0
    assert summary.ledger_active is False


# ── DB-backed: template equivalence (the check that keeps mint-stamping honest) ───────
_DSN = os.environ.get("POSTGRES_DSN")


def _dedicated_database(suffix: str) -> str:
    """A throwaway database of this test's own, derived from the runner's *_test DSN.

    The migration sweep below fans out into EVERY faultline_% schema and takes ACCESS EXCLUSIVE
    locks (ALTER TABLE ... ADD COLUMN). Run against the shared suite database it (a) mutates
    every other test's tenant mid-run and (b) blocks behind any xdist worker that holds a read
    transaction open — the 2026-09-29 full-suite hang (#44: psql -f 097_tombstone.sql waiting
    49 minutes on an idle-in-transaction backend). Its own database removes both couplings.
    """
    from urllib.parse import urlsplit, urlunsplit

    import psycopg2

    p = urlsplit(_DSN)
    base = p.path.lstrip("/")
    assert base.endswith("_test"), "refusing to derive a throwaway from a non-*_test database"
    name = f"{base}_{suffix}"[:63]
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


def _drop_dedicated_database(dsn: str) -> None:
    from urllib.parse import urlsplit

    import psycopg2

    name = urlsplit(dsn).path.lstrip("/")
    assert "_mint" in name and name != urlsplit(_DSN).path.lstrip("/"), name
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN not set")
def test_fresh_mint_is_migration_equivalent(monkeypatch):
    """A FRESHLY MINTED schema must already carry everything the migrations would apply.

    Provisioning does NOT run migrations — it applies templates/user_schema.sql and seeds from
    `public`. `stamp_schema_as_current` marks a new tenant as migration-current on that basis,
    so if the template ever falls behind, a new tenant is stamped complete while actually being
    stale. That is the one way this design can silently lose a migration.

    This test is the guard: it mints a schema, sweeps EVERY migration over it, and asserts the
    STRUCTURE did not change. It caught a real gap when written —
    `entity_aliases.coreference_warrant` (migration 244) had never been added to the template, so
    every fresh tenant was born one column short.

    Runs in a DEDICATED database (booted with the real corpus first), never the shared one —
    see _dedicated_database for the lock-chain hang that motivated it.

    If this goes red, a migration added a column without updating the template. Update
    src/provisioning/templates/user_schema.sql; do NOT weaken the assertion.
    """
    import subprocess
    import uuid

    import psycopg2

    from src.provisioning.schema_manager import create_user_schema

    mig_dir = os.environ.get("FAULTLINE_MIGRATIONS_DIR", "migrations")
    if not os.path.isdir(mig_dir):
        pytest.skip(f"migrations dir {mig_dir!r} not found")

    dsn = _dedicated_database("mint" + uuid.uuid4().hex[:6])
    try:
        boot = bm.run_boot_migrations(dsn, mig_dir, echo=False)
        assert boot.fatal is None and boot.ledger_active, (boot.fatal, boot.ledger_error)
        monkeypatch.setenv("POSTGRES_DSN", dsn)  # create_user_schema connects via the env DSN
        monkeypatch.setenv("PROVISIONING_CORTEX_BUDGET_SEC",
                           os.environ.get("PROVISIONING_CORTEX_BUDGET_SEC", "2"))

        user_id = str(uuid.uuid4())
        schema, status = create_user_schema(user_id, user_id.replace("-", "_"))
        assert status == "ready", f"fresh provision failed: {status}"

        def columns():
            conn = psycopg2.connect(dsn)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT table_name, column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = %s ORDER BY 1, 2",
                        (schema,),
                    )
                    return cur.fetchall()
            finally:
                conn.close()

        # FIRST-MINT CONTRACT: a new tenant must be usable immediately.
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                    (schema,),
                )
                # 36 since migration 273 (entity_attributes_history); the literal was stale at 35.
                # 38 since migration 300 (growth_work_queue, gauntlet R8). NOTE: pristine saas
                # already minted 37 here (a table drifted in without bumping the literal —
                # pre-existing, observed at f9acab14); this lane's bump counts both.
                # 41 since migration 323 (the sense layer: senses + verbnet_classes +
                # sense_verbnet_bindings — LANGUAGE-STANDARDS-SPEC.md §2; template
                # block + migration are the paired halves).
                # 42 since migration 324 (growth_signals — the error/recall enhancement
                # loop's capture store; template block + migration are the paired
                # halves; rebased on the sense layer's sequential counter).
                assert cur.fetchone()[0] == 42, "fresh mint must land all 42 tables"
                cur.execute(f"SET search_path TO {schema}")
                cur.execute("SELECT count(DISTINCT category) FROM linguistic_cues")
                assert cur.fetchone()[0] >= 20, "fresh mint must seed the cue classes"
                # FILED-2 (SEC-BUG-3 closure addendum; migration 317, 2026-11-12): a fresh
                # mint's entity_attributes carries the FULL bitemporal pair. 011's bare
                # ALTER never fanned to tenant schemas and the template carried only
                # valid_until — the valid_from contract was broken on the fresh-mint lane
                # for every consumer (the serve lane's savepoint fallback masked it). The
                # template column + 317's idempotent fan are the paired halves; this pin
                # holds the template half directly (the sweep-equivalence check below
                # holds it structurally now that 317 FANS — a template miss makes 317's
                # ADD COLUMN IF NOT EXISTS a real change and the sweep goes red).
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'entity_attributes'",
                    (schema,),
                )
                _ea_cols = {r[0] for r in cur.fetchall()}
                assert "valid_from" in _ea_cols, (
                    "fresh-mint entity_attributes must carry valid_from (the bitemporal "
                    f"pair complete); got {sorted(_ea_cols)}")
        finally:
            conn.close()

        before = columns()
        for name in sorted(os.listdir(mig_dir)):
            if name.endswith(".sql"):
                subprocess.run(["psql", dsn, "-f", os.path.join(mig_dir, name)],
                               capture_output=True, text=True, check=False)
        after = columns()

        added = [c for c in after if c not in before]
        assert not added, (
            "the migration corpus added structure a fresh mint did not have — the template has "
            f"fallen behind and stamp_schema_as_current would mark a stale tenant complete: {added}"
        )
    finally:
        _drop_dedicated_database(dsn)


@pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN not set")
def test_cortex_fresh_mint_is_migration_equivalent(monkeypatch):
    """The CORTEX half of the fresh-mint contract — BOTH directions (adversarial R1 upgrade).

    A freshly minted `flagent_<uuid>` schema comes from templates/cortex_schema.sql; existing
    seats get their columns from the numbered migrations. Two drift directions, two arms:

      ARM 1 (corpus-ahead): sweep every migration over a TEMPLATE mint, assert structure did
        not change. Catches a migration adding a column the template lacks.
      ARM 2 (corpus-behind — the pre-301 defect class): replay the corpus onto a BARE schema
        (schema minted, no table — migration 147 builds cortex_episodes) and assert it reaches
        TEMPLATE PARITY (same table/column/type set). Before migration 301 this arm was RED:
        the template carried model/note_type/project and no migration created them, so the
        corpus alone could not reconstruct the schema — only healed by the in-process lazy
        ALTER at note-write time, invisible to a migration-replay deployment (e.g. a
        standalone Volenti Cortex). This is the behavioral pin for 301; the corpus sweep is
        shared, so ARM 2 also fails loud if the sweep no-ops (bare schema left without the
        table ⇒ vacuous-green killed).

    Known blind spot (inherited from the tenant-side test): a pure column DROP in a future
    cortex migration keeps ARM 1's `added` empty; rename/type-change are caught. ARM 2's
    set-equality catches drops on the corpus side.

    If this goes red, template and migrations have drifted — fix the lagging side; do NOT
    weaken the assertion.
    """
    import subprocess
    import uuid

    import psycopg2

    from src.provisioning.schema_manager import create_cortex_schema, derive_cortex_schema_name

    mig_dir = os.environ.get("FAULTLINE_MIGRATIONS_DIR", "migrations")
    if not os.path.isdir(mig_dir):
        pytest.skip(f"migrations dir {mig_dir!r} not found")

    dsn = _dedicated_database("mintcx" + uuid.uuid4().hex[:6])
    try:
        boot = bm.run_boot_migrations(dsn, mig_dir, echo=False)
        assert boot.fatal is None and boot.ledger_active, (boot.fatal, boot.ledger_error)
        monkeypatch.setenv("POSTGRES_DSN", dsn)  # create_cortex_schema connects via the env DSN

        user_id = str(uuid.uuid4())
        schema, status = create_cortex_schema(user_id)
        assert status == "ready", f"fresh cortex provision failed: {status}"
        assert schema == derive_cortex_schema_name(user_id)
        assert schema.startswith("flagent_"), "the truth firewall: cortex schemas are flagent_*"

        # The BARE SEAT: a tenant schema with no cortex sibling. Migration 147 derives
        # `flagent_<slug>` from every `faultline_<slug>` it finds — that IS the corpus's
        # cortex-reconstruction path — so the corpus sweep must build flagent_bare_<hex>'s
        # cortex from nothing (a bare flagent_ schema with no faultline_ sibling is never
        # visited by 147 and would test nothing).
        bare_slug = "bare_" + uuid.uuid4().hex[:8]
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "faultline_{bare_slug}"')
        finally:
            conn.close()

        def columns(of_schema: str):
            conn = psycopg2.connect(dsn)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT table_name, column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = %s ORDER BY 1, 2",
                        (of_schema,),
                    )
                    return cur.fetchall()
            finally:
                conn.close()

        before = columns(schema)
        for name in sorted(os.listdir(mig_dir)):
            if name.endswith(".sql"):
                subprocess.run(["psql", dsn, "-f", os.path.join(mig_dir, name)],
                               capture_output=True, text=True, check=False)
        after = columns(schema)

        # ARM 1 — corpus-ahead drift: the sweep must add nothing a template mint lacks.
        added = [c for c in after if c not in before]
        assert not added, (
            "the migration corpus added cortex structure a fresh mint did not have — the cortex "
            f"template has fallen behind: {added}"
        )

        # ARM 2 — corpus-behind drift (the pre-301 defect class): the corpus alone must
        # reconstruct the cortex shape for a brand-new seat (147 derives flagent_<slug> from
        # the bare faultline_<slug>, then 173/217/261/262/301 uplift it) to TEMPLATE PARITY.
        # Also the vacuous-green kill: if the sweep no-oped, the derived cortex schema has no
        # cortex_episodes at all and this set-compare fails loud on the missing table.
        replayed = set(columns(f"flagent_{bare_slug}"))
        minted = set(before)
        assert replayed, (
            "the migration replay built NOTHING on the bare seat's cortex — the sweep no-oped "
            "(psql/DSN/auth failure), this equivalence check is vacuous"
        )
        missing = minted - replayed
        assert not missing, (
            "the migration corpus cannot reconstruct the cortex template — a column exists ONLY "
            f"in the template (add it to a numbered migration): {sorted(missing)}"
        )
        extra = replayed - minted
        assert not extra, (
            "the migration corpus carries cortex structure the template lacks — the template "
            f"has fallen behind: {sorted(extra)}"
        )
    finally:
        _drop_dedicated_database(dsn)


# ── #55: an unreachable database is a FAILED boot, never "applied=N failed=0 exit 0" ───────
def _closed_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens on it now
    return port


@pytest.mark.skipif(__import__("shutil").which("psql") is None, reason="psql binary not installed")
def test_55_unreachable_database_fails_loudly_with_real_psql(tmp_path, capsys, monkeypatch):
    """Issue #55, the measured input: a DSN pointing at a closed port.

    Before: every psql printed "Connection refused", yet the run ended
    `applied=186 ... failed=0` and main() returned 0. After: nothing counts as applied, the
    summary is fatal, an ERROR line is printed, and main() exits non-zero (2 = unreachable).
    Real psql, real socket — no double — so the exit-status reading is what is pinned.
    """
    for i in range(3):
        (tmp_path / f"00{i}_m.sql").write_text("SELECT 1;")
    dsn = f"postgresql://postgres@127.0.0.1:{_closed_port()}/nope_test?connect_timeout=3"
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    monkeypatch.setenv("FAULTLINE_MIGRATIONS_DIR", str(tmp_path))
    monkeypatch.delenv("FAULTLINE_MIGRATION_LEDGER", raising=False)

    summary = bm.run_boot_migrations(echo=True)
    assert summary.ran_count == 0, f"unreachable DB counted files as applied: {summary.ran}"
    assert summary.fatal and "unreachable" in summary.fatal
    assert summary.not_executed == ["000_m"], "must stop at the first unreachable file"
    out = capsys.readouterr().out
    assert "ERROR: migrations FAILED" in out and "Migrations complete" not in out

    rc = bm.main([])
    assert rc == bm.EXIT_DB_UNREACHABLE == 2
    err = capsys.readouterr().err
    assert "ERROR: boot_migrations: database unreachable" in err


def test_55_psql_connection_exit_is_not_counted_as_applied(tmp_path, monkeypatch):
    """Same rule with the ledger path forced INACTIVE and psql doubled at exit 2."""
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "002_b.sql").write_text("SELECT 2;")
    calls = []
    monkeypatch.setattr(bm, "_run_psql", lambda dsn, path: calls.append(path) or bm.PsqlOutput(
        'psql: error: connection to server at "127.0.0.1", port 1 failed: Connection refused', 2))
    monkeypatch.setattr(bm, "_connect", lambda dsn: (_ for _ in ()).throw(
        bm.LedgerUnavailable("connect failed: OperationalError")))
    summary = bm.run_boot_migrations(dsn="postgresql://unused", migrations_dir=str(tmp_path), echo=False)
    assert summary.ran_count == 0 and summary.fatal
    assert len(calls) == 1, "every later file would fail the same way — stop at the first"


def test_55_control_sql_errors_in_an_executed_file_keep_startup_going(tmp_path, monkeypatch, capsys):
    """Control: a file that REACHED the server and hit SQL errors (psql exit 0, ERROR lines)
    is the historical "startup continues" case — it is ran, errored, and main() exits 0. The
    #55 fix keys on psql's exit status, not on the word ERROR, so this must not turn fatal."""
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    monkeypatch.setenv("FAULTLINE_MIGRATION_LEDGER", "false")
    monkeypatch.setenv("FAULTLINE_MIGRATIONS_DIR", str(tmp_path))
    monkeypatch.setattr(bm, "_run_psql", lambda dsn, path: bm.PsqlOutput(
        'psql:m.sql:3: ERROR:  42710: constraint "x" already exists', 0))
    summary = bm.run_boot_migrations(dsn="postgresql://unused", echo=False)
    assert summary.ran_count == 1 and summary.errored and summary.fatal is None
    assert bm.main([]) == 0


def test_55_psql_own_fatal_error_is_not_executed_and_not_fatal(tmp_path, monkeypatch):
    """psql exit 1 (its own fatal error, e.g. file unreadable): that file did not run, so it
    is never counted applied — but the database is up, so later files still run."""
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "002_b.sql").write_text("SELECT 2;")
    monkeypatch.setenv("FAULTLINE_MIGRATION_LEDGER", "false")
    rcs = iter([1, 0])
    monkeypatch.setattr(bm, "_run_psql", lambda dsn, path: bm.PsqlOutput("", next(rcs)))
    summary = bm.run_boot_migrations(dsn="postgresql://unused", migrations_dir=str(tmp_path), echo=False)
    assert summary.ran == ["002_b"] and summary.not_executed == ["001_a"]
    assert summary.fatal is None
