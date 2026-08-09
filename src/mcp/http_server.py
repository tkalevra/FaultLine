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
import hashlib
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
import psycopg2
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

import src.mcp.premise as _premise
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


# 2026-07-28 (SEP-2322): ALL results carry a required `resultType` field — "complete" for
# ordinary results. Clients MUST treat results from earlier-protocol servers that omit the
# field as "complete", so adding it is non-breaking for every legacy client while making every
# result predictably parsable for modern ones. The server also SHOULD identify itself in each
# result's `_meta` (io.modelcontextprotocol/serverInfo) — self-reported, for display/logging
# only, never trusted by clients.
_SERVER_INFO_META = {"io.modelcontextprotocol/serverInfo": {
    "name": "faultline-mcp", "version": "1.0.0"}}


def _jsonrpc_result(req_id: Any, result: Any, *, _meta: bool = True) -> dict:
    """Wrap `result` in a 2026-07-28 JSON-RPC envelope (resultType + serverInfo).

    Non-breaking by construction: the spec REQUIRES clients to treat a missing `resultType` as
    "complete", so adding the field never changes how a legacy client reads the payload — it only
    makes the shape explicit (predictably parsable) for modern clients. `_meta` serverInfo is
    SHOULD-level (SEP-2575). A result that already carries `resultType` (server/discover) or its
    own `_meta` is never double-stamped; an EMPTY result (ping / notifications/initialized ack)
    is returned byte-identical so legacy ack semantics stay untouched.
    """
    envelope = {"jsonrpc": "2.0", "id": req_id, "result": result}
    if isinstance(result, dict) and result:
        if "resultType" not in result:
            envelope["result"]["resultType"] = "complete"
        if _meta and "_meta" not in result:
            envelope["result"]["_meta"] = _SERVER_INFO_META
    return envelope


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


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


# ── Dashboard-backed credential resolution (seats + rotated MCP keys) ─────────
# When the operator has minted seats or rotated the MCP key from the control-
# plane webui, the authoritative credentials live in the shared postgres DB
# (both containers talk to the same instance). The MCP server consults them
# here so that:
#   • a per-seat token authenticates AND identifies (the token's hash resolves
#     to a seat's user_id → returned as the principal → bind_tenant's dormant
#     "Option A" activates with NO call-site change; the token IS the identity,
#     the X-OpenWebUI-User-Id header becomes a cross-check, not the source).
#   • a rotated-then-revoked MCP key stops working IMMEDIATELY (no cache; a
#     single indexed lookup on every auth).
# When the DB is unreachable OR the operator has minted nothing yet, behaviour
# falls back to the original env-MCP_API_KEY / anonymous-dev path (back-compat).

def _dashboard_dsn() -> str:
    return os.environ.get("POSTGRES_DSN", "").strip()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dashboard_lookup(query: str, params: tuple) -> Optional[tuple]:
    """Run a single SELECT against the shared DB; return the first row or None.

    Opens a short-lived connection per call — local postgres, one indexed probe,
    negligible cost. Any error → None (auth falls back to env); the MCP path
    never hard-fails on a DB hiccup. Blocking, but ~ms and not on the chat path.
    """
    dsn = _dashboard_dsn()
    if not dsn:
        return None
    try:
        with psycopg2.connect(dsn, connect_timeout=3) as db, db.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()
    except Exception:  # noqa: BLE001 — DB optional; degrade to env auth
        return None


def _resolve_seat_token_db(credentials: Optional[str]) -> Optional[str]:
    """Return the seat's user_id if credentials is a valid active seat token."""
    if not credentials:
        return None
    row = _dashboard_lookup(
        "SELECT user_id FROM public.dashboard_seats "
        "WHERE token_hash = %s AND active = TRUE",
        (_sha256_hex(credentials),),
    )
    if not row:
        return None
    return str(row[0])


def _mcp_key_active_db(credentials: Optional[str]) -> bool:
    """True if credentials matches an active rotated MCP key in the DB."""
    if not credentials:
        return False
    return _dashboard_lookup(
        "SELECT 1 FROM public.dashboard_mcp_keys "
        "WHERE key_hash = %s AND is_active = TRUE LIMIT 1",
        (_sha256_hex(credentials),),
    ) is not None


def _any_dashboard_credential_configured() -> bool:
    """True if the operator has minted ANY seat or rotated key (DB-gated).

    Gates the anonymous-dev fallback: once seats/keys exist in the DB, the open
    anonymous mode is suppressed even if MCP_API_KEY env is unset, so minting the
    first seat closes the open posture."""
    # NOTE the empty params tuple on BOTH probes. `_dashboard_lookup(query, params)` takes
    # params as a REQUIRED positional; omitting it raises TypeError at the CALL SITE, i.e.
    # before the callee's `try`, so its documented "any error -> None, never hard-fails"
    # fail-safe never gets the chance to run. Every credential-less request then 500s inside
    # require_auth — which is the DEFAULT posture for a fresh self-hosted install.
    return _dashboard_lookup(
        "SELECT 1 FROM public.dashboard_seats WHERE active = TRUE LIMIT 1", ()
    ) is not None or _dashboard_lookup(
        "SELECT 1 FROM public.dashboard_mcp_keys WHERE is_active = TRUE LIMIT 1", ()
    ) is not None


def _resolve_principal(credentials: str | None) -> str | None:
    """Map a presented bearer credential to a principal.

    Resolution order:
      1. Per-seat token (DB) → returns the seat's user_id UUID. bind_tenant's
         Option A then treats the token as the authoritative identity.
      2. Rotated MCP key (DB) → 'shared'.
      3. Env MCP_API_KEY (back-compat / bootstrap) → 'shared'.
      4. Anonymous dev mode → only when NO credentials are configured anywhere
         (no env key AND no DB seats/keys) AND no credential was presented.

    Returns None on every miss → caller raises 401. Constant-time on the env
    secret path (hmac.compare_digest); the DB paths compare sha256 digests via
    indexed equality (the preimage is the secret, so there is no partial-secret
    timing to leak).
    """
    # 1. Per-seat token (highest precedence — strongest auth).
    seat_uid = _resolve_seat_token_db(credentials)
    if seat_uid:
        return seat_uid
    # 2. Rotated MCP key in the DB.
    if _mcp_key_active_db(credentials):
        return "shared"
    # 3. Env MCP_API_KEY.
    if MCP_API_KEY:
        if credentials is not None and hmac.compare_digest(credentials, MCP_API_KEY):
            return "shared"
        return None
    # 4. Anonymous dev mode — only when nothing is configured AND nothing was
    # presented. Once a seat/key exists, the open posture is suppressed.
    if credentials is None and not _any_dashboard_credential_configured():
        return "anonymous"
    return None


def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """FastAPI dependency enforcing the bearer scheme; returns the principal.

    Returns a principal id — either "shared"/"anonymous" (transport auth) or a
    seat's user_id UUID (per-seat token: the token IS the identity). Raises 401
    on failure. Logs only length + reason — never any prefix of the secret.

    NB: do NOT short-circuit on ``not MCP_API_KEY`` — _resolve_principal now
    consults the dashboard DB (seats + rotated keys) and only falls back to the
    anonymous-dev posture when NOTHING is configured anywhere. Short-circuiting
    here would let a minted seat be bypassed whenever the env key is unset.
    """
    presented = creds.credentials if creds is not None else None
    principal = _resolve_principal(presented)
    if principal is None:
        client = request.client.host if request.client else "unknown"
        if creds is None:
            _log(f"REST 401 from {client} — no/blank bearer credential")
        elif creds.scheme.lower() != "bearer":
            _log(f"REST 401 from {client} — non-bearer scheme {creds.scheme!r}")
        else:
            _log(f"REST 401 from {client} — credential rejected ({len(presented)} chars)")
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

    Per-seat token override: when the principal is itself a UUID (a minted seat token
    authenticated), the token IS the identity — the seat's user_id is authoritative and
    any client-claimed header is ignored. This is what makes a seat token actually scope
    a connection in real OpenWebUI wiring (OpenWebUI forwards its OWN logged-in user's
    UUID as X-OpenWebUI-User-Id, which would otherwise mismatch the seat and 403). The
    token proves identity; the header is transport plumbing.
    """
    if principal and _mcp._TENANT_UUID_RE.match(principal.strip().lower()):
        return principal.strip().lower()
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
        "'delete that I have a dog named Rex', 'forget that Ada is my spouse'. This "
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

    # ── 2026-07-28 header-based routing (SEP-2243) ────────────────────────────────────
    # The spec requires Streamable HTTP POSTs to carry `Mcp-Method`/`Mcp-Name` headers so
    # gateways/WAFs can route and authorize WITHOUT parsing the JSON body. We ACCEPT them and,
    # when present and self-consistent, they are authoritative for routing: a gateway that
    # already verified the method can skip body parsing entirely. When they are ABSENT (a
    # legacy client, or any client that predates the header requirement), we fall back to the
    # body `method` exactly as before — never break a legacy caller for lack of a header.
    _hdr_method = request.headers.get("mcp-method")
    if _hdr_method:
        # A body method that disagrees with the header is a mis-routed request — the header is
        # what a load balancer acted on, so surface HeaderMismatchError rather than silently
        # processing under a different method than the one routed.
        if method and method != _hdr_method:
            _log(f"method mismatch header={_hdr_method!r} body={method!r} id={req_id!r}")
            return JSONResponse(
                _jsonrpc_error(req_id, _premise.ERROR_HEADER_MISMATCH,
                               f"Mcp-Method header {_hdr_method!r} disagrees with body method "
                               f"{method!r}"),
                status_code=400,
            )
        method = _hdr_method
    _hdr_name = request.headers.get("mcp-name")
    if _hdr_name:
        _log(f"Mcp-Name={_hdr_name!r}")

    # ── 2026-07-28 version negotiation (SEP-2575) ─────────────────────────────────────
    # Modern clients carry `io.modelcontextprotocol/protocolVersion` in `_meta` on EVERY
    # request (there is no initialize handshake). A modern request naming a version we do not
    # support gets UnsupportedProtocolVersionError with the supported list, per the spec —
    # the client picks a mutually-supported version and retries. Legacy requests (no `_meta`
    # version) are untouched: they negotiate via `initialize` below.
    _req_meta = (params or {}).get("_meta") or {}
    _req_version = (
        _req_meta.get("io.modelcontextprotocol/protocolVersion")
        or request.headers.get("mcp-protocol-version")
        or ""
    )
    if _req_version and _req_version not in _premise.SUPPORTED_PROTOCOL_VERSIONS:
        _log(f"Unsupported protocol version {_req_version!r} id={req_id!r}")
        return JSONResponse(
            _jsonrpc_error(
                req_id, _premise.ERROR_UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                data={
                    "supported": list(_premise.SUPPORTED_PROTOCOL_VERSIONS),
                    "requested": _req_version,
                },
            ),
            status_code=400,
        )

    # ── Dispatch table ────────────────────────────────────────────────────────

    if method == "server/discover":
        # 2026-07-28: servers MUST implement server/discover (SEP-2575). Lets a modern client
        # learn supported protocol versions + capabilities + identity in one request, before
        # any other RPC. No auth-gated state here: this is identity/capability advertisement,
        # answered identically for every caller.
        _log("server/discover")
        return JSONResponse(_jsonrpc_result(
            req_id, _premise.discover_result(), _meta=False))

    if method == "initialize":
        # Spec: echo the client's protocolVersion if we support it, else offer our latest.
        # (We used to hardcode 2025-03-26 and ignore the request entirely.)
        _client_ver = (params or {}).get("protocolVersion")
        return JSONResponse(
            _jsonrpc_result(req_id, {
                "protocolVersion": _premise.negotiate_protocol_version(_client_ver),
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
        return JSONResponse(_jsonrpc_result(
            req_id, {
                # CacheableResult (2026-07-28, SEP-2549): ttlMs + cacheScope let clients cache
                # the tool catalog and reduce polling; deterministic order (TOOLS list) keeps
                # prompt-cache hit rates stable. Non-breaking for legacy clients.
                "tools": _mcp.TOOLS,
                "ttlMs": _premise._LIST_TTL_MS,
                "cacheScope": _premise._LIST_CACHE_SCOPE,
            }))

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        # Resolve tenant identity ONCE via the shared bind_tenant() seam (brain not
        # transport). OpenWebUI forwards the authenticated user's UUID via
        # X-OpenWebUI-User-Id; an explicit arguments.user_id takes precedence over it.
        # bind_tenant validates the claimed id (well-formed UUID, spoof-guard against
        # _principal) and applies FAULTLINE_USER_ID only as a single-user fallback.
        #
        # Per-seat token override: when _principal is itself a UUID (a minted seat
        # token authenticated), the token IS the identity — authoritative; the
        # client-claimed header is ignored (see _resolve_rest_user_id for the why).
        if _principal and _mcp._TENANT_UUID_RE.match(_principal.strip().lower()):
            resolved_user_id = _principal.strip().lower()
        else:
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
