"""MCP server implementation — raw stdio protocol (no external MCP library needed).

Handles tool discovery (`tools/list`) and tool execution (`tools/call`) following
the Model Context Protocol JSON-RPC convention over stdin/stdout.
"""

import asyncio
import json
import os
import re as _re
import sys
import time as _time
from typing import Any

import httpx

# ── Injection signal detection ────────────────────────────────────────────────
# Pre-flight check applied to `text` in remember_facts_tool() before forwarding to
# /extract/rewrite.  Only matches explicit instruction-override constructs — NOT normal
# personal data such as names, addresses, relationships, or occupations.
# Patterns require multi-word specificity to keep false-positive rate effectively zero.
# Mitigates TM-01/TM-09 (prompt injection via ingested facts).

# ── MCP recall output cleaning ────────────────────────────────────────────────
# Strip internal FaultLine metadata annotations before returning facts to MCP callers.
# Mirrors _clean_fact_for_injection() in the OpenWebUI Filter but lives here so the
# MCP path produces equally clean output without depending on filter code.

_MCP_STRIP_PATTERNS = [
    _re.compile(r'^\[(?:staged|Class [ABC]|Class-[ABC])\]\s*', _re.I),
    _re.compile(r'\bconfidence=[\d.]+\b', _re.I),
    _re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', _re.I),
]


def _clean_for_mcp(text: str) -> str:
    """Strip internal metadata annotations from a fact string before MCP return."""
    for pat in _MCP_STRIP_PATTERNS:
        text = pat.sub("", text)
    return text.strip()


# ── Identity pattern detection (ingest gating) ──────────────────────────────
# Mirrors Filter ingest gate (faultline_function.py:3445-3449): messages matching
# self-identification patterns bypass the word-count minimum.
_IDENTITY_RE = _re.compile(
    r"(?i)\b(?:my\s+name\s+is|i\s+am|call\s+me|i'm)\b"
)


def _passes_ingest_gate(text: str) -> bool:
    """Would `text` actually be ingested? (word_count >= 3 OR self-identity regex).

    Single source of truth for the ingest gate, shared by remember_facts_tool's
    STATEMENT path and recall_memory_tool's STATEMENT-diversion guard so the two
    sites cannot drift on what "ingestable" means. A recall search-term the model
    reformulated down to a bare 1-2 word keyword classifies STATEMENT but does NOT
    pass this gate — so recall_memory_tool must NOT divert it to ingest (it would be
    rejected "too short" and the recall would be eaten); it falls through to recall.
    """
    return len(text.split()) >= 3 or bool(_IDENTITY_RE.search(text))

# ── Injection signal detection ────────────────────────────────────────────────
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
    _OUTPUT_SCHEMAS,
    validate_query,
    validate_text,
    validate_user_id,
)
from src.wgm.gate import WGMValidationGate

FAULTLINE_API_URL = os.environ.get("FAULTLINE_API_URL", "http://localhost:8000").rstrip("/")
# FAULTLINE_USER_ID is the SINGLE-USER / DEV fallback ONLY. It is consulted only when
# no caller-supplied identity is present (see bind_tenant / resolve_effective_user_id).
# It MUST be unset in any multi-user deploy, else it would mask real per-user identity.
FAULTLINE_USER_ID = os.environ.get("FAULTLINE_USER_ID", "").strip()

# Strict UUID gate (lowercased), mirroring schema_manager._UUID_RE. A claimed tenant id
# must match this before it is trusted as an identity / interpolated into a schema name.
_TENANT_UUID_RE = _re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)


class TenantSpoofError(Exception):
    """Raised when a request claims a tenant it is not authorized to act as.

    Carries a 4xx-mappable status so both transports (JSON-RPC /mcp and the OpenAPI
    REST shorthand) can translate it to the right HTTP response.
    """

    def __init__(self, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def bind_tenant(principal: str | None, claimed_user_id: str) -> str:
    """Resolve the tenant a request is permitted to act as. SINGLE identity seam.

    Consulted by BOTH MCP transports (the JSON-RPC /mcp dispatcher and the OpenAPI
    REST shorthand) so identity is resolved ONCE, transport-agnostically (brain not
    transport). The result is the authoritative ``user_id`` that downstream binds via
    ``SET search_path TO faultline_<slug>`` (NO public).

    Precedence: caller-supplied identity WINS; ``FAULTLINE_USER_ID`` is consulted ONLY
    as a single-user/dev fallback when the caller supplies nothing.

    Spoof-guard (DEV/SECURITY-multiuser-tenant-isolation.md RP-3):

    * Option A (FUTURE — per-user tokens): when ``principal`` itself carries a bound
      user_id (i.e. ``_resolve_principal`` returns a UUID instead of "shared"/"anonymous"),
      a non-empty ``claimed_user_id`` that disagrees is a spoof → raise TenantSpoofError
      (403). This branch is present and dormant; it activates with NO call-site change the
      moment ``_resolve_principal`` is swapped for a token→user_id lookup.
    * Option B (TODAY — shared key): the shared bearer is transport auth, not identity.
      Under the documented trust assumption that the OpenWebUI↔MCP hop is the sole client
      on a trusted segment and OpenWebUI stamps the correct logged-in user's UUID into
      ``X-OpenWebUI-User-Id``, the claimed id IS the identity. We still validate it is a
      well-formed UUID and fail loud on a malformed value rather than route it blindly.

    Fail-loud: a malformed (non-UUID) claimed id raises TenantSpoofError(400). An empty
    claim with no fallback raises TenantSpoofError(400) — never a silent shared-pool route.
    """
    claimed = (claimed_user_id or "").strip().lower()

    # ── Option A: principal carries its own bound identity (per-user tokens). ──
    # Dormant today (_resolve_principal returns "shared"/"anonymous", not a UUID).
    principal_is_identity = bool(principal) and bool(_TENANT_UUID_RE.match(principal.strip().lower()))
    if principal_is_identity:
        principal_uid = principal.strip().lower()
        if claimed and claimed != principal_uid:
            raise TenantSpoofError(
                "tenant spoof attempt: claimed user_id does not match authenticated principal",
                status_code=403,
            )
        return principal_uid

    # ── Option B: shared key (or anonymous dev). Caller wins; pin is fallback. ──
    # TRUST ASSUMPTION: the shared bearer proves a known client; we trust the
    # OpenWebUI→MCP hop (sole client on a trusted segment) to stamp the correct
    # X-OpenWebUI-User-Id. The claimed id is the identity under that boundary only.
    effective = claimed or FAULTLINE_USER_ID.strip().lower()
    if not effective:
        raise TenantSpoofError(
            "user_id required: no caller identity and no FAULTLINE_USER_ID fallback",
            status_code=400,
        )
    if not _TENANT_UUID_RE.match(effective):
        # Fail loud — never interpolate a malformed id into a schema name.
        raise TenantSpoofError(
            f"malformed user_id (not a well-formed UUID): {effective!r}",
            status_code=400,
        )
    return effective

# NOTE (ingest-spine Part 1): this flag NO LONGER gates remember_facts_tool — the Class-C
# store_context residue fallback was REMOVED (held-blob: DROP, no un-walkable islands). The flag
# is retained for the standalone store_context_tool / backend /store_context config only; the
# remember path drops residue that cannot build a valid triple.
SHORT_TERM_MEMORY = os.environ.get("SHORT_TERM_MEMORY", "true").strip().lower() not in ("false", "0", "no")

# Backend INGEST_ENABLED freeze switch ("knowledge-store mode"). Deliberately NO env
# read here — the BACKEND is authoritative (avoids split-brain config between
# containers). The gated backend endpoints return 200-shaped {"status": "ingest_disabled"}
# responses; tools detect that status and surface this message instead of pretending
# success (success-shaped zeros). GET /internal/ingest-route also reports
# `ingest_enabled` for the fire-and-forget /learn path that cannot inspect its response.
_INGEST_DISABLED_STATUS = "ingest_disabled"
_INGEST_DISABLED_MESSAGE = (
    "Memory ingest is currently disabled (knowledge-store mode). No facts were stored."
)


def _is_ingest_disabled(payload: Any) -> bool:
    """True when a backend response dict carries the freeze-switch status."""
    return isinstance(payload, dict) and payload.get("status") == _INGEST_DISABLED_STATUS

# When true (default), recall_memory_tool consults the SAME DB-weighted intent brain that
# remember_facts_tool uses and ROUTES by the resulting intent (brain-not-transport): a
# CORRECTION/RETRACTION the model mis-picked as recall defers to retract_fact_tool, a STATEMENT
# defers to the ingest path, and QUERY (or any classify error / low confidence) falls through to
# the normal /query recall. FAIL-SAFE: any classify failure → plain recall (recall never breaks).
RECALL_INTENT_ROUTING = os.environ.get("RECALL_INTENT_ROUTING", "true").strip().lower() not in ("false", "0", "no")

# When true (default), remember_facts_tool harvests fact-bearing spans on EVERY route, not just
# STATEMENT. A turn the user sent to remember is MEANT to store facts; if its dominant intent
# classifies QUERY ("can you help me plan X? by the way, I fixed the fence three weeks ago") or
# CORRECTION/RETRACTION, the buried fact would otherwise be dropped before extraction ever ran.
# So before bailing on a non-STATEMENT route, fire the SAME cheap intent-independent harvest the
# recall path uses (_harvest_turn_facts → /harvest-spans: deterministic segmenter + reframe +
# verb-lift + GLiNER2, NO LLM triple extraction). The STATEMENT branch is UNCHANGED — it still
# goes through /extract/rewrite and must NOT double-harvest (the harvest only runs on the branches
# that would otherwise return without ingesting). FAIL-SAFE: any harvest failure is swallowed by
# _harvest_turn_facts and the original route's response is returned unchanged (today's behavior).
INGEST_INTENT_INDEPENDENT_HARVEST = os.environ.get(
    "INGEST_INTENT_INDEPENDENT_HARVEST", "true"
).strip().lower() not in ("false", "0", "no")

# Candidate URLs probed in order when the configured URL is unreachable.
# Docker container IPs shift on rebuild; the bridge gateway (172.16.0.1) is
# stable and reachable from sibling containers on the same Docker network.
_FAULTLINE_URL_CANDIDATES: list[str] = [
    FAULTLINE_API_URL,
    "http://faultline:8000",
    "http://172.16.0.1:8000",
    "http://host.docker.internal:8000",
    "http://localhost:8000",
]
_FAULTLINE_URL_DETECTED: bool = False


async def _detect_faultline_url() -> None:
    """Probe candidate URLs and update FAULTLINE_API_URL to the first that answers.

    Called once before the first tool operation. Result is cached for the process
    lifetime — no per-call overhead after the initial probe.
    """
    global FAULTLINE_API_URL, _FAULTLINE_URL_DETECTED
    if _FAULTLINE_URL_DETECTED:
        return
    _FAULTLINE_URL_DETECTED = True  # mark early to prevent concurrent probes

    seen: set[str] = set()
    for candidate in _FAULTLINE_URL_CANDIDATES:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            async with httpx.AsyncClient(timeout=3.0) as probe:
                r = await probe.get(f"{candidate}/health")
                if r.status_code == 200:
                    FAULTLINE_API_URL = candidate
                    return
        except Exception:
            continue
    # No candidate answered — keep the env var value and let callers surface errors

# ── UX Humanization — rotating progress messages ──────────────────────────────

def _rotate(pool: list, index: int) -> str:
    """Deterministic rotation through a pool by index. Never random."""
    return pool[index % len(pool)] if pool else ""


_MCP_PROGRESS_STEP1 = [
    "Checking your memory profile...",
    "Looking up your profile...",
    "Accessing memory...",
    "Checking in with memory...",
]

_MCP_PROGRESS_STEP2 = [
    "Profile ready — running now...",
    "Memory loaded — working on it...",
    "Got your facts — processing...",
    "Memory ready — one moment...",
]

_MCP_PROGRESS_DONE = [
    "Done.",
    "Complete.",
    "All set.",
    "Finished.",
]

# ─────────────────────────────────────────────────────────────────────────────

# Module-level HTTP client — initialised in run_mcp_server(), used by all tool handlers.
_http_client: httpx.AsyncClient | None = None

# Tracks whether notifications/initialized has been received.
_initialized: bool = False

# Tracks user IDs that have already been provisioned this session.
_provisioned_users: set[str] = set()


# ── HTTP helpers ─────────────────────────────────────────────────────────────


async def _post(url: str, **kwargs) -> httpx.Response:
    """POST with stale-client fallback.

    The lifespan AsyncClient can silently go stale after a container restart
    or network hiccup. Retry once with a fresh client on any ConnectError so
    every tool call is resilient without duplicating the fallback pattern.
    """
    try:
        return await _http_client.post(url, **kwargs)
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        async with httpx.AsyncClient(timeout=30.0) as fresh:
            return await fresh.post(url, **kwargs)


async def _get(url: str, **kwargs) -> httpx.Response:
    """GET with stale-client fallback (same rationale as _post)."""
    try:
        return await _http_client.get(url, **kwargs)
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        async with httpx.AsyncClient(timeout=30.0) as fresh:
            return await fresh.get(url, **kwargs)


# ── Provisioning helper ──────────────────────────────────────────────────────


async def _ensure_provisioned(user_id: str) -> bool:
    """Trigger provisioning if needed, then poll until ready (up to ~18 s total).

    The backend provisioning worker wakes every 5 s.  On first encounter the initial
    GET enqueues the job and returns "not_found"; we then sleep 6 s so the worker has
    time to notice the new job before burning poll slots on guaranteed misses.

    Returns True if the user schema is confirmed ready, False otherwise.
    Callers must not proceed with tool execution when False is returned.
    """
    await _detect_faultline_url()  # probe once, cache working URL for process lifetime
    if user_id in _provisioned_users:
        return True
    client = _http_client
    own_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
        own_client = True
    try:
        # ── Initial GET: check status AND trigger enqueue if not_found ───────────
        just_enqueued = False
        try:
            resp = await client.get(
                f"{FAULTLINE_API_URL}/provisioning/status",
                params={"user_id": user_id},
                timeout=5.0,
            )
            init_status = resp.json().get("status")
            if init_status == "ready":
                _provisioned_users.add(user_id)
                return True
            if init_status == "not_found":
                # Backend just enqueued the provisioning job.  The worker sleeps
                # PROVISIONING_POLL_INTERVAL (default 5 s) between checks, so polls
                # at t=2 s and t=4 s are guaranteed misses.  Sleep 6 s first to let
                # the worker wake and start schema creation before we start polling.
                just_enqueued = True
                _log(f"Provisioning enqueued for {user_id[:8]} — waiting 6 s for worker")
            else:
                _log(f"Provisioning status for {user_id[:8]}: {init_status}")
        except Exception as e:
            _log(f"Initial provisioning GET failed for {user_id[:8]}: {e}")

        if just_enqueued:
            await asyncio.sleep(6.0)

        # ── Poll loop: up to 27 × 2 s = 54 s additional wait ────────────────────
        # Provisioning now takes ~22 s (was <18 s), so the old 12 s budget timed out and the
        # MCP fired the backend call anyway against a not-yet-ready schema. Wait it out instead
        # (~60 s total with the 6 s pre-sleep) so the transport never dispatches a doomed call.
        # COURTESY ONLY: the BACKEND _ensure_tenant_ready guard is the real fix — even if this
        # poll were to time out, the backend blocks-or-503s rather than running blind.
        for attempt in range(27):
            try:
                resp = await client.get(
                    f"{FAULTLINE_API_URL}/provisioning/status",
                    params={"user_id": user_id},
                    timeout=5.0,
                )
                if resp.json().get("status") == "ready":
                    _provisioned_users.add(user_id)
                    _log(f"Provisioning ready for {user_id[:8]} (attempt {attempt + 1})")
                    return True
            except Exception as e:
                _log(f"Provisioning poll {attempt + 1} failed for {user_id[:8]}: {e}")
            await asyncio.sleep(2.0)

        _log(f"Provisioning not ready after timeout for {user_id[:8]} — not proceeding")
        return False
    except Exception as e:
        _log(f"Provisioning check failed for {user_id[:8]}: {e}")
        return False
    finally:
        if own_client:
            await client.aclose()


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


async def _harvest_turn_facts(text: str, user_id: str) -> int:
    """Intent-INDEPENDENT fact harvest — POST the RAW turn to /harvest-spans (the cheap
    deterministic segmenter + GLiNER2, NO LLM) and ingest any edges. Runs on a recall turn so
    a fact buried in a question ("...help me plan it? by the way, I fixed the fence three weeks
    ago") is captured even though the turn routes QUERY. Best-effort: never raises, returns the
    edge count. The segmenter only fires on turns that actually carry a fact-bearing span."""
    try:
        resp = await _http_client.post(
            f"{FAULTLINE_API_URL}/harvest-spans",
            json={"text": text, "user_id": user_id},
        )
        resp.raise_for_status()
        harvest_data = resp.json()
        # Backend freeze switch: nothing to harvest OR ingest — genuinely read-only.
        if _is_ingest_disabled(harvest_data):
            _log("harvest_turn_facts: backend ingest disabled (knowledge-store mode) — skipped")
            return 0
        edges = harvest_data.get("edges", []) or []
        if not edges:
            return 0
        ingest_result = await ingest_tool(text, user_id, edges, source="mcp")
        # Backend freeze switch: /ingest stored nothing — do NOT report the edges as captured.
        if _is_ingest_disabled(ingest_result):
            _log("harvest_turn_facts: backend ingest disabled (knowledge-store mode) — nothing stored")
            return 0
        _log(f"harvest_turn_facts: ingested {len(edges)} buried-fact edge(s)")
        return len(edges)
    except Exception as exc:
        _log(f"harvest_turn_facts_skip: {exc!r}")
        return 0


async def _ground_self_predication_facts(text: str, user_id: str) -> int:
    """Self-predication grounding (INGEST routes ONLY — never recall, so recall pays no LLM
    latency): POST the turn to /ground-self-predication (the LLM grounds a bare-copula "I am X"
    on the entity-match layer → routes to feels / also_known_as / occupation) and ingest any
    edge. This is the principled replacement for the greedy name regex — bare-copula feelings
    AND names are captured by GROUNDING, not pattern-guessing. Best-effort: never raises;
    returns the edge count. The backend gate fires only on an actual 'I am X' construction."""
    try:
        resp = await _http_client.post(
            f"{FAULTLINE_API_URL}/ground-self-predication",
            json={"text": text, "user_id": user_id},
        )
        resp.raise_for_status()
        ground_data = resp.json()
        # Backend freeze switch: grounding gated backend-side — nothing to ingest.
        if _is_ingest_disabled(ground_data):
            _log("ground_self_predication: backend ingest disabled (knowledge-store mode) — skipped")
            return 0
        edges = ground_data.get("edges", []) or []
        if not edges:
            return 0
        ingest_result = await ingest_tool(text, user_id, edges, source="mcp")
        # Backend freeze switch: /ingest stored nothing — do NOT report the edges as captured.
        if _is_ingest_disabled(ingest_result):
            _log("ground_self_predication: backend ingest disabled (knowledge-store mode) — nothing stored")
            return 0
        _log(f"ground_self_predication: ingested {len(edges)} self-fact edge(s)")
        return len(edges)
    except Exception as exc:
        _log(f"ground_self_predication_skip: {exc!r}")
        return 0


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
    resp = await _post(f"{FAULTLINE_API_URL}/retract", json=body)
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


async def _learn_via_llm(
    topic: str,
    user_id: str,
    source_url: str | None = None,
    online: bool = False,
) -> dict[str, Any]:
    """Fire-and-forget /learn — return immediately, backend processes async.

    The LLM ontology generation takes 30-60 seconds. Blocking the MCP tool
    call for that long makes OpenWebUI appear frozen. Instead: start the
    backend call as a background task and return an acknowledgment immediately.
    The facts will be available by the time the user asks about the topic.

    When source_url is provided, fetches the page content and passes it as
    source_text to the backend so the LLM grounds ontology in real content.
    On any fetch failure, falls back to topic-only (LLM training knowledge).

    Backend freeze switch: because this path is fire-and-forget (the ack returns
    before /learn responds), the disabled status on the /learn response can never
    reach the caller. So consult the BACKEND-AUTHORITATIVE freeze state via
    GET /internal/ingest-route (no env read here — brain, not transport) BEFORE
    scheduling: frozen → return the disabled message instead of a fake "building
    concept map" ack. Fail-safe: brain unreachable → proceed as today.
    """
    try:
        _route_resp = await _http_client.get(
            f"{FAULTLINE_API_URL}/internal/ingest-route", timeout=5.0
        )
        _route_resp.raise_for_status()
        if _route_resp.json().get("ingest_enabled") is False:
            _log("learn_via_llm: backend ingest disabled (knowledge-store mode) — /learn not dispatched")
            return {"status": _INGEST_DISABLED_STATUS, "memory": _INGEST_DISABLED_MESSAGE}
    except Exception as exc:
        _log(f"learn_via_llm ingest-state probe failed (proceeding): {exc!r}")

    async def _background_learn() -> None:
        import re as _re2
        source_text: str | None = None

        if source_url:
            try:
                async with httpx.AsyncClient(timeout=15.0) as fetcher:
                    fetch_resp = await fetcher.get(
                        source_url,
                        follow_redirects=True,
                        headers={"User-Agent": "FaultLine/1.0"},
                    )
                    fetch_resp.raise_for_status()
                    raw = fetch_resp.text
                    # Strip HTML tags, collapse whitespace
                    text = _re2.sub(r'<[^>]+>', ' ', raw)
                    text = _re2.sub(r'\s+', ' ', text).strip()
                    source_text = text[:8000]
                    _log(f"learn_online.fetched url={source_url} chars={len(source_text)}")
            except Exception as e:
                _log(f"learn_online.fetch_failed url={source_url} error={e} — falling back to LLM-only")

        body: dict[str, Any] = {"topic": topic, "user_id": user_id}
        if source_text:
            body["source_text"] = source_text
        if source_url:
            body["source_url"] = source_url

        try:
            client = _http_client
            if client is None:
                client = httpx.AsyncClient(timeout=120.0)
                resp = await client.post(f"{FAULTLINE_API_URL}/learn", json=body)
                await client.aclose()
            else:
                try:
                    resp = await client.post(
                        f"{FAULTLINE_API_URL}/learn",
                        json=body,
                        timeout=120.0,
                    )
                except Exception:
                    async with httpx.AsyncClient(timeout=120.0) as fresh:
                        resp = await fresh.post(f"{FAULTLINE_API_URL}/learn", json=body)
            _log(f"expand_complete topic={topic!r} status={resp.status_code} body={resp.text[:120]}")
        except Exception as e:
            _log(f"expand_background_failed topic={topic!r} error={e}")

    asyncio.create_task(_background_learn())

    if online and source_url:
        ack = (
            f"Building concept map for '{topic}' from {source_url} — maps how concepts relate, "
            f"runs in the background (~30s). Ask me about '{topic}' in a moment."
        )
    elif online:
        ack = (
            f"Building concept map for '{topic}' — for richer results, add a source: "
            f"/expand {topic} online https://your-source.com"
        )
    else:
        ack = (
            f"Building concept map for '{topic}' — maps how concepts relate to each other, "
            f"runs in the background (~30s). Ask me about '{topic}' in a moment."
        )

    return {"memory": ack}


async def _maybe_intercept_slash(raw: str, user_id: str) -> dict[str, Any] | None:
    """Intercept /expand slash-commands before normal tool processing.

    Shared by recall_memory_tool and learn_facts_tool so the /expand command
    works on both entry points. Returns the _learn_via_llm(...) result dict when
    the input is an /expand command, else None (caller proceeds normally).

    Defined ABOVE both call sites per the nested-helpers-precede-call-sites rule
    (CLAUDE.md). Regex/semantics are unchanged from the original inline block.
    """
    _expand_full_re = _re.compile(
        r'^/expand\s+(?P<topic>.+?)(?:\s+online(?:\s+(?P<url>https?://\S+))?)?\s*$',
        _re.I,
    )
    m = _expand_full_re.match(raw.strip())
    if m:
        topic = m.group("topic").strip()
        url = m.group("url")  # may be None
        online = "online" in raw.lower()
        return await _learn_via_llm(topic, user_id, source_url=url, online=online)
    return None


# ── BRAIN-SLOW BREAKER (brain-independence lane, 2026-10-25; FOSS port 2026-10-26) ────
#
# THE DEFECT (live on private prod 2026-10-24): the API-layer passage-first fix works (a
# direct POST /query serves document_hits in <10s with the NIM brain rate-limited), but
# recall_memory through THIS server still hung 15s+ because its PRE-QUERY brain call
# (/classify-intent) blocked the pipeline behind the LLM tiers inside classify (the
# corroboration and sub-gate escalation budgets server-side, each with retries). The
# per-call timeout bounds ONE call; this breaker carries the "brain is slow" verdict
# ACROSS turns so the next recall does not re-pay the probe:
#
#   classify probe budget exceeded (or classify transport-unreachable)
#     → _mark_brain_slow(user_id): that SEAT's window opens for MCP_BRAIN_SLOW_COOLDOWN_S
#       (default 60s). PER-SEAT on purpose (round-2 review finding 3): one tenant's
#       rate-limited brain must not fast-path another tenant's healthy-brain turns.
#     → every recall_memory for THAT seat in the window takes the FAST PATH: intent routing
#       and the confidence gate are SKIPPED entirely and /query fires directly with the raw
#       text (the API's passage-first floor + QUERY_WALK_TIMEOUT_S bound the rest)
#     → a HEALTHY full-route /classify-intent answer for that seat CLOSES the window.
#       RECOVERY PATHS (round-2 review finding 4, stated honestly): the fast-path arm
#       skips classify entirely, so a RECALL-ONLY seat closes its window by cooldown
#       expiry, or by any healthy WRITE-lane classify (remember/retract classify every
#       turn), whichever comes first — a recall-only seat can pay up to one cooldown
#       window of route-fidelity loss per trip (its turns are still served, and retained
#       verbatim; only the intent diverts are suspended).
#
# MCP_BRAIN_DEGRADED=1 is the ops-owned explicit arm of the same path (declare it, don't
# measure it — e.g. while the brain box is being worked on); it is GLOBAL because the
# operator is describing the BOX. This is a LATENCY breaker, not a correctness one: the
# fast path substitutes no route (QUERY-for-recall is the documented read-only fail-safe —
# it writes nothing, deletes nothing, and /query parses the text itself), so a
# false-positive open window costs route fidelity on one turn, never data.
_BRAIN_SLOW: dict[str, dict[str, Any]] = {}


def _env_float(name: str, default: float) -> float:
    """Env knob reader (positive float or the default; junk falls back silently)."""
    try:
        _v = float((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return _v if _v > 0.0 else default


def _classify_probe_budget_s() -> float:
    """The RECALL classify probe's budget (MCP_CLASSIFY_TIMEOUT_S, default 3s).

    PRECEDENCE (round-2 review finding 9): this knob REPLACES the 10s table default for
    the RECALL PROBE only — write lanes keep the 10s legacy budget because their escalated
    verdicts are load-bearing route decisions (a 3s ceiling there would time out a HEALTHY
    brain's escalation and route corrections through the STATEMENT fail-open). It must sit
    UNDER the LLM-tier budgets inside /classify-intent that it exists to escape."""
    return _env_float("MCP_CLASSIFY_TIMEOUT_S", 3.0)


def _brain_slow_cooldown_s() -> float:
    return _env_float("MCP_BRAIN_SLOW_COOLDOWN_S", 60.0)


def _mark_brain_slow(user_id: str, reason: str) -> None:
    _now = _time.monotonic()
    # prune expired seats so the map stays bounded by LIVE windows, not tenant history
    for _k in [k for k, v in _BRAIN_SLOW.items() if float(v.get("until") or 0.0) <= _now]:
        _BRAIN_SLOW.pop(_k, None)
    _BRAIN_SLOW[user_id or "anonymous"] = {
        "until": _now + _brain_slow_cooldown_s(), "reason": str(reason)[:200]}
    _log(f"brain_slow.marked seat={(user_id or 'anonymous')[:8]} "
         f"cooldown_s={_brain_slow_cooldown_s():.0f} reason={str(reason)[:200]}"
         " — that seat's recall turns take the fast path (/query direct)")


def _brain_slow_active(user_id: str | None = None) -> bool:
    if os.environ.get("MCP_BRAIN_DEGRADED", "").strip().lower() in ("1", "true", "yes"):
        return True
    _st = _BRAIN_SLOW.get(user_id or "anonymous")
    return bool(_st) and _time.monotonic() < float(_st.get("until") or 0.0)


def _brain_slow_reason(user_id: str | None = None) -> str:
    if os.environ.get("MCP_BRAIN_DEGRADED", "").strip().lower() in ("1", "true", "yes"):
        return "MCP_BRAIN_DEGRADED=1 (ops-declared, global)"
    _st = _BRAIN_SLOW.get(user_id or "anonymous") or {}
    return str(_st.get("reason") or "")


def _brain_slow_reset(why: str = "") -> None:
    if _BRAIN_SLOW:
        _log(f"brain_slow.cleared {why}")
    _BRAIN_SLOW.clear()


def _brain_transport_slow(exc: BaseException) -> bool:
    """Round-2 review finding 5 (FOSS shape): only TRANSPORT exhaustion — timeout or
    unreachable — marks the seat slow. A deterministic backend DECISION (4xx) or a
    malformed body is the backend DECIDING, not crawling: the full pipeline is correct
    there and the trip turn must not fast-path retention on it."""
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


async def _classify_and_gate(text: str, user_id: str, *,
                             gliner2_fallback: bool = False,
                             probe_budget: float | None = None,
                             ) -> tuple[str, float, float]:
    """Consult the DB-weighted intent BRAIN: /classify-intent + per-user confidence gate.

    Single source of truth for the route decision, shared by remember_facts_tool and
    recall_memory_tool (transport-parity: the brain lives ONCE backend-side; both transports
    consume it). /classify-intent already applies the per-user confidence gate AND the
    low-confidence LLM escalation, so callers DEFER to the returned intent — they must not
    re-derive a weaker "confidence < gate → STATEMENT" route that would clobber an escalated
    CORRECTION. The `gate` is returned only for the diagnostic log line; it does not drive routing.

    BOUNDED PROBE (brain-independence lane, 2026-10-25): ``probe_budget`` bounds this call
    when given — the RECALL probe passes ``MCP_CLASSIFY_TIMEOUT_S`` (default 3s), because
    the LLM tiers INSIDE /classify-intent are the 15s+ prod hang and recall must never wait
    for them. Write lanes leave it None (the legacy 10s — their escalated verdicts are
    load-bearing route decisions). When the budget is exceeded (or the endpoint is
    transport-unreachable) the brain is marked slow for this SEAT (that seat's recall turns
    fast-path for the cooldown window), and — when this lane still needs a route on a
    degraded box (``gliner2_fallback=True``, currently remember_facts) — ONE re-ask with
    ``skip_llm: true`` fetches the DETERMINISTIC answer (spaCy preroutes + GLiNER2 + DB
    pattern layers, no LLM tier). That is still the backend's route, not a transport guess.
    ``gliner2_fallback=False`` (the default — byte-identical to the classic contract;
    recall) skips the re-ask: recall's fail-safe IS the fast path — fire /query with the
    raw text and let the passage-first floor answer.

    A classify failure still RAISES (after the re-ask, when enabled) — callers keep their
    own fail-safe (recall falls back to the plain read-only walk; remember defaults to
    STATEMENT). The confidence-gate read stays non-fatal and is SKIPPED on the GLiNER2-only
    arm (a slow brain must not be probed twice for a value nothing routes on).
    """
    intent = "STATEMENT"
    confidence = 0.0
    _classify_budget = probe_budget if probe_budget is not None else 10.0
    _from_fallback = False
    try:
        classify_resp = await _http_client.post(
            f"{FAULTLINE_API_URL}/classify-intent",
            params={"user_id": user_id},
            json={"text": text},
            timeout=_classify_budget,
        )
        classify_resp.raise_for_status()
        classify_data = classify_resp.json()
        intent = classify_data.get("intent", "STATEMENT")
        confidence = float(classify_data.get("confidence", 0.0))
    except Exception as exc:
        # Transport exhaustion (timeout / unreachable): the LLM tiers inside
        # /classify-intent are the hang — NEVER wait for them. Mark THIS seat's window
        # (its recall turns fast-path until a healthy full-route answer closes it), then
        # degrade to the deterministic answer when this lane needs a route at all.
        # A deterministic backend decision (4xx) or malformed body does NOT mark: the
        # backend is DECIDING, not crawling (round-2 review finding 5).
        if _brain_transport_slow(exc):
            _mark_brain_slow(user_id, f"{type(exc).__name__}: {str(exc)[:120]}")
        if not gliner2_fallback:
            _log(f"intent_classify_unavailable: {exc!r} — NOT defaulting; caller surfaces the failure")
            raise
        _log(f"intent_classify_unavailable: {exc!r} — re-asking GLiNER2-only (skip_llm)")
        try:
            classify_resp = await _http_client.post(
                f"{FAULTLINE_API_URL}/classify-intent",
                params={"user_id": user_id},
                json={"text": text, "skip_llm": True},
                timeout=_classify_budget,
            )
            classify_resp.raise_for_status()
            classify_data = classify_resp.json()
            intent = classify_data.get("intent", "STATEMENT")
            confidence = float(classify_data.get("confidence", 0.0))
            _from_fallback = True
        except Exception as exc2:  # noqa: BLE001 — any failure here: surface the ORIGINAL below
            _log(f"intent_classify_gliner2_only_unavailable: {exc2!r} — NOT defaulting; "
                 "caller surfaces the original failure")
            raise exc

    if not _from_fallback:
        # A healthy FULL-route answer closes THIS seat's brain-slow window (half-open
        # recovery: the next turn pays the full pipeline again, immediately). Other seats'
        # windows are untouched — their evidence is their own.
        _BRAIN_SLOW.pop(user_id or "anonymous", None)

    gate = 0.70
    if _from_fallback:
        _log("confidence_gate_skipped (GLiNER2-only arm — diagnostic read omitted on a slow brain)")
    else:
        try:
            gate_resp = await _http_client.get(
                f"{FAULTLINE_API_URL}/confidence-gate/{user_id}",
                timeout=5.0,
            )
            gate_resp.raise_for_status()
            gate = float(gate_resp.json().get("threshold", 0.70))
        except Exception as exc:
            _log(f"confidence_gate_fallback: {exc!r} — defaulting to 0.70")

    _log(f"intent_classified: intent={intent} confidence={confidence:.3f} gate={gate:.3f}"
         f"{' (gliner2_only)' if _from_fallback else ''}")
    return intent, confidence, gate


async def _statement_extractor_route(user_id: str) -> str:
    """Consult the BRAIN for the STATEMENT-ingest extractor (D1, transport-parity).

    The decision lives ONCE backend-side (gated by ``SENTENCE_PIPELINE``); the MCP is a pure
    consumer and never reads the flag itself. Returns "spine" (route STATEMENT through the
    deterministic strength-passing spine: /harvest-spans → /ingest) or "rewrite" (today's
    /extract/rewrite → /ingest path).

    FAIL-SAFE: any error / unreachable brain → "rewrite" (today's behavior). The spine route NEVER
    engages on a brain decision we could not confirm — so flag-OFF / brain-down is byte-identical
    to current prod.
    """
    try:
        resp = await _http_client.get(
            f"{FAULTLINE_API_URL}/internal/ingest-route",
            timeout=5.0,
        )
        resp.raise_for_status()
        route = (resp.json().get("statement_extractor") or "rewrite").strip().lower()
        return route if route in ("spine", "rewrite") else "rewrite"
    except Exception as exc:
        _log(f"statement_extractor_route_fallback: {exc!r} — defaulting to rewrite")
        return "rewrite"


async def _ingest_statement_via_spine(
    text: str, user_id: str, source_ref: str | None = None
) -> dict[str, Any] | None:
    """STATEMENT ingest via the DETERMINISTIC SPINE (D1). Calls /harvest-spans (the spine: LLM
    atomize-only → spaCy deriver → GLiNER2 typing → ±6 backbone attach, NO LLM triple extraction)
    and ingests the returned edges ONCE via /ingest.

    source_ref (document lane only): citable provenance (URL/filename/title) threaded into the
    /ingest body so document-derived facts carry their citation (migration 128). None (the
    conversational default) omits the key entirely — byte-identical request to today's.

    Returns the /ingest response on success (>=1 edge), or None to signal the caller to FALL BACK
    to the legacy /extract/rewrite path (fail-safe: spine produced no edge / any error → None, so a
    clearly-declarative statement is NEVER silently dropped). The spine's own residue→Class-C floor
    (store_context, inside /harvest-spans) is independent and is NOT a duplicate of these edges.

    NO DOUBLE-INGEST: this is the SOLE ingest of the statement text when it returns non-None — the
    caller skips /extract/rewrite, the no-edges harvest fallback, and self-predication grounding
    (the spine's derive_sentence_facts already covers "I am X" / "my favorite X")."""
    try:
        resp = await _http_client.post(
            f"{FAULTLINE_API_URL}/harvest-spans",
            json={"text": text, "user_id": user_id},
            timeout=60.0,
        )
        resp.raise_for_status()
        harvest_data = resp.json()
        # Backend freeze switch: do NOT fall through to /extract/rewrite (it is frozen
        # too) — short-circuit with the disabled status so the caller surfaces the message.
        if _is_ingest_disabled(harvest_data):
            _log("statement_via_spine: backend ingest disabled (knowledge-store mode)")
            return {"status": _INGEST_DISABLED_STATUS, "message": _INGEST_DISABLED_MESSAGE}
        edges = harvest_data.get("edges", []) or []
    except Exception as exc:
        _log(f"statement_spine_harvest_failed: {exc!r} — falling back to /extract/rewrite")
        return None
    if not edges:
        # No durable edge from the spine (residue, if any, was already held in Class C inside
        # /harvest-spans). Signal fall-through so the legacy extractor gets a shot — fail-safe, not
        # a silent drop. No double-ingest: the spine ingested NOTHING here (it only returns edges).
        return None
    _ingest_body: dict[str, Any] = {
        "text": text, "user_id": user_id, "edges": edges, "source": "mcp",
    }
    if source_ref:
        _ingest_body["source_ref"] = source_ref
    try:
        ingest_resp = await _http_client.post(
            f"{FAULTLINE_API_URL}/ingest",
            json=_ingest_body,
            timeout=30.0,
        )
        ingest_resp.raise_for_status()
    except Exception as exc:
        # The spine produced edges but /ingest failed. Do NOT fall back to /extract/rewrite here:
        # re-extracting the same text risks a partial double-write if /ingest partially applied.
        # Fail loud with a non-drop signal — the orchestrator/operator sees the error.
        _log(f"statement_spine_ingest_failed: {exc!r}")
        return {"status": "error", "reason": "spine ingest failed", "committed": 0}
    _spine_data = ingest_resp.json()
    # Backend freeze switch: /ingest stored nothing — surface it, never success-shaped zeros.
    if _is_ingest_disabled(_spine_data):
        _log("statement_via_spine: backend ingest disabled (knowledge-store mode)")
        return {"status": _INGEST_DISABLED_STATUS, "message": _INGEST_DISABLED_MESSAGE}
    _log(f"statement_via_spine: ingested {len(edges)} edge(s)")
    return _spine_data



#: Passage display ceiling (R6): the chunker caps chunks at _DOC_CHUNK_MAX_CHARS (1200),
#: so a real passage is quoted WHOLE — this ceiling only trims pathological hand-seeded
#: rows, never an honest chunk. Deterministic constant, not a smart trim.
_DOC_PASSAGE_DISPLAY_CHARS = 1500

#: Passage cleanup subset of _MCP_STRIP_PATTERNS: annotation debris ONLY. The UUID-token
#: pattern that lives in this repo's legacy _MCP_STRIP_PATTERNS list is DELIBERATELY
#: excluded here — a UUID quoted in the user's own document is CONTENT, not a store
#: entity id to redact (the passage has no store-membership context; it must stay
#: verbatim, per the membership-gated-strip ruling the upstream renderer codified).
_DOC_PASSAGE_STRIP_PATTERNS = [
    _re.compile(r'^\[(?:staged|Class [ABC]|Class-[ABC])\]\s*', _re.I),
    _re.compile(r'\bconfidence=[\d.]+\b', _re.I),
]


def _render_document_hit_lines(hits: list) -> list[str]:
    """Deterministic assert lines for the document passage lane (migration 299 → R6).

    A PASSAGE hit renders as "From your document <title>: <the actual chunk text that
    matches> (document id N, section M)" — the passage IS the answer, quoted verbatim
    (whitespace-collapsed for the single-line assert band), never a stitched headline.
    A title-only discovery row (chunk_text None — the title/source matched but no single
    chunk did) renders the W2 shape: "You have a document: <title> (document id N)".
    Pure string shaping over rows the backend SELECT already returned — NO LLM, no
    re-ranking, no smartening. The import status is stated honestly when the document is
    not 'ready': a stub hit is VERBATIM TEXT ONLY, never committed facts (the stub-lane
    honesty rule, migration 299). Never raises — a malformed hit is skipped."""
    lines: list[str] = []
    # Per-status honesty (adversarial-review fix): 'error' is NOT "still extracting" —
    # the facts are not coming; say what is actually true for each terminal state.
    _STATUS_SUFFIX = {
        "pending": "import pending — verbatim text only, facts still extracting",
        "processing": "import processing — verbatim text only, facts still extracting",
        "partial": "import partial — verbatim text only, some sections failed",
        "error": "import failed — verbatim text only; retry_document can re-read it",
    }
    for _h in hits or []:
        try:
            if not isinstance(_h, dict):
                continue
            _title = (_h.get("title") or _h.get("source_ref")
                      or f"document {_h.get('document_id')}").strip()
            _title = _clean_for_mcp(str(_title))[:120]
            _status = str(_h.get("status") or "").strip()
            _status_note = ""
            if _status and _status != "ready":
                _status_note = _STATUS_SUFFIX.get(
                    _status, f"import {_status} — verbatim text only")
            _passage = _h.get("chunk_text")
            if isinstance(_passage, str) and _passage.strip():
                # THE PASSAGE IS THE ANSWER: quote the chunk verbatim (whitespace-
                # collapsed — chunks carry markdown newlines; the assert band is one
                # line), capped only at the pathological-row ceiling above. Cleanup is
                # LIMITED to _DOC_PASSAGE_STRIP_PATTERNS (internal annotation debris);
                # the UUID strip is DELIBERATELY skipped — see its definition above.
                for _pat in _DOC_PASSAGE_STRIP_PATTERNS:
                    _passage = _pat.sub("", _passage)
                _passage = " ".join(_passage.split())
                if len(_passage) > _DOC_PASSAGE_DISPLAY_CHARS:
                    _passage = _passage[:_DOC_PASSAGE_DISPLAY_CHARS - 1].rstrip() + "…"
                _sec = _h.get("chunk_index")
                _sec = f", section {int(_sec) + 1}" if isinstance(_sec, int) else ""
                _line = (f"From your document {_title}: {_passage} "
                         f"(document id {_h.get('document_id')}{_sec}")
            else:
                # Title/source-only match (or a legacy W2-shaped hit): the document row
                # exists but no single chunk answered — discovery, not passage.
                _line = f"You have a document: {_title} (document id {_h.get('document_id')}"
            if _status_note:
                _line += f"; {_status_note}"
            _line += ")."
            lines.append(_line)
        except Exception:  # noqa: BLE001 — a malformed hit never breaks recall
            continue
    return lines


# ── INSTANT DOCUMENT STUB FACTS (migration 299 era, plan W2.2) ──────────────────────────
#
# The enqueue commit makes the verbatim text keyword-searchable (stub_tsv, migration 299);
# THIS lane makes the document's STRUCTURE walkable in the graph at the same instant — the
# document entity, its title/filename alias, and the headings the parser reported — ZERO
# LLM, deterministic, minted from PARSER-PROVIDED STRUCTURE ONLY.
#
# THE BUG-05 TRAP: interrogative fragments ("what is X", "how does Y work") must never
# become junk ENTITIES with fabricated parent_of edges. The mint below therefore carries
# a hard screen, pinned by tests:
#   * NEVER from chunk text — body prose is extraction's job; the stub mints
#     only the document's own name surface and markdown ATX headings (parser structure:
#     docling renders headings as '#'; naive_v1 pasted text simply carries none).
#   * NEVER a question-shaped name — a title/filename/heading that reads as a question
#     mints NOTHING (the document stays fully searchable via stub_tsv; only the GRAPH
#     mint is suppressed). A question is a yield shape, not a thing.
# Provenance follows the document lane's existing convention: source="document" →
# llm_inferred, staged Class B, source_ref citation threaded (migration 128), seeded rels
# only (instance_of P31, also_known_as P742, part_of P361, owns) — no invented vocabulary.
#

_STUB_INTERROGATIVES = frozenset({
    "what", "who", "whom", "whose", "when", "where", "why", "how", "which",
})

# Bounded structure yield: a stub is a table of contents, not a re-index of the body.
_STUB_MAX_HEADINGS = 20

_STUB_ATX_HEADING_RE = _re.compile(r"^\s{0,3}#{1,6}\s+(\S.*?)\s*$")


def _stub_is_question_shaped(name: str) -> bool:
    """True when a name surface reads as a QUESTION — the BUG-05 guard.

    Deterministic, closed-class: a trailing '?' or a leading wh-interrogative word. A
    question-shaped string mints NOTHING (graph-side); it stays searchable via stub_tsv."""
    s = (name or "").strip().rstrip("!. ")
    if s.endswith("?"):
        return True
    first = _re.match(r"[A-Za-z]+", s)
    return bool(first) and first.group(0).lower() in _STUB_INTERROGATIVES


def _stub_filename_stem(source_ref: str) -> str:
    """The filename surface of a source_ref ('file:///a/b/Report_v2.pdf' → 'Report_v2').

    A URL path TAIL WITHOUT an extension is also a legitimate document name surface
    ('https://site/docs/whitepaper' → 'whitepaper' — a slug the publisher named the
    document by); only an EMPTY tail yields ''. The question-shape screen still applies
    downstream before anything mints."""
    s = (source_ref or "").strip()
    if not s:
        return ""
    tail = _re.split(r"[\\/]", s)[-1].strip()
    if not tail or tail in (".", ".."):
        return ""
    # URL query-string / fragment tails are NOT part of the document's name surface
    # (adversarial-review fix): 'whitepaper?id=9' or 'faq#what-is-x' mint as junk.
    tail = _re.sub(r"[?#].*$", "", tail).strip()
    if not tail:
        return ""
    stem = _re.sub(r"\.[A-Za-z0-9]{1,8}$", "", tail).strip()
    return stem


def _stub_heading_names(chunks: list[str]) -> list[str]:
    """Markdown ATX headings the PARSER reported ('#'-prefixed lines in the chunk text).

    Docling (docling_v1) renders layout headings exactly this way; naive_v1 pasted text
    carries none unless the source itself was markdown (then it IS provided structure).
    Question-shaped headings are dropped HERE, at the yield — they never reach an edge."""
    out: list[str] = []
    seen: set[str] = set()
    for chunk in chunks or []:
        for line in (chunk or "").splitlines():
            m = _STUB_ATX_HEADING_RE.match(line or "")
            if not m:
                continue
            h = m.group(1).strip().strip("*_`").strip()
            if not h or len(h) > 200 or h.lower() in seen:
                continue
            if _stub_is_question_shaped(h):
                continue  # BUG-05: a question-shaped heading is yield text, not a thing
            seen.add(h.lower())
            out.append(h)
            if len(out) >= _STUB_MAX_HEADINGS:
                return out
    return out


def _mint_document_stub_edges(*, title: str, source_ref: str,
                              chunks: list[str]) -> list[dict[str, Any]]:
    """Build the deterministic stub edges for ONE document. Pure — no I/O, no LLM.

    Returns [] (mint NOTHING) when no non-question-shaped name surface exists, or when the
    only surface is question-shaped (the BUG-05 pin)."""
    t = (title or "").strip()
    stem = _stub_filename_stem(source_ref)
    name = ""
    aliases: list[str] = []
    if t and not _stub_is_question_shaped(t):
        name = t
        if stem and stem.lower() != t.lower() and not _stub_is_question_shaped(stem):
            aliases.append(stem)
    elif stem and not _stub_is_question_shaped(stem):
        name = stem
        if t and t.lower() != stem.lower() and not _stub_is_question_shaped(t):
            aliases.append(t)
    if not name:
        # No clean name surface (empty, or the only surface is question-shaped): mint
        # nothing. The document remains fully stub_tsv-searchable; only the graph stays
        # free of a junk entity.
        return []

    edges: list[dict[str, Any]] = [
        # The document instance filed AT its type node — THE HARD LINE respected: the
        # title/filename is the artefact's NAME (naming layer), never itself a place.
        {"subject": name, "rel_type": "instance_of", "object": "document",
         "subject_type": "Object", "object_type": "Concept",
         "fact_provenance": "llm_inferred"},
        # Reachability from the user anchor, exactly like the upload-event lane's owns edge.
        {"subject": "user", "rel_type": "owns", "object": name,
         "subject_type": "Person", "object_type": "Object",
         "fact_provenance": "llm_inferred"},
    ]
    for alias in aliases:
        edges.append({"subject": name, "rel_type": "also_known_as", "object": alias,
                      "subject_type": "Object", "object_type": "Concept",
                      "fact_provenance": "llm_inferred"})
    for h in _stub_heading_names(chunks):
        if h.lower() == name.lower():
            continue
        # A heading IS a component of the document (part_of: component → whole, seeded
        # P361, no invented vocabulary).
        edges.append({"subject": h, "rel_type": "part_of", "object": name,
                      "subject_type": "Concept", "object_type": "Object",
                      "fact_provenance": "llm_inferred"})
    ref = (source_ref or "").strip()
    if ref:
        for e in edges:
            e["source_ref"] = ref
    return edges


async def _mint_document_stub_facts(*, user_id: str, title: str, source_ref: str,
                                    chunks: list[str], document_id: Any) -> int:
    """Write the stub facts for an accepted document. Best-effort + LOUD, zero LLM.

    Returns the edge count minted (0 = nothing minted — no name surface, or the write
    did not land, which is logged). NEVER raises into the accept path: the document is
    already accepted; a failed surface-mint must not fail the ingest."""
    try:
        edges = _mint_document_stub_edges(title=title, source_ref=source_ref, chunks=chunks)
        if not edges:
            _log("document_stub: no mint (no clean name surface) "
                 f"user={user_id[:8]} doc={document_id} ref={(source_ref or '')!r:.120}")
            return 0
        body: dict[str, Any] = {
            "text": f"document stub: {(title or source_ref or '').strip()}"[:200],
            "user_id": user_id,
            "edges": edges,
            "source": "document",  # document tier: → llm_inferred, staged B (never A)
        }
        if (source_ref or "").strip():
            body["source_ref"] = source_ref.strip()
        try:
            resp = await _http_client.post(
                f"{FAULTLINE_API_URL}/ingest", json=body, timeout=15.0)
        except Exception as _ie:  # noqa: BLE001 — transport fault never fails the accept
            _log(f"SECURITY-CRITICAL: document_stub_mint_not_committed "
                 f"user={user_id[:8]} doc={document_id} edges={len(edges)} "
                 f"reason={_ie!r} — stub facts not committed, ingest unaffected")
            return 0
        if resp.status_code >= 400:
            _log(f"SECURITY-CRITICAL: document_stub_mint_refused "
                 f"user={user_id[:8]} doc={document_id} edges={len(edges)} "
                 f"status={resp.status_code} — stub facts not committed, ingest unaffected")
            return 0
        data = {}
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 — non-JSON success shape
            data = {}
        # HONEST COUNT: report what the store SAID landed (committed + staged — the
        # document tier is staged-B by design). The fallback to the sent count fires
        # ONLY when the success shape carries neither key; an explicit zero stays an
        # honest zero (the store said nothing landed).
        landed = None
        try:
            if "committed" in data or "staged" in data:
                landed = int(data.get("committed") or 0) + int(data.get("staged") or 0)
        except (TypeError, ValueError):
            landed = None
        _log(f"document_stub: minted {landed if landed is not None else '?'}/"
             f"{len(edges)} stub edge(s) doc={document_id} user={user_id[:8]}")
        return landed if landed is not None else len(edges)
    except Exception as stub_exc:  # noqa: BLE001 — the stub never breaks the ingest
        _log(f"SECURITY-CRITICAL: document_stub_mint_failed "
             f"user={user_id[:8]} doc={document_id}: {stub_exc!r} — ingest unaffected")
        return 0


async def recall_memory_tool(query: str, user_id: str) -> dict[str, Any]:
    """Call FaultLine /query endpoint and return human-readable prose.

    If query starts with /learn, generate and ingest an ontological hierarchy
    for the topic as llm_learn facts — no LLM function calling required.

    DB-weighted intent routing (RECALL_INTENT_ROUTING, default on): the route is the BRAIN's
    decision, not the model's tool-pick. After the slash intercept, consult /classify-intent
    (same brain remember_facts_tool uses) and DEFER to the intent — a CORRECTION/RETRACTION the
    model mis-routed to recall goes to retract_fact_tool (→ /retract/correct →
    _detect_structural_correction, e.g. "my pets are not part of my family"); a STATEMENT goes to
    the ingest path. QUERY — or ANY classify error / fallback — falls through to the normal /query
    recall. FAIL-SAFE: recall never breaks; a genuine recall question still recalls.
    """
    intercepted = await _maybe_intercept_slash(query, user_id)
    if intercepted is not None:
        return intercepted

    # ── DB-weighted intent route (brain-not-transport) ───────────────────────
    # The fact that the model called recall_memory is just the entry point; it defers to the
    # backend brain's route. FAIL-SAFE: classify error → fall through to plain recall below.
    _ingest_fallback = None  # non-eating STATEMENT-ingest result; surfaced only if the walk is empty
    _fast_path = False  # brain-slow turn: routing/gate skipped, /query fires with raw text
    if RECALL_INTENT_ROUTING:
        if _brain_slow_active(user_id):
            # ── FAST PATH (brain-independence lane, 2026-10-25) ────────────────────
            # The brain is KNOWN slow (this seat's breaker open from a classify timeout
            # this window, or MCP_BRAIN_DEGRADED=1). SKIP the confidence gate and intent
            # routing entirely and fire /query directly with the raw text: the API's
            # passage-first floor (BUG-01/02/07) serves document_hits regardless of brain
            # state, its own QUERY_WALK_TIMEOUT_S bounds the walk, and /query parses the
            # text itself. This substitutes NO route — QUERY-for-recall is the documented
            # read-only fail-safe (writes nothing, deletes nothing), so a false-positive
            # window costs one turn of route fidelity, never data.
            intent = "QUERY"
            _fast_path = True
            _log(f"recall_memory.fast_path: brain slow for this seat "
                 f"({_brain_slow_reason(user_id)}) — intent routing + confidence gate "
                 "SKIPPED, /query direct")
        else:
            try:
                # gliner2_fallback=False: on a classify timeout DO NOT re-ask — the
                # budget already marked this seat's brain slow, and the re-ask would pay
                # a second budget against the same slow endpoint. THIS turn's degradation
                # is the trip arm below (intent=QUERY + fast-path retention); the NEXT
                # turn enters through the breaker arm above with zero classify spend.
                # probe_budget: the recall probe rides MCP_CLASSIFY_TIMEOUT_S (default
                # 3s) — the one knob that must sit UNDER the LLM-tier budgets it is
                # escaping; write lanes keep the 10s legacy (see _classify_and_gate).
                intent, _confidence, _gate = await _classify_and_gate(
                    query, user_id, gliner2_fallback=False,
                    probe_budget=_classify_probe_budget_s())
            except Exception:
                # TRIP ARM. Transport exhaustion (timeout / unreachable) marked this seat
                # slow on the way out; a deterministic backend DECISION (4xx) or a
                # malformed body did NOT — the full pipeline is correct there. Only the
                # slow shape fast-paths retention (round-2 review finding 5). Either way
                # recall's read-only fail-safe stands: never break recall.
                intent = "QUERY"  # classify unavailable → treat as a genuine recall (never break recall)
                _fast_path = _brain_slow_active(user_id)
            if intent in ("RETRACTION", "CORRECTION"):
                return await retract_fact_tool(query, user_id, classified_intent=intent)
            # STATEMENT → ingest as a NON-EATING fallback. UNIFORM-PATH PRINCIPLE: recall ALWAYS
            # walks the layers; a recall is never replaced by an ingest no-op. GLiNER2 routinely
            # mis-classifies an interrogative as STATEMENT ("how am I feeling" scored STATEMENT) —
            # the old `return remember_facts_tool(...)` then ATE the recall and surfaced
            # {"status":"no_ingest"} instead of walking. Now: attempt the ingest (so a genuinely
            # mis-routed whole statement like "my dog is Rex" still gets stored — remember_facts_tool
            # is itself gated, so a question that extracts nothing stores nothing), but DO NOT return
            # here. Fall through to the /query layer walk; only surface this ingest result if the walk
            # finds nothing (a true statement with nothing to recall). _passes_ingest_gate still
            # filters non-ingestable bare keywords so we don't waste an extraction pass on them.
            _ingest_fallback = None
            if intent == "STATEMENT" and _passes_ingest_gate(query):
                _ingest_fallback = await remember_facts_tool(query, user_id)
            # intent == "QUERY", a non-ingestable STATEMENT, or anything else → normal recall below.

    # Intent-INDEPENDENT harvest: even though this turn routes to recall, a fact may be buried
    # in the question ("...help me? by the way, I fixed the fence three weeks ago"). The cheap
    # segmenter (trigger_span, no LLM) splits it off and GLiNER2 ingests it — so question-carried
    # facts are stored, not dropped at the QUERY gate. Best-effort; no-op when no span is found.
    if _fast_path or _brain_slow_active(user_id):
        # FAST-PATH RETENTION instead of the harvest (brain-independence lane). Keys on
        # the breaker STATE here, not the entry-time flag, so it covers BOTH arms: the
        # entry fast path AND the trip turn (a probe that timed out mid-turn marked this
        # seat on its way out — round-2 review findings 1/2; RECALL_INTENT_ROUTING=off
        # turns take it too when the window is open). /harvest-spans can itself sit on
        # LLM-tailored lanes with only the client's default budget — on the exact box
        # this lane fixes it could re-hang the turn AFTER the routing was skipped. The
        # honest degradation is to retain the turn VERBATIM via /episodic/append (one
        # INSERT, no LLM, its own 5s budget, soft-fail) so the re-miner can extract it
        # once the brain is healthy. The intent is None — the brain classified NOTHING on
        # this turn, and the durable row must not carry a transport-fabricated verdict
        # (round-2 review finding 2). Deterministic, bounded, loses nothing recoverable.
        await _episodic_capture(query, user_id, None)
    else:
        await _harvest_turn_facts(query, user_id)

    resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/query",
        json={"text": query, "user_id": user_id},
    )
    resp.raise_for_status()
    data = resp.json()

    facts = data.get("facts", [])
    # DOCUMENT PASSAGE LANE (migration 299 → R6): the walk payload carries the
    # deterministic per-chunk passage hits over documents chunks (top 3 chunks,
    # backend-ranked by per-chunk ts_rank). Rendered as deterministic ASSERT lines
    # through the document band (BUG-09: passages LEAD every fact band and keep the
    # second person in every voice) — the passage is VERBATIM RETAINED TEXT (a
    # row-level fact, not an inference), so unlike the held band this is stated as
    # plain knowledge. Empty/absent = honest no-match; the backend lane soft-failed
    # to [] and never raised.
    _doc_hits = data.get("document_hits") or []
    _doc_hit_lines = _render_document_hit_lines(_doc_hits)
    attributes: dict = data.get("attributes", {})
    # NOTE: preferred_names / canonical_identity are no longer consumed here —
    # perspective ("you" vs name) is resolved upstream in the backend's
    # convert_to_prose. The MCP layer no longer rewrites identity tokens.

    if not facts and not attributes:
        # The layer walk found nothing. If this turn was a genuinely-ingestable STATEMENT the
        # model mis-routed to recall, surface that ingest result now (it wasn't a recall after
        # all). Otherwise it's an honest empty recall.
        if _ingest_fallback is not None:
            return _ingest_fallback
        if _doc_hit_lines:
            # Empty fact walk, but the stub lane matched: the document hit IS the
            # grounded answer surface for "do I have anything about Y" (migration 299).
            # Deterministic ASSERT lines only — no abstention on top (we DID find
            # something: the document, verbatim). A pending import still says so via
            # the hit line's own honest status suffix. BUG-09: they LEAD (their own
            # band, second person — never rewritten to a third person).
            return {"memory": "\n".join(_doc_hit_lines)}
        return {"memory": "No relevant facts found."}

    # Perspective ("you" vs name) is now resolved UPSTREAM by the backend
    # (convert_to_prose builds prose from graph identity: the querying user's own
    # slots already arrive as "you", everyone else by their preferred alias). The
    # old name→"you" string-substitution map lived here as a tourniquet; it is
    # dead now that the backend emits perspective at build time. Removing it also
    # kills the "\b name \b" rewrite that historically produced "The alexander".
    # _clean_for_mcp is retained as belt-and-suspenders against stray
    # UUID/label tokens in older prose.

    # PART 2 (DESIGN-ingest-spine-and-temporal-recall §"RECALL-SIDE TEMPORAL ORDERING"):
    # when the backend resolved a temporal pivot/ordinal it PRE-SORTED the dated facts
    # chronologically. Hand the model that order as TIMESTAMP-PREFIXED evidence
    # (Event #[i] [date]: …) with an explicit instruction not to reorder — the store
    # (PostgreSQL) already did the date math, so the model only renders prose. This is
    # the fix for temporal inversion (the model reordering an unordered bag).
    _temporal_ordered = bool(data.get("temporal_ordered"))

    def _event_date_str(_f: dict) -> str | None:
        _ed = _f.get("event_date")
        if not _ed:
            return None
        # event_date is an ISO timestamp string from the backend; the calendar day is
        # the human-meaningful key. Best-effort slice, never raises.
        try:
            return str(_ed)[:10]
        except Exception:
            return None

    # Stance (confidence-as-voice): split facts by fact_class so the preamble can
    # instruct the model to ASSERT corroborated facts (A/B) and HOLD/soften
    # speculative ones (C). Stance is never printed as a label — it shapes the
    # preamble only (CLAUDE.md: no internal labels leak to user-facing text).
    assert_lines: list[str] = []
    hold_lines: list[str] = []
    event_lines: list[str] = []  # PART 2: chronological, timestamp-prefixed evidence
    seen: set[str] = set()

    def _emit(text: str, fact_class: str) -> None:
        if not text or text in seen:
            return
        seen.add(text)
        if str(fact_class or "").upper() == "C":
            hold_lines.append(text)
        else:
            assert_lines.append(text)

    for fact in facts:
        fact_class = fact.get("fact_class")
        if fact.get("rel_type") == "context":
            # store_context facts carry unstructured prose in the `object` field
            # (stored verbatim as req.text[:120] by /store_context — never UUIDs
            # or canonical slugs). Use it directly — the `definition` field
            # contains internal annotations that are not suitable for injection.
            # Unbound tier: free prose, no clean slots — pass as associative
            # context (always held/soft, never asserted as a structured fact).
            raw_text = fact.get("object", "")
            if not raw_text:
                continue
            text = _clean_for_mcp(raw_text)
            if text and text not in seen:
                seen.add(text)
                hold_lines.append(text)
            continue
        else:
            definition = fact.get("definition", "")
            if not definition:
                continue
            text = _clean_for_mcp(definition)

        # Under temporal ordering, a DATED fact becomes a timestamp-prefixed event
        # line in the backend's (already chronological) order — never re-tiered, never
        # reordered. Undated facts under the same query still flow through the normal
        # assert/hold tiering below.
        _eds = _event_date_str(fact) if _temporal_ordered else None
        if _eds and text and text not in seen:
            seen.add(text)
            event_lines.append(f"Event #{len(event_lines) + 1} [{_eds}]: {text}")
            continue
        _emit(text, fact_class)

    # Scalar attributes are user-stated/derived facts — treat as assertable.
    for attr, value in attributes.items():
        line = f"{attr}: {value}"
        _emit(line, "A")

    # DOCUMENT-HITS STUB LANE (migration 299, W2.3): the deterministic assert lines ride
    # their OWN document band (BUG-09, 2026-10-24 bench): they LEAD every fact band (a
    # passage is verbatim-from-source; extracted/staged facts may be B-tier debris and
    # must never own the topline above verified text) and they are exempt from any
    # person rewriting ("their document" would mis-attribute the seat's own library).
    # They still count as content below, so a facts-empty-but-doc-hit recall renders the
    # hits instead of the abstention.
    for _dl in _doc_hit_lines:
        seen.add(_dl)

    # Doc hits COUNT AS CONTENT (they did when they rode assert_lines; they still must
    # now that they have their own band): a recall whose facts were all confidence-
    # filtered but which holds a verbatim passage renders the passage, never an
    # abstention on top of a found document.
    if not assert_lines and not hold_lines and not event_lines and not _doc_hit_lines:
        return {"memory": "No relevant facts found."}

    preamble = (
        "The following is what you know from previous conversations. "
        "Treat these as things you personally remember — weave them into "
        "your response naturally, as your own knowledge. Never list them, "
        "never say 'according to my records', never quote them verbatim."
    )

    sections: list[str] = []
    # DOCUMENT PASSAGES LEAD (BUG-09): verbatim source text is the highest-truth band,
    # so it renders FIRST — never below staged/extracted facts — and is never
    # person-rewritten (the seat IS the document owner; "their document" is a
    # mis-attribution, not a neutralization). Plain join, no intro of its own — a
    # passage line already carries its own "From your document <title>: …" framing.
    _docs = [str(x).strip() for x in (_doc_hit_lines or []) if str(x).strip()]
    if _docs:
        sections.append("\n".join(_docs))
    if event_lines:
        # PART 2: the events are ALREADY in true chronological order (PostgreSQL sorted
        # them by date). The model must NOT re-derive or reorder the sequence — it
        # answers ordering/"first…after…" questions directly from this order.
        sections.append(
            "These events are listed in the exact order they happened (earliest "
            "first) — trust this order, do not reorder or recompute it:\n"
            + "\n".join(event_lines)
        )
    if assert_lines:
        sections.append("\n".join(assert_lines))
    if hold_lines:
        # Hold/soften: present speculative recall as tentative, not as fact.
        sections.append(
            "You are less certain about the following — mention them only if "
            "relevant, and tentatively, never as established fact:\n"
            + "\n".join(hold_lines)
        )

    # Re-query hint — NON-EMPTY recall ONLY. This is GUIDANCE to the model, not a
    # backend-state claim: there is no `more_available` boolean (that would have to be
    # computed where the scope/walk cap lives, i.e. backend-side). The model decides
    # whether the user's message had other distinct topics; no keyword detection here
    # (brain-not-transport).
    #
    # ⚠️ BENCHMARK CONTRACT: the EMPTY sentinel {"memory": "No relevant facts found."}
    # above must stay BYTE-IDENTICAL — the abstention scorer keys off that exact string.
    # Appending this hint to an empty recall would silently corrupt every abstention score.
    _more_hint = (
        "\n\nIf their message touched on other distinct topics, recall each before responding."
    )
    return {"memory": f"{preamble}\n\n" + "\n\n".join(sections) + _more_hint}


def _split_sentences(text: str) -> list[str]:
    """Naive sentence split on `[.!?]` boundaries.

    Local, dependency-free counterpart to the backend's chunking sentence split —
    server.py deliberately does not import from src.api.main. Good enough for
    residual-sentence bookkeeping; not used for extraction itself.
    """
    return [s.strip() for s in _re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _extraction_residual_sentences(text: str, edges: list[dict]) -> list[str]:
    """Return input sentences that contributed NO extracted edge.

    A sentence is 'covered' when any edge's subject or object string (lowercased,
    substring match) appears in the lowercased sentence. Everything else is
    residual — content the extractor read but did not structure.
    """
    terms: set[str] = set()
    for e in edges:
        for key in ("subject", "object"):
            val = e.get(key)
            if isinstance(val, str) and val.strip():
                terms.add(val.strip().lower())
    residual: list[str] = []
    for sentence in _split_sentences(text):
        lowered = sentence.lower()
        if not any(term in lowered for term in terms):
            residual.append(sentence)
    return residual


# ── Document chunking (deterministic — no LLM) ──────────────────────────────

# Hard cap on chunks processed per document — bounds total backend work.
_DOC_MAX_CHUNKS = 200
# Soft size/shape targets per chunk (the conversational pipeline chunks at
# 3 sentences; documents get paragraph-shaped chunks of roughly 2-6 sentences).
_DOC_CHUNK_MAX_CHARS = 1200
_DOC_CHUNK_MAX_SENTS = 6
_DOC_CHUNK_MIN_SENTS = 2


def _chunk_document(text: str) -> list[str]:
    """Split a document into paragraph-shaped chunks. Deterministic — no LLM.

    1. Split on blank lines (paragraph boundaries).
    2. Any paragraph over _DOC_CHUNK_MAX_CHARS is re-split on sentence
       boundaries (via _split_sentences) into <=_DOC_CHUNK_MAX_CHARS pieces.
    3. Tiny fragments (fewer than _DOC_CHUNK_MIN_SENTS sentences) are merged
       forward with the following piece so each chunk lands at roughly
       2-6 sentences, never exceeding the char bound during a merge.
    """
    paragraphs = [p.strip() for p in _re.split(r"\n\s*\n", text) if p.strip()]

    # Step 2: break oversized paragraphs on sentence boundaries.
    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= _DOC_CHUNK_MAX_CHARS:
            pieces.append(para)
            continue
        buf: list[str] = []
        buf_len = 0
        for sent in _split_sentences(para):
            if buf and buf_len + len(sent) + 1 > _DOC_CHUNK_MAX_CHARS:
                pieces.append(" ".join(buf))
                buf, buf_len = [], 0
            buf.append(sent)
            buf_len += len(sent) + 1
        if buf:
            pieces.append(" ".join(buf))

    # Step 3: merge tiny fragments forward. Merging only continues while the
    # accumulating chunk is still tiny (< min sentences); a normal paragraph
    # therefore stays its own chunk.
    chunks: list[str] = []
    cur: list[str] = []
    cur_sents = 0
    cur_len = 0
    for piece in pieces:
        n_sents = max(1, len(_split_sentences(piece)))
        if cur and (
            cur_sents >= _DOC_CHUNK_MIN_SENTS
            or cur_sents + n_sents > _DOC_CHUNK_MAX_SENTS
            or cur_len + len(piece) + 2 > _DOC_CHUNK_MAX_CHARS
        ):
            chunks.append("\n\n".join(cur))
            cur, cur_sents, cur_len = [], 0, 0
        cur.append(piece)
        cur_sents += n_sents
        cur_len += len(piece) + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


async def _episodic_capture(text: str, user_id: str, intent: str | None) -> None:
    """Durable episodic capture (safety net) — POST /episodic/append, NEVER fatal.

    Persists the raw utterance VERBATIM before any routing/extraction so short
    fragments, misrouted-as-QUERY text, harvested non-STATEMENT turns, and
    no-extraction ramblings are ALL retained even when they produce zero
    structured facts. The backend endpoint itself is soft-fail (never 500s on an
    unprovisioned schema); any transport failure here is logged and swallowed.
    """
    try:
        await _http_client.post(
            f"{FAULTLINE_API_URL}/episodic/append",
            json={
                "user_id": user_id,
                "raw_text": text,
                "source": "mcp",
                "intent": intent,
                "extracted_fact_count": None,
            },
            timeout=5.0,
        )
    except Exception as exc:
        _log(f"episodic_append_failed (non-fatal): {exc!r}")


async def remember_facts_tool(text: str, user_id: str) -> dict[str, Any]:
    """Call /extract/rewrite then /ingest — full pipeline in one call.

    Mirrors the OpenWebUI Filter intent classification pipeline:
    1. Injection check (security gate — runs first)
    2. GLiNER2 intent classification via /classify-intent
    3. Per-user confidence gate via /confidence-gate
    4. Route: QUERY → early return, RETRACTION/CORRECTION → retract_fact_tool,
       STATEMENT → /extract/rewrite → /ingest
    5. Ingest gating: word count >= 3 or identity pattern match
    """
    # Pre-flight injection check — reject before any LLM or backend call.
    injection_signal = _check_injection_signals(text)
    if injection_signal:
        _log(f"SECURITY: injection signal rejected — {injection_signal[:80]}")
        return {"status": "rejected", "reason": "Input contains disallowed content", "committed": 0}

    # ── Intent classification + per-user gate (Layer 1/3) ────────────────────
    # Shared DB-weighted intent BRAIN (transport-parity — lives ONCE in _classify_and_gate,
    # consumed identically by recall_memory_tool). Non-fatal: default to STATEMENT/0.70 if the
    # endpoint is unavailable (the model called remember_facts → safest fall-through is ingest).
    try:
        # gliner2_fallback=True (brain-independence lane): when the budgeted classify times
        # out, ONE skip_llm re-ask fetches the backend's DETERMINISTIC route (spaCy preroutes
        # + GLiNER2 + DB patterns — no LLM tier) so a slow-brain box still routes statements
        # instead of degrading them. The STATEMENT default below is unchanged for the case
        # where even the deterministic answer cannot be had.
        intent, confidence, gate = await _classify_and_gate(text, user_id, gliner2_fallback=True)
    except Exception:
        intent, confidence, gate = "STATEMENT", 0.0, 0.70

    # ── Durable episodic capture (safety net) ────────────────────────────────
    # Persist the raw utterance VERBATIM before any routing/extraction, so short
    # fragments, misrouted-as-QUERY text, harvested non-STATEMENT turns, and
    # no-extraction ramblings are ALL retained even when they produce zero
    # structured facts. Placed AFTER the injection check (rejected content is
    # never stored) and intent classification (so intent is known), but BEFORE
    # the intent-independent harvest, the routing branches, and the word-count
    # gate below — this is the only point that captures every downstream path.
    # A failure here MUST NEVER break remember_facts: log and continue.
    await _episodic_capture(text, user_id, intent)

    # ── Trust the backend route (transport parity — do NOT re-derive) ────────
    # The route/gate/escalation decision is BRAIN, not transport. /classify-intent already
    # applies the per-user confidence gate AND the low-confidence LLM escalation (the strong
    # gate that interrogates "correction or not?" before routing). Re-applying our OWN weak
    # "confidence < gate → STATEMENT" here would CLOBBER an escalated CORRECTION back to
    # STATEMENT and silently undo the feature. So the MCP DEFERS: it trusts the intent the
    # backend returned. The single source of truth for the route is /classify-intent.
    # (We still fetch `gate` above only for the diagnostic log line; it no longer drives routing.)
    # NOTE: the OpenWebUI Filter (intentionally disabled) carries the same assumption — when it
    # is re-enabled it must defer to the backend route too, not reintroduce a third copy.

    # ── Route by intent ──────────────────────────────────────────────────────
    # INTENT-INDEPENDENT HARVEST (INGEST_INTENT_INDEPENDENT_HARVEST, default on):
    # the model called remember_facts → the turn is MEANT to store facts. The dominant-intent
    # route may be QUERY (buried fact in a question) or CORRECTION/RETRACTION (past-tense "I fixed
    # the fence" mis-scoring as a correction), which historically BAILED before any extraction ran
    # and dropped the fact. Before honoring those non-STATEMENT routes, fire the SAME cheap
    # deterministic harvest the recall path uses (segmenter → reframe → verb-lift → GLiNER2, NO LLM
    # triple extraction; _harvest_turn_facts is fully fail-safe — a failure stores nothing and never
    # raises). This does NOT replace the route: a CORRECTION still goes on to retract (its buried
    # NEW facts are now ALSO captured), a QUERY still returns its "use recall" hint. The STATEMENT
    # branch is left untouched and does NOT call this — it harvests via /extract/rewrite below, so
    # there is no double-ingest.
    if intent != "STATEMENT" and INGEST_INTENT_INDEPENDENT_HARVEST:
        _harvested = await _harvest_turn_facts(text, user_id)
        # ORDERED FALLTHROUGH (no double-ingest of the SAME turn): harvest and grounding can both
        # capture the SAME self-predication fact for one turn ("I felt stressed yesterday" → feels).
        # If they BOTH ingest, the turn lands twice — one copy stamped with event_date, one undated —
        # and recall's facts-over-staged dedup then shadows the dated copy with the undated one. So
        # ground a bare-copula self-statement ("I am worried"/"I am Alex") ONLY when harvest captured
        # nothing for this turn. This mirrors the STATEMENT branch's harvest→ground fallthrough below.
        # FAIL-SAFE: a turn whose only fact comes from grounding is still captured (harvest returns 0).
        if not _harvested:
            await _ground_self_predication_facts(text, user_id)

    if intent == "QUERY":
        return {"status": "query_detected", "message": "Use recall_memory for queries"}

    if intent in ("RETRACTION", "CORRECTION"):
        return await retract_fact_tool(text, user_id, classified_intent=intent)

    # intent == "STATEMENT" — proceed with ingest pipeline.

    # ── Ingest gating (mirrors Filter faultline_function.py:3445-3449) ───────
    # Shared with recall_memory_tool's STATEMENT-diversion guard via _passes_ingest_gate
    # so both sites agree on what "ingestable" means (word_count >= 3 OR self-identity).
    if not _passes_ingest_gate(text):
        # No held-blob fallback here (ingest-spine "no-islands" spec, guarded by
        # test_store_context_residue_fallback_removed). The episodic capture above
        # already retains the fragment verbatim for re-mining by the backfill.
        # DIRECTIVE, not a status report. Models parrot tool output verbatim, so a
        # descriptive message ("Text too short for fact extraction") made weak models
        # announce "I couldn't store that" to the user — breaking the silence-is-the-feature
        # design. The CAUSE is logged for us; the model is told what to DO.
        _log(f"no_ingest: too short ({len(text.split())} words): {text[:60]!r}")
        return {"status": "no_ingest", "message": "Respond normally; do not mention memory or storage.",
                "isError": True}

    # ── D1: STATEMENT extractor route (brain-not-transport) ──────────────────
    # WHICH extractor a STATEMENT goes through is a BRAIN decision (gated backend-side by
    # SENTENCE_PIPELINE, default OFF). The MCP consumes that decision; it does NOT read the flag.
    #   • route == "spine"   → run the DETERMINISTIC strength-passing spine (/harvest-spans →
    #     /ingest) as the PRIMARY extractor. LLM is segmentation-only (the spine's atomizer); NO
    #     /extract/rewrite triple extraction. The spine ingests its edges ONCE and self-handles
    #     self-predication ("I am X" / "my favorite X") + residue→Class-C, so we do NOT also run
    #     the no-edges harvest fallback or _ground_self_predication for this text → NO DOUBLE-INGEST.
    #     FAIL-SAFE: spine yields no edge (None) → fall through to the legacy /extract/rewrite path
    #     below (never a silent drop). A successful spine ingest RETURNS here.
    #   • route == "rewrite" (DEFAULT, flag OFF / brain unreachable) → fall straight through to the
    #     existing /extract/rewrite path below, BYTE-IDENTICAL to today's prod behavior.
    if await _statement_extractor_route(user_id) == "spine":
        _spine_result = await _ingest_statement_via_spine(text, user_id)
        if _spine_result is not None:
            return _spine_result
        # None → spine produced no edge / errored → fall through to /extract/rewrite (fail-safe).
        _log("statement_via_spine: no edges — falling back to /extract/rewrite (fail-safe)")

    rewrite_resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/extract/rewrite",
        json={"text": text, "user_id": user_id},
        timeout=60.0,
    )
    rewrite_resp.raise_for_status()
    _rewrite_data = rewrite_resp.json()
    # Backend freeze switch: extraction is gated backend-side too (its entity-strengthen
    # phase writes knowledge rows). Short-circuit HERE — the harvest/grounding fallbacks
    # and /ingest below are all frozen as well; surface the message, not zero-shaped churn.
    if _is_ingest_disabled(_rewrite_data):
        _log("remember_facts: backend ingest disabled (knowledge-store mode)")
        return {"status": _INGEST_DISABLED_STATUS, "message": _INGEST_DISABLED_MESSAGE}
    _raw_edges = _rewrite_data.get("edges", [])
    edges = [e for e in _raw_edges if not e.get("low_confidence", False)]
    # Low-confidence edges stay excluded from /ingest (deliberate — protects the
    # WGM gate) but are demoted to the Class C context lane in the remainder
    # capture after a successful ingest below, not silently dropped.
    low_conf_edges = [e for e in _raw_edges if e.get("low_confidence", False)]
    if not edges:
        # /extract/rewrite returns no structured edges for CONSTRUCTION-only facts — most
        # notably affective statements ("I feel anxious"): the complement is not a GLiNER2
        # entity, so the LLM/GLiNER2 extractor produces nothing. /harvest-spans DOES capture
        # these (the deterministic feel-verb seam, segmenter-independent). Strong-ingest:
        # before dropping to a Class-C blob, fire the SAME intent-independent harvest the
        # non-STATEMENT branch uses. It's cheap on a bare feeling (no fact-bearing span → no
        # reframe LLM) and fully fail-safe. If it captured a real fact, we're done.
        if INGEST_INTENT_INDEPENDENT_HARVEST:
            _harvested = await _harvest_turn_facts(text, user_id)
            if _harvested:
                return {"status": "stored", "harvested": _harvested,
                        "message": f"Captured {_harvested} fact(s)."}
        # Self-predication grounding: "I am X" → LLM grounds X on the entity-match layer →
        # routes to feels/also_known_as/occupation. Retires the greedy name regex; captures
        # bare-copula feelings AND names by GROUNDING. Last builder before residue is DROPPED.
        _grounded = await _ground_self_predication_facts(text, user_id)
        if _grounded:
            return {"status": "stored", "grounded": _grounded,
                    "message": f"Captured {_grounded} self-fact(s)."}
        # INGEST-SPINE (Part 1, item 4) — HELD-BLOB: DROP. The guardrailed builder ran on every
        # clause (/extract/rewrite → /harvest-spans decompose → grounding) and produced no valid,
        # hierarchy-placeable triple. Residue that cannot build a triple even after grounding is
        # DROPPED — there is NO store_context Class-C blob. An un-walkable held blob is exactly the
        # island the no-islands invariant forbids; "is this worth keeping?" == "did the builder
        # produce a valid triple?", and here the answer is no. (The Class-C store_context fallback
        # that used to live here is removed per the ingest-spine spec; SHORT_TERM_MEMORY no longer
        # gates this path.)
        _log(f"residue_dropped: no valid triple from {text[:60]!r}")
        # DIRECTIVE, not a status report — see the too-short branch above.
        return {"status": "no_ingest", "message": "Respond normally; do not mention memory or storage.",
                "isError": True}
    ingest_resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/ingest",
        json={"text": text, "user_id": user_id, "edges": edges, "source": "mcp"},
        timeout=30.0,
    )
    ingest_resp.raise_for_status()
    ingest_result = ingest_resp.json()

    # Backend freeze switch: surface a clear message instead of pretending success.
    if _is_ingest_disabled(ingest_result):
        _log("remember_facts: backend ingest disabled (knowledge-store mode)")
        return {"status": _INGEST_DISABLED_STATUS, "message": _INGEST_DISABLED_MESSAGE}

    # ── Extraction remainder capture ─────────────────────────────────────────
    # Extraction succeeded for SOME of the input; sentences that produced no
    # structured edge — plus low-confidence edges filtered from /ingest — go to
    # the Class C fuzzy lane instead of the void. Fire-and-forget supplement:
    # any failure here must never affect the already-committed ingest response.
    # (The spine route above self-handles its residue backend-side; this covers
    # the legacy /extract/rewrite path only.)
    remainder_stored = False
    if SHORT_TERM_MEMORY:
        try:
            residual_sentences = _extraction_residual_sentences(text, edges)
            residual_text = " ".join(residual_sentences)
            if len(residual_text.split()) < 4:
                # Trivially small residual is noise — episodic log has it verbatim.
                residual_text = ""

            demoted_lines: list[str] = []
            for e in low_conf_edges:
                subj = str(e.get("subject", "")).strip()
                rel = str(e.get("rel_type", "")).strip().replace("_", " ")
                obj = str(e.get("object", "")).strip()
                if subj and rel and obj:
                    demoted_lines.append(f"{subj} {rel} {obj}.")
            if demoted_lines:
                _log(f"low_confidence_demoted_to_context: count={len(demoted_lines)}")

            supplement_parts = ([residual_text] if residual_text else []) + demoted_lines
            if supplement_parts:
                supplement = " ".join(supplement_parts)
                await store_context_tool(text=supplement, user_id=user_id)
                remainder_stored = True
                _log(
                    f"remainder_stored: residual_sentences={len(residual_sentences) if residual_text else 0} "
                    f"demoted_edges={len(demoted_lines)} chars={len(supplement)}"
                )
        except Exception as exc:
            _log(f"remainder_capture_failed (non-fatal): {exc!r}")
    ingest_result["remainder_stored"] = remainder_stored
    return ingest_result


async def ingest_document_tool(
    text: str, user_id: str, source_ref: str = "", title: str = ""
) -> dict[str, Any]:
    """Document/bulk ingest lane — a wrapper over the existing endpoints.

    Documents (PDF text, web articles, long pasted notes) are STATEMENTS by
    definition, so intent classification and the word-count ingest gate are
    skipped entirely. The document is chunked deterministically by paragraph
    (no LLM, unlike the 3-sentence density chunking tuned for conversation),
    and each chunk runs the SAME brain-decided STATEMENT extraction fork
    remember_facts uses — the deterministic spine (/harvest-spans → /ingest)
    when the backend routes "spine", falling back to the legacy
    /extract/rewrite → /ingest → remainder-capture path — with durable
    per-chunk episodic retention BEFORE extraction. Per-chunk failures never
    abort the document.

    Provisioning is gated by the TRANSPORT (same as every other tool):
    _call_tool for JSON-RPC, the REST endpoint for OpenWebUI's OpenAPI path.
    The injection check runs here, ONCE per document, before any backend call.

    source_ref/title: citable provenance (migration 128) — threaded into the
    episodic log AND every per-chunk /ingest so document-derived facts carry
    their citation through staging, promotion, and the Qdrant payload.
    """
    ref = (source_ref or title or "").strip()

    # W2.2: set at accept time (after chunking); 0 on every early-return path.
    stub_facts_minted = 0

    def _summary(status: str, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "status": status,
            "chunks": 0,
            "chunks_failed": 0,
            "facts_committed": 0,
            "facts_staged": 0,
            "context_stored": 0,
            "truncated": False,
            "source_ref": ref,
            # W2.2 instant stub: count of deterministic document-structure edges minted
            # at accept time (0 = no clean name surface or the mint did not land).
            "stub_facts_minted": stub_facts_minted,
        }
        base.update(overrides)
        return base

    # ── Pre-flight injection check — reject before any backend call ─────────
    injection_signal = _check_injection_signals(text)
    if injection_signal:
        _log(f"SECURITY: injection signal rejected (ingest_document) — {injection_signal[:80]}")
        return _summary("rejected", reason="Input contains disallowed content")

    # ── Deterministic paragraph chunking ─────────────────────────────────────
    chunks = _chunk_document(text)
    if not chunks:
        return _summary("no_content", message="Document contained no text to ingest")

    truncated = False
    if len(chunks) > _DOC_MAX_CHUNKS:
        _log(
            f"ingest_document: truncating {len(chunks)} chunks to {_DOC_MAX_CHUNKS} "
            f"(ref={ref!r}) — remainder NOT processed"
        )
        chunks = chunks[:_DOC_MAX_CHUNKS]
        truncated = True

    # ── D1: extractor route (brain-not-transport) — resolved ONCE per document ──
    # Same fork as the remember_facts STATEMENT branch: the backend brain decides
    # WHICH extractor a statement goes through (SENTENCE_PIPELINE flag, backend-side).
    # Documents are statements, so they take the same route: "spine" → per-chunk
    # /harvest-spans + /ingest (deterministic, self-handles residue), with the
    # legacy /extract/rewrite path as the per-chunk fail-safe fallback; "rewrite"
    # (default / brain unreachable) → straight to /extract/rewrite per chunk.
    extractor_route = await _statement_extractor_route(user_id)

    _log(
        f"ingest_document: {len(chunks)} chunks (ref={ref!r}, chars={len(text)}, "
        f"route={extractor_route})"
    )

    # ── INSTANT STUB FACTS (migration 299 era, plan W2.2) ─────────────────────
    # Right after acceptance the tool mints deterministic stub facts (document entity +
    # title/filename alias + parser-reported headings; source="document" → staged B;
    # ZERO LLM, never from chunk text or question-shaped surfaces — the BUG-05 trap),
    # so "what is doc X / do I have anything about Y" is answerable the moment the
    # document is accepted, before any chunk extraction completes. The count rides the
    # response as ``stub_facts_minted``. Best-effort: a mint failure never fails ingest.
    stub_facts_minted = await _mint_document_stub_facts(
        user_id=user_id, title=title, source_ref=ref, chunks=chunks, document_id=None)

    sem = asyncio.Semaphore(3)

    async def _process_chunk(idx: int, chunk: str) -> dict[str, int]:
        out = {"committed": 0, "staged": 0, "context": 0, "failed": 0, "disabled": 0}
        async with sem:
            # (a) Durable episodic retention BEFORE extraction — every chunk is
            # kept verbatim even if extraction fails. Non-fatal on failure.
            try:
                await _http_client.post(
                    f"{FAULTLINE_API_URL}/episodic/append",
                    json={
                        "user_id": user_id,
                        "raw_text": chunk,
                        "source": "document",
                        "source_ref": ref or None,
                        "intent": None,
                        "extracted_fact_count": None,
                    },
                    timeout=5.0,
                )
            except Exception as exc:
                _log(f"ingest_document chunk {idx}: episodic_append_failed (non-fatal): {exc!r}")

            # (b) Deterministic spine first when the brain routed "spine" — mirrors
            # the remember_facts STATEMENT fork. A successful spine ingest is the
            # SOLE ingest for this chunk (the spine self-handles residue backend-
            # side, so the remainder capture below is rewrite-path-only). None →
            # no edges / harvest error → fall through to /extract/rewrite
            # (fail-safe, never a silent drop). An explicit error dict (spine
            # edges ingested-failed) counts the chunk failed WITHOUT re-extracting
            # (re-extraction risks a partial double-write; the episodic log has
            # the chunk verbatim for later re-mining).
            if extractor_route == "spine":
                spine_result = await _ingest_statement_via_spine(
                    chunk, user_id, source_ref=ref or None
                )
                if spine_result is not None:
                    if _is_ingest_disabled(spine_result):
                        # Backend freeze switch: nothing was stored for this chunk.
                        out["disabled"] = 1
                    elif spine_result.get("status") == "error":
                        out["failed"] = 1
                        _log(f"ingest_document chunk {idx}: spine ingest failed (non-fatal)")
                    else:
                        out["committed"] = int(spine_result.get("committed", 0) or 0)
                        out["staged"] = int(spine_result.get("staged", 0) or 0)
                    return out

            edges: list[dict] = []
            low_conf_edges: list[dict] = []
            try:
                # (c) LLM relation extraction — same endpoint/timeout as remember_facts.
                rewrite_resp = await _http_client.post(
                    f"{FAULTLINE_API_URL}/extract/rewrite",
                    json={"text": chunk, "user_id": user_id},
                    timeout=60.0,
                )
                rewrite_resp.raise_for_status()
                _chunk_rewrite = rewrite_resp.json()
                # Backend freeze switch: extraction gated backend-side — nothing
                # can be stored for this chunk; flag disabled, skip the rest.
                if _is_ingest_disabled(_chunk_rewrite):
                    out["disabled"] = 1
                    return out
                all_edges = _chunk_rewrite.get("edges", [])
                # (d) Low-confidence edges stay out of /ingest (protects the WGM
                # gate) but are demoted to the Class C context lane below.
                edges = [e for e in all_edges if not e.get("low_confidence", False)]
                low_conf_edges = [e for e in all_edges if e.get("low_confidence", False)]

                # (e) Ingest through the WGM gate. source="mcp" so provenance
                # routes to user_stated — the user explicitly submitted this
                # document, same as a remember_facts submission. source_ref
                # threads the citation into facts/staged_facts (migration 128).
                if edges:
                    ingest_resp = await _http_client.post(
                        f"{FAULTLINE_API_URL}/ingest",
                        json={
                            "text": chunk,
                            "user_id": user_id,
                            "edges": edges,
                            "source": "mcp",
                            "source_ref": ref or None,
                        },
                        timeout=30.0,
                    )
                    ingest_resp.raise_for_status()
                    ingest_result = ingest_resp.json()
                    # Backend freeze switch: nothing was stored for this chunk.
                    if _is_ingest_disabled(ingest_result):
                        out["disabled"] = 1
                        return out
                    out["committed"] = int(ingest_result.get("committed", 0) or 0)
                    out["staged"] = int(ingest_result.get("staged", 0) or 0)
            except Exception as exc:
                # Per-chunk failure must never abort the document — the chunk is
                # already retained in the episodic log for later re-mining.
                out["failed"] = 1
                _log(f"ingest_document chunk {idx}: extract/ingest failed (non-fatal): {exc!r}")
                return out

            # (f) Extraction remainder + demoted low-confidence edges → Class C
            # fuzzy lane (same pattern as remember_facts' rewrite path). With zero
            # edges the residual is the whole chunk, so nothing extractable is
            # dropped. Non-fatal: the structured ingest above already succeeded.
            if SHORT_TERM_MEMORY:
                try:
                    residual_sentences = _extraction_residual_sentences(chunk, edges)
                    residual_text = " ".join(residual_sentences)
                    if len(residual_text.split()) < 4:
                        # Trivially small residual is noise — episodic log has it verbatim.
                        residual_text = ""

                    demoted_lines: list[str] = []
                    for e in low_conf_edges:
                        subj = str(e.get("subject", "")).strip()
                        rel = str(e.get("rel_type", "")).strip().replace("_", " ")
                        obj = str(e.get("object", "")).strip()
                        if subj and rel and obj:
                            demoted_lines.append(f"{subj} {rel} {obj}.")

                    supplement_parts = ([residual_text] if residual_text else []) + demoted_lines
                    if supplement_parts:
                        context_result = await store_context_tool(text=" ".join(supplement_parts), user_id=user_id)
                        if _is_ingest_disabled(context_result):
                            # Backend freeze switch (chunk had no gate-worthy edges,
                            # so /ingest never ran) — flag disabled, count nothing.
                            out["disabled"] = 1
                        else:
                            out["context"] = 1
                except Exception as exc:
                    _log(f"ingest_document chunk {idx}: remainder_capture_failed (non-fatal): {exc!r}")
        return out

    results = await asyncio.gather(*(_process_chunk(i, c) for i, c in enumerate(chunks)))

    # Backend freeze switch: if any chunk hit the disabled backend, the whole
    # document was not stored — say so clearly instead of reporting zero counts.
    if any(r.get("disabled") for r in results):
        _log("ingest_document: backend ingest disabled (knowledge-store mode)")
        return _summary(_INGEST_DISABLED_STATUS, message=_INGEST_DISABLED_MESSAGE)

    chunks_failed = sum(r["failed"] for r in results)
    if chunks_failed == 0:
        status = "ok"
    elif chunks_failed >= len(chunks):
        status = "failed"
    else:
        status = "partial"

    summary = _summary(
        status,
        chunks=len(chunks),
        chunks_failed=chunks_failed,
        facts_committed=sum(r["committed"] for r in results),
        facts_staged=sum(r["staged"] for r in results),
        context_stored=sum(r["context"] for r in results),
        truncated=truncated,
    )
    _log(
        f"ingest_document done: status={status} chunks={summary['chunks']} "
        f"failed={chunks_failed} committed={summary['facts_committed']} "
        f"staged={summary['facts_staged']} context={summary['context_stored']}"
    )
    return summary


def _parse_ontological_statements(text: str) -> list[dict]:
    """Parse 'X (Type) is a subclass/instance/part of Y (Type)' statements into edges.

    Bypasses /extract/rewrite — LLM-generated structured statements are already
    in the correct form and don't need LLM re-extraction. Handles singular/plural
    and 'a/an' variants. Captures optional (Type) annotations for entity typing.
    """
    import re as _re

    _VALID_TYPES = {"person", "animal", "organization", "location", "object", "concept"}
    _TYPE_RE = _re.compile(r'^(.+?)\s*(?:\((\w+)\))?\s*$')

    def _extract_name_type(raw: str) -> tuple:
        m = _TYPE_RE.match(raw.strip())
        if not m:
            return raw.strip().lower(), None
        name = m.group(1).strip().lower()
        etype = m.group(2)
        if etype and etype.lower() in _VALID_TYPES:
            return name, etype.title()
        return name, None

    patterns = [
        (_re.compile(r'^(.+?)\s+(?:is|are)\s+(?:a\s+|an\s+)?subclass(?:es)?\s+of\s+(.+)$', _re.I), 'subclass_of'),
        (_re.compile(r'^(.+?)\s+(?:is|are)\s+(?:a\s+|an\s+)?instance(?:s)?\s+of\s+(.+)$', _re.I), 'instance_of'),
        (_re.compile(r'^(.+?)\s+(?:is|are)\s+(?:a\s+)?part(?:s)?\s+of\s+(.+)$', _re.I), 'part_of'),
    ]
    edges = []
    for line in text.strip().splitlines():
        line = line.strip().rstrip('.')
        if not line:
            continue
        for pattern, rel_type in patterns:
            m = pattern.match(line)
            if m:
                subj, subj_type = _extract_name_type(m.group(1).strip())
                obj, obj_type = _extract_name_type(m.group(2).strip())
                if subj and obj:
                    edge = {"subject": subj, "rel_type": rel_type, "object": obj}
                    if subj_type:
                        edge["subject_type"] = subj_type
                    if obj_type:
                        edge["object_type"] = obj_type
                    edges.append(edge)
                break
    return edges


async def learn_facts_tool(text: str, user_id: str) -> dict[str, Any]:
    """Parse LLM-generated ontological statements and ingest as source=llm_learn.

    Parses 'X is a subclass of Y', 'X is an instance of Y', 'X is a part of Y'
    directly into edges — no LLM re-extraction needed for already-structured input.
    """
    intercepted = await _maybe_intercept_slash(text, user_id)
    if intercepted is not None:
        return intercepted

    edges = _parse_ontological_statements(text)
    if not edges:
        return {"status": "no_facts", "message": "No ontological statements parsed — use forms: 'X is a subclass of Y', 'X is an instance of Y', 'X is a part of Y'"}
    ingest_resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/ingest",
        json={"text": text, "user_id": user_id, "edges": edges, "source": "llm_learn"},
    )
    ingest_resp.raise_for_status()
    data = ingest_resp.json()
    # Backend freeze switch: surface a clear message instead of "Learned 0 facts".
    if _is_ingest_disabled(data):
        _log("learn_facts: backend ingest disabled (knowledge-store mode)")
        return {"status": _INGEST_DISABLED_STATUS, "message": _INGEST_DISABLED_MESSAGE}
    committed = data.get("committed", 0)
    staged = data.get("staged", 0)
    return {
        "status": "learned",
        "committed": committed,
        "staged": staged,
        "total": committed + staged,
        "message": f"Learned {committed + staged} facts (llm_learn — {committed} committed, {staged} staged)",
    }


async def retract_fact_tool(
    text: str, user_id: str, *, classified_intent: str | None = None
) -> dict[str, Any]:
    """Call FaultLine /retract/correct endpoint with GLiNER2 intent classification.

    When called directly by the LLM (classified_intent is None), runs the same
    _classify_and_gate pipeline used by remember_facts_tool()/recall_memory_tool()
    (single source of truth), with a RETRACT-SPECIFIC fail-safe: on classify failure
    the default is RETRACTION (the model explicitly chose this tool), not the STATEMENT
    default _classify_and_gate uses for remember/recall. STATEMENT/QUERY redirect to
    remember_facts (data preservation); RETRACTION/CORRECTION proceed to /retract/correct
    (CORRECTION is a non-destructive supersede and is never downgraded to RETRACTION).
    When called from remember_facts_tool()/recall_memory_tool() (classified_intent
    provided), skips classification to avoid double-classifying.
    """
    intent = classified_intent

    if intent is None:
        # ── Intent classification (shared brain, retract-specific fail-safe) ──────
        # Uses the SAME _classify_and_gate helper as remember_facts_tool and
        # recall_memory_tool (single source of truth for the route decision — eliminates
        # the prior inline /classify-intent + /confidence-gate drift).
        # RETRACT-SPECIFIC FAIL-SAFE: _classify_and_gate RAISES on classify failure (its
        # own default is STATEMENT — the safe ingest path for remember/recall). The model
        # EXPLICITLY chose retract_fact here, so on classify failure we default to
        # RETRACTION (the user's tool choice is the strongest signal a delete was intended),
        # NOT STATEMENT. This is the one reason this block catches and overrides instead of
        # just deferring to the helper's default.
        try:
            intent, confidence, gate = await _classify_and_gate(text, user_id)
        except Exception as exc:
            _log(f"retract_fact intent_classify_fallback: {exc!r} — defaulting to RETRACTION")
            intent, confidence, gate = "RETRACTION", 0.0, 0.70

        _log(f"retract_fact intent_classified: intent={intent} confidence={confidence:.3f} gate={gate:.3f}")

        # STATEMENT/QUERY → redirect to ingest (fail toward DATA PRESERVATION, not deletion).
        # A model that calls retract_fact with a STATEMENT or QUERY made a tool-selection
        # error; the old behavior forced RETRACTION here, which destroyed data the user
        # meant to store (measured: "my name is alice and I am 42" archived real facts).
        # Hand off to remember_facts instead. No redirect loop: when remember_facts_tool
        # re-routes to retract_fact_tool it passes classified_intent=..., which skips this
        # whole block (the `if intent is None` guard above). Only the direct, model-invoked
        # path (classified_intent=None) can reach this redirect.
        if intent in ("STATEMENT", "QUERY"):
            _log(f"retract_fact: model mis-pick ({intent}) — redirecting to remember_facts to preserve data")
            return await remember_facts_tool(text, user_id)
        # RETRACTION and CORRECTION proceed to /retract/correct.
        # NOTE (data preservation wins): a low-confidence CORRECTION is NOT downgraded to
        # RETRACTION. RETRACTION deletes data; CORRECTION is a NON-DESTRUCTIVE supersede
        # (the brain said "supersede, don't delete"). The old
        # `if confidence < gate: intent = "RETRACTION"` line forced a low-confidence
        # CORRECTION into a destructive delete — the same data-loss class the STATEMENT
        # redirect above fixes. It has been removed. A low-confidence RETRACTION stays
        # RETRACTION (the model explicitly asked to forget). `confidence`/`gate` are now
        # diagnostic-only here (consistent with remember_facts_tool/recall_memory_tool —
        # the gate does not drive routing; the brain's intent does).

    # Use a dedicated 90s timeout: /retract/correct invokes LLM extraction which takes 14–55s
    # under load. The shared _http_client is 30s which is insufficient.
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            f"{FAULTLINE_API_URL}/retract/correct",
            json={"text": text, "user_id": user_id, "intent": intent},
        )
    resp.raise_for_status()
    data = resp.json()
    # Backend freeze switch: retractions/corrections are also paused in
    # knowledge-store mode — surface it clearly instead of pretending success.
    if _is_ingest_disabled(data):
        _log("retract_fact: backend ingest disabled (knowledge-store mode)")
        return {
            "status": _INGEST_DISABLED_STATUS,
            "message": "Memory ingest is currently disabled (knowledge-store mode). "
                       "Retractions and corrections are paused; no changes were made.",
        }
    return data


async def forget_fact_tool(
    user_id: str,
    subject: str,
    rel_type: str | None = None,
    old_value: str | None = None,
) -> dict[str, Any]:
    """Call FaultLine /forget endpoint — bounded, reversible tombstone of ONE named fact.

    Mirrors retract_tool, but routes to the dedicated /forget endpoint which FORCES
    mode='hard_delete' (a recoverable tombstone, reversible via /unforget). This is the
    ONLY trigger for the tombstone, for an EXPLICIT "forget this specific fact about me"
    on a NAMED target — never a broad/bulk wipe.

    BOUNDED TARGET ONLY: requires a specific resolved (subject, rel_type[, old_value])
    target. There is no wildcard / "forget everything" capability; a missing subject is a
    no-op on the backend, never a broadening delete.
    """
    body: dict[str, Any] = {"user_id": user_id, "subject": subject}
    if rel_type:
        body["rel_type"] = rel_type
    if old_value:
        body["old_value"] = old_value
    resp = await _post(f"{FAULTLINE_API_URL}/forget", json=body)
    resp.raise_for_status()
    data = resp.json()
    # Backend freeze switch: forget (tombstone) is paused in knowledge-store mode.
    if _is_ingest_disabled(data):
        _log("forget_fact: backend ingest disabled (knowledge-store mode)")
        return {
            "status": _INGEST_DISABLED_STATUS,
            "message": "Memory ingest is currently disabled (knowledge-store mode). "
                       "Forget is paused; no changes were made.",
        }
    return data


# ── Tool dispatch ────────────────────────────────────────────────────────────

TOOL_DISPATCH: dict[str, callable] = {
    "recall_memory": recall_memory_tool,
    "remember_facts": remember_facts_tool,
    "ingest_document": ingest_document_tool,
    "learn_facts": learn_facts_tool,
    "retract_fact": retract_fact_tool,
    "forget_fact": forget_fact_tool,
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

    # SECURITY (Phase 0, RP-2 §0a): validate the EFFECTIVE user_id regardless of
    # the FAULTLINE_USER_ID pin. Caller-supplied identity wins; the pin is consulted
    # only as a single-user fallback (matches _call_tool / bind_tenant precedence).
    # With no identity at all this becomes the front-line empty-user_id rejection so
    # a tool never proceeds with no resolvable identity.
    # STRIP BEFORE the pin fallback, matching bind_tenant (server.py:154). Without the strip a
    # whitespace-only user_id ("   ") is TRUTHY, so it never reaches the pin and this validator
    # REJECTS an identity that bind_tenant would happily resolve — the two identity seams
    # disagreed. Unreachable over HTTP (both transports run bind_tenant first, so the argument is
    # already normalized), but live on the direct/stdio path. A defense-in-depth validator that
    # contradicts the seam it backstops is worse than no validator: it makes the guarantee a lie.
    effective_user_id = (user_id or "").strip() or FAULTLINE_USER_ID
    err = validate_user_id(effective_user_id)
    if err:
        return {"error": f"Invalid user_id: {err}"}

    if tool_name == "recall_memory":
        err = validate_query(arguments.get("query", ""))
        if err:
            return {"error": f"Invalid query: {err}"}

    elif tool_name in ("remember_facts", "learn_facts", "retract_fact", "ingest_document"):
        err = validate_text(arguments.get("text", ""))
        if err:
            return {"error": f"Invalid text: {err}"}

    elif tool_name in ("extract", "query", "store_context") and "text" in arguments:
        err = validate_text(arguments["text"])
        if err:
            return {"error": f"Invalid text: {err}"}

    if tool_name == "ingest":
        err = WGMValidationGate.validate_edge_inputs(arguments.get("edges", []))
        if err:
            return {"error": f"Invalid edges: {err}"}

    if tool_name in ("retract", "forget_fact"):
        # BOUNDED TARGET: a forget MUST name exactly one subject — no wildcard / bulk wipe.
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


def _send_progress(
    progress_token: str | int | None,
    progress: float,
    total: float | None = None,
    message: str | None = None,
) -> None:
    """Send a notifications/progress notification. No-op if progress_token is None."""
    if progress_token is None:
        return
    params: dict = {"progressToken": progress_token, "progress": progress}
    if total is not None:
        params["total"] = total
    if message is not None:
        params["message"] = message
    _send({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})


async def _call_tool(tool_name: str, arguments: dict, progress_token: str | int | None = None) -> dict:
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

    # Resolve effective user_id: CALLER-SUPPLIED identity WINS; FAULTLINE_USER_ID is
    # consulted ONLY as a single-user/dev fallback when the caller supplies nothing.
    # (Previously the pin unconditionally overrode the caller, collapsing every tenant
    # onto one schema — DEV/SECURITY-multiuser-tenant-isolation.md F1a.)
    # Both transports already resolve identity via bind_tenant() before dispatch, so
    # arguments["user_id"] is authoritative here; this fallback covers any direct/stdio
    # caller that bypassed the HTTP transports.
    effective_user_id = arguments.get("user_id", "") or FAULTLINE_USER_ID
    arguments = {**arguments, "user_id": effective_user_id}

    # Rotation key: stable per tool_name, varied across tools
    _rot = abs(hash(tool_name)) % 4

    # Step 1: before provisioning check
    _send_progress(progress_token, 1, 3, _rotate(_MCP_PROGRESS_STEP1, _rot))

    # Provisioning gate — must be ready before any tool executes.
    provisioned = await _ensure_provisioned(effective_user_id)
    if not provisioned:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "status": "provisioning",
                            "message": "Memory is being set up for you — please retry in a moment.",
                            "committed": 0,
                        }
                    ),
                }
            ]
        }

    # Step 2: provisioning done, about to run tool
    _send_progress(progress_token, 2, 3, _rotate(_MCP_PROGRESS_STEP2, _rot))

    try:
        result = await handler(**arguments)
        # Step 3: work complete, send before assembling the final response
        _send_progress(progress_token, 3, 3, _rotate(_MCP_PROGRESS_DONE, _rot))
        # MCP spec: a tool error the MODEL can self-correct from returns ``isError: true`` at the
        # top level of CallToolResult (alongside ``content``), NOT a JSON-RPC error. The handler
        # signals it by returning a dict containing ``"isError": True`` (e.g. ``no_ingest``); this
        # seam lifts that flag out of the content payload and onto the envelope so a spec-aware
        # client can act on it. The content text is unchanged — models that read the result as
        # text are unaffected.
        envelope: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps(result)}],
        }
        # MCP spec (2025-06-18): a tool that declares ``outputSchema`` is OBLIGATED to
        # return ``structuredContent`` — spec-strict clients (opencode/Claude Desktop/Cline)
        # reject a text-only result with -32600 "did not return structured content". Emit the
        # handler's dict as structuredContent whenever this tool declared an output schema; the
        # text ``content`` above stays for back-compat with text-only clients.
        if tool_name in _OUTPUT_SCHEMAS:
            envelope["structuredContent"] = result
        if isinstance(result, dict) and result.get("isError") is True:
            envelope["isError"] = True
        return envelope
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
                        "capabilities": {"tools": {}, "prompts": {}},
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
                    progress_token = params.get("_meta", {}).get("progressToken")
                    _log(f"Tool call: {tool_name} (user_id={arguments.get('user_id', '?')[:8]}...)")
                    result = await _call_tool(tool_name, arguments, progress_token=progress_token)
                    _send({"jsonrpc": "2.0", "id": req_id, "result": result})

            elif method == "prompts/list":
                if not _initialized:
                    _send({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32002, "message": "Server not initialized"},
                    })
                    continue
                from .prompts import PROMPTS
                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "prompts": [
                            {
                                "name": p["name"],
                                "description": p.get("description", ""),
                                "arguments": p.get("arguments", []),
                            }
                            for p in PROMPTS
                        ]
                    },
                })

            elif method == "prompts/get":
                if not _initialized:
                    _send({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32002, "message": "Server not initialized"},
                    })
                    continue
                from .prompts import PROMPTS
                params = request.get("params", {})
                prompt_name = params.get("name", "")
                prompt_args = params.get("arguments", {})

                prompt_def = next((p for p in PROMPTS if p["name"] == prompt_name), None)
                if prompt_def is None:
                    _send({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": f"Prompt not found: {prompt_name}"},
                    })
                    continue

                # Call the prompt function with any provided arguments
                try:
                    fn = prompt_def["fn"]
                    import inspect
                    sig = inspect.signature(fn)
                    if sig.parameters:
                        text = fn(**{k: v for k, v in prompt_args.items() if k in sig.parameters})
                    else:
                        text = fn()
                except Exception as e:
                    _send({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32603, "message": f"Prompt execution failed: {e}"},
                    })
                    continue

                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "description": prompt_def.get("description", ""),
                        "messages": [
                            {"role": "user", "content": {"type": "text", "text": text}}
                        ],
                    },
                })

            else:
                _log(f"Unknown method: {method}")
                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })
    finally:
        await _http_client.aclose()