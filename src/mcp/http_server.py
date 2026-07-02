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

import hmac
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

import src.mcp.server as _mcp


# ── OpenWebUI OpenAPI tool request/response models ────────────────────────────

class RecallRequest(BaseModel):
    query: str = Field(..., description="The user's current message copied VERBATIM and in full — do NOT summarize, shorten, or reduce it to a keyword or topic. Keep every word, especially 'not', 'no', 'now', 'actually', 'instead' and any names/values. The backend extracts the search topic AND decides intent (recall vs correction) from the whole sentence itself; a reduced query strips the meaning. Required — never leave empty.")
    user_id: str = ""


class RememberRequest(BaseModel):
    text: str = Field(..., description="The user's message in natural language, copied VERBATIM and in full — the raw sentence(s) exactly as they said them. Do NOT pre-extract, summarize, rephrase, or restructure it into facts, triples, or bullet/line items: FaultLine's engine does ALL extraction, typing, and structuring — it needs the raw words. Keep every word, especially 'not'/'no'/'now'/'actually'/'instead' and any names, values, and dates. Required — never leave empty.")
    user_id: str = ""


class IngestDocumentRequest(BaseModel):
    text: str = Field(..., description="The FULL text of the document, article, PDF extraction, or long-form note to store, copied VERBATIM — do NOT summarize, shorten, or pre-extract facts from it. FaultLine chunks and extracts everything itself; it needs the raw text. Required — never leave empty.")
    source_ref: str = Field("", description="OPTIONAL: where this document came from — a URL, filename, or citation string (e.g. 'https://example.com/article', 'meeting-notes-2026-06.pdf'). Stored with every fact extracted from the document so recall can cite its source.")
    title: str = Field("", description="OPTIONAL: the document's title. Used as the source reference when source_ref is not provided.")
    user_id: str = ""


class RetractRequest(BaseModel):
    text: str
    user_id: str = ""


class ForgetRequest(BaseModel):
    subject: str = Field(..., description="WHOSE fact to forget — the named subject of the single fact the user explicitly asked you to forget (e.g. their own name via 'me'/'I', or a specific named person/thing). Required — a forget MUST name exactly one target; never a broad/everything wipe.")
    rel_type: Optional[str] = Field(None, description="OPTIONAL: the relationship of the specific fact to forget (e.g. occupation, has_pet, has_email). Narrows the forget to one fact about the subject. Omit only when the subject identifies a single fact unambiguously.")
    old_value: Optional[str] = Field(None, description="OPTIONAL: the specific value/object of the fact to forget (e.g. the email address, the pet's name). Pins the forget to exactly one fact.")
    user_id: str = ""


class LearnRequest(BaseModel):
    text: str
    user_id: str = ""

# If set, all POST /mcp requests must present: Authorization: Bearer <MCP_API_KEY>
MCP_API_KEY = os.environ.get("MCP_API_KEY", "").strip()

# Comma-separated browser-origin allowlist for CORS. Default empty → no cross-origin
# browser access. The OpenWebUI → :8002 tool call is server-to-server and is NOT
# browser CORS-gated, so an empty allowlist does not break the live path. Operators
# set MCP_ALLOWED_ORIGINS=https://<openwebui-host> only if browser-origin access is needed.
MCP_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
]


# ── Logging ──────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    """Log diagnostic message to stderr (stdout is for process output)."""
    print(f"[mcp-http] {msg}", file=sys.stderr, flush=True)


# ── Lifespan — shared HTTP client ────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    _mcp._http_client = httpx.AsyncClient(timeout=30.0)
    _log(f"HTTP transport started. FaultLine API: {_mcp.FAULTLINE_API_URL}")
    if MCP_API_KEY:
        _log(f"Auth ENABLED — MCP_API_KEY set ({len(MCP_API_KEY)} chars)")
    else:
        _log(
            "WARNING: Auth DISABLED — MCP_API_KEY not set; running OPEN. "
            "This is an unauthenticated write path into every tenant's knowledge "
            "graph — dev/localhost ONLY, never a deployment posture."
        )
    try:
        yield
    finally:
        await _mcp._http_client.aclose()
        _log("HTTP transport shut down.")


# ── App ───────────────────────────────────────────────────────────────────────


app = FastAPI(title="FaultLine MCP", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=MCP_ALLOWED_ORIGINS,
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


# Declared security scheme → FastAPI auto-emits it into /openapi.json
# (components.securitySchemes.HTTPBearer + per-operation security). auto_error=False
# lets unauthenticated requests reach require_auth so we keep our own fail-loud
# logging and 401 (with WWW-Authenticate) instead of FastAPI's generic 403.
_bearer = HTTPBearer(auto_error=False, description="MCP_API_KEY bearer token")


def _resolve_principal(credentials: str | None) -> str | None:
    """Map a presented bearer credential to a principal.

    Today: single shared key → returns 'shared' on match, None on miss.
    Forward-compat (DEV/SECURITY-multiuser-tenant-isolation.md remediation #1):
    swap this body for a token→user_id lookup and return the user_id, without
    touching require_auth's call sites.
    """
    if not MCP_API_KEY:
        return "anonymous"  # unauthenticated mode (dev/localhost only)
    if credentials is None:
        return None
    if hmac.compare_digest(credentials, MCP_API_KEY):
        return "shared"
    return None


def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """FastAPI dependency enforcing the bearer scheme; returns the principal.

    Returns a principal id (threaded so the tenant-isolation follow-up can bind
    principal→user_id here). Raises 401 on failure. Logs only length + reason —
    never any prefix of the secret.
    """
    if not MCP_API_KEY:
        return "anonymous"
    presented = creds.credentials if creds is not None else None
    principal = _resolve_principal(presented)
    if principal is None:
        client = request.client.host if request.client else "unknown"
        if creds is None:
            _log(f"REST 401 from {client} — no/blank bearer credential")
        elif creds.scheme.lower() != "bearer":
            _log(f"REST 401 from {client} — non-bearer scheme {creds.scheme!r}")
        else:
            _log(f"REST 401 from {client} — key mismatch ({len(presented)} chars)")
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def _resolve_rest_user_id(request: Request, body_user_id: str, principal: str | None) -> str:
    """Resolve the tenant for a REST shorthand call via the SAME identity seam as /mcp.

    Reads X-OpenWebUI-User-Id (which OpenWebUI stamps on the REST path too — previously
    dropped here, DEV/SECURITY-multiuser-tenant-isolation.md Finding 1), falling back to
    body.user_id, then runs it through bind_tenant() (spoof-guard + UUID validation +
    FAULTLINE_USER_ID single-user fallback). Translates a spoof/malformed rejection into
    the matching HTTP status — fail loud, never silently route to a wrong/shared tenant.
    """
    claimed = request.headers.get("X-OpenWebUI-User-Id", "") or body_user_id
    try:
        return _mcp.bind_tenant(principal, claimed)
    except _mcp.TenantSpoofError as exc:
        client = request.client.host if request.client else "unknown"
        _log(f"REST {exc.status_code} from {client} — {exc.message}")
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post(
    "/recall_memory",
    summary="Recall facts from FaultLine knowledge graph",
    description=(
        "Call at the START of a turn to look up what you already know when the user "
        "asks about or references something you may know about them, their people, or "
        "their world. This only READS memory — it never saves. To SAVE a new fact the "
        "user states, use remember_facts instead. Treat the results as your own "
        "knowledge, spoken naturally — never as retrieved data. "
        "Pass the user's message VERBATIM and in full as `query` — never reduce it to a "
        "keyword or topic; the backend extracts the topic and detects corrections itself."
    ),
)
async def rest_recall_memory(
    request: Request, body: RecallRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST recall_memory user_id={user_id[:8]}...")
    # Provisioning gate — the REST path (OpenWebUI's live door) must wait out provisioning
    # too, else a fresh tenant's first recall races the schema. Backend _ensure_tenant_ready
    # is the real guard; this returns the clean retry signal without a doomed backend call.
    if not await _mcp._ensure_provisioned(user_id):
        return JSONResponse(
            {"status": "provisioning",
             "message": "Memory is being set up for you — please retry in a moment.",
             "facts": []}
        )
    result = await _mcp.recall_memory_tool(query=body.query, user_id=user_id)
    return JSONResponse(result)


@app.post(
    "/remember_facts",
    summary="Store facts from conversation into FaultLine knowledge graph",
    description=(
        "Save something the user just told you. Call this whenever the user states a "
        "fact about themselves, another person, or their world — a name, relationship, "
        "preference, job, possession, or location — even mentioned in passing, and also "
        "when they correct a prior fact. Default to calling it; skip only pure questions "
        "or chitchat. Do not ask permission first. "
        "IMPORTANT — pass the user's message in NATURAL LANGUAGE, verbatim: do NOT extract, "
        "summarize, or restructure it into facts, triples, or line items yourself. FaultLine's "
        "engine does all extraction, validation, and structuring; it needs the raw sentence(s). "
        "(The only tool where you generate structured statements is learn_facts.)"
    ),
)
async def rest_remember_facts(
    request: Request, body: RememberRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST remember_facts user_id={user_id[:8]}...")
    # Provisioning gate — wait out provisioning on the REST path so a fresh tenant's first
    # remember does not race the schema. Backend _ensure_tenant_ready is the authoritative guard.
    if not await _mcp._ensure_provisioned(user_id):
        return JSONResponse(
            {"status": "provisioning",
             "message": "Memory is being set up for you — please retry in a moment.",
             "committed": 0}
        )
    result = await _mcp.remember_facts_tool(text=body.text, user_id=user_id)
    return JSONResponse(result)


@app.post(
    "/ingest_document",
    summary="Store a document or long-form content into FaultLine knowledge graph",
    description=(
        "Store a document, article, PDF text, or long-form content in memory. Use when "
        "the user shares or pastes a document, article, notes, or any multi-paragraph "
        "body of text and wants it remembered — the whole text is chunked, retained "
        "verbatim, and mined for facts automatically. Pass the FULL text verbatim as "
        "`text`; do NOT summarize or pre-extract facts yourself. Provide `source_ref` "
        "(URL/filename) or `title` when known so extracted facts carry a citation. "
        "Not for conversational messages — use remember_facts for those."
    ),
)
async def rest_ingest_document(
    request: Request, body: IngestDocumentRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST ingest_document user_id={user_id[:8]}... chars={len(body.text)} ref={body.source_ref!r}")
    # Provisioning gate — wait out provisioning on the REST path so a fresh tenant's first
    # document does not race the schema. Backend _ensure_tenant_ready is the authoritative guard.
    if not await _mcp._ensure_provisioned(user_id):
        return JSONResponse(
            {"status": "provisioning",
             "message": "Memory is being set up for you — please retry in a moment.",
             "chunks": 0}
        )
    result = await _mcp.ingest_document_tool(
        text=body.text,
        user_id=user_id,
        source_ref=body.source_ref,
        title=body.title,
    )
    return JSONResponse(result)


@app.post(
    "/learn_facts",
    summary="Store LLM-generated ontological knowledge into FaultLine",
    description=(
        "Store knowledge the LLM generates as explicit ontological statements into the "
        "FaultLine knowledge graph with source=llm_learn. Use when the user asks to learn "
        "a topic. Generate statements in the forms: 'X is a subclass of Y', "
        "'X is an instance of Y', 'X is a part of Y' — one per line — then call this. "
        "Facts are staged as Class B (llm_learn provenance) and confirmed over time."
    ),
)
async def rest_learn_facts(
    request: Request, body: LearnRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST learn_facts user_id={user_id[:8]}...")
    result = await _mcp.learn_facts_tool(text=body.text, user_id=user_id)
    return JSONResponse(result)


@app.post(
    "/retract_fact",
    summary="Remove a stored fact from FaultLine",
    description=(
        "Use ONLY when the user explicitly wants something deleted or forgotten — "
        "signals like 'forget that', 'delete', 'erase', 'remove that'. For corrections "
        "or updated values (the user giving a NEW value for something), use "
        "remember_facts instead, NOT this."
    ),
)
async def rest_retract_fact(
    request: Request, body: RetractRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST retract_fact user_id={user_id[:8]}...")
    result = await _mcp.retract_fact_tool(text=body.text, user_id=user_id)
    return JSONResponse(result)


@app.post(
    "/forget_fact",
    summary="Permanently forget ONE specific named fact from FaultLine",
    description=(
        "Use ONLY when the user EXPLICITLY and deliberately asks you to forget or delete "
        "ONE specific fact about a NAMED target — e.g. 'forget my email address', "
        "'delete that I have a dog named Rex', 'forget that Jordan is my spouse'. This "
        "tombstones exactly the one fact you name (it is recoverable, not a hard wipe). "
        "You MUST name the target: pass `subject` (whose fact — 'me' for the user, or the "
        "named person/thing) and, to pin it, `rel_type` and/or `old_value`. "
        "NEVER call this for a broad or bulk request ('forget everything', 'delete all my "
        "data', 'wipe my memory') — there is no bulk forget; refuse and ask which single "
        "fact. For a CORRECTION (the user giving a NEW value) use remember_facts instead."
    ),
)
async def rest_forget_fact(
    request: Request, body: ForgetRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST forget_fact user_id={user_id[:8]}... subject={body.subject!r} rel_type={body.rel_type!r}")
    result = await _mcp.forget_fact_tool(
        user_id=user_id,
        subject=body.subject,
        rel_type=body.rel_type,
        old_value=body.old_value,
    )
    return JSONResponse(result)


@app.post("/mcp")
async def mcp_endpoint(
    request: Request, _principal: str = Depends(require_auth)
) -> JSONResponse:
    """Stateless MCP JSON-RPC dispatcher. Bearer auth via require_auth dependency."""
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
        # Resolve tenant identity ONCE via the shared bind_tenant() seam (brain not
        # transport). OpenWebUI forwards the authenticated user's UUID via
        # X-OpenWebUI-User-Id; an explicit arguments.user_id takes precedence over it.
        # bind_tenant validates the claimed id (well-formed UUID, spoof-guard against
        # _principal) and applies FAULTLINE_USER_ID only as a single-user fallback.
        claimed = request.headers.get("X-OpenWebUI-User-Id", "") or arguments.get("user_id", "")
        try:
            resolved_user_id = _mcp.bind_tenant(_principal, claimed)
        except _mcp.TenantSpoofError as exc:
            _log(f"tools/call name={tool_name!r} REJECT: {exc.message}")
            return JSONResponse(
                _jsonrpc_error(req_id, -32602, exc.message),
                status_code=exc.status_code,
            )
        arguments = {**arguments, "user_id": resolved_user_id}
        _log(f"tools/call name={tool_name!r} user_id={resolved_user_id[:8]}...")
        result = await _mcp._call_tool(tool_name, arguments)
        return JSONResponse(_jsonrpc_result(req_id, result))

    else:
        _log(f"Unknown method: {method!r}")
        return JSONResponse(
            _jsonrpc_error(req_id, -32601, f"Method not found: {method}"),
            status_code=404,
        )
