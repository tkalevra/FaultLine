"""MCP protocol premise — version constants, error codes, and the discover builder.

Minimal protocol-level port of the 2026-07-28 stateless-spec module (commit ec070b11). This
file carries ONLY the protocol pieces shared by the HTTP and stdio transports: supported
protocol versions, the 2026-07-28 error-code allocations, the CacheableResult hints, and the
``server/discover`` payload builder. The model-facing instructions premise text that rode
alongside these in the upstream module is not part of the public lineage and is deliberately
not ported here — keep this file free of any transport-independent marketing/premise text.
"""

# The 2026-07-28 revision is the STATELESS MCP: no initialize/notifications/initialized
# handshake, no Mcp-Session-Id, every request self-contained with version+capabilities in
# `_meta`, plus the new `server/discover` RPC. FaultLine is DUAL-ERA: legacy clients
# (2025-11-25 and earlier) still send initialize; modern clients (2026-07-28) send per-request
# `_meta`. Both are served.
#
# 2025-11-25 and 2024-10-07 are the revisions proposed by the official MCP client SDKs. The
# 2026-08-10 incident: a stock client SDK proposed 2025-11-25, we were not listing it, and
# `negotiate_protocol_version` echoed LATEST (2026-07-28) instead — a stateless revision the
# client's whitelist has never seen, so initialize failed and the client surfaced the breakage
# as a misleading transport error. These entries close that class of failure: the SDK's own
# handshake is served verbatim, and the echoed value is one the client re-sends in the
# `mcp-protocol-version` header on every subsequent request.
SUPPORTED_PROTOCOL_VERSIONS = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
)
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# 2026-07-28 error-code allocation (SEP-2575): -32020..-32099 is reserved for the MCP
# specification. Named constants so both transports agree without literals.
ERROR_HEADER_MISMATCH = -32020
ERROR_MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
ERROR_UNSUPPORTED_PROTOCOL_VERSION = -32022


def negotiate_protocol_version(requested: str | None) -> str:
    """Echo `requested` if we support it; else offer the newest revision we support that is
    NOT newer than what the client proposed. Never raises.

    The "not newer than requested" cap is the 2026-08-10 fix: our all-caps LATEST
    (2026-07-28) is a stateless revision that stock client SDKs have never seen. If a client
    proposes something we do not list, echoing LATEST hands back a revision the client's own
    whitelist rejects and the handshake dies. All revisions are ISO date strings, so
    lexicographic comparison is chronological.
    """
    if requested:
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            return requested
        for version in SUPPORTED_PROTOCOL_VERSIONS:  # newest first
            if version <= requested:                  # newest one the client can parse
                return version
        if requested < SUPPORTED_PROTOCOL_VERSIONS[-1]:
            return SUPPORTED_PROTOCOL_VERSIONS[-1]    # older than we serve: oldest we know
    return LATEST_PROTOCOL_VERSION


# Cacheability hint (2026-07-28 CacheableResult, SEP-2549): tools/list and server/discover are
# stable per-deployment, so a long TTL with "public" scope lets clients and intermediaries cache
# the catalog. Conservative — a value-flip just means the client holds a stale list for the TTL.
_LIST_TTL_MS = 300_000
_LIST_CACHE_SCOPE = "public"


def discover_result() -> dict:
    """The 2026-07-28 ``server/discover`` payload — one source, both transports.

    Advertises the supported protocol versions, capabilities, and server identity. NOTE: this
    build carries NO ``instructions`` field — the model-facing premise text is not part of the
    public lineage and is not ported here.
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
]
