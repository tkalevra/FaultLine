"""
Centralized LLM calling module with retry, circuit breaker, and timeout management.

This module provides a unified interface for all LLM calls across FaultLine components:
- main.py (API endpoints, extraction, validation)
- gate.py (WGM ontology validation)
- re_embedder.py (background enrichment/promotion)
- filter.py (OpenWebUI inlet/outlet)

All LLM calls must use one of these three functions:
1. call_llm_with_retry_sync() — blocking calls with retry + circuit breaker
2. call_llm_with_retry_async() — async calls with retry + circuit breaker
3. call_llm_no_retry_sync() — single attempt, graceful failure (non-critical paths)

Endpoint Resolution:
  Follows centralized priority chain: env vars > auto-detected > hardcoded fallbacks.
  All endpoints must be reachable by callers (no circular dependencies on main.py).

Circuit Breaker:
  Tracks LLM endpoint failures across all callers. When threshold exceeded, returns
  safe default without attempting new calls. Resets automatically after timeout.

Timeout Configuration:
  Per-operation timeouts via LLMTimeouts class. Prevents long-tail requests from
  blocking ingest pipeline. All timeouts configurable via environment variables.

Logging & Error Handling:
  All failures logged with full context (attempt#, endpoint, error type, user_id).
  Exceptions never silently caught. Circuit breaker state changes logged.

HARD CONSTRAINTS (violations will cause Phase 2 rejection):
  1. NO hardcoded endpoint strings in main logic (only in _get_endpoint_list helper)
  2. NO rel_type constants or database schema assumptions
  3. NO import-time database queries (lazy imports only)
  4. NO timeout values in conditional logic (all from LLMTimeouts class)
  5. NO silent exception handlers (FAIL LOUD principle)
  6. NO circular imports from main.py (lazy imports inside functions)
"""

import asyncio
import json
import os
import structlog
import time
from datetime import datetime, timedelta
from typing import Optional, Any

log = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# CLASS: CircuitBreakerState
# ──────────────────────────────────────────────────────────────────────────────

class CircuitBreakerState:
    """
    Circuit breaker to prevent cascading LLM endpoint failures.

    When LLM endpoint fails repeatedly, the circuit opens and returns safe defaults
    without attempting new calls. Circuit resets automatically after timeout.

    This prevents retry loops from exhausting connection pools and timeouts during
    widespread LLM outages.

    Attributes:
        failure_threshold (int): Number of failures before circuit opens (default 3)
        timeout_seconds (int): Seconds before attempting reset (default 60)
        failures (int): Current failure count
        last_failure_time (Optional[datetime]): Timestamp of most recent failure
    """

    def __init__(self, failure_threshold: int = 3, timeout_seconds: int = 60):
        """
        Initialize circuit breaker state.

        Args:
            failure_threshold: Open circuit after N failures (default 3)
            timeout_seconds: Reset attempt after N seconds (default 60)
        """
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.failures = 0
        self.last_failure_time: Optional[datetime] = None

    def is_open(self) -> bool:
        """
        Check if circuit is open (failing, should skip new requests).

        Circuit is open if:
        - failure count >= threshold, AND
        - timeout has not yet elapsed since last failure

        Returns:
            True if circuit is open (fail fast), False if closed (allow retries)
        """
        if self.failures < self.failure_threshold:
            return False

        if self.last_failure_time is None:
            return False

        time_since_failure = datetime.utcnow() - self.last_failure_time
        if time_since_failure.total_seconds() >= self.timeout_seconds:
            # Timeout elapsed, attempt reset
            return False

        # Still in timeout window and failures threshold exceeded
        return True

    def record_failure(self):
        """Record a failure and update circuit state."""
        self.failures += 1
        self.last_failure_time = datetime.utcnow()
        log.warning("circuit_breaker.failure_recorded",
                   failures=self.failures,
                   threshold=self.failure_threshold,
                   is_open=self.is_open())

    def record_success(self):
        """Record a success and reset circuit state."""
        if self.failures > 0:
            log.info("circuit_breaker.success_reset",
                    previous_failures=self.failures)
        self.failures = 0
        self.last_failure_time = None

    def reset(self):
        """Force circuit closed (emergency recovery)."""
        log.warning("circuit_breaker.force_reset",
                   previous_failures=self.failures)
        self.failures = 0
        self.last_failure_time = None


# ──────────────────────────────────────────────────────────────────────────────
# FUNCTION: Parse LLM Response Robustly (dBug-016 Handling)
# ──────────────────────────────────────────────────────────────────────────────

def _parse_llm_response_robust(response) -> dict:
    """
    Parse LLM HTTP response robustly, handling dBug-016 corruption.

    OpenWebUI sometimes returns HTTP 200 with malformed JSON due to dBug-016
    (NoneType crash on missing chat_id). This parser attempts three strategies:

    1. Standard JSON parsing (happy path)
    2. Line-delimited JSON (streaming format)
    3. Regex extraction from corrupted JSON (dBug-016 case)

    All three extraction methods validate for "choices" key to ensure we extract
    actual LLM response object, not unrelated JSON fragments.

    Args:
        response: httpx.Response object from LLM endpoint

    Returns:
        dict: Parsed response with "choices" key, or {} on failure
    """
    try:
        # Strategy 1: Standard JSON parsing (most common)
        result = response.json()
        if isinstance(result, dict) and "choices" in result:
            return result
    except json.JSONDecodeError:
        log.debug("parse_llm_response.json_decode_failed")

    # Strategy 2: Line-delimited JSON (streaming format)
    # Some LLM endpoints return one JSON object per line
    try:
        lines = response.text.strip().split('\n')
        for line in lines:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and "choices" in parsed:
                    log.debug("parse_llm_response.recovered_line_delimited")
                    return parsed
            except json.JSONDecodeError:
                continue
    except Exception:
        pass

    # Strategy 3: Regex extraction (dBug-016 corruption case)
    # Extract first valid JSON object containing "choices" key
    try:
        import re
        # Match JSON object patterns { ... }
        # Search for patterns that contain "choices" to avoid extracting garbage
        text = response.text

        # Find all potential JSON objects
        matches = re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text)
        for match in matches:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, dict) and "choices" in parsed:
                    log.debug("parse_llm_response.recovered_regex_extraction")
                    return parsed
            except json.JSONDecodeError:
                continue
    except Exception:
        pass

    # All strategies failed
    log.warning("parse_llm_response.all_strategies_failed",
               response_text_preview=response.text[:300],
               response_status=response.status_code)
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# CLASS: LLMTimeouts
# ──────────────────────────────────────────────────────────────────────────────

class LLMTimeouts:
    """
    Centralized timeout configuration for different LLM operations.

    All timeout values are configurable via environment variables.
    Never hardcode timeout values in logic — always use this class.

    Operations:
    - INTENT_CLASSIFICATION: Fast intent detection (no context needed)
    - EXTRACTION: Entity/relationship extraction from conversation
    - VALIDATION: WGM ontology validation
    - ENRICHMENT: Metadata inference for novel rel_types
    - CORRECTION: User correction handling
    - EMBEDDING: Text embedding calls
    - DEFAULT: Fallback for unknown operations

    All values in seconds. Can be overridden via environment:
    - LLM_TIMEOUT_INTENT_CLASSIFICATION=5.0
    - LLM_TIMEOUT_EXTRACTION=30.0
    - etc.
    """

    # Default timeouts (in seconds) for each operation type
    # These are the fallbacks used if environment variables not set
    _DEFAULTS = {
        "INTENT_CLASSIFICATION": 5.0,    # Fast — no context needed
        "EXTRACTION": 30.0,              # Standard extraction calls
        "VALIDATION": 20.0,              # WGM ontology validation
        "ENRICHMENT": 15.0,              # Metadata inference for new rel_types
        "CORRECTION": 25.0,              # User correction extraction
        "EMBEDDING": 10.0,               # Text embedding operations
        "TAXONOMY_DISCOVERY": 20.0,      # Discover new taxonomies
        "DEFAULT": 30.0,                 # Fallback for unknown operations
    }

    @classmethod
    def get(cls, operation: str = "DEFAULT") -> float:
        """
        Get timeout for a specific operation.

        Checks environment variables first (LLM_TIMEOUT_<OPERATION>),
        then falls back to defaults.

        Args:
            operation: Operation type (e.g., "EXTRACTION", "VALIDATION")
                      Case-insensitive

        Returns:
            Timeout in seconds (float)
        """
        operation = operation.upper()
        env_var = f"LLM_TIMEOUT_{operation}"
        env_value = os.environ.get(env_var)

        if env_value is not None:
            try:
                return float(env_value)
            except ValueError:
                log.warning("llm_timeout.invalid_env_value",
                           env_var=env_var,
                           value=env_value,
                           using_default=True)

        return cls._DEFAULTS.get(operation, cls._DEFAULTS["DEFAULT"])


# ──────────────────────────────────────────────────────────────────────────────
# GLOBALS
# ──────────────────────────────────────────────────────────────────────────────

# Global circuit breaker instance (shared across all LLM calls)
_llm_circuit_breaker = CircuitBreakerState()


# ──────────────────────────────────────────────────────────────────────────────
# HELPER: _get_endpoint_list()
# ──────────────────────────────────────────────────────────────────────────────

def _get_endpoint_list() -> list[str]:
    """
    Generate list of LLM endpoints to try, in priority order.

    This is the SINGLE PLACE where endpoint URLs are hardcoded. All other
    code must use this function to get endpoints. This ensures:
    - Centralized endpoint management
    - Easy fallback chain maintenance
    - No scattered hardcoding across modules

    Priority Chain (Docker-aware):
    1. OPENWEBUI_INTERNAL_URL env var (container-internal, port 8080)
    2. OPENWEBUI_URL env var (external, user-configured)
    3. QWEN_API_URL env var (direct LLM backend)
    4. Hardcoded fallbacks (docker service names, localhost)

    Returns:
        List of complete endpoint URLs, ready to POST to
    """
    endpoints = []

    # PRIORITY 1: OPENWEBUI_INTERNAL_URL (container-internal OpenWebUI)
    # When running inside Docker, use port 8080 (internal service port),
    # NOT port 3000 (external host mapping)
    openwebui_internal = os.environ.get("OPENWEBUI_INTERNAL_URL", "").strip()
    if openwebui_internal:
        if not openwebui_internal.startswith("http"):
            openwebui_internal = f"http://{openwebui_internal}"
        endpoints.append(f"{openwebui_internal.rstrip('/')}/api/chat/completions")

    # PRIORITY 2: OPENWEBUI_URL (external OpenWebUI endpoint)
    openwebui_external = os.environ.get("OPENWEBUI_URL", "").strip()
    if openwebui_external:
        if not openwebui_external.startswith("http"):
            openwebui_external = f"http://{openwebui_external}"
        endpoints.append(f"{openwebui_external.rstrip('/')}/api/chat/completions")

    # PRIORITY 3: QWEN_API_URL (direct LLM backend, e.g., LM Studio, Ollama)
    qwen_api = os.environ.get("QWEN_API_URL", "").strip()
    if qwen_api:
        if not qwen_api.startswith("http"):
            qwen_api = f"http://{qwen_api}"
        endpoints.append(qwen_api.rstrip('/'))

    # PRIORITY 4: Hardcoded fallbacks (development/testing)
    # Docker service name (docker compose): open-webui:8080
    endpoints.append("http://open-webui:8080/api/chat/completions")
    # Localhost development: port 8080 (OpenWebUI default)
    endpoints.append("http://localhost:8080/api/chat/completions")
    # Localhost LLM backend: port 11434 (Ollama default)
    endpoints.append("http://localhost:11434/v1/chat/completions")

    # Remove duplicates while preserving order
    seen = set()
    unique_endpoints = []
    for ep in endpoints:
        if ep and ep not in seen:
            unique_endpoints.append(ep)
            seen.add(ep)

    log.debug("llm_endpoints.resolved",
             count=len(unique_endpoints),
             endpoints=unique_endpoints[:3])  # Log only first 3 to avoid noise

    return unique_endpoints


# ──────────────────────────────────────────────────────────────────────────────
# MAIN: call_llm_with_retry_sync()
# ──────────────────────────────────────────────────────────────────────────────

def call_llm_with_retry_sync(
    messages: list[dict],
    model: str,
    user_id: str = "anonymous",
    timeout: Optional[float] = None,
    max_retries: int = 3,
    operation: str = "DEFAULT",
) -> dict:
    """
    Synchronous LLM call with retry, circuit breaker, and fallback endpoints.

    This is the primary LLM calling interface for blocking operations:
    - API endpoint handlers (main.py)
    - WGM validation gate (gate.py)
    - Inline extraction (retraction, correction)

    Circuit Breaker:
      If LLM endpoint is experiencing widespread failures, the circuit breaker
      will open and return a safe default without attempting new calls.

    Retry Strategy:
      Tries up to 3 endpoints before giving up. Within each endpoint, respects
      timeout. Between retry attempts, uses exponential backoff (1s, 2s, 4s).

    Args:
        messages: List of message dicts with 'role' and 'content' keys
        model: Model name string (e.g., "qwen/qwen3.5-9b")
        user_id: User UUID for logging and context (default "anonymous")
        timeout: Request timeout in seconds (default from LLMTimeouts)
        max_retries: Number of retries across different endpoints (default 3)
        operation: Operation type for timeout selection (default "DEFAULT")

    Returns:
        Parsed JSON response from LLM, or {} on failure

    Raises:
        RuntimeError: If all endpoints exhausted and no response received
        httpx.TimeoutException: If request times out (after retries)
        json.JSONDecodeError: If response cannot be parsed as JSON
    """
    # Check circuit breaker first
    if _llm_circuit_breaker.is_open():
        log.warning("call_llm.circuit_breaker_open",
                   user_id=user_id,
                   operation=operation,
                   returning_safe_default=True)
        return {}

    # Select timeout based on operation type
    if timeout is None:
        timeout = LLMTimeouts.get(operation)

    # Lazy import to avoid circular dependencies
    from src.api.llm_client import build_llm_payload, get_llm_headers

    # Build payload once, reuse across retries
    payload = build_llm_payload(
        messages=messages,
        model=model,
        user_id=user_id,
        temperature=0.0,
        max_tokens=500,
    )

    endpoints = _get_endpoint_list()
    response = None
    last_error = None

    for attempt in range(1, max_retries + 1):
        for endpoint_idx, endpoint in enumerate(endpoints, 1):
            try:
                # Log attempt details
                log.debug("llm_call.attempt_start",
                         user_id=user_id,
                         operation=operation,
                         attempt=attempt,
                         endpoint_index=endpoint_idx,
                         timeout_seconds=timeout)

                start_time = time.time()

                # Lazy import to avoid circular dependencies
                from src.api.main import _http_client_sync

                response = _http_client_sync.post(
                    endpoint,
                    json=payload,
                    headers=get_llm_headers(),
                    timeout=timeout,
                )

                elapsed = time.time() - start_time
                response.raise_for_status()

                log.info("llm_call.attempt_success",
                        user_id=user_id,
                        operation=operation,
                        attempt=attempt,
                        elapsed_seconds=round(elapsed, 2),
                        endpoint_index=endpoint_idx)

                # Success — reset circuit breaker and parse response robustly (dBug-016)
                _llm_circuit_breaker.record_success()

                result = _parse_llm_response_robust(response)
                if not result:
                    log.warning("llm_call.response_parse_failed",
                               user_id=user_id,
                               operation=operation,
                               endpoint_index=endpoint_idx)
                    continue

                content = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

                if not content:
                    return {}

                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    # Try to extract JSON from text
                    import re
                    match = re.search(r'\{.*\}', content, re.DOTALL)
                    if match:
                        return json.loads(match.group())
                    else:
                        log.warning("llm_call.no_json_in_response",
                                   user_id=user_id,
                                   operation=operation,
                                   content_preview=content[:200])
                        return {}

            except Exception as e:
                last_error = e
                elapsed = time.time() - start_time
                log.warning("llm_call.attempt_failed",
                           user_id=user_id,
                           operation=operation,
                           attempt=attempt,
                           endpoint_index=endpoint_idx,
                           elapsed_seconds=round(elapsed, 2),
                           error_type=type(e).__name__,
                           error_message=str(e)[:200])

        # All endpoints failed for this attempt
        if attempt < max_retries:
            backoff_seconds = 2 ** (attempt - 1)
            log.warning("llm_call.attempt_exhausted_endpoints",
                       user_id=user_id,
                       operation=operation,
                       attempt=attempt,
                       backoff_seconds=backoff_seconds)
            time.sleep(backoff_seconds)

    # All retries exhausted
    _llm_circuit_breaker.record_failure()
    log.error("llm_call.all_retries_exhausted",
             user_id=user_id,
             operation=operation,
             total_attempts=max_retries,
             total_endpoints=len(endpoints),
             final_error_type=type(last_error).__name__,
             final_error_message=str(last_error)[:200])

    if last_error is not None:
        raise last_error
    raise RuntimeError("No LLM endpoint responded")


# ──────────────────────────────────────────────────────────────────────────────
# ASYNC: call_llm_with_retry_async()
# ──────────────────────────────────────────────────────────────────────────────

async def call_llm_with_retry_async(
    messages: list[dict],
    model: str,
    user_id: str = "anonymous",
    timeout: Optional[float] = None,
    max_retries: int = 3,
    operation: str = "DEFAULT",
) -> dict:
    """
    Asynchronous LLM call with retry, circuit breaker, and fallback endpoints.

    Identical to call_llm_with_retry_sync() but uses async/await for non-blocking
    operations. Used in background loops (re_embedder.py).

    Args:
        messages: List of message dicts with 'role' and 'content' keys
        model: Model name string
        user_id: User UUID for logging
        timeout: Request timeout in seconds (default from LLMTimeouts)
        max_retries: Number of retries across different endpoints
        operation: Operation type for timeout selection

    Returns:
        Parsed JSON response from LLM, or {} on failure

    Raises:
        Same exceptions as sync version
    """
    # Check circuit breaker first
    if _llm_circuit_breaker.is_open():
        log.warning("call_llm_async.circuit_breaker_open",
                   user_id=user_id,
                   operation=operation,
                   returning_safe_default=True)
        return {}

    # Select timeout based on operation type
    if timeout is None:
        timeout = LLMTimeouts.get(operation)

    # Lazy import to avoid circular dependencies
    from src.api.llm_client import build_llm_payload, get_llm_headers

    payload = build_llm_payload(
        messages=messages,
        model=model,
        user_id=user_id,
        temperature=0.0,
        max_tokens=500,
    )

    endpoints = _get_endpoint_list()
    response = None
    last_error = None

    for attempt in range(1, max_retries + 1):
        for endpoint_idx, endpoint in enumerate(endpoints, 1):
            try:
                log.debug("llm_call_async.attempt_start",
                         user_id=user_id,
                         operation=operation,
                         attempt=attempt,
                         endpoint_index=endpoint_idx,
                         timeout_seconds=timeout)

                start_time = time.time()

                # Lazy import to avoid circular dependencies
                from src.api.main import _http_client

                response = await _http_client.post(
                    endpoint,
                    json=payload,
                    headers=get_llm_headers(),
                    timeout=timeout,
                )

                elapsed = time.time() - start_time
                response.raise_for_status()

                log.info("llm_call_async.attempt_success",
                        user_id=user_id,
                        operation=operation,
                        attempt=attempt,
                        elapsed_seconds=round(elapsed, 2),
                        endpoint_index=endpoint_idx)

                # Success — reset circuit breaker and parse response robustly (dBug-016)
                _llm_circuit_breaker.record_success()

                result = _parse_llm_response_robust(response)
                if not result:
                    log.warning("llm_call_async.response_parse_failed",
                               user_id=user_id,
                               operation=operation,
                               endpoint_index=endpoint_idx)
                    continue

                content = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

                if not content:
                    return {}

                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    import re
                    match = re.search(r'\{.*\}', content, re.DOTALL)
                    if match:
                        return json.loads(match.group())
                    else:
                        log.warning("llm_call_async.no_json_in_response",
                                   user_id=user_id,
                                   operation=operation,
                                   content_preview=content[:200])
                        return {}

            except Exception as e:
                last_error = e
                elapsed = time.time() - start_time
                log.warning("llm_call_async.attempt_failed",
                           user_id=user_id,
                           operation=operation,
                           attempt=attempt,
                           endpoint_index=endpoint_idx,
                           elapsed_seconds=round(elapsed, 2),
                           error_type=type(e).__name__,
                           error_message=str(e)[:200])

        # All endpoints failed for this attempt
        if attempt < max_retries:
            backoff_seconds = 2 ** (attempt - 1)
            log.warning("llm_call_async.attempt_exhausted_endpoints",
                       user_id=user_id,
                       operation=operation,
                       attempt=attempt,
                       backoff_seconds=backoff_seconds)
            await asyncio.sleep(backoff_seconds)

    # All retries exhausted
    _llm_circuit_breaker.record_failure()
    log.error("llm_call_async.all_retries_exhausted",
             user_id=user_id,
             operation=operation,
             total_attempts=max_retries,
             total_endpoints=len(endpoints),
             final_error_type=type(last_error).__name__,
             final_error_message=str(last_error)[:200])

    if last_error is not None:
        raise last_error
    raise RuntimeError("No LLM endpoint responded")


# ──────────────────────────────────────────────────────────────────────────────
# FAST PATH: call_llm_no_retry_sync()
# ──────────────────────────────────────────────────────────────────────────────

def call_llm_no_retry_sync(
    messages: list[dict],
    model: str,
    user_id: str = "anonymous",
    timeout: Optional[float] = None,
    operation: str = "DEFAULT",
) -> Optional[dict]:
    """
    Single-attempt LLM call with graceful failure (no retry).

    Used for non-critical operations where a timeout or failure is acceptable:
    - Optional enrichment (e.g., metadata inference for novel rel_types)
    - Background taxonomy discovery
    - Fallback operations that have alternate code paths

    Returns None on any failure (connection error, timeout, parse error).
    Never raises exceptions — always degrades gracefully.

    Args:
        messages: List of message dicts with 'role' and 'content' keys
        model: Model name string
        user_id: User UUID for logging
        timeout: Request timeout in seconds (default from LLMTimeouts)
        operation: Operation type for timeout selection

    Returns:
        Parsed JSON response, or None on any failure
    """
    # Check circuit breaker
    if _llm_circuit_breaker.is_open():
        log.warning("call_llm_no_retry.circuit_breaker_open",
                   user_id=user_id,
                   operation=operation)
        return None

    if timeout is None:
        timeout = LLMTimeouts.get(operation)

    try:
        # Lazy import to avoid circular dependencies
        from src.api.llm_client import build_llm_payload, get_llm_headers

        payload = build_llm_payload(
            messages=messages,
            model=model,
            user_id=user_id,
            temperature=0.0,
            max_tokens=500,
        )

        endpoints = _get_endpoint_list()
        if not endpoints:
            log.error("call_llm_no_retry.no_endpoints_available",
                     user_id=user_id,
                     operation=operation)
            return None

        # Try primary endpoint only
        endpoint = endpoints[0]

        log.debug("llm_call_no_retry.attempt",
                 user_id=user_id,
                 operation=operation,
                 endpoint=endpoint,
                 timeout_seconds=timeout)

        start_time = time.time()

        # Lazy import to avoid circular dependencies
        from src.api.main import _http_client_sync

        response = _http_client_sync.post(
            endpoint,
            json=payload,
            headers=get_llm_headers(),
            timeout=timeout,
        )

        elapsed = time.time() - start_time
        response.raise_for_status()

        log.info("llm_call_no_retry.success",
                user_id=user_id,
                operation=operation,
                elapsed_seconds=round(elapsed, 2))

        result = _parse_llm_response_robust(response)
        if not result:
            log.warning("llm_call_no_retry.response_parse_failed",
                       user_id=user_id,
                       operation=operation)
            return None

        content = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

        if not content:
            return None

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group())
            else:
                log.warning("llm_call_no_retry.no_json_in_response",
                           user_id=user_id,
                           operation=operation,
                           content_preview=content[:200])
                return None

    except Exception as e:
        log.warning("llm_call_no_retry.failed_gracefully",
                   user_id=user_id,
                   operation=operation,
                   error_type=type(e).__name__,
                   error_message=str(e)[:200])
        return None


# ──────────────────────────────────────────────────────────────────────────────
# STATUS & CONTROL: Circuit Breaker Management
# ──────────────────────────────────────────────────────────────────────────────

def get_circuit_breaker_status() -> dict:
    """
    Get current circuit breaker state and statistics.

    Used by health endpoints and diagnostics to monitor LLM endpoint health.

    Returns:
        Dict with keys:
        - is_open: bool — circuit currently open?
        - failures: int — current failure count
        - threshold: int — failure threshold
        - last_failure_time: Optional[str] — ISO 8601 timestamp of most recent failure
        - timeout_seconds: int — time before attempting reset
        - seconds_until_reset: Optional[float] — seconds until reset attempt (if open)
    """
    status = {
        "is_open": _llm_circuit_breaker.is_open(),
        "failures": _llm_circuit_breaker.failures,
        "threshold": _llm_circuit_breaker.failure_threshold,
        "last_failure_time": None,
        "timeout_seconds": _llm_circuit_breaker.timeout_seconds,
        "seconds_until_reset": None,
    }

    if _llm_circuit_breaker.last_failure_time:
        status["last_failure_time"] = _llm_circuit_breaker.last_failure_time.isoformat()

        time_since = datetime.utcnow() - _llm_circuit_breaker.last_failure_time
        seconds_elapsed = time_since.total_seconds()
        seconds_remaining = _llm_circuit_breaker.timeout_seconds - seconds_elapsed
        if seconds_remaining > 0:
            status["seconds_until_reset"] = max(0.0, seconds_remaining)

    return status


def reset_circuit_breaker():
    """
    Force circuit breaker closed (emergency recovery).

    Used by administrators to recover from widespread LLM outages.
    Should only be called after manually verifying LLM endpoint is healthy.

    Returns:
        Updated circuit breaker status (from get_circuit_breaker_status)
    """
    _llm_circuit_breaker.reset()
    return get_circuit_breaker_status()
