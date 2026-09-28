"""DROPTURN — a transient /ingest failure must not discard already-extracted edges.

THE BUG (reproduced live before this test existed, see the internal design record):
every STATEMENT path in src/mcp/server.py extracted edges and POSTed them to /ingest exactly
ONCE. One transient blip on that POST discarded the edges and returned committed:0. Measured on
pre-prod: 21.7% of turns in one 984-turn run; the rate tracks LOAD, not code. The verbatim text
survived in episodic_log, but it re-entered the graph only via the re-embedder's
`reextract_episodic`, which is gated on `created_at < now() - INTERVAL '1 hour'` and drains 5
rows/tenant/300s — a HARD one-hour floor even with an empty backlog, 9-28 days at measured
backlogs — and re-ingests as `llm_inferred`, so a user-STATED Class-A fact came back DEMOTED.

THE FIX: one shared write seam (`_ingest_with_retry`) used by every /ingest call site.
  • bounded in-turn retry of the IDENTICAL body on transient classes only;
  • a serialized, bounded deferred drain when in-turn attempts are exhausted;
  • loud (_log_crit) on give-up, and NEVER a success shape for a write that did not happen.

WHY THE DOUBLE-WRITE GUARD SURVIVES: the guard was about RE-EXTRACTING (a second extraction
yields a DIFFERENT edge set). This seam re-POSTs the SAME edges, so /ingest's idempotency key —
which hashes `edges` (src/api/idempotency.py) — is unchanged, and both `facts` and `staged_facts`
upsert on `ON CONFLICT (subject_id, object_id, rel_type)`. `test_retry_body_is_byte_identical`
is the pin: if a future edit mutates the body between attempts, the key changes, the cache stops
protecting the write, and THAT is when a double-write becomes possible.

Mock-based, no live API. Follows tests/mcp/test_remember_facts_failsafe.py conventions.
"""

import asyncio
import json
import os
import sys

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.mcp.server as S
from src.mcp.server import ingest_document_tool, learn_facts_tool, remember_facts_tool

_UID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_EDGES = [{"subject": "user", "rel_type": "owns", "object": "corolla"}]


def _resp(json_body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=json_body)
    if status >= 400:
        r.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(str(status), request=MagicMock(), response=r)
        )
    else:
        r.raise_for_status = MagicMock()
    return r


@pytest.fixture(autouse=True)
def _pin_ready():
    """Skip the URL probe + provisioning gate, make retries instant, and reset all seam state.

    Backoff base is pinned to 0 so the bounded-retry semantics are tested without the test suite
    paying real wall-clock sleeps — the DELAY is not the contract, the ATTEMPT COUNT and the
    IDENTICAL BODY are.
    """
    S._FAULTLINE_URL_DETECTED = True
    S._provisioned_users.add(_UID)
    saved = (S._INGEST_RETRY_ATTEMPTS, S._INGEST_RETRY_BASE_S, S._INGEST_RETRY_MAX_INFLIGHT,
             S._INGEST_DEFER_ENABLED, S._INGEST_DEFER_ATTEMPTS, S._INGEST_DEFER_BASE_S,
             S._INGEST_DEFER_QUEUE_MAX)
    S._INGEST_RETRY_BASE_S = 0.0
    S._INGEST_DEFER_BASE_S = 0.0
    S._ingest_retry_inflight = 0
    S._ingest_defer_queue = None
    S._ingest_defer_task = None
    for k in S._ingest_retry_stats:
        S._ingest_retry_stats[k] = 0
    yield
    (S._INGEST_RETRY_ATTEMPTS, S._INGEST_RETRY_BASE_S, S._INGEST_RETRY_MAX_INFLIGHT,
     S._INGEST_DEFER_ENABLED, S._INGEST_DEFER_ATTEMPTS, S._INGEST_DEFER_BASE_S,
     S._INGEST_DEFER_QUEUE_MAX) = saved
    if S._ingest_defer_task is not None and not S._ingest_defer_task.done():
        S._ingest_defer_task.cancel()
    S._ingest_defer_queue = None
    S._ingest_defer_task = None
    S._provisioned_users.discard(_UID)


class _Backend:
    """Routing mock. `ingest_script` is a list of outcomes consumed one per /ingest attempt;
    the LAST entry repeats once exhausted. Records every /ingest body verbatim."""

    def __init__(self, ingest_script, *, route="spine", harvest_edges=None, rewrite_edges=None):
        self.ingest_script = list(ingest_script)
        self.route = route
        self.harvest_edges = _EDGES if harvest_edges is None else harvest_edges
        self.rewrite_edges = _EDGES if rewrite_edges is None else rewrite_edges
        self.ingest_bodies = []
        self.ingest_headers = []
        self.urls = []

    def _next_ingest(self):
        step = self.ingest_script[0] if len(self.ingest_script) == 1 else self.ingest_script.pop(0)
        if callable(step):
            step()  # raises a transport error
        return step

    async def post(self, url, *a, **k):
        self.urls.append(url)
        if "/classify-intent" in url:
            return _resp({"intent": "STATEMENT", "confidence": 0.9})
        if "/episodic/append" in url:
            return _resp({"ok": True})
        if "/harvest-spans" in url:
            return _resp({"edges": list(self.harvest_edges), "spans": 1})
        if "/extract/rewrite" in url:
            return _resp({"edges": list(self.rewrite_edges)})
        if "/ground-self-predication" in url:
            return _resp({"edges": []})
        if "/store_context" in url:
            return _resp({"status": "ok"})
        if "/ingest" in url:
            self.ingest_bodies.append(json.loads(json.dumps(k.get("json"))))
            self.ingest_headers.append(dict(k.get("headers") or {}))
            return self._next_ingest()
        return _resp({})

    async def get(self, url, *a, **k):
        if "/confidence-gate" in url:
            return _resp({"threshold": 0.70})
        if "/internal/ingest-route" in url:
            return _resp({"statement_extractor": self.route, "ingest_enabled": True})
        return _resp({})

    def client(self):
        mc = MagicMock()
        mc.post = AsyncMock(side_effect=self.post)
        mc.get = AsyncMock(side_effect=self.get)
        return mc


def _transport_error():
    def _raise():
        raise httpx.ReadTimeout("read timed out")
    return _raise


async def _drain(timeout=2.0):
    """Wait for the deferred worker to empty its queue (it is a background asyncio task)."""
    if S._ingest_defer_queue is None:
        return
    await asyncio.wait_for(S._ingest_defer_queue.join(), timeout=timeout)


# ── 1. The drop, and that it is now recovered in-turn ────────────────────────────────────

@pytest.mark.asyncio
async def test_spine_ingest_recovers_in_turn_after_transient_5xx():
    """THE reported bug: spine produced edges, /ingest 503'd, edges were discarded.
    Now the SAME edge set is re-POSTed and the turn commits."""
    be = _Backend([_resp({"error": "boom"}, 503), _resp({"status": "valid", "committed": 1})])
    with patch("src.mcp.server._http_client", be.client()):
        res = await remember_facts_tool("I drive a Corolla to work", _UID)
    assert res["status"] == "valid" and res["committed"] == 1
    assert len(be.ingest_bodies) == 2, "the failed /ingest was not retried"
    assert S._ingest_retry_stats["recovered"] == 1


@pytest.mark.asyncio
async def test_spine_ingest_recovers_after_transport_error():
    """A pure TRANSPORT error (ReadTimeout — no HTTP response at all) is retried too.
    httpx layers HTTPError → RequestError → TransportError, and TransportError is exactly the
    'request never got a settled answer' class (https://www.python-httpx.org/exceptions/)."""
    be = _Backend([_transport_error(), _resp({"status": "valid", "committed": 2})])
    with patch("src.mcp.server._http_client", be.client()):
        res = await remember_facts_tool("I drive a Corolla to work", _UID)
    assert res["committed"] == 2
    assert len(be.ingest_bodies) == 2


# ── 2. THE DOUBLE-WRITE GUARD ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_body_is_byte_identical_so_the_idempotency_key_is_unchanged():
    """LOAD-BEARING. /ingest dedups on IdempotencyManager.generate_key(), which hashes `edges`.
    A retry is only safe because the body is IDENTICAL — same key ⇒ a backend that already
    committed answers from cache instead of writing twice. If a future edit mutates the body
    between attempts (adds a nonce, a timestamp, a retry counter, re-extracts), the key changes,
    the cache stops protecting the write, and the double-write the original comment feared
    becomes real. This test fails the moment that happens."""
    be = _Backend([_resp({}, 503), _resp({}, 503), _resp({"status": "valid", "committed": 1})])
    S._INGEST_RETRY_ATTEMPTS = 3
    with patch("src.mcp.server._http_client", be.client()):
        await remember_facts_tool("I drive a Corolla to work", _UID)
    assert len(be.ingest_bodies) == 3
    first = json.dumps(be.ingest_bodies[0], sort_keys=True)
    for i, b in enumerate(be.ingest_bodies[1:], start=2):
        assert json.dumps(b, sort_keys=True) == first, (
            f"/ingest attempt {i} sent a DIFFERENT body — the idempotency key changed, "
            f"so this retry is NO LONGER protected against a double-write")


@pytest.mark.asyncio
async def test_no_reextraction_between_ingest_attempts():
    """The original refusal to fall back to /extract/rewrite after an /ingest failure was about
    RE-EXTRACTION producing a different edge set. That reasoning is preserved: the retry loop
    calls /ingest only — it never re-enters an extractor."""
    be = _Backend([_resp({}, 503), _resp({}, 503), _resp({}, 503)], route="spine")
    S._INGEST_RETRY_ATTEMPTS = 3
    with patch("src.mcp.server._http_client", be.client()):
        await remember_facts_tool("I drive a Corolla to work", _UID)
    harvests = [u for u in be.urls if "/harvest-spans" in u]
    rewrites = [u for u in be.urls if "/extract/rewrite" in u]
    assert len(harvests) == 1, "the spine extractor ran more than once across the retries"
    assert len(rewrites) == 0, "a retry fell back into the LLM extractor — that is the double-write risk"


# ── 3. Exhaustion is LOUD and never success-shaped ───────────────────────────────────────

@pytest.mark.asyncio
async def test_exhausted_spine_returns_an_honest_failure_not_a_success_shape():
    be = _Backend([_resp({}, 503)])
    S._INGEST_RETRY_ATTEMPTS = 2
    with patch("src.mcp.server._http_client", be.client()):
        res = await remember_facts_tool("I drive a Corolla to work", _UID)
    assert res["status"] == "error"
    assert res["committed"] == 0
    assert res["retry_pending"] is True and res["edges_pending"] == len(_EDGES)
    assert len(be.ingest_bodies) == 2


@pytest.mark.asyncio
async def test_exhausted_rewrite_path_returns_degraded_and_defers():
    """The second reported path: /extract/rewrite succeeded, /ingest failed, edges discarded."""
    be = _Backend([_resp({}, 500)], route="rewrite")
    S._INGEST_RETRY_ATTEMPTS = 2
    with patch("src.mcp.server._http_client", be.client()):
        res = await remember_facts_tool("I drive a Corolla to work", _UID)
    assert res["status"] == "degraded" and res["committed"] == 0
    assert res["retry_pending"] is True
    assert S._ingest_retry_stats["deferred"] == 1


@pytest.mark.asyncio
async def test_give_up_is_logged_critical():
    """A dropped turn must be VISIBLE — a warning buried in a log is not enough, and a
    success-shaped zero is worse. The seam emits _log_crit with the edge count."""
    be = _Backend([_resp({}, 503)])
    S._INGEST_RETRY_ATTEMPTS = 1
    S._INGEST_DEFER_ENABLED = False
    with patch("src.mcp.server._http_client", be.client()), \
            patch("src.mcp.server._log_crit") as crit:
        res = await remember_facts_tool("I drive a Corolla to work", _UID)
    assert res["committed"] == 0 and res["retry_pending"] is False
    events = [c.args[0] for c in crit.call_args_list]
    assert "ingest_turn_not_committed" in events
    msg = next(c.args[1] for c in crit.call_args_list if c.args[0] == "ingest_turn_not_committed")
    assert "1 extracted edge(s) NOT committed" in msg
    assert "THIS TURN IS DROPPED" in msg


# ── 4. Permanent failures are NOT retried (they are load, not recovery) ──────────────────

@pytest.mark.asyncio
async def test_400_constraint_violation_is_not_retried_and_not_deferred():
    """/ingest answers a psycopg constraint violation with 400. That is a decision the backend
    already made and will make again — repeating it is pure wasted load under exactly the
    conditions that caused the storm."""
    be = _Backend([_resp({"detail": "Database constraint violation"}, 400)])
    S._INGEST_RETRY_ATTEMPTS = 5
    with patch("src.mcp.server._http_client", be.client()), \
            patch("src.mcp.server._log_crit") as crit:
        res = await remember_facts_tool("I drive a Corolla to work", _UID)
    assert len(be.ingest_bodies) == 1, "a deterministic 400 was retried"
    assert S._ingest_retry_stats["deferred"] == 0, "a deterministic 400 was queued for retry"
    assert res["committed"] == 0
    assert "ingest_permanent_failure" in [c.args[0] for c in crit.call_args_list]


@pytest.mark.parametrize("status,transient", [
    (408, True), (425, True), (429, True), (500, True), (502, True), (503, True), (504, True),
    (400, False), (401, False), (403, False), (404, False), (409, False), (422, False),
])
def test_transient_classification(status, transient):
    exc = httpx.HTTPStatusError(str(status), request=MagicMock(), response=_resp({}, status))
    assert S._ingest_failure_is_transient(exc) is transient


@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("t"), httpx.ConnectTimeout("t"), httpx.ConnectError("c"),
    httpx.RemoteProtocolError("p"), httpx.PoolTimeout("p"), httpx.WriteTimeout("w"),
])
def test_transport_errors_are_transient(exc):
    """All of these descend HTTPError → RequestError → TransportError per the httpx docs."""
    assert S._ingest_failure_is_transient(exc) is True


def test_non_httpx_exception_is_not_transient():
    """A genuine programming error is not a transport blip and must never be retried away."""
    assert S._ingest_failure_is_transient(KeyError("boom")) is False


# ── 5. The deferred drain actually lands the fact ────────────────────────────────────────

@pytest.mark.asyncio
async def test_deferred_drain_lands_the_edges_after_in_turn_attempts_are_exhausted():
    """Time-to-retrievable: seconds (one backoff cycle), not the episodic lane's 1-hour floor
    plus a multi-day backlog."""
    be = _Backend([_resp({}, 503), _resp({}, 503), _resp({"status": "valid", "committed": 1})])
    S._INGEST_RETRY_ATTEMPTS = 2
    with patch("src.mcp.server._http_client", be.client()):
        res = await remember_facts_tool("I drive a Corolla to work", _UID)
        assert res["committed"] == 0 and res["retry_pending"] is True
        await _drain()
    assert len(be.ingest_bodies) == 3, "the deferred worker did not re-post"
    assert S._ingest_retry_stats["defer_recovered"] == 1
    # Still the SAME body — the deferred lane is under the same idempotency protection.
    assert be.ingest_bodies[-1] == be.ingest_bodies[0]


@pytest.mark.asyncio
async def test_deferred_drain_gives_up_loudly_and_does_not_spin():
    be = _Backend([_resp({}, 503)])
    S._INGEST_RETRY_ATTEMPTS = 1
    S._INGEST_DEFER_ATTEMPTS = 3
    with patch("src.mcp.server._http_client", be.client()), \
            patch("src.mcp.server._log_crit") as crit:
        await remember_facts_tool("I drive a Corolla to work", _UID)
        await _drain()
    assert len(be.ingest_bodies) == 1 + 3, "the deferred lane is not bounded"
    assert S._ingest_retry_stats["defer_dropped"] == 1
    assert "ingest_defer_exhausted" in [c.args[0] for c in crit.call_args_list]


@pytest.mark.asyncio
async def test_deferred_drain_abandons_a_permanent_failure_immediately():
    """A body that starts transient but turns out to be permanently rejected must not consume
    the whole deferred budget."""
    be = _Backend([_resp({}, 503), _resp({}, 400)])
    S._INGEST_RETRY_ATTEMPTS = 1
    S._INGEST_DEFER_ATTEMPTS = 5
    with patch("src.mcp.server._http_client", be.client()), \
            patch("src.mcp.server._log_crit") as crit:
        await remember_facts_tool("I drive a Corolla to work", _UID)
        await _drain()
    assert len(be.ingest_bodies) == 2
    assert "ingest_defer_permanent_failure" in [c.args[0] for c in crit.call_args_list]


# ── 6. STORM CASE — the recovery lane must never amplify the failure ─────────────────────

@pytest.mark.asyncio
async def test_storm_brake_collapses_in_turn_retries_when_many_are_already_backing_off():
    """The backlog is self-feeding: retries must shed, not multiply. Once _INGEST_RETRY_MAX_INFLIGHT
    turns are already sleeping between attempts, the live path degrades to ONE attempt and lets the
    serialized deferred lane carry the load."""
    be = _Backend([_resp({}, 503)])
    S._INGEST_RETRY_ATTEMPTS = 5
    S._INGEST_RETRY_MAX_INFLIGHT = 2
    S._ingest_retry_inflight = 2  # pretend a storm is already in progress
    S._INGEST_DEFER_ENABLED = False
    with patch("src.mcp.server._http_client", be.client()):
        res = await remember_facts_tool("I drive a Corolla to work", _UID)
    assert len(be.ingest_bodies) == 1, "the storm brake did not shed the extra attempts"
    assert res["committed"] == 0


@pytest.mark.asyncio
async def test_defer_queue_is_bounded_and_a_full_queue_is_a_loud_drop():
    """An unbounded queue under a breaker-open storm is just a slower way to fall over. At
    capacity the seam drops LOUDLY and says so — it never silently swallows the turn."""
    S._INGEST_DEFER_QUEUE_MAX = 1
    S._ingest_defer_queue = asyncio.Queue(maxsize=1)
    S._ingest_defer_queue.put_nowait(({"edges": []}, "filler"))
    S._ingest_defer_task = MagicMock(done=MagicMock(return_value=False))
    with patch("src.mcp.server._log_crit") as crit:
        ok = await S._defer_ingest({"edges": _EDGES, "user_id": _UID}, "storm")
    assert ok is False
    assert S._ingest_retry_stats["defer_rejected"] == 1
    assert "ingest_defer_queue_full" in [c.args[0] for c in crit.call_args_list]
    S._ingest_defer_task = None


@pytest.mark.asyncio
async def test_deferred_lane_is_serialized_so_it_adds_at_most_one_concurrent_ingest():
    """One worker: no matter how deep the queue, the recovery lane issues ONE /ingest at a time,
    which is strictly less load than the traffic that is already failing.

    Concurrency is counted ONLY for posts issued from inside the deferred worker task — an
    earlier version of this test counted the LIVE turns too and reported 3 concurrent calls,
    which measured asyncio.gather, not the drain. The claim under test is about the RECOVERY
    lane's added load, so the instrumentation must be able to tell the two apart."""
    concurrent = {"now": 0, "max": 0}
    be = _Backend([_resp({}, 503)])

    async def counting_post(url, *a, **k):
        from_worker = (
            S._ingest_defer_task is not None
            and asyncio.current_task() is S._ingest_defer_task
        )
        if "/ingest" in url and from_worker:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
            await asyncio.sleep(0.01)
            concurrent["now"] -= 1
        return await be.post(url, *a, **k)

    mc = MagicMock()
    mc.post = AsyncMock(side_effect=counting_post)
    mc.get = AsyncMock(side_effect=be.get)

    S._INGEST_RETRY_ATTEMPTS = 1
    S._INGEST_DEFER_ATTEMPTS = 2
    with patch("src.mcp.server._http_client", mc):
        await asyncio.gather(*(
            remember_facts_tool(f"I drive a Corolla number {i} to work", _UID) for i in range(5)
        ))
        await _drain(timeout=5.0)
    # 5 turns all failed and all deferred, so the drain definitely ran.
    assert S._ingest_retry_stats["deferred"] == 5
    assert concurrent["max"] == 1, (
        f"the deferred drain issued {concurrent['max']} concurrent /ingest calls — "
        f"it can amplify the storm it exists to absorb")


@pytest.mark.asyncio
async def test_only_one_deferred_worker_task_is_ever_created():
    """The serialization guarantee rests on there being exactly ONE consumer. _defer_ingest has
    no await before it creates the task and enqueues, so concurrent turns cannot race a second
    worker into existence — pin that, because adding an await there would silently break it."""
    be = _Backend([_resp({}, 503)])
    S._INGEST_RETRY_ATTEMPTS = 1
    created: list = []
    real_create = asyncio.get_running_loop().create_task

    with patch("src.mcp.server._http_client", be.client()):
        loop = asyncio.get_running_loop()
        with patch.object(type(loop), "create_task", autospec=True) as ct:
            def _spy(self, coro, **kw):
                # asyncio.gather itself wraps each turn in a task — count ONLY the drain worker.
                name = getattr(coro, "__qualname__", "") or getattr(
                    getattr(coro, "cr_code", None), "co_name", "")
                t = real_create(coro, **kw)
                if "_ingest_defer_worker" in name:
                    created.append(t)
                return t
            ct.side_effect = _spy
            await asyncio.gather(*(
                remember_facts_tool(f"I drive a Corolla number {i} to work", _UID)
                for i in range(5)
            ))
        await _drain(timeout=5.0)
    assert len(created) == 1, f"{len(created)} deferred workers were created — the drain is not serialized"


# ── 7. EVERY other write path that can return committed:0 ────────────────────────────────

@pytest.mark.asyncio
async def test_learn_facts_transient_ingest_failure_is_retried_not_dropped():
    """A path NOT in the original report, found by grepping every /ingest write site:
    learn_facts_tool parsed its edges then POSTed once with a bare raise_for_status()."""
    be = _Backend([_resp({}, 503), _resp({"status": "valid", "committed": 1, "staged": 0})])
    with patch("src.mcp.server._http_client", be.client()):
        res = await learn_facts_tool("A beagle is a subclass of a dog", _UID)
    assert res["status"] == "learned" and res["committed"] == 1
    assert len(be.ingest_bodies) == 2


@pytest.mark.asyncio
async def test_learn_facts_exhausted_returns_degraded_never_raises():
    be = _Backend([_resp({}, 503)])
    S._INGEST_RETRY_ATTEMPTS = 2
    with patch("src.mcp.server._http_client", be.client()):
        res = await learn_facts_tool("A beagle is a subclass of a dog", _UID)
    assert res["status"] == "degraded" and res["total"] == 0
    assert res["retry_pending"] is True


@pytest.mark.asyncio
async def test_document_chunk_ingest_failure_is_retried_and_reported():
    """The document lane dropped a whole chunk's edges on one blip, and the summary said only
    'failed' — it could not distinguish a queued chunk from a lost one."""
    be = _Backend([_resp({}, 503)], route="spine")
    S._INGEST_RETRY_ATTEMPTS = 2
    with patch("src.mcp.server._http_client", be.client()):
        res = await ingest_document_tool("Corollas are reliable cars.", _UID)
    assert res["chunks_failed"] >= 1
    assert res["chunks_deferred_retry"] >= 1, (
        "a deferred chunk is indistinguishable from a dropped one in the summary")
    assert res["facts_committed"] == 0


@pytest.mark.asyncio
async def test_harvest_turn_facts_defers_instead_of_silently_returning_zero():
    """_harvest_turn_facts swallows every exception and returns 0. That is still the contract
    (its callers count edges only on a confirmed write) — but the edges are now on the deferred
    drain rather than gone."""
    be = _Backend([_resp({}, 503), _resp({"status": "valid", "committed": 1})])
    S._INGEST_RETRY_ATTEMPTS = 1
    with patch("src.mcp.server._http_client", be.client()):
        n = await S._harvest_turn_facts("I fixed the fence three weeks ago", _UID)
        assert n == 0, "must not claim capture for a write that has not landed"
        await _drain()
    assert S._ingest_retry_stats["defer_recovered"] == 1
    assert len(be.ingest_bodies) == 2


# ── 8. The healthy path is untouched ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_healthy_ingest_posts_exactly_once():
    """No retry, no defer, no added latency when the backend answers cleanly — the seam must be
    invisible on the success path."""
    be = _Backend([_resp({"status": "valid", "committed": 3, "staged": 1})])
    with patch("src.mcp.server._http_client", be.client()):
        res = await remember_facts_tool("I drive a Corolla to work", _UID)
    assert res["committed"] == 3
    assert len(be.ingest_bodies) == 1
    assert S._ingest_retry_stats == {
        "attempts": 1, "retried": 0, "recovered": 0,
        "deferred": 0, "defer_recovered": 0, "defer_dropped": 0, "defer_rejected": 0,
    }


# ── 9. DEFERRED-REPLAY MARKER — a re-send must be distinguishable from a fresh turn ───────
#
# The store's replay guard (store.py::commit, main.py::_commit_staged) can only protect a
# superseded row if it can TELL a replay from a genuine user statement. The signal is the
# X-FL-Deferred-Replay HEADER (src/api/ingest_transport.py), set on every re-send of an
# already-attempted body. Store-half pins: tests/fact_store/test_replay_guard.py.

_MARKER = S._ingest_transport.REPLAY_MARKER_HEADER
_MARKER_VALUE = S._ingest_transport.REPLAY_MARKER_VALUE


@pytest.mark.asyncio
async def test_first_attempt_carries_no_replay_marker():
    """A FIRST attempt IS a genuine write — marking it would make the store freeze rows
    the user is stating right now. Attempt 1 must carry NO marker."""
    be = _Backend([_resp({"status": "valid", "committed": 1})])
    with patch("src.mcp.server._http_client", be.client()):
        await remember_facts_tool("I drive a Corolla to work", _UID)
    assert len(be.ingest_bodies) == 1
    assert _MARKER not in be.ingest_headers[0]


@pytest.mark.asyncio
async def test_retry_attempt_carries_the_marker_in_the_header_never_the_body():
    """Attempt 2 carries the marker as a HEADER; the BODY stays byte-identical. The
    idempotency key hashes edges — a body-borne marker would break the double-write guard
    in the very act of delivering the replay signal."""
    be = _Backend([_resp({}, 503), _resp({"status": "valid", "committed": 1})])
    with patch("src.mcp.server._http_client", be.client()):
        await remember_facts_tool("I drive a Corolla to work", _UID)
    assert len(be.ingest_bodies) == 2
    assert _MARKER not in be.ingest_headers[0]
    assert be.ingest_headers[1].get(_MARKER) == _MARKER_VALUE
    assert be.ingest_bodies[1] == be.ingest_bodies[0], "the marker leaked into the body"


@pytest.mark.asyncio
async def test_deferred_worker_posts_carry_the_marker():
    """The defer lane only ever re-sends bodies that already failed their in-turn attempts
    (that is the only way in), so EVERY worker POST carries the marker — it rides
    _DEFER_LANE_HEADERS alongside the LLM lane header."""
    be = _Backend([_resp({}, 503), _resp({}, 503), _resp({"status": "valid", "committed": 1})])
    S._INGEST_RETRY_ATTEMPTS = 2
    with patch("src.mcp.server._http_client", be.client()):
        await remember_facts_tool("I drive a Corolla to work", _UID)
        await _drain()
    assert len(be.ingest_bodies) == 3
    assert _MARKER not in be.ingest_headers[0]
    assert be.ingest_headers[1][_MARKER] == _MARKER_VALUE
    assert be.ingest_headers[2][_MARKER] == _MARKER_VALUE, "the deferred re-post was unmarked"
    assert be.ingest_headers[2].get(S._llm_lane.LANE_HEADER) == S._llm_lane.LANE_BACKGROUND


@pytest.mark.asyncio
async def test_retry_preserves_caller_headers_and_never_mutates_them():
    """The seam merges the marker INTO the caller's headers without mutating the caller's
    dict (shared module-level literals must stay constant), and keeps any lane marker the
    caller asked for."""
    be = _Backend([_resp({}, 503), _resp({"status": "valid", "committed": 1})])
    caller_headers = {"x-fl-llm-lane": "background"}
    with patch("src.mcp.server._http_client", be.client()):
        data, reason = await S._ingest_with_retry(
            {"edges": _EDGES, "user_id": _UID}, label="t", headers=caller_headers)
    assert reason is None and data["committed"] == 1
    assert caller_headers == {"x-fl-llm-lane": "background"}, "the caller's dict was mutated"
    assert be.ingest_headers[0] == {"x-fl-llm-lane": "background"}
    assert be.ingest_headers[1] == {"x-fl-llm-lane": "background", _MARKER: _MARKER_VALUE}
