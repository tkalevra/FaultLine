"""Serve-mode MCP TOOL-SURFACE gating — the ingest-off seat gets recall_memory and NOTHING else.

The owner's requirement, verbatim in intent: "an agent with ingest off should only be able to
observe and use recall — no other functions should be usable."

Three properties are pinned here:

  1. ALLOW-LIST, not deny-list. ``saas.agent_mode.SERVE_MODE_TOOLS`` is exactly
     ``{"recall_memory"}``; anything NOT in it is hidden and refused in serve mode — including
     a tool that does not exist yet. A deny-list is how ``cortex_recall``/``cortex_remember``
     came to be fully usable by a read-only seat: they were added after ``WRITE_TOOLS`` was
     written and nobody amended it. That must not be able to happen again.
  2. HIDING IS NOT THE BOUNDARY. ``tools/list`` is filtered as an affordance, but a client's
     tool list is a client-side cache we do not control, so every call is re-decided
     server-side by ``serve_tool_block`` and refused with the existing 200-shaped
     ``status: read_only`` payload (read-only is a valid MODE, not an error).
  3. THE OPEN CORE IS UNCHANGED. With the ``saas`` module unimportable, both transports must
     return the full, untouched tool set and gate nothing. FOSS has no seats and no valve.

Run: python3 -m pytest tests/test_serve_mode_tool_gating.py -q
"""

import json
import sys

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

import src.mcp.server as _mcp
from src.mcp import http_server

from saas import agent_mode as saas_agent_mode


UA = "11111111-1111-4111-8111-111111111111"
ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PRINCIPAL_A = f"account:{ACCT_A}"

# Every tool the server advertises today.
ALL_TOOL_NAMES = {t["name"] for t in _mcp.TOOLS}

# Everything a frozen seat must NOT see or be able to call. Derived from the live TOOLS
# constant minus the allow-list, so a tool added tomorrow is automatically covered by these
# tests instead of silently escaping them.
FORBIDDEN_IN_SERVE = sorted(ALL_TOOL_NAMES - {"recall_memory"})


# ════════════════════════════════════════════════════════════════════════════════
# helpers — drive the real gate against a stubbed seat resolution
# ════════════════════════════════════════════════════════════════════════════════

def _seat_env(monkeypatch, *, saas_on=True, ingest=True, account=ACCT_A):
    """Point agent_mode at a synthetic seat. ``account=None`` → seat unresolvable."""
    monkeypatch.setattr(saas_agent_mode, "saas_mode_enabled", lambda: saas_on)
    monkeypatch.setattr(saas_agent_mode, "account_id_for_principal", lambda p, u: account)
    monkeypatch.setattr(saas_agent_mode, "ingest_allowed", lambda a, u: ingest)


@pytest.fixture
def transport_seat(monkeypatch):
    """Same, but also flips the SAAS_MODE flag the src/ helpers read via their lazy import."""
    def _apply(*, saas_on=True, ingest=True, account=ACCT_A):
        monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: saas_on)
        _seat_env(monkeypatch, saas_on=saas_on, ingest=ingest, account=account)
    return _apply


# ════════════════════════════════════════════════════════════════════════════════
# 1 — the allow-list itself
# ════════════════════════════════════════════════════════════════════════════════

def test_serve_mode_tools_is_exactly_recall_memory():
    assert saas_agent_mode.SERVE_MODE_TOOLS == frozenset({"recall_memory"})


def test_serve_allow_list_is_not_the_write_deny_list():
    # The two sets are separate concepts and must stay separate: cortex_recall is a READ, so
    # folding it into WRITE_TOOLS would call a read a write.
    # retry_document added 2026-08-21 (fix/doc-lane-fairness): it re-runs the ingest lane, so
    # a serve (frozen) seat must not be able to trigger ingestion through it. ingest_file
    # same day: the binary door into the same lane.
    assert saas_agent_mode.WRITE_TOOLS == frozenset(
        {"remember_facts", "learn_facts", "retract_fact", "ingest_document", "forget_fact",
         "retry_document", "ingest_file"}
    )
    assert saas_agent_mode.SERVE_MODE_TOOLS.isdisjoint(saas_agent_mode.WRITE_TOOLS)
    # Every write tool is outside the allow-list → the serve gate is a strict superset.
    assert not saas_agent_mode.WRITE_TOOLS & saas_agent_mode.SERVE_MODE_TOOLS


def test_cortex_tools_are_outside_the_allow_list():
    # The regression this whole change exists for.
    assert "cortex_recall" not in saas_agent_mode.SERVE_MODE_TOOLS
    assert "cortex_remember" not in saas_agent_mode.SERVE_MODE_TOOLS
    assert {"cortex_recall", "cortex_remember"} <= ALL_TOOL_NAMES


# ════════════════════════════════════════════════════════════════════════════════
# 2 — visible_tools(): the tools/list affordance filter
# ════════════════════════════════════════════════════════════════════════════════

def test_serve_mode_exposes_exactly_recall_memory(monkeypatch):
    _seat_env(monkeypatch, ingest=False)
    out = saas_agent_mode.visible_tools(PRINCIPAL_A, UA, _mcp.TOOLS)
    assert {t["name"] for t in out} == {"recall_memory"}
    assert len(out) == 1


def test_train_mode_exposes_the_full_set(monkeypatch):
    _seat_env(monkeypatch, ingest=True)
    out = saas_agent_mode.visible_tools(PRINCIPAL_A, UA, _mcp.TOOLS)
    assert {t["name"] for t in out} == ALL_TOOL_NAMES
    assert out == _mcp.TOOLS


def test_saas_off_exposes_the_full_set(monkeypatch):
    _seat_env(monkeypatch, saas_on=False, ingest=False)
    assert saas_agent_mode.visible_tools(PRINCIPAL_A, UA, _mcp.TOOLS) == _mcp.TOOLS


def test_unresolvable_seat_leaves_the_listing_unchanged(monkeypatch):
    # Affordance, not boundary: a caller we cannot identify is listed everything (exactly as
    # today) and refused at the call. Pinning this keeps the filter non-regressive.
    _seat_env(monkeypatch, ingest=False, account=None)
    assert saas_agent_mode.serve_mode_active(PRINCIPAL_A, UA) is None
    assert saas_agent_mode.visible_tools(PRINCIPAL_A, UA, _mcp.TOOLS) == _mcp.TOOLS


def test_visible_tools_hides_an_unknown_future_tool_in_serve_mode(monkeypatch):
    """THE ALLOW-LIST PROPERTY. A tool nobody has thought of yet is hidden by DEFAULT."""
    _seat_env(monkeypatch, ingest=False)
    future = {"name": "delete_everything_v2", "description": "hypothetical", "inputSchema": {}}
    out = saas_agent_mode.visible_tools(PRINCIPAL_A, UA, list(_mcp.TOOLS) + [future])
    assert {t["name"] for t in out} == {"recall_memory"}


def test_visible_tools_shows_an_unknown_future_tool_in_train_mode(monkeypatch):
    _seat_env(monkeypatch, ingest=True)
    future = {"name": "delete_everything_v2", "description": "hypothetical", "inputSchema": {}}
    out = saas_agent_mode.visible_tools(PRINCIPAL_A, UA, list(_mcp.TOOLS) + [future])
    assert "delete_everything_v2" in {t["name"] for t in out}


def test_visible_tools_does_not_mutate_its_input(monkeypatch):
    _seat_env(monkeypatch, ingest=False)
    before = list(_mcp.TOOLS)
    saas_agent_mode.visible_tools(PRINCIPAL_A, UA, _mcp.TOOLS)
    assert _mcp.TOOLS == before


def test_forged_seat_claim_can_only_hide_more(monkeypatch):
    # A claimed user_id that is not a seat of this account resolves to no row → ingest reads
    # as disabled → the FULL surface collapses to recall. Never the other way round.
    monkeypatch.setattr(saas_agent_mode, "saas_mode_enabled", lambda: True)
    monkeypatch.setattr(saas_agent_mode, "account_id_for_principal", lambda p, u: ACCT_A)
    monkeypatch.setattr(saas_agent_mode, "ingest_allowed",
                        lambda a, u: u == UA)   # only the real seat is ingest-enabled
    forged = "99999999-9999-4999-8999-999999999999"
    assert {t["name"] for t in
            saas_agent_mode.visible_tools(PRINCIPAL_A, forged, _mcp.TOOLS)} == {"recall_memory"}


# ════════════════════════════════════════════════════════════════════════════════
# 3 — serve_tool_block(): the call-time boundary
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tool", FORBIDDEN_IN_SERVE)
def test_serve_mode_refuses_every_non_allow_listed_tool(monkeypatch, tool):
    _seat_env(monkeypatch, ingest=False)
    blocked = saas_agent_mode.serve_tool_block(PRINCIPAL_A, UA, tool)
    assert blocked == saas_agent_mode.READ_ONLY_RESPONSE
    assert blocked["status"] == "read_only"


@pytest.mark.parametrize("tool", ["cortex_recall", "cortex_remember"])
def test_cortex_tools_specifically_refused_in_serve_mode(monkeypatch, tool):
    # Called out explicitly: these two were fully usable by a frozen seat before this change,
    # because they are (correctly) absent from WRITE_TOOLS.
    _seat_env(monkeypatch, ingest=False)
    assert saas_agent_mode.serve_tool_block(PRINCIPAL_A, UA, tool) == \
        saas_agent_mode.READ_ONLY_RESPONSE
    # …and the write gate still says nothing about them — the sets stay independent.
    assert saas_agent_mode.write_gate_block(PRINCIPAL_A, UA, tool) is None


def test_recall_memory_is_never_refused(monkeypatch):
    for ingest in (True, False):
        _seat_env(monkeypatch, ingest=ingest)
        assert saas_agent_mode.serve_tool_block(PRINCIPAL_A, UA, "recall_memory") is None


@pytest.mark.parametrize("tool", sorted(ALL_TOOL_NAMES))
def test_train_mode_refuses_nothing(monkeypatch, tool):
    _seat_env(monkeypatch, ingest=True)
    assert saas_agent_mode.serve_tool_block(PRINCIPAL_A, UA, tool) is None


@pytest.mark.parametrize("tool", sorted(ALL_TOOL_NAMES))
def test_saas_off_refuses_nothing(monkeypatch, tool):
    _seat_env(monkeypatch, saas_on=False, ingest=False)
    assert saas_agent_mode.serve_tool_block(PRINCIPAL_A, UA, tool) is None


def test_serve_block_unresolvable_seat_fails_closed(monkeypatch):
    _seat_env(monkeypatch, ingest=True, account=None)
    # Cannot prove the seat is in train mode → refuse everything outside the allow-list…
    assert saas_agent_mode.serve_tool_block(PRINCIPAL_A, UA, "cortex_recall") == \
        saas_agent_mode.READ_ONLY_RESPONSE
    # …but recall always survives; that is the one thing a frozen seat is guaranteed.
    assert saas_agent_mode.serve_tool_block(PRINCIPAL_A, UA, "recall_memory") is None


def test_serve_block_refuses_an_unknown_future_tool(monkeypatch):
    """THE ALLOW-LIST PROPERTY at the boundary, not just in the listing."""
    _seat_env(monkeypatch, ingest=False)
    assert saas_agent_mode.serve_tool_block(PRINCIPAL_A, UA, "delete_everything_v2") == \
        saas_agent_mode.READ_ONLY_RESPONSE


def test_write_gate_block_is_untouched(monkeypatch):
    # The existing write boundary must behave EXACTLY as before: blocks its five, ignores
    # everything else. Regression guard on "do not fold the cortex tools into WRITE_TOOLS".
    _seat_env(monkeypatch, ingest=False)
    for tool in sorted(saas_agent_mode.WRITE_TOOLS):
        assert saas_agent_mode.write_gate_block(PRINCIPAL_A, UA, tool) == \
            saas_agent_mode.READ_ONLY_RESPONSE
    for tool in ("recall_memory", "cortex_recall", "cortex_remember"):
        assert saas_agent_mode.write_gate_block(PRINCIPAL_A, UA, tool) is None


# ── seat resolution shapes ────────────────────────────────────────────────────────

def test_flu_principal_is_its_own_seat(monkeypatch):
    # A per-user token carries no separate user_id claim: the principal IS the seat.
    monkeypatch.setattr(saas_agent_mode, "saas_mode_enabled", lambda: True)
    monkeypatch.setattr(saas_agent_mode, "account_id_for_principal", lambda p, u: ACCT_A)
    seen = {}

    def _ingest(a, u):
        seen["uid"] = u
        return False
    monkeypatch.setattr(saas_agent_mode, "ingest_allowed", _ingest)
    assert saas_agent_mode.serve_mode_active(UA, "") is True
    assert seen["uid"] == UA


def test_no_seat_at_all_is_undecidable(monkeypatch):
    monkeypatch.setattr(saas_agent_mode, "saas_mode_enabled", lambda: True)
    monkeypatch.setattr(saas_agent_mode, "ingest_allowed",
                        lambda a, u: pytest.fail("reached ingest_allowed with no seat"))
    for principal in (None, "shared", "anonymous", PRINCIPAL_A):
        assert saas_agent_mode.serve_mode_active(principal, "") is None


# ════════════════════════════════════════════════════════════════════════════════
# 4 — HTTP transport seams (src/mcp/http_server.py)
# ════════════════════════════════════════════════════════════════════════════════

def test_http_visible_tools_saas_off_is_identity(transport_seat):
    transport_seat(saas_on=False, ingest=False)
    assert http_server._saas_visible_tools(PRINCIPAL_A, UA, _mcp.TOOLS) is _mcp.TOOLS


def test_http_visible_tools_filters_in_serve_mode(transport_seat):
    transport_seat(ingest=False)
    out = http_server._saas_visible_tools(PRINCIPAL_A, UA, _mcp.TOOLS)
    assert {t["name"] for t in out} == {"recall_memory"}


def test_http_visible_tools_full_in_train_mode(transport_seat):
    transport_seat(ingest=True)
    assert http_server._saas_visible_tools(PRINCIPAL_A, UA, _mcp.TOOLS) == _mcp.TOOLS


def test_http_serve_block_saas_off_returns_none(transport_seat):
    transport_seat(saas_on=False, ingest=False)
    assert http_server._saas_serve_block(PRINCIPAL_A, UA, "cortex_recall") is None


def test_http_serve_block_blocks_in_serve_mode(transport_seat):
    transport_seat(ingest=False)
    assert http_server._saas_serve_block(PRINCIPAL_A, UA, "cortex_recall")["status"] == "read_only"


def test_http_serve_block_error_in_saas_mode_fails_closed(monkeypatch):
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)

    def boom(p, u, t):
        raise RuntimeError("gate module broke")
    monkeypatch.setattr("saas.agent_mode.serve_tool_block", boom)
    out = http_server._saas_serve_block(PRINCIPAL_A, UA, "cortex_recall")
    assert out["status"] == "read_only"      # fail-closed refusal, not a pass-through


def test_http_visible_tools_error_in_saas_mode_leaves_listing_unchanged(monkeypatch):
    # The LISTING is an affordance, so a gate error leaves it open rather than blanking the
    # client's tool menu. The call-time gate above is the one that fails closed.
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)

    def boom(p, u, t):
        raise RuntimeError("gate module broke")
    monkeypatch.setattr("saas.agent_mode.visible_tools", boom)
    assert http_server._saas_visible_tools(PRINCIPAL_A, UA, _mcp.TOOLS) == _mcp.TOOLS


# ════════════════════════════════════════════════════════════════════════════════
# 5 — THE FOSS PATH: with the saas module unimportable, nothing changes
# ════════════════════════════════════════════════════════════════════════════════

# Setting a module to None in sys.modules makes `import`/`from … import` raise ImportError —
# the exact failure the open core sees when saas/ is simply not shipped.
_NO_SAAS = {"saas": None, "saas.config": None, "saas.agent_mode": None}


def test_foss_http_tools_list_is_unchanged():
    with patch.dict(sys.modules, _NO_SAAS):
        with pytest.raises(ImportError):
            import saas.config  # noqa: F401  (proves the simulation is real)
        out = http_server._saas_visible_tools(PRINCIPAL_A, UA, _mcp.TOOLS)
    assert out is _mcp.TOOLS
    assert {t["name"] for t in out} == ALL_TOOL_NAMES


@pytest.mark.parametrize("tool", sorted(ALL_TOOL_NAMES))
def test_foss_http_calls_are_never_gated(tool):
    with patch.dict(sys.modules, _NO_SAAS):
        assert http_server._saas_serve_block(PRINCIPAL_A, UA, tool) is None
        assert http_server._saas_write_block(PRINCIPAL_A, UA, tool) is None


def test_foss_stdio_tools_list_is_unchanged(monkeypatch):
    monkeypatch.setattr(_mcp, "FAULTLINE_USER_ID", UA)   # even WITH a pinned seat
    with patch.dict(sys.modules, _NO_SAAS):
        assert _mcp._serve_visible_tools(_mcp.TOOLS) is _mcp.TOOLS


@pytest.mark.parametrize("tool", sorted(ALL_TOOL_NAMES))
def test_foss_stdio_calls_are_never_gated(tool):
    with patch.dict(sys.modules, _NO_SAAS):
        assert _mcp._serve_tool_refusal(UA, tool) is None


# ════════════════════════════════════════════════════════════════════════════════
# 6 — stdio transport seams (src/mcp/server.py)
# ════════════════════════════════════════════════════════════════════════════════

def test_stdio_no_pinned_seat_cannot_filter(monkeypatch, transport_seat):
    # stdio tools/list carries NO identity. With no FAULTLINE_USER_ID pin there is no seat to
    # decide against, so the listing is returned untouched — documented, and safe because the
    # call-time refusal below still holds.
    transport_seat(ingest=False)
    monkeypatch.setattr(_mcp, "FAULTLINE_USER_ID", "")
    assert _mcp._serve_visible_tools(_mcp.TOOLS) is _mcp.TOOLS


def test_stdio_pinned_seat_filters_in_serve_mode(monkeypatch, transport_seat):
    transport_seat(ingest=False)
    monkeypatch.setattr(_mcp, "FAULTLINE_USER_ID", UA)
    assert {t["name"] for t in _mcp._serve_visible_tools(_mcp.TOOLS)} == {"recall_memory"}


def test_stdio_pinned_seat_full_in_train_mode(monkeypatch, transport_seat):
    transport_seat(ingest=True)
    monkeypatch.setattr(_mcp, "FAULTLINE_USER_ID", UA)
    assert _mcp._serve_visible_tools(_mcp.TOOLS) == _mcp.TOOLS


def test_stdio_saas_off_refuses_nothing(transport_seat):
    transport_seat(saas_on=False, ingest=False)
    for tool in sorted(ALL_TOOL_NAMES):
        assert _mcp._serve_tool_refusal(UA, tool) is None


@pytest.mark.parametrize("tool", FORBIDDEN_IN_SERVE)
def test_stdio_refuses_hidden_tools_at_call_time(transport_seat, tool):
    transport_seat(ingest=False)
    assert _mcp._serve_tool_refusal(UA, tool)["status"] == "read_only"


def test_stdio_recall_survives_serve_mode(transport_seat):
    transport_seat(ingest=False)
    assert _mcp._serve_tool_refusal(UA, "recall_memory") is None


def test_stdio_error_in_saas_mode_fails_closed(monkeypatch):
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)

    def boom(p, u, t):
        raise RuntimeError("gate module broke")
    monkeypatch.setattr("saas.agent_mode.serve_tool_block", boom)
    assert _mcp._serve_tool_refusal(UA, "cortex_recall")["status"] == "read_only"


# ════════════════════════════════════════════════════════════════════════════════
# 7 — end to end over the real JSON-RPC dispatcher
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def rpc(monkeypatch):
    """TestClient on the real app with auth disabled (principal 'anonymous')."""
    # The app's STARTUP check refuses to boot with SAAS_MODE on and no MCP_API_KEY. A sibling
    # test file that leaves SAAS_MODE=1 in os.environ therefore breaks this fixture through no
    # fault of its own (the known cross-test env-pollution baseline). Boot with SAAS reading
    # OFF, then switch it on below — the gates call this same resolver, so coverage is
    # identical and this file is immune to whatever ran before it.
    #
    # Patch the RESOLVER, never os.environ: deleting SAAS_MODE from the environment fixes this
    # fixture but flips SAAS_MODE-conditional skips in OTHER test files, which is the exact
    # cross-file pollution pathology this suite already suffers from. Do not add to it.
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: False)
    monkeypatch.setattr(http_server, "MCP_API_KEY", "")
    # These tests turn SAAS_MODE on, which wakes the offline/maintenance middleware. It is
    # fail-CLOSED (503) when the control-plane DB is unreadable, and there is no DB here — stub
    # it "online" so the tests exercise the tool gate rather than the maintenance switch.
    monkeypatch.setattr("saas.platform_settings.offline_state",
                        lambda: {"offline": False, "message": ""})
    monkeypatch.setattr(_mcp, "FAULTLINE_USER_ID", UA)
    monkeypatch.setattr(_mcp, "_FAULTLINE_URL_DETECTED", True)
    with TestClient(http_server.app) as c:
        yield c


def _list_tools(rpc):
    resp = rpc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"X-OpenWebUI-User-Id": UA})
    assert resp.status_code == 200
    return {t["name"] for t in resp.json()["result"]["tools"]}


def test_e2e_serve_mode_lists_only_recall(rpc, transport_seat):
    transport_seat(ingest=False)
    assert _list_tools(rpc) == {"recall_memory"}


def test_e2e_train_mode_lists_everything(rpc, transport_seat):
    transport_seat(ingest=True)
    assert _list_tools(rpc) == ALL_TOOL_NAMES


def test_e2e_saas_off_lists_everything(rpc, transport_seat):
    transport_seat(saas_on=False, ingest=False)
    assert _list_tools(rpc) == ALL_TOOL_NAMES


@pytest.mark.parametrize("tool", FORBIDDEN_IN_SERVE)
def test_e2e_serve_mode_refuses_hidden_tool_calls(rpc, transport_seat, monkeypatch, tool):
    """Never trust the client's cached list — call the hidden tool anyway and get refused."""
    transport_seat(ingest=False)

    async def _never(*a, **kw):
        pytest.fail(f"{tool} reached the backend dispatch in serve mode")
    monkeypatch.setattr(_mcp, "_call_tool", _never)

    resp = rpc.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": {"user_id": UA, "text": "x", "query": "x"}},
    }, headers={"X-OpenWebUI-User-Id": UA})
    assert resp.status_code == 200                       # read-only is a MODE, not an error
    payload = json.loads(resp.json()["result"]["content"][0]["text"])
    assert payload["status"] == "read_only"


def test_e2e_serve_mode_still_serves_recall(rpc, transport_seat, monkeypatch):
    transport_seat(ingest=False)

    async def _fake(name, args, progress_token=None):
        return {"content": [{"type": "text", "text": json.dumps({"memory": "ok"})}]}
    monkeypatch.setattr(_mcp, "_call_tool", _fake)

    resp = rpc.post("/mcp", json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "recall_memory", "arguments": {"user_id": UA, "query": "hi"}},
    }, headers={"X-OpenWebUI-User-Id": UA})
    assert resp.status_code == 200
    assert json.loads(resp.json()["result"]["content"][0]["text"])["memory"] == "ok"
