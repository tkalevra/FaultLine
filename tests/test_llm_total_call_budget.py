"""The account's rate budget must count the TOTAL number of requests it actually sends.

Prod incident 2026-08-11 (account 8e4a51c2 on Cerebras Free tier — a real 5 RPM ceiling):
the configured pace was CORRECT at 5 RPM and the account still 429'd continuously for
hours. Three independent under-counts multiplied:

  D1  the bucket is per-PROCESS (`llm_calls._rate_buckets` is a module dict) and the API
      container runs TWO processes against one account — `uvicorn src.api.main` and
      `python -m src.re_embedder.embedder`. Two `bucket_created` lines for one account key
      in one container's log. The limit was enforced twice, independently.
  D2  a token was spent per logical CALL but up to `max_retries * len(endpoints)` REQUESTS
      were sent on it.
  D3  at 5 RPM a token mints every 12s while LLM_RATE_MAX_WAIT_S is 30 — so any queue
      deeper than ~2 exceeded the cap and FAILED OPEN by construction. Measured: 38
      `wedged_failopen` in one hour against 3 `bucket_paused`.

And the reason it kept producing load rather than settling:

  D4  every LLM helper returns None both when the model answered nothing and when the model
      was never reached, so the re_embedder cached verdicts derived from no information and
      burned each concept's retry budget while the brain was unreachable.

These tests drive the REAL entry points (`call_llm_with_retry_sync`, `_TokenBucket`), not a
reimplementation — the repo's standing lesson is that asserting the write proves nothing
about the read.

Run: python3 tools/fltest.py --bug RATE429 --test tests/test_llm_total_call_budget.py
"""

import os

import httpx
import pytest

import src.api.llm_calls as rl
from src.api import llm_lane as lane
from src.api import redis_coord as RC


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Clean bucket registry, limiter on, no SaaS tenant cfg, shared budget OFF by default."""
    monkeypatch.setattr(rl, "_current_llm_config_or_none", lambda: None)
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("LLM_BASE_URL", "http://test-endpoint:1234")
    monkeypatch.setenv("LLM_RATE_MAX_WAIT_S", "30")
    monkeypatch.setenv("REDIS_COORD", "false")
    monkeypatch.setenv("FL_LLM_DEFAULT_LANE", "")      # interactive unless a test says otherwise
    monkeypatch.setenv("LLM_CONTEXT_GUARD", "false")   # isolate the limiter
    rl.reset_rate_limiter()
    # The _seam below binds a cfg WITH an account_id, so real calls run on a PER-ACCOUNT
    # breaker, not the global one — its state (incl. the connect-phase streak added by
    # BUG-brain-connect-timeout-burns-full-budget-20261026) must not leak across tests.
    rl._tenant_circuit_breakers.clear()
    yield
    rl.reset_rate_limiter()
    rl._tenant_circuit_breakers.clear()


def _seam(monkeypatch, responder, account="acct-budget"):
    """Wire the real sync call path to an in-process responder (no network)."""
    monkeypatch.setattr(rl, "_get_endpoint_list", lambda: ["http://brain:8080/v1/chat/completions"])
    monkeypatch.setattr(rl, "_saas_brain_unavailable", lambda: False)
    monkeypatch.setattr(rl, "_tenant_egress_blocked", lambda: False)
    monkeypatch.setattr(rl, "_health_note", lambda *a, **k: None)
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": account,
                                 "base_url": "http://brain:8080/v1",
                                 "model": "m"})
    monkeypatch.setattr(rl.llm_source_ip, "post_sync", responder)
    rl.reset_rate_limiter()


# ── D2: one token per REQUEST, not per logical call ──────────────────────────────────

def test_a_retrying_call_spends_one_token_per_request(monkeypatch):
    """THE UNDER-COUNT: 3 attempts used to ride on a single token.

    Without the fix this records tokens=1 against requests=3 — the bucket believes the
    account sent a third of what it actually put on the wire.
    """
    posts = {"n": 0}
    tokens = {"n": 0}

    def _post(_client, url, **kw):
        posts["n"] += 1
        raise httpx.ConnectError("boom")   # force every retry to be used

    _seam(monkeypatch, _post)

    real = rl.apply_rate_limit_sync

    def _counting(operation="DEFAULT"):
        tokens["n"] += 1
        return real(operation)

    monkeypatch.setattr(rl, "apply_rate_limit_sync", _counting)
    rl._llm_circuit_breaker.reset()
    try:
        with pytest.raises(Exception):
            rl.call_llm_with_retry_sync(
                messages=[{"role": "user", "content": "hi"}],
                model="m", user_id="u", operation="EXTRACT",
                max_retries=3, timeout=0.1,
            )
    finally:
        rl._llm_circuit_breaker.reset()

    assert posts["n"] == 3, "the seam should have sent one request per attempt"
    assert tokens["n"] == posts["n"], (
        f"the budget must count every REQUEST: spent {tokens['n']} token(s) for "
        f"{posts['n']} request(s) — the difference is invisible outbound load"
    )


# ── D3: background yields; interactive still fails open ──────────────────────────────

def _starved(monkeypatch):
    """A bucket that can never mint a token inside the cap (the 5 RPM / 30s-cap shape)."""
    monkeypatch.setenv("LLM_MAX_RPM", "1")       # 1 token per 60s
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    monkeypatch.setenv("LLM_RATE_MAX_WAIT_S", "0.05")


def test_background_work_defers_instead_of_firing_uncapped(monkeypatch):
    """Deferrable upkeep must NOT inherit the never-lose-a-call fail-open."""
    posts = {"n": 0}

    def _post(_client, url, **kw):
        posts["n"] += 1
        req = httpx.Request("POST", url)
        return httpx.Response(200, text='{"choices":[{"message":{"content":"{}"}}]}',
                              request=req)

    _seam(monkeypatch, _post)
    _starved(monkeypatch)
    rl.reset_rate_limiter()
    monkeypatch.setenv("FL_LLM_DEFAULT_LANE", lane.LANE_BACKGROUND)

    rl._llm_circuit_breaker.reset()
    try:
        # burn the single burst token, then the next call has no capacity within the cap
        rl.call_llm_with_retry_sync(messages=[{"role": "user", "content": "a"}],
                                    model="m", user_id="u", operation="EXTRACT",
                                    max_retries=1, timeout=0.5)
        first = posts["n"]
        rl.call_llm_with_retry_sync(messages=[{"role": "user", "content": "b"}],
                                    model="m", user_id="u", operation="EXTRACT",
                                    max_retries=1, timeout=0.5)
    finally:
        rl._llm_circuit_breaker.reset()

    assert first == 1
    assert posts["n"] == 1, (
        "a BACKGROUND call with no capacity must not reach the wire — it deferred in the "
        "log but still sent the request, which is exactly the prod flood"
    )


def test_interactive_work_still_fails_open(monkeypatch):
    """The inverse guard: a user's request is never sacrificed to pacing.

    If this ever goes red, the fail-open was weakened for interactive traffic — that is a
    regression, not a fix.
    """
    posts = {"n": 0}

    def _post(_client, url, **kw):
        posts["n"] += 1
        req = httpx.Request("POST", url)
        return httpx.Response(200, text='{"choices":[{"message":{"content":"{}"}}]}',
                              request=req)

    _seam(monkeypatch, _post)
    _starved(monkeypatch)
    rl.reset_rate_limiter()
    monkeypatch.setenv("FL_LLM_DEFAULT_LANE", lane.LANE_INTERACTIVE)

    rl._llm_circuit_breaker.reset()
    try:
        for _ in range(3):
            rl.call_llm_with_retry_sync(messages=[{"role": "user", "content": "x"}],
                                        model="m", user_id="u", operation="EXTRACT",
                                        max_retries=1, timeout=0.5)
    finally:
        rl._llm_circuit_breaker.reset()

    assert posts["n"] == 3, "interactive calls must still proceed when the limiter wedges"


# ── D4: "never asked" is not "answered nothing" ──────────────────────────────────────

def test_deferred_background_call_raises_unavailable_when_asked_to(monkeypatch):
    def _post(_client, url, **kw):  # pragma: no cover - must never be reached
        raise AssertionError("a deferred call must not reach the wire")

    _seam(monkeypatch, _post)
    _starved(monkeypatch)
    rl.reset_rate_limiter()
    monkeypatch.setenv("FL_LLM_DEFAULT_LANE", lane.LANE_BACKGROUND)

    # Drain the burst token from the bucket the CALL PATH actually resolves. The key has
    # been BRAIN-scoped (`brain:<ident>`, the house-account-leak fix, 11701e42) since this
    # test was written: it drained a stale `account:acct-budget` bucket the live path never
    # consults, the real bucket was still full, the call fired into the never-reach
    # responder, and the test failed as `retries_exhausted` instead of `rate_deferred`.
    # Deriving the key here keeps the test honest across key-scheme changes — the
    # assertion below is about the DEFER CONTRACT, not the key spelling.
    _key = rl._resolve_rate_limit_key()
    assert _key, "rate-limit key must resolve under the seam's tenant cfg"
    bucket = rl._get_rate_bucket(_key, 1.0 / 60.0, None, "")
    bucket.tokens = 0.0

    rl._llm_circuit_breaker.reset()
    try:
        with pytest.raises(rl.LLMUnavailable) as ei:
            rl.call_llm_with_retry_sync(messages=[{"role": "user", "content": "z"}],
                                        model="m", user_id="u", operation="CLASSIFY_CHAIN",
                                        max_retries=1, timeout=0.5,
                                        raise_on_unavailable=True)
    finally:
        rl._llm_circuit_breaker.reset()
    assert ei.value.reason == "rate_deferred"


def test_default_callers_are_unchanged_by_the_new_flag(monkeypatch):
    """raise_on_unavailable defaults False → every existing caller keeps its contract."""
    def _post(_client, url, **kw):
        raise httpx.ConnectError("down")

    _seam(monkeypatch, _post)
    rl._llm_circuit_breaker.reset()
    try:
        # historical behaviour: the transport error is raised, NOT LLMUnavailable
        with pytest.raises(httpx.ConnectError):
            rl.call_llm_with_retry_sync(messages=[{"role": "user", "content": "q"}],
                                        model="m", user_id="u", operation="EXTRACT",
                                        max_retries=1, timeout=0.1)
    finally:
        rl._llm_circuit_breaker.reset()


# ── D1: one budget for the whole ACCOUNT, across processes ───────────────────────────

def _scratch():
    url = os.environ.get("FLTEST_REDIS_URL", "")
    if not url:
        pytest.skip("no FLTEST_REDIS_URL — cross-process budget test skipped")
    return url


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("REDIS_URL", _scratch())
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_RATE_BUDGET", "true")
    monkeypatch.setenv("REDIS_COORD_SOCKET_TIMEOUT", "2")
    RC.reset_client()
    if RC.client() is None:
        pytest.skip("scratch Redis not reachable")
    yield
    RC.reset_client()


def test_two_processes_cannot_each_spend_the_accounts_whole_budget(live):
    """THE PROD BUG, reproduced: uvicorn and the re_embedder as two independent buckets.

    Both start full (a cold bucket is never penalised), so with per-process accounting the
    account emits TWO requests where its budget allows ONE. On prod that is the difference
    between 5 RPM and 10 RPM against a ceiling of 5.
    """
    scope = RC.scope_for_account("acct-two-procs")
    RC.purge_scope(scope)

    rate, capacity = 1.0 / 60.0, 1.0     # one token per minute, burst 1

    proc_a = rl._TokenBucket(rate, capacity)
    proc_b = rl._TokenBucket(rate, capacity)
    proc_a.coord_scope = scope
    proc_b.coord_scope = scope

    got_a, _ = proc_a._try_acquire()
    got_b, wait_b = proc_b._try_acquire()

    assert got_a is True, "the first process should get the account's only token"
    assert got_b is False, (
        "the SECOND process spent a token the account did not have — this is the "
        "per-process under-count that produced the prod 429 storm"
    )
    assert wait_b > 0.0, "a denied acquire must report how long until capacity exists"
    RC.purge_scope(scope)


def test_without_the_shared_budget_both_processes_still_double_spend(live, monkeypatch):
    """TEETH CHECK — the discriminator is the shared budget, not the assertion's shape.

    With REDIS_COORD_RATE_BUDGET off, this reproduces the prod defect exactly: two fresh
    per-process buckets each hand out the account's "only" token. If this test ever goes
    RED, the fallback path stopped behaving like the historical code and the comparison
    above no longer proves anything.
    """
    monkeypatch.setenv("REDIS_COORD_RATE_BUDGET", "false")
    scope = RC.scope_for_account("acct-two-procs-off")
    RC.purge_scope(scope)

    proc_a = rl._TokenBucket(1.0 / 60.0, 1.0)
    proc_b = rl._TokenBucket(1.0 / 60.0, 1.0)
    proc_a.coord_scope = scope
    proc_b.coord_scope = scope

    assert proc_a._try_acquire()[0] is True
    assert proc_b._try_acquire()[0] is True, (
        "historical behaviour: per-process buckets double-spend. This is the bug the "
        "shared budget removes — it must still be reproducible with the flag off."
    )
    RC.purge_scope(scope)


def test_shared_budget_failure_falls_back_to_local_pacing(monkeypatch):
    """Redis down must degrade to today's in-process behaviour, never to an outage."""
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_RATE_BUDGET", "true")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")   # nothing listening
    RC.reset_client()
    try:
        b = rl._TokenBucket(1.0, 1.0)
        b.coord_scope = RC.scope_for_account("acct-no-redis")
        assert b._shared_budget_wait() is None, "no shared opinion when Redis is unreachable"
        assert b._try_acquire()[0] is True, "must fall back to the local bucket, not deny"
    finally:
        RC.reset_client()


def test_an_unscopable_key_never_builds_an_unscoped_redis_key():
    """redis_coord's hard rule: no key without a validated Scope."""
    assert rl._coord_scope_for_rate_key("") is None
    assert rl._coord_scope_for_rate_key("garbage-no-prefix") is None
    assert rl._coord_scope_for_rate_key("account:") is None
    assert rl._coord_scope_for_rate_key("account:acct-ok") is not None
