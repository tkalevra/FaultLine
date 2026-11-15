-- 328_corpus_document_versioning.sql — DOCUMENTS AS FIRST-CLASS VERSIONED CITIZENS
-- (docs-storage lane, the apache-versions contract).
--
-- THE CONTRACT THIS MIGRATION CARRIES (owner-approved): documents inherit the
-- facts lane's SUPERSESSION discipline — superseded_at + the live-default
-- retrieval. corpus_documents gains a stable FAMILY identity plus a version
-- sequence:
--   document_family TEXT      — stable identity derived from (user_id, the
--                               normalized source_ref-or-filename) via MD5 —
--                               an IDENTITY KEY, not a checksum (content
--                               integrity stays content_sha256), so the SQL
--                               backfill and the application derive IDENTICAL
--                               values with zero extension dependencies
--                               (Postgres md5() is built-in; pgcrypto is not
--                               in the base image). The derivation rule lives
--                               in src/ingest/corpus_store.derive_document_family
--                               and is pinned hermetically; the BACKFILL below
--                               re-derives it for pre-328 rows with the SAME
--                               rule so old rows fold into the same families.
--   version_seq     INTEGER   — 1, 2, 3… per family. Same bytes re-ingested =
--                               sha256 DEDUPE (no new version); different
--                               bytes = a NEW version row and the previous
--                               live row gets superseded_at=now().
--   superseded_at   TIMESTAMPTZ — NULL = live (the DEFAULT in every retrieval
--                               and list), non-NULL = archived but still
--                               byte-readable (the "what did it say in March"
--                               arm).
--
-- LINEAGE (document/page level): corpus_pages carry their doc's lineage
-- implicitly — every live retrieval filters corpus_documents.superseded_at IS
-- NULL, so pages of a superseded version stop serving on live asks WITHOUT a
-- per-page column. The fact-level cascade (uplift-derived staged facts) is
-- FILED as the follow-up, not forced here.
--
-- FLAG: DOC_VERSIONING (default ON; OFF = byte-parity with 327 — no family
-- logic, ingest exactly as today).
--
-- FAN-OUT (the 317/327 pattern): every schema holding corpus_documents (which
-- is exactly the schemas that held `documents` when 327 fanned out), guarded
-- per-schema so one bad schema never skips the rest. Template side
-- (src/provisioning/templates/user_schema.sql) carries the same columns in the
-- SAME commit — the 197/273/298/299/317/327 rule: template + migration, never
-- one without the other. Idempotent: ADD COLUMN IF NOT EXISTS, backfill
-- guarded by column-null probes, CREATE UNIQUE INDEX IF NOT EXISTS. Safe to
-- re-run.
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
                   AND c.relname = 'corpus_documents'
                   AND c.relkind = 'r')
         ORDER BY n.nspname
    LOOP
        BEGIN
            EXECUTE format('ALTER TABLE %I.corpus_documents '
                           'ADD COLUMN IF NOT EXISTS document_family TEXT', _sch);
            EXECUTE format('ALTER TABLE %I.corpus_documents '
                           'ADD COLUMN IF NOT EXISTS version_seq INTEGER', _sch);
            EXECUTE format('ALTER TABLE %I.corpus_documents '
                           'ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ', _sch);
        EXCEPTION
            WHEN OTHERS THEN NULL;
        END;
        BEGIN
            -- BACKFILL (pre-328 rows): derive the family in SQL with the SAME
            -- rule as the application — md5(user_id || chr(0) ||
            -- lower-trimmed source_ref-or-filename, backslash-normalized) —
            -- then number versions by created_at, id within each family,
            -- superseding all but the newest. Idempotent: only touches
            -- NULL-family rows. md5 is BUILT-IN on both sides — no pgcrypto
            -- (the base image does not carry it).
            EXECUTE format($ddl$
                WITH keyed AS (
                    SELECT id,
                           md5(user_id || chr(0) ||
                               replace(lower(btrim(coalesce(
                                   nullif(btrim(source_ref), ''),
                                   nullif(btrim(original_filename), '')))),
                                   '\', '/')) AS fam
                      FROM %I.corpus_documents
                     WHERE document_family IS NULL
                ), numbered AS (
                    SELECT id, fam,
                           row_number() OVER (PARTITION BY fam
                                              ORDER BY created_at, id) AS seq,
                           count(*)  OVER (PARTITION BY fam) AS total
                      FROM keyed
                )
                UPDATE %I.corpus_documents d
                   SET document_family = n.fam,
                       version_seq     = n.seq,
                       superseded_at   = CASE WHEN n.seq < n.total
                                              THEN d.created_at ELSE d.superseded_at END
                  FROM numbered n
                 WHERE d.id = n.id
            $ddl$, _sch);
        EXCEPTION
            WHEN OTHERS THEN NULL;
        END;
        BEGIN
            -- The version-mint guard: one row per (family, version_seq). A
            -- concurrent double-mint collides here instead of silently forking
            -- a family. Non-unique (NULL families from the OFF flag) never
            -- collide — Postgres unique indexes skip NULL keys.
            EXECUTE format(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_corpus_documents_family_version '
                'ON %I.corpus_documents (document_family, version_seq)', _sch);
        EXCEPTION
            WHEN OTHERS THEN NULL;
        END;
        BEGIN
            -- The live-default retrieval path: the passage lane and the list
            -- default both ask "unsuperseded per family" — partial index, the
            -- idx_documents_active discipline.
            EXECUTE format(
                'CREATE INDEX IF NOT EXISTS idx_corpus_documents_live '
                'ON %I.corpus_documents (document_family) '
                'WHERE superseded_at IS NULL', _sch);
        EXCEPTION
            WHEN OTHERS THEN NULL;
        END;
        RAISE NOTICE 'Migration 328: corpus document versioning present in %', _sch;
    END LOOP;
END
$mig$;
