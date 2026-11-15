"""The TENANT-FACING half of the 429 fallback: what a human actually reads.

A guessed number presented as a measurement is worse than no number, so the disclaimer has
to survive the whole trip — pacer → Redis (scope-bound) → GET /tenant/brain-health payload.
This suite drives that trip for real; it does not assert on the write and hope.

Redis-gated by necessity (the publication IS the seam), so the gate is explicit and the
report must state whether it ran. Run with:
    FLTEST_REDIS_URL=redis://172.20.0.2:6379/9 python3 tools/fltest.py --bug RATEFALLBACK \\
        --test tests/test_rate_429_fallback_surface.py
"""

import os

import pytest

from src.api import llm_calls, redis_coord

REDIS_URL = os.environ.get("FLTEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="FLTEST_REDIS_URL not set — this suite exercises the real Redis publication seam",
)


@pytest.fixture(autouse=True)
def _redis_env(monkeypatch):
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    redis_coord.reset_client()
    c = redis_coord.client()
    if c is None:
        pytest.fail("FLTEST_REDIS_URL is set but no client could be built")
    yield
    redis_coord.reset_client()


def _scope(ident="brainA"):
    return redis_coord.scope_for_endpoint(f"https://api.example.invalid/{ident}", "m")


def test_an_assumption_round_trips_and_is_readable_only_under_its_own_scope():
    """Bar #5 at the storage layer: the ceiling never leaks outside its brain scope."""
    a, b = _scope("A"), _scope("B")
    redis_coord.clear_rate_assumption(a)
    redis_coord.clear_rate_assumption(b)

    entry = llm_calls._RATE_429_FALLBACK["api.cerebras.ai"]
    disclaimer = llm_calls.rate_assumption_disclaimer(5, llm_calls.FLOOR_TABLE, entry)
    assert redis_coord.publish_rate_assumption(a, {
        "state": "assumed", "provenance": llm_calls.FLOOR_TABLE, "assumed_rpm": 5,
        "tier": entry["tier"], "source": entry["source"], "recorded": entry["recorded"],
        "disclaimer": disclaimer,
    }) is True

    got = redis_coord.read_rate_assumption(a)
    assert got is not None
    assert got["state"] == "assumed"
    assert got["disclaimer"] == disclaimer
    assert got["source"] == entry["source"]

    # The other brain must know nothing about it.
    assert redis_coord.read_rate_assumption(b) is None
    redis_coord.clear_rate_assumption(a)
    assert redis_coord.read_rate_assumption(a) is None


def test_the_console_payload_labels_an_assumed_ceiling_and_carries_the_disclaimer():
    """The exact wording a tenant sees on the Brain Health card."""
    from saas import tenant_console

    scope = _scope("console")
    entry = llm_calls._RATE_429_FALLBACK["api.cerebras.ai"]
    disclaimer = llm_calls.rate_assumption_disclaimer(4.5, llm_calls.FLOOR_TABLE, entry)
    redis_coord.publish_rate_assumption(scope, {
        "state": "assumed", "provenance": llm_calls.FLOOR_TABLE, "assumed_rpm": 4.5,
        "tier": entry["tier"], "source": entry["source"], "recorded": entry["recorded"],
        "disclaimer": disclaimer,
    })
    try:
        payload = tenant_console._brain_meter_payload(scope, {"max_rpm": 300}, 24)
        thr = payload["throughput"]
        assert thr["ceiling_source"] == "assumed"
        assert thr["ceiling_rpm"] == pytest.approx(4.5)
        assert thr["ceiling_disclaimer"] == disclaimer
        assert thr["ceiling_basis"]["source"] == entry["source"]
        assert thr["ceiling_basis"]["recorded"] == entry["recorded"]
        assert thr["ceiling_basis"]["tier"] == entry["tier"]
    finally:
        redis_coord.clear_rate_assumption(scope)


def test_a_non_rate_429_tells_the_tenant_the_ceiling_was_deliberately_left_alone():
    from saas import tenant_console

    scope = _scope("notrate")
    note = llm_calls.rate_refusal_disclaimer("non_rate_code:1113")
    redis_coord.publish_rate_assumption(scope, {
        "state": "not_rate_limited", "reason": "non_rate_code:1113", "disclaimer": note,
    })
    try:
        thr = tenant_console._brain_meter_payload(scope, {"max_rpm": 300}, 24)["throughput"]
        # The number is NOT relabelled as assumed — we changed nothing — but the tenant is
        # told why the endpoint is 429ing and where to look.
        assert thr["ceiling_source"] != "assumed"
        assert thr["ceiling_disclaimer"] == note
        assert "did NOT change your rate limit" in thr["ceiling_disclaimer"]
        assert thr["ceiling_basis"] is None
    finally:
        redis_coord.clear_rate_assumption(scope)


def test_no_assumption_means_no_disclaimer_ie_todays_output_is_unchanged():
    from saas import tenant_console

    scope = _scope("clean")
    redis_coord.clear_rate_assumption(scope)
    thr = tenant_console._brain_meter_payload(scope, {"max_rpm": 300}, 24)["throughput"]
    assert thr["ceiling_disclaimer"] is None
    assert thr["ceiling_basis"] is None
    assert thr["ceiling_source"] in ("live", "configured", "unknown")


def test_the_pacer_publishes_under_the_brain_scope_it_actually_paced(monkeypatch):
    """End to end: a headerless 429 in the pacer lands a readable disclaimer for the console,
    against the SAME scope the bucket was keyed on."""
    scope = _scope("e2e")
    redis_coord.clear_rate_assumption(scope)
    llm_calls.reset_rate_limiter()
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("LLM_RATE_429_FALLBACK", "1")
    monkeypatch.setenv("LLM_MAX_RPM", "480")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.cerebras.ai/v1")
    monkeypatch.setattr(llm_calls, "_current_llm_config_or_none", lambda: None)
    monkeypatch.setattr(llm_calls, "_resolve_brain_scope", lambda: scope)

    try:
        assert llm_calls.apply_rate_limit_sync("TEST") is True

        class R:
            status_code = 429
            headers = {}
            text = ""

        llm_calls._observe_rate_headers(R(), "TEST")

        got = redis_coord.read_rate_assumption(scope)
        assert got is not None, "the pacer applied an assumption but published nothing"
        assert got["state"] == "assumed"
        assert got["provenance"] == llm_calls.FLOOR_TABLE
        assert "NOT a measurement" in got["disclaimer"]
        assert "inference-docs.cerebras.ai" in got["disclaimer"]

        from saas import tenant_console
        thr = tenant_console._brain_meter_payload(scope, {"max_rpm": 300}, 24)["throughput"]
        assert thr["ceiling_source"] == "assumed"
        assert thr["ceiling_disclaimer"] == got["disclaimer"]
    finally:
        redis_coord.clear_rate_assumption(scope)
        llm_calls.reset_rate_limiter()


def test_a_measured_header_withdraws_the_published_assumption(monkeypatch):
    """Bar #4 at the tenant surface: the disclaimer disappears when the guess is replaced."""
    scope = _scope("withdraw")
    redis_coord.clear_rate_assumption(scope)
    llm_calls.reset_rate_limiter()
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("LLM_RATE_HEADER_LEARNING", "1")
    monkeypatch.setenv("LLM_RATE_429_FALLBACK", "1")
    monkeypatch.setenv("LLM_MAX_RPM", "480")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.cerebras.ai/v1")
    monkeypatch.setattr(llm_calls, "_current_llm_config_or_none", lambda: None)
    monkeypatch.setattr(llm_calls, "_resolve_brain_scope", lambda: scope)

    try:
        llm_calls.apply_rate_limit_sync("TEST")

        class Bare:
            status_code = 429
            headers = {}
            text = ""

        llm_calls._observe_rate_headers(Bare(), "TEST")
        assert redis_coord.read_rate_assumption(scope) is not None

        class Measured:
            status_code = 200
            headers = {
                "x-ratelimit-limit-requests": "1000",
                "x-ratelimit-remaining-requests": "100",
                "x-ratelimit-reset-requests": "60s",
            }
            text = ""

        llm_calls._observe_rate_headers(Measured(), "TEST")

        assert redis_coord.read_rate_assumption(scope) is None, \
            "a real measurement must withdraw the disclaimer, not sit alongside it"
        from saas import tenant_console
        thr = tenant_console._brain_meter_payload(scope, {"max_rpm": 300}, 24)["throughput"]
        assert thr["ceiling_disclaimer"] is None
        assert thr["ceiling_source"] != "assumed"
    finally:
        redis_coord.clear_rate_assumption(scope)
        llm_calls.reset_rate_limiter()
