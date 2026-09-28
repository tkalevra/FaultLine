-- Migration 203: artefacts — per-tenant ARTEFACT RETENTION (stage A) + caption binding (stage A.5)
-- Date: 2026-07-31
-- Spec: the internal design record  (§2.4 schema, §3 A.5, §4 provenance/tier)
--
-- WHY THIS TABLE EXISTS
-- ---------------------
-- The document lane already ratified "typed content becomes triples; UNTYPEABLE content is
-- RETAINED RAW rather than discarded" — `episodic_log` does it for a turn, `documents.chunks`
-- JSONB does it for a document ("the per-document verbatim safety net", migration 183). An
-- image is simply another untypeable artefact. This table is the same safety net for bytes:
-- retain now, understand later. Every later understanding pass (OCR, vision, a better
-- extractor in 2027) is then a pure ADDITION over a retained artefact, never a re-upload and
-- never a one-shot lossy transform of bytes we threw away.
--
-- WHY `bytea` AND NOT `pg_largeobject` (structural, not a preference)
-- ------------------------------------------------------------------
-- `pg_largeobject` is a per-DATABASE system catalog addressed by OID, NOT by schema-qualified
-- name. This system's entire tenant boundary is `SET search_path TO faultline_<slug>` WITHOUT
-- `public`, which gives a large object ZERO isolation. Worse on teardown: the PostgreSQL `lo`
-- docs are explicit that dropping a table ORPHANS its large objects and `lo_manage()` does not
-- fire on DROP — so `DROP SCHEMA CASCADE` (the advertised clean offboard) would leak a
-- deprovisioned seat's document bytes into a database-wide store, recoverable only by a
-- separately-scheduled `vacuumlo`. That is a GDPR Art. 17 erasure hole with a cron job as its
-- only guardrail. `bytea` lives in the tenant's own table: one boundary, one erasure surface,
-- one backup policy — all of them the ones this architecture already has.
--
-- `bytes` is set STORAGE EXTERNAL: TOAST's default EXTENDED tries compression first, and a
-- PDF/JPEG/PNG is already entropy-coded, so that pass burns CPU for ~nothing.
--
-- `external_ref` SHIPS ON DAY ONE and is deliberately unused. It is the pre-built flip point:
-- when a tenant's retained bytes outgrow in-database storage, moving to object storage becomes
-- a background copy plus a flag — CONFIG, NOT A MIGRATION. Exactly one of (bytes, external_ref)
-- is populated; every reader goes through one accessor (src/ingest/artefacts.load_artefact_payload).
--
-- PROVENANCE SPLITS THREE WAYS (§4) AND THE SPLIT IS ENFORCED HERE
-- ---------------------------------------------------------------
--   1. "the user handed me a file named X, N bytes, on DATE"  → the user's own ACT, nothing
--      inferred → fact_provenance='user_stated', fact_class='A'. Real Class-A memory BEFORE a
--      single pixel is read.
--   2. "this text block sits next to this image, so it probably describes it" → the ENGINE's
--      geometry, a guess about intent → caption_fact_class='B'.
--   3. "these pixels say <name>" → stage B, DEFERRED. Columns reserved, nothing writes them.
--
-- The `artefacts_caption_class_b` CHECK is load-bearing, not decoration: it makes the database
-- itself REFUSE a Class-C caption. Class C carries `expires_at = now() + 30 days` and
-- `expire_staged_facts` decays `WHERE fact_class='C'` — routing artefact-derived content to C
-- would put a 30-DAY FUSE on a 200-page corpus. C is for content the engine could not TYPE; a
-- caption is typed fine, the engine is merely less sure it belongs to that image, and that is a
-- CONFIDENCE question (caption_confidence), not a classification failure. Same shape as the
-- feelings ruling: never force a tier from the content's kind.
--
-- IDENTITY / DEDUP
-- ----------------
-- UNIQUE on (user_id, sha256, page_index, artefact_index) via a COALESCE expression index.
-- The plain UNIQUE constraint in the design sketch would NOT have worked: NULLs are DISTINCT in
-- a unique constraint, and `page_index IS NULL` is the common case ("the whole file"), so
-- re-uploading the same 200-page PDF would have double-stored silently. COALESCE(-1) makes the
-- dedup actually fire, and it works on every supported PostgreSQL (NULLS NOT DISTINCT is 15+).
--
-- PER-TENANT: the runtime binds `SET search_path TO {schema}` WITHOUT public, so this table MUST
-- exist INSIDE every tenant schema. NO public seed — artefacts are inherently user-specific,
-- exactly like `documents`. NEW tenants get it from the template
-- (src/provisioning/templates/user_schema.sql — updated in the SAME change as this migration;
-- a migration-only change silently skips every newly-provisioned tenant).
--
-- Idempotent + additive ONLY: CREATE TABLE / CREATE INDEX IF NOT EXISTS, no DROP, no UPDATE.

-- ⚠️ FANOUT GUARD (see migration 200's note and migration 062's fix — MEASURED, not cosmetic).
-- A fanout that iterates `public.user_provisioning` and TRUSTS the row aborts the WHOLE DO block
-- on the first registry row whose schema no longer exists ("schema ... does not exist"), because
-- the block is one transaction — so NO schema gets the table, and the failure surfaces only at
-- the end, after some schemas appear to have succeeded. A registry row outlives its schema
-- routinely (wiped tenant, failed provision, restored dump); this database has 11 such rows.
-- This loop is driven by the CATALOG, so a stale registry row is STRUCTURALLY unable to abort it
-- — strictly stronger than migration 200's registry-JOIN-catalog guard, and it also reaches
-- tenant schemas that predate / are missing from the registry (the same choice migrations 183
-- and 195 made for `documents`, whose rows this table references).

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT s.schema_name
        FROM   information_schema.schemata s
        WHERE  s.schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format($t$
            CREATE TABLE IF NOT EXISTS %I.artefacts (
                id               BIGSERIAL   PRIMARY KEY,
                user_id          TEXT        NOT NULL,
                -- the `documents` row this artefact arrived with, when it arrived with one.
                -- Deliberately NOT a FK: a retained artefact must survive its registry row.
                document_id      BIGINT,

                -- ── IDENTITY of the thing the user handed us (the UPLOAD EVENT) ──────────
                filename         TEXT,
                media_type       TEXT        NOT NULL,
                byte_size        BIGINT      NOT NULL,
                sha256           TEXT        NOT NULL,
                page_index       INTEGER,          -- NULL = the whole file
                artefact_index   INTEGER,          -- NULL = the whole file; else ordinal on the page

                -- ── STORAGE: exactly one of these. The pre-built object-storage flip point ──
                bytes            BYTEA,
                external_ref     TEXT,

                -- ── CAPTURE PROVENANCE: grounded, not inferred (§4 claim 1) ──────────────
                source_ref       TEXT,
                fact_provenance  TEXT        NOT NULL DEFAULT 'user_stated',
                fact_class       TEXT        NOT NULL DEFAULT 'A',
                captured_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

                -- ── PLACEMENT GEOMETRY: stage A.5 INPUT, read off the file itself ─────────
                -- top-origin (pdfplumber `top`/`bottom`) — ONE convention, recorded so a later
                -- reader cannot mix bottom-origin geometry from another library into it.
                page_width       REAL,
                page_height      REAL,
                bbox_x0          REAL,
                bbox_top         REAL,
                bbox_x1          REAL,
                bbox_bottom      REAL,

                -- ── ASSOCIATION: stage A.5 OUTPUT — engine inference. B, never A, never C ──
                caption_text            TEXT,
                caption_provenance      TEXT,
                caption_fact_class      TEXT,
                caption_confidence      REAL,
                caption_method          TEXT,
                caption_declined_reason TEXT,   -- WHY we refused to bind. A decline is a RESULT.
                caption_bound_at        TIMESTAMPTZ,

                -- ── UNDERSTANDING: stage B, DEFERRED. Reserved; nothing writes these ──────
                text_layer       TEXT,
                ocr_text         TEXT,          -- NEVER conflated with text_layer: different
                ocr_engine       TEXT,          -- provenance, different trust.
                ocr_confidence   REAL,
                understood_at    TIMESTAMPTZ,

                CONSTRAINT artefacts_storage_xor
                    CHECK (num_nonnulls(bytes, external_ref) <= 1),
                CONSTRAINT artefacts_byte_size_nonneg
                    CHECK (byte_size >= 0),
                CONSTRAINT artefacts_identity_provenance
                    CHECK (fact_provenance IN ('user_stated','llm_inferred','llm_learned')),
                CONSTRAINT artefacts_identity_class
                    CHECK (fact_class IN ('A','B')),
                -- THE 30-DAY-FUSE GUARD: a caption may only ever be Class B.
                CONSTRAINT artefacts_caption_class_b
                    CHECK (caption_fact_class IS NULL OR caption_fact_class = 'B'),
                CONSTRAINT artefacts_caption_needs_class
                    CHECK (caption_text IS NULL OR caption_fact_class IS NOT NULL)
            )
        $t$, _schema);

        -- Already-compressed payloads: skip TOAST's pointless compression attempt.
        EXECUTE format(
            'ALTER TABLE %I.artefacts ALTER COLUMN bytes SET STORAGE EXTERNAL', _schema);

        -- Dedup / "have I seen this before" — COALESCE so the NULL page_index case dedups.
        EXECUTE format($t$
            CREATE UNIQUE INDEX IF NOT EXISTS uq_artefacts_identity
                ON %I.artefacts (user_id, sha256,
                                 COALESCE(page_index, -1), COALESCE(artefact_index, -1))
        $t$, _schema);

        -- Read path: "show me what came in with this document, in page order".
        EXECUTE format($t$
            CREATE INDEX IF NOT EXISTS idx_artefacts_document
                ON %I.artefacts (document_id, page_index, artefact_index)
        $t$, _schema);

        -- Work queue: artefacts A.5 has neither bound nor declined yet. Tiny by construction,
        -- so a later backfill pass can find them without a seq scan over retained bytes.
        EXECUTE format($t$
            CREATE INDEX IF NOT EXISTS idx_artefacts_unassociated
                ON %I.artefacts (id)
                WHERE caption_text IS NULL AND caption_declined_reason IS NULL
        $t$, _schema);
    END LOOP;
END $$;
