"""
Regression tests for the chat_template_kwargs injection gate in build_llm_payload.

Bug: build_llm_payload unconditionally injected
`chat_template_kwargs={"enable_thinking": False}` for every non-anthropic backend
on the (false) assumption that unknown fields are silently ignored. Strict
OpenAI-compatible providers (Cerebras, OpenAI, Groq, most hosted gateways)
return HTTP 400 for unknown top-level fields, so LLM_BACKEND_TYPE=openai pointed
at such a provider 400s on every call.

Fix: gate the injection to the OpenWebUI/vLLM family (default "openwebui"),
env-extensible via LLM_CHAT_TEMPLATE_KWARGS_BACKENDS. OpenWebUI stays
byte-for-byte unchanged; strict providers get a clean payload.
"""

from src.api.llm_client import build_llm_payload


_MESSAGES = [{"role": "user", "content": "hi"}]


def test_openwebui_keeps_chat_template_kwargs(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openwebui")
    monkeypatch.delenv("LLM_CHAT_TEMPLATE_KWARGS_BACKENDS", raising=False)
    payload = build_llm_payload(_MESSAGES, model="m")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_openai_omits_chat_template_kwargs(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.delenv("LLM_CHAT_TEMPLATE_KWARGS_BACKENDS", raising=False)
    payload = build_llm_payload(_MESSAGES, model="m")
    assert "chat_template_kwargs" not in payload


def test_anthropic_omits_chat_template_kwargs(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND_TYPE", "anthropic")
    monkeypatch.delenv("LLM_CHAT_TEMPLATE_KWARGS_BACKENDS", raising=False)
    payload = build_llm_payload(_MESSAGES, model="m")
    assert "chat_template_kwargs" not in payload


def test_env_extends_backends_to_openai(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.setenv("LLM_CHAT_TEMPLATE_KWARGS_BACKENDS", "openwebui,openai")
    payload = build_llm_payload(_MESSAGES, model="m")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
