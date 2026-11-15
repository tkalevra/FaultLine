"""Chatbot-integration TRIAL — pure-helper + ledger-shape tests (SPEC §12, migration 220).

No DB / no network. These pin the fail-closed liveness logic of ``_trial_active`` /
``_trial_lapsed`` (the gate clause), and the ledger action mapping / non-billable shape of
``record_chatbot_trial_change`` (the function's input contract; the DB write is not exercised).
"""

from datetime import datetime, timedelta, timezone

import pytest

from saas import agent_entitlement, entitlements


# ════════════════════════════════════════════════════════════════════════════════
# _trial_active — the gate's liveness clause (fail-CLOSED)
# ════════════════════════════════════════════════════════════════════════════════

def test_trial_active_future_expiry_not_revoked():
    assert agent_entitlement._trial_active(datetime.now(timezone.utc) + timedelta(days=5), None) is True


def test_trial_active_past_expiry_is_false():
    assert agent_entitlement._trial_active(datetime.now(timezone.utc) - timedelta(days=1), None) is False


def test_trial_active_revoked_is_false_even_if_future():
    future = datetime.now(timezone.utc) + timedelta(days=5)
    assert agent_entitlement._trial_active(future, datetime.now(timezone.utc)) is False


def test_trial_active_null_expiry_is_false():
    assert agent_entitlement._trial_active(None, None) is False


def test_trial_active_naive_timestamp_treated_as_utc_future():
    # a naive future timestamp must still read as live (defensive UTC assumption), not crash
    naive_future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3)
    assert agent_entitlement._trial_active(naive_future, None) is True


def test_trial_active_garbage_is_false_not_raise():
    assert agent_entitlement._trial_active("not-a-timestamp", None) is False


# ════════════════════════════════════════════════════════════════════════════════
# _trial_lapsed — distinguishes "had a trial, now dead" from "never had one"
# ════════════════════════════════════════════════════════════════════════════════

def test_trial_lapsed_past_expiry_is_true():
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    assert agent_entitlement._trial_lapsed(past, None) is True


def test_trial_lapsed_revoked_is_true():
    future = datetime.now(timezone.utc) + timedelta(days=5)
    assert agent_entitlement._trial_lapsed(future, datetime.now(timezone.utc)) is True


def test_trial_lapsed_null_expiry_is_false_never_had_trial():
    assert agent_entitlement._trial_lapsed(None, None) is False


def test_trial_lapsed_active_is_false():
    future = datetime.now(timezone.utc) + timedelta(days=5)
    assert agent_entitlement._trial_lapsed(future, None) is False


# ════════════════════════════════════════════════════════════════════════════════
# chatbot_trial_active — control-plane read fails CLOSED without an account
# ════════════════════════════════════════════════════════════════════════════════

def test_chatbot_trial_active_none_account_is_false():
    assert agent_entitlement.chatbot_trial_active(None) is False
    assert agent_entitlement.chatbot_trial_active("") is False


# ════════════════════════════════════════════════════════════════════════════════
# record_chatbot_trial_change — action mapping + non-billable contract (no DB)
# ════════════════════════════════════════════════════════════════════════════════

def test_trial_ledger_actions_registered():
    """The three trial actions are in LEDGER_ACTIONS so the append-only ledger accepts them."""
    assert "agent_trial" in entitlements.LEDGER_ACTIONS
    assert "agent_trial_extend" in entitlements.LEDGER_ACTIONS
    assert "agent_trial_off" in entitlements.LEDGER_ACTIONS


def test_trial_ledger_actions_not_billable():
    """A trial is FREE — its actions must NOT be in BILLABLE_ACTIONS (no signature required)."""
    for a in ("agent_trial", "agent_trial_extend", "agent_trial_off"):
        assert a not in entitlements.BILLABLE_ACTIONS


def test_record_chatbot_trial_change_rejects_bad_kind():
    r = entitlements.record_chatbot_trial_change("acct", "bogus", days=14)
    assert r["ok"] is False
    assert r["status_code"] == 400


def test_record_chatbot_trial_change_action_map():
    """The internal kind→action map mirrors the ledger action names."""
    assert entitlements._TRIAL_ACTIONS == {
        "grant": "agent_trial", "extend": "agent_trial_extend", "revoke": "agent_trial_off"}
