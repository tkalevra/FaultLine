"""A wait that outlives its caller is not patience — it is a hang.

MEASURED ON PRODUCTION (2026-08-19, owner's seat, z.ai returning HTTP 429):

    cortex_recall   30.20s, 30.27s   -> exactly LLM_RATE_MAX_WAIT_S, then fail-open
    recall_memory   70.22s x4        -> base cap + pause extension, killed by the 60s HTTP timeout

Nothing was computing slowly. Both calls were sleeping in 0.25s slices (`_RATE_MAX_SLICE`)
waiting for a token that could not mint, because a provider 429 publishes a shared hard pause
and `pause_for` extends it with max() semantics on every fresh 429 — so on a persistently
rate-limited key the pause never lifts.

Two things were wrong, and both are fixed here:
  * `_extend_cap_for_hold` extended the wait to `_RATE_PAUSE_EXTENSION_S` (300s) regardless of
    what the caller said it could wait, so a caller holding a 60s HTTP timeout was parked for
    five minutes it did not have;
  * and even inside its own budget, a caller slept toward a deadline it could already prove it
    would not meet.

A MAINTENANCE hold stays exempt: it is user-initiated, short, and the UI that starts it promises
other LLM work will pause, so waiting it out is the promised behaviour, not a futile bet.
"""
import time

import pytest

from src.api import llm_calls


def _bucket(rate=0.0, capacity=0.0):
    b = llm_calls._TokenBucket(rate=rate, capacity=capacity)
    b.tokens = 0.0
    return b


def test_a_provider_pause_longer_than_the_budget_returns_at_once():
    """The production case: 300s of pause, a caller that can afford 30s."""
    b = _bucket()
    b.pause_until = time.monotonic() + 300.0
    started = time.monotonic()
    proceeded = b.acquire_sync(max_wait=30.0)
    elapsed = time.monotonic() - started
    assert proceeded is True, "an interactive caller must still fail-open, not be refused"
    assert elapsed < 1.0, (
        f"parked {elapsed:.1f}s on a pause it could never outlast — the whole defect")


def test_the_same_pause_defers_a_background_caller_at_once():
    """The deferrable lane answers 'skip', and must reach that answer just as fast."""
    b = _bucket()
    b.pause_until = time.monotonic() + 300.0
    started = time.monotonic()
    proceeded = b.acquire_sync(max_wait=30.0, deferrable=True)
    elapsed = time.monotonic() - started
    assert proceeded is False, "a background caller should defer under a provider pause"
    assert elapsed < 1.0, f"background caller parked {elapsed:.1f}s before deferring"


@pytest.mark.asyncio
async def test_the_async_twin_behaves_identically():
    """Fixing one acquire loop and missing its twin is this repo's recurring mistake."""
    b = _bucket()
    b.pause_until = time.monotonic() + 300.0
    started = time.monotonic()
    proceeded = await b.acquire_async(max_wait=30.0)
    elapsed = time.monotonic() - started
    assert proceeded is True
    assert elapsed < 1.0, f"the async loop still parks: {elapsed:.1f}s"


def test_a_pause_that_fits_inside_the_budget_is_still_waited_out():
    """The control, and the reason this is not simply 'never wait'. A pause about to lift is
    worth waiting for — returning early there would trade a working call for a failed one."""
    b = _bucket(rate=1000.0, capacity=1000.0)
    b.tokens = 0.0
    b.pause_until = time.monotonic() + 0.4
    started = time.monotonic()
    proceeded = b.acquire_sync(max_wait=30.0)
    elapsed = time.monotonic() - started
    assert proceeded is True
    assert elapsed >= 0.3, (
        f"returned in {elapsed:.2f}s — a short pause must be waited out, not skipped")


def test_the_provider_pause_extension_never_exceeds_the_callers_budget():
    """`_extend_cap_for_hold` used to grant up to 300s no matter what the caller asked for."""
    b = _bucket()
    b.pause_until = time.monotonic() + 300.0
    assert b._extend_cap_for_hold(max_wait=30.0, cap=30.0) <= 30.0, (
        "a provider pause extended the wait past the caller's stated budget")


def test_a_maintenance_hold_may_still_extend_and_is_waited_out(monkeypatch):
    """Maintenance is deliberate, short, and promised — it keeps the old behaviour."""
    b = _bucket()
    # `_TokenBucket` uses __slots__, so the real field is set rather than the method patched —
    # which is the more honest fixture anyway: it drives the same state the acquire loop reads.
    #
    # A PROVIDER PAUSE IS SET TOO, ON PURPOSE. An earlier version of this test set only
    # `maint_until`, and a mutation that deleted the maintenance guard entirely still passed:
    # with no pause in play the predicate fell through to "no pause, not futile" and returned
    # False for the wrong reason. The two holds have to be present TOGETHER for this to pin
    # which one the exemption is keyed on.
    b.maint_until = time.monotonic() + 120.0
    b.pause_until = time.monotonic() + 300.0
    assert b._maint_remaining() > 0, "fixture did not actually open a maintenance window"
    assert b._pause_remaining() > 0, "fixture did not actually set a provider pause"
    assert b._extend_cap_for_hold(max_wait=30.0, cap=30.0) > 30.0, (
        "a maintenance window must still be able to hold a caller past its base cap")
    assert b._pause_outlasts_budget(cap=30.0, waited=0.0) is False, (
        "maintenance must not be treated as a futile provider pause")


def test_the_escape_hatch_restores_the_old_parking_behaviour(monkeypatch):
    """An operator can opt back in; the DEFAULT is what changed."""
    monkeypatch.setenv("LLM_RATE_PARK_PAST_BUDGET", "true")
    b = _bucket()
    b.pause_until = time.monotonic() + 300.0
    assert b._pause_outlasts_budget(cap=30.0, waited=0.0) is False
    assert b._extend_cap_for_hold(max_wait=30.0, cap=30.0) > 30.0
