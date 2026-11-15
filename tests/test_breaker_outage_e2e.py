"""Drive the REAL `call_llm_with_retry_sync` seam and MEASURE time-to-open under a true outage.

The unit tests in `test_breaker_evidence_model.py` prove the counting rule. They do not prove
the CONSUMER uses it — and "assert the write, never verify the read" is this project's #1
recurring bug class. So this file makes no assertions about `record_failure`; it drives the
actual public entry point against a synthetic dead endpoint and asks the breaker's own
`is_open()` afterwards.

Two arms, identical outage, identical config, only `LLM_BREAKER_EVIDENCE` differs. If the
evidence model regressed outage detection, THIS is where it shows up.
"""

import os
import time
from datetime import datetime, timedelta

import httpx
import pytest

from src.api import llm_calls as L


_EXCEED_BODY = (
    '{"error":{"code":400,"message":"the request exceeds the available context size '
    '(9001 tokens > 4096 tokens)","type":"exceed_context_size_error"}}'
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """One endpoint, no retries/backoff, no rate limiter, no context guard, no Redis, no SaaS."""
    monkeypatch.setenv("LLM_CONTEXT_GUARD", "false")     # isolate the BREAKER, not the guard
    monkeypatch.setenv("REDIS_COORD", "false")
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setattr(L, "_get_endpoint_list", lambda: ["http://brain.invalid/v1/chat/completions"])
    monkeypatch.setattr(L, "_saas_brain_unavailable", lambda: False)
    monkeypatch.setattr(L, "_tenant_egress_blocked", lambda: False)
    # ⚠️ The stub's RETURN VALUE became load-bearing when pacing moved INTO the retry loop
    # (one acquire per POST): the call path treats a falsy return as "no capacity — defer",
    # so a None here deferred every call BEFORE it reached the synthetic dead endpoint; the
    # breaker never saw a POST and never opened (calls=None, 5 red tests carrying no signal).
    # True = "capacity granted", which is what "no rate limiter" has always meant here (the
    # real gate also returns True when the limiter is disabled).
    monkeypatch.setattr(L, "apply_rate_limit_sync", lambda operation="DEFAULT": True)
    monkeypatch.setattr(L, "_health_note", lambda *a, **k: None)
    L._llm_circuit_breaker.reset()
    yield
    L._llm_circuit_breaker.reset()


def _install_transport(monkeypatch, make_failure):
    """Every POST fails the way `make_failure()` says. Calls are counted."""
    stats = {"posts": 0}

    def _post(_client, url, **kw):
        stats["posts"] += 1
        outcome = make_failure()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # an httpx.Response; raise_for_status() will fire

    monkeypatch.setattr(L.llm_source_ip, "post_sync", _post)
    return stats


def _resp(status, body=""):
    req = httpx.Request("POST", "http://brain.invalid/v1/chat/completions")
    return httpx.Response(status, text=body, request=req)


def _drive_until_open(max_calls=60):
    """Call the REAL entry point until the breaker opens. Returns (calls, seconds)."""
    t0 = time.perf_counter()
    for n in range(1, max_calls + 1):
        try:
            L.call_llm_with_retry_sync(
                messages=[{"role": "user", "content": "hello"}],
                model="test-model", user_id="u1", operation="EXTRACT",
                max_retries=1, timeout=0.01,
            )
        except Exception:
            pass  # an exhausted call raises; that is the path under test
        if L._llm_circuit_breaker.is_open():
            return n, time.perf_counter() - t0
    return None, time.perf_counter() - t0


# ── THE MEASUREMENT ───────────────────────────────────────────────────────────

def test_true_outage_time_to_open_is_not_regressed(monkeypatch, capsys):
    """A genuinely dead endpoint must open the breaker in the SAME number of calls, ON or OFF."""
    results = {}
    for arm in ("false", "true"):
        monkeypatch.setenv("LLM_BREAKER_EVIDENCE", arm)
        L._llm_circuit_breaker.reset()
        stats = _install_transport(monkeypatch, lambda: httpx.ConnectError("endpoint is down"))
        calls, secs = _drive_until_open()
        results[arm] = (calls, secs, stats["posts"])

    with capsys.disabled():
        print(f"\n  TRUE OUTAGE (ConnectError), real call_llm_with_retry_sync seam:")
        for arm, label in (("false", "legacy      "), ("true", "evidence-on ")):
            c, s, p = results[arm]
            print(f"    {label} opened after {c} calls / {p} POSTs / {s*1000:.1f} ms")

    n_off, _, _ = results["false"]
    n_on, _, _ = results["true"]
    assert n_off is not None and n_on is not None, "the breaker must open under a true outage"
    assert n_on == n_off, (
        f"REGRESSION: evidence model opened after {n_on} calls, legacy after {n_off}. "
        f"A real outage must trip as promptly as it does today."
    )
    # RE-PINNED (BUG-brain-connect-timeout-burns-full-budget-20261026): a true outage used
    # to open at exactly failure_threshold mixed failures — measured ~25 × 90s ≈ 37 min of
    # dead-brain traffic in the bug. Now TWO consecutive CONNECT-phase exhausted calls
    # (both arms raise httpx.ConnectError — the host never answered a SYN) open the circuit
    # on their own, short of the legacy threshold, so a dead host opens the breaker in
    # seconds, not budget units. The legacy counter itself is unchanged and still pinned by
    # test_breaker_evidence_model.py::test_outage_trips_at_exactly_the_legacy_failure_count.
    assert n_on == min(L._llm_circuit_breaker.failure_threshold, L.connect_trip_n())


def test_true_outage_via_503_also_trips(monkeypatch, capsys):
    """A 5xx is the dependency talking about ITSELF — must count in full, like a timeout."""
    monkeypatch.setenv("LLM_BREAKER_EVIDENCE", "true")
    _install_transport(monkeypatch, lambda: _resp(503, "upstream unavailable"))
    calls, secs = _drive_until_open()
    with capsys.disabled():
        print(f"\n  TRUE OUTAGE (HTTP 503): opened after {calls} calls / {secs*1000:.1f} ms")
    assert calls == L._llm_circuit_breaker.failure_threshold


# ── THE DEFECT, through the same real seam ────────────────────────────────────

def test_repeated_context_overrun_400_never_opens_the_breaker(monkeypatch, capsys):
    """THE CONFIRMED BUG, driven end-to-end.

    Note the context guard is DISABLED here on purpose: the guard already intercepts this
    one error, but it fixed a symptom. This proves the breaker itself now refuses to treat a
    repeated deterministic rejection as availability evidence — the PRINCIPLE, not the case.
    """
    monkeypatch.setenv("LLM_BREAKER_EVIDENCE", "true")
    _install_transport(monkeypatch, lambda: _resp(400, _EXCEED_BODY))
    calls, secs = _drive_until_open(max_calls=40)
    with capsys.disabled():
        print(f"\n  40 identical 400 exceed_context_size_error via the real seam: "
              f"breaker open? {L._llm_circuit_breaker.is_open()} "
              f"(failures={L._llm_circuit_breaker.failures}, "
              f"suppressed={L._llm_circuit_breaker.suppressed})")
    assert calls is None, "40 repeats of ONE deterministic rejection must not open the breaker"
    assert L._llm_circuit_breaker.failures == 1
    assert L._llm_circuit_breaker.suppressed == 39


def test_flag_off_reproduces_the_bug_end_to_end(monkeypatch):
    """The rollback lever is real: OFF restores the legacy (buggy) behaviour byte-for-byte."""
    monkeypatch.setenv("LLM_BREAKER_EVIDENCE", "false")
    _install_transport(monkeypatch, lambda: _resp(400, _EXCEED_BODY))
    calls, _ = _drive_until_open(max_calls=40)
    assert calls == L._llm_circuit_breaker.failure_threshold, (
        "with the flag OFF the 400s must still open the breaker — that is what makes it a "
        "true rollback lever rather than a partial one"
    )


def test_a_success_recovers_and_clears_the_evidence(monkeypatch):
    """The endpoint coming back must fully reset the accounting, not leave residue.

    UPDATED for the connect-phase trip (BUG-brain-connect-timeout-burns-full-budget-20261026):
    two connect-fail calls now OPEN the breaker, so the third call is refused at the entry
    check (no POST, no new evidence) and the recovery probe is only ADMITTED once the
    reset window has elapsed — exactly the production half-open path. The test therefore
    drives the full cycle: open on the streak → window elapses → probe succeeds → every
    counter (legacy AND connect streak) cleared.
    """
    monkeypatch.setenv("LLM_BREAKER_EVIDENCE", "true")
    monkeypatch.setattr(L, "_parse_llm_response_robust",
                        lambda r: {"choices": [{"message": {"content": "{}"}}]})
    state = {"fail": True}
    _install_transport(
        monkeypatch,
        lambda: httpx.ConnectError("down") if state["fail"] else _resp(200, "{}"),
    )
    for _ in range(3):
        try:
            L.call_llm_with_retry_sync(messages=[{"role": "user", "content": "x"}],
                                       model="m", user_id="u", operation="EXTRACT",
                                       max_retries=1, timeout=0.01)
        except Exception:
            pass
    # Calls 1-2 recorded and OPENED the breaker (trip=2); call 3 was refused at the entry
    # check — an open breaker stops burning POSTs against the dead host. That refusal is
    # the fix working, so the legacy counter reads 2, not 3.
    assert L._llm_circuit_breaker.failures == 2
    assert L._llm_circuit_breaker.consecutive_connect_failures == 2
    assert L._llm_circuit_breaker.is_open()
    # The reset window elapses (30s of silence) — the half-open probe is admitted.
    L._llm_circuit_breaker.last_failure_time = (
        datetime.utcnow() - timedelta(seconds=L._llm_circuit_breaker.timeout_seconds + 1))
    assert not L._llm_circuit_breaker.is_open()
    state["fail"] = False
    L.call_llm_with_retry_sync(messages=[{"role": "user", "content": "x"}],
                               model="m", user_id="u", operation="EXTRACT",
                               max_retries=1, timeout=0.01)
    assert L._llm_circuit_breaker.failures == 0
    assert L._llm_circuit_breaker.signatures == {}
    assert L._llm_circuit_breaker.consecutive_connect_failures == 0, (
        "the success must clear the connect streak too — no residue in either counter")
