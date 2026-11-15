"""Issue #107 — a provider answering ``"content": null`` must never crash the LLM call stack.

Recorded shape (tests/fixtures/nim_diffusiongemma_reframe_responses.json): NVIDIA NIM serving
google/diffusiongemma-26b-a4b-it via vLLM, sent the exact REFRAME request with reasoning on.
The model spent the 256-token budget in ``message.reasoning`` and answered ``finish_reason=
"length"`` with ``message.content = null``. Every caller read
``.get("content", "").strip()`` — the default only covers a MISSING key, so ``None.strip()``
raised AttributeError after a successful HTTP 200; the retry loop logged it as an endpoint
failure, retried, raised, and the turn was withheld (``extraction_degraded``).

Pins:
  * the recorded null-content body → the async AND sync retry calls return the honest empty
    result after ONE request (no AttributeError, no retry of a settled answer);
  * control — the recorded normal body from the same model → atoms parse unchanged;
  * fresh-shape controls (different turns, different domains) — answer filed in the reasoning
    channel with a FINISHED generation, and answer delivered as a tool call → parsed; a
    budget-truncated reasoning channel is never salvaged as an answer.
"""
from __future__ import annotations

import json
import pathlib
import sys
import types

import httpx
import pytest

from src.api import llm_calls

_FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures"
                       / "nim_diffusiongemma_reframe_responses.json").read_text())
NIM_NULL_CONTENT = _FIXTURE["reframe_reasoning_effort_low"]["response"]
NIM_NORMAL = _FIXTURE["reframe_plain"]["response"]

_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
_MESSAGES = [{"role": "system", "content": "atomize"},
             {"role": "user", "content": "I live in Fredericton."}]


def _openai_body(message: dict, finish: str = "stop") -> dict:
    return {"id": "chatcmpl-x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", **message},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


@pytest.fixture(scope="module")
def _env():
    """Module-scoped env (never module-level os.environ — see #63)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
    mp.setenv("LLM_BACKEND_TYPE", "openai")
    mp.setenv("WGM_LLM_MODEL", "google/diffusiongemma-26b-a4b-it")
    mp.setenv("LLM_CACHE_ENABLED", "false")
    mp.delenv("SAAS_MODE", raising=False)
    if "src.api.main" not in sys.modules:   # the async path lazy-imports main._http_client
        stub = types.ModuleType("src.api.main")
        stub._http_client = None
        mp.setitem(sys.modules, "src.api.main", stub)
    yield
    mp.undo()


@pytest.fixture
def wire(_env, monkeypatch):
    """Replace only the network hop; everything after the 200 runs for real."""
    calls = {"n": 0, "body": None}

    def _resp(json_body=None):
        return httpx.Response(200, json=calls["body"],
                              request=httpx.Request("POST", _URL))

    async def fake_async(_client, url, *, json, headers, timeout):
        calls["n"] += 1
        return _resp()

    def fake_sync(_client, url, *, json, headers, timeout):
        calls["n"] += 1
        return _resp()

    monkeypatch.setattr(llm_calls, "_post_llm_async", fake_async)
    monkeypatch.setattr(llm_calls, "_post_llm_sync", fake_sync)
    llm_calls._llm_circuit_breaker.record_success()
    return calls


def _async_call(max_retries=2):
    import asyncio
    return asyncio.run(llm_calls.call_llm_with_retry_async(
        messages=_MESSAGES, model="google/diffusiongemma-26b-a4b-it",
        user_id="00000000-0000-0000-0000-000000000107", max_retries=max_retries,
        operation="REFRAME"))


def _sync_call(max_retries=2):
    return llm_calls.call_llm_with_retry_sync(
        messages=_MESSAGES, model="google/diffusiongemma-26b-a4b-it",
        user_id="00000000-0000-0000-0000-000000000107", max_retries=max_retries,
        operation="REFRAME")


# ── the recorded NIM null-content body (red on base: AttributeError) ─────────────────────

def test_recorded_nim_null_content_shape_is_what_we_think():
    choice = NIM_NULL_CONTENT["choices"][0]
    assert choice["message"]["content"] is None
    assert choice["finish_reason"] == "length"
    assert isinstance(choice["message"]["reasoning"], str) and choice["message"]["reasoning"]


def test_async_null_content_returns_empty_after_one_request(wire):
    wire["body"] = NIM_NULL_CONTENT
    out = _async_call()
    assert out == {}
    assert wire["n"] == 1, "a settled 200 must not be retried as an endpoint failure"


def test_sync_null_content_returns_empty_after_one_request(wire):
    wire["body"] = NIM_NULL_CONTENT
    out = _sync_call()
    assert out == {}
    assert wire["n"] == 1


def test_truncated_reasoning_is_never_salvaged_as_the_answer():
    # The recorded reasoning ends mid-JSON; it is an unfinished thought, not an answer.
    assert llm_calls.response_message_text(NIM_NULL_CONTENT, "REFRAME") == ""


# ── control: the recorded normal body from the same model ────────────────────────────────

def test_control_recorded_nim_normal_body_parses(wire):
    wire["body"] = NIM_NORMAL
    out = _async_call()
    assert out == {"atoms": [{"statement": "I live in Fredericton.",
                              "source": "I live in Fredericton."}]}
    assert wire["n"] == 1


def test_control_plain_openai_shape_parses_sync(wire):
    wire["body"] = _openai_body({"content": '{"atoms": []}', "refusal": None})
    assert _sync_call() == {"atoms": []}


# ── fresh-shape controls (other turns, other domains) ────────────────────────────────────

def test_answer_misfiled_in_reasoning_channel_of_a_finished_generation(wire):
    body = _openai_body({"content": None, "reasoning_content":
                         '{"atoms": [{"statement": "The router uses VLAN 40.", '
                         '"source": "The router uses VLAN 40."}]}'}, finish="stop")
    wire["body"] = body
    out = _async_call()
    assert out["atoms"][0]["statement"] == "The router uses VLAN 40."


def test_answer_delivered_as_tool_call(wire):
    body = _openai_body({"content": None, "tool_calls": [{
        "id": "c1", "type": "function", "function": {
            "name": "emit", "arguments":
            '{"atoms": [{"statement": "My bees swarmed in May.", '
            '"source": "My bees swarmed in May."}]}'}}]})
    wire["body"] = body
    out = _sync_call()
    assert out["atoms"][0]["statement"] == "My bees swarmed in May."


def test_openai_multipart_content_is_joined():
    body = _openai_body({"content": [{"type": "text", "text": '{"atoms":'},
                                     {"type": "text", "text": " []}"}]})
    assert llm_calls.response_message_text(body) == '{"atoms": []}'


@pytest.mark.parametrize("body", [
    {}, {"choices": []}, {"choices": [None]}, {"choices": [{"message": None}]},
    {"choices": [{"message": {"content": 42}}]},
])
def test_malformed_bodies_never_raise(body):
    assert llm_calls.response_message_text(body) == ""
