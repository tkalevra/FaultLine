"""HEALTH SWR BOUNDS (interactive-latency lane) — the /health stale-while-revalidate pins.

MEASURED on pre-prod 2026-08-21/22: during a document-drain LLM phase, GET /health stalled
4-16s in waves QUANTIZED at 4s multiples (39 stalls >1s in a 10-minute window; p99 12.0s,
max 16.1s) while same-loop 404s stayed 2-12ms — the loop was NEVER blocked. The stall was
INSIDE /health: socket.getaddrinfo on the (nonexistent on this stack) qdrant host took
8.002s in-container, and httpx's connect timeout does NOT bound DNS resolution. Every
uncached probe wedged on the resolver and concurrent probes piled into the sync-endpoint
threadpool.

The fix: /health serves a cached payload instantly (fresh verbatim; stale instantly with an
additive age_s key + single-flight background refresh on a dedicated thread; cold start
bounded by a hard HEALTH_COLD_DEADLINE_S wall clock via a probe pool that is shut down
WITHOUT waiting — a wedged probe parks ONE worker, never the request path.

These pins hold the MECHANISM, not the incident:
  1. COLD-BOUNDED   — with a wedged probe, the FIRST /health call still returns within the
                      deadline (pre-fix this call hangs for the probe's full wedge).
  2. STALE-INSTANT  — a stale cache is served immediately with age_s, twice in a row
                      (single-flight: no pile of refresh threads on the request path).
  3. FRESH-VERBATIM — a fresh cache is returned byte-identical (no mutation of the cached
                      payload on the fresh path).

Regression lever: removing the deadline (or the SWR block) turns pin 1 into a hang the
join-timeout catches as a failure, not a frozen suite.
"""

import asyncio
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import main as api_main  # noqa: E402


class _Wedge:
    """A probe stand-in that blocks until released (or 60s) — the DNS-wedge shape."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def probe(self, url):
        self.calls += 1
        self.release.wait(timeout=60)
        return False  # even when released, the "host" never answers


@pytest.fixture()
def wedged_qdrant(monkeypatch):
    wedge = _Wedge()
    monkeypatch.setattr(api_main, "_check_qdrant_health", wedge.probe)
    monkeypatch.setattr(api_main, "_check_db_health", lambda dsn: True)
    # cold start for every test: no cache, no parked pool from a previous test
    monkeypatch.setattr(api_main, "_health_cache", {})
    monkeypatch.setattr(api_main, "_health_cache_ts", 0.0)
    yield wedge
    wedge.release.set()  # NEVER leave a parked probe thread behind at suite exit


def test_cold_health_returns_within_the_deadline_with_a_wedged_probe(wedged_qdrant, monkeypatch):
    """COLD-BOUNDED: first call after boot must answer despite a probe that never will.

    The model-loaded gate mirrors prod: a loaded GLiNER2 (the normal serving state —
    _health_refresh_sync raises 503 "Model loading" when it is None, which is the real
    boot behaviour, not the defect under test)."""
    monkeypatch.setattr(api_main, "_gliner2_model", object())
    box = {}

    def call():
        # the route is async def (fast paths on-loop; see the endpoint docstring) —
        # drive it exactly as the server would, via the event loop
        box["resp"] = asyncio.run(api_main.health())

    th = threading.Thread(target=call, daemon=True)
    t0 = time.time()
    th.start()
    th.join(timeout=api_main._HEALTH_COLD_DEADLINE_S + 3.0)
    elapsed = time.time() - t0
    assert not th.is_alive(), (
        "cold /health did not return within the bounded deadline "
        f"(joined {elapsed:.1f}s, deadline {api_main._HEALTH_COLD_DEADLINE_S}s) "
        "— the DNS-wedge stall is back")
    assert elapsed < api_main._HEALTH_COLD_DEADLINE_S + 3.0
    resp = box["resp"]
    # honest payload: db answered fast and OK; the wedged component is reported down,
    # never silently cached-healthy
    assert resp.get("database") == "ok"
    assert resp.get("qdrant") == "unreachable"
    assert "age_s" in resp  # additive honesty key


def test_stale_cache_is_served_instantly_with_age_s(wedged_qdrant):
    """STALE-INSTANT: a stale payload serves immediately (twice), age exposed."""
    cached = {"status": "degraded", "database": "ok", "qdrant": "unreachable",
              "llm": "ok", "model_loaded": True, "age_s": 0.0}
    api_main._health_cache = dict(cached)
    api_main._health_cache_ts = time.time() - 30.0  # STALE (TTL default 5s)

    t0 = time.time()
    resp = asyncio.run(api_main.health())
    first = time.time() - t0
    assert first < 1.0, f"stale /health took {first:.2f}s — the instant-serve path is gone"
    assert resp["age_s"] >= 25.0  # the staleness is EXPOSED, not hidden
    assert resp["database"] == "ok" and resp["qdrant"] == "unreachable"

    t0 = time.time()
    resp2 = asyncio.run(api_main.health())  # immediate second caller must not wait either
    second = time.time() - t0
    assert second < 1.0
    assert resp2["age_s"] >= 25.0
    # single-flight: the refresh that was kicked runs on the DEDICATED executor — the
    # request path never spawned work that blocks the next request. The probe may still
    # be parked at teardown; that is the design (one parked worker, bounded by pool size).


def test_stale_path_refreshes_the_cache_no_self_deadlock(monkeypatch):
    """REGRESSION (shipped live 2026-08-22): the stale path once held the single-flight
    lock while lazily creating the refresh pool under the SAME non-reentrant lock — the
    request thread self-deadlocked, the lock never released, and the live cache stayed
    PINNED AT BOOT with age_s growing to hours (measured 3781s on pre-prod). This pin
    drives the stale path from a COLD module (no pools yet — the exact first-stale-call
    shape) and asserts the refresh actually LANDS."""
    import concurrent.futures as cf

    calls = []

    def slow_probe(url):
        calls.append(time.time())
        time.sleep(0.5)
        return True

    monkeypatch.setattr(api_main, "_check_qdrant_health", slow_probe)
    monkeypatch.setattr(api_main, "_check_db_health", lambda dsn: True)
    monkeypatch.setattr(api_main, "_gliner2_model", object())  # model_loaded gate
    # COLD module: no caches, no pools — recreate the first-stale-call state
    monkeypatch.setattr(api_main, "_health_cache", {
        "status": "ok", "database": "ok", "qdrant": "ok", "llm": "ok",
        "model_loaded": True, "age_s": 0.0, "timestamp": "boot"})
    monkeypatch.setattr(api_main, "_health_cache_ts", time.time() - 30.0)
    monkeypatch.setattr(api_main, "_health_refresh_pool", None)
    monkeypatch.setattr(api_main, "_health_probe_pool", None)

    t0 = time.time()
    resp = asyncio.run(api_main.health())  # stale path: must return INSTANTLY despite cold pools
    first = time.time() - t0
    assert first < 1.0, f"stale /health wedged {first:.1f}s — single-flight self-deadlock"
    assert resp["age_s"] >= 25.0

    # the background refresh must LAND: poll until the cache timestamp advances
    deadline = time.time() + 15
    landed = False
    while time.time() < deadline:
        ts = api_main._health_cache_ts
        if ts > t0:
            landed = True
            break
        time.sleep(0.2)
    assert landed, ("background refresh never landed — the live defect: cache pinned "
                    "at boot, age_s grows forever")
    assert api_main._health_cache["qdrant"] == "ok"  # the SLOW probe answered True
    assert calls, "probe ran"


def test_deadline_exceeded_keeps_last_known_component_state(monkeypatch):
    """DOCSTRING CONTRACT: on deadline the component keeps its LAST KNOWN state — only a
    real probe answer may flip it to 'unreachable'. A wedged probe (deadline) on top of a
    previously-ok qdrant must stay 'ok' in the refreshed payload (it flapped to
    'unreachable' before the None-state distinction landed)."""
    import concurrent.futures as cf

    def wedged_probe(url):
        import time as _t
        _t.sleep(api_main._HEALTH_COLD_DEADLINE_S + 5)  # always exceeds the deadline
        return True

    monkeypatch.setattr(api_main, "_check_qdrant_health", wedged_probe)
    monkeypatch.setattr(api_main, "_check_db_health", lambda dsn: True)
    monkeypatch.setattr(api_main, "_gliner2_model", object())
    # previous cache says qdrant ok — the last-known state to keep
    monkeypatch.setattr(api_main, "_health_cache", {
        "status": "ok", "database": "ok", "qdrant": "ok", "llm": "ok",
        "model_loaded": True, "age_s": 0.0, "timestamp": "prev"})
    monkeypatch.setattr(api_main, "_health_cache_ts", time.time() - 30.0)
    monkeypatch.setattr(api_main, "_health_probe_pool", None)

    m = api_main._health_refresh_sync(bounded=True)
    assert m is None  # refresh returns None; the cache is the artifact
    assert api_main._health_cache["qdrant"] == "ok", (
        "deadline-exceeded flipped a previously-ok component to unreachable")
    assert api_main._health_cache["database"] == "ok"


def test_fresh_cache_is_returned_verbatim(wedged_qdrant):
    """FRESH-VERBATIM: a fresh cache returns byte-identical, no mutation."""
    cached = {"status": "ok", "database": "ok", "qdrant": "ok",
              "llm": "ok", "model_loaded": True, "age_s": 0.0}
    api_main._health_cache = dict(cached)
    api_main._health_cache_ts = time.time()  # FRESH
    resp = asyncio.run(api_main.health())
    assert resp == cached
