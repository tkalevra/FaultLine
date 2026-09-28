"""Stage A.5 — bind a retained artefact to the text that describes it, by PAGE GEOMETRY.

Spec: the internal design record §3.

WHAT THIS IS
------------
An embedded figure carries its description in the text around it — the caption, and the
paragraph that introduces it. **None of that requires reading a pixel.** PDF exposes the
PLACED bounding box of an image XObject and the bounding boxes of text lines, so caption
association is a LAYOUT problem, not a perception problem. That is why A.5 is ~250-600x
cheaper than OCR and why it needs no brain at all — it works on a fail-closed tenant with
no LLM bound, which is the #1 new-tenant churn state.

It is also the larger addressable population: measured on 177 real PDFs / 11 069 pages,
**53.1 % of documents contain at least one embedded image** versus **9.6 % with no text
layer at all**. Images inside documents that DO have text are ~5x more common than scans.

NO WORD LISTS — AND WHY THAT IS NOT A STYLE PREFERENCE
------------------------------------------------------
The reference implementation (PDFFigures 2.0, Clark & Divvala, JCDL 2016) detects captions
with a KEYWORD SEARCH for caption-starting phrases. That is exactly the brittleness this
codebase forbids: it dies on a non-English document, a deck, or a report that says
"Exhibit"/"Schedule"/"Appendix". So phase 1 is REJECTED as built and everything here is:

  * pure geometry (proximity, horizontal overlap, column bands, area/aspect filters), and
  * FORMAT CONSISTENCY — the lexicon-free half of PDFFigures phase 1: a caption is
    typographically DISTINCT from the body text of its own page (different size or face)
    and stands ALONE (separated from the text flow by more than the modal leading). This
    adapts per document instead of asserting a vocabulary, which is strictly better than a
    word list even in English.
  * Its phases 2 and 3 ARE borrowed, because they carry no lexicon: expand/score candidate
    regions, then resolve a GLOBAL non-overlapping assignment so two figures cannot claim
    one caption.

Do not read PDFFigures' F1 0.97 as transferable: both of its eval corpora are exclusively
CS academic two-column papers, and the paper's own control (PDFPlots, tuned for physics)
collapsed to F1 0.555 on them. There is no published number for decks or invoices.

HONESTY ABOUT WHAT THIS CAN DO
------------------------------
Proximity association is an INFERENCE, and a figure confidently bound to its NEIGHBOUR's
caption is WORSE than no caption at all — recall would say "the network topology diagram"
and return the company logo. So every decision here is biased toward DECLINING:

  ============================  =====================================================
  Layout                        What happens
  ============================  =====================================================
  Multi-column                  one-level XY-cut column bands; a candidate in a
                                different band than the image is not admissible
  Floating figure               the introducing paragraph is pages away -> no candidate
                                inside the gap budget -> DECLINE (``no_candidate``)
  Caption above vs below        NO convention is assumed (the "captions go below" rule
                                is a stated convention, never a measured one). Both
                                sides are scored; if they tie within the decision
                                margin -> DECLINE (``ambiguous_side``)
  Side-by-side panels           if two images' best blocks overlap and neither wins by
                                the margin -> BOTH DECLINE (``ambiguous_owner``)
  Full-bleed / background       area over ``MAX_IMAGE_AREA_FRAC`` -> DECLINE
  Logos, rules, bullets         area/aspect filter + cross-page repetition filter
                                (our p99 document has 810 images, nearly all of this)
  ============================  =====================================================

A DECLINE IS A RESULT, NOT A FAILURE. It is recorded with its reason so the next pass can
see what was tried and the user is never told a wrong thing confidently.

DETERMINISM: no LLM, no embedding, no cosine, no randomness, no network. Same page in,
same bindings out. Every threshold is a RATIO of page/image dimensions, never an absolute
point count, so it is resolution- and page-size-independent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from src.api.errors import PublicRefusal


# ── Tuning. All RATIOS (page/image-relative), all env-overridable, none domain-specific ──
def _ratio(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


#: An image smaller than this fraction of the page is decorative (bullet, rule, icon).
MIN_IMAGE_AREA_FRAC = _ratio("ARTEFACT_MIN_IMAGE_AREA_FRAC", 0.01)
#: An image larger than this fraction of the page is a background/full-bleed plate: it is
#: "near" every block on the page, so proximity means nothing for it.
MAX_IMAGE_AREA_FRAC = _ratio("ARTEFACT_MAX_IMAGE_AREA_FRAC", 0.85)
#: Extreme aspect ratio = a rule / divider / spacer, not a figure.
MAX_IMAGE_ASPECT = _ratio("ARTEFACT_MAX_IMAGE_ASPECT", 20.0)
#: Vertical search budget, expressed in MODAL LINE HEIGHTS of the page itself.
MAX_GAP_LINES = _ratio("ARTEFACT_MAX_GAP_LINES", 3.0)
#: A candidate must share at least this much of the narrower x-span with the image.
MIN_H_OVERLAP = _ratio("ARTEFACT_MIN_H_OVERLAP", 0.35)
#: The winner must beat the runner-up by this RELATIVE margin, else the call is ambiguous.
DECISION_MARGIN = _ratio("ARTEFACT_DECISION_MARGIN", 0.25)
#: Below this score we decline rather than bind.
MIN_CONFIDENCE = _ratio("ARTEFACT_MIN_CAPTION_CONFIDENCE", 0.55)
#: A block longer than this is a body paragraph, not a caption. A LENGTH bound, not a lexicon.
MAX_CAPTION_TOKENS = int(_ratio("ARTEFACT_MAX_CAPTION_TOKENS", 60))
#: An identical placed box recurring on at least this fraction of pages is a running
#: head / footer / logo, however large it is.
REPEAT_PAGE_FRAC = _ratio("ARTEFACT_REPEAT_PAGE_FRAC", 0.5)
#: Whitespace gutter (fraction of page width) that separates two text columns.
COLUMN_GUTTER_FRAC = _ratio("ARTEFACT_COLUMN_GUTTER_FRAC", 0.04)

#: Score weights. Sum to 1.0 by construction; asserted in the tests.
W_PROXIMITY, W_OVERLAP, W_ISOLATION, W_DISTINCT = 0.35, 0.25, 0.20, 0.20

#: Every reason A.5 can refuse to bind. Closed set — a caller may branch on these.
DECLINE_REASONS = (
    "no_geometry",            # the PDF layout could not be read at all (dependency absent)
    "decorative_small",       # below MIN_IMAGE_AREA_FRAC
    "decorative_aspect",      # rule / divider
    "decorative_repeated",    # same box on most pages: running head, logo
    "background_plate",       # above MAX_IMAGE_AREA_FRAC: near everything, so near nothing
    "no_text",                # page has no text lines (a scan — that is stage B's problem)
    "no_candidate",           # nothing admissible inside the gap budget / column band
    "ambiguous_side",         # above and below tie: no convention is assumed
    "ambiguous_owner",        # two images compete for one block and neither wins
    "low_confidence",         # best candidate scored below MIN_CONFIDENCE
)


class GeometryUnavailable(PublicRefusal, RuntimeError):
    """Raised when page geometry cannot be read (the optional PDF reader is absent).
    A ``PublicRefusal``: the sentence is authored here and travels into the tool result as
    ``degraded_reason`` (src/api/errors.py).

    The caller must treat this as "no association", NEVER as "no artefact": stage A has
    already retained the bytes and retention must never depend on stage A.5 succeeding.
    """


# ── Geometry primitives. Top-origin ONLY (pdfplumber `top`/`bottom`) ────────────────────
# Mixing a bottom-origin library's coordinates into these is a real footgun (pdfminer/PDF
# native is bottom-left, PyMuPDF is top-left), so the convention is stated once, here, and
# the loader is the only thing allowed to translate into it.

@dataclass(frozen=True)
class Box:
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    @property
    def area(self) -> float:
        return self.width * self.height

    def rounded(self, q: float = 1.0) -> tuple:
        """Quantised key for cross-page repetition detection."""
        return (round(self.x0 / q), round(self.top / q), round(self.x1 / q), round(self.bottom / q))


@dataclass(frozen=True)
class TextLine:
    box: Box
    text: str
    size: float | None = None        # modal character size on the line
    fontname: str | None = None      # modal font face on the line


@dataclass(frozen=True)
class PlacedImage:
    index: int                       # ordinal of this image ON ITS PAGE
    box: Box
    name: str | None = None          # XObject name, when the reader exposes one


@dataclass(frozen=True)
class PageLayout:
    page_index: int
    width: float
    height: float
    images: tuple[PlacedImage, ...] = ()
    lines: tuple[TextLine, ...] = ()


@dataclass(frozen=True)
class TextBlock:
    """Contiguous lines that read as one visual unit."""
    box: Box
    text: str
    size: float | None
    fontname: str | None
    line_count: int


@dataclass(frozen=True)
class CaptionBinding:
    """The A.5 verdict for ONE image. `bound=False` carries the reason it was refused."""
    page_index: int
    artefact_index: int
    bound: bool
    caption_text: str | None = None
    confidence: float | None = None
    method: str | None = None
    declined_reason: str | None = None
    #: Class B when bound — NEVER A (it is an inference), NEVER C (C carries a 30-day
    #: expiry and would put a fuse on a 200-page corpus). See the spec §4.
    fact_class: str | None = None
    #: The signal breakdown, so a wrong bind can be explained rather than guessed at.
    signals: dict = field(default_factory=dict)


def _decline(page_index: int, artefact_index: int, reason: str, **signals) -> CaptionBinding:
    if reason not in DECLINE_REASONS:                      # fail loud on an unknown reason
        raise ValueError(f"unknown decline reason: {reason!r}")
    return CaptionBinding(
        page_index=page_index, artefact_index=artefact_index, bound=False,
        declined_reason=reason, signals=dict(signals),
    )


# ── Page statistics: everything adaptive is derived FROM THE PAGE, never asserted ───────

def _modal(values: Sequence[float], quantum: float = 0.5) -> float | None:
    """Most common value, quantised. Deterministic tie-break: the smallest bucket."""
    if not values:
        return None
    buckets: dict[float, int] = {}
    for v in values:
        buckets[round(v / quantum) * quantum] = buckets.get(round(v / quantum) * quantum, 0) + 1
    best = max(buckets.items(), key=lambda kv: (kv[1], -kv[0]))
    return best[0]


def _column_bands(lines: Sequence[TextLine], page_width: float) -> list[tuple[float, float]]:
    """One level of recursive XY-cut: split the page into column bands on whitespace.

    A two-column page whose gutter is wider than COLUMN_GUTTER_FRAC yields two bands; an
    ordinary single-column page yields one, and every membership test below is then a
    no-op. This is the multi-column guard: the nearest block by distance can sit in the
    ADJACENT column and be semantically unrelated.
    """
    if not lines or page_width <= 0:
        return []
    spans = sorted((ln.box.x0, ln.box.x1) for ln in lines)
    gutter = COLUMN_GUTTER_FRAC * page_width
    bands: list[list[float]] = []
    for x0, x1 in spans:
        if bands and x0 <= bands[-1][1] + gutter:
            bands[-1][1] = max(bands[-1][1], x1)
        else:
            bands.append([x0, x1])
    return [(b[0], b[1]) for b in bands]


def _band_of(box: Box, bands: Sequence[tuple[float, float]]) -> int | None:
    """Index of the band containing most of `box`; None when it spans bands or is outside."""
    if not bands:
        return None
    best_idx, best_cov = None, 0.0
    for i, (b0, b1) in enumerate(bands):
        cov = max(0.0, min(box.x1, b1) - max(box.x0, b0))
        if cov > best_cov:
            best_idx, best_cov = i, cov
    if best_idx is None or box.width <= 0:
        return None
    # Spanning: covered by no single band to 60 % -> treat as spanning (admits every band).
    return best_idx if (best_cov / box.width) >= 0.6 else None


def _group_blocks(lines: Sequence[TextLine], bands: Sequence[tuple[float, float]],
                  modal_height: float) -> list[TextBlock]:
    """Group lines into visual blocks. A break is a GEOMETRIC or TYPOGRAPHIC change only.

    ⚠️ Grouping runs PER COLUMN BAND. Reading a two-column page in pure top-order
    INTERLEAVES the columns (left line 1, right line 1, left line 2, ...), so a
    document-order grouper breaks on every line and shatters both columns into
    single-line "blocks" — which then look exactly like short standalone captions. That
    is a caption-detector's worst failure mode and it was caught by the multi-column
    test, not by reasoning. Partition first, then group.
    """
    by_band: dict[int | None, list[TextLine]] = {}
    for ln in lines:
        by_band.setdefault(_band_of(ln.box, bands), []).append(ln)

    blocks: list[list[TextLine]] = []
    for band in sorted(by_band, key=lambda b: (b is None, b)):
        ordered = sorted(by_band[band], key=lambda ln: (ln.box.top, ln.box.x0))
        current: list[TextLine] = []
        for ln in ordered:
            if current:
                prev = current[-1]
                gap = ln.box.top - prev.box.bottom
                same_face = (ln.fontname == prev.fontname)
                same_size = (
                    ln.size is None or prev.size is None
                    or abs(ln.size - prev.size) <= 0.05 * max(ln.size, prev.size)
                )
                if gap <= 1.5 * modal_height and same_face and same_size:
                    current.append(ln)
                    continue
            if current:
                blocks.append(current)
            current = [ln]
        if current:
            blocks.append(current)

    out: list[TextBlock] = []
    for group in blocks:
        box = Box(
            x0=min(l.box.x0 for l in group), top=min(l.box.top for l in group),
            x1=max(l.box.x1 for l in group), bottom=max(l.box.bottom for l in group),
        )
        text = " ".join(l.text.strip() for l in group if l.text and l.text.strip()).strip()
        out.append(TextBlock(
            box=box, text=text,
            size=_modal([l.size for l in group if l.size is not None], 0.1),
            fontname=group[0].fontname, line_count=len(group),
        ))
    return out


def _h_overlap_frac(a: Box, b: Box) -> float:
    """Shared x-span as a fraction of the NARROWER span (a short caption under a wide
    figure and a wide caption under a narrow figure both score 1.0)."""
    denom = min(a.width, b.width)
    if denom <= 0:
        return 0.0
    return max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0)) / denom


# ── The association itself ──────────────────────────────────────────────────────────────

def associate_page(page: PageLayout, repeated: Iterable[tuple] = (),
                   doc_modal_height: float | None = None) -> list[CaptionBinding]:
    """Bind each admissible image on ONE page to its caption block, or decline with a reason."""
    repeated_keys = set(repeated)
    page_area = max(1e-6, page.width * page.height)

    heights = [ln.box.height for ln in page.lines if ln.box.height > 0]
    modal_height = _modal(heights, 0.5) or doc_modal_height or 12.0
    page_size = _modal([ln.size for ln in page.lines if ln.size], 0.1)
    bands = _column_bands(page.lines, page.width)
    blocks = _group_blocks(page.lines, bands, modal_height)
    max_gap = MAX_GAP_LINES * modal_height

    # ── Admissibility: which images are worth associating at all (pure geometry) ────────
    results: dict[int, CaptionBinding] = {}
    live: list[PlacedImage] = []
    for img in page.images:
        frac = img.box.area / page_area
        aspect = (img.box.width / img.box.height) if img.box.height > 0 else float("inf")
        if img.box.rounded() in repeated_keys:
            results[img.index] = _decline(page.page_index, img.index, "decorative_repeated")
        elif frac < MIN_IMAGE_AREA_FRAC:
            results[img.index] = _decline(page.page_index, img.index, "decorative_small",
                                          area_frac=round(frac, 5))
        elif aspect > MAX_IMAGE_ASPECT or aspect < (1.0 / MAX_IMAGE_ASPECT):
            results[img.index] = _decline(page.page_index, img.index, "decorative_aspect",
                                          aspect=round(aspect, 3))
        elif frac > MAX_IMAGE_AREA_FRAC:
            results[img.index] = _decline(page.page_index, img.index, "background_plate",
                                          area_frac=round(frac, 5))
        elif not blocks:
            results[img.index] = _decline(page.page_index, img.index, "no_text")
        else:
            live.append(img)

    # ── Score each live image against every block, both sides, no side preference ──────
    scored: dict[int, list[tuple[float, TextBlock, dict]]] = {}
    for img in live:
        img_band = _band_of(img.box, bands)
        cands: list[tuple[float, TextBlock, dict]] = []
        for blk in blocks:
            if not blk.text or len(blk.text.split()) > MAX_CAPTION_TOKENS:
                continue                                   # a body paragraph is not a caption
            blk_band = _band_of(blk.box, bands)
            if img_band is not None and blk_band is not None and img_band != blk_band:
                continue                                   # MULTI-COLUMN GUARD
            below = blk.box.top - img.box.bottom
            above = img.box.top - blk.box.bottom
            if below >= 0 and below <= max_gap:
                gap, side = below, "below"
            elif above >= 0 and above <= max_gap:
                gap, side = above, "above"
            else:
                continue
            overlap = _h_overlap_frac(blk.box, img.box)
            if overlap < MIN_H_OVERLAP:
                continue

            proximity = 1.0 - min(1.0, gap / max_gap) if max_gap > 0 else 0.0
            # FORMAT CONSISTENCY (lexicon-free): a caption is typographically distinct from
            # the page's body text, and stands alone rather than being a paragraph's first line.
            distinct = 0.5
            if page_size and blk.size and abs(blk.size - page_size) > 0.05 * page_size:
                distinct = 1.0
            elif blk.line_count <= 2 and len(blk.text.split()) <= MAX_CAPTION_TOKENS // 3:
                distinct = 0.75
            isolation = 1.0 if blk.line_count <= 3 else 0.4
            score = (W_PROXIMITY * proximity + W_OVERLAP * min(1.0, overlap)
                     + W_ISOLATION * isolation + W_DISTINCT * distinct)
            cands.append((score, blk, {
                "side": side, "gap": round(gap, 2), "overlap": round(overlap, 3),
                "proximity": round(proximity, 3), "isolation": isolation,
                "distinct": distinct, "column_band": blk_band,
            }))
        cands.sort(key=lambda c: (-c[0], c[1].box.top, c[1].box.x0))
        scored[img.index] = cands

    # ── Verdict per image: no-candidate / side ambiguity / confidence floor ────────────
    provisional: dict[int, tuple[float, TextBlock, dict]] = {}
    for img in live:
        cands = scored[img.index]
        if not cands:
            results[img.index] = _decline(page.page_index, img.index, "no_candidate")
            continue
        best = cands[0]
        # NO ABOVE/BELOW CONVENTION IS ASSUMED. If the best candidate on the other side is
        # within the decision margin, the page has not told us which one it is.
        other = next((c for c in cands[1:] if c[2]["side"] != best[2]["side"]), None)
        if other and best[0] > 0 and (best[0] - other[0]) / best[0] < DECISION_MARGIN:
            results[img.index] = _decline(page.page_index, img.index, "ambiguous_side",
                                          best=round(best[0], 3), runner_up=round(other[0], 3))
            continue
        if best[0] < MIN_CONFIDENCE:
            results[img.index] = _decline(page.page_index, img.index, "low_confidence",
                                          best=round(best[0], 3))
            continue
        provisional[img.index] = best

    # ── GLOBAL ASSIGNMENT (PDFFigures phase 3, lexicon-free half): one block, one owner ──
    # Side-by-side panels are the case this exists for: proximity alone cannot apportion
    # N images among M captions, and a confident wrong apportionment is the worst outcome.
    by_block: dict[int, list[int]] = {}
    for idx, (_score, blk, _sig) in provisional.items():
        by_block.setdefault(id(blk), []).append(idx)
    for _blk_id, claimants in by_block.items():
        if len(claimants) == 1:
            continue
        claimants.sort(key=lambda i: -provisional[i][0])
        winner, runner = claimants[0], claimants[1]
        w_score, r_score = provisional[winner][0], provisional[runner][0]
        decisive = w_score > 0 and (w_score - r_score) / w_score >= DECISION_MARGIN
        for idx in claimants:
            if idx == winner and decisive:
                continue
            results[idx] = _decline(page.page_index, idx, "ambiguous_owner",
                                    best=round(w_score, 3), runner_up=round(r_score, 3),
                                    claimants=len(claimants))
            provisional.pop(idx, None)

    for idx, (score, blk, sig) in provisional.items():
        results[idx] = CaptionBinding(
            page_index=page.page_index, artefact_index=idx, bound=True,
            caption_text=blk.text, confidence=round(min(1.0, score), 4),
            method="geometry_v1", fact_class="B", signals=sig,
        )

    return [results[i.index] for i in page.images]


def repeated_boxes(pages: Sequence[PageLayout]) -> set[tuple]:
    """Placed boxes recurring on >= REPEAT_PAGE_FRAC of pages: running heads, logos, rules.

    Document-level, geometric, and lexicon-free. This is the practical killer the spec
    names: a 40x40 logo repeated on 200 pages is not a figure, and our p99 document has
    810 images that are almost entirely this.
    """
    if len(pages) < 3:
        return set()
    counts: dict[tuple, int] = {}
    for pg in pages:
        for key in {img.box.rounded() for img in pg.images}:
            counts[key] = counts.get(key, 0) + 1
    threshold = max(2, int(REPEAT_PAGE_FRAC * len(pages)))
    return {k for k, n in counts.items() if n >= threshold}


def associate_document(pages: Sequence[PageLayout]) -> list[CaptionBinding]:
    """Run A.5 over a whole document, with the cross-page repetition filter applied."""
    repeated = repeated_boxes(pages)
    heights = [ln.box.height for pg in pages for ln in pg.lines if ln.box.height > 0]
    doc_modal = _modal(heights, 0.5)
    out: list[CaptionBinding] = []
    for pg in pages:
        out.extend(associate_page(pg, repeated=repeated, doc_modal_height=doc_modal))
    return out


# ── Optional reader. A MISSING DEPENDENCY DEGRADES; IT NEVER LOSES THE ARTEFACT ─────────

def geometry_available() -> bool:
    """True iff a PDF layout reader is importable. Callers branch on this, never on an
    ImportError deep inside a loop."""
    try:                                    # pragma: no cover - trivial import probe
        import pdfplumber  # noqa: F401
        return True
    except Exception:
        return False


def load_pdf_layout(source, max_pages: int | None = None,
                    on_page: "callable | None" = None) -> list[PageLayout]:
    """Read placed-image + text-line geometry from a PDF via pdfplumber (MIT, pure Python).

    ``source`` is a filesystem path OR any binary file-like object (``io.BytesIO``). The
    binary intake door passes a BytesIO deliberately: an uploaded file is parsed entirely
    in memory, so there is no temp file to race, leak, or hand to another process.

    ``on_page(page_no, pdfplumber_page)`` is called before each page is read, so a caller
    can enforce its own per-page bounds (image-count caps) and abort a resource bomb
    without a second parse. Raising from it aborts the read.

    THIS IS THE ONLY PLACE ALLOWED TO TRANSLATE INTO THIS MODULE'S COORDINATE CONVENTION.
    A second reader would be a second chance to mix bottom-origin coordinates in, which is
    the footgun this module opens by stating the convention once, here.

    ⚠️ pdfplumber is an OWNER DECISION and is NOT in ``pyproject.toml``: absent, this
    raises :class:`GeometryUnavailable` and stage A retention is unaffected. See the spec
    §8 open question 2.

    ⚠️ PyMuPDF/``fitz`` must NOT be substituted here. It is AGPL-3.0 with a paid Artifex
    commercial licence, it is an in-process import, and AGPL's network clause covers hosted
    use. ``mutool`` carries the same dual
    licence, so there is no subprocess escape hatch either. ``pdfimages -list`` (poppler)
    was tested and emits NO position data at all, so the poppler CLI cannot do A.5.
    """
    try:
        import pdfplumber
    except Exception as exc:                # noqa: BLE001 - any import failure degrades alike
        raise GeometryUnavailable(
            "pdfplumber is not installed; stage A.5 caption association is unavailable "
            "(stage A retention is unaffected)"
        ) from exc

    pages: list[PageLayout] = []
    with pdfplumber.open(source) as pdf:
        for pno, page in enumerate(pdf.pages):
            if max_pages is not None and pno >= max_pages:
                break
            if on_page is not None:
                on_page(pno, page)
            # pdfplumber's `top`/`bottom` are already top-origin — the ONE convention this
            # module uses. Never mix in `y0`/`y1` (bottom-origin) from the same objects.
            images = tuple(
                PlacedImage(
                    index=i,
                    box=Box(float(im["x0"]), float(im["top"]), float(im["x1"]), float(im["bottom"])),
                    name=str(im.get("name") or "") or None,
                )
                for i, im in enumerate(page.images or ())
            )
            lines: list[TextLine] = []
            for ln in (page.extract_text_lines() or ()):
                chars = ln.get("chars") or ()
                lines.append(TextLine(
                    box=Box(float(ln["x0"]), float(ln["top"]), float(ln["x1"]), float(ln["bottom"])),
                    text=str(ln.get("text") or ""),
                    size=_modal([float(c["size"]) for c in chars if c.get("size")], 0.1),
                    fontname=(chars[0].get("fontname") if chars else None),
                ))
            pages.append(PageLayout(
                page_index=pno, width=float(page.width), height=float(page.height),
                images=images, lines=tuple(lines),
            ))
    return pages
