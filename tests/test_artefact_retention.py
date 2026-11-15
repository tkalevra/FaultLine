"""ARTEFACT — stage A retention + stage A.5 geometric caption binding.

Spec: DEV/DESIGN-image-and-scanned-document-ingest.md.

What this pins, in the order it matters:

 1. FLAG-OFF IS BYTE-IDENTICAL. `ARTEFACT_RETENTION` off → no cursor call at all. Importing
    this module cannot change any existing behaviour.
 2. THE TIER SPLIT. Upload event → user_stated / Class A. Caption → Class B, and Class C is
    REFUSED loudly (C carries a 30-day expiry; that is the "force this content kind to C"
    bug the feelings ruling was reverted for).
 3. THE READ, NOT THE WRITE. A retained artefact and its caption are fetched back through
    the real accessors against a REAL PostgreSQL tenant schema built from the REAL template.
    A row nothing reads is worthless.
 4. THE DECLINE BEHAVIOUR. Every geometric error mode the spec names — multi-column,
    floating figure, side-by-side panels, decorative logos, full-bleed plates, above/below
    ambiguity — declines with a reason rather than binding a neighbour's caption.
 5. NO WORD LISTS. The binder never reads a caption's vocabulary; the same layout binds
    identically whatever language the caption is in.

Run with:
  python3 tools/fltest.py --bug ARTEFACT --test tests/test_artefact_retention.py \
      --note "stage A retention + A.5 geometry"

The DB half is SKIPPED without POSTGRES_DSN (or a reachable local dev database); the
geometry half needs nothing at all.
"""
from __future__ import annotations

import os
import uuid

import pytest

from src.ingest import artefacts as A
from src.ingest.artefact_geometry import (
    Box, CaptionBinding, PageLayout, PlacedImage, TextLine,
    W_DISTINCT, W_ISOLATION, W_OVERLAP, W_PROXIMITY,
    associate_document, associate_page, repeated_boxes,
)


# ══════════════════════════════════════════════════════════════════════════════════════
# fixtures / builders — a synthetic page in PDF-ish top-origin points (612x792 = Letter)
# ══════════════════════════════════════════════════════════════════════════════════════

PAGE_W, PAGE_H = 612.0, 792.0


def _line(text, x0, top, x1=None, height=12.0, size=10.0, font="Body"):
    x1 = x1 if x1 is not None else x0 + 6.0 * len(text)
    return TextLine(box=Box(x0, top, x1, top + height), text=text, size=size, fontname=font)


def _body_block(top, x0=72.0, x1=540.0, lines=4, size=10.0, prefix="body"):
    """A paragraph: several tightly-spaced lines in the page's dominant font."""
    return [_line(f"{prefix} line {i} with enough words to read as prose", x0, top + i * 14.0,
                  x1=x1, size=size) for i in range(lines)]


def _page(images, lines, page_index=0, width=PAGE_W, height=PAGE_H):
    return PageLayout(page_index=page_index, width=width, height=height,
                      images=tuple(images), lines=tuple(lines))


def _simple_figure_page(caption_text="Site network layout, Kitchener office"):
    """One clear figure with one short, typographically distinct caption below it."""
    img = PlacedImage(index=0, box=Box(150.0, 200.0, 450.0, 420.0))
    lines = [
        *_body_block(80.0),
        _line(caption_text, 150.0, 432.0, x1=450.0, size=8.0, font="Caption"),
        *_body_block(520.0, prefix="after"),
    ]
    return _page([img], lines)


# ══════════════════════════════════════════════════════════════════════════════════════
# 1. FLAG-OFF IS BYTE-IDENTICAL
# ══════════════════════════════════════════════════════════════════════════════════════

class _ExplodingCursor:
    """Any database contact at all is a failure of the flag-off contract."""
    def execute(self, *a, **k):      # pragma: no cover - must never run
        raise AssertionError("flag-off must not touch the database")

    def fetchone(self, *a, **k):     # pragma: no cover
        raise AssertionError("flag-off must not touch the database")


def test_flag_off_retain_is_a_noop(monkeypatch):
    monkeypatch.setenv("ARTEFACT_RETENTION", "false")
    assert A.retention_enabled() is False
    assert A.retain_artefact(_ExplodingCursor(), user_id="u", media_type="application/pdf",
                             data=b"%PDF-1.7") is None


def test_flag_off_bind_caption_is_a_noop(monkeypatch):
    monkeypatch.setenv("ARTEFACT_CAPTION_BINDING", "false")
    binding = CaptionBinding(page_index=0, artefact_index=0, bound=True,
                             caption_text="x", confidence=0.9, method="geometry_v1",
                             fact_class="B")
    assert A.bind_caption(_ExplodingCursor(), user_id="u", artefact_id=1, binding=binding) is None


def test_flags_default_on_since_the_owner_rollout():
    """Both flags flipped default-ON 2026-08-21 (owner ruling: "Images must be imported
    into the DB on best effort" — the staged rollout ended and the lane went live).
    Explicit OFF still disables each — the byte-for-byte rollback lever."""
    for var in ("ARTEFACT_RETENTION", "ARTEFACT_CAPTION_BINDING"):
        os.environ.pop(var, None)
    assert A.retention_enabled() is True
    assert A.caption_binding_enabled() is True
    os.environ["ARTEFACT_RETENTION"] = "false"
    os.environ["ARTEFACT_CAPTION_BINDING"] = "false"
    try:
        assert A.retention_enabled() is False
        assert A.caption_binding_enabled() is False
    finally:
        for var in ("ARTEFACT_RETENTION", "ARTEFACT_CAPTION_BINDING"):
            os.environ.pop(var, None)


# ══════════════════════════════════════════════════════════════════════════════════════
# 2. THE TIER SPLIT — A for the upload event, B for the caption, C REFUSED
# ══════════════════════════════════════════════════════════════════════════════════════

def test_upload_event_is_user_stated_class_a():
    assert A.IDENTITY_PROVENANCE == "user_stated"
    assert A.IDENTITY_CLASS == "A"
    edges = A.upload_event_edges(filename="topology.pdf", media_type="application/pdf",
                                 captured_date="2026-07-31")
    assert edges, "the upload event must produce memory before a pixel is read"
    assert all(e["fact_provenance"] == "user_stated" for e in edges)
    assert all(e["event_date"] == "2026-07-31" for e in edges)
    # instance_of is a SEEDED Class-A rel, so this edge is Class A under both states of
    # USER_STATED_PROMOTES_TO_A — it does not depend on that flag.
    inst = [e for e in edges if e["rel_type"] == "instance_of"]
    assert inst and inst[0]["subject"] == "topology.pdf" and inst[0]["object"] == "pdf"


def test_upload_event_type_node_comes_from_the_media_type_not_a_word_list():
    """The type node is derived from IANA metadata that arrived WITH the file."""
    assert A._type_node_from_media_type("application/pdf") == "pdf"
    assert A._type_node_from_media_type("image/png") == "png"
    assert A._type_node_from_media_type("image/svg+xml") == "svg"      # suffix, not +xml
    assert A._type_node_from_media_type("image/x-ms-bmp") == "ms-bmp"  # x- carries no meaning
    assert A._type_node_from_media_type("application/pdf; charset=binary") == "pdf"
    assert A._type_node_from_media_type("garbage") is None


def test_hard_line_the_artefact_never_becomes_a_place():
    """The filename is a NAME filed AT a type node — never a place itself.

    A place is what other things hang off. `instance_of` must point FROM the named
    instance TO the type; a hierarchy edge pointing the other way would make "topology.pdf"
    an L4 node, which is the category error the founding distinction exists to prevent.
    """
    edges = A.upload_event_edges(filename="topology.pdf", media_type="application/pdf")
    hierarchy = [e for e in edges if e["rel_type"] in ("instance_of", "subclass_of")]
    assert hierarchy, "the instance must be filed at a type"
    for e in hierarchy:
        assert e["subject"] == "topology.pdf" and e["object"] != "topology.pdf"
        assert e["rel_type"] != "subclass_of", "a named instance is never a subclass"


def test_caption_class_is_b_and_c_is_refused_loudly(monkeypatch):
    monkeypatch.setenv("ARTEFACT_CAPTION_BINDING", "true")
    assert A.CAPTION_CLASS == "B"
    for bad in ("C", "A", None):
        binding = CaptionBinding(page_index=0, artefact_index=0, bound=True,
                                 caption_text="figure", confidence=0.9,
                                 method="geometry_v1", fact_class=bad)
        with pytest.raises(ValueError):
            A.bind_caption(_ExplodingCursor(), user_id="u", artefact_id=1, binding=binding)


def test_geometry_only_ever_produces_class_b():
    """No layout can make the binder emit A or C — the class is not layout-dependent."""
    for binding in associate_page(_simple_figure_page()):
        assert binding.fact_class in (None, "B")
        if binding.bound:
            assert binding.fact_class == "B"


# ══════════════════════════════════════════════════════════════════════════════════════
# 3. A.5 — it binds the obvious case
# ══════════════════════════════════════════════════════════════════════════════════════

def test_weights_sum_to_one():
    assert abs((W_PROXIMITY + W_OVERLAP + W_ISOLATION + W_DISTINCT) - 1.0) < 1e-9


def test_binds_a_clear_caption_below_a_figure():
    [b] = associate_page(_simple_figure_page())
    assert b.bound, b.declined_reason
    assert b.caption_text.startswith("Site network layout")
    assert b.method == "geometry_v1"
    assert 0.0 < b.confidence <= 1.0
    assert b.signals["side"] == "below"


def test_binds_a_caption_ABOVE_a_figure_too():
    """No above/below convention is assumed — the "captions go below" rule is a stated
    convention, never a measured one, so geometry decides per page."""
    img = PlacedImage(index=0, box=Box(150.0, 300.0, 450.0, 520.0))
    lines = [
        *_body_block(80.0),
        _line("Rack elevation, second floor", 150.0, 280.0, x1=450.0, size=8.0, font="Caption"),
        *_body_block(600.0, prefix="after"),
    ]
    [b] = associate_page(_page([img], lines))
    assert b.bound, b.declined_reason
    assert b.signals["side"] == "above"


def test_no_word_list_the_same_layout_binds_in_any_language():
    """The binder is subject- and language-agnostic: identical geometry, identical verdict.

    An English "Figure 3: ..." and a caption in another script must behave the same, which
    a keyword-based caption detector (PDFFigures 2.0 phase 1) cannot do.
    """
    a = associate_page(_simple_figure_page("Figure 3: network topology"))[0]
    b = associate_page(_simple_figure_page("Abbildung 3: Netzwerktopologie"))[0]
    c = associate_page(_simple_figure_page("图 3：网络拓扑"))[0]
    assert a.bound and b.bound and c.bound
    assert a.confidence == b.confidence == c.confidence


def test_determinism_same_page_same_verdict():
    page = _simple_figure_page()
    first = associate_page(page)
    for _ in range(5):
        assert associate_page(page) == first


# ══════════════════════════════════════════════════════════════════════════════════════
# 4. A.5 — THE ERROR MODES. Declining beats binding a neighbour's caption.
# ══════════════════════════════════════════════════════════════════════════════════════

def test_decorative_logo_is_never_associated():
    """A 40x40 logo is not a figure. Our p99 document has 810 images, nearly all of this."""
    img = PlacedImage(index=0, box=Box(72.0, 40.0, 112.0, 80.0))
    lines = [_line("Quarterly facilities report", 72.0, 90.0, x1=400.0, size=8.0, font="Caption")]
    [b] = associate_page(_page([img], lines))
    assert not b.bound and b.declined_reason == "decorative_small"


def test_horizontal_rule_is_never_associated():
    img = PlacedImage(index=0, box=Box(72.0, 300.0, 540.0, 302.0))
    lines = [_line("Section two", 72.0, 310.0, x1=400.0, size=8.0, font="Caption")]
    [b] = associate_page(_page([img], lines))
    assert not b.bound and b.declined_reason in ("decorative_small", "decorative_aspect")


def test_full_bleed_background_declines():
    """A background plate is "near" every block on the page, so proximity means nothing."""
    img = PlacedImage(index=0, box=Box(0.0, 0.0, PAGE_W, PAGE_H))
    lines = [_line("Annual report 2026", 72.0, 300.0, x1=400.0, size=18.0, font="Title")]
    [b] = associate_page(_page([img], lines))
    assert not b.bound and b.declined_reason == "background_plate"


def test_repeated_running_head_declines_across_the_document():
    """Same placed box on most pages = running head / logo, however large it is."""
    logo = Box(60.0, 30.0, 260.0, 130.0)
    pages = [
        _page([PlacedImage(index=0, box=logo)],
              [_line(f"Chapter {p} introduction", 72.0, 150.0, x1=400.0, size=8.0, font="Caption")],
              page_index=p)
        for p in range(6)
    ]
    assert logo.rounded() in repeated_boxes(pages)
    verdicts = associate_document(pages)
    assert verdicts and all(v.declined_reason == "decorative_repeated" for v in verdicts)


def test_multi_column_never_binds_across_the_gutter():
    """The nearest block by distance can sit in the ADJACENT column, semantically unrelated."""
    left_img = PlacedImage(index=0, box=Box(60.0, 200.0, 290.0, 400.0))
    lines = [
        # left column body + the figure's own column has NO caption text near it
        *[_line("left column prose continues here", 60.0, 60.0 + i * 14.0, x1=290.0)
          for i in range(8)],
        # right column: a short distinct block level with the figure — geometrically the
        # nearest short block, semantically another column's heading.
        _line("Table 4 revenue by region", 330.0, 405.0, x1=550.0, size=8.0, font="Caption"),
        *[_line("right column prose continues here", 330.0, 60.0 + i * 14.0, x1=550.0)
          for i in range(8)],
    ]
    [b] = associate_page(_page([left_img], lines))
    assert not b.bound, f"bound across the gutter to {b.caption_text!r}"
    assert b.declined_reason in ("no_candidate", "low_confidence")


def test_floating_figure_with_a_distant_paragraph_declines():
    """The introducing paragraph is pages away; nothing sits inside the gap budget."""
    img = PlacedImage(index=0, box=Box(150.0, 120.0, 450.0, 300.0))
    lines = _body_block(650.0)          # far below the gap budget
    [b] = associate_page(_page([img], lines))
    assert not b.bound and b.declined_reason == "no_candidate"


def test_side_by_side_panels_decline_rather_than_apportion():
    """Two panels, one caption between them: proximity cannot apportion, so BOTH decline."""
    left = PlacedImage(index=0, box=Box(60.0, 200.0, 290.0, 400.0))
    right = PlacedImage(index=1, box=Box(300.0, 200.0, 530.0, 400.0))
    lines = [
        _line("Before and after the rack migration", 60.0, 412.0, x1=530.0,
              size=8.0, font="Caption"),
    ]
    verdicts = associate_page(_page([left, right], lines))
    assert len(verdicts) == 2
    assert not any(v.bound for v in verdicts), "one caption cannot own two figures"
    assert {v.declined_reason for v in verdicts} <= {"ambiguous_owner", "ambiguous_side",
                                                     "no_candidate", "low_confidence"}


def test_equidistant_above_and_below_declines_as_ambiguous():
    """Two equally good candidates, one on each side: the page has not told us which."""
    img = PlacedImage(index=0, box=Box(150.0, 300.0, 450.0, 400.0))
    lines = [
        _line("Rack elevation second floor", 150.0, 276.0, x1=450.0, size=8.0, font="Caption"),
        _line("Rack elevation second floor", 150.0, 412.0, x1=450.0, size=8.0, font="Caption"),
    ]
    [b] = associate_page(_page([img], lines))
    assert not b.bound and b.declined_reason == "ambiguous_side"


def test_a_body_paragraph_is_never_bound_as_a_caption():
    """A 200-word paragraph adjacent to a figure is not its caption."""
    img = PlacedImage(index=0, box=Box(150.0, 200.0, 450.0, 400.0))
    long_line = " ".join(["word"] * 120)
    lines = [_line(long_line, 150.0, 412.0, x1=450.0)]
    [b] = associate_page(_page([img], lines))
    assert not b.bound


def test_page_with_no_text_declines_no_text():
    """A scan: no text layer at all. That is stage B's population, and stage B is deferred."""
    img = PlacedImage(index=0, box=Box(40.0, 40.0, 500.0, 700.0))
    [b] = associate_page(_page([img], []))
    assert not b.bound and b.declined_reason in ("no_text", "background_plate")


def test_every_decline_carries_a_known_reason():
    from src.ingest.artefact_geometry import DECLINE_REASONS
    pages = [_simple_figure_page(),
             _page([PlacedImage(index=0, box=Box(72.0, 40.0, 112.0, 80.0))], []),
             _page([PlacedImage(index=0, box=Box(0.0, 0.0, PAGE_W, PAGE_H))], [])]
    for v in associate_document(pages):
        assert v.bound or v.declined_reason in DECLINE_REASONS


def test_geometry_missing_dependency_degrades_and_never_raises_into_retention():
    """pdfplumber is an owner decision and is not installed. Absent, A.5 is unavailable and
    stage A retention is untouched — the failure must be a typed, catchable one."""
    from src.ingest.artefact_geometry import GeometryUnavailable, geometry_available
    if geometry_available():
        pytest.skip("pdfplumber present — the degradation path is not exercisable here")
    with pytest.raises(GeometryUnavailable):
        from src.ingest.artefact_geometry import load_pdf_layout
        load_pdf_layout("/nonexistent.pdf")


# ══════════════════════════════════════════════════════════════════════════════════════
# 5. THE READ, NOT THE WRITE — a real tenant schema built from the REAL template
# ══════════════════════════════════════════════════════════════════════════════════════

def _dsn() -> str | None:
    return os.getenv("POSTGRES_DSN") or os.getenv("ARTEFACT_TEST_DSN")


@pytest.fixture()
def tenant_cursor():
    """A throwaway tenant schema carrying ONLY the artefacts DDL from the real template,
    with the tenant search_path bound WITHOUT public (the production isolation shape).

    Dropped unconditionally; the connection is closed in the same finally, so no
    idle-in-transaction backend can be stranded (that has wedged a bench before)."""
    dsn = _dsn()
    if not dsn:
        pytest.skip("no POSTGRES_DSN — DB round-trip skipped")
    psycopg2 = pytest.importorskip("psycopg2")
    from pathlib import Path
    import re as _re

    repo = Path(__file__).resolve().parent.parent
    template = (repo / "src/provisioning/templates/user_schema.sql").read_text()
    start = template.index("CREATE TABLE IF NOT EXISTS {schema_name}.artefacts")
    ddl = template[start:]
    assert "artefacts_caption_class_b" in ddl, "template lost the Class-B caption guard"

    schema = "faultline_artefact_test_" + uuid.uuid4().hex[:12]
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(ddl.replace("{schema_name}", f'"{schema}"'))
            # PRODUCTION ISOLATION SHAPE: tenant schema only, NO public on the search_path.
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


def test_retained_artefact_is_RETRIEVABLE(tenant_cursor, _on):
    """VERIFY THE READ: store bytes, then fetch metadata AND payload back through the real
    accessors. A row nothing reads is worthless."""
    user = str(uuid.uuid4())
    payload = b"%PDF-1.7\n" + bytes(range(256)) * 8
    res = A.retain_artefact(tenant_cursor, user_id=user, media_type="application/pdf",
                            data=payload, filename="topology.pdf", document_id=41,
                            page_index=3, artefact_index=0, source_ref="onboarding upload",
                            placement=(150.0, 200.0, 450.0, 420.0), page_size=(612.0, 792.0))
    assert res["duplicate"] is False
    assert res["fact_provenance"] == "user_stated" and res["fact_class"] == "A"
    assert res["sha256"] == A.sha256_hex(payload)

    rows = A.fetch_artefacts(tenant_cursor, user_id=user, document_id=41)
    assert len(rows) == 1
    row = rows[0]
    assert row["filename"] == "topology.pdf" and row["byte_size"] == len(payload)
    assert row["fact_class"] == "A" and row["fact_provenance"] == "user_stated"
    assert row["bbox_x0"] == 150.0 and row["bbox_bottom"] == 420.0
    assert "bytes" not in row, "a metadata read must never haul the payload"

    kind, blob = A.load_artefact_payload(tenant_cursor, user_id=user,
                                         artefact_id=res["artefact_id"])
    assert kind == "inline" and blob == payload, "the bytes must come back byte-identical"


def test_other_tenant_cannot_read_the_artefact(tenant_cursor, _on):
    """user_id scoping inside the schema, on top of the schema boundary itself."""
    user, other = str(uuid.uuid4()), str(uuid.uuid4())
    res = A.retain_artefact(tenant_cursor, user_id=user, media_type="image/png",
                            data=b"\x89PNG\r\n\x1a\n", filename="rack.png")
    assert A.fetch_artefacts(tenant_cursor, user_id=other) == []
    assert A.load_artefact_payload(tenant_cursor, user_id=other,
                                   artefact_id=res["artefact_id"]) is None


def test_reupload_dedups_and_reports_duplicate(tenant_cursor, _on):
    """Re-uploading the same 200-page PDF must not double-store — and the answer "you
    already gave me this" comes for free."""
    user = str(uuid.uuid4())
    payload = b"%PDF-1.7 same bytes"
    first = A.retain_artefact(tenant_cursor, user_id=user, media_type="application/pdf",
                              data=payload, filename="contracts.pdf")
    second = A.retain_artefact(tenant_cursor, user_id=user, media_type="application/pdf",
                               data=payload, filename="contracts.pdf")
    assert first["duplicate"] is False and second["duplicate"] is True
    assert first["artefact_id"] == second["artefact_id"]
    assert len(A.fetch_artefacts(tenant_cursor, user_id=user)) == 1


def test_dedup_holds_for_whole_file_rows_with_null_page_index(tenant_cursor, _on):
    """The NULL page_index case is the COMMON one. A plain UNIQUE constraint treats NULLs as
    distinct and would have double-stored silently — the COALESCE index is what makes the
    dedup actually fire."""
    user = str(uuid.uuid4())
    for _ in range(3):
        A.retain_artefact(tenant_cursor, user_id=user, media_type="application/pdf",
                          data=b"whole file", filename="scan.pdf")
    assert len(A.fetch_artefacts(tenant_cursor, user_id=user)) == 1


def test_caption_binding_is_retrievable_and_lands_class_b(tenant_cursor, _on):
    """The whole A + A.5 loop, read back: retain → associate by geometry → fetch."""
    user = str(uuid.uuid4())
    res = A.retain_artefact(tenant_cursor, user_id=user, media_type="image/png",
                            data=b"\x89PNG-figure", filename="fig.png", document_id=7,
                            page_index=0, artefact_index=0,
                            placement=(150.0, 200.0, 450.0, 420.0), page_size=(612.0, 792.0))
    [binding] = associate_page(_simple_figure_page())
    assert binding.bound
    A.bind_caption(tenant_cursor, user_id=user, artefact_id=res["artefact_id"],
                   binding=binding)

    [row] = A.fetch_artefacts(tenant_cursor, user_id=user, captioned_only=True)
    assert row["caption_text"].startswith("Site network layout")
    assert row["caption_fact_class"] == "B"                 # never A, never C
    assert row["caption_provenance"] == "engine_inferred"
    assert row["fact_class"] == "A"                         # identity tier is UNCHANGED by A.5
    assert row["caption_confidence"] > 0
    assert row["caption_bound_at"] is not None


def test_a_decline_is_recorded_not_dropped(tenant_cursor, _on):
    """Recording WHY we refused is what stops the next pass re-deciding blind, and what
    lets the product say "I kept it but could not tell which text describes it"."""
    user = str(uuid.uuid4())
    res = A.retain_artefact(tenant_cursor, user_id=user, media_type="image/png",
                            data=b"\x89PNG-logo", filename="logo.png")
    logo_page = _page([PlacedImage(index=0, box=Box(72.0, 40.0, 112.0, 80.0))],
                      [_line("Quarterly report", 72.0, 90.0, x1=400.0, size=8.0, font="Caption")])
    [binding] = associate_page(logo_page)
    assert not binding.bound
    A.bind_caption(tenant_cursor, user_id=user, artefact_id=res["artefact_id"],
                   binding=binding)

    [row] = A.fetch_artefacts(tenant_cursor, user_id=user)
    assert row["caption_text"] is None
    assert row["caption_fact_class"] is None
    assert row["caption_declined_reason"] == "decorative_small"
    # A declined artefact is no longer in the work queue: it was ATTEMPTED.
    assert A.pending_association(tenant_cursor, user_id=user) == []


def test_database_itself_refuses_a_class_c_caption(tenant_cursor, _on):
    """The CHECK is the backstop for anything that bypasses bind_caption(). Class C would
    put a 30-day fuse on a 200-page corpus."""
    psycopg2 = pytest.importorskip("psycopg2")
    user = str(uuid.uuid4())
    res = A.retain_artefact(tenant_cursor, user_id=user, media_type="image/png",
                            data=b"\x89PNG-x", filename="x.png")
    with pytest.raises(psycopg2.errors.CheckViolation):
        tenant_cursor.execute(
            "UPDATE artefacts SET caption_text=%s, caption_fact_class='C' WHERE id=%s",
            ("smuggled caption", res["artefact_id"]),
        )


def test_external_ref_round_trips_through_the_same_accessor(tenant_cursor, _on):
    """The object-storage flip point: only load_artefact_payload learns about it."""
    user = str(uuid.uuid4())
    res = A.retain_artefact(tenant_cursor, user_id=user, media_type="application/pdf",
                            external_ref="s3://bucket/key.pdf", byte_size=4_100_000,
                            sha256="f" * 64, filename="big.pdf")
    assert A.load_artefact_payload(tenant_cursor, user_id=user,
                                   artefact_id=res["artefact_id"]) == ("external",
                                                                       "s3://bucket/key.pdf")


def test_bytes_and_external_ref_are_mutually_exclusive(tenant_cursor, _on):
    with pytest.raises(ValueError):
        A.retain_artefact(tenant_cursor, user_id="u", media_type="application/pdf",
                          data=b"x", external_ref="s3://k")
    with pytest.raises(ValueError):
        A.retain_artefact(tenant_cursor, user_id="u", media_type="application/pdf")


def test_oversize_artefact_is_refused_whole_never_truncated(tenant_cursor, _on, monkeypatch):
    """A truncated artefact is corrupt bytes that look like good bytes."""
    monkeypatch.setenv("ARTEFACT_MAX_BYTES", "64")
    user = str(uuid.uuid4())
    with pytest.raises(A.ArtefactTooLarge):
        A.retain_artefact(tenant_cursor, user_id=user, media_type="application/pdf",
                          data=b"x" * 65, filename="huge.pdf")
    assert A.fetch_artefacts(tenant_cursor, user_id=user) == []


def test_summary_counters_report_the_truth(tenant_cursor, _on):
    """§6: a user must LEARN what a document yielded. Retained-but-unread is a DEGRADED
    state over a retained artefact, not data loss — and it must be countable."""
    user = str(uuid.uuid4())
    kept = A.retain_artefact(tenant_cursor, user_id=user, media_type="image/png",
                             data=b"\x89PNG-1", filename="a.png", document_id=9,
                             page_index=0, artefact_index=0)
    dropped = A.retain_artefact(tenant_cursor, user_id=user, media_type="image/png",
                                data=b"\x89PNG-2", filename="b.png", document_id=9,
                                page_index=1, artefact_index=0)
    A.retain_artefact(tenant_cursor, user_id=user, media_type="image/png",
                      data=b"\x89PNG-3", filename="c.png", document_id=9,
                      page_index=2, artefact_index=0)
    A.bind_caption(tenant_cursor, user_id=user, artefact_id=kept["artefact_id"],
                   binding=associate_page(_simple_figure_page())[0])
    A.bind_caption(
        tenant_cursor, user_id=user, artefact_id=dropped["artefact_id"],
        binding=CaptionBinding(page_index=1, artefact_index=0, bound=False,
                               declined_reason="ambiguous_owner"))
    summary = A.summarize_artefacts(A.fetch_artefacts(tenant_cursor, user_id=user,
                                                      document_id=9))
    assert summary["artefacts_retained"] == 3
    assert summary["captions_bound"] == 1
    assert summary["captions_declined"] == 1
    assert summary["unattempted"] == 1
    assert summary["decline_reasons"] == {"ambiguous_owner": 1}
