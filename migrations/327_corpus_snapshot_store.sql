-- 327_corpus_snapshot_store.sql — STORE-FIRST DOCUMENT INGEST (wound-map round 4).
--
-- THE WOUND (measured twice live on pre-prod, week of 2026-11-09):
--   document enqueue → per-chunk SYNCHRONOUS brain extraction (180s ReadTimeouts) →
--   /extract/rewrite fallback → UNBOUNDED retries → the stack clogged (59/62GB RSS,
--   provisioning worker DIED); the re-embedder also continuously re-mines document
--   content. The doc lane shares the brain + re-embedder lane with live serving and
--   there was no backpressure anywhere.
--
-- THE FIX THIS TABLE PAIR CARRIES (owner design: "snapshot/compress/store the image;
-- the litmus: document ingest doesn't cause the system to cack, 100% accuracy"):
--   `corpus_documents` — ONE ROW PER SOURCE ARTEFACT: the ORIGINAL BYTES, compressed
--       (zlib, application-side — stdlib, zero new deps; codec stamped in
--       snapshot_codec so a future zstd/pg_compression swap stays honest), the
--       content_sha256 of the RAW bytes (byte-fidelity contract: round-trip the
--       snapshot, hash it, compare), and a status gate.
--   `corpus_pages` — the page-grounded layout text, VERBATIM. Any ref/box-style
--       anchor tokens an OCR engine returns (<｜ref｜>/<｜box｜> per the DeepSeek OCR
--       guide) are part of the ZERO-LOSS contract and are never stripped.
--       finish_reason per page: 'stop' = complete text, 'length' = truncated output
--       (the page is INCOMPLETE and the document must say so), NULL = never attempted
--       (pixel page pending an OCR uplift).
--
-- STATUS GATE (the zero-loss fidelity gate, straight from the OCR guide):
--   ready      ⇔ EVERY page has finish_reason='stop' AND non-empty text.
--   incomplete ⇔ ANY page is 'length' or pending (NULL) — the ingest response says so.
--   pending    ⇔ extraction not yet terminal (uplift running).
--   failed     ⇔ the format could not be read at all (snapshot still retained).
--
-- FAN-OUT (the 317/183 pattern): every schema holding a `documents` table (public
-- never holds one — document rows are inherently user-specific), the pg_namespace
-- pg_class probe, guarded per-schema loop (one bad schema never skips the rest).
-- Template side (src/provisioning/templates/user_schema.sql) carries the same DDL in
-- the SAME commit — the 197/273/298/299/317 rule: template + migration, never one
-- without the other. Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT
-- EXISTS. Safe to re-run.
DO $mig$
DECLARE
    _sch text;
BEGIN
    FOR _sch IN
        SELECT n.nspname
          FROM pg_namespace n
         WHERE EXISTS (
               SELECT 1
                 FROM pg_class c
                WHERE c.relnamespace = n.oid
                  AND c.relname = 'documents'
                  AND c.relkind = 'r')
         ORDER BY n.nspname
    LOOP
        BEGIN
            EXECUTE format($ddl$
                CREATE TABLE IF NOT EXISTS %I.corpus_documents (
                    id                BIGSERIAL   PRIMARY KEY,
                    user_id           TEXT        NOT NULL,
                    source_ref        TEXT,
                    original_filename TEXT,
                    mime              TEXT,
                    byte_size         BIGINT      NOT NULL DEFAULT 0,
                    -- SHA-256 over the RAW (uncompressed) source bytes. The retrieval
                    -- lane proves snapshot fidelity by decompressing raw_snapshot and
                    -- re-hashing: any mismatch is data loss, loudly.
                    content_sha256    TEXT        NOT NULL,
                    -- The compressed ORIGINAL BYTES. Codec stamped beside it so a
                    -- future zstd / pg_compression swap never silently reinterprets
                    -- old rows. zlib level 9, application-side (stdlib; no new deps).
                    raw_snapshot      BYTEA,
                    snapshot_codec    TEXT        NOT NULL DEFAULT 'zlib',
                    page_count        INTEGER     NOT NULL DEFAULT 0,
                    status            TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'ready', 'incomplete', 'failed')),
                    -- Which OCR engine produced the page texts ('none' = direct
                    -- text extraction, no model — the text_recognition shape).
                    ocr_engine       TEXT        NOT NULL DEFAULT 'none',
                    error             TEXT,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            $ddl$, _sch);
        EXCEPTION
            WHEN undefined_table THEN NULL;   -- dropped between probe and CREATE
            WHEN OTHERS THEN NULL;
        END;
        BEGIN
            EXECUTE format($ddl$
                CREATE TABLE IF NOT EXISTS %I.corpus_pages (
                    document_id   BIGINT      NOT NULL
                        REFERENCES %I.corpus_documents(id) ON DELETE CASCADE,
                    page_index    INTEGER     NOT NULL,
                    -- The page's layout-grounded text VERBATIM. Anchor tokens
                    -- (<｜ref｜>/<｜box｜>) are CONTENT, never stripped.
                    layout_text   TEXT        NOT NULL DEFAULT '',
                    -- 'stop' | 'length' | NULL (never attempted — pending OCR).
                    finish_reason TEXT,
                    UNIQUE (document_id, page_index)
                )
            $ddl$, _sch, _sch);
        EXCEPTION
            WHEN undefined_table THEN NULL;
            WHEN OTHERS THEN NULL;
        END;
        BEGIN
            -- The retrieval lane's one access path (the idx_documents_stub_tsv
            -- discipline): GIN over the page text itself, expression-indexed so the
            -- writes need no trigger and the reads need no second pass.
            EXECUTE format(
                'CREATE INDEX IF NOT EXISTS idx_corpus_pages_text_gin ON %I.corpus_pages '
                'USING GIN (to_tsvector(''simple'', layout_text))', _sch);
        EXCEPTION
            WHEN undefined_table THEN NULL;
            WHEN OTHERS THEN NULL;
        END;
        BEGIN
            -- The OCR-uplift cohort scan: incomplete documents with pages still
            -- pending. Partial, like idx_documents_active.
            EXECUTE format(
                'CREATE INDEX IF NOT EXISTS idx_corpus_documents_uplift ON %I.corpus_documents '
                '(created_at) WHERE status = ''incomplete''', _sch);
        EXCEPTION
            WHEN undefined_table THEN NULL;
            WHEN OTHERS THEN NULL;
        END;
        RAISE NOTICE 'Migration 327: corpus snapshot store present in %', _sch;
    END LOOP;
END
$mig$;
