"""Regression — audit #5: serve-mode write-bypass via the recall_memory divert.

`recall_memory` is a READ tool and by design never consults the transport write-gate, but it
DIVERTS into a write: to `retract_fact` on a CORRECTION/RETRACTION, and to `remember_facts` on a
mis-routed STATEMENT. Before this fix, a read-only (serve-mode) seat could archive or ingest
THROUGH the recall entry point — the entry tool name ("recall_memory") smuggled the effective
write past the freeze. `_serve_mode_blocks_divert` now gates the EFFECTIVE action at the divert;
when the seat can't write, recall must NOT divert — it falls through to a plain read-only recall.

SAAS off / open-core → the guard is absent and behavior is byte-for-byte unchanged (all diverts
proceed). Fail-CLOSED: SAAS on but the gate errors → refuse the hidden write.
"""

import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.mcp.server as srv
from src.mcp.server import _serve_mode_blocks_divert, recall_memory_tool, _learn_via_llm


def _swallow_task(coro):
    """create_task stand-in: close the inline _background_learn() coroutine (so it never runs
    and emits no 'coroutine was never awaited' warning) and return a dummy handle."""
    coro.close()
    return MagicMock()


# ── unit: the guard truth table ──────────────────────────────────────────────

def test_guard_saas_off_allows_divert():
    # Open-core / SAAS off → gate absent → divert allowed (unchanged behavior).
    with patch("saas.config.saas_mode_enabled", return_value=False):
        assert _serve_mode_blocks_divert("u1", "retract_fact") is False


def test_guard_blocks_when_gate_blocks():
    with patch("saas.config.saas_mode_enabled", return_value=True), \
         patch("saas.agent_mode.write_gate_block", return_value={"status": "read_only"}):
        assert _serve_mode_blocks_divert("u1", "retract_fact") is True
        assert _serve_mode_blocks_divert("u1", "remember_facts") is True


def test_guard_allows_when_gate_permits():
    # Train-mode seat (ingest allowed) → gate returns None → divert proceeds.
    with patch("saas.config.saas_mode_enabled", return_value=True), \
         patch("saas.agent_mode.write_gate_block", return_value=None):
        assert _serve_mode_blocks_divert("u1", "retract_fact") is False


def test_guard_fail_closed_on_gate_error():
    # SAAS on but the control-plane read throws → fail-CLOSED (refuse the hidden write).
    with patch("saas.config.saas_mode_enabled", return_value=True), \
         patch("saas.agent_mode.write_gate_block", side_effect=RuntimeError("db down")):
        assert _serve_mode_blocks_divert("u1", "retract_fact") is True


# ── integration: recall must not divert into a write in serve mode ────────────

def _empty_query_client():
    """An _http_client whose /query returns an empty walk (so recall ends cleanly)."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"facts": [], "attributes": {}})
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_correction_recall_does_not_retract_in_serve_mode():
    """A CORRECTION-classified recall on a serve seat NEVER reaches retract_fact_tool; it falls
    through to a plain read-only recall (the read is honored, the smuggled supersede is refused)."""
    fake_retract = AsyncMock(return_value={"memory": "retracted"})
    fake_client = _empty_query_client()

    with patch.object(srv, "RECALL_INTENT_ROUTING", True), \
         patch.object(srv, "_maybe_intercept_slash", AsyncMock(return_value=None)), \
         patch.object(srv, "_classify_and_gate", AsyncMock(return_value=("CORRECTION", 0.9, {}))), \
         patch.object(srv, "retract_fact_tool", fake_retract), \
         patch.object(srv, "_harvest_turn_facts", AsyncMock(return_value=None)), \
         patch.object(srv, "_http_client", fake_client), \
         patch("saas.config.saas_mode_enabled", return_value=True), \
         patch("saas.agent_mode.write_gate_block", return_value={"status": "read_only"}):
        result = await recall_memory_tool("forget my dog Rex", "seat-uuid")

    fake_retract.assert_not_awaited()      # the destructive divert was refused
    fake_client.post.assert_awaited()      # fell through to the /query read
    # VEHICLE-SWAP (SERVE_PLAIN_ANSWERS, 2026-11-14): the plain honest-empty line.
    assert "I don't have anything on that yet." in result["memory"]  # empty read → abstention


@pytest.mark.asyncio
async def test_correction_recall_diverts_when_write_allowed():
    """Control: in train mode (write allowed) the SAME correction STILL diverts — now onto
    the DEFERRED lane (recall-llm-burn, 2026-10-26): the supersede is queued (intent
    preserved, unattested authorship), never awaited inline, and the response carries the
    background note plus the /query read."""
    fake_defer = AsyncMock(return_value=True)
    fake_retract = AsyncMock(return_value={"memory": "retracted"})
    fake_client = _empty_query_client()

    with patch.object(srv, "RECALL_INTENT_ROUTING", True), \
         patch.object(srv, "_maybe_intercept_slash", AsyncMock(return_value=None)), \
         patch.object(srv, "_classify_and_gate", AsyncMock(return_value=("CORRECTION", 0.9, {}))), \
         patch.object(srv, "_defer_correction_supersede", fake_defer), \
         patch.object(srv, "retract_fact_tool", fake_retract), \
         patch.object(srv, "_harvest_turn_facts", AsyncMock(return_value=None)), \
         patch.object(srv, "_http_client", fake_client), \
         patch("saas.config.saas_mode_enabled", return_value=True), \
         patch("saas.agent_mode.write_gate_block", return_value=None):
        result = await recall_memory_tool("forget my dog Rex", "seat-uuid")

    fake_defer.assert_awaited_once_with("forget my dog Rex", "seat-uuid", "CORRECTION")
    fake_retract.assert_not_awaited()       # write allowed → the supersede proceeds, deferred
    fake_client.post.assert_awaited()       # the /query read still happened
    assert "processing in the background" in result["memory"]


# ── audit-#5 RESIDUAL: the /expand slash-command write-bypass ─────────────────
#
# `/expand <topic>` reaches the write dispatch (_learn_via_llm → fire-and-forget backend /learn,
# source=llm_learn) THROUGH the recall_memory READ tool via _maybe_intercept_slash — ABOVE recall's
# intent-divert guards. Before the fix, a frozen (serve-mode) seat could WRITE ontology rows via
# recall_memory("/expand X"). The gate now sits at the top of _learn_via_llm (the single write
# chokepoint, shared by both entry points) and honors the SAME agent-mode freeze as learn_facts.


def _ingest_route_client(ingest_enabled=True):
    """An _http_client whose GET /internal/ingest-route reports the GLOBAL freeze state.
    (post is present but must NOT be used on the /expand path — the backend /learn call is
    scheduled via asyncio.create_task, which the tests stub out.)"""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"ingest_enabled": ingest_enabled})
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_expand_via_recall_does_not_learn_in_serve_mode():
    """FROZEN seat: recall_memory("/expand robotics online") must NOT dispatch a backend /learn.
    The serve-mode gate short-circuits _learn_via_llm BEFORE the ingest-route probe OR create_task;
    the caller gets the read-only ingest_disabled response, no write is scheduled."""
    fake_client = _ingest_route_client()

    with patch.object(srv, "_http_client", fake_client), \
         patch.object(srv.asyncio, "create_task", side_effect=_swallow_task) as fake_task, \
         patch("saas.config.saas_mode_enabled", return_value=True), \
         patch("saas.agent_mode.write_gate_block", return_value={"status": "read_only"}):
        result = await recall_memory_tool("/expand robotics online", "seat-uuid")

    fake_task.assert_not_called()           # no background /learn scheduled
    fake_client.get.assert_not_awaited()    # gate fired BEFORE the ingest-route probe
    fake_client.post.assert_not_awaited()   # nothing written
    assert result.get("status") == srv._INGEST_DISABLED_STATUS


@pytest.mark.asyncio
async def test_expand_via_recall_learns_when_write_allowed():
    """Control — TRAIN mode (write allowed): the SAME /expand DOES dispatch the backend /learn."""
    fake_client = _ingest_route_client(ingest_enabled=True)

    with patch.object(srv, "_http_client", fake_client), \
         patch.object(srv.asyncio, "create_task", side_effect=_swallow_task) as fake_task, \
         patch("saas.config.saas_mode_enabled", return_value=True), \
         patch("saas.agent_mode.write_gate_block", return_value=None):
        result = await recall_memory_tool("/expand robotics online", "seat-uuid")

    fake_task.assert_called_once()          # the fire-and-forget /learn was scheduled
    assert "concept map" in result.get("memory", "").lower()


@pytest.mark.asyncio
async def test_expand_saas_off_learns_byte_for_byte():
    """SAAS OFF / open-core: the guard is absent → /expand dispatches exactly as before."""
    fake_client = _ingest_route_client(ingest_enabled=True)

    with patch.object(srv, "_http_client", fake_client), \
         patch.object(srv.asyncio, "create_task", side_effect=_swallow_task) as fake_task, \
         patch("saas.config.saas_mode_enabled", return_value=False):
        result = await recall_memory_tool("/expand robotics", "seat-uuid")

    fake_task.assert_called_once()
    assert "concept map" in result.get("memory", "").lower()


@pytest.mark.asyncio
async def test_learn_via_llm_direct_fail_closed_on_gate_error():
    """Unit — SAAS on and the control-plane read THROWS → fail-CLOSED: no probe, no learn."""
    fake_client = _ingest_route_client()

    with patch.object(srv, "_http_client", fake_client), \
         patch.object(srv.asyncio, "create_task", side_effect=_swallow_task) as fake_task, \
         patch("saas.config.saas_mode_enabled", return_value=True), \
         patch("saas.agent_mode.write_gate_block", side_effect=RuntimeError("db down")):
        result = await _learn_via_llm("robotics", "seat-uuid")

    fake_task.assert_not_called()
    fake_client.get.assert_not_awaited()
    assert result.get("status") == srv._INGEST_DISABLED_STATUS
