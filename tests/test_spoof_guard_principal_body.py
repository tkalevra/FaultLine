"""IDENTITY-HONESTY BUG 4 - the self-service 403: name the authenticated principal's seat.

Measured on the owner's live demo (2026-08-21): the per-user-token spoof guard rejects a
model-filled wrong user_id with 403 "tenant spoof attempt" but the body named NO principal,
so the human saw only a generic transport error ("streamable HTTP error") with nothing
actionable. The caller's own principal seat is their OWN identity - safe to disclose to
them - and naming it lets a human (or a model) self-correct: omit user_id and the transport
binds it.

Pins:
  * the 403 body names BOTH the rejected claim AND the token's actual seat;
  * the body steers toward the fix (omit user_id - the transport binds it);
  * the tool descriptions (tools.py::TOOLS, the single source of truth OpenWebUI reads)
    steer models to OMIT user_id rather than invent/carry one over;
  * the at-cap branch keeps its distinct message (license is a ceiling, not a spoof).
"""
import re

import pytest

from src.mcp import server as mcp_server


PRINCIPAL = "22222222-2222-4222-8222-222222222222"
CLAIMED = "33333333-3333-4333-8333-333333333333"


def test_spoof_403_names_the_principal_seat():
    """The gold bar: the 403 body tells the caller which seat their token actually is."""
    with pytest.raises(mcp_server.TenantSpoofError) as exc_info:
        mcp_server.bind_tenant(PRINCIPAL, CLAIMED)
    err = exc_info.value
    assert err.status_code == 403
    assert PRINCIPAL in (err.message or ""), (
        f"the 403 body must name the authenticated principal's seat ({PRINCIPAL}) - "
        f"got {err.message!r} (bug 4)")


def test_spoof_403_names_the_rejected_claim():
    with pytest.raises(mcp_server.TenantSpoofError) as exc_info:
        mcp_server.bind_tenant(PRINCIPAL, CLAIMED)
    assert CLAIMED in (exc_info.value.message or ""), (
        "the 403 body must name the rejected claimed user_id so the caller can see WHICH "
        "value was refused")


def test_spoof_403_steers_to_self_service():
    with pytest.raises(mcp_server.TenantSpoofError) as exc_info:
        mcp_server.bind_tenant(PRINCIPAL, CLAIMED)
    msg = exc_info.value.message or ""
    assert re.search(r"omit user_id", msg, re.IGNORECASE), (
        "the 403 body should steer the caller to the fix: omit user_id and the transport "
        "binds the seat")


def test_matching_claim_still_binds():
    """The happy path is untouched: a claim equal to the principal binds normally."""
    assert mcp_server.bind_tenant(PRINCIPAL, PRINCIPAL) == PRINCIPAL


def test_omitted_claim_binds_to_principal():
    assert mcp_server.bind_tenant(PRINCIPAL, "") == PRINCIPAL


# ── Tool-description steering (the model-side half of the fix) ────────────────


def test_tool_descriptions_steer_to_omit_user_id():
    """Every user_id description in TOOLS must steer models to OMIT the field (the
    transport binds the authenticated seat; a mismatched model-filled value is the
    measured failure mode)."""
    from src.mcp import tools as mcp_tools
    steered = 0
    total = 0
    for tool in mcp_tools.TOOLS:
        schema = tool.get("parameters") or tool.get("inputSchema") or {}
        props = (schema.get("properties") or {})
        uid = props.get("user_id")
        if not uid:
            continue
        total += 1
        desc = (uid.get("description") or "").lower()
        if "omit" in desc and ("never invent" in desc or "best omitted" in desc):
            steered += 1
    assert total > 0, "expected user_id fields in TOOLS"
    assert steered == total, (
        f"{total - steered} of {total} user_id descriptions do not steer to OMIT - "
        "a model filling user_id is the measured 403 source (bug 4)")


def test_tool_descriptions_forbid_carryover():
    """The failure mode is a model CARRYING OVER a remembered user_id; the description
    must forbid it explicitly."""
    from src.mcp import tools as mcp_tools
    for tool in mcp_tools.TOOLS:
        schema = tool.get("parameters") or tool.get("inputSchema") or {}
        uid = (schema.get("properties") or {}).get("user_id")
        if not uid:
            continue
        desc = (uid.get("description") or "").lower()
        assert "carry over" in desc or "never invent" in desc, (
            f"tool {tool.get('name')}: user_id description must forbid "
            "inventing/carrying over the value")
