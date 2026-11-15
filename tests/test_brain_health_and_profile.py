"""BYO-brain health telemetry + latency-profile binding.

Pins the four things that could silently rot:

 1. SAAS_MODE OFF is byte-for-byte unchanged (no writes, no timeout change, no imports that bite).
 2. The telemetry lane CANNOT break its host — the health path is made to RAISE and the caller
    (the atomizer, i.e. the ingest path) still completes with the same result.
 3. The profile actually BINDS to the per-operation budget. A profile that displays but does not
    take effect is theatre, and it is the exact bug class this repo keeps hitting.
 4. The percentile read off the latency histogram is a nearest-rank RANGE, never a fabricated
    precise number.

Run with:
  python3 tools/fltest.py --bug BRAINHEALTH --test tests/test_brain_health_and_profile.py \
      --note "increment 1+2: health aggregate + profile binding"
"""

import asyncio
import importlib
import os

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from a known env; no test leaks a flag into the next."""
    for var in ("SAAS_MODE", "LLM_TIMEOUT_REFRAME", "BRAIN_HEALTH_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    yield


def _llm_calls():
    import src.api.llm_calls as m
    # the ImportError memo is process-global; clear it so ordering cannot hide a regression
    m._PROFILE_SINK_MISSING = False
    m._HEALTH_SINK_MISSING = False
    return m


# ══════════════════════════════════════════════════════════════════════════════════════
# 1. SAAS_MODE OFF — byte-for-byte unchanged
# ══════════════════════════════════════════════════════════════════════════════════════

def test_saas_off_timeouts_are_unchanged(monkeypatch):
    """No SAAS_MODE → the shipped defaults, exactly. This is the regression pin for the
    whole profile mechanism: it must be invisible when the SaaS layer is off."""
    m = _llm_calls()
    assert m.LLMTimeouts.get("REFRAME") == 6.0
    assert m.LLMTimeouts.get("EXTRACT") == 30.0
    assert m.LLMTimeouts.get("SOMETHING_UNKNOWN") == 30.0   # DEFAULT


def test_saas_off_health_record_is_a_noop(monkeypatch):
    """With SAAS off the recorder must not buffer anything — no work, no I/O, no growth."""
    from saas import brain_health
    brain_health._buf.clear()
    brain_health.record("11111111-2222-3333-4444-555555555555", brain_health.M_LLM_CALL,
                        operation="REFRAME", elapsed_seconds=1.0)
    assert brain_health._buf == {}


def test_saas_off_env_override_still_wins(monkeypatch):
    """An operator's explicit env budget is untouched by any of this."""
    monkeypatch.setenv("LLM_TIMEOUT_REFRAME", "12.5")
    m = _llm_calls()
    assert m.LLMTimeouts.get("REFRAME") == 12.5


# ══════════════════════════════════════════════════════════════════════════════════════
# 2. FAIL-SAFE — a monitoring bug must not become an outage
# ══════════════════════════════════════════════════════════════════════════════════════

def test_health_note_swallows_a_raising_sink(monkeypatch):
    """Make the health recorder raise; the tee must swallow it and return normally."""
    from saas import brain_health

    def _boom(*a, **kw):
        raise RuntimeError("telemetry exploded")

    monkeypatch.setattr(brain_health, "record", _boom)

    import src.extraction.reframe as reframe
    reframe._HEALTH_SINK_MISSING = False
    reframe._health_note("11111111-2222-3333-4444-555555555555", "reframe_invocation")  # no raise

    m = _llm_calls()
    m._health_note("11111111-2222-3333-4444-555555555555", "llm_call", "REFRAME", 1.0)  # no raise


def test_ingest_path_completes_when_the_health_lane_raises(monkeypatch):
    """THE PROOF: with the health path raising at EVERY tee, the atomizer still produces its
    normal result. Telemetry is not allowed to change what ingest does, ever."""
    monkeypatch.setenv("SAAS_MODE", "1")
    from saas import brain_health

    def _boom(*a, **kw):
        raise RuntimeError("telemetry exploded")

    monkeypatch.setattr(brain_health, "record", _boom)

    import src.extraction.reframe as reframe
    reframe._HEALTH_SINK_MISSING = False

    calls = {"n": 0}

    async def _fake_llm(**kwargs):
        calls["n"] += 1
        return {"atoms": [{"statement": "Anna moved to Waterloo.",
                           "source": "Anna moved to Waterloo."}]}

    monkeypatch.setattr("src.api.llm_calls.call_llm_with_retry_async", _fake_llm)
    monkeypatch.setattr("src.api.llm_calls.LLMModels.get", staticmethod(lambda op: "test-model"))

    result = asyncio.run(
        reframe.reframe_to_atomic("Anna moved to Waterloo.",
                                  user_id="11111111-2222-3333-4444-555555555555"))

    assert calls["n"] == 1, "the LLM call still happened"
    assert result.used_llm is True
    assert [a.text for a in result.atoms] == ["Anna moved to Waterloo."]


def test_collect_seat_metrics_never_raises_on_a_dead_db(monkeypatch):
    """A read failure degrades to available=False, never to a 500 on the console."""
    monkeypatch.setenv("SAAS_MODE", "1")
    from saas import brain_health
    monkeypatch.setattr(brain_health, "_schema_for_user",
                        lambda uid: (_ for _ in ()).throw(RuntimeError("db down")))
    out = brain_health.collect_seat_metrics("11111111-2222-3333-4444-555555555555")
    assert out["available"] is False


# ══════════════════════════════════════════════════════════════════════════════════════
# 3. THE PROFILE MUST BIND — not merely display
# ══════════════════════════════════════════════════════════════════════════════════════

def _bind_profile(monkeypatch, profile):
    """Stub the tenant-brain ContextVar read with a config carrying `profile`."""
    monkeypatch.setenv("SAAS_MODE", "1")
    import saas.llm_context as ctx
    monkeypatch.setattr(ctx, "current_llm_config",
                        lambda: {"account_id": "acct", "latency_profile": profile})


@pytest.mark.parametrize("profile,expected", [
    ("hosted-fast", 6.0),     # no floors declared → today's default, unchanged
    ("local", 45.0),          # the design doc's number, and the fix for the 2026-07-31 incident
    ("slow", 90.0),
    (None, 6.0),              # never calibrated → unchanged
    ("nonsense", 6.0),        # an unknown stored value must never widen anything
])
def test_profile_binds_to_the_reframe_budget(monkeypatch, profile, expected):
    _bind_profile(monkeypatch, profile)
    m = _llm_calls()
    assert m.LLMTimeouts.get("REFRAME") == expected


def test_profile_only_ever_widens_never_narrows(monkeypatch):
    """max(env-or-default, floor): an operator who widened a budget globally keeps their value,
    and a profile can NEVER introduce a timeout that today's config would not have had."""
    monkeypatch.setenv("LLM_TIMEOUT_REFRAME", "120")
    _bind_profile(monkeypatch, "local")            # local floor is 45s, env is 120s
    m = _llm_calls()
    assert m.LLMTimeouts.get("REFRAME") == 120.0


def test_profile_read_failure_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    import saas.llm_context as ctx
    monkeypatch.setattr(ctx, "current_llm_config",
                        lambda: (_ for _ in ()).throw(RuntimeError("control plane down")))
    m = _llm_calls()
    assert m.LLMTimeouts.get("REFRAME") == 6.0


@pytest.mark.parametrize("p95_ms,expected", [
    (900, "hosted-fast"),
    (3_999, "hosted-fast"),
    (4_000, "local"),
    (12_400, "local"),        # the design doc's worked example
    (19_999, "local"),
    (20_000, "slow"),
    (None, "hosted-fast"),    # unusable measurement → never silently widens
    (0, "hosted-fast"),
])
def test_profile_resolution_is_deterministic(p95_ms, expected):
    from saas.brain_profile import resolve_profile_from_p95
    assert resolve_profile_from_p95(p95_ms) == expected


# ══════════════════════════════════════════════════════════════════════════════════════
# 4. THE HISTOGRAM MATH
# ══════════════════════════════════════════════════════════════════════════════════════

def test_latency_bucketing_is_monotone():
    from saas.brain_health import _latency_bucket_ms
    assert _latency_bucket_ms(0.1) == 250
    assert _latency_bucket_ms(5.5) == 6_000
    assert _latency_bucket_ms(6.0) == 6_000
    assert _latency_bucket_ms(6.001) == 8_000
    assert _latency_bucket_ms(1000.0) == 2_147_483_647


def test_percentile_is_a_nearest_rank_range():
    """Read off the histogram → the BUCKET the q-th observation lands in, so the console can
    say 'p95 is 12–20s' instead of inventing a number the histogram cannot support."""
    from saas.brain_health import _percentile_range_ms
    hist = {1_000: 90, 20_000: 10}     # 90 fast calls, 10 slow ones
    assert _percentile_range_ms(hist, 0.50) == (None, 1_000)
    assert _percentile_range_ms(hist, 0.95) == (1_000, 20_000)
    assert _percentile_range_ms({}, 0.95) is None


def test_record_buckets_into_the_buffer(monkeypatch):
    """Recording a call increments BOTH the count cell and its latency-histogram cell."""
    monkeypatch.setenv("SAAS_MODE", "1")
    from saas import brain_health
    brain_health._buf.clear()
    # Hold the flush: record() hands the buffer to a flush thread (and CLEARS it) once
    # _FLUSH_INTERVAL_S (60s) has elapsed since the last flush. When this ran more than a minute
    # after the module was first imported (it is test #22 of this file; the file takes ~66s) the
    # very first record flushed and this read [] - order/timing dependence (#44). The buffering is
    # what is under test here; the flush has its own pins.
    monkeypatch.setattr(brain_health, "_maybe_flush_async", lambda: None)
    uid = "11111111-2222-3333-4444-555555555555"
    brain_health.record(uid, brain_health.M_LLM_CALL, operation="REFRAME", elapsed_seconds=7.0)
    metrics = sorted({k[3] for k in brain_health._buf})
    assert metrics == ["llm_call", "llm_latency"]
    lat_cell = [k for k in brain_health._buf if k[3] == "llm_latency"][0]
    assert lat_cell[4] == 8_000          # 7.0s lands in the (6s, 8s] bucket
    brain_health._buf.clear()


def test_unknown_metric_names_are_refused(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    from saas import brain_health
    brain_health._buf.clear()
    brain_health.record("11111111-2222-3333-4444-555555555555", "made_up_metric")
    assert brain_health._buf == {}


# ══════════════════════════════════════════════════════════════════════════════════════
# 5. THE TENANT-FACING WORDING (banding + no value judgement)
# ══════════════════════════════════════════════════════════════════════════════════════

def _metrics(**kw):
    base = {
        "available": True, "window_hours": 24,
        "llm_calls": 0, "llm_call_failures": 0, "llm_failure_rate": None,
        "breaker_open_events": 0, "no_brain_events": 0,
        "reframe_invocations": 0, "reframe_fallbacks": 0, "atomizer_fallback_rate": None,
        "sacred_token_drops": 0, "sacred_token_drop_rate": None,
        "latency_by_operation": {},
    }
    base.update(kw)
    return base


def test_band_red_when_no_brain_is_bound():
    from saas.brain_health import assess
    v = assess(_metrics(no_brain_events=12), None, 6.0)
    assert v["band"] == "red"
    assert "No model endpoint is bound." in v["headline"]
    assert "Base URL" in v["action"]


def test_band_amber_on_atomizer_fallback_carries_the_numbers_and_blames_the_budget():
    """The load-bearing framing correction: the message must name OUR budget, not the model."""
    from saas.brain_health import assess
    m = _metrics(llm_calls=100, llm_failure_rate=0.0, reframe_invocations=100,
                 reframe_fallbacks=41, atomizer_fallback_rate=0.41,
                 latency_by_operation={"REFRAME": {"samples": 100,
                                                   "p50_ms": {"from": 8_000, "to": 12_000},
                                                   "p95_ms": {"from": 12_000, "to": 20_000}}})
    v = assess(m, "hosted-fast", 6.0)
    assert v["band"] == "amber"
    assert "41%" in v["headline"]
    assert "41 of 100" in v["detail"]
    assert "6s budget" in v["detail"]
    assert "12s–20s" in v["detail"]
    assert "not a fault in your model" in v["detail"]
    # never a value judgement about their choice of model
    lowered = (v["headline"] + v["detail"] + v["action"]).lower()
    for banned in ("too slow", "bad model", "underperform", "weak model", "poor model"):
        assert banned not in lowered


def test_band_green_reports_real_numbers():
    from saas.brain_health import assess
    m = _metrics(llm_calls=500, llm_call_failures=2, llm_failure_rate=0.004,
                 reframe_invocations=120, reframe_fallbacks=1, atomizer_fallback_rate=0.008,
                 latency_by_operation={"REFRAME": {"samples": 120,
                                                   "p50_ms": {"from": None, "to": 2_000},
                                                   "p95_ms": {"from": 2_000, "to": 4_000}}})
    v = assess(m, "hosted-fast", 6.0)
    assert v["band"] == "green"
    assert "502 endpoint calls" in v["headline"]


def test_a_small_sample_does_not_band_on_a_rate():
    """3 of 4 calls failing is noise, not a signal — a rate needs a floor of observations."""
    from saas.brain_health import assess
    v = assess(_metrics(llm_calls=1, llm_call_failures=3, llm_failure_rate=0.75), None, 6.0)
    assert v["band"] == "green"


def test_assess_on_unavailable_metrics_is_unknown_not_a_crash():
    from saas.brain_health import assess
    v = assess({"available": False}, None, 6.0)
    assert v["band"] == "unknown"
    assert v["action"]
