"""The circuit breaker must trip on EVIDENCE THE DEPENDENCY IS UNAVAILABLE — nothing else.

Confirmed defect this pins: the breaker is keyed per ACCOUNT, so when it opens EVERY seat on
that account is blocked. Three `400 exceed_context_size_error` rejections from ONE well-grown
tenant opened it and degraded every correctly-sized turn behind them.

The rule under test: **the same request failing N times is ONE fact observed N times.**

The RISK of that rule — and therefore the most important test in this file — is that a real
outage might now trip more slowly. `test_outage_trips_at_exactly_the_legacy_failure_count`
and `test_outage_time_to_open_not_regressed` MEASURE it. They must never be relaxed.
"""

import os
import time

import httpx
import pytest

from src.api import llm_calls as L


# ── helpers ───────────────────────────────────────────────────────────────────

def _breaker(threshold=5, timeout=30):
    return L.CircuitBreakerState(failure_threshold=threshold, timeout_seconds=timeout)


def _status_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://brain.local/v1/chat/completions")
    resp = httpx.Response(status, text=body, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


_EXCEED_BODY = (
    '{"error":{"code":400,"message":"the request exceeds the available context size. '
    'try increasing the context size or enable context shift (9001 tokens > 4096 tokens)",'
    '"type":"exceed_context_size_error"}}'
)


@pytest.fixture(autouse=True)
def _evidence_on(monkeypatch):
    monkeypatch.setenv("LLM_BREAKER_EVIDENCE", "true")
    yield


def _fail(b, exc, operation="EXTRACT"):
    """Drive the REAL seam: classify then record, exactly as the call path does."""
    klass, token = L.classify_failure(exc)
    b.record_failure(klass, L._failure_signature(klass, token, operation))
    return klass


# ── classification ────────────────────────────────────────────────────────────

def test_context_overrun_400_is_a_verdict_on_the_request_not_the_dependency():
    klass, token = L.classify_failure(_status_error(400, _EXCEED_BODY))
    assert klass == L.CLASS_REQUEST
    # The structured error TYPE enters the signature; the free-text message (which carries
    # per-request token counts) must not, or every rejection would look unique.
    assert token == "http_400:exceed_context_size_error"
    assert "9001" not in token


@pytest.mark.parametrize("exc", [
    httpx.ConnectTimeout("connect timed out"),
    httpx.ReadTimeout("read timed out"),
    httpx.ConnectError("connection refused"),
    httpx.PoolTimeout("pool exhausted"),
    httpx.RemoteProtocolError("server disconnected"),
    _status_error(502),
    _status_error(503),
    _status_error(504),
    _status_error(429),
])
def test_transport_and_5xx_are_evidence_about_the_dependency(exc):
    assert L.classify_failure(exc)[0] == L.CLASS_DEPENDENCY


def test_unknown_failure_defaults_to_dependency_so_it_is_never_LESS_sensitive():
    """Fail-safe: anything unclassified must behave exactly as legacy (counted in full)."""
    assert L.classify_failure(None)[0] == L.CLASS_DEPENDENCY
    assert L.classify_failure(ValueError("who knows"))[0] == L.CLASS_DEPENDENCY


def test_free_text_error_body_contributes_no_token():
    """A prose body must not enter the signature — it would defeat de-duplication."""
    _, token = L.classify_failure(_status_error(400, "Bad Request: prompt too long at 9001"))
    assert token == "http_400"


# ── THE DEFECT ────────────────────────────────────────────────────────────────

def test_one_repeated_deterministic_rejection_never_opens_the_breaker():
    """THE CONFIRMED BUG. 3 identical 400s opened the breaker for every seat on the account."""
    b = _breaker(threshold=5)
    for _ in range(50):
        _fail(b, _status_error(400, _EXCEED_BODY))
    assert not b.is_open(), "a single deterministic rejection is ONE fact, not 50"
    assert b.failures == 1
    assert b.suppressed == 49


def test_five_hundred_DIFFERENT_oversized_documents_are_still_one_fact():
    """The payload is deliberately NOT in the signature.

    Fingerprinting the request body would make 500 different oversized documents look like
    500 independent facts — the exact bug, re-created. The signature is the CLASS of
    rejection, not the instance.
    """
    b = _breaker(threshold=5)
    for i in range(500):
        exc = _status_error(400, _EXCEED_BODY.replace("9001", str(9000 + i)))
        _fail(b, exc)
    assert not b.is_open()
    assert b.failures == 1


def test_distinct_rejection_shapes_DO_accumulate_evidence():
    """An endpoint rejecting every operation shape is likely genuinely misconfigured."""
    b = _breaker(threshold=5)
    for op in ("EXTRACT", "REFRAME", "VALIDATION", "INTENT_CLASSIFICATION", "RETRACTION"):
        _fail(b, _status_error(400, _EXCEED_BODY), operation=op)
    assert b.failures == 5
    assert b.is_open(), "five distinct rejection shapes IS diversity — trip"


# ── THE RISK: a real outage must still trip fast ──────────────────────────────

def test_outage_trips_at_exactly_the_legacy_failure_count():
    """A genuine outage: every call fails at the transport. Must open on failure #5, as today."""
    b = _breaker(threshold=5)
    for i in range(1, 5):
        _fail(b, httpx.ConnectTimeout("endpoint is down"))
        assert not b.is_open(), f"must not open early (after {i})"
    _fail(b, httpx.ConnectTimeout("endpoint is down"))
    assert b.is_open(), "5th dependency failure must open the breaker — unchanged from legacy"
    assert b.suppressed == 0, "dependency failures are NEVER de-duplicated"


def test_outage_during_a_retry_storm_of_ONE_turn_still_trips():
    """The retry-storm case, stated explicitly.

    `_ingest_with_retry` re-POSTs a byte-IDENTICAL body, so every retry of one turn produces
    an identical request. If the signature included the payload this would look like one
    fact and mask a real outage. It does not, because a real outage is CLASS_DEPENDENCY and
    dependency evidence is never de-duplicated — identical or not.
    """
    b = _breaker(threshold=5)
    for _ in range(5):
        _fail(b, httpx.ConnectTimeout("same turn, same body, endpoint still down"))
    assert b.is_open()


def test_outage_time_to_open_not_regressed():
    """MEASURED, not asserted: evidence-ON must not open later than evidence-OFF.

    Same synthetic outage driven through both configurations; compares BOTH the number of
    failures needed and the wall-clock time to open.
    """
    def run(evidence: str):
        os.environ["LLM_BREAKER_EVIDENCE"] = evidence
        b = _breaker(threshold=5)
        t0 = time.perf_counter()
        n = 0
        while not b.is_open() and n < 1000:
            n += 1
            _fail(b, httpx.ConnectTimeout("outage"))
        return n, time.perf_counter() - t0

    try:
        n_off, t_off = run("false")
        n_on, t_on = run("true")
    finally:
        os.environ["LLM_BREAKER_EVIDENCE"] = "true"

    print(f"\n  time-to-open  legacy: {n_off} failures / {t_off*1000:.3f} ms"
          f"   evidence-model: {n_on} failures / {t_on*1000:.3f} ms")
    assert n_on == n_off == 5, "an outage must open at the same failure count as legacy"
    # Generous ceiling: this measures that no I/O or lock was added to the hot failure path.
    assert t_on < max(t_off * 5, 0.05), "evidence accounting must add no material latency"


def test_a_deterministic_rejection_does_not_delay_a_real_outage():
    """Mixed traffic: the suppressed 400s must not consume the budget for real evidence."""
    b = _breaker(threshold=5)
    for _ in range(200):
        _fail(b, _status_error(400, _EXCEED_BODY))       # 1 unit, then suppressed
    for _ in range(4):
        _fail(b, httpx.ConnectTimeout("now the endpoint really dies"))
    assert b.is_open(), "1 request-unit + 4 dependency units == threshold 5"


# ── flag OFF is byte-for-byte legacy ──────────────────────────────────────────

def test_flag_off_reproduces_the_legacy_counter_exactly(monkeypatch):
    monkeypatch.setenv("LLM_BREAKER_EVIDENCE", "false")
    b = _breaker(threshold=5)
    for _ in range(5):
        _fail(b, _status_error(400, _EXCEED_BODY))
    assert b.failures == 5
    assert b.suppressed == 0
    assert b.is_open(), "OFF must reproduce the old (buggy) behaviour exactly — that is the lever"


def test_default_record_failure_signature_is_legacy():
    """Any caller that has not been taught the evidence model is unchanged."""
    b = _breaker(threshold=3)
    for _ in range(3):
        b.record_failure()
    assert b.failures == 3 and b.is_open()


# ── window / lifecycle ────────────────────────────────────────────────────────

def test_success_clears_the_signature_memory():
    b = _breaker(threshold=5)
    _fail(b, _status_error(400, _EXCEED_BODY))
    b.record_success()
    assert b.signatures == {} and b.failures == 0 and b.suppressed == 0
    _fail(b, _status_error(400, _EXCEED_BODY))
    assert b.failures == 1, "after a success the signature is fresh evidence again"


def test_window_expiry_lets_a_signature_count_again():
    b = _breaker(threshold=5, timeout=1)
    _fail(b, _status_error(400, _EXCEED_BODY))
    assert b.failures == 1
    b.last_failure_time = b.last_failure_time.replace(year=b.last_failure_time.year - 1)
    _fail(b, _status_error(400, _EXCEED_BODY))
    assert b.failures == 2, "a NEW window is new evidence — suppression is never permanent"


def test_signature_memory_is_bounded_and_degrades_to_legacy():
    """A pathological spread of signatures must not grow memory without limit."""
    b = _breaker(threshold=10_000)
    for i in range(L.CircuitBreakerState._MAX_SIGNATURES + 40):
        _fail(b, _status_error(400, _EXCEED_BODY), operation=f"OP_{i}")
    assert len(b.signatures) <= L.CircuitBreakerState._MAX_SIGNATURES
    # Past the cap every failure counts (legacy behaviour) — fail-safe, never fail-blind.
    assert b.failures == L.CircuitBreakerState._MAX_SIGNATURES + 40


def test_health_surface_reports_the_evidence_accounting():
    """A suppressed-but-real problem must be VISIBLE, not merely absent."""
    L._llm_circuit_breaker.reset()
    _fail(L._llm_circuit_breaker, _status_error(400, _EXCEED_BODY))
    _fail(L._llm_circuit_breaker, _status_error(400, _EXCEED_BODY))
    st = L.get_circuit_breaker_status()
    try:
        assert st["evidence_model_enabled"] is True
        assert st["suppressed_failures"] == 1
        assert st["distinct_signatures"] == 1
        assert "tenant_breakers" in st, "per-account breakers must appear on the health surface"
    finally:
        L._llm_circuit_breaker.reset()
