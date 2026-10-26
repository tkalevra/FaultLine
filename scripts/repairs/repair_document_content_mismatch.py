#!/usr/bin/env python3
"""BUG-03 repair (adversarial bench 2026-10-24): rebuild a document's chunks from its
TRUE source when the stored chunks are wrong-content (measured on prod: doc 18 —
"On the Compressibility of Quantized Large Language Models", arxiv 2403.01384 —
holds chunks byte-identical to doc 20, the crayfish sous-vide paper; md5(chunks::text)
936ca8f5242ffae6dac48ef1d7053ecb on BOTH rows).

ROOT CAUSE (verified 2026-10-25, read-only): no code path ever rewrites
``documents.chunks`` after the enqueue INSERT, and the enqueue INSERT is a single
atomic statement — so the wrong bytes entered through the CLIENT-SUPPLIED request
(the bench fetch harness mis-paired critic03/critic03b texts; see
benchmarks/openalex-doc-recall/README.md). The live systemic gap is that
``POST /documents/enqueue`` performs NO content-vs-source validation and no
duplicate-chunk-across-documents detection — a mis-paired fetch is silently stored
under whatever title it arrives with (upstream bug filed alongside this script).

WHAT THIS DOES (idempotent, verify-first, dry-run default):
  1. VERIFY the corruption signature read-only: the target row's chunk hash collides
     with a DIFFERENT document (distinct source_ref) — the mis-pair fingerprint.
  2. RESOLVE the true source, in priority order:
       a. the R1 retained original (artefacts row, media_type text/plain, linked to
          this document id — src/ingest/artefacts.py::load_document_source);
       b. ``--source-file`` (operator-supplied extracted text);
       c. re-fetch the row's ``source_ref`` (only with ``--allow-fetch``), parsed
          through the REAL parser seam (src/ingest/doc_parser.py) so the stamped
          parser_version stays honest.
  3. VALIDATE the source against the row's TITLE before any write (the pin): every
     significant title token must occur in the text. This is the check whose absence
     caused BUG-03 — crayfish bytes FAIL the "compressibility/quantized/large/
     language/models" token set and are refused loudly, never re-stored.
  4. RE-CHUNK with the production chunker (src/mcp/server.py::_chunk_document — the
     same function the enqueue path used; no drift).
  5. WRITE (apply only): optimistic UPDATE guarded on the OLD chunk hash (a concurrent
     fix or drain transition is never clobbered), re-pending the row so the drain
     re-extracts from the corrected text. The corrupt chunks are NOT retained in-place
     (documents.chunks has no history column) — the collision report in step 1 is the
     forensic record, so the dry-run output should be saved alongside this script.
  6. POST-VERIFY: the new chunk hash differs from the old one AND from every other
     document's hash in the tenant.

USAGE (the orchestrator runs this; dry-run is the DEFAULT):
    python3 scripts/repairs/repair_document_content_mismatch.py \
        --user-id <uuid> --document-id 18            # verify + plan (no writes)
    python3 scripts/repairs/repair_document_content_mismatch.py \
        --user-id <uuid> --document-id 18 --allow-fetch --apply

Exit codes: 0 ok · 1 validation refused (WRONG CONTENT — do not force) ·
2 nothing to do (no collision / already repaired) · 3 infra error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

import psycopg2

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _REPO_ROOT)

#: Title-token floor: tokens shorter than this are not significant evidence.
_MIN_TOKEN_LEN = 4

#: English function words that appear in almost every title and prove nothing.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "onto", "over", "under", "based",
    "using", "toward", "towards", "about", "between", "among", "their", "your",
    "study", "analysis", "survey", "review", "research", "approach", "method",
})


def significant_title_tokens(title: str) -> list[str]:
    """Deterministic significant-token set for title/source validation.

    Lowercased, >= ``_MIN_TOKEN_LEN`` alphabetic tokens minus a closed function-word
    class. NOT a domain word zoo — the words come from the row's own title, whatever
    it is (subject-agnostic: the same check protects a crayfish paper from quantized-
    LLM bytes as exactly as the reverse).
    """
    toks = re.findall(r"[a-z]+", (title or "").lower())
    return sorted({t for t in toks if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS})


def source_matches_title(source_text: str, title: str) -> tuple[bool, list[str]]:
    """THE CONTENT PIN: every significant title token must occur in the source text.

    Returns (ok, missing_tokens). A text that is the WRONG PAPER fails on most tokens
    (crayfish text vs the quantization title: 4+ of 5 tokens missing) and is REFUSED —
    the exact check whose absence stored crayfish chunks under doc 18's title.
    """
    sig = significant_title_tokens(title)
    if not sig:
        # A title with no significant tokens cannot be validated — refuse rather than
        # store unverifiable bytes (fail-safe toward not repeating BUG-03).
        return False, ["<no significant title tokens to validate against>"]
    lowered = (source_text or "").lower()
    missing = [t for t in sig if t not in lowered]
    return (not missing), missing


def _fetch_url(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "faultline-repair-doc18/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — operator-run, explicit URL from the row
        return resp.read()


def _chunks_differ(a: list[str], b: list[str]) -> bool:
    """Direct list equality — the identical-bytes guard. (No python-side hashing: the
    SQL side compares ``md5(chunks::text)`` and that stays the DB's job; here exact
    equality of the chunk lists is both stronger and simpler.)"""
    return list(a or []) != list(b or [])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user-id", required=True, help="seat UUID (tenant schema)")
    ap.add_argument("--document-id", type=int, required=True, help="documents.id to repair")
    ap.add_argument("--dsn", default=os.environ.get("POSTGRES_DSN"))
    ap.add_argument("--source-file", help="local extracted-text file (preferred over fetch)")
    ap.add_argument("--allow-fetch", action="store_true",
                    help="permit re-fetching the row's source_ref over HTTP")
    ap.add_argument("--apply", action="store_true",
                    help="WRITE the repair. Default is dry-run (no writes).")
    args = ap.parse_args()

    if not args.dsn:
        print("ERROR: no --dsn and no POSTGRES_DSN env", file=sys.stderr)
        return 3

    from src.mcp.server import _chunk_document, _DOC_MAX_CHUNKS
    from src.provisioning.schema_manager import derive_user_slug_from_uuid

    schema = f"faultline_{derive_user_slug_from_uuid(args.user_id)}"
    print(f"target schema: {schema}  document: {args.document_id}  "
          f"mode: {'APPLY' if args.apply else 'DRY-RUN'}")

    conn = None
    try:
        conn = psycopg2.connect(args.dsn)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")

            # ── 1. verify the corruption signature read-only ──────────────────────
            cur.execute(
                "SELECT id, title, coalesce(source_ref,''), status, chunk_count, "
                "       parser_version, md5(chunks::text), chunks "
                "  FROM documents WHERE id = %s", (args.document_id,))
            row = cur.fetchone()
            if row is None:
                print(f"document {args.document_id} not found in {schema}")
                return 2
            doc_id, title, source_ref, status, chunk_count, parser_version, \
                old_hash, chunks_raw = row
            chunks = chunks_raw if isinstance(chunks_raw, list) else json.loads(chunks_raw or "[]")
            print(f"row: id={doc_id} status={status} chunks={chunk_count} "
                  f"parser={parser_version}\n     title={title!r}\n     source_ref={source_ref!r}")

            cur.execute(
                "SELECT id, title, coalesce(source_ref,'') FROM documents "
                " WHERE md5(chunks::text) = %s AND id <> %s", (old_hash, doc_id))
            collisions = cur.fetchall()
            if not collisions:
                print("\nno cross-document chunk collision for this row — the registered "
                      "BUG-03 signature is ABSENT. Nothing to repair (refusing to rewrite "
                      "a non-colliding row blind).")
                return 2
            for cid, ctitle, cref in collisions:
                print(f"COLLISION: doc {cid} shares chunk hash {old_hash} — {ctitle!r} ({cref})")

            # ── 2. resolve the true source ───────────────────────────────────────
            source_text: str | None = None
            resolved_via = None
            # The R1 probe runs INSIDE the open write transaction and a missing table/
            # row (the expected pre-R1 case) raises — which would ABORT the whole
            # transaction and surface later as "current transaction is aborted" at the
            # UPDATE (the transaction-poisoning trap). SAVEPOINT it: the probe's
            # failure is contained, the repair proceeds.
            cur.execute("SAVEPOINT r1_source_probe")
            try:
                from src.ingest.artefacts import load_document_source
                art = load_document_source(cur, user_id=args.user_id, document_id=doc_id)
                cur.execute("RELEASE SAVEPOINT r1_source_probe")
            except Exception as exc:  # noqa: BLE001 — absent table/row is expected pre-R1
                cur.execute("ROLLBACK TO SAVEPOINT r1_source_probe")
                print(f"(R1 artefact lookup unavailable: {exc!r})")
                art = None
            if art and isinstance(art.get("payload"), (bytes, bytearray)):
                source_text = art["payload"].decode("utf-8", errors="replace")
                resolved_via = f"R1 retained original (artefact id {art['id']})"
            elif args.source_file:
                with open(args.source_file, "rb") as fh:
                    source_text = fh.read().decode("utf-8", errors="replace")
                resolved_via = f"--source-file {args.source_file}"
            elif args.allow_fetch and source_ref:
                print(f"fetching {source_ref} …")
                data = _fetch_url(source_ref)
                if data[:5] == b"%PDF-":
                    from src.ingest.doc_parser import extract_pdf_with_parser
                    parsed, used_parser = extract_pdf_with_parser(data)
                    source_text = parsed.full_text
                    parser_version = used_parser  # honest stamp: what ACTUALLY parsed it
                    print(f"parsed PDF via {used_parser}: {len(source_text)} chars, "
                          f"{parsed.page_count} pages")
                else:
                    source_text = data.decode("utf-8", errors="replace")
                resolved_via = f"re-fetched {source_ref}"
            else:
                print("\nno R1 retained original for this document and no --source-file, "
                      "and --allow-fetch not given — cannot resolve the true source. "
                      "Nothing written.")
                return 3
            print(f"source resolved via: {resolved_via} ({len(source_text)} chars)")

            # ── 3. VALIDATE content vs title — the pin, before any write ─────────
            ok, missing = source_matches_title(source_text, title)
            if not ok:
                print(f"\nVALIDATION REFUSED: source text is NOT the titled paper. "
                      f"Missing title tokens: {missing}. This is the BUG-03 guard — "
                      f"wrong bytes are never stored, even by the repair.")
                return 1
            print(f"validation ok: all significant title tokens present "
                  f"({significant_title_tokens(title)})")

            # ── 4. re-chunk with the production chunker ──────────────────────────
            new_chunks = _chunk_document(source_text)
            if not new_chunks:
                print("VALIDATION REFUSED: source text produced zero chunks")
                return 1
            if len(new_chunks) > _DOC_MAX_CHUNKS:
                print(f"VALIDATION REFUSED: {len(new_chunks)} chunks exceeds the per-row "
                      f"bound {_DOC_MAX_CHUNKS} — repair does not segment; handle manually")
                return 1
            if not _chunks_differ(new_chunks, chunks):
                print("\nre-chunked text is IDENTICAL to the stored (colliding) chunks — "
                      "the 'true source' is the same wrong bytes. Nothing written.")
                return 1
            print(f"re-chunked: {len(new_chunks)} chunks (was {chunk_count})")

            if not args.apply:
                print("\nDRY-RUN: no rows written. Re-run with --apply to write the "
                      "repaired chunks and re-pend the document.")
                return 0

            # ── 5. optimistic guarded write + re-pend for drain re-extraction ────
            with conn.cursor() as wcur:
                wcur.execute(f"SET search_path TO {schema}")
                wcur.execute(
                    """
                    UPDATE documents
                       SET chunks = %s::jsonb, chunk_count = %s, total_chunks = %s,
                           parser_version = %s,
                           status = 'pending', chunk_state = '{}'::jsonb,
                           attempts = 0, error = NULL,
                           started_at = NULL, ready_at = NULL, brain_deferred_since = NULL
                     WHERE id = %s AND md5(chunks::text) = %s
                    RETURNING id
                    """,
                    (json.dumps(new_chunks), len(new_chunks), len(new_chunks),
                     parser_version, doc_id, old_hash))
                fixed = wcur.fetchone()
                if fixed is None:
                    conn.rollback()
                    print("optimistic guard fired: the row's chunk hash changed under us "
                          "(concurrent repair or drain transition) — nothing written")
                    return 2
            conn.commit()
            print(f"APPLIED: document {doc_id} chunks rebuilt from {resolved_via}, "
                  f"status re-pended for drain re-extraction")

            # ── 6. post-verify ───────────────────────────────────────────────────
            with conn.cursor() as vcur:
                vcur.execute(f"SET search_path TO {schema}")
                vcur.execute("SELECT md5(chunks::text) FROM documents WHERE id = %s",
                             (doc_id,))
                now_hash = vcur.fetchone()[0]
                vcur.execute(
                    "SELECT count(*) FROM documents "
                    " WHERE md5(chunks::text) = %s AND id <> %s", (now_hash, doc_id))
                remaining = vcur.fetchone()[0]
            print(f"post-verify: new sql-side chunk hash {now_hash}; "
                  f"cross-document collisions remaining: {remaining} "
                  f"({'OK' if remaining == 0 else 'UNEXPECTED — investigate'})")
            return 0
    except Exception as exc:  # noqa: BLE001 — operator script: report, never spam
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        print(f"ERROR: {exc!r}", file=sys.stderr)
        return 3
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
