"""build_llm_payload: chat_template_kwargs must ONLY go to backends that accept it.

THE BUG this pins: the payload builder unconditionally injected
``chat_template_kwargs={"enable_thinking": False}`` for every non-anthropic backend, on
the assumption that "unknown fields are silently ignored." That is FALSE for strict
OpenAI-compatible providers — Cerebras returns HTTP 400
(``chat_template_kwargs ... is unsupported``, code=wrong_api_format), and OpenAI / Groq /
most hosted gateways reject unknown top-level fields the same way. A tenant BYO brain
(backend_type="openai") pointed at such a provider then 400s on EVERY engine LLM call.

The fix gates the field to the OpenWebUI/vLLM family (default ``openwebui``), env-extensible
via ``LLM_CHAT_TEMPLATE_KWARGS_BACKENDS``. OpenWebUI (pre-prod/prod) is byte-for-byte
unchanged; strict providers get a clean payload and rely on the post-hoc _strip_think_tags
net. Many deployments use OpenWebUI — that path MUST keep the field.

Run: python3 tools/fltest.py --bug BRAIN-KEY-DIVERGE --test tests/test_llm_payload_chat_template_kwargs.py
"""

import pytest

from src.api.llm_client import build_llm_payload


_MSGS = [{"role": "user", "content": "extract"}]


def test_openwebui_keeps_chat_template_kwargs(monkeypatch):
    # The pre-prod / prod path — MANY deployments use OpenWebUI. Must be unchanged.
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openwebui")
    monkeypatch.delenv("LLM_CHAT_TEMPLATE_KWARGS_BACKENDS", raising=False)
    payload = build_llm_payload(messages=_MSGS, model="qwen3.5-9b")
    assert payload.get("chat_template_kwargs") == {"enable_thinking": False}


def test_openai_backend_omits_chat_template_kwargs(monkeypatch):
    # Cerebras / OpenAI / Groq — strict: they 400 on the field. It must be ABSENT.
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.delenv("LLM_CHAT_TEMPLATE_KWARGS_BACKENDS", raising=False)
    payload = build_llm_payload(messages=_MSGS, model="gemma-4-31b")
    assert "chat_template_kwargs" not in payload


def test_anthropic_never_gets_chat_template_kwargs(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND_TYPE", "anthropic")
    monkeypatch.delenv("LLM_CHAT_TEMPLATE_KWARGS_BACKENDS", raising=False)
    payload = build_llm_payload(messages=_MSGS, model="claude-x")
    assert "chat_template_kwargs" not in payload


def test_env_override_opts_a_strict_backend_in(monkeypatch):
    # A self-hoster whose gateway DOES accept the field can extend the allow-set.
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.setenv("LLM_CHAT_TEMPLATE_KWARGS_BACKENDS", "openwebui,openai")
    payload = build_llm_payload(messages=_MSGS, model="local-qwen")
    assert payload.get("chat_template_kwargs") == {"enable_thinking": False}
