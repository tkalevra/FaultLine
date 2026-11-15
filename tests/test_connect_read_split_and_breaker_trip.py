"""CONNECT/READ SPLIT + BREAKER CONNECT TRIP - BUG-brain-connect-timeout-burns-full-budget-20261026.

The bug, measured live on pre-prod against a SYN-blackholed brain host (198.51.100.20,
100% packet loss): every per-request LLM timeout arrived at httpx as a bare number, which
sets the CONNECT phase to the FULL read budget - so a host that will never answer a SYN
was waited on for 90s (REFRAME) / 120s (NL_FILL_BATCH) / 180s (atomic_subject_resolution)
per attempt, and the per-(lane x error-class) breaker needed threshold=25 such failures
(~37 minutes) to open, with each half-open probe re-paying the full budget.

Two cooperating fixes, both pinned here:

  FIX A - split_connect_timeout(): every per-request LLM timeout is normalized at the POST
      seams (llm_calls._post_llm_sync/_post_llm_async and llm_client.call_llm) into an
      httpx.Timeout with a SHORT connect phase (LLM_CONNECT_TIMEOUT_S, default 5s - the
      bug file measures "declared unreachable in ~3-5s") and the UNCHANGED read budget for
      connected-but-slow. A caller that asked for a TIGHTER connect keeps it; a bare
      number smaller than the connect knob keeps IT (a 0.01s test budget must not grow).

  FIX B - the connect-phase breaker trip: consecutive exhausted calls whose final failure
      is httpx.ConnectTimeout/ConnectError (the host answered NOTHING - decisive evidence
      about the dependency, never about our payload) open the circuit on their own at
      LLM_BREAKER_CONNECT_TRIP (default 2, 0 = byte-for-byte legacy), under the SAME
      reset-window discipline as the legacy counter, so the half-open probe that may
      discover recovery still fires - and now costs a connect budget, not a read budget.

The live connect-phase pins drive a REAL httpx client against a REAL TCP connect
blackhole (loopback listener with a filled accept queue -> further SYNs silently dropped,
verified identical to the pre-prod fault_kind=unreachable shape).
"""

import asyncio
import contextlib
import os
import socket
import sys
import time
from datetime import datetime, timedelta
from types import ModuleType
from unittest.mock import MagicMock

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import llm_calls as L  # noqa: E402


# -- FIX A: the normalization itself -------------------------------------------


def test_a_bare_budget_keeps_its_read_phase_and_shortens_connect(monkeypatch):
    monkeypatch.delenv("LLM_CONNECT_TIMEOUT_S", raising=False)  # the 5s default
    t = L.split_connect_timeout(30.0)
    assert isinstance(t, httpx.Timeout)
    assert t.read == 30.0, "connected-but-slow still owns the full read budget"
    assert t.write == 30.0 and t.pool == 30.0
    assert t.connect == L.llm_connect_timeout_s() == 5.0


def test_the_connect_knob_is_env_tunable_and_clamped(monkeypatch):
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_S", "2.0")
    assert L.llm_connect_timeout_s() == 2.0
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_S", "999")
    assert L.llm_connect_timeout_s() == 60.0, "a nonsense env value must not unclamp"
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_S", "0.05")
    assert L.llm_connect_timeout_s() == 0.5, "a nonsense env value must not kill the floor"
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_S", "junk")
    assert L.llm_connect_timeout_s() == 5.0, "env parse error -> default (LLMTimeouts rule)"


def test_an_existing_timeout_never_gets_a_WIDER_connect(monkeypatch):
    t = L.split_connect_timeout(httpx.Timeout(90.0, connect=2.0))
    assert t.connect == 2.0, "a caller that asked for a tighter connect keeps it"
    assert t.read == 90.0
    t2 = L.split_connect_timeout(httpx.Timeout(90.0, connect=None))
    assert t2.connect == 5.0, "an unset connect phase is brought DOWN to the knob"


def test_none_passes_through_unchanged():
    assert L.split_connect_timeout(None) is None


def test_a_tiny_test_budget_does_not_grow_a_connect_budget():
    t = L.split_connect_timeout(0.01)
    assert t.connect == 0.01 and t.read == 0.01


def test_the_sync_seam_normalizes_the_per_request_timeout(monkeypatch):
    captured = {}

    def _post(_client, url, **kw):
        captured.update(kw)
        raise httpx.ConnectError("stub")

    monkeypatch.setattr(L.llm_source_ip, "post_sync", _post)
    with pytest.raises(httpx.ConnectError):
        L._post_llm_sync(MagicMock(), "http://brain.invalid/v1", json={}, headers={}, timeout=90)
    t = captured["timeout"]
    assert isinstance(t, httpx.Timeout), "the seam must normalize, not forward the bare number"
    assert t.read == 90.0 and t.connect == 5.0


def test_the_async_seam_normalizes_the_same_way(monkeypatch):
    captured = {}

    async def _post(_client, url, **kw):
        captured.update(kw)
        raise httpx.ConnectError("stub")

    monkeypatch.setattr(L.llm_source_ip, "post_async", _post)

    async def _run():
        await L._post_llm_async(MagicMock(), "http://brain.invalid/v1", json={}, headers={}, timeout=90)

    with pytest.raises(httpx.ConnectError):
        asyncio.run(_run())
    assert captured["timeout"].read == 90.0 and captured["timeout"].connect == 5.0


def test_the_tenant_guarded_path_gets_the_split_too(monkeypatch):
    """The SaaS BYO door (shared_guarded_client) is the same seam's other branch."""
    captured = {}

    class _Fake:
        def post(self, url, **kw):
            captured.update(kw)
            raise httpx.ConnectError("stub")

    fake_mod = ModuleType("saas.llm_config")
    fake_mod.shared_guarded_client = lambda: _Fake()
    fake_mod.shared_guarded_async_client = lambda: _Fake()
    monkeypatch.setitem(sys.modules, "saas.llm_config", fake_mod)
    monkeypatch.setattr(L, "_tenant_byo_cfg", lambda: {"account_id": "acct-split"})
    with pytest.raises(httpx.ConnectError):
        L._post_llm_sync(MagicMock(), "http://brain.invalid/v1", json={}, headers={}, timeout=60)
    assert captured["timeout"].read == 60.0 and captured["timeout"].connect == 5.0


def test_llm_client_call_llm_door_applies_the_split(monkeypatch):
    """The other brain POST door (llm_client.call_llm) builds its OWN client - same rule."""
    from src.api import llm_client as LC
    captured = {}

    class _Fake:
        client_timeout = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            resp = MagicMock()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"choices": [{"message": {"content": "{}"}}]}
            return resp

    fake = _Fake()

    class _Factory:
        def __call__(self, timeout=None):
            fake.client_timeout = timeout
            return fake

    monkeypatch.setattr(LC.httpx, "Client", _Factory())
    monkeypatch.setattr(LC, "_saas_mode", lambda: False)
    LC.call_llm("http://brain.invalid/v1/chat/completions", {}, timeout=90)
    t = fake.client_timeout
    assert isinstance(t, httpx.Timeout)
    assert t.read == 90.0 and t.connect == 5.0


# -- FIX A, live: the connect budget is felt on a REAL blackhole ----------------


@contextlib.contextmanager
def _connect_blackhole():
    """Loopback listener with a filled accept queue: further SYNs are silently dropped,
    so connect() hangs until its budget - a genuine TCP connect-phase blackhole."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    fillers = []
    try:
        for _ in range(2):
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.settimeout(5)
            c.connect((host, port))
            fillers.append(c)
        victim = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        victim.settimeout(2)
        t0 = time.perf_counter()
        try:
            victim.connect((host, port))
            if time.perf_counter() - t0 < 1.0:
                pytest.skip("kernel accepts the overflow - rig is not blackholing here")
        except OSError:
            pass
        finally:
            victim.close()
        yield host, port
    finally:
        for c in fillers:
            with contextlib.suppress(Exception):
                c.close()
        srv.close()


def test_a_dead_host_is_refused_at_the_connect_budget_not_the_read_budget(monkeypatch):
    """THE LITMUS, pinned: same 6s read budget, only the connect phase differs.

    Uniform Timeout(6) (the pre-fix shape) hangs the full 6s; split(6, connect=1) is
    refused at ~1s. Measured live against the SAME blackhole on both sides.
    """
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_S", "1.0")
    with _connect_blackhole() as target:
        url = f"http://{target[0]}:{target[1]}/v1/chat/completions"

        t0 = time.perf_counter()
        with pytest.raises(httpx.ConnectTimeout):
            with httpx.Client(timeout=httpx.Timeout(6.0)) as c:  # the PRE-FIX shape
                c.post(url, json={})
        uniform_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        with pytest.raises(httpx.ConnectTimeout):
            with httpx.Client(timeout=L.split_connect_timeout(6.0)) as c:  # the FIX shape
                c.post(url, json={})
        split_s = time.perf_counter() - t0

    assert uniform_s >= 5.5, f"uniform budget must burn the read phase on connect: {uniform_s:.2f}s"
    assert split_s < 3.0, f"the connect split must refuse a dead host in seconds: {split_s:.2f}s"
    assert split_s < uniform_s * 0.6, "the split must be measurably faster, not cosmetically"


# -- FIX B: the breaker connect trip --------------------------------------------


def _breaker(threshold=25, timeout_seconds=30):
    return L.CircuitBreakerState(failure_threshold=threshold, timeout_seconds=timeout_seconds)


def test_connect_streak_opens_the_breaker_short_of_the_threshold(monkeypatch):
    monkeypatch.delenv("LLM_BREAKER_CONNECT_TRIP", raising=False)  # default 2
    b = _breaker(threshold=25)  # the bug-file prod threshold: legacy needs 25 failures
    b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    assert not b.is_open(), "ONE connect fail must not trip"
    b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    assert b.is_open(), "TWO consecutive connect fails must trip short of threshold=25"


def test_a_non_connect_failure_resets_the_streak_host_is_up(monkeypatch):
    monkeypatch.delenv("LLM_BREAKER_CONNECT_TRIP", raising=False)
    b = _breaker(threshold=25)
    b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ReadTimeout", connect_phase=False)
    assert b.consecutive_connect_failures == 0, "a read failure means the host ACCEPTED - up"
    assert not b.is_open()


def test_the_connect_trip_respects_the_reset_window(monkeypatch):
    """The half-open probe must still fire: past the reset window the circuit re-opens
    only on the NEXT connect fail - and that probe now costs a connect budget, not a read."""
    monkeypatch.delenv("LLM_BREAKER_CONNECT_TRIP", raising=False)
    b = _breaker(threshold=25, timeout_seconds=30)
    b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    assert b.is_open()
    b.last_failure_time = datetime.utcnow() - timedelta(seconds=31)  # window elapsed
    assert not b.is_open(), "the reset window must admit the recovery probe"


def test_connect_trip_zero_restores_the_legacy_predicate_exactly(monkeypatch):
    """THE ROLLBACK LEVER: LLM_BREAKER_CONNECT_TRIP=0 -> byte-for-byte the legacy breaker."""
    monkeypatch.setenv("LLM_BREAKER_CONNECT_TRIP", "0")
    b = _breaker(threshold=25)
    for _ in range(2):
        b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    assert not b.is_open(), "legacy: 2 failures < threshold 25"
    assert b.failures == 2, "the legacy counter still advances - nothing is hidden"


def test_success_clears_the_connect_streak(monkeypatch):
    monkeypatch.delenv("LLM_BREAKER_CONNECT_TRIP", raising=False)
    b = _breaker(threshold=25)
    b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    b.record_success()
    assert not b.is_open() and b.consecutive_connect_failures == 0


def test_force_reset_clears_the_connect_streak():
    b = _breaker(threshold=25)
    b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    b.record_failure(L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    b.reset()
    assert not b.is_open() and b.consecutive_connect_failures == 0


def test_failure_classification_shape_is_unchanged():
    """The bug-log signature dependency|REFRAME|ConnectTimeout - class and token are
    exactly what they were; only the WALL and the TRIP changed."""
    assert L.classify_failure(httpx.ConnectTimeout("timed out")) == (
        L.CLASS_DEPENDENCY, "ConnectTimeout")
    assert L.classify_failure(httpx.ConnectError("refused")) == (
        L.CLASS_DEPENDENCY, "ConnectError")
    assert L.classify_failure(httpx.ReadTimeout("slow")) == (
        L.CLASS_DEPENDENCY, "ReadTimeout")
    assert L.is_connect_phase_failure(httpx.ReadTimeout("slow")) is False


# -- FIX B, through the REAL seam ------------------------------------------------


@pytest.fixture()
def _isolate_seam(monkeypatch):
    """Same isolation shape as test_breaker_outage_e2e: no neighbours, no SaaS,
    no Redis, no rate limiter, no health notes - only the breaker under test."""
    monkeypatch.setenv("LLM_CONTEXT_GUARD", "false")
    monkeypatch.setenv("REDIS_COORD", "false")
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setattr(L, "_get_endpoint_list", lambda: ["http://brain.invalid/v1/chat/completions"])
    monkeypatch.setattr(L, "_saas_brain_unavailable", lambda: False)
    monkeypatch.setattr(L, "_tenant_egress_blocked", lambda: False)
    monkeypatch.setattr(L, "apply_rate_limit_sync", lambda operation="DEFAULT": True)
    monkeypatch.setattr(L, "_health_note", lambda *a, **k: None)
    L._llm_circuit_breaker.reset()
    yield
    L._llm_circuit_breaker.reset()


def test_the_real_seam_advances_the_streak_on_a_connect_error(monkeypatch, _isolate_seam):
    def _post(_client, url, **kw):
        raise httpx.ConnectError("endpoint is down")

    monkeypatch.setattr(L.llm_source_ip, "post_sync", _post)
    for _ in range(2):
        with pytest.raises(Exception):
            L.call_llm_with_retry_sync(messages=[{"role": "user", "content": "x"}],
                                       model="m", user_id="u1", operation="EXTRACT",
                                       max_retries=1, timeout=0.01)
    assert L._llm_circuit_breaker.consecutive_connect_failures == 2
    assert L._llm_circuit_breaker.is_open(), "two real connect-fail calls must open the circuit"


def test_the_real_seam_restarts_the_streak_on_a_read_timeout(monkeypatch, _isolate_seam):
    def _post(_client, url, **kw):
        raise httpx.ReadTimeout("connected but slow")

    monkeypatch.setattr(L.llm_source_ip, "post_sync", _post)
    for _ in range(3):
        with pytest.raises(Exception):
            L.call_llm_with_retry_sync(messages=[{"role": "user", "content": "x"}],
                                       model="m", user_id="u1", operation="EXTRACT",
                                       max_retries=1, timeout=0.01)
    assert L._llm_circuit_breaker.consecutive_connect_failures == 0
    assert L._llm_circuit_breaker.failures == 3, "legacy counter counts read failures as before"
    assert not L._llm_circuit_breaker.is_open(), "threshold 5: three reads must not trip"


def test_the_async_seam_advances_the_streak_the_same_way(monkeypatch, _isolate_seam):
    async def _post(_client, url, **kw):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(L.llm_source_ip, "post_async", _post)

    async def _one():
        await L.call_llm_with_retry_async(messages=[{"role": "user", "content": "x"}],
                                          model="m", user_id="u1", operation="EXTRACT",
                                          max_retries=1, timeout=0.01)

    for _ in range(2):
        with pytest.raises(Exception):
            asyncio.run(_one())
    assert L._llm_circuit_breaker.consecutive_connect_failures == 2
    assert L._llm_circuit_breaker.is_open()


def test_the_health_surface_exposes_the_connect_streak(_isolate_seam):
    L._llm_circuit_breaker.record_failure(
        L.CLASS_DEPENDENCY, "dependency|EXTRACT|ConnectTimeout", connect_phase=True)
    st = L.get_circuit_breaker_status()
    assert st["connect_phase_streak"] == 1, "Fowler: breaker state changes must be observable"
    assert st["connect_trip_n"] == L.connect_trip_n()
