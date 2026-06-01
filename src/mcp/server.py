"""MCP server implementation — raw stdio protocol (no external MCP library needed).

Handles tool discovery (`tools/list`) and tool execution (`tools/call`) following
the Model Context Protocol JSON-RPC convention over stdin/stdout.
"""

import asyncio
import json
import os
import re as _re
import sys
from typing import Any

import httpx

# ── Injection signal detection ────────────────────────────────────────────────
# Pre-flight check applied to `text` in remember_facts_tool() before forwarding to
# /extract/rewrite.  Only matches explicit instruction-override constructs — NOT normal
# personal data such as names, addresses, relationships, or occupations.
# Patterns require multi-word specificity to keep false-positive rate effectively zero.
# Mitigates TM-01/TM-09 (prompt injection via ingested facts).

_INJECTION_PATTERNS = [
    _re.compile(
        r'\bignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|context)\b',
        _re.I,
    ),
    _re.compile(r'\byou\s+are\s+now\s+(a\s+)?(new|different|another)\b', _re.I),
    _re.compile(r'\bnew\s+(system\s+)?instructions?\s*:', _re.I),
    _re.compile(r'<\|(?:system|user|assistant|im_start|im_end)\|>', _re.I),
    _re.compile(r'\[(?:INST|/INST|SYS|/SYS)\]'),
    _re.compile(r'<(?:system|assistant)\s*>', _re.I),
    _re.compile(
        r'\boverride\s+(all\s+)?(previous|prior|system)\s+(instructions?|prompts?)\b',
        _re.I,
    ),
]


def _check_injection_signals(text: str) -> str | None:
    """Return a description of the matched injection signal, or None if the text is clean.

    Scans `text` for known prompt-injection constructs before the text is forwarded
    to /extract/rewrite.  Conservative by design — only matches explicit multi-word
    instruction-override directives.  Normal personal data (names, places, occupations,
    family descriptions) will not match any of these patterns.
    """
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return f"Input contains prompt injection signal: {pattern.pattern[:60]}"
    return None

from .tools import (
    TOOLS,
    validate_edges,
    validate_query,
    validate_text,
    validate_user_id,
)

FAULTLINE_API_URL = os.environ.get("FAULTLINE_API_URL", "http://localhost:8000").rstrip("/")
FAULTLINE_USER_ID = os.environ.get("FAULTLINE_USER_ID", "").strip()

# Module-level HTTP client — initialised in run_mcp_server(), used by all tool handlers.
_http_client: httpx.AsyncClient | None = None

# Tracks whether notifications/initialized has been received.
_initialized: bool = False

# Tracks user IDs that have already been provisioned this session.
_provisioned_users: set[str] = set()


# ── Provisioning helper ──────────────────────────────────────────────────────


async def _ensure_provisioned(user_id: str) -> None:
    """Poll /provisioning/status until ready (up to 12 s). Non-fatal on failure."""
    if user_id in _provisioned_users:
        return
    try:
        for _ in range(6):  # up to 12s total
            resp = await _http_client.get(
                f"{FAULTLINE_API_URL}/provisioning/status",
                params={"user_id": user_id},
                timeout=5.0,
            )
            if resp.json().get("status") == "ready":
                _provisioned_users.add(user_id)
                return
            await asyncio.sleep(2.0)
    except Exception as e:
        _log(f"Provisioning check failed for {user_id[:8]}: {e}")
        # Non-fatal — don't block tool calls if provisioning endpoint unreachable


# ── Tool handlers ────────────────────────────────────────────────────────────


async def extract_tool(text: str, user_id: str) -> dict[str, Any]:
    """Call FaultLine /extract endpoint."""
    resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/extract",
        json={"text": text, "user_id": user_id},
    )
    resp.raise_for_status()
    return resp.json()


async def ingest_tool(
    text: str, user_id: str, edges: list[dict], source: str = "mcp"
) -> dict[str, Any]:
    """Call FaultLine /ingest endpoint."""
    resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/ingest",
        json={
            "text": text,
            "user_id": user_id,
            "edges": edges,
            "source": source,
        },
    )
    resp.raise_for_status()
    return resp.json()


async def query_tool(text: str, user_id: str, top_k: int = 5) -> dict[str, Any]:
    """Call FaultLine /query endpoint."""
    resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/query",
        json={"text": text, "user_id": user_id, "top_k": top_k},
    )
    resp.raise_for_status()
    return resp.json()


async def retract_tool(
    user_id: str,
    subject: str,
    rel_type: str | None = None,
    old_value: str | None = None,
    behavior: str | None = None,
) -> dict[str, Any]:
    """Call FaultLine /retract endpoint."""
    body: dict[str, Any] = {"user_id": user_id, "subject": subject}
    if rel_type:
        body["rel_type"] = rel_type
    if old_value:
        body["old_value"] = old_value
    if behavior:
        body["behavior"] = behavior
    resp = await _http_client.post(f"{FAULTLINE_API_URL}/retract", json=body)
    resp.raise_for_status()
    return resp.json()


async def store_context_tool(text: str, user_id: str) -> dict[str, Any]:
    """Call FaultLine /store_context endpoint."""
    resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/store_context",
        json={"text": text, "user_id": user_id},
    )
    resp.raise_for_status()
    return resp.json()


async def recall_memory_tool(query: str, user_id: str) -> dict[str, Any]:
    """Call FaultLine /query endpoint."""
    resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/query",
        json={"text": query, "user_id": user_id},
    )
    resp.raise_for_status()
    return resp.json()


async def remember_facts_tool(text: str, user_id: str) -> dict[str, Any]:
    """Call /extract/rewrite then /ingest — full pipeline in one call."""
    # Pre-flight injection check — reject before any LLM or backend call.
    injection_signal = _check_injection_signals(text)
    if injection_signal:
        _log(f"SECURITY: injection signal rejected — {injection_signal[:80]}")
        return {"status": "rejected", "reason": "Input contains disallowed content", "committed": 0}

    rewrite_resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/extract/rewrite",
        json={"text": text, "user_id": user_id},
    )
    rewrite_resp.raise_for_status()
    edges = [
        e for e in rewrite_resp.json().get("edges", [])
        if not e.get("low_confidence", False)
    ]
    if not edges:
        return {"status": "no_facts", "message": "No confident facts extracted from text"}
    ingest_resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/ingest",
        json={"text": text, "user_id": user_id, "edges": edges, "source": "mcp"},
    )
    ingest_resp.raise_for_status()
    return ingest_resp.json()


async def retract_fact_tool(text: str, user_id: str) -> dict[str, Any]:
    """Call FaultLine /retract/correct endpoint."""
    resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/retract/correct",
        json={"text": text, "user_id": user_id},
    )
    resp.raise_for_status()
    return resp.json()


# ── Tool dispatch ────────────────────────────────────────────────────────────

TOOL_DISPATCH: dict[str, callable] = {
    "recall_memory": recall_memory_tool,
    "remember_facts": remember_facts_tool,
    "retract_fact": retract_fact_tool,
    # Low-level tools kept for direct testing — not advertised in TOOLS schema
    "extract": extract_tool,
    "ingest": ingest_tool,
    "query": query_tool,
    "retract": retract_tool,
    "store_context": store_context_tool,
}


# ── Input validation (mirrors tools.py validators) ────────────────────────────


def _validate_tool_input(tool_name: str, arguments: dict) -> dict | None:
    """Return error response dict if input invalid, None if valid."""
    user_id: str = arguments.get("user_id", "")

    if not FAULTLINE_USER_ID:  # only validate user_id from args if no env override
        err = validate_user_id(user_id)
        if err:
            return {"error": f"Invalid user_id: {err}"}

    if tool_name == "recall_memory":
        err = validate_query(arguments.get("query", ""))
        if err:
            return {"error": f"Invalid query: {err}"}

    elif tool_name in ("remember_facts", "retract_fact"):
        err = validate_text(arguments.get("text", ""))
        if err:
            return {"error": f"Invalid text: {err}"}

    elif tool_name in ("extract", "query", "store_context") and "text" in arguments:
        err = validate_text(arguments["text"])
        if err:
            return {"error": f"Invalid text: {err}"}

    if tool_name == "ingest":
        err = validate_edges(arguments.get("edges", []))
        if err:
            return {"error": f"Invalid edges: {err}"}

    if tool_name == "retract":
        if not arguments.get("subject", "").strip():
            return {"error": "subject must not be empty"}

    return None


# ── MCP message loop ─────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    """Log diagnostic message to stderr (stdout is for MCP protocol)."""
    print(f"[mcp-server] {msg}", file=sys.stderr, flush=True)


def _send(response: dict) -> None:
    """Send a JSON-RPC response to stdout."""
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


async def _call_tool(tool_name: str, arguments: dict) -> dict:
    """Dispatch tool call and return result or error."""
    validation_error = _validate_tool_input(tool_name, arguments)
    if validation_error:
        return {"content": [{"type": "text", "text": json.dumps(validation_error)}]}

    handler = TOOL_DISPATCH.get(tool_name)
    if handler is None:
        return {
            "content": [
                {"type": "text", "text": json.dumps({"error": f"Unknown tool: {tool_name}"})}
            ]
        }

    # Resolve effective user_id: env override takes precedence over argument.
    effective_user_id = FAULTLINE_USER_ID if FAULTLINE_USER_ID else arguments.get("user_id", "")
    if FAULTLINE_USER_ID:
        arguments = {**arguments, "user_id": FAULTLINE_USER_ID}

    # Transparent provisioning — non-fatal if endpoint unreachable.
    await _ensure_provisioned(effective_user_id)

    try:
        result = await handler(**arguments)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    except httpx.TimeoutException:
        return {"content": [{"type": "text", "text": json.dumps({"error": "FaultLine API timeout"})}]}
    except httpx.HTTPStatusError as e:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"error": f"FaultLine API error {e.response.status_code}"}
                    ),
                }
            ]
        }
    except httpx.RequestError as e:
        return {
            "content": [
                {"type": "text", "text": json.dumps({"error": f"FaultLine API unreachable: {e}"})}
            ]
        }
    except Exception as e:
        return {
            "content": [
                {"type": "text", "text": json.dumps({"error": f"Unexpected error: {str(e)}"})}
            ]
        }


async def run_mcp_server() -> None:
    """Run the MCP server on stdin/stdout using raw JSON-RPC protocol."""
    global _http_client, _initialized
    _http_client = httpx.AsyncClient(timeout=30.0)
    try:
        _log("MCP server starting (raw stdio protocol)")
        _log(f"FaultLine API URL: {FAULTLINE_API_URL}")
        _log("Awaiting MCP messages on stdin...")

        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:  # EOF
                break
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _log(f"Invalid JSON received: {line[:100]}")
                continue

            req_id = request.get("id")
            method = request.get("method", "")

            if method == "initialize":
                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "faultline-mcp", "version": "1.0.0"},
                    },
                })

            elif method == "notifications/initialized":
                _initialized = True
                # Notifications do not get a response — continue without sending.

            elif method == "ping":
                _send({"jsonrpc": "2.0", "id": req_id, "result": {}})

            elif method in ("tools/list", "tools/call"):
                if not _initialized:
                    _send({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32002,
                            "message": "Server not initialized — send notifications/initialized first",
                        },
                    })
                    continue

                if method == "tools/list":
                    _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})

                else:  # tools/call
                    params = request.get("params", {})
                    tool_name = params.get("name", "")
                    arguments = params.get("arguments", {})
                    _log(f"Tool call: {tool_name} (user_id={arguments.get('user_id', '?')[:8]}...)")
                    result = await _call_tool(tool_name, arguments)
                    _send({"jsonrpc": "2.0", "id": req_id, "result": result})

            else:
                _log(f"Unknown method: {method}")
                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })
    finally:
        await _http_client.aclose()
