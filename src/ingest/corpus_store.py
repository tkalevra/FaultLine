"""STORE-FIRST document ingest (wound-map round 4, migration 327).

THE WOUND THIS MODULE IS THE FIX FOR
------------------------------------
Document ingest used to hand every chunk to SYNCHRONOUS brain extraction (180s
ReadTimeouts, /extract/rewrite fallback, unbounded retries) on the SAME lane that
serves live traffic — measured twice on pre-prod: 59/62GB RSS, the provisioning
worker dead. Owner design: "snapshot/compress/store the image; the litmus:
document ingest doesn't cause the system to cack, 100% accuracy".

This module is the PURE half of the new lane: format detection, page splitting,
compression, and the zero-loss status gate. NO LLM, NO network, NO DB — everything
here is hermetically testable; the API endpoint (``/corpus/documents`` in
src/api/main.py) owns the transactional writes, and src/ingest/ocr_engines.py owns
the pluggable OCR uplift.

COMPRESSION CHOICE (documented, per the design's "your choice" mandate)
-----------------------------------------------------------------------
zlib level 9, application-side, via the STDLIB ``zlib`` module:

* zero new dependencies (the repo rule for this build: no heavy deps), and it is
  importable in every image that runs this code — Python ships it;
* deterministic round-trip, so the ``content_sha256``-over-RAW-bytes fidelity
  contract is provable: decompress(raw_snapshot) hashes to content_sha256;
* the codec is STAMPED per row (``snapshot_codec``), so a future zstd /
  pg_compression swap adds a new codec string and never silently reinterprets
  old rows. Postgres has no in-core column compression API we can pin per-table
  without an extension (pg_compression is not in the base image), so doing it in
  the application is also the only choice that deploys today.

ZERO-LOSS TEXT CONTRACT
-----------------------
``corpus_pages.layout_text`` stores whatever extraction/OCR produced VERBATIM.
DeepSeek-OCR-style layout anchors (``<｜ref｜>`` / ``<｜box｜>`` tokens with
coordinates) are CONTENT — nothing in this module ever strips, rewrites, or
"cleans" page text. The status gate below is the fidelity enforcement: a page
whose ``finish_reason`` is not exactly ``'stop'`` can never make the document
``ready``.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Optional

# ── Compression (documented choice: stdlib zlib level 9 — see module docstring) ─────
SNAPSHOT_CODEC = "zlib"
_SNAPSHOT_ZLIB_LEVEL = 9


def compress_snapshot(raw: bytes) -> bytes:
    """Compress the RAW source bytes for storage (zlib 9). Round-trip deterministic."""
    import zlib
    return zlib.compress(raw or b"", _SNAPSHOT_ZLIB_LEVEL)


def decompress_snapshot(blob: bytes, codec: str = SNAPSHOT_CODEC) -> bytes:
    """The inverse of :func:`compress_snapshot`. Refuses an unknown codec loudly
    (a mis-stamped row must never be "decompressed" into garbage silently)."""
    import zlib
    if (codec or "").strip().lower() != SNAPSHOT_CODEC:
        raise ValueError(f"unknown snapshot codec: {codec!r}")
    return zlib.decompress(blob or b"")


def content_sha256(raw: bytes) -> str:
    """SHA-256 over the RAW (uncompressed) source bytes — the fidelity contract."""
    return hashlib.sha256(raw or b"").hexdigest()


# ── Format detection (magic bytes FIRST — the claimed name is a hint, not truth) ─────

#: The format kinds the store-first lane understands.
#:   text  — text-native bytes the store splits into pages directly (no model,
#:           the DeepSeek text_recognition shape: "no model" for Word/PPT-without-
#:           images analogues; here: txt/md/code/csv/json + docx/pptx via a stdlib
#:           zip+XML walk)
#:   pdf   — page-split via the existing deterministic pdfplumber lane
#:   image — a bare pixel format (PNG/JPEG/GIF/BMP/WebP/TIFF): snapshot stored,
#:           layout_text EMPTY pending the OCR uplift
#:   unknown — bytes we cannot honestly classify: snapshot retained, status failed
FORMAT_TEXT = "text"
FORMAT_PDF = "pdf"
FORMAT_IMAGE = "image"
FORMAT_UNKNOWN = "unknown"

_MAGIC = (
    (b"%PDF-", FORMAT_PDF),
    (b"\x89PNG\r\n\x1a\n", FORMAT_IMAGE),
    (b"\xff\xd8\xff", FORMAT_IMAGE),          # JPEG
    (b"GIF87a", FORMAT_IMAGE),
    (b"GIF89a", FORMAT_IMAGE),
    (b"BM", FORMAT_IMAGE),                    # BMP (2-byte prefix; combined w/ size sanity below)
    (b"II*\x00", FORMAT_IMAGE),               # TIFF little-endian
    (b"MM\x00*", FORMAT_IMAGE),               # TIFF big-endian
)

_WEBP_RIFF = b"RIFF"


def _zip_first_entry(data: bytes, wanted: tuple[str, ...]) -> bool:
    """True when the zip's FIRST entry matches one of ``wanted`` (the ODF/OOXML
    "the mimetype/storage shape leads" convention). Stdlib, bounded to names."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            return bool(names) and names[0] in wanted
    except Exception:  # noqa: BLE001 — a non-zip must classify as unknown, not raise
        return False


def detect_format(data: bytes, filename: str = "") -> str:
    """Classify source bytes into the store-first lane's format kinds.

    Magic bytes decide; the filename extension only breaks ties the bytes cannot
    (a ``.docx``/``.pptx`` is a PK zip whose FIRST entry names the flavour:
    ``word/`` vs ``ppt/``). A PK zip that is neither (an .odt, a stray zip) is
    UNKNOWN — retained honestly, never guessed at. UTF-8-decodable non-binary
    bytes with no magic are TEXT (txt/md/code/csv/json all land here).
    """
    if not data:
        return FORMAT_UNKNOWN
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == _WEBP_RIFF and data[8:12] == b"WEBP":
        return FORMAT_IMAGE
    if data.startswith(b"PK\x03\x04"):
        if _zip_first_entry(data, ("word/",)):
            return FORMAT_TEXT   # docx: direct text extraction below (no lib needed)
        if _zip_first_entry(data, ("ppt/",)):
            return FORMAT_TEXT   # pptx: same stdlib XML walk
        return FORMAT_UNKNOWN
    # No container magic: text-native iff it decodes as UTF-8 without NUL bytes
    # (a NUL is the classic binary tell). This admits txt/md/code/csv/json and
    # any other prose format honestly, without a mime zoo.
    if b"\x00" not in data:
        try:
            data.decode("utf-8")
            return FORMAT_TEXT
        except UnicodeDecodeError:
            pass
    return FORMAT_UNKNOWN


def guess_mime(fmt: str, filename: str = "") -> str:
    """Best-effort MIME for the corpus row (display metadata, never a dispatch key)."""
    _ext = re.sub(r"^.*\.", "", (filename or "").strip()).lower()
    if fmt == FORMAT_PDF:
        return "application/pdf"
    if fmt == FORMAT_IMAGE:
        return {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp",
            "tif": "image/tiff", "tiff": "image/tiff",
        }.get(_ext, "application/octet-stream")
    if fmt == FORMAT_TEXT:
        if (filename or "").strip().lower().endswith(".docx"):
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if (filename or "").strip().lower().endswith(".pptx"):
            return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        return {
            ".md": "text/markdown", ".csv": "text/csv", ".json": "application/json",
        }.get("." + _ext, "text/plain")
    return "application/octet-stream"


# ── Page splitting (deterministic, no model — the text_recognition "no model" shape) ─

@dataclass
class CorpusPage:
    """One page's layout-grounded text + its fidelity stamp.

    ``finish_reason`` semantics (the DeepSeek OCR guide contract):
      'stop'  — the text for this page is complete;
      'length'— the text was TRUNCATED at an output limit: the page is incomplete
                and the document must never report ready;
      None    — never attempted (a pixel page pending the OCR uplift).
    """
    page_index: int
    text: str = ""
    finish_reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"page_index": self.page_index,
                "text": self.text,
                "finish_reason": self.finish_reason}


# A single text "page" is bounded so a pasted 2MB txt does not become one
# unbounded TEXT row; the split point is a form feed when present (the classic
# printed-page break) and a character budget otherwise. NOTHING is dropped.
_TEXT_PAGE_MAX_CHARS = 8000


def _split_text_pages(text: str) -> list[CorpusPage]:
    pages: list[CorpusPage] = [p for p in text.split("\f")]
    out: list[CorpusPage] = []
    idx = 0
    for page in pages:
        if len(page) <= _TEXT_PAGE_MAX_CHARS:
            out.append(CorpusPage(idx, page, "stop"))
            idx += 1
            continue
        # Budget-split an oversized form-feed page; every slice keeps 'stop' —
        # the split is deterministic and total, not a truncation.
        for off in range(0, len(page), _TEXT_PAGE_MAX_CHARS):
            out.append(CorpusPage(idx, page[off:off + _TEXT_PAGE_MAX_CHARS], "stop"))
            idx += 1
    return out


# Minimal stdlib OOXML text walk — deliberately NOT a full parser: w:t (docx) and
# a:t (pptx) runs are the text carriers; namespaces are matched by LOCAL NAME so
# no namespace map is trusted. No new deps (python-docx/python-pptx are NOT in
# the base image; adding them was ruled out for this build).
def _ooxml_text_runs(data: bytes, tag_local: str, page_container: str) -> list[CorpusPage]:
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist()
                 if n.startswith(page_container) and n.endswith(".xml")]
        pages: list[CorpusPage] = []
        for pno, name in enumerate(sorted(names)):
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                pages.append(CorpusPage(pno, "", None))  # unparseable part: pending
                continue
            runs = [el.text or "" for el in root.iter()
                    if el.tag.rsplit("}", 1)[-1] == tag_local]
            pages.append(CorpusPage(pno, "".join(runs), "stop"))
        return pages


def extract_pages(data: bytes, fmt: str) -> list[CorpusPage]:
    """Deterministic page extraction for text-native formats (NO model, NO LLM).

    * text/plain-ish → form-feed/char-budget pages, every page 'stop';
    * docx (word/) → one page per word/document.xml paragraph? No — per XML part:
      the stdlib walk reads ``word/document.xml`` as ONE page stream (paragraph
      boundaries are joins, page boundaries are not in the XML). PPTX gets one
      page PER SLIDE (ppt/slides/slideN.xml) — that IS its page shape.
    * pdf → the existing deterministic pdfplumber page walk (binary_intake);
      a page with text is 'stop'; a text-less page is None (pending OCR).
    * image / unknown → NO pages here (the snapshot is the store; the OCR uplift
      fills pages later). Returns [] — never a fabricated empty-text 'stop' page.
    """
    if fmt == FORMAT_TEXT:
        if data.startswith(b"PK\x03\x04") and _zip_first_entry(data, ("word/",)):
            pages = _ooxml_text_runs(data, "t", "word/")
            return pages
        if data.startswith(b"PK\x03\x04") and _zip_first_entry(data, ("ppt/",)):
            pages = _ooxml_text_runs(data, "t", "ppt/slides/")
            return pages
        return _split_text_pages(data.decode("utf-8", errors="replace"))
    if fmt == FORMAT_PDF:
        from src.ingest.binary_intake import extract_pdf, GeometryUnavailable
        try:
            doc = extract_pdf(data)
        except GeometryUnavailable:
            return []   # pdfplumber absent: snapshot retained, pages pending uplift
        out: list[CorpusPage] = []
        for p in doc.pages:
            has_text = bool((p.text or "").strip())
            out.append(CorpusPage(p.page_index, p.text or "",
                                  "stop" if has_text else None))
        return out
    return []


# ── The zero-loss status gate ────────────────────────────────────────────────────────

STATUS_READY = "ready"
STATUS_INCOMPLETE = "incomplete"
STATUS_PENDING = "pending"
STATUS_FAILED = "failed"


def pages_status(pages: list[CorpusPage]) -> tuple[str, dict[str, Any]]:
    """The fidelity gate: ('ready'|'incomplete', report).

    'ready' IFF there is at least one page AND EVERY page has finish_reason
    exactly 'stop' AND non-empty text. ANY 'length' page → 'incomplete' with the
    page list (the zero-loss contract: a truncated page must be visible in the
    ingest response, never papered over). A pending (None / empty-text) page →
    'incomplete' with the pending list. The report is plain data so the endpoint
    can hand it to the caller verbatim — honesty at the door, not after.
    """
    if not pages:
        return STATUS_INCOMPLETE, {"pages_pending_ocr": 0, "pages_length": 0,
                                   "pages": 0,
                                   "reason": "no_pages_extracted"}
    length_pages = [p.page_index for p in pages if p.finish_reason == "length"]
    # 'pending' = never attempted (None) or no text to show for it. 'stop' pages
    # are only ever stamped on non-empty text by our extractors, but the gate
    # re-derives from the rows — it does not trust construction.
    pending_pages = sorted(
        p.page_index for p in pages
        if p.finish_reason != "length"
        and (p.finish_reason is None or not (p.text or "").strip()))
    if not length_pages and not pending_pages:
        return STATUS_READY, {"pages_pending_ocr": 0, "pages_length": 0,
                              "pages": len(pages)}
    return STATUS_INCOMPLETE, {
        "pages_pending_ocr": pending_pages,
        "pages_length": length_pages,
        "pages": len(pages),
        "reason": ("page_output_truncated" if length_pages
                   else "pages_pending_ocr"),
    }


# ── Snapshot assembly ────────────────────────────────────────────────────────────────

@dataclass
class CorpusSnapshot:
    """Everything one corpus_documents + corpus_pages write needs. Pure data."""
    user_id: str
    source_ref: str = ""
    original_filename: str = ""
    mime: str = ""
    byte_size: int = 0
    content_sha256_: str = ""
    raw_snapshot: bytes = b""
    snapshot_codec: str = SNAPSHOT_CODEC
    pages: list[CorpusPage] = field(default_factory=list)
    status: str = STATUS_PENDING
    status_report: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def decompressed(self) -> bytes:
        return decompress_snapshot(self.raw_snapshot, self.snapshot_codec)


def build_snapshot(*, user_id: str, data: bytes, filename: str = "",
                   source_ref: str = "",
                   pages: Optional[list[dict[str, Any]]] = None,
                   ocr_engine: str = "none") -> CorpusSnapshot:
    """Assemble the store-first snapshot from RAW source bytes.

    ``pages`` (optional) lets a caller that ALREADY parsed the bytes hand over its
    page results (the MCP binary door's pdfplumber pass) so nothing parses twice.
    Shape: [{page_index, text, finish_reason}] — finish_reason omitted/None means
    pending; a caller-supplied 'length' rides through untouched (the gate honors
    it). ANCHOR TOKENS IN ``text`` ARE STORED VERBATIM — never cleaned here.
    """
    fmt = detect_format(data, filename)
    snap = CorpusSnapshot(
        user_id=user_id,
        source_ref=(source_ref or "").strip(),
        original_filename=(filename or "").strip(),
        mime=guess_mime(fmt, filename),
        byte_size=len(data),
        content_sha256_=content_sha256(data),
        raw_snapshot=compress_snapshot(data),
    )
    if fmt == FORMAT_UNKNOWN:
        snap.status = STATUS_FAILED
        snap.error = "unknown_format"
        snap.status_report = {"reason": "unknown_format", "pages": 0}
        return snap
    if pages is None:
        corpus_pages = extract_pages(data, fmt)
    else:
        corpus_pages = [
            CorpusPage(int(p.get("page_index", i)),
                       str(p.get("text") or ""),
                       p.get("finish_reason"))
            for i, p in enumerate(pages)
        ]
        corpus_pages.sort(key=lambda p: p.page_index)
    snap.pages = corpus_pages
    snap.status, snap.status_report = pages_status(corpus_pages)
    return snap


# ── Backpressure (the cap the wound lacked) ──────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    import os
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


#: The legacy documents-registry uplift queue cap. The SNAPSHOT is never refused
#: (store-first: the bytes always land); what the cap bounds is the DOWNSTREAM
#: background extraction work the snapshot can schedule. Over cap → the uplift is
#: DEFERRED and the response says so — ingest stays bounded-fast under a corpus
#: dump, which is the litmus.
CORPUS_UPLIFT_QUEUE_CAP = _env_int("CORPUS_UPLIFT_QUEUE_CAP", 200)


def uplift_admission(active_downstream: int,
                     cap: Optional[int] = None) -> dict[str, Any]:
    """Pure admission decision for scheduling downstream uplift work.

    Returns {'admit': bool, 'cap': cap, 'active': active}. A negative cap means
    'no cap' (operator override); the decision is data, not a side effect.
    ``cap`` resolves CORPUS_UPLIFT_QUEUE_CAP at CALL time (late-bound) so an
    operator env change or a test monkeypatch is honored."""
    _cap = CORPUS_UPLIFT_QUEUE_CAP if cap is None else int(cap)
    admit = True if _cap < 0 else (int(active_downstream) < _cap)
    return {"admit": admit, "cap": _cap, "active": int(active_downstream)}


# ── Text-lane backpressure (wound-map round 5, DOC_TEXT_LANE_BACKPRESSURE) ──────────
#
# THE WOUND (tools/regression_results/doc_clog_litmus_report.md, wound 1): a ~1MB
# ingest_document chunks to ~3.6k chunks and the drain enqueues/claims ALL of them
# at once — every one a brain extraction call on the SAME event loop that serves
# live traffic. Under co-load the loop starved (healthcheck timeouts,
# doc_drain.backend_stalled, container restart). The snapshot-store lane (above)
# capped its DOWNSTREAM uplift; the legacy text lane's chunk drain had no cap at
# all. This is that cap, aligned with the corpus uplift cap by default.

def seat_chunk_budget() -> int:
    """The per-seat per-PASS chunk budget for the text-lane drain.

    Defaults to the LIVE CORPUS_UPLIFT_QUEUE_CAP env (one number, one doctrine:
    the same cap that bounds the corpus uplift bounds the legacy drain's
    scheduled chunks per pass — an operator who moves one moves both). Negative
    = no cap (operator override). Resolved at CALL time so an operator env
    change or a test monkeypatch is honored."""
    return _env_int("DOC_SEAT_CHUNK_BUDGET",
                    _env_int("CORPUS_UPLIFT_QUEUE_CAP", CORPUS_UPLIFT_QUEUE_CAP))


def chunk_pass_admission(owed: int, active_downstream: int = 0,
                         cap: Optional[int] = None) -> dict[str, Any]:
    """Pure per-pass admission for the text-lane chunk drain.

    Returns {'admit': n, 'cap': cap, 'active': active, 'owed': owed,
    'deferred': owed - n} — how many of ``owed`` chunks THIS drain pass may
    schedule. ``admit`` is clamped to [0, owed]; 0 means DEFER (never REFUSE:
    the document re-pends with its ledger and the next pass admits more — the
    defer-not-refuse envelope, same contract as uplift_admission). A negative
    cap admits everything (operator override = byte-parity with the uncapped
    lane). The decision is data, not a side effect."""
    _cap = seat_chunk_budget() if cap is None else int(cap)
    _owed = max(0, int(owed))
    _active = max(0, int(active_downstream))
    if _cap < 0:
        n = _owed
    else:
        n = max(0, min(_owed, _cap - _active))
    return {"admit": n, "cap": _cap, "active": _active, "owed": _owed,
            "deferred": _owed - n}


#: Hard ceiling on UNPRODUCTIVE drain passes (each re-pend with no owed-chunk
#: decrease) before the document finalizes terminally with an honest failed
#: status. Kills the re-pend-forever tail of the 180s-timeout fallback loop.
DOC_TEXT_LANE_RETRY_CEILING_ENV = "DOC_TEXT_LANE_RETRY_CEILING"


def retry_ceiling() -> int:
    """Resolve DOC_TEXT_LANE_RETRY_CEILING at CALL time (late-bound)."""
    return _env_int(DOC_TEXT_LANE_RETRY_CEILING_ENV, 6)


def retry_ceiling_verdict(passes_unproductive: int, made_progress: bool,
                          ceiling: Optional[int] = None) -> dict[str, Any]:
    """Pure verdict: has this document exhausted its HARD retry ceiling?

    Returns {'exhausted': bool, 'ceiling': c, 'passes': n}. ``made_progress``
    (the owed count decreased this pass) RESETS the keying question — a
    productive document is draining, not looping, and is never terminated by
    this ceiling (the same progress-aware doctrine the defer-clock bound uses).
    A negative ceiling disables the bound (operator override)."""
    _c = retry_ceiling() if ceiling is None else int(ceiling)
    _n = int(passes_unproductive)
    exhausted = (not made_progress) and _c >= 0 and _n >= _c
    return {"exhausted": exhausted, "ceiling": _c, "passes": _n}


# ── Versioned supersession (migration 328 — the apache-versions contract) ────────────
#
# Documents inherit the facts lane's supersession discipline: a stable FAMILY
# identity per source artefact, version_seq within the family, and
# superseded_at marking archived versions. THE DEFAULT in every retrieval and
# list is the LIVE (unsuperseded) version per family; archived versions stay
# byte-readable forever (GET /corpus/documents/{id}/original?version=N).
#
# FLAG: DOC_VERSIONING (default ON; OFF = byte-parity with 327 — no family
# logic, ingest exactly as today).

def derive_document_family(user_id: str, source_ref: str = "",
                           original_filename: str = "") -> Optional[str]:
    """The stable family identity for a source artefact.

    DERIVATION RULE (documented + pinned; migration 328's backfill derives the
    SAME value in SQL for pre-328 rows, so both sides must stay in lockstep):
      key  = source_ref if it trims to non-empty, else original_filename;
             the key is ASCII-lowercased, whitespace-trimmed, and backslashes
             are normalized to forward slashes (Windows-path tolerance — the
             same artefact dropped as ``config\\httpd.conf`` and
             ``config/httpd.conf`` is ONE family);
      family = md5(user_id + "\\x00" + key) hex.
    MD5 is an IDENTITY KEY, not a checksum — content integrity stays
    content_sha256 — chosen because Postgres's built-in md5() matches it with
    zero extension dependencies (pgcrypto is not in the base image). Returns
    None when BOTH names are empty: an unnamed artefact has no stable identity
    and every ingest of it lands as its own row (the honest 327 behavior).
    """
    key = (source_ref or "").strip()
    if not key:
        key = (original_filename or "").strip()
    if not key:
        return None
    norm = key.lower().replace("\\", "/")
    return hashlib.md5(
        ((user_id or "") + "\x00" + norm).encode("utf-8")).hexdigest()


def resolve_version_action(family_versions: list[dict[str, Any]],
                           new_sha: str) -> dict[str, Any]:
    """Pure dedupe-vs-mint decision for one ingest against a family's versions.

    ``family_versions``: [{id, content_sha256, version_seq, superseded_at}] for
    ONE document_family (any order). Decision table (documented + pinned):
      * same bytes as the LIVE version (unsuperseded) → {'action': 'duplicate',
        'duplicate_of': <live id>} — sha256 dedupe, NO new version row. A
        re-ingest is a no-op, not a version.
      * anything else → {'action': 'mint', 'next_version_seq': max(seq)+1 (or
        1), 'supersede_id': <live id or None>}. NOTE the deliberate rule:
        re-ingesting an ARCHIVED version's bytes while a different live version
        stands MINTS a new version — a genuine rollback ("the config was
        reverted") is a real event in the artefact's history, never silently
        deduped to a dead row.
    """
    versions = sorted(
        (v for v in (family_versions or [])
         if isinstance(v, dict) and v.get("id") is not None),
        key=lambda v: (int(v.get("version_seq") or 0), int(v["id"])))
    live = [v for v in versions if v.get("superseded_at") is None]
    live_row = live[-1] if live else None
    if live_row is not None and live_row.get("content_sha256") == new_sha:
        return {"action": "duplicate", "duplicate_of": int(live_row["id"]),
                "live_id": int(live_row["id"]),
                "live_version_seq": live_row.get("version_seq")}
    next_seq = (int(versions[-1]["version_seq"]) + 1) if versions else 1
    return {"action": "mint", "next_version_seq": next_seq,
            "supersede_id": (int(live_row["id"]) if live_row is not None
                             else None)}


class SnapshotIntegrityError(Exception):
    """The stored snapshot does not hash back to content_sha256 — DATA LOSS.

    Raised by :func:`verify_snapshot` instead of ever returning corrupted
    bytes; the read verb maps this to HTTP 500 + a loud log (never serve a
    byte that fails the fidelity contract)."""


def verify_snapshot(raw_snapshot: bytes, snapshot_codec: str,
                    expected_sha256: str) -> bytes:
    """The RETURN VERB's gate: decompress and RE-HASH before serving.

    Returns the ORIGINAL raw bytes iff sha256(decompress(raw_snapshot)) ==
    expected_sha256. Any failure — unknown codec, corrupted zlib stream, hash
    mismatch — raises :class:`SnapshotIntegrityError`; corrupted bytes are
    NEVER returned to a caller."""
    try:
        raw = decompress_snapshot(raw_snapshot, snapshot_codec)
    except Exception as e:  # noqa: BLE001 — any decode failure is corruption
        raise SnapshotIntegrityError(
            f"snapshot undecodable (codec={snapshot_codec!r}): {e}") from e
    if content_sha256(raw) != (expected_sha256 or ""):
        raise SnapshotIntegrityError(
            f"snapshot hash mismatch: stored {expected_sha256} != "
            f"re-derived {content_sha256(raw)}")
    return raw
