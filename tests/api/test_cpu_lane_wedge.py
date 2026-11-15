"""TX6 — the background CPU-lane WEDGE BOUND pins (gauntlet tx6-bg-cpu-wedge).

MECHANISM (pinned 2026-09-04, DEV/review/gauntlet/tx6-bg-cpu-wedge/MECHANISM.md):
the fork-context process pool in src/api/cpu_lane.py forks workers from the
torch/OpenMP-warmed API main thread; a worker inherits poisoned libgomp team state
and its first OpenMP-parallel op waits forever on a barrier futex — ALIVE, so no
BrokenProcessPool, so the unbounded `await asyncio.wrap_future(fut)` in
_dispatch_process never returned, run_cpu's finally _release() never ran, and the
admission permit stayed pinned at cap forever (live on pre-prod: inflight == 2,
waiting == 10, for hours).

THE FIX under test — two independent layers:
  * LAYER 1 (structural bound): _dispatch_process bounds submit+await with
    asyncio.wait_for(DOC_PROC_TIMEOUT). A timeout with an already-done future takes
    the honest result; a liveness probe (DOC_PROC_PROBE_S) spares a slow-but-draining
    pool; a stalled probe (every worker wedged) tears the pool down, SIGKILLing the
    wedged workers, and the call takes the existing thread fallback ONCE — the permit
    is released by run_cpu's existing finally. NO retry loop anywhere.
  * LAYER 2 (hazard removal): the pool is built with initializer=_proc_init, which
    pins torch intra/inter-op threads to 1 in each worker BEFORE its first op, so a
    worker never enters an OpenMP parallel region at all.

RED AT PARENT (c8018991): the wedge tests cannot complete — the dispatch never
returns, so their outer honesty bound (asyncio.wait_for around the work) fires; the
initializer test fails on the absent initializer; the env test fails on the absent
knob. Green only with both layers.

The wedge is simulated WITHOUT torch: a stand-in distinguishes parent-thread from
worker-process by pid and sleeps in the worker only — faithful to the pinned
mechanism (a worker that is alive but never returns), which is ALL layer 1 needs.
Layer 2's torch call is asserted via a stub torch module. The real fork/libgomp
wedge and its initializer cure are additionally repro-verified live in throwaway
containers (MECHANISM.md; re-verified this round on the faultline-wgm image).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import types

import pytest

from src.api import cpu_lane
from src.api.llm_lane import LANE_BACKGROUND, use_lane

pytestmark = pytest.mark.asyncio

# outer honesty bound: on the PARENT code the dispatch wedges, and the test must go
# RED on a timeout, not hang the suite.
_OUTER_S = 25.0

_PARENT_PID = [0]
_SLOW_HONEST_S = [0.8]  # module-level so the forked child inherits it


def _slow_honest_mod(value=None):
    """Module-level (picklable) slow-but-honest worker fn: completes in the child
    after a configurable delay; in the parent thread (the fallback) it reports
    "thread" so the done-race test can prove WHICH lane produced the result."""
    if os.getpid() != _PARENT_PID[0]:
        time.sleep(_SLOW_HONEST_S[0])
        return "proc"
    return "thread"


def _wedge_stand_in(value=None):
    """Worker-process: alive, silent, never returns (the pinned wedge shape).
    Parent thread (the fallback): answers instantly."""
    if os.getpid() == _PARENT_PID[0]:
        return "thread"
    time.sleep(90)
    return "proc"


def _proc_vs_thread(value=None):
    """Reports WHERE it ran — used to prove which lane produced a result."""
    return "proc" if os.getpid() != _PARENT_PID[0] else "thread"


def _fast_ok(value=None):
    return "ok"


async def _bg_call(func, *a):
    """run_cpu on the background lane (the only lane that rides the process pool)."""
    with use_lane(LANE_BACKGROUND):
        return await cpu_lane.run_cpu(func, *a)


@pytest.fixture(autouse=True)
def _fresh_lane(monkeypatch):
    """Isolate the process-lane globals per test (pool, budget, timing knobs).

    _PARENT_PID is set BEFORE any pool exists in the test body, so every worker
    forked during the test inherits the right value (fork inheritance is the
    design under test).
    """
    cpu_lane._proc_broken()
    cpu_lane._proc_failures = 0
    # Tolerate the PARENT code (no knobs yet) so the revert-red run is BEHAVIORAL
    # (the dispatch genuinely never returns → the outer bound fires), not a setup
    # AttributeError that proves nothing.
    if hasattr(cpu_lane, "_PROC_TIMEOUT_S"):
        monkeypatch.setattr(cpu_lane, "_PROC_TIMEOUT_S", 1.5)
        monkeypatch.setattr(cpu_lane, "_PROC_PROBE_S", 1.0)
    _PARENT_PID[0] = os.getpid()
    yield
    cpu_lane._proc_broken()
    cpu_lane._proc_failures = 0


# ── LAYER 1: the wedge bound ───────────────────────────────────────────────────


async def test_wedged_dispatch_is_bounded_permit_released_and_lane_recovers():
    """BOTH workers wedge → both calls return via threads within the bound, the
    permit is released (inflight back to 0), the wedged pool is charged/reforked,
    and a subsequent background call acquires promptly — the admission is never
    permanently pinned (the live 5-hour wedge, at test scale)."""
    t0 = time.monotonic()
    out = await asyncio.wait_for(
        asyncio.gather(_bg_call(_wedge_stand_in), _bg_call(_wedge_stand_in)),
        timeout=_OUTER_S)
    elapsed = time.monotonic() - t0
    assert out == ["thread", "thread"], "the thread fallback must carry the work"
    assert elapsed < _OUTER_S, "RED at parent: the dispatch never returned"
    snap = cpu_lane.snapshot()
    assert snap["inflight"] == 0, "permit must be released by the existing finally"
    assert cpu_lane._proc_failures >= 1, "a wedged pool must be charged/reforked"
    t1 = time.monotonic()
    assert await asyncio.wait_for(_bg_call(_fast_ok), timeout=_OUTER_S) == "ok"
    assert time.monotonic() - t1 < _OUTER_S, "lane must recover, not stay pinned"


async def test_slow_but_draining_pool_is_not_torn_down():
    """ONE slow call, pool still drains (the probe answers on the free worker):
    threads for THIS call, but the pool, its other worker and its budget survive —
    slow-but-honest work is not punished (the liveness re-check)."""
    _pool_before = cpu_lane._proc_pool()
    failures_before = cpu_lane._proc_failures
    t0 = time.monotonic()
    out = await asyncio.wait_for(_bg_call(_wedge_stand_in), timeout=_OUTER_S)
    assert out == "thread"
    assert time.monotonic() - t0 < _OUTER_S
    assert cpu_lane._proc_failures == failures_before, "slow is not broken: no charge"
    assert cpu_lane._proc_executor is _pool_before, "a draining pool must survive"
    # the abandoned in-flight worker job must not block the NEXT process-lane call
    assert await asyncio.wait_for(_bg_call(_fast_ok), timeout=_OUTER_S) == "ok"


async def test_done_race_takes_the_honest_process_result(monkeypatch):
    """A future that is DONE when the timeout fires is consumed as-is — the honest
    process result wins and the work is NOT duplicated onto the thread fallback.

    Deterministic by construction: asyncio.wait_for is patched for the ONE dispatch
    await to hold until the underlying future is actually done and only then raise
    TimeoutError — exactly the race window the guard exists for."""
    real_wait_for = asyncio.wait_for
    armed = {"fired": False}

    async def racing_wait_for(fut, timeout=None, **kw):
        if timeout is getattr(cpu_lane, "_PROC_TIMEOUT_S", 1.5) and not armed["fired"]:
            armed["fired"] = True
            _deadline = time.monotonic() + 3.0
            while not fut.done() and time.monotonic() < _deadline:
                await asyncio.sleep(0.02)
            # done → the honest race; not done in 3s → raise anyway (bounded fake)
            raise asyncio.TimeoutError()
        return await real_wait_for(fut, timeout=timeout, **kw)

    monkeypatch.setattr(asyncio, "wait_for", racing_wait_for)
    out = await asyncio.wait_for(_bg_call(_slow_honest_mod), timeout=_OUTER_S)
    assert armed["fired"], "the race must have been injected"
    # "proc" can ONLY come from the worker: the done-race guard consumed the honest
    # future WITHOUT the fallback re-running the work (which would say "thread").
    assert out == "proc"


def test_thread_fallback_marker_is_distinguishable():
    """Sanity for the pid discriminator the wedge tests ride on."""
    assert _proc_vs_thread() == "thread", "in the parent process it must say thread"


# ── LAYER 2: the hazard removed at source ──────────────────────────────────────


async def test_pool_is_built_with_the_single_thread_initializer():
    """The pool must carry initializer=_proc_init (torch threads=1 BEFORE the first
    op — the repro-verified hazard removal)."""
    pool = cpu_lane._proc_pool()
    assert pool is not None
    fut = pool.submit(cpu_lane._proc_noop)
    await asyncio.wait_for(asyncio.wrap_future(fut), timeout=10)
    assert pool._initializer is cpu_lane._proc_init, "RED at parent: no initializer"


def test_proc_init_pins_torch_threads_to_one(monkeypatch):
    """_proc_init sets intra-op (and best-effort inter-op) threads to 1, and is a
    safe no-op when torch is absent or refuses the late inter-op set."""
    seen = []
    fake = types.ModuleType("torch")
    fake.set_num_threads = lambda n: seen.append(("intra", n))
    fake.set_num_interop_threads = lambda n: seen.append(("inter", n))
    monkeypatch.setitem(sys.modules, "torch", fake)
    cpu_lane._proc_init()
    assert ("intra", 1) in seen
    fake.set_num_interop_threads = lambda n: (_ for _ in ()).throw(RuntimeError("late"))
    cpu_lane._proc_init()  # must not raise
    monkeypatch.setitem(sys.modules, "torch", None)
    cpu_lane._proc_init()  # absent torch: no-op, never raises


async def test_killing_a_torn_down_pool_leaves_no_wedged_orphans():
    """_proc_broken SIGKILLs worker processes: a wedged (sleeping) worker must be
    gone after teardown, not orphaned holding a COW copy of the models."""
    pool = cpu_lane._proc_pool()
    await asyncio.wait_for(
        asyncio.wrap_future(pool.submit(cpu_lane._proc_noop)), timeout=10)
    fut = pool.submit(_wedge_stand_in)  # one worker now sleeps 90s — "wedged"
    await asyncio.sleep(0.4)            # let it start inside the worker
    pids = list(getattr(pool, "_processes", {}))
    assert pids, "pool must have forked workers by now"
    cpu_lane._proc_broken()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not [p for p in pids if os.path.exists("/proc/%d" % p)]:
            break
        time.sleep(0.05)
    alive = [p for p in pids if os.path.exists("/proc/%d" % p)]
    assert not alive, "wedged workers must be SIGKILLed on teardown, not orphaned"
    del fut  # the abandoned future resolves BrokenProcessPool; nobody awaits it


# ── the env knobs ──────────────────────────────────────────────────────────────


def test_timeout_knob_is_env_overridable(monkeypatch):
    """DOC_PROC_TIMEOUT / DOC_PROC_PROBE_S are real env knobs (the operator lever
    beside DOC_LANE_CPU_OFF). RED at parent: the constants do not exist."""
    import importlib
    try:
        monkeypatch.setenv("DOC_PROC_TIMEOUT", "7")
        monkeypatch.setenv("DOC_PROC_PROBE_S", "2.5")
        cpu_lane._proc_broken()  # release the old pool before re-executing the module
        reloaded = importlib.reload(cpu_lane)
        assert reloaded._PROC_TIMEOUT_S == 7.0
        assert reloaded._PROC_PROBE_S == 2.5
    finally:
        monkeypatch.delenv("DOC_PROC_TIMEOUT", raising=False)
        monkeypatch.delenv("DOC_PROC_PROBE_S", raising=False)
        cpu_lane._proc_broken()
        importlib.reload(cpu_lane)  # restore default knobs for any later test
