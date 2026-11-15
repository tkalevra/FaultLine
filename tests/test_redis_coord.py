"""`redis_coord` — every key is tenant-scoped, and Redis is OPTIONAL.

Two contracts, both load-bearing:

1. **An unscoped key must be UNREPRESENTABLE.** Not "we remembered to scope it" — the type
   system refuses. A missing scope in a shared Redis is a cross-tenant leak, and the audit
   found fifteen inline f-string key prefixes across nine construction sites with no builder
   at all. `test_poison_*` are that proof.

2. **Redis absent / unreachable / erroring must degrade to in-process behaviour**, never
   raise, never block. A coordination bug must not become an outage.

The live-Redis tests use a SCRATCH db (default 15) and purge their own keys. They SKIP when
no Redis is reachable, so the suite is green on a bare checkout.
"""

import os

import pytest

from src.api import redis_coord as RC


ACCT = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


@pytest.fixture(autouse=True)
def _clean():
    RC.reset_client()
    RC.reset_open_cache()
    yield
    RC.reset_client()
    RC.reset_open_cache()


# ══════════════════════════════════════════════════════════════════════════════
# POISON TESTS — an unscoped key must be impossible to build
# ══════════════════════════════════════════════════════════════════════════════

def test_poison_a_bare_string_is_refused_by_type():
    """The whole point of `Scope` being a type: a plausible-looking uuid is still refused,
    because the guarantee is that VALIDATION happened somewhere auditable."""
    with pytest.raises(RC.UnscopedKeyError):
        RC.key("brk", ACCT, "open")          # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [None, "", "   ", "\t\n"])
def test_poison_empty_scope_values_are_refused(bad):
    with pytest.raises(RC.UnscopedKeyError):
        RC.scope_for_account(bad)
    with pytest.raises(RC.UnscopedKeyError):
        RC.scope_for_seat(bad)
    with pytest.raises(RC.UnscopedKeyError):
        RC.scope_for_endpoint(bad)


@pytest.mark.parametrize("bad", [
    "none", "None", "NULL", "undefined", "unknown", "anonymous",
    "default", "all", "any", "global", "shared", "*", "-", "n/a",
])
def test_poison_placeholder_scopes_are_refused(bad):
    """These LOOK like a scope but are the ABSENCE of one. Accepting `"anonymous"` writes
    every unresolved caller's state to one key that many tenants resolve to."""
    with pytest.raises(RC.UnscopedKeyError):
        RC.scope_for_account(bad)


def test_poison_scope_cannot_be_constructed_directly():
    """No back door around the validating factories."""
    with pytest.raises(RC.UnscopedKeyError):
        RC.Scope("acct", "")
    with pytest.raises(RC.UnscopedKeyError):
        RC.Scope("acct", "whatever-i-like")


def test_poison_none_key_parts_are_refused():
    s = RC.scope_for_account(ACCT)
    with pytest.raises(RC.UnscopedKeyError):
        RC.key("brk", s, None)
    with pytest.raises(RC.UnscopedKeyError):
        RC.key("brk", s, "")


def test_poison_namespace_must_be_a_plain_token():
    s = RC.scope_for_account(ACCT)
    for bad_ns in ("", "  ", "has:colon", "has space", "UPPER*"):
        with pytest.raises(RC.UnscopedKeyError):
            RC.key(bad_ns, s, "x")


def test_every_key_carries_its_scope_and_two_tenants_never_collide():
    a = RC.key("brk", RC.scope_for_account(ACCT), "open")
    b = RC.key("brk", RC.scope_for_account(OTHER), "open")
    assert a != b
    assert a.startswith("fl:brk:acct:") and b.startswith("fl:brk:acct:")
    # No key may be a prefix of another tenant's — a SCAN-based purge would take both.
    assert not a.startswith(b) and not b.startswith(a)


def test_the_single_tenant_scope_is_a_deliberate_act_not_a_fallback():
    """Open-core has no account. It gets an EXPLICIT scope you must ask for by name — you
    cannot arrive here by passing an empty string to a factory."""
    s = RC.scope_single_tenant()
    assert RC.key("brk", s, "open") == "fl:brk:solo:single_tenant:open"


def test_endpoint_scope_never_stores_the_url_in_plaintext():
    """A BYO base_url can carry an API key. It must never reach a Redis key or a log."""
    secret = "https://api.example.com/v1?api_key=sk-SUPERSECRET-DO-NOT-LEAK"
    k = RC.key("ctxwin", RC.scope_for_endpoint(secret, "qwen"), "n_ctx")
    assert "SUPERSECRET" not in k
    assert "api.example.com" not in k
    assert "sk-" not in k


def test_endpoint_scope_separates_models_on_one_host():
    """Two models behind one endpoint have different windows — they must not share a key."""
    a = RC.scope_for_endpoint("http://brain:8080/v1", "qwen3.5-9b")
    b = RC.scope_for_endpoint("http://brain:8080/v1", "qwen3.5-36b")
    assert a != b


# ══════════════════════════════════════════════════════════════════════════════
# REDIS IS OPTIONAL — absent, disabled, or broken must all degrade silently
# ══════════════════════════════════════════════════════════════════════════════

def test_disabled_by_default_nothing_talks_to_redis(monkeypatch):
    monkeypatch.delenv("REDIS_COORD", raising=False)
    RC.reset_client()
    assert RC.enabled() is False
    assert RC.client() is None
    assert RC.breaker_enabled() is False
    assert RC.window_cache_enabled() is False
    assert RC.semaphore_enabled() is False


def test_unreachable_redis_degrades_and_never_raises(monkeypatch):
    """Point it at a dead port. Every entry point must answer 'no shared opinion'."""
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_BREAKER", "true")
    monkeypatch.setenv("REDIS_COORD_WINDOW", "true")
    monkeypatch.setenv("REDIS_COORD_LLM_SEMAPHORE", "true")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")   # nothing listens here
    monkeypatch.setenv("REDIS_COORD_SOCKET_TIMEOUT", "0.2")
    RC.reset_client()
    s = RC.scope_for_account(ACCT)

    assert RC.client() is None
    assert RC.publish_open(s, 30) is False
    assert RC.shared_open_until(s) is None
    RC.clear_shared_open(s)                       # must not raise
    assert RC.publish_window(s, 4096) is False
    assert RC.read_window(s) is None
    assert RC.inflight_count(s) is None
    # FAIL-OPEN is deliberate: a Redis outage must never block an LLM call.
    assert RC.try_acquire_slot(s, "holder-1") is True
    RC.release_slot(s, "holder-1")                # must not raise


def test_missing_redis_library_degrades(monkeypatch):
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setattr(RC, "_redis", None)
    RC.reset_client()
    assert RC.client() is None


def test_a_broken_client_degrades_rather_than_propagating(monkeypatch):
    """Redis up but every command erroring (OOM, wrong type, auth) — same contract."""
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_BREAKER", "true")
    monkeypatch.setenv("REDIS_COORD_LLM_SEMAPHORE", "true")

    class Exploding:
        def __getattr__(self, _name):
            def _boom(*a, **k):
                raise RuntimeError("OOM command not allowed when used memory > maxmemory")
            return _boom

    monkeypatch.setattr(RC, "_client", Exploding())
    monkeypatch.setattr(RC, "_client_built", True)
    s = RC.scope_for_account(ACCT)
    assert RC.publish_open(s, 30) is False
    assert RC.shared_open_until(s) is None
    assert RC.try_acquire_slot(s, "h") is True     # fail-OPEN
    RC.release_slot(s, "h")


def test_an_unscoped_key_still_raises_even_when_redis_is_broken(monkeypatch):
    """The fail-safe `except` must NOT swallow a scoping bug — that is the one error class
    that must always be loud, because silently swallowing it is how a leak ships."""
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_BREAKER", "true")
    monkeypatch.setenv("REDIS_COORD_WINDOW", "true")
    monkeypatch.setattr(RC, "_client", object())
    monkeypatch.setattr(RC, "_client_built", True)
    with pytest.raises(RC.UnscopedKeyError):
        RC.publish_open("not-a-scope", 30)         # type: ignore[arg-type]
    with pytest.raises(RC.UnscopedKeyError):
        RC.read_window("not-a-scope")              # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════════════
# LIVE REDIS (scratch db) — the round-trip actually works and isolates tenants
# ══════════════════════════════════════════════════════════════════════════════

def _scratch_url():
    return os.environ.get("FLTEST_REDIS_URL", "")


def _live(monkeypatch, **flags):
    url = _scratch_url()
    if not url:
        pytest.skip("no FLTEST_REDIS_URL — live Redis round-trip skipped")
    monkeypatch.setenv("REDIS_URL", url)
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_SOCKET_TIMEOUT", "2")
    for k, v in flags.items():
        monkeypatch.setenv(k, v)
    RC.reset_client()
    RC.reset_open_cache()
    if RC.client() is None:
        pytest.skip("scratch Redis not reachable")


def test_live_breaker_state_is_shared_and_tenant_isolated(monkeypatch):
    _live(monkeypatch, REDIS_COORD_BREAKER="true", REDIS_COORD_OPEN_CACHE_MS="0")
    a, b = RC.scope_for_account(ACCT), RC.scope_for_account(OTHER)
    try:
        assert RC.publish_open(a, 30) is True
        assert RC.shared_open_until(a) is not None, "another process must see the open verdict"
        assert RC.shared_open_until(b) is None, "a DIFFERENT account must be unaffected"
        RC.clear_shared_open(a)
        assert RC.shared_open_until(a) is None, "a success must clear it, not wait out the TTL"
    finally:
        RC.purge_scope(a)
        RC.purge_scope(b)


def test_live_window_cache_round_trips_and_isolates_brains(monkeypatch):
    _live(monkeypatch, REDIS_COORD_WINDOW="true")
    a = RC.scope_for_endpoint("http://brain-a:8080/v1", "qwen")
    b = RC.scope_for_endpoint("http://brain-b:8080/v1", "qwen")
    try:
        assert RC.publish_window(a, 4096) is True
        assert RC.read_window(a) == 4096
        assert RC.read_window(b) is None, "a different brain must not inherit a window"
    finally:
        RC.purge_scope(a)
        RC.purge_scope(b)


def test_live_semaphore_enforces_a_global_ceiling(monkeypatch):
    """The real fix for 8N-per-request concurrency against a 4-slot server."""
    _live(monkeypatch, REDIS_COORD_LLM_SEMAPHORE="true", LLM_GLOBAL_CONCURRENCY="4")
    s = RC.scope_for_account(ACCT)
    try:
        RC.purge_scope(s)
        held = [RC.try_acquire_slot(s, f"h{i}") for i in range(4)]
        assert all(held), "the first 4 must get slots"
        assert RC.try_acquire_slot(s, "h4") is False, "the 5th must be refused — 4 slots"
        assert RC.inflight_count(s) == 4
        RC.release_slot(s, "h0")
        assert RC.try_acquire_slot(s, "h4") is True, "a released slot must be reusable"
    finally:
        RC.purge_scope(s)


def test_live_semaphore_slot_of_a_crashed_holder_expires(monkeypatch):
    """A dead process must not permanently consume capacity — that is why it is a lease ZSET
    and not an INCR/DECR counter."""
    _live(monkeypatch, REDIS_COORD_LLM_SEMAPHORE="true", LLM_GLOBAL_CONCURRENCY="2",
          REDIS_COORD_LLM_LEASE_SECONDS="1")
    s = RC.scope_for_account(ACCT)
    try:
        RC.purge_scope(s)
        assert RC.try_acquire_slot(s, "crashed-a")
        assert RC.try_acquire_slot(s, "crashed-b")
        assert RC.try_acquire_slot(s, "c") is False
        import time as _t
        _t.sleep(1.2)                       # both leases expire; nobody ever released them
        assert RC.try_acquire_slot(s, "c") is True, "an expired lease must free the slot"
    finally:
        RC.purge_scope(s)


def test_live_two_tenants_never_share_a_semaphore(monkeypatch):
    _live(monkeypatch, REDIS_COORD_LLM_SEMAPHORE="true", LLM_GLOBAL_CONCURRENCY="1")
    a, b = RC.scope_for_account(ACCT), RC.scope_for_account(OTHER)
    try:
        RC.purge_scope(a)
        RC.purge_scope(b)
        assert RC.try_acquire_slot(a, "x") is True
        assert RC.try_acquire_slot(a, "y") is False
        assert RC.try_acquire_slot(b, "x") is True, "tenant B's ceiling is its own"
    finally:
        RC.purge_scope(a)
        RC.purge_scope(b)
