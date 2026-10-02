"""#148: every OpenWebUI user occupies a seat, and the operator can seat one from the console.

Before: minting ANY seat (say, for Claude Desktop) silently 403'd every OpenWebUI user on the
shared MCP key with "seat required", while the console and README said OpenWebUI users were
"scoped automatically". The only fix was the re-seat-by-user_id field that nothing pointed to,
and the operator had no way to learn a refused user's id.

Now (design (a), the cap is NOT loosened):
  * the 403 names the refused user_id and says exactly how to seat it;
  * the MCP gate lists the refused user_id in ``public.dashboard_seat_requests`` (bounded);
  * ``GET /api/dashboard/seat-requests`` (operator-gated) lists refused users and tenants that
    hold memory without a seat; minting a seat for one clears it and admits the user;
  * a seated OpenWebUI user still counts against FOSS_MAX_SEATS.

Needs the fl-test-pg throwaway postgres (127.0.0.1:55432); the database name ends in ``_test``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid

import psycopg2
import pytest

_ADMIN = os.environ.get("FL_SEATCAP_ADMIN_DSN", "postgresql://faultline:faultline@127.0.0.1:55432/faultline")
_DBNAME = "owuiseats_pin_test"
OP = {"Authorization": "Bearer operator-owui-token"}
SHARED = "shared-owui-key"


def _dsn() -> str:
    return _ADMIN.rsplit("/", 1)[0] + "/" + _DBNAME


@pytest.fixture(scope="module")
def migrated_db():
    try:
        admin = psycopg2.connect(_ADMIN, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"throwaway postgres unavailable: {exc}")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {_DBNAME}")
        cur.execute(f"CREATE DATABASE {_DBNAME}")
    env = {**os.environ, "POSTGRES_DSN": _dsn(), "FAULTLINE_MIGRATIONS_DIR": "./migrations"}
    subprocess.run([sys.executable, "-m", "src.provisioning.boot_migrations"],
                   env=env, check=True, capture_output=True)
    mp = pytest.MonkeyPatch()
    mp.setenv("POSTGRES_DSN", _dsn())
    mp.setenv("FAULTLINE_ADMIN_TOKEN", "operator-owui-token")
    yield _dsn()
    mp.undo()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {_DBNAME} WITH (FORCE)")
    admin.close()


@pytest.fixture()
def db(migrated_db):
    conn = psycopg2.connect(migrated_db)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE public.dashboard_seats, public.user_provisioning, public.users CASCADE")
        cur.execute("SELECT to_regclass('public.dashboard_seat_requests')")
        if cur.fetchone()[0]:
            cur.execute("TRUNCATE public.dashboard_seat_requests")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture()
def mcp(db, monkeypatch):
    from fastapi.testclient import TestClient
    from src.mcp import http_server
    monkeypatch.setattr(http_server, "MCP_API_KEY", SHARED)

    async def _fake_gate(user_id):
        return ""  # falsy → the door answers without a backend

    monkeypatch.setattr(http_server._mcp, "_ensure_provisioned", _fake_gate)
    return TestClient(http_server.app)


def _console():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api import dashboard
    dashboard._rate_buckets.clear()
    app = FastAPI()
    app.include_router(dashboard.router)
    return TestClient(app)


def _owui_recall(client, owui_uid):
    """What OpenWebUI sends: the shared key plus its signed-in user's id header."""
    return client.post("/recall_memory", json={"query": "what do I know"},
                       headers={"Authorization": f"Bearer {SHARED}", "X-OpenWebUI-User-Id": owui_uid})


def _mint(label="claude-desktop", user_id=None):
    body = {"label": label}
    if user_id:
        body["user_id"] = user_id
    return _console().post("/api/dashboard/seats", json=body, headers=OP)


def _provision(conn, uid: str) -> None:
    from src.provisioning.provisioning_status import ensure_user_provisioned
    from src.provisioning.schema_manager import derive_user_slug_from_uuid
    ensure_user_provisioned(uid, derive_user_slug_from_uuid(uid), conn)


def test_refusal_names_the_user_and_says_how_to_seat_it(mcp):
    assert _mint().status_code == 201
    owui = str(uuid.uuid4())
    r = _owui_recall(mcp, owui)
    assert r.status_code == 403
    assert "seat required" in r.text
    assert owui in r.text, "the operator needs the refused user_id"
    assert "OpenWebUI" in r.text and "console" in r.text, r.text


def test_refused_owui_user_is_listed_then_seated_from_the_console(mcp):
    assert _mint().status_code == 201
    owui = str(uuid.uuid4())
    assert _owui_recall(mcp, owui).status_code == 403
    c = _console()
    listed = c.get("/api/dashboard/seat-requests", headers=OP)
    assert listed.status_code == 200, listed.text
    waiting = {w["user_id"]: w for w in listed.json()["waiting"]}
    assert owui in waiting and "requested" in waiting[owui]["source"]
    assert listed.json()["seat_posture"] is True
    # Seat it the way the console does (the re-seat-by-user_id mint).
    assert _mint(label="jane", user_id=owui).status_code == 201
    assert owui not in {w["user_id"] for w in c.get("/api/dashboard/seat-requests", headers=OP).json()["waiting"]}
    assert _owui_recall(mcp, owui).status_code == 200


def test_tenant_with_memory_but_no_seat_is_listed(mcp, db):
    legacy = str(uuid.uuid4())
    _provision(db, legacy)  # an OpenWebUI user from before the first seat
    assert _mint().status_code == 201
    waiting = {w["user_id"]: w for w in _console().get("/api/dashboard/seat-requests", headers=OP).json()["waiting"]}
    assert legacy in waiting and "has_memory" in waiting[legacy]["source"]


def test_seated_owui_users_count_against_the_cap(mcp):
    from src.api.dashboard import FOSS_MAX_SEATS
    for i in range(FOSS_MAX_SEATS):
        assert _mint(label=f"s{i}").status_code == 201
    late = str(uuid.uuid4())
    assert _owui_recall(mcp, late).status_code == 403
    assert _mint(label="late", user_id=late).status_code == 409   # cap unchanged
    assert _owui_recall(mcp, late).status_code == 403


def test_seat_requests_route_is_operator_gated(db):
    assert _console().get("/api/dashboard/seat-requests").status_code == 401


def test_waiting_list_is_bounded(db):
    from src.provisioning.provisioning_status import SEAT_REQUESTS_KEEP, record_seat_request
    with db.cursor() as cur:
        for _ in range(SEAT_REQUESTS_KEEP + 10):
            record_seat_request(cur, str(uuid.uuid4()))
        cur.execute("SELECT COUNT(*) FROM public.dashboard_seat_requests")
        assert cur.fetchone()[0] == SEAT_REQUESTS_KEEP
    db.commit()
