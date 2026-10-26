"""MCP protocol premise — version constants, error codes, and the discover builder.

Minimal protocol-level port of the 2026-07-28 stateless-spec module (commit ec070b11). This
file carries ONLY the protocol pieces shared by the HTTP and stdio transports: supported
protocol versions, the 2026-07-28 error-code allocations, the CacheableResult hints, and the
``server/discover`` payload builder. The model-facing instructions premise text that rode
alongside these in the upstream module is not part of the public lineage and is deliberately
not ported here — keep this file free of any transport-independent marketing/premise text.

One narrow exception (2026-10-26, shadow-memory routing fix): ``SERVER_INSTRUCTIONS`` below.
It is NOT the upstream premise — no behavioral contract, no marketing — only the MCP-spec
``initialize.instructions`` routing notice that tells the model FaultLine's tools are the
user's memory and the host's built-in note tools are not. Without it the native-MCP door
advertises nothing at server level, and models route memory to ephemeral host notes
(shadow-memory user report, 2026-10-24).
"""

# The 2026-07-28 revision is the STATELESS MCP: no initialize/notifications/initialized
# handshake, no Mcp-Session-Id, every request self-contained with version+capabilities in
# `_meta`, plus the new `server/discover` RPC. FaultLine is DUAL-ERA: legacy clients
# (2025-11-25 and earlier) still send initialize; modern clients (2026-07-28) send per-request
# `_meta`. Both are served.
SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# 2026-07-28 error-code allocation (SEP-2575): -32020..-32099 is reserved for the MCP
# specification. Named constants so both transports agree without literals.
ERROR_HEADER_MISMATCH = -32020
ERROR_MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
ERROR_UNSUPPORTED_PROTOCOL_VERSION = -32022


def negotiate_protocol_version(requested: str | None) -> str:
    """Echo `requested` if we support it, else offer our latest. Never raises."""
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


# Cacheability hint (2026-07-28 CacheableResult, SEP-2549): tools/list and server/discover are
# stable per-deployment, so a long TTL with "public" scope lets clients and intermediaries cache
# the catalog. Conservative — a value-flip just means the client holds a stale list for the TTL.
_LIST_TTL_MS = 300_000
_LIST_CACHE_SCOPE = "public"


# MCP-spec ``initialize.instructions`` (the only server-level guidance most clients surface in
# the system prompt — OpenWebUI does on native MCP connections since v0.6.31). Routing notice
# ONLY: names the memory tools, names the host built-ins it must NOT route memory to. The tool
# descriptions carry the same differentiation for clients that never read instructions.
SERVER_INSTRUCTIONS = (
    "FaultLine is this user's persistent memory. Save what they tell you with remember_facts; "
    "look it up with recall_memory. Never route memory to the host's built-in note tools "
    "(write_note, search_notes, view_note) — where a host offers them, they are temporary "
    "notes, not FaultLine memory."
)


def discover_result() -> dict:
    """The 2026-07-28 ``server/discover`` payload — one source, both transports.

    Advertises the supported protocol versions, capabilities, and server identity. NOTE: this
    build carries NO ``instructions`` field on ``server/discover`` — the upstream premise is not
    part of the public lineage. The narrow routing notice that IS public lives in
    ``SERVER_INSTRUCTIONS`` and rides ``initialize`` only (see module docstring).
    """
    return {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {}, "prompts": {}},
        "_meta": {"io.modelcontextprotocol/serverInfo": {
            "name": "faultline-mcp", "version": "1.0.0"}},
        "ttlMs": _LIST_TTL_MS,
        "cacheScope": _LIST_CACHE_SCOPE,
    }


__all__ = [
    "SUPPORTED_PROTOCOL_VERSIONS", "LATEST_PROTOCOL_VERSION",
    "negotiate_protocol_version", "discover_result",
    "ERROR_HEADER_MISMATCH", "ERROR_MISSING_REQUIRED_CLIENT_CAPABILITY",
    "ERROR_UNSUPPORTED_PROTOCOL_VERSION", "_LIST_TTL_MS", "_LIST_CACHE_SCOPE",
    "SERVER_INSTRUCTIONS",
]
