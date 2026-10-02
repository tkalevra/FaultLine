"""#147: rotating the MCP key in the console supersedes the env MCP_API_KEY.

Before: ``_resolve_principal`` always accepted the env ``MCP_API_KEY`` as step 3, so after a
console rotation (done because the key leaked) the wizard-written ``.env`` key still answered
200, while the console promised "the old key stops working immediately". The OpenWebUI tab
also reported ``api_key_set: false`` because compose gave ``MCP_API_KEY`` only to the MCP
service.

Now: once a rotated key is active, the env key is refused; the console reports which key is
in force (``api_key_source``) and compose passes the env key to the backend so it can tell.

Needs the fl-test-pg throwaway postgres (127.0.0.1:55432); the database name ends in ``_test``.
"""
from __future__ import annotations

import os
import subprocess
import sys

import psycopg2
import pytest

_ADMIN = os.environ.get("FL_SEATCAP_ADMIN_DSN", "postgresql://faultline:faultline@127.0.0.1:55432/faultline")
_DBNAME = "mcpkeyrot_pin_test"
OP = {"Authorization": "Bearer operator-rot-token"}
ENV_KEY = "env-key-from-dotenv"


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
    mp.setenv("FAULTLINE_ADMIN_TOKEN", "operator-rot-token")
    mp.setenv("MCP_API_KEY", ENV_KEY)  # compose now passes it to the backend too
    yield _dsn()
    mp.undo()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {_DBNAME} WITH (FORCE)")
    admin.close()


@pytest.fixture()
def clients(migrated_db, monkeypatch):
    with psycopg2.connect(migrated_db) as db, db.cursor() as cur:
        cur.execute("TRUNCATE public.dashboard_mcp_keys, public.dashboard_seats, "
                    "public.user_provisioning, public.users CASCADE")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api import dashboard
    from src.mcp import http_server
    monkeypatch.setattr(http_server, "MCP_API_KEY", ENV_KEY)

    async def _fake_gate(user_id):
        return ""

    monkeypatch.setattr(http_server._mcp, "_ensure_provisioned", _fake_gate)
    dashboard._rate_buckets.clear()
    console = FastAPI()
    console.include_router(dashboard.router)
    return TestClient(http_server.app), TestClient(console)


def _recall(mcp, key):
    return mcp.post("/recall_memory", json={"query": "what do I know",
                                            "user_id": "11111111-2222-4333-8444-555555555555"},
                    headers={"Authorization": f"Bearer {key}"})


def test_rotation_revokes_the_env_key(clients):
    mcp, console = clients
    assert _recall(mcp, ENV_KEY).status_code == 200
    r = console.post("/api/dashboard/openwebui/rotate-key", headers=OP)
    assert r.status_code == 200
    new_key = r.json()["api_key"]
    assert _recall(mcp, new_key).status_code == 200
    assert _recall(mcp, ENV_KEY).status_code == 401, "the leaked .env key must stop working"


def test_console_reports_which_key_is_in_force(clients):
    _, console = clients
    before = console.get("/api/dashboard/openwebui", headers=OP).json()
    assert before["api_key_set"] is True and before["api_key_source"] == "env"
    assert before["env_key_superseded"] is False
    console.post("/api/dashboard/openwebui/rotate-key", headers=OP)
    after = console.get("/api/dashboard/openwebui", headers=OP).json()
    assert after["api_key_source"] == "rotated" and after["env_key_superseded"] is True


def test_compose_passes_mcp_key_to_the_backend():
    import yaml
    from pathlib import Path
    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text())
    env = compose["services"]["faultline"].get("environment") or {}
    assert "MCP_API_KEY" in env, "the console reads MCP_API_KEY on the backend to report the key state"
