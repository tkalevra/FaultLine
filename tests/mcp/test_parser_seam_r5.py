"""R5 PARSER SEAM — the layout-aware parser upgrade lane behind DOC_PARSER.

Pins (gauntlet R5, `lane/cpu-parser`):
  * DEFAULT INSTALL, DOCLING ABSENT — `DOC_PARSER` unset stamps naive_v1 and never
    imports docling; `DOC_PARSER=docling` on a host without the optional package
    falls back to naive with a LOUD log and never raises.
  * DOCLING PRESENT, CPU MODE — a docling conversion stamps `docling_v1` and its
    MARKDOWN STRUCTURE (headings, tables) survives into the chunker's chunks.
  * FALLBACK NEVER CRASHES INGEST — a docling conversion that raises (or renders
    nothing) degrades to naive_v1; the caller keeps a parseable document.
  * THE STAMP REACHES THE ROW — `ingest_document_tool` puts `parser_version` on
    every `/documents/enqueue` body; the backend INSERT writes it, with honest
    degradation on a pre-298 schema.
  * REMINE_WITH_PARSER — the re-parse trigger stub: same-stamp re-chunk from the
    STORED original is served; a cross-parser request declines loudly
    (`needs_original_bytes`) rather than writing a lying stamp; unknown stamps
    are refused; the original row is never mutated.

Docling itself is faked for the seam tests (the real package is an optional extra,
absent from the default test install); the REAL parse comparison lives in the lane
report, run against the actual docling package in an isolated venv.
"""

import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.mcp.server as server
import src.ingest.doc_parser as dp
from src.ingest.binary_intake import ExtractedDocument


# ── helpers ──────────────────────────────────────────────────────────────────────

def _resp(json_obj, status=200):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.status_code = status
    r.json.return_value = json_obj
    return r


class _BlockDocling:
    """A meta-path finder that makes `docling*`/`docling_core*` UNIMPORTABLE — the
    default-install condition, enforced even on a host where the extra is installed."""

    PREFIXES = ("docling", "docling_core")

    def find_module(self, fullname, path=None):  # noqa: D401 (legacy hook is fine)
        return self.find_spec(fullname, path)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(self.PREFIXES):
            raise ImportError(f"[test] docling blocked (simulated absence): {fullname}")
        return None

    def exec_module(self, module):  # pragma: no cover - never reached
        raise ImportError("docling blocked")


@pytest.fixture
def docling_absent():
    finder = _BlockDocling()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)


@pytest.fixture
def stub_naive(monkeypatch):
    """Replace the NAIVE half (pdfplumber) so seam pins never depend on the PDF
    reader — the seam contract under test is dispatch + stamp + fallback."""
    from src.ingest.binary_intake import ExtractedPage

    def _fake_extract_pdf(data, max_pages=None):
        text = "naive fallback prose, comfortably past the forty-char text threshold"
        return ExtractedDocument(
            media_type="application/pdf",
            pages=(ExtractedPage(page_index=0, text=text,
                                 char_count=len(text), image_count=0),),
        )

    import src.ingest.binary_intake as bi
    monkeypatch.setattr(bi, "extract_pdf", _fake_extract_pdf)


class _FakeTextItem:
    def __init__(self, text, label="text", level=None, page_no=1):
        self.text = text
        self.label = label
        self.level = level
        self.prov = [SimpleNamespace(page_no=page_no)]


class _FakeTableItem:
    def __init__(self, markdown, page_no=1):
        self.label = "table"
        self.prov = [SimpleNamespace(page_no=page_no)]
        self._md = markdown

    def export_to_markdown(self):
        return self._md


class _FakePictureItem:
    def __init__(self, page_no=1):
        self.label = "picture"
        self.prov = [SimpleNamespace(page_no=page_no)]


class _FakeDoclingDocument:
    def __init__(self, items):
        self._items = items

    def iterate_items(self):
        for it in self._items:
            yield it, 1


class _FakeResult:
    def __init__(self, document):
        self.document = document


_BUILT_PIPELINE_OPTIONS: list = []   # captured converter configs, for the CPU pin


def _install_fake_docling(monkeypatch, items, boom=False):
    """A fake `docling` package tree whose converter returns the given items.

    Also RESETS the module's converter singleton: production caches the converter
    per process, so without the reset the first test's fake would serve every
    later docling test (cross-test cache pollution the pins must not tolerate)."""
    monkeypatch.setattr(dp, "_converter", None)
    docling = types.ModuleType("docling")
    dc_mod = types.ModuleType("docling.document_converter")

    class DocumentConverter:
        def __init__(self, format_options=None):
            self.format_options = format_options
            for opt in format_options.values():
                _BUILT_PIPELINE_OPTIONS.append(opt.pipeline_options)

        def convert(self, source, max_num_pages=None):
            if boom:
                raise RuntimeError("docling model load failed (fake)")
            return _FakeResult(_FakeDoclingDocument(items))

    class PdfFormatOption:
        def __init__(self, pipeline_options=None):
            self.pipeline_options = pipeline_options

    dc_mod.DocumentConverter = DocumentConverter
    dc_mod.PdfFormatOption = PdfFormatOption

    base_mod = types.ModuleType("docling.datamodel.base_models")
    base_mod.InputFormat = SimpleNamespace(PDF="pdf")
    po_mod = types.ModuleType("docling.datamodel.pipeline_options")

    class PdfPipelineOptions:
        def __init__(self):
            self.do_ocr = None
            self.do_table_structure = None
            self.accelerator_options = None

    po_mod.PdfPipelineOptions = PdfPipelineOptions
    ao_mod = types.ModuleType("docling.datamodel.accelerator_options")

    class AcceleratorDevice:
        CPU = "cpu"

    class AcceleratorOptions:
        def __init__(self, num_threads=1, device=None):
            self.num_threads = num_threads
            self.device = device

    ao_mod.AcceleratorDevice = AcceleratorDevice
    ao_mod.AcceleratorOptions = AcceleratorOptions

    dm_mod = types.ModuleType("docling.datamodel")
    core_mod = types.ModuleType("docling_core")
    core_types = types.ModuleType("docling_core.types")
    core_io = types.ModuleType("docling_core.types.io")

    class DocumentStream:
        def __init__(self, name=None, stream=None):
            self.name = name
            self.stream = stream

    core_io.DocumentStream = DocumentStream
    monkeypatch.setitem(sys.modules, "docling", docling)
    monkeypatch.setitem(sys.modules, "docling.document_converter", dc_mod)
    monkeypatch.setitem(sys.modules, "docling.datamodel", dm_mod)
    monkeypatch.setitem(sys.modules, "docling.datamodel.base_models", base_mod)
    monkeypatch.setitem(sys.modules, "docling.datamodel.pipeline_options", po_mod)
    monkeypatch.setitem(sys.modules, "docling.datamodel.accelerator_options", ao_mod)
    monkeypatch.setitem(sys.modules, "docling_core", core_mod)
    monkeypatch.setitem(sys.modules, "docling_core.types", core_types)
    monkeypatch.setitem(sys.modules, "docling_core.types.io", core_io)


# ── 1. default install / absence ─────────────────────────────────────────────────

def test_default_selection_is_naive_v1(monkeypatch):
    monkeypatch.delenv("DOC_PARSER", raising=False)
    assert dp.configured_parser_version() == "naive_v1"
    assert dp.selected_parser_name() == ""


def test_docling_selection_stamps_docling_v1(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    assert dp.configured_parser_version() == "docling_v1"


def test_unknown_selector_never_selects_docling(monkeypatch):
    """An env typo must not half-select docling — it degrades to naive."""
    monkeypatch.setenv("DOC_PARSER", "doclng")
    assert dp.configured_parser_version() == "naive_v1"


def test_docling_selected_but_absent_falls_back_to_naive_never_crashes(
        monkeypatch, docling_absent, stub_naive):
    """THE DEFAULT-INSTALL PIN: DOC_PARSER=docling on a host without the package →
    ONE loud fallback log → naive_v1 result. Import failure is the same road as a
    parse failure: fallback, never a crash."""
    monkeypatch.setenv("DOC_PARSER", "docling")
    crits = []
    monkeypatch.setattr(dp.log, "critical",
                        lambda msg, *a, **k: crits.append(str(msg)))
    doc, stamp = dp.extract_pdf_with_parser(b"%PDF-1.7 minimal bytes")
    assert stamp == "naive_v1"
    assert isinstance(doc, ExtractedDocument)
    assert doc.is_text_bearing
    assert crits and "docling_parser.fallback" in crits[0]


def test_naive_default_never_imports_docling(monkeypatch, docling_absent, stub_naive):
    monkeypatch.delenv("DOC_PARSER", raising=False)
    doc, stamp = dp.extract_pdf_with_parser(b"%PDF-1.7 whatever")
    assert stamp == "naive_v1"          # blocked import would only fire if docling
    assert isinstance(doc, ExtractedDocument)  # were actually attempted


# ── 2. docling present: stamp + markdown structure reaches the chunker ──────────

_HEADING = "Evaluation of Retrieval Augmented Generation"
_TABLE_MD = "| System | Score |\n|---|---|\n| MinerU | 86.47 |\n| Docling | 84.1 |"
_BODY = ("The corpus is ingested through a deterministic spine and the layout "
         "model supplies reading order ahead of chunking.")

_DOCLING_ITEMS = [
    _FakeTextItem(_HEADING, label="section_header", level=1, page_no=1),
    _FakeTextItem(_BODY, label="text", page_no=1),
    _FakeTableItem(_TABLE_MD, page_no=2),
]


def test_cpu_only_pin_device_cpu_and_ocr_off(monkeypatch):
    """THE CPU-ONLY HOST PIN (critic finding 9): deleting the AcceleratorDevice.CPU
    pin (→ CUDA-resolved torch default) or re-enabling OCR must go RED here — the
    hard host contract may not live only in comments."""
    monkeypatch.setenv("DOC_PARSER", "docling")
    _BUILT_PIPELINE_OPTIONS.clear()
    _install_fake_docling(monkeypatch, _DOCLING_ITEMS)
    doc, stamp = dp.extract_pdf_with_parser(b"%PDF-1.7 fake")
    assert stamp == "docling_v1"
    assert _BUILT_PIPELINE_OPTIONS, "converter was never built"
    opts = _BUILT_PIPELINE_OPTIONS[-1]
    assert opts.do_ocr is False, "OCR must stay deferred (stage B doctrine)"
    assert opts.do_table_structure is True, "tables must reach the chunker"
    assert opts.accelerator_options.device == "cpu", \
        "the converter must pin AcceleratorDevice.CPU — never the torch default"


def test_docling_parse_stamps_and_carries_markdown_structure(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    _install_fake_docling(monkeypatch, _DOCLING_ITEMS)
    doc, stamp = dp.extract_pdf_with_parser(b"%PDF-1.7 fake")
    assert stamp == "docling_v1"
    assert doc.media_type == "application/pdf"
    text = doc.full_text
    # HEADINGS and TABLES survive the parse as markdown...
    assert f"# {_HEADING}" in text
    assert "| System | Score |" in text
    # ...and the STRUCTURE reaches the chunker: the blank-line paragraph split
    # turns the heading/body/table boundaries into chunk boundaries.
    chunks = server._chunk_document(text)
    # The heading and the table are PARAGRAPH BOUNDARIES for the chunker: the
    # table never merges into the prose block, and the markdown survives verbatim.
    assert len(chunks) >= 2
    assert any("# " in c for c in chunks)
    assert any("MinerU" in c for c in chunks)
    assert not any("Evaluation" in c and "MinerU" in c for c in chunks), \
        "heading/blank-line boundaries must not collapse heading, prose and table"


def test_docling_pages_preserved_via_item_provenance(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    _install_fake_docling(monkeypatch, _DOCLING_ITEMS)
    doc, _stamp = dp.extract_pdf_with_parser(b"%PDF-1.7 fake")
    assert doc.page_count == 2
    assert doc.pages[0].page_index == 0
    assert doc.pages[1].page_index == 1
    assert _TABLE_MD in doc.pages[1].text


def test_running_heads_and_pictures_do_not_reach_the_text(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    items = [
        _FakeTextItem("Page 7", label="page_header", page_no=1),
        _FakePictureItem(page_no=1),
        _FakeTextItem("real content", label="text", page_no=1),
    ]
    _install_fake_docling(monkeypatch, items)
    doc, stamp = dp.extract_pdf_with_parser(b"%PDF-1.7 fake")
    assert stamp == "docling_v1"
    assert "Page 7" not in doc.full_text
    assert "real content" in doc.full_text


# ── 3. fallback never crashes ingest ─────────────────────────────────────────────

def test_a_docling_parse_failure_falls_back_and_stamps_naive(monkeypatch, stub_naive):
    monkeypatch.setenv("DOC_PARSER", "docling")
    _install_fake_docling(monkeypatch, [], boom=True)
    crits = []
    monkeypatch.setattr(dp.log, "critical", lambda msg, *a, **k: crits.append(str(msg)))
    doc, stamp = dp.extract_pdf_with_parser(b"%PDF-1.7 fake")
    assert stamp == "naive_v1"
    assert any("docling_parser.fallback" in c for c in crits)


def test_a_docling_empty_render_falls_back_to_naive(monkeypatch, stub_naive):
    """Docling 'worked' but read nothing — naive gets the chance naive always had."""
    monkeypatch.setenv("DOC_PARSER", "docling")
    _install_fake_docling(monkeypatch, [_FakePictureItem(page_no=1)])
    crits = []
    monkeypatch.setattr(dp.log, "critical", lambda msg, *a, **k: crits.append(str(msg)))
    doc, stamp = dp.extract_pdf_with_parser(b"%PDF-1.7 fake")
    assert stamp == "naive_v1"
    assert any("docling_empty" in c for c in crits)


def test_one_bad_table_does_not_kill_the_page(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    bad = _FakeTableItem("| broken |")
    bad.export_to_markdown = lambda: (_ for _ in ()).throw(RuntimeError("bad table"))
    _install_fake_docling(monkeypatch, [bad, _FakeTextItem("prose survives", page_no=1)])
    doc, stamp = dp.extract_pdf_with_parser(b"%PDF-1.7 fake")
    assert stamp == "docling_v1"
    assert "prose survives" in doc.full_text


# ── 4. the stamp reaches the row ─────────────────────────────────────────────────

def test_ingest_document_enqueues_with_parser_version_stamp(monkeypatch):
    """Every /documents/enqueue body carries the parser stamp — default naive_v1."""
    posts = []

    class _Client:
        async def post(self, url, json=None, timeout=None, headers=None):
            posts.append((url, json))
            return _resp({"status": "pending", "document_id": 1,
                          "chunk_count": len(json.get("chunks", [])),
                          "eta_seconds": 5, "truncated": False})

    monkeypatch.setattr(server, "_client", lambda: _Client())
    monkeypatch.setattr(server, "_check_injection_signals", lambda t: None)

    import asyncio

    async def _run():
        return await server.ingest_document_tool("Paragraph one.\n\nParagraph two.", "u-r5")

    summary = asyncio.run(_run())
    enqueue_bodies = [j for url, j in posts if url.endswith("/documents/enqueue")]
    assert enqueue_bodies, "enqueue never happened"
    assert all(b.get("parser_version") == "naive_v1" for b in enqueue_bodies)
    assert summary["status"] in ("pending", "ok")


def test_ingest_document_stamps_docling_version_when_given(monkeypatch):
    posts = []

    class _Client:
        async def post(self, url, json=None, timeout=None, headers=None):
            posts.append((url, json))
            return _resp({"status": "pending", "document_id": 2,
                          "chunk_count": 1, "eta_seconds": 5, "truncated": False})

    monkeypatch.setattr(server, "_client", lambda: _Client())
    monkeypatch.setattr(server, "_check_injection_signals", lambda t: None)

    import asyncio

    async def _run():
        return await server.ingest_document_tool(
            "Layout-aware prose.", "u-r5", parser_version="docling_v1")

    asyncio.run(_run())
    enqueue_bodies = [j for url, j in posts if url.endswith("/documents/enqueue")]
    assert enqueue_bodies and enqueue_bodies[0]["parser_version"] == "docling_v1"


def test_ingest_file_core_passes_the_parser_stamp_to_the_text_lane(monkeypatch):
    """The binary door: DOC_PARSER=docling → the document row is stamped docling_v1."""
    monkeypatch.setenv("DOC_PARSER", "docling")
    _install_fake_docling(monkeypatch, [
        _FakeTextItem("Heading", label="section_header", level=1, page_no=1),
        _FakeTextItem("Body prose of the uploaded research paper.", label="text", page_no=1),
    ])
    captured = {}

    async def fake_ingest_document_tool(text, user_id, source_ref="", title="",
                                        parser_version="naive_v1"):
        captured["parser_version"] = parser_version
        return {"status": "pending", "document_id": 9}

    monkeypatch.setattr(server, "ingest_document_tool", fake_ingest_document_tool)
    monkeypatch.setattr(server, "_check_injection_signals", lambda t: None)
    monkeypatch.setattr(server, "_post_artefact_retain", AsyncMock(
        return_value={"status": "ok", "artefact_id": "a1"}))
    monkeypatch.setattr(server, "_ingest_with_retry", AsyncMock(
        return_value=({"committed": 1}, None)))
    monkeypatch.setattr(server, "_artefact_placements", lambda *a, **k: [])

    import asyncio
    result = asyncio.run(server.ingest_file_core(
        user_id="u-r5", data=b"%PDF-1.7 fake", claimed_name="paper.pdf",
        source_ref="arxiv:1234", title="Paper"))
    assert captured["parser_version"] == "docling_v1"
    assert result["status"] == "pending"


def test_backend_enqueue_writes_parser_version_and_degrades_on_pre298(monkeypatch):
    """The INSERT names parser_version; a pre-298 schema falls back WITHOUT the
    column (the row then honestly reads the naive_v1 default)."""
    import psycopg2

    import src.api.main as apimain
    monkeypatch.setattr(apimain, "_INGEST_ENABLED", True)
    monkeypatch.setattr(apimain, "_derive_tenant_schema", lambda u: "faultline_u_r5")

    executed = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))
            if "parser_version" in sql and sql.strip().upper().startswith("INSERT"):
                raise psycopg2.errors.UndefinedColumn(
                    'column "parser_version" of relation "documents" does not exist')

        def fetchone(self):
            return (4242,)

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(apimain.psycopg2, "connect", lambda dsn: _Conn())
    req = apimain.DocumentEnqueueRequest(
        user_id="u-r5", chunks=["hello world"], chunk_count=1,
        parser_version="docling_v1")
    out = apimain.documents_enqueue(req)
    assert out["status"] == "pending" and out["document_id"] == 4242
    inserts = [sql for sql, _p in executed if sql.upper().startswith("INSERT")]
    assert any("parser_version" in s for s in inserts), "stamped INSERT never attempted"
    fallback = inserts[-1]
    assert "parser_version" not in fallback, "fallback INSERT must omit the 298 column"
    assert "part_index" in fallback, "fallback must keep the 195-era part accounting"


def test_backend_documents_source_reads_stored_original_and_stamps(monkeypatch):
    import src.api.main as apimain
    monkeypatch.setattr(apimain, "_derive_tenant_schema", lambda u: "faultline_u_r5")

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self._sql = sql

        def fetchone(self):
            if "parser_version" in self._sql:
                return (7, "arxiv:42", "A Paper", ["chunk a", "chunk b"],
                        "docling_v1", "ready", 0, 1, 5)
            return (7, "arxiv:42", "A Paper", ["chunk a", "chunk b"], "ready", 0, 1, 5)

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(apimain.psycopg2, "connect", lambda dsn: _Conn())
    out = apimain.documents_source_remine(
        apimain.DocumentSourceRequest(user_id="u-r5", document_id=7))
    assert out["status"] == "ok"
    assert out["parser_version"] == "docling_v1"
    assert out["chunks"] == ["chunk a", "chunk b"]


# ── 5. remine_with_parser — the re-parse trigger stub ────────────────────────────

def _route_client(posts, source_payload):
    class _Client:
        async def post(self, url, json=None, timeout=None, headers=None):
            posts.append((url, json))
            if url.endswith("/documents/source"):
                return _resp(source_payload)
            return _resp({"status": "pending", "document_id": 55,
                          "chunk_count": 1, "eta_seconds": 5, "truncated": False})

    return _Client()


def test_remine_same_stamp_rechunks_from_the_stored_original(monkeypatch):
    posts = []
    monkeypatch.setattr(server, "_client", lambda: _route_client(posts, {
        "status": "ok", "document_id": 7, "source_ref": "arxiv:42",
        "title": "A Paper", "chunks": ["chunk a", "chunk b"],
        "chunk_count": 2, "parser_version": "naive_v1", "row_status": "partial",
    }))
    monkeypatch.setattr(server, "_check_injection_signals", lambda t: None)

    import asyncio
    out = asyncio.run(server.remine_with_parser("u-r5", 7, "naive_v1"))
    assert out["status"] == "requeued"
    assert out["source_document_id"] == 7
    assert out["parser_version"] == "naive_v1"
    enqueues = [j for url, j in posts if url.endswith("/documents/enqueue")]
    assert enqueues, "the re-mine must enqueue a fresh row"
    assert all(b["parser_version"] == "naive_v1" for b in enqueues)
    joined = "\n\n".join(b for b in enqueues[0]["chunks"])
    assert "chunk a" in joined and "chunk b" in joined


def test_remine_cross_parser_declines_rather_than_lie_about_the_stamp(monkeypatch):
    """A naive-stored row cannot be 're-mined as docling' from chunk text — the
    honest answer is needs_original_bytes, never a docling-stamped naive row."""
    posts = []
    crits = []
    monkeypatch.setattr(server, "_client", lambda: _route_client(posts, {
        "status": "ok", "document_id": 7, "source_ref": None, "title": None,
        "chunks": ["chunk a"], "chunk_count": 1,
        "parser_version": "naive_v1", "row_status": "partial",
    }))
    import asyncio
    out = asyncio.run(server.remine_with_parser("u-r5", 7, "docling_v1"))
    assert out["status"] == "needs_original_bytes"
    assert out["row_parser_version"] == "naive_v1"
    enqueues = [j for url, j in posts if url.endswith("/documents/enqueue")]
    assert not enqueues, "a declined re-mine must not write a row"


def test_remine_unknown_stamp_is_refused(monkeypatch):
    posts = []
    monkeypatch.setattr(server, "_client",
                        lambda: _route_client(posts, {"status": "ok"}))
    import asyncio
    out = asyncio.run(server.remine_with_parser("u-r5", 7, "gpt_v9"))
    assert out["status"] == "unknown_parser_version"
    assert not [j for url, j in posts if url.endswith("/documents/enqueue")]


def test_remine_invalid_document_id_is_caller_error_not_outage(monkeypatch):
    """critic finding 3: a garbage id is a self-correctable caller error, decided
    OUTSIDE the read try — it must never masquerade as source_unavailable."""
    posts = []
    monkeypatch.setattr(server, "_client", lambda: _route_client(posts, {"status": "ok"}))
    import asyncio
    out = asyncio.run(server.remine_with_parser("u-r5", "not-a-number", "naive_v1"))
    assert out["status"] == "invalid_document_id"
    assert not [j for url, j in posts], "no read may even be attempted for a garbage id"


def test_remine_surfaces_source_part_context(monkeypatch):
    """critic finding 2 (stub scope): the re-mine REPORTS the source row's DOCLOSS
    part accounting instead of silently implying 'not a slice' (threading is TODO(R2))."""
    posts = []
    monkeypatch.setattr(server, "_client", lambda: _route_client(posts, {
        "status": "ok", "document_id": 7, "source_ref": "big.pdf",
        "title": "Big", "chunks": ["part text"], "chunk_count": 1,
        "parser_version": "naive_v1", "row_status": "partial",
        "part_index": 2, "part_count": 5, "total_chunks": 640,
    }))
    monkeypatch.setattr(server, "_check_injection_signals", lambda t: None)
    import asyncio
    out = asyncio.run(server.remine_with_parser("u-r5", 7, "naive_v1"))
    assert out["source_part_index"] == 2
    assert out["source_part_count"] == 5
    assert out["source_total_chunks"] == 640
    assert out["row_status"] == "partial"


def test_backend_enqueue_refuses_a_lying_stamp_shape(monkeypatch):
    """critic finding 4: the write door refuses a parser_version outside the
    [a-z0-9_]+ shape — garbage is never stored as provenance — and NORMALIZES
    case/whitespace before the INSERT (the stored stamp is the clean form)."""
    import psycopg2

    import src.api.main as apimain
    monkeypatch.setattr(apimain, "_INGEST_ENABLED", True)
    monkeypatch.setattr(apimain, "_derive_tenant_schema", lambda u: "faultline_u_r5")

    executed = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))

        def fetchone(self):
            return (99,)

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(apimain.psycopg2, "connect", lambda dsn: _Conn())

    refused = apimain.documents_enqueue(apimain.DocumentEnqueueRequest(
        user_id="u-r5", chunks=["hello"], parser_version="Docling V1!"))
    assert refused["status"] == "invalid_parser_version"
    assert refused["document_id"] is None
    assert not [sql for sql, _ in executed if sql.upper().startswith("INSERT")], \
        "a refused stamp must never reach an INSERT"

    executed.clear()
    ok = apimain.documents_enqueue(apimain.DocumentEnqueueRequest(
        user_id="u-r5", chunks=["hello"], parser_version="  Docling_v1  "))
    assert ok["status"] == "pending"
    inserts = [(sql, p) for sql, p in executed if sql.upper().startswith("INSERT")]
    assert inserts and inserts[0][1][-1] == "docling_v1", \
        f"stamp must be normalized to the clean form, got {inserts[0][1][-1]!r}"


def test_remine_source_read_failure_is_loud_and_honest(monkeypatch):
    class _Broken:
        def post(self, url, json=None, timeout=None, headers=None):
            raise RuntimeError("backend down")

    monkeypatch.setattr(server, "_client", lambda: _Broken())
    import asyncio
    out = asyncio.run(server.remine_with_parser("u-r5", 7, "naive_v1"))
    assert out["status"] == "source_unavailable"
    assert out.get("isError") is True


def test_remine_missing_document_is_not_found_not_enqueued(monkeypatch):
    posts = []
    monkeypatch.setattr(server, "_client", lambda: _route_client(posts, {
        "status": "not_found", "chunks": []}))
    import asyncio
    out = asyncio.run(server.remine_with_parser("u-r5", 999, "naive_v1"))
    assert out["status"] == "not_found"
    assert not [j for url, j in posts if url.endswith("/documents/enqueue")]
