"""CONSTRAINED customer-agent lane is HINGED ON AGENT SEATS.

The ``customer_chat`` MCP tool (the customer-facing constrained lane) must be gated on the
EXACT same entitlement as ``/v1/agent/chat``: ``seat_type='agent'`` + the chat-agent feature
/ bucket — never reachable on a STANDARD seat or an un-entitled account. This file pins that
at the gate level:

  1. ``_saas_customer_chat_block`` REFUSES a standard seat (serve_allowed → not_agent_seat).
  2. ``_saas_customer_chat_block`` REFUSES an un-entitled account (feature off / no bucket).
  3. ``_saas_customer_chat_block`` ALLOWS an entitled agent seat.
  4. fail-closed: a gate error / unresolvable account → refuse, never advertise/serve.
  5. ``tools/list`` does NOT advertise ``customer_chat`` for a refused seat.
  6. open-core (saas unimportable / SAAS off) → no gate, no advertisement.

Run: python3 -m pytest tests/test_customer_chat_entitlement.py -q
"""

import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, ".")

from src.mcp import http_server


UA = "11111111-1111-4111-8111-111111111111"
ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PRINCIPAL_A = f"account:{ACCT_A}"


def _allowed(account=ACCT_A, user=UA):
    return (True, None)


def _refused(reason_code, status=402):
    return (False, {"reason_code": reason_code, "status": status,
                    "message": f"refused {reason_code}"})


@pytest.fixture(autouse=True)
def saas_on(monkeypatch):
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)
    import saas.config as _cfg
    monkeypatch.setattr(_cfg, "saas_mode_enabled", lambda: True)


def _saas_block(principal, user, account):
    """Drive _saas_customer_chat_block. saas_mode_enabled is patched True by the autouse
    fixture on the MODULE (saas.config.saas_mode_enabled) which the helper's
    `from saas.config import saas_mode_enabled` resolves at call time; tests that need
    SAAS-OFF patch it back explicitly."""
    return http_server._saas_customer_chat_block(principal, user, account)


def _visible_tools(monkeypatch, serve_allowed):
    """Patch serve_allowed + account resolution, call _saas_visible_tools."""
    with patch("saas.agent_entitlement.serve_allowed", side_effect=serve_allowed):
        with patch("saas.agent_mode.account_id_for_principal",
                   lambda p, u: ACCT_A if p == PRINCIPAL_A else None):
            return http_server._saas_visible_tools(PRINCIPAL_A, UA, [])


# ════════════════════════════════════════════════════════════════════════════════
# 1–2. the gate refuses a non-agent seat / un-entitled account
# ════════════════════════════════════════════════════════════════════════════════

def test_block_refuses_standard_seat():
    with patch("saas.agent_entitlement.serve_allowed", side_effect=lambda a, u: _refused("not_agent_seat")):
        with patch("saas.agent_mode.account_id_for_principal",
                   lambda p, u: ACCT_A if p == PRINCIPAL_A else None):
            b = _saas_block(PRINCIPAL_A, UA, ACCT_A)
    assert b is not None
    assert b["reason_code"] == "not_agent_seat"


def test_block_refuses_feature_off():
    with patch("saas.agent_entitlement.serve_allowed", side_effect=lambda a, u: _refused("agent_feature_off")):
        b = _saas_block(PRINCIPAL_A, UA, ACCT_A)
    assert b is not None
    assert b["reason_code"] == "agent_feature_off"


def test_block_refuses_unresolvable_account():
    with patch("saas.agent_entitlement.serve_allowed", side_effect=lambda a, u: _refused("no_account")):
        with patch("saas.agent_mode.account_id_for_principal", lambda p, u: None):
            b = _saas_block(PRINCIPAL_A, UA, None)
    assert b is not None
    assert b["reason_code"] == "no_account"


# ════════════════════════════════════════════════════════════════════════════════
# 3. the gate allows an entitled agent seat
# ════════════════════════════════════════════════════════════════════════════════

def test_block_allows_entitled_agent_seat():
    with patch("saas.agent_entitlement.serve_allowed", side_effect=lambda a, u: _allowed()):
        b = _saas_block(PRINCIPAL_A, UA, ACCT_A)
    assert b is None


# ════════════════════════════════════════════════════════════════════════════════
# 4. fail-closed: a gate error / SAAS-off refuses (never silently serves)
# ════════════════════════════════════════════════════════════════════════════════

def test_block_fail_closed_on_error(monkeypatch):
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)
    with patch("saas.agent_entitlement.serve_allowed", side_effect=RuntimeError("boom")):
        b = _saas_block(PRINCIPAL_A, UA, ACCT_A)
    assert b is not None
    assert b["reason_code"] == "agent_feature_off"


def test_block_noop_when_saas_off(monkeypatch):
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: False)
    b = _saas_block(PRINCIPAL_A, UA, ACCT_A)
    assert b is None  # open-core: no gate (today's behavior), no advertisement


# ════════════════════════════════════════════════════════════════════════════════
# 5. tools/list does not advertise customer_chat for a refused seat
# ════════════════════════════════════════════════════════════════════════════════

def test_visible_tools_hides_for_refused_seat(monkeypatch):
    tools = _visible_tools(monkeypatch, lambda a, u: _refused("not_agent_seat"))
    assert all(t.get("name") != "customer_chat" for t in tools)


def test_visible_tools_advertises_for_entitled_agent_seat(monkeypatch):
    tools = _visible_tools(monkeypatch, lambda a, u: _allowed())
    assert any(t.get("name") == "customer_chat" for t in tools)
