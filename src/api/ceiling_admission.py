"""CEILING ADMISSION — replace the INTERACTIVE lane's fail-open with a bounded admit decision.

THE PROBLEM (spec DEV/DESIGN-brain-budget-event-cache.md §4)
------------------------------------------------------------
Today an INTERACTIVE LLM call (a user ingest turn) fail-OPENS at LLM_RATE_MAX_WAIT_S=30s. On
a learned ceiling of 5 RPM (one token / 12s) any queue deeper than ~2 calls exceeds the cap
inside the wait window, so the limiter "succeeds" (returns True), the call proceeds UN-PACED,
the brain 429s, and the harvest degrades to one edge instead of what the person said. The
verbatim turn is captured but the structured fact is lost. The 30s wait is also 15x the user's
"couple of seconds" bar — the very thing the fail-open was protecting against (long waits) IS
what it produces on a low ceiling.

THE REPLACEMENT — a bounded admission decision made BEFORE the extraction call
-----------------------------------------------------------------------------
  acquire_interactive(operation, budget_s) -> "proceed" | "defer"

  • "proceed" → run the FULL extraction pipeline now, exactly as today. The rate limiter
                takes the token as part of the call.
  • "defer"   → the caller has ALREADY captured the turn verbatim (the floor — see the
                server.py ingest seam). Hand the raw text to the deferred statement-extraction
                drain so the SAME pipeline runs when capacity returns, with source="mcp"
                preserved → fact_provenance="user_stated" (the explicit non-regression on
                the reextract_episodic residual that demotes to llm_inferred).

THE DECISION TABLE (FAIL-SAFE TO "proceed" ON ANY UNKNOWN)
---------------------------------------------------------
  brain scope not resolvable (SAAS off / FOSS / no cfg)      → proceed
  brain_available_tokens probe absent (Deliverable C unwired)→ proceed
  tokens >= 1.0 (a token is free NOW)                        → proceed
  tokens < 1.0 AND rate known AND
      (1.0 - tokens) / rate <= budget_s                      → proceed  (token mints in budget)
  tokens < 1.0 AND (rate unknown OR mint would exceed budget)→ defer    (POSITIVE knowledge
                                                                          that capacity won't arrive)
  ANY exception in the probe                                 → proceed

The defer is taken ONLY when we POSITIVELY know capacity will not arrive in budget. Every
unknown returns "proceed" — preserving today's behaviour where Deliverable C's probes are
not wired, and on FOSS / SAAS-off where no shared brain bucket exists. The only thing worse
than fail-open is breaking the call entirely.

INTERACTIVE-IN-FLIGHT COUNTER
-----------------------------
``interactive_inflight_incr/decr/count`` tag "an interactive call is in flight on this brain"
so the re-embedder supervisor (Deliverable C) can defer BACKGROUND work while a user turn is
live on the same brain. This module owns the counter because it serves the admission decision
primarily; Deliverable C reads it via the same Redis key. Redis-scoped via
``redis_coord.key("intinf", scope)``; TTL-bumped on incr so a stuck holder's tag cannot
accumulate; fail-safe to 0 on any error.
"""

from __future__ import annotations

from typing import Optional

try:  # pragma: no cover - import guard
    from src.api import redis_coord
except Exception:  # pragma: no cover
    redis_coord = None  # type: ignore


_NS_INFLIGHT = "intinf"
_INFLIGHT_TTL_S = 60.0   # a stuck holder's tag should expire, not accumulate


def _brain_scope():
    """The resolved brain scope for the current call, or None. Never raises.

    Reuses the ONE source of truth from ``llm_calls`` so admission sees the same scope the
    rate budget, the circuit breaker, and the re-embedder supervisor see. Returns None when
    SAAS is off / no cfg / the no-brain sentinel / any error — the caller falls back to
    today's behaviour (no shared opinion → proceed)."""
    try:
        from src.api import llm_calls
        return llm_calls._resolve_brain_scope()
    except Exception:
        return None


# ── Interactive in-flight counter (Redis-scoped, fail-safe to 0) ──────────────

def interactive_inflight_incr(scope) -> int:
    """Mark an interactive call in-flight on this brain. Returns the new count, or 0 on any
    error / when shared coordination is unavailable. TTL-bumped so a holder that crashed
    without decrementing cannot accumulate forever."""
    if redis_coord is None or scope is None:
        return 0
    c = redis_coord.client()
    if c is None:
        return 0
    try:
        k = redis_coord.key(_NS_INFLIGHT, scope, "n")
        n = int(c.incr(k))
        c.expire(k, int(_INFLIGHT_TTL_S))
        return n
    except Exception:
        return 0


def interactive_inflight_decr(scope) -> int:
    """Mark an interactive call as no longer in-flight. Never goes below 0. Returns the new
    count, or 0 on any error."""
    if redis_coord is None or scope is None:
        return 0
    c = redis_coord.client()
    if c is None:
        return 0
    try:
        k = redis_coord.key(_NS_INFLIGHT, scope, "n")
        n = int(c.decr(k))
        if n < 0:  # a decrement without a matching incr (crash recovery) — clamp at 0
            c.set(k, "0")
            n = 0
        return n
    except Exception:
        return 0


def interactive_inflight_count(scope) -> int:
    """How many interactive calls are in flight on this brain right now. 0 on any error or
    when Redis / shared coordination is unavailable."""
    if redis_coord is None or scope is None:
        return 0
    c = redis_coord.client()
    if c is None:
        return 0
    try:
        k = redis_coord.key(_NS_INFLIGHT, scope, "n")
        raw = c.get(k)
        return int(raw) if raw else 0
    except Exception:
        return 0


# ── Admission decision ────────────────────────────────────────────────────────

def _probe_tokens_now(scope) -> Optional[float]:
    """Available tokens on this brain's bucket RIGHT NOW.

    None when Deliverable C's ``brain_available_tokens`` probe is not wired / not enabled /
    any error — fail-safe to "no shared opinion" (the caller returns "proceed")."""
    if redis_coord is None or scope is None:
        return None
    fn = getattr(redis_coord, "brain_available_tokens", None)
    if fn is None:
        return None
    try:
        v = fn(scope)
    except Exception:
        return None
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _probe_rate_per_sec(scope) -> Optional[float]:
    """The brain bucket's refill rate (tokens/sec). None when unknown.

    Used to compute how long until the next token mints: ``(1.0 - tokens) / rate``."""
    if redis_coord is None or scope is None:
        return None
    fn = getattr(redis_coord, "brain_rate_per_sec", None)
    if fn is None:
        return None
    try:
        v = fn(scope)
    except Exception:
        return None
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _brain_cfg_max_rpm() -> Optional[float]:
    """The brain's CONFIGURED ceiling (max_rpm) from the resolved cfg, or None.

    The COLD-START fallback: when the shared Redis bucket is absent (a brain idle longer than
    its bucket TTL — ~60s at 5 RPM — or never-yet-called), ``brain_rate_per_sec`` returns None
    and the gate cannot defer. But the operator-set ``max_rpm`` on the brain cfg IS the
    ceiling (e.g. 5 for a Cerebras Free-Trial key), so the gate can — and must — still defer
    for a low-configured brain even when the live bucket is cold. Without this the gate is
    structurally inert for any brain briefly idle, which defeats the whole point at 5 RPM.
    """
    try:
        from saas.llm_context import current_llm_config
        cfg = current_llm_config()
        if cfg:
            mr = cfg.get("max_rpm")
            if mr is not None:
                mr = float(mr)
                if mr > 0:
                    return mr
    except Exception:
        return None
    return None


def _probe_maintenance(scope) -> Optional[float]:
    """Seconds left in this brain's MAINTENANCE window (a user-driven Detect-cap probe is
    quiescing the brain), or None when none is in force / the probe is unavailable.

    None on ANY error — the fail-safe direction for admission is "proceed" (unchanged
    behavior), and an unreadable maintenance flag must not silently defer ingest."""
    if redis_coord is None or scope is None:
        return None
    fn = getattr(redis_coord, "brain_maintenance_remaining", None)
    if fn is None:
        return None
    try:
        v = fn(scope)
    except Exception:
        return None
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _meter_decision(scope, decision: str) -> str:
    """Count an admission decision against the BRAIN scope, then return it UNCHANGED.

    This is how the console can say "N pieces of work were handed to the background drain"
    instead of the tenant discovering it by noticing their turns felt slow. Counted only once
    the scope is resolved — a fail-safe "proceed" taken because there was no brain scope to
    reason about is not an admission decision about any brain, and attributing it to one would
    be a fabrication.

    NEVER raises and NEVER alters the decision: the gate's behaviour is byte-for-byte what it
    was before instrumentation. The identity return is deliberate so a call site reads
    ``return _meter_decision(scope, "defer")`` and cannot accidentally drop the verdict.
    """
    try:
        from src.api import brain_meter
        brain_meter.record(
            scope,
            brain_meter.KIND_DEFER if decision == "defer" else brain_meter.KIND_ADMIT)
    except Exception:  # noqa: BLE001 — instrumentation must never break admission
        pass
    return decision


def acquire_interactive(operation: str, budget_s: float, estimated_calls: int = 1) -> str:
    """The admission decision for an INTERACTIVE ingest call.

    Returns ONE of:
      • ``"proceed"`` — capacity is available now (or will mint within the budget). The
        caller runs the FULL extraction pipeline now, exactly as today; the rate limiter
        takes the token as part of the call.
      • ``"defer"``   — no capacity within ``budget_s``. The caller has already captured the
        turn verbatim (the floor); it hands the raw text to the deferred statement-extraction
        drain for FULL completion later, preserving ``source="mcp" → fact_provenance=
        "user_stated"``.

    Decision table (FAIL-SAFE TO "proceed" — see the module docstring for the rationale):

      | condition                                                | decision |
      |----------------------------------------------------------|----------|
      | scope not resolvable / SAAS off / FOSS                   | proceed  |
      | brain_available_tokens probe absent (Deliv C not wired)  | proceed  |
      | tokens >= 1.0                                            | proceed  |
      | tokens < 1.0, rate known, (1.0 - tokens) / rate          | proceed  |
      |   <= budget_s  (token will mint in budget)               |          |
      | tokens < 1.0, no rate OR mint would exceed budget        | defer    |
      | ANY exception in the probe                               | proceed  |

    Parameters
    ----------
    operation:
        The LLM operation name (e.g. ``"EXTRACT"``). Present for log/metric attribution only —
        admission is SUBJECT-AGNOSTIC and never branches on the operation name.
    budget_s:
        What is LEFT of the interactive turn budget when this is called (pass
        ``llm_lane.turn_budget_remaining()``). ``inf`` when unbudgeted (a background/test path)
        — admission still proceeds when a token is free, defers only when one will not arrive
        in the (infinite) budget, which the rate being unknown/zero rules out → fail-safe to
        proceed.
    estimated_calls:
        How many LLM calls the work the caller is ABOUT to start will make against the brain
        (default 1). A multi-fact STATEMENT extraction is NOT one call — it is REFRAME (atomize)
        plus one EXTRACT chunk per ~3 sentences. At a low ceiling (5 RPM = 12s/token) a 3-call
        extraction paces ~36s, which violates the turn budget EVEN THOUGH the first call had a
        free token. Passing the honest estimate lets the gate DEFER the whole extraction (verbatim
        already captured; the deferred drain completes it) instead of proceeding and making the
        user wait 30-45s. Measured live on pre-prod 2026-08-12: a 5-fact statement took 45s
        without this; the gate proceeded because it only checked the first call.
    """
    try:
        scope = _brain_scope()
        if scope is None:
            # No shared brain bucket (SAAS off / FOSS / unresolved cfg). The local in-process
            # bucket handles pacing; we have no shared opinion to defer on.
            return "proceed"

        # A MAINTENANCE window (user-driven Detect-cap probe) is POSITIVE knowledge that no
        # capacity will arrive inside any turn budget — the probe holds the bucket still by
        # design and the rate gate would park the extraction anyway. Defer: the verbatim turn
        # is already captured, the deferred drain completes it the moment the window lifts,
        # and the user's reply is not made to wait out the probe. Fail-safe (unreadable
        # maintenance state) falls through to the normal decision — never defers on a guess.
        maint = _probe_maintenance(scope)
        if maint is not None and maint > 0:
            return _meter_decision(scope, "defer")

        tokens = _probe_tokens_now(scope)
        rate = _probe_rate_per_sec(scope)
        # COLD-START FALLBACK: a brain idle longer than its bucket TTL (~60s at 5 RPM) or
        # never-yet-called has NO Redis bucket → both probes return None. The operator-set
        # max_rpm on the cfg IS the ceiling, so fall back to it and treat cached tokens as 0
        # (conservative). This is what lets the gate defer at a low-configured brain even when
        # the live bucket is cold — without it the gate is inert for any briefly-idle brain.
        if tokens is None:
            tokens = 0.0
        if rate is None:
            cfg_rpm = _brain_cfg_max_rpm()
            if cfg_rpm and cfg_rpm > 0:
                rate = cfg_rpm / 60.0

        try:
            budget = float(budget_s) if budget_s is not None else 0.0
        except (TypeError, ValueError):
            budget = 0.0
        if budget < 0:
            budget = 0.0
        # Treat inf (unbudgeted) as effectively-unlimited → always proceed when there's any path
        # to capacity (matches the single-call fail-safe for background/test callers).
        import math as _math
        if _math.isinf(budget):
            return _meter_decision(scope, "proceed")

        try:
            needed = max(1, int(estimated_calls or 1))
        except (TypeError, ValueError):
            needed = 1

        # Whole extraction fits in the currently-available burst → proceed.
        if tokens >= needed:
            return _meter_decision(scope, "proceed")

        if rate is None or rate <= 0:
            # No live rate AND no configured max_rpm → cannot positively say capacity will not
            # arrive — fail-safe: proceed (the limiter's own pacing serves it).
            return _meter_decision(scope, "proceed")

        # Capacity that will exist within the turn budget = what's left now + what mints by then.
        capacity_in_budget = tokens + rate * budget
        if capacity_in_budget >= needed:
            return _meter_decision(scope, "proceed")

        # POSITIVE knowledge: the FULL extraction will not complete inside the turn budget. DEFER
        # — the verbatim floor is already captured; the deferred drain runs the SAME pipeline when
        # capacity returns. This is the path that honors "turn comes back in a couple of seconds"
        # at a low ceiling.
        return _meter_decision(scope, "defer")
    except Exception:
        # The only thing worse than fail-open is breaking the call entirely.
        return "proceed"
