"""#118: every operator route is gated by the ONE operator credential, and the gate can see it.

Before: ``POST /admin/logging/level`` and ``POST /internal/doc-lane/control`` on the backend
(:8000) answered with no credential at all, ``/admin/cache/clear-embeddings`` took a second
operator key (``ADMIN_API_KEY``) from the QUERY STRING with a non-constant-time compare, and
the dashboard router was not even mounted (no seat could be minted on the FOSS line).

Now: every ``/admin/*`` and operator ``/internal/*`` route depends on
``dashboard.require_operator`` (``FAULTLINE_ADMIN_TOKEN`` bearer, fail-closed when unset);
the two service-to-service ``/internal`` routes (MCP / re-embedder) depend on
``backend_auth.require_service``. The release-gate AST walk below covers EVERY FastAPI
route in ``src/``, not only ``dashboard.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
_GATES = {"require_operator", "require_service"}
_METHODS = {"get", "post", "put", "delete", "patch", "api_route", "head", "options"}


def _routes():
    """Yield (file, func, path, decorator-owner, gate-names) for every FastAPI route in src/."""
    for f in sorted(SRC.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in node.decorator_list:
                if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr in _METHODS and d.args
                        and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str)):
                    continue
                owner = ast.unparse(d.func.value)
                gates = set()
                for dflt in node.args.defaults + [x for x in node.args.kw_defaults if x is not None]:
                    if isinstance(dflt, ast.Call) and getattr(dflt.func, "id", None) == "Depends" and dflt.args:
                        gates.add(ast.unparse(dflt.args[0]).split(".")[-1])
                for kw in d.keywords:
                    if kw.arg == "dependencies":
                        gates |= {g for g in _GATES if g in ast.unparse(kw.value)}
                yield f.relative_to(SRC.parent), node.name, d.args[0].value, owner, gates


def test_every_admin_and_internal_route_is_gated():
    seen, ungated = [], []
    for f, fn, path, owner, gates in _routes():
        if path.startswith(("/admin", "/internal")):
            seen.append(path)
            if not gates & _GATES:
                ungated.append(f"{f}::{fn} {path}")
    assert seen, "AST walk found no /admin or /internal routes — the walk itself is broken"
    assert not ungated, "ungated operator routes:\n  " + "\n  ".join(ungated)


def test_every_dashboard_route_requires_operator():
    rows = [(fn, path, gates) for f, fn, path, owner, gates in _routes() if str(f).endswith("api/dashboard.py")]
    assert len(rows) >= 11
    assert all("require_operator" in g for _, _, g in rows), [r for r in rows if "require_operator" not in r[2]]


def test_no_second_operator_credential():
    main = (SRC / "api" / "main.py").read_text(encoding="utf-8")
    assert "ADMIN_API_KEY" not in main


# ── Runtime: the real backend app (no lifespan) ──────────────────────────────

@pytest.fixture(scope="module")
def client():
    mp = pytest.MonkeyPatch()
    mp.setenv("FAULTLINE_ADMIN_TOKEN", "op-token-118")
    mp.setenv("ADMIN_API_KEY", "legacy-key")
    mp.delenv("FAULTLINE_BACKEND_SECRET", raising=False)
    from fastapi.testclient import TestClient
    from src.api import main
    yield TestClient(main.app), mp
    mp.undo()


OP = {"Authorization": "Bearer op-token-118"}


@pytest.mark.parametrize("method,path,kw", [
    ("post", "/admin/logging/level?level=INFO", {}),
    ("get", "/admin/logging/level", {}),
    ("post", "/admin/cache/clear-embeddings", {}),
    ("get", "/internal/doc-lane/control", {}),
    ("post", "/internal/doc-lane/control", {"json": {"paused": False}}),
    ("get", "/internal/intent-layer-stats", {}),
])
def test_operator_route_refuses_without_bearer_and_accepts_with(client, method, path, kw):
    c, _ = client
    assert getattr(c, method)(path, **kw).status_code == 401
    assert getattr(c, method)(path, headers={"Authorization": "Bearer wrong"}, **kw).status_code == 401
    assert getattr(c, method)(path, headers=OP, **kw).status_code != 401


def test_legacy_query_string_key_no_longer_works(client):
    c, _ = client
    assert c.post("/admin/cache/clear-embeddings?api_key=legacy-key").status_code == 401


def test_fail_closed_when_operator_token_unset(client):
    c, mp = client
    mp.delenv("FAULTLINE_ADMIN_TOKEN")
    try:
        assert c.post("/admin/logging/level?level=INFO", headers=OP).status_code == 401
    finally:
        mp.setenv("FAULTLINE_ADMIN_TOKEN", "op-token-118")


def test_dashboard_is_mounted(client):
    c, _ = client
    assert c.get("/api/dashboard/config").status_code == 401  # mounted AND gated (was 404)


def test_service_routes_need_the_service_secret(client):
    """#121: /internal/ingest-route answers the MCP (secret) and refuses everyone else."""
    c, mp = client
    mp.setenv("FAULTLINE_BACKEND_SECRET", "svc-118")
    try:
        assert c.get("/internal/ingest-route").status_code == 401
        assert c.get("/internal/ingest-route", headers={"X-FaultLine-Backend-Secret": "svc-118"}).status_code == 200
    finally:
        mp.delenv("FAULTLINE_BACKEND_SECRET")
