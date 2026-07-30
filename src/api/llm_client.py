"""Centralized LLM endpoint authentication, configuration, and request building."""

import os
import httpx
import structlog
import uuid
import time
from typing import Optional, Any

log = structlog.get_logger(__name__)


# ── Backend type configuration ────────────────────────────────────────────────
# Path appended to LLM_BASE_URL per backend type.
# "raw" type expects LLM_BASE_URL to already be the full URL (no path appended).
_BACKEND_PATHS: dict[str, str] = {
    "openwebui":  "/api/chat/completions",
    "ollama":     "/v1/chat/completions",
    "lm_studio":  "/v1/chat/completions",
    "openai":     "/v1/chat/completions",
    "anthropic":  "/v1/messages",
    "groq":       "/openai/v1/chat/completions",
    "localai":    "/v1/chat/completions",
    "raw":        "",  # full URL in LLM_BASE_URL
}

# Response parsing strategy per backend type.
# "openai" covers all OpenAI-compatible backends.
_BACKEND_RESPONSE_FORMAT: dict[str, str] = {
    "openwebui":  "openai",
    "ollama":     "openai",
    "lm_studio":  "openai",
    "openai":     "openai",
    "anthropic":  "anthropic",
    "groq":       "openai",
    "localai":    "openai",
    "raw":        "openai",
}


# ── Per-user intent confidence gate bounds (single source of truth) ───────────
# CANONICAL DEFINITION. The re_embedder writer (src/re_embedder/embedder.py) and
# the /classify-intent + /confidence-gate readers (src/api/main.py) ALL import
# these — do not redefine the literals anywhere else.
#
# The gate is the GLiNER2 intent-confidence threshold above which /classify-intent
# trusts GLiNER2 directly (skipping pattern-match escalation). The re_embedder
# self-tunes it from correction feedback and may bias it DOWNWARD toward GATE_MIN
# where GLiNER2 is proven reliable (cheaper, less escalation, trust GLiNER2 more).
# That downward bias is a deliberate PRODUCT DECISION — a human may revisit the
# direction, but code must not flip it.
GATE_MIN = 0.50
GATE_MAX = 0.75
GATE_DEFAULT = 0.70


def clamp_gate(value: float) -> float:
    """Clamp an intent confidence gate to [GATE_MIN, GATE_MAX]."""
    return max(GATE_MIN, min(GATE_MAX, value))


def get_backend_type() -> str:
    """Return normalised LLM_BACKEND_TYPE, defaulting to 'openwebui'."""
    return os.environ.get("LLM_BACKEND_TYPE", "openwebui").lower().strip()


def get_backend_endpoint() -> str | None:
    """
    Build the full LLM endpoint URL from LLM_BACKEND_TYPE + LLM_BASE_URL.
    Returns None if LLM_BASE_URL is not set
    (caller falls through to legacy get_endpoint_list() chain).
    """
    backend_type = get_backend_type()
    base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    if not base_url:
        return None
    path = _BACKEND_PATHS.get(backend_type, "/v1/chat/completions")
    return f"{base_url}{path}"


def get_backend_response_format() -> str:
    """Return response format key for the active backend. 'openai' or 'anthropic'."""
    return _BACKEND_RESPONSE_FORMAT.get(get_backend_type(), "openai")


def _thinking_kwarg_backends() -> set[str]:
    """
    Backends that accept the `chat_template_kwargs={"enable_thinking": False}`
    top-level field on the request payload.

    This is NOT universally safe. Strict OpenAI-compatible providers reject
    unknown top-level fields with an HTTP 400 rather than silently ignoring
    them — e.g. Cerebras returns 400 ("chat_template_kwargs ... is unsupported",
    code=wrong_api_format), and OpenAI / Groq / most hosted gateways behave the
    same way. Injecting the field unconditionally 400s EVERY LLM call for those
    users. So we gate the injection to the OpenWebUI/vLLM family (default just
    "openwebui") and rely on the post-hoc `_strip_think_tags` net elsewhere.

    Env-extensible via LLM_CHAT_TEMPLATE_KWARGS_BACKENDS (comma-separated) for
    self-hosted vLLM/OpenWebUI-style backends that DO honour the field.
    """
    raw = os.environ.get("LLM_CHAT_TEMPLATE_KWARGS_BACKENDS", "openwebui")
    return {b.strip().lower() for b in raw.split(",") if b.strip()}


# ── Reasoning / "thinking" suppression, keyed by ENDPOINT HOST ────────────────────────
# WHY HOST-KEYED AND NOT backend_type: ``backend_type="openai"`` is what a tenant picks for
# EVERY OpenAI-compatible provider — DeepSeek, Cerebras, Groq, OpenRouter, xAI, LM Studio —
# and each has a DIFFERENT (or absent) reasoning switch. "OpenAI-compatible" describes the
# transport, not the parameter vocabulary. Keying on backend_type is therefore structurally
# unable to get this right; we key on the host, exactly as ``resolve_provider_limits`` does.
#
# WHY THIS MATTERS AT ALL: FaultLine uses the LLM only for STRUCTURED extraction, where a
# chain of thought is pure waste — and worse than waste. Measured 2026-07-30 against DeepSeek
# v4-flash with a control:
#     thinking disabled → 1291ms, 236 output tokens, 0 reasoning, 5/5 parsed
#     thinking default  → 5844ms, 4472 output tokens, ~15.8k reasoning chars, 4/5 parsed
# The reasoning run produced an UNPARSEABLE response — so leaving reasoning on is a
# CORRECTNESS defect in an extraction pipeline, not merely a cost one.
#
# WHY NOT "optimistically send and retry on 400": because the dangerous failure is SILENT.
# We sent Qwen/vLLM's ``enable_thinking`` to DeepSeek and got HTTP 200 with full reasoning —
# no error to retry on. A 400-retry strategy catches the strict providers and misses exactly
# the ones that quietly ignore you. So: an explicit per-host allowlist, and NO field at all
# for an unrecognized host (guessing there risks a hard 400 on every call — the Cerebras
# ``chat_template_kwargs`` incident).
#
# ``can_disable=False`` is recorded HONESTLY: several providers cannot turn reasoning off at
# all (xAI Grok, Gemini 3, Gemini 2.5 Pro, Claude Fable/Mythos). For those we send the lowest
# available effort and the caller can surface a warning — we never pretend suppression worked.
#
# Sources (fetched 2026-07-30): DeepSeek https://api-docs.deepseek.com/guides/thinking_mode/ ·
# OpenAI https://developers.openai.com/api/docs/guides/reasoning · Anthropic
# https://platform.claude.com/docs/en/build-with-claude/thinking · Gemini
# https://ai.google.dev/gemini-api/docs/generate-content/thinking · Groq
# https://console.groq.com/docs/reasoning · DashScope
# https://www.alibabacloud.com/help/en/model-studio/deep-thinking · Mistral
# https://docs.mistral.ai/capabilities/reasoning/ · xAI https://docs.x.ai/docs/guides/reasoning ·
# OpenRouter https://openrouter.ai/docs/use-cases/reasoning-tokens · Cerebras
# https://inference-docs.cerebras.ai/capabilities/reasoning
#
# Each entry: ``models`` = ordered (substring, spec) pairs, first match wins; ``default`` =
# fallback for that host. A spec is {"fields": {...merged into payload...},
# "can_disable": bool}. An EMPTY fields dict means "nothing to send" (already off by default).
_REASONING_CONTROLS: dict[str, dict] = {
    # DeepSeek — LIVE-VERIFIED here with a control run (see numbers above). The toggle
    # defaults to "enabled" on v4, which is why v4 reasoned unprompted. `deepseek-chat`
    # (legacy alias) is already non-thinking; sending the field is harmless and explicit.
    "api.deepseek.com": {
        "models": [],
        "default": {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True},
    },
    # OpenAI — `reasoning_effort` is REJECTED (400 unsupported_parameter) on non-reasoning
    # models like gpt-4o, so it is scoped to the reasoning families only. "none" is documented
    # for latency-critical work; the older o-series predates it, so use its lowest ("low").
    "api.openai.com": {
        "models": [
            ("gpt-5", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
            ("o1", {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
            ("o3", {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
            ("o4", {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
        ],
        # gpt-4o / gpt-4-turbo / gpt-3.5: no reasoning, and the field would 400. Send nothing.
        "default": {"fields": {}, "can_disable": True},
    },
    # Anthropic — thinking is OPT-IN on 4.x and earlier (omit = off). On the adaptive-thinking
    # models it defaults ON and needs an explicit disable. Fable/Mythos REJECT the disable.
    "api.anthropic.com": {
        "models": [
            ("claude-fable", {"fields": {}, "can_disable": False}),
            ("claude-mythos", {"fields": {}, "can_disable": False}),
            ("claude-opus-5", {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True}),
            ("claude-sonnet-5", {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True}),
            ("claude-opus-4-7", {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True}),
            ("claude-opus-4-8", {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True}),
        ],
        "default": {"fields": {}, "can_disable": True},   # 4.5 and earlier: off unless asked
    },
    # Google Gemini — TWO different fields by generation, and several models cannot disable.
    # 2.5 Flash/Flash-Lite accept budget 0; 2.5 Pro has a 128-token FLOOR; Gemini 3 replaced
    # the budget with thinkingLevel ("low"|"high" only — no off value exists).
    "generativelanguage.googleapis.com": {
        "models": [
            ("gemini-2.5-flash",
             {"fields": {"thinkingConfig": {"thinkingBudget": 0}}, "can_disable": True}),
            ("gemini-2.5-pro",
             {"fields": {"thinkingConfig": {"thinkingBudget": 128}}, "can_disable": False}),
            ("gemini-3",
             {"fields": {"thinkingConfig": {"thinkingLevel": "low"}}, "can_disable": False}),
        ],
        "default": {"fields": {}, "can_disable": True},
    },
    # Groq — per-model: Qwen3 supports "none"; GPT-OSS only has low/medium/high (no off).
    "api.groq.com": {
        "models": [
            ("qwen", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
            ("gpt-oss", {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
        ],
        "default": {"fields": {}, "can_disable": True},
    },
    # Alibaba DashScope / Qwen — `enable_thinking` (top-level on raw REST, which is what we
    # send). Thinking-ONLY model ids cannot be turned off.
    "dashscope-intl.aliyuncs.com": {
        "models": [
            ("thinking", {"fields": {}, "can_disable": False}),
            ("qwq", {"fields": {}, "can_disable": False}),
            ("qwen", {"fields": {"enable_thinking": False}, "can_disable": True}),
        ],
        "default": {"fields": {}, "can_disable": True},
    },
    "dashscope.aliyuncs.com": {
        "models": [
            ("thinking", {"fields": {}, "can_disable": False}),
            ("qwq", {"fields": {}, "can_disable": False}),
            ("qwen", {"fields": {"enable_thinking": False}, "can_disable": True}),
        ],
        "default": {"fields": {}, "can_disable": True},
    },
    # Mistral — reasoning moved into the general models via `reasoning_effort`; "none" = off.
    "api.mistral.ai": {
        "models": [
            ("mistral-small", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
            ("mistral-medium", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
        ],
        "default": {"fields": {}, "can_disable": True},
    },
    # xAI Grok — docs are explicit: "Reasoning cannot be disabled." Depth only. NOTE the
    # footgun: on grok-4.20-multi-agent the SAME field name means agent count, not depth —
    # so we deliberately send NOTHING here rather than guess a semantic.
    "api.x.ai": {
        "models": [],
        "default": {"fields": {}, "can_disable": False},
    },
    # OpenRouter — unified object; it translates/degrades downstream per provider.
    "openrouter.ai": {
        "models": [],
        "default": {"fields": {"reasoning": {"effort": "none"}}, "can_disable": True},
    },
    # Cerebras — zai-glm accepts "none"; gpt-oss/gemma have no off value. The boolean
    # `disable_reasoning` is deprecated (2026-07-21) — do NOT build on it.
    "api.cerebras.ai": {
        "models": [
            ("zai-glm", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
            ("gpt-oss", {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
            ("gemma", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
        ],
        "default": {"fields": {}, "can_disable": True},
    },
}

# Where each provider reports a reasoning-token COUNT, so suppression can be VERIFIED rather
# than assumed. Paths are dotted, relative to the parsed response. Providers absent from this
# map do not expose a count (verify via content instead). DeepSeek IS here — measured live at
# completion_tokens_details.reasoning_tokens = 440/503/791 on the un-suppressed control.
_REASONING_USAGE_PATHS: tuple[str, ...] = (
    "usage.completion_tokens_details.reasoning_tokens",   # OpenAI-compat, DeepSeek, Groq
    "usage.output_tokens_details.reasoning_tokens",       # OpenAI Responses
    "usage.output_tokens_details.thinking_tokens",        # Anthropic
    "usage.reasoning_tokens",                             # xAI (NOT folded into completion)
    "usageMetadata.thoughtsTokenCount",                   # Gemini
)


def _reasoning_suppression_enabled() -> bool:
    """Kill switch for reasoning suppression. Default ON — it is a correctness fix.

    Set ``LLM_REASONING_SUPPRESSION=false`` to send no suppression field anywhere (e.g. to
    isolate a provider that has started rejecting a field we inject).
    """
    return os.environ.get("LLM_REASONING_SUPPRESSION", "true").strip().lower() \
        not in ("false", "0", "no", "off")


def resolve_reasoning_controls(base_url: Optional[str], model: Optional[str]) -> dict:
    """Resolve how to disable reasoning for an endpoint host + model.

    Returns ``{"fields": dict, "can_disable": bool, "known": bool, "source": str}``.
      * ``known=True``  → recognized provider; ``fields`` are safe to merge into the payload.
      * ``known=False`` → unrecognized host (self-hosted / LAN / new provider). ``fields`` is
        EMPTY: we do not guess, because an unsupported top-level field HTTP-400s on strict
        providers and would break every call on that endpoint.
      * ``can_disable=False`` → this model cannot have reasoning fully disabled (documented);
        ``fields`` may still carry the lowest available effort.
    Fail-safe: any parse problem → unknown, no fields.
    """
    host = ""
    try:
        from urllib.parse import urlparse
        if base_url:
            host = (urlparse(str(base_url)).hostname or "").lower()
    except Exception:
        host = ""

    entry = None
    for known_host, e in _REASONING_CONTROLS.items():
        if host == known_host or host.endswith("." + known_host):
            entry = e
            break
    if entry is None:
        return {"fields": {}, "can_disable": True, "known": False,
                "source": host or "unknown"}

    m = (model or "").lower()
    for model_key, spec in entry.get("models", []):
        if model_key in m:
            return {"fields": dict(spec["fields"]), "can_disable": spec["can_disable"],
                    "known": True, "source": host}
    spec = entry.get("default", {"fields": {}, "can_disable": True})
    return {"fields": dict(spec["fields"]), "can_disable": spec["can_disable"],
            "known": True, "source": host}


def _reasoning_base_url() -> Optional[str]:
    """The base_url this call will hit, used to resolve the provider's reasoning switch.

    Open-core reads the configured global endpoint. (The hosted build resolves a per-tenant
    BYO endpoint first; that indirection has no meaning here, where there is exactly one
    configured brain.)
    """
    return os.environ.get("LLM_BASE_URL") or None

def get_llm_headers() -> dict:
    """
    Return HTTP headers for LLM authentication.

    Backend-type-aware: Anthropic uses x-api-key + anthropic-version headers;
    all other backends use standard Bearer token.
    Single source of truth for all LLM endpoint auth across all modules.
    """
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    backend = get_backend_type()

    if backend == "anthropic":
        headers: dict = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if api_key:
            headers["x-api-key"] = api_key
        return headers

    # All other backends: standard Bearer token
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def get_embedding_headers() -> dict:
    """
    Return HTTP headers for the EMBEDDING endpoint.

    The embedding endpoint may live on a DIFFERENT host than the chat LLM
    (EMBEDDING_API_URL) and may need its own auth. Precedence:
    - EMBEDDING_API_KEY (dedicated embedding key) wins when set,
    - else fall back to LLM_API_KEY (same provider / same key case),
    - else no auth (keyless local embedders — Ollama / LM Studio).
    Embedding endpoints are OpenAI-compatible (Bearer); Anthropic has none.
    """
    api_key = (os.environ.get("EMBEDDING_API_KEY", "").strip()
               or os.environ.get("LLM_API_KEY", "").strip())
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def build_llm_payload(
    messages: list[dict],
    model: str,
    user_id: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 200,
    thinking: Optional[dict] = None,
    stream: bool = False,
    **args
) -> dict:
    """
    Build LLM request payload with centralized dBug-016 chat_id injection.

    dBug-016: OpenWebUI requires chat_id in request to prevent NoneType crash
    in process_chat middleware. Injecting user_id as chat_id avoids the upstream
    OpenWebUI bug when calling /api/chat/completions from FaultLine modules.

    dprompt-121: FaultLine internal LLM calls (extraction, WGM, etc.) default to
    stream=false. Some OpenAI-compatible implementations incorrectly default to
    streaming when stream is omitted (see unslothai/unsloth#5047), causing
    response.json() to hang on SSE format responses. Explicit stream=false ensures
    non-streaming JSON responses across all backends.

    Args:
        messages: List of message dicts with role/content
        model: Model name string
        user_id: User UUID to inject as chat_id (prevents dBug-016 crash)
        temperature: LLM temperature (default 0.0 for deterministic)
        max_tokens: Max output tokens
        thinking: Thinking config dict (e.g. {"type": "disabled"})
        stream: Whether to stream response (default False for internal calls)
        **args: Additional fields to merge into payload

    Returns:
        Complete payload dict ready for httpx.post(json=payload)
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }

    backend = get_backend_type()

    # dBug-016 fix: chat_id is OpenWebUI-specific — only inject when talking to OpenWebUI.
    # Priority chain: user_id → FAULTLINE_USER_ID → dynamic timestamp fallback.
    if backend == "openwebui":
        chat_id = user_id or os.environ.get("FAULTLINE_USER_ID") or f"faultline-{int(time.time())}"
        payload["chat_id"] = chat_id

    # thinking field: OpenWebUI/Anthropic only — strip for all other backends to avoid
    # sending unsupported fields (e.g. Ollama, LM Studio reject unknown keys in strict mode).
    if thinking:
        if backend in ("openwebui", "anthropic"):
            payload["thinking"] = thinking
        # else: silently drop — unsupported by this backend

    # ── Reasoning/thinking suppression ───────────────────────────────────────
    # FaultLine uses LLMs exclusively for structured JSON extraction — thinking
    # mode wastes tokens, adds latency, and breaks JSON parsing.  Disable it
    # across ALL backends unconditionally (openwebui included — qwen3.5-9b served
    # behind OpenWebUI otherwise spends the completion budget reasoning, which
    # truncates extraction output mid-array).
    #
    # Qwen3.5 ≤9B should default to thinking-off, but serving frameworks
    # (LM Studio, llama.cpp) have known bugs where the default is ignored.
    # Explicit suppression is required.  See BUGS/qwen3-thinking-mode/.
    #
    # chat_template_kwargs is the Qwen3.5 / vLLM / OpenWebUI mechanism.
    #
    # This field is NOT universally safe: strict OpenAI-compatible providers
    # reject unknown top-level fields with an HTTP 400 instead of ignoring them
    # (Cerebras: "chat_template_kwargs ... is unsupported", code=wrong_api_format;
    # OpenAI / Groq / most hosted gateways do the same). Injecting it
    # unconditionally 400s EVERY call for a user on such a provider. So gate the
    # injection to the OpenWebUI/vLLM family (default "openwebui", env-extensible
    # via LLM_CHAT_TEMPLATE_KWARGS_BACKENDS); strict providers get a clean payload
    # and rely on the post-hoc `_strip_think_tags` net.
    #
    # anthropic never gets it: its API has no chat_template_kwargs and handles
    # thinking via the dedicated `thinking` field, normalized in the
    # anthropic-specific block below (disabled == field absent).
    if backend in _thinking_kwarg_backends():
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    # ── Reasoning suppression, HOST-resolved (the hosted-provider half) ──────────
    # The block above only covers SELF-HOSTED vLLM/OpenWebUI, whose LAN host we cannot
    # recognize. Hosted providers each need their OWN documented field — and the
    # fallback above ("strict providers rely on the post-hoc _strip_think_tags net")
    # is not suppression at all: stripping tags is COSMETIC, since the tokens and the
    # latency are already spent, and if the model burned its budget reasoning the JSON
    # may be truncated with nothing left to salvage.
    #
    # Measured against DeepSeek v4-flash with a control (2026-07-30):
    #     thinking disabled -> 1291ms,  236 output tokens, 0 reasoning,      5/5 parsed
    #     thinking default  -> 5844ms, 4472 output tokens, ~15.8k reasoning, 4/5 parsed
    # The un-suppressed run produced an UNPARSEABLE response, so leaving reasoning on is
    # a CORRECTNESS defect for structured extraction, not merely a cost one.
    #
    # Merged, never overwritten: an explicit caller-supplied `thinking` (handled above)
    # wins, so a deliberate opt-in is not clobbered by the blanket suppression.
    if _reasoning_suppression_enabled():
        try:
            controls = resolve_reasoning_controls(_reasoning_base_url(), model)
            for key, value in (controls.get("fields") or {}).items():
                payload.setdefault(key, value)
            if controls.get("known") and not controls.get("can_disable"):
                # Honest signal: this model CANNOT have reasoning fully disabled. We sent the
                # lowest effort available and must not pretend it is off.
                log.warning("llm_reasoning.cannot_disable", host=controls.get("source"),
                            model=model,
                            note="provider does not support disabling reasoning; lowest "
                                 "available effort sent — expect reasoning tokens")
        except Exception as exc:
            # Never let suppression resolution break a call — worst case we reason.
            log.warning("llm_reasoning.resolve_failed", error=repr(exc)[:160])

    # Anthropic-specific request shape adjustments
    if backend == "anthropic":
        # Extract system message from messages array into top-level system field
        system_content = ""
        user_messages = []
        for msg in payload.get("messages", []):
            if msg.get("role") == "system":
                system_content = msg.get("content", "")
            else:
                user_messages.append(msg)
        if system_content:
            payload["system"] = system_content
        payload["messages"] = user_messages
        # max_tokens is required for Anthropic — ensure it is present
        if "max_tokens" not in payload:
            payload["max_tokens"] = 1024
        # Remove thinking if set to disabled — Anthropic treats absent as "no thinking"
        if payload.get("thinking", {}).get("type") == "disabled":
            del payload["thinking"]

    # Merge any additional fields, but NEVER allow args to override stream
    # (stream=false is CRITICAL and must not be overridden by callers)
    args.pop("stream", None)
    payload.update(args)

    return payload


# ── Endpoint resolution (canonical home) ─────────────────────────────────────


def get_endpoint_list() -> list[str]:
    """
    Canonical source of truth for LLM endpoint URL priority chain.

    Moved here from src/api/llm_calls._get_endpoint_list() so all config
    logic lives in one module. llm_calls._get_endpoint_list() delegates here.

    Priority (Docker-aware):
    0. LLM_BACKEND_TYPE + LLM_BASE_URL → backend-typed endpoint (highest priority)
    1. OPENWEBUI_INTERNAL_URL  → appends /api/chat/completions
    2. OPENWEBUI_URL           → appends /api/chat/completions
    3. QWEN_API_URL            → used as-is
    4. Hardcoded fallbacks     → only when no env vars set

    Returns:
        Ordered list of complete endpoint URLs, deduplicated, ready to POST to.
    """
    endpoints = []

    # Priority 0: typed backend (LLM_BACKEND_TYPE + LLM_BASE_URL) — always wins
    typed = get_backend_endpoint()
    if typed:
        endpoints.append(typed)
        # Return immediately — typed backend is authoritative; no legacy fallthrough
        log.debug("llm_endpoints.resolved", count=1, first=typed)
        return endpoints

    openwebui_internal = os.environ.get("OPENWEBUI_INTERNAL_URL", "").strip()
    if openwebui_internal:
        if not openwebui_internal.startswith("http"):
            openwebui_internal = f"http://{openwebui_internal}"
        endpoints.append(f"{openwebui_internal.rstrip('/')}/api/chat/completions")

    openwebui_external = os.environ.get("OPENWEBUI_URL", "").strip()
    if openwebui_external:
        log.warning("OPENWEBUI_URL is deprecated — use LLM_BACKEND_TYPE + LLM_BASE_URL instead")
        if not openwebui_external.startswith("http"):
            openwebui_external = f"http://{openwebui_external}"
        endpoints.append(f"{openwebui_external.rstrip('/')}/api/chat/completions")

    qwen_api = os.environ.get("QWEN_API_URL", "").strip()
    if qwen_api:
        if not qwen_api.startswith("http"):
            qwen_api = f"http://{qwen_api}"
        endpoints.append(qwen_api.rstrip("/"))

    if not endpoints:
        endpoints.append("http://open-webui:8080/api/chat/completions")
        endpoints.append("http://localhost:8080/api/chat/completions")
        endpoints.append("http://localhost:11434/v1/chat/completions")

    seen = set()
    unique = []
    for ep in endpoints:
        if ep and ep not in seen:
            unique.append(ep)
            seen.add(ep)

    log.debug("llm_endpoints.resolved", count=len(unique), first=unique[0] if unique else None)
    return unique


def get_llm_chat_url() -> str:
    """
    Return the primary LLM chat endpoint URL.

    Convenience wrapper over get_endpoint_list() for callers that only need
    a single URL. Returns first (highest-priority) endpoint.

    Returns:
        str — complete endpoint URL ready to POST to.
    """
    endpoints = get_endpoint_list()
    if not endpoints:
        log.critical("llm_endpoint.no_endpoints_available")
        return "http://open-webui:8080/api/chat/completions"
    url = endpoints[0]
    log.info("llm_endpoint.selected", endpoint=url)
    return url


def get_embedding_url(chat_url: str) -> str:
    """
    Derive embedding endpoint URL from the active chat endpoint.

    Backend-type-aware:
    - Explicit EMBEDDING_API_URL env var always wins.
    - Anthropic has no embedding endpoint — returns "" (fastembed fallback handles this).
    - openwebui with LLM_BASE_URL → {base}/api/embeddings
    - Other backends with LLM_BASE_URL → {base}/v1/embeddings
    - Legacy path (no LLM_BASE_URL) → derive from chat_url path substitution.

    Args:
        chat_url: The active chat completions endpoint URL.

    Returns:
        str — complete embedding endpoint URL, or "" if not applicable.
    """
    explicit = os.environ.get("EMBEDDING_API_URL", "").strip()
    if explicit:
        return explicit

    backend = get_backend_type()
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")

    if backend == "anthropic":
        # Anthropic has no embedding endpoint — fastembed fallback handles this
        return ""

    if base:
        if backend == "openwebui":
            return f"{base}/api/embeddings"
        # All other backends with explicit base URL use OpenAI-compat path
        return f"{base}/v1/embeddings"

    # Legacy derivation from chat URL (no LLM_BASE_URL set)
    return (
        chat_url
        .replace("/api/chat/completions", "/api/embeddings")
        .replace("/v1/chat/completions", "/v1/embeddings")
        .replace("/openai/v1/chat/completions", "/v1/embeddings")
    )


def get_health_check_url(chat_url: str) -> str:
    """
    Derive LLM health check URL from the active chat endpoint.

    Backend-type-aware:
    - Anthropic: /v1/models (returns 200 with model list; server reachable = up)
    - OpenWebUI: /api/version
    - OpenAI-compat: /v1/models (or /openai/v1/models for Groq)

    Args:
        chat_url: The active chat completions endpoint URL.

    Returns:
        str — URL to GET for health check (200/401/404 = up, exception = down).
    """
    backend = get_backend_type()

    if backend == "anthropic":
        base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        if base:
            return f"{base}/v1/models"
        # Fallback: derive from chat_url
        return chat_url.replace("/v1/messages", "/v1/models")

    if "/api/chat/completions" in chat_url:
        return chat_url.replace("/api/chat/completions", "/api/version")

    return (
        chat_url
        .replace("/v1/chat/completions", "/v1/models")
        .replace("/openai/v1/chat/completions", "/openai/v1/models")
    )


# ── Diagnostic ────────────────────────────────────────────────────────────────


def get_llm_config() -> dict:
    """
    Return fully resolved LLM configuration as a plain dict.

    Reads all LLM-related env vars, resolves the active endpoint, and
    returns a snapshot that completely describes the current runtime
    configuration. Safe to log and return from /health — no secrets
    exposed (API key presence indicated, value masked).

    Returns:
        dict with keys: chat_endpoint, embedding_endpoint, health_check_url,
        model, pattern_extraction_model, auth_type, api_key_set, endpoint_source,
        backend_type (forward-compat field, always "legacy" until Phase 2).
    """
    chat_url = get_llm_chat_url()
    embed_url = get_embedding_url(chat_url)
    health_url = get_health_check_url(chat_url)
    api_key = os.environ.get("LLM_API_KEY", "").strip()

    # Determine which env var provided the winning endpoint
    if os.environ.get("OPENWEBUI_INTERNAL_URL", "").strip():
        source = "OPENWEBUI_INTERNAL_URL"
    elif os.environ.get("OPENWEBUI_URL", "").strip():
        source = "OPENWEBUI_URL"
    elif os.environ.get("QWEN_API_URL", "").strip():
        source = "QWEN_API_URL"
    else:
        source = "fallback"

    return {
        "backend_type": get_backend_type(),
        "chat_endpoint": chat_url,
        "embedding_endpoint": embed_url or "(none — fastembed fallback)",
        "health_check_url": health_url,
        # Model identity is pure config — report the raw env value, never a
        # guessed literal. "(unset)" surfaces a misconfiguration loudly in /health.
        "model": os.environ.get("WGM_LLM_MODEL") or "(unset)",
        "pattern_extraction_model": (
            os.environ.get("PATTERN_EXTRACTION_MODEL")
            or os.environ.get("WGM_LLM_MODEL")
            or "(unset)"
        ),
        "auth_type": "bearer" if api_key else "none",
        "api_key_set": bool(api_key),
        "endpoint_source": source,
    }


def call_llm(
    url: str,
    payload: dict,
    timeout: float = 30.0,
    debug_stats: Optional[bool] = None
) -> dict:
    """
    Make LLM API call with automatic LM Studio stats logging.

    Calls LLM endpoint via httpx and logs LM Studio backend metrics if available
    (time_to_first_token_seconds, tokens_per_second, etc.). Controlled by
    DEBUG_LM_STUDIO_STATS environment variable valve.

    Args:
        url: LLM endpoint URL (e.g., OpenWebUI or LM Studio)
        payload: Request payload (from build_llm_payload or custom)
        timeout: Request timeout in seconds
        debug_stats: Override DEBUG_LM_STUDIO_STATS env var for this call

    Returns:
        LLM response dict (OpenAI-compatible format)

    Raises:
        httpx.RequestError: On network/timeout errors
        ValueError: On non-200 response
    """
    debug_enabled = debug_stats is not None and debug_stats
    if debug_stats is None:
        debug_enabled = os.environ.get("DEBUG_LM_STUDIO_STATS", "").lower() in ("true", "1", "yes")

    headers = get_llm_headers()
    headers["Content-Type"] = "application/json"

    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    # Log LM Studio stats if available and enabled
    if debug_enabled and isinstance(data, dict):
        stats = data.get("stats")
        if stats and isinstance(stats, dict):
            log.info(
                "lm_studio.stats",
                time_to_first_token_ms=round(stats.get("time_to_first_token_seconds", 0) * 1000),
                tokens_per_second=round(stats.get("tokens_per_second", 0), 2),
                total_output_tokens=stats.get("total_output_tokens"),
                input_tokens=stats.get("input_tokens"),
                model=payload.get("model", "unknown"),
            )

    return data
