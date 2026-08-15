"""Unit tests for the coordination core (src/api/redis_coord.py).

Pins the three hard rules at the module's heart:
1. Redis is OPTIONAL — every entry point fails safe (None / degraded-ok), never raises.
2. Every key is scoped — an unscoped key is UNREPRESENTABLE, not merely discouraged.
3. Endpoint identity is a DIGEST — a URL carrying credentials can never leak into a
   Redis key or a log line.

Plus, when a live coordination redis is reachable (REDIS_URL): the atomicity of the
Lua takes under concurrency.

Fixture hygiene: every URL is RFC 5737 TEST-NET-1 (192.0.2.0/24) or localhost.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from src.api import redis_coord as RC

# Coordination state persists in redis BY DESIGN (it is the whole point) — every
# stateful test uses a per-RUN scope so re-running the suite never inherits the
# previous run's budget. RFC 5737 TEST-NET-1 hosts throughout.
_RUN = uuid.uuid4().hex[:8]


def _live_redis():
    """A pingable connection from the module's own URL source, or None."""
    try:
        import redis as _redis
        url = RC.redis_url()
        c = _redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)
        c.ping()
        return c
    except Exception:
        return None


_LIVE = _live_redis()

# Only the ATOMICITY tests need a live redis. The scope/fail-safe contract tests
# run EVERYWHERE — a no-redis environment must still verify that an unscoped key
# is unrepresentable and that failures are swallowed (guarding the rules the
# module's whole design rests on).
needs_redis = pytest.mark.skipif(
    _LIVE is None,
    reason="REDIS_URL unreachable — coordination tests need a live redis "
           "(e.g. docker run -p 6379:6379 redis:7-alpine)",
)


@pytest.fixture(autouse=True)
def _module_caches():
    RC.reset_client()
    yield
    RC.reset_client()


# ── rule 2: an unscoped key is unrepresentable ────────────────────────────────

def test_scope_is_not_directly_constructible():
    with pytest.raises(RC.UnscopedKeyError):
        RC.Scope("brain", "whatever")


def test_key_rejects_a_bare_string_scope():
    with pytest.raises(RC.UnscopedKeyError):
        RC.key("llmrate", "not-a-scope")


def test_placeholders_cannot_become_scopes():
    for poison in ("", "  ", "none", "NULL", "unknown", "default", "*"):
        with pytest.raises(RC.UnscopedKeyError):
            RC.scope_for_endpoint(poison, "m")


def test_key_namespace_is_constrained():
    s = RC.scope_for_endpoint("http://192.0.2.10:1", "m")
    with pytest.raises(RC.UnscopedKeyError):
        RC.key("NOT-LOWER", s)
    with pytest.raises(RC.UnscopedKeyError):
        RC.key("", s)


def test_scopes_normalize_and_discriminate():
    a = RC.scope_for_endpoint("http://192.0.2.10:11434/", "m1")
    same = RC.scope_for_endpoint("http://192.0.2.10:11434", "m1")
    assert a == same
    assert a != RC.scope_for_endpoint("http://192.0.2.10:11434", "m2")
    assert a != RC.scope_for_endpoint("http://192.0.2.11:11434", "m1")


# ── rule "no credential leakage" ──────────────────────────────────────────────

def test_endpoint_identity_is_a_digest_not_the_url():
    url = "http://192.0.2.10:1/v1?apikey=SECRET-MATERIAL"
    s = RC.scope_for_endpoint(url, "m")
    assert "SECRET-MATERIAL" not in str(s)
    assert "SECRET-MATERIAL" not in RC.key("llmrate", s, "bucket")
    assert "192.0.2.10" not in RC.key("llmrate", s, "bucket")


def test_safe_redis_url_strips_credentials():
    import os
    old = os.environ.get("REDIS_URL")
    try:
        os.environ["REDIS_URL"] = "redis://:supersecret@192.0.2.20:6379/0"
        assert RC.safe_redis_url() == "redis://192.0.2.20:6379/0"
        assert "supersecret" not in RC.safe_redis_url()
    finally:
        if old is None:
            os.environ.pop("REDIS_URL", None)
        else:
            os.environ["REDIS_URL"] = old


# ── rule 1: Redis is optional — fail-safe, never raises ───────────────────────

def test_disabled_coordination_yields_no_shared_opinion(monkeypatch):
    monkeypatch.setenv("REDIS_COORD", "false")
    RC.reset_client()
    s = RC.scope_for_endpoint("http://192.0.2.10:1", "m")
    assert RC.client() is None
    assert RC.take_token_state(s, 1.0, 1.0) is None
    out = RC.try_take_daily(s, 10, 60)
    assert out.get("ok") is True and out.get("degraded") is True
    assert RC.peek_daily(s) is None


def test_unreachable_coordination_fails_safe(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://192.0.2.99:6399/0")   # TEST-NET: unroutable
    RC.reset_client()
    s = RC.scope_for_endpoint("http://192.0.2.10:1", "m")
    assert RC.client() is None
    assert RC.take_token_state(s, 1.0, 1.0) is None
    out = RC.try_take_daily(s, 10, 60)
    assert out.get("ok") is True and out.get("degraded") is True


# ── rule 3: atomicity under concurrency (live redis) ──────────────────────────

@needs_redis
def test_shared_rate_take_never_overspends_under_concurrency():
    """Ten threads, one shared bucket, burst 5, refill ~0: exactly 5 tokens out."""
    RC.reset_client()
    s = RC.scope_for_endpoint("http://192.0.2.31:1", f"atomic-rate-{_RUN}")
    granted = []
    lock = threading.Lock()

    def _take():
        st = RC.take_token_state(s, 0.0001, 5.0)   # ~zero refill
        if st is not None and st.get("wait_s", 0) <= 0:
            with lock:
                granted.append(1)

    threads = [threading.Thread(target=_take) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Exactly the capacity: the atomic Lua take cannot double-spend a token.
    assert len(granted) == 5


@needs_redis
def test_shared_daily_take_never_overspends_under_concurrency():
    RC.reset_client()
    s = RC.scope_for_endpoint("http://192.0.2.32:1", f"atomic-daily-{_RUN}")
    ok = []
    lock = threading.Lock()

    def _take():
        out = RC.try_take_daily(s, 7, 3600)
        if out.get("ok") and not out.get("degraded"):
            with lock:
                ok.append(1)

    threads = [threading.Thread(target=_take) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 7


@needs_redis
def test_daily_take_force_counts_but_never_refuses():
    RC.reset_client()
    s = RC.scope_for_endpoint("http://192.0.2.33:1", f"daily-force-{_RUN}")
    for _ in range(3):
        out = RC.try_take_daily(s, 2, 3600, force=True)
        assert out.get("ok") is True
    peek = RC.peek_daily(s)
    assert peek is not None and peek["used"] == 3      # counted, past cap
    assert peek["remaining"] == 0
    refused = RC.try_take_daily(s, 2, 3600, force=False)
    assert refused.get("ok") is False and refused.get("reset_ms", 0) > 0
