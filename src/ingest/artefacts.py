"""Stage A — ARTEFACT RETENTION, and the read path that makes it worth anything.

Spec: the internal design record §2 (storage), §4 (provenance/tier).

THE PRINCIPLE THIS INHERITS
---------------------------
The document lane already ratified: typed content becomes triples; UNTYPEABLE content is
RETAINED RAW rather than discarded (``episodic_log`` for a turn, ``documents.chunks`` for a
document — "the per-document verbatim safety net"). An image is simply another untypeable
artefact. Retain it now, understand it later.

Retention is not merely sequencing — it changes what understanding is ALLOWED to be. With
the artefact retained, every later pass (A.5 caption binding today, OCR or a better
extractor in 2027) is a pure ADDITION over something we still hold, never a one-shot lossy
transform of bytes we threw away.

PROVENANCE SPLITS THREE WAYS — AND ONLY THE FIRST IS THE USER SPEAKING
---------------------------------------------------------------------
1. **The upload event** — "you handed me ``topology.pdf``, 4.1 MB, on 2026-07-31". This is
   a fact about an ACT THE USER PERFORMED. Nothing is inferred, so it is ``user_stated`` /
   **Class A**, exactly like any other ``source="mcp"`` write. Retention therefore delivers
   real Class-A memory BEFORE A SINGLE PIXEL IS READ.
2. **The caption binding** — "this text block sits next to this image, so it describes it".
   That is the ENGINE's geometry guessing at intent → **Class B**.
3. **OCR text** — a perception of pixels, with a measurable error rate. Stage B, DEFERRED.

**Class B, never C**, and the distinction is load-bearing: Class C carries
``expires_at = now() + 30 days`` and ``expire_staged_facts`` decays ``WHERE fact_class='C'``,
so routing artefact-derived content to C puts a 30-DAY FUSE on a 200-page corpus. C is for
content the engine could not TYPE. A caption types fine — the engine is merely less sure it
belongs to that image, and that is a CONFIDENCE question, not a classification failure. The
database CHECK (``artefacts_caption_class_b``) and :func:`bind_caption` both refuse anything
else; the precedent is the feelings ruling, which was mis-built once as "force this content
kind to C" and reverted.

THE HARD LINE
-------------
A caption is CONTENT from the user's document, filed AT a place. The artefact is an
artefact — it is **never** an L4 node, and neither is its caption. A proper name inside a
caption belongs to the NAMING layer (``also_known_as``/``pref_name``), never to L4. Only the
TYPE of the depicted thing touches L4, and it attaches to the type node.

FLAG-GATED, DEFAULT ON since 2026-08-21
---------------------------------------
``ARTEFACT_RETENTION`` (default ON since the owner's images-best-effort rollout ruling;
explicit ``false`` remains the byte-identical rollback lever) gates every write here. With
it off this module makes no database call at all, which is why importing it cannot change
any existing behaviour.

The module IS wired to intake paths since the binary door landed: POST /ingest_file (raw
bytes) and the ingest_file tool / /ingest_file_b64 (base64) both drive it through the
shared ingest_file_core. (The earlier "NOT WIRED" note below described the pre-door state
and is kept for history:) ``ingest_document``/``POST /v1/documents`` take ``text: str``;
there is no ``UploadFile``, no multipart, and no upload control in any
console anywhere in ``src/``. FaultLine never sees a PDF today, it
sees a string some client already extracted. This module is therefore exercised from a
test/CLI seam, and is deliberately transport-agnostic (it takes a cursor and bytes) so it
does not depend on a door that does not exist yet.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Iterable, Sequence

from .artefact_geometry import CaptionBinding

#: Every write path here is gated. Default ON since 2026-08-21 (owner images-best-effort
#: rollout); flag-off is byte-identical to not having this module at all (no DDL is
#: touched, no row is written, no exception is raised) — the rollback lever.
_FLAG_RETENTION = "ARTEFACT_RETENTION"
#: Gates only the ASSOCIATION write. Retention can be on while association is off — that
#: is the intended v1 posture (retain first; associate once the geometry read is trusted).
_FLAG_CAPTION = "ARTEFACT_CAPTION_BINDING"

#: Hard per-artefact cap, well below PostgreSQL's 1 GB bytea field limit. Sized against the
#: measured p99 of 4.9 MB/page: 32 MB admits a ~100-page image-heavy scan and refuses a
#: pathological one LOUDLY (never by silently truncating).
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

#: The tier ruling, as constants so no caller can spell them differently.
IDENTITY_PROVENANCE = "user_stated"     # the upload event: the user's own act
IDENTITY_CLASS = "A"
CAPTION_PROVENANCE = "engine_inferred"  # geometry: the engine's guess about intent
CAPTION_CLASS = "B"                     # NEVER A, NEVER C — see the module docstring

#: Columns a metadata read returns. `bytes` is DELIBERATELY ABSENT: a recall surface must
#: never haul a 4 MB payload to render one line. Payload is fetched by explicit request
#: through load_artefact_payload().
_META_COLUMNS = (
    "id", "user_id", "document_id", "filename", "media_type", "byte_size", "sha256",
    "page_index", "artefact_index", "external_ref", "source_ref",
    "fact_provenance", "fact_class", "captured_at",
    "page_width", "page_height", "bbox_x0", "bbox_top", "bbox_x1", "bbox_bottom",
    "caption_text", "caption_provenance", "caption_fact_class", "caption_confidence",
    "caption_method", "caption_declined_reason", "caption_bound_at",
    "chunk_index", "chunk_bind_method",
    "text_layer", "ocr_text", "ocr_engine", "ocr_confidence", "understood_at",
)

#: The two ways an artefact can be tied to the text around it (migration 208). Kept apart
#: on purpose — collapsing them would launder a coarse guess into a precise fact.
CHUNK_BIND_METHODS = ("caption", "page_lead")


class ArtefactTooLarge(ValueError):
    """The artefact exceeds the configured cap. Raised LOUDLY — never silently truncated:
    a truncated artefact is corrupt bytes that look like good bytes."""


def _flag_on(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off", "")


def retention_enabled() -> bool:
    return _flag_on(_FLAG_RETENTION, default="true")


def caption_binding_enabled() -> bool:
    return _flag_on(_FLAG_CAPTION, default="true")


def max_artefact_bytes() -> int:
    try:
        return int(os.getenv("ARTEFACT_MAX_BYTES", "").strip() or DEFAULT_MAX_BYTES)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _row_to_dict(cur, row) -> dict[str, Any]:
    return dict(zip([d[0] for d in cur.description], row))


# ── WRITE: retention ────────────────────────────────────────────────────────────────────

def retain_artefact(
    cur,
    *,
    user_id: str,
    media_type: str,
    data: bytes | None = None,
    external_ref: str | None = None,
    filename: str | None = None,
    document_id: int | None = None,
    page_index: int | None = None,
    artefact_index: int | None = None,
    source_ref: str | None = None,
    byte_size: int | None = None,
    sha256: str | None = None,
    placement: Sequence[float] | None = None,
    page_size: Sequence[float] | None = None,
    placement_only: bool = False,
) -> dict[str, Any] | None:
    """Retain ONE artefact in the CALLER-BOUND tenant schema. Returns None when flag-off.

    The cursor must already carry the tenant ``search_path`` (the existing ``bind_tenant``
    chokepoint) — this module never derives or sets a schema, so it adds no new isolation
    boundary and needs no new poison test.

    ``data`` XOR ``external_ref``: bytes live inline today; ``external_ref`` is the
    pre-built object-storage flip point, so moving a tenant out is a background copy plus a
    flag — config, not a migration. With ``external_ref`` the caller supplies ``byte_size``
    and ``sha256`` (they describe the payload, wherever it lives).

    Re-retaining the same payload is idempotent by ``(user_id, sha256, page_index,
    artefact_index)`` and reports ``duplicate=True`` — which is also the "you already gave
    me this on March 3rd" answer, for free.
    """
    if not retention_enabled():
        return None

    if placement_only:
        # A PLACEMENT ROW: an image embedded INSIDE a retained file. It deliberately
        # carries NEITHER bytes NOR external_ref, and the DB agrees — the storage CHECK is
        # `num_nonnulls(bytes, external_ref) <= 1`, so "neither" is legal by construction.
        #
        # WHY NOT STORE THE EMBEDDED IMAGE'S OWN BYTES: extracting an XObject means
        # re-encoding it for every filter that is not already a whole file (FlateDecode is
        # raw pixel data, not a PNG), which needs an imaging dependency and produces a
        # DERIVED asset — precisely what the spec rules out ("store the artefact as
        # uploaded"; re-rendering multiplies storage ~10x for something regenerable). The
        # container file IS retained, so the pixels are never lost: this row is the
        # geometry + caption + context record that makes them FINDABLE.
        #
        # It shares the container's `sha256` and adds page/artefact index, so the identity
        # index (user_id, sha256, COALESCE(page_index,-1), COALESCE(artefact_index,-1))
        # dedups it exactly right: re-uploading the same PDF re-derives the same rows
        # instead of multiplying them.
        if data is not None or external_ref is not None:
            raise ValueError(
                "placement_only= describes an image INSIDE a retained container: pass "
                "neither data= nor external_ref= (the container holds the payload)"
            )
        if not sha256:
            raise ValueError("placement_only= requires the CONTAINER's sha256= for identity")
        if page_index is None or artefact_index is None:
            raise ValueError(
                "placement_only= requires page_index= and artefact_index= — without them "
                "it is indistinguishable from the container row and would collide with it"
            )
        byte_size = int(byte_size or 0)
    elif (data is None) == (external_ref is None):
        raise ValueError("retain_artefact requires exactly one of data= or external_ref=")
    if not user_id:
        raise ValueError("retain_artefact requires a user_id")
    if not media_type:
        raise ValueError("retain_artefact requires a media_type")

    if data is not None:
        cap = max_artefact_bytes()
        if len(data) > cap:
            raise ArtefactTooLarge(
                f"artefact is {len(data)} bytes, cap is {cap} "
                f"(filename={filename!r}) — refused whole, never truncated"
            )
        byte_size = len(data)
        sha256 = sha256 or sha256_hex(data)
    else:
        if byte_size is None or not sha256:
            raise ValueError("external_ref= requires byte_size= and sha256= for the payload")

    x0 = top = x1 = bottom = None
    if placement is not None:
        x0, top, x1, bottom = (float(v) for v in placement)
    pw = ph = None
    if page_size is not None:
        pw, ph = (float(v) for v in page_size)

    cur.execute(
        """
        INSERT INTO artefacts (
            user_id, document_id, filename, media_type, byte_size, sha256,
            page_index, artefact_index, bytes, external_ref, source_ref,
            fact_provenance, fact_class,
            page_width, page_height, bbox_x0, bbox_top, bbox_x1, bbox_bottom
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (user_id, sha256, COALESCE(page_index, -1), COALESCE(artefact_index, -1))
        DO UPDATE SET
            -- Re-upload: keep the ORIGINAL capture (the first time the user gave it to us is
            -- the memory), refresh only the attribution that can legitimately improve.
            document_id  = COALESCE(EXCLUDED.document_id, artefacts.document_id),
            filename     = COALESCE(artefacts.filename, EXCLUDED.filename),
            source_ref   = COALESCE(artefacts.source_ref, EXCLUDED.source_ref)
        RETURNING id, captured_at, (xmax = 0) AS inserted
        """,
        (
            user_id, document_id, filename, media_type, byte_size, sha256,
            page_index, artefact_index,
            (memoryview(data) if data is not None else None), external_ref, source_ref,
            IDENTITY_PROVENANCE, IDENTITY_CLASS,
            pw, ph, x0, top, x1, bottom,
        ),
    )
    row = cur.fetchone()
    return {
        "artefact_id": row[0],
        "captured_at": row[1],
        "duplicate": not bool(row[2]),
        "sha256": sha256,
        "byte_size": byte_size,
        "fact_provenance": IDENTITY_PROVENANCE,
        "fact_class": IDENTITY_CLASS,
    }


# ── WRITE: the A.5 association (Class B, or an honest decline) ───────────────────────────

def bind_caption(cur, *, user_id: str, artefact_id: int,
                 binding: CaptionBinding) -> dict[str, Any] | None:
    """Persist ONE A.5 verdict. A DECLINE IS RECORDED, not dropped.

    Recording the decline (with its reason) is what stops the next pass re-deciding blind
    and what lets the product say "I kept this figure but could not tell which text
    describes it" instead of asserting a neighbour's caption. Refuses any class other than
    B, loudly — the database CHECK is the backstop, this is the first line.
    """
    if not caption_binding_enabled():
        return None
    if binding.bound and binding.fact_class != CAPTION_CLASS:
        raise ValueError(
            f"caption association must be Class {CAPTION_CLASS} "
            f"(got {binding.fact_class!r}) — Class C would put a 30-day expiry on it and "
            f"Class A would assert an inference as user truth"
        )
    if binding.bound and not (binding.caption_text or "").strip():
        raise ValueError("a bound caption must carry text")

    cur.execute(
        """
        UPDATE artefacts
           SET caption_text            = %s,
               caption_provenance      = %s,
               caption_fact_class      = %s,
               caption_confidence      = %s,
               caption_method          = %s,
               caption_declined_reason = %s,
               caption_bound_at        = now()
         WHERE id = %s AND user_id = %s
        RETURNING id, caption_text, caption_fact_class, caption_confidence,
                  caption_declined_reason
        """,
        (
            binding.caption_text if binding.bound else None,
            CAPTION_PROVENANCE if binding.bound else None,
            CAPTION_CLASS if binding.bound else None,
            binding.confidence if binding.bound else None,
            binding.method if binding.bound else None,
            None if binding.bound else binding.declined_reason,
            artefact_id, user_id,
        ),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(cur, row)


# ── WRITE: the CONTEXT TIE (migration 208) ───────────────────────────────────────────────

def bind_chunk(cur, *, user_id: str, artefact_id: int, document_id: int | None,
               chunk_index: int | None, method: str | None) -> dict[str, Any] | None:
    """Tie ONE artefact to the ``documents.chunks`` position its surroundings landed in.

    THIS IS THE HALF THAT MAKES A RETAINED IMAGE A MEMORY. ``document_id`` + ``page_index``
    say which file and which page; neither says what was being SAID around it. The mined
    prose lives in ``documents.chunks`` (a JSONB array) and facts are extracted per CHUNK,
    so the chunk INDEX is the unit that carries "the surrounding text". With it, a walk
    that reaches the paragraph can reach the figure printed beside it.

    ``method`` MUST be one of :data:`CHUNK_BIND_METHODS` — 'caption' (the A.5-bound caption
    was located verbatim, precise) or 'page_lead' (A.5 declined, so the page's leading line
    anchors it, coarse but honest). Passing ``chunk_index=None`` records an HONEST MISS: the
    artefact stays retained and unbound rather than being filed against a paragraph we only
    guessed at. A wrong tie is the same class of harm as a neighbour's caption — worse than
    none.

    THE HARD LINE: this binds a MEMORY to a MEMORY (the artefact to the user's own text). It
    creates no L4 node and files nothing into the class hierarchy.
    """
    if not caption_binding_enabled():
        return None
    if chunk_index is not None and method not in CHUNK_BIND_METHODS:
        raise ValueError(
            f"chunk bind method must be one of {CHUNK_BIND_METHODS} (got {method!r}) — "
            f"an unexplained tie is refused here and by the database CHECK"
        )
    if chunk_index is None and method is not None:
        raise ValueError("a bind method without a chunk_index describes nothing")

    cur.execute(
        """
        UPDATE artefacts
           SET document_id       = COALESCE(%s, document_id),
               chunk_index       = %s,
               chunk_bind_method = %s
         WHERE id = %s AND user_id = %s
        RETURNING id, document_id, chunk_index, chunk_bind_method
        """,
        (document_id, chunk_index, method, artefact_id, user_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(cur, row)


# ── READ: the half that makes retention worth anything ──────────────────────────────────

def fetch_artefacts(cur, *, user_id: str, document_id: int | None = None,
                    sha256: str | None = None, artefact_id: int | None = None,
                    captioned_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    """Metadata read, in page order. NEVER returns ``bytes`` (see ``_META_COLUMNS``).

    This is the surface a recall/console lane calls — the same shape the ``documents``
    registry is read with today (a direct per-tenant table read, not a graph walk), so it
    introduces no new retrieval mechanism.
    """
    where = ["user_id = %s"]
    params: list[Any] = [user_id]
    if document_id is not None:
        where.append("document_id = %s")
        params.append(document_id)
    if sha256:
        where.append("sha256 = %s")
        params.append(sha256)
    if artefact_id is not None:
        where.append("id = %s")
        params.append(artefact_id)
    if captioned_only:
        where.append("caption_text IS NOT NULL")
    params.append(int(limit))
    cur.execute(
        f"SELECT {', '.join(_META_COLUMNS)} FROM artefacts "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY document_id NULLS LAST, page_index NULLS FIRST, artefact_index NULLS FIRST, id "
        f"LIMIT %s",
        params,
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def fetch_artefacts_for_chunk(cur, *, user_id: str, document_id: int,
                              chunk_index: int, limit: int = 25) -> list[dict[str, Any]]:
    """THE READ THE CONTEXT TIE EXISTS FOR: what came in beside THIS chunk of THIS document.

    This is the recall-side entry point. A query resolves to some prose; that prose came
    from a chunk; this answers "and there was a figure here, described thus". Metadata
    only — a recall surface must never haul a 4 MB payload to render one line.

    Ordered by page/artefact so a multi-figure chunk renders in the order it was printed.
    """
    cur.execute(
        f"SELECT {', '.join(_META_COLUMNS)} FROM artefacts "
        f" WHERE user_id = %s AND document_id = %s AND chunk_index = %s "
        f" ORDER BY page_index NULLS FIRST, artefact_index NULLS FIRST, id "
        f" LIMIT %s",
        (user_id, document_id, int(chunk_index), int(limit)),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def describe_artefact_for_recall(row: dict[str, Any]) -> str | None:
    """One honest, human line for a retained artefact. Confidence is spoken, never labelled.

    Mirrors the recall voice rule the MCP already follows: an ENGINE INFERENCE (the caption
    binding is Class B) is stated tentatively, and an artefact whose caption A.5 DECLINED is
    reported as present-but-undescribed rather than dressed in a neighbour's words. Returns
    None for a row with nothing worth saying.
    """
    where = row.get("filename") or row.get("source_ref") or "an uploaded file"
    page = row.get("page_index")
    page_txt = f" on page {int(page) + 1}" if page is not None else ""
    caption = (row.get("caption_text") or "").strip()
    if caption:
        return (f"There is an image{page_txt} in {where}, which appears to be described as: "
                f"\"{caption}\".")
    if row.get("caption_declined_reason"):
        return (f"There is an image{page_txt} in {where}; I kept it but could not tell "
                f"which text describes it.")
    if row.get("media_type"):
        return f"There is a retained file, {where}."
    return None


def load_artefact_payload(cur, *, user_id: str, artefact_id: int) -> tuple[str, Any] | None:
    """THE single accessor that resolves ``bytes`` XOR ``external_ref``.

    Every reader goes through here, which is precisely what makes the object-storage flip
    config rather than a migration: the day a tenant's payloads move, only this function
    learns about it. Returns ``("inline", bytes)`` / ``("external", ref)`` / ``("absent",
    None)``, or ``None`` when the artefact does not belong to this user.
    """
    cur.execute(
        "SELECT bytes, external_ref FROM artefacts WHERE id = %s AND user_id = %s",
        (artefact_id, user_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    payload, ref = row
    if payload is not None:
        return ("inline", bytes(payload))
    if ref:
        return ("external", ref)
    return ("absent", None)


def pending_association(cur, *, user_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Artefacts A.5 has neither bound nor declined yet — the backfill work queue."""
    cur.execute(
        "SELECT id, document_id, page_index, artefact_index, page_width, page_height, "
        "       bbox_x0, bbox_top, bbox_x1, bbox_bottom "
        "  FROM artefacts "
        " WHERE user_id = %s AND caption_text IS NULL AND caption_declined_reason IS NULL "
        " ORDER BY id LIMIT %s",
        (user_id, int(limit)),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── The upload event as GRAPH memory (Class A) ───────────────────────────────────────────

def _type_node_from_media_type(media_type: str) -> str | None:
    """Derive the L4 TYPE node from the artefact's own media type — data, not vocabulary.

    ``application/pdf`` -> ``pdf``; ``image/png`` -> ``png``. The IANA subtype ARRIVED WITH
    THE FILE; it is machine metadata, the same species as the atomic detector's datatype
    labels (``ipv4``/``mac``/``email``). No word list is consulted and no generic noun is
    invented — if the growth engine later wants ``pdf -subclass_of-> document`` it grows it
    per tenant, which is the one place allowed to mint that.
    """
    mt = (media_type or "").split(";")[0].strip().lower()
    if "/" not in mt:
        return None
    subtype = mt.split("/", 1)[1].strip()
    if subtype.startswith("x-"):          # x- experimental prefix (RFC 6648) carries no meaning
        subtype = subtype[2:]
    subtype = subtype.split("+", 1)[0]    # structured suffix: image/svg+xml -> svg, not xml
    return subtype.strip() or None


def upload_event_edges(*, filename: str | None, media_type: str,
                       captured_date: str | None = None,
                       source_ref: str | None = None,
                       self_surface: str = "user") -> list[dict[str, Any]]:
    """Build the UPLOAD-EVENT edges for the graph. Pure — no I/O, no LLM, no cursor.

    "You gave me ``topology.pdf`` on this date" is a genuine memory about an act the user
    performed. WIRED since 2026-08-21 (owner challenge: "what use is a document that
    cannot be surfaced on query?"): the binary door fires these through
    ``POST /ingest`` under ``source="document"`` so the FILE ITSELF — its name, its type,
    the user's ownership — is walkable from recall, not just byte-retained. Under the
    document tier (owner ruling, same day) the router re-stamps provenance to
    ``llm_inferred`` and the class force lands them at staged B — the edge-level
    ``user_stated`` stamp here is the extractor's attestation claim, which the router
    (correctly) owns the final say on, exactly like the spine deriver's stamp.

    Two edges, both on SEEDED rel_types, no invented vocabulary:

    * ``(<filename>, instance_of, <media subtype>)`` — the named artefact instance filed AT
      its type node, SUBJECT-AGNOSTICALLY grounded: the subtype token (png, pdf, jpeg…)
      comes from the MEDIA TYPE the sniffer already read (RFC 6838 structured-suffix
      normalised), never a code list, and the node's type claim is ``Concept`` — admitted
      by the seeded rel's tail constraint — so the ordinary /ingest hierarchy
      enrichment (taxonomy lookup, bidirectional type correction, the ±6/WordNet growth
      ladder) grounds it into per-tenant L4 exactly like any other type node. THE HARD
      LINE: the filename is the artefact's NAME (the naming layer); it is filed AT the
      type node, never AS one. The artefact never becomes a place.
    * ``(<user>, owns, <filename>)`` — reachability from the user anchor, so a walk from
      "me" can reach what I uploaded.
    """
    name = (filename or "").strip()
    if not name:
        return []
    edges: list[dict[str, Any]] = []
    type_node = _type_node_from_media_type(media_type)
    if type_node:
        edges.append({
            "subject": name, "rel_type": "instance_of", "object": type_node,
            "subject_type": "Object", "object_type": "Concept",
            "fact_provenance": IDENTITY_PROVENANCE,
        })
    edges.append({
        "subject": self_surface, "rel_type": "owns", "object": name,
        "subject_type": "Person", "object_type": "Object",
        "fact_provenance": IDENTITY_PROVENANCE,
    })
    if captured_date:
        for e in edges:
            e["event_date"] = captured_date
            e["event_date_granularity"] = "day"
    if source_ref:
        for e in edges:
            e["source_ref"] = source_ref
    return edges


def caption_edges(*, filename: str | None, placements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build graph edges for the geometry-BOUND captions of an artefact's placements.

    Only BOUND placements contribute (the geometry module is decline-biased: an
    unbindable figure records its decline reason, never a neighbour's caption — importing
    THAT would be fabrication). The caption text is the STRING VALUE; ``has_caption`` is a
    growth-ready surface the ontology engine adopts per tenant through the normal
    novel-rel staging lane (a document-lane novel rel stages at B under the 2026-08-21
    tier, never A), never hardcoded validation anywhere. The artefact instance carries no
    type claim here — its ``instance_of`` grounding is the upload edge's job, one fact one
    place.
    """
    name = (filename or "").strip()
    if not name:
        return []
    out: list[dict[str, Any]] = []
    for p in placements or []:
        text = ((p or {}).get("caption_text") or "").strip()
        if not text:
            continue
        out.append({
            "subject": name, "rel_type": "has_caption", "object": text,
            "subject_type": "Object", "fact_provenance": IDENTITY_PROVENANCE,
        })
    return out


def summarize_artefacts(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Counters for an honest per-document report (§6): retained vs associated vs declined."""
    total = bound = declined = 0
    reasons: dict[str, int] = {}
    retained_bytes = 0
    for r in rows:
        total += 1
        retained_bytes += int(r.get("byte_size") or 0)
        if r.get("caption_text"):
            bound += 1
        elif r.get("caption_declined_reason"):
            declined += 1
            reasons[r["caption_declined_reason"]] = reasons.get(r["caption_declined_reason"], 0) + 1
    return {
        "artefacts_retained": total,
        "retained_bytes": retained_bytes,
        "captions_bound": bound,
        "captions_declined": declined,
        "decline_reasons": reasons,
        "unattempted": total - bound - declined,
    }
