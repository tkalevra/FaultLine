"""L1 DETERMINISTIC-FIRST + latency-aware skip (litmus fix L2, ticket .../06).

When the tenant brain is measured SLOW (rolling p95 over DOC_L1_BRAIN_P95_SKIP_MS,
or its saved profile is local/slow), a document chunk's edges are derived by the
IN-PROCESS deterministic lane and committed NOW; the LLM's richer read is DEFERRED
to the R2 remine lane instead of spending the 40-RPM budget on a 28.5s-p95 brain.
The skip replaces a SLOW read, never a CAPTURE: a deterministic zero/None keeps the
brain ladder. Rollback: DOC_L1_DETERMINISTIC_FIRST=false.

House style: DB-less, monkeypatched.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import src.re_embedder.embedder as emb  # noqa: E402
from saas import brain_autocalibrate as ba  # noqa: E402


# R11 hermeticity (gauntlet): DOC_CHUNK_SPINE_FIRST is read call-time INSIDE
# _document_chunk_edges (the seam these tests drive) and DOC_L1_BRAIN_P95_SKIP_MS
# is an IMPORT-TIME bind — ambient env wins before any delenv, so the threshold
# is restored by setattr (all critic-measured to redden 5 pins here).
@pytest.fixture(autouse=True)
def _pin_l1_env_defaults(monkeypatch):
    monkeypatch.delenv("DOC_CHUNK_SPINE_FIRST", raising=False)
    monkeypatch.delenv("DOC_L1_BRAIN_P95_SKIP_MS", raising=False)
    monkeypatch.setattr(emb, "_DOC_L1_BRAIN_P95_SKIP_MS", 8000)


# ---------------------------------------------------------------------------
# _brain_lane_slow: the two independent slow signals
# ---------------------------------------------------------------------------
class _NoDB:
    def cursor(self):
        raise RuntimeError("no db")


def test_profile_slow_signal(monkeypatch):
    monkeypatch.setattr(emb, "_DOC_L1_BRAIN_P95_SKIP_MS", 8000)
    import saas.llm_context as lc
    monkeypatch.setattr(lc, "current_llm_config",
                        lambda: {"latency_profile": "slow"})
    why = emb._brain_lane_slow("u1")
    assert why == "profile:slow"


def test_profile_local_signal(monkeypatch):
    import saas.llm_context as lc
    monkeypatch.setattr(lc, "current_llm_config",
                        lambda: {"latency_profile": "local"})
    assert emb._brain_lane_slow("u1") == "profile:local"


def test_no_signal_returns_none(monkeypatch):
    import saas.llm_context as lc
    monkeypatch.setattr(lc, "current_llm_config", lambda: {})
    monkeypatch.setattr(ba, "rolling_p95_for", lambda uid: None)
    assert emb._brain_lane_slow("u1") is None


def test_p95_over_threshold_signals(monkeypatch):
    import saas.llm_context as lc
    monkeypatch.setattr(lc, "current_llm_config", lambda: {})
    monkeypatch.setattr(ba, "rolling_p95_for", lambda uid: 28500)  # the prod wound
    why = emb._brain_lane_slow("u1")
    assert why and why.startswith("p95:28500")


def test_p95_under_threshold_is_no_signal(monkeypatch):
    import saas.llm_context as lc
    monkeypatch.setattr(lc, "current_llm_config", lambda: {})
    monkeypatch.setattr(ba, "rolling_p95_for", lambda uid: 1260)  # the nemotron candidate
    assert emb._brain_lane_slow("u1") is None


def test_rolling_p95_for_is_read_only_and_underobserved_brain_is_none(monkeypatch):
    ba.reset_for_tests()
    cfg = {"brain_scope": "account", "account_id": "a1", "seat_user_id": "u1",
           "base_url": "https://api.example.com", "model": "m"}
    # below _MIN_SAMPLES: no verdict
    assert ba.rolling_p95_for("u1", cfg=cfg) is None
    for ms in (1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900,
               2000, 2100, 2200, 2300, 2400, 2500, 2600, 2700, 2800, 29000):
        ba.observe("u1", "REFRAME", "ok" if ms != 29000 else "timeout",
                   elapsed_s=ms / 1000.0, budget_s=30.0, cfg=cfg)
    p95 = ba.rolling_p95_for("u1", cfg=cfg)
    assert p95 is not None and p95 >= 2000  # a timeout lands at its budget cutoff


# ---------------------------------------------------------------------------
# The seam: slow brain -> deterministic-first; zero keeps the ladder
# ---------------------------------------------------------------------------
class _Resp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"status": "success", "edges": []}


def test_slow_brain_takes_deterministic_edges(monkeypatch):
    monkeypatch.setattr(emb, "DOC_L1_DETERMINISTIC_FIRST", True)
    monkeypatch.setattr(emb, "_brain_lane_slow", lambda uid: "p95:28500ms")
    monkeypatch.setattr(emb, "_deterministic_chunk_edges",
                        lambda chunk: [{"subject_id": "s", "rel_type": "r",
                                        "object_id": "o", "confidence": 0.8}])
    urls = []
    monkeypatch.setattr(emb.httpx, "post",
                        lambda url, *a, **k: urls.append(str(url)) or _Resp())
    got = emb._document_chunk_edges("Diane lives in Toronto.", "u" * 8,
                                    "http://backend", "rewrite")
    assert got and got[0]["rel_type"] == "r"
    assert not urls, "a slow brain must NOT be called when deterministic edges exist"


def test_deterministic_zero_keeps_the_ladder(monkeypatch):
    monkeypatch.setattr(emb, "DOC_L1_DETERMINISTIC_FIRST", True)
    monkeypatch.setattr(emb, "_brain_lane_slow", lambda uid: "p95:28500ms")
    monkeypatch.setattr(emb, "_deterministic_chunk_edges", lambda chunk: [])
    urls = []
    monkeypatch.setattr(emb.httpx, "post",
                        lambda url, *a, **k: urls.append(str(url)) or _Resp())
    emb._document_chunk_edges("The router forwards packets.", "u" * 8,
                              "http://backend", "rewrite")
    assert any("/harvest-spans" in u for u in urls), (
        "a deterministic ZERO must fall through to the brain ladder")


def test_deterministic_layer_down_keeps_the_ladder(monkeypatch):
    monkeypatch.setattr(emb, "DOC_L1_DETERMINISTIC_FIRST", True)
    monkeypatch.setattr(emb, "_brain_lane_slow", lambda uid: "profile:slow")
    monkeypatch.setattr(emb, "_deterministic_chunk_edges", lambda chunk: None)
    urls = []
    monkeypatch.setattr(emb.httpx, "post",
                        lambda url, *a, **k: urls.append(str(url)) or _Resp())
    emb._document_chunk_edges("The router forwards packets.", "u" * 8,
                              "http://backend", "rewrite")
    assert any("/harvest-spans" in u for u in urls)


def test_flag_off_is_byte_legacy(monkeypatch):
    monkeypatch.setattr(emb, "DOC_L1_DETERMINISTIC_FIRST", False)
    probed = []
    monkeypatch.setattr(emb, "_brain_lane_slow", lambda uid: probed.append(uid) or "profile:slow")
    urls = []
    monkeypatch.setattr(emb.httpx, "post",
                        lambda url, *a, **k: urls.append(str(url)) or _Resp())
    emb._document_chunk_edges("The router forwards packets.", "u" * 8,
                              "http://backend", "rewrite")
    assert not probed, "flag OFF: the latency probe must not even run"
    assert any("/harvest-spans" in u for u in urls)


def test_fast_brain_keeps_the_ladder(monkeypatch):
    monkeypatch.setattr(emb, "DOC_L1_DETERMINISTIC_FIRST", True)
    monkeypatch.setattr(emb, "_brain_lane_slow", lambda uid: None)  # fast brain
    urls = []
    monkeypatch.setattr(emb.httpx, "post",
                        lambda url, *a, **k: urls.append(str(url)) or _Resp())
    emb._document_chunk_edges("The router forwards packets.", "u" * 8,
                              "http://backend", "rewrite")
    assert any("/harvest-spans" in u for u in urls), (
        "a fast brain keeps today's brain-first ladder")
