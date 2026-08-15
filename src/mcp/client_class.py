"""CLIENT CLASSIFICATION — who is operating this client? (2026-08-14 incident)

FaultLine's user memory is a record of what the HUMAN said. On 2026-08-14 a build
agent (a coding assistant speaking MCP on a user's credentials) polluted a real
user's seat through the AUTOMATIC write lanes: recall_memory's per-turn harvest
ingested the agent's query text as ``source="mcp"`` → ``user_stated``, and the
unadvertised-but-dispatchable ``store_context`` tool landed the agent's own brief
sentences in episodic_log (``source="store_context_deferred"``) where they were
re-mined into durable facts. The text was agent-authored by construction, yet it
entered the graph indistinguishable from the user's own words.

The discriminator for AUTO-writes is therefore WHO IS OPERATING THE CLIENT, never
what the text looks like (no "looks technical" content sniffing — client identity
is who-is-speaking, not what-the-text-resembles). A coding agent's automatic
captures are its own working text; a chat frontend's automatic captures are human
turns. For CODING-AGENT clients the server closes the automatic write lanes
(recall's turn harvest, the store_context tool) while leaving the EXPLICIT
``remember_facts`` of a human turn open in every class — a human talking THROUGH a
coding agent must still be remembered.

This module is deliberately PURE: no imports from server.py / http_server.py, so
both transports (and tests) can depend on it without cycles. The ContextVar is
set ONCE per request at the transport edge (mcp-name / User-Agent / clientInfo)
and read deep inside tool handlers; unset or unknown names read as False, which
fails toward today's behavior (chat default) — classification can widen the gate
but never break a request.
"""

from __future__ import annotations

import contextvars

# Known coding-agent / build-assistant client names, lowercase. Matched after
# strip+lower normalization. A name NOT in this set is treated as a chat
# frontend (OpenWebUI, a browser, curl, …) → not a coding agent → auto-writes
# stay open, exactly as before this module existed.
CODING_AGENT_CLIENTS: frozenset[str] = frozenset({
    "opencode", "claude-code", "claude code", "claude cli", "codex", "cursor",
    "cursor-agent", "windsurf", "cline", "roo-code", "roo code", "gemini-cli",
    "gemini cli", "aider", "amp", "zed", "continue", "kilo code", "copilot",
    "github copilot", "goose", "crush", "symphony",
})

# Per-request client identity. None = unset/unknown → NOT a coding agent (chat
# default). A ContextVar (not a module global) so concurrent async requests on
# one process cannot read each other's client: each request task inherits a
# copy of the context at its edge, the transport sets it there, and every
# await below — no matter how deep in the tool handlers — sees this request's
# client and only this request's.
_client_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "faultline_client_name", default=None
)


def is_coding_agent(name: str | None) -> bool:
    """True iff `name` (normalized: strip + lower) is a known coding-agent client.

    Unknown, empty, or None → False (fail toward today's behavior = chat default,
    where the automatic write lanes stay open). Pure function, no state.
    """
    if not name:
        return False
    return name.strip().lower() in CODING_AGENT_CLIENTS


def set_client_class(name: str | None) -> None:
    """Record the client identity for the CURRENT request context.

    Called once at the transport edge (HTTP: mcp-name header → User-Agent leading
    token; stdio: initialize clientInfo.name, once for the process lifetime).
    Never raises on None/unknown — an unidentifiable client simply reads as chat.
    """
    _client_name.set((name or "").strip() or None)


def current_client_is_coding_agent() -> bool:
    """The gate read, deep in a tool handler: is the CURRENT speaker a coding agent?

    Unset/unknown → False. This is the ONLY predicate the auto-write gates
    consult — never the text's content.
    """
    return is_coding_agent(_client_name.get())


def current_client_name() -> str | None:
    """Raw normalized client name of the current context (None when unset).

    Used for traceability on write-path log lines (``client=<name or 'chat'>``).
    """
    return _client_name.get()
