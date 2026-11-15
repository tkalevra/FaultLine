"""Respectful account-wide client-side LLM rate limiter — pacing + isolation + fail-safe proof.

The limiter lives at the centralized LLM call chokepoint (src/api/llm_calls.py) and PACES
outbound calls per-ACCOUNT so we never burst past a metered provider's published rate
(e.g. dashscope qwen-turbo: 600 RPM PER ALIBABA-CLOUD-ACCOUNT, shared by every seat).

Coverage:
  (a) N concurrent calls to ONE account key are PACED to ~RATE/sec (token-bucket spacing).
  (b) two DIFFERENT account keys are INDEPENDENT — a saturated key never delays another.
  (c) DISABLED (LLM_RATE_LIMIT_ENABLED=0) → no delay / no-op.
  (d) FAIL-SAFE when no key is resolvable → no-op (never lose a call), no raise.
  (e) fail-safe when the key source raises → gate swallows and proceeds.
  (f) the wedged fail-open cap (LLM_RATE_MAX_WAIT_S) proceeds instead of hanging forever.

These use a LOW rate + small burst and assert wall-clock spacing (real time.monotonic()),
per the spec option "a low RATE + assert spacing". No live LLM / network is touched — the
tests drive the gate functions directly.

Run: python3 -m pytest tests/test_llm_rate_limiter.py -q
"""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

import src.api.llm_calls as rl


@pytest.fixture(autouse=True)
def _clean_rate_env(monkeypatch):
    """Each test starts from a clean bucket registry and a known-off SaaS key source.

    Default: no tenant cfg (→ url key from LLM_BASE_URL), limiter ENABLED. Individual
    tests override env/key-source as needed. Buckets are cleared before AND after so a
    rate captured at creation never bleeds across tests.
    """
    monkeypatch.setattr(rl, "_current_llm_config_or_none", lambda: None)
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("LLM_BASE_URL", "http://test-endpoint:1234")
    # generous fail-safe cap so it never trips in the pacing tests
    monkeypatch.setenv("LLM_RATE_MAX_WAIT_S", "30")
    rl.reset_rate_limiter()
    yield
    rl.reset_rate_limiter()


# ── (a) pacing: concurrent calls to one key are spaced to ~RATE/sec ──────────────────

def test_sync_paces_concurrent_calls_to_one_account(monkeypatch):
    # rate = 20 tokens/sec (0.05s spacing), burst = 1 → only the first is free.
    monkeypatch.setenv("LLM_MAX_RPM", str(20 * 60))
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    rl.reset_rate_limiter()

    n = 5
    start = time.monotonic()
    for _ in range(n):
        rl.apply_rate_limit_sync("EXTRACTION")
    elapsed = time.monotonic() - start

    # 5 calls, burst 1: 4 must wait ~0.05s each → >= ~0.2s. Allow slack for scheduler.
    assert elapsed >= 0.15, f"expected pacing >= 0.15s for {n} calls, got {elapsed:.3f}s"
    # sanity: not absurdly slow
    assert elapsed < 5.0


def test_async_paces_concurrent_tasks_to_one_account(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RPM", str(20 * 60))  # 20/sec
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    rl.reset_rate_limiter()

    async def _run():
        start = time.monotonic()
        await asyncio.gather(*(rl.apply_rate_limit_async("EXTRACTION") for _ in range(5)))
        return time.monotonic() - start

    elapsed = asyncio.run(_run())
    assert elapsed >= 0.15, f"expected async pacing >= 0.15s, got {elapsed:.3f}s"
    assert elapsed < 5.0


# ── (b) independence: two different account keys don't throttle each other ───────────

def test_two_accounts_are_independent(monkeypatch):
    # Very slow rate + burst 1 so a second acquire on the SAME key is expensive.
    monkeypatch.setenv("LLM_MAX_RPM", str(2 * 60))  # 2/sec → 0.5s spacing
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    rl.reset_rate_limiter()

    # Two DISTINCT brains (different endpoints) → distinct brain-scoped buckets. (Under the old
    # account-keyed model these would key off account_id alone; under brain-scoping they key off
    # the resolved brain, so two different endpoints are independent even on one account.)
    cfg_a = {"account_id": "acct", "brain_scope": "account",
             "base_url": "http://brain-a:8080/v1", "model": "m", "api_key": "k"}
    cfg_b = {"account_id": "acct", "brain_scope": "account",
             "base_url": "http://brain-b:8080/v1", "model": "m", "api_key": "k"}
    _cfg = cfg_a
    monkeypatch.setattr(rl, "_current_llm_config_or_none", lambda: _cfg)

    # Brain A: drain its single burst token (free), then a second acquire would block ~0.5s.
    rl.apply_rate_limit_sync("EXTRACTION")  # free (burst)

    t0 = time.monotonic()
    rl.apply_rate_limit_sync("EXTRACTION")  # A must now wait ~0.5s
    a_wait = time.monotonic() - t0

    # Brain B: first acquire is free (independent full bucket) → near-instant.
    _cfg = cfg_b
    t1 = time.monotonic()
    rl.apply_rate_limit_sync("EXTRACTION")
    b_wait = time.monotonic() - t1

    assert a_wait >= 0.3, f"brain A second call should have paced, got {a_wait:.3f}s"
    assert b_wait < 0.1, f"brain B (independent) should be instant, got {b_wait:.3f}s"
    # distinct brain-scoped buckets exist (resolved, not hardcoded account: strings)
    _cfg = cfg_a
    key_a = rl._resolve_rate_limit_key()
    _cfg = cfg_b
    key_b = rl._resolve_rate_limit_key()
    assert key_a != key_b, "two distinct brains must resolve to two distinct keys"
    assert key_a in rl._rate_buckets and key_b in rl._rate_buckets


def test_one_brain_shared_across_seats(monkeypatch):
    """All seats of ONE corporate account share ONE brain → ONE bucket (the scoping rule,
    spec §0). Renamed from test_account_key_shared_across_seats: the correct invariant is
    BRAIN-scoped sharing (same resolved brain ⇒ same bucket), not account-row sharing."""
    monkeypatch.setenv("LLM_MAX_RPM", str(10 * 60))
    monkeypatch.setenv("LLM_RATE_BURST", "3")
    rl.reset_rate_limiter()
    # Two seats resolving the SAME corporate brain cfg → same brain scope → same key.
    shared = {"account_id": "acct-shared", "brain_scope": "account",
              "base_url": "http://shared-brain:8080/v1", "model": "m", "api_key": "k"}
    monkeypatch.setattr(rl, "_current_llm_config_or_none", lambda: shared)
    rl.apply_rate_limit_sync("EXTRACTION")
    rl.apply_rate_limit_sync("EXTRACTION")
    assert len(rl._rate_buckets) == 1, \
        "two seats on one shared brain must share exactly one bucket"
    [k] = rl._rate_buckets.keys()
    assert k.startswith("brain:"), f"brain-scoped key expected, got {k!r}"


# ── (c) disabled → no-op, no delay ───────────────────────────────────────────────────

def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "0")
    monkeypatch.setenv("LLM_MAX_RPM", "1")   # 1/sec — would be very slow IF enabled
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    rl.reset_rate_limiter()

    start = time.monotonic()
    for _ in range(10):
        rl.apply_rate_limit_sync("EXTRACTION")
    elapsed = time.monotonic() - start

    assert elapsed < 0.05, f"disabled limiter must not delay, got {elapsed:.3f}s"
    # no buckets ever created when disabled
    assert rl._rate_buckets == {}


def test_disabled_async_is_noop(monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "off")
    monkeypatch.setenv("LLM_MAX_RPM", "1")
    rl.reset_rate_limiter()

    async def _run():
        start = time.monotonic()
        await asyncio.gather(*(rl.apply_rate_limit_async("X") for _ in range(10)))
        return time.monotonic() - start

    assert asyncio.run(_run()) < 0.05
    assert rl._rate_buckets == {}


# ── (d) fail-safe: no resolvable key → no-op ────────────────────────────────────────

def test_no_key_resolvable_is_noop(monkeypatch):
    # No tenant cfg AND no LLM_BASE_URL AND endpoint list empty → key None → no-op.
    monkeypatch.setattr(rl, "_current_llm_config_or_none", lambda: None)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr(rl, "_get_endpoint_list", lambda: [])
    monkeypatch.setenv("LLM_MAX_RPM", "1")
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    rl.reset_rate_limiter()

    assert rl._resolve_rate_limit_key() is None
    start = time.monotonic()
    for _ in range(5):
        rl.apply_rate_limit_sync("EXTRACTION")  # must not raise, must not delay
    elapsed = time.monotonic() - start
    assert elapsed < 0.05
    assert rl._rate_buckets == {}


def test_key_source_error_fails_open(monkeypatch):
    """If the key source itself raises, the gate swallows it and proceeds (never lose a call)."""
    def _boom():
        raise RuntimeError("saas import blew up")
    # _current_llm_config_or_none already swallows internally, so raise from the resolver.
    monkeypatch.setattr(rl, "_resolve_rate_limit_key", _boom)
    rl.reset_rate_limiter()
    # Must not raise.
    rl.apply_rate_limit_sync("EXTRACTION")
    asyncio.run(rl.apply_rate_limit_async("EXTRACTION"))


# ── (e) endpoint fallback key derivation ─────────────────────────────────────────────

def test_key_falls_back_to_endpoint_list(monkeypatch):
    monkeypatch.setattr(rl, "_current_llm_config_or_none", lambda: None)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr(rl, "_get_endpoint_list", lambda: ["http://legacy:9/v1/chat/completions"])
    assert rl._resolve_rate_limit_key() == "url:http://legacy:9/v1/chat/completions"


def test_brain_scope_preferred_over_env_url(monkeypatch):
    """A resolved brain cfg yields a brain-scoped key (preferred over the env url fallback).

    Renamed from test_key_prefers_account_over_url: the keying follows the BRAIN (spec §0/§1),
    not the account row, so a full brain cfg resolves to ``brain:<ident>``.
    """
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-1", "brain_scope": "account",
                                 "base_url": "http://brain:1/v1", "model": "m", "api_key": "k"})
    key = rl._resolve_rate_limit_key()
    assert key.startswith("brain:"), f"resolved brain cfg must yield a brain: key, got {key!r}"


def test_incomplete_cfg_falls_back_to_env_url(monkeypatch):
    """A cfg WITHOUT enough fields to identify a brain (no owner) → env url fallback.

    Renamed from test_key_uses_base_url_when_no_account: under brain-scoping, base_url alone
    does not identify a brain (an owner + base_url + model + api_key does). An incomplete cfg
    therefore falls through to the env url key, same as the FOSS / unbound path.
    """
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"base_url": "http://tenant-brain:8080/"})
    assert rl._resolve_rate_limit_key() == "url:http://test-endpoint:1234"


# ── (f) wedged fail-open cap: proceed instead of hanging forever ─────────────────────

def test_wedged_failopen_bounded(monkeypatch):
    # Impossibly slow rate + tiny max-wait → the bucket can never yield a token in time,
    # but the acquire must RETURN (proceed) after ~max_wait, never hang.
    monkeypatch.setenv("LLM_MAX_RPM", "0.06")   # 0.001/sec — effectively never refills
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    monkeypatch.setenv("LLM_RATE_MAX_WAIT_S", "0.2")
    rl.reset_rate_limiter()

    # Drain the single burst token.
    rl.apply_rate_limit_sync("EXTRACTION")

    start = time.monotonic()
    rl.apply_rate_limit_sync("EXTRACTION")  # would block forever; must fail-open at ~0.2s
    elapsed = time.monotonic() - start

    assert 0.15 <= elapsed < 1.0, f"wedged limiter should fail-open near max_wait, got {elapsed:.3f}s"


# ── (g) endpoint-driven published-limits registry + HARD CLAMP at provider max ───────

def test_registry_resolves_dashscope_qwen_turbo():
    lim = rl.resolve_provider_limits("https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                                     "qwen-turbo")
    assert lim["known"] is True
    assert lim["rpm"] == 600
    assert lim["tpm"] == 5_000_000


def test_registry_unknown_host_is_not_clamped():
    lim = rl.resolve_provider_limits("http://192.0.2.20:1234/v1", "qwen3.5-9b")
    assert lim["known"] is False  # self-hosted / LAN → no hard clamp


def test_clamp_caps_configured_rate_at_provider_max(monkeypatch):
    # Operator misconfigures a HUGE rate against a 600-RPM provider → must clamp down.
    monkeypatch.setenv("LLM_MAX_RPM", "100000")   # absurd: ~1666/sec configured
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    rate, limits = rl._effective_rate_per_sec(
        "https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo")
    # floor(600 * 0.9) / 60 = 540/60 = 9.0 tokens/sec — comfortably under 600 RPM (10/s).
    assert rate == pytest.approx(9.0, abs=1e-6)
    assert limits["known"] is True
    # Structurally impossible to exceed provider max: effective RPM (=rate*60) <= 600.
    assert rate * 60 <= 600


def test_clamp_keeps_lower_configured_rate(monkeypatch):
    # If configured is BELOW the provider cap, configured wins (we don't speed up to the cap).
    monkeypatch.setenv("LLM_MAX_RPM", str(120))   # 2/sec configured
    rate, limits = rl._effective_rate_per_sec(
        "https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo")
    assert rate == pytest.approx(2.0, abs=1e-6)   # min(2/s, 9/s) = 2/s


def test_unknown_host_keeps_configured_rate(monkeypatch):
    # Self-hosted endpoint: no clamp — the operator's configured pace is honored as-is.
    monkeypatch.setenv("LLM_MAX_RPM", str(1200))  # 20/sec
    rate, limits = rl._effective_rate_per_sec("http://truenas:1234/v1", "qwen3.5-36b")
    assert rate == pytest.approx(20.0, abs=1e-6)
    assert limits["known"] is False


def test_clamp_enforced_through_bucket_pacing(monkeypatch):
    # End-to-end: even with an absurd configured rate, pacing against a known 600-RPM
    # provider is spaced by the CLAMPED 9/sec (≈0.111s), proving the bucket honors the cap.
    monkeypatch.setenv("LLM_MAX_RPM", "100000")
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "bench",
                                 "base_url": "https://dashscope-intl.aliyuncs.com/v1",
                                 "model": "qwen-turbo"})
    rl.reset_rate_limiter()

    n = 4
    start = time.monotonic()
    for _ in range(n):
        rl.apply_rate_limit_sync("EXTRACTION")
    elapsed = time.monotonic() - start
    # 3 waits at ~1/9s ≈ 0.33s. Assert it clearly paced (would be ~0 at the absurd rate).
    assert elapsed >= 0.2, f"clamp must pace at ~9/s, got {elapsed:.3f}s for {n} calls"
    [k] = rl._rate_buckets.keys()
    assert rl._rate_buckets[k].rate == pytest.approx(9.0, abs=1e-6)


# ── (h) LIVE CLAMP RE-SYNC: a brain/endpoint switch must re-apply the provider clamp ──

def test_bucket_resync_applies_provider_clamp_after_endpoint_switch(monkeypatch):
    """The PROD 429 bug (2026-08-08): a bucket created against an UNKNOWN host freezes
    the old no-clamp rate forever — the SAME account switching to a metered provider must
    re-clamp in place, with NO restart and NO new key."""
    # First: brain on an UNKNOWN host (homelab) at the configured pace 480 RPM = 8/s.
    monkeypatch.setenv("LLM_MAX_RPM", "480")
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    cfg = {"account_id": "acct-hdp",
           "base_url": "http://homelab:8080/v1", "model": "gemma-4-31b"}
    monkeypatch.setattr(rl, "_current_llm_config_or_none", lambda: cfg)
    rl.reset_rate_limiter()

    rl.apply_rate_limit_sync("EXTRACTION")  # creates the account bucket at the UNKNOWN rate
    bucket = rl._rate_buckets[rl._resolve_rate_limit_key()]
    assert bucket.rate == pytest.approx(8.0, abs=1e-6)
    assert bucket.capacity == pytest.approx(1.0, abs=1e-6)

    # The tenant now switches to api.cerebras.ai (KNOWN, gemma → published 300 RPM).
    cfg["base_url"] = "https://api.cerebras.ai/v1"
    rl.apply_rate_limit_sync("EXTRACTION")

    # Clamp = min(8/s, floor(300 * 0.9)/60 = 4.5/s) = 4.5/s — on the SAME bucket key.
    bucket = rl._rate_buckets[rl._resolve_rate_limit_key()]
    assert bucket.rate == pytest.approx(4.5, abs=1e-6), \
        "stale-clamp bug: the Cerebras provider limit never applied after the switch"

    # And it ACTUALLY paces at the tighter clamp: the switch drained the burst token,
    # so the next acquire must wait ~1/4.5s (≈0.22s), not ~1/8s (0.125s).
    start = time.monotonic()
    rl.apply_rate_limit_sync("EXTRACTION")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.18, f"resynced bucket must pace at 4.5/s, got {elapsed:.3f}s"


def test_resync_clamps_tokens_to_new_capacity(monkeypatch):
    """A live re-clamp must never mint free burst: tokens stay ≤ the new capacity, and a
    no-change resync is a no-op (returns False)."""
    monkeypatch.setenv("LLM_MAX_RPM", str(2 * 60))  # 2/s
    monkeypatch.setenv("LLM_RATE_BURST", "1")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "A", "brain_scope": "account",
                                 "base_url": "http://x:1/v1", "model": "m", "api_key": "k"})
    rl.reset_rate_limiter()

    rl.apply_rate_limit_sync("EXTRACTION")  # drain the single burst token
    b = rl._rate_buckets[rl._resolve_rate_limit_key()]
    assert b.tokens == 0.0

    # Resync to a HIGHER rate with a bigger capacity → tokens must NOT jump up.
    assert b.resync_rate(10.0, 3.0) is True
    assert b.tokens == 0.0
    assert b.rate == pytest.approx(10.0, abs=1e-9)
    assert b.capacity == pytest.approx(3.0, abs=1e-9)

    # Resync to a LOWER capacity clamps the existing balance down.
    b.tokens = 2.5
    assert b.resync_rate(1.0, 1.0) is True
    assert b.tokens == pytest.approx(1.0, abs=1e-9)

    # Identical rate+capacity → no change reported.
    assert b.resync_rate(1.0, 1.0) is False


# ── (i) Retry-After honoring: the provider's own window drives the backoff ──────────

def _rate_limited_error(headers):
    req = httpx.Request("POST", "https://api.cerebras.ai/v1/chat/completions")
    resp = httpx.Response(429, headers=headers,
                          text='{"error":{"code":"request_quota_exceeded"}}', request=req)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:  # carries .response.headers for _retry_after_seconds
        return exc
    raise AssertionError("429 response should have raised")


def test_retry_after_seconds_parses_provider_retry_after():
    exc = _rate_limited_error({"retry-after": "3"})
    assert rl._retry_after_seconds(exc) == pytest.approx(3.0, abs=1e-9)


def test_retry_after_seconds_parses_epoch_reset_seconds():
    # Cerebras-documented x-ratelimit-reset-requests: a UTC timestamp when the window
    # replenishes. Epoch-seconds (well past 1e9) → seconds-until-reset.
    future = int(time.time()) + 7
    exc = _rate_limited_error({"x-ratelimit-reset-requests": str(future)})
    assert rl._retry_after_seconds(exc) == pytest.approx(7.0, abs=1.0)


def test_retry_after_seconds_parses_offset_reset():
    # A small value is treated as seconds-until-reset (offset form).
    exc = _rate_limited_error({"x-ratelimit-reset": "5"})
    assert rl._retry_after_seconds(exc) == pytest.approx(5.0, abs=1e-9)


def test_retry_after_seconds_missing_header_returns_none():
    assert rl._retry_after_seconds(_rate_limited_error({})) is None


def test_retry_after_seconds_malformed_header_returns_none():
    assert rl._retry_after_seconds(_rate_limited_error({"retry-after": "not-a-number"})) is None


def test_retry_after_seconds_non_http_error_returns_none():
    assert rl._retry_after_seconds(ConnectionError("boom")) is None


def test_retry_after_honored_through_real_sync_retry_seam(monkeypatch):
    """The retry loop must wait out the provider's Retry-After (≥3s here), NOT its own
    1s/2s exponential backoff — driving the REAL call_llm_with_retry_sync entry point."""
    monkeypatch.setenv("LLM_CONTEXT_GUARD", "false")   # isolate the rate limiter
    monkeypatch.setenv("REDIS_COORD", "false")
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setattr(rl, "_get_endpoint_list",
                        lambda: ["https://api.cerebras.ai/v1/chat/completions"])
    monkeypatch.setattr(rl, "_saas_brain_unavailable", lambda: False)
    monkeypatch.setattr(rl, "_tenant_egress_blocked", lambda: False)
    monkeypatch.setattr(rl, "_health_note", lambda *a, **k: None)

    sleeps = []
    monkeypatch.setattr(rl.time, "sleep", lambda s: sleeps.append(s))

    def _post(_client, url, **kw):
        req = httpx.Request("POST", url)
        return httpx.Response(429, headers={"retry-after": "3"},
                              text='{"error":{}}', request=req)

    monkeypatch.setattr(rl.llm_source_ip, "post_sync", _post)
    rl._llm_circuit_breaker.reset()

    with pytest.raises(httpx.HTTPStatusError):
        rl.call_llm_with_retry_sync(
            messages=[{"role": "user", "content": "hello"}],
            model="gemma-4-31b", user_id="u1", operation="EXTRACT",
            max_retries=2, timeout=1.0,
        )
    assert sleeps, "expected at least one backoff sleep"
    assert sleeps[-1] >= 3.0, f"backoff must honor Retry-After=3, slept {sleeps[-1]}s"
    rl._llm_circuit_breaker.reset()


# ── (j) LAYER 2 — adaptive header learning: re-pace from provider x-ratelimit-* ──────

def _success_response(headers, endpoint="https://api.cerebras.ai/v1/chat/completions"):
    """A 200 httpx.Response carrying arbitrary response headers (the learner's input)."""
    req = httpx.Request("POST", endpoint)
    resp = httpx.Response(200, headers=headers,
                          text='{"choices":[{"message":{"content":"{}"}}]}', request=req)
    resp.raise_for_status()
    return resp


def _seed_bucket(rate=8.0):
    # Brain-scoped keying (DEV/DESIGN-brain-budget-event-cache.md §1): the bucket key follows
    # the resolved BRAIN, not the account row. Resolve it from the cfg the test has already
    # monkeypatched so the seeded bucket matches what _observe_rate_headers / apply_rate_limit_sync
    # look up. (Earlier form hardcoded "account:acct-hdr", which the brain re-key made stale.)
    key = rl._resolve_rate_limit_key()
    if key is None:
        key = "test:fallback"  # defensive — a brain cfg resolves a key in every learner test
    rl._rate_buckets[key] = rl._TokenBucket(rate, rl._rate_burst_capacity(rate))
    return key, rl._rate_buckets[key]


def _capture_log(monkeypatch):
    """Record structlog .info events emitted by the learner (to assert 'bucket_learned')."""
    events = []
    original = rl.log.info

    def _info(event, **kw):
        events.append((event, kw))
        original(event, **kw)

    monkeypatch.setattr(rl.log, "info", _info)
    return events


def test_observe_learns_cerebras_free_trial_remaining(monkeypatch):
    """A Free-Trial key (5 RPM) reports remaining=4 with a ~60s window → the bucket
    tightens from the 300-RPM clamp down to the real ~3.6 RPM the provider is granting."""
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "https://api.cerebras.ai/v1",
                                 "model": "gemma-4-31b"})
    monkeypatch.setattr(rl.time, "time", lambda: 1_700_000_000.0)  # pin reset math
    key, bucket = _seed_bucket(rate=4.5)  # floor(300 * 0.9) / 60 — the published clamp
    reset_epoch_s = int(1_700_000_000.0) + 60

    rl._observe_rate_headers(_success_response({
        "x-ratelimit-limit-requests": "5",
        "x-ratelimit-remaining-requests": "4",
        "x-ratelimit-reset-requests": str(reset_epoch_s),
    }))

    # learned = 4/60*60*0.9 = 3.6 RPM = 0.06 rps → target = min(4.5, 0.06) = 0.06.
    assert bucket.rate == pytest.approx(0.06, abs=1e-6), \
        "header learner must tighten the bucket toward the provider's real tier"
    assert bucket.pause_until == 0.0  # remaining=4 is NOT exhausted → no pause


def test_observe_never_loosens_beyond_effective_clamp(monkeypatch):
    """Even a header claim of a huge budget must never pace faster than the operator's
    configured rate (the effective clamp is the ceiling the learner tightens into)."""
    monkeypatch.setenv("LLM_MAX_RPM", "120")
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "http://192.0.2.20:8080/v1",
                                 "model": "qwen3.5-9b"})
    key, bucket = _seed_bucket(rate=2.0)  # 120 RPM / 60
    reset_epoch_s = int(time.time()) + 1

    rl._observe_rate_headers(_success_response({
        "x-ratelimit-limit-requests": "10000",
        "x-ratelimit-remaining-requests": "9999",
        "x-ratelimit-reset-requests": str(reset_epoch_s),
    }))

    # learned ≈ 9999/1*60*0.9 ≈ huge → target = min(2.0, huge) = 2.0 (unchanged).
    assert bucket.rate == pytest.approx(2.0, abs=1e-6)


def test_observe_recovers_after_tier_improvement(monkeypatch):
    """A bucket tightened to Free-Trial pace must recover UP once the key's tier improves
    (higher remaining on a full window) — bounded by the current effective clamp."""
    monkeypatch.setenv("LLM_MAX_RPM", "480")
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "https://api.cerebras.ai/v1",
                                 "model": "gemma-4-31b"})
    monkeypatch.setattr(rl.time, "time", lambda: 1_700_000_000.0)  # pin reset math
    key, bucket = _seed_bucket(rate=0.083)  # tightened by an earlier Free-Trial window (5 RPM)
    reset_epoch_s = int(1_700_000_000.0) + 60

    rl._observe_rate_headers(_success_response({
        "x-ratelimit-limit-requests": "300",
        "x-ratelimit-remaining-requests": "299",
        "x-ratelimit-reset-requests": str(reset_epoch_s),
    }))

    # learned = 299/60*60*0.9 = 269.1 RPM = 4.485 rps → target = min(4.5, 4.485) = 4.485.
    assert bucket.rate == pytest.approx(4.485, abs=1e-6), \
        "header learner must recover when the provider's actual tier allows it"
    assert bucket.rate <= 4.5 + 1e-9  # never above the effective clamp


def test_observe_pauses_until_reset_when_budget_exhausted(monkeypatch):
    """remaining <= 1 → HARD PAUSE: the bucket hands out no tokens until the window reset."""
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "https://api.cerebras.ai/v1",
                                 "model": "gemma-4-31b"})
    monkeypatch.setattr(rl.time, "time", lambda: 1_700_000_000.0)  # pin reset math
    key, bucket = _seed_bucket()
    reset_epoch_s = int(1_700_000_000.0) + 2

    rl._observe_rate_headers(_success_response({
        "x-ratelimit-remaining-requests": "1",
        "x-ratelimit-reset-requests": str(reset_epoch_s),
    }))
    assert bucket.pause_until > time.monotonic(), "bucket must be paused by the learner"

    # A subsequent acquire through the real gate must wait out the reset (~2s), not skip it.
    start = time.monotonic()
    rl.apply_rate_limit_sync("EXTRACTION")
    waited = time.monotonic() - start
    assert waited >= 1.5, f"paused bucket must wait out the reset, got {waited:.3f}s"
    assert waited < 4.0
    assert bucket.pause_until == 0.0, "pause must clear once the window resets"


def test_observe_no_headers_noop(monkeypatch):
    """A self-hosted endpoint sends no rate-limit headers → nothing to learn, no change."""
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr", "base_url": "http://x:1/",
                                 "model": "m"})
    key, bucket = _seed_bucket(rate=2.0)
    rl._observe_rate_headers(_success_response({}))
    assert bucket.rate == pytest.approx(2.0, abs=1e-6)
    assert bucket.pause_until == 0.0


def test_observe_malformed_headers_noop(monkeypatch):
    """Garbage header values must not raise and must not change the bucket (fail-safe)."""
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "https://api.cerebras.ai/v1",
                                 "model": "gemma-4-31b"})
    key, bucket = _seed_bucket(rate=2.0)
    rl._observe_rate_headers(_success_response({
        "x-ratelimit-limit-requests": "garbage",
        "x-ratelimit-remaining-requests": "??",
        "x-ratelimit-reset-requests": "never",
    }))
    assert bucket.rate == pytest.approx(2.0, abs=1e-6)
    assert bucket.pause_until == 0.0


def test_parse_openrouter_bare_headers_returns_full_measurement():
    """OpenRouter (and some proxies) emit the UNSUFFIXED X-RateLimit-{Limit,Remaining,Reset}
    on a 429. The bare family must resolve these as a FULL measurement (limit+remaining+reset),
    not a reset-only partial — a partial previously no-oped the learner AND (parsed non-None)
    skipped the 429 fallback, stranding every OpenRouter free-tier 429."""
    parsed = rl._parse_rate_limit_headers({
        "x-ratelimit-limit": "20",
        "x-ratelimit-remaining": "5",
        "x-ratelimit-reset": "5",
    })
    assert parsed is not None
    assert parsed["source"] == "openai-compatible/bare"
    assert parsed["limit_requests"] == 20
    assert parsed["remaining_requests"] == 5
    assert parsed["reset_seconds"] is not None and parsed["reset_seconds"] > 0


def test_parse_lone_reset_returns_none_not_partial():
    """A LONE reset (no limit/remaining anywhere) is ambiguous and must return None so a 429
    reaches the fallback — not a non-None partial that no-ops the learner. This is the exact
    shape that stranded OpenRouter 429s: the bare reset matched the suffixed family's reset
    fallback tuple, yielded limit/remaining=None, and (parsed non-None) blocked the fallback.
    Symmetric for bare and suffixed reset-only."""
    assert rl._parse_rate_limit_headers({"x-ratelimit-reset": "5"}) is None
    assert rl._parse_rate_limit_headers({"x-ratelimit-reset-requests": "5"}) is None


def test_observe_learns_openrouter_bare_headers(monkeypatch):
    """End-to-end: an OpenRouter free-tier response (bare X-RateLimit-*, 20 RPM ceiling, 5
    remaining, ~60s window) tightens the bucket to the real ~4.5 RPM the provider is granting —
    the same learning path as suffixed OpenAI/Cerebras headers."""
    monkeypatch.setenv("LLM_MAX_RPM", "480")
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-or",
                                 "base_url": "https://openrouter.ai/api/v1",
                                 "model": "nvidia/nemotron-3-ultra-550b-a55b:free"})
    monkeypatch.setattr(rl.time, "time", lambda: 1_700_000_000.0)  # pin reset math
    key, bucket = _seed_bucket(rate=8.0)  # well above the free-tier ceiling
    reset_epoch_s = int(1_700_000_000.0) + 60

    rl._observe_rate_headers(_success_response({
        "x-ratelimit-limit": "20",
        "x-ratelimit-remaining": "5",
        "x-ratelimit-reset": str(reset_epoch_s),
    }))

    # learned = 5/60*60*0.9 = 4.5 RPM = 0.075 rps -> target = min(8.0, 0.075) = 0.075
    assert bucket.rate == pytest.approx(0.075, abs=1e-6), \
        "bare OpenRouter headers must tighten the bucket like suffixed ones"
    assert bucket.pause_until == 0.0  # remaining=5 is NOT exhausted -> no pause


def test_observe_bare_limit_only_429_routes_to_fallback(monkeypatch):
    """GAUNTLET CAUGHT: a 429 carrying a LONE bare x-ratelimit-limit (no reset, no remaining)
    parses non-None under the bare family but yields no learnable rate. It must still reach the
    published-table fallback (the unsafe direction is a 429 that fails to slow us down), not
    no-op. Pre-consolidation the bare family matched and the observer fell through to a bare
    `return` that swallowed the 429 — this pins that the no-learn exit routes a 429 onward."""
    monkeypatch.setenv("LLM_MAX_RPM", "480")
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-or",
                                 "base_url": "https://openrouter.ai/api/v1",
                                 "model": "nvidia/nemotron-3-ultra-550b-a55b:free"})
    key, bucket = _seed_bucket(rate=8.0)  # well above the free-tier ceiling

    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    resp = httpx.Response(429, headers={"x-ratelimit-limit": "20"},
                          text='{"error":{"message":"slow down"}}', request=req)
    rl._observe_rate_headers(resp)

    # OpenRouter free-tier table entry = 20 RPM -> candidate = 20*0.9/60 = 0.3 rps. A no-op
    # would leave bucket.rate at 8.0; the fallback must tighten it to ~0.3 on an ASSUMED floor.
    assert bucket.rate == pytest.approx(0.3, abs=1e-6), \
        "a lone-limit bare 429 must route to the published-table fallback, not no-op"
    assert bucket.floor_provenance in rl._ASSUMED_PROVENANCES


def test_header_learning_flag_disabled_noop(monkeypatch):
    """LLM_RATE_HEADER_LEARNING=0 → observation is a byte-for-byte no-op."""
    monkeypatch.setenv("LLM_RATE_HEADER_LEARNING", "0")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "https://api.cerebras.ai/v1",
                                 "model": "gemma-4-31b"})
    key, bucket = _seed_bucket(rate=8.0)
    reset_epoch_s = int(time.time()) + 60
    rl._observe_rate_headers(_success_response({
        "x-ratelimit-limit-requests": "5",
        "x-ratelimit-remaining-requests": "4",
        "x-ratelimit-reset-requests": str(reset_epoch_s),
    }))
    assert bucket.rate == pytest.approx(8.0, abs=1e-6)
    assert bucket.pause_until == 0.0


def test_header_learner_honored_through_real_sync_success_seam(monkeypatch):
    """End-to-end through call_llm_with_retry_sync: a success response carrying a
    Free-Trial budget resyncs the real account bucket to the learned rate."""
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    monkeypatch.setenv("LLM_CONTEXT_GUARD", "false")   # isolate the rate limiter
    monkeypatch.setenv("REDIS_COORD", "false")
    monkeypatch.setattr(rl, "_get_endpoint_list",
                        lambda: ["https://api.cerebras.ai/v1/chat/completions"])
    monkeypatch.setattr(rl, "_saas_brain_unavailable", lambda: False)
    monkeypatch.setattr(rl, "_tenant_egress_blocked", lambda: False)
    monkeypatch.setattr(rl, "_health_note", lambda *a, **k: None)
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "https://api.cerebras.ai/v1",
                                 "model": "gemma-4-31b"})
    monkeypatch.setattr(rl.time, "time", lambda: 1_700_000_000.0)  # pin reset math
    rl.reset_rate_limiter()

    def _post(_client, url, **kw):
        req = httpx.Request("POST", url)
        reset_epoch_s = int(1_700_000_000.0) + 60
        return httpx.Response(200, headers={
            "x-ratelimit-limit-requests": "5",
            "x-ratelimit-remaining-requests": "4",
            "x-ratelimit-reset-requests": str(reset_epoch_s),
        }, text='{"choices":[{"message":{"content":"{}"}}]}', request=req)

    monkeypatch.setattr(rl.llm_source_ip, "post_sync", _post)
    rl._llm_circuit_breaker.reset()
    try:
        result = rl.call_llm_with_retry_sync(
            messages=[{"role": "user", "content": "hello"}],
            model="gemma-4-31b", user_id="u1", operation="EXTRACT",
            max_retries=1, timeout=1.0,
        )
        assert result == {}
    finally:
        rl._llm_circuit_breaker.reset()

    bucket = rl._rate_buckets.get(rl._resolve_rate_limit_key())
    assert bucket is not None, "the success seam must have created the account bucket"
    # learned = 4/60*60*0.9 = 3.6 RPM → 0.06 rps; the seam must have applied it.
    assert bucket.rate == pytest.approx(0.06, abs=1e-4), \
        "the success seam must re-pace the bucket from the provider's headers"
    assert bucket.pause_until == 0.0


def test_compose_learn_then_acquire_keeps_learned_floor(monkeypatch):
    """COMPOSE (learn → acquire): the learn is applied on the success seam, then a SECOND
    acquire through apply_rate_limit_sync must KEEP the learned floor — NOT bounce back
    to the 4.5 rps registry clamp. This is the acquire-time resync floor path
    (_get_rate_bucket: target = min(effective_rate, learner_floor_rps)) — the DEFECT-1
    fix that makes the learner's rate survive the very next call."""
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "https://api.cerebras.ai/v1",
                                 "model": "gemma-4-31b"})
    # Seed the bucket at the REGISTRY clamp (gemma on api.cerebras.ai → 300 RPM).
    key, bucket = _seed_bucket(rate=4.5)

    # LEARN: suffixed fixed-window buckets, no reset header. PACE comes from the MINUTE
    # window only (owner-confirmed 2026-08-15): 5/min × 0.9 = 4.5 RPM = 0.075 rps, with
    # the minute allowance (5) as the BURST CAPACITY. The day window (2400) is a QUOTA —
    # published to the daily budget controller, never divided into the per-second rate
    # (that flattening was the 1-per-36s metronome: 0.025 rps with capacity floored to 1).
    real = _captured_evidence("cerebras-httpx-ua")["all_headers"]
    real = {k: v for k, v in real.items() if k.startswith("x-ratelimit")}
    rl._observe_rate_headers(_success_response(real))
    assert bucket.rate == pytest.approx(0.075, abs=1e-6), \
        "premise: the learn must have tightened the bucket to the minute-window pace"
    assert bucket.learner_floor_rps == pytest.approx(0.075, abs=1e-6)
    assert bucket.capacity == pytest.approx(5.0), \
        "the minute window's allowance IS the burst capacity"

    # SECOND ACQUIRE through the real gate — the acquire-time resync must respect the floor.
    rl.apply_rate_limit_sync("EXTRACTION")

    # If the acquire-time resync ignored the floor it would re-clamp toward the registry
    # effective rate (4.5 rps) and the learned rate would be dead on the very next call.
    assert bucket.rate == pytest.approx(0.075, abs=1e-6), \
        "the acquire-time resync must keep the learned floor, not the registry clamp"
    assert bucket.learner_floor_rps == pytest.approx(0.075, abs=1e-6)


# ── (k) P3 — SUFFIXED fixed-window buckets (Cerebras' real shape: -minute/-hour/-day, no reset) ──

def _captured_evidence(name):
    """Load a captured header set from DEV/ratelimit/evidence (the REAL provider responses)."""
    path = (Path(__file__).resolve().parent.parent / "DEV" / "ratelimit" / "evidence" / f"{name}.json")
    return json.loads(path.read_text())


def test_parse_real_cerebras_suffix_headers():
    """The REAL Cerebras success header set is SUFFIXED (x-ratelimit-*-requests-{minute,hour,
    day}) with NO reset header — the parser must recognize it as the 5-RPM Free-Trial bucket
    at a 60s window (the exact shape that used to return None and no-op the learner)."""
    captured = _captured_evidence("cerebras-httpx-ua")
    parsed = rl._parse_rate_limit_headers(captured["all_headers"])
    assert parsed is not None, "Cerebras' suffixed buckets must be recognized"
    assert parsed["source"] == "openai/cerebras"
    assert parsed["limit_requests"] == 5
    assert parsed["remaining_requests"] == 4
    assert parsed["window_seconds"] == 60
    assert parsed["reset_seconds"] is None  # Cerebras sends no reset header at all


def test_observe_learns_cerebras_suffix_bucket_no_reset(monkeypatch):
    """P3: with suffixed fixed-window buckets and NO reset header the learner must still
    re-pace — off the MINUTE-CLASS windows only (owner-confirmed 2026-08-15): 5/min × 0.9
    headroom = 4.5 RPM = 0.075 rps, with the minute allowance (5) as burst capacity. The
    DAY window (2400/day) is a QUOTA — it must NOT be flattened into the per-second rate
    (2400/86400 × 0.9 ≈ 0.025 rps with capacity floored to 1 was the metronome defect:
    stricter short-term than the provider's own policy, which allows all 5 minute-requests
    in the first second)."""
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "https://api.cerebras.ai/v1",
                                 "model": "gemma-4-31b"})
    # A HIGH effective rate (as if the published clamp had never applied) — the header
    # learn must be the binding constraint.
    monkeypatch.setattr(rl, "_effective_rate_per_sec",
                        lambda base_url, model: (8.0, {"source": "registry", "rpm": 480}))
    key, bucket = _seed_bucket(rate=8.0)
    events = _capture_log(monkeypatch)

    # REAL captured Cerebras success headers (all three suffixed buckets present).
    # Feed only the x-ratelimit-* axis: the captured set also carries content-encoding: br,
    # which httpx would try to brotli-decode against our plain-text body.
    real = _captured_evidence("cerebras-httpx-ua")["all_headers"]
    real = {k: v for k, v in real.items() if k.startswith("x-ratelimit")}
    rl._observe_rate_headers(_success_response(real))

    # pace = 5/min (the minute window) × 0.9 = 4.5 RPM = 0.075 rps — NOT the day flattening.
    assert bucket.rate == pytest.approx(0.075, abs=1e-6)
    assert bucket.rate != pytest.approx(0.025, abs=1e-4), \
        "the day-window flattening (the metronome) must be gone"
    assert bucket.capacity == pytest.approx(5.0), \
        "the minute window's allowance is the burst capacity — a 4-call turn proceeds as a unit"
    assert bucket.pause_until == 0.0  # remaining=4 is NOT exhausted → no pause
    assert any(e[0] == "llm_rate_limit.bucket_learned" for e in events), \
        "the learner must log bucket_learned when it re-paces the bucket"
    assert any(e[0] == "llm_rate_limit.day_cap_learned" for e in events), \
        "the day window's allowance (2400) must publish to the daily budget controller"


def test_observe_pauses_for_window_when_remaining_zero(monkeypatch):
    """P3: remaining=0 with a fixed 60s window and NO reset header → the learner HARD-PAUSES
    the bucket for the window length (the window replenishes in ~60s)."""
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "https://api.cerebras.ai/v1",
                                 "model": "gemma-4-31b"})
    key, bucket = _seed_bucket(rate=8.0)

    rl._observe_rate_headers(_success_response({
        "x-ratelimit-limit-requests-minute": "5",
        "x-ratelimit-remaining-requests-minute": "0",
    }))

    assert bucket.pause_until >= time.monotonic() + 59.0, \
        "an exhausted minute window must pause the bucket for the full window"


def test_retry_after_seconds_cerebras_429_only_retry_after():
    """P3: Cerebras' 429 carries ONLY retry-after (no x-ratelimit-* headers at all) — that
    single header must still yield the wait."""
    exc = _rate_limited_error({"retry-after": "57"})
    assert rl._retry_after_seconds(exc) == pytest.approx(57.0, abs=1e-9)


def test_reset_parses_compact_and_iso8601_durations():
    """P3: reset strings in the compact ('4s' / '2m30s') and ISO-8601 duration ('PT1.5S')
    shapes must parse to seconds; garbage must stay None (fail-safe, never a raise)."""
    assert rl._reset_value_to_seconds("4s") == pytest.approx(4.0, abs=1e-9)
    assert rl._reset_value_to_seconds("1.5s") == pytest.approx(1.5, abs=1e-9)
    assert rl._reset_value_to_seconds("2m30s") == pytest.approx(150.0, abs=1e-9)
    assert rl._reset_value_to_seconds("PT1.5S") == pytest.approx(1.5, abs=1e-9)
    assert rl._reset_value_to_seconds("PT1M30S") == pytest.approx(90.0, abs=1e-9)
    assert rl._reset_value_to_seconds("PT1H") == pytest.approx(3600.0, abs=1e-9)
    assert rl._reset_value_to_seconds("not-a-duration") is None
    assert rl._reset_value_to_seconds(None) is None
    assert rl._reset_value_to_seconds("") is None


def test_reset_parses_rfc3339_anthropic_timestamp(monkeypatch):
    """P3 R2: Anthropic's real reset header is an RFC 3339 INSTANT
    ('2026-04-21T14:38:25Z' — the moment the window replenishes), NOT a duration — the old
    ISO-duration reading of that header was a fictional contract. It must parse to
    seconds-until-reset (floored at 0 when already passed)."""
    import datetime as _dt
    fixed = 1_800_000_000.0
    monkeypatch.setattr(rl.time, "time", lambda: fixed)
    future = _dt.datetime.fromtimestamp(fixed + 60, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    assert rl._reset_value_to_seconds(future) == pytest.approx(60.0, abs=1e-6)
    future_offset = _dt.datetime.fromtimestamp(fixed + 45, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")
    assert rl._reset_value_to_seconds(future_offset) == pytest.approx(45.0, abs=1e-6)
    past = _dt.datetime.fromtimestamp(fixed - 5, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    assert rl._reset_value_to_seconds(past) == pytest.approx(0.0, abs=1e-6)


def test_anthropic_realistic_shape_pauses_and_learns(monkeypatch):
    """P3 R2 COMPOSE: the REAL Anthropic header family (anthropic-ratelimit-requests-* with
    the reset as an RFC 3339 timestamp) must drive BOTH the hard-pause (remaining=0 → pause
    for the window) and rate learning (remaining>0 → tighten the bucket). Before the RFC 3339
    fix the reset parsed to None, so the layer's headline protections were silently dead on
    Anthropic."""
    import datetime as _dt
    fixed = 1_800_000_000.0
    monkeypatch.setattr(rl.time, "time", lambda: fixed)
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr",
                                 "base_url": "https://api.anthropic.com/v1",
                                 "model": "claude-3-5-sonnet"})
    monkeypatch.setattr(rl, "_effective_rate_per_sec",
                        lambda base_url, model: (8.0, {"source": "registry", "rpm": 480}))
    key, bucket = _seed_bucket(rate=8.0)

    reset = _dt.datetime.fromtimestamp(fixed + 30, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    rl._observe_rate_headers(_success_response({
        "anthropic-ratelimit-requests-limit": "500",
        "anthropic-ratelimit-requests-remaining": "0",
        "anthropic-ratelimit-requests-reset": reset,
    }, endpoint="https://api.anthropic.com/v1/messages"))
    assert bucket.pause_until >= time.monotonic() + 29.0, \
        "an exhausted Anthropic budget must hard-pause for the full reset window"

    rl.reset_rate_limiter()
    key, bucket = _seed_bucket(rate=8.0)
    reset = _dt.datetime.fromtimestamp(fixed + 60, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    rl._observe_rate_headers(_success_response({
        "anthropic-ratelimit-requests-limit": "500",
        "anthropic-ratelimit-requests-remaining": "200",
        "anthropic-ratelimit-requests-reset": reset,
    }, endpoint="https://api.anthropic.com/v1/messages"))
    # learned = 200/60 * 60 * 0.9 = 180 RPM = 3.0 rps < 8.0 → the tighten must bind.
    assert bucket.rate == pytest.approx(3.0, abs=1e-6), \
        "the Anthropic shape must learn a tightened rate"
    assert bucket.learner_floor_rps == pytest.approx(3.0, abs=1e-6)
    assert bucket.learner_source == "https://api.anthropic.com/v1"


def test_learner_floor_cleared_on_endpoint_switch(monkeypatch):
    """A floor learned from provider A must NOT pace provider B after a brain switch.

    Under BRAIN-SCOPED keying (DEV/DESIGN-brain-budget-event-cache.md §1) this is solved BY
    CONSTRUCTION rather than by a same-bucket clear: switching base_url changes the brain
    identity → a DIFFERENT bucket key → a FRESH bucket for provider B that inherits NOTHING
    from provider A (no learned floor, no pause, no rate). The earlier same-bucket clear
    mechanism in _get_rate_bucket (tag != learner_source) is vestigial under brain-keying
    because a tag change implies a key change implies a new bucket. This test now asserts the
    INVARIANT (provider B is not paced by provider A's learned floor) via the fresh bucket.
    """
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr", "brain_scope": "account",
                                 "base_url": "https://api.cerebras.ai/v1",
                                 "model": "gemma-4-31b", "api_key": "k"})
    key_a, bucket_a = _seed_bucket(rate=4.5)

    real = _captured_evidence("cerebras-httpx-ua")["all_headers"]
    real = {k: v for k, v in real.items() if k.startswith("x-ratelimit")}
    rl._observe_rate_headers(_success_response(real))
    assert bucket_a.learner_floor_rps == pytest.approx(0.075, abs=1e-6)
    assert bucket_a.learner_source == "https://api.cerebras.ai/v1"

    # Switch the brain to a DIFFERENT provider (DashScope). The brain key changes with it.
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "acct-hdr", "brain_scope": "account",
                                 "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                                 "model": "qwen-turbo", "api_key": "k"})
    monkeypatch.setattr(rl, "_effective_rate_per_sec",
                        lambda base_url, model: (10.0, {"source": "registry", "rpm": 600}))
    rl.apply_rate_limit_sync("EXTRACTION")

    key_b = rl._resolve_rate_limit_key()
    assert key_b != key_a, "switching provider must yield a DIFFERENT brain-scoped key"
    bucket_b = rl._rate_buckets[key_b]
    assert bucket_b.learner_floor_rps == 0.0, \
        "the fresh brain bucket must NOT inherit provider A's learned floor"
    assert bucket_b.rate == pytest.approx(10.0, abs=1e-6), \
        "the fresh bucket must clamp to the NEW endpoint's registry rate"
    assert bucket_b._pause_remaining() == 0.0, \
        "the fresh bucket must NOT inherit provider A's pause"


def test_parse_real_dashscope_headers_returns_none():
    """P3 regression: DashScope's real response headers carry NO x-ratelimit-* family at all →
    the parser must return None (nothing to learn — the correct no-op for that provider)."""
    captured = _captured_evidence("dashscope-qwen-turbo")
    assert captured["ratelimit_headers"] == {}
    assert rl._parse_rate_limit_headers(captured["all_headers"]) is None
    # Tokens-axis-only headers must ALSO be a no-op — we pace REQUESTS, not tokens.
    assert rl._parse_rate_limit_headers({"x-ratelimit-limit-tokens-minute": "30000",
                                         "x-ratelimit-remaining-tokens-minute": "29998"}) is None
