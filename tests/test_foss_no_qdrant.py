"""#219 / #218 pins: FOSS ships without Qdrant, and no user-memory path touches it by default.

Postgres is the system of record; USER_MEMORY_VECTOR_LANE defaults false and FOSS has no agent
cortex, so a Qdrant container held nothing and only cost disk, logs and a container. With the
defaults (lane off, QDRANT_URL unset) there must be no qdrant service, no Qdrant HTTP call on
ingest / query / retract / correct, and /health must read ok, not degraded.

The E2E section drives the real app on a throwaway ``*_test`` POSTGRES_DSN (skips without one,
disclosed) and records every outbound HTTP request.
"""
from __future__ import annotations

import ast
import os
import uuid
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = sorted(_ROOT.glob("docker-compose*.yml"))


# ── compose (#219) ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_qdrant_service_volume_or_dependency(path):
    d = yaml.safe_load(path.read_text())
    services = d["services"]
    for name, svc in services.items():
        assert "qdrant" not in str(svc.get("image", "")).lower(), f"{name} runs qdrant"
        deps = svc.get("depends_on") or {}
        assert "qdrant" not in (deps if isinstance(deps, (list, dict)) else []), name
        for net in (svc.get("networks") or []):
            assert "qdrant" not in str(net), f"{name} still needs a qdrant network"
    assert "qdrant" not in services
    assert not any("qdrant" in v for v in (d.get("volumes") or {})), "qdrant volume still declared"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_qdrant_url_defaults_empty(path):
    services = yaml.safe_load(path.read_text())["services"]
    for name, svc in services.items():
        v = (svc.get("environment") or {}).get("QDRANT_URL")
        if v is not None:
            assert str(v) in ("${QDRANT_URL:-}", ""), f"{name}: QDRANT_URL defaults to {v!r}"


def test_no_source_falls_back_to_a_guessed_qdrant_host():
    """AST walk: no string constant in shipped code names the old compose host as a default.
    An unset QDRANT_URL must mean 'no vector store', never http://qdrant:6333."""
    offenders = []
    for py in list((_ROOT / "src").rglob("*.py")) + list((_ROOT / "tools").rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.rstrip("/") == "http://qdrant:6333":
                offenders.append(f"{py.relative_to(_ROOT)}:{node.lineno}")
    assert not offenders, offenders


def test_configured_qdrant_url_unset_means_none(monkeypatch):
    from src.api import qdrant_partition as qp
    monkeypatch.delenv("QDRANT_URL", raising=False)
    assert qp.configured_qdrant_url() == "" and not qp.qdrant_configured()
    monkeypatch.setenv("QDRANT_URL", "  ")
    assert not qp.qdrant_configured()
    monkeypatch.setenv("QDRANT_URL", "http://vectors.example:6333/")
    assert qp.configured_qdrant_url() == "http://vectors.example:6333"


# ── /health (#219) ────────────────────────────────────────────────────────────

@pytest.fixture
def main_mod():
    import src.api.main as M
    return M


def _refresh(M, monkeypatch, qdrant_answer=None):
    calls = []
    monkeypatch.setattr(M, "_gliner2_model", object())
    monkeypatch.setattr(M, "_check_db_health", lambda dsn: True)
    monkeypatch.setattr(M, "_check_llm_health", lambda url: True)

    def _q(url):
        calls.append(url)
        return bool(qdrant_answer)
    monkeypatch.setattr(M, "_check_qdrant_health", _q)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://x@127.0.0.1:1/x_test")
    monkeypatch.setattr(M, "_health_cache", None)
    M._health_refresh_sync(bounded=True)
    return M._health_cache, calls


def test_health_ok_without_qdrant(main_mod, monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    h, calls = _refresh(main_mod, monkeypatch)
    assert calls == [], "no Qdrant probe when none is configured"
    assert h["qdrant"] == "not_configured"
    assert h["status"] == "ok", h


def test_health_control_configured_but_down_still_degrades(main_mod, monkeypatch):
    """Control: an operator who DID configure Qdrant still sees it fail."""
    monkeypatch.setenv("QDRANT_URL", "http://vectors.example:6333")
    h, calls = _refresh(main_mod, monkeypatch, qdrant_answer=False)
    assert calls and h["qdrant"] == "unreachable" and h["status"] == "degraded"


_U = ["11111111-1111-1111-1111-11111111111" + str(i) for i in range(6)]


def test_vector_lane_requires_both_flag_and_url(main_mod, monkeypatch):
    M = main_mod
    for lane, url, want in ((False, "", False), (False, "http://v:6333", False),
                            (True, "", False), (True, "http://v:6333", True)):
        monkeypatch.setattr(M, "USER_MEMORY_VECTOR_LANE", lane)
        if url:
            monkeypatch.setenv("QDRANT_URL", url)
        else:
            monkeypatch.delenv("QDRANT_URL", raising=False)
        assert M._vector_lane_active() is want, (lane, url)


def test_re_embedder_delete_point_is_noop_without_lane(monkeypatch):
    emb = pytest.importorskip("src.re_embedder.embedder")

    def _boom(*a, **k):
        raise AssertionError("qdrant delete with the lane off")
    monkeypatch.setattr(emb.httpx, "post", _boom)
    monkeypatch.setattr(emb, "_USER_MEMORY_VECTOR_LANE", False)
    assert emb._qdrant_delete_fact_point("http://v:6333", _U[0], "staged_facts", 1) is None
    monkeypatch.setattr(emb, "_USER_MEMORY_VECTOR_LANE", True)
    assert emb._qdrant_delete_fact_point("", _U[0], "staged_facts", 1) is None


def test_re_embedder_loop_gates_every_qdrant_maintenance_call():
    """Structural: inside main(), ensure_collection / reconcile_qdrant / upserts run only under
    the vector-lane gate. Read from the AST so a new ungated call is caught."""
    src = (_ROOT / "src/re_embedder/embedder.py").read_text()
    tree = ast.parse(src)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    lines = src.splitlines()
    ungated = []
    for node in ast.walk(main):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in (
                "ensure_collection", "reconcile_qdrant"):
            # walk enclosing source upward for the gate name within the statement's block
            window = "\n".join(lines[max(0, node.lineno - 60):node.lineno])
            if "_vector_lane_on" not in window:
                ungated.append(node.lineno)
    assert not ungated, ungated


# ── E2E: ingest / query / retract / correct with defaults make zero Qdrant calls ─────

_DSN = os.environ.get("POSTGRES_DSN", "")


@pytest.fixture(scope="module")
def e2e():
    if not _DSN or not _DSN.rsplit("/", 1)[-1].endswith("_test"):
        pytest.skip("E2E needs a throwaway *_test POSTGRES_DSN (skipped — disclosed)")
    import httpx
    mp = pytest.MonkeyPatch()
    mp.setenv("FAULTLINE_BACKEND_SECRET", "no-qdrant-secret")
    mp.setenv("FAULTLINE_MIGRATIONS_DIR", "./migrations")
    mp.delenv("QDRANT_URL", raising=False)
    mp.delenv("USER_MEMORY_VECTOR_LANE", raising=False)
    import psycopg2
    from src.provisioning.schema_manager import create_user_schema, derive_user_slug_from_uuid
    import src.api.main as M
    assert M.USER_MEMORY_VECTOR_LANE is False, "default must be lane off"

    seen = []
    _orig_sync, _orig_async = httpx.Client.send, httpx.AsyncClient.send

    def _rec_sync(self, request, *a, **k):
        seen.append(str(request.url))
        return _orig_sync(self, request, *a, **k)

    async def _rec_async(self, request, *a, **k):
        seen.append(str(request.url))
        return await _orig_async(self, request, *a, **k)
    mp.setattr(httpx.Client, "send", _rec_sync)
    mp.setattr(httpx.AsyncClient, "send", _rec_async)

    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    conn = psycopg2.connect(_DSN)
    schema, status = create_user_schema(user_id, slug, conn)
    assert status == "ready", status
    with conn.cursor() as cur:
        cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES "
                    "(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, f"{slug}@example.invalid", slug, slug))
        cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                    "VALUES (%s,%s,'ready',now()) ON CONFLICT DO NOTHING", (user_id, schema))
    conn.commit()
    conn.close()
    mp.setattr(M, "_INGEST_ENABLED", True)
    M._TENANT_READY_CACHE.add(user_id)

    class _D:
        def get(self, k):
            return None

        def setex(self, k, t, v):
            return True

        def ping(self):
            return True

    class _Idem:
        redis_url = "dict://test"
        ttl = 3600
        client = _D()
    mp.setattr(M, "_idempotency_mgr", _Idem())
    from starlette.testclient import TestClient
    with TestClient(M.app) as client:
        client.headers.update({"X-FaultLine-Backend-Secret": "no-qdrant-secret"})
        yield {"client": client, "user_id": user_id, "seen": seen, "M": M}
    mp.undo()
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    conn.close()


def _qdrant_like(url: str) -> bool:
    u = url.lower()
    return ":6333" in u or "//qdrant" in u or "/collections" in u


def test_e2e_user_memory_paths_make_zero_qdrant_calls(e2e):
    c, uid, seen = e2e["client"], e2e["user_id"], e2e["seen"]
    seen.clear()
    edges = [{"subject": "user", "object": "kayak", "rel_type": "owns", "is_preferred_label": False}]
    r = c.post("/ingest", json={"text": "I own a kayak.", "user_id": uid, "edges": edges,
                                "source": "mcp"})
    assert r.status_code == 200, r.text[:300]
    c.post("/store_context", json={"text": "Paddled the river today.", "user_id": uid})
    q = c.post("/query", json={"text": "what do I own?", "user_id": uid})
    assert q.status_code == 200, q.text[:300]
    c.post("/retract", json={"user_id": uid, "subject": "user", "rel_type": "owns",
                             "object": "kayak"})
    c.post("/retract/correct", json={"user_id": uid, "text": "Actually I never had a kayak."})
    h = c.get("/health").json()
    assert h.get("qdrant") == "not_configured"
    bad = [u for u in seen if _qdrant_like(u)]
    assert not bad, bad
