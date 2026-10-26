"""MCP RECALL BRAIN-INDEPENDENCE — the fast path serves passages regardless of brain state.

The MCP-layer twin of the API passage-first fix (gauntlet 2026-10-25; FOSS port 2026-10-26).
The API layer already serves document_hits from /query no matter how slow the LLM brain is;
this lane makes the MCP's recall_memory tool stop BLOCKING on the pre-query brain call
(/classify-intent's LLM tiers are the 15s+ prod hang):

  (a) brain-slow (classify over budget)   → recall returns passages <5s, breaker OPENS
  (b) brain-dead (classify refused fast)  → recall still returns passages
  (c) brain-fast                          → FULL pipeline: classify → gate → harvest →
                                            /query, passages AND facts, breaker stays CLOSED
  (d) breaker open                        → ZERO /classify-intent traffic; episodic retention
                                            + /query direct; passages served
  (flag) MCP_BRAIN_DEGRADED=1             → the ops-declared fast path, same shape as (d)
  (reset) a healthy full-route classify   → closes the window (half-open recovery)
  (remember) _classify_and_gate re-asks GLiNER2-only (skip_llm) when the budgeted probe
                                            fails and the lane needs a route; the re-ask
                                            failing too still surfaces the ORIGINAL cause

FOSS-port adaptation: this lineage has no _brain_request/BrainUnavailable budget-table
machinery (private-lineage) — the probe is a plain httpx POST bounded by probe_budget, the
slow-vs-decision split is httpx.TimeoutException/TransportError vs HTTPStatusError, and the
probe is ONE attempt (no in-turn retry on this engine). The pins are the same contract
expressed against those shapes.

All backend traffic is a URL router mock — no DB, no live API. The slow brain is simulated
the way httpx actually reports one: sleep past the call's OWN timeout kwarg, then ReadTimeout.
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


_UID = "3f2b8c1a-6c4d-4e5f-9a1b-2d3e4f5a6b7c"

_PASSAGE_HITS = [
    {"document_id": 7, "title": "Wind Energy Handbook", "status": "ready",
     "chunk_index": 3, "chunk_text": "Modern turbines achieve 20-40% efficiency.",
     "chunk_count": 8, "rank": 0.09},
]

_QUERY_OK = {
    # `definition` is the prose surface the MCP renderer emits (fact_class A +
    # user_stated provenance → the ASSERT band)
    "facts": [{"definition": "You own a Corolla", "fact_class": "A",
               "fact_provenance": "user_stated", "confidence": 0.95}],
    "attributes": {},
    "document_hits": list(_PASSAGE_HITS),
}


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=payload)
    if status >= 400:
        # faithful to the real client: raise_for_status() raises AFTER the response object
        # returns (a 4xx is a backend DECISION, not a transport error)
        r.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(str(status), request=MagicMock(), response=r))
    else:
        r.raise_for_status = MagicMock()
    return r


class _Router:
    """AsyncMock-shaped backend router. `slow_classify` makes EVERY budgeted
    /classify-intent POST sleep past its own timeout kwarg then raise ReadTimeout (what a
    rate-limited brain that accepts-and-crawls looks like from the client); `dead` raises
    ConnectError instantly. `skip_llm_classify` answers the deterministic re-ask."""

    def __init__(self, *, slow_classify=False, dead_classify=False, skip_llm_classify=None):
        self.posts = []   # (url, json_body)
        self.gets = []
        self.slow_classify = slow_classify
        self.dead_classify = dead_classify
        self.skip_llm_classify = skip_llm_classify
        self.query_payload = dict(_QUERY_OK)

    async def post(self, url, **kwargs):
        body = kwargs.get("json") or {}
        self.posts.append((url, dict(body)))
        if "/classify-intent" in url:
            if body.get("skip_llm") is True:
                if self.skip_llm_classify is not None:
                    return _resp(self.skip_llm_classify)
                return _resp({"intent": "QUERY", "confidence": 0.9})
            if self.dead_classify:
                raise httpx.ConnectError("brain dead: connection refused")
            if self.slow_classify:
                budget = float(kwargs.get("timeout") or 3.0)
                await asyncio.sleep(budget + 0.4)
                raise httpx.ReadTimeout(f"simulated slow brain (> {budget}s)")
            return _resp({"intent": "QUERY", "confidence": 0.9})
        if "/harvest-spans" in url:
            if self.dead_classify:
                raise httpx.ConnectError("harvest down with the brain")
            return _resp({"edges": []})
        if "/episodic/append" in url:
            return _resp({"status": "stored", "id": 11})
        if "/query" in url:
            return _resp(self.query_payload)
        return _resp({})

    async def get(self, url, **kwargs):
        self.gets.append(url)
        if "/confidence-gate" in url:
            return _resp({"threshold": 0.70})
        return _resp({})

    def classify_calls(self):
        return [b for u, b in self.posts if "/classify-intent" in u]

    def post_urls(self):
        return [u for u, _ in self.posts]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Module-state hygiene: the brain-slow breaker is cross-turn by design, so every test
    starts and ends with the window CLOSED; the recall probe knob is pinned tight (0.3s)
    so the slow-brain mock sleeps 0.7s, not 3.4s."""
    S._brain_slow_reset("test isolation")
    monkeypatch.setenv("MCP_CLASSIFY_TIMEOUT_S", "0.3")
    S._FAULTLINE_URL_DETECTED = True
    yield
    S._brain_slow_reset("test isolation")


async def _recall(router):
    with patch.object(S, "_http_client", router):
        return await recall_memory_tool("how efficient are wind turbines", _UID)


# ── (a) brain-slow: budgeted classify exceeded → passages <5s, breaker opens ──────────


async def test_slow_brain_recall_serves_passages_under_5s_and_opens_the_breaker():
    router = _Router(slow_classify=True)
    t0 = time.monotonic()
    result = await _recall(router)
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0, f"recall waited {elapsed:.2f}s on a slow-brain classify"
    assert "Wind Energy Handbook" in result["memory"], result["memory"][:200]
    assert "Modern turbines achieve 20-40% efficiency." in result["memory"]
    # the budget bound the probe to ONE attempt and NO skip_llm re-ask — recall's
    # degradation IS the fast path, not a second classify spend
    assert len(router.classify_calls()) == 1, router.classify_calls()
    # TRIP-TURN RETENTION (round-2 review finding 1): the probe timed out MID-TURN, so the
    # retention decision below must key on the breaker STATE (marked on the way out), not
    # the entry-time flag — the trip turn takes the fast-path retention too, never the
    # full /harvest-spans await that could re-hang it.
    assert not any("/harvest-spans" in u for u in router.post_urls()), router.post_urls()
    assert any("/episodic/append" in u for u in router.post_urls())
    # the /query walk still fired with the RAW text
    assert any("/query" in u for u in router.post_urls())
    query_body = next(b for u, b in router.posts if "/query" in u)
    assert query_body["text"] == "how efficient are wind turbines"
    # the timeout marked this seat's brain slow: the NEXT turn fast-paths
    assert S._brain_slow_active(_UID) is True


# ── (b) brain-dead: classify refused fast → passages still served ────────────────────


async def test_dead_brain_classify_still_serves_passages():
    router = _Router(dead_classify=True)
    result = await _recall(router)

    assert "Modern turbines achieve 20-40% efficiency." in result["memory"]
    assert any("/query" in u for u in router.post_urls())
    assert S._brain_slow_active(_UID) is True  # dead brain also trips the window


# ── (c) brain-fast: the FULL pipeline, breaker stays closed ───────────────────────────


async def test_fast_brain_full_pipeline_and_breaker_stays_closed():
    router = _Router()
    result = await _recall(router)

    # classify → harvest → /query, in order, plus the diagnostic gate GET — and NOTHING else
    assert len(router.post_urls()) == 3, router.post_urls()
    assert "/classify-intent" in router.post_urls()[0]
    assert "/harvest-spans" in router.post_urls()[1]
    assert "/query" in router.post_urls()[2]
    assert any("/confidence-gate" in u for u in router.gets)
    # NO skip_llm on the healthy probe
    assert all(b.get("skip_llm") is not True for b in router.classify_calls())
    # BOTH surfaces render: the fact AND the passage (BUG-09: the passage LEADS)
    assert "Wind Energy Handbook" in result["memory"]
    assert "Corolla" in result["memory"]
    assert result["memory"].index("From your document") < result["memory"].index("Corolla")
    assert S._brain_slow_active(_UID) is False  # a healthy full-route classify closes the window


# ── (d) breaker open: ZERO classify traffic, episodic retention, /query direct ───────


async def test_open_breaker_fires_query_direct_without_classify():
    S._mark_brain_slow(_UID, "pin (d): a prior turn's classify timeout")
    router = _Router(slow_classify=True)   # would hang 0.7s per classify — must never fire
    t0 = time.monotonic()
    result = await _recall(router)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"fast path took {elapsed:.2f}s — it probed something"
    assert router.classify_calls() == [], "the open breaker must skip /classify-intent entirely"
    assert not any("/confidence-gate" in u for u in router.gets), "the gate read is skipped too"
    # deterministic retention replaced the harvest (no /harvest-spans on a degraded turn)
    assert not any("/harvest-spans" in u for u in router.post_urls()), router.post_urls()
    assert any("/episodic/append" in u for u in router.post_urls())
    # the durable row carries NO transport-fabricated verdict: the brain classified
    # nothing this turn
    append_body = next(b for u, b in router.posts if "/episodic/append" in u)
    assert append_body["intent"] is None
    assert any("/query" in u for u in router.post_urls())
    assert "Modern turbines achieve 20-40% efficiency." in result["memory"]


# ── explicit ops-declared degradation: MCP_BRAIN_DEGRADED=1 ──────────────────────────


async def test_env_degraded_flag_takes_the_fast_path_without_measuring(monkeypatch):
    monkeypatch.setenv("MCP_BRAIN_DEGRADED", "1")
    router = _Router()
    result = await _recall(router)

    assert router.classify_calls() == []
    assert any("/query" in u for u in router.post_urls())
    assert "Modern turbines achieve 20-40% efficiency." in result["memory"]
    assert "MCP_BRAIN_DEGRADED" in S._brain_slow_reason(_UID)


# ── half-open recovery: a healthy full-route classify closes the window ──────────────


async def test_healthy_classify_closes_an_open_window():
    """Pins the POP semantics: a healthy full-route classify clears its OWN seat's window.
    (Who can trigger it on a live seat: any WRITE-lane turn — remember/retract classify
    every turn; a recall-only seat recovers by cooldown expiry instead, per the breaker
    block's honest recovery-paths note — round-2 review finding 4.)"""
    S._mark_brain_slow(_UID, "recovery pin")
    assert S._brain_slow_active(_UID) is True
    router = _Router()  # healthy classify answers fast
    with patch.object(S, "_http_client", router):
        intent, conf, gate = await S._classify_and_gate("I drive a Corolla", _UID)
    assert intent == "QUERY" and conf == 0.9
    assert any("/confidence-gate" in u for u in router.gets)  # gate read ran (full pipeline)
    assert S._brain_slow_active(_UID) is False  # the window closed


# ── the remember-lane GLiNER2-only re-ask (sub-fix A) ────────────────────────────────


async def test_classify_budget_exceeded_reasks_gliner2_only_for_route_lanes():
    router = _Router(slow_classify=True, skip_llm_classify={"intent": "STATEMENT", "confidence": 0.55})
    with patch.object(S, "_http_client", router):
        intent, conf, gate = await S._classify_and_gate(
            "my dog is called Rufus", _UID, gliner2_fallback=True, probe_budget=0.3)

    assert intent == "STATEMENT" and conf == 0.55
    calls = router.classify_calls()
    assert len(calls) == 2, calls           # 1 budgeted probe attempt, then ONE skip_llm re-ask
    assert all(b.get("skip_llm") is not True for b in calls[:1])
    assert calls[1].get("skip_llm") is True
    assert router.gets == []                # the diagnostic gate read is skipped on this arm
    assert S._brain_slow_active(_UID) is True   # the probe timeout still marked the window


async def test_gliner2_reask_failing_too_surfaces_the_original_cause():
    # skip_llm arm also over budget (a backend that hangs everything) → the ORIGINAL
    # ReadTimeout is the surfaced failure, unchanged.
    class _AllSlow(_Router):
        """Even the skip_llm re-ask hangs past its budget."""

        async def post(self, url, **kwargs):
            if "/classify-intent" in url:
                body = kwargs.get("json") or {}
                self.posts.append((url, dict(body)))
                budget = float(kwargs.get("timeout") or 3.0)
                await asyncio.sleep(budget + 0.4)
                raise httpx.ReadTimeout("everything is slow")
            return await super().post(url, **kwargs)

    all_slow = _AllSlow()
    with patch.object(S, "_http_client", all_slow), \
         pytest.raises(httpx.ReadTimeout):
        await S._classify_and_gate("my dog is called Rufus", _UID,
                                   gliner2_fallback=True, probe_budget=0.3)
    assert len(all_slow.classify_calls()) == 2  # 1 probe attempt + 1 re-ask


# ── env parsing pins ─────────────────────────────────────────────────────────────────


def test_classify_budget_knobs_parse_with_defaults(monkeypatch):
    monkeypatch.delenv("MCP_CLASSIFY_TIMEOUT_S", raising=False)
    assert S._classify_probe_budget_s() == 3.0
    monkeypatch.setenv("MCP_CLASSIFY_TIMEOUT_S", "1.5")
    assert S._classify_probe_budget_s() == 1.5
    monkeypatch.setenv("MCP_CLASSIFY_TIMEOUT_S", "junk")
    assert S._classify_probe_budget_s() == 3.0  # junk → default, not a crash
    monkeypatch.delenv("MCP_BRAIN_SLOW_COOLDOWN_S", raising=False)
    assert S._brain_slow_cooldown_s() == 60.0


# ── (e) end-to-end through the MCP HTTP transport — the door a client actually uses ──


def test_rest_door_e2e_slow_brain_serves_passages_under_5s(monkeypatch):
    """The REST /recall_memory door (the OpenWebUI/live-client lane), through the real
    FastAPI transport, against a mocked slow brain: the budgeted classify probe times
    out, the turn survives, and the passage floor is served well under the client-timeout
    danger zone."""
    from fastapi.testclient import TestClient
    from src.mcp import http_server as h

    monkeypatch.setattr(S, "_ensure_provisioned", _async_true)
    monkeypatch.setattr(h, "MCP_API_KEY", "")
    with TestClient(h.app) as client:
        router = _Router(slow_classify=True)
        with patch.object(S, "_http_client", router):
            t0 = time.monotonic()
            resp = client.post("/recall_memory",
                               json={"query": "how efficient are wind turbines",
                                     "user_id": _UID})
            elapsed = time.monotonic() - t0

        assert resp.status_code == 200, resp.text[:300]
        body = resp.json()
        memory = body.get("memory") or body.get("content", [{}])[0].get("text", "")
        assert "Modern turbines achieve 20-40% efficiency." in memory, body
        assert elapsed < 5.0, f"the door answered in {elapsed:.2f}s on a slow brain"
        assert S._brain_slow_active(_UID) is True   # the next turn through this door fast-paths


async def _async_true(user_id):
    return True


# ── per-seat isolation (round-2 review finding 3): one tenant's slow brain must not
#    fast-path another tenant's healthy turns — deployments run per-account BYO brains.


async def test_a_marked_seat_does_not_fast_path_another_seat():
    other = "9a8b7c6d-5e4f-4031-9234-56789abcdef0"
    S._mark_brain_slow(other, "the OTHER seat's brain is rate-limited")

    router = _Router()   # this seat's brain is healthy
    result = await _recall(router)

    assert len(router.classify_calls()) == 1   # THIS seat still pays the full pipeline
    assert any("/confidence-gate" in u for u in router.gets)
    assert "Modern turbines achieve 20-40% efficiency." in result["memory"]
    assert S._brain_slow_active(_UID) is False      # this seat's window untouched
    assert S._brain_slow_active(other) is True      # the marked seat keeps its window


# ── RECALL_INTENT_ROUTING=off + open window (round-2 review finding 2): the retention
#    replacement must still fire — routing-off must not resurrect the full harvest.


async def test_routing_off_with_open_window_still_takes_fast_path_retention():
    S._mark_brain_slow(_UID, "pin: window open while intent routing is disabled")
    router = _Router()
    with patch.object(S, "RECALL_INTENT_ROUTING", False), \
         patch.object(S, "_http_client", router):
        result = await recall_memory_tool("how efficient are wind turbines", _UID)

    assert router.classify_calls() == []                    # routing off: no classify anyway
    assert not any("/harvest-spans" in u for u in router.post_urls()), router.post_urls()
    assert any("/episodic/append" in u for u in router.post_urls())
    assert any("/query" in u for u in router.post_urls())
    assert "Modern turbines achieve 20-40% efficiency." in result["memory"]


# ── a deterministic 4xx classify DECISION is not a slow brain (round-2 finding 5) ────


async def test_classify_4xx_decision_takes_the_full_pipeline_not_the_fast_path():
    router = _Router()

    async def _post_400(url, **kwargs):
        body = kwargs.get("json") or {}
        if "/classify-intent" in url and body.get("skip_llm") is not True:
            router.posts.append((url, dict(body)))
            # a 400 RESPONSE OBJECT whose raise_for_status raises — the faithful shape (a
            # 4xx is a backend decision the transport surfaces as-is, never retries)
            return _resp({"detail": "bad request"}, status=400)
        return await _Router.post(router, url, **kwargs)

    router.post = _post_400
    result = await _recall(router)

    assert "Modern turbines achieve 20-40% efficiency." in result["memory"]
    # a backend DECISION must not open the breaker...
    assert S._brain_slow_active(_UID) is False
    # ...nor fast-path the retention: the full harvest fired (the backend is deciding,
    # not crawling)
    assert any("/harvest-spans" in u for u in router.post_urls()), router.post_urls()
    assert not any("/episodic/append" in u for u in router.post_urls())
