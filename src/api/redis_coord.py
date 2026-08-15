"""Cross-process coordination over Redis — ONE client, ONE key builder, scoped keys.

WHY THIS MODULE EXISTS
──────────────────────
A module-level object is PER-PROCESS state, and this deployment runs more than one
process against the same configured LLM. The canonical entrypoint (docker-entrypoint.sh)
backgrounds TWO Python processes in one container — ``uvicorn src.api.main:app`` (the
API) and ``python -m src.re_embedder.embedder`` (the re-embedder) — so any in-memory
budget, bucket, or counter is enforced once per process and the deployment as a whole
sends a MULTIPLE of what any single process believes it is sending. A provider limit
of N requests/minute is not "paced at N" when two processes each pace at N: it is 2N.

This module is the convergence point for state that must be shared across those
processes:

* ``client()``           — one cached, timeout-bounded, fail-safe connection.
* ``Scope``              — a value object you cannot construct from a bare string, so
                           an unscoped key is not merely discouraged, it is
                           **unrepresentable**.
* ``key()``              — the only sanctioned key builder. Rejects a ``str`` scope by
                           TYPE.
* ``take_token_state()`` — an ATOMIC token-bucket take (Lua) for the shared outbound
                           rate budget.
* ``try_take_daily()``   — an ATOMIC check-and-spend (Lua) for the shared daily request
                           budget, with a ``force`` mode that counts without ever
                           refusing.

THREE HARD RULES
────────────────
1. **Redis is OPTIONAL.** Absent, unreachable, or erroring → every entry point here
   returns the "no shared opinion" answer and the caller keeps its in-process
   behaviour. A coordination outage must never become a serving outage. PostgreSQL
   remains the authoritative store; this coordinates, it does not decide.
2. **Every key is scoped.** ``key()`` takes a ``Scope``, never a string. The factories
   validate and raise ``UnscopedKeyError`` on anything empty, placeholder-ish, or
   unknown — a key two different deployments could resolve to is refused.
3. **Atomicity is Redis's own guarantee.** The take scripts run as single Lua scripts:
   per the Redis documentation ("Scripting with Lua"), "Redis guarantees the script's
   atomic execution" — two processes cannot double-spend the same token or the same
   daily slot, and a concurrent reader sees either the pre- or post-state, never a
   torn one.

WHAT IS DELIBERATELY *NOT* HERE
──────────────────────────────
No throttling policy. Which lane defers (background) and which fails open
(interactive) is the caller's decision (see ``src/api/llm_lane.py`` and
``src/api/llm_rate.py``); this module is only the shared-state core.

PRIVACY NOTE
────────────
An LLM endpoint URL can carry an API key in the query string (some providers do
this). Endpoint identity therefore enters a key ONLY as a salted digest —
``scope_for_endpoint`` hashes, and the plaintext URL is never written to Redis nor
logged from this module. ``safe_redis_url()`` exists so startup banners can show
WHERE coordination points without leaking credentials embedded in the URL.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

try:  # pragma: no cover - import guard: redis is a core dep, guarded for import-safety
    import redis as _redis
except Exception:  # pragma: no cover
    _redis = None  # type: ignore

try:  # pragma: no cover
    import structlog
    log = structlog.get_logger()
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("redis_coord")


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
_DEFAULT_URL = "redis://localhost:6379/0"


def _truthy(v: Optional[str], default: bool = True) -> bool:
    raw = str(v or "").strip().lower()
    if not raw:
        return default
    return raw not in ("false", "0", "no", "off")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def redis_url() -> str:
    return os.environ.get("REDIS_URL", "").strip() or _DEFAULT_URL


def enabled() -> bool:
    """Master lever. REDIS_COORD=false opts the deployment out of coordination
    entirely (each process keeps its own in-process state); default ON because the
    canonical deployment ships a Redis service and the shared outbound budget is
    load-bearing, not an optimisation."""
    return _truthy(os.environ.get("REDIS_COORD"), default=True)


def safe_redis_url() -> str:
    """The coordination URL with any credentials stripped, for logs and banners."""
    try:
        parts = urlsplit(redis_url())
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:  # noqa: BLE001 — a banner must never crash startup
        return "(unparseable REDIS_URL)"


def _socket_timeout() -> float:
    # Bounded so a wedged Redis can never add unbounded latency to an LLM call.
    return _num("REDIS_COORD_SOCKET_TIMEOUT", 1.0)


# ──────────────────────────────────────────────────────────────────────────────
# CLIENT — cached, fail-safe, never raises outward
# ──────────────────────────────────────────────────────────────────────────────
_client: Any = None
_client_built = False
_client_lock = threading.Lock()
_last_fail_log = 0.0
_next_retry = 0.0
_RETRY_AFTER_S = 60.0


def client() -> Any:
    """The shared client, or None. NEVER raises — None means "no shared opinion
    available" and every caller keeps its in-process behaviour.

    A FAILED build is retried at most once per _RETRY_AFTER_S: the canonical
    deployment starts the coordination service and the two Python processes at the
    same moment, so the first connect can easily lose that race — caching the
    failure forever would pin the deployment to per-process budgets for its whole
    lifetime (exactly the multiplication this module exists to close) over a
    sub-second startup ordering. The throttle keeps a truly dead Redis cheap: at
    most one reconnect attempt per minute, never a hot reconnect loop.
    """
    global _client, _client_built, _next_retry
    if not enabled():
        return None
    # Fast path checks the CLIENT, never the built FLAG: a concurrent thread can be
    # mid-build holding the lock with _client_built already True and _client still
    # None, and keying the fast path on the flag would hand every other thread a
    # spurious "no shared opinion" for the lifetime of the cache (the failure is
    # cached). Keying on _client means a not-yet-built client falls through to the
    # lock and WAITS for the builder instead of racing it.
    if _client is not None:
        return _client
    now = time.time()
    with _client_lock:
        if _client is not None:
            return _client
        if now < _next_retry:
            # A recent build FAILED and the retry window has not elapsed. The
            # window is armed by the failing build itself, so the first failure
            # (not just the second caller) starts the clock.
            return None
        _next_retry = now + _RETRY_AFTER_S
        _client_built = True
        if _redis is None:
            log.warning("redis_coord.library_missing")
            _client = None
            return None
        try:
            c = _redis.from_url(
                redis_url(),
                decode_responses=True,
                socket_connect_timeout=_socket_timeout(),
                socket_timeout=_socket_timeout(),
                health_check_interval=30,
            )
            c.ping()
            _client = c
            log.info("redis_coord.connected", url=safe_redis_url())
        except Exception as e:  # noqa: BLE001 — Redis is OPTIONAL, by contract
            _note_degraded("connect", e)
            _client = None
    return _client


def reset_client() -> None:
    """Test/diagnostic hook — drop the cached client so a new URL/flag takes effect."""
    global _client, _client_built, _next_retry
    with _client_lock:
        _client = None
        _client_built = False
        _next_retry = 0.0
    # Cached Lua Script objects are bound to the client they were registered on —
    # drop them with the client or a swapped URL would run against a dead connection.
    _reset_scripts()


def _note_degraded(op: str, exc: BaseException) -> None:
    """Log a Redis failure LOUDLY but at most once a minute (a dead Redis fails hot).

    The FIRST failure in a process always logs: the throttle starts "long ago"
    (0.0 would swallow the first minute's worth of failures entirely, hiding the
    very transition into degradation that an operator most needs to see).
    """
    global _last_fail_log
    now = time.time()
    if _last_fail_log > 0.0 and now - _last_fail_log < 60:
        return
    _last_fail_log = now
    log.warning("redis_coord.degraded_no_coordination",
                op=op, error_type=type(exc).__name__, error=str(exc)[:200],
                note="coordination is unavailable; every caller keeps its in-process "
                     "behaviour. This is a DEGRADATION, not a failure — but shared "
                     "budgets (rate, daily) are NOT in force while it lasts.")


# ──────────────────────────────────────────────────────────────────────────────
# SCOPE — an unscoped key must be UNREPRESENTABLE, not merely discouraged
# ──────────────────────────────────────────────────────────────────────────────
class UnscopedKeyError(ValueError):
    """Raised when a key would be built without a real, validated scope."""


# Values that LOOK like a scope but are the absence of one. A caller that passes any
# of these has a resolution bug upstream; silently accepting it writes one
# deployment's state to a key another could also resolve to. Fail loud.
_PLACEHOLDERS = frozenset({
    "none", "null", "nil", "undefined", "unknown", "anonymous", "default",
    "all", "any", "global", "shared", "*", "-", "n/a", "na", "test", "user",
})

_SCOPE_TOKEN = "\x00scope\x00"  # unforgeable-ish marker; a plain str can never carry it


class Scope:
    """A validated coordination scope. Construct ONLY via the module factories.

    ``key()`` accepts a ``Scope`` and rejects a ``str`` by type, so the poison case
    (``key("llmrate", "")`` / ``key("llmrate", None)``) cannot reach Redis — it
    raises before a command is issued.
    """

    __slots__ = ("kind", "ident", "_tok")

    def __init__(self, kind: str, ident: str, _tok: str = "") -> None:
        if _tok != _SCOPE_TOKEN:
            raise UnscopedKeyError(
                "Scope() is not directly constructible — use scope_for_endpoint() "
                "or scope_single_deployment() so the value is VALIDATED."
            )
        self.kind = kind
        self.ident = ident
        self._tok = _tok

    def __str__(self) -> str:
        return f"{self.kind}.{self.ident}"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Scope {self.kind}.{self.ident}>"

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Scope)
                and other.kind == self.kind and other.ident == self.ident)

    def __hash__(self) -> int:
        return hash((self.kind, self.ident))


def _validate(raw: Any, what: str) -> str:
    if raw is None:
        raise UnscopedKeyError(f"{what} is None — refusing to build an unscoped key")
    s = str(raw).strip()
    if not s:
        raise UnscopedKeyError(f"{what} is empty — refusing to build an unscoped key")
    if s.lower() in _PLACEHOLDERS:
        raise UnscopedKeyError(
            f"{what}={s!r} is a placeholder, not a real scope — refusing to build a "
            f"key that two different callers could resolve to"
        )
    return s


def _digest(raw: str, n: int = 20) -> str:
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:n]


def scope_for_endpoint(base_url: Any, model: Any = "") -> Scope:
    """An LLM ENDPOINT's identity (base URL + model), hashed.

    This is the grain a provider meters: everyone configured against the same
    endpoint shares whatever ceiling that endpoint enforces, so the shared budget is
    keyed exactly there. Hashed, never plaintext: some providers carry the API key
    inside the URL, and this value would otherwise be written to Redis and appear in
    key dumps. A changed endpoint or model yields a DIFFERENT scope — the budget
    follows the brain actually being called, and a stale pace from a previous
    endpoint cannot outlive the switch.
    """
    url = _validate(base_url, "base_url")
    mdl = str(model or "").strip()
    return Scope("brain", _digest(f"{url.rstrip('/')}|{mdl}"), _SCOPE_TOKEN)


def scope_single_deployment() -> Scope:
    """The single-deployment scope — one brain, one operator, every caller shares it.

    This exists so a deployment that cannot resolve an endpoint has an EXPLICIT scope
    instead of an empty one. It is a deliberate act to call it — you cannot arrive
    here by passing ``""`` to a factory.
    """
    return Scope("solo", "single_deployment", _SCOPE_TOKEN)


_NS_RE_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def key(namespace: str, scope: Scope, *parts: Any) -> str:
    """THE key builder. ``fl:{namespace}:{scope.kind}:{scope.ident}[:{parts...}]``.

    Raises ``UnscopedKeyError`` unless ``scope`` is a validated ``Scope`` — passing a
    bare string is refused, because the point of the type is that validation happened
    somewhere auditable.
    """
    if not isinstance(scope, Scope):
        raise UnscopedKeyError(
            f"key() requires a Scope, got {type(scope).__name__}. Build one with "
            f"scope_for_endpoint() or scope_single_deployment()."
        )
    ns = str(namespace or "").strip().lower()
    if not ns or not set(ns) <= _NS_RE_OK:
        raise UnscopedKeyError(f"namespace={namespace!r} must be [a-z0-9_]+")
    tail = ":".join(_validate(p, "key part") for p in parts) if parts else ""
    base = f"fl:{ns}:{scope.kind}:{scope.ident}"
    return f"{base}:{tail}" if tail else base


# ──────────────────────────────────────────────────────────────────────────────
# CAPABILITY — SHARED OUTBOUND RATE BUDGET (the endpoint's TOTAL call count)
# ──────────────────────────────────────────────────────────────────────────────
# A token bucket held in a module dict is PER-PROCESS. The canonical deployment runs
# TWO processes (the API and the re-embedder) against the SAME configured LLM, so a
# configured ceiling of N requests/minute is enforced twice INDEPENDENTLY and real
# combined outbound is up to 2N — each process behaves perfectly while the deployment
# misbehaves, which is exactly why the defect is invisible from inside either one.
#
# This makes the budget an ENDPOINT-WIDE total: one bucket in Redis, refilled by
# elapsed time, decremented atomically in Lua so two processes cannot double-spend
# the same token.
#
# FAIL-**OPEN**, deliberately: Redis down/absent → returns "no shared opinion" and
# the caller keeps its in-process bucket. A coordination outage must never become a
# serving outage. The caller is responsible for ANNOUNCING that fallback loudly at
# startup (see llm_rate.announce) — a silent fallback re-ships the per-process
# multiplication to everyone who self-hosts.

_NS_RATE = "llmrate"

# Refill by elapsed time, then take one token if the balance allows. Returns
# {wait_ms} (0 = token granted). Single round-trip, atomic, so concurrent processes
# cannot double-spend the same token.
#   KEYS[1] bucket   ARGV[1] now_ms  ARGV[2] rate_per_sec  ARGV[3] capacity  ARGV[4] ttl_s
#
# PERSISTS rate+cap INTO THE HASH so read-only probes can answer "is there capacity?"
# and "when does a token mint?" WITHOUT spending a token and WITHOUT a second source
# of truth. now_ms is passed in by the Python side from time.time()*1000, keeping the
# script deterministic and identical across callers.
_LUA_RATE_TAKE = """
local st = redis.call('hmget', KEYS[1], 'tokens', 'ts')
local now = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local tokens = tonumber(st[1])
local ts = tonumber(st[2])
if tokens == nil or ts == nil then
  tokens = cap
  ts = now
end
local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(cap, tokens + (elapsed * rate))
local wait_ms = 0
if tokens >= 1.0 then
  tokens = tokens - 1.0
else
  if rate <= 0 then
    return {-1}
  end
  wait_ms = math.ceil(((1.0 - tokens) / rate) * 1000.0)
end
redis.call('hmset', KEYS[1], 'tokens', tokens, 'ts', now, 'rate', rate, 'cap', cap)
redis.call('expire', KEYS[1], ttl)
return {wait_ms}
"""

_scripts: dict[str, Any] = {}
_script_lock = threading.Lock()


def _script(c: Any, name: str, body: str) -> Any:
    """Register (once) a named Lua script. The client library handles
    EVALSHA/NOSCRIPT re-loading."""
    s = _scripts.get(name)
    if s is None:
        with _script_lock:
            s = _scripts.get(name)
            if s is None:
                s = c.register_script(body)
                _scripts[name] = s
    return s


def _reset_scripts() -> None:
    """Test hook — a new client must re-register its scripts against the new connection."""
    with _script_lock:
        _scripts.clear()


def shared_rate_enabled() -> bool:
    """The shared outbound rate budget's master lever. (Coordination reachability
    is enforced separately by ``client()`` returning None → callers fail open to
    their in-process bucket — see ``take_token_state``.)"""
    return enabled()


def take_token_state(scope: Scope, rate_per_sec: float, capacity: float) -> Optional[dict]:
    """Spend one token from the ENDPOINT-WIDE budget shared by every process.

    Returns:
        ``{"wait_s": 0.0}``       — token granted, caller may send now.
        ``{"wait_s": >0.0}``      — seconds until a token mints (not spent).
        ``None``                  — no shared opinion (disabled, Redis down, or a
                                    limiter error). The caller MUST fall back to its
                                    in-process bucket; never treat None as "granted"
                                    and never as "denied".

    A refused take spends nothing — the token is only decremented when it is actually
    taken, so a pacing wait never burns budget it did not use. NEVER raises.
    """
    if not shared_rate_enabled():
        return None
    if rate_per_sec <= 0:
        return None
    c = client()
    if c is None:
        return None
    k = key(_NS_RATE, scope, "bucket")   # scope validated BEFORE touching the client
    try:
        # TTL: generous enough that an idle deployment's balance is not reset mid-pace,
        # bounded so a forgotten key cannot linger forever.
        ttl = max(60, int((capacity / rate_per_sec) * 4) if rate_per_sec > 0 else 60)
        out = _script(c, "rate_take", _LUA_RATE_TAKE)(
            keys=[k],
            args=[int(time.time() * 1000), float(rate_per_sec), float(max(1.0, capacity)), ttl],
        )
        wait_ms = float(out[0])
        if wait_ms < 0:
            return None
        return {"wait_s": wait_ms / 1000.0}
    except Exception as e:  # noqa: BLE001 — fail-OPEN to the local bucket, never lose a call
        _note_degraded("take_token_state", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# CAPABILITY — SHARED DAILY REQUEST BUDGET (the paced per-day core)
# ──────────────────────────────────────────────────────────────────────────────
# Headroom on a per-minute token bucket is BURST-SMOOTHING control: right for
# per-minute axes, wrong for per-day axes. Applied to a daily quota it only moves the
# moment the budget dies ("exhausted at 16:00 instead of 17:00") and then stalls hard
# at the wall. A daily quota against known daily volume is a PACING problem, and
# pacing needs SHARED state: a per-process daily counter is N counters each free to
# spend the whole quota.
#
# Realized here as a slow budget the BACKGROUND lane defers against (see llm_rate):
# the sweep drips through the window and finishes as the window closes, instead of
# racing the ceiling and parking.
#
# * ``used`` lives in a Redis hash, NOT memory — an in-memory counter resets to full
#   on every restart and blows the quota. The key TTL is >= 2x the window and is
#   refreshed on every take, so daily state can never expire mid-window.
# * ROLLING window: the window opens at the first spend of the epoch and rolls
#   ``window_s`` later. No UTC-midnight assumption anywhere — we do not know the
#   provider's reset time and refuse to guess it.
# * ``cap``/``window_s`` are the CURRENT operator opinion, re-persisted on EVERY take
#   — a changed cap applies on the very next take, no restart, no migration.
# * Unknown stays unknown: there is NO invented daily cap here. The caller supplies
#   the cap (operator env); when the operator has not set one, NO daily gate applies.

_NS_DAILY = "llmdaily"

# Atomic check-and-spend, one round-trip. Mirrors the rate-take shape.
#   KEYS[1] budget hash   ARGV[1] now_ms  ARGV[2] cap  ARGV[3] window_s
#   ARGV[4] cost          ARGV[5] ttl_s (>= 2*window_s)   ARGV[6] force (1/0)
# Returns {1, used, remaining} on a grant and {0, reset_ms, 0} on a refusal.
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
-- the shared arithmetic reflects the endpoint's REAL usage. An exhausted day must not
-- make interactive traffic invisible to the background pacer.
if force == 1 or used + cost <= cap then
  used = used + cost
  redis.call('hset', KEYS[1], 'used', used)
  return {1, used, math.max(cap - used, 0)}
end
return {0, wsm + win_s * 1000, 0}
"""


def daily_budget_enabled() -> bool:
    return enabled()


def try_take_daily(scope: Scope, cap: int, window_s: int, cost: int = 1,
                   force: bool = False) -> dict:
    """Spend ``cost`` against the endpoint's DAILY budget. NEVER raises.

    Semantics (single atomic Lua round-trip):
      * key absent          → window_start_ms=now_ms, used=0, then evaluate.
      * window expired      → ROLLING RESET before evaluating, so a late spender
                              never inherits a stale window.
      * used + cost <= cap  → ``{"ok": True, "used": .., "remaining": ..}``.
      * else                → ``{"ok": False, "reset_ms": .., "remaining": 0}`` —
                              the caller defers until reset_ms.
      * ``force=True``      → COUNT the spend and return ok True EVEN PAST CAP. For
                              the INTERACTIVE lane: a user is never deferred, but
                              their usage MUST still land in the shared arithmetic —
                              an exhausted day that stops counting interactive traffic
                              under-reports the endpoint and lets background pace
                              into a wall interactive already spent.

    ``cap``/``window_s`` are ALWAYS re-persisted (a changed cap applies immediately).
    ``cap <= 0`` → ``{"ok": False, "reason": "no_cap"}`` (with a real cap in hand,
    fail toward deferral, not toward spending).

    Redis absent/unreachable/script error → ``{"ok": True, "degraded": True}``
    (fail-open: the per-minute bucket still caps outbound while the daily figure is
    unenforced). A non-Scope ``scope`` is likewise degraded, never an exception.
    """
    try:
        if not daily_budget_enabled():
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
            w = 86400
        try:
            n = int(cost)
        except (TypeError, ValueError):
            n = 1
        if n <= 0:
            n = 1
        if not isinstance(scope, Scope):
            return {"ok": True, "degraded": True}
        cld = client()
        if cld is None:
            return {"ok": True, "degraded": True}
        # Scope validated BEFORE touching the client, and the key built by THE
        # sanctioned builder — same shape as the rate bucket keys.
        k = key(_NS_DAILY, scope)
        ttl = 2 * w
        out = _script(cld, "daily_take", _LUA_DAILY_TAKE)(
            keys=[k],
            args=[int(time.time() * 1000), c, w, n, ttl, 1 if force else 0],
        )
        ok = int(out[0])
        if ok:
            return {"ok": True, "used": int(out[1]), "remaining": int(out[2])}
        return {"ok": False, "reset_ms": int(out[1]), "remaining": 0}
    except Exception as e:  # noqa: BLE001 — fail-OPEN: never lose a call to a Redis blip
        _note_degraded("try_take_daily", e)
        return {"ok": True, "degraded": True}


def peek_daily(scope: Scope) -> Optional[dict]:
    """Read-only snapshot of the endpoint's daily budget, or None. NEVER raises,
    never writes, never spends.

    ``{"used", "cap", "remaining", "window_start_ms", "reset_ms"}`` from the shared
    hash. None when no state exists yet, the scope is not a Scope, or Redis is
    degraded. The caller uses this to defer BEFORE a long token-bucket wait;
    verification uses it to evidence budget state. Reports the stored window as-is
    (a window past reset simply reads stale until the next take rolls it — a read
    must not mutate).
    """
    try:
        if not daily_budget_enabled() or not isinstance(scope, Scope):
            return None
        cld = client()
        if cld is None:
            return None
        k = key(_NS_DAILY, scope)
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
        _note_degraded("peek_daily", e)
        return None
