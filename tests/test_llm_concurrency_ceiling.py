"""`llm_concurrency_slot` — the replacement for a per-REQUEST `asyncio.Semaphore(8)`.

MEASURED DEFECT (main.py): the extract semaphore is constructed inside the request handler,
so it caps ONE request at 8 concurrent LLM calls and caps N concurrent requests at **8N** —
no global bound at all, against a 4-slot llama.cpp server. Measured 11 in-flight on one
lightly-loaded seat and 316 ConnectTimeouts at 3 ms host-level connect latency.

Those ConnectTimeouts are CLASS_DEPENDENCY failures, so over-subscription we caused
ourselves is exactly what opens the account-wide breaker. This is a breaker fix as much as a
throughput fix.

Contract proven here: OFF by default (bare passthrough), a real ceiling when enabled,
fail-OPEN when Redis is unavailable, and the slot is always released.
"""

import asyncio
import os

import pytest

from src.api import llm_calls as L
from src.api import redis_coord as RC


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("LLM_PROCESS_CONCURRENCY", raising=False)
    monkeypatch.delenv("REDIS_COORD", raising=False)
    monkeypatch.delenv("REDIS_COORD_LLM_SEMAPHORE", raising=False)
    L.reset_llm_concurrency()
    RC.reset_client()
    yield
    L.reset_llm_concurrency()
    RC.reset_client()


async def _peak_concurrency(n_tasks: int, hold: float = 0.05) -> int:
    """Run n_tasks through the slot and report the highest number simultaneously inside."""
    state = {"cur": 0, "peak": 0}
    lock = asyncio.Lock()

    async def one():
        async with L.llm_concurrency_slot("EXTRACT"):
            async with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(hold)
            async with lock:
                state["cur"] -= 1

    await asyncio.gather(*[one() for _ in range(n_tasks)])
    return state["peak"]


# ── OFF by default ────────────────────────────────────────────────────────────

def test_unset_is_a_bare_passthrough():
    """No flags → no ceiling at all: byte-for-byte today, where the caller's own semaphore
    is the only bound."""
    peak = asyncio.run(_peak_concurrency(12))
    assert peak == 12, "with no flags set this must not throttle anything"


def test_process_limit_is_zero_by_default():
    assert L.process_llm_concurrency_limit() == 0


# ── LAYER 1: per-PROCESS ceiling (no Redis, no infra) ─────────────────────────

def test_process_ceiling_turns_8N_into_a_real_bound(monkeypatch):
    """The fix for the unbounded term, available with zero infrastructure."""
    monkeypatch.setenv("LLM_PROCESS_CONCURRENCY", "4")
    L.reset_llm_concurrency()
    peak = asyncio.run(_peak_concurrency(20))
    assert peak == 4, f"20 concurrent calls must be capped at 4, saw {peak}"


def test_process_ceiling_still_completes_every_call(monkeypatch):
    """Throttling must never DROP a call — only delay it."""
    monkeypatch.setenv("LLM_PROCESS_CONCURRENCY", "2")
    L.reset_llm_concurrency()
    done = []

    async def run():
        async def one(i):
            async with L.llm_concurrency_slot("EXTRACT"):
                await asyncio.sleep(0.01)
                done.append(i)
        await asyncio.gather(*[one(i) for i in range(15)])

    asyncio.run(run())
    assert sorted(done) == list(range(15))


def test_the_slot_is_released_even_when_the_body_raises(monkeypatch):
    monkeypatch.setenv("LLM_PROCESS_CONCURRENCY", "1")
    L.reset_llm_concurrency()

    async def run():
        for _ in range(3):
            with pytest.raises(ValueError):
                async with L.llm_concurrency_slot("EXTRACT"):
                    raise ValueError("chunk blew up")
        # If the slot leaked, this would deadlock rather than complete.
        async with L.llm_concurrency_slot("EXTRACT"):
            return True

    assert asyncio.run(asyncio.wait_for(run(), timeout=5)) is True


# ── LAYER 2: cross-process ceiling ────────────────────────────────────────────

def test_fleet_ceiling_fails_open_when_redis_is_unreachable(monkeypatch):
    """A Redis outage must never block an LLM call — never lose data to save a metric."""
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_LLM_SEMAPHORE", "true")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    monkeypatch.setenv("REDIS_COORD_SOCKET_TIMEOUT", "0.2")
    monkeypatch.setenv("LLM_GLOBAL_CONCURRENCY", "1")
    RC.reset_client()
    peak = asyncio.run(_peak_concurrency(6))
    assert peak == 6, "unreachable Redis must degrade to no ceiling, not to a stall"


def test_fleet_ceiling_bounds_concurrency_when_redis_is_live(monkeypatch):
    url = os.environ.get("FLTEST_REDIS_URL", "")
    if not url:
        pytest.skip("no FLTEST_REDIS_URL — live fleet-ceiling test skipped")
    monkeypatch.setenv("REDIS_URL", url)
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_LLM_SEMAPHORE", "true")
    monkeypatch.setenv("REDIS_COORD_SOCKET_TIMEOUT", "2")
    monkeypatch.setenv("LLM_GLOBAL_CONCURRENCY", "3")
    monkeypatch.setenv("LLM_BASE_URL", "http://brain-sem-test:8080/v1")
    RC.reset_client()
    if RC.client() is None:
        pytest.skip("scratch Redis not reachable")
    scope = L._breaker_scope()
    try:
        RC.purge_scope(scope)
        peak = asyncio.run(_peak_concurrency(12, hold=0.08))
        assert peak <= 3, f"the fleet ceiling is 3, saw {peak} simultaneously"
        assert RC.inflight_count(scope) == 0, "every slot must be released"
    finally:
        RC.purge_scope(scope)


def test_fleet_wait_exhaustion_proceeds_rather_than_dropping(monkeypatch):
    """If the ceiling cannot be obtained in time we PROCEED. Losing a turn to protect a
    throughput number is the wrong trade — the whole retry seam exists to avoid it."""
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_LLM_SEMAPHORE", "true")
    monkeypatch.setenv("LLM_GLOBAL_CONCURRENCY_MAX_WAIT", "0.05")
    monkeypatch.setattr(RC, "semaphore_enabled", lambda: True)
    monkeypatch.setattr(RC, "try_acquire_slot", lambda *a, **k: False)   # never granted
    monkeypatch.setattr(RC, "release_slot", lambda *a, **k: None)
    monkeypatch.setenv("LLM_BASE_URL", "http://brain-x:8080/v1")

    ran = []

    async def run():
        async with L.llm_concurrency_slot("EXTRACT"):
            ran.append(True)

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert ran == [True], "the call must proceed after the wait is exhausted"
