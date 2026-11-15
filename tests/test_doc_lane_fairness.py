"""DOC-LANE FAIRNESS — the non-starvation pin (the gauntlet's headline requirement).

MEASURED on production 2026-08-21: while a 32-chunk document drained, GET /health timed out
at 15–60s (13.15s best case) with CPU at 0.00% and Postgres idle — the API process froze per
chunk-batch because the extraction endpoints ran the deterministic spaCy/GLiNER inference
INLINE ON THE EVENT LOOP, so every tenant starved for the document's full duration.

THE BAR (owner): a 32-chunk document churning must leave /health p99 under 1s and leave an
interactive request unaffected. This test drives a STUBBED 32-chunk document churn (a busy
CPU stand-in for the spaCy deriver — the real freeze was measured BURSTY, correlated with the
CPU-bound spine pass, NOT the LLM waits) and asserts exactly that.

RED AT PARENT (c0eb3633): at the parent the stub runs inline on the event loop, /health
cannot even be dispatched while the loop is busy, and p99 blows past the bar. The fix
(src/api/cpu_lane.py::run_cpu) moves inference off the loop for BOTH lanes and caps the
background lane below interactive capacity.

WHY A BUSY STAND-IN IS FAITHFUL: the production freeze was 13-60s against ~7 synchronous
detectors + GLiNER2 per chunk at fan-out 6 — seconds of CONTIGUOUS CPU on the loop. The stub
reproduces the mechanism (a pure-Python CPU burn, GIL-released every switch interval, exactly
like real parsing work) at a compressed scale (80ms per chunk) so the test stays fast. The
assertion is on the MECHANISM (loop availability), not on spaCy's exact cost.

Rollback lever: DOC_LANE_CPU_OFF=true restores the inline path — and this test RED, by
design, because that is the behaviour being rolled back to.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

import httpx
import pytest

from src.api import main as api_main

try:
    from src.api.cpu_lane import set_control
except ImportError:  # parent c0eb3633: the fairness lane does not exist yet — the main pin
    # must still RUN there and fail on the STARVATION assertion (a vacuous import-error red
    # proves nothing). Pause-knob calls degrade to no-ops at the parent.
    def set_control(paused=None, admission=None):
        return {"paused": bool(paused), "admission": admission, "note": "cpu_lane absent"}

    _HAS_CPU_LANE = False
else:
    _HAS_CPU_LANE = True

pytestmark = pytest.mark.asyncio

# The churn: 32 chunks (the production doc-7 shape), each an 80ms CPU burn — the compressed
# stand-in for one chunk's deterministic deriver pass.
N_CHUNKS = 32
BURN_MS = 80
HEALTH_BAR_S = 1.0          # the owner's bar: p99 under 1s WHILE the doc churns
INTERACTIVE_BAR_S = 5.0     # an interactive harvest must still complete promptly

# Counted by the stub — the anti-vacuity guard: the pin only means something if every
# chunk's CPU burn actually executed (a door-refused churn is a fast, meaningless one).
_burns: list[int] = [0]

# The boundary-crossing burn counter: since round-6 the BACKGROUND lane runs its CPU
# passes in WORKER PROCESSES, so an in-process counter cannot be seen by the test —
# every burn also appends one line to this file (O_APPEND, one tiny write per burn).
_BURN_LOG = os.path.join(tempfile.gettempdir(), "fl_doc_lane_fairness_burns")


def _busy_deriver(text: str, db_conn=None) -> list:
    """Deterministic-detector stand-in: ~BURN_MS ms of pure-Python CPU on the input.

    Pure Python (not time.sleep) on purpose: sleep releases the loop/GIL and would NOT
    reproduce the production mechanism. The busy loop holds the CPU exactly like the spaCy
    deriver does, yielding the GIL only at the interpreter's switch interval.
    """
    with open(_BURN_LOG, "a") as _f:
        _f.write("1\n")  # visible to the test across the process boundary
    _burns[0] += 1
    _t0 = time.perf_counter()
    _acc = 0
    while (time.perf_counter() - _t0) < (BURN_MS / 1000.0):
        for _i in range(2000):
            _acc += _i * _i
    return [{"subject": "user", "rel_type": "feels", "object": "busy"}] if _acc >= 0 else []


async def _health_latencies(client, deadline_s: float) -> list[float]:
    """Sample /health until the deadline, returning observed latencies (seconds)."""
    lat: list[float] = []
    _loop = asyncio.get_running_loop()
    _end = _loop.time() + deadline_s
    while _loop.time() < _end:
        _t0 = _loop.time()
        _r = await client.get("/health")
        lat.append(_loop.time() - _t0)
        assert _r.status_code in (200, 503), f"health status {_r.status_code}"
        await asyncio.sleep(0.01)
    return lat


def _p99(xs: list[float]) -> float:
    if not xs:
        return float("inf")
    _s = sorted(xs)
    return _s[min(len(_s) - 1, int(0.99 * len(_s)))]


async def test_doc_churn_leaves_health_p99_under_bar_and_interactive_unaffected(monkeypatch):
    """THE PIN: 32-chunk doc churn → /health p99 < 1s AND an interactive harvest completes.

    Red at parent c0eb3633 (inline inference blocks the loop); green with the cpu_lane fix.
    """
    # ── Stubs: provisioning/health probes must not touch network or DB — the measurement
    # target is EVENT-LOOP AVAILABILITY, and every stub below replaces a dependency that is
    # not under test (the critic rule: a fixture must exercise the seam it names).
    async def _no_tenant_wait(user_id, endpoint):
        return True

    monkeypatch.setattr(api_main, "_ensure_tenant_ready", _no_tenant_wait)
    monkeypatch.setattr(api_main, "_detect_feeling_states", _busy_deriver)
    # The suite baseline (tests/conftest.py) sets SAAS_MODE=1, so with no tenant brain bound
    # the harvest is REFUSED AT THE DOOR (saas_no_brain_failclosed) and the churn never burns
    # — a vacuous fast-health pass at BOTH arms. The brain gate is not under test here; the
    # CPU seam is. Bypass exactly that door.
    monkeypatch.setattr(api_main, "_harvest_no_brain_refusal", lambda uid: None)
    monkeypatch.setattr(api_main, "_check_db_health", lambda dsn: True)
    monkeypatch.setattr(api_main, "_check_qdrant_health", lambda url: True)
    monkeypatch.setattr(api_main, "_gliner2_model", object())  # health: model_loaded gate
    monkeypatch.setattr(api_main, "LLM_SPAN_DETECT", False)    # no LLM spend in this harness
    monkeypatch.setattr(api_main, "TURN_FACT_HARVEST", True)
    # Reset the lane control to a known state (a prior test may have paused it).
    set_control(paused=False, admission=2)
    _burns[0] = 0
    open(_BURN_LOG, "w").close()  # truncate: the file is the burn COUNT, not a log

    _user = "00000000-0000-4000-8000-0000000000ff"
    _body = {"text": "This is one plain declarative sentence with no trigger span.",
             "user_id": _user}
    _bg_headers = {"x-fl-llm-lane": "background"}   # the document worker's self-identification

    transport = httpx.ASGITransport(app=api_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # ── THE CHURN: 32 concurrent background-lane harvests (the stubbed document), each
        # burning one deriver-pass of CPU. This is the doc-7 shape at compressed scale.
        async def _one_chunk():
            r = await client.post("/harvest-spans", json=_body, headers=_bg_headers)
            return r.status_code

        churn = [asyncio.create_task(_one_chunk()) for _ in range(N_CHUNKS)]

        # ── MEASUREMENT 1: /health p99 while the document churns.
        _t_start = asyncio.get_running_loop().time()
        latencies = await _health_latencies(
            client, deadline_s=(N_CHUNKS * BURN_MS / 1000.0) * 0.75)
        elapsed_churn = [t for t in asyncio.as_completed(churn)]
        _statuses = await asyncio.gather(*elapsed_churn)
        _wall = asyncio.get_running_loop().time() - _t_start

        # The churn itself must have actually RUN (all chunks answered 200 AND every chunk's
        # CPU burn executed) — a door-refused or short-circuited churn would make the latency
        # assertion vacuously green. The interactive harvest below burns one more.
        assert all(s == 200 for s in _statuses), f"churn statuses: {_statuses}"
        _burn_count = sum(1 for _ in open(_BURN_LOG))
        assert _burn_count >= N_CHUNKS, (
            f"only {_burn_count} of {N_CHUNKS} chunk CPU burns executed (in-process "
            f"counter saw {_burns[0]} — expected when the background lane runs in worker "
            f"processes) — the churn never reached the CPU seam and the fairness pin is "
            f"VACUOUS"
        )

        # ── MEASUREMENT 2: an INTERACTIVE harvest (no background header) mid-churn window.
        _t0 = asyncio.get_running_loop().time()
        _r = await client.post("/harvest-spans", json=_body)
        _interactive_s = asyncio.get_running_loop().time() - _t0
        assert _r.status_code == 200

        # ── THE BAR.
        _p = _p99(latencies)
        assert len(latencies) >= 5, (
            f"only {len(latencies)} /health sample(s) collected while {N_CHUNKS} chunk burns "
            f"ran (max latency {max(latencies) if latencies else float('nan'):.2f}s) — the "
            f"event loop was STARVED: health requests could not even be dispatched"
        )
        assert _p < HEALTH_BAR_S, (
            f"STARVATION: /health p99={_p:.2f}s over {len(latencies)} samples while a "
            f"{N_CHUNKS}-chunk document churned (churn wall {_wall:.2f}s) — the platform "
            f"must stay responsive during document ingest (bar: p99 < {HEALTH_BAR_S}s)"
        )
        assert _interactive_s < INTERACTIVE_BAR_S, (
            f"interactive harvest took {_interactive_s:.2f}s during document churn — "
            f"interactive traffic must not queue behind background work"
        )
        # The background lane must actually have been the slow one — sanity that the stub
        # burned real CPU (at fix, the churn still takes ~ N*BURN/admission wall-clock).
        assert _wall >= (N_CHUNKS * BURN_MS / 1000.0) / 8, (
            "the CPU stand-in did not burn measurable time — the pin is vacuous"
        )


async def test_doc_lane_pause_refuses_background_and_leaves_interactive_alone(monkeypatch):
    """The operator pause knob: background extraction is REFUSED at the door (deferral
    envelope), interactive traffic is untouched, and the refusal is a 200 — never a crash."""
    async def _no_tenant_wait(user_id, endpoint):
        return True

    monkeypatch.setattr(api_main, "_ensure_tenant_ready", _no_tenant_wait)
    monkeypatch.setattr(api_main, "LLM_SPAN_DETECT", False)
    set_control(paused=True, admission=2)
    try:
        transport = httpx.ASGITransport(app=api_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _user = "00000000-0000-4000-8000-0000000000ff"
            _body = {"text": "A plain sentence.", "user_id": _user}
            _r = await client.post("/harvest-spans", json=_body,
                                   headers={"x-fl-llm-lane": "background"})
            assert _r.status_code == 200
            assert _r.json().get("status") == "doc_lane_paused", _r.json()

            # Interactive (no header) is NOT refused — pause gates deferrable work only.
            _ri = await client.post("/harvest-spans", json=_body)
            assert _ri.status_code == 200
            assert _ri.json().get("status") != "doc_lane_paused"
    finally:
        set_control(paused=False)


async def test_run_cpu_propagates_contextvars_to_the_worker():
    """The per-tenant overlays ride ContextVars — a thread that loses them resolves
    extraction metadata against the WRONG tenant. run_cpu must copy the caller's context."""
    if not _HAS_CPU_LANE:
        pytest.skip("cpu_lane absent (parent) — pins the fix's own contract, not the defect")
    from src.api.cpu_lane import run_cpu
    from src.api import rel_type_overlay

    _tok = rel_type_overlay.set_current_schema("faultlane_ctxtest")
    try:
        _seen = await run_cpu(rel_type_overlay.get_current_schema)
        assert _seen == "faultlane_ctxtest", (
            f"worker thread saw schema {_seen!r} — contextvars NOT propagated"
        )
    finally:
        rel_type_overlay.reset_current_schema(_tok)

def _iso_probe():
    # Runs INSIDE the worker process: reports where it executed and which tenant
    # schema the worker re-bound for the call. Module-level so it pickles by
    # reference across the process boundary.
    import os

    from src.api import rel_type_overlay

    return (os.getpid(), rel_type_overlay.get_current_schema())


async def test_background_cpu_runs_out_of_process_with_tenant_context():
    """ROUND-6 ISOLATION PIN: a BACKGROUND-lane CPU pass executes in a WORKER PROCESS
    (out of this interpreter — threads share the GIL, processes do not), and the
    tenant-schema ContextVar is re-bound inside the worker for the call.

    Red the moment the background lane silently regresses to threads (the round-4/5
    measured failure mode returns: 1.8-2.9s synchronized /health freezes while a
    32-chunk document drains). Skips only on explicit operator rollback
    (DOC_LANE_CPU_PROCESSES=0 / DOC_LANE_CPU_OFF) — never silently."""
    if not _HAS_CPU_LANE:
        pytest.skip("cpu_lane absent (parent) — pins the fix's own contract")
    from src.api import cpu_lane, rel_type_overlay
    from src.api.llm_lane import LANE_BACKGROUND, use_lane

    _snap = cpu_lane.snapshot()
    if _snap.get("cpu_off") or _snap.get("cpu_processes", 0) <= 0:
        pytest.skip("process lane disabled by operator (DOC_LANE_CPU_OFF / "
                    "DOC_LANE_CPU_PROCESSES=0) — the rollback config, not a regression")

    _tok = rel_type_overlay.set_current_schema("faultlane_isopin")
    try:
        with use_lane(LANE_BACKGROUND):
            _pid, _schema = await cpu_lane.run_cpu(_iso_probe)
    finally:
        rel_type_overlay.reset_current_schema(_tok)

    import os as _os

    assert _pid != _os.getpid(), (
        f"background CPU ran IN-PROCESS (pid {_pid}) — the GIL isolation regressed"
    )
    assert _schema == "faultlane_isopin", (
        f"worker saw schema {_schema!r} — the tenant binding did not cross the boundary"
    )
