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

        # ── Poll loop: up to 6 × 2 s = 12 s additional wait ─────────────────────
        for attempt in range(6):
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
        edges = resp.json().get("edges", []) or []
        if not edges:
            return 0
        await ingest_tool(text, user_id, edges, source="mcp")
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
        edges = resp.json().get("edges", []) or []
        if not edges:
            return 0
        await ingest_tool(text, user_id, edges, source="mcp")
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
    """
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


async def _classify_and_gate(text: str, user_id: str) -> tuple[str, float, float]:
    """Consult the DB-weighted intent BRAIN: /classify-intent + per-user confidence gate.

    Single source of truth for the route decision, shared by remember_facts_tool and
    recall_memory_tool (transport-parity: the brain lives ONCE backend-side; both transports
    consume it). /classify-intent already applies the per-user confidence gate AND the
    low-confidence LLM escalation, so callers DEFER to the returned intent — they must not
    re-derive a weaker "confidence < gate → STATEMENT" route that would clobber an escalated
    CORRECTION. The `gate` is returned only for the diagnostic log line; it does not drive routing.

    Non-fatal by design: intent defaults to STATEMENT and gate to 0.70 if either endpoint is
    unavailable. Callers decide their own fail-safe (recall_memory_tool falls back to plain recall).
    """
    intent = "STATEMENT"
    confidence = 0.0
    try:
        classify_resp = await _http_client.post(
            f"{FAULTLINE_API_URL}/classify-intent",
            params={"user_id": user_id},
            json={"text": text},
            timeout=10.0,
        )
        classify_resp.raise_for_status()
        classify_data = classify_resp.json()
        intent = classify_data.get("intent", "STATEMENT")
        confidence = float(classify_data.get("confidence", 0.0))
    except Exception as exc:
        _log(f"intent_classify_fallback: {exc!r} — defaulting to STATEMENT")
        raise

    gate = 0.70
    try:
        gate_resp = await _http_client.get(
            f"{FAULTLINE_API_URL}/confidence-gate/{user_id}",
            timeout=5.0,
        )
        gate_resp.raise_for_status()
        gate = float(gate_resp.json().get("threshold", 0.70))
    except Exception as exc:
        _log(f"confidence_gate_fallback: {exc!r} — defaulting to 0.70")

    _log(f"intent_classified: intent={intent} confidence={confidence:.3f} gate={gate:.3f}")
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


async def _ingest_statement_via_spine(text: str, user_id: str) -> dict[str, Any] | None:
    """STATEMENT ingest via the DETERMINISTIC SPINE (D1). Calls /harvest-spans (the spine: LLM
    atomize-only → spaCy deriver → GLiNER2 typing → ±6 backbone attach, NO LLM triple extraction)
    and ingests the returned edges ONCE via /ingest.

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
        edges = resp.json().get("edges", []) or []
    except Exception as exc:
        _log(f"statement_spine_harvest_failed: {exc!r} — falling back to /extract/rewrite")
        return None
    if not edges:
        # No durable edge from the spine (residue, if any, was already held in Class C inside
        # /harvest-spans). Signal fall-through so the legacy extractor gets a shot — fail-safe, not
        # a silent drop. No double-ingest: the spine ingested NOTHING here (it only returns edges).
        return None
    try:
        ingest_resp = await _http_client.post(
            f"{FAULTLINE_API_URL}/ingest",
            json={"text": text, "user_id": user_id, "edges": edges, "source": "mcp"},
            timeout=30.0,
        )
        ingest_resp.raise_for_status()
    except Exception as exc:
        # The spine produced edges but /ingest failed. Do NOT fall back to /extract/rewrite here:
        # re-extracting the same text risks a partial double-write if /ingest partially applied.
        # Fail loud with a non-drop signal — the orchestrator/operator sees the error.
        _log(f"statement_spine_ingest_failed: {exc!r}")
        return {"status": "error", "reason": "spine ingest failed", "committed": 0}
    _log(f"statement_via_spine: ingested {len(edges)} edge(s)")
    return ingest_resp.json()


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
    if RECALL_INTENT_ROUTING:
        try:
            intent, _confidence, _gate = await _classify_and_gate(query, user_id)
        except Exception:
            intent = "QUERY"  # classify unavailable → treat as a genuine recall (never break recall)
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
    await _harvest_turn_facts(query, user_id)

    resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/query",
        json={"text": query, "user_id": user_id},
    )
    resp.raise_for_status()
    data = resp.json()

    facts = data.get("facts", [])
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

    if not assert_lines and not hold_lines and not event_lines:
        return {"memory": "No relevant facts found."}

    preamble = (
        "The following is what you know from previous conversations. "
        "Treat these as things you personally remember — weave them into "
        "your response naturally, as your own knowledge. Never list them, "
        "never say 'according to my records', never quote them verbatim."
    )

    sections: list[str] = []
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

    return {"memory": f"{preamble}\n\n" + "\n\n".join(sections)}


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
        intent, confidence, gate = await _classify_and_gate(text, user_id)
    except Exception:
        intent, confidence, gate = "STATEMENT", 0.0, 0.70

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
        return {"status": "no_ingest", "message": "Text too short for fact extraction"}

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
    _raw_edges = rewrite_resp.json().get("edges", [])
    edges = [e for e in _raw_edges if not e.get("low_confidence", False)]
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
        return {"status": "no_ingest", "message": "No memorable fact detected — nothing stored."}
    ingest_resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/ingest",
        json={"text": text, "user_id": user_id, "edges": edges, "source": "mcp"},
        timeout=30.0,
    )
    ingest_resp.raise_for_status()
    return ingest_resp.json()


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
    /classify-intent + /confidence-gate pipeline used by remember_facts_tool().
    When called from remember_facts_tool() (classified_intent provided), skips
    classification to avoid double-classifying.
    """
    intent = classified_intent

    if intent is None:
        # ── Intent classification (Layer 1) ─────────────────────────────────
        # Non-fatal: default to RETRACTION since the LLM chose retract_fact.
        intent = "RETRACTION"
        confidence = 0.0
        try:
            classify_resp = await _http_client.post(
                f"{FAULTLINE_API_URL}/classify-intent",
                params={"user_id": user_id},
                json={"text": text},
                timeout=10.0,
            )
            classify_resp.raise_for_status()
            classify_data = classify_resp.json()
            intent = classify_data.get("intent", "RETRACTION")
            confidence = float(classify_data.get("confidence", 0.0))
        except Exception as exc:
            _log(f"retract_fact intent_classify_fallback: {exc!r} — defaulting to RETRACTION")

        # ── Per-user confidence gate (Layer 3) ──────────────────────────────
        # Non-fatal: default to 0.70 (same default as Filter).
        gate = 0.70
        try:
            gate_resp = await _http_client.get(
                f"{FAULTLINE_API_URL}/confidence-gate/{user_id}",
                timeout=5.0,
            )
            gate_resp.raise_for_status()
            gate = float(gate_resp.json().get("threshold", 0.70))
        except Exception as exc:
            _log(f"retract_fact confidence_gate_fallback: {exc!r} — defaulting to 0.70")

        _log(f"retract_fact intent_classified: intent={intent} confidence={confidence:.3f} gate={gate:.3f}")

        # STATEMENT or low confidence → fall back to RETRACTION (LLM chose this tool for a reason).
        # Only CORRECTION should override the tool choice.
        if intent == "STATEMENT" or confidence < gate:
            intent = "RETRACTION"

    # Use a dedicated 90s timeout: /retract/correct invokes LLM extraction which takes 14–55s
    # under load. The shared _http_client is 30s which is insufficient.
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            f"{FAULTLINE_API_URL}/retract/correct",
            json={"text": text, "user_id": user_id, "intent": intent},
        )
    resp.raise_for_status()
    return resp.json()


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
    return resp.json()


# ── Tool dispatch ────────────────────────────────────────────────────────────

TOOL_DISPATCH: dict[str, callable] = {
    "recall_memory": recall_memory_tool,
    "remember_facts": remember_facts_tool,
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
    effective_user_id = user_id or FAULTLINE_USER_ID
    err = validate_user_id(effective_user_id)
    if err:
        return {"error": f"Invalid user_id: {err}"}

    if tool_name == "recall_memory":
        err = validate_query(arguments.get("query", ""))
        if err:
            return {"error": f"Invalid query: {err}"}

    elif tool_name in ("remember_facts", "learn_facts", "retract_fact"):
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