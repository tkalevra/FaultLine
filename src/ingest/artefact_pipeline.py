"""The composition: retained bytes → placed images → captions → the CONTEXT TIE.

This is the only place that puts the three built-but-disconnected pieces together:

    binary_intake   (bytes → media type, page text, placed-image geometry)   — no DB, no LLM
    artefact_geometry (geometry → caption bindings, or an honest decline)    — no DB, no LLM
    artefacts       (rows: retention, caption, chunk tie, and the READS)     — DB, no LLM

**There is no LLM on this path, at any step, by construction** — nothing here imports
``llm_calls``, and the two decisions that could have wanted a model (what type is this file,
which text describes this image) are answered by MAGIC BYTES and PAGE GEOMETRY respectively.

ORDERING IS THE WHOLE DESIGN — RETAIN BEFORE UNDERSTAND
------------------------------------------------------
:func:`retain_upload` runs FIRST and alone. It writes the bytes and returns, before any
extraction is attempted, so that a PDF this build cannot parse — pdfplumber absent, a
malformed file, a page-count bomb — still leaves the user's artefact in the database. Every
later pass is then a pure ADDITION over something we still hold, never a one-shot lossy
transform of bytes we threw away. :func:`associate_upload` runs second and may fail freely.

That split is also what keeps the picture work OFF the hot text path, as instructed: the
text extracted from the file goes into the existing document lane untouched and at the
existing speed, and the artefact half is a separate, later, independently-failing step.

THE HARD LINE, restated because this file is where it could be broken
---------------------------------------------------------------------
The artefact is a MEMORY — content the user handed us, filed AT a place. Nothing here
classifies an artefact into L4, mints a type node from a filename, or writes a
``subclass_of`` rung. The only graph edges that exist for an upload are
``artefacts.upload_event_edges()`` — the artefact's NAME filed at the type node its own
IANA media subtype names, plus reachability from the user anchor. A caption is likewise
CONTENT: a proper name inside one belongs to the naming layer, never to L4.
"""
from __future__ import annotations

from typing import Any, Sequence

from . import artefacts as _A
from .artefact_geometry import CaptionBinding, GeometryUnavailable, associate_document
from .binary_intake import ExtractedDocument, resolve_chunk_index


def retain_upload(cur, *, user_id: str, data: bytes, media_type: str,
                  filename: str | None = None, source_ref: str | None = None,
                  document_id: int | None = None) -> dict[str, Any] | None:
    """STEP 1 — the bytes, and nothing else. Returns None when retention is flag-off.

    Deliberately does no extraction: this call is what must survive every downstream
    failure. Re-calling it later with ``document_id`` set is safe and is how the container
    row learns which document it became — ``retain_artefact``'s ON CONFLICT refreshes
    ``document_id`` while KEEPING the original ``captured_at`` (the first time the user
    gave it to us is the memory).
    """
    return _A.retain_artefact(
        cur, user_id=user_id, media_type=media_type, data=data,
        filename=filename, source_ref=source_ref, document_id=document_id,
    )


def associate_upload(cur, *, user_id: str, container_sha256: str, media_type: str,
                     extracted: ExtractedDocument, filename: str | None = None,
                     source_ref: str | None = None, document_id: int | None = None,
                     chunks: Sequence[str] = ()) -> dict[str, Any]:
    """STEP 2 — one row per placed image: geometry, caption verdict, and the chunk tie.

    Each embedded image becomes a PLACEMENT row (no bytes of its own — the retained
    container holds the pixels; see ``retain_artefact(placement_only=True)``), carrying:

    * its placed bounding box and page size, so a later pass can re-decide with better
      logic without re-reading the file;
    * A.5's caption verdict — bound (Class B) or DECLINED WITH A REASON. A decline is
      recorded, never dropped, so the product can say "I kept this figure but could not
      tell which text describes it" instead of asserting a neighbour's caption;
    * ``chunk_index`` + ``chunk_bind_method`` — the tie back to the surrounding prose.

    Every step is independently skippable: retention has already happened, so a failure
    here degrades the record rather than losing the artefact.
    """
    report: dict[str, Any] = {
        "placements": 0, "captions_bound": 0, "captions_declined": 0,
        "chunks_tied": 0, "tie_methods": {}, "decline_reasons": {},
    }
    if not extracted.layouts:
        return report

    bindings: list[CaptionBinding] = associate_document(list(extracted.layouts))
    pages_by_index = {p.page_index: p for p in extracted.pages}

    for b in bindings:
        page_layout = next((pl for pl in extracted.layouts
                            if pl.page_index == b.page_index), None)
        img = next((im for im in (page_layout.images if page_layout else ())
                    if im.index == b.artefact_index), None)
        if page_layout is None or img is None:
            continue

        row = _A.retain_artefact(
            cur, user_id=user_id, media_type=media_type,
            sha256=container_sha256, byte_size=0, placement_only=True,
            filename=filename, source_ref=source_ref, document_id=document_id,
            page_index=b.page_index, artefact_index=b.artefact_index,
            placement=(img.box.x0, img.box.top, img.box.x1, img.box.bottom),
            page_size=(page_layout.width, page_layout.height),
        )
        if row is None:                      # retention flag off — nothing to associate to
            return report
        report["placements"] += 1
        artefact_id = row["artefact_id"]

        _A.bind_caption(cur, user_id=user_id, artefact_id=artefact_id, binding=b)
        if b.bound:
            report["captions_bound"] += 1
        elif b.declined_reason:
            report["captions_declined"] += 1
            report["decline_reasons"][b.declined_reason] = (
                report["decline_reasons"].get(b.declined_reason, 0) + 1)

        # THE TIE. Prefer the caption (precise); fall back to the page's leading line
        # (coarse but honest); record NOTHING when neither resolves.
        idx, method = resolve_chunk_index(
            caption_text=b.caption_text if b.bound else None,
            page=pages_by_index.get(b.page_index),
            chunks=chunks,
        )
        _A.bind_chunk(cur, user_id=user_id, artefact_id=artefact_id,
                      document_id=document_id, chunk_index=idx, method=method)
        if idx is not None:
            report["chunks_tied"] += 1
            report["tie_methods"][method] = report["tie_methods"].get(method, 0) + 1

    return report


def upload_report(*, extracted: ExtractedDocument, retained: dict[str, Any] | None,
                  association: dict[str, Any] | None,
                  degraded_reason: str | None = None) -> dict[str, Any]:
    """The HONEST per-upload summary (§6.2) — what was kept, read, bound, and tied.

    The status is the point of the whole exercise:

    * ``retained_no_text`` — a DEGRADED STATE OVER A RETAINED ARTEFACT, not a data loss.
      "I have kept all 200 pages and I can show them to you, but I could not read any text
      from them, so nothing has entered your memory graph yet." That is materially better
      to tell a paying customer than "done" followed by empty recall — which is exactly
      what happened before this door existed, because a scanned PDF does not extract to
      NOTHING, it extracts to page numbers and stray ligatures, and "garbage-lite" sails
      past an emptiness check.
    * ``retained`` — bytes kept, text found; the text lane owns the rest.
    """
    counters = extracted.counters()
    if degraded_reason:
        counters["degraded_reason"] = degraded_reason
    status = "retained" if extracted.is_text_bearing else "retained_no_text"
    return {
        "status": status,
        "artefact_id": (retained or {}).get("artefact_id"),
        "sha256": (retained or {}).get("sha256"),
        "byte_size": (retained or {}).get("byte_size"),
        "duplicate": (retained or {}).get("duplicate"),
        "retention_recorded": retained is not None,
        **counters,
        **(association or {}),
    }
