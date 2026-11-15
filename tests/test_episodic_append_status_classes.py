"""first-touch-cold-path round 5 — `/episodic/append` answers with the CLASS of failure.

The MCP re-append drain treats a POSITIVE refusal as terminal (CRITICAL) and everything else as
"retry until answered", so the server must never disguise DB/transport trouble as a refusal
(measured round 4: a DB refusing connections came back 200 soft_error → 8 false CRITICALs on a
row that existed). Pins `main.py::episodic_append` / `episodic_probe` / `_episodic_failure_is_retryable`:
  * psycopg2 OperationalError / InterfaceError (refused, lost, statement timeout) → HTTP 503,
    body {"status":"unavailable","retryable":true};
  * ProgrammingError (undefined table/column — unknown or un-migrated seat) → 200 soft_error;
  * a malformed turn_key / user_id → 422 soft_error (positive, never retried).
No database: `psycopg2.connect` is patched to raise each class. Run through tools/fltest.py.
"""
import os
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_UID = str(uuid.uuid4())


def _resp_parts(r):
    """(status_code, body) for either a plain dict return or a JSONResponse."""
    if isinstance(r, dict):
        return 200, r
    import json
    return r.status_code, json.loads(r.body)


@pytest.fixture
def main(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://x:y@127.0.0.1:1/never")
    import src.api.main as m
    return m


def _req(main, **kw):
    from src.api.models import EpisodicAppendRequest
    base = dict(user_id=_UID, raw_text="Retain me.", source="mcp", turn_key=uuid.uuid4().hex)
    base.update(kw)
    return EpisodicAppendRequest(**base)


@pytest.mark.parametrize("exc", [
    psycopg2.OperationalError("connection refused"),
    psycopg2.OperationalError("canceling statement due to statement timeout"),
    psycopg2.errors.QueryCanceled("statement timeout"),
    psycopg2.InterfaceError("connection already closed"),
])
def test_db_transport_trouble_is_503_retryable(main, monkeypatch, exc):
    monkeypatch.setattr(main.psycopg2, "connect", lambda *a, **k: (_ for _ in ()).throw(exc))
    code, body = _resp_parts(main.episodic_append(_req(main)))
    assert code == 503 and body["status"] == "unavailable" and body["retryable"] is True, (code, body)
    code, body = _resp_parts(main.episodic_probe(_req(main)))
    assert code == 503 and body["status"] == "unavailable" and body["retained"] is False


@pytest.mark.parametrize("exc", [
    psycopg2.errors.UndefinedTable('relation "episodic_log" does not exist'),
    psycopg2.errors.UndefinedColumn('column "turn_key" does not exist'),
    psycopg2.errors.InvalidSchemaName("schema does not exist"),
])
def test_unknown_or_unmigrated_seat_is_a_positive_refusal(main, monkeypatch, exc):
    monkeypatch.setattr(main.psycopg2, "connect", lambda *a, **k: (_ for _ in ()).throw(exc))
    code, body = _resp_parts(main.episodic_append(_req(main)))
    assert code == 200 and body["status"] == "soft_error", (code, body)
    code, body = _resp_parts(main.episodic_probe(_req(main)))
    assert code == 200 and body["status"] == "soft_error"


def test_malformed_key_and_seat_are_422_positive_refusals(main, monkeypatch):
    called = []
    monkeypatch.setattr(main.psycopg2, "connect", lambda *a, **k: called.append(1))
    code, body = _resp_parts(main.episodic_append(_req(main, turn_key="x" * 200)))
    assert code == 422 and body["status"] == "soft_error" and "turn_key" in body["reason"]
    code, body = _resp_parts(main.episodic_append(_req(main, user_id="not-a-uuid")))
    assert code == 422 and body["status"] == "soft_error" and "user_id" in body["reason"]
    assert not called  # refused before any DB contact


def test_classifier_helper_is_the_single_place():
    import src.api.main as main
    f = main._episodic_failure_is_retryable
    assert f(psycopg2.OperationalError()) and f(psycopg2.InterfaceError()) and f(ConnectionError())
    assert not f(psycopg2.errors.UndefinedTable()) and not f(psycopg2.DataError()) \
        and not f(psycopg2.IntegrityError()) and not f(ValueError())
