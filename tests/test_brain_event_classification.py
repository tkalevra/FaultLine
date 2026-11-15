"""Customer-facing brain non-answer event log — classification, dedup, fail-safe, no-secret.

WHAT THESE PIN (and why each is here rather than a shape assertion):

  1. THE THEIR-FAULT ↔ OUR-FAULT SPLIT — the line that matters most. Every customer-side cause
     (no brain / breaker / egress / rate / unreachable / timeout / 401 / 402 / 403 / 404 / 5xx)
     classifies to a specific actionable event_type; every FaultLine-side cause (overrun / empty /
     unparseable / 400 / 422 / 413 / unmapped-4xx) classifies to None. BOTH directions are
     asserted explicitly — a classifier that emitted for everything OR for nothing would pass a
     one-direction test and ship the gaslight or the silence.
  2. NO SECRET IN ANY MESSAGE. A response body full of fake keys/tokens/bearer-strings must NOT
     appear in the emitted message. The only interpolated value is an integer status code.
  3. SPAM CONTROL. The same (brain, class) within the window emits exactly ONCE; a different
     class emits again; Redis-down FAILS OPEN (visibility > dedup) bounded by the in-process brake.
  4. FAIL-SAFE. emit_brain_nonanswer NEVER raises — not when classify breaks, not when Redis is
     down, not when eventlog raises. Observability must never break a live LLM call.
  5. THE 429 COUNTER bumps once per observed 429 and reads back from brain_meter; the
     ``brain.rate_limited`` event is a DISTINCT thing (the customer line), deduped separately.
  6. TENANT-SAFE. brain.* events are surfaceable in the per-tenant console; the event_type_prefix
     query fetches the namespace without a fan-out.
"""
import os
import sys
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

# These tests are pure-classification + fake-Redis by default; no real Redis/DB needed.
os.environ.setdefault("REDIS_COORD", "false")


# ── helpers: fake exceptions that carry an HTTP response (like httpx.HTTPStatusError) ──
class _FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeHTTPError(Exception):
    """Mimics httpx.HTTPStatusError: carries .response with .status_code and .text."""
    def __init__(self, status_code, text=""):
        self.response = _FakeResp(status_code, text)
        super().__init__(f"HTTP {status_code}")


class _FakeTransportError(Exception):
    """A bare transport/timeout exception with no .response (like httpx.ConnectError)."""
    pass


# ⚠️ REAL httpx EXCEPTIONS, NOT LOOK-ALIKES. The classifier now asks httpx's own taxonomy
# (isinstance against TimeoutException / the connection-establishment errors / TransportError)
# instead of matching type NAMES, because a name list is a maintained table and a maintained
# table is the defect this whole change removes. A hand-rolled `type("ConnectError", ...)` is
# therefore a state the live path can never produce, and a test built on one measures a
# different object than the one that ships. These are the classes the transport actually raises.
import httpx  # noqa: E402

_HTTPX_REQUEST = httpx.Request("POST", "https://endpoint.example/v1/chat/completions")


def _transport(exc_cls, message="boom"):
    """One real httpx transport error, built the way httpx builds it."""
    return exc_cls(message, request=_HTTPX_REQUEST)


def _http_error(status, text="", headers=None):
    """One real httpx.HTTPStatusError carrying a real Response — status, body and headers."""
    resp = httpx.Response(status, request=_HTTPX_REQUEST, text=text, headers=headers or {})
    return httpx.HTTPStatusError(f"HTTP {status}", request=_HTTPX_REQUEST, response=resp)


def _json_error(message):
    """An OpenAI-compatible error envelope — the shape every OpenAI-shaped provider returns."""
    import json as _json
    return _json.dumps({"error": {"message": message, "type": "invalid_request_error"}})


# Make the classify function importable both ways (saas on path).
def _classify(reason, exc=None):
    from saas import brain_events
    return brain_events.classify_brain_nonanswer(reason, exc=exc)


@pytest.fixture(autouse=True)
def _reset_dedup():
    """Clear the in-process dedup brake between every test (state would otherwise leak)."""
    from saas import brain_events
    brain_events._reset_dedup_for_test()
    yield
    brain_events._reset_dedup_for_test()


# ════════════════════════════════════════════════════════════════════════════════
# 1. THE CLASSIFICATION TABLE — both directions
# ════════════════════════════════════════════════════════════════════════════════
THEIR_FAULT_DIRECT = {
    "saas_no_brain_failclosed": "brain.no_brain",
    "circuit_breaker_open": "brain.breaker_open",
    "tenant_egress_blocked": "brain.egress_blocked",
    "rate_deferred": "brain.rate_limited",
}


@pytest.mark.parametrize("reason,expected_type", list(THEIR_FAULT_DIRECT.items()))
def test_their_fault_direct_reasons_classify_to_actionable_event(reason, expected_type):
    ev = _classify(reason)
    assert ev is not None, f"{reason} must classify (their fault), not suppress"
    assert ev[0] == expected_type
    # The message must be a non-empty, actionable sentence (not a bare token).
    assert len(ev[1]) > 20


def test_their_fault_messages_name_the_action_not_the_provider():
    # Subject-agnostic: no provider name leaks into a message (the overarching scope rule).
    for reason in THEIR_FAULT_DIRECT:
        msg = _classify(reason)[1].lower()
        for provider in ("openai", "anthropic", "cerebras", "dashscope", "openrouter",
                         "deepseek", "ollama", "llama"):
            assert provider not in msg, f"{reason} message names a provider: {provider!r}"


def test_our_fault_direct_reasons_return_none():
    """The explicitly-our-fault reasons MUST suppress — never gaslight the customer."""
    for reason in ("context_overrun_preflight", "context_overrun_server",
                   "empty_llm_content", "unparseable_llm_response"):
        assert _classify(reason) is None, (
            f"{reason} is OUR fault — it must NOT produce a customer-facing line (gaslight)")


# ── all_retries_exhausted, DERIVED from the answer (no status→copy table any more) ──
#
# WHAT CHANGED AND WHY THE OLD EXPECTATIONS WERE THE BUG. This used to pin a dict:
#   401/403 → brain.auth_rejected  "check the credential"
#   402     → brain.out_of_credit  "this account is out of credit"
#   404     → brain.model_not_found "check the model name on the LLM Brain tab"
# The 404 line is demonstrably wrong for the failure this repo has already documented — a
# doubled `/v1` in the BASE URL 404s, and "check the model name" points at the wrong field
# while the provider's own body says `Invalid URL (POST /v1/v1/chat/completions)`. And the dict
# could only speak about four codes; every other refusal was silence.
#
# The classification is now derived: an authentication CHALLENGE (the header RFC 9110 binds to
# 401/407) picks the credential class, TOO_MANY_REQUESTS or a Retry-After picks the rate class,
# the status CLASS picks provider-side vs request-side, and the provider's own message — taken
# only from a designated field of a decoded JSON envelope — is quoted rather than translated.
@pytest.mark.parametrize("status,body,expected_type", [
    (401, "", "brain.auth_rejected"),
    (401, _json_error("Wrong API Key provided"), "brain.auth_rejected"),
    (407, "", "brain.auth_rejected"),
    (403, "", "brain.request_refused"),
    (402, _json_error("Payment required to access this resource. Visit your billing tab."),
     "brain.request_refused"),
    (402, "", "brain.request_refused"),
    (404, _json_error("Invalid URL (POST /v1/v1/chat/completions)"), "brain.request_refused"),
    (429, "", "brain.rate_limited"),
    (500, "", "brain.endpoint_error"),
    (502, "", "brain.endpoint_error"),
    (503, "", "brain.endpoint_error"),
    (504, "", "brain.endpoint_error"),
    (408, "", "brain.request_refused"),
])
def test_exhausted_http_classifies_from_the_answer(status, body, expected_type):
    ev = _classify("all_retries_exhausted:HTTPStatusError", exc=_http_error(status, body))
    assert ev is not None, f"HTTP {status} was answered — it must classify, not vanish"
    assert ev[0] == expected_type
    # The status and the REGISTRY's reason phrase are always carried: they are the standard's
    # words, not ours, so they can neither drift nor be a lie.
    assert str(status) in ev[1]
    from http import HTTPStatus
    assert HTTPStatus(status).phrase in ev[1]


def test_the_providers_own_words_are_quoted_not_translated():
    """Where the endpoint explains itself, its sentence is what the customer reads.

    This is the 402 from production, verbatim. The old table answered it with a line of our
    own invention ("out of credit or over its quota") that happened to be close; for the 404
    below the same mechanism produced a line that pointed at the wrong settings field. Quoting
    is not a nicety — it is the only version that cannot be wrong about a provider we do not
    control.
    """
    ev = _classify("all_retries_exhausted:HTTPStatusError",
                   exc=_http_error(402, _json_error(
                       "Payment required to access this resource. Visit your billing tab.")))
    assert ev is not None
    assert "Visit your billing tab" in ev[1]

    ev404 = _classify("all_retries_exhausted:HTTPStatusError",
                      exc=_http_error(404, _json_error(
                          "Invalid URL (POST /v1/v1/chat/completions)")))
    assert ev404 is not None
    assert "/v1/v1/" in ev404[1]
    # ...and it must NOT invent the old remedy, which sent this person to the model field.
    assert "model name" not in ev404[1].lower()


def test_no_refusal_invents_a_remedy_the_reply_does_not_support():
    """An unexplained refusal reports the answer and explicitly declines to name a cause."""
    ev = _classify("all_retries_exhausted:HTTPStatusError", exc=_http_error(409))
    assert ev is not None
    assert "did not say why" in ev[1]
    for invented in ("firewall", "model name", "out of credit", "rate limit"):
        assert invented not in ev[1].lower()


def test_retry_after_is_read_off_the_wire_and_reported():
    """RFC 9110 §10.2.3 delta-seconds — the endpoint's own statement that this is transient."""
    ev = _classify("all_retries_exhausted:HTTPStatusError",
                   exc=_http_error(429, "", headers={"Retry-After": "20"}))
    assert ev is not None and ev[0] == "brain.rate_limited"
    assert "20 seconds" in ev[1]


@pytest.mark.parametrize("status", [400, 422, 413, 414, 415, 431])
def test_exhausted_our_payload_statuses_suppress(status):
    """400/422/413 and friends are a settled verdict on OUR payload — never gaslight.

    ``_OUR_PAYLOAD_STATUSES`` is the ONE status set that survived the table's removal, and it
    survives because it answers a different question: it decides WHOSE FAULT a refusal is, and
    it carries no copy. Judging that a 413 is our own request's problem is a judgement about
    our own request; writing a customer's remedy beside a number was not.
    """
    ev = _classify("all_retries_exhausted:HTTPStatusError", exc=_http_error(status))
    assert ev is None, f"HTTP {status} is OUR payload fault — must NOT emit a customer line"


@pytest.mark.parametrize("status", [405, 406, 409, 411, 418, 451])
def test_exhausted_other_4xx_reports_without_accusing(status):
    """Every other refusal is REPORTED, and reported without blaming anybody.

    This inverts the old expectation deliberately. The blanket "unmapped 4xx → silence" rule
    existed because the only sentence available was a hand-written accusation, and a false
    accusation is worse than a false silence. With the accusation gone there is nothing left to
    suppress: the line states the status, the registry phrase, and that the reply does not
    identify a cause. Silence had a cost too — it is why a 402 arriving without a body used to
    tell the customer nothing at all.
    """
    ev = _classify("all_retries_exhausted:HTTPStatusError", exc=_http_error(status))
    assert ev is not None
    assert ev[0] == "brain.request_refused"
    assert "did not say why" in ev[1]


@pytest.mark.parametrize("exc_cls,expected_type", [
    # Connection ESTABLISHMENT — nothing was ever sent.
    (httpx.ConnectError, "brain.endpoint_unreachable"),
    (httpx.ConnectTimeout, "brain.endpoint_unreachable"),
    (httpx.ProxyError, "brain.endpoint_unreachable"),
    # The connection was OPEN and the answer was late. A different problem, a different remedy.
    (httpx.ReadTimeout, "brain.endpoint_timeout"),
    (httpx.WriteTimeout, "brain.endpoint_timeout"),
    # It broke partway.
    (httpx.ReadError, "brain.endpoint_unreachable"),
    (httpx.RemoteProtocolError, "brain.endpoint_unreachable"),
])
def test_transport_failures_keep_timeout_and_unreachable_apart(exc_cls, expected_type):
    """A slow endpoint and an absent endpoint were ONE event type. That is the same conflation
    that let a 402 be reported as a timeout, one layer down — so they are two now.

    ⚠️ ``ConnectTimeout`` belongs with UNREACHABLE, not with the timeouts, and httpx's hierarchy
    is why this needs a test: ``ConnectTimeout`` subclasses ``TimeoutException``, so the obvious
    ordering classifies a handshake that never completed — the signature of dropped packets — as
    "your model is slow".
    """
    ev = _classify(f"all_retries_exhausted:{exc_cls.__name__}", exc=_transport(exc_cls))
    assert ev is not None, f"{exc_cls.__name__} is their endpoint, not our payload"
    assert ev[0] == expected_type


def test_pool_exhaustion_is_our_fault_and_never_becomes_a_customer_line():
    """``PoolTimeout`` is a ``TimeoutException``, so it was classified as the tenant's endpoint
    being slow and written into THEIR event log.

    It is FaultLine running out of its own outbound connections — the endpoint was never asked.
    This module's founding rule is that our own failures stay internal, so it suppresses, and
    the tenant-facing health notice says plainly that it is on our side and needs nothing from
    them (see tests/test_brain_fault_honesty.py).
    """
    ev = _classify("all_retries_exhausted:PoolTimeout", exc=_transport(httpx.PoolTimeout))
    assert ev is None, "our own saturation was written into the customer's event log"


def test_exhausted_unknown_exception_no_status_leans_our_fault():
    """An exception the transport taxonomy does not recognise → None (conservative).

    The degradation is the safety property: a class httpx adds later, or an error from some
    other client, lands here and produces SILENCE rather than a confident wrong story.
    """
    class MysteryError(Exception):
        pass
    ev = _classify("all_retries_exhausted:MysteryError", exc=MysteryError())
    assert ev is None


def test_exhausted_with_no_exception_suppresses():
    """all_retries_exhausted without the exc to classify → None (can't classify → don't guess)."""
    ev = _classify("all_retries_exhausted:HTTPStatusError", exc=None)
    assert ev is None


def test_unknown_reason_suppresses():
    """A reason not in the table → None (conservative: never emit for something unclassified)."""
    assert _classify("some_new_reason_we_did_not_add") is None
    assert _classify("") is None
    assert _classify(None) is None


# ════════════════════════════════════════════════════════════════════════════════
# 2. NO SECRET LEAKS — the response body must never reach the message
# ════════════════════════════════════════════════════════════════════════════════
def test_response_body_secret_never_leaks_into_message():
    """A body full of fake credentials must not appear in the emitted message.

    ⚠️ THIS TEST GOT MORE IMPORTANT, NOT LESS. The classifier now DOES quote the provider — so
    the old justification ("it reads the status, never the body") no longer holds and the
    property has to be defended by two mechanisms instead of one: only a DESIGNATED field of a
    decoded JSON envelope is ever quoted (an arbitrary blob is not a sentence anyone wrote for
    a human), and whatever is quoted goes through ``brain_fault._redact`` first. Both are
    exercised below.
    """
    # (1) an arbitrary, non-JSON body is not quoted at all.
    secret_body = 'sk-livesecret12345 Bearer deadbeef {"api_key":"LEAKEDKEY"} password=x'
    ev = _classify("all_retries_exhausted:HTTPStatusError",
                   exc=_http_error(503, secret_body))
    assert ev is not None  # 503 is their fault → emits
    for secret in ("sk-livesecret12345", "Bearer", "deadbeef", "LEAKEDKEY", "password=x"):
        assert secret not in ev[1], f"secret leaked into message: {secret!r}"

    # (2) a provider that writes the rejected key into its OWN message field — OpenAI does
    #     exactly this — is quoted with the credential destroyed.
    echoed = _json_error("Incorrect API key provided: sk-live-AbCd1234EfGh5678IjKl9012. "
                         "You can find your API key at https://example/keys.")
    ev2 = _classify("all_retries_exhausted:HTTPStatusError", exc=_http_error(401, echoed))
    assert ev2 is not None
    assert "sk-live-AbCd1234EfGh5678IjKl9012" not in ev2[1]
    assert "[redacted]" in ev2[1]
    # ...and the surrounding sentence still survives, or redaction has eaten the explanation.
    assert "Incorrect API key provided" in ev2[1]


def test_no_event_type_or_message_carries_a_request_body_token():
    """Across EVERY their-fault classification, the message contains no auth/body token."""
    secrets = ("bearer", "authorization", "api-key:", "sk-", "-----begin")
    cases = [
        ("saas_no_brain_failclosed", None),
        ("circuit_breaker_open", None),
        ("tenant_egress_blocked", None),
        ("rate_deferred", None),
        ("all_retries_exhausted:HTTPStatusError", _http_error(401, "sk-secret")),
        ("all_retries_exhausted:HTTPStatusError", _http_error(402, "bearer x")),
        ("all_retries_exhausted:HTTPStatusError", _http_error(502, "auth=Y")),
        ("all_retries_exhausted:ConnectError", _transport(httpx.ConnectError)),
        # ...and the same again with the tokens inside a real JSON message field, which IS a
        # value this classifier will quote.
        ("all_retries_exhausted:HTTPStatusError",
         _http_error(401, _json_error("bearer sk-abcdefghijklmnop rejected; "
                                      "authorization: Basic ZGVhZGJlZWY6cGFzc3dvcmQ="))),
    ]
    for reason, exc in cases:
        ev = _classify(reason, exc=exc)
        assert ev is not None
        low = ev[1].lower()
        for s in secrets:
            assert s not in low, f"{reason}: secret token {s!r} in message"


# ════════════════════════════════════════════════════════════════════════════════
# 3. SPAM CONTROL + 4. FAIL-SAFE — the emit seam
# ════════════════════════════════════════════════════════════════════════════════
class _InlineThread:
    """A threading.Thread stand-in whose start() runs the target INLINE (synchronous), so the
    fire-and-forget daemon-thread emit becomes testable without joins/flakiness."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)
    def join(self, *a, **k):
        pass


class _FakeRedis:
    """A tiny SETNX simulator: tracks which keys exist + their TTL via a dict."""
    def __init__(self):
        self.store = {}  # key -> value
        self.set_calls = 0
    def set(self, key, val, nx=False, ex=None):
        self.set_calls += 1
        if nx and key in self.store:
            return False
        self.store[key] = val
        return True


@pytest.fixture()
def captured_emits(monkeypatch):
    """Capture eventlog.emit_sync calls + run the daemon-thread emit inline.

    Defaults to a CUSTOMER BYO brain (ownership True) so the existing customer-branch tests run
    unchanged. Operator-branch tests request ``captured_emits_internal`` instead. The dedup is
    forced to IN-PROCESS-ONLY (redis_coord.enabled→False) so a 5-min Redis TTL key claimed by one
    test cannot suppress another in a sweep — the in-process brake IS reset between tests."""
    from saas import brain_events
    from src.api import redis_coord
    calls = []
    monkeypatch.setattr(brain_events.threading, "Thread", _InlineThread)
    monkeypatch.setattr(brain_events, "_customer_account_in_scope", lambda: True)
    monkeypatch.setattr(redis_coord, "enabled", lambda: False)
    import saas.eventlog as eventlog
    monkeypatch.setattr(eventlog, "emit_sync",
                        lambda level, et, msg, account_id=None, user_id=None, redacted=False:
                        calls.append({"level": level, "event_type": et, "message": msg,
                                      "account_id": account_id, "user_id": user_id}))
    return calls


@pytest.fixture()
def captured_emits_internal(monkeypatch):
    """Same capture, but the ownership oracle resolves to the INTERNAL/global brain (no customer
    account in scope) — so emits route to the OPERATOR branch (brain.internal.*, account_id=None)."""
    from saas import brain_events
    from src.api import redis_coord
    calls = []
    monkeypatch.setattr(brain_events.threading, "Thread", _InlineThread)
    monkeypatch.setattr(brain_events, "_customer_account_in_scope", lambda: False)
    monkeypatch.setattr(redis_coord, "enabled", lambda: False)
    import saas.eventlog as eventlog
    monkeypatch.setattr(eventlog, "emit_sync",
                        lambda level, et, msg, account_id=None, user_id=None, redacted=False:
                        calls.append({"level": level, "event_type": et, "message": msg,
                                      "account_id": account_id, "user_id": user_id}))
    return calls


def _emit(reason, exc=None, scope=None, cfg=None, call_user_id="seat-1", monkeypatch=None):
    from saas import brain_events
    brain_events.emit_brain_nonanswer(call_user_id, reason, exc=exc, scope=scope, cfg=cfg)


def test_their_fault_emits_exactly_one_line(captured_emits):
    _emit("circuit_breaker_open")
    assert len(captured_emits) == 1
    assert captured_emits[0]["event_type"] == "brain.breaker_open"
    assert captured_emits[0]["user_id"] == "seat-1"


def test_our_fault_emits_nothing(captured_emits):
    """The negation that matters: an OUR-fault reason produces ZERO customer lines."""
    _emit("empty_llm_content")
    _emit("unparseable_llm_response")
    _emit("context_overrun_server")
    assert captured_emits == []


def test_dedup_suppresses_repeat_within_window(captured_emits):
    """The same (brain, class) within the window emits ONCE — the storm brake."""
    _emit("circuit_breaker_open", call_user_id="seat-1")
    _emit("circuit_breaker_open", call_user_id="seat-1")  # duplicate → suppressed
    _emit("circuit_breaker_open", call_user_id="seat-1")  # still suppressed
    assert len(captured_emits) == 1


def test_dedup_allows_a_different_class(captured_emits):
    """A DIFFERENT reason-class is a different actionable line — both must appear."""
    _emit("circuit_breaker_open")
    _emit("tenant_egress_blocked")
    types = {c["event_type"] for c in captured_emits}
    assert types == {"brain.breaker_open", "brain.egress_blocked"}


def test_dedup_is_per_owner(captured_emits):
    """Two DIFFERENT seats (no scope, distinct user_ids) each get their own line — the dedup
    key follows the OWNER, not a global singleton."""
    _emit("circuit_breaker_open", call_user_id="seat-A")
    _emit("circuit_breaker_open", call_user_id="seat-B")
    assert len(captured_emits) == 2


def test_dedup_failopens_when_redis_down(captured_emits, monkeypatch):
    """REDIS_COORD off (Redis unavailable) → fail-OPEN: the line still emits (visibility > dedup),
    bounded by the in-process brake so a tight loop in one process still coalesces."""
    from src.api import redis_coord
    monkeypatch.setattr(redis_coord, "enabled", lambda: False)
    _emit("rate_deferred")
    assert len(captured_emits) == 1  # emitted despite no Redis


def test_cross_process_dedup_via_fake_redis_setnx(captured_emits, monkeypatch):
    """With REDIS_COORD on + a fake client, a SECOND claim for the same (brain, class) is refused
    by SET NX — the cross-process authoritative dedup. We simulate a second process that already
    holds the key."""
    from src.api import redis_coord
    fake = _FakeRedis()
    # Pre-claim the key as if another process got there first.
    scope = redis_coord.scope_for_endpoint("http://ep-x-test", "model-x")
    rkey = redis_coord.key("brnEvt", scope, "brain.breaker_open")
    fake.store[rkey] = "1"
    monkeypatch.setattr(redis_coord, "enabled", lambda: True)
    monkeypatch.setattr(redis_coord, "client", lambda: fake)
    _emit("circuit_breaker_open", scope=scope, call_user_id="seat-1")
    assert captured_emits == [], "another process owns this window → suppressed"
    # A different class for the same brain is NOT pre-claimed → emits.
    _emit("tenant_egress_blocked", scope=scope, call_user_id="seat-1")
    assert len(captured_emits) == 1


def test_emit_never_raises_when_eventlog_breaks(captured_emits, monkeypatch):
    """If eventlog.emit_sync raises, the caller is unaffected (observability never breaks a call)."""
    import saas.eventlog as eventlog
    monkeypatch.setattr(eventlog, "emit_sync",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    # Must not raise:
    _emit("saas_no_brain_failclosed")


def test_emit_never_raises_when_classify_sees_garbage(captured_emits):
    # A reason whose classification throws internally is swallowed (the try/except in classify).
    _emit(object())  # type: ignore[arg-type]  — garbage reason
    _emit(None)


def test_emit_stamps_account_and_user_from_cfg(captured_emits):
    """The event row carries BOTH account_id (tenant admin sees all) and user_id (seat-scoped)."""
    cfg = {"account_id": "acct-42", "seat_user_id": "seat-7"}
    _emit("saas_no_brain_failclosed", cfg=cfg, call_user_id="caller-fallback")
    assert captured_emits[0]["account_id"] == "acct-42"
    assert captured_emits[0]["user_id"] == "seat-7"  # cfg.seat_user_id preferred over caller


def test_emit_user_id_falls_back_to_caller_for_account_scoped_brain(captured_emits):
    """An account-scoped (shared) brain has no seat_user_id → the calling seat is stamped so the
    individual console can still scope it. (Known limitation: only the first seat to hit the
    fault sees it individually; the tenant admin sees all via account_id.)"""
    cfg = {"account_id": "corp-acct"}  # no seat_user_id
    _emit("saas_no_brain_failclosed", cfg=cfg, call_user_id="seat-99")
    assert captured_emits[0]["user_id"] == "seat-99"


# ════════════════════════════════════════════════════════════════════════════════
# 5. THE 429 COUNTER (brain_meter) — bumps once per observed 429, reads back distinctly
# ════════════════════════════════════════════════════════════════════════════════
def test_429_counter_is_a_distinct_kind():
    from src.api import brain_meter
    assert brain_meter.KIND_RATE429 == "rate429"
    assert brain_meter.KIND_RATE429 in brain_meter._KINDS


def test_429_counter_is_not_part_of_observed_or_hit_rate():
    """rate429 is summed but NOT folded into observed/hit_rate — a rate-limited-but-cached brain
    is not 'busy' and its cache hit rate is not dragged down by throttling."""
    from src.api import brain_meter
    empty = brain_meter.read_window(None)
    assert "rate429" in empty
    assert empty["rate429"] == 0


# ════════════════════════════════════════════════════════════════════════════════
# 6. TENANT-SAFE classification + the event_type_prefix query builder
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("event_type", [
    "brain.no_brain", "brain.key_undecryptable", "brain.breaker_open", "brain.egress_blocked",
    "brain.rate_limited", "brain.auth_rejected", "brain.request_refused",
    "brain.endpoint_unreachable", "brain.endpoint_timeout", "brain.endpoint_error",
])
def test_brain_event_types_are_tenant_safe(event_type):
    """Every brain.* event_type is classified tenant-safe → redacted → surfaceable in the
    per-tenant Advanced→Logs and the individual seat's health card."""
    from saas import eventlog
    assert eventlog._is_tenant_safe(event_type) is True


def test_internal_event_types_are_not_tenant_safe():
    """Operator-only / security classes stay operator-only (the fail-safe default)."""
    from saas import eventlog
    assert eventlog._is_tenant_safe("account.panic") is False
    assert eventlog._is_tenant_safe("auth.login") is False
    assert eventlog._is_tenant_safe("") is False
    assert eventlog._is_tenant_safe("unknown_thing") is False


def test_event_type_prefix_builder_adds_parameterized_like():
    """The prefix filter becomes a bound ``LIKE 'brain.%'`` param — never interpolated."""
    from saas import eventlog
    sql, params = eventlog._build_select(
        "acct-1", None, None, None, 10, 0, force_redacted=True,
        event_type_prefix="brain")
    assert "event_type LIKE %s" in sql
    assert "brain.%" in params
    # The wildcard is appended to the BOUND param, not string-interpolated into SQL.
    assert "redacted = TRUE" in sql


def test_event_type_prefix_strips_percent_to_prevent_wildcard_injection():
    """A caller-supplied '%' in the prefix is stripped so it cannot widen the match."""
    from saas import eventlog
    _, params = eventlog._build_select(
        "acct-1", None, None, None, 10, 0, force_redacted=True,
        event_type_prefix="br%ain")
    assert "brain.%" in params  # the wildcard is appended to the BOUND param, stripped of '%'


def test_exact_event_type_and_prefix_can_coexist():
    from saas import eventlog
    sql, params = eventlog._build_select(
        "acct-1", None, None, None, 10, 0, force_redacted=True,
        event_type="brain.no_brain", event_type_prefix="brain")
    assert "event_type = %s" in sql
    assert "event_type LIKE %s" in sql


# ════════════════════════════════════════════════════════════════════════════════
# PROVE THE DEDUP COUNTER AND THE EVENT ARE DISTINCT (the "same 429 isn't posted twice")
# ════════════════════════════════════════════════════════════════════════════════
def test_rate_limited_event_dedup_collapses_repeats(captured_emits):
    """A rate-429 produces the brain.rate_limited EVENT at most once per window (deduped),
    independent of how many times the counter bumps. This is the 'same 429 isn't posted twice'
    guarantee at the event-log layer."""
    for _ in range(10):
        _emit("rate_deferred", call_user_id="seat-1")
    assert len(captured_emits) == 1
    assert captured_emits[0]["event_type"] == "brain.rate_limited"


# ════════════════════════════════════════════════════════════════════════════════
# 7. BRAIN OWNERSHIP — internal/global-brain failures route OPERATOR-ONLY (no gaslight)
# ════════════════════════════════════════════════════════════════════════════════
# The defect this section pins: emit_brain_nonanswer used to emit a customer-facing brain.*
# line for ANY their-fault reason, with NO check on whether the brain was the customer's BYO
# brain or FaultLine's INTERNAL/global one. A failure of OUR brain surfaced in a customer's
# tenant/individual log — the exact gaslighting the feature exists to prevent. The ownership
# oracle is _tenant_brain() (the canonical read saas_global_brain_blocked uses); None/no-base_url
# ⇒ internal ⇒ brain.internal.* with account_id=None (operator-only).
def test_internal_brain_failure_emits_operator_event_not_customer(captured_emits_internal):
    """T_internal: with NO tenant brain bound, a their-fault reason emits EXACTLY ONE
    brain.internal.* event with account_id=None, and ZERO customer brain.* events."""
    _emit("circuit_breaker_open", call_user_id="seat-1",
          cfg={"account_id": "acct-should-not-appear"})
    assert len(captured_emits_internal) == 1
    ev = captured_emits_internal[0]
    assert ev["event_type"] == "brain.internal.breaker_open"
    assert ev["account_id"] is None, "operator event must be UNSCOPED (account_id=None)"
    assert ev["user_id"] is None


def test_internal_brain_preserves_their_fault_class_under_internal_prefix(captured_emits_internal):
    """Each their-fault class keeps its specific name under brain.internal.* — the operator
    sees WHICH cause, not a generic 'internal failure'. NOTE: ``saas_no_brain_failclosed`` is
    deliberately ABSENT here — it cannot fire on the internal/global brain (it requires a
    customer account with no brain, i.e. the no_brain sentinel, which routes customer-side)."""
    _emit("rate_deferred")
    _emit("tenant_egress_blocked")
    _emit("circuit_breaker_open")
    types = {c["event_type"] for c in captured_emits_internal}
    assert types == {"brain.internal.rate_limited",
                     "brain.internal.egress_blocked",
                     "brain.internal.breaker_open"}


def test_internal_brain_exhausted_classification_routes_to_internal(captured_emits_internal):
    """The HTTP-status classification still runs; the result is re-prefixed brain.internal.*."""
    _emit("all_retries_exhausted:HTTPStatusError", exc=_http_error(401))
    _emit("all_retries_exhausted:ConnectError", exc=_transport(httpx.ConnectError))
    types = {c["event_type"] for c in captured_emits_internal}
    assert "brain.internal.auth_rejected" in types
    assert "brain.internal.endpoint_unreachable" in types


def test_internal_brain_our_fault_still_suppresses(captured_emits_internal):
    """An OUR-fault reason suppresses in the operator branch too — it is our parsing/payload
    bug, already in the container logs, not an operator event-log line."""
    _emit("empty_llm_content")
    _emit("unparseable_llm_response")
    _emit("context_overrun_server")
    assert captured_emits_internal == []


def test_internal_brain_event_is_invisible_to_tenant_safe_query(captured_emits_internal, monkeypatch):
    """T_internal_no_account_scope: the operator event's account_id is None, so an account-scoped
    tenant_safe_query returns ZERO of them — a customer can NEVER see an internal-brain failure.

    Proven against the REAL query builder (not a mock): the WHERE clause hard-forces
    ``account_id = %s``, and ``<any value> = NULL`` is never TRUE in SQL."""
    from saas import eventlog
    _emit("circuit_breaker_open")
    assert len(captured_emits_internal) == 1
    assert captured_emits_internal[0]["account_id"] is None
    # The builder: an account-scoped query cannot match a NULL account_id row. Build the SQL the
    # tenant lane would run and confirm the account_id equality filter is present (the SQL guard;
    # the NULL-semantics guard is ``NULL = 'x'`` → NULL → row excluded, which is PostgreSQL truth).
    sql, params = eventlog._build_select(
        "any-customer-acct", None, None, None, 50, 0, force_redacted=True,
        event_type_prefix="brain.internal")
    assert "account_id = %s" in sql
    assert "any-customer-acct" in params


def test_internal_event_type_is_not_tenant_safe():
    """brain.internal.* is OPERATOR-ONLY by classification (redacted=False) — defense-in-depth on
    top of account_id=None. The tenant lane forces redacted=TRUE, so a redacted=False row is
    excluded even if a future query forgot the account scope."""
    from saas import eventlog
    assert eventlog._is_tenant_safe("brain.internal.breaker_open") is False
    assert eventlog._is_tenant_safe("brain.internal.no_brain") is False
    # Customer brain.* stays tenant-safe (the exclusion is sub-namespace-scoped, not blanket).
    assert eventlog._is_tenant_safe("brain.breaker_open") is True
    assert eventlog._is_tenant_safe("brain.rate_limited") is True


def test_internal_event_dedup_uses_operator_owner(captured_emits_internal):
    """The operator branch dedups on a stable __operator__ owner (NOT a customer account), so a
    sustained internal-brain outage emits one line per class per window, not one per call."""
    for _ in range(5):
        _emit("circuit_breaker_open", call_user_id="seat-1")
    assert len(captured_emits_internal) == 1


def test_customer_brain_unchanged_by_ownership_guard(captured_emits):
    """T_customer_unchanged: with a tenant brain bound (ownership True), a their-fault reason
    still emits the CUSTOMER brain.* event scoped account+user — the existing behavior, not
    regressed by the ownership guard."""
    _emit("circuit_breaker_open", call_user_id="seat-7",
          cfg={"account_id": "acct-42", "seat_user_id": "seat-7"})
    assert len(captured_emits) == 1
    ev = captured_emits[0]
    assert ev["event_type"] == "brain.breaker_open"  # NOT brain.internal.*
    assert ev["account_id"] == "acct-42"
    assert ev["user_id"] == "seat-7"


def test_ownership_oracle_keys_on_account_id_not_base_url(monkeypatch):
    """The ownership predicate is 'a CUSTOMER ACCOUNT is in scope' (account_id present), NOT 'a
    callable brain is resolved (base_url present)'. Those differ on the no_brain sentinel — a
    customer account that has not configured a brain — which MUST route customer-side so the
    'no brain configured' line reaches the customer's feed. Drives the REAL sentinel through the
    REAL oracle (not a stub) — the stubbed emit-path tests cannot see this shape."""
    from saas import brain_events, llm_context
    from src.api import llm_client
    # BYO brain: account_id + base_url → customer.
    monkeypatch.setattr(llm_client, "_tenant_brain",
                        lambda: {"account_id": "acct-1", "base_url": "http://ep", "model": "x"})
    assert brain_events._customer_account_in_scope() is True
    # no_brain SENTINEL (the real constructor): account_id present, NO base_url → CUSTOMER.
    # This is the shape a base_url-keyed oracle got wrong (routed it operator-side).
    monkeypatch.setattr(llm_client, "_tenant_brain",
                        lambda: llm_context._no_brain_sentinel("acct-1"))
    assert brain_events._customer_account_in_scope() is True
    # Internal/global brain: None cfg (no tenant context) → operator.
    monkeypatch.setattr(llm_client, "_tenant_brain", lambda: None)
    assert brain_events._customer_account_in_scope() is False
    # Internal: a cfg with NO account_id (cannot attribute to a customer) → operator.
    monkeypatch.setattr(llm_client, "_tenant_brain", lambda: {"base_url": "http://ep", "model": "x"})
    assert brain_events._customer_account_in_scope() is False


def test_ownership_oracle_fails_safe_to_internal(monkeypatch):
    """If _tenant_brain itself raises (saas layer absent / import fault), the oracle leans
    INTERNAL — never gaslight the customer by assuming a customer account it could not confirm."""
    from saas import brain_events
    from src.api import llm_client
    monkeypatch.setattr(llm_client, "_tenant_brain",
                        lambda: (_ for _ in ()).throw(RuntimeError("saas gone")))
    assert brain_events._customer_account_in_scope() is False


def test_no_brain_sentinel_routes_customer_not_operator(monkeypatch):
    """REGRESSION (the critic's defect): driving the REAL no_brain sentinel through the REAL
    oracle (NOT a stub — that was the coverage hole), saas_no_brain_failclosed emits a CUSTOMER
    brain.no_brain event scoped to the customer's account — NOT brain.internal.* with
    account_id=None. A base_url-keyed oracle starved the customer feed of exactly this line."""
    from saas import brain_events, llm_context
    from src.api import llm_client, redis_coord
    import saas.eventlog as eventlog
    # Capture emits inline + force in-process dedup, but DO NOT stub _customer_account_in_scope —
    # the real oracle must read the real sentinel we bind via _tenant_brain.
    calls = []
    monkeypatch.setattr(brain_events.threading, "Thread", _InlineThread)
    monkeypatch.setattr(redis_coord, "enabled", lambda: False)
    monkeypatch.setattr(eventlog, "emit_sync",
                        lambda level, et, msg, account_id=None, user_id=None, redacted=False:
                        calls.append({"level": level, "event_type": et, "message": msg,
                                      "account_id": account_id, "user_id": user_id}))
    acct = "acct-no-brain-test"
    monkeypatch.setattr(llm_client, "_tenant_brain",
                        lambda: llm_context._no_brain_sentinel(acct))
    # The real oracle, against the real sentinel, says customer-account-in-scope.
    assert brain_events._customer_account_in_scope() is True
    brain_events.emit_brain_nonanswer(
        "seat-1", "saas_no_brain_failclosed",
        cfg=llm_context._no_brain_sentinel(acct))
    assert len(calls) == 1
    ev = calls[0]
    assert ev["event_type"] == "brain.no_brain"   # customer line, NOT brain.internal.*
    assert ev["account_id"] == acct                # scoped to the customer's account
    assert ev["user_id"] == "seat-1"
    assert all(not c["event_type"].startswith("brain.internal.") for c in calls), (
        "no internal event should be emitted when a customer account is in scope")



