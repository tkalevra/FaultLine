"""LLM call LANE, and the difference between "answered nothing" and "never asked".

TWO IDEAS, ONE SMALL MODULE
───────────────────────────

**1. LLMUnavailable — a call that DID NOT HAPPEN.**

Every LLM helper in this codebase returns ``None`` on failure. That is fine for a caller
that simply retries later, and quietly wrong for a caller that CACHES A VERDICT, because
``None`` collapses two very different facts into one value:

  * the model was asked and had no answer  → real evidence, safe to record; and
  * the model was never reached at all     → no evidence whatsoever.

The re-embedder's ontology-growth lanes are the motivating case. They ask the model to
classify a concept, and on ``None`` they write a verdict into ``climb_state`` — which the
cache then HONOURS on an unchanged fingerprint, so the concept is never re-attempted. A
transient outage therefore writes PERMANENT conclusions the model never supplied, and burns
each concept's retry budget on the way. ``LLMUnavailable`` (opt-in, via
``raise_on_unavailable=True``) makes the didn't-happen case impossible to mistake for an
answer, so those callers can record nothing and simply try again on a later sweep.

**2. The lane — is a user waiting on this call?**

A pacing layer that must never lose a user's request will, correctly, let a call through
rather than block it forever. That is right for an interactive call and wrong for
deferrable upkeep: a background sweep firing into a provider that is already refusing
requests turns a rate limit into a flood. The lane lets a pacing layer tell the two apart —
INTERACTIVE keeps whatever fail-open policy it has, BACKGROUND yields and defers, so upkeep
consumes only the capacity interactive traffic left behind. No priority queue to tune.

The lane is a property of the CALLER, never derived from an operation-name list — such a
list is brittle by construction and silently mis-lanes every operation added after it was
written. A background process declares itself once at start-up; genuinely user-driven work
inside that process re-enters the interactive lane for its own scope.

FAIL-SAFE
─────────
Unset or unreadable → INTERACTIVE, which is the unchanged default behaviour. A mis-wired
lane can only ever degrade to "same as before", never to "stops calling the model".
"""

from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from typing import Iterator

# The two lanes. INTERACTIVE = a user is waiting on this call. BACKGROUND = deferrable
# upkeep that must yield to interactive traffic.
LANE_INTERACTIVE = "interactive"
LANE_BACKGROUND = "background"

_VALID_LANES = (LANE_INTERACTIVE, LANE_BACKGROUND)

# Per-context override. Unset → fall back to the PROCESS default (env), then interactive.
_lane: ContextVar[str] = ContextVar("fl_llm_lane", default="")


def _process_default_lane() -> str:
    """The lane this PROCESS runs in when nothing narrower is bound.

    Set by a background worker's entrypoint (``FL_LLM_DEFAULT_LANE=background``) so every
    call it makes is deferrable without threading a flag through dozens of call sites.
    Anything unrecognised → interactive (the unchanged default).
    """
    raw = str(os.environ.get("FL_LLM_DEFAULT_LANE", "") or "").strip().lower()
    return raw if raw in _VALID_LANES else LANE_INTERACTIVE


def current_lane() -> str:
    """The lane in force for the call being made right now. Never raises."""
    try:
        bound = _lane.get()
    except Exception:  # pragma: no cover - ContextVar cannot normally fail
        return LANE_INTERACTIVE
    if bound in _VALID_LANES:
        return bound
    return _process_default_lane()


def is_background() -> bool:
    """True when the current call is deferrable upkeep (must not fail open)."""
    return current_lane() == LANE_BACKGROUND


def set_process_default(lane: str) -> None:
    """Declare the whole process's default lane (call once, at start-up).

    Writes the env var so the default survives into threads/tasks that never inherit this
    ContextVar — background sweeps commonly run on worker threads.

    NOTE for anyone verifying a deploy: because this is a RUNTIME write, it does NOT appear
    in ``/proc/<pid>/environ``, which only ever shows the environment a process was STARTED
    with. Confirm it from a start-up log line or a behavioural probe, never from /proc.
    """
    if lane in _VALID_LANES:
        os.environ["FL_LLM_DEFAULT_LANE"] = lane


@contextlib.contextmanager
def use_lane(lane: str) -> Iterator[None]:
    """Bind a lane for the enclosed scope. Unknown lane → leaves the current one alone.

    The inverse direction matters as much as the forward one: a BACKGROUND process doing
    work a user is actually waiting on should wrap it in ``use_lane(LANE_INTERACTIVE)`` so
    that work is not deferred.
    """
    if lane not in _VALID_LANES:
        yield
        return
    token = _lane.set(lane)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            _lane.reset(token)


# ── CROSSING HTTP ────────────────────────────────────────────────────────────
# The lane is a property of the CALLER, but the re-embedder routes part of its
# sweep through the API's own HTTP endpoints (/extract/rewrite, /harvest-spans):
# those LLM calls execute in the API process, where the ContextVar cannot follow.
# Without an explicit hop the traffic arrives as INTERACTIVE and fail-opens under
# pressure — background sweep volume wearing the interactive lane's protection.
# The SENDER stamps its lane in a header; the RECEIVER re-enters that lane for the
# request's duration. Unset/invalid header → interactive (a human client owes us
# nothing and must never be deferred by default).

LANE_HEADER = "X-FL-LLM-Lane"


def lane_from_header(value: str | None) -> str:
    """Validate an inbound lane header. Anything unrecognised → INTERACTIVE."""
    raw = str(value or "").strip().lower()
    return raw if raw in _VALID_LANES else LANE_INTERACTIVE


class LLMUnavailable(RuntimeError):
    """The call DID NOT HAPPEN — the model was never asked.

    Raised only when a caller opts in with ``raise_on_unavailable=True``. See the module
    docstring: this exists so a verdict-caching caller cannot mistake an unreachable model
    for a model that answered nothing.

    ``reason`` is one of ``rate_deferred`` | ``retries_exhausted`` | ``breaker_open``.
    """

    def __init__(self, reason: str = "unavailable", operation: str = "") -> None:
        self.reason = reason
        self.operation = operation
        super().__init__(f"llm unavailable ({reason})"
                         + (f" for {operation}" if operation else ""))
