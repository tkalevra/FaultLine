"""SaaS immutable control-plane AUDIT LOG — behavior + safety PROOF (no live DB).

Coverage:
  1. record is a NO-OP when SAAS is off (never touches the control plane).
  2. record NEVER raises — a bad/closed connection is swallowed (audit must not wedge an op).
  3. record writes the row with the right params on success.
  4. record coerces a non-UUID account_id to NULL (the column is UUID) rather than failing.
  5. _build_query builds correct parameterized filters (action / account / result ANY).
  6. query_audit is a no-op ([]) when SAAS is off.
  7. control-plane mutations (revoke_key / register_seat) EMIT an audit row (wiring proof).
  8. GET /admin/audit is OPERATOR-only (account key / no key → denied) and read-only.

LIVE-DB is not required. The DB-level append-only trigger (migration 139) is a separate,
DB-enforced guarantee verified by the migration; here we prove the writer + wiring + authz.

Run: python3 -m pytest tests/test_saas_audit.py -q
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import audit as au
from saas import accounts as saas_accounts
from saas import seats as saas_seats
from saas import logs_admin as saas_logs


# ── fake control-plane connection (records SQL; can be forced to fail) ──────────────

class _FakeCursor:
    def __init__(self, recorder, rows=None, description=None, fail=False):
        self._rec = recorder
        self._rows = rows if rows is not None else []
        self._description = description or [("id",)]
        self._fail = fail

    @property
    def description(self):
        return self._description

    def execute(self, sql, params=None):
        self._rec.append((sql, params))
        if self._fail:
            raise RuntimeError("forced execute failure")

    def fetchone(self):
        return self._rows[0] if self._rows else None

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


# ── 1: no-op when SAAS off ──────────────────────────────────────────────────────────

def test_record_noop_when_saas_off(saas_off, monkeypatch):
    monkeypatch.setattr(au.db, "connection",
                        lambda: (_ for _ in ()).throw(AssertionError("DB touched while off")))
    assert au.record("key.mint") is False


# ── 2: never raises ──────────────────────────────────────────────────────────────────

def test_record_swallows_connection_failure(saas_on, monkeypatch):
    def _boom():
        raise RuntimeError("control plane unreachable")
    monkeypatch.setattr(au.db, "connection", _boom)
    assert au.record("account.panic", account_id="a") is False  # no raise


def test_record_swallows_execute_failure(saas_on, monkeypatch):
    rec = []
    conn = _FakeConn(rec, fail=True)
    monkeypatch.setattr(au.db, "connection", lambda: _FakeConnCM(conn))
    assert au.record("key.revoke") is False
    assert conn.rolled_back is True
    assert conn.committed is False


# ── 3: writes the row on success ─────────────────────────────────────────────────────

_ACCT = "11111111-1111-4111-8111-111111111111"


def test_record_writes_row(saas_on, monkeypatch):
    rec = []
    conn = _FakeConn(rec)
    monkeypatch.setattr(au.db, "connection", lambda: _FakeConnCM(conn))
    ok = au.record("key.mint", result="success", account_id=_ACCT,
                   target_type="api_key", target_id="k1", detail="minted")
    assert ok is True
    assert conn.committed is True
    sql, params = rec[0]
    assert "INSERT INTO public.saas_audit_log" in sql
    # (actor_type, actor_id, account_id, target_type, target_id, action, result, detail, ip)
    assert params[2] == _ACCT
    assert params[3] == "api_key"
    assert params[5] == "key.mint"
    assert params[6] == "success"


def test_record_normalizes_bad_result(saas_on, monkeypatch):
    rec = []
    monkeypatch.setattr(au.db, "connection", lambda: _FakeConnCM(_FakeConn(rec)))
    au.record("x", result="nonsense")
    assert rec[0][1][6] == "success"  # unknown result coerced


# ── 4: non-UUID account_id coerced to NULL ───────────────────────────────────────────

def test_record_coerces_bad_account_id_to_null(saas_on, monkeypatch):
    rec = []
    monkeypatch.setattr(au.db, "connection", lambda: _FakeConnCM(_FakeConn(rec)))
    au.record("account.create", account_id="not-a-uuid")
    assert rec[0][1][2] is None  # coerced, not a failed write


# ── 5: query builder ─────────────────────────────────────────────────────────────────

def test_build_query_all_filters():
    sql, params = au._build_query(
        account_id=_ACCT, action="key.revoke",
        results=["failure", "DENIED"], limit=50, offset=10)
    assert "account_id = %s" in sql
    assert "action = %s" in sql
    assert "result = ANY(%s)" in sql
    assert "ORDER BY ts DESC" in sql
    assert _ACCT in params
    assert ["failure", "denied"] in params
    assert params[-2] == 50 and params[-1] == 10


# ── 6: query no-op off ───────────────────────────────────────────────────────────────

def test_query_audit_noop_off(saas_off, monkeypatch):
    monkeypatch.setattr(au.db, "connection",
                        lambda: (_ for _ in ()).throw(AssertionError("must not query")))
    assert au.query_audit() == []


# ── 7: wiring proof — control-plane mutations emit an audit row ──────────────────────

def test_revoke_key_emits_audit(saas_on, monkeypatch):
    calls = []
    monkeypatch.setattr(saas_accounts._audit, "record",
                        lambda *a, **k: calls.append((a, k)))
    # fake DB: revoke UPDATE reports one row changed (rowcount via a fake cursor)
    class _Cur(_FakeCursor):
        rowcount = 1
    class _Conn(_FakeConn):
        def cursor(self):
            return _Cur(self._rec)
    monkeypatch.setattr(saas_accounts.db, "connection",
                        lambda: _FakeConnCM(_Conn([])))
    assert saas_accounts.revoke_key(_ACCT, "key-1") is True
    assert any(a and a[0] == "key.revoke" for a, k in calls)


def test_register_seat_emits_audit(saas_on, monkeypatch):
    calls = []
    monkeypatch.setattr(saas_seats._audit, "record",
                        lambda *a, **k: calls.append((a, k)))
    # fake DB path through register_seat: account active (seat_limit high), no existing seat,
    # 0 active seats, INSERT ok.
    seq = [(10, "active"), None, (0,)]
    class _Cur(_FakeCursor):
        def __init__(self, rec):
            super().__init__(rec)
            self._i = 0
        def fetchone(self):
            v = seq[self._i] if self._i < len(seq) else None
            self._i += 1
            return v
    class _Conn(_FakeConn):
        def cursor(self):
            return _Cur(self._rec)
    monkeypatch.setattr(saas_seats.db, "connection", lambda: _FakeConnCM(_Conn([])))
    out = saas_seats.register_seat(_ACCT, "22222222-2222-4222-8222-222222222222")
    assert out["ok"] is True
    assert any(a and a[0] == "seat.register" for a, k in calls)


# ── 8: GET /admin/audit is operator-only ─────────────────────────────────────────────

@pytest.fixture
def audit_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", "op-secret")
    from saas import accounts as _acc
    monkeypatch.setattr(_acc, "resolve_key_to_account",
                        lambda k: "acct-1" if k == "acct-key" else None)
    monkeypatch.setattr(saas_logs._audit, "query_audit",
                        lambda **kw: [{"id": 1, "action": "key.mint"}])
    app = FastAPI()
    app.include_router(saas_logs.router)
    return TestClient(app)


def test_audit_operator_allowed(audit_client):
    r = audit_client.get("/admin/audit", headers={"Authorization": "Bearer op-secret"})
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_audit_account_key_denied(audit_client):
    r = audit_client.get("/admin/audit", headers={"Authorization": "Bearer acct-key"})
    assert r.status_code == 403


def test_audit_no_key_denied(audit_client):
    r = audit_client.get("/admin/audit")
    assert r.status_code == 403
