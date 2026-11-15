"""Pins: EAGER_LANE_WARM (BUG-R4-3 lane, the residual first-turn cost).

MEASURED on prod (owner seat 3f2a9c41, 2026-11-14): after the REL_TYPE_CATEGORY_BULK_PRIME
fix the steady-state walk is ~2.2s and the door ~2.5s — but the FIRST turn in a fresh
process pays lazy loads that dwarf the walk: spaCy ~4.6s (first ``_get_nlp``), NLTK
WordNet ~4.7-9s (first ``wordnet_ladder._wn``), plus the rel-type category window's first
prime. Fresh-process door: 8.3s; one post-deploy live walk: 5.9s. THE FIX: a daemon
thread at startup warms exactly those resources (api lifespan → ``_eager_warm_lanes``;
mcp http lifespan → ``_mcp_eager_warm_lanes``). Pure load-order change — every resource
keeps its own lock and its lazy caller path; OFF = nothing warmed, byte-parity.

Pins:
  1. the api-side helper warms spaCy (incl. one dummy parse), WordNet, and the
     rel-category prime — and is FAIL-SAFE per resource;
  2. the mcp-side helper warms spaCy + WordNet, fail-safe;
  3. the mcp lifespan schedules the warm with the flag ON and does NOT with OFF.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import src.api.main as M  # noqa: E402
from src.extraction import linguistics as _ling  # noqa: E402
from src.api import wordnet_ladder as _wl  # noqa: E402


class _FakeNlp:
    def __init__(self):
        self.parsed: list[str] = []

    def __call__(self, text):
        self.parsed.append(text)
        return None


def test_api_helper_warms_all_three_lanes(monkeypatch):
    fake_nlp = _FakeNlp()
    calls = {"wn": 0, "prime": 0}
    monkeypatch.setattr(_ling, "_get_nlp", lambda: fake_nlp)
    monkeypatch.setattr(_wl, "_wn", lambda: (calls.__setitem__("wn", calls["wn"] + 1) or object()))
    monkeypatch.setattr(M, "_prime_rel_type_category_window", lambda dsn: calls.__setitem__("prime", calls["prime"] + 1))
    M._eager_warm_lanes("postgresql://fake/warm")
    assert fake_nlp.parsed == ["warmup"], "the warm must parse once to JIT the pipes"
    assert calls["wn"] == 1
    assert calls["prime"] == 1


def test_api_helper_is_failsafe_per_resource(monkeypatch):
    calls = {"wn": 0, "prime": 0}
    def _boom():
        raise RuntimeError("no spaCy bake")
    monkeypatch.setattr(_ling, "_get_nlp", _boom)
    monkeypatch.setattr(_wl, "_wn", lambda: (calls.__setitem__("wn", calls["wn"] + 1) or object()))
    monkeypatch.setattr(M, "_prime_rel_type_category_window", lambda dsn: calls.__setitem__("prime", calls["prime"] + 1))
    M._eager_warm_lanes("postgresql://fake/warm")  # must not raise
    assert calls["wn"] == 1 and calls["prime"] == 1, "one dead lane must not kill the others"


def test_mcp_helper_warms_and_is_failsafe(monkeypatch):
    from src.mcp import http_server as _hs
    fake_nlp = _FakeNlp()
    monkeypatch.setattr(_ling, "_get_nlp", lambda: fake_nlp)
    monkeypatch.setattr(_wl, "_wn", _boom := (lambda: (_ for _ in ()).throw(RuntimeError("no corpus"))))
    _hs._mcp_eager_warm_lanes()  # wordnet dead → must not raise
    assert fake_nlp.parsed == ["warmup"]


@pytest.mark.anyio
def test_mcp_lifespan_schedules_warm_on_and_not_off(monkeypatch, anyio_backend):
    import asyncio
    from src.mcp import http_server as _hs
    from src.mcp import server as _mcp

    ran = threading.Event()
    orig = _hs._mcp_eager_warm_lanes
    monkeypatch.setattr(_ling, "_get_nlp", lambda: None)  # cheap fakes for the real run
    monkeypatch.setattr(_wl, "_wn", lambda: None)

    def fake_warm():
        ran.set()
    monkeypatch.setattr(_hs, "_mcp_eager_warm_lanes", fake_warm)

    async def _spool_recover_noop():
        return None
    monkeypatch.setattr(_mcp, "episodic_spool_recover", _spool_recover_noop)

    async def _drive(flag: str):
        os.environ["EAGER_LANE_WARM"] = flag
        async with _hs.lifespan(_hs.app):
            if flag == "0":
                assert not ran.is_set(), "flag OFF must not warm"
            else:
                assert ran.wait(timeout=5.0), "flag ON must run the warm thread"

    # OFF first (proves nothing was warmed), then ON
    asyncio.run(_drive("0"))
    asyncio.run(_drive("1"))
    os.environ.pop("EAGER_LANE_WARM", None)
