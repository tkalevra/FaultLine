"""THE ONE JOIN — llm_client.resolve_endpoint / resolve_chat_endpoint.

The three base-URL shapes a real operator pastes must all resolve to the same chat URL,
and get_backend_endpoint must use the join (it used to concatenate, doubling a version
segment the base already carried: ``…/v4`` + ``/v1/chat/completions`` → ``…/v4/v1/…``).
"""
import pytest

from src.api import llm_client


@pytest.mark.parametrize("base", [
    "https://api.example.com/api/paas/v4",                    # version in the base
    "https://api.example.com/api/paas/v4/",                   # trailing slash
    "https://api.example.com/api/paas/v4/chat/completions",   # the full endpoint pasted
])
def test_versioned_base_shapes_resolve_to_one_chat_url(base):
    assert llm_client.resolve_chat_endpoint("openai", base) == \
        "https://api.example.com/api/paas/v4/chat/completions"


def test_unversioned_base_gets_the_backend_suffix():
    assert llm_client.resolve_chat_endpoint("ollama", "http://llm:11434") == \
        "http://llm:11434/v1/chat/completions"
    assert llm_client.resolve_chat_endpoint("openwebui", "http://open-webui:8080") == \
        "http://open-webui:8080/api/chat/completions"


def test_raw_backend_is_verbatim():
    assert llm_client.resolve_chat_endpoint("raw", "http://x/custom/path") == "http://x/custom/path"


def test_query_string_survives_the_join():
    assert llm_client.resolve_endpoint("https://h/openai?api-version=1", "/v1/chat/completions") \
        == "https://h/openai/v1/chat/completions?api-version=1"


def test_a_suffix_whose_lead_is_not_a_version_is_never_reshaped():
    # groq's suffix starts with `openai`, so a bare `/v1` base overlaps nothing.
    assert llm_client.resolve_chat_endpoint("groq", "https://api.groq.com/v1") == \
        "https://api.groq.com/v1/openai/v1/chat/completions"


def test_get_backend_endpoint_uses_the_join(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/api/paas/v4")
    assert llm_client.get_backend_endpoint() == \
        "https://api.example.com/api/paas/v4/chat/completions"
