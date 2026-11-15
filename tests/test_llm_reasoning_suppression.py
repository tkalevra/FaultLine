"""Reasoning/thinking suppression must happen at REQUEST time, per provider family.

WHY THIS FILE EXISTS. FaultLine uses the LLM only for structured extraction, where a chain of
thought is waste — and measurably worse than waste. Measured 2026-07-30 against DeepSeek
v4-flash with a control:

    thinking disabled → 1291ms, 236 output tokens, 0 reasoning, 5/5 parsed
    thinking default  → 5844ms, 4472 output tokens, ~15.8k reasoning chars, 4/5 parsed

The un-suppressed run produced an UNPARSEABLE response, so leaving reasoning on is a
CORRECTNESS defect in an extraction pipeline, not merely a cost one.

Two traps these tests exist to prevent regressing:
  1. _strip_think_tags is COSMETIC — it cleans the text after the tokens and latency are
     already spent. It is not suppression and must never again be treated as the fallback.
  2. A provider will ACCEPT a suppression field it does not recognize and reason anyway. We
     sent Qwen/vLLM's enable_thinking to DeepSeek and got HTTP 200 with full reasoning.
     So we key on the endpoint HOST (backend_type="openai" covers DeepSeek, Cerebras,
     Groq, OpenRouter, xAI — all different), and we send NOTHING to an unrecognized host,
     because an unsupported top-level field HTTP-400s on strict providers (the Cerebras
     chat_template_kwargs incident).

SaaS-FIRST NOTE: BYO LLMs are attached at the TENANT (account_llm_config) or SEAT
(seat_llm_config) level; the global LLM_BASE_URL env is IGNORED under SAAS_MODE (the conftest
baseline). Tests that exercise the payload path therefore mock the tenant brain via
monkeypatch of src.api.llm_client._tenant_brain, never the global env.
"""
import pytest

from src.api.llm_client import build_llm_payload, resolve_reasoning_controls, _model_reasons


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
    # 🔴 CORRECTED 2026-08-17 — these two used to expect thinkingConfig, and they were PINNING A
    # DEFECT. thinkingConfig belongs to Google's NATIVE generateContent API. We call Google on
    # its OpenAI-COMPATIBLE surface (/v1beta/openai/chat/completions), which validates strictly
    # against the OpenAI schema and rejects any key outside it — so the native field did not
    # merely fail to suppress, it 400'd the entire call and a correctly-configured Gemini brain
    # reported "probe FAILED". Measured on the live endpoint with an invalid key, so the body is
    # judged before auth (reaching the auth error IS the pass condition):
    #     {model, messages}                          -> "Please pass a valid API key"
    #     {model, messages, thinkingConfig:{...}}    -> "Unknown name \"thinkingConfig\":
    #                                                    Cannot find field."
    #     {model, messages, reasoning_effort:"none"} -> "Please pass a valid API key"
    # A test that asserts what the code does, when what the code does is refused by the
    # provider, is worse than no test: it defended the bug against being fixed.
    ("https://generativelanguage.googleapis.com", "gemini-2.5-flash",
     {"reasoning_effort": "none"}),
    ("https://generativelanguage.googleapis.com", "gemini-3-pro",
     {"reasoning_effort": "low"}),
    # Qwen/DashScope uses its own field name entirely.
    ("https://dashscope-intl.aliyuncs.com", "qwen3-max", {"enable_thinking": False}),
    ("https://openrouter.ai", "anything", {"reasoning": {"effort": "none"}}),
    # NEW hosts (2026-08-17, researched — never guessed):
    ("https://api.z.ai", "glm-5", {"thinking": {"type": "disabled"}}),
    # CORRECTED 2026-11-01 (docs.z.ai chat-completion reference): for the GLM-5.3 family,
    # effort low/medium are MAPPED TO HIGH; only none/minimal skip thinking. The old "low"
    # pin here defended a near-max-reasoning payload — see llm_client.py z.ai models entry.
    ("https://api.z.ai", "glm-5.3", {"thinking": {"type": "enabled"}, "reasoning_effort": "minimal"}),
    ("https://api.z.ai", "glm-5.3-flash", {"reasoning_effort": "minimal"}),
    ("https://api.z.ai", "glm-4.5", {"thinking": {"type": "disabled"}}),
    ("https://api.together.xyz", "deepseek-ai/DeepSeek-V3.1", {"reasoning": {"enabled": False}}),
    ("https://api.fireworks.ai", "accounts/fireworks/models/deepseek-r1",
     {"reasoning_effort": "none"}),
    ("https://api.x.ai", "grok-4.5", {"reasoning_effort": "low"}),
    ("https://api.perplexity.ai", "sonar-reasoning-pro", {}),
    # Z.AI surface split (2026-08-19, live-keyed): standard path disables; coding path
    # forces a floor (z.ai error 1210) — effort=low CONFIRMED by keyed A/B on pre-prod.
    ("https://api.z.ai/api/paas/v4", "glm-5.2", {"thinking": {"type": "disabled"}}),
    ("https://api.z.ai/api/coding/paas/v4", "glm-5.2", {"reasoning_effort": "low"}),
    # CORRECTED 2026-11-01 (prod-key keyed bench, n=5/variant): glm-4.5-air on the CODING
    # surface honors thinking:disabled — reasoning_tokens 0 x5, median 4.81s, 5/5 valid JSON.
    # The 2026-08-18 partial-honor reading stands for glm-5.2 only, not the 4.5 hybrid family.
    ("https://api.z.ai/api/coding/paas/v4", "glm-4.5-air", {"thinking": {"type": "disabled"}}),
])
def test_suppression_field_per_provider(base_url, model, expected):
    assert resolve_reasoning_controls(base_url, model)["fields"] == expected


def test_unknown_host_gets_no_field():
    """A self-hosted / LAN / unrecognized endpoint must receive NO reasoning field.

    Guessing is not free: an unsupported top-level field HTTP-400s on strict OpenAI-compatible
    providers, which would break EVERY call to that endpoint rather than merely failing to
    suppress. known is False so callers can tell "no field needed" from "we don't know".
    """
    for base in ("http://192.168.40.20:8080", "http://localhost:11434", "https://llm.example.com"):
        got = resolve_reasoning_controls(base, "whatever-model")
        assert got["fields"] == {}, base
        assert got["known"] is False, base


def test_unknown_host_reasoning_model_is_flagged():
    """A reasoning model on an UNKNOWN host must be flagged honestly + budget-protected.

    This is the "anything else a customer attaches reasons away unseen" gap: we cannot guess a
    suppression field, but we must not silently pretend it is off either. can_disable=False +
    reasoning_model=True + inflate_budget=True lets the caller warn and pad the budget.
    """
    got = resolve_reasoning_controls("https://llm.example.com", "deepseek-r1")
    assert got["known"] is False
    assert got["can_disable"] is False
    assert got["reasoning_model"] is True
    assert got["inflate_budget"] is True
    assert got["fields"] == {}


@pytest.mark.parametrize("base_url,model", [
    ("https://api.x.ai", "grok-4.5"),                                    # docs: cannot disable
    ("https://generativelanguage.googleapis.com", "gemini-3-pro"),       # low/high only, no off
    ("https://generativelanguage.googleapis.com", "gemini-2.5-pro"),     # 128-token floor
    ("https://api.anthropic.com", "claude-fable-5"),                     # rejects the disable
    ("https://api.groq.com", "openai/gpt-oss-120b"),                     # low/med/high only
    ("https://api.perplexity.ai", "sonar-reasoning-pro"),                # think section cannot be suppressed
    ("https://api.z.ai", "glm-5.3"),                                    # forced thinking; disabled errors
    ("https://api.z.ai/api/coding/paas/v4", "glm-5.2"),                  # coding plan: forced floor (live-keyed)
    ("https://api.z.ai", "glm-4.7"),                                    # forced thinking; no off
    ("https://api.groq.com", "deepseek-r1-distill-qwen-32b"),           # no known off value on Groq
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


# ── the programmatic "does this model reason?" detector ──────────────────────────────────
@pytest.mark.parametrize("model", [
    "gpt-5.4-mini", "o1-mini", "o3", "o4-mini", "deepseek-reasoner", "deepseek-r1",
    "deepseek-v4", "deepseek-v3.1", "qwq-32b", "grok-4.6", "gemini-2.5-pro", "gemini-3-pro",
    "gemini-2.0-flash-thinking", "claude-opus-5", "claude-sonnet-5", "claude-opus-4-7",
    "kimi-k2", "kimi-latest", "glm-4.6", "glm-5", "sonar-reasoning-pro", "sonar-deep-research",
    "gpt-oss-120b", "openai/gpt-oss-20b",
])
def test_model_reasons_detects_reasoning_family(model):
    assert _model_reasons(model) is True


@pytest.mark.parametrize("model", [
    "gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo", "deepseek-chat", "sonar", "sonar-pro",
    "llama-3.1-8b-instant", "mistral-tiny", "mistral-large-latest", "gemini-1.5-pro",
    "gemini-2.0-flash", "claude-3-opus", "claude-haiku-3", "qwen2.5-coder", "qwen3-max",
])
def test_model_reasons_does_not_flag_non_reasoning(model):
    assert _model_reasons(model) is False


# ── the resolved field actually reaches the wire ─────────────────────────────────────────
def test_payload_carries_suppression_and_not_the_vllm_field(monkeypatch):
    """The whole point: it must land in the PAYLOAD, and must not smuggle the vLLM field.

    chat_template_kwargs is what Cerebras 400s on — it is only ever valid for
    self-hosted vLLM/OpenWebUI, never for a hosted OpenAI-compatible provider.
    """

    monkeypatch.setattr("src.api.llm_client._tenant_brain",
                        lambda: {"base_url": "https://api.deepseek.com", "backend_type": "openai"})
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

    monkeypatch.setattr("src.api.llm_client._tenant_brain",
                        lambda: {"base_url": "https://api.deepseek.com", "backend_type": "openai"})
    monkeypatch.setenv("LLM_REASONING_SUPPRESSION", "false")
    payload = build_llm_payload([{"role": "user", "content": "hi"}],
                                "deepseek-v4-flash", max_tokens=2048)
    assert "thinking" not in payload


def test_explicit_caller_thinking_is_not_clobbered(monkeypatch):
    """A deliberate opt-in must survive the blanket suppression (setdefault, not overwrite)."""

    monkeypatch.setattr("src.api.llm_client._tenant_brain",
                        lambda: {"base_url": "https://api.anthropic.com", "backend_type": "anthropic"})
    monkeypatch.delenv("LLM_REASONING_SUPPRESSION", raising=False)
    payload = build_llm_payload([{"role": "user", "content": "hi"}], "claude-sonnet-5",
                                max_tokens=1024, thinking={"type": "enabled",
                                                            "budget_tokens": 2048})
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}


# ── capture-intact: the completion budget is padded when reasoning cannot be disabled ─────
def test_reasoning_headroom_applied_when_cannot_disable(monkeypatch):
    """PIN THE VALUE, don't re-derive it. This asserted `== 256 + _reasoning_token_headroom()`
    — both sides reading the same env var — so deleting the padding entirely still passed, and
    LLM_REASONING_TOKEN_HEADROOM=0 gave an identical green. A guard that computes the expected
    value the same way the code does cannot fail. The headroom is pinned to an explicit number
    here, and the default is asserted separately so a silent default change is also caught."""
    from src.api.llm_calls import _apply_reasoning_headroom, _reasoning_token_headroom
    monkeypatch.setattr("src.api.llm_client._tenant_brain",
                        lambda: {"base_url": "https://api.x.ai", "backend_type": "openai"})
    monkeypatch.setenv("LLM_REASONING_TOKEN_HEADROOM", "512")
    assert _apply_reasoning_headroom("grok-4.5", 256) == 768
    monkeypatch.delenv("LLM_REASONING_TOKEN_HEADROOM", raising=False)
    assert _reasoning_token_headroom() == 1024, "the documented default must not drift silently"
    assert _apply_reasoning_headroom("grok-4.5", 256) == 1280


def test_reasoning_headroom_not_applied_when_can_disable(monkeypatch):
    from src.api.llm_calls import _apply_reasoning_headroom
    monkeypatch.setattr("src.api.llm_client._tenant_brain",
                        lambda: {"base_url": "https://api.deepseek.com", "backend_type": "openai"})
    assert _apply_reasoning_headroom("deepseek-v4-flash", 256) == 256


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

    # expected=True (cannot-disable model): reasoning is expected, must not raise, and must
    # NOT be treated as a suppression failure.
    _audit_reasoning_leak(leaked, "EXTRACT", "u1", expected=True)

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


# ── z.ai serves TWO surfaces on ONE host ────────────────────────────────────────────
# Regression guard for the 2026-08-18 defect. The controls table keyed on HOST alone, so
# api.z.ai matched both the standard surface (/api/paas/v4) and the Coding Plan surface
# (/api/coding/paas/v4). z.ai documents Preserved Thinking as ON by default on the coding
# surface and OFF on the standard one, so the same thinking:{"type":"disabled"} that works
# on the standard surface is accepted and IGNORED on the coding one. Measured live on
# glm-5.2 over /api/coding/paas/v4: reasoning_tokens 19 / 32 / 93 with the field sent.
#
# The failure mode this guards is silent: suppression reported as working while reasoning
# tokens are spent inside the completion budget, which is how extraction JSON gets truncated.


def test_zai_coding_surface_does_not_claim_a_suppression_it_cannot_deliver():
    """The coding surface must report cannot-disable and pad the budget, not fake success.

    RE-RECONCILED 2026-08-21 (fix/doc-lane-fairness): this test survived merge 077c2886 as
    the STALE half — it pinned the 2026-08-18 "do not send a guessed field" decision, while
    the table it exercises was reconciled to the 2026-08-19 LIVE-KEYED A/B (n=6/variant,
    glm-5.2 on /api/coding/paas/v4): effort=low caps reasoning at a ~8-token floor (vs 145
    median with no field) with 6/6 success. The current contract therefore SENDS
    reasoning_effort=low while keeping can_disable=False honest (a floor is not zero) and
    inflating the budget. The merge also left the file uncollectable (a stray conflict
    marker), which is why the staleness surfaced only now.
    """
    spec = resolve_reasoning_controls(
        "https://api.z.ai/api/coding/paas/v4", "glm-5.2")
    assert spec["fields"] == {"reasoning_effort": "low"}, (
        "the live-keyed A/B confirmed effort=low is honored as a floor on the coding "
        "surface — not sending it costs ~145 reasoning tokens/turn (2/6 timeouts)")
    assert spec["can_disable"] is False, "8 tokens is a floor, not an off switch"
    assert spec["inflate_budget"] is True, (
        "cannot-disable means reasoning tokens are spent INSIDE the completion budget; "
        "the budget must be padded or fact-dense turns truncate")


def test_zai_standard_surface_still_suppresses():
    """The standard surface is unaffected — the fix must not disarm the working path."""
    spec = resolve_reasoning_controls(
        "https://api.z.ai/api/paas/v4", "glm-5.2")
    assert spec["fields"] == {"thinking": {"type": "disabled"}}
    assert spec["can_disable"] is True
    assert spec["inflate_budget"] is False


def test_zai_surfaces_are_distinguished_by_path_not_host():
    """Same host, same model, opposite verdicts — that is the whole point of the split."""
    coding = resolve_reasoning_controls(
        "https://api.z.ai/api/coding/paas/v4", "glm-5.2")
    standard = resolve_reasoning_controls(
        "https://api.z.ai/api/paas/v4", "glm-5.2")
    assert coding["can_disable"] != standard["can_disable"]


def test_zai_coding_surface_non_reasoning_model_is_untouched():
    """A non-reasoning GLM on the coding surface must not be dragged into cannot-disable."""
    spec = resolve_reasoning_controls(
        "https://api.z.ai/api/coding/paas/v4", "glm-4-flash")
    assert spec["can_disable"] is True
    assert spec["inflate_budget"] is False


def test_zai_coding_surface_survives_url_spelling_variants():
    """A stored base_url is tenant-typed text. A missing trailing slash or a doubled slash
    from a copy-paste must not silently select the OTHER provider surface — the consequence
    is that we lose the live-keyed reasoning_effort floor, report can_disable=True, leave
    the budget unpadded, and reason at ~145 tokens/turn inside the completion budget.

    (Re-reconciled 2026-08-21 alongside its sibling above: the coding surface's pinned
    contract is the 08-19 live-keyed effort=low floor, not the 08-18 "send nothing".)"""
    for url in ("https://api.z.ai/api/coding/paas/v4",
                "https://api.z.ai/api/coding",
                "https://api.z.ai//api/coding/paas/v4",
                "https://api.z.ai/api/coding/",
                "https://API.Z.AI/API/CODING/PAAS/V4"):
        spec = resolve_reasoning_controls(url, "glm-5.2")
        assert spec["can_disable"] is False, f"{url} must resolve to the CODING surface"
        assert spec["fields"] == {"reasoning_effort": "low"}, (
            f"{url} must send the live-keyed floor field")
    std = resolve_reasoning_controls("https://api.z.ai/api/paas/v4", "glm-5.2")
    assert std["can_disable"] is True, "the standard surface must be unaffected"
