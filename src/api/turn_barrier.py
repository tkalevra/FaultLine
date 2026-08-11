"""TURN BARRIER — admission control, so overload degrades a FEW turns instead of ALL of them.

WHY THIS EXISTS
---------------
``turn_budget`` bounds what a turn SPENDS. It cannot bound what the BOX has. Measured on the
local stack (4C/8T Xeon E3-1505M), after the recall walk was moved off the event loop:

    concurrency   4 → p95 0.69s    0% over 2s
    concurrency   8 → p95 1.06s    0% over
    concurrency  16 → p95 2.00s    0% over      <- the edge
    concurrency  20 → p95 2.53s   40% over
    concurrency  40 → p95 4.93s   47% over

Throughput is FLAT at ~9.2 req/s from 40 concurrent to 80, while latency grows linearly with
offered load. That is the signature of a capacity-saturated system, not a lock — and it means
there is nothing left to shed *inside* a turn. The budget correctly reported those turns as
over, skipped zero optional calls, and was right both times: the deterministic walk is not
optional, and at 40 concurrent it simply does not fit in 2s.

Without admission control, overload is shared EQUALLY: at 80 concurrent every one of the 80
customers waits 8.4s. Nobody is served well. A barrier changes the failure shape — a bounded
number of turns keep meeting the objective, and the excess gets a fast, honest "busy" instead
of a slow, uniform degradation that looks like the product being broken.

THE THRESHOLD IS DELIBERATELY BELOW THE CLIFF. 16 concurrent is where p95 *reaches* 2.0s —
sitting there means running with zero headroom, and the next arrival is already over. The
barrier flips at 14 so the admitted set stays inside budget rather than exactly on it.

WAIT BEFORE YOU SHED
--------------------
A turn that finds the barrier full does NOT fail immediately. It waits up to
``TURN_BARRIER_GRACE_S`` for a slot, because at this speed (p50 ~0.3-0.8s) slots free up fast
and a brief wait converts almost every would-be rejection into a normal, in-budget turn. Only
SUSTAINED overload — where no slot appears within the grace — sheds. This is what keeps
"never fail a customer turn" true in every regime except genuine saturation, where the honest
answer is that the box is full.

THE VALVE
---------
``TURN_BARRIER_ENABLED=false`` disables admission control entirely — unlimited concurrency,
byte-for-byte the prior behaviour. It is an OFF SWITCH, not a tuning knob: if this mechanism
ever misjudges load and starts shedding traffic the box could actually have served, an
operator must be able to take it out of the path in one env var and a restart, without a
code change or a rollback.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Optional

# Below the measured 2.0s-p95 edge (16), so the admitted set runs with headroom, not on it.
MAX_CONCURRENT = int(os.environ.get("TURN_BARRIER_MAX_CONCURRENT", "14"))
# How long a turn waits for a slot before being shed. Sized against the measured p50 (~0.3s):
# long enough that ordinary bursts drain into normal turns, short enough that a shed decision
# is still fast for the customer.
GRACE_S = float(os.environ.get("TURN_BARRIER_GRACE_S", "1.0"))
# THE VALVE. Off → no barrier at all.
ENABLED = os.environ.get("TURN_BARRIER_ENABLED", "true").lower() in ("1", "true", "yes", "on")
# Don't alert on every shed — one trip notice per interval per process.
ALERT_MIN_INTERVAL_S = float(os.environ.get("TURN_BARRIER_ALERT_INTERVAL_S", "900"))

_sem: Optional[asyncio.Semaphore] = None
_sem_loop: Optional[asyncio.AbstractEventLoop] = None
_sem_lock = threading.Lock()

# Optional host-supplied escalation hook, set via ``set_notifier``. Kept as a plain
# module-level slot rather than an import of any particular mailer so this file has NO
# dependency on the layer above it — the open core must not reference the closed one, and a
# self-hosted install must not need a mail stack to use admission control.
_notifier = None


def set_notifier(fn) -> None:
    """Register ``fn(message: str, snapshot: dict)`` to escalate a barrier trip.

    Called once at startup by whatever owns alerting for this deployment (an email sender, a
    webhook, a pager). Passing ``None`` clears it. The callback is invoked at most once per
    ``TURN_BARRIER_ALERT_INTERVAL_S`` and any exception it raises is swallowed.
    """
    global _notifier
    _notifier = fn


_state_lock = threading.Lock()
_state = {
    "in_flight": 0,
    "peak_in_flight": 0,
    "admitted": 0,
    "waited": 0,        # admitted, but only after queueing for a slot
    "shed": 0,
    "tripped": False,    # currently shedding
    "last_trip_at": 0.0,
    "last_alert_at": 0.0,
}


def _get_sem() -> asyncio.Semaphore:
    """One semaphore per event loop.

    Rebuilt if the running loop changes (tests, a reload, a worker respawn) — an
    ``asyncio.Semaphore`` is bound to the loop that created it, and reusing one across loops
    raises at await time. Guarded by a plain threading lock because this can be reached from
    more than one loop during startup.
    """
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    with _sem_lock:
        if _sem is None or _sem_loop is not loop:
            _sem = asyncio.Semaphore(MAX_CONCURRENT)
            _sem_loop = loop
        return _sem


def snapshot() -> dict:
    with _state_lock:
        return dict(_state)


def _should_alert(now: float) -> bool:
    """Rate-limit trip notices. Mutates last_alert_at under the lock so two simultaneous
    shedders cannot both decide to send."""
    with _state_lock:
        if now - _state["last_alert_at"] < ALERT_MIN_INTERVAL_S:
            return False
        _state["last_alert_at"] = now
        return True


class Admission:
    """Result of asking the barrier for a slot.

    ``admitted`` False means the caller must NOT run the turn — answer busy instead. Truthy
    for convenience so callers can write ``if adm:``.
    """

    __slots__ = ("admitted", "waited_s", "in_flight")

    def __init__(self, admitted: bool, waited_s: float, in_flight: int) -> None:
        self.admitted = admitted
        self.waited_s = waited_s
        self.in_flight = in_flight

    def __bool__(self) -> bool:
        return self.admitted


async def acquire() -> Admission:
    """Ask for a slot. Waits up to ``GRACE_S``; never raises.

    Valve off → always admitted, and no accounting beyond a counter, so the disabled path
    costs essentially nothing.
    """
    if not ENABLED:
        return Admission(True, 0.0, -1)
    sem = _get_sem()
    t0 = time.monotonic()
    try:
        # An uncontended acquire completes synchronously inside wait_for, so the common
        # case costs one timer setup and no waiting.
        await asyncio.wait_for(sem.acquire(), GRACE_S)
    except (asyncio.TimeoutError, TimeoutError):
        waited = time.monotonic() - t0
        with _state_lock:
            _state["shed"] += 1
            _state["tripped"] = True
            _state["last_trip_at"] = time.time()
            inflight = _state["in_flight"]
        return Admission(False, waited, inflight)
    waited = time.monotonic() - t0
    with _state_lock:
        _state["in_flight"] += 1
        _state["admitted"] += 1
        if waited > 0.01:
            _state["waited"] += 1
        if _state["in_flight"] > _state["peak_in_flight"]:
            _state["peak_in_flight"] = _state["in_flight"]
        inflight = _state["in_flight"]
    return Admission(True, waited, inflight)


def release() -> None:
    """Return a slot. Safe to call only for an ADMITTED turn (pair it in a ``finally``)."""
    if not ENABLED:
        return
    with _state_lock:
        if _state["in_flight"] > 0:
            _state["in_flight"] -= 1
        if _state["in_flight"] == 0:
            _state["tripped"] = False
    try:
        if _sem is not None:
            _sem.release()
    except Exception:
        # A release without a matching acquire would raise; never let slot bookkeeping
        # take down a request that already produced its answer.
        pass


def notify_trip(log, *, waited_s: float, in_flight: int) -> None:
    """Fire the trip notice through the channels that already exist.

    Rate-limited, best-effort, and NEVER raises — this runs while the box is already
    struggling, and an alerting failure must not become a second outage. Capacity is an
    OPERATOR concern (the box is full; a tenant cannot act on it), so it goes to the platform
    notify address rather than to the seat.
    """
    now = time.time()
    if not _should_alert(now):
        return
    snap = snapshot()
    msg = (f"FaultLine turn barrier TRIPPED: shedding recall turns. "
           f"max_concurrent={MAX_CONCURRENT} in_flight={in_flight} "
           f"waited={waited_s:.2f}s grace={GRACE_S}s "
           f"shed_total={snap['shed']} peak_in_flight={snap['peak_in_flight']}. "
           f"The box is at capacity — add workers/CPU or raise "
           f"TURN_BARRIER_MAX_CONCURRENT if this host can take it.")
    try:
        log.warning("turn_barrier.tripped", max_concurrent=MAX_CONCURRENT,
                    in_flight=in_flight, waited_s=round(waited_s, 3),
                    shed_total=snap["shed"], peak_in_flight=snap["peak_in_flight"])
    except Exception:
        pass
    # Escalate through whatever the HOST registered, if anything. This module deliberately
    # knows NOTHING about mail, webhooks, or any particular deployment's alerting — it hands
    # over a formatted message and a structured snapshot and lets the host decide. A
    # deployment with no notifier configured gets the log line above and nothing else, which
    # is the correct default for a self-hosted install that has no operator inbox.
    #
    # Best-effort by construction: a notifier that raises is swallowed. This fires while the
    # box is ALREADY at capacity, and an alerting failure must never become a second outage.
    fn = _notifier
    if fn is not None:
        try:
            fn(msg, snap)
        except Exception:
            pass


def busy_alert() -> dict:
    """The alert a SHED turn carries back to the client.

    It rides the same ``alerts`` array ``/query`` already returns. NOTE, because the earlier
    version of this docstring overstated it: FaultLine's own MCP client does NOT surface this
    payload. ``src/mcp/server.py`` calls ``resp.raise_for_status()`` before ``resp.json()``,
    so on a 503 the body is never parsed — the ``HTTPStatusError`` propagates into
    ``agent_proxy`` and lands on the non-timeout brain-error branch, which serves stale cache
    or the non-fabricating "lookup did not finish" note. Behaviour is correct either way; the
    payload is simply dead for that client and is here for HTTP callers that read the body.

    The 503 status is the load-bearing part, not this text: an empty 200 would read as "you
    have no facts", which is a claim about the user's memory that we did not look up.
    """
    return {
        "alert_type": "server_busy",
        "message": ("FaultLine is at capacity and did not run this recall. Your memory is "
                    "intact and nothing was lost — retry in a moment."),
        "retry_after_s": 1,
    }
