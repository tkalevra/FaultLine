"""#117: the backend (:8000) is not reachable unauthenticated from the network.

Before: docker-compose published ``"8000:8000"`` on every interface, and the tenant endpoints
trust ``body.user_id``. Anyone on the LAN could read and write any tenant, bypassing the MCP
seat tokens entirely.

Now:
1. Every compose file binds the backend port to 127.0.0.1 (the console and health check still
   work on the host; MCP :8002 is the network front door).
2. Defence in depth: when ``FAULTLINE_BACKEND_SECRET`` is set, the backend refuses API calls
   without ``X-FaultLine-Backend-Secret`` (exempt: /health, /api/dashboard/*, console files,
   the operator bearer, in-container callers), and the MCP server sends that header on every
   backend call (never on the user-URL fetch). Unset = dev posture, unchanged.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SECRET = "backend-secret-117"
HDR = "X-FaultLine-Backend-Secret"


def _backend_port_bindings():
    for f in sorted(ROOT.glob("docker-compose*.yml")):
        doc = yaml.safe_load(f.read_text())
        for name, svc in (doc.get("services") or {}).items():
            for p in svc.get("ports") or []:
                p = str(p)
                if p.split(":")[-1].split("/")[0] == "8000":
                    yield f.name, name, p


def test_backend_port_bound_to_loopback_in_every_compose_file():
    rows = list(_backend_port_bindings())
    assert rows, "no backend :8000 port mapping found — the walk is broken"
    bad = [r for r in rows if not r[2].startswith("127.0.0.1:")]
    assert not bad, f"backend :8000 published beyond loopback: {bad}"


def test_both_services_receive_the_backend_secret():
    doc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for svc in ("faultline", "faultline-mcp"):
        env = doc["services"][svc].get("environment") or {}
        assert "FAULTLINE_BACKEND_SECRET" in env, svc


@pytest.fixture(scope="module")
def main_app():
    from src.api import main
    return main


@pytest.fixture()
def client(main_app, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("FAULTLINE_ADMIN_TOKEN", "op-117")
    return TestClient(main_app.app), monkeypatch


def _not_secret_refusal(r):
    return not (r.status_code == 401 and "backend service credential" in r.text)


def test_secret_set_refuses_tenant_endpoint_without_header(client):
    c, mp = client
    mp.setenv("FAULTLINE_BACKEND_SECRET", SECRET)
    body = {"text": "what do you know", "user_id": "11111111-2222-4333-8444-555555555555"}
    r = c.post("/query", json=body)
    assert r.status_code == 401 and "backend service credential" in r.text
    assert c.post("/query", json=body, headers={HDR: "wrong"}).status_code == 401
    assert c.get("/internal/ingest-route").status_code == 401
    assert c.get("/internal/ingest-route", headers={HDR: SECRET}).status_code == 200


def test_secret_set_exemptions(client):
    c, mp = client
    mp.setenv("FAULTLINE_BACKEND_SECRET", SECRET)
    assert _not_secret_refusal(c.get("/health"))
    assert _not_secret_refusal(c.get("/api/dashboard/config"))      # its own operator auth
    assert c.get("/").status_code == 200                             # console index
    assert c.get("/app.js").status_code == 200                       # console asset
    r = c.get("/admin/logging/level", headers={"Authorization": "Bearer op-117"})
    assert r.status_code == 200                                      # operator bearer passes


def test_no_address_is_a_credential(monkeypatch):
    """#121: neither loopback nor any other source address authenticates."""
    from src.api import backend_auth
    monkeypatch.setenv("FAULTLINE_BACKEND_SECRET", SECRET)
    for host in ("127.0.0.1", "::1", "10.88.0.2", "203.0.113.9", None):
        assert not backend_auth.caller_is_trusted("/query", host, {})
        assert backend_auth.caller_is_trusted("/query", host, {HDR: SECRET})


def test_fails_closed_when_no_secret_resolvable(client):
    """No env secret and no reachable secret store: API requests are refused, not opened."""
    c, mp = client
    mp.delenv("FAULTLINE_BACKEND_SECRET", raising=False)
    mp.delenv("POSTGRES_DSN", raising=False)
    from src.api import backend_auth
    backend_auth._reset_cache_for_tests()
    assert c.get("/internal/ingest-route").status_code == 401
    assert c.post("/query", json={"text": "x", "user_id": "11111111-2222-4333-8444-555555555555"}).status_code == 401


@pytest.mark.parametrize("hdr", [
    {HDR: "caf\u00e9-secret"},
    {"Authorization": "Bearer t\u00f6ken"},
])
def test_non_ascii_credential_is_401_not_500(client, hdr):
    c, mp = client
    mp.setenv("FAULTLINE_BACKEND_SECRET", SECRET)
    r = c.get("/internal/ingest-route", headers={k: v.encode("latin-1") for k, v in hdr.items()})
    assert r.status_code == 401, r.status_code
    r = c.get("/api/dashboard/config", headers={"Authorization": "Bearer t\u00f6ken".encode("latin-1")})
    assert r.status_code == 401, r.status_code


def test_mcp_sends_the_secret_to_the_backend_only(monkeypatch):
    import httpx
    from src.mcp import server
    monkeypatch.setenv("FAULTLINE_BACKEND_SECRET", SECRET)
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get(HDR)))
        return httpx.Response(200, json={})

    real = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr(server, "_http_client", None)
    monkeypatch.setattr(server, "_lazy_http_client", None, raising=False)
    asyncio.run(server._post(f"{server.FAULTLINE_API_URL}/ingest", json={}))
    assert seen and seen[-1][1] == SECRET
    monkeypatch.delenv("FAULTLINE_BACKEND_SECRET")
    assert server._backend_headers() == {}


# ── #121: zero-config auto-minted secret, shared through the DB ─────────────

@pytest.fixture(scope="module")
def secret_db():
    import os
    import subprocess
    import sys
    import psycopg2
    admin_dsn = os.environ.get("FL_SEATCAP_ADMIN_DSN", "postgresql://faultline:faultline@127.0.0.1:55432/faultline")
    name = "backend_secret_pin_test"
    try:
        admin = psycopg2.connect(admin_dsn, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"throwaway postgres unavailable: {exc}")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {name}")
    dsn = admin_dsn.rsplit("/", 1)[0] + "/" + name
    subprocess.run([sys.executable, "-m", "src.provisioning.boot_migrations"],
                   env={**os.environ, "POSTGRES_DSN": dsn, "FAULTLINE_MIGRATIONS_DIR": "./migrations"},
                   check=True, capture_output=True)
    yield dsn
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    admin.close()


def test_zero_config_auto_mints_and_shares_the_secret(secret_db, client):
    import psycopg2
    c, mp = client
    mp.delenv("FAULTLINE_BACKEND_SECRET", raising=False)
    mp.setenv("POSTGRES_DSN", secret_db)
    from src.api import backend_auth
    backend_auth._reset_cache_for_tests()
    assert backend_auth.ensure_backend_secret()          # backend boot
    with psycopg2.connect(secret_db) as db, db.cursor() as cur:
        cur.execute("SELECT secret FROM public.backend_service_secret")
        stored = cur.fetchone()[0]
    assert len(stored) >= 32
    backend_auth._reset_cache_for_tests()                # a different process (MCP / re-embedder)
    assert backend_auth.backend_headers() == {HDR: stored}
    assert backend_auth.ensure_backend_secret()          # second boot keeps the same value
    with psycopg2.connect(secret_db) as db, db.cursor() as cur:
        cur.execute("SELECT count(*), max(secret) FROM public.backend_service_secret")
        assert cur.fetchone() == (1, stored)
    assert c.get("/internal/ingest-route").status_code == 401
    assert c.get("/internal/ingest-route", headers={HDR: stored}).status_code == 200


def test_secret_value_is_never_logged(secret_db, monkeypatch, capsys, caplog):
    monkeypatch.delenv("FAULTLINE_BACKEND_SECRET", raising=False)
    monkeypatch.setenv("POSTGRES_DSN", secret_db)
    from src.api import backend_auth
    backend_auth._reset_cache_for_tests()
    backend_auth.ensure_backend_secret()
    s = backend_auth.backend_secret()
    out = capsys.readouterr()
    assert s and s not in out.out and s not in out.err and s not in caplog.text


def _call_args(text, start):
    """The argument text of the call whose "(" is at ``start`` (balanced parens, comments skipped)."""
    depth, i, out = 0, start, []
    while i < len(text):
        ch = text[i]
        if ch == "#":
            i = text.index("\n", i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return "".join(out)
        out.append(ch)
        i += 1
    return "".join(out)


def test_in_container_callers_send_the_secret():
    """Every backend call in the re-embedder / supervisor carries the service secret."""
    import re
    missing, n = [], 0
    for f in ("embedder.py", "supervisor.py"):
        text = (ROOT / "src" / "re_embedder" / f).read_text()
        for m in re.finditer(r"httpx\.(?:post|get)\(", text):
            args = _call_args(text, m.end() - 1)
            hit = re.search(r"f?\"(?:\{backend_url\}|\{backend_api_url\}|http://faultline:8000)(/[\w/-]+)", args)
            if not hit or hit.group(1) == "/health":
                continue
            n += 1
            if "backend_headers()" not in args and "_backend_auth_headers()" not in args:
                missing.append(f"{f}:{hit.group(1)}")
    assert n >= 12, n
    assert not missing, missing
