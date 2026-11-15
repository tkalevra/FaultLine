"""TRUTHLIE adjacent — the clamp path must LOG, not CRASH.

The completion-clamped log call passed effective_max_tokens BOTH as an explicit kwarg
and inside **plan.as_log() (which already carries it) — a duplicate-keyword TypeError
that crashed _context_preflight for every clamped call. The branch was unreachable for
small operations before the operation-relative floor (they were refused instead), so
the crash sat latent; the floor fix makes clamping the COMMON outcome for tiny ops
near the edge, and the first padded small call to clamp took the whole call down.

Pinned here end-to-end through llm_calls._context_preflight — the seam the wire
actually uses — not just context_window.plan_call.
"""
import pytest

from src.api import context_window as cw
from src.api import llm_calls as LC


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("LLM_CONTEXT_GUARD", "LLM_CONTEXT_WINDOW", "LLM_CONTEXT_PROBE",
              "LLM_CONTEXT_MIN_COMPLETION_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_CONTEXT_PROBE", "false")
    cw.reset_state()
    cw.reset_counters()
    yield
    cw.reset_state()
    cw.reset_counters()


def _edge_plan(monkeypatch, room=63, max_tokens=1040, operation="INTENT_ADJUDICATION"):
    window = 10_000
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", str(window))
    monkeypatch.setattr(cw, "estimate_tokens", lambda m: window - room)
    msgs = [{"role": "user", "content": "x"}]
    eff, block = LC._context_preflight(msgs, max_tokens, operation, None, "test")
    return eff, block


def test_clamped_call_returns_the_clamped_budget_instead_of_crashing(monkeypatch):
    """THE CRASH: a padded small op at the edge clamps; the log line used to raise
    TypeError (duplicate effective_max_tokens) and took the whole call with it. The
    preflight must return the clamped budget and NO block."""
    eff, block = _edge_plan(monkeypatch)
    assert block is None, "a clamped call is a SENT call, never a block"
    assert eff == 63, "the effective budget must be the room the window actually has"


def test_unclamped_call_is_untouched(monkeypatch):
    """Control: with room to spare the preflight is a pass-through."""
    eff, block = _edge_plan(monkeypatch, room=5000, max_tokens=1040)
    assert block is None and eff == 1040


def test_the_clamp_event_is_counted_not_just_survived(monkeypatch):
    """The clamp must be OBSERVABLE: the guard's counter fires (a silent clamp is how
    the crash hid for as long as it did)."""
    _edge_plan(monkeypatch)
    counters = cw.counters()
    assert any(k.startswith(cw.EV_COMPLETION_CLAMPED) for k in counters), (
        f"expected a completion-clamped counter, got {counters}")
