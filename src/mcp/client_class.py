"""CLIENT IDENTITY — a LABEL, never a gate (owner ruling 2026-08-15).

2026-08-14: this module carried a hard-coded list of "coding-agent" client names
and closed the automatic write lanes for them, after a build agent polluted the
user's memory through recall's turn harvest and the store_context tool.

2026-08-15 — OWNER RULING, RECTIFIED: capability must NEVER vary by client name.
An authorized bearer gets the full toolset identically whatever it calls itself;
the MCP capability is gated by AUTH (bearer token), and by nothing else.
A client name is only ever a label — relevant for write-path
traceability (``client=<name|chat>`` log lines), never for admission. The
name list and every classification predicate are deleted; if a client-name
distinction is ever wanted again, it must be built on configuration, never on a
hard-coded enumeration of names (the same subject-agnostic rule the engine
applies everywhere else).

This module is deliberately PURE: no imports from server.py / http_server.py, so
both transports (and tests) can depend on it without cycles. The ContextVar is
set ONCE per request at the transport edge (mcp-name / User-Agent / clientInfo)
and read for labeling only; unset or unknown names read as None, which changes
nothing — a label can never break or widen a request.
"""

from __future__ import annotations

import contextvars

# Per-request client identity. None = unset/unknown. A ContextVar (not a module
# global) so concurrent async requests on one process cannot read each other's
# client: each request task inherits a copy of the context at its edge, the
# transport sets it there, and every await below sees this request's client and
# only this request's. This is a LABEL: no code may branch capability on it.
_client_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "faultline_client_name", default=None
)


def set_client_class(name: str | None) -> None:
    """Record the client identity (label) for the CURRENT request context.

    Called once at the transport edge (HTTP: mcp-name header → User-Agent leading
    token; stdio: initialize clientInfo.name, once for the process lifetime).
    Never raises on None/unknown — an unidentifiable client is simply unlabeled.
    """
    _client_name.set((name or "").strip() or None)


def current_client_name() -> str | None:
    """Raw normalized client name of the current context (None when unset).

    Used for traceability on write-path log lines (``client=<name or 'chat'>``).
    NEVER consult this for admission — the MCP
    capability is gated by auth alone (owner ruling 2026-08-15).
    """
    return _client_name.get()
