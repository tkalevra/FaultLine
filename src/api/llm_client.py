"""Centralized LLM endpoint authentication, configuration, and request building."""

import os
from typing import Optional, Any


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
    **kwargs
) -> dict:
    """
    Build LLM request payload with centralized dBug-016 chat_id injection.

    dBug-016: OpenWebUI requires chat_id in request to prevent NoneType crash
    in process_chat middleware. Injecting user_id as chat_id avoids the upstream
    OpenWebUI bug when calling /api/chat/completions from FaultLine modules.

    dprompt-120: Omit stream parameter (workaround for LM Studio bug #599).
    LM Studio 0.4.13 appears to ignore explicit stream=false, streaming with
    default behavior. Omitting the parameter entirely allows non-streaming by default.

    Args:
        messages: List of message dicts with role/content
        model: Model name string
        user_id: User UUID to inject as chat_id (prevents dBug-016 crash)
        temperature: LLM temperature (default 0.0 for deterministic)
        max_tokens: Max output tokens
        thinking: Thinking config dict (e.g. {"type": "disabled"})
        **kwargs: Additional fields to merge into payload

    Returns:
        Complete payload dict ready for httpx.post(json=payload)
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
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

    # Merge any additional fields
    payload.update(kwargs)

    return payload
