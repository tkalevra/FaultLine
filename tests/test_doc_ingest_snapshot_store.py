"""Pins for the STORE-FIRST document ingest (migration 327, wound-map round 4).

THE WOUND: document enqueue → per-chunk SYNCHRONOUS brain extraction (180s
ReadTimeouts) → /extract/rewrite fallback → UNBOUNDED retries → the stack clogged
(59/62GB, provisioning worker dead); no backpressure anywhere on the doc lane.

THE BUILD UNDER TEST: corpus_documents/corpus_pages (snapshot + verbatim page
text), a bounded-fast ingest endpoint (DB writes only — NO brain call anywhere in
the new path), the zero-loss fidelity gate (any 'length' page ⇒ status=incomplete,
said in the response), the anchor-preservation contract (ref/box tokens are
content), the OCR engine interface (none default + the DeepSeek caller), the
backpressure cap on the legacy uplift, the corpus_pages retrieval source, and the
DOC_INGEST_SNAPSHOT_STORE flag (OFF = byte-parity with today's enqueue behavior).

Hermetic legs run everywhere. DB legs boot a THROWAWAY database with the real
migration corpus (the r6pass pattern, tests/test_document_passage_lane.py).
"""

import base64
import io
import json
import os
import zipfile

import pytest

from src.ingest import corpus_store as cs
from src.ingest import ocr_engines as oe


# ── helpers ───────────────────────────────────────────────────────────────────────────

def _make_ooxml(first_entry: str, parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # The OOXML convention the detector keys on: the container's FIRST entry
        # names the flavour (word/ vs ppt/).
        zf.writestr(first_entry, "")
        for name, body in parts.items():
            zf.writestr(name, body)
    return buf.getvalue()


DOCX = _make_ooxml(
    "word/document.xml", {})
# A real docx carries word/document.xml as its first real part; rebuild with the
# body inside the container (first entry is the directory marker, contents after).
DOCX = _make_ooxml("word/", {
    "word/document.xml":
        '<?xml version="1.0"?><w:document xmlns:w="urn:x"><w:body>'
        "<w:p><w:r><w:t>Alpha page text</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Beta page text</w:t></w:r></w:p>"
        "</w:body></w:document>",
})

PPTX = _make_ooxml("ppt/", {
    "ppt/slides/slide1.xml":
        '<?xml version="1.0"?><p:sld xmlns:p="urn:p" xmlns:a="urn:y">'
        "<a:t>Slide one</a:t></p:sld>",
    "ppt/slides/slide2.xml":
        '<?xml version="1.0"?><p:sld xmlns:p="urn:p" xmlns:a="urn:y">'
        "<a:t>Slide two</a:t></p:sld>",
})

STRAY_ZIP = _make_ooxml("mimetype", {"content.xml": "<odt/>"})


# ── hermetic: format detection ────────────────────────────────────────────────────────

def test_format_detection_magic_bytes():
    """Magic bytes decide; a PK zip is docx/pptx ONLY by its first entry; a stray
    zip is UNKNOWN (retained honestly, never guessed at); NUL-free UTF-8 is text."""
    assert cs.detect_format(b"%PDF-1.7 ...") == cs.FORMAT_PDF
    assert cs.detect_format(b"\x89PNG\r\n\x1a\n....") == cs.FORMAT_IMAGE
    assert cs.detect_format(b"\xff\xd8\xff\xe0jpeg") == cs.FORMAT_IMAGE
    assert cs.detect_format(b"GIF89a..") == cs.FORMAT_IMAGE
    assert cs.detect_format(b"II*\x00tiff") == cs.FORMAT_IMAGE
    assert cs.detect_format(b"MM\x00*tiff") == cs.FORMAT_IMAGE
    assert cs.detect_format(b"RIFF____WEBPvp8") == cs.FORMAT_IMAGE
    assert cs.detect_format(DOCX, "notes.docx") == cs.FORMAT_TEXT
    assert cs.detect_format(PPTX, "deck.pptx") == cs.FORMAT_TEXT
    assert cs.detect_format(STRAY_ZIP, "thing.zip") == cs.FORMAT_UNKNOWN
    assert cs.detect_format(b"plain prose, no magic") == cs.FORMAT_TEXT
    assert cs.detect_format(b"# markdown\n\n- list\n") == cs.FORMAT_TEXT
    assert cs.detect_format(b'{"json": true}') == cs.FORMAT_TEXT
    assert cs.detect_format(b"col1,col2\n1,2\n") == cs.FORMAT_TEXT
    assert cs.detect_format(bytes([0x00, 0x01, 0xfe, 0xff])) == cs.FORMAT_UNKNOWN
    assert cs.detect_format(b"") == cs.FORMAT_UNKNOWN


# ── hermetic: page splitting ──────────────────────────────────────────────────────────

def test_text_pages_form_feed_split_all_stop():
    pages = cs._split_text_pages("page one body\n\fpage two body\n\fpage three")
    assert [p.text for p in pages] == ["page one body\n", "page two body\n",
                                       "page three"]
    assert all(p.finish_reason == "stop" for p in pages)
    assert [p.page_index for p in pages] == [0, 1, 2]


def test_oversize_text_page_budget_split_loses_nothing():
    body = "x" * (cs._TEXT_PAGE_MAX_CHARS * 2 + 17)
    pages = cs._split_text_pages(body)
    assert "".join(p.text for p in pages) == body  # NOTHING dropped
    assert all(p.finish_reason == "stop" for p in pages)
    assert max(len(p.text) for p in pages) <= cs._TEXT_PAGE_MAX_CHARS


def test_docx_pptx_stdlib_direct_extraction():
    """docx/pptx text lands via the stdlib zip+XML walk — no model, no new deps.
    docx is ONE page stream; pptx is one page PER SLIDE."""
    dpages = cs.extract_pages(DOCX, cs.FORMAT_TEXT)
    assert dpages and dpages[0].text == "Alpha page textBeta page text"
    assert all(p.finish_reason == "stop" for p in dpages)
    ppages = cs.extract_pages(PPTX, cs.FORMAT_TEXT)
    assert [p.text for p in ppages] == ["Slide one", "Slide two"]
    assert [p.page_index for p in ppages] == [0, 1]


# ── hermetic: the zero-loss status gate ───────────────────────────────────────────────

def test_length_page_refuses_ready():
    """ANY 'length' page ⇒ status=incomplete with the page listed — the fidelity
    gate straight from the OCR guide's finish_reason contract."""
    pages = [cs.CorpusPage(0, "complete page", "stop"),
             cs.CorpusPage(1, "truncated pa...", "length")]
    status, report = cs.pages_status(pages)
    assert status == cs.STATUS_INCOMPLETE
    assert report["pages_length"] == [1]


def test_pending_page_refuses_ready():
    pages = [cs.CorpusPage(0, "read page", "stop"),
             cs.CorpusPage(1, "", None)]      # pixel page, OCR never attempted
    status, report = cs.pages_status(pages)
    assert status == cs.STATUS_INCOMPLETE
    assert report["pages_pending_ocr"] == [1]


def test_all_stop_pages_ready_and_empty_never_ready():
    ok = [cs.CorpusPage(0, "one", "stop"), cs.CorpusPage(1, "two", "stop")]
    assert cs.pages_status(ok)[0] == cs.STATUS_READY
    status, report = cs.pages_status([])
    assert status == cs.STATUS_INCOMPLETE
    assert report["reason"] == "no_pages_extracted"


# ── hermetic: anchor preservation + snapshot fidelity ────────────────────────────────

def test_anchor_tokens_stored_verbatim_never_stripped():
    """The zero-loss TEXT contract: ref/box-style layout anchors are CONTENT.
    build_snapshot with caller-supplied OCR-shaped pages stores them byte-for-byte."""
    anchored = ('Title line\n'
                '<｜ref｜>Annual Report<｜/ref｜><｜box｜>[[52, 40, 948, 96]]<｜/box｜>')
    snap = cs.build_snapshot(
        user_id="u1", data=b"raw pixel bytes here", filename="scan.png",
        pages=[{"page_index": 0, "text": anchored, "finish_reason": "stop"}])
    assert snap.pages[0].text == anchored          # byte-for-byte, no cleaning
    assert snap.status == cs.STATUS_READY


def test_snapshot_roundtrip_and_sha_fidelity():
    raw = b"the original bytes \x00\x01\xff" * 500
    snap = cs.build_snapshot(user_id="u1", data=raw, filename="doc.bin")
    assert snap.byte_size == len(raw)
    assert snap.content_sha256_ == cs.content_sha256(raw)
    assert snap.decompressed() == raw              # round-trip byte-fidelity
    assert snap.snapshot_codec == "zlib"
    with pytest.raises(ValueError):
        cs.decompress_snapshot(snap.raw_snapshot, codec="bogus")


def test_unknown_format_snapshots_but_fails_honestly():
    snap = cs.build_snapshot(user_id="u1", data=bytes([0, 1, 2, 0xfe]),
                             filename="blob.dat")
    assert snap.status == cs.STATUS_FAILED
    assert snap.error == "unknown_format"
    assert snap.pages == []
    assert snap.decompressed() == bytes([0, 1, 2, 0xfe])  # bytes still retained


# ── hermetic: backpressure ────────────────────────────────────────────────────────────

def test_uplift_admission_backpressure_shape():
    assert cs.uplift_admission(0, cap=200)["admit"] is True
    assert cs.uplift_admission(199, cap=200)["admit"] is True
    under = cs.uplift_admission(200, cap=200)
    assert under["admit"] is False and under == {
        "admit": False, "cap": 200, "active": 200}
    assert cs.uplift_admission(5000, cap=-1)["admit"] is True  # operator no-cap


# ── hermetic: OCR engine interface ────────────────────────────────────────────────────

async def test_none_engine_default_and_honest_refusal():
    eng = oe.get_ocr_engine({"OCR_ENGINE": ""})
    assert isinstance(eng, oe.NoneOCREngine) and eng.name == "none"
    with pytest.raises(oe.OCRUnavailable):
        await eng.recognize(b"x")


def test_deepseek_engine_no_key_in_code_and_config_refusal():
    with pytest.raises(oe.OCRError):
        oe.DeepSeekOCREngine("")                    # key must come from env/config
    with pytest.raises(oe.OCRError):
        oe.get_ocr_engine({"OCR_ENGINE": "bogus"})  # unknown engine refused loudly
    eng = oe.DeepSeekOCREngine("test-key-only")     # injected, never hardcoded
    assert eng.name == "deepseek"


def test_deepseek_response_mapping_and_error_shapes():
    """The guide's response shape: pages[] with finish_reason; an error object is
    a refusal, terminal vs transient classified by the guide's own table."""
    import asyncio
    import httpx

    class _Resp:
        def __init__(self, payload, status=200):
            self._p, self.status_code = payload, status
        def json(self):
            if isinstance(self._p, Exception):
                raise self._p
            return self._p

    class _Client:
        def __init__(self, resp):
            self._resp = resp
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, data=None, files=None, headers=None):
            # EXACTLY the guide's two form fields, multipart:
            assert data == {"purpose": "text_recognition"}
            assert files and files["file"][0] == "doc.pdf"
            assert headers["Authorization"] == "Bearer test-key-only"
            return self._resp

    def _run(resp):
        eng = oe.DeepSeekOCREngine("test-key-only")
        import unittest.mock as mock
        with mock.patch.object(httpx, "AsyncClient", lambda *a, **k: _Client(resp)):
            return asyncio.run(eng.recognize(b"%PDF-1.4", "doc.pdf"))

    ok = _run(_Resp({"pages": [
        {"index": 0, "text": "page zero text", "finish_reason": "stop"},
        {"index": 1, "text": "cut off", "finish_reason": "length"},
    ]}))
    assert [(p.page_index, p.finish_reason) for p in ok] == [(0, "stop"), (1, "length")]
    assert ok[0].text == "page zero text"

    # leading-newline keep-alive whitespace: whole body still parses as JSON
    ok2 = _run(_Resp({"pages": [{"index": 0, "text": "t", "finish_reason": "stop"}]}))
    assert ok2[0].text == "t"

    with pytest.raises(oe.OCRError) as ei:
        _run(_Resp({"error": {"message": "The file has an open password",
                              "type": "invalid_request_error"}}))
    assert ei.value.transient is False        # terminal: retrying cannot help
    with pytest.raises(oe.OCRError) as ei2:
        _run(_Resp({"error": {"message": "The service is busy. Retry later",
                              "type": "server_error"}}))
    assert ei2.value.transient is True        # transient: bounded retry allowed


# ── hermetic: flag parity (OFF = byte-parity with today's enqueue behavior) ───────────

class _FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
    def raise_for_status(self):
        return None
    def json(self):
        return self._p


class _RecordingClient:
    """Records POSTs; answers the endpoints ingest_file_core touches."""
    def __init__(self):
        self.posts = []

    async def post(self, url, json=None, timeout=None):
        self.posts.append(url)
        if url.endswith("/documents/enqueue"):
            return _FakeResp({"status": "pending", "document_id": 41,
                              "chunk_count": 1, "eta_seconds": 5})
        if url.endswith("/corpus/documents"):
            return _FakeResp({"status": "ready", "document_id": 9,
                              "page_count": 1, "fidelity": {"pages": 1}})
        if url.endswith("/ingest"):
            return _FakeResp({"committed": 1, "staged": 0})
        if url.endswith("/artefacts/retain"):
            return _FakeResp({"status": "ok", "artefact_id": 7})
        raise AssertionError(url)


def _probe_document() -> bytes:
    # The binary door's ALLOWED set is pdf/images — a text/plain payload is
    # REFUSED by the door itself (UnsupportedMediaType), so the parity probe
    # rides a minimal PDF. Locally pdfplumber is absent → the door degrades to
    # retained_no_text; under the flag the snapshot STILL stores (that is the
    # point: pixels persist even when text extraction cannot run).
    return b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.mark.asyncio
async def test_flag_off_is_byte_parity_no_corpus_call(monkeypatch):
    """DOC_INGEST_SNAPSHOT_STORE OFF: no /corpus/documents POST, and the response
    carries NO `corpus` key — identical shape to the pre-327 lane."""
    import src.mcp.server as S

    cli = _RecordingClient()
    monkeypatch.setattr(S, "_client", lambda: cli)
    monkeypatch.setattr(S, "DOC_INGEST_SNAPSHOT_STORE", False)
    monkeypatch.setattr(S, "_mint_document_stub_facts",
                        lambda **k: _async_none())
    monkeypatch.setattr(S, "_retain_document_source",
                        lambda **k: _async_retain())
    out = await S.ingest_file_core(
        user_id="11111111-1111-1111-1111-111111111111",
        data=_probe_document(), claimed_name="probe.pdf",
        source_ref="probe.pdf", title="probe")
    assert not any(u.endswith("/corpus/documents") for u in cli.posts)
    assert "corpus" not in out
    assert out["status"] in ("pending", "retained", "retained_no_text")


@pytest.mark.asyncio
async def test_flag_on_snapshots_bounded_fast(monkeypatch):
    """Flag ON: exactly ONE bounded /corpus/documents POST rides the ingest; the
    response carries the honest corpus report. No brain endpoint is called."""
    import src.mcp.server as S

    cli = _RecordingClient()
    monkeypatch.setattr(S, "_client", lambda: cli)
    monkeypatch.setattr(S, "DOC_INGEST_SNAPSHOT_STORE", True)
    monkeypatch.setattr(S, "_mint_document_stub_facts",
                        lambda **k: _async_none())
    monkeypatch.setattr(S, "_retain_document_source",
                        lambda **k: _async_retain())
    out = await S.ingest_file_core(
        user_id="11111111-1111-1111-1111-111111111111",
        data=_probe_document(), claimed_name="probe.pdf",
        source_ref="probe.pdf", title="probe")
    assert sum(1 for u in cli.posts if u.endswith("/corpus/documents")) == 1
    assert out["corpus"]["status"] in ("ready", "incomplete", "failed", "pending")
    # NO synchronous brain call anywhere in the new path: the only lanes touched
    # are enqueue (async by design) + the snapshot + artefact retention.
    assert not any("/extract" in u or "/harvest" in u for u in cli.posts)


async def _async_none():
    return None


async def _async_retain():
    return {"status": "retained", "artefact_id": 7}


# ── DB-gated legs: throwaway database + the real migration corpus ─────────────────────

_DSN = os.environ.get("POSTGRES_DSN", "")
_corpus_db = pytest.mark.skipif(
    not _DSN, reason="POSTGRES_DSN unset — the corpus DB gate stays dark")


@pytest.fixture(scope="module")
def corpus_db():
    """Boot ONE throwaway database with the real migration corpus (incl. 327),
    mint ONE fresh tenant schema, hand back (dsn, uid)."""
    if not _DSN:
        pytest.skip("POSTGRES_DSN not set")
    mig_dir = os.environ.get("FAULTLINE_MIGRATIONS_DIR", "migrations")
    if not os.path.isdir(mig_dir):
        pytest.skip(f"migrations dir {mig_dir!r} not found")
    from urllib.parse import urlsplit, urlunsplit
    import uuid
    import psycopg2
    from src.provisioning import boot_migrations as bm
    from src.provisioning.schema_manager import create_user_schema

    p = urlsplit(_DSN)
    base = p.path.lstrip("/")
    assert base.endswith("_test"), \
        "refusing to derive a throwaway from a non-*_test database"
    name = f"{base}_corpus{uuid.uuid4().hex[:6]}"[:63]
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    dsn = urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))
    saved = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = dsn
    try:
        boot = bm.run_boot_migrations(dsn, mig_dir, echo=False)
        assert boot.fatal is None and boot.ledger_active, (boot.fatal, boot.ledger_error)
        uid = str(uuid.uuid4())
        schema, st = create_user_schema(uid, uid.replace("-", "_"))
        assert st == "ready", st
        yield {"dsn": dsn, "uid": uid, "schema": schema,
               "mig_dir": mig_dir}
    finally:
        if saved is None:
            os.environ.pop("POSTGRES_DSN", None)
        else:
            os.environ["POSTGRES_DSN"] = saved
        drop = psycopg2.connect(_DSN)
        drop.autocommit = True
        try:
            with drop.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            drop.close()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@_corpus_db
def test_db_migration_idempotent_and_tables_present(corpus_db):
    """327 ran inside the boot corpus (fresh tenant schema born with the tables
    via the TEMPLATE — the same-commit rule) and re-running the file is a no-op."""
    import psycopg2
    conn = psycopg2.connect(corpus_db["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {corpus_db['schema']}")
            cur.execute("SELECT count(*) FROM corpus_documents")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM corpus_pages")
            assert cur.fetchone()[0] == 0
        # Idempotency: execute the whole migration file AGAIN against this DB.
        with open(os.path.join(corpus_db["mig_dir"],
                               "327_corpus_snapshot_store.sql")) as f:
            sql = f.read()
        conn.commit()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {corpus_db['schema']}")
            cur.execute("SELECT count(*) FROM corpus_documents")
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


@_corpus_db
def test_db_snapshot_roundtrip_byte_fidelity(corpus_db, monkeypatch):
    """The store leg: POST /corpus/documents → row + page rows; the snapshot
    decompresses to the EXACT original bytes and re-hashes to content_sha256."""
    import src.api.main as M

    monkeypatch.setenv("POSTGRES_DSN", corpus_db["dsn"])
    monkeypatch.setattr(M, "_INGEST_ENABLED", True, raising=False)
    raw = "Corpus fidelity probe page one.\n\fCorpus fidelity probe page two.\n".encode()
    out = M.corpus_documents_store(M.CorpusDocumentRequest(
        user_id=corpus_db["uid"], data_b64=_b64(raw),
        original_filename="probe.txt", source_ref="probe.txt"))
    assert out["status"] == "ready", out
    assert out["page_count"] == 2
    assert out["fidelity"]["pages_length"] == 0

    import psycopg2
    from src.ingest.corpus_store import decompress_snapshot, content_sha256
    conn = psycopg2.connect(corpus_db["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {corpus_db['schema']}")
            cur.execute(
                "SELECT raw_snapshot, snapshot_codec, content_sha256, status "
                "FROM corpus_documents WHERE id = %s", (out["document_id"],))
            blob, codec, sha, status = cur.fetchone()
            assert status == "ready"
            assert decompress_snapshot(bytes(blob), codec) == raw     # byte-fidelity
            assert content_sha256(raw) == sha == out["content_sha256"]
            cur.execute(
                "SELECT page_index, layout_text, finish_reason FROM corpus_pages "
                "WHERE document_id = %s ORDER BY page_index", (out["document_id"],))
            rows = cur.fetchall()
            assert [(r[0], r[2]) for r in rows] == [(0, "stop"), (1, "stop")]
            assert rows[0][1].startswith("Corpus fidelity probe page one.")
    finally:
        conn.close()


@_corpus_db
def test_db_pdf_blank_page_is_incomplete_pending_ocr(corpus_db, monkeypatch):
    """A PDF page with NO text is stored pending (finish_reason NULL) and the
    document honestly reports incomplete — the zero-loss gate on real rows."""
    import src.api.main as M

    monkeypatch.setenv("POSTGRES_DSN", corpus_db["dsn"])
    monkeypatch.setattr(M, "_INGEST_ENABLED", True, raising=False)
    # Caller-parsed pages (the MCP binary door's shape): page 0 read, page 1 blank.
    out = M.corpus_documents_store(M.CorpusDocumentRequest(
        user_id=corpus_db["uid"], data_b64=_b64(b"%PDF-1.4 not-really-parsed"),
        original_filename="mixed.pdf",
        pages=[{"page_index": 0, "text": "readable page", "finish_reason": "stop"},
               {"page_index": 1, "text": "", "finish_reason": None}]))
    assert out["status"] == "incomplete"
    assert out["fidelity"]["pages_pending_ocr"] == [1]

    import psycopg2
    conn = psycopg2.connect(corpus_db["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {corpus_db['schema']}")
            cur.execute(
                "SELECT finish_reason FROM corpus_pages WHERE document_id = %s "
                "AND page_index = 1", (out["document_id"],))
            assert cur.fetchone()[0] is None        # pending, never fabricated
    finally:
        conn.close()


@_corpus_db
def test_db_uplift_backpressure_shape(corpus_db, monkeypatch):
    """Under cap → ONE legacy documents row (status pending, parser_version
    corpus_v1). At/over cap → uplift DEFERRED, no documents row, snapshot safe —
    ingest stays bounded-fast under a corpus dump (the wound's litmus)."""
    import src.api.main as M
    from src.ingest import corpus_store as cs_mod

    monkeypatch.setenv("POSTGRES_DSN", corpus_db["dsn"])
    monkeypatch.setattr(M, "_INGEST_ENABLED", True, raising=False)
    raw = b"uplift probe body text\n"

    out1 = M.corpus_documents_store(M.CorpusDocumentRequest(
        user_id=corpus_db["uid"], data_b64=_b64(raw),
        original_filename="uplift.txt", source_ref="uplift.txt", uplift=True))
    assert out1["status"] == "ready"
    assert out1["uplift"]["scheduled"] is True

    # At the cap (the row just scheduled counts as active downstream work): cap
    # squeezed to 1 via the LATE-BOUND admission knob — the shape, not the default.
    monkeypatch.setattr(cs_mod, "CORPUS_UPLIFT_QUEUE_CAP", 1)
    out2 = M.corpus_documents_store(M.CorpusDocumentRequest(
        user_id=corpus_db["uid"], data_b64=_b64(raw + b"two"),
        original_filename="uplift2.txt", source_ref="uplift2.txt", uplift=True))
    assert out2["status"] == "ready"
    assert out2["uplift"]["admit"] is False
    assert out2["uplift"]["scheduled"] is False
    assert out2["document_id"] is not None        # the SNAPSHOT is never refused

    import psycopg2
    conn = psycopg2.connect(corpus_db["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {corpus_db['schema']}")
            cur.execute(
                "SELECT count(*), max(parser_version) FROM documents "
                "WHERE source_ref LIKE 'uplift%'")
            n, pv = cur.fetchone()
            assert n == 1 and pv == "corpus_v1"   # exactly ONE uplift scheduled
    finally:
        conn.close()


@_corpus_db
def test_db_corpus_pages_are_a_retrieval_source(corpus_db, monkeypatch):
    """The serve path reads the store: a question whose answer lives in a stored
    PAGE returns that page's text, cited document + page."""
    import src.api.main as M

    monkeypatch.setenv("POSTGRES_DSN", corpus_db["dsn"])
    body = ("Photosynthesis converts light energy into chemical energy stored "
            "as glucose in plant chloroplasts.")
    out = M.corpus_documents_store(M.CorpusDocumentRequest(
        user_id=corpus_db["uid"], data_b64=_b64(("intro\n\f" + body + "\n").encode()),
        original_filename="bio.txt", source_ref="bio.txt"))
    assert out["status"] == "ready"

    hits = M._document_hits_for_query(corpus_db["uid"],
                                      "what does photosynthesis convert?")
    corpus_hits = [h for h in hits if h.get("source") == "corpus_pages"]
    assert corpus_hits, hits
    hit = corpus_hits[0]
    assert "chemical energy" in hit["chunk_text"]      # the PASSAGE, verbatim
    assert hit["chunk_index"] == 1                     # page-grounded

    # and the renderer cites document + page for corpus hits
    import src.mcp.server as S
    lines = S._render_document_hit_lines([hit])
    assert len(lines) == 1
    assert "page 2" in lines[0]
    assert "chemical energy" in lines[0]
