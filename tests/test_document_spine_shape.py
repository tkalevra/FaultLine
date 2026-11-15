"""DOCSPINE — the document lane's SHAPE boundary.

Two defects, both measured on the product's own chunks (2026-07-31, seat 9fccbb8a, a real
200-chunk LongMemEval document):

  (1) CONTENT-FREE LANES.  `_partition_chunk_by_role` prepends the shared "[Date: …]" header to
      BOTH lanes so assistant facts get dated. When a chunk holds lines from only one speaker —
      the common case, because the chunker splits on SIZE, not on turn boundaries — the other
      lane comes back as the bare 30-character header and nothing else.  **172 of 381 non-empty
      lanes (45.1%)** were header-only.  Each one called /harvest-spans (0 edges by
      construction), logged `document_chunk_spine_empty`, then paid a full /extract/rewrite LLM
      call on a bare date — and under DOC_CHUNK_FAILURE_LOUD a timeout on that pointless call is
      charged to the CHUNK.

  (2) THE ATOMIZER IS SIZED FOR A CHAT TURN.  `reframe_to_atomic` is prompted for "one chat
      message" and budgeted as one (LLMMaxTokens REFRAME=256, LLMTimeouts REFRAME default 6.0s).
      A document chunk is multi-sentence expository prose. The document lane now PINS the
      deterministic segmenter per REQUEST (`RewriteRequest.deterministic_segmentation`), because
      the caller is the only party that knows the shape of the text it is handing over.

Both are flag-gated and both flags are pinned OFF here as byte-identical to the legacy path.
"""
import pytest

from src.re_embedder import embedder


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# The literal shape the product produces — this exact string was 45.1% of the lanes.
HEADER_ONLY = "[Date: 2023/05/22 (Mon) 18:21]"
WITH_CONTENT = HEADER_ONLY + "\nWalkmeter is an excellent app for tracking your walks."


# ── (1) a lane with no content is not text to extract ────────────────────────


@pytest.mark.parametrize("text,expected", [
    (HEADER_ONLY, True),
    ("", True),
    ("   \n\n  ", True),
    ("[Date: 2023/05/22 (Mon) 18:21]\n[Date: 2023/05/23 (Tue) 09:00]", True),
    (WITH_CONTENT, False),
    ("Walkmeter is an excellent app.", False),
])
def test_lane_content_predicate_is_structural_only(text, expected):
    """It asks only whether a line is scaffolding — never what the line says."""
    assert embedder._lane_has_no_content(text) is expected


def test_header_only_lane_calls_no_extractor_at_all(monkeypatch):
    """THE FIX: 45% of this lane's extractor traffic was spent on a bare date."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_SKIP_CONTENTLESS", True)
    calls = []

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        calls.append(url)
        return _StubResponse({"status": "success", "edges": []})

    monkeypatch.setattr(embedder.httpx, "post", _post)
    assert embedder._document_chunk_edges(HEADER_ONLY, "u", "http://backend", "rewrite") == []
    assert calls == [], f"a header-only lane must reach no extractor, called: {calls}"


def test_header_only_skip_is_a_true_read_and_empty_not_a_failure(monkeypatch):
    """DOCLOSS-B stays honest: [] here means 'read deterministically, nothing in it'."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_SKIP_CONTENTLESS", True)
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)

    def _boom(*a, **k):
        raise AssertionError("no extractor call should happen")

    monkeypatch.setattr(embedder.httpx, "post", _boom)
    assert embedder._document_chunk_edges(HEADER_ONLY, "u", "http://backend", "rewrite") == []


def test_flag_off_still_burns_both_extractor_calls_on_a_bare_header(monkeypatch):
    """FLAG OFF: byte-identical legacy behaviour — spine, then rewrite, on a bare date."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_SKIP_CONTENTLESS", False)
    calls = []

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        calls.append(url)
        return _StubResponse({"status": "success", "edges": []})

    monkeypatch.setattr(embedder.httpx, "post", _post)
    assert embedder._document_chunk_edges(HEADER_ONLY, "u", "http://backend", "rewrite") == []
    assert calls == ["http://backend/harvest-spans", "http://backend/extract/rewrite"], \
        f"legacy path must still call both extractors on a bare header, got {calls}"


def test_a_lane_that_carries_content_is_still_extracted(monkeypatch):
    """ZERO REGRESSION: the skip must never swallow a lane that has an assertion in it."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_SKIP_CONTENTLESS", True)
    edge = {"subject": "walkmeter", "rel_type": "instance_of", "object": "app"}
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _StubResponse(
        {"status": "success", "edges": [edge]}))
    assert embedder._document_chunk_edges(
        WITH_CONTENT, "u", "http://backend", "rewrite") == [edge]


# ── (2) the shape hint reaches /harvest-spans ────────────────────────────────


def _capture_harvest_body(monkeypatch):
    bodies = {}

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        bodies[url.rsplit("/", 1)[-1]] = json
        return _StubResponse({"status": "success", "edges": []})

    monkeypatch.setattr(embedder.httpx, "post", _post)
    return bodies


def test_document_lane_pins_deterministic_segmentation(monkeypatch):
    monkeypatch.setattr(embedder, "DOC_CHUNK_SKIP_CONTENTLESS", True)
    monkeypatch.setattr(embedder, "DOC_CHUNK_SPINE_DETSEG", True)
    bodies = _capture_harvest_body(monkeypatch)
    embedder._document_chunk_edges(WITH_CONTENT, "u", "http://backend", "rewrite")
    assert bodies["harvest-spans"]["deterministic_segmentation"] is True


def test_flag_off_sends_no_segmentation_hint_at_all(monkeypatch):
    """FLAG OFF: the field is ABSENT, so the backend's own flag decides — as today."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_SKIP_CONTENTLESS", True)
    monkeypatch.setattr(embedder, "DOC_CHUNK_SPINE_DETSEG", False)
    bodies = _capture_harvest_body(monkeypatch)
    embedder._document_chunk_edges(WITH_CONTENT, "u", "http://backend", "rewrite")
    assert "deterministic_segmentation" not in bodies["harvest-spans"]


def test_the_hint_never_leaks_onto_the_rewrite_call(monkeypatch):
    """/extract/rewrite has no such concept; its body must stay exactly as it was."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_SKIP_CONTENTLESS", True)
    monkeypatch.setattr(embedder, "DOC_CHUNK_SPINE_DETSEG", True)
    bodies = _capture_harvest_body(monkeypatch)
    embedder._document_chunk_edges(WITH_CONTENT, "u", "http://backend", "rewrite")
    assert set(bodies["rewrite"]) == {"text", "user_id", "force_relation_extraction"}


def test_request_model_defaults_to_no_opinion():
    """Every caller today omits the field, so the process-wide flag still decides."""
    from src.api.models import RewriteRequest
    assert RewriteRequest(text="hi").deterministic_segmentation is None
    assert RewriteRequest(text="hi", deterministic_segmentation=True
                          ).deterministic_segmentation is True
