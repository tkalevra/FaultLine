"""DOC-LOSS §7.2 — the doc-lane chunk fan-out vs a 4-slot brain, POST connect-split.

The design doc (DEV/DESIGN-document-ingest-silent-loss.md §7 item 2) flagged
DOC_CHUNK_CONCURRENCY=16 against a 4-slot brain as the proximate cause of the 831
circuit_breaker_open events, and asked for a 4-6 measurement before the bench switches
over. Two things have moved since it was written, and BOTH are measured here:

  • 55d31515 split the connect phase from the read budget at every LLM POST seam
    (LLM_CONNECT_TIMEOUT_S, default 5s) and made the breaker trip at 2 consecutive
    connect-phase failures (LLM_BREAKER_CONNECT_TRIP). A dead brain no longer costs a
    read budget per in-flight chunk, so the fan-out no longer multiplies the burn.
  • The code default moved off the doc-era world entirely: chunk_queue.py resolves
    PER-TENANT concurrency from the measured p95 + effective rpm (Little's Law) with a
    hard profile ceiling — local=4 (CHUNK_QUEUE_MAX_LOCAL), hosted=12, slow=2 — so a
    CALIBRATED 4-slot brain is capped at 4 regardless of the global; the global default
    (8 since ticket-06's starvation measurement, litmus fix L5) governs only
    un-calibrated tenants and the legacy pool. DOC_CHUNK_CONCURRENCY stays the env
    lever: 16, 6 and 4 are all still reachable for an operator who wants them.

MEASURED (one-off harness, this rig, 2026-11-08; committed pins below are the
jitter-robust 1-second-margin shape of the same two economies):

  Healthy 4-slot brain, prod ratio (service 4s/chunk, EXTRACT read budget 30s):
      fan_out  4 -> 4/4 ok   wall  4.1s   (headroom 87%)
      fan_out  8 -> 8/8 ok   wall 12.1s   (headroom 60%)
      fan_out 16 -> 16/16 ok wall 28.2s   (headroom  6% — the NOMINAL-service edge;
              the DOCLOSS live run measured this same width DEGRADING the brain:
              53/309 chunk timeouts + 831 breaker events — service inflation eats the
              margin. Clean at nominal is not clean under load.)
  Same rig, degraded shape (single-server queue, service 4s): fan_out 16 -> 9/16 fail.
  Blackhole, connect=5s read=30s (post-split): ALL of fan_out 4/8/16 exhaust at
      wall ~= 5.1s — fan-out-INDEPENDENT (pre-split arithmetic: 120/240/480
      call-seconds of budget burn for the same three widths).

VERDICT (evidence, not vibes): the default stays 8. 8 measures clean at the prod ratio
with >=60% headroom and every failure it can still hit is LOUD and routed (a read
timeout is a chunk failure -> chunks_failed -> terminal partial + CRIT, pinned in
tests/test_document_ingest_loss.py; a connect failure defers the whole document,
pinned there too; a blackholed brain trips the breaker in ~2 calls at ~36s wall
regardless of width, pinned in tests/test_connect_read_split_and_breaker_trip.py).
16 is the measured edge, not the default. §7 item 2 is closed as
MEASURED-NO-CHANGE in the design doc, with these numbers.
"""

import contextlib
import os
import socket
import threading
import time

import httpx
import pytest

from src.api import llm_calls as L


# -- ECONOMY 1: a healthy N-slot brain saturates LOUDLY, not silently ------------
#
# A real 4-slot LLM brain serves 4 requests CONCURRENTLY; fan-out wider than the slots
# queues at the brain and the queued wave's response lands a full service-time later.
# The pin holds the shape that matters for the DOC lane: the over-wide wave FAILS WITH
# ReadTimeout — the brain accepted the connection and never answered in budget — which
# the chunk ladder routes to chunks_failed and a terminal 'partial' (never a silent
# 200-empty). The 1.0s margins on both sides are deliberate GIL-jitter headroom.


class _SlotBrain:
    """Loopback server serving exactly `slots` connections CONCURRENTLY, each after
    `service_s` seconds; later connections wait a full wave ((pos-1)//slots) * service_s
    — the shape of a 4-slot LLM brain with fan-out wider than its capacity."""

    def __init__(self, slots, service_s):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self.host, self.port = self._srv.getsockname()
        self._stop = threading.Event()
        self._sem = threading.BoundedSemaphore(slots)
        self._service_s = service_s
        self._lock = threading.Lock()
        self._pos = 0
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        self._srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._pos += 1
                pos = self._pos
            threading.Thread(target=self._serve_one, args=(conn, pos), daemon=True).start()

    def _serve_one(self, conn, pos):
        # The wave wait needs no explicit arithmetic: with only `slots` semaphore
        # permits, connection N > slots blocks in acquire() until a slot frees —
        # exactly one full service-time per extra wave, the queued-at-the-brain shape.
        acquired = self._sem.acquire(timeout=self._service_s * 40)
        try:
            if not acquired:
                return
            time.sleep(self._service_s)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        except Exception:
            pass
        finally:
            self._sem.release()
            try:
                conn.close()
            except Exception:
                pass

    @property
    def url(self):
        return "http://%s:%d/v1/chat/completions" % (self.host, self.port)

    def close(self):
        self._stop.set()
        try:
            self._srv.close()
        except Exception:
            pass


def _fire(url, timeout, n, tally, errs):
    threads = []

    def one():
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(url, json={})
                if r.status_code == 200:
                    tally["ok"] += 1
                else:
                    tally["fail"] += 1
        except Exception as e:
            tally["fail"] += 1
            errs[type(e).__name__] = errs.get(type(e).__name__, 0) + 1

    for _ in range(n):
        t = threading.Thread(target=one)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


def test_fanout_at_the_slot_count_is_clean_and_wider_fails_loud(monkeypatch):
    """4-slot brain, service 2s, EXTRACT-style read budget 3s: fan-out == slots is
    clean; the next wave of a 2x fan-out times out LOUDLY (ReadTimeout — delivered,
    unanswered), which the chunk ladder counts as chunk failures, never as empties."""
    brain = _SlotBrain(slots=4, service_s=2.0)
    try:
        to = L.split_connect_timeout(3.0)
        assert isinstance(to, httpx.Timeout) and to.read == 3.0
        time.sleep(0.1)
        tally, errs = {"ok": 0, "fail": 0}, {}
        _fire(brain.url, to, 4, tally, errs)
        assert tally == {"ok": 4, "fail": 0}, (tally, errs)

        tally, errs = {"ok": 0, "fail": 0}, {}
        _fire(brain.url, to, 8, tally, errs)
        assert tally["ok"] == 4, (tally, errs)
        assert tally["fail"] == 4, (tally, errs)
        assert errs.get("ReadTimeout") == 4, (
            "the over-wide wave must be a READ timeout (brain accepted, did not ",
            f"answer in budget) — loud and routable, never a silent 200-empty: {errs}")
    finally:
        brain.close()


# -- ECONOMY 2: a blackholed brain burns a CONNECT budget, fan-out-independently --
#
# The DOCLOSS-era wound: under uniform budgets, 16-wide fan-out against a dead brain
# burned 16 x read-budget per wave and needed threshold-many failures to trip the
# breaker (831 events / 60 min measured). Post-55d31515 every call dies at the connect
# budget, so the whole batch exhausts in ~one connect budget REGARDLESS of width, and
# the breaker trips at 2 consecutive connect-fails (pinned next door in
# tests/test_connect_read_split_and_breaker_trip.py). Same rig as that file: a filled
# accept queue drops further SYNs, a genuine TCP connect blackhole.


@contextlib.contextmanager
def _connect_blackhole():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    fillers = []
    try:
        for _ in range(2):
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.settimeout(5)
            c.connect((host, port))
            fillers.append(c)
        victim = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        victim.settimeout(2)
        t0 = time.perf_counter()
        try:
            victim.connect((host, port))
            if time.perf_counter() - t0 < 1.0:
                pytest.skip("kernel accepts the overflow - rig is not blackholing here")
        except OSError:
            pass
        finally:
            victim.close()
        yield host, port
    finally:
        for c in fillers:
            with contextlib.suppress(Exception):
                c.close()
        srv.close()


def test_blackhole_exhaustion_wall_is_fanout_independent(monkeypatch):
    """8-wide fan-out against a dead brain: every call exhausts at the CONNECT budget
    (~1s here), so the batch's whole burn is one connect budget — the fan-out no longer
    multiplies it (pre-split arithmetic at prod budgets: 8 x 30s = 240 call-seconds)."""
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_S", "1.0")
    assert L.connect_trip_n() >= 1, "the connect trip must be armed for this economy"
    with _connect_blackhole() as target:
        url = f"http://{target[0]}:{target[1]}/v1/chat/completions"
        to = L.split_connect_timeout(30.0)
        errs = {}
        tally = {"ok": 0, "fail": 0}
        t0 = time.perf_counter()
        _fire(url, to, 8, tally, errs)
        wall = time.perf_counter() - t0
    assert tally["ok"] == 0 and errs.get("ConnectTimeout") == 8, (tally, errs)
    assert wall < 4.0, (
        f"a dead brain must exhaust the whole batch at connect budgets, not read "
        f"budgets: wall={wall:.2f}s for 8 calls")
