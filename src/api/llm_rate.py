"""Outbound LLM rate governance — token-bucket pacing, a SHARED budget, a daily drip.

WHY THIS MODULE EXISTS
──────────────────────
Every configured LLM endpoint has a ceiling — published by the provider, or imposed by
the hardware when it is self-hosted. FaultLine paces its own outbound requests under
that ceiling with three controls, each doing ONE job:

1. **A token bucket (per-second pace).** Refills at ``LLM_MAX_RPM/60`` tokens/sec,
   grants bursts up to ``LLM_RATE_BURST``. Smooths minute-class spikes.
2. **A SHARED budget (cross-process, load-bearing).** The canonical entrypoint
   (docker-entrypoint.sh) backgrounds TWO Python processes in one container — the API
   (``uvicorn src.api.main:app``) and the re-embedder
   (``python -m src.re_embedder.embedder``). A module-level bucket is PER-PROCESS, so
   each process enforces the configured ceiling independently and combined outbound is
   a MULTIPLE of it — each process's own accounting looks perfect while the deployment
   sends 2x what it believes it is sending. That shape is invisible from inside either
   process, which is precisely how it survives. The shared budget moves the bucket
   into Redis (``src/api/redis_coord.py``) behind ONE atomic Lua take, so both
   processes — and every client behind the same configured endpoint, which is everyone
   in this single-brain deployment — spend from the SAME balance. ``announce()`` makes
   the engagement state LOUD at startup; a silent fallback would re-ship the
   multiplication defect to everyone who self-hosts.
3. **A daily budget (paced, not walled).** A daily quota is a PACING problem, not a
   headroom problem: headroom on a per-day figure only moves the moment the budget
   dies and then stalls hard. When the operator sets ``LLM_DAILY_BUDGET_REQ``, the
   BACKGROUND lane defers against the shared daily hash all day and drips through the
   window instead of racing the ceiling. No cap is ever invented: unset → no daily
   gate.

LANES (see src/api/llm_lane.py)
──────────────────────────────
The lane is a property of the CALLER. INTERACTIVE (a user is waiting) keeps the
fail-open: after ``LLM_RATE_MAX_WAIT_S`` the call proceeds uncapped — loudly — because
losing a user's request is worse than exceeding pace. BACKGROUND (the re-embedder
declares itself at startup) DEFERS instead: a background sweep firing uncapped into an
endpoint that is already refusing is how a rate limit becomes a flood. A background
call that cannot get capacity returns False from the gate and the caller skips the
work for this pass — it is never lost, only later.

COUNT REQUESTS, NOT CALLS
─────────────────────────
The gate is taken PER HTTP REQUEST — inside the retry loop, immediately before each
POST — because a call that retries three times puts three requests on the wire. Pacing
per logical call under-counts real outbound load by the retry factor.

ENV KNOBS
─────────
  LLM_RATE_LIMIT_ENABLED   default "1"/on. OFF → this module is a pure no-op and the
                           engine starts and serves exactly as before.
  LLM_MAX_RPM              default 480 (= 8 tokens/sec).
  LLM_RATE_BURST           burst capacity (tokens). Default = 1s worth of tokens
                           (ceil(rate), at least 1).
  LLM_RATE_MAX_WAIT_S      default 30. Fail-safe cap on the per-request wait; after
                           it, INTERACTIVE proceeds uncapped (loudly) and BACKGROUND
                           defers.
  LLM_DAILY_BUDGET_REQ     daily request budget. NO default — unknown stays unknown;
                           unset → no daily gate.
  LLM_DAILY_WINDOW_S       daily window length (rolling, opens at first spend).
                           Default 86400. There is deliberately NO UTC-midnight
                           assumption: we do not know the provider's reset time and
                           refuse to guess it.
  REDIS_URL / REDIS_COORD  the shared budget's coordination service (see
                           redis_coord). REDIS_COORD=false opts out entirely.
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Optional

from src.api import llm_lane, redis_coord
from src.api.llm_lane import LLMUnavailable  # re-exported for callers of the gate

try:  # pragma: no cover
    import structlog
    log = structlog.get_logger()
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("llm_rate")


_TRUE_SET = {"1", "true", "yes", "on"}

# Acquire-loop slice bounds: never sleep the whole remaining wait in one go (the
# shared budget may grant earlier than the local estimate), never busy-spin.
_MAX_SLICE = 0.25
_MIN_SLICE = 0.01

# The canonical deployment's process count (the API + the re-embedder, both
# backgrounded by docker-entrypoint.sh). announce() uses it to quantify the
# per-process fallback; it is a statement about the SHIPPED ENTRYPOINT, not a
# discovery of what is running.
_ENTRYPOINT_PROCESSES = 2


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG (read live so an operator toggle needs no reload)
# ──────────────────────────────────────────────────────────────────────────────
def governance_enabled() -> bool:
    return os.environ.get("LLM_RATE_LIMIT_ENABLED", "1").strip().lower() in _TRUE_SET


def rate_per_sec() -> float:
    """Refill rate in tokens/sec, from LLM_MAX_RPM. Fail-safe default."""
    try:
        rpm = float(os.environ.get("LLM_MAX_RPM", "480"))
        if rpm <= 0:
            return 8.0
        return rpm / 60.0
    except (ValueError, TypeError):
        return 8.0


def burst_capacity(rate: float) -> float:
    """Burst allowance (bucket capacity). Env override; else ~1s worth of tokens."""
    raw = os.environ.get("LLM_RATE_BURST")
    if raw is not None:
        try:
            cap = float(raw)
            if cap >= 1.0:
                return cap
        except (ValueError, TypeError):
            pass
    return max(1.0, float(math.ceil(rate)))


def max_wait_s() -> float:
    """Fail-safe max total wait per acquire. Default 30s."""
    try:
        v = float(os.environ.get("LLM_RATE_MAX_WAIT_S", "30"))
        return v if v > 0 else 30.0
    except (ValueError, TypeError):
        return 30.0


def daily_cap() -> Optional[int]:
    """The operator's daily request budget, or None (no daily gate).

    NO default and NO invented numbers: a provider's daily figure is a fact about the
    operator's tier that this code cannot know. Unknown stays unknown.
    """
    try:
        v = int(str(os.environ.get("LLM_DAILY_BUDGET_REQ", "")).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def daily_window_s() -> int:
    """Daily window length (rolling). Env override; else 86400."""
    try:
        v = int(str(os.environ.get("LLM_DAILY_WINDOW_S", "")).strip())
        return v if v > 0 else 86400
    except (TypeError, ValueError):
        return 86400


# ──────────────────────────────────────────────────────────────────────────────
# SCOPE / KEY RESOLUTION — the budget follows the endpoint actually being called
# ──────────────────────────────────────────────────────────────────────────────
def _resolve_scope(endpoint: Optional[str], model: Optional[str]):
    """The coordination Scope for THIS call: the endpoint+model being called, hashed.

    Everything configured against the same endpoint shares one budget — that is the
    grain at which a ceiling is enforced, and in this deployment every client sits
    behind one configured brain, so the budget is genuinely shared. No endpoint
    resolvable → the explicit single-deployment scope (never an unscoped key).
    """
    base = str(endpoint or "").strip()
    if not base:
        base = str(os.environ.get("LLM_BASE_URL", "")).strip()
    mdl = str(model or os.environ.get("WGM_LLM_MODEL", "")).strip()
    try:
        if base:
            return redis_coord.scope_for_endpoint(base, mdl)
    except redis_coord.UnscopedKeyError:
        pass
    return redis_coord.scope_single_deployment()


# ──────────────────────────────────────────────────────────────────────────────
# LOCAL BUCKET — the per-process fallback and the no-coordination mode
# ──────────────────────────────────────────────────────────────────────────────
class _TokenBucket:
    """A monotonic-clock token bucket for ONE key, shared by sync + async callers.

    The token arithmetic runs under a tiny ``threading.Lock`` held for microseconds
    (never across a sleep), so the same bucket is consistent whether acquired from the
    sync path or an async task — async callers sleep with ``asyncio.sleep`` OUTSIDE
    the lock. ``time.monotonic()`` deliberately: this is runtime pacing, so a
    wall-independent clock is correct (and immune to system-clock jumps).
    """

    __slots__ = ("rate", "capacity", "tokens", "_lock", "_last")

    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity   # a fresh bucket starts full (burst available)
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def _try_acquire(self) -> tuple[bool, float]:
        """One local take attempt. True = token spent, caller may send."""
        with self._lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self._last)
            self._last = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True, 0.0
            wait = (1.0 - self.tokens) / self.rate if self.rate > 0 else 0.0
            return False, wait

    def acquire_sync(self, max_wait: float, deferrable: bool) -> bool:
        waited = 0.0
        while True:
            ok, wait = self._try_acquire()
            if ok:
                return True
            if waited >= max_wait:
                return False if deferrable else _fail_open(waited)
            slice_s = max(_MIN_SLICE, min(wait, max_wait - waited, _MAX_SLICE))
            time.sleep(slice_s)
            waited += slice_s

    async def acquire_async(self, max_wait: float, deferrable: bool) -> bool:
        import asyncio
        waited = 0.0
        while True:
            ok, wait = self._try_acquire()
            if ok:
                return True
            if waited >= max_wait:
                return False if deferrable else _fail_open(waited)
            slice_s = max(_MIN_SLICE, min(wait, max_wait - waited, _MAX_SLICE))
            await asyncio.sleep(slice_s)
            waited += slice_s


_buckets: dict[str, _TokenBucket] = {}
_buckets_lock = threading.Lock()


def _get_bucket(key: str) -> _TokenBucket:
    """The local bucket for this key. Rate/burst are read LIVE so an operator's
    change applies without a restart — a bucket is never frozen at its creation-time
    pace (the stale-clamp defect: switch endpoints, keep the old pace, forever)."""
    rate = rate_per_sec()
    with _buckets_lock:
        b = _buckets.get(key)
        if b is None:
            b = _TokenBucket(rate, burst_capacity(rate))
            _buckets[key] = b
            log.info("llm_rate_limit.bucket_created", key=key,
                     rate_per_sec=round(rate, 3),
                     burst=burst_capacity(rate),
                     shared=redis_coord.client() is not None)
        elif b.rate != rate:
            # Resync under the bucket's OWN lock: an acquire in flight on another
            # thread reads rate/capacity inside b._lock, so mutating them here
            # bare would be a torn read (wait computed against a half-updated rate).
            with b._lock:
                b.rate = rate
                b.capacity = burst_capacity(rate)
                b.tokens = min(b.tokens, b.capacity)
            log.info("llm_rate_limit.bucket_resynced", key=key,
                     rate_per_sec=round(rate, 3))
        return b


def _fail_open(waited: float) -> bool:
    """INTERACTIVE fail-open: a user is waiting; proceed uncapped, LOUDLY.

    The inverse (background) case returns False and defers — a background sweep must
    never fire uncapped into an endpoint that is already refusing us. A fail-open
    under queue pressure at a low rate is not pacing at all: at 5 RPM a token mints
    every 12s, so any queue deeper than max_wait/12 hit this line, and a limiter that
    fail-opens there structurally floods no matter how correct its cap is.
    """
    try:
        log.warning("llm_rate_limit.wedged_failopen",
                    waited_seconds=round(waited, 2),
                    note="rate limiter exceeded LLM_RATE_MAX_WAIT_S — proceeding "
                         "uncapped to avoid losing a user's call; check LLM_MAX_RPM "
                         "/ LLM_RATE_MAX_WAIT_S. Background (deferrable) calls are "
                         "NOT allowed to take this path.")
    except Exception:  # pragma: no cover
        pass
    return True


def _background_deferred(waited: float, reason: str = "no_capacity") -> None:
    """A BACKGROUND call yielded instead of firing uncapped. The healthy counterpart
    of the fail-open: no user is waiting, so the correct answer to "no capacity" is
    to not make the request. Logged at INFO — a deferral is the system working."""
    global _deferred_count
    try:
        with _deferred_lock:
            _deferred_count += 1
        log.info("llm_rate_limit.background_deferred",
                 waited_seconds=round(waited, 2), reason=reason,
                 note="deferrable upkeep yielded to the shared rate budget; it will "
                      "be retried on a later pass. Interactive calls are unaffected.")
    except Exception:  # pragma: no cover
        pass


# OBSERVATION COUNTER: how many background deferrals this PROCESS has made. A
# request that runs in the background lane snapshots it on entry and sets
# rate_deferred on its response if it moved — "asked and got nothing" and "never
# asked" stay distinguishable across EVERY LLM call the request makes (chunk
# extraction, reframe/atomize, span detect, merge proposals), without threading a
# counter through each helper. Defers only ever happen in the background lane, and
# the only background traffic an API process sees is header-declaring sweep
# traffic, so a delta is always attributable to that class — worst case it
# over-marks a concurrent sweep request, which retries next cycle (the safe
# direction). Interactive lanes fail OPEN and can never move this counter.
_deferred_count = 0
_deferred_lock = threading.Lock()


def deferred_counter() -> int:
    """Current process-wide deferral count (snapshot me before/after a request)."""
    with _deferred_lock:
        return _deferred_count


def reset_deferred_counter() -> None:
    """Test hook."""
    global _deferred_count
    with _deferred_lock:
        _deferred_count = 0


# ──────────────────────────────────────────────────────────────────────────────
# SHARED-FIRST ACQUIRE — the Redis bucket is AUTHORITATIVE when it has an opinion
# ──────────────────────────────────────────────────────────────────────────────
def _shared_acquire_wait(scope) -> Optional[float]:
    """Seconds to wait per the CROSS-PROCESS budget, or None for "no shared opinion".

    0.0 means a token was actually SPENT in the shared bucket. A shared opinion
    outranks the local bucket because it is the one counting every process; the local
    bucket only runs when coordination is off/unavailable (announce() says which).
    """
    try:
        st = redis_coord.take_token_state(scope, rate_per_sec(), burst_capacity(rate_per_sec()))
        if st is None:
            return None
        return float(st.get("wait_s", 0.0))
    except Exception:  # noqa: BLE001 — never lose a call to the limiter itself
        return None


def _acquire_sync(key: str, scope, deferrable: bool) -> bool:
    waited = 0.0
    cap = max_wait_s()
    while True:
        shared_wait = _shared_acquire_wait(scope)
        if shared_wait is not None:
            if shared_wait <= 0.0:
                return True
            wait = shared_wait
        else:
            ok, wait = _get_bucket(key)._try_acquire()
            if ok:
                return True
        if waited >= cap:
            if deferrable:
                _background_deferred(waited)
                return False
            return _fail_open(waited)
        slice_s = max(_MIN_SLICE, min(wait, cap - waited, _MAX_SLICE))
        time.sleep(slice_s)
        waited += slice_s


async def _acquire_async(key: str, scope, deferrable: bool) -> bool:
    import asyncio
    waited = 0.0
    cap = max_wait_s()
    while True:
        shared_wait = _shared_acquire_wait(scope)
        if shared_wait is not None:
            if shared_wait <= 0.0:
                return True
            wait = shared_wait
        else:
            ok, wait = _get_bucket(key)._try_acquire()
            if ok:
                return True
        if waited >= cap:
            if deferrable:
                _background_deferred(waited)
                return False
            return _fail_open(waited)
        slice_s = max(_MIN_SLICE, min(wait, cap - waited, _MAX_SLICE))
        await asyncio.sleep(slice_s)
        waited += slice_s


# ──────────────────────────────────────────────────────────────────────────────
# DAILY GATE — BACKGROUND defers against it; INTERACTIVE only COUNTS
# ──────────────────────────────────────────────────────────────────────────────
def _daily_probe(scope) -> bool:
    """Pre-wait PROBE (peek, no spend): a background call against an exhausted day
    must defer IMMEDIATELY, not sleep out LLM_RATE_MAX_WAIT_S first — the
    wait-then-fail-open wedge is the exact defect this gate closes.

    Defers ONLY while the stored window is live AND the CURRENT cap is spent, so an
    operator cap RAISE takes effect on the very next call (the atomic take
    re-persists it). Once the window has rolled the call falls through to the normal
    acquire; the atomic take's Lua is the ONLY code that rolls the window.
    """
    cap = daily_cap()
    if not cap:
        return True
    peek = redis_coord.peek_daily(scope)
    if (peek and int(peek.get("used", 0)) >= cap
            and int(time.time() * 1000) < int(peek.get("reset_ms", 0))):
        _background_deferred(0.0, reason="daily_budget_exhausted")
        return False
    return True


def _daily_spend(scope) -> bool:
    """Take one daily slot AFTER the minute token granted, so a request that never
    fires (minute-defer) does not burn daily budget; the rare lost race (another
    process takes the last slot between grant and spend) defers, which is the safe
    direction. INTERACTIVE spends with force=True: counted, never refused — an
    exhausted day must not make user traffic invisible to the shared arithmetic."""
    cap = daily_cap()
    if not cap:
        return True
    take = redis_coord.try_take_daily(scope, cap, daily_window_s(),
                                      force=not llm_lane.is_background())
    if not take.get("ok") and llm_lane.is_background():
        _background_deferred(0.0, reason="daily_budget_exhausted")
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# THE GATE — pace ONE outbound LLM REQUEST (per attempt, inside the retry loop)
# ──────────────────────────────────────────────────────────────────────────────
def gate_sync(operation: str = "DEFAULT", endpoint: Optional[str] = None,
              model: Optional[str] = None) -> bool:
    """Pace one outbound sync LLM REQUEST. True = the caller may send now.

    False ONLY for a BACKGROUND (deferrable) call that found no capacity within the
    cap — the caller skips this request this pass (raise/handle LLMUnavailable per
    your caller contract). Interactive callers always get True, preserving the
    never-lose-a-user's-call fail-open. Disabled, or any limiter error → True.
    """
    if not governance_enabled():
        return True
    try:
        scope = _resolve_scope(endpoint, model)
        key = f"brain:{scope.ident}"
        if llm_lane.is_background() and not _daily_probe(scope):
            return False
        if not _acquire_sync(key, scope, deferrable=llm_lane.is_background()):
            return False
        return _daily_spend(scope)
    except Exception as e:  # noqa: BLE001 — never lose a call because the limiter broke
        log.warning("llm_rate_limit.error_failopen",
                    operation=operation, mode="sync", error=str(e)[:200])
        return True


async def gate_async(operation: str = "DEFAULT", endpoint: Optional[str] = None,
                     model: Optional[str] = None) -> bool:
    """Pace one outbound async LLM REQUEST. See the sync twin."""
    if not governance_enabled():
        return True
    try:
        scope = _resolve_scope(endpoint, model)
        key = f"brain:{scope.ident}"
        if llm_lane.is_background() and not _daily_probe(scope):
            return False
        if not await _acquire_async(key, scope, deferrable=llm_lane.is_background()):
            return False
        return _daily_spend(scope)
    except Exception as e:  # noqa: BLE001
        log.warning("llm_rate_limit.error_failopen",
                    operation=operation, mode="async", error=str(e)[:200])
        return True


# ──────────────────────────────────────────────────────────────────────────────
# STARTUP ANNOUNCEMENT — the engagement state of the shared budget, said OUT LOUD
# ──────────────────────────────────────────────────────────────────────────────
def announce(process_name: str = "process") -> None:
    """Say, at startup, which governance mode this process is running under.

    Three states, and only one of them is quiet:

    * governance OFF — stated plainly (outbound is un-paced by request);
    * SHARED budget live — the API and the re-embedder (and every client) spend from
      ONE balance held in the coordination service;
    * **LOCAL FALLBACK** — coordination is unreachable and each process keeps its own
      bucket. This is announced LOUDLY, as a warning, stating the consequence: with
      the shipped entrypoint's two processes (the API and the re-embedder) combined
      outbound WILL exceed the configured rate by the number of processes — up to 2x
      the configured rate. A silent fallback here is the original per-process defect
      re-shipped to everyone who self-hosts without the coordination service.

    Also flags a structurally-failing wait cap: when LLM_RATE_MAX_WAIT_S is shorter
    than one token interval, ANY queue exceeds the cap and the interactive path must
    fail open — that configuration cannot pace under load no matter how correct the
    configured rate is.
    """
    try:
        where = f" ({process_name})"
        if not governance_enabled():
            log.info("llm_rate.governance_disabled",
                     process=process_name,
                     note="LLM_RATE_LIMIT_ENABLED is off: outbound LLM requests are "
                          "not paced. The engine starts and serves normally.")
            return
        rate = rate_per_sec()
        if redis_coord.enabled():
            shared = redis_coord.client() is not None
        else:
            shared = False
        if shared:
            log.info("llm_rate.shared_budget_active",
                     process=process_name,
                     coordination=redis_coord.safe_redis_url(),
                     rate_per_sec=round(rate, 3),
                     burst=burst_capacity(rate),
                     daily_cap=daily_cap() or "unset (no daily gate)",
                     note="shared outbound rate budget ACTIVE: this process, the "
                          "other entrypoint processes, and every client behind the "
                          "same configured endpoint spend from ONE shared balance. "
                          "Combined outbound stays within the configured rate.")
        elif not redis_coord.enabled():
            # A DELIBERATE opt-out (REDIS_COORD=false), stated as a choice, not
            # dressed as an outage — but the consequence is still named, because
            # it is the same consequence either way.
            log.warning("llm_rate.LOCAL_FALLBACK_PER_PROCESS_BUDGETS",
                        process=process_name,
                        coordination="opt-out (REDIS_COORD=false)",
                        rate_per_sec=round(rate, 3),
                        entrypoint_processes=_ENTRYPOINT_PROCESSES,
                        note="coordination is deliberately DISABLED — rate "
                             "governance runs PER-PROCESS: each process enforces "
                             "the configured rate independently, so combined "
                             "outbound WILL EXCEED the configured rate by the "
                             "number of processes: with the shipped entrypoint's "
                             f"{_ENTRYPOINT_PROCESSES} processes (the API and the "
                             f"re-embedder) that is up to {_ENTRYPOINT_PROCESSES}x "
                             "the configured rate. Set REDIS_COORD=true with a "
                             "reachable REDIS_URL for the shared budget, or "
                             "LLM_RATE_LIMIT_ENABLED=0 to run deliberately "
                             "un-paced.")
        else:
            log.warning("llm_rate.LOCAL_FALLBACK_PER_PROCESS_BUDGETS",
                        process=process_name,
                        coordination=redis_coord.safe_redis_url(),
                        rate_per_sec=round(rate, 3),
                        entrypoint_processes=_ENTRYPOINT_PROCESSES,
                        note="COORDINATION SERVICE UNAVAILABLE — rate governance is "
                             "falling back to PER-PROCESS buckets. Each process "
                             "enforces the configured rate independently, so "
                             "combined outbound WILL EXCEED the configured rate by "
                             "the number of processes: with the shipped entrypoint's "
                             f"{_ENTRYPOINT_PROCESSES} processes (the API and the "
                             f"re-embedder) that is up to {_ENTRYPOINT_PROCESSES}x "
                             "the configured rate. Start the coordination service "
                             "(REDIS_URL) or set LLM_RATE_LIMIT_ENABLED=0 to run "
                             "deliberately un-paced.")
        if max_wait_s() < (1.0 / rate) and rate > 0:
            log.warning("llm_rate.wait_cap_below_token_interval",
                        process=process_name,
                        max_wait_s=max_wait_s(),
                        token_interval_s=round(1.0 / rate, 3),
                        note="LLM_RATE_MAX_WAIT_S is shorter than one token "
                             "interval: any queue deeper than one exceeds the cap "
                             "and interactive calls fail open (exceed pace) rather "
                             "than pace. Raise LLM_RATE_MAX_WAIT_S or LLM_MAX_RPM.")
    except Exception:  # pragma: no cover — a startup banner must never crash startup
        pass
