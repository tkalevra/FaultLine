"""P0 PASSAGE SEAM — the floor fires regardless of brain state.

Pins the restructure of the /query wrapper (adversarial bench 2026-10-24,
DEV/BUGS/adversarial-bench-20261024/ BUG-01 + BUG-02 + BUG-07) with the walk +
passage seams stubbed at their EXACT wrapper attach points (same pattern as
tests/test_recall_miss_escalation.py — no database needed; the R7 marker-DB
semantics stay pinned there):

  (a) brain-slow (walk far over budget) → passages still served, well under 5s
  (b) brain-dead (walk raises)          → passages still served; with NO floor,
                                           an honest error (never a confident
                                           abstention)
  (c) brain-fast                        → BOTH walk facts AND passages served
  (d) walk budget exceeded              → walk contribution shed, passages
                                           complete, NO markers (fault ≠ miss)
  (e) 10 concurrent slow-brain queries  → ALL 10 get their passages (no
                                           threadpool starvation, BUG-02)
  (order) the passage lane runs BEFORE the walk starts — passage-first, not
                                           concurrent
  (R7 regression) a clean miss + hit still escalates and still hands the marker
                                           rows to the fire-and-forget writer
  (env) QUERY_WALK_TIMEOUT_S parsing: default 30s, junk/non-positive → default
"""

import asyncio
import time

import pytest

from src.api.models import QueryRequest, QueryResponse

# Walk sleeps are deliberately ~4x the budget (the 60s-walk relationship at
# test scale) but short enough that the abandoned worker thread is reaped
# inside each test — an anyio worker parked on time.sleep cannot be cancelled,
# only abandoned, and a lingering non-reaped sleep would drag the suite.
_WALK_SLEEP_S = 2.0
_BUDGET_S = "0.5"

_PASSAGE_HITS = [
    {"document_id": 7, "title": "Wind Energy Handbook", "status": "ready",
     "chunk_index": 3, "chunk_text": "Modern turbines achieve 20-40% efficiency.",
     "chunk_count": 8, "rank": 0.09},
]


async def _ready(user_id, endpoint):
    return None


def _slow_walk(request):
    time.sleep(_WALK_SLEEP_S)
    return QueryResponse(anchor="00000000-0000-0000-0000-000000000000",
                         facts=[{"rel": "owns", "value": "the answer"}])


def _patch(monkeypatch, *, walk, lane, markers=None):
    """Stub the wrapper's three attach points on the real module."""
    import src.api.main as M
    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready)
    monkeypatch.setattr(M, "_require_resolvable_user_id", lambda u, e: u)
    monkeypatch.setattr(M, "_document_hits_for_query", lane)
    monkeypatch.setattr(M, "_query_impl", walk)
    if markers is not None:
        monkeypatch.setattr(M, "_record_recall_miss_markers", markers)
    return M


def _hits_lane(hits):
    return lambda uid, text, limit=3: list(hits)


def _raise_walk(request):
    raise RuntimeError("brain dead: NIM connection refused")


def _req(i=0):
    return QueryRequest(text=f"how efficient are wind turbines {i}",
                        user_id=f"00000000-0000-0000-0000-00000000000{i}")


# ── (order) passage-first, not concurrent ─────────────────────────────────────

async def test_passage_lane_fires_before_the_walk(monkeypatch):
    order = []

    def lane(uid, text, limit=3):
        order.append("lane")
        return list(_PASSAGE_HITS)

    def walk(request):
        order.append("walk")
        return QueryResponse(anchor="a", facts=[])

    M = _patch(monkeypatch, walk=walk, lane=lane)
    resp = await M.query(_req())
    assert order == ["lane", "walk"], order
    assert resp.document_hits == _PASSAGE_HITS


# ── (a) brain-slow: passages served in <5s while the walk is still hanging ────

async def test_slow_brain_passages_served_under_5s(monkeypatch):
    monkeypatch.setenv("QUERY_WALK_TIMEOUT_S", _BUDGET_S)
    M = _patch(monkeypatch, walk=_slow_walk, lane=_hits_lane(_PASSAGE_HITS))

    t0 = time.monotonic()
    resp = await M.query(_req())
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0, f"response waited {elapsed:.2f}s for a hanging walk"
    assert isinstance(resp, QueryResponse)
    assert resp.document_hits == _PASSAGE_HITS    # the floor answered
    assert resp.facts == []                       # the walk was shed, not served
    assert resp.error is None                     # hits exist → no error field
    assert resp.alerts and resp.alerts[0]["alert_type"] == "walk_budget_exceeded"
    await asyncio.sleep(_WALK_SLEEP_S)            # reap the abandoned worker


# ── (b) brain-dead: walk raises ────────────────────────────────────────────────

async def test_dead_brain_passages_still_served(monkeypatch):
    M = _patch(monkeypatch, walk=_raise_walk, lane=_hits_lane(_PASSAGE_HITS))
    resp = await M.query(_req())
    assert resp.document_hits == _PASSAGE_HITS
    assert resp.facts == []
    assert resp.error is None                     # MCP must render the passages
    assert resp.alerts and resp.alerts[0]["alert_type"] == "walk_failed"


async def test_dead_brain_with_no_floor_is_an_honest_error(monkeypatch):
    """No floor + dead walk must NOT read as a confident empty ('you have no
    facts'): backend_soft_failure keys on a non-empty error string."""
    M = _patch(monkeypatch, walk=_raise_walk, lane=_hits_lane([]))
    resp = await M.query(_req())
    assert resp.document_hits == []
    assert resp.facts == []
    assert isinstance(resp.error, str) and resp.error.strip()


# ── (c) brain-fast: both surfaces ──────────────────────────────────────────────

async def test_fast_brain_serves_walk_facts_and_passages(monkeypatch):
    def walk(request):
        # A walk that (wrongly) carries its own stale document_hits: the
        # passage-first lane is the source of truth and must win.
        return QueryResponse(anchor="a", facts=[{"rel": "owns"}],
                             document_hits=[], walk_facts_found=1)

    M = _patch(monkeypatch, walk=walk, lane=_hits_lane(_PASSAGE_HITS))
    resp = await M.query(_req())
    assert resp.facts == [{"rel": "owns"}]
    assert resp.document_hits == _PASSAGE_HITS
    assert resp.error is None and resp.escalated is False


# ── (d) budget exceeded: partial response, complete floor, no markers ─────────

async def test_budget_exceeded_serves_complete_floor_and_no_markers(monkeypatch):
    monkeypatch.setenv("QUERY_WALK_TIMEOUT_S", _BUDGET_S)
    seen = []

    M = _patch(monkeypatch, walk=_slow_walk, lane=_hits_lane(_PASSAGE_HITS),
               markers=lambda uid, rows: seen.append(rows))
    resp = await M.query(_req())

    assert resp.facts == []                        # walk shed → partial response
    assert resp.document_hits == _PASSAGE_HITS     # the floor is complete
    assert resp.escalated is False                 # a fault is not a miss
    assert resp.alerts[0]["budget_s"] == 0.5
    await asyncio.sleep(_WALK_SLEEP_S)             # reap before asserting no write
    assert seen == [], "a shed walk must not write recall_miss markers"
    await asyncio.sleep(0.2)


# ── (e) 10 concurrent queries against a slow brain ────────────────────────────

async def test_ten_concurrent_slow_brain_all_get_passages(monkeypatch):
    monkeypatch.setenv("QUERY_WALK_TIMEOUT_S", _BUDGET_S)

    def lane(uid, text, limit=3):
        # Each query gets its OWN hit (document_id mirrors the query ordinal):
        # a dropped/swapped passage result cannot hide behind a shared payload.
        n = int(text.rsplit(" ", 1)[-1])
        return [{"document_id": n, "title": f"doc {n}", "status": "ready",
                 "chunk_index": 0, "chunk_text": f"answer {n}",
                 "chunk_count": 1, "rank": 0.01 * n}]

    M = _patch(monkeypatch, walk=_slow_walk, lane=lane)
    t0 = time.monotonic()
    resps = await asyncio.gather(*(M.query(_req(i)) for i in range(10)))
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0, f"10 concurrent turns took {elapsed:.2f}s"
    for i, resp in enumerate(resps):
        assert isinstance(resp, QueryResponse), f"q{i}: shed at the barrier"
        assert resp.document_hits == lane(None, f"q {i}"), f"q{i} lost its passages"
        assert resp.facts == []
    await asyncio.sleep(_WALK_SLEEP_S)             # reap the 10 abandoned workers


# ── R7 regression: the restructure must not break the marker loop ─────────────

async def test_clean_miss_plus_hit_still_escalates_and_marks(monkeypatch):
    seen = []

    def walk(request):
        return QueryResponse(anchor="a", facts=[], walk_facts_found=0)

    M = _patch(monkeypatch, walk=walk, lane=_hits_lane(_PASSAGE_HITS),
               markers=lambda uid, rows: seen.append((uid, rows)))
    resp = await M.query(_req())

    assert resp.document_hits == _PASSAGE_HITS
    assert resp.escalated is True
    for _ in range(100):                           # settle the detached task
        await asyncio.sleep(0.02)
        if seen:
            break
    assert len(seen) == 1 and seen[0][1] == _PASSAGE_HITS


# ── (f) the lane itself hangs: the response must still land ───────────────────

async def test_hung_passage_lane_cannot_block_the_response(monkeypatch):
    """Round-2 adversarial finding pinned: passage-first made the lane the head of
    the response, so a wedged Postgres must not become the new blocker the walk
    was. Lane budget 0.5s, lane sleeping 2s, walk INSTANT → the response lands
    with the walk's facts, honest empty passages, and a lane-timeout alert."""
    monkeypatch.setenv("DOCUMENT_HITS_LANE_TIMEOUT_S", "0.5")

    def hung_lane(uid, text, limit=3):
        time.sleep(2.0)
        return list(_PASSAGE_HITS)

    def walk(request):
        return QueryResponse(anchor="a", facts=[{"rel": "owns"}])

    M = _patch(monkeypatch, walk=walk, lane=hung_lane)
    t0 = time.monotonic()
    resp = await M.query(_req())
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0, f"response waited {elapsed:.2f}s for a hung lane"
    assert resp.facts == [{"rel": "owns"}]          # the walk's facts still served
    assert resp.document_hits == []                 # honest empty, never a hang
    assert resp.error is None
    assert any(a["alert_type"] == "document_hits_lane_timeout" for a in resp.alerts)
    await asyncio.sleep(1.7)                        # reap the abandoned lane thread


# ── env parsing ────────────────────────────────────────────────────────────────

def test_query_walk_timeout_s_parsing(monkeypatch):
    import src.api.main as M
    monkeypatch.delenv("QUERY_WALK_TIMEOUT_S", raising=False)
    assert M._query_walk_timeout_s() == 30.0      # default
    monkeypatch.setenv("QUERY_WALK_TIMEOUT_S", "2.5")
    assert M._query_walk_timeout_s() == 2.5
    for bad in ("0", "-3", "junk", ""):
        monkeypatch.setenv("QUERY_WALK_TIMEOUT_S", bad)
        assert M._query_walk_timeout_s() == 30.0, bad
    monkeypatch.delenv("DOCUMENT_HITS_LANE_TIMEOUT_S", raising=False)
    assert M._document_hits_timeout_s() == 5.0    # default
    for bad in ("0", "-1", "junk"):
        monkeypatch.setenv("DOCUMENT_HITS_LANE_TIMEOUT_S", bad)
        assert M._document_hits_timeout_s() == 5.0, bad
    monkeypatch.setenv("DOCUMENT_HITS_LANE_TIMEOUT_S", "1.25")
    assert M._document_hits_timeout_s() == 1.25
