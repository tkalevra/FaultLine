"""BUG-02 (P1, 2026-10-30): the probe result must carry a CURATED provider-refusal
sentence for auth/billing-class statuses — never the raw provider body (CWE-209).

Filed from prod: ExampleCorp (z.ai coding-plan endpoint) saw the model LIST succeed
(unmetered GET) while every chat-shaped call was refused HTTP 429 code 1113 "Insufficient
balance or no resource package. Please recharge." — and the console rendered only
"call failed: HTTPStatusError: the endpoint answered with an HTTP error status". The
actionable sentence lived ONLY in the server log (faultline.errors WARNING +
llm_rate_limit.fallback_declined_not_rate non_rate_code:1113).

Pins (gauntlet saas-probe-provider-detail):
  * 429-with-body code 1113 → the client sees the curated BALANCE sentence;
  * 403 model_access_denied → the curated ACCESS sentence (unmapped code: class, not body);
  * 401 → the curated CREDENTIALS sentence;
  * a genuine rate 429 → NO refusal append (the transport-class sentence stands — guessing
    "billing" on pacing evidence would be a fabricated diagnosis);
  * the RAW BODY is never echoed in any arm (CWE-209 pin);
  * the breaker-open probe result names the retry WINDOW.

Run:  python3 -m pytest tests/test_probe_provider_refusal_detail.py -q
"""

from __future__ import annotations

import httpx
import pytest

BALANCE_BODY = ('{"error":{"code":"1113","message":"Insufficient balance or no resource '
                'package. Please recharge."}}')
ACCESS_BODY = '{"error":{"code":"model_access_denied","message":"You do not have access to ' \
              'this REALLY-SECRET-CANARY model"}}'
RATE_BODY = '{"error":{"code":"1302","message":"Rate limit reached for requests"}}'


def _status_exc(status: int, body: str, url="https://api.example.test/v1/chat/completions"):
    """A real httpx.HTTPStatusError carrying a real response with this body."""
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request, text=body,
                              headers={"Content-Type": "application/json"})
    return httpx.HTTPStatusError(f"Client error {status} for url {url}",
                                 request=request, response=response)


# ── curated_refusal_for_response: the classification itself ──────────────────────────

def test_429_code_1113_maps_to_balance_sentence():
    from saas.errors import curated_refusal_for_response
    s = curated_refusal_for_response(429, BALANCE_BODY)
    assert s is not None and "insufficient balance" in s and "recharge" in s


def test_429_marker_only_maps_to_generic_non_rate_sentence():
    from saas.errors import curated_refusal_for_response
    s = curated_refusal_for_response(429, '{"error":{"message":"please recharge"}}')
    assert s is not None and "account/billing" in s


def test_genuine_rate_429_returns_none():
    from saas.errors import curated_refusal_for_response
    assert curated_refusal_for_response(429, RATE_BODY) is None
    assert curated_refusal_for_response(429, "") is None
    assert curated_refusal_for_response(429, None) is None


def test_401_402_403_map_to_status_sentences():
    from saas.errors import curated_refusal_for_response
    assert "credentials" in curated_refusal_for_response(401, "{}")
    assert "billing" in curated_refusal_for_response(402, "")
    s403 = curated_refusal_for_response(403, ACCESS_BODY)
    assert s403 is not None and "refused access" in s403


def test_5xx_and_garbage_return_none():
    from saas.errors import curated_refusal_for_response
    assert curated_refusal_for_response(500, BALANCE_BODY) is None
    assert curated_refusal_for_response(None, BALANCE_BODY) is None
    assert curated_refusal_for_response("garbage", BALANCE_BODY) is None


def test_non_token_code_is_never_keyed_or_rendered():
    from saas.errors import curated_refusal_for_response
    # prose in a "code" slot is NOT a token → not keyed; 403 falls to the status sentence,
    # and the prose never renders.
    s = curated_refusal_for_response(403, '{"code":"a long sentence not a token"}')
    assert s is not None and "refused access" in s and "not a token" not in s


# ── provider_detail: the seam the probe result renders ───────────────────────────────

def test_probe_detail_carries_balance_sentence_not_raw_body(caplog):
    """THE BUG-02 PIN: 429 code 1113 → curated balance sentence in the CLIENT string; the
    provider's own words never reach it; the server log still carries them redacted."""
    import logging
    from saas import errors as SE
    caplog.set_level(logging.WARNING, logger="faultline.errors")
    exc = _status_exc(429, BALANCE_BODY)
    out = SE.provider_detail(exc, secrets=None, what="call failed")
    assert out.startswith("call failed: HTTPStatusError:"), out
    assert "insufficient balance" in out and "recharge" in out, out
    # CWE-209: the provider's own sentence is NOT echoed to the client
    assert "Please recharge." not in out
    assert "no resource package. Please" not in out
    # the server log channel still exists (the seam logged its redacted sentence record;
    # the provider BODY reaches the log via llm_calls' own response_body WARNING, as on prod)
    recs = [r for r in caplog.records if r.name == "faultline.errors"]
    assert recs and "provider_detail sentence=" in recs[0].getMessage()


def test_probe_detail_403_access_refusal():
    from saas import errors as SE
    out = SE.provider_detail(_status_exc(403, ACCESS_BODY), what="call failed")
    assert "refused access" in out
    assert "REALLY-SECRET-CANARY" not in out   # unmapped code's body never echoes


def test_probe_detail_401_credentials_refusal():
    from saas import errors as SE
    out = SE.provider_detail(_status_exc(401, '{"error":{"message":"bad key"}}'),
                             what="call failed")
    assert "check the API key" in out and "bad key" not in out


def test_probe_detail_genuine_rate_429_keeps_class_sentence():
    from saas import errors as SE
    out = SE.provider_detail(_status_exc(429, RATE_BODY), what="call failed")
    assert "provider refused" not in out
    assert "HTTP error status" in out


def test_probe_detail_key_redaction_still_holds():
    from saas import errors as SE
    key = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    exc = _status_exc(429, BALANCE_BODY,
                      url=f"https://api.example.test/v1?api-key={key}")
    out = SE.provider_detail(exc, secrets=[key], what="call failed")
    assert key not in out


# ── breaker-open window (llm_admin) ──────────────────────────────────────────────────

def test_breaker_open_detail_names_retry_window():
    from saas.llm_admin import _breaker_open_detail
    d = _breaker_open_detail({"error": "circuit_breaker_open",
                              "message": "Language model not responding. Wait 37s or "
                                         "restart backend."})
    assert "circuit breaker open" in d and "retry in 37s" in d, d


def test_breaker_open_detail_without_window_falls_back():
    from saas.llm_admin import _breaker_open_detail
    d = _breaker_open_detail({"error": "circuit_breaker_open"})
    assert "circuit breaker open" in d and "retry" in d
    assert _breaker_open_detail(None)


@pytest.mark.parametrize("bad", [None, 42, {"message": 7}])
def test_breaker_open_detail_never_raises(bad):
    from saas.llm_admin import _breaker_open_detail
    assert isinstance(_breaker_open_detail(bad), str)


# ── the extraction probe result threads breaker + refusal (route-level) ──────────────

def test_probe_extraction_result_shapes():
    """_probe_extraction's two failure returns keep their shape and carry the new detail."""
    import saas.llm_admin as LA

    cfg = {"base_url": "https://api.example.test/v1", "model": "glm-4.5-air",
           "api_key": "sk-test"}

    # breaker-open: the engine answered with the dict, not an exception
    def _fake_call(**_k):
        return {"error": "circuit_breaker_open",
                "message": "Language model not responding. Wait 12s or restart backend."}
    import src.api.llm_calls as LCalls
    real = LCalls.call_llm_with_retry_sync
    LCalls.call_llm_with_retry_sync = _fake_call
    try:
        result = LA._probe_extraction("acct", cfg)
    finally:
        LCalls.call_llm_with_retry_sync = real
    assert result["ok"] is False
    assert "retry in 12s" in result["detail"], result

# ── critic R1 minor #2: the code map must not silently drift from the engine tables ───

def test_code_map_covers_every_engine_documented_non_rate_code():
    """Every code the ENGINE documents as a non-rate 429 verdict must carry a curated
    sentence HERE (critic R1 minor #2: a hand-maintained mirror with no mechanism keeping
    it honest drops silently to the generic sentence when the engine grows a code)."""
    from src.api.llm_calls import _NON_RATE_429_CODES
    from saas.errors import _REFUSAL_CODE_SENTENCES
    documented = set()
    for entry in _NON_RATE_429_CODES.values():
        documented |= set((entry or {}).get("codes") or ())
    missing = documented - set(_REFUSAL_CODE_SENTENCES)
    assert not missing, f"engine-documented non-rate codes with no curated sentence: {sorted(missing)}"


# ── critic R1 minor #1: a coded rate 429 is never relabelled by its prose ─────────────

def test_rate_429_with_marker_words_in_message_stays_rate():
    from saas.errors import curated_refusal_for_response
    # A genuine rate verdict whose MESSAGE happens to contain an engine marker phrase:
    # the machine code is present but unmapped → under-diagnose (None), never fabricate
    # a billing diagnosis out of the prose.
    assert curated_refusal_for_response(
        429, '{"error":{"code":"1302","message":"rate limit: window does not exist yet"}}') is None


def test_junk_code_does_not_mask_mapped_code_nested_deeper():
    from saas.errors import curated_refusal_for_response
    s = curated_refusal_for_response(
        429, '{"code":"x","nested":{"code":"1113","message":"Insufficient balance"}}')
    assert s is not None and "insufficient balance" in s


# ── critic R1 minor #3: ROUTE-level pin for the tenant_console chat-llm branch ────────

def test_tenant_chat_llm_test_route_carries_curated_refusal(monkeypatch):
    """The REAL /tenant/agent/chat-llm/test route against a REAL loopback stub answering
    429 {code:1113} (the litmus shape, pinned so a regression to a bare status number
    fails CI). Loopback is admitted the operator way: SAAS_LLM_ALLOWED_EGRESS."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    monkeypatch.setenv("SAAS_LLM_ALLOWED_EGRESS", "127.0.0.0/8")
    body = _json.dumps(
        {"error": {"code": "1113",
                   "message": "Insufficient balance or no resource package. Please recharge."}}).encode()

    class _Stub(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import saas.tenant_console as TC
        app = FastAPI()
        app.include_router(TC.router)
        app.dependency_overrides[TC.require_dashboard_session] = lambda: "acct-t"
        monkeypatch.setattr(TC, "_seat_or_needs_seat", lambda a, s: (True, False, None))
        monkeypatch.setattr(
            TC._accounts, "get_agent_seat_chat_llm_decrypted",
            lambda a, s: {"endpoint": f"http://127.0.0.1:{srv.server_address[1]}/v1",
                          "model": "m", "api_key": "sk-t"})
        r = TestClient(app).post(
            "/tenant/agent/chat-llm/test",
            json={"endpoint": f"http://127.0.0.1:{srv.server_address[1]}/v1",
                  "model": "m", "api_key": "sk-t"})
        assert r.status_code == 200, r.text
        detail = r.json()["detail"]
        assert r.json()["ok"] is False
        assert "endpoint returned 429" in detail, detail
        assert "insufficient balance" in detail and "recharge" in detail, detail
        assert "Please recharge." not in r.text          # CWE-209: raw body never echoes
    finally:
        srv.shutdown()

