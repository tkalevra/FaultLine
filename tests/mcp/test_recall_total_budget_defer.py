"""MCP RECALL TOTAL BUDGET + DEFERRED DIVERTS — the recall-llm-burn lane (2026-10-26).

The brain-independence lane bounded the CLASSIFY probe (3s) and served passages regardless
of brain state — but the statement/correction LANES still ran INLINE on the recall path
against their own full LLM budgets (30–180s measured live, BUG-recall-door-statement-input-
llm-burn-20261026). This lane bounds the ENTIRE handler and defers the side-lanes:

  (a) hostile statement input (DROP TABLE string), slow brain, breaker CLOSED
      → passages served well inside the budget, no LLM lane held the response, the turn
        is queued unattested on the deferred drain
  (a2) the trailing-apostrophe correction shape ("zephyr'") → same outcome, correction lane
  (b) statement input, healthy lanes → "noted — processing in the background" + passages +
        facts; the deferred worker then runs the FULL pipeline with ingest_source="
        unattested" (authorship survives the defer) under the background LLM lane
  (c) healthy brain + QUERY intent → the FULL pipeline byte-for-byte: classify → harvest →
        /query, facts AND passages, no note, nothing queued
  (d) breaker open → ZERO LLM-lane traffic (classify/harvest/extract/retract all skipped),
        episodic retention + /query direct, passages served; the LLM-bearing /expand
        intercept is DECLINED with a loud envelope instead of burning
  (e) the total budget is a never-exceed CEILING: a turn stuck where no per-call timeout
        covers it is cancelled by the supervisor and answered with the honest degraded
        envelope, inside budget + cancel grace
  (worker) the deferred CORRECTION arm: retract_fact_tool(attested=False) in the
        background, ONE attempt (no blind retry — /retract/correct is not idempotent),
        loud CRIT-equivalent accounting on failure

All backend traffic is a URL router mock — no DB, no live API. Knobs are pinned tight so
the suite stays fast; the DEFAULT budget (12s) has its own parse pin.
"""

import asyncio
import os
import sys
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.mcp.server as S
from src.mcp.server import recall_memory_tool


_UID = "5d4c3b2a-1e0f-4a5b-8c7d-9e0f1a2b3c4d"

_PASSAGE_HITS = [
    {"document_id": 7, "title": "Wind Energy Handbook", "status": "ready",
     "chunk_index": 3, "chunk_text": "Modern turbines achieve 20-40% efficiency.",
     "chunk_count": 8, "rank": 0.09},
]

_QUERY_OK = {
    "facts": [{"definition": "You own a Corolla", "fact_class": "A",
               "provenance": "user_stated", "confidence": 0.95}],
    "attributes": {},
    "document_hits": list(_PASSAGE_HITS),
}


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=payload)
    if status >= 400:
        r.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(str(status), request=MagicMock(), response=r))
    else:
        r.raise_for_status = MagicMock()
    return r


class _Router:
    """AsyncMock-shaped backend router. `intent` is the INSTANT classify answer (the burn
    case is a brain that classifies fast then crawls on the LLM lanes); `hang_lanes` names
    URL substrings whose POSTs sleep ~30s then ReadTimeout (a slow/dead brain); GETs answer
    the route/gate reads. Records every call for the burn pins."""

    HANG_S = 30.0

    def __init__(self, *, intent="QUERY", hang_lanes=(), retract_error=False):
        self.posts = []   # (url, json_body, kwargs)
        self.gets = []
        self.intent = intent
        self.hang_lanes = tuple(hang_lanes)
        self.retract_error = retract_error
        self.query_payload = dict(_QUERY_OK)
        self.rewrite_edges = [
            {"subject": "user", "rel_type": "lives_in", "object": "fitzroy",
             "confidence": 0.9}]

    def _hangs(self, url):
        return any(lane in url for lane in self.hang_lanes)

    async def post(self, url, **kwargs):
        body = kwargs.get("json") or {}
        self.posts.append((url, dict(body), dict(kwargs)))
        if self._hangs(url):
            await asyncio.sleep(self.HANG_S)
            raise httpx.ReadTimeout(f"simulated slow lane: {url}")
        if "/classify-intent" in url:
            return _resp({"intent": self.intent, "confidence": 0.9})
        if "/harvest-spans" in url:
            return _resp({"edges": []})
        if "/episodic/append" in url:
            return _resp({"status": "stored", "id": 41})
        if "/query" in url:
            return _resp(self.query_payload)
        if "/extract/rewrite" in url:
            return _resp({"edges": list(self.rewrite_edges)})
        if "/retract/correct" in url:
            if self.retract_error:
                raise httpx.ConnectError("retract brain dead")
            return _resp({"status": "corrected", "retracted": 2})
        if "/ingest" in url:
            return _resp({"status": "stored", "committed": 1, "staged": 0})
        return _resp({})

    async def get(self, url, **kwargs):
        self.gets.append(url)
        if "/confidence-gate" in url:
            return _resp({"threshold": 0.70})
        if "/internal/ingest-route" in url:
            return _resp({"statement_extractor": "rewrite"})
        return _resp({})

    def post_urls(self):
        return [u for u, _, _ in self.posts]

    def bodies_for(self, needle):
        return [b for u, b, _ in self.posts if needle in u]

    def kwargs_for(self, needle):
        return [kw for u, _, kw in self.posts if needle in u]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Module-state hygiene: breaker reset (cross-turn by design), knobs pinned tight, and
    the deferred-turn drain isolated per test — a leftover worker from a previous test would
    consume this test's queue items the moment they are put."""
    S._brain_slow_reset("test isolation")
    monkeypatch.setenv("MCP_CLASSIFY_TIMEOUT_S", "0.3")
    monkeypatch.setenv("MCP_BRAIN_TIMEOUT_CLASSIFY_INTENT", "0.3")
    monkeypatch.setenv("MCP_RECALL_TOTAL_BUDGET_S", "2.0")
    S._FAULTLINE_URL_DETECTED = True
    _old_task, _old_queue = S._statement_defer_task, S._statement_defer_queue
    if _old_task is not None and not _old_task.done():
        _old_task.cancel()
    S._statement_defer_queue = None
    S._statement_defer_task = None
    for k in list(S._statement_defer_stats):
        S._statement_defer_stats[k] = 0
    yield
    if S._statement_defer_task is not None and not S._statement_defer_task.done():
        S._statement_defer_task.cancel()
    S._statement_defer_queue, S._statement_defer_task = _old_queue, _old_task
    S._brain_slow_reset("test isolation")


async def _recall(router, text="how efficient are wind turbines"):
    with patch.object(S, "_http_client", router):
        return await recall_memory_tool(text, _UID)


def _pin_worker_off():
    """Keep the deferred-turn drain from STARTING its worker this test: a pending future is
    never .done(), so the defer only queues — isolating the response-path pins from the
    background pipeline (pinned separately, with the real worker, below)."""
    S._statement_defer_queue = asyncio.Queue(maxsize=10)
    S._statement_defer_task = asyncio.get_running_loop().create_future()


# ── (a) hostile STATEMENT, slow brain, breaker CLOSED: no LLM lane holds the response ──


async def test_hostile_statement_passages_served_inside_budget_no_llm_burn():
    _pin_worker_off()
    router = _Router(intent="STATEMENT",
                     hang_lanes=("/harvest-spans", "/extract/rewrite", "/retract/correct", "/ingest"))
    t0 = time.monotonic()
    result = await _recall(router, "zephyr'; DROP TABLE facts;--")
    elapsed = time.monotonic() - t0

    # the response was served INSIDE the total budget (default pins 2.0s in the fixture) —
    # the old path burned 30–180s here against the same brain
    assert elapsed < 2.0 + 2.0 + 1.0, f"recall held the connection {elapsed:.2f}s"
    assert "Modern turbines achieve 20-40% efficiency." in result["memory"], result["memory"][:200]
    assert "Wind Energy Handbook" in result["memory"]
    assert not result.get("isError"), result
    # NO LLM lane held the turn: nothing waited for extract/retract/ingest (they hang 30s —
    # a single blocking await would have blown the elapsed bound); the harvest probe fired
    # once and was CUT at the walk-reserve gate
    for lane in ("/extract/rewrite", "/retract/correct", "/ingest"):
        assert not any(lane in u for u in router.post_urls()), (lane, router.post_urls())
    assert sum(1 for u in router.post_urls() if "/harvest-spans" in u) <= 1
    # the turn is QUEUED unattested on the deferred drain (the floor is the episodic row)
    assert S._statement_defer_queue.qsize() == 1
    text, uid, ing_source, route = S._statement_defer_queue._queue[0]
    assert (text, uid, ing_source, route) == (
        "zephyr'; DROP TABLE facts;--", _UID, "unattested", "STATEMENT")
    # the response says what happened to the statement
    assert "processing in the background" in result["memory"], result["memory"][:300]
    # the walk still fired with the RAW hostile text (passage-first floor)
    assert any("/query" in u for u in router.post_urls())
    await asyncio.sleep(0.05)  # let the background verbatim floor land
    assert router.bodies_for("/episodic/append"), "the verbatim floor never fired"


# ── (a2) the trailing-apostrophe CORRECTION shape — same guarantee, correction lane ────


async def test_apostrophe_correction_shape_never_blocks_on_the_correction_lane():
    _pin_worker_off()
    router = _Router(intent="CORRECTION",
                     hang_lanes=("/retract/correct", "/harvest-spans", "/extract/rewrite"))
    t0 = time.monotonic()
    result = await _recall(router, "zephyr'")
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0 + 2.0 + 1.0, f"recall held the connection {elapsed:.2f}s"
    assert "Modern turbines achieve 20-40% efficiency." in result["memory"]
    assert not result.get("isError"), result
    # the correction supersede NEVER ran on the response path — only /query may remain
    assert not any("/retract/correct" in u for u in router.post_urls()), router.post_urls()
    assert any("/query" in u for u in router.post_urls())
    assert "processing in the background" in result["memory"]
    # queued for the background supersede, unattested, intent preserved
    assert S._statement_defer_queue.qsize() == 1
    _t, _u, _s, route = S._statement_defer_queue._queue[0]
    assert (_t, _u, _s, route) == ("zephyr'", _UID, "unattested", "CORRECTION")


# ── (b) statement input, healthy lanes: noted + passages, worker runs it unattested ────


class _ClientShim:
    """Stand-in for httpx.AsyncClient that delegates to the router mock. retract_fact_tool
    builds its OWN raw 90s client (no shared seam), so patching _http_client does not
    cover the deferred correction arm — this shim routes it to the same mock."""

    def __init__(self, router, **_kw):
        self._router = router

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        return await self._router.post(url, **kw)


async def test_statement_deferred_then_worker_runs_full_pipeline_unattested():
    router = _Router(intent="STATEMENT")
    with patch.object(S, "remember_facts_tool") as fake_remember:
        t0 = time.monotonic()
        result = await _recall(router, "my dog is Rex and he is a good boy")
        elapsed = time.monotonic() - t0

    # the RESPONSE: noted + the walk's facts and passages; the LLM-bearing ingest NEVER
    # ran on the response path (elapsed is bounded by the budget even though the worker
    # may complete its fast mock pipeline concurrently — the (a) pin covers the slow brain)
    assert elapsed < 2.0 + 2.0 + 1.0, f"recall held the connection {elapsed:.2f}s"
    assert "processing in the background" in result["memory"], result["memory"][:300]
    assert "Modern turbines achieve 20-40% efficiency." in result["memory"]
    assert "Corolla" in result["memory"]
    assert not result.get("isError"), result
    fake_remember.assert_not_awaited()  # the statement lane is deferred, not inline

    # the deferred WORKER then runs the full pipeline off the response path
    assert S._statement_defer_queue is not None
    await S._statement_defer_queue.join()
    assert any("/extract/rewrite" in u for u in router.post_urls())
    # ...on the BACKGROUND LLM lane (paces, never steals interactive capacity)
    rewrite_kw = router.kwargs_for("/extract/rewrite")
    assert rewrite_kw and rewrite_kw[0].get("headers", {}).get("x-fl-llm-lane") == "background"
    # ...with the AUTHORSHIP preserved: an unattested divert can never come back user_stated
    ingest_bodies = router.bodies_for("/ingest")
    assert ingest_bodies and ingest_bodies[0].get("source") == "unattested", ingest_bodies
    assert S._statement_defer_stats["deferred"] == 1
    assert S._statement_defer_stats["recovered"] == 1
    # the verbatim floor landed with the brain's verdict (not transport-fabricated)
    await asyncio.sleep(0.05)
    epi = router.bodies_for("/episodic/append")
    assert epi and epi[0].get("intent") == "STATEMENT" and epi[0].get("source") == "unattested"


# ── (c) healthy brain + QUERY: the FULL pipeline, byte-for-byte, nothing deferred ──────


async def test_healthy_query_full_pipeline_facts_and_passages_nothing_deferred(monkeypatch):
    monkeypatch.delenv("MCP_RECALL_TOTAL_BUDGET_S", raising=False)  # the 12s default
    router = _Router(intent="QUERY")
    t0 = time.monotonic()
    result = await _recall(router, "how efficient are wind turbines")
    elapsed = time.monotonic() - t0

    # the budget is a CEILING, not a target: the healthy turn is the SAME 3-post pipeline
    # the brain-independence lane pinned, with facts AND passages, and no defer note
    assert elapsed < 5.0, f"healthy turn took {elapsed:.2f}s"
    posts = router.post_urls()
    assert posts == [p for p in posts if any(
        x in p for x in ("/classify-intent", "/harvest-spans", "/query"))], posts
    assert "/classify-intent" in posts[0] and "/harvest-spans" in posts[1] and "/query" in posts[2]
    assert any("/confidence-gate" in u for u in router.gets)
    assert "Corolla" in result["memory"] and "Wind Energy Handbook" in result["memory"]
    assert "processing in the background" not in result["memory"]
    assert S._statement_defer_stats["deferred"] == 0
    assert S._brain_slow_active(_UID) is False


# ── (d) breaker open: zero LLM-lane traffic, retention + /query direct ────────────────


async def test_open_breaker_skips_every_llm_lane_and_serves_passages():
    S._mark_brain_slow(_UID, "pin (d): a prior turn's classify timeout")
    router = _Router(intent="CORRECTION", hang_lanes=("/classify-intent",))  # would hang
    t0 = time.monotonic()
    result = await _recall(router, "zephyr'; DROP TABLE facts;--")
    elapsed = time.monotonic() - t0

    assert elapsed < 1.5, f"fast path took {elapsed:.2f}s — it probed something"
    for lane in ("/classify-intent", "/harvest-spans", "/extract/rewrite",
                 "/retract/correct", "/ingest"):
        assert not any(lane in u for u in router.post_urls()), (lane, router.post_urls())
    assert not any("/confidence-gate" in u for u in router.gets)
    # deterministic retention + the walk — and NOTHING else
    assert any("/episodic/append" in u for u in router.post_urls())
    epi = router.bodies_for("/episodic/append")
    assert epi and epi[0].get("intent") is None  # the brain classified nothing this turn
    assert any("/query" in u for u in router.post_urls())
    assert "Modern turbines achieve 20-40% efficiency." in result["memory"]
    assert not result.get("isError"), result
    # no defer either — routing never ran, so nothing was queued
    assert S._statement_defer_stats["deferred"] == 0


async def test_open_breaker_declines_expand_with_loud_envelope_zero_brain_calls():
    S._mark_brain_slow(_UID, "pin (d): /expand under an open window")
    router = _Router()
    result = await _recall(router, "/expand networking online")

    assert router.posts == [] and router.gets == [], "the learn LLM lane must not burn"
    assert result.get("isError") is True and result.get("status") == "degraded", result
    assert "too slow" in result["memory"], result["memory"][:200]
    assert result.get("brain_slow"), result


# ── (e) the total budget is a never-exceed ceiling — the supervisor arm ───────────────


async def test_budget_supervisor_cancels_a_stuck_turn_and_answers_honestly():
    router = _Router(intent="QUERY")

    async def _stuck_pending(uid):
        await asyncio.sleep(30.0)  # stuck where NO per-call timeout covers it
        return None

    with patch.object(S, "_pending_documents_notice", _stuck_pending), \
         patch.object(S, "_http_client", router):
        t0 = time.monotonic()
        result = await recall_memory_tool("how efficient are wind turbines", _UID)
        elapsed = time.monotonic() - t0

    # never-exceed: budget (2.0) + cancel grace (2.0) + slack — NOT the 30s the turn is
    # stuck for, and not the 240s turn wall
    assert elapsed < 2.0 + 2.0 + 1.5, f"supervisor let the turn run {elapsed:.2f}s"
    assert result.get("isError") is True and result.get("status") == "degraded", result
    assert result.get("recall_budget_s") == 2.0, result
    assert "not an empty memory" in result["memory"], result["memory"][:200]


def test_budget_knob_default_and_limits_table(monkeypatch):
    assert "MCP_RECALL_TOTAL_BUDGET_S" in S._ENV_LIMITS
    monkeypatch.delenv("MCP_RECALL_TOTAL_BUDGET_S", raising=False)
    assert S._recall_total_budget_s() == 12.0


# ── the deferred CORRECTION arm: background supersede, single attempt ─────────────────


async def test_correction_worker_applies_supersede_unattested_in_background():
    router = _Router(intent="CORRECTION")
    with patch.object(S.httpx, "AsyncClient", lambda **kw: _ClientShim(router)):
        # (the never-in-turn pin is (a2)'s — with an instant mock the background worker
        # may legitimately finish before the recall turn does)
        result = await _recall(router, "actually my dog is Rex not Rexx")
        assert "processing in the background" in result["memory"]

        await S._statement_defer_queue.join()
    retract_bodies = router.bodies_for("/retract/correct")
    assert len(retract_bodies) == 1
    assert retract_bodies[0].get("attested") is False, retract_bodies  # authorship pin
    assert retract_bodies[0].get("text") == "actually my dog is Rex not Rexx"
    assert S._statement_defer_stats["recovered"] == 1


async def test_correction_worker_failure_is_one_attempt_loud_and_counted():
    router = _Router(intent="CORRECTION", retract_error=True)
    with patch.object(S.httpx, "AsyncClient", lambda **kw: _ClientShim(router)):
        result = await _recall(router, "actually my dog is Rex not Rexx")
        assert "processing in the background" in result["memory"]

        await S._statement_defer_queue.join()
    # /retract/correct is NOT idempotent (LLM re-extract per call) — exactly ONE attempt,
    # never the statement arm's retry loop
    assert len(router.bodies_for("/retract/correct")) == 1, router.post_urls()
    assert S._statement_defer_stats["dropped"] == 1
    assert S._statement_defer_stats["deferred"] == 1
