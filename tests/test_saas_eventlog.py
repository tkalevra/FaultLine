"""SaaS event-log observability — behavior + safety PROOF (no DB required).

Coverage:
  1. emit is a NO-OP when SAAS is off (never touches the control plane).
  2. emit NEVER raises — a bad/closed connection is swallowed.
  3. query_events builds correct parameterized filters (level ANY, account/user equality,
     regex message-match) and returns newest-first.
  4. the ReDoS statement_timeout guard is applied on every query.
  5. tenant_safe_query HARD-FORCES account_id + redacted=TRUE and cannot be widened.
  6. GET /admin/logs is OPERATOR-only (account key / no key → denied).

LIVE-DB is not required: every test drives the pure SQL builder or a faked control-plane
connection. Live integration auto-skips without a control plane.

Run: python3 -m pytest tests/test_saas_eventlog.py -q   (tests/ is gitignored → git add -f)
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import eventlog as ev
from saas import config as saas_config
from saas import accounts as saas_accounts
from saas import logs_admin as saas_logs


# ════════════════════════════════════════════════════════════════════════════════
# Fake control-plane connection (records SQL; can be forced to fail)
# ════════════════════════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, recorder, rows=None, description=None, fail=False):
        self._rec = recorder
        self._rows = rows or []
        self._description = description or [("id",), ("ts",), ("level",),
                                           ("account_id",), ("user_id",),
                                           ("event_type",), ("message",), ("redacted",)]
        self._fail = fail

    @property
    def description(self):
        return self._description

    def execute(self, sql, params=None):
        self._rec.append((sql, params))
        if self._fail:
            raise RuntimeError("forced execute failure")

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, recorder, rows=None, fail=False):
        self._rec = recorder
        self._rows = rows
        self._fail = fail
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self._rec, rows=self._rows, fail=self._fail)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _FakeConnCM:
    """Context manager mirroring saas.db.connection()."""
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")


@pytest.fixture
def saas_off(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)


# ════════════════════════════════════════════════════════════════════════════════
# 1 — emit is a NO-OP when SAAS is off
# ════════════════════════════════════════════════════════════════════════════════

def test_emit_noop_when_saas_off(saas_off, monkeypatch):
    calls = []
    monkeypatch.setattr(ev.db, "connection",
                        lambda: (_ for _ in ()).throw(AssertionError("DB touched while SAAS off")))
    # asyncio.to_thread must never be reached either.
    monkeypatch.setattr(ev.asyncio, "to_thread",
                        lambda *a, **k: calls.append(a) or None)
    asyncio.run(ev.emit("INFO", "test", "hello"))
    assert calls == []  # no thread offload, no DB


# ════════════════════════════════════════════════════════════════════════════════
# 2 — emit NEVER raises (bad/closed connection swallowed)
# ════════════════════════════════════════════════════════════════════════════════

def test_emit_swallows_connection_failure(saas_on, monkeypatch):
    def _boom():
        raise RuntimeError("control plane unreachable")
    monkeypatch.setattr(ev.db, "connection", _boom)
    # Must NOT raise.
    asyncio.run(ev.emit("WARN", "test", "boom", account_id="a", user_id="u"))


def test_emit_swallows_execute_failure(saas_on, monkeypatch):
    rec = []
    conn = _FakeConn(rec, fail=True)
    monkeypatch.setattr(ev.db, "connection", lambda: _FakeConnCM(conn))
    asyncio.run(ev.emit("CRIT", "test", "explode"))
    # failure path rolled back and never raised
    assert conn.rolled_back is True
    assert conn.committed is False


def test_emit_writes_row_on_success(saas_on, monkeypatch):
    rec = []
    conn = _FakeConn(rec)
    monkeypatch.setattr(ev.db, "connection", lambda: _FakeConnCM(conn))
    asyncio.run(ev.emit("INFO", "http.request", "GET /x -> 200",
                        account_id="acct-1", user_id="user-1", redacted=True))
    assert conn.committed is True
    sql, params = rec[0]
    assert "INSERT INTO public.saas_event_log" in sql
    assert params[0] == "INFO"
    assert params[1] == "acct-1"
    assert params[2] == "user-1"
    assert params[3] == "http.request"
    assert params[5] is True


def test_emit_normalizes_bad_level(saas_on, monkeypatch):
    rec = []
    conn = _FakeConn(rec)
    monkeypatch.setattr(ev.db, "connection", lambda: _FakeConnCM(conn))
    asyncio.run(ev.emit("nonsense", "t", "m"))
    sql, params = rec[0]
    assert params[0] == "INFO"  # unknown level coerced to INFO, not dropped


# ════════════════════════════════════════════════════════════════════════════════
# 3 — query_events builds correct filters (pure builder, no DB)
# ════════════════════════════════════════════════════════════════════════════════

def test_build_select_all_filters():
    sql, params = ev._build_select(
        account_id="acct-1", user_id="user-1",
        levels=["warn", "CRIT"], regex="pin.*cpu",
        limit=50, offset=10, force_redacted=False,
    )
    assert "account_id = %s" in sql
    assert "user_id = %s" in sql
    assert "level = ANY(%s)" in sql
    assert "message ~ %s" in sql
    assert "ORDER BY ts DESC, id DESC" in sql
    # bound params, level list normalized (upper-cased)
    assert "acct-1" in params
    assert "user-1" in params
    assert ["WARN", "CRIT"] in params
    assert "pin.*cpu" in params
    # limit/offset appended last
    assert params[-2] == 50
    assert params[-1] == 10
    # no forced redacted clause
    assert "redacted = TRUE" not in sql


def test_build_select_no_filters_is_bare_firehose():
    sql, params = ev._build_select(None, None, None, None, 200, 0, force_redacted=False)
    assert "WHERE" not in sql
    assert params == [200, 0]


def test_build_select_event_type_prefix_is_unscoped_namespaced_like():
    """The operator Engine-health view fetches FaultLine's OWN internal-model events
    (brain.internal.*) UNSCOPED — no account_id, because these rows are ours, not a tenant's.
    _build_select must emit a parameterized LIKE 'brain.internal.%' for the prefix and must
    NOT add an account scope. The wildcard is a bound param (never interpolated)."""
    sql, params = ev._build_select(None, None, None, None, 200, 0,
                                   force_redacted=False, event_type_prefix="brain.internal")
    assert "event_type LIKE %s" in sql
    assert "account_id = %s" not in sql          # unscoped — operational, never tenant
    assert "brain.internal.%" in params          # wildcard bound, not interpolated
    # a %-injection attempt in the prefix is stripped (the token is namespace-safe)
    sql2, params2 = ev._build_select(None, None, None, None, 200, 0,
                                    force_redacted=False, event_type_prefix="brain%internal")
    assert params2.count("braininternal.%") == 1


def test_build_select_caps_limit():
    sql, params = ev._build_select(None, None, None, None, 10 ** 9, 0, force_redacted=False)
    assert params[-2] == ev._MAX_LIMIT


# ════════════════════════════════════════════════════════════════════════════════
# 4 — the statement_timeout ReDoS guard is applied
# ════════════════════════════════════════════════════════════════════════════════

def test_query_applies_statement_timeout(saas_on, monkeypatch):
    rec = []
    conn = _FakeConn(rec, rows=[])
    monkeypatch.setattr(ev.db, "connection", lambda: _FakeConnCM(conn))
    ev.query_events(regex="a+", limit=5)
    # first executed statement must be the per-statement timeout guard
    first_sql, first_params = rec[0]
    assert "statement_timeout" in first_sql.lower()
    assert first_params == (ev.STATEMENT_TIMEOUT_MS,)


def test_query_timeout_raises_event_query_timeout(saas_on, monkeypatch):
    """When the driver reports QueryCanceled on the SELECT, _run_query raises the clean
    EventQueryTimeout (never a hang, never a bare 500)."""
    psycopg2 = pytest.importorskip("psycopg2")
    canceled_exc = psycopg2.errors.QueryCanceled

    rec = []

    class _TimeoutCursor(_FakeCursor):
        def execute(self, sql, params=None):
            rec.append((sql, params))
            if "SELECT" in sql:
                raise canceled_exc("canceling statement due to statement timeout")

    class _TimeoutConn(_FakeConn):
        def cursor(self):
            return _TimeoutCursor(rec)

    conn = _TimeoutConn(rec)
    monkeypatch.setattr(ev.db, "connection", lambda: _FakeConnCM(conn))
    with pytest.raises(ev.EventQueryTimeout):
        ev.query_events(regex="(a+)+$")
    assert conn.rolled_back is True


def test_run_query_noop_when_saas_off(saas_off, monkeypatch):
    monkeypatch.setattr(ev.db, "connection",
                        lambda: (_ for _ in ()).throw(AssertionError("DB touched while off")))
    assert ev.query_events(regex="x") == []


# ════════════════════════════════════════════════════════════════════════════════
# 5 — tenant_safe_query hard-forces account_id + redacted=TRUE, cannot be widened
# ════════════════════════════════════════════════════════════════════════════════

def test_tenant_safe_query_forces_account_and_redacted():
    sql, params = ev._build_select(
        account_id="acct-X", user_id=None, levels=None, regex=None,
        limit=200, offset=0, force_redacted=True,
    )
    assert "account_id = %s" in sql
    assert "redacted = TRUE" in sql
    assert "acct-X" in params


def test_tenant_safe_query_empty_account_returns_nothing(saas_on, monkeypatch):
    monkeypatch.setattr(ev.db, "connection",
                        lambda: (_ for _ in ()).throw(AssertionError("must not query")))
    assert ev.tenant_safe_query("") == []


def test_tenant_safe_query_actually_binds_forced_clauses(saas_on, monkeypatch):
    rec = []
    conn = _FakeConn(rec, rows=[])
    monkeypatch.setattr(ev.db, "connection", lambda: _FakeConnCM(conn))
    ev.tenant_safe_query("acct-9", regex="secret")
    # rec[0] is the timeout SET LOCAL; rec[1] is the SELECT.
    select_sql, select_params = rec[1]
    assert "account_id = %s" in select_sql
    assert "redacted = TRUE" in select_sql
    assert "acct-9" in select_params
    # A tenant cannot widen away redacted — there is no argument for it.
    assert "redacted = FALSE" not in select_sql


# ════════════════════════════════════════════════════════════════════════════════
# 6 — GET /admin/logs is OPERATOR-only
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def logs_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", "op-secret")
    # account key resolver — an account key must NOT reach the firehose
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account",
                        lambda k: "acct-1" if k == "acct-key" else None)
    # stub the DB read so an authorized call returns cleanly
    monkeypatch.setattr(saas_logs._eventlog, "query_events",
                        lambda **kw: [{"id": 1, "message": "ok"}])
    app = FastAPI()
    app.include_router(saas_logs.router)
    return TestClient(app)


def test_logs_operator_allowed(logs_client):
    r = logs_client.get("/admin/logs", headers={"Authorization": "Bearer op-secret"})
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_logs_account_key_denied(logs_client):
    r = logs_client.get("/admin/logs", headers={"Authorization": "Bearer acct-key"})
    assert r.status_code == 403


def test_logs_no_key_denied(logs_client):
    r = logs_client.get("/admin/logs")
    assert r.status_code == 403


def test_logs_timeout_returns_400(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", "op-secret")

    def _timeout(**kw):
        raise saas_logs._eventlog.EventQueryTimeout("too expensive")
    monkeypatch.setattr(saas_logs._eventlog, "query_events", _timeout)
    app = FastAPI()
    app.include_router(saas_logs.router)
    c = TestClient(app)
    r = c.get("/admin/logs?regex=%28a%2B%29%2B",
              headers={"Authorization": "Bearer op-secret"})
    assert r.status_code == 400
