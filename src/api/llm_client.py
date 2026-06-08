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
    # Priority chain: user_id → FAULTLINE_MEMORY_CHAIN_UUID → dynamic timestamp fallback.
    if backend == "openwebui":
        chat_id = user_id or os.environ.get("FAULTLINE_MEMORY_CHAIN_UUID")
        if not chat_id:
            chat_id = f"local:faultline-{int(time.time() * 1000)}"
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
    # across ALL backends unconditionally.
    #
    # Qwen3.5 ≤9B should default to thinking-off, but serving frameworks
    # (LM Studio, llama.cpp) have known bugs where the default is ignored.
    # Explicit suppression is required.  See BUGS/qwen3-thinking-mode/.
    #
    # chat_template_kwargs is the Qwen3.5 / vLLM / OpenAI-compat mechanism.
    # Unknown fields are silently ignored by backends that don't support them.
    if backend not in ("anthropic", "openwebui"):
        payload["chat_template_kwargs"] = {"enable_thinking": False}

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
        model, category_model, auth_type, api_key_set, endpoint_source,
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
        "model": os.environ.get("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
        "category_model": os.environ.get("CATEGORY_LLM_MODEL", "qwen2.5-coder"),
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
