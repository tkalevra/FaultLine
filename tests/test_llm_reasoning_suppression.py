"""Reasoning/thinking suppression must happen at REQUEST time, per provider family.

WHY THIS FILE EXISTS. FaultLine uses the LLM only for structured extraction, where a chain of
thought is waste — and measurably worse than waste. Measured 2026-07-30 against DeepSeek
v4-flash with a control:

    thinking disabled → 1291ms, 236 output tokens, 0 reasoning, 5/5 parsed
    thinking default  → 5844ms, 4472 output tokens, ~15.8k reasoning chars, 4/5 parsed

The un-suppressed run produced an UNPARSEABLE response, so leaving reasoning on is a
CORRECTNESS defect in an extraction pipeline, not merely a cost one.

Two traps these tests exist to prevent regressing:
  1. ``_strip_think_tags`` is COSMETIC — it cleans the text after the tokens and latency are
     already spent. It is not suppression and must never again be treated as the fallback.
  2. A provider will ACCEPT a suppression field it does not recognize and reason anyway. We
     sent Qwen/vLLM's ``enable_thinking`` to DeepSeek and got HTTP 200 with full reasoning.
     So we key on the endpoint HOST (``backend_type="openai"`` covers DeepSeek, Cerebras,
     Groq, OpenRouter, xAI — all different), and we send NOTHING to an unrecognized host,
     because an unsupported top-level field HTTP-400s on strict providers (the Cerebras
     ``chat_template_kwargs`` incident) and would break every call on that endpoint.
"""
import pytest

from src.api.llm_client import build_llm_payload, resolve_reasoning_controls


# ── the field is correct per provider family ────────────────────────────────────────────
@pytest.mark.parametrize("base_url,model,expected", [
    # DeepSeek: LIVE-VERIFIED. Toggle defaults to "enabled" on v4, hence the explicit disable.
    ("https://api.deepseek.com", "deepseek-v4-flash", {"thinking": {"type": "disabled"}}),
    ("https://api.deepseek.com", "deepseek-v4-pro", {"thinking": {"type": "disabled"}}),
    # OpenAI: reasoning_effort is REJECTED (400) on non-reasoning models → send nothing.
    ("https://api.openai.com", "gpt-4o", {}),
    ("https://api.openai.com", "gpt-4o-mini", {}),
    ("https://api.openai.com", "gpt-5.1-chat-latest", {"reasoning_effort": "none"}),
    # o-series predates "none"; its lowest is "low".
    ("https://api.openai.com", "o3-mini", {"reasoning_effort": "low"}),
    # Anthropic: opt-in on 4.5-and-earlier (omit = off); defaults ON on the adaptive models.
    ("https://api.anthropic.com", "claude-sonnet-4-5", {}),
    ("https://api.anthropic.com", "claude-sonnet-5", {"thinking": {"type": "disabled"}}),
    ("https://api.anthropic.com", "claude-opus-5", {"thinking": {"type": "disabled"}}),
    # Gemini 2.5 Flash accepts budget 0; Gemini 3 replaced budget with level and has no off.
    ("https://generativelanguage.googleapis.com", "gemini-2.5-flash",
     {"thinkingConfig": {"thinkingBudget": 0}}),
    ("https://generativelanguage.googleapis.com", "gemini-3-pro",
     {"thinkingConfig": {"thinkingLevel": "low"}}),
    # Qwen/DashScope uses its own field name entirely.
    ("https://dashscope-intl.aliyuncs.com", "qwen3-max", {"enable_thinking": False}),
    ("https://openrouter.ai", "anything", {"reasoning": {"effort": "none"}}),
])
def test_suppression_field_per_provider(base_url, model, expected):
    assert resolve_reasoning_controls(base_url, model)["fields"] == expected


def test_unknown_host_gets_no_field():
    """A self-hosted / LAN / unrecognized endpoint must receive NO reasoning field.

    Guessing is not free: an unsupported top-level field HTTP-400s on strict OpenAI-compatible
    providers, which would break EVERY call to that endpoint rather than merely failing to
    suppress. `known` is False so callers can tell "no field needed" from "we don't know".
    """
    for base in ("http://192.168.40.20:8080", "http://localhost:11434", "https://llm.example.com"):
        got = resolve_reasoning_controls(base, "whatever-model")
        assert got["fields"] == {}, base
        assert got["known"] is False, base


@pytest.mark.parametrize("base_url,model", [
    ("https://api.x.ai", "grok-4.5"),                                    # docs: cannot disable
    ("https://generativelanguage.googleapis.com", "gemini-3-pro"),       # low/high only, no off
    ("https://generativelanguage.googleapis.com", "gemini-2.5-pro"),     # 128-token floor
    ("https://api.anthropic.com", "claude-fable-5"),                     # rejects the disable
    ("https://api.groq.com", "openai/gpt-oss-120b"),                     # low/med/high only
])
def test_cannot_disable_is_reported_honestly(base_url, model):
    """Where a provider CANNOT turn reasoning off, we must say so rather than pretend.

    This is the honesty half: the caller warns/accounts for reasoning tokens instead of
    assuming a clean extraction. Silently claiming suppression is how 1300 wasted tokens per
    call stayed invisible.
    """
    got = resolve_reasoning_controls(base_url, model)
    assert got["known"] is True
    assert got["can_disable"] is False


# ── the resolved field actually reaches the wire ─────────────────────────────────────────
def test_payload_carries_suppression_and_not_the_vllm_field(monkeypatch):
    """The whole point: it must land in the PAYLOAD, and must not smuggle the vLLM field.

    ``chat_template_kwargs`` is what Cerebras 400s on — it is only ever valid for
    self-hosted vLLM/OpenWebUI, never for a hosted OpenAI-compatible provider.
    """
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.delenv("LLM_REASONING_SUPPRESSION", raising=False)
    payload = build_llm_payload([{"role": "user", "content": "hi"}],
                                "deepseek-v4-flash", max_tokens=2048)
    assert payload["thinking"] == {"type": "disabled"}
    assert "chat_template_kwargs" not in payload


def test_kill_switch_sends_nothing(monkeypatch):
    """LLM_REASONING_SUPPRESSION=false must send no suppression field anywhere.

    Escape hatch for the day a provider starts rejecting a field we inject — the operator can
    disable suppression without a code change or redeploy.
    """
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("LLM_REASONING_SUPPRESSION", "false")
    payload = build_llm_payload([{"role": "user", "content": "hi"}],
                                "deepseek-v4-flash", max_tokens=2048)
    assert "thinking" not in payload


def test_explicit_caller_thinking_is_not_clobbered(monkeypatch):
    """A deliberate opt-in must survive the blanket suppression (setdefault, not overwrite)."""
    monkeypatch.setenv("LLM_BACKEND_TYPE", "anthropic")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.anthropic.com")
    monkeypatch.delenv("LLM_REASONING_SUPPRESSION", raising=False)
    payload = build_llm_payload([{"role": "user", "content": "hi"}], "claude-sonnet-5",
                                max_tokens=1024, thinking={"type": "enabled",
                                                           "budget_tokens": 2048})
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}


# ── verification: an IGNORED flag must be detected, never assumed away ───────────────────
def test_reasoning_leak_is_detected_from_usage():
    """We cannot trust a 200 — so a non-zero reasoning-token count must be surfaced.

    Covers the provider-specific usage paths, because there is no universal field. Absence of
    a count is NOT treated as proof of success (that would re-create the original blind spot).
    """
    from src.api.llm_calls import _audit_reasoning_leak, _dig

    # DeepSeek / OpenAI-compat path — measured live at 440/503/791 on the un-suppressed control.
    leaked = {"choices": [{"message": {"content": "[]"}}],
              "usage": {"completion_tokens": 488,
                        "completion_tokens_details": {"reasoning_tokens": 440}}}
    assert _dig(leaked, "usage.completion_tokens_details.reasoning_tokens") == 440
    _audit_reasoning_leak(leaked, "EXTRACT", "u1")        # must not raise

    for path, payload in [
        ("anthropic", {"usage": {"output_tokens_details": {"thinking_tokens": 12}}}),
        ("gemini", {"usageMetadata": {"thoughtsTokenCount": 34}}),
        ("xai", {"usage": {"reasoning_tokens": 56}}),
    ]:
        _audit_reasoning_leak(payload, "EXTRACT", "u1")   # must not raise

    # Clean response and malformed input are both silent, and never raise.
    _audit_reasoning_leak({"usage": {"completion_tokens": 47}}, "EXTRACT", "u1")
    _audit_reasoning_leak({}, "EXTRACT", None)
    _audit_reasoning_leak({"usage": None}, "EXTRACT", None)


def test_strip_think_tags_is_documented_as_cosmetic():
    """Regression guard on the LESSON, not just the code.

    If someone deletes the warning from the docstring they are likely also re-adopting
    tag-stripping as the suppression story, which is the bug this whole file exists to prevent.
    """
    from src.api.llm_calls import _strip_think_tags
    assert _strip_think_tags("<think>a b c</think>ok") == "ok"
    assert "COSMETIC" in (_strip_think_tags.__doc__ or "")
