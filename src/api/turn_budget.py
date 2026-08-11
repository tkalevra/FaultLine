"""TURN BUDGET — a wall-clock deadline that travels with the request, not with the call.

THE CORRECTION THIS MODULE EXISTS FOR
-------------------------------------
On 2026-08-10 a measured recall sample (n=30, one seat, local stack) came back:

    p50 = 0.58s      p90 = 12.44s     p95 = 12.86s     max = 19.81s

against a 2s customer-facing budget. The distribution was NOT random and NOT the
"slow model" story. It was bimodal by QUERY CLASS:

  * a query whose path RESOLVES  ("who is my manager", "where do I live")  → 0.08-0.79s
  * a query whose path does NOT  ("what are my preferences")               → 3.5-19.8s

The whole gap was ONE call. ``determine_path`` fires ``_aspect_map_via_llm`` INLINE for
each unresolved aspect word — ``_ASPECT_MAX_INLINE_WORDS`` (2) x ``_ASPECT_INLINE_TIMEOUT_S``
(8s) = **16 seconds of LLM on the read path**. Worse, it is ``call_llm_no_retry_sync`` — a
SYNCHRONOUS call inside an ``async def`` FastAPI handler, so it blocks the EVENT LOOP: every
other tenant's turn queues behind it. And worst of all, on breach the result is DISCARDED —
the code falls straight through to ``_aspect_record_miss``, the async backstop that would
have handled the word anyway. The 16 seconds bought nothing.

A per-call timeout cannot fix this. 8s is a perfectly reasonable bound for ONE call and a
catastrophic one for a turn that must answer in 2s — and two of them are reasonable
individually while being 16s together. The bound has to live on the TURN, be shared by every
call the turn makes, and be consulted BEFORE a call is dialled rather than after it hangs.

WHAT THIS IS
------------
An ambient, monotonic wall-clock budget on a ``ContextVar``. ``/query`` opens one; anything
downstream — however deep, sync or async — asks ``remaining()`` before spending. A call that
does not fit is not attempted: it is ABANDONED to the staging path that already exists.

Ambient by DELIBERATE choice. Threading a deadline parameter through the read path would mean
touching a 44k-line module and every call site between the handler and the brain; every site
missed is a silent unbounded hole. A ContextVar is inherited by ``asyncio`` tasks and read
correctly from ``to_thread`` workers, so one ``open_budget()`` at the door governs everything
beneath it.

FAIL-OPEN, ALWAYS. No budget open (a worker, a test, a background sweep, a CLI) →
``remaining()`` is ``inf`` and ``can_afford()`` is ``True``. Off the customer's hot path there
IS no deadline to enforce, and this module must never be the reason batch work stops working.
That is why the default is unbounded rather than zero: the failure mode of a mis-wired budget
is "behaves exactly like today", never "silently stops calling the brain".
"""

from __future__ import annotations

import contextvars
import os
import time
from contextlib import contextmanager
from typing import Optional

# The customer-facing p95 target. Every inline brain call on a budgeted turn is measured
# against what is LEFT of this, never against its own private timeout.
TURN_BUDGET_S = float(os.environ.get("TURN_BUDGET_S", "2.0"))

# Refuse to dial a call that cannot plausibly finish. A brain call that returns in under
# ~250ms is not a real round-trip to a hosted endpoint, so a budget thinner than this buys
# a near-certain timeout — we would pay the latency AND still take the staged path. Skipping
# straight to staging is strictly faster and reaches the same end state.
MIN_VIABLE_CALL_S = float(os.environ.get("TURN_MIN_VIABLE_CALL_S", "0.25"))

_budget: contextvars.ContextVar[Optional["_Budget"]] = contextvars.ContextVar(
    "faultline_turn_budget", default=None
)


class _Budget:
    """A monotonic deadline. ``time.monotonic`` deliberately — a turn budget must not be
    perturbed by NTP/clock steps the way ``time.time`` can be."""

    __slots__ = ("started", "total", "label", "skipped", "spent_calls")

    def __init__(self, total: float, label: str) -> None:
        self.started = time.monotonic()
        self.total = total
        self.label = label
        # Observability: what the budget ACTUALLY refused this turn. A deadline you cannot
        # see working is indistinguishable from one that never fires.
        self.skipped: list[str] = []
        self.spent_calls = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> float:
        return self.total - self.elapsed()


@contextmanager
def open_budget(total_s: Optional[float] = None, label: str = "turn"):
    """Open a turn budget for everything executed inside the block.

    Re-entrant by NESTING, not by replacement: an inner block never widens an outer deadline.
    If a budget is already open, the inner one inherits ``min(remaining, requested)`` so a
    sub-operation can only ever be STRICTER than the turn that contains it. Without that, a
    nested ``open_budget(2.0)`` late in an already-1.9s-old turn would hand out a fresh 2s and
    the outer guarantee would silently evaporate.
    """
    requested = TURN_BUDGET_S if total_s is None else float(total_s)
    outer = _budget.get()
    if outer is not None:
        requested = min(requested, max(0.0, outer.remaining()))
    b = _Budget(requested, label)
    token = _budget.set(b)
    try:
        yield b
    finally:
        _budget.reset(token)


def current() -> Optional[_Budget]:
    return _budget.get()


def remaining() -> float:
    """Seconds left on the turn. ``inf`` when unbudgeted (off the hot path)."""
    b = _budget.get()
    if b is None:
        return float("inf")
    return b.remaining()


def expired() -> bool:
    return remaining() <= 0.0


def can_afford(cost_s: float, *, what: str = "") -> bool:
    """May this turn spend ``cost_s`` on a brain call?

    This is the whole point of the module, so it is deliberately conservative: we ask whether
    the call fits in what is LEFT, not whether it fits in the budget. A turn that has already
    spent 1.8s of 2s cannot afford an 8s call, and must not start one "just in case it is
    fast this time" — that is precisely the reasoning that produced a 19.81s p-max.

    Unbudgeted → always True (background lanes keep today's behaviour exactly).
    """
    b = _budget.get()
    if b is None:
        return True
    left = b.remaining()
    if left < MIN_VIABLE_CALL_S or cost_s > left:
        if what:
            b.skipped.append(what)
        return False
    b.spent_calls += 1
    return True


def clamp_timeout(desired_s: float) -> float:
    """The timeout a call may actually use: never longer than the turn has left.

    Use together with ``can_afford`` — that decides WHETHER to dial, this decides for HOW
    LONG. A call permitted with 0.9s left gets a 0.9s socket timeout, not its stock 8s, so it
    cannot overrun the turn it was admitted into.

    NEVER RETURNS ZERO. ``httpx``/``requests``/``socket`` variously read ``timeout=0`` as
    "non-blocking" or "no timeout at all" — and "no timeout" is the precise opposite of what
    an exhausted budget should produce. A budget at zero that handed back an UNBOUNDED call
    would invert this whole module. Callers are expected to gate on ``can_afford`` first (it
    refuses anything under ``MIN_VIABLE_CALL_S``); this floor exists so that a caller who
    forgets still cannot manufacture an unbounded call.
    """
    b = _budget.get()
    if b is None:
        return desired_s
    return max(MIN_VIABLE_CALL_S, min(desired_s, b.remaining()))


def report() -> dict:
    """Structured close-out for the turn's log line — what the deadline actually did."""
    b = _budget.get()
    if b is None:
        return {"budgeted": False}
    return {
        "budgeted": True,
        "label": b.label,
        "budget_s": round(b.total, 3),
        "elapsed_s": round(b.elapsed(), 3),
        "remaining_s": round(b.remaining(), 3),
        "over_budget": b.remaining() < 0,
        "brain_calls_admitted": b.spent_calls,
        "brain_calls_skipped": len(b.skipped),
        "skipped": b.skipped[:8],
    }
