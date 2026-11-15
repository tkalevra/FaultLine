"""Bench results dashboard is OPERATOR-GATED (src/mcp/http_server.py).

The benchmark results (LongMemEval feed + the fltest ledger) are published as JSON into
<dashboard>/data/*.json — a named volume mounted INSIDE the StaticFiles dashboard dir. Left on
the raw static mount they were world-readable at /dashboard/data/*.json, i.e. publicly snoopable.

The fix gates the READ path behind the SAME operator credential the server-stats card uses
(saas.admin.require_operator): a dedicated GET /dashboard/data/{fname} route is registered BEFORE
the StaticFiles mount, so it takes precedence and 403s any non-operator caller. This proves:

  * no credential                -> 403 (was: 200 + full results)
  * break-glass operator KEY     -> 200 (require_operator accepts the static op key)
  * unknown filename             -> 404 (whitelist: lme_results.json | test_runs.json)

SAAS_MODE (which decides the import-time dashboard mount) is the suite-wide baseline set once in
tests/conftest.py. This module's own operator key is bound PER TEST below — it must never be
written into ``os.environ`` at import, because that is process-wide and four sibling modules each
want a different key. tests/ is gitignored → add with ``git add -f``.

Run: python3 tools/fltest.py --bug BENCH-ADMIN-GATE --test tests/test_saas_bench_dashboard_gate.py
"""

import pytest
from fastapi.testclient import TestClient

import src.mcp.http_server as h

OP_KEY = "bench-gate-op-super-secret-key"


@pytest.fixture(autouse=True)
def _own_operator_key(monkeypatch):
    """Bind THIS module's operator key for the duration of ONE test, then restore.

    ``saas/admin.py`` reads SAAS_OPERATOR_KEY LIVE per request, so the key only has to be right
    while the test runs — function-scoped monkeypatch is exactly the right lifetime, and it means
    collection order can no longer decide the result.
    """
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)



@pytest.fixture
def client():
    # No context manager → lifespan (DB / shared http client) is not run; these are
    # routing/auth-only assertions. No host header → "internal" face → no surface gating.
    return TestClient(h.app, follow_redirects=False)


def _op_hdr():
    return {"Authorization": f"Bearer {OP_KEY}"}


def test_bench_data_requires_operator_credential(client):
    """Anonymous read of the published results must be denied (was public)."""
    for name in ("test_runs.json", "lme_results.json"):
        r = client.get(f"/dashboard/data/{name}")
        assert r.status_code == 403, (name, r.status_code, r.text[:200])


def test_bench_data_served_to_operator(client):
    """The break-glass operator KEY (same guard as /admin/server-stats) is admitted, and
    the endpoint returns JSON — an empty, page-renderable payload when nothing is published."""
    r = client.get("/dashboard/data/test_runs.json", headers=_op_hdr())
    assert r.status_code == 200, (r.status_code, r.text[:200])
    body = r.json()
    # test_runs.json shape is {"runs": [...]}; missing file → {"runs": []}.
    assert isinstance(body, dict) and "runs" in body

    r2 = client.get("/dashboard/data/lme_results.json", headers=_op_hdr())
    assert r2.status_code == 200, (r2.status_code, r2.text[:200])
    assert isinstance(r2.json(), (dict, list))


def test_bench_data_filename_is_whitelisted(client):
    """An operator can only reach the two known bench files — no arbitrary path read."""
    r = client.get("/dashboard/data/secrets.json", headers=_op_hdr())
    assert r.status_code == 404, (r.status_code, r.text[:200])
    # Path traversal is not a route match either (still gated, never a file read).
    r2 = client.get("/dashboard/data/..%2f..%2fapp.js", headers=_op_hdr())
    assert r2.status_code in (403, 404), (r2.status_code, r2.text[:200])
