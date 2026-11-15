"""Tests for the brain-scoped cache/upkeep meter that feeds GET /tenant/brain-health.

WHAT THESE PIN, and why each one exists rather than being a shape assertion:

  1. The counters are ISOLATED BY BRAIN SCOPE. This is the product rule ("never wider than the
     brain") at the storage layer.
  2. ``cache_get`` records a HIT when it serves and a MISS when it does not — proved by driving
     the REAL ``cache_get``/``cache_put`` against a REAL Redis and reading the counter back,
     not by asserting that a mock was called. (The seam is inside ``cache_get`` precisely so a
     hit and its miss cannot drift; a test that mocked ``cache_get`` would prove nothing.)
  3. ABSENCE IS NOT ZERO. ``read_window`` must distinguish "cannot measure" from "measured, and
     it was nothing", and must return ``hit_rate=None`` rather than 0.0 over zero lookups —
     the console renders those three differently and a fake 0% reads as total cache failure.
  4. INSTRUMENTATION NEVER BREAKS THE CALL. A meter that raises must not propagate out of
     ``cache_get``.

⚠️ REDIS IS REQUIRED and the skip is GUARDED. A "0 passed, N skipped" run of this file would be
indistinguishable from a green one while proving nothing, so the final test asserts the DB/Redis
lane actually ran.
"""
import os
import uuid

import pytest

os.environ.setdefault("REDIS_URL", "redis://172.20.0.2:6379/0")
os.environ["REDIS_COORD"] = "true"
os.environ["BRAIN_METER"] = "true"
os.environ["LLM_INGEST_CACHE"] = "true"   # explicit ON (do not depend on SAAS_MODE here)

from src.api import brain_meter, llm_cache, redis_coord  # noqa: E402

_RAN = {"value": False}


def _redis_or_skip():
    c = redis_coord.client()
    if c is None:
        pytest.skip("redis unavailable — this file cannot prove anything without it")
    _RAN["value"] = True
    return c


@pytest.fixture()
def scope_a():
    return redis_coord.scope_for_endpoint(f"http://brain-a-{uuid.uuid4().hex}", "model-a")


@pytest.fixture()
def scope_b():
    return redis_coord.scope_for_endpoint(f"http://brain-b-{uuid.uuid4().hex}", "model-b")


# ── 1. isolation ───────────────────────────────────────────────────────────────

def test_counters_are_isolated_by_brain_scope(scope_a, scope_b):
    """Two brains' counters never mix — the storage-layer half of the scoping rule."""
    _redis_or_skip()
    for _ in range(5):
        brain_meter.record(scope_a, brain_meter.KIND_HIT)
    for _ in range(2):
        brain_meter.record(scope_b, brain_meter.KIND_HIT)

    a = brain_meter.read_window(scope_a)
    b = brain_meter.read_window(scope_b)
    assert a["hit"] == 5, a
    assert b["hit"] == 2, b
    # The decisive assertion: neither reads the SUM. A shared key would give both 7.
    assert a["hit"] != 7 and b["hit"] != 7


def test_a_bare_string_scope_is_refused_not_silently_shared():
    """``record`` must no-op on anything that is not a validated Scope.

    If a plain string were accepted, two tenants could resolve to the same key — the exact
    cross-tenant merge ``redis_coord.key()``'s type check exists to make unrepresentable.
    """
    _redis_or_skip()
    brain_meter.record("account-123", brain_meter.KIND_HIT)   # must not raise
    brain_meter.record(None, brain_meter.KIND_HIT)            # must not raise
    assert brain_meter.read_window("account-123")["available"] is False
    assert brain_meter.read_window(None)["available"] is False


def test_an_unknown_kind_is_refused(scope_a):
    _redis_or_skip()
    brain_meter.record(scope_a, "not_a_real_kind")
    assert brain_meter.read_window(scope_a)["observed"] == 0


# ── 2. the real cache seam ─────────────────────────────────────────────────────

def test_cache_get_records_a_miss_then_a_hit(monkeypatch, scope_a):
    """Drive the REAL cache: a miss is counted as a miss, and a subsequent hit as a hit."""
    _redis_or_skip()
    monkeypatch.setattr(llm_cache, "_resolve_scope", lambda: scope_a)

    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "Fraggle is my dog."}]
    sys_prompt = llm_cache.system_prompt_from_messages(messages)

    # 1st call — nothing stored yet → MISS.
    assert llm_cache.cache_get("EXTRACT", sys_prompt, messages) is None
    w = brain_meter.read_window(scope_a)
    assert (w["miss"], w["hit"]) == (1, 0), w

    # Store, then read again → HIT, and the value comes back intact.
    llm_cache.cache_put("EXTRACT", sys_prompt, messages, [{"s": "fraggle"}])
    assert llm_cache.cache_get("EXTRACT", sys_prompt, messages) == [{"s": "fraggle"}]
    w = brain_meter.read_window(scope_a)
    assert (w["miss"], w["hit"]) == (1, 1), w
    assert w["hit_rate"] == 0.5


def test_a_different_prompt_is_a_separate_miss(monkeypatch, scope_a):
    """"Different sentence → different key" must still hold with metering in the path."""
    _redis_or_skip()
    monkeypatch.setattr(llm_cache, "_resolve_scope", lambda: scope_a)
    m1 = [{"role": "user", "content": "one"}]
    m2 = [{"role": "user", "content": "two"}]
    llm_cache.cache_put("EXTRACT", "", m1, [{"a": 1}])
    assert llm_cache.cache_get("EXTRACT", "", m1) == [{"a": 1}]
    assert llm_cache.cache_get("EXTRACT", "", m2) is None
    w = brain_meter.read_window(scope_a)
    assert (w["hit"], w["miss"]) == (1, 1), w


def test_a_non_cacheable_operation_is_not_metered(monkeypatch, scope_a):
    """An op outside the whitelist never touches the cache, so it must not inflate the miss
    count — otherwise the hit rate is dragged down by calls the cache never claimed."""
    _redis_or_skip()
    monkeypatch.setattr(llm_cache, "_resolve_scope", lambda: scope_a)
    assert llm_cache.cache_get("VALIDATION", "", [{"role": "user", "content": "x"}]) is None
    assert brain_meter.read_window(scope_a)["observed"] == 0


# ── 3. absence is not zero ─────────────────────────────────────────────────────

def test_untouched_scope_reads_measured_but_empty(scope_a):
    """Redis is up and this brain did nothing: available=True, all zeros, hit_rate NONE."""
    _redis_or_skip()
    w = brain_meter.read_window(scope_a)
    assert w["available"] is True
    assert w["observed"] == 0
    assert w["hit_rate"] is None, "0.0 would render as a totally failing cache"


def test_hit_rate_is_none_not_zero_over_no_lookups(scope_a):
    _redis_or_skip()
    brain_meter.record(scope_a, brain_meter.KIND_DEFER)   # activity, but no cache lookups
    w = brain_meter.read_window(scope_a)
    assert w["defer"] == 1
    assert w["hit_rate"] is None


def test_meter_disabled_reports_unavailable_not_zero(monkeypatch, scope_a):
    _redis_or_skip()
    monkeypatch.setenv("BRAIN_METER", "false")
    w = brain_meter.read_window(scope_a)
    assert w["available"] is False, "a disabled meter must not claim a measurement of 0"


# ── 4. instrumentation must never break the call ───────────────────────────────

def test_a_meter_explosion_never_escapes_cache_get(monkeypatch, scope_a):
    """The whole point of the try/except around the meter: a counter fault is invisible."""
    _redis_or_skip()
    monkeypatch.setattr(llm_cache, "_resolve_scope", lambda: scope_a)

    def boom(*a, **k):
        raise RuntimeError("redis on fire")

    monkeypatch.setattr(brain_meter, "record", boom)
    msgs = [{"role": "user", "content": "still works"}]
    assert llm_cache.cache_get("EXTRACT", "", msgs) is None          # miss, no exception
    llm_cache.cache_put("EXTRACT", "", msgs, [{"ok": True}])
    assert llm_cache.cache_get("EXTRACT", "", msgs) == [{"ok": True}]  # hit, no exception


def test_record_never_raises_on_a_dead_client(monkeypatch, scope_a):
    _redis_or_skip()
    monkeypatch.setattr(redis_coord, "client", lambda: None)
    brain_meter.record(scope_a, brain_meter.KIND_HIT)      # must be a silent no-op
    assert brain_meter.read_window(scope_a)["available"] is False


# ── 5. the in-process mirror (the FOSS / Redis-down fallback) ──────────────────

def test_local_mirror_records_even_though_the_console_cannot_read_it(scope_a):
    """The mirror exists for single-process/no-Redis installs. It is NOT the console's source
    (the cache runs in the API process, the console in the MCP process) — pinned here so the
    distinction is not "simplified" away later."""
    _redis_or_skip()
    brain_meter.reset_local()
    brain_meter.record(scope_a, brain_meter.KIND_HIT)
    snap = brain_meter.local_snapshot()
    assert snap.get(str(scope_a), {}).get("hit") == 1


# ── false-green guard ──────────────────────────────────────────────────────────

def test_zzz_the_redis_lane_actually_ran():
    """Turns a fully-skipped file into a RED. A skipped test is not a passing test."""
    assert _RAN["value"], ("every test in this file skipped — Redis was unreachable, so this "
                           "file proved NOTHING despite reporting no failures")
