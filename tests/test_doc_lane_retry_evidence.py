"""DOC-LANE evidence persistence + chunk-level retry — the market-parity pins.

LlamaParse ships per-page error taxonomy; Unstructured ships failed-document retry;
LlamaIndex's RAG failure-mode checklist treats chunk-failure surfacing as table stakes.
FaultLine's lane used to persist COUNTS ONLY and discard the evidence (measured on
production docs 3/4/7, Aug-8 and Aug-21: error='', chunk_state='{}' — the reasons were
unrecoverable and the failed chunks unidentifiable). These tests pin the parity:

  • the per-chunk REASON CODE survives extraction → documents.chunk_state (both lanes);
  • the retry lane re-owes ONLY failed/never-reached chunks (done chunks are kept);
  • retry is IDEMPOTENT under double-fire (atomic status-guarded transition);
  • the recall footer NAMES the retriable documents and points at the retry lane.
"""

from __future__ import annotations

import json

import pytest

import src.re_embedder.embedder as embedder

# pyproject sets asyncio_mode=auto: plain `async def test_*` run without explicit marks.


# ── reason threading: the 4-tuple ────────────────────────────────────────────


class _R:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"committed": 1, "staged": 0}


def test_chunk_failure_reason_escapes_in_the_return_tuple(monkeypatch):
    """The drain's ledger can only persist what _process_document_chunk RETURNS — the
    DOCLOSS-B reason code (extractor status / transport class) must ride the 4-tuple,
    not collapse into a bare failed=1 (the Aug-8 evidence loss)."""
    def _edges(text, *a, **k):
        raise embedder.DocumentChunkExtractionFailed("empty_llm_response (1/2 sub-chunks unread)")

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    out = embedder._process_document_chunk(
        "one chunk", idx=0, doc_id=7, user_id="u", backend_url="http://b",
        source_ref="ref")
    assert out[2] == 1, "the chunk counts failed"
    assert out[3] and "empty_llm_response" in out[3], (
        f"reason code must escape for persistence — got {out[3]!r}"
    )


def test_chunk_success_returns_none_reason(monkeypatch):
    monkeypatch.setattr(embedder, "_document_chunk_edges", lambda *a, **k: [
        {"subject": "a", "rel_type": "r", "object": "b"}])
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _R())
    out = embedder._process_document_chunk(
        "one chunk", idx=0, doc_id=7, user_id="u", backend_url="http://b",
        source_ref="ref")
    assert out == (1, 0, 0, None)


# ── legacy-lane ledger write at finalize ─────────────────────────────────────


class _LedgerCursor:
    """Fake cursor for the LEGACY drain: capability probes say chunk_state EXISTS
    (migration 205) so the terminal finalize must carry the ledger."""

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
            return ([c for c in self._conn.chunks], "ref", 1)
        if "column_name = 'chunk_state'" in sql:
            return (1,)  # migration 205 present
        return None


class _LedgerConn:
    def __init__(self, chunks):
        self.chunks = chunks
        self.statements = []

    def cursor(self):
        return _LedgerCursor(self)

    def commit(self):
        return None

    def rollback(self):
        return None


def _terminal_ledger(conn):
    """The chunk_state JSONB argument of the terminal finalize UPDATE, if present."""
    for sql, params in conn.statements:
        if "SET status = %s, ready_at = now()" in sql and "chunk_state = %s::jsonb" in sql:
            for p in (params or ()):
                if isinstance(p, str) and p.startswith("{"):
                    return json.loads(p)
    return None


def test_legacy_lane_persists_the_per_chunk_ledger_with_reasons(monkeypatch):
    """THE AUG-8 FIX: the legacy ThreadPool lane must write the SAME per-chunk ledger the
    queue lane writes — WHICH chunks failed and WHY — not counts only (chunk_state='{}')."""
    monkeypatch.setattr(embedder, "_backend_is_reachable", lambda _url: True)
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _R())

    def _edges(text, *a, **k):
        if "bad" in text:
            raise embedder.DocumentChunkExtractionFailed("empty_llm_response")
        return [{"subject": "a", "rel_type": "r", "object": "b"}]

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    conn = _LedgerConn(["good one", "bad one"])
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    ledger = _terminal_ledger(conn)
    assert ledger is not None, (
        "legacy finalize wrote NO chunk_state ledger — per-chunk evidence discarded "
        "(the production docs 3/4/7 shape)"
    )
    assert ledger.get("0", {}).get("s") == "done" and ledger["0"]["c"] == 1
    assert ledger.get("1", {}).get("s") == "failed"
    assert "empty_llm_response" in ledger["1"].get("e", ""), (
        f"failed chunk must carry its reason code: {ledger.get('1')!r}"
    )


def test_legacy_lane_without_chunk_state_column_still_finalizes(monkeypatch):
    """Pre-migration-205 schema: no ledger write, but the counts + terminal status must
    still land (the capability probe must not break the finalize)."""
    monkeypatch.setattr(embedder, "_backend_is_reachable", lambda _url: True)
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: _R())
    monkeypatch.setattr(embedder, "_document_chunk_edges",
                        lambda *a, **k: [{"subject": "a", "rel_type": "r", "object": "b"}])
    conn = _LedgerConn(["good one"])
    # force the probe OFF (pre-205)
    _orig_fetchone = _LedgerCursor.fetchone

    def _no_chunkstate(self):
        sql = (self._last or ("", None))[0]
        if "column_name = 'chunk_state'" in sql:
            return None
        return _orig_fetchone(self)

    _LedgerCursor.fetchone = _no_chunkstate
    try:
        embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    finally:
        del _LedgerCursor.fetchone
    assert _terminal_ledger(conn) is None, "no chunk_state column → no ledger write"
    for sql, params in conn.statements:
        if "SET status = %s, ready_at = now()" in sql:
            assert params[0] == "ready"


# ── /documents/retry — idempotent, keeps done chunks ─────────────────────────


class _RetryCursor:
    def __init__(self, conn):
        self._conn = conn
        self._last = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.statements.append((" ".join(sql.split()), params))
        self._last = (sql, params)
        if sql.strip().startswith("UPDATE documents"):
            self._conn.flips += 1
            self.rowcount = 1

    def fetchall(self):
        sql = (self._last or ("", None))[0]
        if "status IN ('partial', 'error')" in sql and "chunks" in sql:
            # the candidate SELECT: return the terminal rows the conn was seeded with
            return [(d["id"], d["chunks"], d.get("chunk_state", {}))
                    for d in self._conn.rows if d["status"] in ("partial", "error")]
        if "SELECT id, status FROM documents" in sql:
            return [(d["id"], d["status"]) for d in self._conn.rows]
        return []

    def fetchone(self):
        return None


class _RetryConn:
    """Seeded documents registry; `flips` counts terminal→pending UPDATEs (the atomic
    transition — the idempotency mechanism under double-fire)."""

    def __init__(self, rows):
        self.rows = rows
        self.flips = 0
        self.statements = []

    def cursor(self):
        return _RetryCursor(self)

    def commit(self):
        # apply the flip to the seeded state, as real Postgres would
        for d in self.rows:
            if d["status"] in ("partial", "error"):
                d["status"] = "pending"
        return None

    def rollback(self):
        return None


@pytest.fixture
def _retry_db(monkeypatch):
    from src.api import main as api_main

    # ONE shared registry across every connect() in the test — real state persists across
    # the two calls of a double-fire, so the fake must too.
    conn = _RetryConn([{
        "id": 3, "status": "partial",
        "chunks": ["c0", "c1", "c2", "c3"],
        "chunk_state": {"0": {"s": "done", "c": 2, "g": 0},
                        "1": {"s": "failed", "e": "empty_llm_response", "a": 3}},
    }])

    def _connect(_dsn):
        return conn

    monkeypatch.setattr(api_main.psycopg2, "connect", _connect)
    monkeypatch.setattr(api_main, "_derive_tenant_schema",
                        lambda uid: f"faultline_{uid[:8]}")
    _retry_db.conn = conn
    return _retry_db


async def test_retry_re_owes_only_failed_chunks(_retry_db):
    from src.api import main as api_main

    out = api_main.documents_retry(
        api_main.DocumentRetryRequest(user_id="00000000-0000-4000-8000-0000000000aa",
                                      document_id=3))
    assert out["status"] == "retry_queued" and out["retried"] == 1
    doc = out["documents"][0]
    # chunk 0 done kept; chunks 1 (failed), 2 & 3 (never reached) re-owed
    assert doc["chunks_kept"] == 1 and doc["chunks_owed"] == 3
    # the ledger written back keeps ONLY the done entries (the retry contract)
    sql_params = [p for sql, p in _retry_db.conn.statements
                  if "chunk_state = %s::jsonb" in sql and "status = 'pending'" in sql]
    assert sql_params, "retry must persist the kept-ledger"
    kept = json.loads(sql_params[0][0])
    assert kept == {"0": {"s": "done", "c": 2, "g": 0}}, (
        f"kept ledger must hold ONLY done entries — got {kept!r}"
    )


async def test_retry_double_fire_is_idempotent(_retry_db):
    """THE DOUBLE-FIRE PIN: firing retry twice must not re-pend twice — the second call
    sees the document already pending/processing and reports already_active (0 flips)."""
    from src.api import main as api_main

    req = api_main.DocumentRetryRequest(user_id="00000000-0000-4000-8000-0000000000aa",
                                        document_id=3)
    first = api_main.documents_retry(req)
    flips_after_first = _retry_db.conn.flips
    assert first["status"] == "retry_queued"

    second = api_main.documents_retry(req)
    assert second["status"] == "already_active", (
        f"double-fire must report already_active — got {second['status']!r}"
    )
    assert _retry_db.conn.flips == flips_after_first, (
        "double-fire performed a SECOND terminal→pending transition"
    )


async def test_retry_with_nothing_failed_reports_nothing_to_retry(_retry_db):
    from src.api import main as api_main

    _retry_db.conn.rows = [{"id": 9, "status": "ready", "chunks": ["c0"],
                            "chunk_state": {}}]
    out = api_main.documents_retry(
        api_main.DocumentRetryRequest(user_id="00000000-0000-4000-8000-0000000000aa",
                                      document_id=9))
    assert out["status"] == "nothing_to_retry" and out["retried"] == 0


# ── recall footer names retriable docs ───────────────────────────────────────


class _FooterResp:
    status_code = 200

    def json(self):
        return {
            "pending": False, "count": 0, "chunks_remaining": 0, "eta_seconds": 0,
            "failed": 2, "failed_chunks": 6,
            "failed_docs": [
                {"id": 3, "title": "opencode operational process", "source_ref": None,
                 "chunks_failed": 1, "retriable": True},
                {"id": 7, "title": None, "source_ref": "https://example.com/guide",
                 "chunks_failed": 5, "retriable": True},
            ],
        }


async def test_footer_names_retriable_documents_and_the_retry_lane(monkeypatch):
    from src.mcp import server as mcp_server

    class _Cli:
        async def get(self, *a, **k):
            return _FooterResp()

    monkeypatch.setattr(mcp_server, "_http_client", _Cli())
    notice = await mcp_server._pending_documents_notice("u")
    assert notice is not None
    assert "did NOT finish importing" in notice
    assert "opencode operational process" in notice, "footer must NAME the retriable doc"
    assert "example.com/guide" in notice, "source_ref names the untitled doc"
    assert "retry_document" in notice, "footer must point at the retry lane"


async def test_footer_without_failed_docs_is_unchanged_shape(monkeypatch):
    from src.mcp import server as mcp_server

    class _Quiet:
        def json(self):
            return {"pending": True, "count": 1, "chunks_remaining": 4,
                    "eta_seconds": 60, "failed": 0, "failed_chunks": 0,
                    "failed_docs": []}

        status_code = 200

    class _Cli:
        async def get(self, *a, **k):
            return _Quiet()

    monkeypatch.setattr(mcp_server, "_http_client", _Cli())
    notice = await mcp_server._pending_documents_notice("u")
    assert notice is not None and "still importing" in notice
    assert "did NOT finish" not in notice
