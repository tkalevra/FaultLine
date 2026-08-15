"""Unit tests for the outbound LLM rate limiter (src/api/llm_rate.py).

No coordination service required: these pin the CONFIG surface, the LOCAL bucket
semantics (pacing math, interactive fail-open vs background defer), the per-request
gate wiring at all three LLM call sites, and the startup announcement contract —
including the load-bearing LOUD local-fallback warning that names the per-process
multiplication.

Fixture hygiene: every URL in these tests is RFC 5737 TEST-NET-1 (192.0.2.0/24) or
localhost — documentation addresses that can never route to a real provider.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from src.api import llm_lane, llm_rate, redis_coord


class _StubLog:
    """Capture structured log calls (event, kwargs) for assertions."""

    def __init__(self):
        self.events = []

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        def _record(event, **kwargs):
            self.events.append((name, event, kwargs))
        return _record


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Every test starts from pristine module caches and a coordination-OFF default
    (the unit suite must not depend on, or touch, any live redis)."""
    monkeypatch.setenv("REDIS_COORD", "false")
    monkeypatch.delenv("REDIS_URL", raising=False)
    redis_coord.reset_client()
    llm_rate._buckets.clear()
    yield
    redis_coord.reset_client()
    llm_rate._buckets.clear()
    os.environ.pop("FL_LLM_DEFAULT_LANE", None)


# ── config surface ────────────────────────────────────────────────────────────

def test_governance_disabled_makes_gate_a_pure_noop(monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "false")
    assert llm_rate.gate_sync("DEFAULT") is True


def test_rate_from_rpm_with_fail_safe_default(monkeypatch):
    assert llm_rate.rate_per_sec() == pytest.approx(480 / 60.0)
    monkeypatch.setenv("LLM_MAX_RPM", "60")
    assert llm_rate.rate_per_sec() == pytest.approx(1.0)
    monkeypatch.setenv("LLM_MAX_RPM", "bogus")
    assert llm_rate.rate_per_sec() == pytest.approx(8.0)


def test_burst_default_is_one_second_of_tokens(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RPM", "60")
    assert llm_rate.burst_capacity(1.0) == 1.0
    monkeypatch.setenv("LLM_RATE_BURST", "5")
    assert llm_rate.burst_capacity(1.0) == 5.0


def test_daily_cap_is_never_invented(monkeypatch):
    """Unknown stays unknown: no env → no daily gate. A fabricated default cap would
    throttle an endpoint that never asked for one."""
    monkeypatch.delenv("LLM_DAILY_BUDGET_REQ", raising=False)
    assert llm_rate.daily_cap() is None
    monkeypatch.setenv("LLM_DAILY_BUDGET_REQ", "500")
    assert llm_rate.daily_cap() == 500
    monkeypatch.setenv("LLM_DAILY_BUDGET_REQ", "-3")
    assert llm_rate.daily_cap() is None


def test_scope_follows_endpoint_and_model(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://192.0.2.10:11434")
    a = llm_rate._resolve_scope(None, "m1")
    same = llm_rate._resolve_scope("http://192.0.2.10:11434/", "m1")
    other_model = llm_rate._resolve_scope("http://192.0.2.10:11434", "m2")
    other_host = llm_rate._resolve_scope("http://192.0.2.11:11434", "m1")
    assert a == same                       # normalization: trailing slash irrelevant
    assert a != other_model                # the budget follows the brain called
    assert a != other_host                 # ...and the host called
    assert "192.0.2.10" not in str(a)      # endpoint identity is a DIGEST, never raw


# ── local bucket semantics ────────────────────────────────────────────────────

def test_local_bucket_burst_then_pace():
    monkeypatch_env = None  # noqa: F841 — clarity only
    os.environ["LLM_MAX_RPM"] = "600"       # 10 tokens/sec
    os.environ["LLM_RATE_BURST"] = "2"
    try:
        b = llm_rate._TokenBucket(10.0, 2.0)
        assert b._try_acquire()[0] is True  # burst
        assert b._try_acquire()[0] is True  # burst
        ok, wait = b._try_acquire()
        assert ok is False and wait > 0     # paced: refill needed
    finally:
        os.environ.pop("LLM_MAX_RPM", None)
        os.environ.pop("LLM_RATE_BURST", None)


def test_interactive_fails_open_background_defers(monkeypatch):
    """The lane split, pinned: after LLM_RATE_MAX_WAIT_S an INTERACTIVE call proceeds
    uncapped (a user is waiting) while a BACKGROUND call yields."""
    monkeypatch.setenv("LLM_MAX_RPM", "1")          # 1/60 rps — one token a minute
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    monkeypatch.setenv("LLM_RATE_MAX_WAIT_S", "0.05")
    scope = llm_rate._resolve_scope("http://192.0.2.20:1", "m")
    key = f"brain:{scope.ident}"
    b = llm_rate._get_bucket(key)
    assert b._try_acquire()[0] is True              # drain the burst token

    with llm_lane.use_lane(llm_lane.LANE_INTERACTIVE):
        assert b.acquire_sync(0.05, deferrable=False) is True
    with llm_lane.use_lane(llm_lane.LANE_BACKGROUND):
        assert b.acquire_sync(0.05, deferrable=True) is False


# ── the per-process multiplication the shared budget exists to close ─────────

def test_two_independent_local_buckets_double_the_outbound(monkeypatch):
    """THE defect, reproduced at unit level: two processes, each with a correct
    per-process bucket, send 2x the configured rate. This is the control the shared
    budget is measured against (the shared-budget run is in
    test_rate_shared_budget.py)."""
    monkeypatch.setenv("LLM_MAX_RPM", "6000")       # 100 rps — bucket never binds
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    grants = {"a": 0, "b": 0}
    deadline = time.monotonic() + 0.4

    def _spin(which: str) -> None:
        # Each thread is a stand-in process with its OWN local bucket: exactly the
        # shape of two module-level buckets in two OS processes.
        own = llm_rate._TokenBucket(llm_rate.rate_per_sec(), llm_rate.burst_capacity(llm_rate.rate_per_sec()))
        while time.monotonic() < deadline:
            if own._try_acquire()[0]:
                grants[which] += 1

    t1 = threading.Thread(target=_spin, args=("a",))
    t2 = threading.Thread(target=_spin, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    # Each bucket grants burst + rate*T = 1 + 40 ≈ 41; two buckets ≈ 82 — DOUBLE a
    # single-bucket run. Assert the multiplication directly: the two-process total
    # exceeds what ONE bucket could have granted by ~2x.
    single_bucket_ceiling = 1 + 100 * 0.4
    assert grants["a"] + grants["b"] > 1.5 * single_bucket_ceiling


# ── gate wiring at the three LLM call sites ───────────────────────────────────

def _defer_env(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND_TYPE", "raw")
    monkeypatch.setenv("LLM_BASE_URL", "http://192.0.2.30:1")
    monkeypatch.setenv("LLM_MAX_RPM", "1")           # one token per minute
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    monkeypatch.setenv("LLM_RATE_MAX_WAIT_S", "0.05")
    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")


def _drain_local_bucket():
    scope = llm_rate._resolve_scope(None, None)
    llm_rate._get_bucket(f"brain:{scope.ident}")._try_acquire()


def test_sync_retry_site_defers_without_posting(monkeypatch):
    from src.api import llm_calls
    _defer_env(monkeypatch)
    _drain_local_bucket()
    posts = []
    monkeypatch.setattr(llm_calls.llm_source_ip, "post_sync",
                        lambda *a, **k: posts.append(1) or (_ for _ in ()).throw(ConnectionError("refused")))
    with llm_lane.use_lane(llm_lane.LANE_BACKGROUND):
        out = llm_calls.call_llm_with_retry_sync(
            [{"role": "user", "content": "x"}], "test-model", max_retries=2)
    assert out == {"error": "rate_deferred"}
    assert posts == []                                # nothing hit the wire


def test_sync_retry_site_raises_llm_unavailable_when_opted_in(monkeypatch):
    from src.api import llm_calls
    _defer_env(monkeypatch)
    _drain_local_bucket()
    monkeypatch.setattr(llm_calls.llm_source_ip, "post_sync",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused")))
    with llm_lane.use_lane(llm_lane.LANE_BACKGROUND):
        with pytest.raises(llm_calls.LLMUnavailable) as ei:
            llm_calls.call_llm_with_retry_sync(
                [{"role": "user", "content": "x"}], "test-model",
                max_retries=2, raise_on_unavailable=True)
    assert ei.value.reason == "rate_deferred"


async def test_async_retry_site_defers_without_posting(monkeypatch):
    from src.api import llm_calls
    _defer_env(monkeypatch)
    _drain_local_bucket()
    posts = []

    async def _fake_post(*a, **k):
        posts.append(1)
        raise ConnectionError("refused")

    monkeypatch.setattr(llm_calls.llm_source_ip, "post_async", _fake_post)
    with llm_lane.use_lane(llm_lane.LANE_BACKGROUND):
        out = await llm_calls.call_llm_with_retry_async(
            [{"role": "user", "content": "x"}], "test-model", max_retries=2)
    assert out == {"error": "rate_deferred"}
    assert posts == []


def test_no_retry_site_defers_to_none(monkeypatch):
    from src.api import llm_calls
    _defer_env(monkeypatch)
    _drain_local_bucket()
    posts = []
    monkeypatch.setattr(llm_calls.llm_source_ip, "post_sync",
                        lambda *a, **k: posts.append(1) or (_ for _ in ()).throw(ConnectionError("refused")))
    with llm_lane.use_lane(llm_lane.LANE_BACKGROUND):
        out = llm_calls.call_llm_no_retry_sync(
            [{"role": "user", "content": "x"}], "test-model")
    assert out is None
    assert posts == []


def test_disabled_governance_leaves_the_call_path_byte_identical(monkeypatch):
    from src.api import llm_calls
    _defer_env(monkeypatch)
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "false")
    calls = []
    monkeypatch.setattr(llm_calls.llm_source_ip, "post_sync",
                        lambda *a, **k: calls.append(1))
    _drain_local_bucket()
    out = llm_calls.call_llm_no_retry_sync(
        [{"role": "user", "content": "x"}], "test-model")
    assert calls == [1]     # the POST happened despite a drained bucket: pure no-op


# ── the startup announcement contract ─────────────────────────────────────────

def test_announce_local_fallback_is_loud_and_names_the_multiplication(monkeypatch):
    """A deployment WITHOUT the coordination service must be TOLD — at warning level,
    naming the consequence: per-process budgets mean outbound exceeds the configured
    rate by the number of processes. A silent fallback re-ships the original bug."""
    stub = _StubLog()
    monkeypatch.setattr(llm_rate, "log", stub)
    llm_rate.announce("api")            # REDIS_COORD=false → no coordination
    warns = [e for e in stub.events if e[0] == "warning"]
    assert any(e[1] == "llm_rate.LOCAL_FALLBACK_PER_PROCESS_BUDGETS" for e in warns), \
        "the local fallback must be a WARNING"
    note = next(e[2]["note"] for e in warns
                if e[1] == "llm_rate.LOCAL_FALLBACK_PER_PROCESS_BUDGETS")
    assert "EXCEED the configured rate by the number of processes" in note
    assert "2" in note and "API" in note and "re-embedder" in note


def test_announce_disabled_states_it_plainly(monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "false")
    stub = _StubLog()
    monkeypatch.setattr(llm_rate, "log", stub)
    llm_rate.announce("api")
    assert any(e[1] == "llm_rate.governance_disabled" for e in stub.events)
    assert not any(e[1] == "llm_rate.LOCAL_FALLBACK_PER_PROCESS_BUDGETS" for e in stub.events)


def test_announce_flags_wait_cap_below_token_interval(monkeypatch):
    """LLM_RATE_MAX_WAIT_S shorter than one token interval structurally cannot pace:
    any queue deeper than one exceeds the cap and interactive calls fail open."""
    monkeypatch.setenv("LLM_MAX_RPM", "6")        # 0.1 rps → 10s per token
    monkeypatch.setenv("LLM_RATE_MAX_WAIT_S", "1")
    stub = _StubLog()
    monkeypatch.setattr(llm_rate, "log", stub)
    llm_rate.announce("api")
    assert any(e[1] == "llm_rate.wait_cap_below_token_interval"
               and e[0] == "warning" for e in stub.events)


# ── the lane crossing HTTP (the header hop) ───────────────────────────────────

def test_lane_header_vocabulary_round_trips():
    """The SENDER's vocabulary and the RECEIVER's validator are the same module —
    pinned so a rename on one side cannot silently strand the other."""
    assert llm_lane.lane_from_header(llm_lane.LANE_BACKGROUND) == llm_lane.LANE_BACKGROUND
    assert llm_lane.lane_from_header(llm_lane.LANE_INTERACTIVE) == llm_lane.LANE_INTERACTIVE
    assert llm_lane.lane_from_header("INTERACTIVE") == llm_lane.LANE_INTERACTIVE  # case-folded
    assert llm_lane.lane_from_header("nonsense") == llm_lane.LANE_INTERACTIVE    # fail-safe
    assert llm_lane.lane_from_header(None) == llm_lane.LANE_INTERACTIVE          # no header = human


def test_api_middleware_honors_the_lane_header(monkeypatch):
    """The API side of the hop: a request carrying the background lane header runs
    its handler (and every LLM call the handler makes) in the BACKGROUND lane; a
    request without it stays interactive. Verified against the real middleware
    function, not a reimplementation."""
    from src.api import main as api_main
    import asyncio

    seen = {}

    async def _handler(request):
        seen["lane"] = llm_lane.current_lane()
        return "resp"

    class _Req:
        headers: dict = {}

    req = _Req()
    req.headers = {llm_lane.LANE_HEADER: "background"}
    out = asyncio.run(api_main.llm_lane_from_request_header(req, _handler))
    assert out == "resp" and seen["lane"] == llm_lane.LANE_BACKGROUND

    req2 = _Req()
    req2.headers = {}
    out2 = asyncio.run(api_main.llm_lane_from_request_header(req2, _handler))
    assert out2 == "resp" and seen["lane"] == llm_lane.LANE_INTERACTIVE


def test_embedder_reextract_posts_stamp_the_lane_header():
    """The EMBEDDER side of the hop: the sweep's LLM-bearing HTTP calls must carry
    the lane header, or its extraction traffic arrives interactive and fail-opens.
    Pinned by source inspection of the two call sites (no HTTP in unit tests)."""
    import inspect
    from src.re_embedder import embedder
    src = inspect.getsource(embedder._reextract_row_edges)
    assert "llm_lane.LANE_HEADER" in src                    # vocabulary: the shared one
    assert src.count("headers=_lane_headers") >= 2          # stamped on BOTH posts


# ── a deferral must never be recorded as a verdict (the consumer contract) ────
# The class of defect: a sweep consumer caches/memoizes/stamps on the LLM call's
# outcome; a rate-deferred call NEVER HAPPENED, so recording from it writes
# permanent wrong state (a memo row the drain then honours forever). These pin the
# three FIXED sites by monkeypatching the LLM call to the deferred shape and
# asserting the write does not land.

def _deferred_env(monkeypatch):  # same shape as the wiring tests
    monkeypatch.setenv("LLM_BACKEND_TYPE", "raw")
    monkeypatch.setenv("LLM_BASE_URL", "http://192.0.2.30:1")
    monkeypatch.setenv("LLM_MAX_RPM", "1")
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    monkeypatch.setenv("LLM_RATE_MAX_WAIT_S", "0.05")
    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")


def test_synonym_convergence_does_not_memoize_a_deferral(monkeypatch):
    """On a deferred proposal the rel must be left UNMEMOIZED (no left_novel row)
    — 'asked and did not converge' is the only thing the memo may record."""
    from src.re_embedder import embedder
    from src.api import llm_calls
    _deferred_env(monkeypatch)
    scope = llm_rate._resolve_scope(None, None)
    llm_rate._get_bucket(f"brain:{scope.ident}")._try_acquire()   # drain burst

    # The gate will defer (background lane, no capacity); the call must raise
    # LLMUnavailable (raise_on_unavailable=True) rather than return a shape.
    monkeypatch.setattr(llm_calls.llm_source_ip, "post_sync",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused")))
    with llm_lane.use_lane(llm_lane.LANE_BACKGROUND):
        with pytest.raises(llm_lane.LLMUnavailable):
            embedder._llm_propose_equivalence(
                "novelrel", "novel rel", {"Person"}, {"Thing"},
                {"cand_a": {"nl": "candidate a"}, "cand_b": {"nl": "candidate b"}},
                "http://192.0.2.31:1")


def test_aspect_map_does_not_answer_a_deferral(monkeypatch):
    """A deferred aspect-map raises LLMUnavailable (did-not-ask) instead of
    returning (None, 0.0), which the caller would stamp as 'left_unmapped'."""
    from src.re_embedder import embedder
    from src.api import llm_calls
    _deferred_env(monkeypatch)
    scope = llm_rate._resolve_scope(None, None)
    llm_rate._get_bucket(f"brain:{scope.ident}")._try_acquire()
    monkeypatch.setattr(llm_calls.llm_source_ip, "post_sync",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused")))
    with llm_lane.use_lane(llm_lane.LANE_BACKGROUND):
        with pytest.raises(llm_lane.LLMUnavailable):
            embedder._aspect_map_via_llm_async(
                "aspectword", [("attr_one", ""), ("attr_two", "")], "http://192.0.2.32:1")


def test_extract_rewrite_marks_rate_deferred_and_the_embedder_treats_it_as_retry():
    """The HTTP hop: a fully-deferred extraction is NOT a zero-edge success.
    Pinned at both ends: the endpoint contract carries the marker, and the
    embedder's re-extract row handler raises on it (leaving reextracted_at NULL)."""
    import inspect
    from src.re_embedder import embedder
    src = inspect.getsource(embedder._reextract_row_edges)
    assert "rate_deferred" in src
    assert "raise RateDeferred" in src          # the DEFER raise specifically, not any raise
    from src.api import main as api_main
    msrc = inspect.getsource(api_main.extract_rewrite)
    assert "rate_deferred" in msrc
    assert "_deferred_chunks" in msrc or "_defer_snapshot" in msrc
    # The poison-row guard must EXEMPT defers: a >30d row must not be stamped by
    # the best-effort guard when the model was never asked.
    rsrc = inspect.getsource(embedder.reextract_episodic)
    assert "except RateDeferred" in rsrc


def test_a_deferred_extracts_response_is_never_idempotency_cached():
    """A partial extraction must not be memorized as final: the idempotency-cache
    call is guarded by the deferral marker."""
    import inspect
    from src.api import main as api_main
    msrc = inspect.getsource(api_main.extract_rewrite)
    i = msrc.find("cache_response")
    assert i > 0
    guard = msrc[:i].rstrip().rsplit("\n", 3)[-3:]
    assert any("deferred" in ln for ln in guard), \
        "the cache_response call must be gated on no-deferral"


def test_poison_row_guard_does_not_stamp_a_deferred_row(monkeypatch):
    """BEHAVIORAL pin (a source-string assert is order-blind — round 4 proved the
    suite stays green with except RateDeferred moved after except Exception):
    when the extraction is rate-deferred, no UPDATE of reextracted_at may execute,
    even for a >30-day-old row that the poison guard would otherwise stamp."""
    from datetime import datetime, timedelta
    from src.re_embedder import embedder

    executed: list[str] = []

    class _Cur:
        def __init__(self, rows):
            self._rows = rows
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, *a, **k):
            executed.append(str(sql))
        def fetchall(self):
            return [(1, "some old text", None, "STATEMENT",
                     datetime.now() - timedelta(days=40))]   # >30d: poison-guard eligible
        @property
        def rowcount(self):
            return 0

    class _DB:
        def cursor(self):
            return _Cur(None)
        def commit(self):
            pass
        def rollback(self):
            pass

    def _defer(*a, **k):
        raise embedder.RateDeferred("extraction rate-deferred (no capacity this pass)")

    monkeypatch.setattr(embedder, "_reextract_row_edges", _defer)
    monkeypatch.setattr(embedder, "_rollback_and_reapply_search_path", lambda *a, **k: None)
    # Intent routing must not send the row down a retraction path that never
    # reaches the extractor; a plain STATEMENT row exercises the deferred path.
    out = embedder.reextract_episodic(
        _DB(), "http://192.0.2.51:1", "11111111-2222-3333-4444-555555555555",
        schema_name="faultline_test")
    stamps = [s for s in executed if "reextracted_at = now()" in s]
    assert stamps == [], f"a deferred row was stamped: {stamps}"
    assert out == 0


# ── startup-failure cache must not be permanent ───────────────────────────────

def test_client_failure_is_retried_not_cached_forever(monkeypatch):
    """The canonical deployment starts redis and both Python processes at the same
    instant; the first connect can lose that race. A failure cached forever would
    pin the deployment to per-process budgets for its whole lifetime — the exact
    multiplication the shared budget exists to close. The second attempt (after the
    retry window) MUST try again."""
    import redis as _redis
    attempts = {"n": 0}

    def _flaky_from_url(*a, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("startup race: redis not up yet")
        c = _redis.from_url(*a, **kw)
        c.ping = lambda: True          # stand in for a live server
        return c

    monkeypatch.setenv("REDIS_COORD", "true")
    RC = redis_coord
    monkeypatch.setattr(redis_coord, "_redis", type("M", (), {"from_url": staticmethod(_flaky_from_url)}))
    redis_coord._client = None
    redis_coord._client_built = False
    redis_coord._next_retry = 0.0
    assert redis_coord.client() is None                    # first attempt: the startup race
    redis_coord._next_retry = 0.0                          # simulate the retry window elapsing
    got = redis_coord.client()
    assert got is not None and attempts["n"] == 2  # retried and connected
    # Cleanup: restore real caches for later tests.
    redis_coord._client = None
    redis_coord._client_built = False
    redis_coord._next_retry = 0.0
