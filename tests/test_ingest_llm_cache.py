"""Deliverable B — brain-scoped ingest LLM cache. Acceptance via fltest.

Proves the five contracts from DEV/DESIGN-brain-budget-event-cache.md §2:
  1. Same (op, system, messages) twice → second call is a cache HIT, POST called ONCE.
  2. Different brain scope → different key → MISS (scope isolation).
  3. Same brain+prompt, different operation → whitelisted caches, non-whitelisted does not.
  4. LLM_INGEST_CACHE=false → no caching (POST always called).
  5. cache_put raises → the call still succeeds and returns its real result (fail-safe).

The tests exercise the REAL hooks in ``llm_calls.call_llm_with_retry_sync`` against
a minimal fake POST and a real (in-memory) Redis, so the scope-isolation guarantee
is proven at the key level — not by mocking the cache itself away.
"""
import asyncio
import fnmatch
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))


# ──────────────────────────────────────────────────────────────────────────────
# TEST HELPERS — minimal fake Redis + a fake POST that counts calls
# ──────────────────────────────────────────────────────────────────────────────

class _FakeRedis:
    """Minimal in-memory Redis: get/set/delete/scan_iter/ping. Enough for the cache."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def get(self, k):
        return self._store.get(k)

    def set(self, k, v, ex=None):
        self._store[k] = v

    def delete(self, k):
        self._store.pop(k, None)

    def scan_iter(self, match=None, count=None):
        for k in list(self._store):
            if match is None or fnmatch.fnmatch(k, match):
                yield k

    def ping(self):
        pass

    def store_keys(self):
        """Test introspection: the raw keys currently held."""
        return set(self._store.keys())


class _FakeResponse:
    """Minimal httpx.Response stand-in for _parse_llm_response_robust."""

    def __init__(self, content_json):
        # The parser reads response.json() first; on failure it reads .text.
        # We make .text the canonical JSON so both paths agree.
        body = {"choices": [{"message": {"content": content_json}}]}
        self._text = json.dumps(body)
        self.status_code = 200

    @property
    def text(self):
        return self._text

    def json(self):
        return json.loads(self._text)

    def raise_for_status(self):
        pass


def _brain_cfg(account_id, user_id, base, model, key, scope="seat"):
    """A resolved brain cfg matching saas/llm_config._build_config stamps."""
    cfg = {
        "account_id": account_id, "brain_scope": scope,
        "backend_type": "openai", "base_url": base, "model": model, "api_key": key,
    }
    if scope == "seat":
        cfg["seat_user_id"] = user_id
    return cfg


@pytest.fixture
def fake_redis(monkeypatch):
    """Wire a _FakeRedis into redis_coord.client() so the REAL cache_get/put run."""
    from src.api import redis_coord

    fake = _FakeRedis()
    # redis_coord.client() is the single fail-safe connection getter. Bypass
    # enabled() (which gates on REDIS_COORD) — the cache calls client() directly.
    monkeypatch.setattr(redis_coord, "client", lambda: fake)
    # Also patch the module-level reference in case anything cached it.
    monkeypatch.setattr(redis_coord, "_client", fake)
    monkeypatch.setattr(redis_coord, "_client_built", True)
    return fake


@pytest.fixture
def cache_on(monkeypatch):
    """Force the cache ON and clear SAAS-agnostic gate state."""
    monkeypatch.setenv("LLM_INGEST_CACHE", "true")

    # Reset llm_cache's view (it reads env on every call, so nothing to reset).
    yield
    monkeypatch.delenv("LLM_INGEST_CACHE", raising=False)


def _stub_llm_call_gates(monkeypatch, *, response_content='[{"subject":"me","relation":"is","object":"here"}]'):
    """Stub the gates in call_llm_with_retry_sync so a MISS reaches the POST.

    Returns a dict with ``post_count`` that the test asserts on.
    """
    import src.api.llm_calls as lc

    monkeypatch.setattr(lc, "_saas_brain_unavailable", lambda: False)
    monkeypatch.setattr(lc, "_breaker_is_open", lambda b: False)
    monkeypatch.setattr(lc, "_apply_tenant_brain",
                        lambda model, mt: (lc._breaker_for(None), model, mt, {}))
    monkeypatch.setattr(lc, "_context_preflight",
                        lambda msgs, mt, op, uid, mode: (mt, None))
    monkeypatch.setattr(lc, "_tenant_egress_blocked", lambda: False)
    monkeypatch.setattr(lc, "_get_endpoint_list", lambda: ["http://test.local/v1/chat"])
    monkeypatch.setattr(lc, "apply_rate_limit_sync", lambda op: True)

    post_count = {"n": 0}
    _resp = _FakeResponse(response_content)

    def _fake_post(client, endpoint, **kw):
        post_count["n"] += 1
        return _resp

    monkeypatch.setattr(lc.llm_source_ip, "post_sync", _fake_post)
    return post_count


# ──────────────────────────────────────────────────────────────────────────────
# TEST 1 — same prompt twice → cache HIT on second → POST called ONCE
# ──────────────────────────────────────────────────────────────────────────────

def test_same_prompt_twice_second_is_cache_hit(fake_redis, cache_on, monkeypatch):
    """Acceptance §2.1: repeated identical ingest → 1 brain call then 0."""
    import src.api.llm_calls as lc

    cfg = _brain_cfg("acct-x", "user-aaa", "https://api.test.com/v1", "m1", "sk-aaa")
    monkeypatch.setattr(lc, "_current_llm_config_or_none", lambda: cfg)

    post_count = _stub_llm_call_gates(monkeypatch)

    messages = [{"role": "system", "content": "extract facts"},
                {"role": "user", "content": "I live in Berlin."}]

    r1 = lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    r2 = lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")

    assert post_count["n"] == 1, f"second call should HIT the cache; POST ran {post_count['n']} times"
    assert r1 == r2, "cached result must equal the live result (same edges)"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 2 — different brain scope → different key → MISS
# ──────────────────────────────────────────────────────────────────────────────

def test_different_brain_scope_is_miss(fake_redis, cache_on, monkeypatch):
    """Acceptance §2.2 + §0: two seats with own brains on a house account never share."""
    import src.api.llm_calls as lc

    cfg_a = _brain_cfg("acct-singles", "user-aaa", "https://api.test.com/v1", "m1", "sk-aaa")
    cfg_b = _brain_cfg("acct-singles", "user-bbb", "https://api.test.com/v1", "m1", "sk-bbb")

    post_count = _stub_llm_call_gates(monkeypatch)

    messages = [{"role": "system", "content": "extract facts"},
                {"role": "user", "content": "I live in Berlin."}]

    # Seat A calls first → MISS → POST → cached under A's brain scope.
    monkeypatch.setattr(lc, "_current_llm_config_or_none", lambda: cfg_a)
    r1 = lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    assert post_count["n"] == 1

    # Seat B (DIFFERENT brain scope) calls with the SAME prompt → must MISS
    # (the key includes the brain identity, so A's cache entry does not serve B).
    monkeypatch.setattr(lc, "_current_llm_config_or_none", lambda: cfg_b)
    r2 = lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    assert post_count["n"] == 2, ("different brain scope must be a MISS — a seat's cached "
                                  "answer must never serve a different brain")

    # Prove the two entries live under DIFFERENT keys (scope isolation by construction).
    assert len(fake_redis.store_keys()) >= 2, "expected two cache entries under different keys"
    assert r1 == r2  # same prompt → same LLM answer (our fake returns deterministic content)


def test_corporate_seats_share_cache(fake_redis, cache_on, monkeypatch):
    """Acceptance §0: two seats on one corporate brain SHARE one cache."""
    import src.api.llm_calls as lc

    shared_cfg = _brain_cfg("acct-corp", "user-aaa", "https://api.test.com/v1", "m1", "sk-corp",
                            scope="account")

    post_count = _stub_llm_call_gates(monkeypatch)
    messages = [{"role": "system", "content": "extract facts"},
                {"role": "user", "content": "I live in Berlin."}]

    # Seat A on the corporate brain.
    monkeypatch.setattr(lc, "_current_llm_config_or_none", lambda: shared_cfg)
    r1 = lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    assert post_count["n"] == 1

    # Seat B resolves the SAME shared cfg → SAME brain scope → HIT.
    r2 = lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    assert post_count["n"] == 1, "two seats on one corporate brain MUST share the cache"
    assert r1 == r2


# ──────────────────────────────────────────────────────────────────────────────
# TEST 3 — whitelisted op caches; non-whitelisted does not
# ──────────────────────────────────────────────────────────────────────────────

def test_whitelisted_caches_nonwhitelisted_does_not(fake_redis, cache_on, monkeypatch):
    """Acceptance §2: only the real deterministic ingest operations cache.

    The whitelist is the LITERAL string each call site passes (verified by grep of src/), not a
    concept name: REFRAME (atomize), EXTRACT (relation-extract), INTENT_PRECLASSIFY/
    INTENT_ADJUDICATION (classify-chain). ENRICHMENT is intentionally NOT cached (ambiguous
    across ≥5 call sites with different semantics).
    """
    import src.api.llm_calls as lc

    cfg = _brain_cfg("acct-x", "user-aaa", "https://api.test.com/v1", "m1", "sk-aaa")
    monkeypatch.setattr(lc, "_current_llm_config_or_none", lambda: cfg)

    post_count = _stub_llm_call_gates(monkeypatch)
    messages = [{"role": "system", "content": "x"},
                {"role": "user", "content": "y"}]

    # Whitelisted: EXTRACT → caches on first call, HIT on second.
    lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    assert post_count["n"] == 1, "EXTRACT is whitelisted → second call should HIT"

    # Whitelisted: REFRAME → caches too (the real atomize literal).
    post_count["n"] = 0
    lc.call_llm_with_retry_sync(messages, model="m1", operation="REFRAME")
    lc.call_llm_with_retry_sync(messages, model="m1", operation="REFRAME")
    assert post_count["n"] == 1, "REFRAME is whitelisted → second call should HIT"

    # Non-whitelisted: VALIDATION → never caches → POST every time.
    post_count["n"] = 0
    lc.call_llm_with_retry_sync(messages, model="m1", operation="VALIDATION")
    lc.call_llm_with_retry_sync(messages, model="m1", operation="VALIDATION")
    assert post_count["n"] == 2, "VALIDATION is NOT whitelisted → every call POSTs"

    # Non-whitelisted BY DESIGN: ENRICHMENT (ambiguous across call sites) → never caches.
    post_count["n"] = 0
    lc.call_llm_with_retry_sync(messages, model="m1", operation="ENRICHMENT")
    lc.call_llm_with_retry_sync(messages, model="m1", operation="ENRICHMENT")
    assert post_count["n"] == 2, "ENRICHMENT is NOT cached (ambiguous across call sites)"


def test_operation_whitelist_boundaries():
    """The frozenset is exactly the four REAL call-site literals — nothing else sneaks in."""
    from src.api.llm_cache import _CACHEABLE_OPS, cacheable_operation

    assert _CACHEABLE_OPS == frozenset({
        "REFRAME", "EXTRACT", "INTENT_PRECLASSIFY", "INTENT_ADJUDICATION"})
    # The concept names from the first draft MUST be absent (no call site passes them).
    for op in ("ATOMIZE", "ASPECT_SYNONYM", "INTENT_CLASSIFICATION", "EXTRACTION"):
        assert op not in _CACHEABLE_OPS, f"{op} is a concept name no call site passes"
    for op in ("VALIDATION", "RETRACTION_EXTRACTION", "CLASSIFY_CHAIN", "DEFAULT", "",
               None, "recall", "DOC", "ENRICHMENT"):
        assert not cacheable_operation(op), f"{op!r} must NOT be cacheable"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 4 — LLM_INGEST_CACHE=false → no caching (POST always called)
# ──────────────────────────────────────────────────────────────────────────────

def test_kill_switch_disables_cache(fake_redis, monkeypatch):
    """LLM_INGEST_CACHE=false → kill switch → no caching, POST every time."""
    import src.api.llm_calls as lc

    monkeypatch.setenv("LLM_INGEST_CACHE", "false")
    cfg = _brain_cfg("acct-x", "user-aaa", "https://api.test.com/v1", "m1", "sk-aaa")
    monkeypatch.setattr(lc, "_current_llm_config_or_none", lambda: cfg)
    post_count = _stub_llm_call_gates(monkeypatch)

    messages = [{"role": "system", "content": "x"},
                {"role": "user", "content": "y"}]

    lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    assert post_count["n"] == 2, "kill switch OFF → every call POSTs, nothing cached"
    assert fake_redis.store_keys() == set(), "no entries should be written with cache OFF"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 5 — cache_put raises → call still succeeds (fail-safe)
# ──────────────────────────────────────────────────────────────────────────────

def test_cache_put_failure_never_breaks_call(fake_redis, cache_on, monkeypatch):
    """Acceptance §2: a cache write that fails is logged and skipped — never blocks."""
    import src.api.llm_calls as lc
    from src.api import llm_cache

    cfg = _brain_cfg("acct-x", "user-aaa", "https://api.test.com/v1", "m1", "sk-aaa")
    monkeypatch.setattr(lc, "_current_llm_config_or_none", lambda: cfg)

    expected = [{"subject": "me", "relation": "is", "object": "alive"}]
    post_count = _stub_llm_call_gates(monkeypatch, response_content=json.dumps(expected))

    # Sabotage cache_put to raise — the call MUST still return its real result.
    def _boom(*a, **kw):
        raise RuntimeError("redis exploded during SET")
    monkeypatch.setattr(llm_cache, "cache_put", _boom)

    messages = [{"role": "system", "content": "x"},
                {"role": "user", "content": "y"}]

    result = lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    assert result == expected, "call must return its REAL result even if cache_put raises"
    assert post_count["n"] == 1


def test_cache_get_failure_never_breaks_call(fake_redis, cache_on, monkeypatch):
    """A cache_get that raises is swallowed — the call proceeds as a normal MISS."""
    import src.api.llm_calls as lc
    from src.api import llm_cache

    cfg = _brain_cfg("acct-x", "user-aaa", "https://api.test.com/v1", "m1", "sk-aaa")
    monkeypatch.setattr(lc, "_current_llm_config_or_none", lambda: cfg)

    expected = [{"subject": "me", "relation": "is", "object": "fine"}]
    post_count = _stub_llm_call_gates(monkeypatch, response_content=json.dumps(expected))

    def _boom(*a, **kw):
        raise RuntimeError("redis exploded during GET")
    monkeypatch.setattr(llm_cache, "cache_get", _boom)

    messages = [{"role": "system", "content": "x"},
                {"role": "user", "content": "y"}]

    result = lc.call_llm_with_retry_sync(messages, model="m1", operation="EXTRACT")
    assert result == expected, "call must return its REAL result even if cache_get raises"
    assert post_count["n"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# UNIT TESTS — key normalization + cache module internals
# ──────────────────────────────────────────────────────────────────────────────

def test_canonical_messages_strips_ephemeral_ids():
    """Ephemeral ids (request_id, trace_id, ts, session) are stripped; semantic content stays."""
    from src.api.llm_cache import _canonical_messages, _prompt_hash

    base = [{"role": "system", "content": "extract"},
            {"role": "user", "content": "I live in Berlin."}]

    with_ids = [{"role": "system", "content": "extract", "request_id": "abc123",
                 "trace_id": "t1", "created_ts": "2026-08-12T10:00:00Z"},
                {"role": "user", "content": "I live in Berlin.", "session_id": "s1"}]

    # Stripping ephemeral ids → same canonical JSON → same hash.
    assert _canonical_messages(base) == _canonical_messages(with_ids), \
        "ephemeral ids must be stripped so same-sentence → same key"
    assert _prompt_hash("extract", base) == _prompt_hash("extract", with_ids)


def test_canonical_messages_preserves_semantic_content():
    """Different content → different hash (extraction must not be weakened)."""
    from src.api.llm_cache import _prompt_hash

    a = [{"role": "user", "content": "I live in Berlin."}]
    b = [{"role": "user", "content": "I live in Munich."}]
    assert _prompt_hash("", a) != _prompt_hash("", b), \
        "different content MUST hash differently — cache must never serve the wrong answer"


def test_cache_enabled_auto_follows_saas(monkeypatch):
    """cache_enabled() in 'auto' mode tracks SAAS_MODE; explicit true/false overrides."""
    from src.api import llm_cache

    monkeypatch.setenv("LLM_INGEST_CACHE", "auto")
    monkeypatch.setenv("SAAS_MODE", "1")
    assert llm_cache.cache_enabled() is True

    monkeypatch.setenv("SAAS_MODE", "0")
    assert llm_cache.cache_enabled() is False

    monkeypatch.setenv("LLM_INGEST_CACHE", "true")
    monkeypatch.setenv("SAAS_MODE", "0")
    assert llm_cache.cache_enabled() is True, "explicit true overrides SAAS off"

    monkeypatch.setenv("LLM_INGEST_CACHE", "false")
    monkeypatch.setenv("SAAS_MODE", "1")
    assert llm_cache.cache_enabled() is False, "explicit false overrides SAAS on"


def test_cache_get_returns_none_when_no_scope(monkeypatch):
    """No brain cfg → no scope → cache_get returns None (FOSS/unbound path)."""
    import src.api.llm_calls as lc
    from src.api import llm_cache

    monkeypatch.setenv("LLM_INGEST_CACHE", "true")
    monkeypatch.setattr(lc, "_current_llm_config_or_none", lambda: None)
    assert llm_cache.cache_get("EXTRACT", "sys", [{"role": "user", "content": "hi"}]) is None


def test_cache_put_refuses_none():
    """None is the miss sentinel — cache_put must never store it."""
    from src.api import llm_cache

    # This should be a silent no-op, never raise.
    llm_cache.cache_put("EXTRACT", "sys", [{"role": "user", "content": "hi"}], None)


def test_cache_round_trip_with_real_key(fake_redis, cache_on, monkeypatch):
    """cache_put then cache_get returns the exact stored object (round-trip)."""
    import src.api.llm_calls as lc
    from src.api import llm_cache

    cfg = _brain_cfg("acct-x", "user-aaa", "https://api.test.com/v1", "m1", "sk-aaa")
    monkeypatch.setattr(lc, "_current_llm_config_or_none", lambda: cfg)

    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "msg"}]
    result = [{"edges": [{"s": "a", "r": "b", "o": "c"}]}]

    llm_cache.cache_put("EXTRACT", "sys", messages, result)
    got = llm_cache.cache_get("EXTRACT", "sys", messages)
    assert got == result, "round-trip must return the EXACT stored object"
