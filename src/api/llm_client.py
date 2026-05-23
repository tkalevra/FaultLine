"""Centralized LLM endpoint authentication, configuration, and request building."""

import os
import httpx
import structlog
from typing import Optional, Any

log = structlog.get_logger(__name__)


def get_llm_headers() -> dict:
    """
    Return HTTP headers for LLM authentication.

    Reads LLM_API_KEY from environment and builds Authorization header if present.
    Single source of truth for all LLM endpoint auth across all modules.
    """
    llm_api_key = os.environ.get("LLM_API_KEY", "")
    headers = {}
    if llm_api_key:
        headers["Authorization"] = f"Bearer {llm_api_key}"
    return headers


def build_llm_payload(
    messages: list[dict],
    model: str,
    user_id: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 200,
    thinking: Optional[dict] = None,
    stream: bool = False,
    **${LOCATION}args
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
        **${LOCATION}args: Additional fields to merge into payload

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

    # dBug-016 fix: inject chat_id to prevent OpenWebUI process_chat NoneType crash
    # Use user_id if available, fallback to environment variable for cases where
    # user context isn't available (e.g., taxonomy inference, retraction detection)
    chat_id = user_id or os.environ.get("FAULTLINE_MEMORY_CHAIN_UUID")
    if chat_id:
        payload["chat_id"] = chat_id

    # Optional thinking config
    if thinking:
        payload["thinking"] = thinking

    # Merge any additional fields, but NEVER allow ${LOCATION}args to override stream
    # (stream=false is CRITICAL and must not be overridden by callers)
    ${LOCATION}args.pop("stream", None)
    payload.update(${LOCATION}args)

    return payload


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
