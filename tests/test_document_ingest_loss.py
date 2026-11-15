"""DOCLOSS — /ingest_document must never silently lose user content.

Pins the two silent-loss defects this suite exists to prevent, at the seams the LIVE
measurement proved them (seat1, LongMemEval oracle question gpt4_a1b77f9c, 309 chunks):

  (A) HARD 200-CHUNK TRUNCATION. src/mcp/server.py discarded chunks[_DOC_MAX_CHUNKS:]
      after a container-log warning and returned a success-shaped "pending" response.
      Measured: 109 of 309 chunks (35.3%) discarded => 89 of 228 facts (39.0%) destroyed,
      and 26 of 72 ANSWER-BEARING turns thrown away, while the caller was told the
      document "was received and is being processed". The discarded tail was truncated
      BEFORE the enqueue, so it never reached documents.chunks either — migration 183's
      "the chunks JSONB IS the verbatim safety net" did not hold for it.

  (B) FAILED EXTRACTION COUNTED AS "no facts here". call_llm_with_retry_async signals a
      non-answer by RETURNING A DICT ({"error": "circuit_breaker_open", ...} when the
      breaker is open, bare {} when blocked/unparseable) — never by raising — and
      _document_chunk_edges swallowed every exception into []. So a chunk the extractor
      NEVER READ was tallied as a clean chunk with no facts and the document still
      finalized status='ready', chunks_failed=0. Measured on the same run: 53 of 309
      chunks (17.2%) timed out and were swallowed silently; both documents reported
      'ready'; 831 circuit_breaker_open events in the window.

PRIMARY SOURCES for the mechanisms named here:
  • Circuit Breaker — M. Nygard, *Release It!* (2nd ed., Pragmatic Bookshelf, 2018),
    ch. 5 "Stability Patterns" / "Circuit Breaker": an OPEN breaker must fast-fail
    DISTINGUISHABLY so the caller can take a fallback or retry after the reset timeout.
    A rejection indistinguishable from a successful empty answer turns a transient
    outage into permanent data loss.
  • At-least-once delivery / bounded redelivery — G. Hohpe & B. Woolf, *Enterprise
    Integration Patterns* (Addison-Wesley, 2003), "Guaranteed Delivery" and "Dead Letter
    Channel": work that could not be processed is redelivered, and redelivery is BOUNDED
    — after N attempts it goes to a dead-letter/terminal-error state rather than looping
    forever or being dropped.
  • Segmentation of an oversized payload — the classic answer is to bound the UNIT OF
    WORK, not the work: J. Postel, RFC 791 §3.2 (fragmentation/reassembly) is the
    canonical statement that an over-large payload is split and fully delivered, never
    truncated. Here each segment is one `documents` registry row.

Run with:  python3 tools/fltest.py --bug DOCLOSS --test tests/test_document_ingest_loss.py
"""

import asyncio
import json

import pytest

import src.mcp.server as mcp_server
import src.re_embedder.embedder as embedder

# Captured BEFORE the autouse fixture below can patch it, so the probe's own behaviour
# stays testable (the fixture stands it up for every OTHER drain test).
_REAL_BACKEND_PROBE = embedder._backend_is_reachable


@pytest.fixture(autouse=True)
def _backend_up(monkeypatch):
    """DOCDRAIN: the drain now probes the backend BEFORE claiming (it must never burn a
    document against a backend that is not listening). Every pre-existing drain test uses
    a fake backend URL, so default the probe to 'up' and let the DOCDRAIN tests override."""
    monkeypatch.setattr(embedder, "_backend_is_reachable", lambda _url: True)


# ── (A) chunk segmentation — nothing discarded ───────────────────────────────


def _fake_enqueue_response(document_id: int, eta: int = 60):
    class _R:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "pending", "document_id": document_id,
                    "eta_seconds": eta, "chunk_count": 0}

    return _R()


class _EnqueueRecorder:
    """Stands in for the MCP's httpx client on the ingest_document_tool wire.

    ``bodies`` records ONLY the /documents/enqueue POSTs - the part accounting every
    segmentation test below asserts on. Two LATER lanes also POST on this tool's wire
    and are served (not recorded): R1 source retention (/artefacts/retain, bf55ddaf) and
    the enqueue-window stub-fact mint (/ingest, 4e8b8de0). The old recorder asserted
    every POST was an enqueue, so both later lanes CRIT-failed inside every test here
    while the suite still read green.

    fail_from: 0-based part index at which the enqueue starts failing (None = never).
    """

    def __init__(self, fail_from=None):
        self.bodies = []
        self.fail_from = fail_from

    async def post(self, url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        if url.endswith("/documents/enqueue"):
            idx = len(self.bodies)
            self.bodies.append(json)
            if self.fail_from is not None and idx >= self.fail_from:
                raise RuntimeError("simulated registry outage")
            return _fake_enqueue_response(document_id=100 + idx)
        if url.endswith("/artefacts/retain"):
            return _StubResponse({"artefact_id": 7, "duplicate": False})
        assert url.endswith("/ingest"), f"unexpected POST to {url}"
        return _StubResponse({"committed": 0, "staged": 0})


def _over_cap_text(n_paragraphs: int) -> str:
    """A document whose deterministic chunking exceeds _DOC_MAX_CHUNKS.

    Each paragraph is two plain sentences so _chunk_document keeps it as one chunk
    (>= _DOC_CHUNK_MIN_SENTS, well under _DOC_CHUNK_MAX_CHARS). Content is neutral —
    no domain vocabulary, nothing the ontology could key on.
    """
    return "\n\n".join(
        f"Item number {i} was recorded on the list. It carried the label alpha {i}."
        for i in range(n_paragraphs)
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def over_cap_doc():
    text = _over_cap_text(mcp_server._DOC_MAX_CHUNKS + 109)
    chunks = mcp_server._chunk_document(text)
    assert len(chunks) > mcp_server._DOC_MAX_CHUNKS, (
        f"fixture must exceed the cap; got {len(chunks)}")
    return text, chunks


def test_over_cap_document_is_segmented_not_truncated(monkeypatch, over_cap_doc):
    """FLAG ON: every chunk is enqueued across parts — the union loses nothing."""
    text, chunks = over_cap_doc
    rec = _EnqueueRecorder()
    monkeypatch.setattr(mcp_server, "DOC_NO_SILENT_TRUNCATION", True)
    monkeypatch.setattr(mcp_server, "_http_client", rec)

    result = _run(mcp_server.ingest_document_tool(
        text=text, user_id="test-user", source_ref="docloss-test"))

    assert result["status"] == "pending"
    # Every chunk reached the registry, in order, with nothing dropped.
    sent = [c for body in rec.bodies for c in body["chunks"]]
    assert sent == chunks, "segmented enqueue must preserve every chunk, in order"
    assert result["chunks"] == len(chunks)
    assert result["chunks_submitted"] == len(chunks)
    assert result["truncated"] is False
    assert result["parts"] == len(rec.bodies) > 1
    # No part exceeds the per-row resource bound.
    assert all(len(b["chunks"]) <= mcp_server._DOC_MAX_CHUNKS for b in rec.bodies)


def test_flag_off_reproduces_the_legacy_truncation_exactly(monkeypatch, over_cap_doc):
    """FLAG OFF: byte-identical legacy TRUNCATION behaviour (the defect), including the
    response shape's part accounting.

    This is the regression pin for 'OFF is byte-identical' — NOT an endorsement of the
    behaviour it pins. Note precisely what a caller got: status 'pending' (success),
    chunks reported as the TRUNCATED count, and no part accounting at all.

    PIN MOVED WITH EVIDENCE (doc-lane honesty lane, 2026-11-08; the 55d31515 doctrine):
    the pin used to demand the 2026-07 wire exactly — six body keys, OFF envelope free of
    every later key. Three LATER lanes legitimately widened the wire without touching the
    truncation this flag gates:
      • parser_version on the enqueue BODY (08857191, content-repair/re-chunk lane) —
        needed on EVERY enqueue so a registry row can be re-validated when the parser
        changes; stripping it OFF-path would break that lane's contract, not restore it.
      • stub_facts_minted on the envelope (4e8b8de0, keyword-searchable at enqueue).
      • source_artefact_id / source_dedup / source_retained on the envelope (bf55ddaf,
        R1 pasted-text source retention).
    The DOCLOSS-relevant identity is unchanged and still pinned EXACTLY: the six legacy
    body keys PLUS parser_version, a single part, the truncated head, truncated:true,
    and none of the four DOCLOSS part-accounting keys. The OFF lever rolls back THE
    TRUNCATION — it was never a rollback of lanes that landed after this design.
    """
    text, chunks = over_cap_doc
    rec = _EnqueueRecorder()
    monkeypatch.setattr(mcp_server, "DOC_NO_SILENT_TRUNCATION", False)
    monkeypatch.setattr(mcp_server, "_http_client", rec)

    result = _run(mcp_server.ingest_document_tool(
        text=text, user_id="test-user", source_ref="docloss-test"))

    assert len(rec.bodies) == 1
    assert len(rec.bodies[0]["chunks"]) == mcp_server._DOC_MAX_CHUNKS
    assert set(rec.bodies[0]) == {"user_id", "chunks", "chunk_count", "source_ref",
                                  "title", "truncated",
                                  "parser_version"}, "legacy body + parser_version (08857191)"
    assert result["status"] == "pending"
    assert result["chunks"] == mcp_server._DOC_MAX_CHUNKS
    assert result["truncated"] is True
    for added in ("chunks_submitted", "document_ids", "parts", "part_count"):
        assert added not in result, f"OFF must not add {added} to the legacy envelope"


def test_partial_enqueue_is_loud_and_never_reported_as_pending(monkeypatch, over_cap_doc):
    """A document only PARTLY accepted must say so, with counts — never 'pending'."""
    text, chunks = over_cap_doc
    rec = _EnqueueRecorder(fail_from=1)  # part 1 lands, part 2 fails
    monkeypatch.setattr(mcp_server, "DOC_NO_SILENT_TRUNCATION", True)
    monkeypatch.setattr(mcp_server, "_http_client", rec)

    result = _run(mcp_server.ingest_document_tool(
        text=text, user_id="test-user", source_ref="docloss-test"))

    assert result["status"] == "partial"
    assert result["chunks"] == mcp_server._DOC_MAX_CHUNKS
    assert result["chunks_submitted"] == len(chunks)
    assert result["chunks_not_accepted"] == len(chunks) - mcp_server._DOC_MAX_CHUNKS
    # The prose a model relays to the user must carry the warning, not just the fields.
    assert "NOT stored" in result["message"]
    assert "WARNING" in result["message"]


def test_under_cap_document_is_unchanged(monkeypatch):
    """ZERO REGRESSION: a normal, under-cap document keeps the single-part legacy shape."""
    text = _over_cap_text(12)
    chunks = mcp_server._chunk_document(text)
    assert len(chunks) <= mcp_server._DOC_MAX_CHUNKS
    rec = _EnqueueRecorder()
    monkeypatch.setattr(mcp_server, "DOC_NO_SILENT_TRUNCATION", True)
    monkeypatch.setattr(mcp_server, "_http_client", rec)

    result = _run(mcp_server.ingest_document_tool(
        text=text, user_id="test-user", source_ref="docloss-test"))

    assert len(rec.bodies) == 1
    assert result["status"] == "pending"
    assert result["chunks"] == len(chunks)
    assert result["truncated"] is False
    assert result["parts"] == 1
    assert "WARNING" not in result["message"]


# ── (B) a failed extraction is not an empty one ──────────────────────────────


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_breaker_rejected_chunk_raises_brain_unavailable(monkeypatch):
    """An OPEN circuit breaker must defer the document, not read as 'no facts here'."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _StubResponse({
        "status": "degraded", "edges": [], "error": "circuit_breaker_open",
        "extraction_degraded": True, "chunks_total": 2, "chunks_failed": 2,
        "failures": [{"chunk": 0, "reason": "circuit_breaker_open"}],
    }))
    with pytest.raises(embedder.DocumentBrainUnavailable):
        embedder._document_chunk_edges("some chunk text", "u", "http://backend", "rewrite")
    # RuntimeError subclass on purpose — the drain's existing freeze/defer path catches it.
    assert issubclass(embedder.DocumentBrainUnavailable, RuntimeError)


def test_degraded_chunk_raises_chunk_failure(monkeypatch):
    """A partly-unread chunk is a FAILURE, so it can be counted and surfaced."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _StubResponse({
        "status": "success", "edges": [], "error": None,
        "extraction_degraded": True, "chunks_total": 3, "chunks_failed": 1,
        "failures": [{"chunk": 2, "reason": "empty_llm_response"}],
    }))
    with pytest.raises(embedder.DocumentChunkExtractionFailed):
        embedder._document_chunk_edges("some chunk text", "u", "http://backend", "rewrite")


def test_partially_degraded_chunk_keeps_the_edges_it_did_extract(monkeypatch):
    """Reporting a failure must not cost the facts that DID extract (a smaller silent loss).

    NOTE (DOCDRAIN, 2026-07-31): this test was RED on HEAD and the fixture was why, not the
    code. DOC_CHUNK_SPINE_FIRST landed today, so the chunk path now calls /harvest-spans
    BEFORE /extract/rewrite; the old single URL-blind stub answered the SPINE call with
    edges, so _document_chunk_edges returned early and the degraded-rewrite branch this test
    names was never reached. The stub is now URL-aware (spine finds nothing → fall through to
    the rewrite envelope), which is both the real default path and what the test claims to
    pin. Exactly the "check a fixture against the code it actually exercises" trap.

    RE-TRIAGED 2026-08-21 (fix/doc-lane-fairness, un-quarantined): the quarantine's
    "deterministically red on the saas lineage" attribution was WRONG in one specific —
    the four quarantined tests shared ONE root cause and it was FIXTURE DRIFT, not a product
    regression: production call sites now pass headers=_BACKEND_LANE_HEADERS to httpx.post,
    these stubs' signatures only accepted (url, json, timeout) → TypeError → the chunk
    failed and the edges were swallowed, so the pinned BEHAVIOUR was never actually exercised.
    The stubs now mirror httpx's real signature. Verified green at un-quarantine; the
    behaviours they pin (salvage, deferral, timeout plumbing) are live and load-bearing.
    """
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    good = {"subject": "a", "rel_type": "r", "object": "b"}

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        if url.endswith("/harvest-spans"):
            return _StubResponse({"status": "success", "edges": []})
        return _StubResponse({
            "status": "success", "edges": [good], "error": None,
            "extraction_degraded": True, "chunks_total": 4, "chunks_failed": 1,
            "failures": [{"chunk": 3, "reason": "empty_llm_response"}],
        })

    monkeypatch.setattr(embedder.httpx, "post", _post)
    with pytest.raises(embedder.DocumentChunkExtractionFailed) as exc:
        embedder._document_chunk_edges("t", "u", "http://backend", "rewrite")
    assert exc.value.partial_edges == [good]


def test_salvaged_edges_are_ingested_and_the_chunk_still_counts_failed(monkeypatch):
    """The salvage is committed through the WGM gate AND the document lands 'partial'.

    RE-TRIAGED 2026-08-21 (fix/doc-lane-fairness): un-quarantined — fixture drift (stub
    signature missing headers=), not a product regression. See the note on
    test_partially_degraded_chunk_keeps_the_edges_it_did_extract.
    """
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    good = {"subject": "a", "rel_type": "r", "object": "b"}
    posted = []

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        posted.append((url, (json or {}).get("edges")))
        return _StubResponse({"committed": 1, "staged": 0})

    def _edges(chunk, *a, **k):
        err = embedder.DocumentChunkExtractionFailed("degraded")
        err.partial_edges = [good]
        raise err

    monkeypatch.setattr(embedder.httpx, "post", _post)
    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    conn = _FakeConn(["one chunk"])
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert any(u.endswith("/ingest") and e == [good] for u, e in posted), \
        "the edges that DID extract must still be ingested"
    assert _finalize_status(conn) == "partial"


def test_transport_timeout_is_not_an_empty_extraction(monkeypatch):
    """The exact live failure: 53/309 chunks 'timed out' and were counted as clean empties."""
    def _boom(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder.httpx, "post", _boom)
    with pytest.raises(embedder.DocumentChunkExtractionFailed):
        embedder._document_chunk_edges("some chunk text", "u", "http://backend", "rewrite")


def test_flag_off_reproduces_the_silent_swallow(monkeypatch):
    """FLAG OFF: byte-identical legacy behaviour — the failure collapses to []."""
    def _boom(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", False)
    monkeypatch.setattr(embedder.httpx, "post", _boom)
    assert embedder._document_chunk_edges("t", "u", "http://backend", "rewrite") == []


def test_clean_empty_extraction_is_still_empty(monkeypatch):
    """ZERO REGRESSION: a chunk the extractor DID read but found nothing in stays empty."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _StubResponse({
        "status": "success", "edges": [], "error": None,
    }))
    assert embedder._document_chunk_edges("t", "u", "http://backend", "rewrite") == []


def test_low_confidence_filter_survives(monkeypatch):
    """ZERO REGRESSION: WGM-gate hygiene (drop low_confidence edges) is unchanged."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _StubResponse({
        "status": "success",
        "edges": [{"subject": "a", "rel_type": "r", "object": "b"},
                  {"subject": "c", "rel_type": "r", "object": "d", "low_confidence": True}],
    }))
    out = embedder._document_chunk_edges("t", "u", "http://backend", "rewrite")
    assert len(out) == 1 and out[0]["subject"] == "a"


# ── (B) terminal status must reflect reality ─────────────────────────────────


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.statements.append((" ".join(sql.split()), params))
        self._last = (sql, params)

    def fetchall(self):
        sql = (self._last or ("", None))[0]
        if "SELECT id FROM documents" in sql:
            return [(7,)]
        return []

    def fetchone(self):
        sql = (self._last or ("", None))[0]
        if "SET status = 'processing'" in sql:
            return (self._conn.chunks, "ref", 1)
        return None


class _FakeConn:
    def __init__(self, chunks):
        self.chunks = chunks
        self.statements = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        return None

    def rollback(self):
        return None


def _finalize_status(conn):
    for sql, params in conn.statements:
        if "SET status = %s, ready_at = now()" in sql:
            return params[0]
    return None


def test_document_with_unread_chunks_finalizes_partial_not_ready(monkeypatch):
    """The terminal signal must not say 'ready' over chunks the extractor never read."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _StubResponse({}))

    def _edges(chunk, *a, **k):
        if "bad" in chunk:
            raise embedder.DocumentChunkExtractionFailed("timed out")
        return []

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    conn = _FakeConn(["good one", "bad one"])
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert _finalize_status(conn) == "partial"


def test_fully_read_document_still_finalizes_ready(monkeypatch):
    """ZERO REGRESSION: no failures => the terminal status is unchanged."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _StubResponse({}))
    monkeypatch.setattr(embedder, "_document_chunk_edges", lambda *a, **k: [])
    conn = _FakeConn(["good one", "also good"])
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert _finalize_status(conn) == "ready"


def test_flag_off_still_finalizes_ready_over_failures(monkeypatch):
    """FLAG OFF: byte-identical legacy behaviour — 'ready' even with failed chunks."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", False)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _StubResponse({}))

    def _edges(chunk, *a, **k):
        if "bad" in chunk:
            raise embedder.DocumentChunkExtractionFailed("timed out")
        return []

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    conn = _FakeConn(["good one", "bad one"])
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert _finalize_status(conn) == "ready"


# ── (B2) DOCLOSS-B LITMUS at the ACCOUNTING grain ────────────────────────────
#
# The doc-lane honesty litmus: a document with N doomed chunks must report EXACTLY N
# failed — per-chunk states explicit (done / failed-with-reason / never-reached),
# terminal 'partial', never a clean 'ready' — and the LEDGER (documents.chunk_state,
# migration 205) that the MCP document_status surface reads via
# main.py::_doc_chunk_accounting must carry exactly N failed entries naming WHY. Pinned
# at the drain's real finalize UPDATE, not at a helper: if the ladder ever counts a
# doomed chunk as a clean empty again, this goes red on the very statement that lands
# the status. (The original wound measured the opposite: 53 of 309 chunks logged
# 'timed out', returned [], and were counted as clean chunks with no facts; both
# documents finalized 'ready'.)


class _LedgerCursor(_FakeCursor):
    """_FakeCursor + the migration-205 chunk_state capability probe answering PRESENT,
    so the legacy-lane drain persists the per-chunk ledger it built at finalize."""

    def fetchone(self):
        sql = (self._last or ("", None))[0]
        if "column_name = 'chunk_state'" in sql:
            return (1,)
        return super().fetchone()


class _LedgerConn(_FakeConn):
    def cursor(self):
        return _LedgerCursor(self)


def _finalize_statement(conn):
    for sql, params in conn.statements:
        if "SET status = %s, ready_at = now()" in sql:
            return sql, params
    return None, None


def test_n_doomed_chunks_report_exactly_n_failed_never_ready_silent(monkeypatch):
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder.httpx, "post",
                        lambda *a, **k: _StubResponse({"committed": 1, "staged": 0}))

    doomed = {1, 3, 4}          # the extractor NEVER reads these three of five chunks

    def _edges(chunk, *a, **k):
        if "doomed" in chunk:
            raise embedder.DocumentChunkExtractionFailed("timed out")
        return [{"subject": "a", "rel_type": "r", "object": "b"}]

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    chunks = [("doomed %d" % i) if i in doomed else ("clean %d" % i) for i in range(5)]
    conn = _LedgerConn(chunks)
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)

    sql, params = _finalize_statement(conn)
    assert sql is not None, "the document must finalize"
    terminal, committed = params[0], params[1]
    staged, chunks_failed = params[2], params[3]
    assert terminal == "partial", "doomed chunks must never read as a clean 'ready'"
    assert chunks_failed == len(doomed) == 3, (
        f"EXACTLY N failed required; got chunks_failed={chunks_failed}")
    assert committed == 2 and staged == 0, \
        "clean chunks still count; doomed chunks invent no facts"

    # THE LEDGER: exactly N failed entries, each naming WHY it was never read; the two
    # clean chunks are done entries with their captured counts. This is the exact shape
    # GET /documents/status renders (failed_chunks[] with chunk/reason/attempts).
    ledger = json.loads(params[4])
    assert len(ledger) == 5, "every chunk carries an explicit terminal state"
    failed_entries = {k: v for k, v in ledger.items() if v.get("s") == "failed"}
    done_entries = {k: v for k, v in ledger.items() if v.get("s") == "done"}
    assert len(failed_entries) == 3 and len(done_entries) == 2
    assert all(v.get("e") for v in failed_entries.values()), \
        "each failure names its reason code"
    assert all("t" in v for v in ledger.values()), "terminal entries carry stamps"
    assert all(int(done_entries[k]["c"]) == 1 for k in done_entries), \
        "done entries carry their committed counts"

    # THE READ SIDE (main.py::_doc_chunk_accounting — what document_status relays):
    # failed == N exactly, nothing unread, retry-owed == N, classification live.
    from src.api.main import _doc_chunk_accounting
    acct = _doc_chunk_accounting(len(chunks), ledger, staged, committed)
    assert acct["failed"] == 3 and acct["kept"] == 2
    assert acct["unread"] == 0 and acct["owed"] == 3 and acct["accounting"] == "live"


# ── (C) DOCDRAIN — a backend outage must not be charged to the CHUNKS ────────
#
# MEASURED 2026-07-31 on pre-prod. Over one 6h window 1153 of 1166 document-lane chunk
# failures were `ConnectError: [Errno 111] Connection refused` — the re_embedder shares a
# container with the API, so on every deploy/restart it wakes before uvicorn is listening.
# Each refusal was charged to the CHUNK, so seat 2295c6ce docs 1 and 2 were claimed at
# 01:03:16.9 and finalized TERMINAL 'partial' at 01:03:18.4: 400 chunks "processed" in 1.5
# seconds, chunks_failed=400, facts_committed=0. Two whole user documents destroyed by a
# container restart, permanently, with a healthy-looking API.
#
# A connect-level error says nothing about the chunk and everything about the backend — it
# will fail identically for every other chunk — so it belongs on the DEFERRAL path this lane
# already implements (Nygard, *Release It!* 2nd ed., "Circuit Breaker": work is retried
# after the remote end recovers, not discarded), not on the per-chunk failure path.


def test_connection_refused_defers_the_document_instead_of_burning_chunks(monkeypatch):
    """THE 1153-failure bug: ECONNREFUSED must defer the document, not fail its chunks."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)

    def _refused(*a, **k):
        raise embedder.httpx.ConnectError("[Errno 111] Connection refused")

    monkeypatch.setattr(embedder.httpx, "post", _refused)
    with pytest.raises(embedder.DocumentBrainUnavailable):
        embedder._document_chunk_edges("some chunk text", "u", "http://backend", "rewrite")


def test_a_refused_backend_re_pends_the_whole_document(monkeypatch):
    """End of the same path: the document goes back to 'pending', never terminal."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _StubResponse({}))

    def _edges(*a, **k):
        raise embedder.DocumentBrainUnavailable("backend unreachable")

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    conn = _FakeConn(["chunk one", "chunk two"])
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert _finalize_status(conn) is None, "a refused backend must not finalize the document"
    assert any("SET status = 'pending'" in sql for sql, _ in conn.statements), \
        "the document must be returned to 'pending' for a later cycle"


def test_read_timeout_is_still_a_chunk_failure_not_a_deferral(monkeypatch):
    """ZERO REGRESSION: a delivered-but-unanswered request stays chunk-scoped."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)

    def _slow(*a, **k):
        raise embedder.httpx.ReadTimeout("timed out")

    monkeypatch.setattr(embedder.httpx, "post", _slow)
    with pytest.raises(embedder.DocumentChunkExtractionFailed):
        embedder._document_chunk_edges("t", "u", "http://backend", "rewrite")


def test_unreachable_classifier_splits_connect_from_read(monkeypatch):
    """The whole fix hinges on this distinction — pin it directly."""
    assert embedder._is_backend_unreachable(embedder.httpx.ConnectError("refused"))
    assert embedder._is_backend_unreachable(embedder.httpx.ConnectTimeout("no route"))
    assert embedder._is_backend_unreachable(embedder.httpx.PoolTimeout("no conn"))
    assert embedder._is_backend_unreachable(ConnectionRefusedError(111, "refused"))
    assert not embedder._is_backend_unreachable(embedder.httpx.ReadTimeout("timed out"))
    assert not embedder._is_backend_unreachable(embedder.httpx.RemoteProtocolError("bad"))
    assert not embedder._is_backend_unreachable(ValueError("unrelated"))


def test_drain_claims_nothing_when_the_backend_is_not_listening(monkeypatch):
    """DO NOT CLAIM WHAT WE CANNOT PROCESS — the pre-claim probe (deploy-restart guard)."""
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", True)
    monkeypatch.setattr(embedder, "_backend_is_reachable", lambda _url: False)
    conn = _FakeConn(["chunk one"])
    n = embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert n == 0
    assert not any("SET status = 'processing'" in sql for sql, _ in conn.statements), \
        "nothing may be CLAIMED while the backend is unreachable"
    assert _finalize_status(conn) is None


def test_probe_disabled_restores_the_legacy_claim(monkeypatch):
    """FLAG OFF: byte-identical legacy behaviour — claim first, discover the outage later."""
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder, "_backend_is_reachable", lambda _url: False)
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _StubResponse({}))
    monkeypatch.setattr(embedder, "_document_chunk_edges", lambda *a, **k: [])
    conn = _FakeConn(["chunk one"])
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert _finalize_status(conn) == "ready"


def test_backend_reachable_probe_is_fail_safe_closed(monkeypatch):
    """Any probe error means 'do not claim' — a delayed document is always recoverable."""
    def _boom(*a, **k):
        raise embedder.httpx.ConnectError("refused")

    monkeypatch.setattr(embedder.httpx, "get", _boom)
    assert _REAL_BACKEND_PROBE("http://backend") is False


# ── (C) DOCDRAIN — the /ingest client timeout was BELOW the server's worst case ──
#
# MEASURED 2026-07-31, seat 8162c435 doc_id=9 (2 chunks): chunk 0's POST /ingest was aborted
# client-side by a bare `timeout=30.0` literal; its six facts landed in `facts` ~30s later.
# The work SUCCEEDED and we recorded a failure — facts_committed said 4 of 10 and the
# document terminated TERMINAL 'partial' with every fact actually in memory.


def test_ingest_timeout_is_env_tunable_and_above_the_server_worst_case():
    """Never a bare literal, and it must clear the backend's own per-op LLM budget (45s)."""
    assert embedder._DOC_INGEST_HTTP_TIMEOUT >= 60.0


def test_ingest_uses_the_configured_timeout_not_a_literal(monkeypatch):
    """Pin the actual call: the 30.0 literal is what aborted committed work.

    RE-TRIAGED 2026-08-21 (fix/doc-lane-fairness): un-quarantined — fixture drift (stub
    signature missing headers=), not a product regression.
    """
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder, "_DOC_INGEST_HTTP_TIMEOUT", 180.0)
    seen = []

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        seen.append((url, timeout))
        return _StubResponse({"committed": 1, "staged": 0})

    monkeypatch.setattr(embedder.httpx, "post", _post)
    monkeypatch.setattr(embedder, "_document_chunk_edges",
                        lambda *a, **k: [{"subject": "a", "rel_type": "r", "object": "b"}])
    conn = _FakeConn(["one chunk"])
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert ("http://backend/ingest", 180.0) in seen


def test_ingest_timeout_is_reported_as_an_UNKNOWN_outcome(monkeypatch):
    """It is not a clean failure: the edges were DELIVERED and may have committed."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    crits = []
    monkeypatch.setattr(embedder, "_doc_log_crit",
                        lambda event, **f: crits.append((event, f)))

    def _post(url, json=None, timeout=None):  # noqa: A002 - mirrors httpx
        if url.endswith("/ingest"):
            raise embedder.httpx.ReadTimeout("timed out")
        return _StubResponse({})

    monkeypatch.setattr(embedder.httpx, "post", _post)
    monkeypatch.setattr(embedder, "_document_chunk_edges",
                        lambda *a, **k: [{"subject": "a", "rel_type": "r", "object": "b"}])
    conn = _FakeConn(["one chunk"])
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert any(e == "re_embedder.document_ingest_outcome_unknown" for e, _ in crits), \
        "an unknown ingest outcome must fail LOUD, not read as a bare 'timed out'"
    assert _finalize_status(conn) == "partial"


def test_ingest_connection_refused_defers_rather_than_failing_the_chunk(monkeypatch):
    """Same rule one door over: an unreachable backend at /ingest defers the document.

    RE-TRIAGED 2026-08-21 (fix/doc-lane-fairness): un-quarantined — fixture drift (stub
    signature missing headers=), not a product regression.
    """
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        if url.endswith("/ingest"):
            raise embedder.httpx.ConnectError("[Errno 111] Connection refused")
        return _StubResponse({})

    monkeypatch.setattr(embedder.httpx, "post", _post)
    monkeypatch.setattr(embedder, "_document_chunk_edges",
                        lambda *a, **k: [{"subject": "a", "rel_type": "r", "object": "b"}])
    conn = _FakeConn(["one chunk"])
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert _finalize_status(conn) is None
    assert any("SET status = 'pending'" in sql for sql, _ in conn.statements)
