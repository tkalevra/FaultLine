"""THE PARSER SEAM — a layout-aware parse step ahead of the document chunker.

Spec: ``DEV/RESEARCH/doc-ingestion-sota-research-20261021.md`` recommendation R5;
``DEV/RESEARCH/doc-ingestion-litmus-reeval-20261021.md`` (dense research prose
mis-parses on the deterministic spine and leans on the LLM — the 36-RPM wound).

WHAT THIS MODULE IS
-------------------
One seam, two implementations, one honest stamp. Today the binary door parsed PDFs
with whatever pdfplumber's line-walk produced (``naive_v1``) — correct for digital
PDFs but blind to reading order, headings and tables, so dense research prose fed
the spine mush and the LLM leg burned the brain budget. This module makes the
parser a NAMED, SELECTABLE, STAMPED step:

* ``DOC_PARSER=naive`` (default) — today's pdfplumber path, stamped ``naive_v1``.
  Byte-for-byte the legacy behaviour; the default install never imports Docling.
* ``DOC_PARSER=docling`` — IBM Docling in CPU mode, stamped ``docling_v1``:
  layout model + TableFormer, headings/tables/reading order rendered as markdown
  that reaches the chunker.

THE STAMP IS THE CONTRACT
-------------------------
Every document row written from a parse carries its ``parser_version``
(``documents.parser_version``). A chunk's provenance is decided by the ROW it
sits in — one row is one parser's output, never a mix — so ``documents.chunks``
keeps its string-list shape and the re-parse trigger (``remine_with_parser``)
can select and rebuild by stamp. Legacy rows read ``naive_v1`` because that is
honestly what parsed them (column default, migration 298).

DEPENDENCY POSTURE — OPTIONAL, LAZY, FALLBACK, NEVER A CRASH
------------------------------------------------------------
Docling is NOT in the base install (``pyproject.toml`` →
``[project.optional-dependencies].docling``). It is imported lazily inside the
parse call, so ``DOC_PARSER=docling`` on a host without it → ONE loud CRIT log →
fallback to ``naive_v1`` → ingest completes. ANY docling failure (import, model
load, parse, conversion) takes the same road: the artefact is never lost, the
caller never sees a crash, and the row is stamped with what ACTUALLY parsed it.

CPU DISCIPLINE (owner constraint: CPU-only host)
------------------------------------------------
The converter is built with ``AcceleratorDevice.CPU`` explicitly. ⚠️ INSTALL
NOTE: bare ``pip install docling`` resolves torch to the CUDA-bundled Linux
wheels (measured 2026-10-21: ``nvidia_cudnn_cu13`` ~553 MB pulled). The
CPU-only install is::

    pip install torch --index-url https://download.pytorch.org/whl/cpu
    pip install docling

DOC_PARSER is a TEXT-PDF lane. OCR stays DEFERRED to stage B (``do_ocr=False``)
per the artefact doctrine — scanned pages still come out ``retained_no_text``
honestly. The first docling conversion loads its layout models into the local
model cache (a first-use fetch, like GLiNER2's model fetch); after that it is
local and deterministic.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from .binary_intake import MAX_PAGES, ExtractedDocument

log = logging.getLogger(__name__)

#: Parser stamps. ``naive_v1`` = the pdfplumber line-walk (and raw pasted text);
#: ``docling_v1`` = Docling CPU layout-aware conversion.
PARSER_NAIVE = "naive_v1"
PARSER_DOCLING = "docling_v1"

#: The env selector. Read at CALL time (never import time) so tests and callers
#: can flip it; unknown values fall back to naive with a loud log — an env typo
#: must never take the parse lane down.
ENV_DOC_PARSER = "DOC_PARSER"

_DOCLING_SELECTORS = frozenset({"docling", "docling_v1"})

# The converter is built ONCE per process (critic finding: rebuilding reloads the
# layout/TableFormer weights from disk on EVERY parse — a real per-document cost
# on the CPU-only host). Built lazily under a lock; reused thereafter. The
# conversion itself is invoked per parse and is not shared state.
_converter = None
_converter_lock = None


def _get_converter():
    global _converter, _converter_lock
    if _converter_lock is None:
        import threading
        _converter_lock = threading.Lock()
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption

                pipeline_options = PdfPipelineOptions()
                pipeline_options.do_ocr = False             # OCR is stage B; DEFERRED (artefact doctrine)
                pipeline_options.do_table_structure = True  # tables must reach the chunker as tables
                pipeline_options.accelerator_options = AcceleratorOptions(
                    num_threads=max(1, (os.cpu_count() or 2) // 2), device=AcceleratorDevice.CPU)

                _converter = DocumentConverter(format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)})
    return _converter


def selected_parser_name() -> str:
    """The raw env selector, lowercased. Empty when unset/naive."""
    return (os.getenv(ENV_DOC_PARSER, "") or "").strip().lower()


def configured_parser_version() -> str:
    """The parser stamp the CURRENT configuration would use.

    ``docling_v1`` is returned when DOC_PARSER *selects* docling — even when the
    package is absent — because this answers "what is CONFIGURED"; the FALLBACK
    that happens at parse time is recorded where it happens (the note on the
    ExtractedDocument and the parser_version on the row actually written).
    Deliberately named configured_* (not active_*): the only stamp a write path
    may use is the one returned by the parse itself — never this answer.
    """
    return PARSER_DOCLING if selected_parser_name() in _DOCLING_SELECTORS else PARSER_NAIVE


def _note_fallback(where: str, detail: str) -> None:
    """THE LOUD FALLBACK LINE. One CRIT per parse failure — the operator must be
    able to grep for ``docling_parser.fallback`` and discover that DOC_PARSER
    selects docling but docling is not actually serving (absent package, broken
    model load, unreadable PDF). stdlib logger, f-string form (embedder.py rule)."""
    log.critical(
        f"docling_parser.fallback where={where} — DOC_PARSER=docling selected but docling "
        f"did not serve this parse; falling back to {PARSER_NAIVE}. detail: {detail[:400]}"
    )


def _render_item_markdown(item: Any) -> str:
    """One DoclingDocument item → its markdown slice. Tables render as markdown."""
    cls_name = item.__class__.__name__
    label = str(getattr(item, "label", "") or "")
    if cls_name == "TableItem" or label == "table":
        try:
            return item.export_to_markdown() or ""
        except Exception:  # noqa: BLE001 — one bad table must not kill the page
            return ""
    if cls_name == "PictureItem":
        return ""                      # pixels are stage B; captions arrive via provenance
    text = getattr(item, "text", None)
    if not text or not str(text).strip():
        return ""
    text = str(text).strip()
    if "section_header" in label or label == "heading":
        try:
            lvl = int(getattr(item, "level", 1) or 1)
        except (TypeError, ValueError):
            lvl = 1
        return f"{'#' * min(max(lvl, 1), 6)} {text}"
    if label in ("page_header", "page_footer"):
        return ""                      # running heads are not content
    return text


def _docling_extract(data: bytes, max_pages: int | None) -> ExtractedDocument:
    """Docling CPU conversion → ExtractedDocument.

    Raises anything docling raises — the caller owns the fallback. The import is
    lazy: this function only runs when DOC_PARSER selects docling, so the base
    install never even pays the import attempt.
    """
    import io

    from docling_core.types.io import DocumentStream

    converter = _get_converter()

    # convert() takes a Path/URL — or, for in-memory bytes, a DocumentStream
    # (docling_core.types.io). A bare BytesIO is REJECTED by the pydantic signature
    # (measured on docling 2.141: 4 validation errors) → the seam would fall back
    # to naive on every parse, which is exactly the loud fallback the contract
    # wants for a wrong call and exactly what we must not ship.
    stream = DocumentStream(name="upload.pdf", stream=io.BytesIO(data))
    result = converter.convert(
        stream,
        max_num_pages=MAX_PAGES if max_pages is None else min(MAX_PAGES, max_pages))
    doc = result.document

    # Group the DoclingDocument's items by SOURCE PAGE so the per-page ExtractedPage
    # model (page_index/text/char_count) is preserved — counters and the artefact
    # caption tie both read pages. Markdown rendering per item keeps the structure
    # (headings as '#', tables as markdown) that the chunker's blank-line paragraph
    # split then turns into chunk boundaries.
    page_markdown: dict[int, list[str]] = {}
    for item, _level in doc.iterate_items():
        prov = getattr(item, "prov", None)
        try:
            page_no = int(prov[0].page_no) if prov else 1
        except (TypeError, ValueError, IndexError):
            page_no = 1
        md = _render_item_markdown(item)
        if md and md.strip():
            page_markdown.setdefault(page_no, []).append(md)

    from .binary_intake import ExtractedPage
    # Items are joined on a BLANK LINE — each layout block is a paragraph boundary
    # for the chunker, which is precisely how headings/tables structure the chunks.
    page_text = {page_no: "\n\n".join(lines) for page_no, lines in page_markdown.items()}
    pages = tuple(
        ExtractedPage(
            page_index=page_no - 1,          # docling pages are 1-based; ExtractedPage is 0-based
            text=text,
            char_count=len(text.strip()),
            image_count=0,                   # picture text is not read (stage B); count stays 0
        )
        for page_no, text in sorted(page_text.items())
    )
    return ExtractedDocument(
        media_type="application/pdf",
        pages=pages,
        layouts=(),
        # docling text carries its own coordinates; the pdfplumber PageLayout convention
        # is NOT produced by this lane, so stage A.5 caption geometry stays naive-lane
        # for now — honest downgrade, recorded in the counters the caller reports.
        geometry_available=False,
        notes={"parser": PARSER_DOCLING},
    )


def extract_pdf_with_parser(data: bytes, *, max_pages: int | None = None) -> tuple[ExtractedDocument, str]:
    """THE SEAM: PDF bytes → ``(ExtractedDocument, parser_version actually used)``.

    Dispatch on ``DOC_PARSER``. Any docling failure (absent package, model load,
    conversion) logs ONE loud CRIT and falls back to ``naive_v1`` — ingest never
    crashes and the returned stamp says what ACTUALLY parsed the bytes.
    """
    if selected_parser_name() in _DOCLING_SELECTORS:
        try:
            doc = _docling_extract(data, max_pages)
            # "Did docling read ANYTHING" is full_text non-empty — NOT the naive
            # lane's 40-char/page born-digital threshold, which would call a real
            # (but short) layout parse empty and pointlessly re-parse with naive.
            if doc.page_count > 0 and doc.full_text.strip():
                return doc, PARSER_DOCLING
            _note_fallback("docling_empty",
                           "docling produced no text for this PDF; naive retried below")
        except Exception as exc:  # noqa: BLE001 — THE CONTRACT: fallback, never a crash
            _note_fallback("docling_error", f"{type(exc).__name__}: {exc}")

    from .binary_intake import extract_pdf
    return extract_pdf(data, max_pages=max_pages), PARSER_NAIVE
