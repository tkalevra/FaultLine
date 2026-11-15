"""Pin the 2026-07-28 stateless MCP surface on the HTTP transport.

The 2026-07-28 revision makes MCP stateless: no initialize/notifications/initialized handshake
required, no Mcp-Session-Id, every request self-contained with version + capabilities in `_meta`.
It adds `server/discover` (MUST), `resultType` on every result (MUST), CacheableResult
(`ttlMs`/`cacheScope`) on list results, `Mcp-Method`/`Mcp-Name` routing headers, and
`UnsupportedProtocolVersionError`. FaultLine is DUAL-ERA: legacy clients (2025-11-25 and earlier)
still use the initialize handshake untouched; modern clients (2026-07-28) are served statelessly.

These tests pin the ADDITIVE surface only — every legacy behaviour is unchanged, which is the
"no breaking changes" constraint made testable.
"""

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.mcp import premise
from src.mcp.http_server import app

# A well-formed tenant UUID (bind_tenant requires one even in tests).
TEST_USER_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _open_core_env(monkeypatch):
    """SaaS off + auth off for every test in this package (mirrors tests/mcp/conftest)."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    import src.mcp.http_server as h
    monkeypatch.setattr(h, "MCP_API_KEY", "")


# ── server/discover ──────────────────────────────────────────────────────────────
# 2026-07-28: servers MUST implement server/discover (SEP-2575). Lets a modern client learn
# supported protocol versions + capabilities + identity in one request, before any other RPC —
# and is the stdio backward-compatibility probe.

def test_server_discover_advertises_versions_capabilities_and_premise(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": "d1",
                                  "method": "server/discover", "params": {}})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["resultType"] == "complete"
    # supportedVersions includes the modern revision FIRST.
    assert result["supportedVersions"][0] == "2026-07-28"
    assert "2025-06-18" in result["supportedVersions"]  # legacy still supported
    assert "tools" in result["capabilities"]
    # Identity + the premise (transport parity: same FaultLine as initialize/OpenAPI).
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "faultline-mcp"
    assert result["instructions"] == premise.SERVER_INSTRUCTIONS
    # CacheableResult hints (SEP-2549).
    assert result["ttlMs"] > 0
    assert result["cacheScope"] in ("public", "private")


def test_server_discover_works_before_any_initialize(client):
    """Stateless: discover is served WITHOUT a prior initialize handshake (a modern client
    sends it first thing; gating it behind the legacy handshake would break the probe)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "server/discover"})
    assert r.status_code == 200
    assert r.json()["result"]["resultType"] == "complete"


# ── resultType on every result ───────────────────────────────────────────────────
# SEP-2322: all results carry a required resultType; clients MUST treat missing as "complete",
# so adding it is non-breaking AND makes responses predictably parsable.

def test_tools_list_result_carries_resultType_and_cache_hints(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                  "method": "tools/list", "params": {}})
    result = r.json()["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] > 0
    assert result["cacheScope"] in ("public", "private")
    # Server identity on the envelope (SHOULD, SEP-2575).
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "faultline-mcp"


def test_tools_call_result_carries_resultType(client):
    expected = {"content": [{"type": "text", "text": json.dumps({"memory": "no facts"})}]}
    with patch("src.mcp.server._call_tool", new=AsyncMock(return_value=expected)):
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": "recall_memory",
                                                 "arguments": {"query": "x",
                                                               "user_id": TEST_USER_ID}}})
    result = r.json()["result"]
    assert result["resultType"] == "complete"
    # content payload unchanged (text mirror retained).
    assert result["content"] == expected["content"]


def test_empty_ack_results_stay_byte_identical(client):
    """ping / notifications/initialized return `{}` — an empty result must NOT gain resultType
    (legacy ack semantics untouched; the spec's clients treat missing as complete anyway)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert r.json()["result"] == {}


# ── Mcp-Method / Mcp-Name headers (SEP-2243) ─────────────────────────────────────
# Modern Streamable HTTP POSTs carry Mcp-Method/Mcp-Name so gateways route without parsing the
# body. We ACCEPT them; absence (legacy) falls back to the body method.

def test_mcp_method_header_routes_the_request(client):
    """A self-consistent Mcp-Method header is honored (routing without body parsing)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                    headers={"Mcp-Method": "tools/list"})
    assert r.status_code == 200
    assert r.json()["result"]["resultType"] == "complete"


def test_mcp_method_header_mismatch_is_rejected(client):
    """A header that disagrees with the body method is a mis-routed request → HeaderMismatch."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 6, "method": "tools/list"},
                    headers={"Mcp-Method": "tools/call"})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == premise.ERROR_HEADER_MISMATCH  # -32020 (renumbered per SEP-2575)
    assert "disagrees" in err["message"]


def test_mcp_method_header_routes_without_body_method(client):
    """A modern request may omit the body method entirely and route on the header alone."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7},
                    headers={"Mcp-Method": "ping"})
    assert r.status_code == 200
    assert r.json()["result"] == {}


# ── Mcp-Name validation (SEP-2243) ───────────────────────────────────────────────
# Mcp-Name mirrors params.name (tools/call, prompts/get) or params.uri (resources/read). A
# gateway may route, rate-limit or authorize on the HEADER while the server executes the BODY,
# so a disagreement means the two halves acted on different requests — the spec makes rejecting
# it a MUST. Absence stays tolerated: legacy callers send no header at all.

def test_mcp_name_header_mismatch_is_rejected(client):
    """A Mcp-Name that disagrees with params.name is refused, not silently executed."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 30, "method": "tools/call",
                                  "params": {"name": "recall_memory",
                                             "arguments": {"query": "x",
                                                           "user_id": TEST_USER_ID}}},
                    headers={"Mcp-Method": "tools/call", "Mcp-Name": "forget_fact"})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == premise.ERROR_HEADER_MISMATCH  # -32020
    assert "disagrees" in err["message"]


def test_mcp_name_header_matching_the_body_is_accepted(client):
    """The agreeing case must still dispatch — validation may not break the normal path."""
    expected = {"content": [{"type": "text", "text": json.dumps({"memory": "no facts"})}]}
    with patch("src.mcp.server._call_tool", new=AsyncMock(return_value=expected)):
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 31, "method": "tools/call",
                                      "params": {"name": "recall_memory",
                                                 "arguments": {"query": "x",
                                                               "user_id": TEST_USER_ID}}},
                        headers={"Mcp-Method": "tools/call", "Mcp-Name": "recall_memory"})
    assert r.status_code == 200
    assert r.json()["result"]["content"] == expected["content"]


def test_mcp_name_base64_sentinel_is_decoded_before_comparison(client):
    """A name that is not header-safe travels as =?base64?...?= and must decode before compare.

    Without decoding, every non-ASCII tool name would false-positive as a mismatch.
    """
    import base64
    encoded = "=?base64?" + base64.b64encode(b"recall_memory").decode() + "?="
    expected = {"content": [{"type": "text", "text": json.dumps({"memory": "no facts"})}]}
    with patch("src.mcp.server._call_tool", new=AsyncMock(return_value=expected)):
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 32, "method": "tools/call",
                                      "params": {"name": "recall_memory",
                                                 "arguments": {"query": "x",
                                                               "user_id": TEST_USER_ID}}},
                        headers={"Mcp-Method": "tools/call", "Mcp-Name": encoded})
    assert r.status_code == 200


def test_absent_mcp_name_is_still_served(client):
    """Legacy callers send no Mcp-Name — absence must never be treated as a mismatch."""
    expected = {"content": [{"type": "text", "text": json.dumps({"memory": "no facts"})}]}
    with patch("src.mcp.server._call_tool", new=AsyncMock(return_value=expected)):
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 33, "method": "tools/call",
                                      "params": {"name": "recall_memory",
                                                 "arguments": {"query": "x",
                                                               "user_id": TEST_USER_ID}}})
    assert r.status_code == 200


# ── prompts on the HTTP door ─────────────────────────────────────────────────────
# server/discover advertises a `prompts` capability for BOTH doors. The stdio door has always
# served it; the HTTP door answered -32601, so the advertisement was false on this transport.

def test_discover_advertises_prompts(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 34, "method": "server/discover",
                                  "params": {}})
    assert "prompts" in r.json()["result"]["capabilities"]


def test_prompts_list_is_served_on_http(client):
    """The advertised capability must actually answer here, with the CacheableResult contract."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 35, "method": "prompts/list",
                                  "params": {}})
    assert r.status_code == 200
    result = r.json()["result"]
    names = [p["name"] for p in result["prompts"]]
    assert "identity_intro" in names and "recall_context" in names
    assert result["resultType"] == "complete"
    assert result["ttlMs"] > 0
    assert result["cacheScope"] in ("public", "private")


def test_prompts_get_returns_a_message(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 36, "method": "prompts/get",
                                  "params": {"name": "identity_intro", "arguments": {}}})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][0]["content"]["text"].strip()


def test_prompts_get_unknown_name_is_invalid_params(client):
    """2026-07-28 renumbered not-found to -32602 (Invalid Params); match the stdio door."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 37, "method": "prompts/get",
                                  "params": {"name": "no_such_prompt", "arguments": {}}})
    assert r.json()["error"]["code"] == -32602


def test_prompts_get_ignores_an_extra_argument_like_stdio(client):
    """Argument tolerance must match the stdio door — an extra key is ignored on both."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 38, "method": "prompts/get",
                                  "params": {"name": "identity_intro",
                                             "arguments": {"unexpected": "value"}}})
    assert r.status_code == 200


# ── Tool annotations must describe the code that ships with them ─────────────────
# The spec's default for an un-annotated tool is DESTRUCTIVE, so leaving a write tool
# unannotated forces an automated caller to assume the worst. But annotating it wrongly is
# worse than leaving it blank: it is a false statement the client is invited to act on.

def _annotations(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 60, "method": "tools/list",
                                  "params": {}})
    return {t["name"]: t.get("annotations") or {} for t in r.json()["result"]["tools"]}


def test_every_tool_is_annotated(client):
    ann = _annotations(client)
    unannotated = sorted(n for n, a in ann.items() if not a)
    assert unannotated == [], f"unannotated tools: {unannotated}"


def test_remember_facts_is_declared_destructive_because_it_diverts_to_retraction(client):
    """remember_facts LOOKS additive and is not.

    A turn the brain classifies RETRACTION/CORRECTION is handed to retract_fact_tool ->
    /retract/correct, which supersedes and deletes. If this ever flips to False, the server is
    telling clients that "forget my dog" is an additive update.
    """
    assert _annotations(client)["remember_facts"]["destructiveHint"] is True


def test_every_write_tool_is_declared_destructive(client):
    """There is no purely additive write tool on this server, and saying otherwise is the lie.

    Each of these reaches a supersession path: remember_facts diverts to retract_fact_tool;
    ingest_document posts at user_stated authority and archives the conflicting row on an
    immutable rel_type; learn_facts does the same against equal-rank learned facts; and
    cortex_remember marks earlier notes on the topic superseded. An earlier version of this
    branch declared three of them additive after reading only the MCP handlers — the
    destruction happens one layer down, in the backend they POST to.
    """
    ann = _annotations(client)
    for name in ("remember_facts", "cortex_remember", "ingest_document", "learn_facts",
                 "retract_fact", "forget_fact"):
        assert ann[name]["destructiveHint"] is True, name
        assert ann[name]["readOnlyHint"] is False, name


def test_cortex_recall_is_the_only_non_destructive_writer(client):
    """It bumps engagement counters and supersedes nothing — not read-only, not destructive."""
    ann = _annotations(client)
    assert ann["cortex_recall"]["readOnlyHint"] is False
    assert ann["cortex_recall"]["destructiveHint"] is False


def test_the_destructive_tools_stay_destructive(client):
    ann = _annotations(client)
    assert ann["retract_fact"]["destructiveHint"] is True
    assert ann["forget_fact"]["destructiveHint"] is True


def test_no_tool_that_writes_claims_to_be_read_only(client):
    """readOnlyHint on a tool that writes is the same lie in the other direction.

    recall_memory and cortex_recall are in this list DELIBERATELY. Both read like pure reads
    and neither is: recall_memory diverts to retract_fact_tool on RETRACTION/CORRECTION, calls
    remember_facts_tool on STATEMENT and harvests facts on every route; cortex_recall defaults
    bump=True and POSTs it. An earlier version of this test omitted them, which made it pass
    while the server was still making the claim — a test written around the defect instead of
    at it.
    """
    ann = _annotations(client)
    for name in ("recall_memory", "cortex_recall",
                 "remember_facts", "cortex_remember", "ingest_document", "learn_facts",
                 "retract_fact", "forget_fact"):
        assert ann[name].get("readOnlyHint") is not True, name


def test_recall_memory_is_declared_destructive_because_it_diverts_to_retraction(client):
    """The divert is ungated outside SAAS, so the hint must not promise otherwise."""
    assert _annotations(client)["recall_memory"]["destructiveHint"] is True


# ── The REST door must refuse what the JSON-RPC door refuses ─────────────────────
# Every JSON-RPC call runs through _call_tool, which validates first. Every REST endpoint calls
# its handler DIRECTLY, so the validator never ran on this door at all — and this is the door
# OpenWebUI uses. An empty query used to reach the backend and come back as a confident
# "I don't have any information about that": a wrong answer wearing the shape of a right one.

def test_rest_door_rejects_an_empty_query(client):
    r = client.post("/recall_memory",
                    json={"query": "", "user_id": TEST_USER_ID},
                    headers={"X-OpenWebUI-User-Id": TEST_USER_ID})
    assert r.status_code == 400
    assert "Invalid query" in r.json()["error"]


# Every advertised tool, not just the two that were easy to reach. A reviewer deleted the
# validator from six of the eight endpoints and the whole suite stayed green — coverage of two
# endpoints was coverage of the fix, not of the door.
REST_ENDPOINTS_AND_EMPTY_INPUT = [
    ("/recall_memory",   {"query": ""}),
    ("/cortex_recall",   {"query": ""}),
    ("/remember_facts",  {"text": ""}),
    ("/cortex_remember", {"text": ""}),
    ("/ingest_document", {"text": ""}),
    ("/learn_facts",     {"text": ""}),
    ("/retract_fact",    {"text": ""}),
    ("/forget_fact",     {"subject": ""}),
]


@pytest.mark.parametrize("path,body", REST_ENDPOINTS_AND_EMPTY_INPUT,
                         ids=[p.strip("/") for p, _ in REST_ENDPOINTS_AND_EMPTY_INPUT])
def test_every_rest_endpoint_validates_its_input(path, body, client):
    """The JSON-RPC door refuses these through _call_tool; this door must refuse them too."""
    r = client.post(path, json={**body, "user_id": TEST_USER_ID},
                    headers={"X-OpenWebUI-User-Id": TEST_USER_ID})
    assert r.status_code == 400, f"{path} accepted empty input: {r.status_code} {r.text[:200]}"
    assert "error" in r.json()


def test_rest_door_rejects_empty_text_on_a_write(client):
    r = client.post("/remember_facts",
                    json={"text": "", "user_id": TEST_USER_ID},
                    headers={"X-OpenWebUI-User-Id": TEST_USER_ID})
    assert r.status_code == 400
    assert "error" in r.json()


def test_rest_provisioning_failure_is_flagged_like_the_other_door(client):
    """The same failure was detectable on JSON-RPC and silent on REST — the door OpenWebUI uses."""
    with patch("src.mcp.server._ensure_provisioned", new=AsyncMock(return_value=False)):
        r = client.post("/recall_memory",
                        json={"query": "anything", "user_id": TEST_USER_ID},
                        headers={"X-OpenWebUI-User-Id": TEST_USER_ID})
    body = r.json()
    assert body["status"] == "provisioning"
    assert body.get("isError") is True


def test_rest_door_still_serves_valid_input(client):
    """The guard must refuse bad input without breaking the door it guards."""
    expected = {"memory": "no facts"}
    with patch("src.mcp.server.recall_memory_tool", new=AsyncMock(return_value=expected)), \
         patch("src.mcp.server._ensure_provisioned", new=AsyncMock(return_value=True)):
        r = client.post("/recall_memory",
                        json={"query": "what do you know", "user_id": TEST_USER_ID},
                        headers={"X-OpenWebUI-User-Id": TEST_USER_ID})
    assert r.status_code == 200
    assert r.json() == expected


# ── Protocol version negotiation (SEP-2575) ──────────────────────────────────────
# Modern requests carry `_meta.io.modelcontextprotocol/protocolVersion` (or MCP-Protocol-Version
# header). An unsupported version → UnsupportedProtocolVersionError listing what we DO support.

def test_unsupported_protocol_version_returns_actionable_error(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 8, "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}},
    })
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == premise.ERROR_UNSUPPORTED_PROTOCOL_VERSION  # -32022
    # ACTIONABLE: the data carries the supported list + the requested value, so the client can
    # pick a mutually-supported version and retry (spec-mandated shape).
    assert "2026-07-28" in err["data"]["supported"]
    assert err["data"]["requested"] == "1900-01-01"


def test_supported_protocol_version_in_meta_is_accepted(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
    })
    assert r.status_code == 200


def test_legacy_requests_without_version_are_unaffected(client):
    """A legacy request with no _meta version is served exactly as before (dual-era)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
    assert r.status_code == 200


def test_mcp_protocol_version_header_negotiates(client):
    """The MCP-Protocol-Version header is the HTTP carrier of the same negotiation."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 11, "method": "tools/list"},
                    headers={"MCP-Protocol-Version": "1999-01-01"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == premise.ERROR_UNSUPPORTED_PROTOCOL_VERSION


# ── Initialize still echoes the modern version when asked (dual-era) ─────────────
def test_initialize_advertises_the_same_capabilities_as_discover(client):
    """A legacy client handshakes instead of calling server/discover.

    This door served prompts/list while its `initialize` reply said only `tools`, so exactly the
    pre-2025-06-18 caller the dual-era support exists for was told the capability did not exist
    and never called the endpoint that works.
    """
    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-03-26"}})
    disc = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "server/discover",
                                     "params": {}})
    init_caps = set(init.json()["result"]["capabilities"])
    disc_caps = set(disc.json()["result"]["capabilities"])
    assert init_caps == disc_caps, f"initialize={init_caps} discover={disc_caps}"
    assert "prompts" in init_caps


def test_initialize_echoes_2026_version(client):
    """A legacy client that initializes with the 2026-07-28 version gets it echoed."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 12, "method": "initialize",
                                  "params": {"protocolVersion": "2026-07-28"}})
    assert r.json()["result"]["protocolVersion"] == "2026-07-28"


# ── opencode 1.18 regression (2026-08-10): SDK proposes 2025-11-25 ───────────────
# The official MCP TypeScript SDK's LATEST_PROTOCOL_VERSION is "2025-11-25" and its
# supported-version whitelist will reject any echoed revision it has never seen. We were
# not listing 2025-11-25, so `negotiate_protocol_version` replied with 2026-07-28, the SDK
# threw "protocol version is not supported", and the client's SSE fallback surfaced the
# breakage as a misleading "SSE error: Non-200 status code (405)".

def test_initialize_echoes_2025_11_25_for_opencode_sdk(client):
    """The SDK's proposed version must be echoed verbatim, not clobbered with 2026-07-28."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 30, "method": "initialize",
                                  "params": {"protocolVersion": "2025-11-25"}})
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"


def test_2025_11_25_header_on_subsequent_requests_is_accepted(client):
    """The SDK re-sends the echoed value in `mcp-protocol-version` on every request."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 31, "method": "tools/list"},
                    headers={"MCP-Protocol-Version": "2025-11-25"})
    assert r.status_code == 200
    assert "error" not in r.json()


def test_negotiate_never_offers_newer_than_requested(client):
    """An unknown proposed revision gets the newest revision we serve that is <= it."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 32, "method": "initialize",
                                  "params": {"protocolVersion": "2025-12-01"}})
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"
    assert r.json()["result"]["protocolVersion"] != premise.LATEST_PROTOCOL_VERSION


def test_negotiate_unknown_old_revision_falls_back_to_newest_known(client):
    """A proposal older than everything we list still yields something parseable."""
    assert premise.negotiate_protocol_version("2024-08-01") == "2024-10-07"
    assert premise.negotiate_protocol_version("2025-11-25") == "2025-11-25"
    assert premise.negotiate_protocol_version(None) == premise.LATEST_PROTOCOL_VERSION


# ── Cortex discoverability: canonical order ──────────────────────────────────────
# 2026-07-28 §tools: deterministic order enables caching + stable prompt-cache hits, and ORDER
# IS DISCOVERABILITY — the cortex pair must precede the document/learn/retract lanes so a model
# meets "recall your own notes" before "store a document" (the Bar critic's ordering test).

def test_tools_list_orders_cortex_above_the_document_lane(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 13, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    # The cortex pair sits immediately after the user-memory entry points.
    assert names.index("cortex_recall") < names.index("ingest_document")
    assert names.index("cortex_remember") < names.index("ingest_document")
    # The two-memory loop reads read-before-write and both are discoverable early.
    assert names.index("cortex_recall") < names.index("cortex_remember") or \
           names.index("cortex_remember") < names.index("cortex_recall")
    assert names.index("cortex_recall") in (2, 3) or names.index("cortex_remember") in (2, 3)
    # Deterministic: same order across calls.
    r2 = client.post("/mcp", json={"jsonrpc": "2.0", "id": 14, "method": "tools/list"})
    assert [t["name"] for t in r2.json()["result"]["tools"]] == names


def test_openapi_door_orders_cortex_above_the_document_lane(client):
    """The door OpenWebUI actually reads (per-operation iteration order) must match."""
    spec = client.get("/openapi.json").json()
    ops = [i["post"]["operationId"] for i in spec["paths"].values()]
    assert ops.index("cortex_recall") < ops.index("ingest_document")
    assert ops.index("cortex_remember") < ops.index("ingest_document")


# ── Cortex tools carry outputSchema → predictably parsable results ──────────────
def test_cortex_tools_declare_output_schema():
    from src.mcp.tools import _OUTPUT_SCHEMAS
    for name in ("cortex_recall", "cortex_remember"):
        assert name in _OUTPUT_SCHEMAS, (
            f"{name} must declare outputSchema so spec-strict clients get structuredContent"
        )


# ── stdio dual-era gate ──────────────────────────────────────────────────────────
# The stdio loop gates tools/list + tools/call on `_initialized` for LEGACY clients only. A
# modern (2026-07-28) request is STATELESS — gating it behind a handshake the protocol abolished
# would break every modern client on the stdio transport. These run the server as a subprocess
# and drive the wire protocol: server/discover and tools/list need no backend, so the gate
# (not the tool) is what is under test.

import subprocess
import sys as _sys


def _stdio_roundtrip(lines, env_extra=None):
    """Run the stdio MCP server and exchange one JSON-RPC message per line. Returns responses."""
    import os
    env = dict(os.environ)
    # A subprocess cannot reuse this test's monkeypatched open-core env (that is process-wide
    # here, but the child starts fresh) — so unset the SaaS/auth switches explicitly and pin the
    # URL-detection fast path so a tool that never runs cannot probe the network anyway.
    env.pop("SAAS_MODE", None)
    env.pop("MCP_API_KEY", None)
    env.pop("FAULTLINE_USER_ID", None)
    env.update({"FAULTLINE_API_URL": "http://127.0.0.1:1", "_FAULTLINE_URL_DETECTED": "1"})
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # tools/mcp_server.py imports `src.*` — the repo root must be on the child's path.
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [_sys.executable, "tools/mcp_server.py", "--transport", "stdio"],
        cwd=repo,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    try:
        out, err = proc.communicate(input="\n".join(json.dumps(l) for l in lines) + "\n",
                                    timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    responses = [json.loads(x) for x in out.splitlines() if x.strip()]
    return responses, err


def test_stdio_server_discover_is_stateless():
    """A modern client may send server/discover as its FIRST message — no initialize, no gate."""
    responses, err = _stdio_roundtrip([
        {"jsonrpc": "2.0", "id": "sd1", "method": "server/discover", "params": {}},
    ])
    assert responses, f"no response from stdio server; stderr={err[:400]}"
    result = responses[0]["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"][0] == "2026-07-28"


def test_stdio_tools_list_bypasses_the_initialize_gate_for_modern_clients():
    """2026-07-28: a request carrying `_meta` protocolVersion is self-contained and MUST NOT be
    gated on the legacy initialize handshake."""
    responses, err = _stdio_roundtrip([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
         "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}},
    ])
    assert responses, f"no response from stdio server; stderr={err[:400]}"
    result = responses[0]["result"]
    assert result["resultType"] == "complete"
    names = [t["name"] for t in result["tools"]]
    assert names.index("cortex_recall") < names.index("ingest_document")


def test_stdio_legacy_gate_still_holds():
    """DUAL-ERA, legacy half unchanged: a legacy client (no _meta) that calls tools/list before
    initialize still gets the -32002 gate, exactly as before."""
    responses, err = _stdio_roundtrip([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])
    assert responses, f"no response from stdio server; stderr={err[:400]}"
    assert responses[0]["error"]["code"] == -32002, "legacy gate must hold for non-modern requests"


def test_stdio_unsupported_modern_version_is_actionable():
    responses, err = _stdio_roundtrip([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
         "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1999-01-01"}}},
    ])
    assert responses, f"no response from stdio server; stderr={err[:400]}"
    err_body = responses[0]["error"]
    assert err_body["code"] == premise.ERROR_UNSUPPORTED_PROTOCOL_VERSION
    assert "2026-07-28" in err_body["data"]["supported"]
