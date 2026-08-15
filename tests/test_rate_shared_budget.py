"""SHARED-BUDGET tests against a live coordination redis (src/api/llm_rate.py).

This is the suite that pins the defect the shared budget exists to close: the
canonical entrypoint backgrounds TWO PROCESSES (the API and the re-embedder) against
one configured endpoint, and a module-level bucket in each means the deployment sends
a MULTIPLE of the configured rate while every process's own accounting looks perfect.

The two processes here are REAL OS PROCESSES (subprocesses), each with its own module
state — not two threads sharing one module dict, which would share the very state the
defect is about. Coordination on → the combined outbound of both processes stays
within the single shared ceiling; coordination off → the same load runs at ~2x, which
is the defect, measured.

Also pins the daily budget's cross-restart persistence: daily state lives in Redis,
so a restarted process must NOT find a fresh budget (an in-memory counter resets to
full on every deploy and blows the quota).

Fixture hygiene: every endpoint URL is RFC 5737 TEST-NET-1 with a per-test host
byte, so no test can collide with another or with a real provider.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

from src.api import llm_lane, llm_rate, redis_coord as RC

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Coordination state persists in redis BY DESIGN — every stateful test uses a
# per-RUN scope (endpoint model tag) so re-running the suite never inherits the
# previous run's budgets or drained buckets.
_RUN = uuid.uuid4().hex[:8]


def _live_redis():
    try:
        import redis as _redis
        c = _redis.from_url(RC.redis_url(), socket_connect_timeout=0.5, socket_timeout=0.5)
        c.ping()
        return c
    except Exception:
        return None


pytestmark = pytest.mark.skipif(
    _live_redis() is None,
    reason="REDIS_URL unreachable — shared-budget tests need a live coordination "
           "redis (e.g. docker run -p 6379:6379 redis:7-alpine)",
)


# One real process: free-runs the gate as fast as it is granted, in the BACKGROUND
# lane (the volume lane — the re-embedder declares itself background at startup),
# and prints how many requests it was allowed to send.
_SPIN = r"""
import sys, time
sys.path.insert(0, {root!r})
from src.api import llm_rate, llm_lane
llm_lane.set_process_default(llm_lane.LANE_BACKGROUND)
n = 0
end = time.monotonic() + {dur}
while time.monotonic() < end:
    if llm_rate.gate_sync("SPIN", endpoint={endpoint!r}, model={model!r}):
        n += 1
print(n)
"""


def _spin_two_processes(endpoint: str, duration_s: float, env: dict) -> tuple[int, int]:
    """Two REAL processes hammering the gate like the entrypoint's API + re-embedder.
    Returns each process's granted request count."""
    code = _SPIN.format(root=_REPO_ROOT, dur=duration_s, endpoint=endpoint,
                        model=f"m-{_RUN}")
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              cwd=_REPO_ROOT) for _ in range(2)]
    outs = []
    for p in procs:
        out, _ = p.communicate(timeout=duration_s + 60)
        outs.append(int(out.strip().splitlines()[-1]))
    return outs[0], outs[1]


def _spin_env(monkeypatch, coord: str) -> dict:
    monkeypatch.setenv("REDIS_COORD", coord)
    monkeypatch.setenv("LLM_MAX_RPM", "120")          # 2 requests/sec
    monkeypatch.setenv("LLM_RATE_BURST", "2")
    monkeypatch.setenv("LLM_RATE_MAX_WAIT_S", "1.0")
    monkeypatch.delenv("LLM_DAILY_BUDGET_REQ", raising=False)
    return dict(os.environ)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.delenv("LLM_DAILY_BUDGET_REQ", raising=False)
    RC.reset_client()
    llm_rate._buckets.clear()
    yield
    RC.reset_client()
    llm_rate._buckets.clear()


def test_two_processes_combined_outbound_stays_within_the_shared_rate(monkeypatch):
    """THE bar: two OS processes, one configured rate, one shared redis — the
    COMBINED grants of both stay ≤ burst + rate*T. Each process alone would be free
    to run at the full rate; only the shared budget makes the SUM behave."""
    env = _spin_env(monkeypatch, coord="true")
    a, b = _spin_two_processes("http://192.0.2.41:1", 3.0, env)
    total = a + b
    assert total > 0, "the gate must still grant — a limiter that blocks everything is broken"
    # burst (2) + rate*T (2*3=6) = 8, + one token of refill/slice slack. The
    # per-process failure mode this guards against measures ~2x this ceiling.
    assert total <= 9, f"combined outbound {total} exceeded the shared rate ceiling"


def test_control_without_coordination_two_processes_double_the_outbound(monkeypatch):
    """The control that proves the shared budget is load-bearing: identical load and
    rate, coordination OFF — each process now paces independently and the combined
    outbound is ~2x. This is the original defect, measured at process granularity,
    and it is what llm_rate.announce() warns about at startup."""
    env = _spin_env(monkeypatch, coord="false")
    a, b = _spin_two_processes("http://192.0.2.42:1", 3.0, env)
    total = a + b
    # Each process's own bucket grants burst(2) + rate*T(6) = 8 → ~16 combined.
    # Comfortably above the shared ceiling of 8: the multiplication, demonstrated.
    assert total >= 12, f"expected the per-process ~2x multiplication, saw {total}"


def test_daily_budget_defers_background_and_survives_restart(monkeypatch):
    """Cap 3/day: the background lane takes 3 then defers; the budget state lives in
    REDIS, so a 'restarted process' (fresh module caches, new connection) finds the
    SAME spent budget — not a fresh one."""
    monkeypatch.setenv("LLM_DAILY_BUDGET_REQ", "3")
    monkeypatch.setenv("LLM_MAX_RPM", "6000")        # minute pace never binds here
    monkeypatch.setenv("LLM_RATE_BURST", "10")
    endpoint = "http://192.0.2.43:1"
    llm_lane.set_process_default(llm_lane.LANE_BACKGROUND)
    try:
        for _ in range(3):
            assert llm_rate.gate_sync("DAILY", endpoint=endpoint, model=f"m-{_RUN}") is True
        assert llm_rate.gate_sync("DAILY", endpoint=endpoint, model=f"m-{_RUN}") is False
        # "Restart the process": drop every module-level cache and connection.
        RC.reset_client()
        llm_rate._buckets.clear()
        # The daily budget does NOT reset to full — it is a daily quantity.
        assert llm_rate.gate_sync("DAILY", endpoint=endpoint, model=f"m-{_RUN}") is False
        # ...while the INTERACTIVE lane is never deferred by the daily budget
        # (it spends with force: counted, not refused).
        with llm_lane.use_lane(llm_lane.LANE_INTERACTIVE):
            assert llm_rate.gate_sync("DAILY", endpoint=endpoint, model=f"m-{_RUN}") is True
    finally:
        llm_lane.set_process_default(llm_lane.LANE_INTERACTIVE)


def test_daily_budget_rolls_with_the_window(monkeypatch):
    """The window is ROLLING and short here: after it passes, the budget reopens —
    the sweep drips through the window instead of dying at a wall."""
    monkeypatch.setenv("LLM_DAILY_BUDGET_REQ", "2")
    monkeypatch.setenv("LLM_DAILY_WINDOW_S", "2")
    monkeypatch.setenv("LLM_MAX_RPM", "6000")
    monkeypatch.setenv("LLM_RATE_BURST", "10")
    endpoint = "http://192.0.2.44:1"
    llm_lane.set_process_default(llm_lane.LANE_BACKGROUND)
    try:
        assert llm_rate.gate_sync("DAILY", endpoint=endpoint, model=f"m-{_RUN}") is True
        assert llm_rate.gate_sync("DAILY", endpoint=endpoint, model=f"m-{_RUN}") is True
        assert llm_rate.gate_sync("DAILY", endpoint=endpoint, model=f"m-{_RUN}") is False
        time.sleep(2.2)                                # window rolls
        assert llm_rate.gate_sync("DAILY", endpoint=endpoint, model=f"m-{_RUN}") is True
    finally:
        llm_lane.set_process_default(llm_lane.LANE_INTERACTIVE)


def test_shared_budget_is_actively_engaged_not_just_flagged(monkeypatch):
    """The lesson from the flag that said True while zero keys existed: verify the
    bucket by its OUTPUT IN REDIS — the fl:llmrate hash with the persisted rate —
    not by trusting the enabled flag."""
    monkeypatch.setenv("LLM_MAX_RPM", "6000")
    monkeypatch.setenv("LLM_RATE_BURST", "3")
    endpoint = "http://192.0.2.45:1"
    assert llm_rate.gate_sync("EVIDENCE", endpoint=endpoint, model=f"m-{_RUN}") is True
    scope = llm_rate._resolve_scope(endpoint, f"m-{_RUN}")
    k = RC.key("llmrate", scope, "bucket")
    raw = _live_redis().hmget(k, "tokens", "rate", "cap")
    assert raw[0] is not None, "the shared bucket hash must exist in redis after a take"
    assert float(raw[1]) == pytest.approx(100.0)      # 6000/60, persisted by the take
    assert float(raw[2]) == pytest.approx(3.0)
