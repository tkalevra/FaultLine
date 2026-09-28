"""CPU lane — partition CPU-bound inference between interactive requests and deferrable
background extraction, so a churning document ingest can never starve the platform.

WHY THIS EXISTS
───────────────
MEASURED on production 2026-08-21: while a 32-chunk document drained, GET /health timed
out at 15–60s (13.15s best case) with CPU at 0.00% and Postgres idle. The API process
itself froze per chunk-batch and every tenant felt it for the document's full duration.

The mechanism (verified in source, matching the bursty freeze pattern): the extraction
endpoints are ``async def`` and ran the deterministic spaCy/GLiNER2 inference INLINE on
the event loop — ``_harvest_spans_impl`` alone fires ~7 synchronous detectors plus the
spine deriver per request, with zero ``await`` points between them. The document worker
fans chunks in concurrently (``DOC_CHUNK_CONCURRENCY``), each harvest blocks the loop for
the full duration of its CPU pass, and /health — a ``def`` endpoint that still needs the
loop just to be dispatched into the threadpool — starves behind them. During the LLM-wait
phases of /extract/rewrite the loop yields and /health answers in ~0.08s; that asymmetry
is the diagnosis.

THE RULE THIS ENCODES
────────────────────
INTERACTIVE work keeps today's latency; BACKGROUND work may only consume the CPU a
foreground request left behind. Three mechanisms, one seam:

  * OFF-LOOP — ``run_cpu`` moves CPU-bound sync calls off the event loop for BOTH lanes
    (the loop must never run a multi-hundred-ms parse inline, whoever asked for it);
  * PARTITION — background-lane CPU runs on a DEDICATED bounded executor
    (``DOC_LANE_CPU_WORKERS``, default 2) with an ADMISSION cap
    (``DOC_LANE_ADMISSION``, default = workers) so concurrent document chunks can never
    occupy the box's whole CPU budget — the effective document-chunk CPU concurrency is
    the admission count, below interactive capacity BY CONSTRUCTION;
  * CONTROL — an operator pause/throttle knob (``/internal/doc-lane/control``): pause
    rejects new background CPU work immediately with a ``DocLanePaused`` the endpoints
    translate into a deferral envelope (the document worker re-pends, it does not burn
    chunks); throttle adjusts the admission count at runtime without a restart.

The lane is the SAME lane ``src/api/llm_lane.py`` already binds from the
``x-fl-llm-lane`` header per request — the document worker has self-identified as
background since that middleware shipped, so this module adds NO new caller contract.

OUT-OF-PROCESS (2026-08-22, round-6 of the interactive-latency gauntlet)
─────────────────────────
Threads move CPU off the EVENT LOOP but not out of the INTERPRETER: the spaCy/GLiNER2
C-extension passes still hold the one GIL, and measured on pre-prod (rounds 4-5) a
32-chunk drain froze the loop in 1.8-2.9s synchronized waves while a churn+query
overlap carried a ~100-250ms tail — every tenant's /health paid for it. So the
BACKGROUND lane now executes its CPU passes in WORKER PROCESSES
(``DOC_LANE_CPU_PROCESSES``, default 2, ``fork`` context) instead of threads:

  * FORK INHERITANCE is the design, not a convenience: workers fork at the FIRST
    background submit — after the parent has loaded spaCy/GLiNER2 — and inherit the
    loaded models copy-on-write. No re-import, no per-worker model load. Forking is
    never paid on the interactive path (interactive work never touches this branch).
  * CONTEXT: ContextVars cannot cross a process boundary, so the tenant schema and the
    lane are re-bound INSIDE the worker per call (``_proc_entry``); the worker resets
    the binding afterwards so a pooled worker never leaks one tenant into the next call.
  * UNPICKLABLE ⇒ THREADS, LOUDLY: whatever cannot cross the boundary (closure
    predicates; unpicklable results) is bridged by reference where fork makes that
    exact (``_ForkRef``), and otherwise STAYS IN-PROCESS on the legacy bounded
    executor with a logged justification — a broken isolation must degrade to today's
    behaviour, never to "background extraction stops". A pickle/ref failure, a dead
    worker (``BrokenProcessPool``) or an unpicklable result all take that fallback;
    after 5 consecutive process-lane failures the lane hard-disables for this process
    lifetime (fail-safe direction, same rule as everywhere here).

TX6 — THE WEDGE BOUND + THE FORK-HAZARD CURE (2026-09-04)
──────────────────────────────────────────────────────────
The fork inheritance this design rides has a poisoned edge: libgomp caches per-thread
team state in the forking thread's TLS, so a worker forked AFTER the API main thread
ran any torch OpenMP region inherits a barrier whose member threads do not exist in
the child; the worker's first OpenMP op waits on that futex FOREVER, alive and silent
(no BrokenProcessPool), and an unbounded dispatch await then pinned both admission
permits at cap for hours (live on pre-prod 2026-09-04; pinned in
the internal design record). Two independent layers, both
shipped:

  * LAYER 1 — STRUCTURAL BOUND: _dispatch_process bounds the submit+await with
    asyncio.wait_for (DOC_PROC_TIMEOUT, default 120s — comfortably above honest chunk
    work, far below the caller's 180s HTTP budget). A timeout is treated exactly like
    a broken executor, with one honesty refinement: a done-race takes the honest
    result, and a cheap noop liveness probe (DOC_PROC_PROBE_S) spares a slow-but-
    draining pool from being torn down — only a probe that ALSO stalls (every worker
    wedged) triggers the refork. Wedged workers are SIGKILLed on the teardown:
    shutdown(wait=False) alone terminates nothing and a wedged child would orphan a
    COW copy of every model.
  * LAYER 2 — HAZARD REMOVED AT SOURCE: a pool initializer pins torch intra/inter-op
    threads to 1 in each worker BEFORE its first op, so a worker never enters an OpenMP
    parallel region at all (repro-verified). Chosen over mp_context "forkserver",
    which would sever the fork inheritance the _ForkRef/_MethodCall bridge IS — every
    model-handle payload would miss the registry and the entire background CPU load
    would fall back onto the GIL-bound threads, defeating round-6's isolation.
    GOVERNANCE (critic round-1): the critic's OpenMP census found torch's libgomp is
    the ONLY OpenMP runtime linked in the image, so the torch-only pin covers
    everything TODAY — but a FUTURE dependency linking its own libgomp/OpenMP would
    NOT be covered by this pin. Any new C-extension dependency that can enter a
    parallel region must either join this initializer's pin or get its own hazard
    review before riding the background process lane.

No retry loop is added anywhere: a timed-out call takes the thread fallback ONCE (the
existing degrade path), lane-down abort semantics downstream are untouched.

FAIL-SAFE, BOTH DIRECTIONS
──────────────────────────
``DOC_LANE_CPU_OFF=true`` ⇒ ``run_cpu`` executes the call inline on the caller (the
byte-for-byte legacy behaviour — the rollback lever). Any internal error in the admission
path admits the work rather than dropping it: a broken fairness guard must degrade to
today's behaviour, never to "background extraction silently stops".

CONTEXT PROPAGATION IS LOAD-BEARING
────────────────────────────────────
``run_cpu`` copies the caller's contextvars into the worker thread. The per-tenant
overlays (``rel_type_overlay.set_current_schema`` etc.) and the lane itself ride
ContextVars — without this copy, extraction running in a thread would resolve metadata
against the WRONG tenant's schema. Pinned by test.

SUBJECT-AGNOSTIC: no operation names, no endpoint lists — the CALLER's lane decides,
exactly as llm_lane does.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import itertools
import logging
import multiprocessing
import os
import pickle
import sys
import threading
import time
import weakref
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, Callable, Optional

from .llm_lane import LANE_BACKGROUND, current_lane, use_lane

try:  # the tenant-schema binding re-established inside each worker process
    from . import rel_type_overlay as _rel_overlay
except Exception:  # pragma: no cover — an overlay import failure must disable, not corrupt
    _rel_overlay = None

log = logging.getLogger(__name__)

# ── pickle usage policy (semgrep gate) ───────────────────────────────────
# This module uses pickle ONLY as a capability probe (pickle.dumps) to test
# whether objects can cross a multiprocessing fork() boundary. There is NO
# pickle.loads anywhere in this file — no deserialization of any data.
#
# The objects being pickled are INTERNAL only:
#   * func/args/kwargs: bound methods of internal model handles (GLiNER2/spaCy)
#                     + their arguments (spaCy Doc, typed Doc, type dicts)
#   * results/envelope: return values from internal CPU inference functions
#   * exc: exceptions raised by internal CPU inference functions
#
# No external/user-controlled data is ever passed to pickle in this module.
# The picklability probe is required because the process lane sends payloads
# across fork() boundaries and must detect unpicklable objects BEFORE submit.
# ───────────────────────────────────────────────────────────────────────────

# ── configuration (env-tunable, never a bare literal at a call site) ──────────

# Dedicated executor size for BACKGROUND-lane CPU work. 2 keeps a multi-tenant box
# responsive while still extracting 2 chunks' inference in parallel; the interactive
# lane is NOT capped by this (it rides the loop's default executor).
CPU_WORKERS = max(1, int(os.getenv("DOC_LANE_CPU_WORKERS", "2")))

# How many background CPU passes may run at once. Default = workers (a queue of waiting
# chunks forms past this — backpressure, which the worker absorbs by simply holding its
# in-flight HTTP requests; the platform stays responsive).
_ADMISSION_DEFAULT = max(1, int(os.getenv("DOC_LANE_ADMISSION", str(CPU_WORKERS))))

# Operator pause at boot (normally driven at runtime via the control endpoint).
_PAUSED_DEFAULT = os.getenv(
    "DOC_LANE_PAUSED", "false").strip().lower() in ("1", "true", "yes", "on")

# Rollback lever: OFF ⇒ run_cpu is inline (legacy behaviour, byte-for-byte).
DOC_LANE_CPU_OFF = os.getenv(
    "DOC_LANE_CPU_OFF", "false").strip().lower() in ("1", "true", "yes", "on")

# ── process isolation for the background lane (round-6) ───────────────────────
# Worker PROCESSES for background CPU (the GIL cure — see module docstring).
# 0 / false / off ⇒ disabled: the background lane keeps the thread executor
# (byte-for-byte the round-5 behaviour — the rollback lever for THIS change).
_raw_procs = os.getenv("DOC_LANE_CPU_PROCESSES", "2").strip().lower()
_PROC_N = 0 if _raw_procs in ("0", "false", "no", "off") else max(1, int(_raw_procs or "2"))

# The overlay binding is load-bearing for tenant correctness in the worker; without
# it the process lane must not run (threads keep the contextvars copy instead).
if _rel_overlay is None:
    _PROC_N = 0

# Consecutive process-lane failures before the lane hard-disables for this lifetime.
_PROC_FAIL_BUDGET = 5

# ── tx6: the wedge bound + the fork hazard cure (2026-09-04) ────────────────────
# MECHANISM (the internal design record): a worker forked from
# the torch/OpenMP-warmed API main thread inherits poisoned libgomp team state; its
# FIRST OpenMP-parallel op waits forever on a barrier futex. The worker stays ALIVE,
# so ProcessPoolExecutor never raises BrokenProcessPool and the dispatch future never
# resolves — an unbounded await there pins the admission permit at cap forever.
_PROC_TIMEOUT_S = float(os.getenv("DOC_PROC_TIMEOUT", "120"))
# Liveness-probe budget: after a dispatch timeout, a trivial noop submit distinguishes
# "pool wedged" (probe also stalls ⇒ tear down + refork) from "this one call is slow
# but the pool drains" (probe answers ⇒ threads for THIS call, pool stays up).
_PROC_PROBE_S = float(os.getenv("DOC_PROC_PROBE_S", "5"))

# Poll cadence for the admission wait and the pause state. Cross-loop-safe by design:
# an asyncio.Lock/Semaphore/Event binds to the running loop at creation, which breaks
# under the multi-loop discipline of the test suite (a fresh loop per test) and would
# make the operator knob unusable from another loop. A 50ms poll with a plain
# threading.Lock costs nothing measurable and never wedges.
_POLL_S = float(os.getenv("DOC_LANE_POLL_S", "0.05"))


class _ForkRefMiss(KeyError):
    # A bridged reference the worker could not resolve (the entry was registered
    # AFTER that worker forked — fork inheritance is one-shot). The parent converts
    # this into the thread fallback; it is NEVER a failure of the work itself.
    pass


class DocLanePaused(RuntimeError):
    """New background CPU work was REFUSED because the operator paused the lane.

    NOT a failure of the work: the endpoints translate this into a 200-shaped
    ``doc_lane_paused`` envelope, and the document worker treats that exactly like a
    brain-unavailable deferral (the document re-pends, bounded by the existing
    at-least-once machinery — no chunk is burned, no attempt is charged to the pause).
    """


# ── the dedicated background executor (process-wide, started lazily is NOT needed:
#    constructing a ThreadPoolExecutor spawns threads on first submit, not at import) ──
_bg_executor = ThreadPoolExecutor(max_workers=CPU_WORKERS, thread_name_prefix="fl-doc-cpu")

# ── the process lane (background only) ──────────────────────────────────────────
# FORK-REF BRIDGE: a worker forked from THIS process inherits the parent's memory,
# so an object that cannot be PICKLED (the GLiNER2/spaCy model handles passed as
# call arguments) can still be passed BY REFERENCE — register it here, send a
# _ForkRef, and the child resolves it from the registry it inherited at fork.
# Weak values + a monotonic counter (never id()-keys — an id can be REUSED after
# GC, which would hand the child the WRONG object): entries die with their object,
# and a dead entry surfaces as a lookup error the parent converts to the thread
# fallback.
_fork_refs = weakref.WeakValueDictionary()
_fork_ref_ctr = itertools.count()


def _fork_ref_load(key):
    # Resolve a _ForkRef inside the worker (registry inherited at fork).
    try:
        return _fork_refs[key]
    except KeyError as miss:
        raise _ForkRefMiss(key) from miss


class _ForkRef:
    # Picklable stand-in for an unpicklable argument, resolvable only in a
    # forked child of this process.
    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key

    def __reduce__(self):  # crosses the pipe; child reconstitutes via the registry
        return (_fork_ref_load, (self.key,))


class _MethodCall:
    # Picklable stand-in for an UNPICKLABLE BOUND METHOD (the model singletons'
    # methods — e.g. the GLiNER2 relation extractor): the instance crosses by
    # fork-ref, the method by NAME. Closures have no __self__ and never bridge.
    __slots__ = ("self_ref", "name")

    def __init__(self, self_ref, name):
        self.self_ref = self_ref
        self.name = name

    def __reduce__(self):
        return (_method_call_load, (self.self_ref, self.name))


def _method_call_load(self_obj, name):
    # Reconstitute the bound method inside the worker. NOTE: the _ForkRef carried in
    # __reduce__'s args does NOT survive as an _ForkRef — its own __reduce__ resolves
    # it to the OBJECT at unpickle time, so self_obj arrives ALREADY resolved. Do not
    # re-resolve (looking the object up as a key was the double-resolution bug).
    return getattr(self_obj, name)


_known_ref_ids = {}  # id(obj) -> key (identity dedupe; validity re-checked vs the weak dict)


def _bridge_payload(func, args, kwargs, schema):
    # Build the process-boundary payload, bridging unpicklable leaves by fork-ref.
    # Returns (func, args, kwargs, schema) picklable as a whole, or None when the
    # call cannot cross (caller stays on threads).
    #
    # WHAT BRIDGES: unpicklable ARGUMENTS (the model handles) and BOUND METHODS of
    # process-lifetime singletons (as _MethodCall: __self__ by fork-ref + method
    # name) — measured live, the heaviest background call is exactly that shape
    # (GLiNER2.extract_relations). Both ride objects that live for the process's
    # lifetime, which is what makes fork-ref exact.
    #
    # WHAT NEVER BRIDGES: plain closures (no __self__) — bridging one would register
    # an entry after the workers forked, and fork inheritance is one-shot. They stay
    # on threads with one logged line.
    #
    # A registry MISS in an already-forked worker (entry registered after ITS fork)
    # is not an error and not a work failure: the parent reforks the pool so fresh
    # workers inherit the enlarged registry (self-healing, once per late entry) and
    # this one call takes the thread fallback.

    grew = False

    def _one(v):
        # Returns (picklable_or_ref, unbridgeable, newly_registered).
        nonlocal grew
        try:
            pickle.dumps(v)  # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
            return v, False, False
        except Exception:
            # Identity dedupe: re-registering the SAME singleton on every call would
            # both leak the registry and refork the pool per call. The stale-id case
            # (object died, a new one reuses its id) is caught by the identity check
            # against the weak dict — a dead entry reads back None, never a wrong
            # object.
            _k = _known_ref_ids.get(id(v))
            if _k is not None and _fork_refs.get(_k) is v:
                return _ForkRef(_k), False, False
            key = "fr%d" % next(_fork_ref_ctr)
            try:
                _fork_refs[key] = v
            except TypeError:  # not weakref-able either: unbridgeable
                return None, True, False
            _known_ref_ids[id(v)] = key
            grew = True
            return _ForkRef(key), False, True

    try:
        pickle.dumps(func)  # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
    except Exception:
        _slf = getattr(func, "__self__", None)
        _nm = getattr(func, "__name__", None)
        if _slf is None or _nm is None:
            return None, grew
        _bridged_self, _bad, _ = _one(_slf)
        if _bad or not isinstance(_bridged_self, _ForkRef):
            return None, grew
        func = _MethodCall(_bridged_self, _nm)

    try:
        pickle.dumps((func, args, kwargs))  # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
        return (func, args, kwargs, schema), grew
    except Exception:
        pass
    parts = [_one(a) for a in args]
    parts.extend(_one(v) for v in kwargs.values())
    if any(bad for _, bad, _ in parts):
        return None, grew
    bridged = [v for v, _, _ in parts]
    payload = (func, tuple(bridged[:len(args)]),
               dict(zip(kwargs.keys(), bridged[len(args):])), schema)
    try:
        pickle.dumps(payload)  # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
        return payload, grew
    except Exception:
        return None, grew


def _proc_init():
    # tx6 layer 2 — REMOVE THE HAZARD, before the worker's first op: the child
    # inherits libgomp team state from the torch-warmed FORKING thread; pinning
    # intra-op (and inter-op) threads to 1 means the worker NEVER enters an OpenMP
    # parallel region, so the poisoned barrier can never be waited on. Differential
    # repro on the faultline-wgm image: warm-main+fork WITHOUT this = wedged 6/6;
    # WITH torch.set_num_threads(1) in the initializer = ok (see MECHANISM.md).
    # Chosen over mp_context "forkserver": forkserver severs fork inheritance, which
    # the _ForkRef/_MethodCall bridge IS — every model-handle payload would miss the
    # registry and the whole background CPU load would land on the GIL-bound threads
    # (the round-6 process isolation defeated). torch-absent deployments: no-op.
    # GUARD: only touch a torch the PARENT already imported (fork inheritance hands
    # the child the loaded module). A fresh torch IMPORT inside a forked child of a
    # multithreaded parent is itself fork-unsafe and can deadlock the worker at
    # initialization — and if the parent never loaded torch, there is no warmed
    # OpenMP state to defuse anyway, so the pin has nothing to do.
    torch = sys.modules.get("torch")
    if torch is None:
        return
    try:
        torch.set_num_threads(1)
        try:
            torch.set_num_interop_threads(1)
        except Exception:  # settable only before the first parallel work; a refused
            pass           # late set is harmless — intra-op is the load-bearing pin
    except Exception:
        pass  # never let the initializer itself take a worker down


def _proc_noop(*_a, **_k):
    # The liveness probe's payload: picklable, instant, no OpenMP anywhere near it.
    return None


def _proc_entry(func, args, kwargs, schema):
    # Worker-process entry: rebind tenant + lane, run the pass, harden the sendback.
    tok = None
    if schema and _rel_overlay is not None:
        tok = _rel_overlay.set_current_schema(schema)
    try:
        with use_lane(LANE_BACKGROUND):
            try:
                out = func(*args, **kwargs)
                envelope = ("ok", out)
                pickle.dumps(envelope)  # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
                return envelope
            except BaseException as exc:  # noqa: BLE001 — re-raised in the parent
                try:
                    pickle.dumps(exc)  # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
                except Exception:
                    exc = RuntimeError("unpicklable exception %s: %s"
                                       % (type(exc).__name__, exc))
                return ("err", exc)
    finally:
        if tok is not None and _rel_overlay is not None:
            _rel_overlay.reset_current_schema(tok)


_proc_executor = None
_proc_lock = threading.Lock()
_proc_failures = 0


def _proc_pool():
    # The lazily-created fork-context pool. Construction forks NOTHING — workers
    # appear on first submit, i.e. at the first background pass, which is AFTER the
    # parent has loaded spaCy/GLiNER2 (the copy-on-write inheritance the design rides).
    global _proc_executor, _proc_failures
    if _PROC_N <= 0:
        return None
    with _proc_lock:
        if _proc_executor is None:
            _proc_executor = ProcessPoolExecutor(
                max_workers=_PROC_N, mp_context=multiprocessing.get_context("fork"),
                initializer=_proc_init)
            _proc_failures = 0
        return _proc_executor


def _proc_refork():
    # Rebuild the pool WITHOUT charging the failure budget (a fork-ref miss is a
    # timing artifact, not a broken lane): fresh workers inherit the registry as it
    # stands NOW, including entries registered after the previous fork.
    global _proc_executor
    with _proc_lock:
        try:
            if _proc_executor is not None:
                _proc_executor.shutdown(wait=False)
        except Exception:
            pass
        _proc_executor = None


def _kill_pool_workers(pool):
    # Best-effort SIGKILL of the pool's worker processes. shutdown(wait=False) alone
    # TERMINATES nothing: a wedged worker (futex-parked, alive — the tx6 shape) would
    # survive as an orphan holding a COW copy of every model forever. _processes is a
    # private attribute; on any AttributeError we degrade to shutdown-only.
    try:
        procs = list(getattr(pool, "_processes", {}).values())
    except Exception:
        procs = []
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass


def _proc_broken():
    # Drop a dead pool so the next call builds a fresh one (bounded by the budget).
    global _proc_executor, _proc_failures
    with _proc_lock:
        try:
            if _proc_executor is not None:
                _kill_pool_workers(_proc_executor)
                _proc_executor.shutdown(wait=False)
        except Exception:
            pass
        _proc_executor = None
        _proc_failures += 1


_FALLBACK = object()  # sentinel: crossing refused ⇒ threads


async def _pool_alive(pool) -> bool:
    # Cheap liveness re-check: one trivial submit with a short bound. A wedged pool
    # (every worker futex-parked) cannot answer it; a busy-but-honest pool can, even
    # while the ORIGINAL call is still running — that is the discriminator.
    try:
        probe = pool.submit(_proc_noop)
        await asyncio.wait_for(asyncio.wrap_future(probe), timeout=_PROC_PROBE_S)
        return True
    except Exception:
        return False


async def _dispatch_process(func, args, kwargs):
    # Run one background CPU pass in a worker process; _FALLBACK ⇒ stay on threads.
    global _proc_failures
    if _PROC_N <= 0 or _proc_failures >= _PROC_FAIL_BUDGET:
        return _FALLBACK
    pool = _proc_pool()
    if pool is None:
        return _FALLBACK
    schema = None
    try:
        if _rel_overlay is not None:
            schema = _rel_overlay.get_current_schema()
    except Exception:
        schema = None
    loop = asyncio.get_running_loop()
    # Bridge off-loop: pickling (or failing to) a payload must not hold the loop.
    payload, grew = await loop.run_in_executor(None, _bridge_payload, func, args, kwargs, schema)
    if payload is None:
        log.info("cpu_lane.proc_unpicklable_staying_on_threads func=%s",
                 getattr(func, "__qualname__", repr(func)))
        return _FALLBACK
    if grew:
        # A NEW registry entry exists: any already-forked worker would MISS it at
        # queue-unpickle time (before _proc_entry runs, where nothing can catch it —
        # the worker would die). Refork NOW so the workers that serve this very
        # submit inherit the enlarged registry. Misses are thereby made structurally
        # impossible, not merely handled.
        _proc_refork()
        pool = _proc_pool()
        if pool is None:
            return _FALLBACK
    try:
        fut = pool.submit(_proc_entry, *payload)
        wrapped = asyncio.wrap_future(fut)
        try:
            kind, value = await asyncio.wait_for(wrapped, timeout=_PROC_TIMEOUT_S)
        except asyncio.TimeoutError:
            # tx6 layer 1 — STRUCTURAL BOUND: a worker that is alive-but-wedged never
            # resolves this future and never trips BrokenExecutor; the unbounded await
            # this replaces pinned the admission permit at cap forever. Treat a timeout
            # EXACTLY like a broken executor (refork + thread fallback + the caller's
            # finally releases the permit) — with one honesty refinement first: if the
            # future raced us to done, take the honest result; if a trivial probe
            # still drains, this call was merely SLOW — threads for this call, and the
            # pool (and the other in-flight work in it) is left standing.
            #
            # SCOPE OF THE PROBE SPARE (critic-adjudicated, round-1 tidy): the probe
            # spares the pool ONLY when at least one worker is FREE. If every worker
            # is busy with honest work past the budget, the probe QUEUES behind it,
            # stalls at _PROC_PROBE_S, and _proc_broken SIGKILLs honest siblings —
            # callers re-run on threads (correct, but duplicated work; budget
            # charged; 5 cycles hard-disable the process lane to GIL threads).
            # Unreachable at HEAD with ~60x margin: measured honest CPU passes are
            # <=2s against the 120s budget. REVISIT TRIGGER: any future background
            # payload whose honest CPU approaches DOC_PROC_TIMEOUT (raise the budget
            # or move to per-call liveness, not pool-wide teardown).
            #
            # VANISHING RACE (accepted): after wait_for times out it CANCELS the
            # wrapped asyncio future; in the narrow window where the underlying
            # work completes between wait_for's cancellation and the fut.done()
            # check, awaiting the (already cancelled) wrapped future raises
            # CancelledError — a BaseException that deliberately escapes the
            # except-Exception degrade below. Self-healing: run_cpu's finally
            # releases the permit and the caller sees a cancellation, never a silent
            # hang; the pool is untouched.
            if fut.done():
                kind, value = await wrapped
            elif await _pool_alive(pool):
                log.warning(
                    "cpu_lane.proc_slow_fell_to_threads budget_s=%s func=%s",
                    _PROC_TIMEOUT_S, getattr(func, "__qualname__", repr(func)))
                return _FALLBACK
            else:
                _proc_broken()
                log.warning(
                    "cpu_lane.proc_wedge_detected_pool_reforked budget_s=%s func=%s",
                    _PROC_TIMEOUT_S, getattr(func, "__qualname__", repr(func)))
                return _FALLBACK
    except _ForkRefMiss as miss:
        # Self-heal: fresh workers will inherit the registry entry this worker
        # missed. NOT charged to the failure budget — a miss is a timing artifact
        # of lazy registration, not a broken lane. This call takes the threads.
        _proc_refork()
        log.info("cpu_lane.proc_forkref_miss_reforked_staying_on_threads key=%s", miss)
        return _FALLBACK
    except BrokenExecutor:
        _proc_broken()
        log.warning("cpu_lane.proc_pool_broken_fell_to_threads")
        return _FALLBACK
    except Exception as exc:  # pickle/pipe/worker failures — degrade, never drop work
        _proc_broken()
        log.warning("cpu_lane.proc_submit_failed_fell_to_threads err=%s", exc)
        return _FALLBACK
    if kind == "ok":
        with _proc_lock:
            _proc_failures = 0
        return value
    raise value

# ── runtime control state (the operator knob's backing store) ─────────────────
_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "paused": _PAUSED_DEFAULT,
    "admission": _ADMISSION_DEFAULT,
    "inflight": 0,
    "waiting": 0,
    "paused_count": 0,   # diagnostics: refusals since boot
    "rejected": 0,       # diagnostics: DocLanePaused raises since boot
}


def snapshot() -> dict[str, Any]:
    """The lane's control + load state (the control endpoint GET and log lines)."""
    with _state_lock:
        return {
            "paused": bool(_state["paused"]),
            "admission": int(_state["admission"]),
            "cpu_workers": CPU_WORKERS,
            "cpu_processes": _PROC_N,
            "inflight": int(_state["inflight"]),
            "waiting": int(_state["waiting"]),
            "paused_count": int(_state["paused_count"]),
            "rejected": int(_state["rejected"]),
            "cpu_off": bool(DOC_LANE_CPU_OFF),
        }


def set_control(paused: Optional[bool] = None, admission: Optional[int] = None) -> dict[str, Any]:
    """Operator knob: pause/resume the background CPU lane and/or resize its admission.

    Returns the post-change snapshot. Never raises on None inputs; an invalid admission
    (< 1) is refused loudly (returns the snapshot unchanged with ``error`` set) rather
    than silently clamping an operator's intent to something they did not write.
    """
    err = None
    with _state_lock:
        if paused is not None:
            _state["paused"] = bool(paused)
            if paused:
                _state["paused_count"] += 1
        if admission is not None:
            try:
                _adm = int(admission)
            except (TypeError, ValueError):
                _adm = -1
            if _adm < 1 or _adm > 64:
                err = f"admission must be an int in [1, 64], got {admission!r}"
            else:
                _state["admission"] = _adm
    out = snapshot()
    if err is not None:
        out["error"] = err
    log.info("cpu_lane.control_set paused=%s admission=%s err=%s",
             out["paused"], out["admission"], err)
    return out


def _admit_sync() -> bool:
    """One non-blocking admission attempt. True = admitted (caller MUST release)."""
    with _state_lock:
        if _state["paused"]:
            _state["rejected"] += 1
            raise DocLanePaused("document CPU lane paused by operator")
        if _state["inflight"] < _state["admission"]:
            _state["inflight"] += 1
            return True
        return False


async def _admit() -> None:
    """Acquire one background-CPU admission permit (polling wait, cross-loop-safe)."""
    try:
        if _admit_sync():
            return
    except DocLanePaused:
        raise
    with _state_lock:
        _state["waiting"] += 1
    try:
        deadline = time.monotonic() + 3600.0  # absolute safety bound; the caller's own
        # HTTP timeout governs the real ceiling — this only stops an orphaned waiter.
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_S)
            try:
                if _admit_sync():
                    return
            except DocLanePaused:
                raise
    finally:
        with _state_lock:
            _state["waiting"] -= 1


def _release() -> None:
    with _state_lock:
        _state["inflight"] = max(0, _state["inflight"] - 1)


async def _dispatch(executor: Optional[ThreadPoolExecutor],
                    func: Callable[..., Any],
                    args: tuple, kwargs: dict) -> Any:
    """Run ``func(*args, **kwargs)`` on ``executor`` (None = the loop's default pool),
    with the CALLER'S contextvars copied into the worker thread.

    The context copy is load-bearing, not hygiene: the per-tenant metadata overlays and
    the lane itself ride ContextVars, and a thread that loses them resolves extraction
    metadata against the wrong tenant. Mutations made inside the thread do NOT flow back
    (a copy, exactly like ``asyncio.to_thread``) — callers must not rely on ContextVar
    writes from inside ``run_cpu`` work.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(executor, lambda: ctx.run(call))


def paused_for_background() -> bool:
    """True when the operator paused the lane AND the current request rides the BACKGROUND lane.

    The PAUSE DOOR: endpoints check this at entry (before any extraction spend) and return a
    200-shaped ``doc_lane_paused`` envelope, which the document worker treats as a
    brain-unavailable-style DEFERRAL (re-pend, bounded, no chunk burned). Pause is enforced at
    the door rather than at every CPU site because the CPU sites sit inside broad
    ``except Exception`` fail-safes that would swallow a mid-flight ``DocLanePaused``; a pause
    flipped mid-request therefore lets that request finish degraded-but-honest, and the NEXT
    request is refused at the door. Interactive traffic never sees this — pause only ever
    gates deferrable work.
    """
    if DOC_LANE_CPU_OFF:
        return False
    try:
        if current_lane() != LANE_BACKGROUND:
            return False
        with _state_lock:
            return bool(_state["paused"])
    except Exception:  # noqa: BLE001 — a broken pause probe must never block a request
        return False


async def run_cpu(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Await ``func(*args, **kwargs)`` with lane-aware CPU fairness.

    BACKGROUND lane → dedicated bounded executor, gated by the admission cap and the
    operator pause (``DocLanePaused`` refuses new work while paused).
    Any other lane (INTERACTIVE / unset) → the loop's DEFAULT executor: off the event
    loop (the loop must never run inference inline) but uncapped — a user waiting on a
    recall turn must not queue behind document work OR behind a semaphore.

    Fail-safe: ``DOC_LANE_CPU_OFF`` ⇒ inline call (legacy behaviour). An internal
    admission failure admits the work rather than dropping it.
    """
    if DOC_LANE_CPU_OFF:
        return func(*args, **kwargs)
    if current_lane() == LANE_BACKGROUND:
        try:
            await _admit()
        except DocLanePaused:
            raise
        except Exception as _adm_err:  # noqa: BLE001 — a broken guard must not stop work
            log.warning("cpu_lane.admit_failed_admitting_open err=%s", _adm_err)
            return await _dispatch(_bg_executor, func, args, kwargs)
        try:
            if _PROC_N > 0:
                _out = await _dispatch_process(func, args, kwargs)
                if _out is not _FALLBACK:
                    return _out
                # the boundary refused this call (unpicklable payload/result, dead
                # pool, failure budget) — the bounded threads below are today's
                # behaviour, and the refusal was logged with its reason upstream
            return await _dispatch(_bg_executor, func, args, kwargs)
        finally:
            _release()
    return await _dispatch(None, func, args, kwargs)
