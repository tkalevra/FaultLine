"""R14 - residence ATTR lane (critic RG-Z-4 follow-up).

Live-trace conclusions pinned here (the R13-window DB + API-log trace, bench seat
4c7e21b8, repro 'We moved from Falker's Step in Dyer's Hollow to Candler's Rest.'):

  * The R13 moved-from dest REL row DID land live (row 1028 lives_in braider's hold
    @now) - the critic's 'missing rel' was a final-state misread: the battery_c rep's
    F6 supersede archived it 2.3s later and the fresh statement won (row 1030 live) -
    EXACTLY the single-current contract (fresh statement wins, old archives).
  * The REAL attr-lane wound: RETIRE-WITHOUT-WRITE. The F4 residence supersede retired
    the prior attr, and the SS5 authority gate HELD the replacement write
    (scalar_supersede_held_by_authority) - the attr slot left with NO live value.
    Fixed: the SS5 hold is evaluated BEFORE the retire; a held write keeps the prior
    attr live and drops with the honest refusal.
  * A second live wound belongs to the correction rails (NOT this lane's file): the
    scalar-slot twin retire sweeps live residence rel rows (all objects, no age
    window) and killed the same-turn destination row - reported to the owning lane.
    This file pins only this lane's fix and the authority tiers it relies on.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import main as api_main  # noqa: E402


def test_r14_retire_held_before_write_source_contract():
    """The F4 residence retire evaluates the SS5 hold FIRST: a held write must not
    retire the prior attr (retire-without-write left the slot empty live)."""
    import inspect
    src = inspect.getsource(api_main._ingest_impl)
    assert "ingest.scalar_retire_held_by_authority" in src
    assert "SS5 hold evaluated BEFORE the residence" in src
    # the hold precedes the supersession record in the F4 block
    hold_at = src.find("ingest.scalar_retire_held_by_authority")
    retire_at = src.find('cause="residence_current_supersede"')
    assert 0 < hold_at < retire_at


def test_r14_authority_tiers_user_tier_not_held():
    """A user-tier incoming write is never held by a user-tier stored row (the MCP
    door's user_stated must beat the R11-era stored user_corrected junk); a
    lower-authority incoming IS held."""
    assert api_main._scalar_gate_hold("user_corrected", "user_stated") is False
    assert api_main._scalar_gate_hold("user_stated", "user_corrected") is False
    assert api_main._scalar_gate_hold("mcp", "user_stated") is False  # legacy canon
    assert api_main._scalar_gate_hold("user_stated", "llm_inferred") is True
    assert api_main._scalar_gate_hold("user_stated", "llm_learned") is True


def test_r14_stored_rank_canonicalizes_legacy_tokens():
    """Stored legacy tokens rank through the canonical mirror (mcp -> user_stated)."""
    assert api_main._scalar_gate_stored_rank("user_corrected") == 3
    assert api_main._scalar_gate_stored_rank("mcp") == 3
    assert api_main._scalar_gate_stored_rank("assistant") == 3
    assert api_main._scalar_gate_stored_rank("llm_inferred") == 1
