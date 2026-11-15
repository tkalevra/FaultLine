"""Centralized LLM endpoint authentication, configuration, and request building."""

import os
import re
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
    # Google serves its OpenAI-compatible surface under /v1beta/openai/ — same shape as Groq's
    # /openai/v1, a different order. Without this entry a tenant picking "OpenAI Compatible"
    # resolves …/v1/chat/completions against generativelanguage.googleapis.com, which 404s, and
    # the natural next move (pasting the /v1beta base) yields …/v1beta/v1/chat/completions.
    # NOTE: the model LIST is not a sibling of this path — see _models_suffix_for's override.
    "gemini":     "/v1beta/openai/chat/completions",
    "localai":    "/v1/chat/completions",
    "raw":        "",  # full URL in LLM_BASE_URL
}

# ══════════════════════════════════════════════════════════════════════════════════════
# ENDPOINT RESOLUTION — one join for the WHOLE family (chat · models · embeddings · health)
# ══════════════════════════════════════════════════════════════════════════════════════
#
# WHY: ``_BACKEND_PATHS`` assumes the tenant's base_url stops at the host (or at an
# unversioned prefix) and that WE supply the version segment. That assumption is wrong for
# a large slice of the real world, and it was wrong in FOUR places at once — chat here,
# ``get_embedding_url``, ``get_health_check_url``, and ``saas/llm_config.list_remote_models``
# — each hardcoding its own ``/v1/…``. Fixing only chat leaves a tenant with a working brain
# whose model picker 404s and whose health probe lies. So the join lives HERE, once, and
# every site calls it.
#
# THE THREE SHAPES A REAL TENANT PASTES (all three must work; all three were observed):
#   1. version in the BASE      https://api.z.ai/api/paas/v4          → …/v4/chat/completions
#   2. no version at all        https://…/compatible-mode/            → …/compatible-mode/v1/chat/completions
#   3. the FULL endpoint        https://api.z.ai/api/paas/v4/chat/completions → unchanged
# Shape 1 produced ``…/v4/v1/chat/completions`` (404). Fighting that, the natural next move
# is to paste the full endpoint — which produced ``…/chat/completions/v1/chat/completions``
# (404 again). Shape 3 is therefore not a hypothetical: it is where a blocked user lands.
#
# HOW (structural, never a provider list — this repo forbids ``if "z.ai" in url``):
#   a) ``_api_root`` strips a trailing OPERATION tail (``…/chat/completions``, ``…/messages``)
#      from whatever the tenant pasted, yielding the provider's API root. Shape 3 collapses
#      onto shape 1, and every family member then derives from the SAME root — which is the
#      whole point: models/embeddings/health can no longer disagree with chat.
#   b) ``_join_path`` appends the canonical suffix, skipping any LEADING run of suffix
#      segments the root already carries. Segment equality is exact, EXCEPT that two version
#      segments are interchangeable (``v4`` satisfies the suffix's ``v1``) — the base's
#      version wins, because it is what the tenant read off the provider's own docs.
#
# WHAT THE OVERLAP RULE DELIBERATELY DOES NOT DO: it never reaches into the MIDDLE of a
# suffix. Groq's ``/openai/v1/chat/completions`` starts with ``openai``, not a version, so a
# base of ``https://api.groq.com/v1`` overlaps nothing and the legacy (doubled) URL is
# preserved verbatim — we do not silently reshape a suffix whose leading segment we did not
# put there. A base of ``https://api.groq.com/openai/v1`` DOES overlap two segments and
# resolves cleanly. Both are pinned.
#
# The version grammar is ``v`` + digits + an optional standard pre-release qualifier: ``v1``,
# ``v4``, and the real in-the-wild ``v1beta`` / ``v1alpha`` (Google generativelanguage),
# ``v1beta1`` (Kubernetes-style). It deliberately does NOT match ordinary words that merely
# start with v+digits (``v2ray``), and it is only ever matched against PATH segments, so a
# host like ``v4.example.com`` is never mistaken for a versioned base.
_VERSION_SEGMENT_RE = re.compile(r"^v\d+(?:alpha|beta|rc)?\d*$", re.IGNORECASE)

# Operation tails a tenant may have pasted INSTEAD of a base URL (shape 3). Longest first;
# exactly one is stripped. These are terminal OPERATION names — a base URL legitimately
# ending in one of them does not exist, which is what makes the strip safe. Note that
# ``/api/chat/completions`` (OpenWebUI) needs no entry of its own: stripping ``chat/completions``
# leaves ``…/api``, which the overlap rule then absorbs when the suffix is re-appended.
_ENDPOINT_TAILS: tuple[tuple[str, ...], ...] = (
    ("chat", "completions"),
    ("messages",),
    ("completions",),
    ("responses",),
    ("embeddings",),
)

# scheme://host[:port] — matched so the path can be manipulated as segments without
# ``urlsplit`` mis-reading a scheme-less ``host:port`` base (LM Studio / Ollama style) as a
# URL scheme. No match (no ``://``) → the whole string is treated as path, which round-trips
# such a base byte-for-byte.
_SCHEME_HOST_RE = re.compile(r"^(?P<head>[a-zA-Z][a-zA-Z0-9+.\-]*://[^/?#]*)(?P<rest>.*)$")


def _split_base(url: str) -> tuple[str, list[str], str]:
    """``url`` → (scheme+host, path segments, ``?query#fragment``).

    Query/fragment are carried through untouched (Azure-style ``?api-version=…`` bases) and
    re-attached AFTER the appended path, which plain string concatenation got wrong.
    """
    u = (url or "").strip()
    tail = ""
    cuts = [i for i in (u.find("?"), u.find("#")) if i != -1]
    if cuts:
        cut = min(cuts)
        u, tail = u[:cut], u[cut:]
    m = _SCHEME_HOST_RE.match(u)
    head, rest = (m.group("head"), m.group("rest")) if m else ("", u)
    return head, [s for s in rest.split("/") if s], tail


def _rebuild(head: str, segments: list[str], tail: str) -> str:
    path = "/".join(segments)
    if head:
        return head + ("/" + path if path else "") + tail
    return path + tail


def _segments_interchangeable(a: str, b: str) -> bool:
    """Exact match, OR both are API version segments (``v4`` satisfies a suffix's ``v1``)."""
    if a.lower() == b.lower():
        return True
    return bool(_VERSION_SEGMENT_RE.match(a) and _VERSION_SEGMENT_RE.match(b))


def _leading_overlap(base_segments: list[str], suffix_segments: list[str]) -> int:
    """Longest k such that the base's LAST k segments are the suffix's FIRST k segments."""
    for k in range(min(len(base_segments), len(suffix_segments)), 0, -1):
        if all(_segments_interchangeable(base_segments[len(base_segments) - k + i],
                                         suffix_segments[i])
               for i in range(k)):
            return k
    return 0


def _api_root(url: str) -> str:
    """Strip one trailing OPERATION tail so ``url`` becomes the provider's API root.

    ``https://api.z.ai/api/paas/v4/chat/completions`` → ``https://api.z.ai/api/paas/v4``.
    A URL that is already a root is returned unchanged.
    """
    if not url:
        return ""
    head, segments, tail = _split_base(url)
    lowered = [s.lower() for s in segments]
    for candidate in _ENDPOINT_TAILS:
        n = len(candidate)
        if n <= len(segments) and tuple(lowered[-n:]) == candidate:
            segments = segments[:-n]
            break
    return _rebuild(head, segments, tail)


def resolve_endpoint(base_url: str, suffix: str) -> str:
    """THE join. ``base_url`` (whatever the tenant pasted) + a canonical operation suffix.

    Every LLM URL this codebase builds goes through here — chat, model discovery, embeddings
    and the health probe — so a tenant whose chat endpoint resolves can never have a model
    picker or a health probe pointing somewhere else.

    An empty ``suffix`` means "the base IS the full URL" (backend_type ``raw``): returned
    verbatim, never rewritten.
    """
    if not base_url:
        return ""
    if not suffix:
        return base_url.rstrip("/")
    head, segments, tail = _split_base(_api_root(base_url))
    suffix_segments = [s for s in suffix.strip("/").split("/") if s]
    k = _leading_overlap(segments, suffix_segments)
    if k:
        log.debug("llm.endpoint.overlap_absorbed",
                  base_url=base_url, suffix=suffix, absorbed=suffix_segments[:k])
    return _rebuild(head, segments + suffix_segments[k:], tail)


def resolve_chat_endpoint(backend_type: str, base_url: str) -> str:
    """The chat-completions URL for ``backend_type`` given the tenant's ``base_url``."""
    return resolve_endpoint(
        base_url, _BACKEND_PATHS.get(backend_type, "/v1/chat/completions"))


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


# ── Programmatic "does this model reason?" detection ─────────────────────────────
# A model that spends reasoning (thinking/CoT) tokens when left unsuppressed. This is the
# programmatic half of the task: instead of relying solely on a hand-maintained host map,
# we classify the MODEL id so that (a) a new reasoning model on a KNOWN host can still get
# the host's reasoning_default suppression, and (b) a reasoning model on an UNKNOWN host
# (self-hosted / new provider) is flagged honestly and budget-protected rather than left to
# reason away unseen. Matched as lowercased substrings; deliberately EXCLUDES off-by-default
# families (mistral-*, plain qwen, claude-*-4-5-and-earlier, gemini-1.5/2.0-non-thinking,
# gpt-4/3.5, llama-*, sonar/sonar-pro, deepseek-chat) so a non-reasoning model never gets a
# field that could 400. Sources: each provider's current reasoning docs (recorded 2026-08-17).
_REASONING_MODEL_MARKERS: tuple[str, ...] = (
    "gpt-5", "o1", "o3", "o4",                 # OpenAI reasoning families (GPT-5 / o-series)
    "deepseek-reasoner", "deepseek-r1", "deepseek-v4", "deepseek-v3.1",  # v3.1 = hybrid reasoning
    "qwq",                                     # Qwen QwQ (thinking-only)
    "grok-",                                   # xAI Grok
    "gemini-2.5", "gemini-3", "gemini-2.0-flash-thinking", "gemini-thinking",
    "claude-fable", "claude-mythos", "claude-opus-5", "claude-sonnet-5",
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
    "claude-sonnet-4-6", "claude-sonnet-4-7",  # adaptive-thinking Claude (4.6+ / 5)
    "kimi-k2", "kimi-latest",                  # Moonshot Kimi reasoning
    "glm-4.5", "glm-4.6", "glm-4.7", "glm-5",  # z.ai GLM (hybrid / thinking-on); 4.5 added 2026-11-01 — glm-4.5-air measured reasoning 114-296 tok/call un-suppressed (prod key bench), docs.z.ai thinking-mode lists 4.5 as hybrid two-mode
    "sonar-reasoning", "sonar-deep-research",  # Perplexity reasoning
    "gpt-oss",                                 # OpenAI GPT-OSS (effort-adjustable, no off)
    "reasoning", "reasoner", "thinking", "magistral",  # generic catch-all
)


def _model_reasons(model: Optional[str]) -> bool:
    """Return True if the model id plausibly belongs to a reasoning (thinking) family.

    Best-effort and PURE (no I/O). Used only as a fallback signal — an explicit entry in a
    host's ``models`` list always wins. A False here for a real reasoning model is not a
    correctness hole on a KNOWN host (its ``reasoning_default``/``models`` still apply when
    matched); on an UNKNOWN host a False here means "send nothing, assume off" — the same
    fail-safe the old code used, now documented rather than accidental.
    """
    m = (model or "").lower()
    return any(marker in m for marker in _REASONING_MODEL_MARKERS)


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
# https://inference-docs.cerebras.ai/capabilities/reasoning · Z.AI
# https://docs.z.ai/guides/capabilities/thinking-mode.md (fetched 2026-08-19)
#
# Each entry: ``models`` = ordered (substring, spec) pairs, first match wins; ``default`` =
# fallback for that host. A spec is {"fields": {...merged into payload...},
# "can_disable": bool}. An EMPTY fields dict means "nothing to send" (already off by default).
# Optional ``surfaces`` = ordered (url-path prefix, sub-entry) pairs — for providers that
# serve differently-behaving surfaces under one host (z.ai: the Coding-Plan path cannot
# fully disable thinking; the standard path can).
_REASONING_CONTROLS: dict[str, dict] = {
    # Every entry carries "source" (the provider's OWN current reasoning docs, recorded on
    # "recorded") — the repo's data-table doctrine: a published fact that goes stale needs a
    # URL and a date a human can re-check. "models" = ordered (substring, spec) pairs, first
    # match wins. "reasoning_default" = the spec for a DETECTED reasoning model (via
    # _model_reasons) NOT explicitly listed — this closes the "a new reasoning model falls to
    # the non-reasoning default and reasons away unseen" gap. "default" = non-reasoning models.
    # spec = {"fields": {...}, "can_disable": bool}; empty fields = "nothing to send".
    # DeepSeek — LIVE-VERIFIED (control run): v4 defaults thinking ON. deepseek-reasoner/r1 are
    # thinking-only. deepseek-chat is non-thinking (field harmless, kept explicit).
    "api.deepseek.com": {
        "source": "https://api-docs.deepseek.com/guides/thinking_mode/",
        "recorded": "2026-08-17",
        "models": [
            ("deepseek-reasoner", {"fields": {}, "can_disable": False}),
            ("deepseek-r1", {"fields": {}, "can_disable": False}),
        ],
        "reasoning_default": {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True},
        "default": {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True},
    },
    # NVIDIA NIM — Nemotron 3 reasons by DEFAULT and the off switch is a chat-template kwarg,
    # not a top-level field: NVIDIA's own reference says "To disable reasoning, add
    # enable_thinking=False", and the model cards show it on the wire as
    # {"chat_template_kwargs": {"enable_thinking": false}}. Added because an UNKNOWN host sends
    # NO suppression field at all — so a tenant who bound NIM was billed for chain-of-thought on
    # every extraction call, and got the "cannot suppress reasoning on this endpoint" banner
    # rather than a working switch.
    #
    # `default` deliberately sends NOTHING. NIM fronts many non-Nemotron models (Llama, Mistral,
    # Qwen) whose templates do not declare `enable_thinking`, and an unknown kwarg is the exact
    # shape that 400s a whole account — the same trap recorded for OpenAI's reasoning_effort on
    # gpt-4o. Only a model matched below, or one _model_reasons detects, gets the field.
    "integrate.api.nvidia.com": {
        "source": "https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-super-120b-a12b",
        "recorded": "2026-08-19",
        "models": [
            ("nemotron", {"fields": {"chat_template_kwargs": {"enable_thinking": False}},
                          "can_disable": True}),
            ("qwq", {"fields": {"chat_template_kwargs": {"enable_thinking": False}},
                     "can_disable": True}),
            ("deepseek-r1", {"fields": {}, "can_disable": False}),
        ],
        "reasoning_default": {"fields": {"chat_template_kwargs": {"enable_thinking": False}},
                              "can_disable": True},
        "default": {"fields": {}, "can_disable": True},
    },
    # OpenAI — reasoning_effort REJECTED (400) on non-reasoning models (gpt-4o). "none" is
    # documented on GPT-5.x; the o-series (o1/o3/o4) predates it → lowest is "low".
    "api.openai.com": {
        "source": "https://developers.openai.com/api/docs/guides/reasoning",
        "recorded": "2026-08-17",
        "models": [
            ("gpt-5", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
            ("o1", {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
            ("o3", {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
            ("o4", {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
        ],
        "reasoning_default": {"fields": {"reasoning_effort": "none"}, "can_disable": True},
        "default": {"fields": {}, "can_disable": True},
    },
    # Anthropic — thinking OPT-IN on 4.5-and-earlier (omit = off); adaptive-thinking models
    # (opus-4.6+/sonnet-4.6+/5) default ON. Fable/Mythos REJECT the disable.
    "api.anthropic.com": {
        "source": "https://platform.claude.com/docs/en/build-with-claude/thinking",
        "recorded": "2026-08-17",
        "models": [
            ("claude-fable", {"fields": {}, "can_disable": False}),
            ("claude-mythos", {"fields": {}, "can_disable": False}),
            ("claude-opus-5", {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True}),
            ("claude-sonnet-5", {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True}),
            ("claude-opus-4-7", {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True}),
            ("claude-opus-4-8", {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True}),
        ],
        "reasoning_default": {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True},
        "default": {"fields": {}, "can_disable": True},   # 4.5 and earlier: off unless asked
    },
    # Google Gemini — 2.5 uses thinkingBudget (0 = off); 2.5 Pro has a 128-token floor; 3
    # replaced budget with thinkingLevel ("low"|"high" only — no off value).
    # 🔴 THESE FIELDS ARE THE **OpenAI-COMPATIBLE** ONES, AND THAT DISTINCTION IS THE WHOLE
    # ENTRY. Google runs TWO APIs. Its NATIVE generateContent surface takes `thinkingConfig`
    # (thinkingBudget / thinkingLevel) nested in generationConfig — which is what the docs
    # linked below describe, and what this entry used to send. But we call Google on its
    # OPENAI-COMPATIBLE surface (_BACKEND_PATHS["gemini"] → /v1beta/openai/chat/completions),
    # and that surface validates the payload strictly against the OpenAI schema and REJECTS
    # any key it does not know. Sending a native field there does not degrade — it 400s the
    # whole call, and the tenant sees "probe FAILED" on a correctly-configured endpoint.
    #
    # Measured against the live API rather than reasoned about, with a deliberately invalid
    # key so the body is judged before auth is:
    #   {model, messages}                          -> 400 "Please pass a valid API key"   (body OK)
    #   {model, messages, thinkingConfig:{…}}      -> 400 "Unknown name \"thinkingConfig\":
    #                                                      Cannot find field."             (REJECTED)
    #   {model, messages, reasoning_effort:"none"} -> 400 "Please pass a valid API key"   (body OK)
    # Reaching the auth check IS the pass condition: the payload got past schema validation.
    #
    # `reasoning_effort` is chosen over the also-accepted extra_body.google.thinking_config
    # because it is the SAME field every other provider in this table uses, so the shape stays
    # uniform and a reader does not have to know which of Google's two APIs we are on to
    # predict what gets sent. If we ever add a NATIVE Gemini backend, it needs its OWN entry
    # keyed by that backend — not a second dialect smuggled into this one.
    "generativelanguage.googleapis.com": {
        "source": "https://ai.google.dev/gemini-api/docs/openai (compat surface); thinking "
                  "semantics: https://ai.google.dev/gemini-api/docs/generate-content/thinking",
        "recorded": "2026-08-17",
        "models": [
            ("gemini-2.5-flash",
             {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
            ("gemini-2.5-pro",
             {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
            ("gemini-3",
             {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
        ],
        "reasoning_default": {"fields": {"reasoning_effort": "low"}, "can_disable": False},
        "default": {"fields": {}, "can_disable": True},
    },
    # Groq — "none" (disable reasoning) is ONLY supported by Qwen 3.6; GPT-OSS only low/med/high.
    "api.groq.com": {
        "source": "https://console.groq.com/docs/reasoning",
        "recorded": "2026-08-17",
        "models": [
            ("qwen3.6", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
            ("gpt-oss", {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
        ],
        # "none" is ONLY Qwen 3.6; "low/medium/high" is ONLY GPT-OSS. Any OTHER reasoning
        # model on Groq has NO known off value → send nothing, flag honestly + inflate budget.
        "reasoning_default": {"fields": {}, "can_disable": False},
        "default": {"fields": {}, "can_disable": True},
    },
    # Alibaba DashScope / Qwen — enable_thinking (top-level raw REST). thinking-only ids
    # (qwq / *-thinking) cannot be turned off.
    "dashscope-intl.aliyuncs.com": {
        "source": "https://www.alibabacloud.com/help/en/model-studio/deep-thinking",
        "recorded": "2026-08-17",
        "models": [
            ("thinking", {"fields": {}, "can_disable": False}),
            ("qwq", {"fields": {}, "can_disable": False}),
            ("qwen", {"fields": {"enable_thinking": False}, "can_disable": True}),
        ],
        "reasoning_default": {"fields": {"enable_thinking": False}, "can_disable": True},
        "default": {"fields": {}, "can_disable": True},
    },
    "dashscope.aliyuncs.com": {
        "source": "https://www.alibabacloud.com/help/en/model-studio/deep-thinking",
        "recorded": "2026-08-17",
        "models": [
            ("thinking", {"fields": {}, "can_disable": False}),
            ("qwq", {"fields": {}, "can_disable": False}),
            ("qwen", {"fields": {"enable_thinking": False}, "can_disable": True}),
        ],
        "reasoning_default": {"fields": {"enable_thinking": False}, "can_disable": True},
        "default": {"fields": {}, "can_disable": True},
    },
    # Mistral — reasoning_effort; "none" = off (off by default on the non-magistral models).
    "api.mistral.ai": {
        "source": "https://docs.mistral.ai/capabilities/reasoning/",
        "recorded": "2026-08-17",
        "models": [
            ("mistral-small", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
            ("mistral-medium", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
        ],
        "reasoning_default": {"fields": {"reasoning_effort": "none"}, "can_disable": True},
        "default": {"fields": {}, "can_disable": True},
    },
    # xAI Grok — "Reasoning cannot be disabled"; reasoning_effort defaults "high", "low" is the
    # floor on grok-4.5/4.6. Footgun: on grok-4.20 the SAME field means agent count / is not
    # accepted — send NOTHING for the whole 4.20 family.
    "api.x.ai": {
        "source": "https://docs.x.ai/developers/model-capabilities/text/reasoning",
        "recorded": "2026-08-17",
        "models": [
            ("grok-4.20", {"fields": {}, "can_disable": False}),
        ],
        "reasoning_default": {"fields": {"reasoning_effort": "low"}, "can_disable": False},
        "default": {"fields": {}, "can_disable": False},
    },
    # OpenRouter — unified reasoning object {effort,max_tokens,exclude}; effort "none" is the
    # best-effort off (degrades per underlying provider).
    "openrouter.ai": {
        "source": "https://openrouter.ai/docs/api_reference/parameters",
        "recorded": "2026-08-17",
        "models": [],
        "reasoning_default": {"fields": {"reasoning": {"effort": "none"}}, "can_disable": True},
        "default": {"fields": {"reasoning": {"effort": "none"}}, "can_disable": True},
    },
    # Cerebras — reasoning_effort "none" = disabled on zai-glm/gemma; gpt-oss only low/med/high
    # (LIVE-VERIFIED: "none" 400s on gpt-oss). disable_reasoning deprecated (2026-07-21).
    "api.cerebras.ai": {
        "source": "https://inference-docs.cerebras.ai/capabilities/reasoning",
        "recorded": "2026-08-17",
        "models": [
            ("zai-glm", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
            ("gpt-oss", {"fields": {"reasoning_effort": "low"}, "can_disable": False}),
            ("gemma", {"fields": {"reasoning_effort": "none"}, "can_disable": True}),
        ],
        "reasoning_default": {"fields": {"reasoning_effort": "low"}, "can_disable": False},
        "default": {"fields": {}, "can_disable": True},
    },
    # z.ai (Zhipu GLM) — thinking:{"type":"disabled"} disables thinking (GLM-4.6/4.7/5 hybrid or
    # thinking-on). THE live BYO case that previously reasoned away unseen.
    "api.z.ai": {
        "source": "https://docs.z.ai/guides/capabilities/thinking-mode (thinking defaults per "
                  "surface); coding surface: https://zcode.z.ai/en/docs/configuration",
        "recorded": "2026-08-18",
        # z.ai SERVES TWO SURFACES ON ONE HOST, and until 2026-08-18 this entry matched both.
        # The standard surface is /api/paas/v4; the CODING PLAN surface is /api/coding/paas/v4,
        # and zcode's own configuration guide is explicit that the coding URL "must be the
        # Coding-only endpoint ... not the general endpoint".
        #
        # The FIELD NAME IS NOT THE BUG, which is what makes this different from the Gemini
        # incident. z.ai's thinking-mode doc lists GLM-5.2 among the models that accept
        # thinking:{"type":"disabled"}. What differs is the DEFAULT: that doc states Preserved
        # Thinking is ENABLED by default on the Coding Plan endpoint and DISABLED on the
        # standard one. Measured on live pre-prod (glm-5.2 on /api/coding/paas/v4, with
        # thinking:{"type":"disabled"} in the payload): reasoning_tokens came back 19, 32 and 93
        # across EXTRACT / ENRICHMENT / CLASSIFY_CHAIN and raised suppression_ineffective. The
        # field is therefore ACCEPTED AND IGNORED on this surface.
        #
        # WHY NOT PROBE IT with a deliberately invalid key, the technique that settled Gemini:
        # IT DOES NOT DISCRIMINATE ON THIS HOST. Measured — a payload carrying a deliberately
        # bogus key (nonexistent_field_xyz) returns the SAME 401 "token expired or incorrect"
        # as a clean body. z.ai authenticates BEFORE it schema-validates, so "reached the auth
        # error" proves nothing here and would have yielded a confident false positive. Only
        # the bogus-field CONTROL case revealed that.
        #
        # THE GENERAL RULE, because this nearly cost us a false green: A PROBE TECHNIQUE IS
        # ONLY VALID ONCE A CONTROL HAS PROVEN THE PROBE CAN FAIL. Never port a technique from
        # one provider to another without re-establishing that first — send a deliberately
        # nonsensical payload and confirm it is REJECTED. If the control passes too, the probe
        # is measuring nothing, and eight identical "successes" are eight pieces of no evidence.
        #
        # So this surface is recorded HONESTLY as cannot-disable rather than handed a guessed
        # replacement field. can_disable=False sets inflate_budget, which pads the completion
        # budget so a fact-dense turn still returns complete atoms — the actual truncation risk
        # the CRIT warns about — and stops us claiming a suppression we cannot deliver.
        # reasoning_effort is the plausible candidate (zcode documents it as the OpenAI-surface
        # knob) but GLM is documented as accepting only max/high/low with NO "none", so it is
        # not an off switch and is not sent on a guess. Confirming any candidate needs a real
        # KEYED call: send it, then verify reasoning_tokens == 0 — acceptance is not evidence.
        # CONFIRMED (2026-08-19, the keyed call above): live keyed A/B on pre-prod glm-5.2
        # /api/coding/paas/v4 (n=6/variant): effort=low → reasoning median 8 tok, 6/6
        # success, 1.8s median; no-field → 145 tok median, 2/6 timeouts; thinking-disabled
        # on this path → 19-93 tok (partially honored). reasoning_effort=low is therefore
        # sent on the coding surface — with can_disable=False kept honest (8 tok is a floor,
        # not zero).
        "surfaces": [
            ("/api/coding/", {
                "models": [
                    # Forced-thinking ids are forced on this surface too.
                    ("glm-5.3", {"fields": {"reasoning_effort": "minimal"}, "can_disable": False}),
                    ("glm-5.3-flash", {"fields": {"reasoning_effort": "minimal"}, "can_disable": False}),
                    ("glm-4.7", {"fields": {}, "can_disable": False}),
                    ("glm-4.5v", {"fields": {}, "can_disable": False}),
                    # CORRECTED 2026-11-01 (prod-key keyed bench, n=5/variant): glm-4.5-air on
                    # THIS surface honors thinking:{"type":"disabled"} — reasoning_tokens 0/0/0/0/0,
                    # median 4.81s, 5/5 valid JSON — contra the 2026-08-18 glm-5.2 reading below.
                    # The Aug measurement stands for glm-5.2 (partial honor, 19-93 tok); the
                    # disable works for the 4.5 hybrid family. The generic reasoning_default
                    # (effort=low) still applies to models not listed here.
                    ("glm-4.5-air", {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True}),
                ],
                # Every GLM reasoning model on the coding surface: thinking is preserved by
                # default and our disable is ignored (measured) — but the keyed A/B above
                # confirms reasoning_effort=low caps it at the floor. Send effort=low, keep
                # can_disable=False honest, inflate the budget.
                "reasoning_default": {"fields": {"reasoning_effort": "low"}, "can_disable": False},
                "default": {"fields": {}, "can_disable": True},
            }),
        ],
        "models": [
            # GLM-5.3: forced thinking. thinking.type "disabled" ERRORS ("GLM-5.3 no longer
            # supports disabling thinking"). CORRECTED 2026-11-01 against docs.z.ai
            # chat-completion reference: for the GLM-5.3 family, reasoning_effort 'none' or
            # 'minimal' SKIPS thinking, while 'low' and 'medium' are MAPPED TO HIGH — the old
            # effort:"low" here was therefore handing z.ai near-max reasoning on every call.
            # GLM-5.3-FLASH must be listed BEFORE glm-5.3: model matching is substring-based,
            # so 'glm-5.3' would otherwise capture the flash id too.
            ("glm-5.3-flash", {"fields": {"reasoning_effort": "minimal"}, "can_disable": False}),
            ("glm-5.3", {"fields": {"thinking": {"type": "enabled"}, "reasoning_effort": "minimal"},
                         "can_disable": False}),
            # GLM-5.3-FLASH: forced thinking too (docs.z.ai thinking-mode, 2026-11-01:
            # "GLM-5.3 and GLM-5.3-FLASH use forced thinking and cannot be disabled").
            # GLM-4.7 / GLM-4.5V: forced thinking (cannot disable); reasoning_effort is 5.2+ only.
            ("glm-4.7", {"fields": {}, "can_disable": False}),
            ("glm-4.5v", {"fields": {}, "can_disable": False}),
        ],
        # GLM-5.2/5.1/5/5-Turbo/5V-Turbo/4.6/4.5 auto-decide → thinking.type "disabled" works.
        "reasoning_default": {"fields": {"thinking": {"type": "disabled"}}, "can_disable": True},
        "default": {"fields": {}, "can_disable": True},
    },
    # Together — three behavioural classes: hybrid (reasoning:{"enabled":false}), adjustable
    # (reasoning_effort low/med/high), always-reason (untoggleable).
    "api.together.xyz": {
        "source": "https://docs.together.ai/docs/inference/chat/reasoning",
        "recorded": "2026-08-17",
        "models": [
            ("deepseek-r1", {"fields": {}, "can_disable": False}),   # always-reason
            ("qwq", {"fields": {}, "can_disable": False}),           # always-reason
        ],
        "reasoning_default": {"fields": {"reasoning": {"enabled": False}}, "can_disable": True},
        "default": {"fields": {}, "can_disable": True},
    },
    # Fireworks — reasoning_effort "none" disables reasoning entirely (documented off switch).
    "api.fireworks.ai": {
        "source": "https://docs.fireworks.ai/guides/reasoning",
        "recorded": "2026-08-17",
        "models": [],
        "reasoning_default": {"fields": {"reasoning_effort": "none"}, "can_disable": True},
        "default": {"fields": {}, "can_disable": True},
    },
    # Perplexity — sonar-reasoning emits a <think> section that CANNOT be suppressed (docs +
    # community-confirmed). sonar/sonar-pro are non-reasoning.
    "api.perplexity.ai": {
        "source": "https://docs.perplexity.ai/docs/sonar/models/sonar-reasoning-pro",
        "recorded": "2026-08-17",
        "models": [],
        "reasoning_default": {"fields": {}, "can_disable": False},
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


# ── LEARNED REJECTIONS — the half of "validate reasoning OFF" that was missing ──────
#
# The table above is a RECORDED GUESS about each provider's field names. The system already
# validates one failure mode: suppression that is ACCEPTED BUT IGNORED shows up as reasoning
# tokens in the response and raises the suppression_ineffective CRIT. It did not handle the
# other one: suppression the endpoint REJECTS OUTRIGHT. Google's OpenAI-compatible surface
# 400s on any key outside the OpenAI schema, so one wrong field name in the table did not
# degrade the call — it killed it, and the tenant saw "probe FAILED" on an endpoint that was
# configured perfectly.
#
# A hand-maintained table WILL be wrong again: providers rename fields, and a provider we have
# never seen has no entry at all. So the resolver learns. When a call comes back rejecting a
# field we injected, that (host, field) is remembered and never sent to that host again, and
# the next call self-heals instead of failing forever. Process-local and deliberately not
# persisted: it is a cache of an observation, it must re-verify after a restart, and a provider
# that later ADDS support gets picked up on its own.
#
# This makes the table a starting hypothesis rather than a hard dependency, which is what
# "any endpoint is probed and validated for reasoning off" actually requires.
_REJECTED_REASONING_FIELDS: dict[str, set] = {}


def note_reasoning_field_rejected(base_url: Optional[str], error_text: str,
                                  sent_fields: Optional[dict] = None) -> list:
    """Record any reasoning field this host rejected. Returns the field names learned.

    Matches the field NAME against the error body rather than trying to parse each provider's
    error schema — the one thing every provider's "unknown field" message reliably contains is
    the offending name, and a false positive here costs only a suppression field we stop
    sending to one host. Never raises: this runs inside an exception handler.
    """
    learned = []
    try:
        if not error_text:
            return learned
        from urllib.parse import urlparse
        host = (urlparse(str(base_url or "")).hostname or "").lower()
        if not host:
            return learned
        blob = str(error_text)
        low = blob.lower()
        # Only treat it as a schema rejection — a 401/429/timeout body must never poison
        # the cache and silently stop us suppressing reasoning on a healthy provider.
        if not any(m in low for m in (
                "unknown name", "unknown field", "cannot find field", "unrecognized",
                "unexpected keyword", "not permitted", "invalid_request_error",
                "extra fields not permitted", "additionalproperties")):
            return learned
        candidates = set(sent_fields or {})
        if not candidates:
            for spec in _REASONING_CONTROLS.values():
                for _m, s in (spec.get("models") or []):
                    candidates.update((s.get("fields") or {}).keys())
                for k in ("reasoning_default", "default"):
                    candidates.update(((spec.get(k) or {}).get("fields") or {}).keys())
        # Word-boundary, not substring: a bare `in` learns "reasoning" from an error naming
        # "reasoning_effort", which is a different field and a different provider's control.
        # Harmless here (the cache is per host) but it would misreport WHICH field was refused,
        # and that log line is how the recorded table gets corrected.
        import re as _re
        for name in candidates:
            if name and _re.search(r"(?<![\w.-])%s(?![\w-])" % _re.escape(name), blob):
                _REJECTED_REASONING_FIELDS.setdefault(host, set()).add(name)
                learned.append(name)
        if learned:
            log.warning("llm_reasoning.field_rejected", host=host, fields=sorted(learned),
                        reason="endpoint refused this reasoning-control field; it will not be "
                               "sent to this host again this process. The recorded table entry "
                               "for this host is wrong or the provider renamed the field.")
    except Exception:
        return learned
    return learned


def resolve_reasoning_controls(base_url: Optional[str], model: Optional[str]) -> dict:
    """Resolve how to disable reasoning for an endpoint host + model.

    Returns keys:
      * fields          - suppression fields safe to merge into the payload (may be {}).
      * can_disable     - True when reasoning can be fully turned off for this model.
      * known           - True when the endpoint host is a recognized provider.
      * source          - the matched host (or "unknown").
      * reasoning_model - programmatic _model_reasons(model) verdict.
      * reason          - human-readable reason when can_disable is False / unknown.
      * inflate_budget  - True when the completion budget must be padded so a long
                          fact-dense turn still returns complete atoms (cannot-disable
                          reasoning models spend reasoning tokens INSIDE the completion
                          budget - measured on Cerebras gpt-oss-120b).

    Fail-safe: any parse problem -> unknown, no fields, can_disable=True.
    """
    host = ""
    path = ""
    try:
        from urllib.parse import urlparse
        if base_url:
            _u = urlparse(str(base_url))
            host = (_u.hostname or "").lower()
            # NORMALISE before matching. A stored base_url is tenant-typed text: a missing
            # trailing slash or a doubled slash from a copy-paste must not silently select a
            # DIFFERENT provider surface. Collapse repeated slashes and guarantee exactly one
            # trailing slash, so "/api/coding", "//api/coding/paas/v4" and
            # "/api/coding/paas/v4" all match the same prefix.
            path = re.sub(r"/{2,}", "/", (_u.path or "").lower()).rstrip("/") + "/"
    except Exception:
        host = ""
        path = ""

    reasoning_model = _model_reasons(model)

    def _spec(spec: dict, known: bool, reason: str = "") -> dict:
        can_disable = bool(spec.get("can_disable", True))
        # Drop anything this host has already refused (see _REJECTED_REASONING_FIELDS). If
        # every field we had is gone, we can no longer claim reasoning is disabled — say so
        # honestly via can_disable rather than reporting success we cannot deliver, so the
        # completion budget still gets padded and the leak audit does not raise a spurious
        # suppression_ineffective CRIT for a field the endpoint never accepted.
        _fields = dict(spec.get("fields") or {})
        _refused = _REJECTED_REASONING_FIELDS.get(host) or set()
        if _refused:
            _kept = {k: v for k, v in _fields.items() if k not in _refused}
            if _fields and not _kept:
                can_disable = False
                reason = (reason or "") + (
                    " · every recorded reasoning-control field was refused by this endpoint; "
                    "reasoning cannot be disabled here until a working field is known")
            _fields = _kept
        return {
            "fields": _fields,
            "can_disable": can_disable,
            "known": known,
            "source": host or "unknown",
            "reasoning_model": reasoning_model,
            "reason": reason,
            # A model that cannot disable reasoning IS a reasoning model by definition
            # (non-reasoning models always have can_disable=True) — pad the budget whenever
            # reasoning is unavoidable, regardless of whether _model_reasons caught the id.
            "inflate_budget": (not can_disable),
        }

    entry = None
    for known_host, e in _REASONING_CONTROLS.items():
        if host == known_host or host.endswith("." + known_host):
            entry = e
            break

    if entry is None:
        if reasoning_model:
            return _spec({"fields": {}, "can_disable": False}, False,
                         reason="unrecognized provider + reasoning model detected - cannot "
                                "safely guess a suppression field; flag honestly and keep "
                                "capture intact")
        return _spec({"fields": {}, "can_disable": True}, False)

    # SURFACE OVERRIDE. A host can expose two INCOMPATIBLE APIs on different paths, and the
    # host key alone cannot tell them apart — the Gemini entry says a second surface needs its
    # OWN entry, and this is that mechanism for the case where the host is identical.
    for _prefix, _sub in (entry.get("surfaces") or []):
        if path.startswith(_prefix):
            entry = _sub
            break

    # Surface split: one host may serve differently-behaving paths (z.ai coding plan).
    # A matched surface's sub-entry REPLACED the host entry above, so its own
    # models/reasoning_default apply here; forced-thinking ids (glm-5.3) stay honored
    # because each surface lists them with its own spec.
    m = (model or "").lower()
    for model_key, model_spec in entry.get("models", []):
        if model_key in m:
            return _spec(model_spec, True)
    if reasoning_model and "reasoning_default" in entry:
        return _spec(entry["reasoning_default"], True,
                     reason="reasoning model not explicitly listed; host reasoning_default")
    return _spec(entry.get("default", {"fields": {}, "can_disable": True}), True)
# ── MODEL CLASS for the FETCH MODELS cost-color legend ───────────────────────────────
# The consoles' FETCH MODELS flow colors each returned model so a NEW user can see, before
# spending a cent, which brains are cheap to run. The classes are DERIVED from
# resolve_reasoning_controls — the same per-host table that actually drives the payload — so
# the legend can never promise a control the request path does not deliver:
#   GREEN  - reasoning can be fully turned off (or the model never reasons at all).
#   ORANGE - reasoning cannot be turned OFF, but the table records a REDUCIBLE effort floor
#            (the resolved control sends reasoning_effort="low" — gemini-3-pro, groq/xai
#            gpt-oss floors, z.ai coding-surface reasoning_default).
#   RED    - a reasoning model whose reasoning can be neither disabled nor reduced
#            (glm-5.3 minimal-effort is NOT a reduction the table trusts; qwq/deepseek-r1/
#            glm-4.7/glm-4.5v/sonar-reasoning send nothing).
#   GREY   - HONEST THIRD STATE: unrecognized host + a model that does not LOOK like a
#            reasoning family. Calls work; the reasoning cost is simply UNVERIFIED. Unknown
#            is not RED — lumping it there would warn every self-hosted vLLM/LM Studio user
#            for no measured reason.
# A recognized host + a detected reasoning model with NO control stays RED even if the host
# itself is unknown to the tenant — reasoning is known, only the host is not.
def classify_model_class(base_url: Optional[str], model: Optional[str]) -> str:
    """Return "green" | "orange" | "red" | "grey" for one (base_url, model) pair.

    PURE: no I/O, no network, no cache mutation of its own beyond what
    resolve_reasoning_controls already reads. NEVER raises per model — the FETCH MODELS
    list is rendered one id at a time and a classification fault must degrade that id to
    "grey", not break the whole list.
    """
    try:
        controls = resolve_reasoning_controls(base_url, model)
        if not controls.get("known"):
            # Unrecognized host: only a DETECTED reasoning family upgrades grey to red —
            # we know the model reasons and recorded no safe control; anything else is
            # honestly unverified.
            return "red" if controls.get("reasoning_model") else "grey"
        if controls.get("can_disable"):
            return "green"
        fields = controls.get("fields") or {}
        for field, value in fields.items():
            if field == "reasoning_effort" and str(value).strip().lower() == "low":
                return "orange"
        return "red"
    except Exception:
        return "grey"


def _reasoning_base_url() -> Optional[str]:
    """The base_url this call will hit — tenant BYO brain first, else the global env.

    SAAS_MODE with no routable tenant brain → None (the global env brain does not exist in
    SaaS; see ``saas_global_brain_blocked``). No network call is made here either way.
    """
    cfg = _tenant_brain()
    if cfg and cfg.get("base_url"):
        return str(cfg["base_url"])
    if _saas_mode():
        return None
    return os.environ.get("LLM_BASE_URL") or None


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


def _tenant_brain() -> Optional[dict]:
    """Return the active per-tenant BYO engine-brain config, or None (→ global env path).

    Lazy + fail-safe: the SaaS layer may be absent (public core) or SAAS_MODE off, in which
    case this returns None and every resolver below behaves EXACTLY as before (byte-for-byte).
    This is the ONE read the whole per-tenant LLM routing hangs on.
    """
    try:
        from saas.llm_context import current_llm_config
        return current_llm_config()
    except Exception:
        return None


# ── SaaS GLOBAL-BRAIN FIREWALL (Gap A) ────────────────────────────────────────
# OWNER RULING (binding): under SAAS_MODE the process-global env brain "shouldn't be used at
# all". An LLM call rides the TENANT'S OWN configured brain (public.seat_llm_config /
# account_llm_config) or it does not run.
#
# The fail-closed check already existed at the three centralized entry points in
# ``llm_calls`` (``_saas_brain_unavailable``) — but the RESOLVERS below still handed out the
# env endpoint/key to anyone who asked, so every hand-rolled or future call site had to
# REMEMBER to guard. That is the "guard covers 4 of 5 call sites" shape this project has
# shipped before. These helpers move the invariant INTO the resolvers: in SaaS mode, with no
# routable tenant brain, there is simply no endpoint and no key to hand out.
#
# SAAS_MODE off → every helper here is inert and the env path is byte-for-byte unchanged.


def _saas_mode() -> bool:
    """SAAS_MODE gate — pure env read, imports NOTHING.

    Mirrors ``saas.config._truthy`` / ``main._saas_mode_enabled`` deliberately: a public
    (open-core) deployment has no ``saas/`` package at all, so this must never import it.
    """
    return str(os.environ.get("SAAS_MODE") or "").strip().lower() not in (
        "", "false", "0", "no", "off"
    )


def saas_global_brain_blocked() -> bool:
    """True iff SAAS_MODE is on and NO ROUTABLE tenant brain resolves for this call.

    "Routable" means a bound config carrying a ``base_url``. That covers all three ways the
    tenant brain can be absent, which the older sentinel-only check did not:
      * ``None``        — the ContextVar/schema was never bound or was lost across a task
                          boundary (the divergence bug, see llm_calls._saas_brain_unavailable);
      * ``no_brain``    — a real account with no brain configured (the cost-safe sentinel);
      * a truthy config with no ``base_url`` — belt-and-braces; ``saas.llm_config._build_config``
        already refuses to emit one, and this makes that refusal non-load-bearing here.

    Fail direction: SAAS off / SaaS layer absent / any error → False, i.e. never blocks the
    open-core env path. A wrong-brain call bills us and silently voids the BYO promise, so in
    SaaS the safe answer is NO CALL.
    """
    if not _saas_mode():
        return False
    cfg = _tenant_brain()
    return not (cfg and cfg.get("base_url"))


# Throttle for the fail-closed CRIT. The failure mode is CORRELATED (one unconfigured account
# generates one refusal per LLM op per turn), and this codebase has already been burned by a
# single warning emitting ~200k log lines and destroying its own evidence. One line per
# (site, account) per minute is loud enough to alert on and cheap enough to keep.
_GLOBAL_BRAIN_CRIT_INTERVAL_S = 60.0
_global_brain_crit_last: dict[str, float] = {}


def _crit_global_brain_refused(site: str) -> None:
    """Fail LOUD (throttled) that a SaaS call was refused the process-global env brain."""
    cfg = _tenant_brain()
    account_id = (cfg or {}).get("account_id") or "(unresolved)"
    key = f"{site}:{account_id}"
    now = time.monotonic()
    last = _global_brain_crit_last.get(key)
    if last is not None and (now - last) < _GLOBAL_BRAIN_CRIT_INTERVAL_S:
        return
    _global_brain_crit_last[key] = now
    try:
        from src.api.logging_config import log_crit
        log_crit(
            log,
            "saas.global_brain_refused",
            site=site,
            account_id=account_id,
            reason=("no brain bound for this request/tenant" if cfg is None
                    else "account has no LLM brain configured"),
            remediation="bind a BYO brain (seat_llm_config / account_llm_config) for this account",
        )
    except Exception:
        log.critical("saas.global_brain_refused", site=site, account_id=account_id)


def get_backend_type() -> str:
    """Return the active backend type. Per-tenant override wins in SaaS mode, else env.

    Falls back to LLM_BACKEND_TYPE (default 'openwebui') when no tenant override is bound.
    """
    cfg = _tenant_brain()
    if cfg and cfg.get("backend_type"):
        return str(cfg["backend_type"]).lower().strip()
    return os.environ.get("LLM_BACKEND_TYPE", "openwebui").lower().strip()


def get_backend_endpoint() -> str | None:
    """
    Build the full LLM endpoint URL from the active backend type + base URL.

    Per-tenant override wins in SaaS mode (the tenant's own brain endpoint); otherwise
    LLM_BACKEND_TYPE + LLM_BASE_URL. Returns None if no base URL is available
    (caller falls through to legacy get_endpoint_list() chain).

    ⚠️ SAAS_MODE: there is NO env fallback. A tenant without a routable brain gets None here
    and ``get_endpoint_list()`` returns EMPTY — the call cannot go out on the operator's
    endpoint. See ``saas_global_brain_blocked``.
    """
    backend_type = get_backend_type()
    cfg = _tenant_brain()
    if cfg and cfg.get("base_url"):
        base_url = str(cfg["base_url"]).rstrip("/")
    elif _saas_mode():
        _crit_global_brain_refused("get_backend_endpoint")
        return None
    else:
        base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    if not base_url:
        return None
    # NOT ``base_url + _BACKEND_PATHS[...]``: that double-appended a version onto a base that
    # already carried one, and re-appended a whole endpoint onto a base that WAS one. See
    # ``resolve_endpoint`` — the same join the models/embeddings/health URLs use.
    return resolve_chat_endpoint(backend_type, base_url)


def get_backend_response_format() -> str:
    """Return response format key for the active backend. 'openai' or 'anthropic'."""
    return _BACKEND_RESPONSE_FORMAT.get(get_backend_type(), "openai")


def _thinking_kwarg_backends() -> set[str]:
    """Backend types that accept the vLLM/OpenWebUI ``chat_template_kwargs`` field.

    Default: ``{"openwebui"}`` — the pre-prod/prod path (qwen behind vLLM/llama.cpp),
    byte-for-byte unchanged. Strict OpenAI-compatible providers (openai, cerebras, groq,
    anthropic, …) are EXCLUDED because they HTTP-400 on unknown top-level fields; they rely
    on the post-hoc ``_strip_think_tags`` net instead. A self-hoster whose gateway does
    accept it can extend the set via ``LLM_CHAT_TEMPLATE_KWARGS_BACKENDS`` (comma-separated).
    Read live from env so a tenant/test toggle needs no import reload.
    """
    raw = os.environ.get("LLM_CHAT_TEMPLATE_KWARGS_BACKENDS", "openwebui")
    return {b.strip().lower() for b in raw.split(",") if b.strip()}


def get_llm_headers() -> dict:
    """
    Return HTTP headers for LLM authentication.

    Backend-type-aware: Anthropic uses x-api-key + anthropic-version headers;
    all other backends use standard Bearer token.
    Single source of truth for all LLM endpoint auth across all modules.

    ⚠️ SAAS_MODE: the global ``LLM_API_KEY`` is NEVER handed out. With no routable tenant
    brain this returns ``{}`` — no credential, and (paired with ``get_backend_endpoint``
    returning None) no endpoint either, so endpoint and key can never DIVERGE onto the env
    pair. Divergence was the 401-retry-storm bug pinned by
    ``tests/test_saas_brain_divergence_failclosed.py``; this closes it at the resolver.
    """
    backend = get_backend_type()
    # Per-tenant override: the tenant's decrypted BYO key + optional extra headers win in
    # SaaS mode; otherwise the global LLM_API_KEY. extra_headers let a tenant add e.g. an
    # org id or a gateway header. (extra_headers is applied for every backend.)
    cfg = _tenant_brain()
    if saas_global_brain_blocked():
        _crit_global_brain_refused("get_llm_headers")
        return {}
    tenant_key = (cfg.get("api_key") if cfg else None)
    tenant_extra = (cfg.get("extra_headers") if cfg else None) or {}
    api_key = (tenant_key if cfg is not None else os.environ.get("LLM_API_KEY", "").strip()) or ""
    api_key = str(api_key).strip()

    if backend == "anthropic":
        headers: dict = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if api_key:
            headers["x-api-key"] = api_key
        if tenant_extra:
            headers.update({str(k): str(v) for k, v in tenant_extra.items()})
        return headers

    # All other backends: standard Bearer token
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if tenant_extra:
        headers.update({str(k): str(v) for k, v in tenant_extra.items()})
    return headers


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
    # chat_template_kwargs is the Qwen3.5 / vLLM / OpenWebUI extension mechanism.
    #
    # ⚠️ CORRECTED: the old code injected it for EVERY non-anthropic backend on the
    # assumption "unknown fields are silently ignored." That assumption is FALSE for
    # strict OpenAI-compatible providers — Cerebras returns HTTP 400
    # ("chat_template_kwargs ... is unsupported", code=wrong_api_format), and OpenAI /
    # Groq / most hosted gateways reject unknown top-level fields the same way. A
    # tenant BYO brain (backend_type="openai") pointed at any such provider then 400s
    # on EVERY engine call. So inject ONLY for the backend families known to accept it
    # (OpenWebUI/vLLM by default; env-extensible for self-hosters whose gateway takes
    # it). Every other backend relies on the universal post-hoc _strip_think_tags net
    # (and the model's own thinking-off default) — no unsupported field on the wire.
    # anthropic never gets it (dedicated `thinking` field, handled below).
    if backend in _thinking_kwarg_backends():
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    # Self-hosted Ollama: documented top-level "think" switch (ollama.com/blog/thinking).
    # LAN host → not host-matchable, so key on backend_type. setdefault: explicit caller wins.
    if backend == "ollama":
        payload.setdefault("think", False)

    # ── Reasoning suppression, HOST-resolved (the hosted-provider half) ──────────
    # The block above only covers SELF-HOSTED vLLM/OpenWebUI, whose LAN host we cannot
    # recognize. Hosted providers each need their OWN documented field — and the old
    # code's fallback ("every other backend relies on _strip_think_tags and the model's
    # own thinking-off default") is precisely the bug: stripping tags is COSMETIC (the
    # tokens and latency are already spent), and the "default off" assumption is false
    # for DeepSeek v4, OpenAI o-series/GPT-5, Gemini, Grok and others.
    #
    # Merged, never overwritten: an explicit caller-supplied `thinking` (handled above)
    # wins, so a deliberate opt-in is not clobbered by the blanket suppression.
    if _reasoning_suppression_enabled():
        try:
            controls = resolve_reasoning_controls(_reasoning_base_url(), model)
            for key, value in (controls.get("fields") or {}).items():
                payload.setdefault(key, value)
            if not controls.get("can_disable"):
                # Honest signal: this model CANNOT have reasoning fully disabled (documented
                # can_disable=False, OR an unrecognized provider hosting a reasoning model). We
                # sent the lowest effort we safely can and must not pretend it is off. The
                # completion budget is padded upstream (call_llm_with_retry_*) so a long
                # fact-dense turn still returns complete atoms instead of coming back empty.
                log.warning("llm_reasoning.cannot_disable", host=controls.get("source"),
                            model=model,
                            known=controls.get("known"),
                            reasoning_model=controls.get("reasoning_model"),
                            inflate_budget=controls.get("inflate_budget"),
                            reason=controls.get("reason") or
                                   "provider does not support disabling reasoning; lowest "
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

    ⚠️ SAAS_MODE short-circuits ALL of 1-4. In SaaS the only legitimate endpoint is the
    tenant's own brain; the legacy chain and the hardcoded fallbacks are operator-global and
    are exactly what the CPU-cloud constraint forbids. No routable tenant brain → EMPTY list.

    Returns:
        Ordered list of complete endpoint URLs, deduplicated, ready to POST to.
        Empty in SaaS mode when no tenant brain is bound (fail-closed, no spend).
    """
    endpoints = []

    # Priority 0: typed backend (LLM_BACKEND_TYPE + LLM_BASE_URL) — always wins
    typed = get_backend_endpoint()
    if typed:
        endpoints.append(typed)
        # Return immediately — typed backend is authoritative; no legacy fallthrough
        log.debug("llm_endpoints.resolved", count=1, first=typed)
        return endpoints

    # SaaS: no tenant brain resolved above ⇒ there is NO endpoint. Never the legacy env
    # chain, never the hardcoded fallbacks. (get_backend_endpoint already CRIT'd.)
    if _saas_mode():
        log.debug("llm_endpoints.resolved", count=0, first=None, saas_failclosed=True)
        return []

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
        str — complete endpoint URL ready to POST to, or "" in SaaS mode with no tenant
        brain bound (callers MUST treat empty as "do not call" — the hardcoded default
        below is an OPEN-CORE convenience and must never be handed to a SaaS tenant).
    """
    endpoints = get_endpoint_list()
    if not endpoints:
        if _saas_mode():
            # Fail-closed: returning the hardcoded open-webui default here would hand a SaaS
            # caller a global endpoint by the back door. (get_backend_endpoint already CRIT'd.)
            return ""
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

    ⚠️ SAAS_MODE: the CHAT brain is never borrowed for embeddings. The base is taken from the
    tenant's own config; with no routable tenant brain this returns "" (the caller then has
    only the LOCAL fastembed path, which is where SaaS embeddings are documented to live —
    ``TODO(byo-embedding)``). ``EMBEDDING_API_URL`` is DELIBERATELY still honoured: it is a
    dedicated, explicitly-configured embedding host (typically a local nomic), not the
    process-global chat brain the owner's ruling is about.

    Args:
        chat_url: The active chat completions endpoint URL.

    Returns:
        str — complete embedding endpoint URL, or "" if not applicable.
    """
    explicit = os.environ.get("EMBEDDING_API_URL", "").strip()
    if explicit:
        return explicit

    backend = get_backend_type()
    if _saas_mode():
        cfg = _tenant_brain() or {}
        tenant_emb = str(cfg.get("embedding_url") or "").strip().rstrip("/")
        if tenant_emb:
            return tenant_emb
        base = str(cfg.get("base_url") or "").strip().rstrip("/")
        if not base:
            return ""
    else:
        base = os.environ.get("LLM_BASE_URL", "").rstrip("/")

    if backend == "anthropic":
        # Anthropic has no embedding endpoint — fastembed fallback handles this
        return ""

    # ONE join, shared with chat/models/health (``resolve_endpoint``). It was
    # ``f"{base}/v1/embeddings"``, which double-appended a version onto a versioned base and
    # appended a whole second endpoint onto a base that already WAS one (backend_type=raw:
    # ``…/v4/chat/completions/v1/embeddings``).
    suffix = "/api/embeddings" if backend == "openwebui" else "/v1/embeddings"
    if base:
        return resolve_endpoint(base, suffix)

    # Legacy derivation when no base URL is configured: the chat URL IS the base we have, so
    # take its API root and append the same suffix. Byte-identical to the old substring
    # replacement for every shape the old code could handle, and correct for the ones it
    # could not.
    return resolve_endpoint(chat_url, suffix) if chat_url else ""


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
        str — URL to GET for health check (200/401/404 = up, exception = down), or "" when
        there is no endpoint to probe (SaaS with no tenant brain — probing the operator's
        global endpoint there would report health for a brain no tenant call will ever use).
    """
    backend = get_backend_type()

    if not chat_url and _saas_mode():
        return ""

    if backend == "anthropic":
        base = (_reasoning_base_url() or "").rstrip("/")
        return resolve_endpoint(base or chat_url, "/v1/models")

    if "/api/chat/completions" in chat_url:
        return chat_url.replace("/api/chat/completions", "/api/version")

    # WAS a substring replace of ``/v1/chat/completions``. With a versioned base that
    # substring does not occur, so the "derived" health URL came back as the CHAT url
    # UNCHANGED — and since this probe treats anything short of an exception as "up", the
    # health check silently degraded into a reachability check that proved nothing about the
    # brain. Deriving from the API root cannot degrade that way; if it somehow did, say so.
    health_url = resolve_endpoint(chat_url, "/v1/models")
    if health_url and health_url == chat_url:
        log.warning("llm.health_check.not_derivable",
                    chat_url=chat_url, backend_type=backend,
                    consequence="probe would GET the chat endpoint — reachability only")
    return health_url


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

    # ── SaaS: report the TENANT lane, never the env lane ──────────────────────
    # This block exists because the old env-only report actively MISLED an investigation:
    # prod (SAAS_MODE=1) self-reported `endpoint_source: "fallback"` + the operator's
    # WGM_LLM_MODEL, which reads as "we are about to call a dead global host". Under SaaS
    # those env values are NOT what any tenant call uses — they are unreachable by
    # construction now. Report what is TRUE for this process.
    if _saas_mode():
        cfg = _tenant_brain()
        if cfg and cfg.get("base_url"):
            return {
                "backend_type": get_backend_type(),
                "chat_endpoint": chat_url,
                "embedding_endpoint": embed_url or "(none — fastembed fallback)",
                "health_check_url": health_url or "(none)",
                "model": cfg.get("model") or "(unset)",
                "pattern_extraction_model": cfg.get("model") or "(unset)",
                "auth_type": "bearer" if cfg.get("api_key") else "none",
                "api_key_set": bool(cfg.get("api_key")),
                "endpoint_source": "saas_tenant_brain",
                "saas_mode": True,
            }
        return {
            "backend_type": get_backend_type(),
            "chat_endpoint": "(none — SaaS fail-closed)",
            "embedding_endpoint": embed_url or "(none — fastembed fallback)",
            "health_check_url": "(none)",
            "model": "(none — SaaS fail-closed)",
            "pattern_extraction_model": "(none — SaaS fail-closed)",
            "auth_type": "none",
            "api_key_set": False,
            # NOT "fallback": there is no fallback in SaaS. This value means "no tenant brain
            # is bound in THIS context" — for the unauthenticated /health probe that is the
            # NORMAL answer (no request tenant), not an incident. Per-account brain coverage
            # is an operator-console question, not a /health one.
            "endpoint_source": "saas_no_tenant_brain_bound",
            "saas_mode": True,
        }

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

    # CALL-TIME egress pin: when a tenant BYO brain is bound (SaaS), the request rides the
    # resolve→vet→pinned, redirect-refusing guarded client from ``saas/llm_config.py`` — the
    # same seam ``llm_calls._post_llm_sync`` uses — so this door cannot reach an internal
    # address a rebinding DNS answer points at. No tenant brain → the legacy plain client.
    # CONNECT/READ SPLIT (BUG-brain-connect-timeout-burns-full-budget-20261026): this door
    # builds its OWN client, so it applies the same short-connect normalization the
    # llm_calls seams do — a SYN-blackholed brain must not burn the read budget at dial.
    from src.api.llm_calls import split_connect_timeout
    timeout = split_connect_timeout(timeout)
    _guarded = None
    if _saas_mode() and (_tenant_brain() or {}).get("base_url"):
        from saas.llm_config import guarded_client
        _guarded = guarded_client(timeout=timeout)
    with (_guarded or httpx.Client(timeout=timeout)) as client:
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
