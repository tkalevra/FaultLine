"""#211 pins: disk pressure is visible in /health and the console, and refuses ingest writes.

An unattended install filled its host disk; /health stayed green until Postgres PANICked on
"No space left on device". Free space is mocked here (shutil.disk_usage), so the pins never
depend on the machine running them.
"""
from __future__ import annotations

import asyncio
import collections
import json

import pytest

from src.api import disk_guard as dg

_Usage = collections.namedtuple("usage", "total used free")


@pytest.fixture
def free(monkeypatch):
    """Set the mocked free percentage per path: free({'/': 50.0}) or free(5.0) for all."""
    state = {}

    def _set(spec):
        state.clear()
        state.update(spec if isinstance(spec, dict) else {"*": spec})
        dg.reset_cache()

    def _usage(path):
        pct = state.get(path, state.get("*"))
        if pct is None:
            raise FileNotFoundError(path)
        total = 1000 * 1024**3
        f = int(total * pct / 100.0)
        return _Usage(total, total - f, f)
    monkeypatch.setattr(dg.shutil, "disk_usage", _usage)
    for k in ("FAULTLINE_DISK_PATHS", "FAULTLINE_DISK_DEGRADE_PCT", "FAULTLINE_DISK_REFUSE_PCT"):
        monkeypatch.delenv(k, raising=False)
    yield _set
    dg.reset_cache()


@pytest.mark.parametrize("pct,want", [(50.0, "ok"), (10.0, "ok"), (9.9, "degraded"),
                                      (3.0, "degraded"), (2.9, "critical"), (0.0, "critical")])
def test_default_thresholds(free, pct, want):
    free(pct)
    s = dg.disk_status()
    assert s["status"] == want and s["degrade_pct"] == 10.0 and s["refuse_pct"] == 3.0
    assert dg.writes_refused() is (want == "critical")


def test_thresholds_and_paths_are_env_configurable(free, monkeypatch):
    monkeypatch.setenv("FAULTLINE_DISK_PATHS", "/, /pgdata")
    monkeypatch.setenv("FAULTLINE_DISK_DEGRADE_PCT", "25")
    monkeypatch.setenv("FAULTLINE_DISK_REFUSE_PCT", "15")
    free({"/": 60.0, "/pgdata": 20.0})
    s = dg.disk_status()
    assert [p["path"] for p in s["paths"]] == ["/", "/pgdata"]
    assert s["min_free_pct"] == 20.0 and s["status"] == "degraded", "the tightest path decides"
    free({"/": 60.0, "/pgdata": 14.0})
    assert dg.disk_status()["status"] == "critical"


def test_probe_failure_never_refuses_writes(free, monkeypatch):
    monkeypatch.setenv("FAULTLINE_DISK_PATHS", "/does-not-exist")
    free({})
    s = dg.disk_status()
    assert s["status"] == "unknown" and not dg.writes_refused()


def test_write_routes_are_ingest_and_reads_are_not():
    for p in ("/ingest", "/store_context", "/extract/rewrite", "/documents/enqueue",
              "/harvest-spans", "/episodic/append"):
        assert dg.is_ingest_write("POST", p), p
    for m, p in (("POST", "/query"), ("GET", "/health"), ("GET", "/api/dashboard/health"),
                 ("POST", "/retract"), ("POST", "/forget"), ("GET", "/ingest")):
        assert not dg.is_ingest_write(m, p), (m, p)


# ── /health + dashboard ──────────────────────────────────────────────────────

@pytest.fixture
def M():
    import src.api.main as m
    return m


def _cached_ok(M, monkeypatch):
    import time
    monkeypatch.setattr(M, "_health_cache", {"status": "ok", "database": "ok", "llm": "ok",
                                             "qdrant": "not_configured", "age_s": 0.0})
    monkeypatch.setattr(M, "_health_cache_ts", time.time())


@pytest.mark.parametrize("pct,status,writes", [(40.0, "ok", "accepted"),
                                               (8.0, "degraded", "accepted"),
                                               (1.0, "degraded", "refused")])
def test_health_reports_disk_live_over_the_cache(M, monkeypatch, free, pct, status, writes):
    _cached_ok(M, monkeypatch)
    free(pct)
    h = asyncio.run(M.health())
    assert h["disk"]["min_free_pct"] == pct
    assert h["status"] == status and h["ingest_writes"] == writes


def test_health_never_upgrades_unhealthy(M, monkeypatch, free):
    import time
    monkeypatch.setattr(M, "_health_cache", {"status": "unhealthy", "database": "unreachable"})
    monkeypatch.setattr(M, "_health_cache_ts", time.time())
    free(1.0)
    assert asyncio.run(M.health())["status"] == "unhealthy"


def test_dashboard_health_carries_disk(M, monkeypatch, free):
    from src.api import dashboard
    _cached_ok(M, monkeypatch)
    free(5.0)
    d = asyncio.run(dashboard.dashboard_health(_op="op"))
    assert d["disk"]["status"] == "degraded" and d["status"] == "degraded"


def test_console_shows_a_disk_banner():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "webui"
    assert 'id="disk-banner"' in (root / "index.html").read_text()
    js = (root / "app.js").read_text()
    assert "renderDiskBanner(disk)" in js and "d.disk" in js


# ── the middleware: ingest refused at critical, recall untouched ────────────

class _URL:
    def __init__(self, path):
        self.path = path


class _Req:
    def __init__(self, method, path):
        self.method, self.url = method, _URL(path)


def _through(M, method, path):
    async def _next(req):
        return "PASSED"
    return asyncio.run(M.disk_pressure_guard(_Req(method, path), _next))


def test_ingest_refused_with_507_when_critical(M, free):
    free(2.0)
    r = _through(M, "POST", "/ingest")
    assert r != "PASSED" and r.status_code == 507
    body = json.loads(r.body)
    assert body["error"] == "insufficient_storage" and "Recall still works" in body["detail"]
    assert _through(M, "POST", "/query") == "PASSED", "recall must keep working"
    assert _through(M, "GET", "/health") == "PASSED"


def test_ingest_accepted_when_only_degraded(M, free):
    free(5.0)
    assert _through(M, "POST", "/ingest") == "PASSED"
    assert _through(M, "POST", "/store_context") == "PASSED"


def test_documented_in_env_example():
    from pathlib import Path
    env = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    for k in ("FAULTLINE_DISK_PATHS", "FAULTLINE_DISK_DEGRADE_PCT", "FAULTLINE_DISK_REFUSE_PCT"):
        assert k in env, k
