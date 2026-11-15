"""FaultLine MCP — Stateless Streamable HTTP transport.

Implements MCP JSON-RPC over HTTP using FastAPI. No external `mcp` package
required — transport is native FastAPI, tool logic reused from server.py.

Design:
- Single endpoint POST /mcp handles all JSON-RPC methods
- Stateless — no Mcp-Session-Id sessions required
- Reuses TOOLS, FAULTLINE_API_URL, FAULTLINE_USER_ID from server.py. NOTE: only the
  JSON-RPC door dispatches through _call_tool(); the REST endpoints below call the
  tool handlers DIRECTLY, so anything _call_tool does for free (input validation,
  the isError envelope) must be arranged explicitly on this door.
- _http_client initialised via FastAPI lifespan (not per-request)
- GET /health returns {"status": "ok", "transport": "http"} (no auth required)

Auth:
- Set MCP_API_KEY env var to require Bearer token on all POST /mcp requests
- Without MCP_API_KEY set the server runs unauthenticated (dev/localhost only)
- OpenWebUI: Settings → Integrations → Tools → add bearer token field
- Claude Desktop: add "Authorization": "Bearer <key>" to headers in config
"""

import hashlib
import hmac
import json
import mimetypes
import os
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

# Serve the self-hosted dashboard webfonts with the correct MIME. Python's mimetypes doesn't
# know woff/woff2 on every platform (falls back to application/octet-stream), and with the
# dashboard's `X-Content-Type-Options: nosniff` a correct type is the robust choice. StaticFiles
# resolves the type via mimetypes.guess_type, so registering here is sufficient.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response


def _static_files_404_on_unsupported_method(static_files_cls):
    """Wrap a Starlette ``StaticFiles`` so an unsupported METHOD yields 404, not 405.

    ``StaticFiles.get_response`` raises ``HTTPException(405)`` for anything other than
    GET/HEAD **before** it ever looks for the file, so a mount answers 405 for every path it
    covers. On a ``"/"`` mount that is EVERY otherwise-unmatched path in the application.

    WHY THAT BROKE A REAL CLIENT. 405 asserts "this path exists, wrong verb". An MCP client
    probing for OAuth endpoints — RFC 7591 ``/register``, RFC 8414
    ``/.well-known/oauth-authorization-server`` — read that as "an authorization server lives
    here" and then failed parsing FastAPI's ``{"detail": ...}`` as an OAuth error object,
    which requires a string ``error`` member. Measured against production with opencode:

        HTTP 405: Invalid OAuth error response: [{ "expected": "string", "path": ["error"],
          "message": "expected string, received undefined" }]
        Raw body: {"detail":"Method Not Allowed"}

    The client never fell back to the bearer token it already held. We serve STATIC bearer
    tokens and run no authorization server, so an honest 404 is both true (the path exists
    under no verb) and the answer that lets discovery conclude "no OAuth here".

    ⚠️ RELATEDLY, DO NOT ADD an ``error`` member to any 401/404 body on this server, and do
    NOT advertise ``resource_metadata`` on the ``WWW-Authenticate`` challenge. RFC 9728 makes
    that parameter optional, and the MCP authorization spec requires a protected-resource
    metadata document to name at least one ``authorization_servers`` entry — we have none, so
    advertising it would invite exactly the flow that cannot complete. Authorization is
    OPTIONAL in MCP; static bearer is a legitimate posture.

    POSTSCRIPT (2026-11-04, port of foss 595d084c): this server sends NO WWW-Authenticate
    challenge at all — a bare challenge triggers Claude Desktop's OAuth 2.1 DCR discovery
    flow, which ignores the static bearer key and 401s every request (measured on a live
    stack). The require_auth 401 raise carries no headers; do not re-add one.

    GET/HEAD are untouched, so every mounted site serves exactly as before.
    """

    class _MethodSafeStatic(static_files_cls):
        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http" and scope.get("method") not in ("GET", "HEAD"):
                await JSONResponse(status_code=404,
                                   content={"detail": "Not Found"})(scope, receive, send)
                return
            await super().__call__(scope, receive, send)

    return _MethodSafeStatic


# ── Landing asset content-hash versioning (serve-time cache busting) ─────────
# The marketing site's assets are served with long-lived caches (the edge holds
# /landing.js for up to a day). The HTML used to reference them UNVERSIONED (or with a
# hand-bumped ``?v=N`` counter), so after a prod deploy Cloudflare kept serving the OLD
# bundle for its full TTL — hit live 2026-11-02: the new auth card was invisible to
# visitors for up to 24h because the HTML pointed at a URL the edge never re-fetched.
# THE FIX IS AT THE HTML, NOT THE HEADERS: the long asset TTLs are CORRECT once every
# URL is fingerprinted, so at serve time each local .js/.css reference in a landing
# HTML page is rewritten to ``?v=<sha256[:12] of the file's bytes>``. A redeploy that
# changes the file changes the hash, changes the URL, and busts every cache — while an
# unchanged file keeps its warm cache. On-disk HTML is never modified; the rewrite is
# pure serve-time text.

_LANDING_ASSET_VERSION_CACHE: dict = {}
_LANDING_VERSION_BYTES = 65536


def _landing_asset_version(file_path: str) -> str:
    """First 12 hex chars of sha256 over the file's BYTES — the ``?v=`` cache-bust token.

    Computed once per (file, mtime) and cached: these are static files, so no request
    ever pays for a hash. A redeploy replaces the file (new mtime) and the next lookup
    re-hashes exactly once; an unchanged file keeps serving its cached digest. Returns
    ``""`` for an unreadable file — callers then leave the reference untouched.
    """
    try:
        st = os.stat(file_path)
        cached = _LANDING_ASSET_VERSION_CACHE.get(file_path)
        if cached is not None and cached[0] == st.st_mtime_ns:
            return cached[1]
        digest = hashlib.sha256()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_LANDING_VERSION_BYTES), b""):
                digest.update(chunk)
        version = digest.hexdigest()[:12]
    except OSError:
        return ""
    _LANDING_ASSET_VERSION_CACHE[file_path] = (st.st_mtime_ns, version)
    return version


# src/href="…" with either quote style — the only attributes that load .js/.css assets.
# The lookbehind pins the match to the START of the attribute name, so a future
# ``data-src``/``xlink:href``-style attribute can never be matched mid-name; the ws1/ws2
# groups preserve any whitespace around ``=`` so the served page differs from disk by the
# ``?v=`` query and NOTHING else.
_LANDING_ASSET_REF_RE = re.compile(
    r"(?<![\w:.-])(?P<attr>src|href)(?P<ws1>\s*)=(?P<ws2>\s*)(?P<q>[\"'])(?P<url>[^\"']*)(?P=q)",
    re.IGNORECASE)

# URL prefixes that are NOT files under the landing root. ``//`` is protocol-relative.
_LANDING_EXTERNAL_PREFIXES = ("//", "data:", "mailto:", "tel:", "javascript:")


def _landing_version_asset_refs(html_text: str, landing_dir: str) -> str:
    """Rewrite every LOCAL .js/.css reference in ``html_text`` to ``path?v=<content hash>``.

    A reference qualifies when, after stripping any ``?query#fragment``, it resolves to a
    file that exists INSIDE ``landing_dir``. Everything else — absolute URLs, other
    schemes, non-landing files, assets that do not exist — passes through untouched, so
    the rewrite can only ever touch this site's own static assets. Idempotent: the base
    path is recovered by stripping the query first, so re-rewriting served HTML (or a
    page still carrying an old hand-bumped ``?v=N``) converges to the current hash.
    """
    landing_root = os.path.abspath(landing_dir)

    def _version(match: "re.Match") -> str:
        url = match.group("url").strip()
        base = url.split("?", 1)[0].split("#", 1)[0]
        if not base or not base.lower().endswith((".js", ".css")):
            return match.group(0)
        if url.lower().startswith(_LANDING_EXTERNAL_PREFIXES) or "://" in base:
            return match.group(0)
        rel = base[1:] if base.startswith("/") else base
        fs_path = os.path.normpath(os.path.join(landing_root, rel))
        if fs_path != landing_root and not fs_path.startswith(landing_root + os.sep):
            return match.group(0)          # escapes the landing root → never touch
        if not os.path.isfile(fs_path):
            return match.group(0)
        version = _landing_asset_version(fs_path)
        if not version:
            return match.group(0)
        quote = match.group("q")
        return (f'{match.group("attr")}{match.group("ws1")}={match.group("ws2")}'
                f'{quote}{base}?v={version}{quote}')

    return _LANDING_ASSET_REF_RE.sub(_version, html_text)


class _LandingVersionedHTMLResponse(Response):
    """A ``text/html`` ``Response`` that — unlike bare ``Response`` — sends NO body on HEAD
    (``Response.__call__`` always sends ``self.body``; ``FileResponse``, which this replaces
    for landing HTML, special-cases HEAD and this class must keep that contract)."""

    async def __call__(self, scope, receive, send):
        if scope.get("method") == "HEAD":
            await send({"type": "http.response.start", "status": self.status_code,
                        "headers": self.raw_headers})
            await send({"type": "http.response.body", "body": b""})
            return
        await super().__call__(scope, receive, send)


def _landing_versioned_html_response(html_path: str,
                                     landing_dir: Optional[str] = None,
                                     if_none_match: Optional[str] = None) -> Response:
    """Serve a landing HTML file with every local asset reference content-hash-versioned.

    The body differs from the file on disk ONLY by ``?v=<hash>`` on local .js/.css refs.
    The ETag is derived from the REWRITTEN bytes (not the file stat), so:
      * a validator that matches the served body 304s (the shell-revalidate no-cache
        contract still costs one conditional request, not a full re-download), and
      * an ASSET edit alone — HTML file untouched, refs re-hashed — produces a NEW etag,
        so a cached shell can never keep pointing at a stale asset URL. That is the exact
        failure the hand-bumped counter allowed: the HTML revalidated, but it validated
        against a body whose URLs never changed.
    """
    with open(html_path, "rb") as fh:
        raw = fh.read()
    try:
        html_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        html_text = raw.decode("utf-8", errors="replace")
    rewritten = _landing_version_asset_refs(
        html_text,
        landing_dir or os.path.dirname(os.path.abspath(html_path)))
    etag = f'"{hashlib.sha256(rewritten.encode("utf-8")).hexdigest()}"'
    if if_none_match and etag in if_none_match:
        not_modified = Response(status_code=304)
        not_modified.headers["ETag"] = etag
        # 304 must not restate entity headers (RFC 9110 §15.4.5) — Response() pre-set
        # content-length/type on the empty body; drop them the way Starlette's own
        # NotModifiedResponse does. (MutableHeaders has no .pop — delete by key.)
        for header in ("content-length", "content-type", "date"):
            try:
                del not_modified.headers[header]
            except KeyError:
                pass
        return not_modified
    return _LandingVersionedHTMLResponse(
        rewritten, media_type="text/html", headers={"ETag": etag})


def _landing_static_files_with_versioned_html(static_files_cls):
    """Wrap a landing ``StaticFiles`` class so every 200 text/html file it serves comes
    back content-hash-versioned (``_landing_versioned_html_response``). Resolution, 404s
    and redirects are the superclass's; only successful HTML files are re-served."""

    class _VersionedHTMLStatic(static_files_cls):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            target = getattr(response, "path", None)   # FileResponse carries its file
            if (response.status_code == 200 and target is not None
                    and str(target).lower().endswith(".html")):
                if_none_match = None
                for key, value in scope.get("headers") or ():
                    if key == b"if-none-match":
                        if_none_match = value.decode("latin-1")
                        break
                # The SITE ROOT is the MOUNT root — not the file's own directory — so a
                # page in a subdirectory (errors/*.html) still resolves its root-level
                # asset refs (landing.css) exactly the way the browser does.
                return _landing_versioned_html_response(
                    os.fspath(target),
                    landing_dir=getattr(self, "directory", None),
                    if_none_match=if_none_match)
            return response

    return _VersionedHTMLStatic
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import src.mcp.server as _mcp
import src.mcp.premise as _premise   # THE PREMISE — one source, every door (transport parity)
# CLIENT IDENTITY (owner ruling 2026-08-15: a LABEL, never a gate): the transport edge
# captures the client name into a ContextVar for write-path traceability + cortex use.
# Nothing gates capability on it — auth is the only gate.
from src.mcp import client_class as _client_class


def _capture_client_class(request: Request) -> None:
    """Record WHICH CLIENT is talking (a label), ONCE, at the transport edge.

    Identity resolution order: the MCP-spec ``mcp-name`` header first, else the
    ``User-Agent`` leading token before '/' (so "opencode/1.18" is labeled
    "opencode" while browsers "Mozilla/5.0 …" and OpenWebUI fall to the chat
    default). Unknown/empty → unset → chat label. NEVER blocks, fails, or
    changes the capability of the request (owner ruling 2026-08-15: an
    authorized bearer gets the full toolset identically whatever its name).
    """
    try:
        _name = (request.headers.get("mcp-name") or "").strip()
        if not _name:
            _name = (request.headers.get("user-agent") or "").split("/", 1)[0].strip()
        _client_class.set_client_class(_name or None)
    except Exception:
        # Capture must never take a request down with it.
        _client_class.set_client_class(None)
# Single source of truth for the passive (tool-description) operating-rules surface — the
# SAME seed rows the active HOW YOU OPERATE recall block draws from, so they can't drift.
# `tool_description_suffix` returns "" when the capability is off (open-core / SAAS-off), so
# descriptions are byte-for-byte today's; when on it folds in the tool's standing rule(s).
from src.api.cortex_usage_seed import tool_description_suffix
from src.api import errors as _errors  # THE ONE ERROR SEAM — no exception text in a body


# ── OpenWebUI OpenAPI tool request/response models ────────────────────────────

class RecallRequest(BaseModel):
    query: str = Field(..., description="The human user's current message copied VERBATIM and in full — do NOT summarize, shorten, or reduce it to a keyword or topic. Keep every word, especially 'not', 'no', 'now', 'actually', 'instead' and any names/values. The backend extracts the search topic AND decides intent (recall vs correction) from the whole sentence itself; a reduced query strips the meaning. Never pass your own working text — operational queries belong to cortex_recall. Required — never leave empty.")
    user_id: str = ""


class RememberRequest(BaseModel):
    text: str = Field(..., description="A HUMAN being's message, copied VERBATIM and in full — the raw sentence(s) exactly as they said them. Do NOT pre-extract, summarize, rephrase, or restructure it into facts, triples, or bullet/line items: FaultLine's engine does ALL extraction, typing, and structuring — it needs the raw words. Keep every word, especially 'not'/'no'/'now'/'actually'/'instead' and any names, values, and dates. NEVER pass your own working text (briefs, plans, analysis, output) — what you learn while working goes to cortex_remember. Required — never leave empty.")
    user_id: str = ""


class IngestDocumentRequest(BaseModel):
    text: str = Field(..., description="The FULL text of a document a HUMAN shared — article, PDF extraction, or long-form note — copied VERBATIM; do NOT summarize, shorten, or pre-extract facts from it. FaultLine chunks and extracts everything itself; it needs the raw text. Never ingest your own working text. Required — never leave empty.")
    source_ref: str = Field("", description="OPTIONAL: where this document came from — a URL, filename, or citation string (e.g. 'https://example.com/article', 'meeting-notes-2026-06.pdf'). Stored with every fact extracted from the document so recall can cite its source.")
    title: str = Field("", description="OPTIONAL: the document's title. Used as the source reference when source_ref is not provided.")
    user_id: str = ""


class RetractRequest(BaseModel):
    text: str
    user_id: str = ""


class ForgetRequest(BaseModel):
    subject: str = Field(..., description="WHOSE fact to forget — the named subject of the single fact the user explicitly asked you to forget (e.g. their own name via 'me'/'I', or a specific named person/thing). Required — a forget MUST name exactly one target; never a broad/everything wipe.")
    rel_type: Optional[str] = Field(None, description="OPTIONAL: the relationship of the specific fact to forget (e.g. occupation, has_pet, has_email). Narrows the forget to one fact about the subject. Omit only when the subject identifies a single fact unambiguously.")
    old_value: Optional[str] = Field(None, description="OPTIONAL: the specific value/object of the fact to forget (e.g. the email address, the pet's name). REQUIRED in practice: without it a subject or relation alone would sweep every matching fact, which this tool refuses.")
    user_id: str = ""


class LearnRequest(BaseModel):
    text: str
    user_id: str = ""


class CortexRememberRequest(BaseModel):
    text: str = Field(..., description="THE RULE, in your own words: when <situation> arises, do <action>, because <mechanism>. Lead with the rule — never with their words, output, or a transcript; a note you write yourself cannot carry their payload. This is YOUR private working note (like your own .claude notes); it is NOT saved as the user's fact. If their exact wording matters, put it in owner_quote; call remember_facts for anything that is a fact about them. Stance: 'my operational note,' never 'you told me.' Required — never leave empty.")
    category: str = Field("", description="What KIND of lesson: gotcha | failed_command | pitfall | correction | howto | overrun. Defaults to 'gotcha' if omitted.")
    tags: list[str] = Field(default_factory=list, description="The triggers/topics this note guards for just-in-time recall (e.g. ['docker','deploy'], ['git','tests'], ['llm-endpoint','config']).")
    evidence: dict | None = Field(None, description="OPTIONAL proof for this lesson — bind the claim to the artifact that produced it: {\"sha\": \"<git commit you checked it against>\", \"ref\": \"<bug/test id>\", \"command\": \"<what you ran>\", \"output\": \"<the actual result>\"}. A note is a CLAIM, and a claim with no proof rots into a trap: a future you re-chases work that was already done. With a `sha`, staleness becomes DECIDABLE (git merge-base --is-ancestor <sha> HEAD), and the note ranks ABOVE unevidenced ones at recall. Record it whenever you can.")
    client_source: str = Field("", description="OPTIONAL: identify which CLIENT/TOOL is writing this note (e.g. 'claude-code', 'opencode'). This is the client, NOT the AI model — self-tag your model via `model`. Auto-stamped from the connection (mcp-name header / User-Agent) when omitted; explicit wins. Lets a multi-model seat tell its own notes apart.")
    owner_quote: str = Field("", description="OPTIONAL: their exact words, as a short quoted fragment — carried SECOND, behind the rule, never as the note body. Include only when the wording itself matters (a name, a value, a threshold); skip it otherwise.")
    env: str = Field("", description="OPTIONAL: the ENVIRONMENT this lesson is true in (e.g. 'truenas', 'p70', 'local'). Same-env notes rank first when you state your env at recall; other-env notes demote slightly and are STAMPED — nothing is ever hidden.")
    branch: str = Field("", description="OPTIONAL: the branch or line of work this lesson was learned on (e.g. 'saas', 'main', a matter number). Notes learned on the branch you name at recall rank above equally-relevant others — it never hides anything.")
    # SCHEMA UPLIFT provenance (owner directive; migration 301) — free strings, bounded 64,
    # never enums. Same affordance as the MCP-native door (tools.py): a model may self-tag;
    # the deployment may default (CORTEX_AGENT_MODEL / CORTEX_PROJECT); explicit always wins.
    model: str = Field("", description="OPTIONAL: the MODEL writing this note (e.g. 'qwen3.5-9b', 'glm-5.3'). Auto-stamped from the deployment (CORTEX_AGENT_MODEL) when omitted — pass it only to override. Rendered as provenance (model=<x>) at recall so a later reader can see WHO learned the lesson; never a filter.")
    type: str = Field("", description="OPTIONAL: FREE-FORM owner-facing type of this note ('code', 'bug', 'operational', anything — loose by design, no vocabulary; emoji/hyphens/spaces fine). Complements, never replaces, the fixed engine `category` enum.")
    project: str = Field("", description="OPTIONAL: the PROJECT this note relates to (e.g. 'faultline', a repo name). A deployment default (CORTEX_PROJECT) is stamped when omitted; explicit wins. Rendered as provenance at recall.")
    user_id: str = ""


class CortexRecallRequest(BaseModel):
    query: str = Field("", description="Describe the situation or task at hand in YOUR OWN words — keywords are fine; do not paste the user's message. Deterministic keyword + tag match. May be empty to list your most-relevant recent notes. These are YOUR private operational notes — not the user's facts.")
    user_id: str = ""
    # Unattended callers (an automatic per-turn client hook) pass false: reading the cortex
    # re-ranks it (last_seen_at drives the candidate window), so a lane that fires on every
    # turn and bumps slowly eats its own tail. A model asking deliberately keeps the default.
    # NOT advertised in the hand-built tool OpenAPI — this is a client-hook affordance, not a
    # knob a model should be reasoning about.
    bump: bool = True
    # Branch ranking signal — same affordance as the MCP tool's branch param. Ranking-only.
    branch: str = Field("", description="OPTIONAL: the branch or line of work you are on right now. Notes learned on that branch rank above equally-relevant others — it never hides anything.")
    # Env ranking signal — same affordance as the MCP tool's env param. Ranking-only.
    env: str = Field("", description="OPTIONAL: the ENVIRONMENT you are operating in right now (e.g. 'truenas', 'p70', 'local'). Env-matching notes rank first; env-mismatched ones demote slightly and render stamped so you see they may not apply here.")
    # "mine" lane affordance — same as the MCP `scope` param, now ADVERTISED on the hand-built
    # tool OpenAPI too (door parity: an OpenWebUI-connected model that knows its own identity
    # can self-scope without the MCP layer).
    client_source: str = Field("", description="OPTIONAL: when set, restrict results to notes tagged with this client PLUS untagged notes (untagged stay visible to everyone). Leave empty for all notes from the seat.")

# If set, all POST /mcp requests must present: Authorization: Bearer <MCP_API_KEY>
MCP_API_KEY = os.environ.get("MCP_API_KEY", "").strip()

# Comma-separated browser-origin allowlist for CORS. Default empty → no cross-origin
# browser access. The OpenWebUI → :8002 tool call is server-to-server and is NOT
# browser CORS-gated, so an empty allowlist does not break the live path. Operators
# set MCP_ALLOWED_ORIGINS=https://<openwebui-host> only if browser-origin access is needed.
MCP_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
]


# ── Security response headers (FOSS CORE — hardens the public MCP transport) ───
# Additive, safe-by-default HTTP security headers on every response. This is a
# TRANSPORT hardening control (SOC 2 CC6.6/CC6.7 boundary protection) and applies to
# the public core exactly as it does to the SaaS deployment — it is NOT SaaS-specific.
#
# It only SETS response headers; it never changes request handling or a response body,
# so it is byte-safe for the API/MCP JSON path. Fully reversible + configurable:
#   MCP_SECURITY_HEADERS=0            → disable the whole middleware (legacy behavior)
#   MCP_HSTS_MAX_AGE=<seconds>        → HSTS max-age; 0 disables the HSTS header
#                                       (only meaningful over TLS; browsers ignore it on http)
#   MCP_CSP_API=<policy>              → CSP for non-HTML (JSON API/MCP) responses
#   MCP_CSP_HTML=<policy>             → CSP for text/html (the dashboard) responses
#   MCP_FRAME_OPTIONS / MCP_REFERRER_POLICY → override those single headers
# Existing headers set by a handler are never clobbered (setdefault semantics).
def _sec_truthy(value: str | None, default: bool) -> bool:
    v = str(value if value is not None else "").strip().lower()
    if v == "":
        return default
    return v not in ("0", "false", "no", "off")


SECURITY_HEADERS_ENABLED = _sec_truthy(os.environ.get("MCP_SECURITY_HEADERS"), True)

# round 13: through the ONE env parse core (loud on garbage / non-finite / below floor, never raises)
_HSTS_MAX_AGE = _mcp._env_int("MCP_HSTS_MAX_AGE", 63072000)  # 2y default; floor/ceiling: server._ENV_LIMITS

_FRAME_OPTIONS = os.environ.get("MCP_FRAME_OPTIONS", "DENY").strip() or "DENY"
_REFERRER_POLICY = os.environ.get("MCP_REFERRER_POLICY", "no-referrer").strip() or "no-referrer"

# API responses (JSON) load no resources — lock everything down.
_CSP_API = os.environ.get(
    "MCP_CSP_API", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
).strip()
# HTML (the SaaS dashboard) needs self scripts/styles + Google Fonts + inline style attrs.
# Scripts are external (/dashboard/app.js) so script-src stays 'self' (no unsafe-inline).
_CSP_HTML = os.environ.get(
    "MCP_CSP_HTML",
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
).strip()


def _apply_security_headers(response) -> None:
    """Set the safe-default security headers on a response (never clobbers existing).

    Split out (pure) so it is unit-testable without a live ASGI stack.
    """
    if not SECURITY_HEADERS_ENABLED:
        return
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", _FRAME_OPTIONS)
    h.setdefault("Referrer-Policy", _REFERRER_POLICY)
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if _HSTS_MAX_AGE > 0:
        h.setdefault(
            "Strict-Transport-Security",
            f"max-age={_HSTS_MAX_AGE}; includeSubDomains",
        )
    if "Content-Security-Policy" not in h:
        # A 304 Not Modified carries NO content-type, so the ctype-based selection below would
        # fall to the restrictive _CSP_API ("default-src 'none'"). On revalidation the browser
        # updates the cached document's stored headers with the 304's — so a warm-cache reload of
        # an HTML page (the dashboard forces "no-cache, must-revalidate" → a conditional request
        # every load) would replace the good _CSP_HTML with default-src 'none', blocking the
        # page's own stylesheets AND scripts and rendering it unstyled/dead. INTERMITTENT because
        # a cold cache serves 200+_CSP_HTML (styled) while a warm cache serves 304 (broken).
        # Leave CSP untouched on a 304 so the browser keeps the policy from the original 200.
        if getattr(response, "status_code", 200) != 304:
            ctype = h.get("content-type", "")
            policy = _CSP_HTML if ctype.startswith("text/html") else _CSP_API
            if policy:
                h["Content-Security-Policy"] = policy


# ── Host-based surface gating (SaaS two-face split) ───────────────────────────
# One MCP instance can serve two single-purpose PUBLIC faces behind a reverse proxy
# (e.g. NPM). Each face exposes ONLY its own surface; the other surface 404s so the
# gated face is indistinguishable from "no such route". A host in NEITHER list is
# "internal" (LAN / direct IP:8002 / unset config) → NO gating (today's behavior).
#
# Config (env, comma-separated, case-insensitive, any :port stripped; NO baked-in
# real domains — unset ⇒ every host is internal ⇒ the gate is a no-op):
#   SAAS_ADMIN_HOSTS   → hosts classified as the ADMIN/operator (god-mode) face
#   SAAS_TENANT_HOSTS  → hosts classified as the TENANT face (+ MCP client surface)
#
# SECURE by default + FAIL-SAFE: a listed public host is honored (never silently
# downgraded to "internal" so god-mode could leak); a classify error → "internal".
# A host present in BOTH lists resolves to TENANT (the safer, non-god-mode face).
# The two parsers are LIFTED into ``src/mcp/host_rules.py`` (2026-09-14, gauntlet
# checkout-origin-authority) so ``saas/tenant_checkout_routes.py::_request_origin`` gates the
# Stripe return origin on the SAME allowlist through the SAME code — one parser, imported twice,
# never copy-pasted. The private names are kept bound here because the conftest ``saas_host_faces``
# fixture and the face-gate tests reach them as ``h._parse_host_list`` / ``h._strip_host_port``.
from src.mcp import host_rules as _host_rules  # noqa: E402
from src.mcp.host_rules import parse_host_list as _parse_host_list  # noqa: E402
from src.mcp.host_rules import strip_host_port as _strip_host_port  # noqa: E402


SAAS_ADMIN_HOSTS = _parse_host_list(os.environ.get("SAAS_ADMIN_HOSTS"))
SAAS_TENANT_HOSTS = _parse_host_list(os.environ.get("SAAS_TENANT_HOSTS"))
# Volenti (the PARENT company, volenti.ca) — a single-purpose CORPORATE face. Unset ⇒
# no host classifies as volenti ⇒ pure no-op (the page is still reachable at
# /volenti.html on the public/internal faces). When set, that host roots to the Volenti
# corporate site and 404s every product/console/API surface (see _gate_blocks).
SAAS_VOLENTI_HOSTS = _parse_host_list(os.environ.get("SAAS_VOLENTI_HOSTS"))


def _derive_tenant_base_domains() -> frozenset[str]:
    """Registrable base domain(s) whose *.subdomains are the per-tenant scoped surface.

    Explicit override via SAAS_TENANT_BASE_DOMAINS (comma-separated), UNIONed with the base
    (last two labels) of each configured tenant host — so `app.faultline.ca` / `mcp.faultline.ca`
    auto-derive `faultline.ca` and NO extra config is needed. Any host that is one of these bases
    or a subdomain of it (and isn't an explicitly-classified volenti/admin/tenant host) is the
    TENANT face. WITHOUT this an arbitrary `*.faultline.ca` label falls through to "internal"
    (gate no-op) and would serve the god-mode operator console the moment a wildcard DNS record
    exists — a subdomain-takeover / console-exposure landmine. Empty when SAAS is unconfigured
    (fail-safe: behavior unchanged).
    """
    # Derivation LIFTED to ``src.mcp.host_rules.derive_tenant_base_domains`` (round 3 of gauntlet
    # checkout-origin-authority) so the Stripe return-origin check honours the same suffix rule;
    # this wrapper keeps reading the module-level SAAS_TENANT_HOSTS so the conftest fixture's
    # monkeypatch-then-rederive contract is unchanged.
    return _host_rules.derive_tenant_base_domains(
        SAAS_TENANT_HOSTS, os.environ.get("SAAS_TENANT_BASE_DOMAINS"))


SAAS_TENANT_BASE_DOMAINS = _derive_tenant_base_domains()


def _trusted_proxy_nets() -> list:
    """Parse SAAS_TRUSTED_PROXY_IPS (comma-separated IPs/CIDRs) into ip_network objects.

    These are the reverse-proxy hops (Caddy / NPM / docker gateway) permitted to SET
    ``X-Forwarded-*``. Empty (unset) → today's behavior: the forwarded header is honored from
    any peer (the audit notes prod is already mitigated by Caddy pinning `X-Forwarded-Host`).
    Set it to lock forwarded-header trust to the real proxy so a direct client cannot spoof
    the served face (audit MED #8)."""
    import ipaddress
    raw = os.environ.get("SAAS_TRUSTED_PROXY_IPS", "")
    nets = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            nets.append(ipaddress.ip_network(tok, strict=False))
        except Exception:
            continue
    return nets


def _peer_is_trusted_proxy(request: "Request") -> bool:
    """True iff the immediate peer is in SAAS_TRUSTED_PROXY_IPS (so its X-Forwarded-* is trusted).

    Unset list → True (behavior unchanged, honor the header). Configured list → only the listed
    proxy IPs are trusted; a direct client (any other peer) is not, so it cannot spoof the face."""
    nets = _trusted_proxy_nets()
    if not nets:
        return True  # not configured → preserve today's behavior (honor forwarded headers)
    try:
        import ipaddress
        peer = getattr(getattr(request, "client", None), "host", None)
        if not peer:
            return False
        ip = ipaddress.ip_address(peer)
        return any(ip in net for net in nets)
    except Exception:
        return False  # cannot evaluate the peer → do not trust a forwarded header


def _effective_host(request: "Request") -> str:
    """Resolve the caller-facing host: X-Forwarded-Host (proxy) first, else Host.

    ``X-Forwarded-Host`` is honored ONLY when the immediate peer is a trusted proxy (see
    ``_peer_is_trusted_proxy`` / SAAS_TRUSTED_PROXY_IPS) — otherwise it is ignored and the real
    ``Host`` header is used, so a direct client cannot spoof which face is served (audit MED #8).
    When SAAS_TRUSTED_PROXY_IPS is unset the header is honored from any peer (unchanged)."""
    xfh = request.headers.get("x-forwarded-host") or ""
    if xfh and _peer_is_trusted_proxy(request):
        raw = xfh.split(",")[0]
    else:
        raw = request.headers.get("host") or ""
    return _strip_host_port(raw)


def _classify_face(request: "Request") -> str:
    """Classify the request's face → "volenti" | "admin" | "tenant" | "internal".

    Volenti (the corporate parent face) is checked FIRST — it is the most restricted
    surface (serves ONLY the Volenti site) so a host in multiple lists resolves to the
    safest face. Tenant is checked before admin so a host misconfigured into both
    resolves to the safer (non-god-mode) face. Any error → "internal" (fail-safe: never
    over-gate on a parse fault; a genuinely-listed public host classifies deterministically).
    """
    try:
        host = _effective_host(request)
        if not host:
            return "internal"
        if host in SAAS_VOLENTI_HOSTS:
            return "volenti"
        if host in SAAS_TENANT_HOSTS:
            return "tenant"
        if host in SAAS_ADMIN_HOSTS:
            return "admin"
        # Per-tenant scoped subdomain surface: any *.<tenant-base> not explicitly classified
        # above is the TENANT face (serves tenant content, 404s the operator console). This
        # closes the landmine where an arbitrary subdomain fell through to "internal" and would
        # expose the god-mode console once wildcard DNS exists. Admin (a .ts.net MagicDNS host)
        # and volenti (a different registrable domain) are matched exactly above, so they are
        # never re-captured here.
        for base in SAAS_TENANT_BASE_DOMAINS:
            if host == base or host.endswith("." + base):
                return "tenant"
    except Exception:  # pragma: no cover - defensive
        return "internal"
    return "internal"


# Shared surface — ALWAYS allowed on every face.
_GATE_ALWAYS_ALLOW_EXACT = frozenset({
    "/health",
    "/security.txt",
    "/openapi.json",
    "/tools/openapi.json",   # OpenWebUI tool-server spec (minimal 3.0.3, 8 tools) — served on every face
    "/dashboard/style.css",
    "/dashboard/favicon.svg",
})
_GATE_ALWAYS_ALLOW_PREFIX = ("/.well-known/",)

# TENANT/MCP client REST shorthands (mounted for OpenWebUI) + the MCP endpoint.
# god-mode (admin) face 404s ALL of these.
_TENANT_MCP_EXACT = frozenset({
    "/mcp",
    "/recall_memory",
    "/remember_facts",
    "/ingest_document",
    # The BINARY door. Must be listed here or it would be served on the god-mode (admin)
    # face as well as the tenant one — a byte-accepting endpoint is the last thing that
    # should leak onto an extra surface.
    "/ingest_file",
    "/learn_facts",
    "/retract_fact",
    "/forget_fact",
    # The ENGINE-STRUCTURE door. A tenant/MCP client tool like the rest, so it must be listed
    # here or it is also served on the operator (admin) face.
    "/review_structure",
    # The CONSTRAINED customer-agent lane (SAAS only). Served on the tenant/MCP faces; 404'd
    # on the operator (admin) face like every other MCP client tool.
    "/customer_chat",
})

# Operator (god-mode) console assets — tenant face 404s these. Includes the index
# under every way StaticFiles(html=True) can serve it, so the operator console can
# never leak on the tenant face.
_OPERATOR_CONSOLE_EXACT = frozenset({
    "/dashboard",
    "/dashboard/",
    "/dashboard/index.html",
    "/dashboard/app.js",
    # Benchmark results dashboard — operator-only (data routes are require_operator-gated;
    # the page + its JS live behind the operator face too, like index.html/app.js).
    "/dashboard/bench.html",
    "/dashboard/bench_dash.js",
    "/dashboard/data/lme_results.json",
    "/dashboard/data/test_runs.json",
    # cycle_events.json powers the Cycle Monitor panel. It was PUBLISHED but never SERVED —
    # omitted from both this gate list and _BENCH_DATA_FILES below — so the panel read "no cycle
    # events yet" indefinitely while the emitter wrote a healthy file. Keep all three in sync.
    "/dashboard/data/cycle_events.json",
    # ── ADDED 2026-08-13 (drift audit) ────────────────────────────────────────────────
    # The OPERATOR BILLING DASHBOARD. Both files were reachable at
    # https://app.faultline.ca/dashboard/billing.html — the CUSTOMER face — because
    # StaticFiles serves ANY file under saas/dashboard/ and this frozenset is the only thing
    # that hides an operator page from the tenant face. The DATA is safe (every /admin/*
    # route 404s on that face), but ~35 KB of operator JavaScript — internal route names,
    # panel structure, the shape of the whole billing control plane — was public. Found by
    # ENUMERATING THE FILESYSTEM against this allowlist rather than reading the allowlist;
    # see test_every_dashboard_and_landing_file_is_classified_by_the_face_gate, which now
    # fails if any file on disk is neither listed here nor declared deliberately shared.
    "/dashboard/billing.html",
    "/dashboard/billing.js",
})

# Tenant console assets — admin face 404s these. Includes the individual ("My Memory")
# console, which is a product-facing (non-operator) surface served alongside the tenant
# console on the tenant face and hidden from the operator/admin face.
_TENANT_CONSOLE_EXACT = frozenset({
    "/dashboard/tenant.html",
    "/dashboard/tenant.js",
    "/dashboard/user.html",
    "/dashboard/user.js",
    # The self-serve subscription page (ToS + signature + Stripe Checkout). StaticFiles serves
    # ANY file dropped into saas/dashboard/ with no per-file registration, so a new tenant page
    # is exposed on the operator face until it is listed HERE — the same footgun that would
    # otherwise leak every future console page. Its API (/tenant/subscribe et al.) is already
    # covered by the `_under(path, "/tenant")` rule below.
    "/dashboard/subscribe.html",
    "/dashboard/subscribe.js",
    # The brain-health bar (left column of the tenant console). Same reason as above: it is a
    # tenant-console asset, so it is hidden on the operator face.
    "/dashboard/brain-health.js",
    # The signed-agreement archive — the consumer for section 8's "you can obtain a complete copy
    # at any time", which had none. Tenant-only for the same reason as every entry above: it
    # renders a customer's own signed contracts.
    "/dashboard/agreements.html",
    "/dashboard/agreements.js",
})

# Public marketing site (saas/landing/*) + the "Customer access" login chooser. These
# are the PUBLIC front door — served on the tenant + internal faces and 404'd on the
# admin (god-mode / operator) face, which stays operator-only. The bare root "/" is
# NOT listed here: on the admin face "/" resolves to the operator console via
# _root_by_face, while on tenant/internal it resolves to the marketing splash.
_LANDING_EXACT = frozenset({
    "/index.html",
    # The marketing wordmark. Referenced ONLY by saas/landing/index.html — neither
    # volenti.html nor legal.html uses it, so it is landing-face, not shared.
    "/logo.svg",
    "/access.html",
    "/signup.html",
    "/signup.js",
    "/quick.html",
    "/tour.html",
    "/business.html",
    "/geek.html",
    "/numbers.html",
    "/research.html",
    "/news.html",
    "/landing.css",
    "/landing.js",
    "/research.css",
    "/research.js",
    "/volenti.html",
    # ── ADDED 2026-08-13 (drift audit) ────────────────────────────────────────────────
    # 13 marketing files existed on disk and were absent from this frozenset, so they were
    # served on the ADMIN (operator) face — the one face that is supposed to carry nothing
    # but the operator console. The content is PUBLIC, so the impact is low; the DRIFT is
    # the finding. Every one of these arrived after the frozenset was written and nothing
    # forced anybody to register them. That is now enforced by
    # test_every_dashboard_and_landing_file_is_classified_by_the_face_gate.
    "/applications.html",
    "/benchmark.js",
    "/chat-agent.css",
    "/chat-agent.html",
    "/ecosystem.css",
    "/ecosystem.html",
    "/security.html",
    "/signup-team.html",
    "/signup-team.js",
    "/wiki.html",
    "/wiki-longmemeval.html",
    # ── ADDED 2026-11-12 (drift audit round 2 — the consensus/parity deploy) ─────────
    # llms.txt + AI-INGEST.md: the AI-crawler indexes. cortex.html: the cortex page.
    # benchmark.json + the diagrams: the numbers page data. The IndexNow key: the
    # search-engine ping key. All public, landing-face.
    "/AI-INGEST.md",
    "/AI-INGEST.md",
    # ── ADDED 2026-11-12 (the news blog — the MD-driven displayer) ─────────────────────
    "/news/index.html",
    "/news/posts/manifest.json",
    "/news/posts/2026-11-12-the-overhaul.md",
    "/news/news.js",
    "/cortex.html",
    "/data/benchmark.json",
    "/diagrams/how-it-works.mmd",
    "/diagrams/ingest-query.mmd",
    "/llms.txt",
    "/indexnow-772ec3887509e3987dcf8c7026ceec74.key.txt",
    "/BingSiteAuth.xml",
    # The terms summary (2026-08-13): the site's only Terms link target. No page-specific
    # script — it loads landing.js like every other static page here.
    "/terms.html",
    # Investors page: the honest-inventory page linked from the landing footer. Static,
    # self-contained, no page-specific script — same doc pattern as terms.html.
    "/investors.html",
    # Founder portrait used by investors.html ("Who's behind it" card).
    "/founder.jpg",
})
# ``/shared/`` holds the wiki shell (CSS + JS) used by wiki.html / wiki-longmemeval.html;
# ``/data/`` holds the landing site's own benchmark JSON (distinct from /dashboard/data/).
# Both are landing assets and belong off the admin face with the pages that load them.
_LANDING_PREFIX = ("/diagrams/", "/shared/", "/data/")

# ══════════════════════════════════════════════════════════════════════════════════════
# DELIBERATELY SHARED — served on EVERY face, listed in NO per-face allowlist
# ══════════════════════════════════════════════════════════════════════════════════════
# 🔴 A GENUINELY SHARED ASSET MUST NOT BE ADDED TO EITHER FACE'S FROZENSET. Those sets are
# DENY-lists for the OTHER face, so listing a shared script in one of them 404s it on the
# console that needs it. ``style.css`` / ``feature_copy.js`` / ``tour.js`` / ``alerts.js`` /
# ``status_banner.js`` are the standing precedent: all render on both consoles.
#
# This tuple exists so the drift test can tell "shared BY DECISION" apart from "nobody ever
# registered it" — which are byte-identical in the gate's behaviour and were, until now,
# indistinguishable to a reviewer. Adding a path here is a DECISION somebody has to type.
_FACE_SHARED_BY_DESIGN = frozenset({
    # ── ADDED 2026-11-12: the site META/ICON files — every page head links them, all faces.
    "/BingSiteAuth.xml",
    "/apple-touch-icon.png",
    "/favicon-96.png",
    "/og-image.png",
    "/favicon.ico",
    # The Ontario internet-agreement supplier disclosure (name, business address, telephone,
    # O. Reg. 17/05 s.32). HOSTED on volenti.ca (see _VOLENTI_ALLOW_EXACT) but genuinely needed
    # on the product face as well: saas/dashboard/subscribe.html links to it as the durable copy
    # of the three facts it also states inline. Gating it to one face would break that link at
    # the exact moment a consumer is deciding whether to accept.
    "/legal.html",
    # Shared dashboard chrome + scripts, loaded by BOTH consoles.
    "/dashboard/style.css", "/dashboard/favicon.svg", "/dashboard/faultline_logo.svg",
    "/dashboard/alerts.js", "/dashboard/feature_copy.js", "/dashboard/tour.js",
    "/dashboard/onboarding.js", "/dashboard/status_banner.js", "/dashboard/offline.html",
    # endpoint_preview.js: self-wiring endpoint-URL preview, loaded by BOTH the tenant and
    # individual engine-brain forms (tenant.html + user.html) — pinned by
    # test_endpoint_preview_parity::test_dashboard_js_has_no_inline_script_or_handlers.
    "/dashboard/endpoint_preview.js",
    # The PUBLIC benchmark leaderboard: linked from the marketing site
    # (saas/landing/index.html → app.faultline.ca/dashboard/leaderboard.html) as well as
    # from both consoles. Gating it to either face would break a published public link.
    "/dashboard/leaderboard.html", "/dashboard/leaderboard.js",
    # The help wiki + printable/PDF build and the connection guides: product documentation,
    # reachable from every console.
    "/dashboard/help/index.html", "/dashboard/help/help.js", "/dashboard/help/_print.html",
    "/dashboard/help/faultline-help.pdf",
    "/dashboard/guides/faultline-guide-api-key.pdf",
    "/dashboard/guides/faultline-guide-claude-desktop.pdf",
    "/dashboard/guides/faultline-guide-generic.pdf",
    "/dashboard/guides/faultline-guide-openwebui.pdf",
    # Web fonts, self-hosted (the CSP forbids a CDN), needed by every page on every face.
    "/dashboard/fonts/LICENSE-JetBrainsMono.txt",
    "/dashboard/fonts/jetbrains-mono-latin-300-normal.woff2",
    "/dashboard/fonts/jetbrains-mono-latin-400-normal.woff2",
    "/dashboard/fonts/jetbrains-mono-latin-500-normal.woff2",
    "/fonts/LICENSE-JetBrainsMono.txt",
    "/fonts/jetbrains-mono-latin-300-normal.woff2",
    "/fonts/jetbrains-mono-latin-400-normal.woff2",
    "/fonts/jetbrains-mono-latin-500-normal.woff2",
    "/favicon.svg",
    # Error pages: rendered on whichever face the error happened on.
    "/errors/403.html", "/errors/404.html", "/errors/50x.html",
    # Crawler/standards protocol files. Harmless on any face, and robots.txt in particular
    # must answer on every hostname that resolves here.
    "/robots.txt", "/sitemap.xml", "/humans.txt",
    "/.well-known/security.txt",
    # BUILD SCRIPTS that live beside their output. They are source, not a served surface;
    # they are listed so the drift test does not report them as unclassified, and flagged
    # here so a reviewer can see that we KNOW they are readable.
    # ⚠️ OWNER CALL, REPORTED NOT CHANGED: these three ship inside the static directory and
    # are therefore downloadable on every face. Nothing in them is a credential (checked),
    # but they do not belong in a public asset root. Moving them is a build-layout change.
    "/dashboard/build_review.py", "/dashboard/guides/build_guides.py",
    "/dashboard/help/build_help.py",
    # ⚠️ ORPHAN, REPORTED NOT REMOVED. /dashboard/data/benchmark.json has NO reader: the
    # landing chart reads /data/benchmark.json (saas/landing/benchmark.js) and the console
    # leaderboard reads the live /leaderboard/data route. It is a stale copy under the
    # bench-data volume; deleting it is a data-volume decision, not a gating one.
    "/dashboard/data/benchmark.json",
})

# The ONLY paths the corporate Volenti face serves — its own site + the shared
# always-allow set (health/security.txt, resolved earlier). Everything else 404s so
# volenti.ca is a clean single-purpose parent-company domain (no product/console/API).
_VOLENTI_ALLOW_EXACT = frozenset({"/", "/volenti.html",
                                   # The Ontario internet-agreement disclosure (supplier
                                   # name, business address, telephone). O. Reg. 17/05 s.32
                                   # requires it BEFORE a consumer accepts, so the same
                                   # three facts are ALSO inline on the subscribe page —
                                   # this page is the durable copy, not the only copy.
                                   "/legal.html"})


def _under(path: str, prefix: str) -> bool:
    """True iff path is the router prefix itself or a child of it (no /adminX match)."""
    return path == prefix or path.startswith(prefix + "/")


def _gate_blocks(face: str, path: str) -> bool:
    """Return True iff `path` must 404 on this `face`. Internal → never blocks."""
    if face == "internal":
        return False
    if path in _GATE_ALWAYS_ALLOW_EXACT:
        return False
    for pfx in _GATE_ALWAYS_ALLOW_PREFIX:
        if path.startswith(pfx):
            return False
    if face == "volenti":
        # Corporate parent face (volenti.ca): ALLOW-LIST — serve ONLY the Volenti site
        # (+ the always-allow set handled above). Every FaultLine product surface — the
        # marketing pages, /mcp, /v1, /tenant, /admin, /me, /dashboard, the tool REST
        # endpoints — 404s so the parent-company domain never exposes the product plane.
        return path not in _VOLENTI_ALLOW_EXACT
    if face == "tenant":
        # Hide the operator console + the /admin and /operator control-plane routers.
        if path in _OPERATOR_CONSOLE_EXACT:
            # EXCEPT the bare path (2026-10-21): _dashboard_bare_by_face resolves it to a 302
            # to /access.html — a pointer to the customer door, zero operator content. Without
            # this a tenant typing /dashboard eats a bare 404 (the owner did exactly that).
            if path not in ("/dashboard", "/dashboard/"):
                return True
        if _under(path, "/admin") or _under(path, "/operator"):
            return True
        return False
    if face == "admin":
        # Hide the tenant console, the /tenant router, and the MCP client surface.
        if path in _TENANT_CONSOLE_EXACT:
            return True
        if path in _TENANT_MCP_EXACT:
            return True
        if _under(path, "/tenant"):
            return True
        # Hide the public developer API (/v1/*) — a tenant/API-client surface, like /mcp.
        if _under(path, "/v1"):
            return True
        # Hide the public marketing site + the "Customer access" chooser (operator-only
        # face); "/" stays the operator console (resolved in _root_by_face, not listed here).
        if path in _LANDING_EXACT:
            return True
        for pfx in _LANDING_PREFIX:
            if path.startswith(pfx):
                return True
        # Hide the individual ("My Memory") self-serve realm — a tenant-face product surface.
        if _under(path, "/me"):
            return True
        return False
    return False  # unknown face → fail-safe open (never over-gate)


def _gated_not_found(path: str) -> JSONResponse:
    """A 404 byte-identical to FastAPI's default (body + headers) so the gated
    surface is indistinguishable from a route that does not exist."""
    resp = JSONResponse(status_code=404, content={"detail": "Not Found"})
    try:
        _apply_security_headers(resp)
        if path.startswith("/dashboard"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
    except Exception:  # pragma: no cover - a 404 must never itself error
        pass
    return resp


# ── Logging ──────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    """Log diagnostic message to stderr (stdout is for process output)."""
    print(f"[mcp-http] {msg}", file=sys.stderr, flush=True)


# ── Lifespan — shared HTTP client ────────────────────────────────────────────


def _mcp_eager_warm_lanes() -> None:
    """Startup warm of the MCP door's LAZY first-use resources (daemon thread, fail-safe
    per resource): the spaCy pipeline the sense-disambiguation/self-verify lanes parse
    with (+ one dummy parse to JIT the pipes) and the NLTK WordNet corpus. Pure
    load-order change — each resource has its own lock and lazy callers take the same
    path whether the warm ran or not (see the lifespan comment for measured costs)."""
    _t0 = time.monotonic()
    _done: list[str] = []
    try:
        from src.extraction import linguistics as _ling
        _nlp = _ling._get_nlp()
        if _nlp is not None:
            _nlp("warmup")
            _done.append("spacy")
    except Exception as _e:  # noqa: BLE001 — fail-safe: lazy path stays available
        _log(f"eager_lane_warm(mcp).spacy_failed: {type(_e).__name__}: {str(_e)[:100]}")
    try:
        from src.api import wordnet_ladder as _wl
        if _wl._wn() is not None:
            _done.append("wordnet")
    except Exception as _e:  # noqa: BLE001
        _log(f"eager_lane_warm(mcp).wordnet_failed: {type(_e).__name__}: {str(_e)[:100]}")
    _log(f"eager_lane_warm(mcp).done warmed={','.join(_done) or 'none'} "
         f"elapsed_s={round(time.monotonic() - _t0, 2)}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── SaaS fail-closed startup guards (SAAS_MODE only; no-op when off). ─────────
    # Refuse to start OPEN (MCP_API_KEY unset) or with a pinned default tenant
    # (FAULTLINE_USER_ID set) in multi-account mode. Raises → process refuses to start.
    try:
        from saas.config import enforce_startup_guards, saas_mode_enabled
        enforce_startup_guards(MCP_API_KEY, _mcp.FAULTLINE_USER_ID)
        _saas_on = saas_mode_enabled()
    except ImportError:
        _saas_on = False  # module absent → public core, saas off
    if _saas_on:
        _log("SAAS_MODE ENABLED — per-account isolation active (seat check live).")
        # Bind the saas redactor into the error seam EXPLICITLY (round 4): this process
        # renders tool results through src/api/errors.py and must not hold the identity default.
        try:
            from saas import redact as _saas_redact
            _saas_redact.install_engine_redactor()
        except Exception as _re:  # noqa: BLE001
            _log(f"saas log redactor NOT installed ({type(_re).__name__}) — CRIT lines unredacted")

    _mcp._http_client = _mcp.TurnBoundedClient(httpx.AsyncClient(timeout=30.0))  # round 17: every request honours the turn wall
    _log(f"HTTP transport started. FaultLine API: {_mcp.FAULTLINE_API_URL}")
    if MCP_API_KEY:
        _log(f"Auth ENABLED — MCP_API_KEY set ({len(MCP_API_KEY)} chars)")
    else:
        _log(
            "WARNING: Auth DISABLED — MCP_API_KEY not set; running OPEN. "
            "This is an unauthenticated write path into every tenant's knowledge "
            "graph — dev/localhost ONLY, never a deployment posture."
        )
    # first-touch-cold-path round 5: re-queue any spooled (still-unconfirmed) retained turns
    # from a previous process, so a restart never silently forgets a pending re-append.
    try:
        await _mcp.episodic_spool_recover()
    except Exception as _sp_exc:  # noqa: BLE001 — recovery is best-effort, never blocks startup
        _log(f"episodic_spool_recover failed (non-fatal): {_sp_exc!r}")
    # ── EAGER LANE WARM (BUG-R4-3 lane; flag EAGER_LANE_WARM, default ON; OFF =
    # byte-parity with today's lazy first-use costs) ────────────────────────────
    # MEASURED on prod (owner seat, 2026-11-14): the door's steady state is ~2.5s
    # post bulk-prime, but the FIRST recall in a fresh MCP process pays lazy loads
    # that dwarf the walk — the sense-disambiguation/self-verify lanes touch spaCy
    # (~4.6s first _get_nlp) and NLTK WordNet (~4.7s+ first _wn), so a fresh-process
    # door measured 8.3s vs 2.6s warm. Warm them in a daemon thread at startup so
    # the first USER turn after every deploy never pays them. Pure load-order change.
    if os.environ.get("EAGER_LANE_WARM", "true").strip().lower() not in ("0", "false", "no"):
        _log("eager_lane_warm_scheduled (mcp)")
        threading.Thread(
            target=_mcp_eager_warm_lanes, daemon=True, name="mcp-eager-lane-warm").start()
    try:
        yield
    finally:
        # Bounded flush of the episodic re-append drain, then a CRITICAL inventory of anything
        # still unconfirmed (the in-memory queue does not survive this exit; the spool does).
        try:
            await _mcp.episodic_shutdown_flush()
        except Exception as _fl_exc:  # noqa: BLE001
            _log(f"episodic_shutdown_flush failed (non-fatal): {_fl_exc!r}")
        await _mcp._http_client.aclose()
        _log("HTTP transport shut down.")


# ── App ───────────────────────────────────────────────────────────────────────


# ── Interactive API docs (Swagger/ReDoc UI) — disabled in prod/SaaS ───────────
# The Swagger `/docs` + ReDoc `/redoc` UIs map the FULL surface (incl. the /v1/patients PHI
# lane + agent-cortex ops) and need not be advertised publicly (audit LOW #10). In SAAS_MODE
# they are turned OFF (routes 404) unless an operator explicitly re-enables via MCP_ENABLE_DOCS=1.
# SAAS off (public core / dev) → the UIs stay on (byte-for-byte today's behavior). The raw
# /openapi.json remains the MINIMAL tools-only 3.0.3 spec (app.openapi override below) — it
# never exposed the internal control-plane routes — and /tools/openapi.json stays public for
# OpenWebUI tool discovery.
def _docs_ui_enabled() -> bool:
    if _sec_truthy(os.environ.get("MCP_ENABLE_DOCS"), False):
        return True  # explicit operator opt-in
    try:
        from saas.config import saas_mode_enabled as _sme
        return not _sme()  # SaaS on → off; SaaS off → on (unchanged)
    except Exception:
        return True  # public core / import fault → unchanged (docs on)


_DOCS_UI_ON = _docs_ui_enabled()

app = FastAPI(
    title="FaultLine MCP",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_UI_ON else None,
    redoc_url="/redoc" if _DOCS_UI_ON else None,
)


class _TurnWallMiddleware:
    """ROUND 15: ONE per-request turn wall (MCP_TURN_WALL_S) opened at the HTTP door — REST, JSON-RPC
    and /v1 alike — so the provisioning gate, the brain budgets, the capture and the /ingest
    retries inside the request are all bounded and the loud status returns before the caller
    (OpenWebUI's AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER ← AIOHTTP_CLIENT_TIMEOUT, 300 s) hangs up.
    Pure ASGI (no BaseHTTPMiddleware task hop) so the ContextVar reaches the endpoint."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        tok = _mcp._turn_wall_open()
        try:
            return await self.app(scope, receive, send)
        finally:
            try:
                _mcp._turn_deadline.reset(tok)
            except ValueError:
                pass


app.add_middleware(_TurnWallMiddleware)

# ── Dynamic CORS (per-tenant origin ACL; SAAS-aware) ──────────────────────────
# Replaces a static allow-list. Under SAAS the origin decision is delegated to saas.acl,
# which REFLECTS ANY origin — the transport never hard-blocks a browser; the REAL per-tenant
# origin enforcement is moved POST-AUTH (require_auth + saas.acl.origin_allowed_for_account).
# SAAS off (or the saas package absent) → the static MCP_ALLOWED_ORIGINS env list, today's
# behavior EXACTLY. This is safe because auth is a header Bearer token, NOT an ambient cookie:
# reflecting the origin cannot let a cross-origin page make an authed call without the token,
# and the bearer + per-tenant schema remain the data-isolation boundary.
def _cors_origin_ok(origin: str | None) -> bool:
    if not origin:
        return False
    try:
        from saas.acl import cors_allows
        return cors_allows(origin)
    except Exception:
        return origin in MCP_ALLOWED_ORIGINS  # FOSS-core / import-failure fallback


# Explicit CORS request-header allow-list (never reflect `*` — audit MED #6). These are the
# only headers the MCP/dashboard/API clients actually send on a cross-origin call: the bearer,
# JSON content-type, the per-user scoping headers, the MCP session id, and the dashboard CSRF
# token. Reflecting `*` needlessly widens what a cross-origin page may set.
_CORS_ALLOW_HEADERS = (
    "Authorization, Content-Type, X-OpenWebUI-User-Id, X-User-ID, "
    "Mcp-Session-Id, X-CSRF-Token, X-Requested-With"
)


@app.middleware("http")
async def _dynamic_cors(request: Request, call_next):  # noqa: ANN001, ANN201
    origin = request.headers.get("origin")
    allowed = _cors_origin_ok(origin)
    # Preflight — answered here (no route declares an explicit OPTIONS handler).
    if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
        if allowed:
            return Response(status_code=200, headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            })
        return Response(status_code=400, headers={"Vary": "Origin"})  # disallowed → browser blocks
    response = await call_next(request)
    if allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Expose-Headers"] = "Mcp-Session-Id"
        response.headers.setdefault("Vary", "Origin")
    return response


# Security response headers on EVERY response (safe-default; MCP_SECURITY_HEADERS=0 to
# disable). Additive header-only mutation → never touches the body / request handling.
@app.middleware("http")
async def _security_headers(request: Request, call_next):  # noqa: ANN001, ANN201
    response = await call_next(request)
    try:
        _apply_security_headers(response)
    except Exception:  # pragma: no cover - a header pass must never break a response
        pass
    return response

# ── SaaS control-plane admin router (SAAS_MODE only) ──────────────────────────
# Mounted ONLY when SAAS_MODE is on so the public core has no /admin surface when the
# flag is off. Import is guarded so the module being absent never breaks startup.
try:
    from saas.config import saas_mode_enabled as _saas_mode_enabled
    if _saas_mode_enabled():
        from saas.admin import router as _saas_admin_router
        app.include_router(_saas_admin_router)
        _log("SAAS_MODE — /admin control-plane router mounted.")

        # Operator log firehose (GET /admin/logs) over public.saas_event_log.
        from saas.logs_admin import router as _saas_logs_router
        app.include_router(_saas_logs_router)
        _log("SAAS_MODE — /admin/logs firehose router mounted.")

        # Per-tenant BYO-LLM engine-brain config (/admin/llm-config*).
        from saas.llm_admin import router as _saas_llm_admin_router
        app.include_router(_saas_llm_admin_router)
        _log("SAAS_MODE — /admin/llm-config router mounted.")

        # Tenant-console HUMAN auth realm (/tenant/* login + /admin/dashboard-users*).
        from saas.tenant_console import router as _saas_tenant_console_router
        app.include_router(_saas_tenant_console_router)
        _log("SAAS_MODE — /tenant control-plane (dashboard auth) router mounted.")

        # Tenant self-serve SUBSCRIPTION surface (/tenant/plans, /tenant/subscribe,
        # /tenant/contracts, /tenant/contract/{id}). A separate module from the console so the
        # money/signature path has its own file, but it rides the SAME session dependency
        # (require_dashboard_session) — the signer's email + account are session-derived, never
        # request input. Terms + signed contracts land in the public.* control plane
        # (migration 243).
        from saas.tenant_checkout_routes import router as _saas_tenant_checkout_router
        app.include_router(_saas_tenant_checkout_router)
        _log("SAAS_MODE — /tenant subscription (plans/subscribe/contracts) router mounted.")

        # Operator (super-admin) HUMAN auth realm (/operator/* + /admin/operator-users).
        from saas.operator_console import router as _saas_operator_console_router
        app.include_router(_saas_operator_console_router)
        _log("SAAS_MODE — /operator control-plane (operator auth) router mounted.")

        # Individual self-serve realm (/me/* login + gated signup, single-user scope).
        from saas.user_console import router as _saas_user_console_router
        app.include_router(_saas_user_console_router)
        _log("SAAS_MODE — /me individual self-serve console router mounted.")

        # UNIFIED SIGN-IN (BUGS/login-uth-eq-login-unified-signin-20261101): ONE credential
        # door (POST /auth/unified-login) that probes both realms and returns a
        # DISCRIMINATED result (seat / tenant / mfa / both). It never mints a session
        # itself — 'both' issues a short-TTL single-use ROUTING grant for the
        # /access.html chooser; single-realm outcomes run the realm's OWN login function.
        # Mounted AFTER the two realm routers so their session issuers are initialized.
        from saas.unified_login import router as _saas_unified_login_router
        app.include_router(_saas_unified_login_router)
        # SAME-PAGE CROSS-ORIGIN LOGIN (BUGS/same-page-login-20261102): strict,
        # path-scoped CORS for exactly the three /auth doors the marketing card POSTs.
        # Registered LAST so it is OUTERMOST — its decision is final for those paths
        # (an attacker Origin gets no Access-Control-* header even though the wide
        # per-tenant reflection may allow that origin for bearer-token routes). Every
        # other endpoint passes through untouched.
        from saas.unified_login import UnifiedAuthCorsMiddleware as _unified_auth_cors
        app.add_middleware(_unified_auth_cors)
        _log("SAAS_MODE — /auth unified sign-in router mounted (strict door-scoped CORS).")

        # PUBLIC tenant self-signup (/signup/team) + its operator queue
        # (/admin/tenant-signups*). A SEPARATE module so the operator-only
        # POST /admin/accounts route keeps its require_operator dependency untouched.
        from saas.tenant_signup import router as _saas_tenant_signup_router
        app.include_router(_saas_tenant_signup_router)
        _log("SAAS_MODE — /signup/team public tenant self-signup router mounted.")

        # Guided-onboarding TOUR progress (/tour/*), shared by all three human realms.
        # The (realm, principal_id) is derived from whichever signed session cookie is
        # present (BOLA-safe) — see saas/tour.py.
        from saas.tour import router as _saas_tour_router
        app.include_router(_saas_tour_router)
        _log("SAAS_MODE — /tour guided-onboarding progress router mounted.")

        # First-run ONBOARDING checklist (/onboarding/*), shared by the tenant + individual
        # realms. Same BOLA-safe signed-session principal as the tour; live completion signals
        # (brain configured / key minted / connected / first memory) drive the checklist. See
        # saas/onboarding_flow.py.
        from saas.onboarding_flow import router as _saas_onboarding_router
        app.include_router(_saas_onboarding_router)
        _log("SAAS_MODE — /onboarding first-run checklist router mounted.")

        # ── Stripe billing + email lifecycle (sandbox build) ─────────────────────────
        # The dashboard READ surface (billing.js calls /admin/billing/*). Operator-only,
        # whoami-gated so the JS hides controls the viewer may not use (canary #2). The richer
        # per-permission gates (can_issue_refunds / can_manage_branding / can_manage_email_controls)
        # ride on individual routes inside this router.
        from saas.billing_dashboard_routes import router as _saas_billing_dashboard_router
        app.include_router(_saas_billing_dashboard_router)
        _log("SAAS_MODE — /admin/billing dashboard router mounted.")

        # The operator ACTION surface (begin checkout, issue refund, register webhook,
        # subscriptions list). Separate prefix (/admin/billing-ops) from the dashboard so both
        # mount without path collision. Operator-only.
        from saas.stripe_routes import router as _saas_billing_ops_router
        app.include_router(_saas_billing_ops_router)
        _log("SAAS_MODE — /admin/billing-ops actions router mounted.")

        # Email-controls panel surface (/admin/emails/*): the capture outbox read, the
        # retention-email dedupe log, feature-state get/put. Operator-only.
        from saas.email_controls_routes import router as _saas_email_controls_router
        app.include_router(_saas_email_controls_router)
        _log("SAAS_MODE — /admin/emails email-controls router mounted.")

        # PUBLIC Stripe webhook — NO auth, NO SAAS prefix. Mounted at /payments/stripe-webhook.
        # Reachable by Stripe from the public internet; gated by WEBHOOK SIGNATURE VERIFICATION
        # ONLY (the route acks 200 fast + processes async, per Stripe docs best practices). This
        # is the SOLE path that grants/revokes entitlement from a payment event — a redirect
        # grants nothing, and a forged signature is rejected before any action.
        from saas.stripe_routes import webhook_router as _saas_stripe_webhook_router
        app.include_router(_saas_stripe_webhook_router)
        _log("SAAS_MODE — /payments/stripe-webhook public webhook route mounted.")

        # Tenant dashboard: thin static UI over the /admin API, served ONLY in SAAS_MODE.
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
        _dash_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "saas", "dashboard")
        )
        if os.path.isfile(os.path.join(_dash_dir, "index.html")):
            # Bare root → the console, so a reverse-proxied domain (e.g. https://host/) lands on
            # the UI instead of a bare 404. Only registered when the dashboard is mounted.
            _landing_dir = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "saas", "landing")
            )
            _landing_index = os.path.join(_landing_dir, "index.html")
            _user_html = os.path.join(_dash_dir, "user.html")

            @app.get("/", include_in_schema=False)
            async def _root_by_face(request: Request):  # noqa: ANN202
                # Serve the RIGHT surface at a clean root per classified face:
                #   admin           → operator console (index.html) — UNCHANGED
                #   tenant/internal → the marketing splash (the public front door);
                #                      customers enter the console via /access.html
                face = _classify_face(request)
                if face == "admin":
                    return FileResponse(os.path.join(_dash_dir, "index.html"))
                if face == "volenti":
                    # Parent-company domain → the Volenti corporate site at a clean root.
                    _volenti_html = os.path.join(_landing_dir, "volenti.html")
                    if os.path.isfile(_volenti_html):
                        return FileResponse(_volenti_html)
                if os.path.isfile(_landing_index):
                    # Same content-hash versioning the catch-all mount applies — a clean
                    # root and /index.html must serve byte-identical (equally versioned)
                    # markup, or the root would keep the unversioned 24h-cache wound alive.
                    return _landing_versioned_html_response(
                        _landing_index,
                        if_none_match=request.headers.get("if-none-match"))
                # Landing missing → fall back to the prior console behavior.
                if face == "tenant":
                    _tenant_html = os.path.join(_dash_dir, "tenant.html")
                    if os.path.isfile(_tenant_html):
                        return FileResponse(_tenant_html)
                from fastapi.responses import RedirectResponse
                return RedirectResponse(url="/dashboard")

            # Bare /dashboard is an UNLABELED DOOR (2026-10-21: the owner typed it and got a
            # bare 404 — the console exists only at explicit file paths). Resolve it per face:
            # tenant/internal → the "Customer access" chooser (the designed two-login door:
            # tenant console + individual My Memory); admin → the operator console. 302, never
            # 301 (same cache rule as the signup redirects). Registered BEFORE the /dashboard
            # mount so the exact path wins over StaticFiles' own 404.

            @app.get("/dashboard", include_in_schema=False)
            async def _dashboard_bare_by_face(request: Request):  # noqa: ANN202
                from fastapi.responses import RedirectResponse  # fn-local: matches _root_by_face
                face = _classify_face(request)
                if face == "admin":
                    return RedirectResponse(url="/dashboard/index.html")
                return RedirectResponse(url="/access.html")

            # Browser-facing page route for the individual "My Memory" console.
            # GET /me in a BROWSER (Accept: text/html) serves the user.html page; every
            # other GET /me (API clients: application/json, */*, or no Accept) falls
            # through UNTOUCHED to the JSON /me handler in the user_console router, which
            # stays byte-for-byte intact (session-authed → 401 for unauthenticated API
            # callers). Implemented as a pre-routing check so route precedence never has
            # to override the router's GET /me. The surface gate (registered later, runs
            # outermost) already 404s /me on the admin face before this can serve the page.
            @app.middleware("http")
            async def _me_page_route(request: Request, call_next):  # noqa: ANN001, ANN202
                if (
                    request.method == "GET"
                    and request.url.path == "/me"
                    and "text/html" in (request.headers.get("accept") or "").lower()
                    and os.path.isfile(_user_html)
                ):
                    return FileResponse(_user_html)
                return await call_next(request)

            @app.get("/dashboard", include_in_schema=False)
            async def _dashboard_index():  # noqa: ANN202
                return FileResponse(os.path.join(_dash_dir, "index.html"))

            # ── BENCH DASHBOARD DATA — OPERATOR-GATED (not publicly snoopable) ──────────
            # The benchmark results (LongMemEval feed + the fltest ledger) are published as
            # JSON into <dash>/data/ (a named volume mounted INSIDE the StaticFiles dir so the
            # history outlives an image rebuild — see benchmarks/harness/bench_publish.py).
            # Left on the raw static mount they were world-readable at /dashboard/data/*.json.
            # Gate the READ behind the SAME operator credential the server-stats card uses
            # (saas.admin.require_operator → 403 without an operator session/key). Registered
            # BEFORE the StaticFiles mount below, so it takes precedence over the static handler
            # for /dashboard/data/*. The WRITE path (bench_publish docker-cp into data/) is
            # untouched — only serving is gated. bench_dash.js fetches ./data/*.json same-origin,
            # so the operator session cookie rides along automatically (CSP-safe, no new scheme).
            from saas.admin import require_operator as _require_operator
            # ALLOWLIST, not a pattern: ``fname`` is an EXACT member of this closed set of bare
            # basenames or the request never reaches a filesystem call. That is the whole
            # path-injection defence (CodeQL py/path-injection: "validate against a list of
            # known-safe values" — codeql.github.com/codeql-query-help/python/py-path-injection).
            # Pinned by tests/test_bench_data_allowlist.py: every member is a bare ``*.json``
            # basename (no separator, no ``..``), and a traversal name is refused with 404.
            _BENCH_DATA_FILES = frozenset({"lme_results.json", "test_runs.json",
                                           "cycle_events.json"})
            _bench_data_dir = os.path.join(_dash_dir, "data")
            # CQ-hardening 2026-09-17: the response path is BUILT from the closed allowlist
            # itself, never from the request string — the request token only LOOKS UP a
            # prebuilt constant path, so no request bytes ever reach a filesystem path
            # (the allowlist remedy in structural form; the annotation is gone because there
            # is no request-derived path to flag).
            _BENCH_DATA_PATHS = {name: os.path.join(_bench_data_dir, name)
                                 for name in _BENCH_DATA_FILES}

            @app.get("/dashboard/data/{fname}", include_in_schema=False)
            async def _bench_data(fname: str,
                                  _op: bool = Depends(_require_operator)):  # noqa: ANN202
                # require_operator already 403'd any non-operator caller before we get here.
                fpath = _BENCH_DATA_PATHS.get(fname)
                if fpath is None:
                    return _gated_not_found(f"/dashboard/data/{fname}")
                if not os.path.isfile(fpath):
                    # Nothing published yet → an empty payload the page renders as "no data".
                    empty = {"runs": []} if fname == "test_runs.json" else {}
                    return JSONResponse(content=empty)
                return FileResponse(fpath, media_type="application/json")

            # ── PUBLIC anonymized leaderboard DATA — GATE-FREE, ANON COLUMNS ONLY ───────
            # The community benchmark leaderboard (saas/dashboard/leaderboard.html) is PUBLIC.
            # This endpoint returns ONLY the anonymized columns (model_label, scores, wall_time,
            # created_at) via saas.community_bench.list_public_results — which never SELECTs
            # account_id/user_id. No auth, no face gate (reachable on tenant + internal faces;
            # the page links from every console + the marketing site). There is deliberately NO
            # path from here to an identity column — the anonymization boundary is the query.
            @app.get("/leaderboard/data", include_in_schema=False)
            async def _leaderboard_data(limit: int = 250):  # noqa: ANN202
                try:
                    from saas.community_bench import list_public_results
                    return JSONResponse(content={"results": list_public_results(limit)})
                except Exception:
                    return JSONResponse(content={"results": []})

            # Assets (app.js/style.css/logo) — html=True lets /dashboard/ serve index too.
            # Same 405-vs-404 wrapper as the landing catch-all: POST /dashboard/<anything>
            # answered 405 for the same StaticFiles reason. Lower impact (not on an OAuth
            # discovery path) but the identical defect class, so it is fixed at the same seam
            # rather than only where it happened to be noticed.
            app.mount("/dashboard",
                      _static_files_404_on_unsupported_method(StaticFiles)(
                          directory=_dash_dir, html=True),
                      name="dashboard")

            # Cache-busting: browsers otherwise pin /dashboard/app.js|tenant.js|style.css
            # across rebuilds and run STALE UI code. Force a revalidation on every load so a
            # redeploy is picked up immediately (no ?v= to bump). Scoped to /dashboard only.
            @app.middleware("http")
            async def _dashboard_no_cache(request: Request, call_next):  # noqa: ANN001, ANN202
                response = await call_next(request)
                if request.url.path.startswith("/dashboard"):
                    response.headers["Cache-Control"] = "no-cache, must-revalidate"
                    response.headers["Pragma"] = "no-cache"
                return response

            _log(f"SAAS_MODE — /dashboard static UI mounted from {_dash_dir} (no-cache).")
        else:
            _log(f"SAAS_MODE — dashboard dir missing ({_dash_dir}); UI not mounted.")
except Exception as _saas_exc:  # pragma: no cover - defensive
    _log(f"SaaS admin router not mounted: {_saas_exc}")


# ── SaaS request-event middleware (SAAS_MODE only; best-effort, never blocks) ──────
# Emits one INFO structured event (method + path + status) per request into the log
# firehose. SAAS off → pure passthrough (today's behavior EXACTLY). The emit is
# fail-safe: any error is swallowed inside eventlog.emit and can never break a request.
# NOTE: we deliberately do NOT re-derive the auth principal here (that lives in
# require_auth) to avoid duplicating auth logic — account/user are read only if a prior
# layer cheaply stashed them on request.state.
# TODO(observability): thread the resolved account/user from require_auth onto
#   request.state so this event can carry the tenant tag without re-resolving the key.
@app.middleware("http")
async def _saas_request_event(request: Request, call_next):  # noqa: ANN001, ANN201
    response = await call_next(request)
    try:
        from saas.config import saas_mode_enabled as _saas_on
        if _saas_on():
            from saas import eventlog as _eventlog
            _acct = getattr(request.state, "account_id", None)
            _user = getattr(request.state, "user_id", None)
            await _eventlog.emit(
                "INFO", "http.request",
                f"{_client_ip(request)} {request.method} {request.url.path} -> {response.status_code}",
                account_id=_acct, user_id=_user,
            )
    except Exception:  # pragma: no cover - middleware must never break the response
        pass
    return response


# ── Sliding-idle refresh for the OPERATOR session (SAAS_MODE only) ─────────────
# The operator session token expires an ABSOLUTE hour after login, kicking an admin
# mid-task. This re-issues the operator cookie with a fresh iat on every authenticated
# request, converting it to a SLIDING window: logout only after ~1h of INACTIVITY. All
# logic (SAAS gate, cookie-present check, "did the handler already set/clear this cookie"
# guard for login/logout) lives in dashboard_auth.refresh_operator_session_sliding, which
# is fail-safe (any error → no refresh → the token's own absolute expiry still governs).
@app.middleware("http")
async def _operator_session_sliding(request: Request, call_next):  # noqa: ANN001, ANN201
    response = await call_next(request)
    try:
        from saas.dashboard_auth import refresh_operator_session_sliding
        refresh_operator_session_sliding(request, response)
    except Exception:  # pragma: no cover - middleware must never break the response
        pass
    return response


# ── Host-based surface gate (GUARDRAIL) ───────────────────────────────────────
# Registered LAST → outermost middleware → runs BEFORE routing on every request,
# regardless of SAAS_MODE or the request-event middleware above. When both host
# lists are unset every host is "internal" and this is a pure no-op (today's
# behavior EXACTLY). When a host IS classified admin/tenant, the disallowed surface
# 404s with a body/headers identical to a genuine "no such route".
@app.middleware("http")
async def _surface_gate(request: Request, call_next):  # noqa: ANN001, ANN201
    try:
        face = _classify_face(request)
        if _gate_blocks(face, request.url.path):
            return _gated_not_found(request.url.path)
    except Exception:  # pragma: no cover - a gate fault must fail OPEN (internal)
        pass
    return await call_next(request)


# ── OFFLINE / MAINTENANCE gate (SAAS_MODE only; migration 174) ────────────────
# Registered AFTER _surface_gate → it is now the OUTERMOST middleware → it runs FIRST,
# before routing, on every request. SAAS off → a pure passthrough (one env read, ZERO
# imports from saas/) — the public core has no offline switch and behaves byte-for-byte
# as today.
#
# ⚠️ THE OPERATOR REALM IS EXEMPT — THIS IS LOAD-BEARING, NOT A CONVENIENCE. ⚠️
# The offline switch LIVES on the operator console (/dashboard/index.html → /admin/*).
# If the gate covered it, turning the product offline would also turn OFF the only control
# that turns it back on: a one-way trap with no way out short of a DB edit. Every future
# tightening of this list must preserve the operator path.
#
# Default DENY, explicit ALLOW: anything not in the operator allow-list is offline. A new
# route added tomorrow is therefore covered by default, rather than silently escaping the
# gate — the fail-closed direction.

# The operator's own surfaces: its API (/admin — require_operator), its auth realm
# (/operator — login/session/MFA), its console assets (/dashboard), and the liveness probe
# (/health — the orchestrator must never see a maintenance page as a dead container).
_OFFLINE_EXEMPT_PREFIXES = ("/health", "/admin", "/operator", "/dashboard")

# ...but NOT the tenant / individual console PAGES, which are served out of the same
# /dashboard static dir. They are the product's front door for users, so they get the
# maintenance page (their APIs — /tenant/*, /me/* — are denied by the default-deny anyway).
# subscribe.html is blocked for a STRONGER reason than the other two. /dashboard is offline-
# EXEMPT (it carries the operator console), so without this entry the subscription page would
# render during maintenance while POST /tenant/subscribe is denied by the gate's default-deny —
# i.e. a customer types their legal name into a signature box, clicks "I agree", and gets an
# opaque 503. Taking a signature and a card during maintenance is the wrong failure mode; so is
# a signature box whose submit is dead. The maintenance page is the honest answer.
_OFFLINE_BLOCKED_PAGES = ("/dashboard/tenant.html", "/dashboard/user.html",
                          "/dashboard/subscribe.html",
                          # Without this the archive renders during maintenance while every
                          # /tenant/* call behind it is default-denied — a page about the
                          # customer's legal records where each one fails with an opaque 503.
                          "/dashboard/agreements.html")

_OFFLINE_RETRY_AFTER = "600"  # 10 min — a hint, not a promise


def _offline_exempt(request: Request, path: str) -> bool:
    """True when the path belongs to the OPERATOR realm (never taken offline)."""
    if path in _OFFLINE_BLOCKED_PAGES:
        return False
    if path.startswith(_OFFLINE_EXEMPT_PREFIXES):
        return True
    # The bare root on the ADMIN face serves the operator console (see _root_by_face), so it
    # must survive maintenance too. On any other face "/" is the user-facing front door.
    if path == "/":
        try:
            return _classify_face(request) == "admin"
        except Exception:
            return False
    return False


def _offline_response(request: Request, message: str) -> Response:
    """503 for a blocked request: the maintenance PAGE for a browser, JSON for an API/MCP client."""
    accept = (request.headers.get("accept") or "").lower()
    headers = {"Retry-After": _OFFLINE_RETRY_AFTER, "Cache-Control": "no-store"}
    if "text/html" in accept:
        page = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "saas", "dashboard",
            "offline.html")
        page = os.path.abspath(page)
        if os.path.isfile(page):
            from fastapi.responses import FileResponse
            return FileResponse(page, status_code=503, headers=headers)
    return JSONResponse(
        {"error": "offline", "status": "offline", "message": message},
        status_code=503, headers=headers)


@app.middleware("http")
async def _offline_gate(request: Request, call_next):  # noqa: ANN001, ANN201
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return await call_next(request)   # public core / SaaS off → no-op
    except Exception:
        return await call_next(request)       # no saas/ module at all → no-op

    path = request.url.path
    try:
        if _offline_exempt(request, path):
            return await call_next(request)
        from saas import platform_settings as _platform_settings
        state = _platform_settings.offline_state()   # TTL-cached; never raises
        if state["offline"]:
            return _offline_response(request, state["message"])
    except Exception:
        # FAIL-CLOSED. A gate we cannot evaluate must not admit users to a product whose
        # control plane is unreadable — and the operator console is already exempt above,
        # so this can never lock the operator out of the switch.
        return _offline_response(
            request, "FaultLine is temporarily unavailable. Please try again shortly.")
    return await call_next(request)


# ── Helpers ───────────────────────────────────────────────────────────────────


# 2026-07-28 (SEP-2322): ALL results carry a required `resultType` field — "complete" for
# ordinary results, "input_required" for MRTR interim results. Clients MUST treat results from
# earlier-protocol servers that omit the field as "complete", so adding it is non-breaking
# for every legacy client while making every result predictably parsable for modern ones.
# The server also SHOULD identify itself in each result's `_meta` (io.modelcontextprotocol/
# serverInfo) — self-reported, for display/logging only, never trusted by clients.
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


# ── Client-facing tool spec (the compatibility WATERFALL) ─────────────────────
# Goal: connect out-of-the-box for as many clients as possible, not just OpenWebUI.
#   • OpenAPI clients (OWUI OpenAPI tool servers, generic importers) probe {base}/openapi.json.
#     FastAPI's auto spec is OpenAPI 3.1.0 (which OWUI's importer can't parse) AND exposes all
#     ~94 internal routes (/admin, /tenant, dashboard). So we OVERRIDE /openapi.json (+ /docs)
#     with a MINIMAL, hand-built OpenAPI **3.0.3** doc that lists ONLY the 8 tool endpoints.
#     3.0.3 is the broadly-supported dialect; tools-only keeps the control-plane OUT of the
#     public spec (info-disclosure win). Also served at /tools/openapi.json as an explicit alias.
#   • MCP-native clients (Claude Desktop, …) use the parallel /mcp JSON-RPC lane.
# Both lanes authenticate with the SAME bearer (MCP_API_KEY / per-seat token).
_TOOL_OPS = [
    {"path": "/recall_memory", "op": "recall_memory", "summary": "Recall facts from memory",
     "description": "Look up what you already know about the user, their people, or their world. "
                    "Not inert: text may be captured and corrections acted on. Pass the user's "
                    "(a human's) message verbatim as "
                    "`query`; your own operational queries belong to cortex_recall.",
     "props": {"query": {"type": "string", "description": "The human user's message, verbatim and in full — never your own working text."}},
     "required": ["query"]},
    {"path": "/remember_facts", "op": "remember_facts", "summary": "Remember facts from a message",
     "description": "Save something a HUMAN just told you in conversation (a name, relationship, "
                    "preference, job, possession, location — even in passing). Pass THEIR words "
                    "verbatim as `text` — never your own working text (that is cortex_remember's lane).",
     "props": {"text": {"type": "string", "description": "The human's message, verbatim — do not pre-extract; never your own working text."}},
     "required": ["text"]},
    # Agent Cortex — YOUR OWN operational memory, firewalled into flagent_<uuid>. These are the
    # agent's private working notes (how to OPERATE), NEVER the user's facts. They belong in this
    # table for the same reason the six above do: it is the ONLY source the public tool spec is
    # built from, so a route that is missing here is invisible to every OpenAPI client (OpenWebUI).
    #
    # OWN-WORDS lane (mirrors tools.py): the note is written by the agent in its own words —
    # never a paste of the user's message — and the query is composed by the agent too. Verbatim
    # passing is the USER-memory lane's rule (see recall_memory / remember_facts above); a
    # pasted payload in a cortex note is an injection vector into future sessions.
    #
    # ORDER = DISCOVERABILITY (2026-07-28 spec §tools: deterministic tool order enables caching
    # and stable prompt-cache hit rates — and ORDER IS DISCOVERABILITY for the models that read
    # the list). This mirrors the canonical TOOLS order from tools.py: user-memory pair, then the
    # cortex pair, then the document/learn/retract lanes. OpenWebUI builds its function specs
    # from this iteration order and preserves it, so a cortex-first ordering here is exactly what
    # its models see — the agent-cortex loop is presented as prominently as the MCP door presents
    # it, and the model meets "recall your own notes" before it meets "store a document".
    {"path": "/cortex_recall", "op": "cortex_recall",
     "summary": "Recall your OWN operational notes before acting (Agent Cortex)",
     "description": "Look up YOUR OWN operational notes BEFORE you act (gotchas, failed commands, "
                    "pitfalls, how-tos you recorded with cortex_remember). Before acting on a task "
                    "like one you have done before, call this FIRST — before any recall_memory. "
                    "Hold them tentatively — they are your private working notes, NOT the user's "
                    "facts. To recall what the USER told you, use recall_memory instead.",
      "props": {"query": {"type": "string", "description": "The task or situation at hand, in your own words — keywords are fine."},
      "branch": {"type": "string", "description": "OPTIONAL: the branch/line of work you are on right now. Same-branch notes rank higher; it never hides anything."},
      "env": {"type": "string", "description": "OPTIONAL: the ENVIRONMENT you are in right now. Env-matching notes rank first; mismatched demote slightly and render stamped - never hidden."},
      "client_source": {"type": "string", "description": "OPTIONAL: which client/tool you are (e.g. 'claude-code', 'opencode'). Passing it self-scopes results to YOUR notes plus untagged ones; omit for every note on the seat."}},
      "required": ["query"]},
    {"path": "/cortex_remember", "op": "cortex_remember",
     "summary": "Record your OWN operational note (Agent Cortex)",
     "description": "Record YOUR OWN operational lesson so you stop repeating your own mistakes — a "
                    "gotcha, a command that failed (and the fix), a pitfall, a correction, a how-to. "
                    "Write the note yourself, in your own words — NEVER a paste of the user's message "
                    "or terminal output; if their exact words matter as a fact, use remember_facts. "
                    "This is YOUR private working memory: it is NOT the user's fact and is never "
                    "recalled as something they told you. Use remember_facts for the user's facts. "
                    "Call this at the end of a task when you learned something worth keeping.",
      "props": {"text": {"type": "string", "description": "The lesson in your own words — a short note to yourself; never a paste of the user's message or output."},
      "owner_quote": {"type": "string", "description": "OPTIONAL: their exact words, as a short quoted fragment — carried SECOND, behind the rule, never as the note body. Only when the wording itself matters."},
      "branch": {"type": "string", "description": "OPTIONAL: the branch/line of work this lesson was learned on. Same-branch notes rank higher at recall; it never hides anything."},
      "env": {"type": "string", "description": "OPTIONAL: the ENVIRONMENT this lesson is true in (e.g. 'truenas', 'p70', 'local'). Same-env notes rank first; other-env demote slightly and are stamped - never hidden."},
                "category": {"type": "string", "description": "gotcha | failed_command | pitfall | correction | howto | overrun. Defaults to gotcha."},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "Triggers/topics this note guards, for later recall (e.g. ['docker','deploy'])."},
                "evidence": {"type": "object", "description": "OPTIONAL proof: {\"sha\": \"<commit you checked it against>\", \"ref\": \"<bug/test id>\", \"command\": \"<what you ran>\", \"output\": \"<the result>\"}. A claim with proof ranks above unevidenced ones and stays checkable."},
                "client_source": {"type": "string", "description": "OPTIONAL: which client/tool is writing (e.g. 'claude-code', 'opencode') — the client, NOT the AI model (self-tag your model via `model`). Auto-stamped from the connection (mcp-name header / User-Agent) when omitted; explicit wins."},
                # SCHEMA UPLIFT provenance (owner directive; migration 301) — the OpenAPI door
                # advertises what the MCP-native door advertises (door parity, the
                # test_openapi_serves_the_canonical_* discipline): without these props an
                # OpenWebUI-connected model cannot self-tag its model identity at all.
                "model": {"type": "string", "description": "OPTIONAL: the MODEL writing this note (e.g. 'qwen3.5-9b'). Auto-stamped from the deployment (CORTEX_AGENT_MODEL) when omitted; explicit wins. Rendered as model=<x> at recall."},
                "type": {"type": "string", "description": "OPTIONAL: FREE-FORM note type ('code', 'bug', 'operational', anything). Complements the fixed `category` enum."},
                "project": {"type": "string", "description": "OPTIONAL: the PROJECT this note relates to (e.g. 'faultline', a repo name). Deployment default CORTEX_PROJECT applies when omitted; explicit wins."}},
      "required": ["text"]},
    {"path": "/ingest_document", "op": "ingest_document", "summary": "Ingest a document",
     "description": "Store a document / article / long-form text. Pass the FULL text as `text`; "
                    "optionally `source_ref` (URL/filename) and `title` for citation.",
     "props": {"text": {"type": "string", "description": "Full text of a document a HUMAN shared, verbatim — never your own working text."},
               "source_ref": {"type": "string", "description": "URL or filename for citation."},
               "title": {"type": "string", "description": "Document title."}},
     "required": ["text"]},
    {"path": "/ingest_file_b64", "op": "ingest_file", "summary": "Ingest a file (base64)",
     "description": "Upload a FILE (PDF or image) a HUMAN shared as base64 `data_b64`. "
                    "The file is retained, its text extracted deterministically and mined "
                    "like ingest_document; figures bind to surrounding text. Best effort; "
                    "may supersede existing facts, like any ingest.",
     "props": {"data_b64": {"type": "string", "description": "The file's raw bytes as base64 — the whole file."},
               "filename": {"type": "string", "description": "Optional citation label."},
               "source_ref": {"type": "string", "description": "Optional origin URL/path."},
               "title": {"type": "string", "description": "Optional document title."}},
     "required": ["data_b64"]},
    {"path": "/document_status", "op": "document_status", "summary": "Document import status",
     "description": "Report import status of ingested documents: still-processing, partially "
                    "imported (with per-section failure reasons), and retriable. Pure read.",
     "props": {"document_id": {"type": "integer", "description": "Optional: one document only. Omit for all."}},
     "required": []},
    # description= is inherited from tools.py::TOOLS by _build_tool_openapi and is NOT read
    # from here; this entry owns only the transport shape (path/props/required).
    {"path": "/review_structure", "op": "review_structure", "summary": "Review engine-grown structure",
     "props": {"kind": {"type": "string", "description": "Optional filter: cues | rel_types | groupings | ladder. Omit for all."},
               "cue": {"type": "string", "description": "To retire one derived cue: its exact cue string as listed. Requires category."},
               "category": {"type": "string", "description": "The exact category of the cue being retired. Required alongside cue."},
               "reactivate": {"type": "boolean", "description": "Restore a previously retired cue (undo). Default false."}},
     "required": []},
    {"path": "/retry_document", "op": "retry_document", "summary": "Retry failed document sections",
     "description": "Reprocess just the unread sections of a partially-imported document (the "
                    "original text is retained). May supersede existing facts, like any ingest. "
                    "Idempotent: an already-queued document reports already_active.",
     "props": {"document_id": {"type": "integer", "description": "The document id to retry (from document_status). Optional if source_ref given."},
               "source_ref": {"type": "string", "description": "Alternative: retry every partially-imported document with this URL/filename."}},
     "required": []},
    {"path": "/learn_facts", "op": "learn_facts", "summary": "Learn ontological facts",
     "description": "Store LLM-generated ontological statements ('X is a subclass of Y', one per "
                    "line) as `text`. Use when the user asks to learn a topic.",
     "props": {"text": {"type": "string", "description": "Ontological statements, one per line."}},
     "required": ["text"]},
    {"path": "/retract_fact", "op": "retract_fact", "summary": "Retract a fact",
     "description": "Use ONLY when the user explicitly wants something deleted/forgotten. Pass the "
                    "user's request verbatim as `text`. For a NEW value, use remember_facts instead.",
     "props": {"text": {"type": "string", "description": "The human user's forget/delete request, verbatim — never your own working text."}},
     "required": ["text"]},
    {"path": "/forget_fact", "op": "forget_fact", "summary": "Forget one specific named fact",
     "description": "Tombstone a named stored fact. Pass `subject` (whose fact — 'me' or the "
                    "named person/thing) and `old_value` (the stored value to remove); "
                    "`rel_type` narrows it further. A subject or relation alone is refused.",
     "props": {"subject": {"type": "string", "description": "Whose fact — 'me' or the named person/thing."},
                "rel_type": {"type": "string", "description": "Relation to pin the fact (optional)."},
                "old_value": {"type": "string",
                              "description": "REQUIRED: the stored value to remove. Without it a "
                                             "subject or relation alone would sweep every matching "
                                             "fact, so the call is refused."}},
      "required": ["subject", "old_value"]},
    # CONSTRAINED CUSTOMER-AGENT lane (SAAS only, moat) — the product's public door. OpenWebUI
    # builds its tool list from _TOOL_OPS, so this entry is what makes the lane reachable to
    # OpenWebUI-connected models as well as MCP-native clients. The OpenAPI spec built below
    # iterates _TOOL_OPS; the *_GATE 404s this path on the operator (admin) face, and the saas
    # handler fail-closes 404/refuses off-core. Its call-time enforcement is in the tools/call
    # branch + saas/agent_mcp_tool.py.
    {"path": "/customer_chat", "op": "customer_chat",
     "summary": "Chat with the tenant's CONSTRAINED customer agent",
     "description": "Send a customer's message to the tenant's constrained customer agent. Every "
                    "turn is triaged before it is answered: in-scope answered only from the "
                    "tenant's ingested corpus; out-of-scope declined + redirected to a human or "
                    "the tenant's site; consequential turns (medical/legal/financial/safety) "
                    "refused and directed to licensed professionals; abstains when it cannot "
                    "ground. Scoped per customer via X-OpenWebUI-User-Id / "
                    "ENABLE_FORWARD_USER_INFO_HEADERS.",
     "props": {"messages": {"type": "array",
                            "description": "OpenAI-shaped messages: [{\"role\": \"user\", "
                                           "\"content\": ...}]. The last user message is the "
                                           "turn; it is triaged and grounded.",
                            "items": {"type": "object"}},
               "user_id": {"type": "string",
                           "description": "Customer seat UUID (auto-resolved from "
                                          "X-OpenWebUI-User-Id when omitted)."}},
     "required": ["messages"]},
]

_TOOL_OPENAPI_CACHE: dict | None = None


def _build_tool_openapi() -> dict:
    """Minimal OpenAPI 3.0.3 doc describing ONLY the 8 tools (cached). Broadly client-compatible."""
    global _TOOL_OPENAPI_CACHE
    if _TOOL_OPENAPI_CACHE is not None:
        return _TOOL_OPENAPI_CACHE

    # SINGLE SOURCE OF TRUTH for tool descriptions: src/mcp/tools.py::TOOLS.
    #
    # This used to serve `t["description"]` — a SEPARATE, hand-written, STRIPPED-DOWN copy. So a
    # native MCP client (Claude Desktop, via tools/list) read the rich description, while an
    # OpenAPI client (OpenWebUI, and every model behind it) read the impoverished one and never
    # saw the WHEN. `recall_memory` is the clearest casualty: the canonical text opens "Call at
    # the START of a turn..." and the copy served here dropped that trigger entirely, leaving a
    # model to infer from "Look up what you already know" that it should... look things up, at
    # some unspecified point, for some unspecified reason.
    #
    # A real connected model reported back: "wtf is this and how does it help me". It was right.
    # We shipped it the worse half of our own documentation, then read its confusion as weak
    # tool-calling. Three hand-maintained copies of the same prose WILL drift; the drift IS the
    # bug. _TOOL_OPS now owns only the transport-specific schema shape (path/props/required) and
    # inherits the prose. Fall back to the local string only if a tool is somehow absent from
    # TOOLS, so a mismatch degrades instead of KeyError-ing the whole spec.
    _canonical = {t["name"]: t["description"] for t in _mcp.TOOLS}
    # The SaaS-gated lane keeps its canonical text MOAT-SIDE, in saas/agent_mcp_tool.py, because
    # it must not exist in the open-core tools.py. Fold it in here so `customer_chat` obeys the
    # SAME single-source rule as every other tool: without this, _TOOL_OPS' local "description"
    # string was the served copy, i.e. exactly the second hand-written copy this resolver was
    # built to abolish (the drift that once shipped OpenWebUI an impoverished recall_memory).
    # Guarded: a public build has no saas/ and no customer_chat op to describe.
    try:
        from saas.agent_mcp_tool import TOOL_SCHEMA as _saas_tool_schema
        _canonical.setdefault(_saas_tool_schema["name"], _saas_tool_schema["description"])
    except Exception:
        pass

    def _within_budget(op: str, desc: str) -> str:
        """Enforce the OpenAI function-description limit at the SERVING seam.

        OpenAI-compatible function-calling REJECTS a tool whose `description` exceeds 1024 chars.
        The MCP/Anthropic door has no such cap. So when both doors were unified onto the long
        canonical text (+ the premise prefix + the standing-rule suffix), three tools crossed 1024
        and the OpenWebUI-hosted model's ENTIRE TOOL LIST was rejected — FaultLine went from "the
        model doesn't know WHEN to call us" straight to "the model CANNOT call us at all". The
        cruel part: the MCP door stayed perfectly healthy, so every check I ran looked green.

        FAIL LOUD, BUT STAY UP. Serving an over-limit spec breaks EVERY tool for that client; a
        clamped description merely loses a trailing sentence. So clamp at a word boundary and
        log_crit — never silently, and never by shipping a spec we know a client will reject.
        """
        limit = _premise.OPENAI_DESCRIPTION_LIMIT
        if len(desc) <= limit:
            return desc
        cut = desc[:limit].rsplit(" ", 1)[0].rstrip(" ,;—-")
        _log(f"CRITICAL openapi.description_over_limit: tool '{op}' is {len(desc)} chars, over "
             f"the {limit}-char OpenAI function-calling limit — CLAMPED to {len(cut)}. An "
             f"unclamped spec makes an OpenAI-compatible client reject the WHOLE tool list. "
             f"Shorten it at the source (tools.py::TOOLS / premise.TOOL_PREMISE / standing rules).")
        return cut

    paths: dict = {}
    for t in _TOOL_OPS:
        paths[t["path"]] = {
            "post": {
                "operationId": t["op"],
                "summary": t["summary"],
                # `t.get("description", t["summary"])` — NOT `t["description"]`. The
                # canonical text from tools.py::TOOLS is the ONLY thing ever served (the
                # parity test pins that), so the local key is unreachable dead prose and a
                # new op should not have to paste a second copy of its description here just
                # to satisfy an eagerly-evaluated fallback. Existing ops are unaffected (they
                # all carry the key); a future op carrying neither degrades to its summary
                # instead of raising KeyError and taking /openapi.json down with it.
                "description": _within_budget(
                    t["op"], _canonical.get(t["op"], t.get("description", t["summary"]))),
                "security": [{"HTTPBearer": []}],   # per-op (some clients read this, not just global)
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": t["props"],
                        "required": t["required"],
                    }}},
                },
                "responses": {"200": {
                    "description": "Result",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                }},
            }
        }
    _TOOL_OPENAPI_CACHE = {
        "openapi": "3.0.3",
        "info": {
            "title": "FaultLine Memory",
            "version": "1.0.0",
            # THE PREMISE. `info.description` is the only server-level prose an OpenAPI client
            # ever sees — OpenAPI has no `instructions` field, so this slot carries the whole
            # "what is this and why would you want it". It used to hold a one-line WHAT
            # ("Persistent, validated knowledge-graph memory") which tells a model nothing about
            # when to reach for it or why its answers beat the model's own recollection.
            "description": _premise.OPENAPI_DESCRIPTION,
        },
        "servers": [{"url": "/"}],   # relative → client calls the same base it fetched the spec from
        "paths": paths,
        "components": {"securitySchemes": {"HTTPBearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"HTTPBearer": []}],
    }
    return _TOOL_OPENAPI_CACHE


@app.get("/tools/openapi.json", include_in_schema=False)
async def tools_openapi() -> JSONResponse:
    """Explicit alias for the minimal tool spec (same as the overridden /openapi.json)."""
    return JSONResponse(_build_tool_openapi())


# Override the default /openapi.json (+ /docs, /redoc) with the clean tools-only 3.0.3 spec.
app.openapi = _build_tool_openapi  # type: ignore[assignment]


# ── RFC 9116 security.txt (responsible-disclosure contact) ────────────────────
# Served at both the well-known path and root. Contact/canonical/policy are env-driven
# (SAAS_SECURITY_CONTACT / SAAS_SECURITY_CANONICAL / SAAS_SECURITY_POLICY); Expires is a
# rolling ~1y so the file never goes stale. Not auth-gated by design — it's a public
# disclosure pointer. No SaaS coupling; harmless when the envs are unset (sane defaults).
def _security_txt() -> str:
    from datetime import datetime, timedelta, timezone
    contact = (os.environ.get("SAAS_SECURITY_CONTACT") or "mailto:security@volenti.ca").strip()
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Canonical + Policy default off the public base URL so the file is fully RFC-9116-populated
    # without extra env config; the specific SAAS_SECURITY_* envs override when set.
    base = (os.environ.get("SAAS_MCP_PUBLIC_URL") or "").strip().rstrip("/")
    canonical = (os.environ.get("SAAS_SECURITY_CANONICAL") or base).strip().rstrip("/")
    policy = (os.environ.get("SAAS_SECURITY_POLICY")
              or (f"{base}/security.html" if base else "")).strip()
    lines = [f"Contact: {contact}", f"Expires: {expires}", "Preferred-Languages: en"]
    if canonical:
        lines.append(f"Canonical: {canonical}/.well-known/security.txt")
    if policy:
        lines.append(f"Policy: {policy}")
    return "\n".join(lines) + "\n"


@app.get("/.well-known/security.txt", include_in_schema=False)
@app.get("/security.txt", include_in_schema=False)
async def security_txt():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(_security_txt(), media_type="text/plain")


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
    # ── SaaS mode: resolve the key to an ACCOUNT principal (control plane). ───────
    # SAAS_MODE off → this whole block is skipped and behavior is EXACTLY today's
    # (single shared key → "shared"). Import is lazy + fail-closed: any resolver error
    # denies (returns None → 401), never falls through to the shared path.
    try:
        from saas.config import saas_mode_enabled
        if saas_mode_enabled():
            if credentials is None:
                return None
            # Per-user token (flu_…): resolve to the BOUND UUID → this becomes the principal
            # and activates bind_tenant Option A (the token can act ONLY as this UUID; any
            # other claimed X-OpenWebUI-User-Id → 403). An unknown/revoked token or an
            # inactive seat → None (fail-closed → 401); NEVER retried as an account key.
            from saas.tokens import is_user_token, resolve_user_token
            if is_user_token(credentials):
                return resolve_user_token(credentials)
            # Account key (flk_…): resolve to the account principal (control plane).
            from saas.accounts import resolve_key_to_account
            account_id = resolve_key_to_account(credentials)
            if account_id is None:
                return None
            return f"account:{account_id}"
    except Exception:
        return None  # fail-closed

    if not MCP_API_KEY:
        return "anonymous"  # unauthenticated mode (dev/localhost only)
    if credentials is None:
        return None
    if hmac.compare_digest(credentials, MCP_API_KEY):
        return "shared"
    return None


def _saas_rate_limited(principal: str | None) -> bool:
    """Per-account request rate limit at the auth chokepoint (SAAS_MODE only).

    Returns True when the request must be 429'd, else False. SAAS off / module absent /
    flag off / any error → False (today's behavior EXACTLY — the limiter is FAIL-OPEN and
    never wrongly blocks). Mirrors the guarded lazy-import pattern of _saas_write_block.
    """
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return False
        from saas.ratelimit import allow_request
        return not allow_request(principal)
    except Exception:
        return False  # fail-open


def _client_ip(request: Request) -> str:
    """Inbound source IP for audit — X-Forwarded-For first hop, but only when the immediate peer
    is a trusted proxy (SAAS_TRUSTED_PROXY_IPS); otherwise the real peer (a direct client cannot
    forge its logged source IP). Unset trusted-proxy list → honor XFF as before."""
    xff = request.headers.get("x-forwarded-for") or ""
    if xff and _peer_is_trusted_proxy(request):
        ip = xff.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else ""
    return ip or "unknown"


def _saas_origin_denied(principal: str | None, origin: str | None) -> bool:
    """Post-auth per-tenant ORIGIN ACL at the auth chokepoint (SAAS_MODE only).

    True when this browser Origin must be 403'd for the resolved account (ACL ENABLED + origin
    not allow-listed). SAAS off / no Origin (server-to-server) / module absent / any error →
    False (fail-OPEN — never wrongly block; data isolation is the bearer, not this). Mirrors the
    guarded lazy-import pattern of _saas_rate_limited.
    """
    if not origin:
        return False  # server-to-server (no browser Origin) is never gated
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return False
        from saas.agent_mode import account_id_for_principal
        from saas.acl import origin_allowed_for_account
        account = account_id_for_principal(principal, "")
        return not origin_allowed_for_account(account, origin)
    except Exception:
        return False  # fail-open


def _saas_ip_denied(principal: str | None, request: Request) -> bool:
    """Post-auth inbound source-IP allow/deny rules at the auth chokepoint (SAAS_MODE only).

    The IP twin of _saas_origin_denied above (migration 303, BUG-05): rules from
    (GLOBAL ∪ the resolved account) in public.saas_ip_rules, most-specific (longest-prefix)
    match wins, deny beats allow on an exact tie, NO matching rule → allow. SAAS off /
    unparseable client IP / no account resolvable / module absent / any error → False
    (FAIL-OPEN — the documented posture matching the origin ACL: this is defense-in-depth
    on top of the bearer, never the data boundary). The client IP is resolved by
    _client_ip (XFF first hop only behind a trusted proxy, else the real peer — a direct
    client cannot forge its evaluated source IP). Mirrors the guarded lazy-import pattern
    of _saas_rate_limited / _saas_origin_denied.

    KNOWN LIMITATION (critic round 1, W1 — pre-existing _client_ip semantics, now
    load-bearing): BEHIND a configured trusted proxy the evaluated IP is the XFF LEFTMOST
    hop, which the CLIENT chose, not the proxy — a caller holding a valid bearer can spoof
    its evaluated IP past a deny rule. Rightmost-trusted-hop parsing is the standard fix
    and an owner call (it also changes the request log and the inbound-IP table); until
    then treat IP rules behind a proxy as abuse-reduction, not a hard boundary — which is
    exactly the fail-open defense-in-depth posture this gate documents.
    """
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return False
        from saas.agent_mode import account_id_for_principal
        from saas.ip_acl import ip_denied
        account = account_id_for_principal(principal, "")
        return ip_denied(account, _client_ip(request))
    except Exception:
        return False  # fail-open


def _stash_saas_principal_state(
    request: Request, principal: str | None, user_id: str | None = None
) -> None:
    """Stamp the resolved account/user onto ``request.state`` for the SaaS event
    middleware (which READS ``request.state.account_id``/``user_id`` AFTER the response
    to tag the firehose). Without this, every http.request row is emitted with
    account_id=NULL and never surfaces in the per-account operator logs view.

    Deterministic + FAIL-SAFE:
      - SAAS off / any error → leave request.state UNSET → today's None (no regression).
      - account principal ("account:<id>") → stamp request.state.account_id = <id>.
      - per-user token principal (a bare bound UUID, flu_ path) → stamp user_id from it.
      - an explicitly resolved user_id (bind_tenant result) always wins for user_id.
    SaaS-guarded so it is a pure no-op outside SAAS_MODE.
    """
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return
        st = request.state
        if principal:
            if principal.startswith("account:"):
                acct = principal.split(":", 1)[1].strip()
                if acct:
                    st.account_id = acct
            elif principal not in ("shared", "anonymous"):
                # flu_ per-user token → the principal IS the bound user UUID.
                st.user_id = principal
        if user_id:
            st.user_id = user_id
    except Exception:
        # Fail-safe: never let observability tagging break auth/dispatch.
        pass


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
        # NO WWW-Authenticate challenge here (port of foss 595d084c, 2026-11-04): a bare
        # 'WWW-Authenticate: Bearer' triggers Claude Desktop's OAuth 2.1 discovery flow — it
        # ignores the configured static bearer key and sends a DCR-generated token instead, so
        # every request 401s (measured on the live foss public stack). A plain 401 keeps
        # static-key auth working. All three doors (/mcp JSON-RPC, REST tools, attachment
        # bridge) funnel through this one raise, so one removal covers the server.
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )
    # Tag request.state for the SaaS event middleware (account_id from an account
    # principal; user_id if this is a per-user token). bind_tenant later refines user_id
    # on the REST/tools-call paths. Fail-safe no-op outside SAAS_MODE.
    _stash_saas_principal_state(request, principal)
    # Per-account request rate limit (SAAS_MODE only, flag-gated, fail-open). A positive
    # over-limit signal → 429; everything else passes through unchanged.
    if _saas_rate_limited(principal):
        client = request.client.host if request.client else "unknown"
        _log(f"REST 429 from {client} — per-account rate limit exceeded")
        raise HTTPException(
            status_code=429,
            detail="Too Many Requests",
            headers={"Retry-After": "1"},
        )
    # Per-tenant ORIGIN ACL (SAAS_MODE only, post-auth, fail-open). A browser Origin that is
    # not allow-listed for an ACL-enabled tenant → 403. Server-to-server calls (no Origin) pass,
    # so the live OWUI tool path is never affected.
    if _saas_origin_denied(principal, request.headers.get("origin")):
        client = request.client.host if request.client else "unknown"
        _log(f"REST 403 from {client} — origin not allowed by tenant ACL")
        raise HTTPException(status_code=403, detail="Origin not allowed")
    # Inbound source-IP allow/deny rules (SAAS_MODE only, post-auth, FAIL-OPEN — the same
    # documented defense-in-depth posture as the origin gate above; migration 303). The
    # client IP is resolved by _client_ip (XFF first hop ONLY behind a trusted proxy, else
    # the real peer). The denial log deliberately does NOT echo the IP — the inbound-IP
    # table under Access control already records it for operator review.
    if _saas_ip_denied(principal, request):
        _log("REST 403 — client IP denied by IP rule (most-specific match, action=deny)")
        raise HTTPException(status_code=403, detail="Client IP not allowed")
    return principal


# ── UPSTREAM SEAT IDENTITY CAPTURE (SAAS_MODE only, best-effort) ───────────────────────
#
# THE PROBLEM IT SOLVES. A tenant wires OpenWebUI ONCE with an account key (flk_); every
# signed-in OWUI user then auto-provisions as a seat BY UUID, because the only identity this
# transport consumed was `X-OpenWebUI-User-Id`. The tenant's Seats page therefore renders a
# column of bare UUIDs. OpenWebUI ALSO forwards the signed-in user's display name and email
# when the operator sets `ENABLE_FORWARD_USER_INFO_HEADERS=true`; we read them here and park
# them beside the seat (saas.seats.record_owui_identity → migration 210).
#
# CONTRACT — all four properties are load-bearing:
#   1. SAAS_MODE OFF ⇒ ABSENT. The whole lane hangs off an ``account:``-shaped principal,
#      which only ``_resolve_principal`` under SAAS_MODE ever produces. In the open core the
#      first branch returns immediately, no header is read and no module is imported.
#   2. NEVER FATAL. Everything is inside one try/except returning None. A malformed,
#      oversized, absent, or hostile header value is IGNORED — it can never fail a tool call.
#      Absent identity is the NORMAL case (older OWUI, the flag off, a non-OWUI client) and
#      leaves the seat rendering exactly as it does today.
#   3. NEVER LOGGED. This function emits no log line, and the values are handed to
#      ``record_owui_identity`` which is likewise silent. The surrounding ``_log`` calls
#      already truncate user_id to 8 chars and gain nothing new here. PII does not go to a
#      long-lived, noisy log.
#   4. REFRESHED, not just captured-once. It runs on EVERY call, not only at auto-provision,
#      so a user renaming themselves in OpenWebUI is reflected. The DB write is suppressed
#      unless a value actually changed (see ``saas.seats._owui_identity_cache``).
#
# ── The headers, VERIFIED against OpenWebUI source (not assumed) ───────────────────────
# Read from the OpenWebUI repo, branch `main` @ package version 0.11.0:
#   * `backend/open_webui/env.py` defines the names as env-overridable constants;
#     the shipped defaults are `X-OpenWebUI-User-Name` / `-User-Id` / `-User-Email` /
#     `-User-Role` (plus `X-OpenWebUI-Chat-Id` / `-Message-Id`). We want two of them.
#   * `backend/open_webui/utils/headers.py::include_user_info_headers` builds them.
#   * `backend/open_webui/utils/tools.py::build_tool_server_headers` — docstring
#     "Shared by MCP and OpenAPI paths" — applies them under
#     `if ENABLE_FORWARD_USER_INFO_HEADERS and user:`, and its call site is inside the
#     `if type == "openapi":` branch of `get_tools()`, i.e. THE door OpenWebUI uses to call
#     our REST shorthand. So these arrive on the live path, not merely the LLM proxy.
#
# ⚠️ TWO UPSTREAM CONFIGS CAPTURE NOTHING, BY DESIGN, AND MUST STAY GRACEFUL:
#   * `ENABLE_FORWARD_USER_INFO_HEADERS` unset/false → no headers at all.
#   * `FORWARD_USER_INFO_HEADER_JWT_SECRET` SET → OpenWebUI sends ONE signed
#     `X-OpenWebUI-User-Jwt` instead and SKIPS the four plain headers. We deliberately do not
#     parse it: verifying it needs the tenant's shared secret, which is a whole configuration
#     surface for a decorative field.
#   In both cases we capture nothing and the seat renders exactly as it does today.
_OWUI_NAME_HEADERS = ("X-OpenWebUI-User-Name",)
_OWUI_EMAIL_HEADERS = ("X-OpenWebUI-User-Email",)

# Hard ceiling on the raw header BEFORE any processing. A 64KB header cannot be allowed to
# reach a sanitizer, a percent-decoder, or a DB parameter. Anything longer is dropped whole —
# it is not a person's name. (Well above the RFC 5321 §4.5.3.1.3 320-octet address cap.)
_OWUI_HEADER_RAW_MAX = 1024


def _read_owui_header(request: Request, names: tuple) -> str:
    """First non-empty value among ``names``, raw-length-capped. "" when nothing usable."""
    for name in names:
        raw = request.headers.get(name)     # Starlette matches header names case-insensitively
        if not raw:
            continue
        if len(raw) > _OWUI_HEADER_RAW_MAX:
            return ""                      # implausible for an identity — drop, never truncate
        return raw
    return ""


def _percent_decode(raw: str) -> str:
    """Percent-decode, or return ``raw`` unchanged if it is not valid percent-encoding."""
    try:
        from urllib.parse import unquote
        decoded = unquote(raw, errors="strict")
        return decoded if decoded else raw
    except Exception:
        return raw


def _decode_owui_name(raw: str) -> str:
    """The display name is percent-encoded UPSTREAM — decode it.

    HTTP header values are latin-1 by RFC 9110 §5.5, so a non-ASCII display name cannot ride
    a header raw. OpenWebUI encodes exactly this one field:
    ``quote(user.name.strip(), safe=' ')`` (`utils/headers.py`) — spaces stay literal,
    everything else including non-ASCII is escaped. Decoding is therefore the CORRECT read of
    the wire format, not a guess, and "José" must not be stored as "Jos%C3%A9".
    """
    return _percent_decode(raw) if raw and "%" in raw else raw


def _decode_owui_email(raw: str) -> str:
    """The email is NOT percent-encoded on current OpenWebUI — decode only if that's the only
    way it makes sense.

    CURRENT `main` sends the email `.strip()`-ed but UNQUOTED (`utils/headers.py`), so the
    normal case must pass through untouched — blind decoding would corrupt an address whose
    local-part legitimately contains a literal ``%`` (permitted by RFC 5322 §3.2.3 atext).
    But OpenWebUI's history disagrees with itself: commit `f7920df` ("Fix non-ascii error
    issue") percent-encoded ALL FOUR fields, and only later commits narrowed it to the name.
    An older pinned OpenWebUI therefore really can send ``someone%40example.com``.

    Resolved without guessing at versions, by SHAPE: decode only when the raw value does not
    look like an address and the decoded one does. Current OWUI (valid raw) never decodes; an
    older build's encoded value does; a literal-``%`` address that is already valid is left
    alone. Deterministic in all three directions.
    """
    if not raw or "%" not in raw:
        return raw
    if _looks_like_email(raw):
        return raw
    decoded = _percent_decode(raw)
    return decoded if _looks_like_email(decoded) else raw


def _looks_like_email(value: str) -> bool:
    """Cheap shape probe used ONLY to choose an encoding. Not address validation."""
    return value.count("@") == 1 and all(value.partition("@")[::2]) and "." in value.rpartition("@")[2]


def _capture_owui_identity(request: Request, principal: str | None, user_id: str) -> None:
    """Park the upstream display name/email on this seat. SAAS-only, silent, never raises."""
    try:
        if not (isinstance(principal, str) and principal.startswith("account:")):
            return                          # SAAS off / non-account principal → lane absent
        name = _decode_owui_name(_read_owui_header(request, _OWUI_NAME_HEADERS))
        email = _decode_owui_email(_read_owui_header(request, _OWUI_EMAIL_HEADERS))
        if not name and not email:
            return                          # the normal case when the flag is off upstream
        account_id = principal.split(":", 1)[1].strip()
        from saas.seats import record_owui_identity
        record_owui_identity(account_id, user_id, name=name or None, email=email or None)
    except Exception:
        return                              # decorative lane — a failure here is not a failure


def _control_plane_envelope(payload: dict) -> dict:
    """Wrap a CONTROL-PLANE refusal in the same tool-result envelope a dispatched tool gets.

    These refusals (serve-mode `read_only`, `quota_exceeded`, entitlement blocks) are returned
    BEFORE `_call_tool` runs, so they never met the choke point every dispatched result passes
    through. A reviewer drove the consequence live: `quota_exceeded` reached the caller on both
    HTTP doors with `isError` absent, while `server.status_is_failure("quota_exceeded")` was
    True and `/v1` mapped the same payload to HTTP 402 — three doors disagreeing about a write
    that did not happen.

    The classification decides, not this function: `read_only` is a MODE and stays unflagged;
    `quota_exceeded` is a refusal and is flagged. `structuredContent` is included because the
    write tools declare an `outputSchema`, and the 2026-07-28 spec requires such tools to
    provide structured results.
    """
    stamped = _mcp.stamp_is_error(dict(payload))
    envelope: dict = {
        "content": [{"type": "text", "text": json.dumps(stamped)}],
        "structuredContent": stamped,
    }
    if stamped.get("isError") is True:
        envelope["isError"] = True
    return envelope


def _tool_response(result: Any, status_code: int = 200,
                   tool_name: str | None = None) -> JSONResponse:
    """Return a tool result on the REST door, stamped with the same failure verdict as JSON-RPC.

    The two doors are different code paths — the REST endpoints call their handlers directly and
    never touch ``_call_tool`` — so anything the JSON-RPC dispatcher does for free has to be
    arranged here explicitly. Before this existed, the SAME failure (a tenant still provisioning,
    say) was detectable on one door and silent on the other, on the door OpenWebUI uses.

    ``stamp_is_error`` is the single place the question "did this fail?" is answered, keyed on
    the result's own ``status``, so a new exit is classified whether or not anyone remembers to
    flag it. Endpoints whose tool sits in a zero-count lane (forget/retract/correct) pass their
    ``tool_name`` so the third arm — "did this operation actually do anything?" — reaches this
    door too.
    """
    return JSONResponse(_mcp.stamp_is_error(result, tool_name), status_code=status_code)


def _rest_validation_error(tool_name: str, arguments: dict) -> JSONResponse | None:
    """Run the SAME input validation the JSON-RPC door runs, or return None if the input is fine.

    The two doors had drifted apart in a way that is invisible from either side alone. Every
    JSON-RPC call goes through ``_call_tool``, which validates first; every REST endpoint calls
    its handler DIRECTLY, so ``_validate_tool_input`` never ran here at all — an empty query
    reached the backend and came back as a cheerful "I don't have any information about that",
    which is a wrong answer dressed as a right one. OpenWebUI uses this door, so it was the
    door where it mattered most.

    400 is the honest status for input the server refuses to act on; the body keeps the same
    ``{"error": ...}`` shape the JSON-RPC door reports, so a client sees one vocabulary.
    """
    err = _mcp._validate_tool_input(tool_name, arguments)
    if err is None:
        return None
    _log(f"REST {tool_name} rejected: {err.get('error')}")
    return JSONResponse(err, status_code=400)


def _resolve_rest_user_id(request: Request, body_user_id: str, principal: str | None) -> str:
    """Resolve the tenant for a REST shorthand call via the SAME identity seam as /mcp.

    Reads X-OpenWebUI-User-Id (which OpenWebUI stamps on the REST path too — previously
    dropped here, DEV/SECURITY-multiuser-tenant-isolation.md Finding 1), falling back to
    body.user_id, then runs it through bind_tenant() (spoof-guard + UUID validation +
    FAULTLINE_USER_ID single-user fallback). Translates a spoof/malformed rejection into
    the matching HTTP status — fail loud, never silently route to a wrong/shared tenant.

    CLIENT LABEL SEAM (owner ruling 2026-08-15 — label, never a gate): every REST tool
    route (and the whole /v1 facade via _resolve_v1_user_id, and /customer_chat) funnels
    through HERE — so capturing the client identity at the top of this function labels
    every REST door with no route left behind. Traceability only; capability is unchanged
    whatever the name.
    """
    _capture_client_class(request)
    claimed = request.headers.get("X-OpenWebUI-User-Id", "") or body_user_id
    try:
        resolved = _mcp.bind_tenant(principal, claimed)
        # Refine the event-middleware tag with the concrete resolved tenant user.
        _stash_saas_principal_state(request, principal, resolved)
        # Park the upstream display name/email on the seat (SAAS only, silent, never fatal).
        # AFTER bind_tenant: the seat must be authorized before we write anything against it.
        _capture_owui_identity(request, principal, resolved)
        return resolved
    except _mcp.TenantSpoofError as exc:
        client = request.client.host if request.client else "unknown"
        _log(f"REST {exc.status_code} from {client} — {exc.message}")
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


# Literal fallback used ONLY when SAAS is on but the gate module errors (fail-closed block).
_SAAS_READ_ONLY_FALLBACK = {"status": "read_only", "detail": "agent mode: serve (ingest disabled)"}


def _saas_write_block(principal: str | None, user_id: str, tool_name: str) -> dict | None:
    """Agent-mode ingest gate at the MCP WRITE tools (SAAS_MODE only).

    Returns the read-only response dict when the write must be blocked, else None.
    SAAS_MODE OFF → always None (today's behavior EXACTLY — the gate is absent). In SAAS
    mode the decision is fail-CLOSED: any gate error blocks the write (writes NOTHING).
    ``recall_memory`` is never passed here (it must not consult the gate).
    """
    try:
        from saas.config import saas_mode_enabled
        saas_on = saas_mode_enabled()
    except Exception:
        saas_on = False
    if not saas_on:
        return None
    try:
        from saas.agent_mode import write_gate_block
        return write_gate_block(principal, user_id, tool_name)
    except Exception:
        return _SAAS_READ_ONLY_FALLBACK


def _saas_serve_block(principal: str | None, user_id: str, tool_name: str) -> dict | None:
    """Serve-mode TOOL-SURFACE gate (SAAS_MODE only) — the call-time boundary.

    A seat whose ingest valve is OFF may use ``recall_memory`` and nothing else. Returns the
    read-only response dict when the call must be refused, else None. This backstops the
    ``tools/list`` filter below: a client's tool list is a client-side CACHE, so a hidden tool
    that is called anyway must still be refused here, server-side.

    SAAS_MODE OFF / the saas module absent → always None (open core: today's behavior EXACTLY,
    the gate is absent). In SAAS mode the decision is fail-CLOSED: any gate error refuses.
    """
    try:
        from saas.config import saas_mode_enabled
        saas_on = saas_mode_enabled()
    except Exception:
        saas_on = False
    if not saas_on:
        return None
    try:
        from saas.agent_mode import serve_tool_block
        return serve_tool_block(principal, user_id, tool_name)
    except Exception:
        return _SAAS_READ_ONLY_FALLBACK


def _saas_customer_chat_block(principal: str | None, user_id: str,
                              account_id: "str | None" = None) -> dict | None:
    """CONSTRAINED customer-agent ENTITLEMENT gate (SAAS_MODE only) — the call-time boundary.

    ``customer_chat`` is the SaaS moat's customer-facing lane, and it is HINGED ON AGENT SEATS
    exactly like ``/v1/agent/chat``: the bound seat must be a live ``seat_type='agent'`` seat
    AND the account must hold the chat-agent feature (paid or a live trial) with a provisioned
    seat bucket. This runs the SAME ``agent_entitlement.serve_allowed`` gate the /v1 chat proxy
    runs, so the MCP/REST customer door can never reach a constrained pipeline on a STANDARD
    seat or an un-entitled account. Returns the refusal dict when the call must be refused,
    else None. Fail-closed: any gate error refuses."""
    try:
        from saas.config import saas_mode_enabled
        saas_on = saas_mode_enabled()
    except Exception:
        saas_on = False
    if not saas_on:
        return None
    # Resolve the account when the caller did not already (tools/list has no bind_tenant by
    # design; the tools/call branch resolves it explicitly). An unresolvable account is a
    # fail-closed refusal — never advertise/serve the lane for a seat we cannot vouch for.
    if not account_id:
        account_id = _saas_account_for(principal, user_id)
    if not account_id:
        return {"status": "not_entitled", "reason_code": "no_account",
                "message": "no account context for this seat."}
    try:
        from saas import agent_entitlement as _ae
        allowed, refusal = _ae.serve_allowed(account_id, user_id)
    except Exception:
        allowed, refusal = False, None
    if allowed:
        return None
    r = refusal or {}
    return {
        "status": "not_entitled",
        "reason_code": str(r.get("reason_code") or "agent_feature_off"),
        "message": str(r.get("message") or "the constrained customer-agent lane is not "
                       "available for this account."),
    }


def _saas_visible_tools(principal: str | None, user_id: str, tools: list) -> list:
    """Serve-mode ``tools/list`` filter (SAAS_MODE only) — the affordance.

    A frozen (ingest OFF) seat is shown ONLY the tools it may actually use. Returns ``tools``
    UNCHANGED when the saas module is absent, when SAAS_MODE is off, or on ANY error — the
    open core has no seats and no ingest valve, so its tool list must stay exactly as it is
    today. Decision logic lives in ``saas.agent_mode.visible_tools``; this only asks.

    ⚠️ CACHING CAVEAT — ``notifications/tools/list_changed`` is NOT sent on this transport.
    MCP clients cache ``tools/list``, so a valve flipped mid-session leaves the client holding
    a stale list until it re-lists. There is no fix available here: this transport is STATELESS
    by design — it issues no ``Mcp-Session-Id`` and ``GET /mcp`` answers 405 (no SSE stream, see
    mcp_endpoint_get), so the server has NO channel on which to push an unsolicited
    notification to a connected client. The stale list is an AFFORDANCE error only, never a
    capability one: ``_saas_serve_block`` re-decides on every single call, so a client acting
    on a stale list is refused at the call.
    """
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return tools
    except Exception:
        return tools
    try:
        from saas.agent_mode import visible_tools
        filtered = visible_tools(principal, user_id, tools)
    except Exception:
        filtered = tools
    # CONSTRAINED CUSTOMER-AGENT TOOL (SAAS only, moat): the constrained chat lane is served
    # as an MCP tool on this server. Append it AFTER the serve-mode filter so a frozen (serve)
    # seat still sees it (chat is a READ of the brain — serve seats may chat), and a public
    # build without saas/ never advertises it. HINGED ON AGENT SEATS: only advertised when the
    # seat is entitled (the same serve_allowed gate the call-time boundary runs), so a standard
    # seat is never even shown a tool its call would be refused. Its call-time enforcement
    # lives in the tools/call branch + saas/agent_mcp_tool.py.
    try:
        from saas.agent_mcp_tool import TOOL_SCHEMA as _customer_schema
        if _saas_customer_chat_block(principal, user_id) is not None:
            return filtered
        out = [t for t in filtered]
        if not any(isinstance(t, dict) and t.get("name") == "customer_chat" for t in out):
            out = [_customer_schema] + out
        return out
    except Exception:
        return filtered


def _saas_account_for(principal: str | None, user_id: str) -> str | None:
    """Resolve the account_id for a principal/seat (SAAS only). None on miss/error."""
    try:
        from saas.agent_mode import account_id_for_principal
        return account_id_for_principal(principal, user_id)
    except Exception:
        return None


def _saas_quota_block(principal: str | None, user_id: str, tool_name: str) -> dict | None:
    """HARD usage-cap pre-check at the WRITE tools (SAAS_MODE only), checked AFTER agent-mode.

    Returns a ``quota_exceeded`` payload when a memories_kept / topics_trained HARD cap is
    reached (block the write; recall is never gated), else None. FAIL-OPEN by design: SAAS off
    or ANY error → None (allow) — a control-plane hiccup must never deny a paying customer.
    """
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return None
    except Exception:
        return None
    try:
        from saas import metering, seats as _seats
        account_id = _saas_account_for(principal, user_id)
        if not account_id:
            return None
        seat_type = _seats.seat_type_of(account_id, user_id)
        return metering.hard_cap_block(account_id, user_id, seat_type, tool_name)
    except Exception:
        return None  # fail-open


def _saas_api_addon_block(principal: str | None, user_id: str) -> dict | None:
    """Fail-CLOSED API-access add-on gate for the /v1 developer API (SAAS_MODE only).

    Returns a ``api_not_enabled`` payload when the account has NOT enabled the paid API add-on,
    else None. SAAS_MODE OFF → always None (no-op: self-host has no billing — /v1 is just the
    open-core third door). Called by the /v1 router AFTER auth+BOLA and BEFORE the tool + meter,
    so a disabled account is never served and never billed.

    Deliberately UNLIKE ``_saas_quota_block`` (which is fail-OPEN): if we cannot CONFIRM the door
    is open — for ANY reason — we keep it shut. Refusing a call the customer may not have consented
    to is safe; serving one they didn't buy is the exact harm the add-on lever forbids.
    """
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return None                      # open-core / self-host: no-op
    except Exception:
        return None
    try:
        from saas import api_addon
        account_id = _saas_account_for(principal, user_id)
        # LEVEL 1 — the account CEILING (migration 160): did the account buy /v1 at all?
        if not account_id or not api_addon.api_enabled(account_id):
            return {"status": "api_not_enabled",
                    "detail": "API access is not enabled for this account — a billing admin "
                              "can enable it in Plan & Capacity.",
                    "enable_at": "/dashboard/tenant#plan"}
        # LEVEL 2 — the per-SEAT GRANT (migration 161): the account bought API, but THIS seat
        # must ALSO be granted (least-privilege; default OFF). This is the "Jenn from accounting"
        # firewall — an unused seat's flu_ token is never a live API door just because the
        # account add-on is on. Distinct code so the caller can tell the two states apart.
        if not api_addon.seat_api_enabled(account_id, user_id):
            return {"status": "seat_api_not_enabled",
                    "detail": "API access is not enabled for this seat — a tenant admin can "
                              "enable it on the seat.",
                    "enable_at": "/dashboard/tenant#seats"}
        return None
    except Exception:
        # FAIL-CLOSED (opposite of the quota gate): unverifiable → keep the door shut.
        return {"status": "api_not_enabled",
                "detail": "API access could not be verified for this account.",
                "enable_at": "/dashboard/tenant#plan"}


def _bearer_token_from_request(request: Request) -> str:
    """Extract the raw presented bearer token from the Authorization header (empty if none).
    Used ONLY by the clinical-lane scope gate — the memory path never re-reads the raw key."""
    h = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    parts = h.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _saas_account_for_principal(principal: str | None) -> str | None:
    """Resolve the account_id for a principal with no seat context (the /v1/patients verbs act at
    the ACCOUNT level via an flk_ key). None on miss (fail-closed at the gate)."""
    return _saas_account_for(principal, "")


def _saas_patient_provisioning_block(principal: str | None, request: Request) -> dict | None:
    """Fail-CLOSED authority gate for the clinical /v1/patients mint/deprovision verbs
    (SAAS_MODE only). Returns a block payload (mapped to a clean, NON-billed 4xx by the /v1
    envelope) unless ALL hold, else None (allow). Mirrors ``_saas_api_addon_block``'s fail-closed
    asymmetry — any doubt keeps the door shut. Composes THREE levels (DESIGN §1.2/§6.1):

      L0 — the general /v1 API add-on account ceiling is ON (the lane sits BEHIND /v1); this is
           also what makes patient MEMORY calls work (the mint arms the patient seat's /v1 grant,
           which requires this ceiling).
      L1 — the patient-lane account ceiling (accounts.patient_provisioning_enabled) is ON.
      L2 — the PRESENTED KEY carries the 'provision_patients' scope (an ordinary memory key, or an
           flu_ per-user token, never does → insufficient_scope).

    SAAS off → the lane is a SaaS feature; return a patient_lane_disabled block (the /v1 patient
    routes only exist meaningfully under SaaS)."""
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return {"status": "patient_lane_disabled",
                    "detail": "the clinical patient lane is a SaaS feature"}
    except Exception:
        return {"status": "patient_lane_disabled",
                "detail": "the clinical patient lane could not be verified"}
    try:
        from saas import api_addon as _api_addon
        from saas import patients as _patients
        from saas.accounts import key_scopes as _key_scopes
        account_id = _saas_account_for_principal(principal)
        if not account_id:
            return {"status": "patient_lane_disabled",
                    "detail": "no account context for this credential"}
        # L0 — the general /v1 API add-on ceiling (the lane composes behind it).
        if not _api_addon.api_enabled(account_id):
            return {"status": "api_not_enabled",
                    "detail": "enable the /v1 API access add-on before the clinical lane.",
                    "enable_at": "/dashboard/tenant#plan"}
        # L1 — the patient-lane account ceiling.
        if not _patients.patient_lane_enabled(account_id):
            return {"status": "patient_lane_disabled",
                    "detail": "the clinical patient lane is not enabled for this account.",
                    "enable_at": "/dashboard/tenant#plan"}
        # L2 — the presented key must carry the provisioning scope.
        token = _bearer_token_from_request(request)
        scopes = _key_scopes(token) if token else []
        if _patients.PROVISION_SCOPE not in scopes:
            return {"status": "insufficient_scope",
                    "detail": "this key is not authorized to provision patients "
                              "(it needs the 'provision_patients' scope)."}
        return None
    except Exception:
        # FAIL-CLOSED: unverifiable → keep the door shut.
        return {"status": "patient_lane_disabled",
                "detail": "the clinical patient lane could not be verified."}


def _saas_patient_mint_attribution(request: Request) -> dict | None:
    """Resolve the presented provisioning key → ``{key_id, sub_admin_id}`` for patient-mint
    ATTRIBUTION (migration 170). SaaS-only; best-effort — any miss/error → None so an authorized
    mint still proceeds (unattributed) rather than failing. NEVER returns the key value."""
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return None
        from saas.accounts import resolve_key_attribution
        token = _bearer_token_from_request(request)
        if not token:
            return None
        rec = resolve_key_attribution(token)
        if not rec:
            return None
        return {"key_id": rec.get("key_id"),
                "sub_admin_id": rec.get("owner_dashboard_user_id")}
    except Exception:
        return None


async def _saas_meter(
    principal: str | None, user_id: str, tool_name: str, api: bool = False
) -> None:
    """Best-effort post-success flow-meter bump (SAAS_MODE only). NEVER raises, NEVER blocks.

    ``api=True`` (the /v1 developer-API door passes it) additionally bumps the transport-scoped
    ``api_calls`` meter. MCP/REST-shim callers leave it False → their metering is unchanged.
    """
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return
        from saas import metering
        account_id = _saas_account_for(principal, user_id)
        await metering.bump_for_tool(account_id, user_id, tool_name, api=api)
    except Exception:
        pass


@app.post(
    "/recall_memory",
    summary="Recall facts from FaultLine knowledge graph",
    description=(
        "Call at the START of a turn to look up what you already know when the user "
        "asks about or references something you may know about them, their people, or "
        "their world. Not inert: text may be captured and corrections acted on. To SAVE "
        "deliberately, use remember_facts. "
        "The result is what you already know about the user from previous conversations. "
        "Draw on it naturally to help them: speak to the user directly (address them as "
        "'you'), weave it into flowing prose in your own words, and use it only where it "
        "is relevant. It describes the user, not you — treat it as your own knowledge, "
        "never as retrieved data. Anything explicitly marked less certain is unconfirmed: "
        "raise it only if relevant and phrase it tentatively. "
        "Pass the HUMAN user's own message VERBATIM and in full as `query` — never reduce it "
        "to a keyword or topic, and never your own working text; the backend extracts the "
        "topic and detects corrections itself."
        + tool_description_suffix("recall_memory")
    ),
)
async def rest_recall_memory(
    request: Request, body: RecallRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST recall_memory user_id={user_id[:8]}...")
    _invalid = _rest_validation_error("recall_memory", {"query": body.query, "user_id": user_id})
    if _invalid is not None:
        return _invalid
    # Provisioning gate — the REST path (OpenWebUI's live door) must wait out provisioning
    # too, else a fresh tenant's first recall races the schema. Backend _ensure_tenant_ready
    # is the real guard; this returns the clean retry signal without a doomed backend call.
    _gate = await _mcp._ensure_provisioned(user_id)
    if not _gate:  # honest wait OR terminal failure — rendered by the ONE envelope
        return _tool_response(_mcp.provisioning_gate_envelope(_gate, facts=[]))
    result = await _mcp.recall_memory_tool(query=body.query, user_id=user_id)
    await _saas_meter(_principal, user_id, "recall_memory")
    return _tool_response(result)


@app.post(
    "/cortex_remember",
    summary="Record your OWN operational note so you stop repeating mistakes (Agent Cortex)",
    description=(
        "Record YOUR OWN operational note — a lesson about how to OPERATE, so you stop "
        "repeating your own mistakes: a gotcha, a command that failed (and the fix), a "
        "pitfall, a correction, a how-to, a command overrun. Write the note YOURSELF, in "
        "your own words — NEVER paste the user's raw message, terminal output, or a "
        "transcript; a note you write yourself cannot carry their payload. If their exact "
        "words matter as a fact, that belongs in remember_facts, not here. This is YOUR "
        "private working memory (like your own .claude notes): it is NOT saved as the "
        "user's fact and is never recalled as something they told you. Stance is always "
        "'my operational note,' never 'you told me.'"
    ),
)
async def rest_cortex_remember(
    request: Request, body: CortexRememberRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    # AGENT-INTERNAL: cortex_remember is NOT a WRITE_TOOL (it is not a user-fact surface; it
    # lands in the firewalled cortex schema), so _saas_write_block does not govern it. It IS
    # governed by the serve-mode TOOL SURFACE: a frozen seat gets recall_memory and nothing
    # else, and this REST shorthand is the same capability behind a different door — gating
    # only the MCP tool would leave the door open.
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST cortex_remember user_id={user_id[:8]}...")
    _invalid = _rest_validation_error("cortex_remember", {"text": body.text, "user_id": user_id})
    if _invalid is not None:
        return _invalid
    _blocked = _saas_serve_block(_principal, user_id, "cortex_remember")
    if _blocked is not None:
        _log(f"REST cortex_remember BLOCKED (serve mode) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    _gate = await _mcp._ensure_provisioned(user_id)
    if not _gate:  # honest wait OR terminal failure — rendered by the ONE envelope
        return _tool_response(_mcp.provisioning_gate_envelope(_gate, written=False))
    result = await _mcp.cortex_remember_tool(
        text=body.text, user_id=user_id,
        category=(body.category or None),
        tags=(body.tags or None),
        evidence=(getattr(body, "evidence", None) or None),
        client_source=(body.client_source or None),
        branch=(body.branch or None),
        env=(getattr(body, "env", "") or None),
        owner_quote=(body.owner_quote or None),
        # SCHEMA UPLIFT provenance (owner directive; migration 301) — same door-parity as the
        # MCP-native surface: the OpenAPI shorthand forwards what the model self-tags; the
        # deployment auto-stamp (CORTEX_AGENT_MODEL / CORTEX_PROJECT) applies inside the tool.
        model=(getattr(body, "model", "") or None),
        type=(getattr(body, "type", "") or None),
        project=(getattr(body, "project", "") or None),
    )
    return _tool_response(result)


@app.post(
    "/cortex_recall",
    summary="Recall your OWN operational notes before acting (Agent Cortex)",
    description=(
        "Look up YOUR OWN operational notes before acting (gotchas, failed commands, "
        "pitfalls, corrections, how-tos you recorded with cortex_remember). Before acting "
        "on a task like one you have done before, call this FIRST — before any "
        "recall_memory. Compose your OWN `query` — describe the task or situation in your "
        "own words; keywords are fine. Deterministic keyword + tag match, top-K. These are "
        "your private working notes — hold them tentatively and use them to operate better; "
        "they are NOT the user's facts. To recall what the USER told you, use "
        "recall_memory."
    ),
)
async def rest_cortex_recall(
    request: Request, body: CortexRecallRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    # Same serve-mode tool-surface gate as the MCP tool: cortex_recall is a READ, but it is a
    # read of the AGENT's own notes, not of user memory, and it is outside SERVE_MODE_TOOLS.
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST cortex_recall user_id={user_id[:8]}...")
    _invalid = _rest_validation_error("cortex_recall", {"query": body.query, "user_id": user_id})
    if _invalid is not None:
        return _invalid
    _blocked = _saas_serve_block(_principal, user_id, "cortex_recall")
    if _blocked is not None:
        _log(f"REST cortex_recall BLOCKED (serve mode) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    _gate = await _mcp._ensure_provisioned(user_id)
    if not _gate:  # honest wait OR terminal failure — rendered by the ONE envelope
        return _tool_response(_mcp.provisioning_gate_envelope(_gate, memory=""))
    _cs = (body.client_source or "").strip() or None
    result = await _mcp.cortex_recall_tool(
        query=body.query, user_id=user_id,
        bump=bool(getattr(body, "bump", True)),
        # If the REST client identifies itself, self-scope to "mine"; otherwise today's "all".
        scope=("mine" if _cs else "all"),
        client_source=_cs,
        branch=(getattr(body, "branch", "") or None),
        env=(getattr(body, "env", "") or None),
    )
    return _tool_response(result)


@app.post(
    "/remember_facts",
    summary="Store facts from conversation into FaultLine knowledge graph",
    description=(
        "Save something the user just told you. Call this whenever the user states a "
        "fact about themselves, another person, or their world — a name, relationship, "
        "preference, job, possession, or location — even mentioned in passing, and also "
        "when they correct a prior fact. Default to calling it; skip only pure questions "
        "or chitchat. Do not ask permission first. "
        "IMPORTANT — pass a HUMAN being's own message in NATURAL LANGUAGE, verbatim: do NOT "
        "extract, summarize, or restructure it into facts, triples, or line items yourself, "
        "and NEVER pass your own working text (briefs, plans, output — those go to "
        "cortex_remember). FaultLine's engine does all extraction, validation, and "
        "structuring; it needs the raw sentence(s). "
        "(The only tool where you generate structured statements is learn_facts.)"
        + tool_description_suffix("remember_facts")
    ),
)
async def rest_remember_facts(
    request: Request, body: RememberRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST remember_facts user_id={user_id[:8]}...")
    _invalid = _rest_validation_error("remember_facts", {"text": body.text, "user_id": user_id})
    if _invalid is not None:
        return _invalid
    _blocked = _saas_write_block(_principal, user_id, "remember_facts")
    if _blocked is not None:
        _log(f"REST remember_facts BLOCKED (agent mode: serve) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    _quota = _saas_quota_block(_principal, user_id, "remember_facts")
    if _quota is not None:
        _log(f"REST remember_facts QUOTA {_quota.get('metric')} user_id={user_id[:8]}...")
        return _tool_response(_quota)
    # Provisioning gate — wait out provisioning on the REST path so a fresh tenant's first
    # remember does not race the schema. Backend _ensure_tenant_ready is the authoritative guard.
    _gate = await _mcp._ensure_provisioned(user_id)
    if not _gate:  # honest wait OR terminal failure — rendered by the ONE envelope
        return _tool_response(_mcp.provisioning_gate_envelope(_gate, committed=0))
    result = await _mcp.remember_facts_tool(text=body.text, user_id=user_id)
    await _saas_meter(_principal, user_id, "remember_facts")
    return _tool_response(result)


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
    _invalid = _rest_validation_error("ingest_document", {"text": body.text, "user_id": user_id})
    if _invalid is not None:
        return _invalid
    _blocked = _saas_write_block(_principal, user_id, "ingest_document")
    if _blocked is not None:
        _log(f"REST ingest_document BLOCKED (agent mode: serve) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    _quota = _saas_quota_block(_principal, user_id, "ingest_document")
    if _quota is not None:
        _log(f"REST ingest_document QUOTA {_quota.get('metric')} user_id={user_id[:8]}...")
        return _tool_response(_quota)
    # Provisioning gate — wait out provisioning on the REST path so a fresh tenant's first
    # document does not race the schema. Backend _ensure_tenant_ready is the authoritative guard.
    _gate = await _mcp._ensure_provisioned(user_id)
    if not _gate:  # honest wait OR terminal failure — rendered by the ONE envelope
        return _tool_response(_mcp.provisioning_gate_envelope(_gate, chunks=0))
    result = await _mcp.ingest_document_tool(
        text=body.text,
        user_id=user_id,
        source_ref=body.source_ref,
        title=body.title,
    )
    await _saas_meter(_principal, user_id, "ingest_document")
    return _tool_response(result)


class DocumentStatusRequest(BaseModel):
    document_id: int | None = Field(None, description="OPTIONAL: report one document only. Omit for all.")
    user_id: str = ""


class IngestFileB64Request(BaseModel):
    data_b64: str = Field(..., description="The file's raw bytes as base64 — the whole file, not an excerpt. Required.")
    filename: str = Field("", description="OPTIONAL: the file's name (citation label).")
    source_ref: str = Field("", description="OPTIONAL: where the file came from (URL/path).")
    title: str = Field("", description="OPTIONAL: document title.")
    user_id: str = ""


@app.post("/ingest_file_b64")
async def rest_ingest_file_b64(
    request: Request, body: IngestFileB64Request, _principal: str = Depends(require_auth)
) -> JSONResponse:
    """The OpenAPI/tool-shorthand door for the binary lane (JSON base64).

    The raw-bytes `/ingest_file` endpoint cannot be expressed through the hand-built
    OpenAPI generator (it hardcodes application/json), so OpenWebUI-class clients get
    THIS JSON shape — same core, same gates, same best-effort contract."""
    from src.ingest import binary_intake as _bi
    if not _bi.binary_intake_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST ingest_file_b64 user_id={user_id[:8]}... b64chars={len(body.data_b64)} "
         f"name={body.filename!r}")
    _blocked = _saas_write_block(_principal, user_id, "ingest_file")
    if _blocked is not None:
        _log(f"REST ingest_file_b64 BLOCKED (agent mode: serve) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    _gate = await _mcp._ensure_provisioned(user_id)
    if not _gate:  # honest wait OR terminal failure — rendered by the ONE envelope
        return _tool_response(_mcp.provisioning_gate_envelope(_gate, chunks=0))
    result = await _mcp.ingest_file_tool(
        data_b64=body.data_b64, user_id=user_id, filename=body.filename,
        source_ref=body.source_ref, title=body.title)
    if result.get("status") not in ("rejected", "invalid_request", "soft_error"):
        await _saas_meter(_principal, user_id, "ingest_file")
    return _tool_response(result)


@app.post("/document_status")
async def rest_document_status(
    request: Request, body: DocumentStatusRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    result = await _mcp.document_status_tool(user_id=user_id, document_id=body.document_id)
    return _tool_response(result)


class IngestAttachmentRequest(BaseModel):
    """Server-side attachment bridge payload (OpenWebUI filter → FaultLine).

    The seat identity rides the X-OpenWebUI-User-Id header (the authoritative seam for
    every OWUI-originated call); body.user_id is accepted only as the same fallback the
    other REST doors honor. The credential is the TOOL-SERVER bearer — see the door
    docstring for why that is a different trust class from a seat flu_ token."""
    filename: str = Field("", description="The attachment's file name (citation label).")
    data_b64: str = Field(..., description="The file's raw bytes as base64 — the whole file, not an excerpt. Required.")
    source_ref: str = Field("", description="OPTIONAL: where the file came from (URL/path).")
    title: str = Field("", description="OPTIONAL: document title.")
    user_id: str = ""


@app.post(
    "/ingest/attachment",
    summary="Server-side attachment bridge: ingest an OWUI attachment at attach time",
    description=(
        "Authenticated server-to-server door for the OpenWebUI attachment filter. Accepts "
        "{filename, data_b64} plus the seat identity (X-OpenWebUI-User-Id) and delegates to "
        "the SAME ingest pipeline as ingest_file — original-bytes artefact retention, stub "
        "facts, parser stamp, identical response shape — so the MODEL never re-transmits a "
        "document; its job shrinks to a status check (bridge.status_tool). AUTH: the "
        "TOOL-SERVER credential (Authorization: Bearer == the OWUI tool-server connection "
        "key, MCP_API_KEY in the open core / the account key under SAAS_MODE). This is "
        "middleware-trusted server-to-server auth — DISTINCT from per-seat flu_ tokens: the "
        "caller is the OWUI platform acting FOR the seat it names in X-OpenWebUI-User-Id, "
        "and bind_tenant's BOLA firewall still refuses any seat outside the credential's "
        "account. Rate-limited per seat. FAILURE SEMANTICS (two tiers, deliberately): "
        "auth/rate-limit/disabled-door are HTTP status semantics (401/429/404, the same "
        "vocabulary every other door raises through require_auth, and exactly what the "
        "OWUI filter checks by status code); every HANDLER failure (bad b64, oversized, "
        "unsupported media, refused, unexpected exception) returns a tool envelope — "
        "status + message, never a raw traceback."
    ),
)
async def rest_ingest_attachment(
    request: Request, body: IngestAttachmentRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    """The OpenWebUI attachment filter's door (server-side parsing — owner directive).

    Users never wait for their local AI to parse/re-transmit documents: the FILTER (not
    the model) POSTs each freshly-attached file here at attach time, and this door runs
    the identical ingest_file pipeline. AUTH MODEL — READ BEFORE TOUCHING: the bearer is
    the TOOL-SERVER credential (MCP_API_KEY in the open core; the OWUI tool-server
    connection's account key under SAAS_MODE). That is MIDDLEWARE-TRUSTED SERVER-TO-SERVER
    auth and is DISTINCT from seat flu_ tokens — a flu_ token pins its caller to one bound
    UUID for direct per-seat clients; this door's caller is the OWUI platform itself, and
    the seat it acts for arrives in X-OpenWebUI-User-Id through the SAME bind_tenant BOLA
    firewall every other OWUI-originated call passes (a claimed UUID outside the
    credential's account → refused, never re-scoped).
    """
    from src.ingest import binary_intake as _bi
    if not _bi.binary_intake_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST ingest/attachment user_id={user_id[:8]}... b64chars={len(body.data_b64)} "
         f"name={body.filename!r}")
    if _mcp._attachment_bridge_rate_limited(user_id):
        _log(f"REST ingest/attachment 429 user_id={user_id[:8]}... — per-seat bridge "
             f"rate limit ({_mcp._INGEST_ATTACHMENT_RATE_LIMIT} per "
             f"{_mcp._INGEST_ATTACHMENT_RATE_WINDOW_S}s window)")
        raise HTTPException(
            status_code=429,
            detail="Too Many Requests",
            headers={"Retry-After": str(_mcp._INGEST_ATTACHMENT_RATE_WINDOW_S)},
        )
    _blocked = _saas_write_block(_principal, user_id, "ingest_file")
    if _blocked is not None:
        _log(f"REST ingest/attachment BLOCKED (agent mode: serve) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    _gate = await _mcp._ensure_provisioned(user_id)
    if not _gate:  # honest wait OR terminal failure — rendered by the ONE envelope
        return _tool_response(_mcp.provisioning_gate_envelope(_gate, chunks=0))
    try:
        result = await _mcp.ingest_attachment_bridge(
            user_id=user_id, filename=body.filename, data_b64=body.data_b64,
            source_ref=body.source_ref, title=body.title)
    except Exception as e:  # noqa: BLE001 — a filter is polling this door mid-chat; fail-safe
        # shape, never a raw 500: the OWUI inlet must stay conversational and the log
        # must carry the cause (public_detail = type name + correlation id, no secrets).
        _log(f"REST ingest/attachment FAILED user_id={user_id[:8]}... err={type(e).__name__}")
        return _tool_response({
            "status": "error", "isError": True,
            "message": _errors.public_detail(e, where="mcp.ingest_attachment.bridge",
                                             what=type(e).__name__)})
    if result.get("status") not in ("rejected", "invalid_request", "soft_error"):
        await _saas_meter(_principal, user_id, "ingest_file")
    return _tool_response(result)


class ReviewStructureRequest(BaseModel):
    kind: str = Field("", description="Optional filter: cues | rel_types | groupings | ladder. Omit for all.")
    cue: str = Field("", description="To retire one derived cue: its exact cue string as listed by a prior read. Requires category.")
    category: str = Field("", description="The exact category of the cue being retired. Required alongside cue.")
    reactivate: bool = Field(False, description="Restore a previously retired cue (undo).")
    user_id: str = ""


@app.post("/review_structure")
async def rest_review_structure(
    request: Request, body: ReviewStructureRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    result = await _mcp.review_structure_tool(
        user_id=user_id, kind=body.kind or None, cue=body.cue or None,
        category=body.category or None, reactivate=bool(body.reactivate),
    )
    return _tool_response(result)


class RetryDocumentRequest(BaseModel):
    document_id: int | None = Field(None, description="The document id to retry (from document_status). Optional if source_ref is given.")
    source_ref: str = Field("", description="Alternative: retry every partially-imported document with this URL/filename.")
    user_id: str = ""


@app.post("/retry_document")
async def rest_retry_document(
    request: Request, body: RetryDocumentRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST retry_document user_id={user_id[:8]}... doc={body.document_id} ref={body.source_ref!r}")
    # WRITE gate — retry re-runs the ingest lane, so it honors the same agent-mode freeze.
    _blocked = _saas_write_block(_principal, user_id, "retry_document")
    if _blocked is not None:
        _log(f"REST retry_document BLOCKED (agent mode: serve) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    result = await _mcp.retry_document_tool(
        user_id=user_id, document_id=body.document_id, source_ref=body.source_ref or None)
    if result.get("status") == "retry_queued":
        await _saas_meter(_principal, user_id, "retry_document")
    return _tool_response(result)


async def _read_body_capped(request: Request, cap: int) -> bytearray:
    """Stream the request body, refusing the moment it exceeds ``cap``.

    ``Content-Length`` IS NOT TRUSTED — it is a client-supplied claim, and honouring it
    would let a caller declare 1 KB and then send gigabytes. The cap is enforced against
    what has ACTUALLY ARRIVED, so the worst case is ``cap`` bytes of memory plus one
    inbound chunk, whatever the header said. This is the first endpoint in FaultLine that
    accepts attacker-chosen bytes; there was no body-size limit anywhere before it.
    """
    buf = bytearray()
    async for piece in request.stream():
        buf.extend(piece)
        if len(buf) > cap:
            raise HTTPException(
                status_code=413,
                detail=(f"file exceeds the {cap} byte limit — refused WHOLE. A truncated "
                        f"file is corrupt bytes that look like good bytes."),
            )
    return buf


@app.post(
    "/ingest_file",
    summary="Upload a real FILE (PDF or image) into FaultLine memory",
    description=(
        "Upload a PDF or image as RAW BYTES in the request body. The file itself is "
        "retained, its text (if any) is extracted deterministically and mined exactly as "
        "/ingest_document does, and each embedded figure is bound by page geometry to the "
        "text printed around it. Send the bytes as the body; set X-Filename for the name. "
        "No model of any kind reads the file."
    ),
)
async def rest_ingest_file(
    request: Request, _principal: str = Depends(require_auth)
) -> JSONResponse:
    """THE BINARY DOOR — the thing that did not exist.

    Stages A (retention) and A.5 (caption binding) were built and BLOCKED: both take bytes,
    and every door in the system took ``text: str``. FaultLine never saw a PDF, it saw a
    string some client had already extracted — and when that client was OpenWebUI's default
    (pypdf) an image-only PDF failed upstream with "The content provided is empty", so the
    failure never reached us at all.

    ── WHY RAW BYTES AND NOT MULTIPART OR BASE64 (a deliberate choice) ───────────────────
    * **multipart/form-data** is the reflex answer and it costs a NEW DEPENDENCY:
      FastAPI's ``UploadFile``/``File`` require ``python-multipart``, which is not in
      ``pyproject.toml``. A dependency is an owner decision, and multipart also cannot be
      expressed through this server's hand-built OpenAPI generator, which hardcodes
      ``application/json``.
    * **base64 in a JSON body** needs no dependency but inflates every upload by 33 %,
      forces the whole payload to be buffered and PARSED as one JSON string, and makes the
      size cap fight the JSON parser rather than the socket.
    * **raw body** needs nothing, streams natively so the cap is enforced as bytes arrive,
      and is the natural shape for a binary. Metadata rides headers. This is what a plain
      ``curl --data-binary @file.pdf`` sends.

    ── SECURITY POSTURE, STATED ─────────────────────────────────────────────────────────
    1. **Size capped WHILE STREAMING** (:func:`_read_body_capped`) — ``Content-Length`` is
       never trusted; 413 refuses the whole file rather than truncating it.
    2. **Type SNIFFED from content**, never from ``Content-Type`` or the extension
       (``binary_intake.resolve_media_type``). ALLOW-LIST, so an unknown format is refused
       by default. The client's claims are logged, not obeyed.
    3. **Filename reduced to a label** — separators, NULs and control characters stripped.
       Nothing here ever touches the filesystem: parsing runs over an in-memory buffer.
    4. **Container bombs bounded** — page and image counts are capped and exceeding one
       raises rather than silently truncating.
    5. **Nothing is executed** — pure-Python parsing, no rasteriser, no shell-out, no PDF
       JavaScript, no nested-container unpacking.
    6. **Extracted text re-enters through the existing injection guard** before any of it
       is ingested: text lifted out of a PDF is attacker-controlled prose and gets exactly
       the same treatment as text typed into ``remember_facts``.
    7. **Same tenancy, freeze, quota and metering gates as ``ingest_document``** — it is
       passed as the tool name deliberately, so a serve-mode (read-only) seat cannot write
       through the new door and a quota applies unchanged.

    Flag-gated by ``ARTEFACT_BINARY_INTAKE`` (default ON since 2026-08-21, the owner
    images-best-effort rollout): with it off the door answers
    404, which is byte-for-byte the behaviour of a build without this endpoint.
    """
    from src.ingest import binary_intake as _bi

    if not _bi.binary_intake_enabled():
        raise HTTPException(status_code=404, detail="Not Found")

    user_id = _resolve_rest_user_id(request, "", _principal)
    # A binary upload IS a document write: inherit the freeze/quota/metering gates whole
    # rather than minting a parallel set that could drift out of step with them.
    _blocked = _saas_write_block(_principal, user_id, "ingest_document")
    if _blocked is not None:
        _log(f"REST ingest_file BLOCKED (agent mode: serve) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    _quota = _saas_quota_block(_principal, user_id, "ingest_document")
    if _quota is not None:
        return _tool_response(_quota)

    data = bytes(await _read_body_capped(request, _bi.max_upload_bytes()))
    if not data:
        raise HTTPException(status_code=400, detail="empty body — send the file bytes")

    claimed_name = request.headers.get("X-Filename")
    source_ref = (request.headers.get("X-Source-Ref") or claimed_name or "").strip()
    title = (request.headers.get("X-Title") or "").strip()

    _gate = await _mcp._ensure_provisioned(user_id)
    if not _gate:  # honest wait OR terminal failure — rendered by the ONE envelope
        return _tool_response(_mcp.provisioning_gate_envelope(_gate, chunks=0))

    # ── THE SHARED CORE (2026-08-21): the same pipeline the base64 MCP tool door runs,
    #    lifted into src/mcp/server.py so the two transports cannot drift. The two
    #    whole-file refusals keep their HTTP shapes here.
    try:
        result = await _mcp.ingest_file_core(
            user_id=user_id, data=data, claimed_name=claimed_name or "upload",
            source_ref=source_ref, title=title)
    except _bi.UnsupportedMediaType as exc:
        _log(f"REST ingest_file REFUSED type user_id={user_id[:8]}... {exc}")
        # Both are PublicRefusal subclasses: the authored sentence passes the seam verbatim.
        raise HTTPException(status_code=415,
                            detail=_errors.public_detail(exc, where="rest.ingest_file.media_type"))
    except _bi.ArtefactBomb as exc:
        _log(f"REST ingest_file REFUSED structure user_id={user_id[:8]}... {exc}")
        raise HTTPException(status_code=422,
                            detail=_errors.public_detail(exc, where="rest.ingest_file.artefact_bomb"))

    await _saas_meter(_principal, user_id, "ingest_document")
    return _tool_response(result)


# ── MOVED to src/mcp/server.py (2026-08-21): the binary-door helpers live next to the
# shared `ingest_file_core` so the REST door and the base64 tool door cannot drift.
# These aliases keep every existing import/pin (tests reference them by this module)
# pointing at the ONE implementation.
_ingest_file_message = _mcp._ingest_file_message
_artefact_placements = _mcp._artefact_placements
_post_artefact_retain = _mcp._post_artefact_retain


@app.post(
    "/learn_facts",
    summary="Store LLM-generated ontological knowledge into FaultLine",
    description=(
        "Store knowledge the LLM generates as explicit ontological statements into the "
        "FaultLine knowledge graph with source=llm_learn. Use when the user asks to learn "
        "a topic. Generate statements in the forms: 'X is a subclass of Y', "
        "'X is an instance of Y', 'X is a part of Y' — one per line — then call this. "
        "Spanish forms are accepted too ('X es una subclase de Y', 'X es una instancia de Y', "
        "'X es parte de Y'). Facts are staged as Class B (llm_learn provenance) and confirmed "
        "over time. A no_facts response is a FORMAT error to retry, not a missing capability."
    ),
)
async def rest_learn_facts(
    request: Request, body: LearnRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST learn_facts user_id={user_id[:8]}...")
    _invalid = _rest_validation_error("learn_facts", {"text": body.text, "user_id": user_id})
    if _invalid is not None:
        return _invalid
    _blocked = _saas_write_block(_principal, user_id, "learn_facts")
    if _blocked is not None:
        _log(f"REST learn_facts BLOCKED (agent mode: serve) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    _quota = _saas_quota_block(_principal, user_id, "learn_facts")
    if _quota is not None:
        _log(f"REST learn_facts QUOTA {_quota.get('metric')} user_id={user_id[:8]}...")
        return _tool_response(_quota)
    # Provisioning gate (round 10): the one write door that skipped it — a fresh seat's first
    # learn raced the schema and surfaced a backend error instead of the honest envelope.
    _gate = await _mcp._ensure_provisioned(user_id)
    if not _gate:
        return _tool_response(_mcp.provisioning_gate_envelope(_gate, committed=0, staged=0, total=0))
    result = await _mcp.learn_facts_tool(text=body.text, user_id=user_id)
    await _saas_meter(_principal, user_id, "learn_facts")
    return _tool_response(result)


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
    _invalid = _rest_validation_error("retract_fact", {"text": body.text, "user_id": user_id})
    if _invalid is not None:
        return _invalid
    _blocked = _saas_write_block(_principal, user_id, "retract_fact")
    if _blocked is not None:
        _log(f"REST retract_fact BLOCKED (agent mode: serve) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    # ROUND 9: this door was UNGATED — a terminal/failed seat reached /retract/correct and came
    # back as the backend's own failure shape. The same one-line gate + shared renderer as
    # every other door (honest wait or terminal envelope; unreachable → proceed, the tool is loud).
    _gate = await _mcp._ensure_provisioned(user_id)
    if not _gate:
        return _tool_response(_mcp.provisioning_gate_envelope(_gate), tool_name="retract_fact")
    result = await _mcp.retract_fact_tool(text=body.text, user_id=user_id)
    return _tool_response(result, tool_name="retract_fact")


@app.post(
    "/forget_fact",
    summary="Permanently forget ONE specific named fact from FaultLine",
    description=(
        "Use ONLY when the user EXPLICITLY and deliberately asks you to forget or delete "
        "ONE specific fact about a NAMED target — e.g. 'forget my email address', "
        "'delete that I have a dog named Rex', 'forget that Vera is my spouse'. This "
        "tombstones the stored value you name (it is recoverable, not a hard wipe). "
        "You MUST name the target: pass `subject` (whose fact — 'me' for the user, or the "
        "named person/thing) AND `old_value` (the stored value to remove); `rel_type` "
        "narrows it further. A subject alone, or a relation alone, is refused. "
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
    _invalid = _rest_validation_error("forget_fact", {"subject": body.subject,
                                            "rel_type": body.rel_type,
                                            "old_value": body.old_value,
                                            "user_id": user_id})
    if _invalid is not None:
        return _invalid
    _blocked = _saas_write_block(_principal, user_id, "forget_fact")
    if _blocked is not None:
        _log(f"REST forget_fact BLOCKED (agent mode: serve) user_id={user_id[:8]}...")
        return _tool_response(_blocked)
    _gate = await _mcp._ensure_provisioned(user_id)  # round 9: gated like every other door
    if not _gate:
        return _tool_response(_mcp.provisioning_gate_envelope(_gate), tool_name="forget_fact")
    result = await _mcp.forget_fact_tool(
        user_id=user_id,
        subject=body.subject,
        rel_type=body.rel_type,
        old_value=body.old_value,
    )
    return _tool_response(result, tool_name="forget_fact")


class CustomerChatRequest(BaseModel):
    messages: list = Field(
        default_factory=list, description="OpenAI-shaped messages: [{\"role\": \"user\", "
                                          "\"content\": ...}]. The last user message is the turn "
                                          "to triage + answer.")
    user_id: str = Field("", description="Customer seat UUID — auto-resolved from "
                                         "X-OpenWebUI-User-Id when omitted.")


@app.post(
    "/customer_chat",
    summary="Chat with the tenant's CONSTRAINED customer agent",
    description=(
        "Send a customer's message to the tenant's CONSTRAINED customer agent (SAAS only). "
        "Every turn is triaged before it is answered: in-scope questions are answered ONLY "
        "from the tenant's ingested corpus; out-of-scope questions are declined and redirected "
        "to a human or the tenant's site; consequential turns (medical/legal/financial/safety) "
        "are refused and directed to the tenant's licensed professionals; the agent abstains "
        "when it cannot ground, saying plainly it is an informational tool, not a professional. "
        "This is the customer-facing lane — the caller's identity (via X-OpenWebUI-User-Id / "
        "ENABLE_FORWARD_USER_INFO_HEADERS) scopes which customer's memory is read."
    ),
)
async def rest_customer_chat(
    request: Request, body: CustomerChatRequest, _principal: str = Depends(require_auth)
) -> JSONResponse:
    """The CONSTRAINED customer-agent lane over the REST door (the OpenWebUI tool path).

    Resolves the customer's seat via the SAME bind_tenant identity seam (per-customer memory
    stays private and separated), then runs the REAL constrained pipeline server-side. Fails
    closed off-core (no account → refusal, never an ungrounded guess)."""
    user_id = _resolve_rest_user_id(request, body.user_id, _principal)
    _log(f"REST customer_chat user_id={user_id[:8]}...")
    try:
        account_id = _saas_account_for(_principal, user_id)
    except Exception:
        account_id = None
    if not account_id:
        # Through the same seam. This lane speaks `ok: false` rather than a `status`, so the
        # status-keyed verdict is a no-op on it and the flag has to be mapped explicitly.
        return _tool_response({"ok": False, "isError": True,
                               "reply": "I'm an informational tool — I could not identify your "
                                        "account context. A team member can help.",
                               "error": "no account context"})
    # HINGED ON AGENT SEATS — the same entitlement gate /v1/agent/chat runs
    # (seat_type='agent' + the chat-agent feature/bucket). A standard seat or an
    # un-entitled account must never reach the constrained customer lane.
    _cust_block = _saas_customer_chat_block(_principal, user_id, account_id)
    if _cust_block is not None:
        _log(f"REST customer_chat REFUSED (entitlement) user_id={user_id[:8]}... "
             f"reason={_cust_block.get('reason_code')}")
        return _tool_response(_cust_block)
    try:
        from saas.agent_mcp_tool import run_customer_chat
        out = await run_customer_chat(account_id, user_id, body.messages or [])
    except Exception as exc:
        _log(f"REST customer_chat FAILED user_id={user_id[:8]}... err={str(exc)[:160]}")
        out = {"ok": False,
               "reply": "I'm an informational tool, and something went wrong reaching my "
                        "information — a team member can help.",
               "hiccup": f"{type(exc).__name__}"}
    await _saas_meter(_principal, user_id, "customer_chat")
    # Through the same seam as every other tool result. This lane speaks its own vocabulary
    # (`ok: false` rather than a `status`), so the status-keyed verdict is a no-op on it — the
    # stamp is harmless here and keeps the door from being the one exception nobody re-checks.
    if isinstance(out, dict) and out.get("ok") is False:
        out["isError"] = True
    return _tool_response(out)


# ── Public developer API (/v1/*) — THIRD DOOR, thin facade over the same tools ──
# Additive: coexists with the OpenWebUI REST shim above and the /mcp JSON-RPC lane
# below. Re-implements NO memory logic — it authenticates + resolves the tenant with
# the SAME seams the REST shim uses (require_auth, _resolve_rest_user_id/bind_tenant,
# the _saas_* gates, _saas_meter) and calls the SAME tool functions. Mounted HERE, after
# every helper it needs is defined, so build_router receives live callables (no cycle).
def _resolve_v1_user_id(request: "Request", body_user_id: str, principal: str | None) -> str:
    """Tenant resolution for /v1 — honors X-FaultLine-User-Id in addition to the legacy
    X-OpenWebUI-User-Id, then funnels through the SAME _resolve_rest_user_id (bind_tenant)
    BOLA guard. Adding a header fallback only; the isolation decision is unchanged."""
    effective_body = body_user_id or request.headers.get("X-FaultLine-User-Id", "")
    return _resolve_rest_user_id(request, effective_body, principal)


try:
    from src.mcp import api_v1 as _api_v1

    # Clinical/patient lane (migration 169): pass the SaaS ``patients`` orchestration module when
    # available; absent (open-core / no saas dir) → the /v1/patients routes fail-closed 404. The
    # import is best-effort so a public build without the saas module still serves /v1.
    try:
        from saas import patients as _saas_patients_mod
    except Exception:
        _saas_patients_mod = None

    async def _late_bound_ensure_provisioned(user_id: str) -> bool:
        return await _mcp._ensure_provisioned(user_id)

    app.include_router(
        _api_v1.build_router(
            require_auth=require_auth,
            resolve_user_id=_resolve_v1_user_id,
            # LATE-BOUND ON PURPOSE. This used to pass the function OBJECT, which froze the
            # binding at import: `/v1`'s provisioning gate could not be substituted afterwards,
            # so a test patching `server._ensure_provisioned` patched a name this router had
            # already stopped consulting. A reviewer found the branch's own `/v1` test doing
            # exactly that — a green assertion against a seam that no longer existed. Resolving
            # the attribute per call makes the gate drivable, and matches how every other
            # collaborator here is looked up.
            ensure_provisioned=_late_bound_ensure_provisioned,
            write_block=_saas_write_block,
            quota_block=_saas_quota_block,
            api_addon_block=_saas_api_addon_block,
            meter=_saas_meter,
            tools=_mcp,
            log=_log,
            patient_provisioning_block=_saas_patient_provisioning_block,
            account_for=_saas_account_for_principal,
            patients=_saas_patients_mod,
            mint_attribution=_saas_patient_mint_attribution,
        )
    )

    @app.exception_handler(HTTPException)
    async def _v1_http_exception_handler(request: Request, exc: HTTPException):  # noqa: ANN202
        """Reshape auth/BOLA HTTPExceptions into the consistent /v1 error envelope, SCOPED
        to /v1 paths. Non-/v1 routes keep FastAPI's default {"detail": ...} body byte-for-byte."""
        if request.url.path.startswith("/v1"):
            resp = JSONResponse(
                _api_v1.http_exception_body(exc.status_code, exc.detail),
                status_code=exc.status_code,
            )
        else:
            resp = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if getattr(exc, "headers", None):
            for k, v in exc.headers.items():
                resp.headers.setdefault(k, v)
        return resp

    from fastapi.exceptions import RequestValidationError as _RequestValidationError
    from fastapi.encoders import jsonable_encoder as _jsonable_encoder

    @app.exception_handler(_RequestValidationError)
    async def _v1_validation_exception_handler(request: Request, exc: _RequestValidationError):  # noqa: ANN202
        """Malformed/invalid body on /v1 → the consistent {"error": {...}} envelope (400
        bad_request, matching /v1's own bad-input status). Non-/v1 routes keep FastAPI's
        default 422 {"detail": [...]} body byte-for-byte."""
        errors = _jsonable_encoder(exc.errors())
        if request.url.path.startswith("/v1"):
            return JSONResponse(
                {"error": {"code": "bad_request", "message": "Invalid request body", "detail": errors}},
                status_code=400,
            )
        return JSONResponse({"detail": errors}, status_code=422)

    _log("Public developer API mounted at /v1 (facade over the shared tools).")
except Exception as _v1_exc:  # pragma: no cover - defensive; /v1 must never break startup
    _log(f"/v1 API not mounted: {_v1_exc}")


# ── Phase 2A keystone — the through-FaultLine agent LLM-proxy + tenant-delegate. ────
# SAAS MOAT: mounted ONLY when the ``saas`` package imports; absent (open-core / no saas
# dir) → the two endpoints never register and 404 as unknown paths. Each handler ALSO
# fail-closes 404 when SAAS_MODE is off (the surface does not exist on the open core). The
# brain key is resolved server-side from account_llm_config / seat_llm_config (Fernet-
# encrypted) and NEVER enters the request/response. Reuses the SAME seat-resolution +
# account-resolution seams the /v1 facade above uses (require_auth + _resolve_v1_user_id +
# _saas_account_for), so BOLA + the flu_ token bind are identical to every other /v1 verb.
try:
    from saas import agent_proxy as _agent_proxy

    app.include_router(
        _agent_proxy.build_agent_proxy_router(
            require_auth=require_auth,
            resolve_user_id=_resolve_v1_user_id,
            account_for=_saas_account_for,
            log=_log,
        )
    )
    _log("Through-FaultLine agent proxy mounted at /v1/agent/chat + /v1/auth/tenant-delegate.")
except Exception as _agent_proxy_exc:  # pragma: no cover - defensive; never break startup
    _log(f"Agent proxy not mounted: {_agent_proxy_exc}")


@app.get("/mcp")
async def mcp_endpoint_get() -> Response:
    """Streamable HTTP requires the MCP endpoint to answer GET — with an SSE stream, or 405.

    MCP spec 2025-06-18, "Listening for Messages from the Server" §3, verbatim:

        The server MUST either return `Content-Type: text/event-stream` in response to this
        HTTP GET, or else return HTTP 405 Method Not Allowed, indicating that the server does
        not offer an SSE stream at this endpoint.

    and, earlier: "The server MUST provide a single HTTP endpoint path … that supports both
    POST and GET methods."

    We are STATELESS — every JSON-RPC request gets a single JSON response on POST, and there
    is no server-initiated stream to listen to. So 405 is the correct, spec-sanctioned answer.

    ⚠️ WITHOUT THIS ROUTE THE GET FELL THROUGH TO THE MARKETING CATCH-ALL AND 404'd, which is
    a spec violation and a materially different signal: per the backwards-compatibility
    section a 404 invites a client to go hunting for the DEPRECATED 2024-11-05 HTTP+SSE
    transport, while 405 says plainly "right endpoint, no SSE here". Deliberately NOT
    auth-gated: the answer is identical for every caller and carries nothing, and demanding
    credentials to be told "no stream here" only obscures transport negotiation.
    """
    return JSONResponse(
        status_code=405,
        content={"detail": "Method Not Allowed — this MCP endpoint does not offer an SSE "
                           "stream. Send JSON-RPC via POST /mcp."},
        headers={"Allow": "POST"},
    )


@app.delete("/mcp")
async def mcp_endpoint_delete() -> Response:
    """Session termination — we have no sessions, so 405, which the spec explicitly allows.

    MCP spec 2025-06-18, "Session Management" §5: a client leaving SHOULD send an HTTP DELETE
    to the MCP endpoint to terminate its session, and "The server MAY respond to this request
    with HTTP 405 Method Not Allowed, indicating that the server does not allow clients to
    terminate sessions." We are stateless and never issue `Mcp-Session-Id`, so there is
    nothing to terminate.

    ⚠️ AN EXPLICIT ROUTE IS REQUIRED — it cannot be left to fall through. The marketing
    catch-all is a Mount at "/", which FULLY matches /mcp for any verb and therefore beats
    Starlette's partial-match 405 for a registered path with an unregistered method. So DELETE
    landed on the catch-all and answered 404 — and 404 is a LOADED code here: §3/§4 of the
    same section make 404 mean "your session was terminated, start a new one with a fresh
    InitializeRequest". Answering that to a client trying to hang up would tell it to
    re-initialize instead.
    """
    return JSONResponse(
        status_code=405,
        content={"detail": "Method Not Allowed — this MCP endpoint is stateless and does not "
                           "issue sessions, so there is nothing to terminate."},
        headers={"Allow": "POST"},
    )


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
    # CLIENT IDENTITY (owner ruling 2026-08-15): capture the client name at the edge —
    # every JSON-RPC method below (tools/call included) runs in this request's context,
    # so write-path log lines (and the cortex) can label THIS request's client. mcp-name
    # first, else the User-Agent leading token; never blocks or gates the request.
    # (The initialize handler below additionally prefers params.clientInfo.name — belt and
    # braces for clients that identify only in the handshake body.)
    _capture_client_class(request)
    _hdr_name = request.headers.get("mcp-name")
    if _hdr_name:
        _log(f"Mcp-Name={_hdr_name!r}")
        # SEP-2243: Mcp-Name mirrors params.name (tools/call, prompts/get) or params.uri
        # (resources/read). A gateway may route, rate-limit or authorize on the HEADER while
        # the server executes the BODY — so a disagreement between them is not a cosmetic
        # nit, it is the two halves of the system acting on different requests. The spec
        # therefore makes validation a MUST. Absence stays tolerated (legacy callers send
        # no header); only a PRESENT header that contradicts the body is refused.
        _body_name = (params or {}).get("name") or (params or {}).get("uri")
        if _body_name and _premise.decode_header_value(_hdr_name) != str(_body_name):
            _log(f"name mismatch header={_hdr_name!r} body={_body_name!r} id={req_id!r}")
            return JSONResponse(
                _jsonrpc_error(req_id, _premise.ERROR_HEADER_MISMATCH,
                               f"Mcp-Name header {_hdr_name!r} disagrees with body name "
                               f"{_body_name!r}"),
                status_code=400,
            )

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
        # any other RPC — and is the stdio backward-compatibility probe. No auth-gated
        # state here: this is identity/capability advertisement, answered identically for
        # every caller (the seat claim is NOT consulted, mirroring tools/list).
        _log("server/discover")
        return JSONResponse(_jsonrpc_result(
            req_id, _premise.discover_result(), _meta=False))

    if method == "initialize":
        # Spec: echo the client's protocolVersion if we support it, else offer our latest.
        # We used to hardcode 2025-03-26 and ignore the request entirely.
        #
        # NOTE (2026-08-14, critic-verified): we deliberately do NOT read clientInfo.name
        # here for identity-label purposes. This transport is STATELESS — the initialize
        # request's context dies with that request, and initialize never carries a tool
        # call, so a label set here can never reach a later tools/call. The
        # per-request identity capture at the top of this handler (mcp-name header, else
        # User-Agent leading token) is the ONLY HTTP-edge signal; the handshake's
        # clientInfo IS captured on the stdio door (single-client process lifetime).
        _client_ver = (params or {}).get("protocolVersion")
        return JSONResponse(
            _jsonrpc_result(req_id, {
                "protocolVersion": _premise.negotiate_protocol_version(_client_ver),
                # Matches the stdio door AND server/discover. This said {"tools": {}} while
                # prompts/list answered on this transport, so a legacy client that handshakes
                # instead of calling server/discover was told the capability did not exist.
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": "faultline-mcp", "version": "1.0.0"},
                # THE PREMISE, at handshake — the ONLY channel that reaches a model BEFORE it
                # calls anything. We already had a teaching layer (the `HOW YOU OPERATE` block
                # from cortex_usage_seed), but it rides the RECALL RESPONSE: it only reaches a
                # model that has ALREADY called recall_memory. The guidance that teaches a model
                # when to call the tool was being delivered inside that tool's own output — so a
                # model that did not yet know why it would ever call recall never saw the brief
                # telling it when to call recall. That layer stays REACTIVE; this one is
                # PROACTIVE. They are complements. See src/mcp/premise.py.
                "instructions": _premise.SERVER_INSTRUCTIONS,
            })
        )

    elif method == "notifications/initialized":
        # Stateless HTTP: no persistent session to gate on.
        # Acknowledge with empty result (unlike stdio which sends no response).
        return JSONResponse(_jsonrpc_result(req_id, {}))

    elif method == "ping":
        return JSONResponse(_jsonrpc_result(req_id, {}))

    elif method == "prompts/list":
        # THE ADVERTISEMENT HAS TO BE TRUE. ``server/discover`` declares a ``prompts``
        # capability for BOTH doors (premise.discover_result), and the stdio door has
        # always served it — but this HTTP door answered ``-32601 Method not found``, so
        # every conforming client that believed the advertisement spent a round trip
        # discovering the claim was empty. Serving the same PROMPTS the stdio door serves
        # is the honest repair: deliver the capability rather than withdraw the claim.
        from .prompts import PROMPTS
        return JSONResponse(_jsonrpc_result(
            req_id, {
                "prompts": [
                    {
                        "name": p["name"],
                        "description": p.get("description", ""),
                        "arguments": p.get("arguments", []),
                    }
                    for p in PROMPTS
                ],
                # Same CacheableResult contract as tools/list — a prompt catalog is exactly
                # the kind of stable list a client should cache instead of re-polling.
                "ttlMs": _premise._LIST_TTL_MS,
                "cacheScope": _premise._LIST_CACHE_SCOPE,
            }))

    elif method == "prompts/get":
        from .prompts import PROMPTS
        _prompt_name = params.get("name", "")
        _prompt_args = params.get("arguments", {}) or {}
        _prompt_def = next((p for p in PROMPTS if p["name"] == _prompt_name), None)
        if _prompt_def is None:
            # -32602 (Invalid Params), matching the stdio door and the 2026-07-28
            # renumbering of not-found from -32002 to -32602.
            return JSONResponse(
                _jsonrpc_error(req_id, -32602, f"Prompt not found: {_prompt_name}"),
                status_code=404,
            )
        # Argument handling mirrors the stdio door: filter the caller's arguments down to the
        # prompt function's own signature, so an extra key is ignored on BOTH transports rather
        # than accepted on one and rejected on the other. A genuinely missing REQUIRED argument
        # still raises, and is reported as invalid params (-32602) on BOTH doors — stdio
        # answers the same case -32602 (run_mcp_server's TypeError handler); an earlier
        # revision of this comment claimed stdio answered -32603, which was the pre-fix
        # behaviour, not the current one.
        import inspect
        _fn = _prompt_def["fn"]
        _sig = inspect.signature(_fn)
        try:
            _text = _fn(**{k: v for k, v in _prompt_args.items() if k in _sig.parameters})
        except TypeError as exc:
            # A TypeError's text names our Python function and its parameter list — internal
            # shape, not the caller's business (CWE-209). The caller is owed WHICH required
            # argument is missing, and the signature we already hold says that exactly.
            _missing = sorted(
                n for n, prm in _sig.parameters.items()
                if prm.default is inspect.Parameter.empty
                and prm.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                and n not in _prompt_args)
            _why = (f"missing required argument(s): {', '.join(_missing)}" if _missing
                    else _errors.public_detail(exc, where="mcp.prompts_get.arguments",
                                               what="arguments not accepted"))
            return JSONResponse(
                _jsonrpc_error(req_id, -32602,
                               f"Invalid arguments for prompt {_prompt_name!r}: {_why}"),
                status_code=400,
            )
        except Exception as exc:
            # Door parity: stdio wraps prompt execution in a broad handler and reports a failure
            # as -32603. Not reachable with today's three prompts (they are pure string
            # formatters), but without it a future prompt that raises would surface as an
            # unhandled 500 on HTTP and a clean JSON-RPC error on stdio — the two doors would
            # disagree about the same failure.
            _log(f"prompts/get {_prompt_name!r} raised {type(exc).__name__}: {exc}")
            return JSONResponse(
                _jsonrpc_error(req_id, -32603,
                               _errors.public_detail(exc, where="mcp.prompts_get.execute",
                                                     what="Prompt execution failed")),
                status_code=500,
            )
        return JSONResponse(_jsonrpc_result(
            req_id, {
                "description": _prompt_def.get("description", ""),
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": _text}}
                ],
            }))

    elif method == "tools/list":
        # Serve-mode filter (SAAS only): a seat with ingest OFF is advertised ONLY the tools
        # it may use. The seat is the authenticated principal plus the claimed OWUI user id —
        # read RAW here rather than via bind_tenant(), because bind_tenant has side effects
        # (seat auto-provisioning) that must not fire on a mere listing, and because a forged
        # claim can only ever hide MORE tools, never reveal any. Outside SAAS this is the
        # untouched _mcp.TOOLS constant.
        _seat_claim = request.headers.get("X-OpenWebUI-User-Id", "") or ""
        return JSONResponse(_jsonrpc_result(
            req_id, {
                # CacheableResult (2026-07-28, SEP-2549): ttlMs + cacheScope let clients cache
                # the tool catalog and reduce polling; deterministic order (TOOLS list) keeps
                # prompt-cache hit rates stable. Non-breaking for legacy clients.
                "tools": _saas_visible_tools(_principal, _seat_claim, _mcp.TOOLS),
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
        # Refine the event-middleware tag with the concrete resolved tenant user.
        _stash_saas_principal_state(request, _principal, resolved_user_id)
        # Park the upstream display name/email on the seat (SAAS only, silent, never fatal).
        # AFTER bind_tenant: the seat must be authorized before we write anything against it.
        _capture_owui_identity(request, _principal, resolved_user_id)
        # Agent-mode ingest gate (SAAS_MODE only) at the WRITE tools. recall_memory and every
        # read tool pass through untouched; a blocked write returns a clean read-only tool
        # result (HTTP 200, JSON-RPC success) and dispatches NOTHING to the backend.
        _blocked = _saas_write_block(_principal, resolved_user_id, tool_name)
        if _blocked is not None:
            _log(f"tools/call name={tool_name!r} BLOCKED (agent mode: serve) "
                 f"user_id={resolved_user_id[:8]}...")
            return JSONResponse(_jsonrpc_result(req_id, _control_plane_envelope(_blocked)))
        # Serve-mode TOOL-SURFACE gate (SAAS only) — the boundary behind the tools/list filter.
        # A frozen seat may call recall_memory and NOTHING else; anything else is refused here
        # even if the client's cached tool list still advertises it. Same clean read-only
        # result shape as the write gate above (HTTP 200 — read-only is a mode, not an error).
        # ⚠️ EXCEPTION: ``customer_chat`` (the CONSTRAINED customer lane) is a READ of the
        # brain, not a memory write — exactly like ``/v1/agent/chat``, a frozen (serve) seat
        # MAY chat. It must NOT be blocked by the serve-mode surface gate, so it is intercepted
        # BEFORE the gate and dispatched straight to the saas constrained pipeline.
        if tool_name == "customer_chat":
            _log(f"tools/call customer_chat user_id={resolved_user_id[:8]}...")
            try:
                account_id = _saas_account_for(_principal, resolved_user_id)
            except Exception:
                account_id = None
            if not account_id:
                # This branch is intercepted BEFORE _call_tool, so the dispatcher's stamp never
                # sees it — the flag has to be set here or the refusal ships as a success.
                return JSONResponse(_jsonrpc_result(req_id, {
                    "content": [{"type": "text",
                                 "text": json.dumps({"error": "no account context for this seat"})}],
                    "isError": True,
                }))
            # HINGED ON AGENT SEATS — the same entitlement gate /v1/agent/chat runs
            # (seat_type='agent' + the chat-agent feature/bucket). A standard seat or an
            # un-entitled account must never reach the constrained customer lane.
            _cust_block = _saas_customer_chat_block(_principal, resolved_user_id, account_id)
            if _cust_block is not None:
                _log(f"tools/call customer_chat REFUSED (entitlement) "
                     f"user_id={resolved_user_id[:8]}... reason={_cust_block.get('reason_code')}")
                return JSONResponse(_jsonrpc_result(req_id, {
                    "content": [{"type": "text", "text": json.dumps(_cust_block)}],
                    "isError": True,
                }))
            try:
                from saas.agent_mcp_tool import run_customer_chat
                out = await run_customer_chat(account_id, resolved_user_id,
                                              arguments.get("messages") or [])
            except Exception as exc:
                _log(f"tools/call customer_chat FAILED user_id={resolved_user_id[:8]}... "
                     f"err={str(exc)[:160]}")
                out = {"ok": False,
                       "reply": "I'm an informational tool, and something went wrong reaching my "
                                "information — a team member can help.",
                       "hiccup": f"{type(exc).__name__}"}
            await _saas_meter(_principal, resolved_user_id, "customer_chat")
            _cc_envelope: dict = {
                "content": [{"type": "text", "text": json.dumps(out)}],
                "structuredContent": out,
            }
            if isinstance(out, dict) and out.get("ok") is False:
                _cc_envelope["isError"] = True
            return JSONResponse(_jsonrpc_result(req_id, _cc_envelope))

        _blocked = _saas_serve_block(_principal, resolved_user_id, tool_name)
        if _blocked is not None:
            _log(f"tools/call name={tool_name!r} BLOCKED (serve mode: tool not permitted) "
                 f"user_id={resolved_user_id[:8]}...")
            return JSONResponse(_jsonrpc_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(_blocked)}]
            }))
        # HARD usage-cap gate (SAAS only, after agent-mode, fail-open). A reached memories_kept /
        # topics_trained cap returns a clean quota_exceeded tool result; dispatches NOTHING.
        _quota = _saas_quota_block(_principal, resolved_user_id, tool_name)
        if _quota is not None:
            _log(f"tools/call name={tool_name!r} QUOTA {_quota.get('metric')} "
                 f"user_id={resolved_user_id[:8]}...")
            return JSONResponse(_jsonrpc_result(req_id, _control_plane_envelope(_quota)))
        _log(f"tools/call name={tool_name!r} user_id={resolved_user_id[:8]}...")
        result = await _mcp._call_tool(tool_name, arguments)
        # Best-effort flow-meter bump (SAAS only) after a successful dispatch.
        await _saas_meter(_principal, resolved_user_id, tool_name)
        return JSONResponse(_jsonrpc_result(req_id, result))

    else:
        _log(f"Unknown method: {method!r}")
        return JSONResponse(
            _jsonrpc_error(req_id, -32601, f"Method not found: {method}"),
            status_code=404,
        )


# ── SaaS marketing site (SAAS_MODE only) — MUST be registered LAST ────────────────
# The public marketing site + "Customer access" chooser (saas/landing/*) is a
# StaticFiles(html=True) catch-all at "/". It is mounted HERE, at the very end of the
# module, so it is the LOWEST-precedence route: every API route, the /dashboard mount,
# /mcp, /health, /security.txt, /.well-known/*, and the /me · /tenant · /admin · /operator
# routers are all registered earlier and therefore win — the catch-all only serves an
# OTHERWISE-UNMATCHED path (/, /tour.html, /access.html, /landing.css, /diagrams/*, …).
# A root mount registered inside the earlier SAAS block would shadow every route defined
# after it (the tool endpoints, /mcp, /health), so it is deliberately appended last.
#
# This block is SAAS_MODE-gated exactly like the /dashboard mount: with SAAS_MODE OFF it
# imports nothing and mounts nothing, so a public build is byte-for-byte unchanged.
try:
    from saas.config import saas_mode_enabled as _saas_mode_enabled_landing
    if _saas_mode_enabled_landing():
        from fastapi.staticfiles import StaticFiles as _LandingStaticFiles

        # THE mount that produced the opencode failure: at "/" it covers every otherwise-
        # unmatched path, so StaticFiles' 405-for-non-GET became the answer for every OAuth
        # discovery probe. See _static_files_404_on_unsupported_method for the full account.
        _LandingCatchAll = _static_files_404_on_unsupported_method(_LandingStaticFiles)

        _landing_site_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "saas", "landing")
        )
        if os.path.isfile(os.path.join(_landing_site_dir, "index.html")):
            # Content-hash cache busting: HTML files come back with every local .js/.css
            # reference rewritten to ?v=<sha256[:12] of the asset's bytes> (see the helper
            # block above for the incident this fixes). The 404/405 behaviour and the file
            # resolution are the wrapped class's; only successful HTML is re-served.
            _LandingVersioned = _landing_static_files_with_versioned_html(_LandingCatchAll)
            app.mount(
                "/",
                _LandingVersioned(directory=_landing_site_dir, html=True),
                name="landing",
            )

            # -- SHELL REVALIDATION (no-cache on every text/html response) ------------
            # The HTML shell carries the ?v=<content-hash> cache-bust for every local
            # landing asset (applied at serve time by the landing mount / root route).
            # With no Cache-Control header, browsers HEURISTICALLY cache the shell (10% of
            # shell-age) and then keep loading the OLD bundle for tens of minutes
            # after a deploy -- observed live 2026-10-17 (owner saw the pre-CF page
            # on pre-prod while the origin served v=42). no-cache (not no-store):
            # the browser still uses its cache, but revalidates against the etag
            # every load -- one 304 instead of staleness. Static assets keep their
            # heuristic caching; the ?v= query is their bust.
            @app.middleware("http")
            async def _landing_shell_revalidate(request: Request, call_next):  # noqa: ANN202
                response = await call_next(request)
                if response.headers.get("content-type", "").startswith("text/html"):
                    response.headers.setdefault("Cache-Control", "no-cache")
                return response
            _log(
                "SAAS_MODE — marketing site mounted (catch-all, LAST/lowest-precedence) "
                f"from {_landing_site_dir}."
            )
        else:
            _log(
                f"SAAS_MODE — landing dir missing ({_landing_site_dir}); "
                "marketing site not mounted."
            )
except Exception as _saas_landing_exc:  # pragma: no cover - defensive
    _log(f"SaaS marketing site not mounted: {_saas_landing_exc}")
