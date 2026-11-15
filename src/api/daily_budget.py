"""Redis-backed DAILY REQUEST BUDGET per brain scope — the paced RPD core (dprompt-155 §2.3).

WHY THIS MODULE EXISTS
──────────────────────
The per-minute token bucket (redis_coord CAPABILITY 5) applies 80% headroom to RPM. That is a
BURST-SMOOTHING control: right for per-minute axes, wrong for per-day axes. Applied to RPD,
headroom only moves the moment the budget dies ("exhausted at 16:00 instead of 17:00") and then
the caller stalls hard at the wall. A rate limiter that stalls at the wall instead of pacing is
an anti-pattern — the sweep then sits parked for hours and the tenant's background work never
finishes.

A daily quota against known daily volume is a PACING problem:

    drip_rate = remaining_daily_budget / seconds_left_in_window

Realized as a slow budget: the background lane defers while the daily figure is spent down,
drips through the window, and finishes as the window closes instead of racing the ceiling. The
bar this serves (dprompt-155 §2.3, minimum bars): a ~2,800 req/day sweep against a 1,000-RPD
free-tier lane is NEVER refused outright, NEVER exhausts the budget by noon, and degrades
gracefully.

DESIGN DECISIONS THAT ARE LOAD-BEARING
──────────────────────────────────────
* **State survives process restart.** ``used`` lives in a Redis hash, not memory — an
  in-memory counter resets to full on every deploy and blows the provider quota (§2.3's third
  minimum bar). The key TTL is >= 2× the window and is refreshed on every take, so daily state
  can NEVER expire mid-window.
* **ROLLING window, no UTC-midnight anywhere.** §2.3's second bar: the window reset is derived
  from provider data (the cap VALUE arrives in the limits dict — ``resolve_provider_limits``
  carrying ``rpd`` from the published-limits/429-fallback tables), never assumed to be UTC
  midnight. The window starts when the first spend of the epoch is taken and rolls
  ``window_s`` later; a late spender never inherits a stale window.
* **cap/window_s are the CURRENT operator/table opinion, re-persisted on EVERY take.** A
  changed cap applies on the very next take — no restart, no migration.
* **Unknown stays unknown.** A provider that publishes no daily figure (e.g. a trial tier)
  must NOT get an invented number: ``daily_cap_from_limits`` returns None and the caller
  applies NO daily gate. Failing closed into a fabricated cap would throttle a provider that
  never asked us to (same doctrine as ``resolve_provider_limits`` on ``rpm=None``).
* **FAIL-OPEN on coordination outage** — mirroring redis_coord CAPABILITY 5's doctrine: Redis
  absent/unreachable/script-error → ``{"ok": True, "degraded": True}``, logged at INFO (never
  crit). A coordination outage must not become an ingest outage; the per-minute bucket still
  caps outbound while the daily figure is unenforced. The ONE fail-closed case is a known cap
  of zero → ``{"ok": False, "reason": "no_cap"}``: with a real cap in hand, fail toward
  deferral, not toward spending.
* **Atomicity by Redis's own guarantee.** The check-and-spend runs as one Lua script — per the
  Redis documentation ("Scripting with Lua", redis.io/docs/latest/develop/programmability/
  eval-intro): "Redis guarantees the script's atomic execution. While executing the script, all
  server activities are blocked during its entire runtime." Two processes therefore cannot
  double-spend the same daily slot, and a concurrent reader sees either the pre- or the
  post-state, never a torn hash. The script is registered via ``redis_coord``'s cached-script
  pattern (``register_script`` — the client library handles EVALSHA/NOSCRIPT re-loading).

KEY LAYOUT — mirrors redis_coord's llmrate pattern exactly
──────────────────────────────────────────────────────────
``fl:llmdaily:<kind>:<ident>`` built by ``redis_coord.key(NS_DAILY, scope)`` — the same
``Scope``-typed, placeholder-refusing builder the rate bucket uses (``fl:llmrate:<kind>:<ident>``
plus a tail part), so a daily budget is scoped to the BRAIN by construction and an unscoped key
is unrepresentable. No new key-builder exists in this module by design.

HASH FIELDS: ``used`` (int, spent this window), ``window_start_ms`` (int, epoch-ms this window
opened), ``window_s`` (int, persisted current opinion), ``cap`` (int, persisted current opinion).

LANE POLICY IS NOT HERE. Who defers (BACKGROUND defers, INTERACTIVE keeps the fail-open) is the
caller's decision in llm_calls.py/llm_lane.py; this module is only the data/plumbing core.

VERIFICATION KNOBS (documented for the test builder):
    FL_DAILY_BUDGET_REQ  — int > 0, WINS over the limits-dict ``rpd`` when set (operator
                           override; also how a driver pins a known cap regardless of table).
    FL_DAILY_WINDOW_S    — int > 0, default 86400 (also how a driver shortens the window).
"""

from __future__ import annotations

import os
import time
from typing import Optional

try:  # pragma: no cover - import guard, same pattern as ceiling_admission
    from src.api import redis_coord as RC
except Exception:  # pragma: no cover - daily budget degrades to fail-open without it
    RC = None  # type: ignore

try:  # pragma: no cover
    import structlog
    log = structlog.get_logger()
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("daily_budget")


# Redis key namespace; the full key is fl:llmdaily:<kind>:<ident> via redis_coord.key().
NS_DAILY = "llmdaily"

_DEFAULT_WINDOW_S = 86400

_last_fail_log = 0.0


def _note_fail(op: str, exc: BaseException) -> None:
    """Log a daily-budget degradation at INFO, throttled to once a minute.

    Deliberately NOT redis_coord._note_degraded's WARNING and never log_crit: per the frozen
    contract this degradation is expected under a Redis outage and the per-minute bucket still
    caps outbound — INFO (rate-limited so a dead Redis failing hot cannot spam) is the honest
    level for "daily pacing is unenforced right now".
    """
    global _last_fail_log
    now = time.time()
    if now - _last_fail_log < 60:
        return
    _last_fail_log = now
    log.info("daily_budget.degraded_fail_open",
             op=op, error_type=type(exc).__name__, error=str(exc)[:200],
             note="daily pacing unenforced; per-minute bucket still caps outbound")


def _env_int(name: str) -> Optional[int]:
    try:
        v = int(str(os.environ.get(name, "")).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def daily_cap_from_limits(limits: Optional[dict]) -> Optional[int]:
    """The DAILY request cap for this brain, or None when nothing is known.

    ``limits`` is the provider-limits dict as produced by ``resolve_provider_limits`` — the
    daily figure arrives as DATA in ``limits['rpd']`` (populated for hosts in the published
    -limits / ``_RATE_429_FALLBACK`` tables; e.g. openrouter.ai → 50, api.groq.com → 100).
    No provider-name branching happens here (constraint 5): a number we did not receive does
    not exist, and None means the caller applies NO daily gate — a provider that publishes no
    daily figure must not get an invented cap.

    ``FL_DAILY_BUDGET_REQ`` (int > 0) WINS over the table when set — an operator knob, and the
    verification knob a test driver uses to pin a known cap.

    Returns a positive int, or None when neither source yields one (absent/<=0/unparseable).
    """
    env = _env_int("FL_DAILY_BUDGET_REQ")
    if env is not None:
        return env
    if not isinstance(limits, dict):
        return None
    raw = limits.get("rpd")
    if raw is None:
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def daily_window_s() -> int:
    """The daily window length in seconds. FL_DAILY_WINDOW_S (int > 0) else 86400.

    The default is a ROLLING window: there is deliberately NO UTC-midnight assumption anywhere
    in this module — the window opens at the first spend and rolls ``window_s`` later. The env
    is the operator/verification knob (a driver shortens the window with it).
    """
    return _env_int("FL_DAILY_WINDOW_S") or _DEFAULT_WINDOW_S


# Atomic check-and-spend, one round-trip. Mirrors redis_coord's _LUA_RATE_TAKE shape.
#   KEYS[1] budget hash   ARGV[1] now_ms  ARGV[2] cap  ARGV[3] window_s  ARGV[4] cost
#   ARGV[5] ttl_s (>= 2*window_s)
# Returns {ok, used, remaining} on a grant and {0, reset_ms, 0} on a refusal. All integer
# math — now_ms is passed in by the Python side from time.time()*1000 (same as
# take_token_state), keeping the script deterministic and identical across callers.
_LUA_DAILY_TAKE = """
local now = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
local win_s = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local force = tonumber(ARGV[6]) or 0
if cap <= 0 then
  return {0, 0, 0}
end
if cost < 1 then
  cost = 1
end
local st = redis.call('hmget', KEYS[1], 'used', 'window_start_ms')
local used = tonumber(st[1])
local wsm = tonumber(st[2])
if used == nil or wsm == nil then
  used = 0
  wsm = now
end
if now >= wsm + win_s * 1000 then
  wsm = now
  used = 0
end
redis.call('hset', KEYS[1], 'used', used, 'window_start_ms', wsm,
           'window_s', win_s, 'cap', cap)
redis.call('expire', KEYS[1], ttl)
-- force (INTERACTIVE lane): COUNT the spend but NEVER refuse — used may exceed cap so
-- the shared arithmetic reflects the account's REAL usage (an exhausted day must not
-- make interactive traffic invisible to the background pacer; owner directive 2026-08-15).
if force == 1 or used + cost <= cap then
  used = used + cost
  redis.call('hset', KEYS[1], 'used', used)
  return {1, used, math.max(cap - used, 0)}
end
return {0, wsm + win_s * 1000, 0}
"""


def try_take_daily(scope, cap: int, window_s: int, cost: int = 1,
                   force: bool = False) -> dict:
    """Spend ``cost`` against the brain's DAILY budget. NEVER raises.

    Semantics (single atomic Lua round-trip — see module docstring for the atomicity source):
      * key absent          → window_start_ms=now_ms, used=0, then evaluate.
      * window expired      → ROLLING RESET (window_start_ms=now_ms, used=0) BEFORE evaluating,
                              so a late spender never inherits a stale window.
      * used + cost <= cap  → ``{"ok": True, "used": used, "remaining": cap - used}``.
      * else                → ``{"ok": False, "reset_ms": window_start_ms + window_s*1000,
                              "remaining": 0}`` — the caller defers until reset_ms.
      * ``force=True``      → COUNT the spend and return ok True EVEN PAST CAP (used may
                              exceed cap; remaining clamps at 0). For the INTERACTIVE
                              lane: a user is never deferred, but their usage MUST still
                              land in the shared arithmetic — an exhausted day that stops
                              counting interactive traffic under-reports the account and
                              lets background pace into a wall interactive already spent
                              (owner directive 2026-08-15: count interactive spends into
                              the daily hash without ever deferring them).
    ``cap``/``window_s`` are ALWAYS re-persisted (they are the CURRENT operator/table opinion;
    a changed cap applies immediately, no restart). The key TTL is >= 2×window_s on every take,
    so daily state never expires mid-window. ``cost <= 0`` is treated as 1. ``cap <= 0`` →
    ``{"ok": False, "reason": "no_cap"}`` (never ok on a zero cap — fail toward deferral).

    Redis absent/unreachable/script error → ``{"ok": True, "degraded": True}`` (fail-open, the
    CAPABILITY 5 doctrine; the per-minute bucket still caps outbound). A non-Scope ``scope``
    (e.g. None from scope_for_brain) is likewise degraded, never an exception.
    """
    try:
        if RC is None:
            return {"ok": True, "degraded": True}
        try:
            c = int(cap)
        except (TypeError, ValueError):
            c = 0
        if c <= 0:
            return {"ok": False, "reason": "no_cap"}
        try:
            w = int(window_s)
        except (TypeError, ValueError):
            w = 0
        if w <= 0:
            w = daily_window_s()
        try:
            n = int(cost)
        except (TypeError, ValueError):
            n = 1
        if n <= 0:
            n = 1
        if not isinstance(scope, RC.Scope):
            return {"ok": True, "degraded": True}
        cld = RC.client()
        if cld is None:
            return {"ok": True, "degraded": True}
        # Scope validated BEFORE touching the client, and the key built by THE sanctioned
        # builder — same shape as redis_coord's llmrate keys: fl:llmdaily:<kind>:<ident>.
        k = RC.key(NS_DAILY, scope)
        ttl = 2 * w
        out = RC._script(cld, "daily_take", _LUA_DAILY_TAKE)(
            keys=[k],
            args=[int(time.time() * 1000), c, w, n, ttl, 1 if force else 0],
        )
        ok = int(out[0])
        if ok:
            return {"ok": True, "used": int(out[1]), "remaining": int(out[2])}
        return {"ok": False, "reset_ms": int(out[1]), "remaining": 0}
    except Exception as e:  # noqa: BLE001 — fail-OPEN: never lose a call to a Redis blip
        _note_fail("try_take_daily", e)
        return {"ok": True, "degraded": True}


def peek_daily(scope) -> Optional[dict]:
    """Read-only snapshot of the brain's daily budget, or None. NEVER raises, never writes,
    never spends.

    ``{"used", "cap", "remaining", "window_start_ms", "reset_ms"}`` from the shared hash.
    None when no state exists yet, the scope is not a Scope, or Redis is degraded. The caller
    uses this to defer BEFORE a 30s token-bucket wait; verification uses it to evidence budget
    state. Reports the stored window as-is (a window past reset simply reads stale until the
    next take rolls it — a read must not mutate).
    """
    try:
        if RC is None:
            return None
        if not isinstance(scope, RC.Scope):
            return None
        cld = RC.client()
        if cld is None:
            return None
        k = RC.key(NS_DAILY, scope)
        used, cap, wsm, win_s = cld.hmget(k, "used", "cap", "window_start_ms", "window_s")
        if used is None or cap is None or wsm is None or win_s is None:
            return None
        u, cp, w0, ws = int(used), int(cap), int(wsm), int(win_s)
        return {
            "used": u,
            "cap": cp,
            "remaining": max(0, cp - u),
            "window_start_ms": w0,
            "reset_ms": w0 + ws * 1000,
        }
    except Exception as e:  # noqa: BLE001 — no opinion, never break the caller
        _note_fail("peek_daily", e)
        return None


def publish_day_cap(scope, cap: int, window_s: int) -> bool:
    """Record a LEARNED daily request allowance (from the provider's own day-window
    header, e.g. x-ratelimit-limit-requests-day) into the budget hash. Never raises.

    This is the QUOTA side of the pace/quota split: a day-window allowance is a CAP for
    this controller's arithmetic (remaining ÷ time-left); it must NEVER be divided into
    the per-second rate (that flattening is the metronome defect). The stored ``day_cap``
    outranks the published-table ``rpd`` (a measurement beats a table value, same rank
    doctrine as the minute floor) but NOT the operator's ``FL_DAILY_BUDGET_REQ``.

    Writes ``day_cap`` + ``day_window_s`` WITHOUT spending and WITHOUT touching the
    window state — a read-only-adjacent write on the same hash the take uses, so the
    cap applies from the very next take (the Lua re-persists cap on every take).
    """
    try:
        if RC is None or not isinstance(scope, RC.Scope):
            return False
        cld = RC.client()
        if cld is None:
            return False
        k = RC.key(NS_DAILY, scope)
        cld.hset(k, mapping={"day_cap": int(cap), "day_window_s": int(window_s)})
        return True
    except Exception as e:  # noqa: BLE001 — publishing must never break learning
        _note_fail("publish_day_cap", e)
        return False


def learned_day_cap(scope) -> Optional[dict]:
    """The learned daily allowance, or None. Read-only, never raises.

    ``{"day_cap": int, "day_window_s": int}`` from the budget hash. Absent/degraded →
    None (the caller falls back to the table ``rpd``). NOT the effective cap — the
    caller applies the precedence env > learned > table.
    """
    try:
        if RC is None or not isinstance(scope, RC.Scope):
            return None
        cld = RC.client()
        if cld is None:
            return None
        k = RC.key(NS_DAILY, scope)
        dc, ws = cld.hmget(k, "day_cap", "day_window_s")
        if dc is None:
            return None
        return {"day_cap": int(dc),
                "day_window_s": int(ws) if ws is not None else 86400}
    except Exception as e:  # noqa: BLE001 — no opinion, never break the caller
        _note_fail("learned_day_cap", e)
        return None
