"""Minimal suite-wide fixtures for the FOSS test suite (CBA port).

Deliberately NARROW: the public-core repo has no SaaS baseline environment and no
boot-migration ledger, so this conftest provides exactly what the ported R6/R7/R8/R9
test modules need and NOTHING that mutates process state at import:

  * ``postgres_dsn`` — the runner's DSN if a database is actually REACHABLE, else
    None. Tests request it (or ``requires_postgres``) and skip honestly when None.
    A truthy POSTGRES_DSN is not evidence of a database, so this connects once and
    the answer is the connection, not the string.
  * ``requires_postgres`` — skip-gate + DSN guarantee for the duration of a test.
  * ``apply_migration_files`` — the FOSS migration convention (docker-entrypoint
    runs every ``migrations/*.sql`` via psql at boot, idempotently) expressed for
    tests: apply the whole corpus, sorted, each file as one batch, autocommit.

No SAAS_MODE, no env baseline, no module-state surgery — existing tests that do not
request these fixtures are unaffected.
"""

import glob
import os

import pytest

_RUNNER_POSTGRES_DSN = os.environ.get("POSTGRES_DSN") or None


@pytest.fixture(scope="session")
def postgres_dsn():
    """The runner's DSN if a database is actually REACHABLE, else None."""
    if not _RUNNER_POSTGRES_DSN:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(_RUNNER_POSTGRES_DSN, connect_timeout=5)
        conn.close()
        return _RUNNER_POSTGRES_DSN
    except Exception:
        return None


@pytest.fixture
def requires_postgres(postgres_dsn, monkeypatch):
    """Skip (honestly) when there is no reachable database; else pin POSTGRES_DSN."""
    if postgres_dsn is None:
        pytest.skip("no reachable database (POSTGRES_DSN unset or unreachable)")
    monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
    return postgres_dsn


@pytest.fixture
def apply_migration_files():
    """Return a callable(dsn) that applies the repo's full migration corpus.

    Mirrors docker-entrypoint.sh: every migrations/*.sql, lexicographic order,
    each file executed as one batch with autocommit (the files are idempotent by
    contract — guarded fanout DO blocks, IF NOT EXISTS throughout).
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mig_dir = os.path.join(repo_root, "migrations")

    def _apply(dsn, mig_directory=None):
        import psycopg2
        target = mig_directory or mig_dir
        files = sorted(glob.glob(os.path.join(target, "*.sql")))
        assert files, f"no migration files under {target}"
        for path in files:
            with open(path, "r", encoding="utf-8") as fh:
                sql = fh.read()
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
            finally:
                conn.close()

    return _apply
