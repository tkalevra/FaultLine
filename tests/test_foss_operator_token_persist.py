"""#145: the auto-minted operator token survives a backend restart.

Before: with ``FAULTLINE_ADMIN_TOKEN`` unset, the lifespan minted a fresh random token into
``os.environ`` on EVERY process start, so ``docker compose restart faultline`` locked the
operator out (old token 401) and printed another live credential to the log each boot.

Now: the first boot stores only the SHA-256 in ``public.operator_admin_token`` and prints the
plaintext once; later boots print nothing and the same token keeps working.

Needs the fl-test-pg throwaway postgres (127.0.0.1:55432); the database name ends in ``_test``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg2
import pytest

_ADMIN = os.environ.get("FL_SEATCAP_ADMIN_DSN", "postgresql://faultline:faultline@127.0.0.1:55432/faultline")
_DBNAME = "optoken_pin_test"
_ROOT = Path(__file__).resolve().parents[1]


def _dsn() -> str:
    return _ADMIN.rsplit("/", 1)[0] + "/" + _DBNAME


@pytest.fixture(scope="module")
def token_db():
    try:
        admin = psycopg2.connect(_ADMIN, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"throwaway postgres unavailable: {exc}")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {_DBNAME}")
        cur.execute(f"CREATE DATABASE {_DBNAME}")
    sql = (_ROOT / "migrations" / "291_operator_admin_token.sql").read_text()
    with psycopg2.connect(_dsn()) as db, db.cursor() as cur:
        cur.execute(sql)
    mp = pytest.MonkeyPatch()
    mp.setenv("POSTGRES_DSN", _dsn())
    mp.delenv("FAULTLINE_ADMIN_TOKEN", raising=False)
    yield _dsn(), mp
    mp.undo()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {_DBNAME} WITH (FORCE)")
    admin.close()


def _boot(capsys):
    """One simulated backend boot: a fresh process has an empty cache."""
    from src.api import operator_token as ot
    ot._reset_cache_for_tests()
    outcome = ot.ensure_operator_token()
    printed = capsys.readouterr().out
    m = re.search(r"FAULTLINE_ADMIN_TOKEN \([^)]*\):\n  (\S+)", printed)
    return outcome, (m.group(1) if m else None), printed


def test_token_is_stable_across_restarts_and_printed_once(token_db, capsys):
    from src.api import operator_token as ot
    outcome1, token, _ = _boot(capsys)
    assert outcome1 == "minted" and token
    outcome2, again, printed2 = _boot(capsys)
    assert outcome2 == "persisted"
    assert again is None and token not in printed2, "the token must be printed on first boot only"
    assert ot.operator_token_ok(token), "the first-boot token must still work after a restart"
    assert not ot.operator_token_ok(token + "x")


def test_only_the_hash_is_stored(token_db, capsys):
    dsn, _ = token_db
    from src.api import operator_token as ot
    with psycopg2.connect(dsn) as db, db.cursor() as cur:
        cur.execute("SELECT token_sha256 FROM public.operator_admin_token")
        (stored,) = cur.fetchone()
    assert re.fullmatch(r"[0-9a-f]{64}", stored)
    ot._reset_cache_for_tests()


def test_console_accepts_the_persisted_token_after_restart(token_db, capsys):
    """The real dashboard gate, env unset: the persisted token authenticates (was env-only)."""
    from src.api import operator_token as ot
    with psycopg2.connect(token_db[0]) as db, db.cursor() as cur:
        cur.execute("DELETE FROM public.operator_admin_token")
    _, token, _ = _boot(capsys)
    ot._reset_cache_for_tests()  # restart
    from fastapi.testclient import TestClient
    from src.api import main
    c = TestClient(main.app, raise_server_exceptions=False)
    assert c.get("/api/dashboard/config", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert c.get("/api/dashboard/config", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_rotate_replaces_the_token(token_db, capsys):
    from src.api import operator_token as ot
    _, old, _ = _boot(capsys)
    if old is None:  # a row already exists from an earlier test: rotate to get a known value
        ot.rotate_operator_token()
        old = re.search(r"\(rotated\):\n  (\S+)", capsys.readouterr().out).group(1)
    assert ot.rotate_operator_token() == "rotated"
    new = re.search(r"\(rotated\):\n  (\S+)", capsys.readouterr().out).group(1)
    ot._reset_cache_for_tests()
    assert ot.operator_token_ok(new) and not ot.operator_token_ok(old)


def test_env_token_wins_and_nothing_is_minted(token_db, capsys):
    _, mp = token_db
    from src.api import operator_token as ot
    mp.setenv("FAULTLINE_ADMIN_TOKEN", "pinned-op")
    try:
        assert ot.ensure_operator_token() == "env"
        assert "pinned-op" not in capsys.readouterr().out
        assert ot.operator_token_ok("pinned-op")
    finally:
        mp.delenv("FAULTLINE_ADMIN_TOKEN")


def test_pinning_retires_the_first_boot_token(token_db, capsys):
    """#164: after a pinned boot, un-pinning must not revive the logged first-boot token."""
    dsn, mp = token_db
    from src.api import operator_token as ot
    with psycopg2.connect(dsn) as db, db.cursor() as cur:
        cur.execute("DELETE FROM public.operator_admin_token")
    _, t0, _ = _boot(capsys)
    assert t0
    mp.setenv("FAULTLINE_ADMIN_TOKEN", "pinned-op")
    try:
        assert ot.ensure_operator_token() == "env"
    finally:
        mp.delenv("FAULTLINE_ADMIN_TOKEN")
    ot._reset_cache_for_tests()
    assert not ot.operator_token_ok(t0), "un-pinning revived the first-boot token"
    outcome, t1, _ = _boot(capsys)
    assert outcome == "minted" and t1 and t1 != t0
