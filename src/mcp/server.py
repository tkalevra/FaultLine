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
    validate_edges,
    validate_query,
    validate_text,
    validate_user_id,
)

FAULTLINE_API_URL = os.environ.get("FAULTLINE_API_URL", "http://localhost:8000").rstrip("/")
FAULTLINE_USER_ID = os.environ.get("FAULTLINE_USER_ID", "").strip()

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


async def recall_memory_tool(query: str, user_id: str) -> dict[str, Any]:
    """Call FaultLine /query endpoint and return human-readable prose.

    If query starts with /learn, generate and ingest an ontological hierarchy
    for the topic as llm_learn facts — no LLM function calling required.
    """
    _expand_full_re = _re.compile(
        r'^/expand\s+(?P<topic>.+?)(?:\s+online(?:\s+(?P<url>https?://\S+))?)?\s*$',
        _re.I,
    )
    m = _expand_full_re.match(query.strip())
    if m:
        topic = m.group("topic").strip()
        url = m.group("url")  # may be None
        online = "online" in query.lower()
        return await _learn_via_llm(topic, user_id, source_url=url, online=online)

    resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/query",
        json={"text": query, "user_id": user_id},
    )
    resp.raise_for_status()
    data = resp.json()

    facts = data.get("facts", [])
    preferred_names: dict = data.get("preferred_names", {})
    attributes: dict = data.get("attributes", {})
    canonical_identity: str = data.get("canonical_identity", "")

    if not facts and not attributes:
        return {"memory": "No relevant facts found."}

    # Build a slug→name map; always resolve the querying user's identity to "you"
    slug = canonical_identity.replace("-", "_")
    display: dict[str, str] = {}
    for uid, name in preferred_names.items():
        uid_slug = uid.replace("-", "_")
        if uid == canonical_identity or uid_slug == slug or uid == "user":
            display[uid] = "you"
            display[uid_slug] = "you"
        elif name and name != uid and name != uid_slug:
            display[uid] = name
            display[uid_slug] = name

    lines: list[str] = []

    for fact in facts:
        # Skip raw store_context cache entries — transport tag, not structured knowledge.
        # "context" is set at /store_context write time and confirmed absent from rel_types table.
        if fact.get("rel_type") == "context":
            continue
        definition = fact.get("definition", "")
        if not definition:
            continue
        text = _clean_for_mcp(definition)
        if not text:
            continue
        for token, replacement in display.items():
            text = text.replace(token, replacement)
        lines.append(text)

    for attr, value in attributes.items():
        lines.append(f"{attr}: {value}")

    return {"memory": "\n".join(lines) if lines else "No relevant facts found."}


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
        timeout=60.0,
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
        timeout=30.0,
    )
    ingest_resp.raise_for_status()
    return ingest_resp.json()


def _parse_ontological_statements(text: str) -> list[dict]:
    """Parse 'X is a subclass/instance/part of Y' statements directly into edges.

    Bypasses /extract/rewrite — LLM-generated structured statements are already
    in the correct form and don't need LLM re-extraction. Handles singular/plural
    and 'a/an' variants.
    """
    import re as _re
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
                subject = m.group(1).strip().lower()
                obj = m.group(2).strip().lower()
                if subject and obj:
                    edges.append({"subject": subject, "rel_type": rel_type, "object": obj})
                break
    return edges


async def learn_facts_tool(text: str, user_id: str) -> dict[str, Any]:
    """Parse LLM-generated ontological statements and ingest as source=llm_learn.

    Parses 'X is a subclass of Y', 'X is an instance of Y', 'X is a part of Y'
    directly into edges — no LLM re-extraction needed for already-structured input.
    """
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


async def retract_fact_tool(text: str, user_id: str) -> dict[str, Any]:
    """Call FaultLine /retract/correct endpoint."""
    resp = await _http_client.post(
        f"{FAULTLINE_API_URL}/retract/correct",
        json={"text": text, "user_id": user_id, "intent": "RETRACTION"},
    )
    resp.raise_for_status()
    return resp.json()


# ── Tool dispatch ────────────────────────────────────────────────────────────

TOOL_DISPATCH: dict[str, callable] = {
    "recall_memory": recall_memory_tool,
    "remember_facts": remember_facts_tool,
    "learn_facts": learn_facts_tool,
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

    elif tool_name in ("remember_facts", "learn_facts", "retract_fact"):
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

    # Resolve effective user_id: env override takes precedence over argument.
    effective_user_id = FAULTLINE_USER_ID if FAULTLINE_USER_ID else arguments.get("user_id", "")
    if FAULTLINE_USER_ID:
        arguments = {**arguments, "user_id": FAULTLINE_USER_ID}

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
                    progress_token = params.get("_meta", {}).get("progressToken")
                    _log(f"Tool call: {tool_name} (user_id={arguments.get('user_id', '?')[:8]}...)")
                    result = await _call_tool(tool_name, arguments, progress_token=progress_token)
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