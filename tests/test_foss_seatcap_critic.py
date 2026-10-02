"""Critic pins for foss-seatcap (fc0453a2). Every test here is RED on fc0453a2.

1. Revocation is not durable: revoking the LAST active seat flips seat_refusal back to the
   open posture, which admits every EXISTING tenant -- so revoked tenants are live again via
   the shared key / anonymous dev posture, and live tenants can exceed FOSS_MAX_SEATS.
2. wait_for_schema_ready's fallback admission raises SeatLimitError inside its own broad
   ``except Exception`` poll loop: the refusal is swallowed, the loop polls to timeout, and the
   caller answers 503 "provisioning" (a retry signal) instead of 403.
3. backend_auth.caller_is_trusted treats the container's own address as a credential. Under
   rootless podman (pasta AND bridge/rootlessport, measured on Fedora 44) a HOST process that
   connects to the published 127.0.0.1:8000 arrives with source == the container's own IP,
   and on bare metal / host networking every local process is 127.0.0.1. So
   FAULTLINE_BACKEND_SECRET is bypassed by any host-local process.
4. The MCP #119 gate fails OPEN: any DB error (or no POSTGRES_DSN, as in
   docker-compose-portainer-withoutqdrant.yml) returns "no refusal", and the backend only gates
   tenant BIRTH, so a seatless existing tenant is reachable with the shared key.

Needs fl-test-pg (127.0.0.1:55432); own throwaway database (name ends in _test).
"""
from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from pathlib import Path

import psycopg2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_ADMIN = os.environ.get("FL_SEATCAP_ADMIN_DSN", "postgresql://faultline:faultline@127.0.0.1:55432/faultline")
_DBNAME = "seatcap_critic_test"


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
        cur.execute(f"DROP DATABASE IF EXISTS {_DBNAME} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {_DBNAME}")
    import subprocess
    import sys
    env = {**os.environ, "POSTGRES_DSN": _dsn(), "FAULTLINE_MIGRATIONS_DIR": "./migrations"}
    subprocess.run([sys.executable, "-m", "src.provisioning.boot_migrations"],
                   env=env, check=True, capture_output=True)
    mp = pytest.MonkeyPatch()
    mp.setenv("POSTGRES_DSN", _dsn())
    mp.setenv("FAULTLINE_ADMIN_TOKEN", "operator-critic-token")
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


def _provision(conn, uid: str) -> None:
    from src.provisioning.provisioning_status import ensure_user_provisioned
    from src.provisioning.schema_manager import derive_user_slug_from_uuid
    ensure_user_provisioned(uid, derive_user_slug_from_uuid(uid), conn)


def _admin():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api import dashboard
    dashboard._rate_buckets.clear()
    app = FastAPI()
    app.include_router(dashboard.router)
    return TestClient(app), {"Authorization": "Bearer operator-critic-token"}


def _mint(body=None) -> dict:
    c, h = _admin()
    r = c.post("/api/dashboard/seats", json=body or {}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _revoke(uid: str) -> None:
    c, h = _admin()
    assert c.delete(f"/api/dashboard/seats/{uid}", headers=h).status_code == 200


def _admitted(conn, uid: str) -> bool:
    from src.provisioning.provisioning_status import seat_refusal
    with conn.cursor() as cur:
        ok = seat_refusal(cur, uid) is None
    conn.commit()
    return ok


def _tenants(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT user_id::text FROM public.user_provisioning")
        rows = [r[0] for r in cur.fetchall()]
    conn.commit()
    return rows


# ── 1. revocation durability / live-tenant cap ───────────────────────────────

def test_revoking_every_seat_does_not_reopen_revoked_tenants(db):
    seats = [_mint({"label": f"s{i}"}) for i in range(2)]
    for s in seats:
        _provision(db, s["user_id"])
    _revoke(seats[0]["user_id"])
    assert not _admitted(db, seats[0]["user_id"])          # control: refused while a seat remains
    _revoke(seats[1]["user_id"])                            # operator revokes the last seat
    reopened = [s["user_id"][:8] for s in seats if _admitted(db, s["user_id"])]
    assert not reopened, f"revoked tenants admitted again after the last revoke: {reopened}"


def test_live_tenants_never_exceed_cap_after_mint_revoke_cycle(db):
    from src.api.dashboard import FOSS_MAX_SEATS
    for _ in range(FOSS_MAX_SEATS):                         # open posture fills the cap
        _provision(db, str(uuid.uuid4()))
    for _ in range(2):                                      # mint -> use -> revoke, twice
        s = _mint()
        _provision(db, s["user_id"])
        _revoke(s["user_id"])
    live = [u for u in _tenants(db) if _admitted(db, u)]
    assert len(live) <= FOSS_MAX_SEATS, (
        f"{len(live)} live tenants (> FOSS_MAX_SEATS={FOSS_MAX_SEATS}) after revoking every seat")


# ── 2. wait_for_schema_ready swallows the refusal ───────────────────────────

def test_wait_for_schema_ready_fallback_refusal_is_raised_not_polled(db, monkeypatch):
    from src.api.dashboard import FOSS_MAX_SEATS
    from src.provisioning.provisioning_status import SeatLimitError
    from src.provisioning.schema_manager import derive_user_slug_from_uuid
    monkeypatch.delenv("PROVISIONING_TIMEOUT_SEC", raising=False)
    from src.api import main
    for _ in range(FOSS_MAX_SEATS):
        _provision(db, str(uuid.uuid4()))
    novel = str(uuid.uuid4())
    t0 = time.monotonic()
    outcome = None
    try:
        outcome = asyncio.run(main.wait_for_schema_ready(
            user_id=novel, user_slug=derive_user_slug_from_uuid(novel), db=db, timeout_sec=3))
    except SeatLimitError:
        outcome = "refused"
    took = time.monotonic() - t0
    assert outcome == "refused", (
        f"seat-cap refusal swallowed by the poll loop: returned {outcome!r} after {took:.1f}s "
        "(caller maps False to 503 'provisioning')")
    assert novel not in _tenants(db)


# ── 3. an IP address is not a credential ────────────────────────────────────

def _own_non_loopback():
    try:
        addrs = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        addrs = []
    return [a for a in addrs if not a.startswith("127.")] or ["10.88.0.2"]


def test_container_own_address_is_not_a_backend_credential(monkeypatch):
    """Rootless podman presents a host process as the container's own IP (measured:
    pasta -> 10.20.30.40 == container addr; bridge -> 10.88.0.2 == container addr)."""
    from src.api import backend_auth
    monkeypatch.setenv("FAULTLINE_BACKEND_SECRET", "critic-secret")
    backend_auth._own_addresses.cache_clear()
    own = _own_non_loopback()[0]
    monkeypatch.setattr(backend_auth, "_own_addresses", lambda: frozenset({"127.0.0.1", "::1", own}))
    assert not backend_auth.caller_is_trusted("/ingest", own, {}), (
        f"no-secret request from {own} (the container's own address) trusted")
    assert backend_auth.caller_is_trusted("/ingest", own, {"X-FaultLine-Backend-Secret": "critic-secret"})


def test_loopback_is_not_a_backend_credential(monkeypatch):
    """Bare-metal uvicorn / network_mode: host -> every local process is 127.0.0.1."""
    from src.api import backend_auth
    monkeypatch.setenv("FAULTLINE_BACKEND_SECRET", "critic-secret")
    assert not backend_auth.caller_is_trusted("/query", "127.0.0.1", {})


# ── 4. MCP #119 gate fails open ──────────────────────────────────────────────

def test_mcp_seat_gate_fails_closed_when_seat_store_unreachable(monkeypatch):
    from src.mcp import http_server
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://faultline:faultline@127.0.0.1:1/nope_test")
    refusal = http_server._seat_cap_refusal(str(uuid.uuid4()), "shared")
    assert refusal, "shared key admitted for an unverifiable user_id when the seat store is unreachable"


def test_every_compose_mcp_service_can_reach_the_seat_store():
    bad = []
    for f in sorted(ROOT.glob("docker-compose*.yml")):
        doc = yaml.safe_load(f.read_text())
        for name, svc in (doc.get("services") or {}).items():
            ep = " ".join(svc.get("entrypoint") or []) if isinstance(svc.get("entrypoint"), list) else str(svc.get("entrypoint") or "")
            if "mcp_server.py" in ep and "POSTGRES_DSN" not in (svc.get("environment") or {}):
                bad.append(f"{f.name}:{name}")
    assert not bad, f"MCP service without POSTGRES_DSN (seat tokens + #119 gate silently off): {bad}"


# ── 5. round 3: the always-on backend secret strands the shipped OpenWebUI modules ─

def test_shipped_openwebui_modules_that_call_the_backend_send_the_service_secret():
    """README still offers openwebui/faultline_function.py ('Legacy alternative ... still exists
    for automatic injection'). With the secret always on (6845e4f3), any module that calls the
    backend :8000 directly without X-FaultLine-Backend-Secret gets 401 on every call."""
    bad = []
    for f in sorted((ROOT / "openwebui").glob("*.py")):
        src = f.read_text()
        if "faultline:8000" in src and "X-FaultLine-Backend-Secret" not in src:
            bad.append(f.name)
    assert not bad, f"shipped OpenWebUI modules call :8000 without the backend secret (401 on every call): {bad}"
