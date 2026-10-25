"""LLM call LANE — is this call serving a user right now, or is it deferrable upkeep?

WHY THIS EXISTS
───────────────
The rate limiter's fail-open ("a wedged limiter must never hang a call forever —
proceed and log_crit") is CORRECT for a call a user is waiting on: losing their
request is worse than briefly exceeding a provider's pace. It is exactly WRONG for
the re_embedder's growth sweep, which is deferrable by construction — there is no
user waiting, and firing anyway converts a rate limit into a flood.

Measured on prod 2026-08-11 (Cerebras Free tier, 5 RPM = one token per 12s,
LLM_RATE_MAX_WAIT_S=30): any queue deeper than ~2 calls exceeds the cap and fails
open BY CONSTRUCTION. 38 `llm_rate_limit.wedged_failopen` in one hour against 3
`bucket_paused`, with the busiest minutes running 16/11/10 outbound attempts against
a ceiling of 5. The sweep was not merely rate-limited; it was structurally unpaceable.

THE RULE THIS ENCODES
─────────────────────
User input SUPERSEDES upkeep. An INTERACTIVE call keeps today's fail-open, so a user
is never made to lose a request. A BACKGROUND call must WAIT for real capacity and,
if there isn't any within the cap, DEFER — it does not fire. Background work therefore
consumes only the capacity a user's calls left behind, automatically, with no priority
queue to tune and no operation-name list to maintain.

SUBJECT-AGNOSTIC BY DESIGN
──────────────────────────
The lane is NOT derived from an operation-name zoo (`if operation == "CLASSIFY_CHAIN"`)
— that is the brittleness this repo forbids, and it would silently mis-lane every new
operation. It is a property of the CALLER: the re_embedder process declares itself
background once at start-up, and any lane that is genuinely user-driven (e.g. draining
a document a user just uploaded) opts back into interactive for that scope.

FAIL-SAFE
─────────
Unset / unreadable → INTERACTIVE, which is today's behaviour byte-for-byte. A mis-wired
lane can only ever degrade to "same as now", never to "stops calling the brain".
"""

from __future__ import annotations

import contextlib
import os
import time
from contextvars import ContextVar
from typing import Any, Iterator, Optional

# The two lanes. INTERACTIVE = a user is waiting on this call. BACKGROUND = deferrable
# upkeep that must yield to interactive traffic.
LANE_INTERACTIVE = "interactive"
LANE_BACKGROUND = "background"

_VALID_LANES = (LANE_INTERACTIVE, LANE_BACKGROUND)

# Per-context override. Unset → fall back to the PROCESS default (env), then interactive.
_lane: ContextVar[str] = ContextVar("fl_llm_lane", default="")


def _process_default_lane() -> str:
    """The lane this PROCESS runs in when nothing narrower is bound.

    Set by the re_embedder entrypoint (``FL_LLM_DEFAULT_LANE=background``) so every
    call it makes is deferrable without threading a flag through ~40 call sites.
    Anything unrecognised → interactive (today's behaviour).
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

    Writes the env var so the default survives into threads/tasks that never inherit
    this ContextVar — the re_embedder runs its sweeps on worker threads.
    """
    if lane in _VALID_LANES:
        os.environ["FL_LLM_DEFAULT_LANE"] = lane


# The lane travels with the CALLER — including across an HTTP hop. The re_embedder process
# declares itself background once at start-up, but its LLM-bearing work (extraction,
# harvest) executes in the API process via /extract/rewrite and /harvest-spans, where that
# declaration cannot reach: the lane is a ContextVar + process env var, neither of which
# crosses a socket. Without this header the sweep's calls ride the API's INTERACTIVE lane
# and fail OPEN after LLM_RATE_MAX_WAIT_S — the exact uncapped-fire shape
# (`llm_rate_limit.wedged_failopen`) this module exists to prevent on deferrable work.
# The API binds the lane from this header for the duration of the request (one middleware).
# Fail-safe direction: absent/unknown → the receiving side changes nothing (interactive).
LANE_HEADER = "x-fl-llm-lane"


def lane_from_raw(value: Any) -> Optional[str]:
    """Validate a raw header/env value as a lane name. Unknown/empty/garbage → None.

    The receiving side must treat None as "no opinion" and leave the lane untouched —
    never map garbage onto a lane, and never let a client-set value widen privileges:
    INTERACTIVE is not privileged (it keeps the fail-open, which is the LEAST protective
    setting for the provider), so a spoofed header can only make the caller's own
    background work defer MORE, never less.
    """
    raw = str(value or "").strip().lower()
    return raw if raw in _VALID_LANES else None


@contextlib.contextmanager
def use_lane(lane: str) -> Iterator[None]:
    """Bind a lane for the enclosed scope. Unknown lane → leaves the current one alone.

    The inverse direction matters as much as the forward one: a BACKGROUND process
    draining a document a user just uploaded should wrap that work in
    ``use_lane(LANE_INTERACTIVE)`` so genuinely user-driven work is not deferred.
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


class LLMUnavailable(RuntimeError):
    """The call DID NOT HAPPEN — the brain was never asked.

    Raised (opt-in, ``raise_on_unavailable=True``) when the request was deferred for
    capacity or every attempt was exhausted without a settled answer. This exists to
    kill a specific and expensive conflation: every LLM helper in this repo returns
    ``None`` both when the model answered "no" and when the model was never reached,
    so callers that cache a verdict were recording DECISIONS DERIVED FROM NO
    INFORMATION and burning the concept's retry budget while doing it.

    ``reason`` is one of ``rate_deferred`` | ``retries_exhausted`` | ``breaker_open``.
    """

    def __init__(self, reason: str = "unavailable", operation: str = "") -> None:
        self.reason = reason
        self.operation = operation
        super().__init__(f"llm unavailable ({reason})"
                         + (f" for {operation}" if operation else ""))


# ──────────────────────────────────────────────────────────────────────────────
# INTERACTIVE TURN BUDGET (Deliverable D — additive, do not touch the lane
# primitives above; they are load-bearing)
# ──────────────────────────────────────────────────────────────────────────────
# WHY A SECOND BUDGET ContextVar (and not a reuse of src/api/turn_budget.py)
# -----------------------------------------------------------------------
# ``turn_budget.py`` already implements an ambient monotonic deadline on a ContextVar
# for the /query READ path, with the right shape (nestable, fail-open-to-inf, monotonic).
# The ingest write path needs the SAME shape but on a SEPARATE var: a /query turn's
# deadline and an ingest turn's deadline are different requests on different code paths,
# and a budget set on one must not bleed into the other. Reusing the shape (not the
# variable) is the deliberate choice — copy the proven contract, bind it to a different
# ContextVar so the two paths stay independent.
#
# FAIL-OPEN, ALWAYS — same as ``turn_budget``: unset → ``turn_budget_remaining()`` is
# ``inf`` and admission falls through to today's behaviour. The failure mode of a
# mis-wired budget is "behaves exactly like today", never "silently defers every turn".
INTERACTIVE_TURN_BUDGET_S = float(os.environ.get("INTERACTIVE_TURN_BUDGET_S", "4.0"))

# The owner's "couple of seconds" bar (spec §4). Default 4.0 leaves a tail reserve for the
# HTTP transport after admission + extraction + ingest round out the call.

_turn_deadline: ContextVar[Optional[float]] = ContextVar(
    "fl_interactive_turn_deadline", default=None
)


def set_turn_budget_deadline(monotonic_deadline: Optional[float]) -> None:
    """Bind the per-request monotonic deadline directly (the helper behind
    ``open_turn_budget``). Pass None to clear. Use ``open_turn_budget`` unless you have a
    specific reason to set the raw value (e.g. constructing a deadline from an externally
    measured starting point). Never raises."""
    _turn_deadline.set(monotonic_deadline)


@contextlib.contextmanager
def open_turn_budget(budget_s: float):
    """Bind an interactive turn budget for the enclosed scope.

    Re-entrant by NESTING, not by replacement: an inner block inherits
    ``min(remaining_outer, requested)`` so it can only ever be STRICTER than the turn
    that contains it. Mirrors ``turn_budget.open_budget``'s contract verbatim — without
    that, a nested ``open_turn_budget(2.0)`` late in an already-3.9s-old turn would hand
    out a fresh 2s and the outer guarantee would silently evaporate."""
    requested = float(budget_s)
    outer = _turn_deadline.get()
    now = time.monotonic()
    if outer is not None:
        requested = min(requested, max(0.0, outer - now))
    deadline = now + requested
    token = _turn_deadline.set(deadline)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            _turn_deadline.reset(token)


def turn_budget_remaining() -> float:
    """Seconds left on the interactive turn budget.

    ``inf`` when unbudgeted (off the hot path → no deadline to enforce, today's
    behaviour). Negative clamped to 0 so a caller comparing ``remaining <= 0`` is not
    surprised by a tiny negative from clock skew."""
    d = _turn_deadline.get()
    if d is None:
        return float("inf")
    return max(0.0, d - time.monotonic())
