"""The CONSUMERS actually read the shared state — verified from the consumer backwards.

`test_redis_coord.py` proves the store round-trips. That is asserting the WRITE. This file
asserts the READ: it drives `llm_calls._breaker_is_open()` and `context_window.resolve_window()`
— the functions the live call path calls — and shows they change behaviour because of state
another process published.

Two processes are simulated by publishing under one scope and reading with a FRESH in-process
state, which is exactly the situation N uvicorn workers are in.
"""

import os

import pytest

from src.api import context_window as CW
from src.api import llm_calls as L
from src.api import redis_coord as RC


def _scratch():
    url = os.environ.get("FLTEST_REDIS_URL", "")
    if not url:
        pytest.skip("no FLTEST_REDIS_URL — cross-process consumer tests skipped")
    return url


@pytest.fixture
def live(monkeypatch):
    url = _scratch()
    monkeypatch.setenv("REDIS_URL", url)
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_SOCKET_TIMEOUT", "2")
    monkeypatch.setenv("REDIS_COORD_OPEN_CACHE_MS", "0")
    RC.reset_client()
    RC.reset_open_cache()
    if RC.client() is None:
        pytest.skip("scratch Redis not reachable")
    yield
    RC.reset_client()
    RC.reset_open_cache()


# ── shared breaker: worker A's trip protects workers B..N ─────────────────────

def test_worker_b_fails_fast_because_worker_a_tripped(live, monkeypatch):
    """The confirmed gap: N workers hold N independent opinions about one brain."""
    monkeypatch.setenv("REDIS_COORD_BREAKER", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://brain-shared:8080/v1")
    monkeypatch.setenv("WGM_LLM_MODEL", "qwen-test")
    scope = L._breaker_scope()
    assert scope is not None
    try:
        RC.purge_scope(scope)
        worker_a = L.CircuitBreakerState(failure_threshold=2, timeout_seconds=30)
        worker_b = L.CircuitBreakerState(failure_threshold=2, timeout_seconds=30)

        worker_a.record_failure()
        worker_a.record_failure()
        assert worker_a.is_open()
        L._publish_breaker_state(worker_a)

        # Worker B has seen ZERO failures of its own.
        assert worker_b.failures == 0
        assert worker_b.is_open() is False, "locally it knows nothing"
        assert L._breaker_is_open(worker_b) is True, (
            "the CONSUMER must adopt the shared verdict — otherwise worker A's trip "
            "protects nobody and the recovery probe fires once per worker"
        )
    finally:
        RC.purge_scope(scope)


def test_a_success_anywhere_releases_every_worker(live, monkeypatch):
    monkeypatch.setenv("REDIS_COORD_BREAKER", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://brain-shared:8080/v1")
    scope = L._breaker_scope()
    try:
        RC.purge_scope(scope)
        a = L.CircuitBreakerState(failure_threshold=1, timeout_seconds=300)
        a.record_failure()
        L._publish_breaker_state(a)
        b = L.CircuitBreakerState(failure_threshold=1, timeout_seconds=300)
        assert L._breaker_is_open(b) is True

        L._clear_shared_breaker()
        RC.reset_open_cache()
        c = L.CircuitBreakerState(failure_threshold=1, timeout_seconds=300)
        assert L._breaker_is_open(c) is False, "recovery must not wait out the TTL"
    finally:
        RC.purge_scope(scope)


def test_the_shared_view_can_only_ADD_an_open_verdict(live, monkeypatch):
    """One-way by design: shared state can never force a locally-open breaker CLOSED, so a
    stale/wrong shared value can only ever cost `timeout_seconds` of fast-fail."""
    monkeypatch.setenv("REDIS_COORD_BREAKER", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://brain-shared:8080/v1")
    scope = L._breaker_scope()
    try:
        RC.purge_scope(scope)                     # nothing shared: no open verdict anywhere
        b = L.CircuitBreakerState(failure_threshold=1, timeout_seconds=300)
        b.record_failure()
        assert b.is_open()
        assert L._breaker_is_open(b) is True, "an empty shared view must not close a local trip"
    finally:
        RC.purge_scope(scope)


def test_breaker_scope_is_never_unscoped(monkeypatch):
    """Even with nothing configured, the scope resolver produces an EXPLICIT scope."""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("REDIS_COORD", "true")
    s = L._breaker_scope()
    assert isinstance(s, RC.Scope)
    assert RC.key("brk", s, "open").startswith("fl:brk:")


def test_breaker_sharing_off_by_default_is_pure_in_process(monkeypatch):
    monkeypatch.delenv("REDIS_COORD", raising=False)
    monkeypatch.delenv("REDIS_COORD_BREAKER", raising=False)
    RC.reset_client()
    b = L.CircuitBreakerState(failure_threshold=1, timeout_seconds=30)
    assert L._breaker_is_open(b) is False
    b.record_failure()
    assert L._breaker_is_open(b) is True
    L._publish_breaker_state(b)      # must be a silent no-op, not an error


# ── shared learned context window ─────────────────────────────────────────────

def test_a_fresh_worker_inherits_the_window_another_worker_overran_to_learn(live, monkeypatch):
    """Without this, every worker must overrun ONCE to learn the same number."""
    monkeypatch.setenv("REDIS_COORD_WINDOW", "true")
    monkeypatch.setenv("LLM_CONTEXT_GUARD", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://brain-w:8080/v1")
    monkeypatch.setenv("WGM_LLM_MODEL", "qwen-test")
    monkeypatch.delenv("LLM_CONTEXT_WINDOW", raising=False)
    monkeypatch.setenv("LLM_CONTEXT_PROBE", "false")
    scope = CW._coord_scope()
    assert scope is not None
    try:
        RC.purge_scope(scope)
        CW.reset_state()
        # Worker A learns the hard way — from the server's own refusal.
        learned = CW.learn_from_rejection({"n_ctx": 4096, "n_prompt_tokens": 9001})
        assert learned == 4096

        # Worker B: brand-new process state, has never overrun anything.
        CW.reset_state()
        window, source = CW.resolve_window(allow_probe=False)
        assert window == 4096, "the CONSUMER must read the shared window"
        assert source == CW.SOURCE_LEARNED, (
            "and must carry the same AUTHORITY — it came from the endpoint itself, "
            "not from a guess"
        )
    finally:
        RC.purge_scope(scope)
        CW.reset_state()


def test_window_cache_off_by_default_leaves_resolution_untouched(monkeypatch):
    monkeypatch.delenv("REDIS_COORD", raising=False)
    monkeypatch.delenv("REDIS_COORD_WINDOW", raising=False)
    monkeypatch.setenv("LLM_CONTEXT_GUARD", "true")
    monkeypatch.setenv("LLM_CONTEXT_PROBE", "false")
    monkeypatch.delenv("LLM_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("LLM_CONTEXT_WINDOW_DEFAULT", raising=False)
    RC.reset_client()
    CW.reset_state()
    assert CW._coord_scope() is None
    assert CW._shared_window() is None
    window, source = CW.resolve_window(allow_probe=False)
    assert source in (CW.SOURCE_UNKNOWN, CW.SOURCE_DEFAULT)
    CW.reset_state()


def test_two_different_brains_never_share_a_learned_window(live, monkeypatch):
    """A tenant BYO endpoint's window must never leak onto another tenant's brain."""
    monkeypatch.setenv("REDIS_COORD_WINDOW", "true")
    monkeypatch.setenv("LLM_CONTEXT_GUARD", "true")
    monkeypatch.setenv("LLM_CONTEXT_PROBE", "false")
    monkeypatch.delenv("LLM_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("LLM_CONTEXT_WINDOW_DEFAULT", raising=False)

    monkeypatch.setenv("LLM_BASE_URL", "http://tenant-a-brain:8080/v1")
    scope_a = CW._coord_scope()
    monkeypatch.setenv("LLM_BASE_URL", "http://tenant-b-brain:8080/v1")
    scope_b = CW._coord_scope()
    assert scope_a != scope_b
    try:
        RC.purge_scope(scope_a)
        RC.purge_scope(scope_b)
        monkeypatch.setenv("LLM_BASE_URL", "http://tenant-a-brain:8080/v1")
        CW.reset_state()
        CW.learn_from_rejection({"n_ctx": 4096})

        monkeypatch.setenv("LLM_BASE_URL", "http://tenant-b-brain:8080/v1")
        CW.reset_state()
        assert CW._shared_window() is None, "tenant B must not inherit tenant A's window"
    finally:
        RC.purge_scope(scope_a)
        RC.purge_scope(scope_b)
        CW.reset_state()
