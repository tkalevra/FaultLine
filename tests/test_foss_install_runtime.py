"""FOSS fresh-install runtime pins (found by the 2026-10-02 fresh-install validation).

Each test drives the real backend app through TestClient (no lifespan, no database).
"""
from __future__ import annotations

import time

import pytest

OP = {"Authorization": "Bearer op-token-install"}


@pytest.fixture(scope="module")
def client():
    mp = pytest.MonkeyPatch()
    mp.setenv("FAULTLINE_ADMIN_TOKEN", "op-token-install")
    from fastapi.testclient import TestClient
    from src.api import main
    yield TestClient(main.app, raise_server_exceptions=False), main, mp
    mp.undo()


# ── #146: GET /api/dashboard/health 500'd on an un-awaited coroutine ────────

def test_dashboard_health_returns_the_cached_health(client):
    c, main, mp = client
    mp.setattr(main, "_health_cache", {
        "status": "ok", "database": "ok", "qdrant": "ok", "llm": "ok",
        "re_embedder": {"ok": True}, "llm_config": {"backend_type": "ollama"},
    })
    mp.setattr(main, "_health_cache_ts", time.time())
    r = c.get("/api/dashboard/health", headers=OP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["database"] == "ok" and body["qdrant"] == "ok" and body["llm"] == "ok"


def test_dashboard_health_still_requires_the_operator(client):
    c, _, _ = client
    assert c.get("/api/dashboard/health").status_code == 401
