"""CHEAP-FLOOR-FIRST recall (litmus fix L6, ticket DEV/BUGS/prod-document-drain-20261103/06).

The MCP recall lane used to bank passage evidence ONLY inside the /query walk — when
the budget envelope fired or the walk died, the response was a BARE degraded note
while the deterministic floor (stub_tsv passages, a single SELECT the API serves in
~ms) sat unasked. Fix: a bounded floor-only /query probe (floor_only=True — the API
serves document_hits and SKIPS the walk entirely) fires at t0 and every degraded arm
renders REAL passage lines plus the honest degraded note.

House style: monkeypatched httpx, no DB.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import src.mcp.server as S  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


_HIT = {"document_id": 10, "chunk_index": 2, "title": "networking guide",
        "chunk_text": "The WAF sits in front of the router.",
        "status": "ready"}


# ---------------------------------------------------------------------------
# the pure envelope builder
# ---------------------------------------------------------------------------
def test_envelope_carries_passages_and_stays_degraded():
    env = S._floor_degraded_envelope(
        [_HIT],
        "⚠ I could not complete the memory lookup within the time budget.")
    assert env["isError"] is True
    assert env["status"] == "degraded"
    assert "From your document networking guide" in env["memory"]
    assert "WAF" in env["memory"]
    assert "degraded" in env["memory"].lower() or "⚠" in env["memory"]


def test_envelope_without_hits_is_the_bare_note():
    base = "⚠ bare degraded note."
    env = S._floor_degraded_envelope([], base)
    assert env["memory"] == base  # no fabricated passages


def test_render_is_never_broken_by_malformed_hits():
    env = S._floor_degraded_envelope(["not-a-dict", None, _HIT], "⚠ note.")
    assert "WAF" in env["memory"]  # the good hit still renders


# ---------------------------------------------------------------------------
# the probe: floor_only=True, bounded, soft-fails to []
# ---------------------------------------------------------------------------
def test_floor_probe_posts_floor_only_flag(monkeypatch):
    bodies = []

    class _Client:
        async def post(self, url, json=None, timeout=None):
            bodies.append((url, json, timeout))
            return _Resp({"document_hits": [_HIT]})

    monkeypatch.setattr(S, "_client", lambda: _Client())
    hits = asyncio.get_event_loop().run_until_complete(
        S._floor_probe("network layout", "u" * 8)) \
        if False else None
    # (asyncio.run for a clean loop per test)
    hits = asyncio.run(S._floor_probe("network layout", "u" * 8))
    assert hits == [_HIT]
    url, body, timeout = bodies[0]
    assert body["floor_only"] is True
    assert body["text"] == "network layout"
    assert timeout == S._FLOOR_PROBE_TIMEOUT_S


def test_floor_probe_soft_fails_on_non_200(monkeypatch):
    class _Client:
        async def post(self, url, json=None, timeout=None):
            return _Resp({}, status=503)

    monkeypatch.setattr(S, "_client", lambda: _Client())
    assert asyncio.run(S._floor_probe("q", "u")) == []


def test_floor_probe_soft_fails_on_transport_error(monkeypatch):
    class _Client:
        async def post(self, url, json=None, timeout=None):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(S, "_client", lambda: _Client())
    assert asyncio.run(S._floor_probe("q", "u")) == []


def test_floor_probe_soft_fails_on_timeout(monkeypatch):
    # the fake HONORS the caller's timeout the way httpx does — the probe must give up
    # inside its own budget, not ride a hung client for the full wall
    class _Client:
        async def post(self, url, json=None, timeout=None):
            await asyncio.wait_for(asyncio.sleep(30), timeout=timeout)

    monkeypatch.setattr(S, "_client", lambda: _Client())
    import time as _t
    _t0 = _t.monotonic()
    assert asyncio.run(S._floor_probe("q", "u")) == []
    assert _t.monotonic() - _t0 < S._FLOOR_PROBE_TIMEOUT_S + 1.0, (
        "the probe must return within its own bounded budget")


# ---------------------------------------------------------------------------
# the budget envelope joins the probe (bounded extra wait)
# ---------------------------------------------------------------------------
def test_budget_envelope_renders_banked_passages(monkeypatch):
    async def fake_probe(query, user_id):
        return [_HIT]

    monkeypatch.setattr(S, "_floor_probe", fake_probe)
    # Budget of 0.1s: the impl (which sleeps far longer) must be abandoned, then the
    # envelope joins the probe and renders the banked passage.
    async def slow_impl(query, user_id):
        await asyncio.sleep(30)
        return {"memory": "full render", "status": "ok"}

    monkeypatch.setattr(S, "_recall_memory_tool_impl", slow_impl)
    monkeypatch.setattr(S, "_recall_total_budget_s", lambda: 0.1)
    monkeypatch.setattr(S, "_turn_deadline", type("_TD", (), {
        "get": staticmethod(lambda: None),
        "set": staticmethod(lambda t: None),
        "reset": staticmethod(lambda tok=None: None),
    })())
    out = asyncio.run(S._recall_total_budget_run("network layout", "u" * 8))
    assert out["isError"] is True
    assert "WAF" in out["memory"], out["memory"]


def test_budget_envelope_without_probe_hits_is_bare(monkeypatch):
    async def empty_probe(query, user_id):
        return []

    async def slow_impl(query, user_id):
        await asyncio.sleep(30)
        return {"memory": "full render"}

    monkeypatch.setattr(S, "_floor_probe", empty_probe)
    monkeypatch.setattr(S, "_recall_memory_tool_impl", slow_impl)
    monkeypatch.setattr(S, "_recall_total_budget_s", lambda: 0.1)
    monkeypatch.setattr(S, "_turn_deadline", type("_TD", (), {
        "get": staticmethod(lambda: None),
        "set": staticmethod(lambda t: None),
        "reset": staticmethod(lambda tok=None: None),
    })())
    out = asyncio.run(S._recall_total_budget_run("network layout", "u" * 8))
    assert out["isError"] is True
    assert out["memory"].startswith("⚠")
