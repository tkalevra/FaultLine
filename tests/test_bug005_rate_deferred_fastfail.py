"""BUG-005 — RATE-DEFERRED FAST-FAIL (the retry ladder must not eat its own rate budget).

Prod wedge, 2026-10-20 (synthetic-account 7e4b90d2, BYOM NIM brain max_rpm=40): every harvest turn
of the document drain degraded with ``first_reason=REFRAME:rate_deferred`` — the limiter
never put the turn's LLM requests on the wire — yet the chunk path STILL paid the full
timeout ladder: spine (up to its full client timeout), then the /extract/rewrite fallback.
129/133 chunks died that way; the ladder starved the very budget that would have served
the next turn. Evidence: DEV/BUGS/OWUI_test/BUG-005-doc-drain-wedge-reframe-rate-deferred.md

THE CONTRACT PINNED HERE:
  (a) a rate_deferred outcome → the /extract/rewrite fallback is NEVER called and the
      spine is not retried — the chunk path raises the PACED class immediately;
  (b) every NON-rate failure keeps today's exact ladder, byte for byte;
  (c) the typed outcome surfaces to the drain / the MCP inline lane as a reschedule-able
      state (``rate_deferred`` rides the exception so the drain's attempt-refund
      (``_paced``) branch fires; the MCP sync lane queues the chunk and counts
      ``chunks_deferred_retry``).

Network is mocked throughout; deterministic; no live LLM.
"""
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.re_embedder.embedder as emb
import src.mcp.server as server
from src.re_embedder.embedder import DocumentBrainUnavailable, _document_chunk_edges

BIO = (
    "Photosynthesis converts carbon dioxide and water into glucose and oxygen. "
    "The Calvin cycle occurs in the stroma of the chloroplast."
)

PACED_ENVELOPE_TYPED = {
    "edges": [], "spans": 0, "status": "degraded",
    "edges_withheld": 2, "extraction_degraded": True,
    "llm_calls_unanswered": 1,
    "llm_unanswered_operations": {"REFRAME": 1},
    "llm_first_unanswered_reason": "REFRAME:rate_deferred",
    "llm_rate_deferred": True,
}
# The SAME envelope an OLDER backend (without the typed field) still produces — the
# reason string is the only signal, and the fast-fail must honor it.
PACED_ENVELOPE_SUBSTRING = {
    k: v for k, v in PACED_ENVELOPE_TYPED.items() if k != "llm_rate_deferred"}
# A degraded envelope whose cause is NOT pacing (e.g. the brain answered and garbled
# JSON) — today's ladder applies to it, unchanged.
DEGRADED_NOT_PACED = {
    "edges": [], "spans": 0, "status": "degraded",
    "edges_withheld": 2, "extraction_degraded": True,
    "llm_calls_unanswered": 1,
    "llm_unanswered_operations": {"EXTRACT": 1},
    "llm_first_unanswered_reason": "EXTRACT:all_retries_exhausted",
}


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _resp(json_obj, status=200):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.status_code = status
    r.json.return_value = json_obj
    return r


def _recorder(calls, spine_payload):
    """httpx.post double that records URL order and answers the spine once."""
    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        calls.append(url)
        if url.endswith("/harvest-spans"):
            return _StubResponse(spine_payload)
        if url.endswith("/extract/rewrite"):
            return _StubResponse({"status": "success", "edges": [
                {"subject": "photosynthesis", "rel_type": "produces", "object": "glucose"}]})
        raise AssertionError(f"unexpected url {url}")
    return _post


# ── (a) rate_deferred → fast fail, NO rewrite fallback, NO spine retry ────────


@pytest.mark.parametrize("envelope", [PACED_ENVELOPE_TYPED, PACED_ENVELOPE_SUBSTRING],
                         ids=["typed_field", "reason_substring_old_backend"])
def test_rate_deferred_spine_envelope_never_reaches_rewrite(monkeypatch, envelope):
    """THE core pin: one spine call, then the PACED raise. Rewrite is never dialed."""
    calls = []
    monkeypatch.setattr(emb.httpx, "post", _recorder(calls, envelope))
    with pytest.raises(DocumentBrainUnavailable) as ei:
        _document_chunk_edges(BIO, "u1", "http://backend", "rewrite")
    assert calls == ["http://backend/harvest-spans"], \
        f"no fallback and no retry may follow a paced turn, called: {calls}"
    assert "rate_deferred" in str(ei.value)


def test_rate_deferred_rewrite_style_error_field_is_detected(monkeypatch):
    """A paced envelope that signals via ``error`` (the /extract/rewrite shape) is
    detected by the same predicate — no third ladder rung."""
    calls = []
    paced = dict(PACED_ENVELOPE_SUBSTRING)
    paced["llm_first_unanswered_reason"] = None
    paced["error"] = "rate_deferred"
    monkeypatch.setattr(emb.httpx, "post", _recorder(calls, paced))
    with pytest.raises(DocumentBrainUnavailable) as ei:
        _document_chunk_edges(BIO, "u1", "http://backend", "rewrite")
    assert calls == ["http://backend/harvest-spans"]
    assert "rate_deferred" in str(ei.value)


def test_paced_raise_carries_the_reschedule_refund_marker():
    """PIN (c), drain half: the drain's freeze handler refunds the attempt and re-pends
    WITHOUT burning budget iff ``"rate_deferred" in str(exception)`` — the exact
    predicate (``_paced``) must be satisfiable by what the fast-fail raises."""
    calls = []
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(emb.httpx, "post", _recorder(calls, PACED_ENVELOPE_TYPED))
        with pytest.raises(DocumentBrainUnavailable) as ei:
            _document_chunk_edges(BIO, "u1", "http://backend", "rewrite")
    finally:
        monkeypatch.undo()
    assert "rate_deferred" in str(ei.value), (
        "the raised outcome must read as PACED to the drain's refund branch — anything "
        "else re-pends the document while still consuming an attempt")


def test_paced_detector_ignores_non_degraded_payloads():
    """Fail-safe direction: a plain payload is never misread as paced."""
    assert emb._envelope_rate_deferred({"status": "success", "edges": []}) is False
    assert emb._envelope_rate_deferred(None) is False
    assert emb._envelope_rate_deferred("rate_deferred") is False


# ── (b) NON-rate failures keep today's exact ladder ──────────────────────────


def test_degraded_but_not_paced_still_falls_back_to_rewrite(monkeypatch):
    """A degraded harvest with a NON-rate reason: today's ladder, byte for byte."""
    calls = []
    monkeypatch.setattr(emb.httpx, "post", _recorder(calls, DEGRADED_NOT_PACED))
    edges = _document_chunk_edges(BIO, "u1", "http://backend", "rewrite")
    assert calls == ["http://backend/harvest-spans", "http://backend/extract/rewrite"]
    assert [e["subject"] for e in edges] == ["photosynthesis"]


def test_plain_spine_empty_still_falls_back_to_rewrite(monkeypatch):
    calls = []
    monkeypatch.setattr(emb.httpx, "post", _recorder(calls, {"edges": []}))
    _document_chunk_edges(BIO, "u1", "http://backend", "rewrite")
    assert calls == ["http://backend/harvest-spans", "http://backend/extract/rewrite"]


def test_spine_readtimeout_still_falls_back_to_rewrite(monkeypatch):
    """A READ timeout is an uncertain outcome, not a paced one — today's ladder applies
    (the prod wedge's ladder only becomes self-inflicted when the reason is pacing)."""
    calls = []

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        calls.append(url)
        if url.endswith("/harvest-spans"):
            raise emb.httpx.ReadTimeout("timed out")
        if url.endswith("/extract/rewrite"):
            return _StubResponse({"status": "success", "edges": []})
        raise AssertionError(url)

    monkeypatch.setattr(emb.httpx, "post", _post)
    assert _document_chunk_edges(BIO, "u1", "http://backend", "rewrite") == []
    assert calls == ["http://backend/harvest-spans", "http://backend/extract/rewrite"]


def test_rewrite_rung_rate_deferred_still_raises_the_paced_class(monkeypatch):
    """The rewrite rung's own paced raise (pre-existing) is part of the contract: the
    exception must say rate_deferred so the drain refunds the attempt."""
    calls = []

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        calls.append(url)
        if url.endswith("/harvest-spans"):
            return _StubResponse({"edges": []})
        if url.endswith("/extract/rewrite"):
            return _StubResponse({"status": "degraded", "edges": [],
                                  "extraction_degraded": True, "chunks_total": 1,
                                  "chunks_failed": 1, "error": "rate_deferred"})
        raise AssertionError(url)

    monkeypatch.setattr(emb.httpx, "post", _post)
    with pytest.raises(DocumentBrainUnavailable) as ei:
        _document_chunk_edges(BIO, "u1", "http://backend", "rewrite")
    assert calls == ["http://backend/harvest-spans", "http://backend/extract/rewrite"]
    assert "rate_deferred" in str(ei.value)


# ── the backend half: the ledger stamps the typed outcome ────────────────────


def test_rate_deferred_nonanswer_stamps_scope_snapshot_and_peek():
    from src.api.llm_calls import (begin_llm_degradation_scope,
                                   llm_degradation_snapshot,
                                   llm_scope_rate_deferred,
                                   reset_llm_degradation_scope,
                                   _record_llm_nonanswer)
    tok = begin_llm_degradation_scope()
    try:
        assert llm_scope_rate_deferred() is False          # nothing recorded yet
        _record_llm_nonanswer("REFRAME", "rate_deferred")
        assert llm_scope_rate_deferred() is True           # the abort signal is live
        snap = llm_degradation_snapshot()
        assert snap["llm_rate_deferred"] is True           # rides the envelope additively
        assert snap["llm_first_unanswered_reason"] == "REFRAME:rate_deferred"
    finally:
        reset_llm_degradation_scope(tok)


def test_non_rate_nonanswer_never_stamps_the_paced_signal():
    from src.api.llm_calls import (begin_llm_degradation_scope,
                                   llm_degradation_snapshot,
                                   llm_scope_rate_deferred,
                                   reset_llm_degradation_scope,
                                   _record_llm_nonanswer)
    tok = begin_llm_degradation_scope()
    try:
        _record_llm_nonanswer("EXTRACT", "all_retries_exhausted")
        assert llm_scope_rate_deferred() is False
        assert llm_degradation_snapshot()["llm_rate_deferred"] is False
    finally:
        reset_llm_degradation_scope(tok)


def test_no_scope_peek_is_false_and_never_raises():
    from src.api.llm_calls import llm_scope_rate_deferred
    assert llm_scope_rate_deferred() is False


@pytest.mark.asyncio
async def test_harvest_wrapper_envelope_carries_the_typed_outcome(monkeypatch):
    """PIN (c), backend half: /harvest-spans' degraded envelope carries
    ``llm_rate_deferred`` so every consumer can branch on the typed signal."""
    from src.api import main as api_main
    from src.api import llm_calls
    from src.api.models import RewriteRequest

    async def _paced_impl(req):
        llm_calls._record_llm_nonanswer("REFRAME", "rate_deferred")
        return {"edges": [{"subject": "a", "rel_type": "r", "object": "b"}], "spans": 1}

    monkeypatch.setattr(api_main, "_harvest_spans_impl", _paced_impl)
    out = await api_main.harvest_spans(RewriteRequest(text="A r b.", user_id="u1"))
    assert out["status"] == "degraded"
    assert out["extraction_degraded"] is True
    assert out["edges"] == []                       # the withhold is unchanged
    assert out["llm_rate_deferred"] is True
    assert out["llm_first_unanswered_reason"] == "REFRAME:rate_deferred"


# ── the API-side half: a paced turn skips its remaining LLM ops ───────────────


@pytest.mark.asyncio
async def test_paced_sentence_pipeline_skips_remaining_llm_ops(monkeypatch):
    """PIN (a), API half: once the turn's ledger holds a rate_deferred, the sentence
    pipeline's remaining per-sentence LLM op (the merge relation-fill) is NEVER called —
    the deterministic deriver still ran, the envelope shape is unchanged. The A/B control
    proves the guard (not the stubbing) is what suppresses the call."""
    from src.api import main as api_main
    from src.api import llm_calls
    from src.api.models import RewriteRequest
    import src.extraction.reframe as _reframe_mod

    merge_calls: list = []
    reframe_calls: list = []

    async def _no_merge(*a, **k):
        merge_calls.append(1)
        return []

    async def _fake_reframe(text, user_id, messages=None):
        reframe_calls.append(1)
        return types.SimpleNamespace(atoms=[], used_llm=False)

    class _Doc:
        ents = [object()]

    monkeypatch.setattr(api_main, "_compute_ingest_reference_and_text",
                        lambda t: (None, t))
    monkeypatch.setattr(api_main, "_repair_frame", lambda t, u: None)
    monkeypatch.setattr(_reframe_mod, "reframe_to_atomic", _fake_reframe)
    monkeypatch.setattr(api_main, "get_gliner_model", lambda: object())
    monkeypatch.setattr(api_main, "_gliner2_entity_labels",
                        lambda source=None: [])
    monkeypatch.setattr(api_main, "_build_typed_doc",
                        lambda sent, gmodel, labels=None: _Doc())
    monkeypatch.setattr(api_main, "_merge_capture_edges", _no_merge)

    _UUID = "11111111-1111-1111-1111-111111111111"

    async def _run():
        return await api_main._harvest_via_sentence_pipeline(
            RewriteRequest(text=BIO, user_id=_UUID), _UUID)

    # A: PACED — a rate_deferred REFRAME was already recorded (the real limiter stamps
    # this BEFORE the sentence loop runs).
    tok = llm_calls.begin_llm_degradation_scope()
    try:
        llm_calls._record_llm_nonanswer("REFRAME", "rate_deferred")
        out = await _run()
    finally:
        llm_calls.reset_llm_degradation_scope(tok)
    assert merge_calls == [], \
        "a paced turn must skip its remaining per-sentence LLM ops (merge relation-fill)"
    assert len(reframe_calls) <= 1, "reframe runs at most once, BEFORE the loop"
    assert isinstance(out, dict) and set(out) >= {"edges", "spans"}, \
        "the envelope shape is unchanged (the wrapper withholds, not the pipeline)"

    # B: CONTROL — the same turn, ledger clean: the merge fill DOES run (guard proven).
    tok = llm_calls.begin_llm_degradation_scope()
    try:
        await _run()
    finally:
        llm_calls.reset_llm_degradation_scope(tok)
    assert len(merge_calls) >= 1, \
        "with no paced signal the pipeline keeps today's ladder (guard is what suppressed A)"


# ── the MCP inline lane: paced chunk → queued, deferred_retry counted ────────


@pytest.mark.asyncio
async def test_mcp_sync_chunk_rate_deferred_queues_and_counts_deferred(monkeypatch):
    """PIN (c), MCP half: on the synchronous ingest_document path a paced harvest turn
    is queued on the defer worker (the cheap reschedule) and counted
    ``chunks_deferred_retry`` — the /extract/rewrite fallback is never dialed and the
    turn does not sit out the wall re-asking the brain."""
    calls = []

    async def fake_post(url, json=None, timeout=None, headers=None):
        calls.append(url)
        if url.endswith("/documents/enqueue"):
            return _resp({"status": "soft_error", "document_id": None})
        if url.endswith("/episodic/append"):
            return _resp({"status": "stored", "id": 1})
        if url.endswith("/harvest-spans"):
            return _resp(PACED_ENVELOPE_TYPED)
        raise AssertionError(f"unexpected call on a paced turn: {url}")

    async def fake_get(url, timeout=None):
        return _resp({"statement_extractor": "spine"})

    deferred_with = []

    async def _fake_defer(text, user_id, *, ingest_source="mcp"):
        deferred_with.append((text, user_id, ingest_source))
        return True

    monkeypatch.setattr(server, "_http_client",
                        types.SimpleNamespace(post=fake_post, get=fake_get))
    monkeypatch.setattr(server, "_defer_statement_extraction", _fake_defer)
    monkeypatch.setattr(server, "SHORT_TERM_MEMORY", False)
    out = await server.ingest_document_tool(BIO, "u-alice", source_ref="bio.txt")

    assert not any(u.endswith("/extract/rewrite") for u in calls), \
        f"the rewrite fallback must never run on a paced turn, called: {calls}"
    assert sum(1 for u in calls if u.endswith("/harvest-spans")) == 1, \
        f"the spine must not be retried on a paced turn, called: {calls}"
    assert deferred_with and deferred_with[0][2] == "document", \
        "the chunk must be handed to the paced defer worker with document authorship"
    assert out["chunks_failed"] >= 1
    assert out["chunks_deferred_retry"] == sum(
        1 for _ in deferred_with)
    assert out["chunks_deferred_retry"] >= 1


# ── the typed outcome on the MCP seam (detector precedence + forwarding) ──────


def test_mcp_paced_detector_typed_field_first_substring_fallback():
    """The typed field is authoritative when present; the reason-string scan keeps
    OLDER backends (no typed field) detected; non-rate degraded is never paced."""
    degraded = {"extraction_degraded": True}
    assert server._result_is_paced_not_answered(
        {**degraded, "llm_rate_deferred": True}) is True
    assert server._result_is_paced_not_answered(
        {**degraded, "llm_rate_deferred": False,
         "llm_first_unanswered_reason": "REFRAME:rate_deferred"}) is True
    assert server._result_is_paced_not_answered(
        {**degraded, "llm_unanswered_operations": {"REFRAME": 1}}) is False
    assert server._result_is_paced_not_answered(
        {**degraded, "llm_rate_deferred": False,
         "llm_first_unanswered_reason": "EXTRACT:all_retries_exhausted"}) is False
    assert server._result_is_paced_not_answered(
        {"status": "success", "llm_rate_deferred": True}) is False
    assert server._result_is_paced_not_answered(None) is False


@pytest.mark.asyncio
async def test_spine_seam_forwards_the_typed_outcome(monkeypatch):
    """The harvest→inline seam's degraded dict carries ``llm_rate_deferred`` (typed) so
    the paced branch keys first-class — and an OLDER backend (reason string only)
    still classifies paced via the substring fallback."""
    class _R:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    async def fake_brain_request(op, method, url, **kw):
        return _R(PACED_ENVELOPE_TYPED)

    monkeypatch.setattr(server, "_brain_request", fake_brain_request)
    out = await server._ingest_statement_via_spine(BIO, "u1")
    assert out["status"] == "extraction_degraded"
    assert out["llm_rate_deferred"] is True
    assert server._result_is_paced_not_answered(out) is True

    async def fake_brain_request_old(op, method, url, **kw):
        return _R(PACED_ENVELOPE_SUBSTRING)

    monkeypatch.setattr(server, "_brain_request", fake_brain_request_old)
    out_old = await server._ingest_statement_via_spine(BIO, "u1")
    assert out_old["llm_rate_deferred"] is False
    assert server._result_is_paced_not_answered(out_old) is True
