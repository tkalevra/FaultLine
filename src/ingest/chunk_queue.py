"""PARALLEL — a Redis-backed, lease-claimed WORK QUEUE for the ingest lanes.

THE MEASUREMENT THIS MODULE EXISTS FOR
--------------------------------------
Document ingest runs at ~11 seconds per chunk. Measured end-to-end through the worker seam
(18 chunks / 34 extraction lanes, two clean tenants, ``DOC_CHUNK_CONCURRENCY=2``):
``legacy 930.3s / 19 chunk-lane failures`` vs ``fixed 202.4s / 5 failures``. Scaled, a
200-chunk document is ~19 minutes and a ~1678-chunk corpus is ~2.8 hours — for the FIRST
thing a new tenant does, which is dump a pile of documents at us.

Two levers exist, and the LLM is NOT one of them: the deployment's configured endpoint is
a given, and so is its capacity. The levers are **parallelism** and
**calls-per-chunk**. This module is the first.

WHAT WAS ACTUALLY WRONG WITH THE OLD SHAPE
------------------------------------------
``drain_pending_documents`` fanned a document's chunks across a ``ThreadPoolExecutor`` and
waited on ``f.result()`` for all of them. That is parallel, but it is not a QUEUE, and the
difference is the whole point:

  * **No unit-of-work claim.** The unit of redelivery was the DOCUMENT. A worker that died
    mid-drain stranded every chunk it had already completed as well as the ones it had not,
    and a reclaim re-ran all of them.
  * **A failed chunk BURNED.** ``chunks_failed > 0`` finalized the document TERMINAL
    ``'partial'`` with no re-mine path. ~1000 chunks were burned that way in one day.
  * **One global concurrency constant.** ``DOC_CHUNK_CONCURRENCY`` is one number for every
    tenant — a number correct for a hosted API is hostile to a 4-slot self-hosted box.



DESIGN — TWO DURABILITY LAYERS, DELIBERATELY
--------------------------------------------
Redis is the COORDINATION layer, never the system of record:

  1. **Postgres is the ledger.** ``documents.chunks`` holds the verbatim text and
     ``documents.chunk_state`` (migration 205) holds each chunk's terminal state. A total
     process loss costs at most the un-flushed tail of ``chunk_state``: the document lease
     expires, the row is reclaimed, and only the chunks with no terminal state are re-queued.
  2. **Redis is the claim.** Within a run, an item is claimed with a VISIBILITY TIMEOUT
     (ready LIST → in-flight ZSET scored by deadline). A worker that dies holding a claim does
     not take the item with it: the lease expires and any reaper returns it to ready with
     ``attempts + 1``. Redelivery is BOUNDED — past ``max_attempts`` the item goes to a
     dead-letter list and is reported, never looped forever (Hohpe & Woolf, *Enterprise
     Integration Patterns*, "Guaranteed Delivery" + "Dead Letter Channel"; Nygard,
     *Release It!* 2nd ed., "Circuit Breaker" for the outage-vs-item distinction).

REDIS IS OPTIONAL, ALWAYS. ``available()`` probes it once per process (cached, short TTL) and
every entry point degrades to the caller's existing serial/pool behaviour rather than failing.
A memory lane must never be harder to run than the thing it accelerates.

EXTENSIBILITY — THE ITEM TYPE IS THE EXTENSION POINT
-----------------------------------------------------
A queue item is ``{kind, tenant, batch, key, payload, attempts}``. ``kind`` selects a
registered handler; NOTHING in this module knows what a "chunk" is. The artefact/caption lane
(``src/ingest/artefacts.py``, migration 203), which is blocked today only on there being no
binary intake path, slots in later as::

    register_handler("artefact_extract", _run_artefact_item)
    q.enqueue([WorkItem(kind="artefact_extract", tenant=uid, batch=f"doc:{doc_id}",
                        key=f"art:{artefact_id}", payload={...})])

— same claim, same lease, same redelivery, same per-tenant concurrency, no structural change.
Mixed-kind batches are supported: a document batch may carry text chunks AND artefact items,
and the completion condition (queue drained) is unchanged.

⚠️ PER-TENANT ISOLATION. Every key is scoped by ``tenant`` (the tenant's user uuid) and by
``batch``; there is no cross-tenant key and no shared queue. This module never opens a database
connection and never touches ``search_path``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from src.api import errors as _errors  # THE ONE ERROR SEAM

try:  # pragma: no cover - import guard; redis is a core dep, guarded for import-safety
    import redis as _redis
except Exception:  # pragma: no cover
    _redis = None  # type: ignore

import logging

log = logging.getLogger(__name__)


# ── configuration (env-tunable, never a bare literal at a call site) ──────────────────

# Same default URL as src/api/idempotency.py — one Redis, one pattern.
_REDIS_URL = os.getenv("REDIS_URL", "redis://faultline-redis:6379/0")

# Key namespace. Distinct prefix from the idempotency cache (`idem:`) and the re_embedder's
# `faultline:queue:` list so a FLUSHDB-free purge of one lane cannot touch another.
_NS = os.getenv("CHUNK_QUEUE_NAMESPACE", "flcq")

# How long a claim is honoured before ANY reaper may return the item to ready. Must comfortably
# exceed the worst-case processing time of one item, or a healthy-but-slow item is redelivered
# while still running (at-least-once becomes at-least-twice for no reason). The document lane's
# per-chunk HTTP budget is _DOC_CHUNK_HTTP_TIMEOUT + _DOC_INGEST_HTTP_TIMEOUT (180 + 180s
# today), so the default sits above their sum.
_LEASE_SECONDS = float(os.getenv("CHUNK_QUEUE_LEASE_SECONDS", "420"))

# Bounded redelivery per ITEM (distinct from the DOCUMENT-level _DOC_MAX_ATTEMPTS). Past this
# the item is dead-lettered and reported — a poison chunk cannot loop forever.
_MAX_ATTEMPTS = max(1, int(os.getenv("CHUNK_QUEUE_MAX_ATTEMPTS", "3")))

# Socket budget for a queue operation. Small on purpose: a queue op is O(1) and a slow Redis
# must degrade to "no queue" rather than becoming a new latency source.
_SOCKET_TIMEOUT = float(os.getenv("CHUNK_QUEUE_SOCKET_TIMEOUT", "3"))

# How long an availability probe result is trusted (seconds). Bounds both the cost of probing
# and how long a recovered Redis stays unused.
_AVAILABILITY_TTL = float(os.getenv("CHUNK_QUEUE_AVAILABILITY_TTL", "30"))

# Wall-clock ceiling for one run_workers() call, as a MULTIPLE of the lease. Defence against a
# handler that neither returns nor raises; never the primary bound.
_RUN_DEADLINE_LEASES = float(os.getenv("CHUNK_QUEUE_RUN_DEADLINE_LEASES", "8"))


class QueueUnavailable(RuntimeError):
    """Redis could not be reached. Callers DEGRADE — they never fail the ingest on this."""


# ── the work item ────────────────────────────────────────────────────────────────────


@dataclass
class WorkItem:
    """One unit of work.

    ``kind``    selects the handler (the extension point — see the module docstring).
    ``tenant``  the tenant's user uuid; scopes every Redis key. Never optional.
    ``batch``   groups items that finish together (e.g. ``doc:41``); scopes the keys too, so a
                purge/enumerate of one document cannot disturb another.
    ``key``     stable identity WITHIN the batch (e.g. ``chunk:7``). Used by the caller to
                reconcile results back onto its own ledger; the queue treats it as opaque.
    ``payload`` everything the handler needs. Carried IN the item, not looked up, so a worker
                in another PROCESS can run it with no shared state.
    ``id``      unique per delivery attempt. Load-bearing: the in-flight set is a ZSET, so two
                items with identical JSON would collapse into one member.
    """

    kind: str
    tenant: str
    batch: str
    key: str
    payload: dict = field(default_factory=dict)
    attempts: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id, "kind": self.kind, "tenant": self.tenant,
            "batch": self.batch, "key": self.key, "attempts": self.attempts,
            "payload": self.payload,
        }, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def from_json(raw: str) -> "WorkItem":
        d = json.loads(raw)
        return WorkItem(kind=d.get("kind", ""), tenant=d.get("tenant", ""),
                        batch=d.get("batch", ""), key=d.get("key", ""),
                        payload=d.get("payload") or {}, attempts=int(d.get("attempts") or 0),
                        id=d.get("id") or uuid.uuid4().hex)


# ── handler registry (the extension point) ───────────────────────────────────────────

_HANDLERS: dict[str, Callable[[WorkItem], Any]] = {}


def register_handler(kind: str, fn: Callable[[WorkItem], Any]) -> None:
    """Register the callable that processes items of ``kind``. Idempotent (last wins)."""
    _HANDLERS[str(kind)] = fn


def get_handler(kind: str) -> Optional[Callable[[WorkItem], Any]]:
    return _HANDLERS.get(str(kind))


# ── availability probe ───────────────────────────────────────────────────────────────

_probe_cache: tuple[float, bool] = (0.0, False)


def available(redis_url: Optional[str] = None, force: bool = False) -> bool:
    """Is Redis usable right now? Cached for ``_AVAILABILITY_TTL``. NEVER raises.

    This is the gate every caller checks before choosing the queue lane. False → the caller
    keeps doing exactly what it does today."""
    global _probe_cache
    if _redis is None:
        return False
    now = time.monotonic()
    if not force and now < _probe_cache[0]:
        return _probe_cache[1]
    ok = False
    try:
        client = _redis.from_url(redis_url or _REDIS_URL, decode_responses=True,
                                 socket_timeout=_SOCKET_TIMEOUT,
                                 socket_connect_timeout=_SOCKET_TIMEOUT)
        ok = bool(client.ping())
    except Exception as e:  # noqa: BLE001 — an optional accelerator never raises at its gate
        log.info(f"chunk_queue.redis_unavailable ({type(e).__name__}) — "
                 f"degrading to the in-process lane")
        ok = False
    _probe_cache = (now + _AVAILABILITY_TTL, ok)
    return ok


# ── the queue ────────────────────────────────────────────────────────────────────────

# Atomic CLAIM: pop the head of `ready` and register it in `inflight` with its deadline, in one
# server-side step. Without this, a crash between LPOP and ZADD loses the item outright — the
# exact failure this whole module exists to remove.
_LUA_CLAIM = """
local v = redis.call('lpop', KEYS[1])
if not v then return false end
redis.call('zadd', KEYS[2], ARGV[1], v)
return v
"""

# Atomic RETURN-TO-READY / DEAD-LETTER. The ZREM guard is what makes it safe for two reapers to
# race: exactly one of them removes the member, and only that one re-pushes. The other no-ops.
_LUA_MOVE = """
if redis.call('zrem', KEYS[1], ARGV[1]) == 0 then return 0 end
redis.call('rpush', KEYS[2], ARGV[2])
return 1
"""

_LUA_ACK = """
return redis.call('zrem', KEYS[1], ARGV[1])
"""


class RedisWorkQueue:
    """A reliable (claim / lease / bounded-redelivery / dead-letter) queue over plain Redis.

    Deliberately NOT Redis Streams: consumer groups add operational surface (XAUTOCLAIM,
    group creation/trim, per-group state) for semantics we get in three small Lua scripts,
    and this queue must be able to vanish entirely (Redis down) without ceremony."""

    def __init__(self, redis_url: Optional[str] = None, namespace: str = _NS,
                 lease_seconds: float = _LEASE_SECONDS, max_attempts: int = _MAX_ATTEMPTS,
                 client: Any = None):
        self.namespace = namespace
        self.lease_seconds = float(lease_seconds)
        self.max_attempts = max(1, int(max_attempts))
        if client is not None:
            self.client = client
        else:
            if _redis is None:
                raise QueueUnavailable("redis library not installed")
            try:
                self.client = _redis.from_url(
                    redis_url or _REDIS_URL, decode_responses=True,
                    socket_timeout=_SOCKET_TIMEOUT, socket_connect_timeout=_SOCKET_TIMEOUT)
                self.client.ping()
            except Exception as e:  # noqa: BLE001
                raise QueueUnavailable(f"{type(e).__name__}: {e}") from e
        try:
            self._claim = self.client.register_script(_LUA_CLAIM)
            self._move = self.client.register_script(_LUA_MOVE)
            self._ack = self.client.register_script(_LUA_ACK)
        except Exception as e:  # noqa: BLE001
            raise QueueUnavailable(f"script registration failed: {e}") from e

    # ── keys (tenant- AND batch-scoped: no shared namespace, ever) ────────────────
    def _k(self, tenant: str, batch: str, suffix: str) -> str:
        return f"{self.namespace}:{tenant}:{batch}:{suffix}"

    def _ready(self, tenant: str, batch: str) -> str:
        return self._k(tenant, batch, "ready")

    def _inflight(self, tenant: str, batch: str) -> str:
        return self._k(tenant, batch, "inflight")

    def _dead(self, tenant: str, batch: str) -> str:
        return self._k(tenant, batch, "dead")

    def _batches_key(self, tenant: str) -> str:
        return f"{self.namespace}:{tenant}:batches"

    # ── producer ──────────────────────────────────────────────────────────────────
    def enqueue(self, items: Iterable[WorkItem]) -> int:
        """Push items onto their batch's ready list. Returns how many were pushed."""
        n = 0
        pipe = self.client.pipeline()
        batches: set[tuple[str, str]] = set()
        for it in items:
            pipe.rpush(self._ready(it.tenant, it.batch), it.to_json())
            batches.add((it.tenant, it.batch))
            n += 1
        # A registry of live batches, so a FUTURE standalone worker process can DISCOVER work
        # instead of being told about it. Not consumed by today's in-process pool.
        for tenant, batch in batches:
            pipe.sadd(self._batches_key(tenant), batch)
        pipe.execute()
        return n

    # ── consumer ──────────────────────────────────────────────────────────────────
    def claim(self, tenant: str, batch: str) -> Optional[WorkItem]:
        """Atomically take the next ready item and hold it under a lease. None when empty."""
        deadline = time.time() + self.lease_seconds
        raw = self._claim(keys=[self._ready(tenant, batch), self._inflight(tenant, batch)],
                          args=[deadline])
        if not raw:
            return None
        try:
            return WorkItem.from_json(raw)
        except Exception:  # noqa: BLE001 — an unparseable member is poison; drop the claim
            try:
                self._ack(keys=[self._inflight(tenant, batch)], args=[raw])
            except Exception:
                pass
            log.warning("chunk_queue.unparseable_item_dropped")
            return None

    def ack(self, item: WorkItem) -> bool:
        """The item completed. Remove it from in-flight so no lease can redeliver it."""
        try:
            return bool(self._ack(keys=[self._inflight(item.tenant, item.batch)],
                                  args=[item.to_json()]))
        except Exception as e:  # noqa: BLE001
            log.warning(f"chunk_queue.ack_failed key={item.key}: {e}")
            return False

    def nack(self, item: WorkItem, reason: str = "") -> str:
        """The item FAILED. Return it to ready with attempts+1, or dead-letter it.

        Returns 'requeued' | 'dead' | 'lost'. 'lost' means another party already reclaimed the
        lease — correct and harmless: the item is somebody else's now, exactly once."""
        nxt = WorkItem(kind=item.kind, tenant=item.tenant, batch=item.batch, key=item.key,
                       payload=item.payload, attempts=item.attempts + 1)
        if nxt.attempts >= self.max_attempts:
            target, outcome = self._dead(item.tenant, item.batch), "dead"
            nxt.payload = dict(item.payload)
            nxt.payload["_dead_reason"] = str(reason)[:300]
        else:
            target, outcome = self._ready(item.tenant, item.batch), "requeued"
        try:
            moved = self._move(keys=[self._inflight(item.tenant, item.batch), target],
                               args=[item.to_json(), nxt.to_json()])
        except Exception as e:  # noqa: BLE001
            log.warning(f"chunk_queue.nack_failed key={item.key}: {e}")
            return "lost"
        return outcome if moved else "lost"

    def reap(self, tenant: str, batch: str, now: Optional[float] = None) -> int:
        """Return every LEASE-EXPIRED in-flight item to ready (or dead-letter it).

        THIS is the answer to 'a crashed worker's chunk must not burn'. Any participant may
        call it; the ZREM guard inside ``_LUA_MOVE`` makes concurrent reapers safe."""
        t = time.time() if now is None else float(now)
        try:
            expired = self.client.zrangebyscore(self._inflight(tenant, batch), 0, t)
        except Exception as e:  # noqa: BLE001
            log.warning(f"chunk_queue.reap_failed tenant={tenant[:8]}: {e}")
            return 0
        n = 0
        for raw in expired or []:
            try:
                item = WorkItem.from_json(raw)
            except Exception:  # noqa: BLE001
                try:
                    self._ack(keys=[self._inflight(tenant, batch)], args=[raw])
                except Exception:
                    pass
                continue
            if self.nack(item, reason="lease_expired") in ("requeued", "dead"):
                n += 1
        if n:
            log.warning(f"chunk_queue.reaped tenant={tenant[:8]} batch={batch} items={n} "
                        f"(a worker died or overran its {self.lease_seconds}s lease — "
                        f"the work RETURNED to the queue, it was not burned)")
        return n

    # ── introspection / lifecycle ─────────────────────────────────────────────────
    def depth(self, tenant: str, batch: str) -> dict:
        try:
            pipe = self.client.pipeline()
            pipe.llen(self._ready(tenant, batch))
            pipe.zcard(self._inflight(tenant, batch))
            pipe.llen(self._dead(tenant, batch))
            ready, inflight, dead = pipe.execute()
            return {"ready": int(ready or 0), "inflight": int(inflight or 0),
                    "dead": int(dead or 0)}
        except Exception:  # noqa: BLE001
            return {"ready": 0, "inflight": 0, "dead": 0}

    def dead_letters(self, tenant: str, batch: str) -> list:
        try:
            return [WorkItem.from_json(r)
                    for r in (self.client.lrange(self._dead(tenant, batch), 0, -1) or [])]
        except Exception:  # noqa: BLE001
            return []

    def purge(self, tenant: str, batch: str) -> None:
        """Drop every key for ONE batch. Called at the start of a run so a previous crashed
        run's residue can never be double-processed alongside a fresh enqueue."""
        try:
            self.client.delete(self._ready(tenant, batch), self._inflight(tenant, batch),
                               self._dead(tenant, batch))
            self.client.srem(self._batches_key(tenant), batch)
        except Exception as e:  # noqa: BLE001
            log.warning(f"chunk_queue.purge_failed tenant={tenant[:8]} batch={batch}: {e}")


# ── running a batch ──────────────────────────────────────────────────────────────────


class FatalBatchError(Exception):
    """The handler says the WHOLE batch must stop (e.g. the tenant's brain is unavailable).

    Distinct from an item failure on purpose: an outage is not a property of the item, so
    charging it to the item destroys the batch one item at a time — the exact defect that
    burned 400 chunks in 1.5 seconds during a container restart. The item is returned to the
    queue WITHOUT consuming an attempt, and the caller takes its own deferral path."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message)
        self.cause = cause


def _run_one(queue: RedisWorkQueue, item: WorkItem) -> tuple[str, Any]:
    """Execute ONE claimed item in a worker thread. NEVER raises.

    Returns ``(outcome, value)`` where outcome ∈ {'ok', 'failed', 'dead', 'fatal', 'nohandler'}."""
    handler = get_handler(item.kind)
    if handler is None:
        queue.nack(item, reason=f"no handler for kind={item.kind}")
        return ("nohandler", None)
    try:
        value = handler(item)
    except FatalBatchError as fe:
        # Do NOT consume an attempt: put it straight back so the batch can resume intact once
        # the outage clears. Bounded by the CALLER's own document-level attempt counter.
        try:
            requeue = WorkItem(kind=item.kind, tenant=item.tenant, batch=item.batch,
                               key=item.key, payload=item.payload, attempts=item.attempts)
            queue._move(keys=[queue._inflight(item.tenant, item.batch),
                              queue._ready(item.tenant, item.batch)],
                        args=[item.to_json(), requeue.to_json()])
        except Exception:  # noqa: BLE001
            pass
        return ("fatal", fe)
    except Exception as e:  # noqa: BLE001 — item-scoped failure; bounded redelivery decides
        # the reason travels into the dead-letter payload (``_dead_reason``) — type + ref only
        outcome = queue.nack(item, reason=_errors.public_detail(e, where="chunk_queue.run_one", what=type(e).__name__))
        return ("dead" if outcome == "dead" else "failed", e)
    queue.ack(item)
    return ("ok", value)


def run_batch(queue: RedisWorkQueue, tenant: str, batch: str, concurrency: int,
              on_result: Optional[Callable[[WorkItem, str, Any], None]] = None,
              deadline_seconds: Optional[float] = None) -> dict:
    """Drain one batch with ``concurrency`` in-process workers. Returns a tally.

    ``on_result(item, outcome, value)`` is invoked on the CALLING thread only — so a caller may
    safely write its Postgres ledger from it (a psycopg2 connection is not thread-safe, and the
    document lane's own comment has said so since it shipped).

    A ``FatalBatchError`` from any item stops the drain, leaves the remaining items in the queue
    and reports ``fatal`` — the caller takes its existing deferral path."""
    conc = max(1, int(concurrency))
    started = time.monotonic()
    limit = float(deadline_seconds if deadline_seconds is not None
                  else queue.lease_seconds * _RUN_DEADLINE_LEASES)
    tally = {"ok": 0, "failed": 0, "dead": 0, "nohandler": 0, "fatal": 0,
             "reaped": 0, "concurrency": conc, "fatal_error": None}

    with ThreadPoolExecutor(max_workers=conc, thread_name_prefix="flcq") as ex:
        inflight: dict = {}
        stop = False
        while True:
            if time.monotonic() - started > limit:
                log.warning(f"chunk_queue.run_deadline tenant={tenant[:8]} batch={batch} "
                            f"after={limit}s (items left in the queue, not lost)")
                break
            # Any participant may reap; doing it here means a peer that died mid-run has its
            # work picked up by THIS run rather than waiting for the next cycle.
            if not stop:
                tally["reaped"] += queue.reap(tenant, batch)
                while len(inflight) < conc:
                    item = queue.claim(tenant, batch)
                    if item is None:
                        break
                    inflight[ex.submit(_run_one, queue, item)] = item
            if not inflight:
                if stop:
                    break
                # Nothing running and nothing claimable: a peer may still hold a lease, but
                # this run's work is done. (reap() above already returned anything expired.)
                if queue.depth(tenant, batch)["ready"] == 0:
                    break
                continue
            done, _pending = wait(list(inflight.keys()), return_when=FIRST_COMPLETED,
                                  timeout=min(5.0, limit))
            if not done:
                continue
            for fut in done:
                item = inflight.pop(fut)
                try:
                    outcome, value = fut.result()
                except Exception as e:  # noqa: BLE001 — _run_one is total; belt and braces
                    outcome, value = "failed", e
                tally[outcome] = tally.get(outcome, 0) + 1
                if outcome == "fatal":
                    stop = True
                    tally["fatal_error"] = value
                if on_result is not None:
                    try:
                        on_result(item, outcome, value)
                    except Exception as cb_err:  # noqa: BLE001 — a ledger write must not kill the drain
                        log.warning(f"chunk_queue.on_result_failed key={item.key}: {cb_err}")
    return tally


# ── per-tenant concurrency, from MEASURED capacity ───────────────────────────────────
#
# ONE GLOBAL NUMBER IS THE BUG. `DOC_CHUNK_CONCURRENCY` applies the same fan-out to a hosted
# API with hundreds of concurrent slots and to a self-hosted 4-slot box, and on 2026-07-31 the
# second case is exactly what happened: 24 concurrent requests at a 4-slot server produced a
# 6-deep queue and then timeouts that were charged to the CHUNKS.
#
# A deployment that has MEASURED its own endpoint (p50/p95) can derive the concurrency
# from that measurement — it is NOT re-implemented — and combined with the effective outbound
# rate limit (LLM_MAX_RPM, clamped by src/api/llm_rate).
#
# THE RULE — Little's Law, not a guess. For a stable system, the number of requests in flight
# equals arrival rate × service time:
#
#     L = λ · W          (J. D. C. Little, "A Proof for the Queuing Formula L = λW",
#                         Operations Research 9(3), 1961)
#
# with λ = effective_rpm / 60 (what we are ALLOWED to send) and W = p95 seconds (what one call
# actually costs on THEIR endpoint). Sending more than L concurrent calls cannot make the
# endpoint faster; it only builds the queue that produces the timeouts.
#
# The profile then supplies a hard CEILING, because a self-hosted endpoint's slot count is not
# something we can infer from latency alone and getting it wrong is expensive in exactly one
# direction.

_PROFILE_CEILING: dict[str, int] = {
    # A hosted API absorbs wide fan-out; this is the case the ~11s/chunk number was measured on.
    "hosted-fast": max(1, int(os.getenv("CHUNK_QUEUE_MAX_HOSTED", "12"))),
    # A self-hosted box typically serves a handful of slots. Deliberately conservative.
    "local": max(1, int(os.getenv("CHUNK_QUEUE_MAX_LOCAL", "4"))),
    # An endpoint whose p95 is already >20s is saturated; widening makes it worse.
    "slow": max(1, int(os.getenv("CHUNK_QUEUE_MAX_SLOW", "2"))),
}


def concurrency_from_measurement(p95_ms: Optional[float], effective_rpm: Optional[float],
                                 profile: Optional[str], default: int) -> int:
    """PURE: resolve a per-tenant concurrency from a measurement. Never raises.

    Fail-safe direction is deliberate and asymmetric: an ABSENT or unusable measurement returns
    ``default`` (today's global), never a wider value — an un-calibrated tenant must not be
    silently hammered. A PRESENT measurement may narrow below the default (that is the whole
    point) and may widen only up to its profile's ceiling."""
    try:
        ceiling = _PROFILE_CEILING.get(str(profile or "").strip().lower())
    except Exception:  # noqa: BLE001
        ceiling = None
    try:
        w = float(p95_ms) / 1000.0
        lam = float(effective_rpm) / 60.0
    except (TypeError, ValueError):
        return max(1, int(default))
    if w <= 0 or lam <= 0:
        return max(1, int(default))
    little = int(lam * w)                      # L = λ·W, floored
    if ceiling is None:
        # Measured, but no profile to bound it: never exceed today's global.
        return max(1, min(little, int(default)))
    return max(1, min(little, ceiling))


def resolve_tenant_concurrency(user_id: str, default: int) -> tuple[int, str]:
    """Per-tenant chunk concurrency + a one-line REASON (for the log).

    Returns ``(concurrency, reason)``. Open core resolves the ceiling from the
    ``DOC_CHUNK_CONCURRENCY`` env when set; otherwise ``(default, 'default:...')`` —
    byte-identical to today's behaviour."""
    d = max(1, int(default))
    raw = os.environ.get("DOC_CHUNK_CONCURRENCY", "").strip()
    if raw:
        try:
            conc = max(1, int(raw))
            return (conc, f"env:DOC_CHUNK_CONCURRENCY={raw}")
        except Exception as e:  # noqa: BLE001
            return (d, f"default:{type(e).__name__}")
    return (d, "default:not_configured")
