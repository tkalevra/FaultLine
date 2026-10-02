"""FOSS seat cap: a client cannot create more than FOSS_MAX_SEATS tenants without a minted seat.

THE BYPASS (found by audit, reproduced on a fresh migrated *_test database):

    The dashboard caps MINTED seats at ``FOSS_MAX_SEATS`` (atomic, advisory-locked), but the
    tenant itself is created by ``provisioning_status.ensure_user_provisioned``, which every
    tenant-schema endpoint reaches (``/ingest``, ``/query``, ``/retract``, ``/classify-intent``,
    ``/harvest-spans``, ``/extract/rewrite`` via ``_ensure_tenant_ready``; and
    ``/provisioning/status``) and which provisioned ANY unseen UUID. The MCP shared key (or the
    anonymous dev posture) passes the client's ``user_id`` straight through ``bind_tenant``, and
    the backend on :8000 takes the body ``user_id`` with no auth at all. So a sixth, seventh,
    ... hundredth tenant was one request away, seats or no seats.

THE FIX: the cap is enforced where a tenant is BORN. ``ensure_user_provisioned`` admits a NEW
user_id only under the same advisory lock the seat mint holds, and only if (a) that user_id holds
an active ``dashboard_seats`` row, or (b) no seat is active yet (open/dev posture) and fewer than
``FOSS_MAX_SEATS`` tenants exist. Otherwise it raises ``SeatLimitError`` (→ HTTP 403 "seat limit
— mint a seat in the dashboard"). The MCP refuses the same request at its front door.

Needs the fl-test-pg throwaway postgres (127.0.0.1:55432); the database name ends in ``_test``.
"""
from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

_ADMIN = os.environ.get("FL_SEATCAP_ADMIN_DSN", "postgresql://faultline:faultline@127.0.0.1:55432/faultline")
_DBNAME = "seatcap_pin_test"


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
    import subprocess
    import sys
    env = {**os.environ, "POSTGRES_DSN": _dsn(), "FAULTLINE_MIGRATIONS_DIR": "./migrations"}
    subprocess.run([sys.executable, "-m", "src.provisioning.boot_migrations"],
                   env=env, check=True, capture_output=True)
    mp = pytest.MonkeyPatch()
    mp.setenv("POSTGRES_DSN", _dsn())
    mp.setenv("FAULTLINE_ADMIN_TOKEN", "operator-pin-token")
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
    conn.commit()
    yield conn
    conn.close()


def _provisioned(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM public.user_provisioning")
        n = cur.fetchone()[0]
    conn.commit()
    return n


def _provision(conn, uid: str) -> None:
    from src.provisioning.provisioning_status import ensure_user_provisioned
    from src.provisioning.schema_manager import derive_user_slug_from_uuid
    ensure_user_provisioned(uid, derive_user_slug_from_uuid(uid), conn)


def _seat_limit_error():
    from src.provisioning import provisioning_status
    return getattr(provisioning_status, "SeatLimitError", None)


def _expect_refused(conn, uid: str) -> None:
    before = _provisioned(conn)
    err = _seat_limit_error() or ()
    refusal = None
    try:
        _provision(conn, uid)
    except err as exc:  # type: ignore[misc]
        refusal = str(exc)
    conn.rollback()
    after = _provisioned(conn)
    assert after == before, f"BYPASS: user_id {uid[:8]} was provisioned ({before} -> {after} tenants)"
    assert refusal and "mint a seat" in refusal, f"no seat-limit refusal raised (got {refusal!r})"


def _mint_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api import dashboard
    dashboard._rate_buckets.clear()
    app = FastAPI()
    app.include_router(dashboard.router)
    return TestClient(app), {"Authorization": "Bearer operator-pin-token"}


def _mint(n: int) -> list[dict]:
    client, hdr = _mint_client()
    out = []
    for i in range(n):
        r = client.post("/api/dashboard/seats", json={"label": f"s{i}"}, headers=hdr)
        out.append({"status": r.status_code, **r.json()})
    return out


# ── Pin 1: the mint cap (existing behaviour) ─────────────────────────────────

def test_mint_five_sixth_refused(db):
    from src.api.dashboard import FOSS_MAX_SEATS
    res = _mint(FOSS_MAX_SEATS + 1)
    assert [r["status"] for r in res[:FOSS_MAX_SEATS]] == [201] * FOSS_MAX_SEATS
    assert res[-1]["status"] == 409


# ── Pin 2: open posture caps distinct tenants ────────────────────────────────

def test_open_posture_admits_at_most_cap_tenants(db):
    from src.api.dashboard import FOSS_MAX_SEATS
    uids = [str(uuid.uuid4()) for _ in range(FOSS_MAX_SEATS)]
    for uid in uids:
        _provision(db, uid)
    assert _provisioned(db) == FOSS_MAX_SEATS
    _expect_refused(db, str(uuid.uuid4()))
    # Control: an already-admitted tenant is not a new admission and keeps working.
    _provision(db, uids[0])
    assert _provisioned(db) == FOSS_MAX_SEATS


# ── Pin 3: once seats exist, a novel non-seat user_id is refused ─────────────

def test_seats_in_use_refuse_novel_user_id(db):
    seats = _mint(1)
    assert seats[0]["status"] == 201
    _expect_refused(db, str(uuid.uuid4()))


def test_existing_seat_keeps_working_at_cap(db):
    """Control: every minted seat provisions, even with the cap full."""
    from src.api.dashboard import FOSS_MAX_SEATS
    seats = _mint(FOSS_MAX_SEATS)
    for s in seats:
        _provision(db, s["user_id"])
    assert _provisioned(db) == FOSS_MAX_SEATS
    _provision(db, seats[0]["user_id"])  # re-touch: still fine
    _expect_refused(db, str(uuid.uuid4()))


def test_revoked_seat_cannot_be_born(db):
    seats = _mint(2)
    client, hdr = _mint_client()
    assert client.delete(f"/api/dashboard/seats/{seats[1]['user_id']}", headers=hdr).status_code == 200
    _expect_refused(db, seats[1]["user_id"])


# ── Pin 4: the MCP front door ────────────────────────────────────────────────

@pytest.fixture()
def mcp(db, monkeypatch):
    from fastapi.testclient import TestClient
    from src.mcp import http_server
    monkeypatch.setattr(http_server, "MCP_API_KEY", "shared-pin-key")

    seen: list[str] = []

    async def _fake_gate(user_id):
        seen.append(user_id)
        return ""  # falsy → the door answers the provisioning envelope without a backend

    monkeypatch.setattr(http_server._mcp, "_ensure_provisioned", _fake_gate)
    return TestClient(http_server.app), seen


def test_shared_key_novel_user_id_refused_once_seats_exist(mcp):
    client, seen = mcp
    assert _mint(1)[0]["status"] == 201
    novel = str(uuid.uuid4())
    r = client.post("/recall_memory", json={"query": "what do I know", "user_id": novel},
                    headers={"Authorization": "Bearer shared-pin-key"})
    assert r.status_code == 403, r.text
    assert "mint a seat" in r.text
    assert novel not in seen
    rpc = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "recall_memory",
                                               "arguments": {"query": "x", "user_id": novel}}},
                      headers={"Authorization": "Bearer shared-pin-key"})
    assert rpc.status_code == 403, rpc.text
    assert "mint a seat" in rpc.text


def test_shared_key_seat_user_id_still_passes(mcp):
    """Control: the shared key acting for a real seat is not refused."""
    client, seen = mcp
    seat = _mint(1)[0]
    r = client.post("/recall_memory", json={"query": "what do I know", "user_id": seat["user_id"]},
                    headers={"Authorization": "Bearer shared-pin-key"})
    assert r.status_code == 200, r.text
    assert seen == [seat["user_id"]]


def test_seat_token_cannot_act_as_another_user_id(mcp):
    client, seen = mcp
    a, b = _mint(2)
    for body_uid, hdr_uid in ((b["user_id"], ""), (str(uuid.uuid4()), ""), ("", b["user_id"])):
        headers = {"Authorization": f"Bearer {a['token']}"}
        if hdr_uid:
            headers["X-OpenWebUI-User-Id"] = hdr_uid
        r = client.post("/recall_memory", json={"query": "q", "user_id": body_uid}, headers=headers)
        assert r.status_code == 200, r.text
    rpc = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "recall_memory",
                                               "arguments": {"query": "x", "user_id": b["user_id"]}}},
                      headers={"Authorization": f"Bearer {a['token']}"})
    assert rpc.status_code == 200
    assert seen and set(seen) == {a["user_id"]}, f"seat token acted as {set(seen) - {a['user_id']}}"


# ── Pin 5: the backend (:8000) maps the refusal to 403, never a 503 retry loop ─

@pytest.fixture()
def backend(db, monkeypatch):
    monkeypatch.setenv("PROVISIONING_TIMEOUT_SEC", "1")
    from src.api import main
    main._TENANT_READY_CACHE.clear()
    return main


def _fill_open_posture(conn) -> list[str]:
    from src.api.dashboard import FOSS_MAX_SEATS
    uids = [str(uuid.uuid4()) for _ in range(FOSS_MAX_SEATS)]
    for uid in uids:
        _provision(conn, uid)
    return uids


def _assert_403(exc_info, conn, before):
    assert getattr(exc_info.value, "status_code", None) == 403, repr(exc_info.value)
    assert "mint a seat" in str(exc_info.value.detail)
    assert _provisioned(conn) == before


@pytest.mark.parametrize("door", ["sync_guard", "async_guard", "provisioning_status"])
def test_backend_refuses_sixth_tenant_with_403(backend, db, door):
    import asyncio
    from fastapi import HTTPException
    _fill_open_posture(db)
    before = _provisioned(db)
    novel = str(uuid.uuid4())
    with pytest.raises(HTTPException) as ei:
        if door == "sync_guard":
            backend._ensure_tenant_ready_sync(novel, "/retract")
        elif door == "async_guard":
            asyncio.run(backend._ensure_tenant_ready(novel, "/query"))
        else:
            backend.provisioning_status_endpoint(user_id=novel)
    _assert_403(ei, db, before)


def test_backend_seat_user_id_is_admitted(backend, db):
    """Control: with seats minted and the open cap long since irrelevant, a seat provisions."""
    seat = _mint(1)[0]
    out = backend.provisioning_status_endpoint(user_id=seat["user_id"])
    assert out.get("status") in ("provisioning", "ready"), out


# ── Pin 6 (#119): once seats exist, an EXISTING tenant without a seat is refused ─

def _recall(client, uid, token="shared-pin-key"):
    return client.post("/recall_memory", json={"query": "what do I know", "user_id": uid},
                       headers={"Authorization": f"Bearer {token}"})


def _mint_for(uid: str) -> dict:
    client, hdr = _mint_client()
    r = client.post("/api/dashboard/seats", json={"user_id": uid}, headers=hdr)
    return {"status": r.status_code, **r.json()}


def test_revoke_then_403_then_remint_restores(mcp, db):
    client, seen = mcp
    keep, gone = _mint(2)
    _provision(db, gone["user_id"])
    assert _recall(client, gone["user_id"]).status_code == 200
    adm, hdr = _mint_client()
    assert adm.delete(f"/api/dashboard/seats/{gone['user_id']}", headers=hdr).status_code == 200
    r = _recall(client, gone["user_id"])
    assert r.status_code == 403 and "seat required" in r.text, r.text
    assert _recall(client, gone["user_id"], token=gone["token"]).status_code == 401  # old token dead
    assert _provisioned(db) == 1  # nothing deleted
    again = _mint_for(gone["user_id"])
    assert again["status"] == 201 and again["user_id"] == gone["user_id"], again
    assert _recall(client, gone["user_id"]).status_code == 200
    assert _recall(client, gone["user_id"], token=again["token"]).status_code == 200
    assert _recall(client, gone["user_id"], token=gone["token"]).status_code == 401
    assert _recall(client, keep["user_id"]).status_code == 200  # control


def test_pre_seat_tenant_needs_a_seat_once_seats_exist(mcp, db):
    client, seen = mcp
    legacy = str(uuid.uuid4())
    _provision(db, legacy)                      # open posture: admitted
    assert _recall(client, legacy).status_code == 200
    assert _mint(1)[0]["status"] == 201         # seats now in use
    r = _recall(client, legacy)
    assert r.status_code == 403 and "seat required" in r.text, r.text
    rpc = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "recall_memory",
                                               "arguments": {"query": "x", "user_id": legacy}}},
                      headers={"Authorization": "Bearer shared-pin-key"})
    assert rpc.status_code == 403 and "seat required" in rpc.text
    assert _mint_for(legacy)["status"] == 201
    assert _recall(client, legacy).status_code == 200


def test_remint_counts_against_cap_and_active_remint_conflicts(db):
    from src.api.dashboard import FOSS_MAX_SEATS
    seats = _mint(FOSS_MAX_SEATS)
    assert _mint_for(seats[0]["user_id"])["status"] == 409          # already active
    assert _mint_for(str(uuid.uuid4()))["status"] == 409            # cap full
    adm, hdr = _mint_client()
    adm.delete(f"/api/dashboard/seats/{seats[0]['user_id']}", headers=hdr)
    assert _mint_for(seats[0]["user_id"])["status"] == 201          # freed slot, same tenant
    assert _mint_for("not-a-uuid")["status"] == 400


# ── Pin 7 (#122): the MCP seat gate fails CLOSED on a seat-store error ───────

def test_mcp_seat_gate_503_when_store_unreachable(mcp, monkeypatch):
    client, seen = mcp
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://faultline:faultline@127.0.0.1:1/nope_test")
    r = _recall(client, str(uuid.uuid4()))
    assert r.status_code == 503 and "seat store unavailable" in r.text, r.text
    rpc = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "recall_memory",
                                               "arguments": {"query": "x", "user_id": str(uuid.uuid4())}}},
                      headers={"Authorization": "Bearer shared-pin-key"})
    assert rpc.status_code == 503
    monkeypatch.delenv("POSTGRES_DSN")
    assert _recall(client, str(uuid.uuid4())).status_code == 503
    assert not seen
