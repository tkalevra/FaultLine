"""BUG-04 (adversarial bench 20261024) — papers drain wedged, zero facts.

THE WEDGE (measured on prod 2026-10-25, owner seat, docs 10-20): the drain is
ALIVE but every chunk fails — a rate_deferred harvest envelope or a ReadTimeout
against the paced NIM brain (957 ReadTimeouts / 6h; 19 rate_deferred
fast-fails; 41 harvest degrade-WITHHOLDS discarding edges_withheld=11-14
ALREADY-DERIVED edges per chunk). The backend's degrade-withhold is per-TURN,
not per-EDGE, so it discards the deterministic spine's own edges whenever ONE
LLM op (REFRAME / atomic_subject_resolution) is paced — and the re-pend loop
never exits: 11 papers, 5 days, 0 facts.

THE FIX (owner ruling R9 applied to the CHUNK drain — "the spine ALWAYS runs,
the brain enriches on top"): ``_document_chunk_edges`` rescues deterministically
(IN-PROCESS spine + GLiNER2, zero brain) at the two brain-paced arms before
keeping today's verdict.

Pins:

  (R1) rate_deferred envelope + deterministic-extractable content -> the
       deterministic edges are RETURNED (committed by the caller via /ingest)
       and the /extract/rewrite fallback is NEVER reached (a paced brain gets
       no second ladder — BUG-005's fast-fail stands).
  (R2) rate_deferred + ZERO deterministic edges on real content -> today's
       DocumentBrainUnavailable fast-fail, unchanged (a chunk the extractors
       did not read is never counted empty).
  (R3) rate_deferred + deterministic layer DOWN (None) -> fast-fail unchanged.
  (R4) BOTH extract doors dead at the transport level (ReadTimeout ×2 — the
       brain completely unavailable) + deterministic-extractable content ->
       end-to-end through ``_process_document_chunk``: the chunk's edges are
       COMMITTED through /ingest, the chunk reports failed=0.
  (R5) DOC_CHUNK_DETERMINISTIC_RESCUE=false -> byte-for-byte the legacy
       verdicts (rollback lever works).

The rate_deferred re-pend/lease behaviour around these raises is pinned
unchanged in tests/test_doc_drain_resilience.py (that suite drives the raises
themselves; this one drives the rescue that must fire first).
"""

import json

import pytest

from src.re_embedder import embedder

pytestmark = pytest.mark.usefixtures("requires_postgres")

CHUNK = ("Marcus owns two vintage synthesizers. "
         "His sister Lena works at the hospital in Elmstead.")

_DET_EDGES = [{"subject": "marcus", "rel_type": "works_for",
               "object": "hospital", "confidence": 0.8,
               "fact_provenance": "user_stated"}]

_USER = "11111111-2222-4333-8444-555555555555"
_BACKEND = "http://backend.test"


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"status": "success"}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise embedder.httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=None)


_RATE_DEFERRED_ENVELOPE = {
    "edges": [], "status": "degraded", "extraction_degraded": True,
    "edges_withheld": 12, "llm_rate_deferred": True,
    "llm_first_unanswered_reason": "REFRAME:rate_deferred",
}


@pytest.fixture()
def wire(monkeypatch):
    """Route every httpx.post by URL; records the calls."""
    calls = []
    _routes = {}

    def _fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        action = _routes.get(url, ("ok", None))
        kind, payload = action
        if kind == "raise":
            raise payload
        if kind == "resp":
            return _FakeResp(payload=payload) if isinstance(payload, dict) \
                else _FakeResp(status_code=payload)
        return _FakeResp(payload={"status": "success", "committed": 2,
                                  "staged": 1})

    monkeypatch.setattr(embedder.httpx, "post", _fake_post)

    def _route(url, kind, payload=None):
        _routes[url] = (kind, payload)

    return {"calls": calls, "route": _route,
            "urls": lambda: [c["url"] for c in calls]}


def _harvest_url(): return f"{_BACKEND}/harvest-spans"
def _rewrite_url(): return f"{_BACKEND}/extract/rewrite"
def _ingest_url(): return f"{_BACKEND}/ingest"


# ── pin R1: paced brain + deterministic content -> edges, no rewrite ladder ───

def test_rate_deferred_returns_deterministic_edges_without_rewrite(wire, monkeypatch):
    wire["route"](_harvest_url(), "resp", _RATE_DEFERRED_ENVELOPE)
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: list(_DET_EDGES))

    edges = embedder._document_chunk_edges(CHUNK, _USER, _BACKEND, "rewrite")

    assert edges == _DET_EDGES
    # the paced brain got NO second ladder: harvest is the only brain call
    assert wire["urls"]() == [_harvest_url()]


def test_rescue_logs_the_cause(wire, monkeypatch, caplog):
    wire["route"](_harvest_url(), "resp", _RATE_DEFERRED_ENVELOPE)
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: list(_DET_EDGES))
    with caplog.at_level("INFO", logger="src.re_embedder.embedder"):
        embedder._document_chunk_edges(CHUNK, _USER, _BACKEND, "rewrite")
    assert any("document_chunk_deterministic_rescue" in r.message
               and "rate_deferred" in r.message for r in caplog.records)


# ── pin R2/R3: zero edges / layer down -> today's fast-fail ───────────────────

def test_rate_deferred_zero_edges_keeps_the_fast_fail(wire, monkeypatch):
    wire["route"](_harvest_url(), "resp", _RATE_DEFERRED_ENVELOPE)
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", lambda chunk: [])
    with pytest.raises(embedder.DocumentBrainUnavailable, match="rate_deferred"):
        embedder._document_chunk_edges(CHUNK, _USER, _BACKEND, "rewrite")
    assert _rewrite_url() not in wire["urls"]()


def test_rate_deferred_layer_down_keeps_the_fast_fail(wire, monkeypatch):
    wire["route"](_harvest_url(), "resp", _RATE_DEFERRED_ENVELOPE)
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", lambda chunk: None)
    with pytest.raises(embedder.DocumentBrainUnavailable, match="rate_deferred"):
        embedder._document_chunk_edges(CHUNK, _USER, _BACKEND, "rewrite")


# ── pin R4: brain COMPLETELY unavailable -> edges still committed ─────────────

def test_brain_completely_unavailable_chunk_still_commits(wire, monkeypatch):
    """THE BUG-04 PIN: both extract doors die at the transport level
    (ReadTimeout ×2 — read-level, so the backend itself is alive) and the
    chunk's deterministic-extractable edges are STILL committed through the
    normal /ingest door. The drain wedge becomes a progress path."""
    wire["route"](_harvest_url(), "raise",
                  embedder.httpx.ReadTimeout("brain too slow"))
    wire["route"](_rewrite_url(), "raise",
                  embedder.httpx.ReadTimeout("brain too slow"))
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: list(_DET_EDGES))

    committed, staged, failed, reason = embedder._process_document_chunk(
        CHUNK, idx=0, doc_id=1, user_id=_USER, backend_url=_BACKEND,
        source_ref="paper.pdf", statement_route="rewrite")

    assert failed == 0 and reason is None
    assert committed == 2 and staged == 1
    urls = wire["urls"]()
    assert urls == [f"{_BACKEND}/episodic/append", _harvest_url(),
                    _rewrite_url(), _ingest_url()]
    ingest = wire["calls"][-1]
    assert ingest["kwargs"]["json"]["edges"] == _DET_EDGES
    assert ingest["kwargs"]["json"]["source"] == "document"


def test_brain_unavailable_zero_edges_still_fails_the_chunk(wire, monkeypatch):
    """A chunk with NOTHING deterministic-extractable is NOT rescued: the
    transport failure stands (DOCLOSS-B — never count an unread chunk as
    empty)."""
    wire["route"](_harvest_url(), "raise",
                  embedder.httpx.ReadTimeout("brain too slow"))
    wire["route"](_rewrite_url(), "raise",
                  embedder.httpx.ReadTimeout("brain too slow"))
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", lambda chunk: [])

    committed, staged, failed, reason = embedder._process_document_chunk(
        CHUNK, idx=0, doc_id=1, user_id=_USER, backend_url=_BACKEND,
        source_ref="paper.pdf", statement_route="rewrite")

    assert failed == 1 and reason
    assert committed == 0 and staged == 0
    assert _ingest_url() not in wire["urls"]()


# ── pin R5: the rollback lever ─────────────────────────────────────────────────

def test_flag_off_restores_legacy_rate_deferred_verdict(wire, monkeypatch):
    monkeypatch.setattr(embedder, "DOC_CHUNK_DETERMINISTIC_RESCUE", False)
    wire["route"](_harvest_url(), "resp", _RATE_DEFERRED_ENVELOPE)
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: list(_DET_EDGES))  # would rescue if live
    with pytest.raises(embedder.DocumentBrainUnavailable, match="rate_deferred"):
        embedder._document_chunk_edges(CHUNK, _USER, _BACKEND, "rewrite")


def test_flag_off_restores_legacy_transport_verdict(wire, monkeypatch):
    monkeypatch.setattr(embedder, "DOC_CHUNK_DETERMINISTIC_RESCUE", False)
    wire["route"](_harvest_url(), "raise",
                  embedder.httpx.ReadTimeout("brain too slow"))
    wire["route"](_rewrite_url(), "raise",
                  embedder.httpx.ReadTimeout("brain too slow"))
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: list(_DET_EDGES))
    with pytest.raises(embedder.DocumentChunkExtractionFailed):
        embedder._document_chunk_edges(CHUNK, _USER, _BACKEND, "rewrite")


# ── the rescue uses the R9 in-process seam (never the brain-bearing doors) ─────

def test_rescue_never_uses_the_extract_doors_for_extraction(wire, monkeypatch):
    """The rescued edges come from the IN-PROCESS deterministic seam — the only
    wire traffic on a rescued chunk is the harvest probe, the (dead) rewrite
    fallback, and the /ingest commit. Tripwire: if the rescue ever routes
    through _lane_has_no_content-suppressed or brain-bearing paths, this
    shape pin fails."""
    wire["route"](_harvest_url(), "resp", _RATE_DEFERRED_ENVELOPE)
    seen_chunks = []
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: seen_chunks.append(chunk) or list(_DET_EDGES))
    embedder._document_chunk_edges(CHUNK, _USER, _BACKEND, "rewrite")
    assert seen_chunks == [CHUNK]      # the seam saw the exact chunk text
