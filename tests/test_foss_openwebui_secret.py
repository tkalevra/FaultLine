"""#125: the shipped OpenWebUI modules send the backend service secret, and only to the backend.

OpenWebUI has no access to FaultLine's database, so it cannot read the auto-minted secret. The
modules take FAULTLINE_BACKEND_SECRET from a valve (default: the env var), attach it via an
httpx request hook to the FaultLine backend origin ONLY (never the LLM endpoint), and print a
clear error when the backend answers 401. The operator can copy the current secret from the
operator-gated GET /api/dashboard/backend-secret.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
HDR = "X-FaultLine-Backend-Secret"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"owui_{name}", ROOT / "openwebui" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive(hooks, url, status=200):
    seen = {}

    def handler(request):
        seen["hdr"] = request.headers.get(HDR)
        return httpx.Response(status, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), event_hooks=hooks) as c:
            await c.get(url)

    asyncio.run(go())
    return seen["hdr"]


def test_legacy_function_sends_secret_to_backend_only(monkeypatch):
    monkeypatch.delenv("FAULTLINE_BACKEND_SECRET", raising=False)
    mod = _load("faultline_mcp")
    fn = mod.Function()
    fn.valves.FAULTLINE_URL = "http://faultline:8000"
    fn.valves.FAULTLINE_BACKEND_SECRET = "owui-secret"
    hooks = fn._fl_backend_client()["event_hooks"]
    assert _drive(hooks, "http://faultline:8000/user/x/recent-facts") == "owui-secret"
    assert _drive(hooks, "http://open-webui:8080/api/chat/completions") is None


def test_filter_sends_secret_to_resolved_backend_only(monkeypatch):
    monkeypatch.setenv("FAULTLINE_BACKEND_SECRET", "env-secret")
    mod = _load("faultline_function")
    url = mod._get_faultline_url("http://faultline:8000")
    assert _drive(mod._FL_ASYNC_HOOKS, f"{url}/query") == "env-secret"
    assert _drive(mod._FL_ASYNC_HOOKS, "http://open-webui:8080/api/chat/completions") is None
    assert mod._fl_headers_for(url) == {HDR: "env-secret"}
    assert mod._fl_headers_for("http://open-webui:8080") == {}
    assert "FAULTLINE_BACKEND_SECRET" in mod.Filter.Valves.model_fields


def test_backend_401_prints_actionable_error(monkeypatch, capsys):
    monkeypatch.delenv("FAULTLINE_BACKEND_SECRET", raising=False)
    mod = _load("faultline_mcp")
    fn = mod.Function()
    fn.valves.FAULTLINE_BACKEND_SECRET = ""
    _drive(fn._fl_backend_client()["event_hooks"], "http://faultline:8000/ingest", status=401)
    err = capsys.readouterr().err
    assert "401" in err and "FAULTLINE_BACKEND_SECRET" in err


def test_docs_say_owui_needs_the_secret_set_explicitly():
    for f in ("README.md", ".env.example"):
        text = (ROOT / f).read_text()
        assert "OpenWebUI" in text and "FAULTLINE_BACKEND_SECRET" in text
        assert "cannot read" in text, f


# ── operator-gated reveal ────────────────────────────────────────────────────

@pytest.fixture()
def dash(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api import dashboard
    dashboard._rate_buckets.clear()
    monkeypatch.setenv("FAULTLINE_ADMIN_TOKEN", "op-125")
    monkeypatch.setenv("FAULTLINE_BACKEND_SECRET", "revealed-125")
    logged = []
    monkeypatch.setattr(dashboard, "_log_action", lambda *a, **k: logged.append((a, k)))
    app = FastAPI()
    app.include_router(dashboard.router)
    return TestClient(app), logged


def test_reveal_requires_operator(dash):
    c, logged = dash
    assert c.get("/api/dashboard/backend-secret").status_code == 401
    assert c.get("/api/dashboard/backend-secret", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert not logged


def test_reveal_returns_secret_and_never_logs_it(dash, capsys, caplog):
    c, logged = dash
    r = c.get("/api/dashboard/backend-secret", headers={"Authorization": "Bearer op-125"})
    assert r.status_code == 200 and r.json()["secret"] == "revealed-125"
    assert r.headers.get("cache-control") == "no-store"
    assert logged and "revealed-125" not in repr(logged)
    out = capsys.readouterr()
    assert "revealed-125" not in out.out + out.err + caplog.text
