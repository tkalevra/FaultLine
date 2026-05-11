"""MCP server implementation — raw stdio protocol (no external MCP library needed).

Handles tool discovery (`tools/list`) and tool execution (`tools/call`) following
the Model Context Protocol JSON-RPC convention over stdin/stdout.
"""

import asyncio
import json
import os
import sys
from typing import Any

import httpx

from .tools import (
    TOOLS,
    validate_edges,
    validate_text,
    validate_user_id,
)

FAULTLINE_API_URL = os.environ.get("FAULTLINE_API_URL", "http://localhost:8001").rstrip("/")


# ── Tool handlers ────────────────────────────────────────────────────────────


async def extract_tool(text: str, user_id: str) -> dict[str, Any]:
    """Call FaultLine /extract endpoint."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{FAULTLINE_API_URL}/extract",
            json={"text": text, "user_id": user_id},
        )
        resp.raise_for_status()
        return resp.json()


async def ingest_tool(
    text: str, user_id: str, edges: list[dict], source: str = "mcp"
) -> dict[str, Any]:
    """Call FaultLine /ingest endpoint."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
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
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
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
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{FAULTLINE_API_URL}/retract", json=body)
        resp.raise_for_status()
        return resp.json()


async def store_context_tool(text: str, user_id: str) -> dict[str, Any]:
    """Call FaultLine /store_context endpoint."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{FAULTLINE_API_URL}/store_context",
            json={"text": text, "user_id": user_id},
        )
        resp.raise_for_status()
        return resp.json()


# ── Tool dispatch ────────────────────────────────────────────────────────────

TOOL_DISPATCH: dict[str, callable] = {
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
    err = validate_user_id(user_id)
    if err:
        return {"error": f"Invalid user_id: {err}"}

    if tool_name in ("extract", "query", "store_context") and "text" in arguments:
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
    _log("MCP server starting (raw stdio protocol)")
    _log(f"FaultLine API URL: {FAULTLINE_API_URL}")
    _log("Awaiting MCP messages on stdin...")

    for line in sys.stdin:
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

        if method == "tools/list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})

        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            _log(f"Tool call: {tool_name} (user_id={arguments.get('user_id', '?')[:8]}...)")
            result = await _call_tool(tool_name, arguments)
            _send({"jsonrpc": "2.0", "id": req_id, "result": result})

        elif method == "initialize":
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "faultline-mcp", "version": "1.0.0"},
                },
            })

        else:
            _log(f"Unknown method: {method}")
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })
