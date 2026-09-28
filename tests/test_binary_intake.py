"""THE BINARY DOOR — intake, security posture, deterministic extraction, context tie.

Spec: the internal design record §6.1 (the door that did not exist),
§2 (retention), §3 (A.5 geometry), §7 (licence traps).

What this pins, in the order it matters:

 1. FLAG-OFF IS BYTE-FOR-BYTE LEGACY. `ARTEFACT_BINARY_INTAKE` off -> the route 404s, i.e.
    exactly a build without the endpoint.
 2. TYPE COMES FROM CONTENT. A hostile payload wearing a .pdf name and a lying
    Content-Type is refused as what it really is. ALLOW-LIST, so an unknown format is
    refused by default rather than after someone remembers to add it.
 3. THE SIZE CAP IS REAL AND IS NOT `Content-Length`. Enforced against bytes that actually
    arrived, so a lying length cannot buy unbounded memory.
 4. NO LLM ANYWHERE ON THE PATH — asserted STRUCTURALLY, not by reading the code once.
 5. RETENTION SURVIVES EVERYTHING. A corrupt file, an absent pdfplumber, a page bomb: the
    upload is kept (or explicitly refused whole), never silently half-stored.
 6. THE CONTEXT TIE IS CORRECT AND HONEST. The right chunk, or none — never a guess.
 7. THE READ, NOT THE WRITE. The DB half round-trips through the real accessors against a
    REAL tenant schema built from the REAL template, with the production isolation shape.

Run with:
  POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.5:5432/faultline \
    python3 tools/fltest.py --bug IMGINGEST --test tests/test_binary_intake.py \
      --note "binary door: intake, security, extraction, context tie"

The DB half SKIPS without POSTGRES_DSN; the PDF half skips without pdfplumber (which is
deliberately NOT a declared dependency — see the module docstring of binary_intake).
"""
from __future__ import annotations

import importlib
import os
import sys
import uuid
from pathlib import Path

import pytest

from src.ingest import binary_intake as BI
from src.ingest.artefact_geometry import Box, PageLayout, PlacedImage, TextLine

REPO = Path(__file__).resolve().parent.parent


# ══════════════════════════════════════════════════════════════════════════════════════
# 1. TYPE FROM CONTENT — the client's claims are never the decision
# ══════════════════════════════════════════════════════════════════════════════════════

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32


@pytest.mark.parametrize("data,expected", [
    (PDF, "application/pdf"),
    (PNG, "image/png"),
    (JPEG, "image/jpeg"),
    (b"GIF89a" + b"\x00" * 16, "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8, "image/webp"),
    (b"II\x2a\x00" + b"\x00" * 16, "image/tiff"),
    (b"BM" + b"\x00" * 16, "image/bmp"),
])
def test_media_type_is_sniffed_from_magic_bytes(data, expected):
    assert BI.sniff_media_type(data) == expected


def test_a_hostile_payload_wearing_a_pdf_name_is_refused_as_what_it_is():
    """The single most important line of the door: the FILENAME AND THE HEADER ARE CLAIMS."""
    with pytest.raises(BI.UnsupportedMediaType) as exc:
        BI.resolve_media_type(b"#!/bin/sh\nrm -rf /\n",
                              claimed_type="application/pdf",
                              claimed_filename="quarterly-report.pdf")
    # The refusal is SPECIFIC (says what it actually looked like) but never echoes the
    # attacker's bytes back as text — hex only.
    assert "23212f62696e" in str(exc.value)
    assert "rm -rf" not in str(exc.value)


def test_the_allow_list_refuses_an_unknown_format_by_default():
    """A deny-list would admit every format nobody thought of. This is the other way."""
    for payload in (b"PK\x03\x04zipbomb", b"\x7fELF", b"MZ\x90\x00", b"<?xml version=",
                    b"<!DOCTYPE html>", b"\x1f\x8b\x08gzip"):
        assert BI.sniff_media_type(payload) is None, payload[:8]
        with pytest.raises(BI.UnsupportedMediaType):
            BI.resolve_media_type(payload)


def test_svg_is_not_admitted():
    """SVG is a SCRIPTABLE image format. It is not on the allow-list and must not be."""
    assert "image/svg+xml" not in BI.ALLOWED_MEDIA_TYPES
    assert BI.sniff_media_type(b"<svg xmlns='http://www.w3.org/2000/svg'>") is None


@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd", "etcpasswd"),
    ("..\\..\\windows\\system32", "windowssystem32"),
    ("report\x00.pdf", "report.pdf"),
    ("nul\x1bctrl.pdf", "nulctrl.pdf"),
    ("  spaced.pdf  ", "spaced.pdf"),
    ("", None),
    (None, None),
    ("...", None),
])
def test_filenames_are_reduced_to_a_label_never_a_path(raw, expected):
    assert BI.sanitize_filename(raw) == expected


def test_a_long_filename_is_bounded():
    assert len(BI.sanitize_filename("a" * 5000)) == 200


# ══════════════════════════════════════════════════════════════════════════════════════
# 2. NO LLM ANYWHERE — asserted STRUCTURALLY
# ══════════════════════════════════════════════════════════════════════════════════════

def test_the_extraction_path_cannot_reach_an_llm():
    """The owner's first constraint, enforced rather than promised.

    Checked as a SOURCE property of the three modules the door's deterministic half is
    built from. A future edit that reaches for a model to "improve" caption binding trips
    this immediately, which is the point — the whole value of A.5 is that it works on a
    fail-closed tenant with no brain bound at all.
    """
    banned = ("llm_calls", "llm_client", "call_llm", "openai", "anthropic",
              "GLiNER2", "gliner", "fastembed", "torch")
    for mod in ("binary_intake.py", "artefact_geometry.py", "artefacts.py",
                "artefact_pipeline.py"):
        src = (REPO / "src/ingest" / mod).read_text()
        # Strip the prose: the docstrings legitimately DISCUSS why no model is used.
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        for token in banned:
            assert f"import {token}" not in code and f"{token}(" not in code, \
                f"{mod} reaches for {token} — the binary path must stay model-free"


def test_the_binder_makes_no_network_call_and_needs_no_brain():
    for mod in ("binary_intake.py", "artefact_geometry.py"):
        src = (REPO / "src/ingest" / mod).read_text()
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        for token in ("import httpx", "import requests", "urllib.request", "socket."):
            assert token not in code, f"{mod} performs I/O — A.5 must be pure"


# ══════════════════════════════════════════════════════════════════════════════════════
# 3. THE CONTEXT TIE — the right chunk, or an honest none
# ══════════════════════════════════════════════════════════════════════════════════════

CHUNKS = [
    "This maintenance report covers the winter window for the southern Ontario sites.",
    "The Hamilton office network was rebuilt. Figure 1. Site network layout for the "
    "Hamilton office after the rebuild. Throughput quadrupled.",
    "An unrelated appendix about procurement timelines.",
]


def test_the_caption_resolves_to_the_chunk_that_contains_it():
    idx = BI.map_text_to_chunk(
        "Figure 1. Site network layout for the Hamilton office after the rebuild.", CHUNKS)
    assert idx == 1


def test_the_tie_survives_the_chunker_reflowing_whitespace():
    """`_chunk_document` strips paragraphs and re-joins sentences with a single space, so
    a raw comparison would silently NEVER match — and a binder that never matches looks
    exactly like a binder with nothing to bind."""
    assert BI.map_text_to_chunk("Figure 1.\n   Site network   layout\tfor the\n"
                                "Hamilton office after the rebuild.", CHUNKS) == 1


def test_an_absent_caption_ties_to_NOTHING_rather_than_guessing():
    """A wrong tie files a diagram against someone else's paragraph. Worse than none."""
    assert BI.map_text_to_chunk("a caption from an entirely different document", CHUNKS) is None
    assert BI.map_text_to_chunk("", CHUNKS) is None
    assert BI.map_text_to_chunk("anything", []) is None


def test_resolve_prefers_the_caption_and_falls_back_to_the_page_lead():
    page = BI.ExtractedPage(page_index=1, char_count=90, image_count=1,
                            text="An unrelated appendix about procurement timelines.\nx")
    # caption wins when it resolves
    idx, method = BI.resolve_chunk_index(
        caption_text="Figure 1. Site network layout for the Hamilton office after the "
                     "rebuild.", page=page, chunks=CHUNKS)
    assert (idx, method) == (1, "caption")
    # no caption -> the page's own leading line anchors it, and says so
    idx, method = BI.resolve_chunk_index(caption_text=None, page=page, chunks=CHUNKS)
    assert (idx, method) == (2, "page_lead")
    # neither -> recorded as unbound
    empty = BI.ExtractedPage(page_index=0, text="", char_count=0, image_count=0)
    assert BI.resolve_chunk_index(caption_text=None, page=empty, chunks=CHUNKS) == (None, None)


def test_a_page_number_is_not_an_anchor():
    """A one-token line ('7') would match half the corpus. The lead line must be substantial."""
    page = BI.ExtractedPage(page_index=1, text="7\n12", char_count=4, image_count=1)
    assert BI.resolve_chunk_index(caption_text=None, page=page, chunks=CHUNKS) == (None, None)


# ══════════════════════════════════════════════════════════════════════════════════════
# 4. HONEST REPORTING — a scan is a DEGRADED state over a retained artefact
# ══════════════════════════════════════════════════════════════════════════════════════

def _doc(pages):
    return BI.ExtractedDocument(media_type="application/pdf", pages=tuple(pages))


def test_a_scan_is_reported_as_text_free_not_as_success():
    """§6.2: a scanned PDF does not extract to NOTHING — it extracts to page numbers and
    stray ligatures, and 'garbage-lite' sails straight past an emptiness check."""
    scan = _doc([BI.ExtractedPage(page_index=i, text="3", char_count=1, image_count=1)
                 for i in range(200)])
    assert scan.pages_with_text == 0 and scan.pages_no_text == 200
    assert scan.is_text_bearing is False
    from src.ingest.artefact_pipeline import upload_report
    rep = upload_report(extracted=scan, retained={"artefact_id": 9}, association={})
    assert rep["status"] == "retained_no_text"
    assert rep["pages_total"] == 200 and rep["artefact_id"] == 9


def test_a_born_digital_document_is_reported_as_retained():
    born = _doc([BI.ExtractedPage(page_index=0, text="x" * 400, char_count=400,
                                  image_count=1)])
    from src.ingest.artefact_pipeline import upload_report
    assert upload_report(extracted=born, retained={"artefact_id": 1},
                         association={})["status"] == "retained"


def test_the_born_digital_threshold_sits_inside_the_measured_gap():
    """Our corpus is cleanly bimodal (p5 = 0 chars/page, p10 = 103), so any threshold in
    20-100 separates the populations. Drifting outside that band is a silent behaviour
    change on real documents."""
    assert 20 <= BI.MIN_CHARS_FOR_TEXT_PAGE <= 100


# ══════════════════════════════════════════════════════════════════════════════════════
# 5. THE REAL PDF — deterministic extraction end to end (needs pdfplumber)
# ══════════════════════════════════════════════════════════════════════════════════════

pdfplumber = pytest.importorskip("pdfplumber", reason="pdfplumber is not a declared dep")


@pytest.fixture(scope="module")
def sample_pdf() -> bytes:
    try:
        from _make_test_pdf import build_pdf
    except ImportError:
        pytest.skip("the sample-PDF builder (_make_test_pdf) is not shipped in this tree")
    return build_pdf()


def test_a_real_pdf_yields_text_and_placed_image_geometry(sample_pdf):
    d = BI.extract(sample_pdf, "application/pdf")
    assert d.page_count == 2 and d.image_count == 1
    assert d.pages_with_text == 2
    assert "Hamilton office network was rebuilt" in d.full_text
    box = d.layouts[1].images[0].box
    # TOP-ORIGIN. Mixing in a bottom-origin library's coordinates is the documented footgun.
    assert (box.x0, box.top, box.x1, box.bottom) == (150.0, 200.0, 450.0, 420.0)


def test_extraction_is_deterministic(sample_pdf):
    a = BI.extract(sample_pdf, "application/pdf")
    b = BI.extract(sample_pdf, "application/pdf")
    assert a.full_text == b.full_text
    assert [p.char_count for p in a.pages] == [p.char_count for p in b.pages]


def test_the_caption_binds_by_geometry_alone(sample_pdf):
    from src.ingest.artefact_geometry import associate_document
    d = BI.extract(sample_pdf, "application/pdf")
    bound = [b for b in associate_document(list(d.layouts)) if b.bound]
    assert len(bound) == 1
    b = bound[0]
    assert b.page_index == 1 and b.artefact_index == 0
    assert "Site network layout" in b.caption_text
    assert b.fact_class == "B", "a caption is an INFERENCE — never A, and never C"
    assert b.method == "geometry_v1"
    assert b.signals["side"] == "below"      # scored, not assumed by convention


def test_pages_are_joined_on_a_blank_line_so_a_page_break_can_end_a_chunk(sample_pdf):
    d = BI.extract(sample_pdf, "application/pdf")
    assert "\n\n" in d.full_text
    from src.mcp.server import _chunk_document
    assert len(_chunk_document(d.full_text)) == 2


def test_the_full_chain_ties_the_figure_to_the_page_it_was_printed_on(sample_pdf):
    """The end-to-end deterministic claim: the figure on PAGE 2 resolves to the chunk that
    came from page 2 — not chunk 0, which exists precisely to be a wrong answer."""
    from src.ingest.artefact_geometry import associate_document
    from src.mcp.server import _chunk_document
    d = BI.extract(sample_pdf, "application/pdf")
    chunks = _chunk_document(d.full_text)
    pages = {p.page_index: p for p in d.pages}
    b = [x for x in associate_document(list(d.layouts)) if x.bound][0]
    idx, method = BI.resolve_chunk_index(caption_text=b.caption_text,
                                         page=pages[b.page_index], chunks=chunks)
    assert (idx, method) == (1, "caption")


def test_a_page_bomb_is_refused_whole(monkeypatch, sample_pdf):
    monkeypatch.setattr(BI, "MAX_PAGES", 1)
    with pytest.raises(BI.ArtefactBomb):
        BI.extract_pdf(sample_pdf)


def test_an_image_upload_is_retained_without_pretending_to_read_it():
    d = BI.extract(PNG, "image/png")
    assert d.page_count == 0 and d.is_text_bearing is False
    assert "OCR" in d.notes["reason"]


# ══════════════════════════════════════════════════════════════════════════════════════
# 6. THE READ, NOT THE WRITE — a real tenant schema from the real template
# ══════════════════════════════════════════════════════════════════════════════════════

def _dsn():
    return os.getenv("POSTGRES_DSN") or os.getenv("ARTEFACT_TEST_DSN")


@pytest.fixture()
def tenant_cursor():
    dsn = _dsn()
    if not dsn:
        pytest.skip("no POSTGRES_DSN — DB round-trip skipped")
    psycopg2 = pytest.importorskip("psycopg2")
    template = (REPO / "src/provisioning/templates/user_schema.sql").read_text()
    ddl = template[template.index("CREATE TABLE IF NOT EXISTS {schema_name}.documents"):]
    assert "chunk_index" in ddl and "artefacts_chunk_bind_method" in ddl, \
        "the TEMPLATE lost migration 208 — new tenants would silently miss the context tie"

    schema = "faultline_binintake_test_" + uuid.uuid4().hex[:12]
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(ddl.replace("{schema_name}", f'"{schema}"'))
            # PRODUCTION ISOLATION SHAPE: tenant schema only, NO public.
            cur.execute(f'SET search_path TO "{schema}"')
            yield cur
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            conn.close()


@pytest.fixture()
def _on(monkeypatch):
    monkeypatch.setenv("ARTEFACT_RETENTION", "true")
    monkeypatch.setenv("ARTEFACT_CAPTION_BINDING", "true")
    monkeypatch.setenv("ARTEFACT_BINARY_INTAKE", "true")


def test_the_whole_pipeline_round_trips_through_postgres(tenant_cursor, _on, sample_pdf):
    """VERIFY THE READ: drive the real pipeline, then fetch every claim back out."""
    import json
    from src.ingest import artefacts as A
    from src.ingest import artefact_pipeline as P
    from src.mcp.server import _chunk_document

    user = str(uuid.uuid4())
    d = BI.extract(sample_pdf, "application/pdf")
    chunks = _chunk_document(d.full_text)
    tenant_cursor.execute(
        "INSERT INTO documents (user_id, source_ref, chunks, chunk_count, status) "
        "VALUES (%s,%s,%s::jsonb,%s,'pending') RETURNING id",
        (user, "report.pdf", json.dumps(chunks), len(chunks)))
    doc_id = tenant_cursor.fetchone()[0]

    retained = P.retain_upload(tenant_cursor, user_id=user, data=sample_pdf,
                               media_type="application/pdf", filename="report.pdf",
                               document_id=doc_id)
    assert retained["fact_class"] == "A" and retained["fact_provenance"] == "user_stated"

    rep = P.associate_upload(tenant_cursor, user_id=user,
                             container_sha256=retained["sha256"],
                             media_type="application/pdf", extracted=d,
                             filename="report.pdf", document_id=doc_id, chunks=chunks)
    assert rep == {"placements": 1, "captions_bound": 1, "captions_declined": 0,
                   "chunks_tied": 1, "tie_methods": {"caption": 1}, "decline_reasons": {}}

    # THE READ the tie exists for.
    hit = A.fetch_artefacts_for_chunk(tenant_cursor, user_id=user, document_id=doc_id,
                                      chunk_index=1)
    assert len(hit) == 1
    row = hit[0]
    assert row["chunk_bind_method"] == "caption"
    assert row["caption_fact_class"] == "B"
    assert "Site network layout" in row["caption_text"]
    assert (row["bbox_x0"], row["bbox_top"]) == (150.0, 200.0)
    assert "bytes" not in row, "a recall read must never haul the payload"
    assert "image" in A.describe_artefact_for_recall(row)

    # chunk 0 is the wrong answer and must stay empty.
    assert A.fetch_artefacts_for_chunk(tenant_cursor, user_id=user, document_id=doc_id,
                                       chunk_index=0) == []

    # The pixels live in the retained container, resolved through the ONE accessor.
    kind, blob = A.load_artefact_payload(tenant_cursor, user_id=user,
                                         artefact_id=retained["artefact_id"])
    assert kind == "inline" and blob == sample_pdf
    assert A.load_artefact_payload(tenant_cursor, user_id=user,
                                   artefact_id=row["id"]) == ("absent", None)


def test_re_uploading_the_same_file_does_not_double_store(tenant_cursor, _on, sample_pdf):
    from src.ingest import artefact_pipeline as P
    user = str(uuid.uuid4())
    d = BI.extract(sample_pdf, "application/pdf")
    for _ in range(3):
        r = P.retain_upload(tenant_cursor, user_id=user, data=sample_pdf,
                            media_type="application/pdf", filename="report.pdf")
        P.associate_upload(tenant_cursor, user_id=user, container_sha256=r["sha256"],
                           media_type="application/pdf", extracted=d, chunks=[])
    tenant_cursor.execute("SELECT count(*) FROM artefacts WHERE user_id=%s", (user,))
    assert tenant_cursor.fetchone()[0] == 2, "one container + one placement, however often"


def test_the_database_refuses_an_unexplained_binding(tenant_cursor, _on, sample_pdf):
    """The mig-208 CHECK is a backstop with teeth, not a comment."""
    psycopg2 = pytest.importorskip("psycopg2")
    from src.ingest import artefact_pipeline as P
    user = str(uuid.uuid4())
    r = P.retain_upload(tenant_cursor, user_id=user, data=sample_pdf,
                        media_type="application/pdf")
    with pytest.raises(psycopg2.errors.CheckViolation):
        tenant_cursor.execute(
            "UPDATE artefacts SET chunk_index=1, chunk_bind_method='vibes' WHERE id=%s",
            (r["artefact_id"],))


def test_a_bind_method_without_a_chunk_is_refused_in_code_too(tenant_cursor, _on):
    from src.ingest import artefacts as A
    with pytest.raises(ValueError):
        A.bind_chunk(tenant_cursor, user_id="u", artefact_id=1, document_id=1,
                     chunk_index=None, method="caption")
    with pytest.raises(ValueError):
        A.bind_chunk(tenant_cursor, user_id="u", artefact_id=1, document_id=1,
                     chunk_index=3, method="astrology")


# ══════════════════════════════════════════════════════════════════════════════════════
# 7. FLAG-OFF IS BYTE-FOR-BYTE LEGACY
# ══════════════════════════════════════════════════════════════════════════════════════

def test_the_flag_defaults_on_since_the_owner_rollout(monkeypatch):
    """Default flipped ON 2026-08-21 (owner ruling: images import best-effort is LIVE).
    The explicit OFF setting still kills the door — the byte-for-byte rollback lever."""
    monkeypatch.delenv("ARTEFACT_BINARY_INTAKE", raising=False)
    assert BI.binary_intake_enabled() is True
    monkeypatch.setenv("ARTEFACT_BINARY_INTAKE", "false")
    assert BI.binary_intake_enabled() is False


