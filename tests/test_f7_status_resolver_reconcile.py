"""FOG-FAMILY F7 — the status/banner lane must consult the TURN-LANE brain resolution.

THE WOUND (bench lab, DEV/FINDINGS-fog-family-lab-20261107.md F7): recall STATUS banners cycled
"endpoint-unreachable" -> "no-model-endpoint" while ingest resolved the account-level brain
fine. The status lane renders from PROBE ROWS; recall is deterministic (zero LLM calls), so
during a recall-heavy session nothing rewrites those rows and the banner keeps describing a
moment that has passed. The turn lane resolves the brain fresh per request
(bind_request_brain -> _account_for_user + seat_brain.resolve_brain) — and ingest worked.

THE FIX (BRAIN_STATUS_RESOLVER_RECONCILE, default ON): when the worst lane is a NO-ENDPOINT
state (no_brain / key_undecryptable / egress_blocked), degradation_notice consults the SAME
chain; a REAL brain resolves -> silent (the healthy contract). Real faults (FAIL / FLAPPING,
which carry endpoint/fault evidence about a live network attempt) are UNTOUCHED.

PURE tests — probe state injected, resolver monkeypatched. Same SAAS_MODE import discipline as
tests/test_brain_no_brain_banner_and_skip.py.
Run: python3 -m pytest tests/test_f7_status_resolver_reconcile.py -q
"""
import importlib
import os

import pytest

_prev_saas_mode = os.environ.get("SAAS_MODE")
os.environ["SAAS_MODE"] = _prev_saas_mode or "1"
try:
    bh = importlib.import_module("saas.brain_health")
finally:
    if _prev_saas_mode is None:
        os.environ.pop("SAAS_MODE", None)
    else:
        os.environ["SAAS_MODE"] = _prev_saas_mode


def _state(state):
    """One degraded lane, minimal row shape worst_lane reads."""
    return {"HARVEST": {"state": state, "endpoint_host": None, "detail": None,
                        "observed_at": None}}


# ── THE WOUND, CLOSED ──────────────────────────────────────────────────────────────────────
def test_no_endpoint_banner_goes_silent_when_the_turn_lane_resolves(monkeypatch):
    """A NO_BRAIN record the engine's own resolver contradicts is about a moment that has
    passed — the banner must not tell a seat with a working brain to go configure one."""
    monkeypatch.setattr(bh, "BRAIN_STATUS_RESOLVER_RECONCILE", True)
    monkeypatch.setattr(bh, "read_probe_state", lambda uid: _state(bh.STATE_NO_BRAIN))
    monkeypatch.setattr(bh, "_turn_lane_brain_resolves", lambda uid: True)
    assert bh.degradation_notice("user-abcd1234") is None


def test_key_undecryptable_record_also_reconciles(monkeypatch):
    """The KEK-mismatch window stamped key_undecryptable rows; once the resolver succeeds the
    record is stale the same way (the lab's banner CYCLE was exactly this interleaving)."""
    monkeypatch.setattr(bh, "BRAIN_STATUS_RESOLVER_RECONCILE", True)
    monkeypatch.setattr(bh, "read_probe_state", lambda uid: _state(bh.STATE_KEY_UNDECRYPTABLE))
    monkeypatch.setattr(bh, "_turn_lane_brain_resolves", lambda uid: True)
    assert bh.degradation_notice("user-abcd1234") is None


def test_egress_blocked_record_also_reconciles(monkeypatch):
    """Egress-refused rows are the third no-endpoint state; a resolver that NOW reaches the
    address's config means the record is stale (the egress policy read the saved row)."""
    monkeypatch.setattr(bh, "BRAIN_STATUS_RESOLVER_RECONCILE", True)
    monkeypatch.setattr(bh, "read_probe_state", lambda uid: _state(bh.STATE_EGRESS_BLOCKED))
    monkeypatch.setattr(bh, "_turn_lane_brain_resolves", lambda uid: True)
    assert bh.degradation_notice("user-abcd1234") is None


# ── REAL FAULTS ARE NEVER SILENCED ─────────────────────────────────────────────────────────
def test_real_fail_banner_survives_a_working_resolution(monkeypatch):
    """STATE_FAIL carries endpoint evidence about a LIVE network attempt — the reconcile gate
    must not swallow it even when resolution succeeds."""
    monkeypatch.setattr(bh, "BRAIN_STATUS_RESOLVER_RECONCILE", True)
    monkeypatch.setattr(bh, "read_probe_state", lambda uid: _state(bh.STATE_FAIL))
    monkeypatch.setattr(bh, "_turn_lane_brain_resolves", lambda uid: True)
    notice = bh.degradation_notice("user-abcd1234")
    assert notice, "a real FAIL must still render"


def test_flapping_banner_survives(monkeypatch):
    monkeypatch.setattr(bh, "BRAIN_STATUS_RESOLVER_RECONCILE", True)
    monkeypatch.setattr(bh, "read_probe_state", lambda uid: _state(bh.STATE_FLAPPING))
    monkeypatch.setattr(bh, "_turn_lane_brain_resolves", lambda uid: True)
    assert bh.degradation_notice("user-abcd1234")


# ── GATE DISCIPLINE ────────────────────────────────────────────────────────────────────────
def test_flag_off_restores_rows_only_banner(monkeypatch):
    """BRAIN_STATUS_RESOLVER_RECONCILE=OFF -> the probe rows alone decide (pre-fix behavior)."""
    monkeypatch.setattr(bh, "BRAIN_STATUS_RESOLVER_RECONCILE", False)
    monkeypatch.setattr(bh, "read_probe_state", lambda uid: _state(bh.STATE_NO_BRAIN))
    monkeypatch.setattr(bh, "_turn_lane_brain_resolves", lambda uid: True)
    notice = bh.degradation_notice("user-abcd1234")
    assert notice and "No model endpoint" in notice


def test_sentinel_resolution_does_not_silence_the_setup_banner(monkeypatch):
    """resolve_brain returning the NO-BRAIN SENTINEL is NOT a resolution — the setup banner
    stays (the seat really has nothing)."""
    monkeypatch.setattr(bh, "BRAIN_STATUS_RESOLVER_RECONCILE", True)
    monkeypatch.setattr(bh, "read_probe_state", lambda uid: _state(bh.STATE_NO_BRAIN))
    monkeypatch.setattr(bh, "_turn_lane_brain_resolves", lambda uid: False)
    notice = bh.degradation_notice("user-abcd1234")
    assert notice and "No model endpoint" in notice


def test_resolver_probe_is_read_only_and_fail_safe(monkeypatch):
    """_turn_lane_brain_resolves: real cfg -> True; sentinel/None -> False; error -> False;
    no owning account -> False (control-plane seats keep today's banner)."""
    import saas.llm_context as lc
    import saas.seat_brain as sb

    monkeypatch.setattr(lc, "_account_for_user", lambda uid: "acct-1")
    monkeypatch.setattr(sb, "resolve_brain", lambda uid, acct: {"base_url": "https://z.ai"})
    assert bh._turn_lane_brain_resolves("user-abcd1234") is True

    monkeypatch.setattr(sb, "resolve_brain", lambda uid, acct: {"no_brain": True})
    assert bh._turn_lane_brain_resolves("user-abcd1234") is False

    monkeypatch.setattr(sb, "resolve_brain", lambda uid, acct: None)
    assert bh._turn_lane_brain_resolves("user-abcd1234") is False

    def _raise(uid, acct):
        raise RuntimeError("db down")
    monkeypatch.setattr(sb, "resolve_brain", _raise)
    assert bh._turn_lane_brain_resolves("user-abcd1234") is False

    monkeypatch.setattr(lc, "_account_for_user", lambda uid: None)
    monkeypatch.setattr(sb, "resolve_brain", lambda uid, acct: {"base_url": "x"})
    assert bh._turn_lane_brain_resolves("user-abcd1234") is False
