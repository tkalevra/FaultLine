"""FaultLine MCP — Stateless Streamable HTTP transport.

Implements MCP JSON-RPC over HTTP using FastAPI. No external `mcp` package
required — transport is native FastAPI, tool logic reused from server.py.

Design:
- Single endpoint POST /mcp handles all JSON-RPC methods
- Stateless — no Mcp-Session-Id sessions required
- Reuses _call_tool(), TOOLS, FAULTLINE_API_URL, FAULTLINE_USER_ID from server.py
- _http_client initialised via FastAPI lifespan (not per-request)
- GET /health returns {"status": "ok", "transport": "http"} (no auth required)

Auth:
- Set MCP_API_KEY env var to require Bearer token on all POST /mcp requests
- Without MCP_API_KEY set the server runs unauthenticated (dev/localhost only)
- OpenWebUI: Settings → Integrations → Tools → add bearer token field
- Claude Desktop: add "Authorization": "Bearer <key>" to headers in config
"""

import os
import sys
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

import src.mcp.server as _mcp


# ── OpenWebUI OpenAPI tool request/response models ────────────────────────────

class RecallRequest(BaseModel):
    query: str
    user_id: str = ""


class RememberRequest(BaseModel):
    text: str
    user_id: str = ""


class RetractRequest(BaseModel):
    text: str
    user_id: str = ""

# If set, all POST /mcp requests must present: Authorization: Bearer <MCP_API_KEY>
MCP_API_KEY = os.environ.get("MCP_API_KEY", "").strip()


# ── Logging ──────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    """Log diagnostic message to stderr (stdout is for process output)."""
    print(f"[mcp-http] {msg}", file=sys.stderr, flush=True)


# ── Lifespan — shared HTTP client ────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    _mcp._http_client = httpx.AsyncClient(timeout=30.0)
    _log(f"HTTP transport started. FaultLine API: {_mcp.FAULTLINE_API_URL}")
    try:
        yield
    finally:
        await _mcp._http_client.aclose()
        _log("HTTP transport shut down.")


# ── App ───────────────────────────────────────────────────────────────────────


app = FastAPI(title="FaultLine MCP", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _jsonrpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "transport": "http"})


# ── OpenWebUI-compatible REST tool endpoints ──────────────────────────────────
# OpenWebUI connects via OpenAPI type, discovers these via /openapi.json,
# and calls them directly. Auth enforced via the shared MCP_API_KEY check.


def _check_auth(request: Request) -> bool:
    if not MCP_API_KEY:
        return True
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[7:] == MCP_API_KEY


@app.post(
    "/recall_memory",
    summary="Recall facts from FaultLine knowledge graph",
    description=(
        "Query the FaultLine knowledge graph to recall facts relevant to the "
        "conversation. Call this at the start of any turn where you need to "
        "remember things about the user — their name, family, pets, preferences, "
        "or history. Returns human-readable prose facts."
    ),
)
async def rest_recall_memory(body: RecallRequest, request: Request) -> JSONResponse:
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user_id = _mcp.FAULTLINE_USER_ID or body.user_id
    _log(f"REST recall_memory user_id={user_id[:8]}...")
    result = await _mcp.recall_memory_tool(query=body.query, user_id=user_id)
    return JSONResponse(result)


@app.post(
    "/remember_facts",
    summary="Store facts from conversation into FaultLine knowledge graph",
    description=(
        "Store facts from the current conversation into the FaultLine knowledge "
        "graph. Call this when the user states something worth remembering: their "
        "name, family members, pets, preferences, job, location, or corrections "
        "to prior facts. Runs full extract → validate → ingest pipeline."
    ),
)
async def rest_remember_facts(body: RememberRequest, request: Request) -> JSONResponse:
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user_id = _mcp.FAULTLINE_USER_ID or body.user_id
    _log(f"REST remember_facts user_id={user_id[:8]}...")
    result = await _mcp.remember_facts_tool(text=body.text, user_id=user_id)
    return JSONResponse(result)


@app.post(
    "/retract_fact",
    summary="Remove or correct a stored fact in FaultLine",
    description=(
        "Remove or correct a previously stored fact. Use when the user says "
        "something was wrong, has changed, or should be forgotten. Accepts "
        "natural language such as 'forget that Aurora is a computer' or "
        "'Des is 13 now not 12'."
    ),
)
async def rest_retract_fact(body: RetractRequest, request: Request) -> JSONResponse:
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user_id = _mcp.FAULTLINE_USER_ID or body.user_id
    _log(f"REST retract_fact user_id={user_id[:8]}...")
    result = await _mcp.retract_fact_tool(text=body.text, user_id=user_id)
    return JSONResponse(result)


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """Stateless MCP JSON-RPC dispatcher."""
    # Bearer token auth — enforced when MCP_API_KEY is set.
    if MCP_API_KEY:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != MCP_API_KEY:
            _log("SECURITY: rejected request — missing or invalid Bearer token")
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Parse JSON body — return parse error on malformed input.
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _jsonrpc_error(None, -32700, "Parse error"),
            status_code=400,
        )

    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {}) or {}

    _log(f"method={method!r} id={req_id!r}")

    # ── Dispatch table ────────────────────────────────────────────────────────

    if method == "initialize":
        return JSONResponse(
            _jsonrpc_result(req_id, {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "faultline-mcp", "version": "1.0.0"},
            })
        )

    elif method == "notifications/initialized":
        # Stateless HTTP: no persistent session to gate on.
        # Acknowledge with empty result (unlike stdio which sends no response).
        return JSONResponse(_jsonrpc_result(req_id, {}))

    elif method == "ping":
        return JSONResponse(_jsonrpc_result(req_id, {}))

    elif method == "tools/list":
        return JSONResponse(_jsonrpc_result(req_id, {"tools": _mcp.TOOLS}))

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        _log(f"tools/call name={tool_name!r} user_id={str(arguments.get('user_id', '?'))[:8]}...")
        result = await _mcp._call_tool(tool_name, arguments)
        return JSONResponse(_jsonrpc_result(req_id, result))

    else:
        _log(f"Unknown method: {method!r}")
        return JSONResponse(
            _jsonrpc_error(req_id, -32601, f"Method not found: {method}"),
            status_code=404,
        )
