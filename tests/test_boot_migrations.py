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
_SCHEMAS = ["public", "faultline_alpha", "faultline_partial"]


def test_clean_run_blames_nobody():
    lines, blamed, unattributed = bm.attribute_errors("CREATE TABLE\nINSERT 0 3\n", _SCHEMAS)
    assert lines == [] and blamed == set() and unattributed is False


def test_error_naming_a_schema_blames_only_that_schema():
    """~20 migrations error every boot against one partial schema. The healthy schemas must
    still be stampable, or those files never converge and the ledger delivers nothing."""
    out = 'psql:m.sql:60: ERROR:  relation "faultline_partial.staged_facts" does not exist'
    lines, blamed, unattributed = bm.attribute_errors(out, _SCHEMAS)
    assert len(lines) == 1
    assert blamed == {"faultline_partial"}
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
    """`faultline_partial` must not absorb the blame belonging to `faultline_partial_two`."""
    schemas = ["public", "faultline_partial", "faultline_partial_two"]
    out = 'ERROR:  relation "faultline_partial_two.facts" does not exist'
    _lines, blamed, unattributed = bm.attribute_errors(out, schemas)
    assert blamed == {"faultline_partial_two"}
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


@pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN not set")
def test_fresh_mint_is_migration_equivalent():
    """A FRESHLY MINTED schema must already carry everything the migrations would apply.

    Provisioning does NOT run migrations — it applies templates/user_schema.sql and seeds from
    `public`. `stamp_schema_as_current` marks a new tenant as migration-current on that basis,
    so if the template ever falls behind, a new tenant is stamped complete while actually being
    stale. That is the one way this design can silently lose a migration.

    This test is the guard: it mints a schema, sweeps EVERY migration over it, and asserts the
    STRUCTURE did not change. It caught a real gap when written —
    `entity_aliases.coreference_warrant` (migration 244) had never been added to the template, so
    every fresh tenant was born one column short.

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

    user_id = str(uuid.uuid4())
    schema, status = create_user_schema(user_id, user_id.replace("-", "_"))
    assert status == "ready", f"fresh provision failed: {status}"

    def columns():
        with psycopg2.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s ORDER BY 1, 2",
                (schema,),
            )
            return cur.fetchall()

    try:
        # FIRST-MINT CONTRACT: a new tenant must be usable immediately.
        with psycopg2.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                (schema,),
            )
            # 34 on the open-core template (entity_attributes_history from migration 273
            # included; the hosted layer's two brain-telemetry tables are not part of it).
            assert cur.fetchone()[0] == 34, "fresh mint must land all 34 tables"
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("SELECT count(DISTINCT category) FROM linguistic_cues")
            assert cur.fetchone()[0] >= 20, "fresh mint must seed the cue classes"

        before = columns()
        for name in sorted(os.listdir(mig_dir)):
            if name.endswith(".sql"):
                subprocess.run(["psql", _DSN, "-f", os.path.join(mig_dir, name)],
                               capture_output=True, text=True, check=False)
        after = columns()

        added = [c for c in after if c not in before]
        assert not added, (
            "the migration corpus added structure a fresh mint did not have — the template has "
            f"fallen behind and stamp_schema_as_current would mark a stale tenant complete: {added}"
        )
    finally:
        with psycopg2.connect(_DSN) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                cur.execute("DELETE FROM public.schema_migration_status WHERE schema_name = %s",
                            (schema,))
                cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
