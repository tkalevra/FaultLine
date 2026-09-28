"""THE DOOR — deterministic binary intake. No LLM, no vision model, no network.

Spec: the internal design record §6.1/§6.2 (the missing door),
§2 (retention), §3 (A.5 geometry), §7 (what not to reach for).

WHAT WAS MISSING, AND WHY THIS FILE EXISTS
------------------------------------------
``src/ingest/artefacts.py`` (stage A) and ``artefact_geometry.py`` (stage A.5) were built
first and were BLOCKED: both take a cursor and bytes, and **nothing in FaultLine could hand
them bytes**. Every door took ``text: str`` — ``ingest_document_tool`` (``server.py``),
REST ``POST /ingest_document`` (``http_server.py``), ``POST /v1/documents``
(``api_v1.py``) — with no ``UploadFile``, no multipart, and no upload control in any
console. FaultLine never saw a PDF; it saw a string some client had already extracted, and
when that client was OpenWebUI's default (pypdf) an image-only PDF returned
``400 "The content provided is empty"`` upstream, so the failure never even reached us.

This module is the deterministic half of the door: **bytes in → media type, page text, and
placed-image geometry out**. It performs NO database work and holds NO transport types, so
it is testable with nothing but a byte string.

THE TWO CONSTRAINTS IN THE ASK, HONOURED LITERALLY
--------------------------------------------------
1. **NO LLM.** Nothing here calls a model of any kind. Media type comes from MAGIC BYTES,
   page text and image placement come from the PDF's own content streams. Caption binding
   is pure geometry (``artefact_geometry``). The one thing a vision model would add — OCR
   of pixels — is stage B and stays DEFERRED, as ruled.
2. **Tied back to the CONTEXT.** :func:`map_text_to_chunk` resolves the ``documents.chunks``
   index an artefact's surrounding text landed in, which is what makes the artefact row
   reachable from the same chunk the prose was mined from. An image alone is not a memory.

EVERY BYTE IS HOSTILE — the posture, stated
-------------------------------------------
This is the first endpoint in the system that accepts attacker-chosen bytes, so the
defences are structural, not advisory:

* **Type is SNIFFED, never declared.** :func:`sniff_media_type` reads magic bytes. The
  client's ``Content-Type`` and the filename extension are RECORDED as claims and are
  never used to dispatch — a ``.pdf`` that is really something else is refused as what it
  really is. **Allow-list, not deny-list**: an unrecognised signature is refused, so a new
  hostile format is refused by DEFAULT rather than after someone remembers to add it.
* **Size is capped while STREAMING**, by the caller, not here — ``Content-Length`` is a
  claim too. :data:`DEFAULT_MAX_UPLOAD_BYTES` is the number; the transport enforces it as
  the body is consumed so a lying length cannot buy unbounded memory.
* **Resource bombs are bounded, loudly.** A PDF is a compressed container: page count,
  images per page and total images are capped (:data:`MAX_PAGES`, :data:`MAX_IMAGES_PER_PAGE`,
  :data:`MAX_IMAGES_TOTAL`) and exceeding one raises rather than truncating silently.
* **Nothing is executed and nothing touches the filesystem.** Parsing is pure-Python
  in-process over an in-memory buffer (``io.BytesIO``) — no temp file, no shell-out, no
  rasteriser, no font programme, and pdfminer does not run PDF JavaScript or open/launch
  actions. There is no unpacking of nested containers.
* **Filenames are sanitised to a LABEL, never a path.** :func:`sanitize_filename` strips
  directory separators, NULs and control characters. The value is only ever stored in a
  column and rendered — it never reaches a filesystem call.
* **Extracted text re-enters through the existing guard.** Text lifted out of a PDF is
  attacker-controlled prose; the caller runs it through the same
  ``_check_injection_signals`` the typed text path uses before any of it is ingested.

DEPENDENCY POSTURE — DEGRADE, NEVER LOSE THE ARTEFACT
-----------------------------------------------------
Text and geometry both come from **pdfplumber (MIT, pure Python, no ML, no GPU, no
network)**, which is an OWNER DECISION and is deliberately NOT in ``pyproject.toml``.
Absent, :func:`extract_pdf` raises :class:`~.artefact_geometry.GeometryUnavailable` and the
caller still RETAINS the file — degraded, never lost. ⚠️ **PyMuPDF/``fitz`` must never be
substituted**: AGPL-3.0 + paid Artifex licence, in-process import, network clause covers
hosted control planes. ``mutool`` is the same
dual licence, so there is no subprocess escape hatch.

An IMAGE upload needs no dependency at all: it is retained and its type sniffed with
nothing but the standard library.
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .artefact_geometry import GeometryUnavailable, PageLayout
from src.api.errors import PublicRefusal


# ── Flag. Default ON since 2026-08-21 (owner ruling: "Images must be imported into the
# DB on best effort" — the staged rollout ended and the lane went live). Default OFF
# remains available as the byte-for-byte rollback lever: with it off the door does not
# exist and nothing else changes.
_FLAG_INTAKE = "ARTEFACT_BINARY_INTAKE"


def _flag_on(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off", "")


def binary_intake_enabled() -> bool:
    return _flag_on(_FLAG_INTAKE, default="true")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


#: Transport-level cap. Distinct from ``artefacts.max_artefact_bytes()`` (the per-ROW cap)
#: on purpose: this bounds what we will READ off a socket, that bounds what we will STORE.
#: Sized to the same measured p99 of 4.9 MB/page — 32 MB admits a ~100-page image-heavy
#: scan and refuses a pathological one LOUDLY, never by silently truncating.
DEFAULT_MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: Container bombs: a few hundred KB of PDF can declare millions of pages/images.
MAX_PAGES = _int_env("ARTEFACT_MAX_PAGES", 2000)
MAX_IMAGES_PER_PAGE = _int_env("ARTEFACT_MAX_IMAGES_PER_PAGE", 200)
MAX_IMAGES_TOTAL = _int_env("ARTEFACT_MAX_IMAGES_TOTAL", 5000)

#: §6.2's born-digital test: extracted characters per page. Our own corpus measurement
#: (177 PDFs / 11 069 pages) is cleanly bimodal — p5 = 0 chars/page, p10 = 103 — so any
#: threshold in 20-100 separates the populations. Nutrient uses <40; CCpdf used >100 at
#: 93.15 % precision. 40 sits inside the measured gap.
MIN_CHARS_FOR_TEXT_PAGE = _int_env("ARTEFACT_MIN_CHARS_PER_PAGE", 40)


def max_upload_bytes() -> int:
    return _int_env("ARTEFACT_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)


class UploadTooLarge(ValueError):
    """The upload exceeded the transport cap. Refused WHOLE — a truncated PDF is corrupt
    bytes that look like good bytes, and the user would be told it was received."""


class UnsupportedMediaType(PublicRefusal, ValueError):
    """The sniffed signature is not on the allow-list. Carries what it actually looked
    like so the refusal can be specific rather than "invalid file". A ``PublicRefusal``: the
    sentence is authored here and is the caller's to read (src/api/errors.py)."""


class ArtefactBomb(PublicRefusal, ValueError):
    """A structurally pathological container (page/image count). Refused loudly — and the
    refusal sentence is authored, so it passes the error seam verbatim (``PublicRefusal``)."""


# ══════════════════════════════════════════════════════════════════════════════════════
# 1. TYPE FROM CONTENT — the client's claims are recorded, never trusted
# ══════════════════════════════════════════════════════════════════════════════════════

#: Magic-byte signatures, ALLOW-LIST. ``(offset, signature, media_type)``.
#: Sources are the format specifications themselves: PDF %PDF- (ISO 32000-1 §7.5.2);
#: PNG 8-byte signature (RFC 2083 §3.1); JPEG SOI FFD8FF (ITU-T T.81); GIF87a/89a
#: (GIF89a spec §17); RIFF....WEBP (WebP container spec); TIFF II*\\0 / MM\\0*
#: (TIFF 6.0 §2); BMP "BM" (Windows BITMAPFILEHEADER).
_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"%PDF-",                 "application/pdf"),
    (0, b"\x89PNG\r\n\x1a\n",     "image/png"),
    (0, b"\xff\xd8\xff",          "image/jpeg"),
    (0, b"GIF87a",                "image/gif"),
    (0, b"GIF89a",                "image/gif"),
    (0, b"II\x2a\x00",            "image/tiff"),
    (0, b"MM\x00\x2a",            "image/tiff"),
    (0, b"BM",                    "image/bmp"),
)

#: Media types this door will accept. Everything else is refused BY DEFAULT.
ALLOWED_MEDIA_TYPES = frozenset({
    "application/pdf", "image/png", "image/jpeg", "image/gif",
    "image/webp", "image/tiff", "image/bmp",
})


def sniff_media_type(data: bytes) -> str | None:
    """Media type from MAGIC BYTES alone. Returns None when nothing matches.

    The client's ``Content-Type`` header and the filename extension are both
    attacker-controlled and are never consulted here. This is the ONLY thing allowed to
    decide what a payload is.
    """
    if not data:
        return None
    for offset, sig, media_type in _SIGNATURES:
        if data[offset:offset + len(sig)] == sig:
            return media_type
    # WebP is a two-part RIFF signature: "RIFF" ....(size).... "WEBP".
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def describe_bytes(data: bytes, limit: int = 16) -> str:
    """A safe hex preview of the leading bytes, for a SPECIFIC refusal message.

    Hex only — never decoded to text, so a refusal can never echo attacker-chosen
    characters back into a log line or a response body.
    """
    return data[:limit].hex() or "<empty>"


def resolve_media_type(data: bytes, *, claimed_type: str | None = None,
                       claimed_filename: str | None = None) -> str:
    """Decide the media type, or refuse. Claims are for the LOG, not for the decision."""
    sniffed = sniff_media_type(data)
    if sniffed is None or sniffed not in ALLOWED_MEDIA_TYPES:
        raise UnsupportedMediaType(
            f"unsupported or unrecognised file signature ({describe_bytes(data)}); "
            f"client claimed type={(claimed_type or '')[:64]!r} "
            f"filename={(claimed_filename or '')[:64]!r}; "
            f"accepted: {', '.join(sorted(ALLOWED_MEDIA_TYPES))}"
        )
    return sniffed


#: Anything that could make a name act like a path or a control sequence.
_FILENAME_STRIP = re.compile(r"[\x00-\x1f\x7f/\\]+")


def sanitize_filename(name: str | None, *, max_len: int = 200) -> str | None:
    """Reduce a client-supplied filename to a harmless LABEL.

    The value is stored in a column and rendered back to a human; it never reaches a
    filesystem call, because this module never writes a file. Sanitising anyway is
    defence in depth against the day someone does — plus it keeps ``../`` and control
    characters out of logs and consoles.
    """
    if not name:
        return None
    cleaned = _FILENAME_STRIP.sub("", str(name)).strip().strip(".")
    cleaned = cleaned[:max_len].strip()
    return cleaned or None


# ══════════════════════════════════════════════════════════════════════════════════════
# 2. DETERMINISTIC EXTRACTION — page text + placed-image geometry. No model, anywhere.
# ══════════════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ExtractedPage:
    page_index: int
    text: str
    char_count: int
    image_count: int

    @property
    def has_text(self) -> bool:
        return self.char_count >= MIN_CHARS_FOR_TEXT_PAGE


@dataclass(frozen=True)
class ExtractedDocument:
    """Everything read off the file, with NOTHING inferred and nothing generated."""
    media_type: str
    pages: tuple[ExtractedPage, ...] = ()
    layouts: tuple[PageLayout, ...] = ()
    geometry_available: bool = True
    degraded_reason: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def pages_with_text(self) -> int:
        return sum(1 for p in self.pages if p.has_text)

    @property
    def pages_no_text(self) -> int:
        return self.page_count - self.pages_with_text

    @property
    def image_count(self) -> int:
        return sum(p.image_count for p in self.pages)

    @property
    def full_text(self) -> str:
        """Page texts joined on a BLANK LINE.

        Deliberate: ``_chunk_document`` splits on blank lines, so a page boundary becomes
        a chunk boundary candidate. That is what keeps a caption in the same chunk as the
        prose it sits with rather than straddling two pages.
        """
        return "\n\n".join(p.text for p in self.pages if p.text.strip())

    @property
    def is_text_bearing(self) -> bool:
        return self.pages_with_text > 0

    def counters(self) -> dict[str, Any]:
        """§6.2's per-document counters — the honest report, computed not guessed."""
        return {
            "pages_total": self.page_count,
            "pages_with_text": self.pages_with_text,
            "pages_no_text": self.pages_no_text,
            "images_found": self.image_count,
            "geometry_available": self.geometry_available,
            "degraded_reason": self.degraded_reason,
        }


def extract_pdf(data: bytes, *, max_pages: int | None = None) -> ExtractedDocument:
    """Read page text + placed-image geometry from PDF bytes. Deterministic; no model.

    Parsed from an in-memory buffer — the bytes never touch the filesystem, so there is no
    temp file to race, leak, or be handed to another process.

    Raises :class:`~.artefact_geometry.GeometryUnavailable` when pdfplumber is absent. The
    caller MUST treat that as "no extraction", never as "no artefact": retention has
    already happened and must never depend on this succeeding.
    """
    from .artefact_geometry import load_pdf_layout

    cap = MAX_PAGES if max_pages is None else min(MAX_PAGES, max_pages)
    seen_images = [0]

    def _guard(pno: int, page) -> None:
        """Bound the container BOMBS as the pages are walked, not after.

        A few hundred KB of PDF can declare millions of pages or images. Refusing WHOLE is
        deliberate — a partially-read bomb is not a smaller document, it is a document we
        would then have to lie about.
        """
        if pno >= cap:
            raise ArtefactBomb(
                f"PDF declares more than {cap} pages — refused whole "
                f"(raise ARTEFACT_MAX_PAGES deliberately if this is genuine)"
            )
        n = len(page.images or ())
        if n > MAX_IMAGES_PER_PAGE:
            raise ArtefactBomb(
                f"page {pno} declares {n} images, per-page cap is "
                f"{MAX_IMAGES_PER_PAGE} — refused whole"
            )
        seen_images[0] += n
        if seen_images[0] > MAX_IMAGES_TOTAL:
            raise ArtefactBomb(
                f"PDF declares more than {MAX_IMAGES_TOTAL} images — refused whole")

    # ONE reader, in artefact_geometry, which is the only place allowed to translate into
    # the top-origin convention. Deriving page TEXT from the layout's own lines (rather
    # than calling pdfplumber a second time) is what keeps the text and the geometry
    # describing the same read — a second reader would be a second chance to drift.
    layouts = load_pdf_layout(io.BytesIO(data), on_page=_guard)

    pages: list[ExtractedPage] = []
    for layout in layouts:
        page_text = "\n".join(ln.text for ln in layout.lines if ln.text.strip())
        pages.append(ExtractedPage(
            page_index=layout.page_index, text=page_text,
            char_count=len(page_text.strip()), image_count=len(layout.images),
        ))

    return ExtractedDocument(
        media_type="application/pdf",
        pages=tuple(pages), layouts=tuple(layouts), geometry_available=True,
    )


def extract(data: bytes, media_type: str) -> ExtractedDocument:
    """Dispatch on the SNIFFED media type. An image yields no text — that is a RESULT.

    A bare image carries no text layer and no internal geometry to bind against, so it is
    retained and reported as text-free rather than run through a reader that would invent
    something. Reading its pixels is stage B (OCR), which is deferred.
    """
    if media_type == "application/pdf":
        return extract_pdf(data)
    return ExtractedDocument(
        media_type=media_type, pages=(), layouts=(),
        geometry_available=True,
        degraded_reason=None,
        notes={"kind": "image", "reason": "a bare image has no text layer (OCR is stage B)"},
    )


# ══════════════════════════════════════════════════════════════════════════════════════
# 3. THE TIE BACK TO CONTEXT — which chunk did this artefact's surroundings land in?
# ══════════════════════════════════════════════════════════════════════════════════════

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    """Whitespace-normalised, casefolded. ``_chunk_document`` strips paragraphs and joins
    sentences with a single space, so a caption is a substring of its chunk only AFTER
    normalisation. Comparing raw would silently never match — and a binder that never
    matches looks exactly like a binder that has nothing to bind."""
    return _WS.sub(" ", (s or "")).strip().casefold()


def map_text_to_chunk(needle: str, chunks: Sequence[str]) -> int | None:
    """Index of the first chunk CONTAINING ``needle``. None when it is not found.

    This is the artefact→context tie: the caption (or, failing that, the page's own
    leading text) is a verbatim substring of the page text that was chunked, so locating
    it identifies the ``documents.chunks`` position the artefact belongs beside.

    Returns None rather than guessing. A wrong chunk binding would file a diagram against
    someone else's paragraph, which is the same class of harm as binding a neighbour's
    caption — worse than no binding at all.
    """
    target = _norm(needle)
    if not target or not chunks:
        return None
    for i, chunk in enumerate(chunks):
        if target in _norm(chunk):
            return i
    return None


def resolve_chunk_index(*, caption_text: str | None, page: ExtractedPage | None,
                        chunks: Sequence[str]) -> tuple[int | None, str | None]:
    """Resolve an artefact's chunk, preferring its CAPTION, falling back to its PAGE.

    Returns ``(chunk_index, method)``. ``method`` is recorded so a later reader can tell a
    precise caption-anchored tie from a coarse page-anchored one, rather than treating
    both as equally trustworthy.

    * ``caption`` — the bound caption text was located verbatim. Precise.
    * ``page_lead`` — no caption (A.5 declined), so the page's first substantial line
      anchors it. Coarse but honest: it says "this artefact came from around here".
    * ``None``     — neither resolved. Recorded as unbound, never fabricated.
    """
    if caption_text and caption_text.strip():
        idx = map_text_to_chunk(caption_text, chunks)
        if idx is not None:
            return idx, "caption"
    if page is not None and page.text.strip():
        for line in page.text.splitlines():
            if len(line.strip()) >= 12:          # a page number is not an anchor
                idx = map_text_to_chunk(line, chunks)
                if idx is not None:
                    return idx, "page_lead"
    return None, None
