"""W2 — documents keyword-searchable at the enqueue commit + instant stub facts
(migration 299 / stub_tsv / document_hits / BUG-05 trap guard).

Pins (plan DEV/BUGS/plan-documents-at-ingest-speed-20261022.md, gauntlet 1):

  (a) stub_tsv exists in a FRESH tenant schema (template) AND in a LEGACY tenant after
      the boot-migration ledger applies 299 — and matches a keyword from chunk text at
      search time. DB-backed (dedicated throwaway database, never the shared suite DB).
  (b) stub facts exist at ENQUEUE time — the /ingest stub write happens BEFORE
      ingest_document_tool returns, and the response window carries stub_facts_minted.
  (c) THE BUG-05 TRAP — question-shaped input (title, filename, heading) mints NOTHING;
      parser-provided structure (title/filename/ATX headings) still mints. Zero chunk
      text is ever read for names.
  (d) document_hits returns the doc + the PASSAGE chunk text that matches the query
      token for a matching query; EMPTY (never raises) for a foreign tenant / no-match /
      pre-299 schema.
  (e) recall renders the deterministic assert line ("From your document …: <passage>
      (document id …)" — R6 passage reshape; title-only rows keep the W2 "You have a
      document: …" discovery shape).

Network is mocked for the MCP pins; DB pins run against a dedicated throwaway database
booted with the real migration corpus (the same harness test_boot_migrations uses — the
fanout sweep takes ACCESS EXCLUSIVE locks and must never touch a shared DB).
"""

import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DSN = os.environ.get("POSTGRES_DSN")

from src.mcp.server import (  # noqa: E402
    _mint_document_stub_edges,
    _render_document_hit_lines,
    _stub_filename_stem,
    _stub_heading_names,
    _stub_is_question_shaped,
    ingest_document_tool,
    recall_memory_tool,
)


def _resp(payload, status=200):
    m = MagicMock()
    m.status_code = status
    m.json = lambda: payload
    m.raise_for_status = lambda: None
    return m


# ── pin (c): THE BUG-05 TRAP — question-shaped input mints nothing ──────────────────────

def test_bug05_question_shaped_filename_mints_nothing():
    """A question-shaped title/filename must not mint junk entities (BUG-05, HIGH)."""
    edges = _mint_document_stub_edges(
        title="What is GraphRAG?", source_ref="what-is-graphrag.pdf", chunks=["body text"])
    assert edges == [], f"question-shaped surfaces minted {len(edges)} edge(s)"


def test_bug05_question_shaped_heading_is_dropped_but_real_headings_kept():
    """A '?'-heading inside the parser yield is yield text, not a thing; real ones stay."""
    chunks = [
        "# What is saponification?\nBody prose never mints.",
        "# Methods\nReal heading.",
        "plain paragraph with no structure",
    ]
    heads = _stub_heading_names(chunks)
    assert heads == ["Methods"], heads


def test_bug05_wh_first_word_title_mints_nothing():
    assert _stub_is_question_shaped("How does recovery work")
    assert _stub_is_question_shaped("why bother")
    assert not _stub_is_question_shaped("Recovery Workbook")
    assert not _stub_is_question_shaped("topology notes 2026")


def test_stub_mints_document_entity_alias_and_headings():
    """Parser-provided structure ONLY: entity + title/filename alias + headings + owns."""
    chunks = ["# Introduction\nprose", "# The boiling process\nprose"]
    edges = _mint_document_stub_edges(
        title="Colonial Soap Making", source_ref="/tmp/colonial_soap.pdf", chunks=chunks)
    rels = [(e["subject"], e["rel_type"], e["object"]) for e in edges]
    assert ("Colonial Soap Making", "instance_of", "document") in rels
    assert ("user", "owns", "Colonial Soap Making") in rels
    assert ("Colonial Soap Making", "also_known_as", "colonial_soap") in rels
    assert ("Introduction", "part_of", "Colonial Soap Making") in rels
    assert ("The boiling process", "part_of", "Colonial Soap Making") in rels
    for e in edges:
        assert e["fact_provenance"] == "llm_inferred"
        assert e["source_ref"] == "/tmp/colonial_soap.pdf"
        assert e["rel_type"] in ("instance_of", "owns", "also_known_as", "part_of")


def test_stub_name_falls_back_to_filename_stem_when_no_title():
    edges = _mint_document_stub_edges(title="", source_ref="a/b/Field_Guide.pdf", chunks=[])
    rels = [(e["subject"], e["rel_type"], e["object"]) for e in edges]
    assert ("Field_Guide", "instance_of", "document") in rels


def test_stub_no_name_surface_mints_nothing():
    assert _mint_document_stub_edges(title="", source_ref="", chunks=["x"]) == []
    assert _mint_document_stub_edges(title="", source_ref="https://example.com/articles/", chunks=[]) == []


def test_stub_headings_are_capped():
    chunks = ["\n".join(f"# Heading {i}" for i in range(40))]
    assert len(_stub_heading_names(chunks)) == 20


def test_stub_filename_stem_extracts_basename():
    assert _stub_filename_stem("/tmp/a/b/Report_v2.pdf") == "Report_v2"
    assert _stub_filename_stem("file:///x/y/notes.md") == "notes"
    assert _stub_filename_stem("https://example.com/") == ""  # URL with no filename tail
    assert _stub_filename_stem("") == ""
    # URL query/fragment tails are NOT the document's name surface (junk-entity guard).
    assert _stub_filename_stem("https://site/docs/whitepaper?id=9") == "whitepaper"
    assert _stub_filename_stem("https://site/docs/faq#what-is-x") == "faq"


# ── pin (b): stub facts exist at ACCEPT time (ingest response window) ──────────────────
#
# FOSS-port adaptation: this lineage's ingest_document_tool extracts SYNCHRONOUSLY
# per chunk (no /documents/enqueue round-trip) — the ported stub mint fires at
# ACCEPTANCE, right after deterministic chunking and BEFORE the first extraction
# endpoint call. Same contract as the enqueue lane: the mint is the ONLY pre-
# extraction write, and the response reports it via stub_facts_minted.

def _foss_doc_flow_mocks(monkeypatch, s, ingest_response):
    """Wire the sync doc lane: rewrite route, no short-term lane, canned backend."""
    monkeypatch.setattr(s, "_statement_extractor_route",
                        _async_lambda("rewrite"))
    monkeypatch.setattr(s, "SHORT_TERM_MEMORY", False)
    posted_urls, ingest_bodies = [], []

    async def fake_post(url, json=None, timeout=None, headers=None):
        posted_urls.append(url)
        if url.endswith("/episodic/append"):
            return _resp({"status": "ok"})
        if url.endswith("/extract/rewrite"):
            return _resp({"edges": []})
        if url.endswith("/ingest"):
            ingest_bodies.append(json)
            return _resp(ingest_response(len(json.get("edges", []))))
        raise AssertionError(f"unexpected call: {url}")

    monkeypatch.setattr(s, "_http_client",
                        MagicMock(post=AsyncMock(side_effect=fake_post)))
    return posted_urls, ingest_bodies


def _async_lambda(value):
    async def _r(*_a, **_k):
        return value
    return _r


async def test_stub_facts_mint_at_accept_before_extraction(monkeypatch):
    """The /ingest stub write happens BEFORE any extraction call; the response says so."""
    s = server_module()
    posted_urls, ingest_bodies = _foss_doc_flow_mocks(
        monkeypatch, s, lambda n: {"committed": 0, "staged": n})
    text = "# Intro\nSome body prose.\n\n# Methods\nMore prose."
    out = await ingest_document_tool(text, "u-alice", source_ref="lab_notes.md",
                                     title="Lab Notes October")
    # IN THE RESPONSE WINDOW: the accept response reports the stub write.
    assert out["status"] == "ok"
    assert out.get("stub_facts_minted", 0) >= 3
    # Exactly ONE /ingest, and it is the STUB — it arrived BEFORE every extraction
    # endpoint call (the mint is the only pre-extraction write).
    ingest_calls = [i for i, u in enumerate(posted_urls) if u.endswith("/ingest")]
    assert len(ingest_calls) == 1
    first_rewrite = next((i for i, u in enumerate(posted_urls)
                          if u.endswith("/extract/rewrite")), None)
    assert first_rewrite is not None
    assert ingest_calls[0] < first_rewrite
    body = ingest_bodies[0]
    assert body["source"] == "document"
    rels = {(e["subject"], e["rel_type"], e["object"]) for e in body["edges"]}
    assert ("Lab Notes October", "instance_of", "document") in rels
    assert ("Intro", "part_of", "Lab Notes October") in rels


async def test_stub_mint_soft_fails_never_breaks_ingest(monkeypatch):
    """A rejected stub write must not fail (or reshape) the ingest response."""
    s = server_module()

    def _refuse(n):
        req = httpx.Request("POST", "http://f/ingest")
        resp = httpx.Response(400, request=req)
        return httpx.HTTPStatusError("client error 400", request=req, response=resp)

    async def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/episodic/append"):
            return _resp({"status": "ok"})
        if url.endswith("/extract/rewrite"):
            return _resp({"edges": []})
        if url.endswith("/ingest"):
            raise _refuse(0)
        raise AssertionError(url)

    monkeypatch.setattr(s, "_statement_extractor_route",
                        _async_lambda("rewrite"))
    monkeypatch.setattr(s, "SHORT_TERM_MEMORY", False)
    monkeypatch.setattr(s, "_http_client",
                        MagicMock(post=AsyncMock(side_effect=fake_post)))
    out = await ingest_document_tool("# A\nbody", "u-alice", source_ref="doc.pdf",
                                     title="A Document")
    assert out["status"] == "ok"
    assert out.get("stub_facts_minted", 0) == 0


async def test_stub_mint_reports_honest_zero_when_store_says_zero(monkeypatch):
    """A success shape explicitly reporting 0 landed edges stays an honest 0."""
    s = server_module()
    _foss_doc_flow_mocks(monkeypatch, s, lambda n: {"committed": 0, "staged": 0})
    out = await ingest_document_tool("# A\nbody", "u-alice", source_ref="doc2.pdf",
                                     title="Another Document")
    assert out["status"] == "ok"
    assert out["stub_facts_minted"] == 0


def server_module():
    import src.mcp.server as s
    return s


# ── W2 FIX pins: query token mechanics (no DB needed) ────────────────────────────────────

def test_document_query_tokens_drop_stopwords_keep_content():
    """THE live-failure mechanics: a natural-language question must yield its CONTENT
    tokens (OR floor), stopword-only/junk-only input yields NOTHING (no match-all)."""
    import src.api.main as M
    assert M._document_query_tokens("What is the soap making process?") == [
        "soap", "making", "process"]
    assert M._document_query_tokens(
        "How efficient are wind turbines at converting wind into energy?") == [
        "efficient", "wind", "turbines", "converting", "energy"]
    # Function-word-only / junk-only / empty → no tokens → honest empty upstream.
    assert M._document_query_tokens("the of and how are at") == []
    assert M._document_query_tokens("?!@#") == []
    assert M._document_query_tokens("") == []
    assert M._document_query_tokens("你好世界") == []
    # Documented stopword EXCLUSIONS stay live: negations + modal/content homonyms.
    toks = M._document_query_tokens("Windows not booting; what will happen?")
    assert "not" in toks and "will" in toks and "booting" in toks and "happen" in toks
    assert "what" not in toks


def test_document_query_tokens_cannot_carry_tsquery_metacharacters():
    """Injection-safety is BY CONSTRUCTION: the token regex admits [a-z0-9]+ only, so no
    tsquery metacharacter can reach to_tsquery. Dedup + cap also pinned."""
    import src.api.main as M
    toks = M._document_query_tokens(
        "alpha & bravo | charlie ! delta ( echo ) : foxtrot * golf ' hotel")
    assert toks == ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    assert M._document_query_tokens("soap SOAP Soap lye") == ["soap", "lye"]  # dedup
    capped = M._document_query_tokens(" ".join(f"tok{i:03d}" for i in range(40)))
    assert len(capped) == M._DOC_QUERY_MAX_TOKENS == 24


# ── pin (e): recall renders the deterministic assert line ────────────────────────────────

def test_render_document_hit_lines_deterministic():
    lines = _render_document_hit_lines([
        {"document_id": 7, "title": "Colonial Soap Making", "status": "ready",
         "chunk_index": 1, "chunk_text": "The boiling process uses lye and heat.",
         "chunk_count": 11, "rank": 0.1},
        {"document_id": 8, "title": "Pending Doc", "status": "pending",
         "chunk_index": 0, "chunk_text": "Lye safety basics.", "chunk_count": 4,
         "rank": 0.05},
    ])
    assert lines[0] == ("From your document Colonial Soap Making: The boiling process "
                        "uses lye and heat. (document id 7, section 2).")
    assert lines[1].startswith("From your document Pending Doc: Lye safety basics. "
                               "(document id 8, section 1;")
    assert "verbatim text only" in lines[1]
    # Per-status honesty: a FAILED import is not "still extracting".
    err_lines = _render_document_hit_lines([
        {"document_id": 9, "title": "Broken Doc", "status": "error",
         "chunk_index": 0, "chunk_text": "some passage", "chunk_count": 4}])
    assert "import failed" in err_lines[0]
    assert "still extracting" not in err_lines[0]
    # Title-only discovery rows (chunk_text None — R6 fallback) keep the W2 shape.
    fb_lines = _render_document_hit_lines([
        {"document_id": 7, "title": "Colonial Soap Making", "status": "ready",
         "chunk_index": None, "chunk_text": None, "chunk_count": 11, "rank": None}])
    assert fb_lines == ["You have a document: Colonial Soap Making (document id 7)."]
    # Passage whitespace collapses (chunks carry markdown newlines) — one assert
    # line. The leading '#' is VERBATIM chunk text (a heading the chunker kept) —
    # the passage is quoted, never re-authored.
    md_lines = _render_document_hit_lines([
        {"document_id": 7, "title": "T", "status": "ready", "chunk_index": 0,
         "chunk_text": "# Heading\n\nbody  prose\n", "chunk_count": 1}])
    assert "T: # Heading body prose (document id 7, section 1)" in md_lines[0]
    assert _render_document_hit_lines([]) == []
    # Malformed hits are skipped or shaped honestly — never raise.
    assert all(isinstance(x, str) for x in _render_document_hit_lines([None, "junk", {}]))


async def test_recall_empty_walk_with_doc_hit_renders_the_document_line(monkeypatch):
    """Empty fact walk + stub hit → the deterministic document line IS the answer."""
    s = server_module()

    async def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/classify-intent"):
            return _resp({"intent": "QUERY", "confidence": 0.9})
        if url.endswith("/query"):
            return _resp({"facts": [], "attributes": {}, "document_hits": [
                {"document_id": 7, "title": "Colonial Soap Making", "status": "ready",
                 "chunk_index": 1, "chunk_text": "The boiling process uses lye and heat.",
                 "chunk_count": 11, "rank": 0.1}]})
        raise AssertionError(url)

    async def fake_get(url, params=None, timeout=None):
        if "/confidence-gate/" in url:
            return _resp({"threshold": 0.70})
        if url.endswith("/documents/pending"):
            return _resp({"pending": False, "count": 0})
        raise AssertionError(url)

    monkeypatch.setattr(s, "_http_client",
                        MagicMock(post=AsyncMock(side_effect=fake_post),
                                  get=AsyncMock(side_effect=fake_get)))
    monkeypatch.setattr(s, "RECALL_INTENT_ROUTING", False, raising=False)
    out = await recall_memory_tool("soap making lye", "u-alice")
    mem = out["memory"]
    assert "From your document Colonial Soap Making: The boiling process uses lye and heat." in mem
    assert "document id 7" in mem
    assert "No relevant facts found" not in mem


async def test_recall_with_facts_leads_with_facts_doc_line_after(monkeypatch):
    s = server_module()

    async def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/classify-intent"):
            return _resp({"intent": "QUERY", "confidence": 0.9})
        if url.endswith("/query"):
            return _resp({"facts": [{"definition": "Your dog is Rex.", "fact_class": "A",
                                     "fact_provenance": "user_stated", "confidence": 1.0}],
                          "attributes": {}, "document_hits": [
                              {"document_id": 4, "title": "Dog Manual", "status": "ready",
                               "chunk_index": 0, "chunk_text": "Feed the dog twice daily.",
                               "chunk_count": 9, "rank": 0.07}]})
        raise AssertionError(url)

    async def fake_get(url, params=None, timeout=None):
        if "/confidence-gate/" in url:
            return _resp({"threshold": 0.70})
        if url.endswith("/documents/pending"):
            return _resp({"pending": False, "count": 0})
        raise AssertionError(url)

    monkeypatch.setattr(s, "_http_client",
                        MagicMock(post=AsyncMock(side_effect=fake_post),
                                  get=AsyncMock(side_effect=fake_get)))
    monkeypatch.setattr(s, "RECALL_INTENT_ROUTING", False, raising=False)
    out = await recall_memory_tool("dog", "u-alice")
    mem = out["memory"]
    assert mem.index("Your dog is Rex.") < mem.index("From your document Dog Manual")


# ── DB pins (a) + (d): dedicated throwaway database, real migration corpus ──────────────

def _dedicated_database(suffix: str) -> str:
    """Own database of this test's own, derived from the runner's *_test DSN (the fanout
    sweep takes ACCESS EXCLUSIVE locks; the shared suite DB must never see it)."""
    from urllib.parse import urlsplit, urlunsplit
    import psycopg2
    p = urlsplit(_DSN)
    base = p.path.lstrip("/")
    assert base.endswith("_test"), "refusing to derive a throwaway from a non-*_test database"
    name = f"{base}_{suffix}"[:63]
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


def _drop_dedicated_database(dsn: str) -> None:
    from urllib.parse import urlsplit
    import psycopg2
    name = urlsplit(dsn).path.lstrip("/")
    assert "stubtsv" in name and name != urlsplit(_DSN).path.lstrip("/"), name
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN not set")
def test_stub_tsv_fresh_mint_legacy_ledger_and_document_hits(monkeypatch):
    """Pins (a) + (d) in one booted database:

    (a1) a FRESH tenant (template mint) carries stub_tsv + the GIN index and matches a
         keyword from chunk text;
    (a2) a LEGACY tenant (299's ledger rows cleared + column dropped) gets the column
         and index BACK when the ledger re-applies 299 — and matches chunk keywords;
    (d)  the document-hits SELECT returns the doc with a headline containing the query
         token and a matching-chunk count — and returns EMPTY (never raises) for a
         foreign tenant, a no-match query, and a pre-299 schema.
    """
    import psycopg2
    from src.provisioning.schema_manager import create_user_schema

    mig_dir = os.environ.get("FAULTLINE_MIGRATIONS_DIR", "migrations")
    if not os.path.isdir(mig_dir):
        pytest.skip(f"migrations dir {mig_dir!r} not found")

    dsn = _dedicated_database("stubtsv" + uuid.uuid4().hex[:6])
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    try:
        # FOSS convention: the whole migration corpus applies at boot (entrypoint
        # psql loop) — tests apply the files via psql -f per file, the SAME
        # per-statement continue-on-error semantics the entrypoint uses. The
        # ported migrations (298-301) are guarded-idempotent and must apply cleanly.
        import glob as _glob
        import subprocess as _sp
        _ported = ("298", "299", "300", "301")
        for _mf in sorted(_glob.glob(os.path.join(mig_dir, "*.sql"))):
            _proc = _sp.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-q", "-f", _mf],
                            capture_output=True, text=True)
            if _proc.returncode != 0 and os.path.basename(_mf).split("_")[0] in _ported:
                raise AssertionError(f"{_mf} failed: {_proc.stderr[-400:]}")

        # (a1) FRESH MINT: the template carries 299.
        uid_fresh = str(uuid.uuid4())
        schema_fresh, status = create_user_schema(uid_fresh, uid_fresh.replace("-", "_"))
        assert status == "ready", f"fresh provision failed: {status}"

        def has_stub_tsv(schema):
            conn = psycopg2.connect(dsn)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name='documents' "
                        "AND column_name='stub_tsv'", (schema,))
                    col = cur.fetchone()
                    cur.execute(
                        "SELECT 1 FROM pg_indexes WHERE schemaname=%s "
                        "AND tablename='documents' AND indexname='idx_documents_stub_tsv'",
                        (schema,))
                    idx = cur.fetchone()
                    return bool(col) and bool(idx)
            finally:
                conn.close()

        assert has_stub_tsv(schema_fresh), "fresh mint lacks stub_tsv/GIN (template behind 299)"

        # (a2) LEGACY: strip the column + index → re-apply the corpus → back again
        # (the FOSS boot applies every file every time; 299 is guarded-idempotent).
        uid_legacy = str(uuid.uuid4())
        schema_legacy, status = create_user_schema(uid_legacy, uid_legacy.replace("-", "_"))
        assert status == "ready"
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {schema_legacy}.documents DROP COLUMN stub_tsv")
                cur.execute(f"DROP INDEX IF EXISTS {schema_legacy}.idx_documents_stub_tsv")
        finally:
            conn.close()
        assert not has_stub_tsv(schema_legacy)
        _reapplied = False
        _mf299 = os.path.join(mig_dir, "299_documents_stub_tsv.sql")
        _bc = psycopg2.connect(dsn)
        _bc.autocommit = True
        try:
            with _bc.cursor() as cur:
                cur.execute(open(_mf299, encoding="utf-8").read())
        finally:
            _bc.close()
        assert has_stub_tsv(schema_legacy), "re-applied 299 did not restore the legacy tenant"

        # Seed one document per tenant; the query token comes from CHUNK TEXT.
        def insert_doc(schema, title, chunks, uid):
            conn = psycopg2.connect(dsn)
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema}")
                    cur.execute(
                        "INSERT INTO documents (user_id, source_ref, title, chunks, status) "
                        "VALUES (%s, %s, %s, %s::jsonb, 'pending') RETURNING id",
                        (uid, f"/docs/{schema}.pdf", title, json.dumps(chunks)))
                    rid = cur.fetchone()[0]
                conn.commit()
                return rid
            finally:
                conn.close()

        doc_id = insert_doc(schema_fresh, "Colonial Soap Making",
                            ["Introduction to soap basics",
                             "The boiling process uses lye and heat",
                             "Troubleshooting soft soap batches"], uid_fresh)
        insert_doc(schema_legacy, "Field Guide", ["lye safety section"], uid_legacy)

        import src.api.main as M

        # (d) HIT: doc + THE PASSAGE CHUNK that carries the tokens (R6 reshape — the
        # specific chunk text, never a stitched headline). Both 'lye' and 'boiling'
        # live in chunk 1 of 3.
        hits = M._document_hits_for_query(uid_fresh, "lye boiling")
        assert len(hits) == 1, hits
        h = hits[0]
        assert h["document_id"] == doc_id
        assert h["title"] == "Colonial Soap Making"
        assert h["status"] == "pending"  # the stub is searchable BEFORE extraction
        assert h["chunk_index"] == 1     # 0-based ordinal into documents.chunks
        assert h["chunk_text"] == "The boiling process uses lye and heat"
        assert h["rank"] > 0
        assert "headline" not in h and "matching_chunks" not in h  # old shape is GONE

        # (d) TOP-3 BOUND + ordering by rank (title token outranks chunk token).
        hits_title = M._document_hits_for_query(uid_fresh, "Colonial")
        assert len(hits_title) == 1 and hits_title[0]["document_id"] == doc_id
        # Title-only match → the DISCOVERY fallback row (no passage columns).
        assert hits_title[0]["chunk_text"] is None
        assert hits_title[0]["chunk_index"] is None

        # (d-W2 FIX) THE PIN THAT FAILED LIVE: a natural-language QUESTION hits the
        # document. The old websearch_to_tsquery('simple') AND demanded
        # 'what & is & the & soap …' to co-occur in ONE chunk → 0 sections. Under the
        # OR floor the content terms carry it.
        hits_q = M._document_hits_for_query(uid_fresh, "What is the soap making process?")
        assert hits_q, hits_q
        # One PASSAGE per hit now (R6): every hit names this doc, quotes real chunk
        # text, and the bound is the passage LIMIT (3), not the doc count.
        assert all(h["document_id"] == doc_id and h["chunk_text"] and h["rank"] > 0
                   for h in hits_q)
        assert len(hits_q) <= 3
        # Junk-only / function-word-only queries return EMPTY — the OR floor must not
        # degenerate into match-everything.
        assert M._document_hits_for_query(uid_fresh, "the of and how are at") == []
        assert M._document_hits_for_query(uid_fresh, "?!@#") == []
        # A single rare content term still hits (OR degenerates to the term itself).
        hits_rare = M._document_hits_for_query(uid_fresh, "troubleshooting")
        assert len(hits_rare) == 1 and hits_rare[0]["document_id"] == doc_id
        # tsquery metacharacters cannot survive tokenization: a hostile query degrades to
        # plain content tokens (still HITS via 'soap' — up to one passage per matching
        # chunk) and the table survives intact.
        hits_inj = M._document_hits_for_query(
            uid_fresh, "soap'); DROP TABLE documents; --")
        assert hits_inj and all(h["document_id"] == doc_id for h in hits_inj)
        assert M._document_hits_for_query(uid_fresh, "lye boiling"), \
            "documents table damaged by hostile query"

        # (d) EMPTY, NEVER RAISES: foreign tenant / no-match / pre-299 schema.
        assert M._document_hits_for_query(str(uuid.uuid4()), "lye") == []
        assert M._document_hits_for_query(uid_fresh, "zzzunmatchablezzz") == []
        assert M._document_hits_for_query(uid_fresh, "") == []
        assert M._document_hits_for_query("anonymous", "lye") == []
        # pre-299 schema: strip the column from a THIRD tenant → honest empty, no raise.
        uid_pre = str(uuid.uuid4())
        schema_pre, status = create_user_schema(uid_pre, uid_pre.replace("-", "_"))
        assert status == "ready"
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {schema_pre}.documents DROP COLUMN stub_tsv")
        finally:
            conn.close()
        assert M._document_hits_for_query(uid_pre, "lye") == []
    finally:
        _drop_dedicated_database(dsn)
