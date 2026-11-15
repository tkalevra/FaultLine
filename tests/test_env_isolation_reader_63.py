"""#63 pin, reader half: another module's import-time os.environ writes must not reach this one.

tests/test_env_isolation_writer_63.py sets FL_ENV_ISOLATION_PROBE_63 and deletes the baseline
SAAS_COOKIE_SECURE at import. Whole-suite, same worker or not, this module must still see the
suite baseline — the state it would see if run on its own. Red on the pre-#63 conftest (the
setdefault/pop applied process-wide at collection), green once collection brackets each module.
"""
import os


def test_foreign_import_time_set_does_not_leak():
    assert "FL_ENV_ISOLATION_PROBE_63" not in os.environ, (
        "a sibling module's import-time os.environ.setdefault leaked into this module")


def test_foreign_import_time_delete_does_not_leak():
    # Control from a different angle: a DELETE of a baseline value must not leak either.
    assert os.environ.get("SAAS_COOKIE_SECURE") == "false", (
        "a sibling module's import-time os.environ.pop removed a suite-baseline value here")


def test_pg_application_name_names_the_running_test():
    expected = ("fl:tests/test_env_isolation_reader_63.py::"
                "test_pg_application_name_names_the_running_test")[:63]
    assert os.environ.get("PGAPPNAME") == expected


def test_a_connection_left_idle_in_transaction_is_attributed_to_its_test(request):
    """#44: the module-end probe finds a backend this test leaves idle in transaction, by name."""
    import sys
    dsn = os.environ.get("POSTGRES_DSN", "")
    if "test" not in dsn:
        import pytest
        pytest.skip("needs a throwaway *_test POSTGRES_DSN")
    import psycopg2
    conftest = sys.modules.get("conftest") or next(
        m for n, m in sys.modules.items() if n.endswith("conftest") and
        hasattr(m, "idle_in_transaction_backends"))
    conn = psycopg2.connect(dsn)          # PGAPPNAME is read here
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")       # opens a transaction, then goes idle in it
        rows = conftest.idle_in_transaction_backends(os.environ["PGAPPNAME"])
        assert any(r[0] == conn.info.backend_pid for r in rows), rows
    finally:
        conn.rollback()
        conn.close()
    assert not any(r[0] == conn.info.backend_pid
                   for r in conftest.idle_in_transaction_backends(os.environ["PGAPPNAME"]))


# ── run-time half: a test body's own unrestored os.environ writes end with that test ─────────
def test_runtime_a_test_body_writes_and_pops_without_restoring():
    os.environ["FL_RUNTIME_PROBE_63"] = "leaked"   # the shape ~30 test bodies use
    os.environ.pop("SAAS_COOKIE_SECURE", None)     # ... and the DSN-pop shape


def test_runtime_b_the_next_test_sees_neither():
    assert "FL_RUNTIME_PROBE_63" not in os.environ
    assert os.environ.get("SAAS_COOKIE_SECURE") == "false"
