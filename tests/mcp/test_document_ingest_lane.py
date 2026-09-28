"""DOCLANE — async document-ingestion lane (migration 183 `documents` registry).

Covers the four pieces the flagship lane hangs on:
  * Registry lifecycle (REAL cursor): pending → processing → ready via the exact
    SQL the enqueue endpoint + worker use, plus the recall pending-probe SELECT.
  * Document-lane routing: the WHOLE chunk goes to the tenant-brain LLM
    (/extract/rewrite) as the PRIMARY extractor — the spine is NOT trusted for dense
    domain prose it mis-parses, so its junk (non-zero mis-POS edges) can never be kept
    (_document_chunk_edges is LLM-only for documents).
  * Not-ready gating: recall prepends an honest "still processing" line WITHOUT
    blocking already-ready facts, and fail-safe when nothing is pending.
  * No-regression: ingest_document still enqueues fast (and falls back to the
    synchronous path when the registry is unreachable); remember_facts untouched.

Network is mocked; the ONE DB test uses a real Postgres cursor (skips if
unreachable). Deterministic, no live LLM.
"""

import os
import sys
import types
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.re_embedder.embedder as emb
import src.mcp.server as server
from src.mcp.server import ingest_document_tool, recall_memory_tool, _pending_documents_notice


BIO = (
    "Photosynthesis converts carbon dioxide and water into glucose and oxygen. "
    "The Calvin cycle occurs in the stroma of the chloroplast. "
    "Chlorophyll absorbs light most strongly in the blue and red wavelengths."
)


def _resp(json_obj, status=200):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.status_code = status
    r.json.return_value = json_obj
    return r


@pytest.fixture(autouse=True)
def _backend_up(monkeypatch):
    """DOCDRAIN: the drain now probes the backend before claiming (it must never burn a
    document against a backend that is not listening — see DOC_BACKEND_READY_PROBE). The
    tests here replace `emb.httpx` with a post-only double, so stand the probe up
    explicitly rather than letting a missing `.get` read as an outage."""
    monkeypatch.setattr(emb, "_backend_is_reachable", lambda _url: True)


# ── Document-lane routing (LLM-primary, spine NOT trusted for domain prose) ───

def test_chunk_edges_falls_back_to_the_llm_with_the_whole_chunk(monkeypatch):
    """The LLM extractor receives the WHOLE chunk (never a residual slice), and every
    fact in it lands.

    CONTRACT UPDATED (DOCDRAIN, 2026-07-31). This test was named
    `..._is_llm_primary_never_calls_spine` and asserted `harvest == 0`. That contract was
    DELIBERATELY REVERSED earlier the same day by DOC_CHUNK_SPINE_FIRST (default ON): the
    document lane was the only ingest path that never tried the deterministic extractor, so
    it now calls /harvest-spans first and falls back to /extract/rewrite when the spine
    yields nothing. The test was left pinning the retired behaviour and was RED on HEAD.

    What still matters — and is what this test now pins — is the part the original was
    really protecting: when the LLM IS used it gets the WHOLE chunk, not the old
    residual-only slice that let mis-parsed spine junk survive while real facts were lost
    ('Photosynthesis converts CO2 and water into glucose and oxygen' → the residual hybrid
    kept (dioxide, use, light energy) and never captured glucose/oxygen).
    """
    calls = {"harvest": 0, "rewrite": 0, "rewrite_text": None,
             "harvest_lane": None, "rewrite_lane": None}

    def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/harvest-spans"):
            calls["harvest"] += 1
            calls["harvest_lane"] = {k.lower(): v for k, v in (headers or {}).items()}.get("x-fl-llm-lane")
            return _resp({"edges": []})       # spine finds nothing → fall back
        if url.endswith("/extract/rewrite"):
            calls["rewrite"] += 1
            calls["rewrite_text"] = json["text"]
            calls["rewrite_lane"] = {k.lower(): v for k, v in (headers or {}).items()}.get("x-fl-llm-lane")
            return _resp({"edges": [
                {"subject": "photosynthesis", "rel_type": "produces", "object": "glucose"},
                {"subject": "photosynthesis", "rel_type": "produces", "object": "oxygen"},
                {"subject": "calvin cycle", "rel_type": "located_in", "object": "stroma"},
                {"subject": "chlorophyll", "rel_type": "absorbs", "object": "light"},
            ]})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(emb, "httpx", types.SimpleNamespace(post=fake_post))
    # Pass 'spine' route to prove the lane's behaviour does not hinge on the brain's
    # STATEMENT route — the document lane makes its own extractor decision.
    edges = emb._document_chunk_edges(BIO, "u1", "http://backend", "spine")

    assert calls["harvest"] == 1            # spine tried first (DOC_CHUNK_SPINE_FIRST)
    assert calls["rewrite"] == 1            # then the LLM, because the spine found nothing
    assert calls["rewrite_text"] == BIO     # the WHOLE chunk, not a residual slice
    # dprompt-155: the sweep's LLM-bearing posts carry the BACKGROUND lane across the
    # HTTP boundary — without the header the API runs them INTERACTIVE (fail-open).
    assert calls["harvest_lane"] == "background"
    assert calls["rewrite_lane"] == "background"
    subjects = {e["subject"] for e in edges}
    assert subjects == {"photosynthesis", "calvin cycle", "chlorophyll"}   # all facts land
    # No spine → no (dioxide/water, use, light energy) junk can ever survive.
    assert not any(e["subject"] in ("carbon dioxide", "dioxide", "water") for e in edges)


def test_chunk_edges_rewrite_route_sends_whole_chunk(monkeypatch):
    """route=rewrite: the whole chunk goes to the LLM extractor after the spine comes up
    empty (contract updated with DOC_CHUNK_SPINE_FIRST — see the test above)."""
    calls = {"harvest": 0, "rewrite_text": None}

    def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/harvest-spans"):
            calls["harvest"] += 1
            return _resp({"edges": []})
        if url.endswith("/extract/rewrite"):
            calls["rewrite_text"] = json["text"]
            return _resp({"edges": [{"subject": "photosynthesis", "rel_type": "produces", "object": "glucose"}]})
        raise AssertionError(url)

    monkeypatch.setattr(emb, "httpx", types.SimpleNamespace(post=fake_post))
    edges = emb._document_chunk_edges(BIO, "u1", "http://backend", "rewrite")
    assert calls["harvest"] == 1            # spine tried first, found nothing
    assert calls["rewrite_text"] == BIO     # whole chunk
    assert len(edges) == 1


def test_chunk_edges_drops_low_confidence(monkeypatch):
    def fake_post(url, json=None, timeout=None, headers=None):
        return _resp({"edges": [
            {"subject": "a", "rel_type": "r", "object": "b"},
            {"subject": "c", "rel_type": "r", "object": "d", "low_confidence": True},
        ]})
    monkeypatch.setattr(emb, "httpx", types.SimpleNamespace(post=fake_post))
    edges = emb._document_chunk_edges("A r b. C r d.", "u1", "http://backend", "rewrite")
    assert len(edges) == 1 and edges[0]["subject"] == "a"


def test_chunk_edges_freeze_raises(monkeypatch):
    """A frozen backend (ingest_disabled) must RAISE so the worker defers, not drop."""
    def fake_post(url, json=None, timeout=None, headers=None):
        return _resp({"status": "ingest_disabled"})
    monkeypatch.setattr(emb, "httpx", types.SimpleNamespace(post=fake_post))
    with pytest.raises(RuntimeError):
        emb._document_chunk_edges(BIO, "u1", "http://backend", "spine")


# ── ingest_document tool: enqueue-fast + fail-safe sync fallback ─────────────

@pytest.mark.asyncio
async def test_ingest_document_enqueues_and_returns_fast(monkeypatch):
    """The tool must return status=pending with a doc id + ETA and do NO extraction."""
    posted = []

    async def fake_post(url, json=None, timeout=None, headers=None):
        posted.append(url)
        if url.endswith("/documents/enqueue"):
            return _resp({"status": "pending", "document_id": 7,
                          "chunk_count": json["chunk_count"], "eta_seconds": 35})
        raise AssertionError(f"unexpected extraction call: {url}")

    monkeypatch.setattr(server, "_http_client", MagicMock(post=AsyncMock(side_effect=fake_post)))
    out = await ingest_document_tool(BIO, "u-alice", source_ref="bio.txt")

    assert out["status"] == "pending"
    assert out["document_id"] == 7
    assert out["chunks"] >= 1
    assert out["eta_seconds"] == 35
    assert "processed" in out["message"].lower()
    # FAST: only the enqueue was called — no /harvest-spans, /extract/rewrite, /ingest.
    assert posted == [f"{server.FAULTLINE_API_URL}/documents/enqueue"]


@pytest.mark.asyncio
async def test_ingest_document_falls_back_to_sync_when_registry_soft_fails(monkeypatch):
    """Registry unreachable / pre-migration → synchronous per-chunk path (never drop)."""
    async def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/documents/enqueue"):
            return _resp({"status": "soft_error", "document_id": None})
        if url.endswith("/extract/rewrite"):
            return _resp({"edges": [{"subject": "photosynthesis", "rel_type": "produces", "object": "glucose"}]})
        if url.endswith("/ingest"):
            return _resp({"committed": 1, "staged": 0})
        if url.endswith("/episodic/append"):
            return _resp({"status": "stored", "id": 1})
        raise AssertionError(url)

    async def fake_get(url, timeout=None):
        # _statement_extractor_route → rewrite (no spine in fallback for this test)
        return _resp({"statement_extractor": "rewrite"})

    monkeypatch.setattr(server, "_http_client",
                        MagicMock(post=AsyncMock(side_effect=fake_post), get=AsyncMock(side_effect=fake_get)))
    # Keep the fallback lean: skip the Class-C remainder store.
    monkeypatch.setattr(server, "SHORT_TERM_MEMORY", False)
    out = await ingest_document_tool(BIO, "u-alice")

    assert out["status"] in ("ok", "partial")
    assert out["facts_committed"] >= 1


# ── Recall not-ready UX ──────────────────────────────────────────────────────

def _recall_mocks(query_json, pending_json):
    """Build a post/get side-effect pair for a recall turn with a document possibly pending."""
    async def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/classify-intent"):
            return _resp({"intent": "QUERY", "confidence": 0.9})
        if url.endswith("/harvest-spans"):
            return _resp({"edges": []})
        if url.endswith("/query"):
            return _resp(query_json)
        raise AssertionError(url)

    async def fake_get(url, params=None, timeout=None):
        if "/confidence-gate/" in url:
            return _resp({"threshold": 0.70})
        if url.endswith("/documents/pending"):
            return _resp(pending_json)
        raise AssertionError(url)

    return AsyncMock(side_effect=fake_post), AsyncMock(side_effect=fake_get)


@pytest.mark.asyncio
async def test_recall_not_ready_status_is_out_of_band_not_memory(monkeypatch):
    # THE HARD LINE: the drain banner is NOT a recalled memory. It must NOT be prepended
    # into (or woven through) the asserted-memory prose — it is fenced under its own
    # "STATUS (not memory)" label, AFTER the ready facts, and framed as background.
    post, get = _recall_mocks(
        query_json={"facts": [{"definition": "Your dog is Rex.", "fact_class": "A",
                               "fact_provenance": "user_stated", "confidence": 1.0}],
                    "attributes": {}},
        pending_json={"pending": True, "count": 1, "chunks_remaining": 3, "eta_seconds": 30},
    )
    monkeypatch.setattr(server, "_http_client", MagicMock(post=post, get=get))
    out = await recall_memory_tool("what pets do I have", "u-alice")
    mem = out["memory"]
    # The memory prose leads with the real fact — the status never prepends to it.
    assert mem.startswith("Your dog is Rex.")
    assert "Rex" in mem                                            # ready fact NOT blocked
    # The banner is present but OUT OF BAND: fenced + background-framed, never memory voice.
    assert "=== STATUS (not memory) ===" in mem
    assert "background: still importing 1 document" in mem
    assert "30 seconds" in mem
    # The ready fact must appear BEFORE the status fence (status is appended, not prepended).
    assert mem.index("Rex") < mem.index("=== STATUS (not memory) ===")
    # No memory-voice framing of the drain leaks in.
    assert "I'm still processing" not in mem


@pytest.mark.asyncio
async def test_recall_empty_walk_with_pending_is_status_only_not_memory(monkeypatch):
    post, get = _recall_mocks(
        query_json={"facts": [], "attributes": {}},
        pending_json={"pending": True, "count": 2, "chunks_remaining": 8, "eta_seconds": 60},
    )
    monkeypatch.setattr(server, "_http_client", MagicMock(post=post, get=get))
    out = await recall_memory_tool("tell me about the manual", "u-alice")
    mem = out["memory"]
    # Empty walk + pending → the notice surfaces ALONE, but still under the status label
    # (never as a bare asserted-memory string) and background-framed.
    assert "=== STATUS (not memory) ===" in mem
    assert "still importing 2 documents" in mem
    assert "I'm still processing" not in mem
    assert mem != "No relevant facts found."


@pytest.mark.asyncio
async def test_recall_no_notice_when_nothing_pending(monkeypatch):
    post, get = _recall_mocks(
        query_json={"facts": [], "attributes": {}},
        pending_json={"pending": False},
    )
    monkeypatch.setattr(server, "_http_client", MagicMock(post=post, get=get))
    out = await recall_memory_tool("anything", "u-alice")
    assert "don't have any information" in out["memory"]  # empty recall → honest abstention


@pytest.mark.asyncio
async def test_pending_notice_failsafe(monkeypatch):
    # Probe raises → None (recall never breaks).
    monkeypatch.setattr(server, "_http_client",
                        MagicMock(get=AsyncMock(side_effect=Exception("down"))))
    assert await _pending_documents_notice("u1") is None
    # Non-200 → None.
    monkeypatch.setattr(server, "_http_client",
                        MagicMock(get=AsyncMock(return_value=_resp({}, status=503))))
    assert await _pending_documents_notice("u1") is None


# ── Registry lifecycle on a REAL Postgres cursor ─────────────────────────────

def _dsn():
    return os.environ.get(
        "DOCLANE_TEST_DSN",
        "postgresql://faultline:faultline@172.20.0.2:5432/faultline",
    )


@pytest.fixture
def real_schema():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(_dsn(), connect_timeout=5)
    except Exception as e:
        pytest.skip(f"local Postgres unreachable: {e}")
    schema = f"faultline_doclane_it_{uuid.uuid4().hex[:8]}"
    ddl = (
        f"CREATE SCHEMA {schema};"
        f"SET search_path TO {schema};"
        "CREATE TABLE documents ("
        "  id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, source_ref TEXT, title TEXT,"
        "  chunks JSONB NOT NULL DEFAULT '[]'::jsonb, chunk_count INTEGER NOT NULL DEFAULT 0,"
        "  status TEXT NOT NULL DEFAULT 'pending', chunks_failed INTEGER NOT NULL DEFAULT 0,"
        "  facts_committed INTEGER NOT NULL DEFAULT 0, facts_staged INTEGER NOT NULL DEFAULT 0,"
        "  context_stored INTEGER NOT NULL DEFAULT 0, truncated BOOLEAN NOT NULL DEFAULT false,"
        "  error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        "  started_at TIMESTAMPTZ, ready_at TIMESTAMPTZ);"
        "CREATE INDEX idx_documents_active ON documents (created_at) "
        "  WHERE status IN ('pending','processing');"
    )
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    yield conn, schema
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        conn.commit()
    finally:
        conn.close()


# ── Fast doc-drain PRE-PASS ordering (latency fix) ───────────────────────────

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.upper().startswith("SET SEARCH_PATH TO "):
            self.conn.cur_schema = s.split()[-1]
        elif "FROM documents WHERE status = 'pending'" in s:
            self.conn.probed.append(self.conn.cur_schema)
            self.conn.probe_hit = self.conn.pending.get(self.conn.cur_schema, False)

    def fetchone(self):
        return (1,) if self.conn.probe_hit else None


class _FakeConn:
    def __init__(self, pending):
        self.pending = pending          # schema -> bool (has a pending doc)
        self.cur_schema = None
        self.probe_hit = False
        self.probed = []                # schemas probed, in order
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True






# ── Conservative ETA (under-promise / over-deliver, derived from poll cadence) ─

def test_doc_eta_is_conservative_and_scales_with_chunks():
    """ETA is derived from the poll interval + per-chunk extraction, never under-shoots
    the pickup delay, and grows with chunk count (rounded UP to 5s)."""
    from src.api import main as _m

    poll = _m._DOC_ETA_POLL_INTERVAL
    per = _m._DOC_ETA_PER_CHUNK_SECONDS
    margin = _m._DOC_ETA_STARTUP_MARGIN

    # Floor: even a 0-chunk doc waits ~one poll interval to be claimed.
    assert _m._doc_eta_seconds(0) >= poll
    # Monotonic in chunk count, and strictly larger once chunks add real work.
    assert _m._doc_eta_seconds(5) > _m._doc_eta_seconds(0)
    assert _m._doc_eta_seconds(10) > _m._doc_eta_seconds(3)
    # Multiple of 5 (rounded UP) and never below the exact derived cost.
    for n in (0, 1, 3, 8, 20):
        eta = _m._doc_eta_seconds(n)
        assert eta % 5 == 0
        assert eta >= poll + margin + n * per - 4   # rounding never drops below raw-4
        assert eta >= poll                          # always >= one poll interval
    # Over-delivers vs the drain's realistic cost: for a 3-chunk doc the worker's own
    # extraction is ~3*per seconds; the promise is comfortably larger (interval grace).
    assert _m._doc_eta_seconds(3) >= poll + 3 * per


def test_registry_lifecycle_real_cursor(real_schema, monkeypatch):
    """pending → processing → ready through the WORKER, hybrid extraction stubbed —
    proving the exact claim/finalize SQL against a real Postgres cursor."""
    conn, schema = real_schema
    import json as _json
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        cur.execute(
            "INSERT INTO documents (user_id, source_ref, chunks, chunk_count, status) "
            "VALUES (%s, %s, %s::jsonb, %s, 'pending') RETURNING id",
            ("u-real", "bio.txt", _json.dumps([BIO]), 1),
        )
        doc_id = cur.fetchone()[0]
    conn.commit()

    # Pending-probe (the recall not-ready SELECT) sees it.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), COALESCE(sum(chunk_count),0) FROM documents "
                    "WHERE status IN ('pending','processing')")
        assert cur.fetchone() == (1, 1)

    # Stub the backend extraction so the worker's REAL SQL drives the lifecycle.
    def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/episodic/append"):
            return _resp({"status": "stored", "id": 1})
        if url.endswith("/harvest-spans"):
            return _resp({"edges": []})
        if url.endswith("/extract/rewrite"):
            return _resp({"edges": [{"subject": "photosynthesis", "rel_type": "produces", "object": "glucose"}]})
        if url.endswith("/ingest"):
            return _resp({"committed": 1, "staged": 0})
        raise AssertionError(url)
    monkeypatch.setattr(emb, "httpx", types.SimpleNamespace(post=fake_post))

    finalized = emb.drain_pending_documents(conn, "http://backend", "u-real",
                                            schema_name=schema, statement_route="rewrite")
    assert finalized == 1

    with conn.cursor() as cur:
        cur.execute("SELECT status, facts_committed, chunks_failed, ready_at, started_at "
                    "FROM documents WHERE id=%s", (doc_id,))
        status, committed, failed, ready_at, started_at = cur.fetchone()
    assert status == "ready"
    assert committed == 1
    assert failed == 0
    assert ready_at is not None and started_at is not None

    # Now nothing is pending → the recall probe goes quiet.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents WHERE status IN ('pending','processing')")
        assert cur.fetchone()[0] == 0


# ── DOCDRAIN — a FAILED import must be visible to the user ───────────────────
#
# Until this change, /documents/pending counted ONLY ('pending','processing'). A document
# that terminated 'partial'/'error' with unread chunks simply stopped being mentioned: the
# "still importing" line disappeared, nothing said "and it did not land", and the only way
# a user could learn their 200-page PDF stored nothing was to ask about it and be told
# "I don't have any information about that". Silence on a write path that ate a corpus is
# the worst failure available to this system, so the failure now gets its own out-of-band
# line — background-framed, never memory voice (THE HARD LINE).


@pytest.mark.asyncio
async def test_failed_document_import_is_reported_to_the_user(monkeypatch):
    post, get = _recall_mocks(
        query_json={"facts": [], "attributes": {}},
        pending_json={"pending": False, "count": 0, "chunks_remaining": 0,
                      "eta_seconds": 0, "failed": 2, "failed_chunks": 400},
    )
    monkeypatch.setattr(server, "_http_client", MagicMock(post=post, get=get))
    out = await recall_memory_tool("what did that report say", "u-alice")
    mem = out["memory"]
    assert "did NOT finish importing" in mem, \
        "a document that ate the user's content must never fail silently"
    assert "400" in mem and "not in memory" in mem
    assert "=== STATUS (not memory) ===" in mem, "still out-of-band, never memory voice"


@pytest.mark.asyncio
async def test_no_failures_reports_exactly_what_it_did_before(monkeypatch):
    """ZERO REGRESSION: with failed=0 the notice is byte-identical to the legacy line."""
    post, get = _recall_mocks(
        query_json={"facts": [], "attributes": {}},
        pending_json={"pending": True, "count": 1, "chunks_remaining": 3,
                      "eta_seconds": 30, "failed": 0, "failed_chunks": 0},
    )
    monkeypatch.setattr(server, "_http_client", MagicMock(post=post, get=get))
    out = await recall_memory_tool("anything", "u-alice")
    mem = out["memory"]
    assert "still importing 1 document" in mem
    assert "did NOT finish importing" not in mem


@pytest.mark.asyncio
async def test_probe_without_the_new_keys_is_still_safe(monkeypatch):
    """An older backend answering the probe without failed/failed_chunks must not break."""
    post, get = _recall_mocks(
        query_json={"facts": [], "attributes": {}},
        pending_json={"pending": True, "count": 1, "chunks_remaining": 3, "eta_seconds": 30},
    )
    monkeypatch.setattr(server, "_http_client", MagicMock(post=post, get=get))
    out = await recall_memory_tool("anything", "u-alice")
    assert "still importing 1 document" in out["memory"]
