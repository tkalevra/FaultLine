"""BRAIN METER — cheap, brain-scoped counters for cache hits/misses and deferred upkeep.

WHY THIS MODULE EXISTS
──────────────────────
The ingest cache (``src/api/llm_cache.py``) logs ``llm_cache.hit`` and NOTHING else. A hit is
therefore visible in the log stream while the hit RATE is not, and — the part that actually
matters to a tenant — "the cache is working" is indistinguishable from "nothing has exercised
it yet". Both render as silence. This module supplies the missing denominator.

⚠️ WHY REDIS AND NOT AN IN-PROCESS COUNTER (the load-bearing constraint — read before
"simplifying" this to a module-level dict).
The cache runs inside the **API** process (``src/api/llm_calls.py``, and the re_embedder
process). The console endpoint that displays these numbers, ``GET /tenant/brain-health``,
is mounted on the **MCP** process (``src/mcp/http_server.py:728`` includes
``saas.tenant_console``; ``src/api/main.py`` does NOT). They are separate containers on
production (``faultline-api`` / ``faultline-mcp``). An in-process counter written by the
cache is therefore **structurally invisible** to the endpoint that would read it.

What makes Redis the right answer rather than merely an available one: both containers
already share ONE Redis (verified on production — both carry
``REDIS_URL=redis://redis:6379/0`` and ``REDIS_COORD=true``), and the cache ALREADY writes
its entries there under the same brain scope. The counters ride the same client, the same
key builder, and the same scope — so a counter can never describe a different brain than
the cache entry it counts.

An in-process mirror is kept anyway (``local_snapshot``) because it costs nothing, works
when Redis is down, and is the only thing available in FOSS/no-Redis deployments. It is
explicitly NOT what the endpoint reads — see the docstring on ``local_snapshot``.

THE SCOPE IS THE BRAIN, NEVER WIDER (the isolation rule this feature lives or dies on)
──────────────────────────────────────────────────────────────────────────────────────
Every counter is keyed through ``redis_coord.key(ns, scope, ...)`` with the SAME
``redis_coord.Scope`` the cache, the rate budget and the circuit breaker use — the one
returned by ``scope_for_brain(cfg)``, whose identity tuple is
``(owner_kind, owner_id, base_url, model, sha256(api_key)[:16])``. That is what makes the
product rule hold BY CONSTRUCTION rather than by convention:

  * corporate account, one shared brain → every seat resolves the SAME cfg → the SAME scope
    → ONE shared counter. A tenant-wide number there is CORRECT; the seats genuinely share
    the brain, the ceiling and the cache.
  * house account, per-seat brains → each seat resolves its OWN cfg → DISTINCT scopes →
    ISOLATED counters even though the seats share an ``account_id``. Aggregating them would
    be a leak.
  * two customers on the same public endpoint+model → DISTINCT api keys → DISTINCT scopes.

``key()`` REFUSES a bare string by type, so a caller cannot accidentally build an unscoped
(and therefore cross-tenant) counter key. The failure mode is an exception at build time,
not a silent merge of two tenants' numbers.

ROLLING WINDOW, NOT A LIFETIME TOTAL
────────────────────────────────────
Counters are written into FIXED HOURLY BUCKETS and read back as a sum over the last N hours.
A single ever-incrementing pair would make the hit rate converge to a historical average and
stop responding to what the brain is doing NOW — useless for a live health bar. Bucketing
also means the data expires itself (TTL just over the retention window): there is no cleanup
job, and a brain that goes quiet decays to "no traffic" instead of reporting stale glory.

FOUR HARD CONTRACTS
───────────────────
1. **NEVER raise.** Every public function is wrapped. A counter failure must never surface
   in, slow, or abort an LLM call. ``record`` returns None on any error.
2. **NEVER gate anything.** Nothing in the product may branch on these numbers. They are a
   read-out, not a control input.
3. **ONE extra round trip, pipelined.** ``record`` issues INCR+EXPIRE in a single pipeline
   (one RTT to a local Redis, sub-millisecond) against an LLM call measured in hundreds of
   milliseconds to tens of seconds. The read path (``cache_get``'s GET) is left byte-for-byte
   unchanged — instrumentation must not alter the behaviour it measures.
4. **Absence is reported as absence.** ``read_window`` distinguishes "no Redis / cannot tell"
   (``available=False``) from "Redis is fine and this brain has genuinely done nothing"
   (``available=True``, all zeros). A health bar MUST NOT render those two states the same
   way, and it cannot tell them apart if this module flattens them into 0.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

try:  # pragma: no cover - import guard
    import structlog
    log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("brain_meter")


# The counter namespace. Distinct from the cache's own "llmc" so a counter key can never
# collide with a cached VALUE key (same scope, same Redis, different meaning).
_NS = "bmtr"

# The event kinds this module counts. A closed set: an unknown kind is refused rather than
# silently minting a new counter nobody reads.
KIND_HIT = "hit"        # the ingest cache served an answer — no outbound call was made
KIND_MISS = "miss"      # the cache was consulted for a cacheable op and had nothing
KIND_DEFER = "defer"    # ceiling admission deferred work to the background drain
KIND_ADMIT = "admit"    # ceiling admission let work proceed inline
# A provider rate-limit RESPONSE (HTTP 429) was received from the brain's endpoint. Bumped at
# the ONE chokepoint that observes every response (``llm_calls._observe_rate_headers``), so the
# count reflects 429s the provider actually SENT — not our internal deferrals. The brain-health
# bar renders this as "recent 429s" when non-zero, alongside the ceiling + its source.
KIND_RATE429 = "rate429"

_KINDS = frozenset({KIND_HIT, KIND_MISS, KIND_DEFER, KIND_ADMIT, KIND_RATE429})

# How many hourly buckets a read sums over, and therefore how long a bucket must live.
DEFAULT_WINDOW_HOURS = 24
_MAX_WINDOW_HOURS = 168  # one week — bounds the MGET fan-out


def _bucket_ttl_seconds(window_hours: int) -> int:
    """Bucket lifetime: the full window plus one hour of slack, so the oldest bucket in a
    read is still alive when it is read."""
    return int((window_hours + 1) * 3600)


def _hour_bucket(now: Optional[float] = None) -> int:
    return int((now if now is not None else time.time()) // 3600)


def enabled() -> bool:
    """True unless explicitly switched off.

    ``BRAIN_METER=false`` is the kill switch. Defaults ON: the counters are read-only
    instrumentation that gates nothing, and the failure mode of having them off is a health
    bar that cannot tell "idle" from "broken" — the exact gap this closes.
    """
    return str(os.environ.get("BRAIN_METER", "true")).strip().lower() not in (
        "false", "0", "no", "off")


# ──────────────────────────────────────────────────────────────────────────────
# IN-PROCESS MIRROR — a fallback view, NOT what the console reads
# ──────────────────────────────────────────────────────────────────────────────

_local_lock = threading.Lock()
_local: dict[tuple[str, str], int] = {}


def local_snapshot() -> dict[str, dict[str, int]]:
    """Process-local counts since start, ``{scope_str: {kind: n}}``.

    ⚠️ NOT the console's data source and never can be: the cache runs in the API process and
    ``GET /tenant/brain-health`` is served by the MCP process, so this dict is empty in the
    process that answers the request. It exists for (a) a Redis-down deployment, (b) FOSS
    single-process installs, and (c) tests that want to assert the hook fired without a Redis.
    Cheap, unbounded only by the number of distinct brains a single process has served.
    """
    with _local_lock:
        out: dict[str, dict[str, int]] = {}
        for (scope_s, kind), n in _local.items():
            out.setdefault(scope_s, {})[kind] = n
        return out


def reset_local() -> None:
    """Test hook — drop the in-process mirror."""
    with _local_lock:
        _local.clear()


# ──────────────────────────────────────────────────────────────────────────────
# WRITE
# ──────────────────────────────────────────────────────────────────────────────

def record(scope: Any, kind: str, window_hours: int = DEFAULT_WINDOW_HOURS) -> None:
    """Count ONE event of ``kind`` against ``scope``'s current hourly bucket. NEVER raises.

    ``scope`` must be a validated ``redis_coord.Scope`` (None → no-op: an unresolvable brain
    has no counter, and inventing one would attribute its traffic to the wrong tenant).
    """
    if not enabled():
        return
    if kind not in _KINDS or scope is None:
        return
    # The in-process mirror first — it is free, and it still records the event when Redis is
    # unreachable, which is exactly when the shared counter cannot.
    try:
        with _local_lock:
            k = (str(scope), kind)
            _local[k] = _local.get(k, 0) + 1
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.api import redis_coord
        c = redis_coord.client()
        if c is None:
            return
        # key() validates the scope BY TYPE — a bare string raises here rather than writing a
        # counter that two tenants could both resolve to.
        rkey = redis_coord.key(_NS, scope, kind, _hour_bucket())
        pipe = c.pipeline(transaction=False)
        pipe.incr(rkey)
        pipe.expire(rkey, _bucket_ttl_seconds(window_hours))
        pipe.execute()
    except Exception:  # noqa: BLE001 — instrumentation must NEVER break or slow a call
        return


# ──────────────────────────────────────────────────────────────────────────────
# READ
# ──────────────────────────────────────────────────────────────────────────────

def read_window(scope: Any, window_hours: int = DEFAULT_WINDOW_HOURS) -> dict:
    """Sum every counter for ``scope`` over the last ``window_hours``.

    Returns::

        {"available": bool, "window_hours": int,
         "hit": int, "miss": int, "defer": int, "admit": int, "rate429": int,
         "hit_rate": float|None, "observed": int}

    ``available`` is the honest-absence flag demanded by contract 4:

      * ``False`` → we could NOT read (no scope, Redis down, meter off). The caller must say
        "cannot tell", never "0%".
      * ``True`` with ``observed == 0`` → Redis answered and this brain has genuinely done
        nothing in the window. The caller should say "not used yet", never "0% — degraded".

    ``hit_rate`` is None when ``hit + miss == 0`` — a rate over no samples is not 0.0, and
    rendering it as 0.0 would paint an idle brain as a totally failing cache.

    ``rate429`` is the count of provider 429 responses observed in the window. It is NOT part
    of ``observed`` (which counts cache lookups + admission decisions, not rate responses) so a
    rate-limited-but-cached brain does not read as "busy".
    """
    empty = {"available": False, "window_hours": window_hours,
             "hit": 0, "miss": 0, "defer": 0, "admit": 0, "rate429": 0,
             "hit_rate": None, "observed": 0}
    if not enabled() or scope is None:
        return empty
    try:
        hours = max(1, min(int(window_hours or DEFAULT_WINDOW_HOURS), _MAX_WINDOW_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_WINDOW_HOURS
    empty["window_hours"] = hours
    try:
        from src.api import redis_coord
        c = redis_coord.client()
        if c is None:
            return empty
        now_bucket = _hour_bucket()
        buckets = [now_bucket - i for i in range(hours)]
        # rate429 is summed but NOT folded into ``observed`` (it counts provider responses, not
        # cache/admission activity), so it is iterated separately below.
        kinds = (KIND_HIT, KIND_MISS, KIND_DEFER, KIND_ADMIT, KIND_RATE429)
        # ONE round trip for the whole window: build every key, MGET once.
        keys: list[str] = []
        for kind in kinds:
            for b in buckets:
                keys.append(redis_coord.key(_NS, scope, kind, b))
        raw = c.mget(keys)
        out = dict(empty)
        out["available"] = True
        idx = 0
        for kind in kinds:
            total = 0
            for _ in buckets:
                v = raw[idx] if idx < len(raw) else None
                idx += 1
                if v:
                    try:
                        total += int(v)
                    except (TypeError, ValueError):
                        pass
            out[kind] = total
        hits, misses = out[KIND_HIT], out[KIND_MISS]
        looked = hits + misses
        out["hit_rate"] = (hits / looked) if looked else None
        out["observed"] = looked + out[KIND_DEFER] + out[KIND_ADMIT]
        return out
    except Exception:  # noqa: BLE001 — a monitoring read must never break its own console
        return empty
