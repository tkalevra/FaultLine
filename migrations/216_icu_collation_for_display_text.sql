-- Migration 216: ICU collation for human-readable NAME text — fix linguistic sort order.
-- Date: 2026-08-05
--
-- THE DEFECT (measured on production, not theorised)
-- ---------------------------------------------------
-- Production `faultline-postgres` runs `postgres:16-alpine`. The database DECLARES
-- `datcollate = en_US.utf8` with `datlocprovider = c` (libc) — but the image is musl, musl
-- ships no locale definitions, and PostgreSQL therefore silently degrades that collation to
-- BYTE ordering. The DB reports a locale it is not honouring, which is exactly why this has
-- been invisible.
--
-- Measured on a `postgres:16-alpine` reproducing the production initdb byte-for-byte
-- (`datcollate=en_US.utf8`, `datlocprovider=c`):
--     actual:   avion < ecole < ile < zebra < école < île        <- accents sort AFTER z
--     correct:  avion < ecole < école < ile < île < zebra
--
-- THIS IS AN ENGLISH DEFECT, NOT A FRENCH ONE. `résumé`, `naïve`, `café`, `Zoë` and every
-- accented user-entered name sorts wrong today. ICU and C disagree on 92 of 351 sampled word
-- pairs. English orthography contains accented forms, and personal names — the overwhelming
-- case in a memory system that stores what people are called — carry them constantly. The
-- Unicode Collation Algorithm (UTS #10) treats a diacritic as a SECONDARY difference: `école`
-- belongs beside `ecole`, never after `zebra`. Byte order is not a weaker sort, it is a
-- different and wrong one.
--
-- WHY COLUMN-LEVEL COLLATE AND NOT A NEW DATABASE
-- ------------------------------------------------
-- Database-level collation is immutable after `initdb` — changing it means a dump/restore of
-- production. It is NOT the only granularity available. PostgreSQL supports collation at the
-- COLUMN level, and a column-level collation is what an unqualified `ORDER BY` resolves
-- against. That is the whole reason this fix is a migration and not a code change:
--
--     CONFIRMED BY EXECUTION — after this migration a PLAIN, UNMODIFIED `ORDER BY alias`
--     (no `COLLATE` clause added to any query) returns linguistic order. All 13 alias sort
--     sites are fixed WITHOUT being edited. There is not one line of application change.
--
-- Also confirmed by execution: `ALTER TABLE ... ALTER COLUMN ... TYPE text COLLATE` on a
-- same-type column does NOT rewrite the heap (relfilenode unchanged before/after). It
-- rebuilds only the indexes that depend on the column, under an ACCESS EXCLUSIVE lock held
-- for that rebuild. For `alias` that is exactly ONE index (the `UNIQUE (entity_id, alias)`
-- constraint); for `display_form` it is ZERO — no index references it.
--
-- THE SAFETY PROPERTY — DETERMINISTIC, AND WHY THAT IS LOAD-BEARING
-- ------------------------------------------------------------------
-- `en-US-x-icu` is a DETERMINISTIC collation (`pg_collation.collisdeterministic = true`,
-- verified in the running image). Under a deterministic collation PostgreSQL falls back to
-- bytewise comparison for EQUALITY, so such a collation changes ORDERING ONLY. It does not
-- change `=`, and it does not change `lower()`.
--
-- That distinction is not academic here. This codebase:
--   * dedups on lowercased values,
--   * derives UUID v5 surrogates from `name.lower().strip()`,
--   * keys `entity_aliases` on `UNIQUE (entity_id, alias)` and uses it as an `ON CONFLICT`
--     target,
--   * enforces `lower(display_form) = alias` at the Python write seam (migration 214).
-- A NON-deterministic collation (`deterministic = false` — the kind that makes `'ete' = 'été'`
-- TRUE) would silently break every one of those: two distinct names would collide on the
-- unique key and dedup would merge separate people. It is deliberately NOT used, and the
-- guard below refuses to run if the target collation ever became non-deterministic.
--
-- Proven by execution against a schema carrying the real constraint, AFTER the ALTER:
--     'ete' = 'été'          -> false        (equality unchanged)
--     'resume' = 'résumé'    -> false        (equality unchanged)
--     lower('RÉSUMÉ')        -> 'résumé'     (case folding unchanged)
--     duplicate (entity_id, alias) INSERT  -> still violates the unique constraint
--     ON CONFLICT (entity_id, alias) DO UPDATE -> still resolves and still fires
--
-- SCOPE — DETERMINED BY MEASUREMENT, DELIBERATELY NARROW
-- ------------------------------------------------------
-- A sweep of every `ORDER BY` in src/ and migrations/ (171 occurrences; 19 sorting on
-- a text column) found the display-text surface is `entity_aliases` and nothing else. Two
-- columns change:
--
--   1. entity_aliases.alias — THE display sort key. 13 sites order by it, all as the
--      tiebreaker after `is_preferred DESC`, and the first row is what gets rendered as the
--      entity's name. Two sites (`main.py` group_name, `embedder.py::_name_of_entity`) are
--      `LIMIT 1`, so the collation single-handedly picks the displayed name; two more
--      (`ORDER BY char_length(alias) DESC/ASC, alias`) use it to break ties between
--      equal-length aliases during longest/shortest-match resolution.
--   2. entity_aliases.display_form — the observed-casing overlay from migration 214, i.e.
--      the column that is actually RENDERED. No query sorts it *today*, so this one is
--      pre-emptive, and it is included for two concrete reasons rather than tidiness: it
--      carries ZERO indexes so the ALTER is free, and leaving the two sibling name columns
--      on divergent collations is a latent trap — the first `ORDER BY display_form` anyone
--      writes (the obvious thing to do now that 214 has shipped) would silently get byte
--      order back. Note the mixed-collation expression already in use is safe: measured,
--      `COALESCE(display_form, alias)` derives the NON-default collation, so that seam
--      returns correct order even before this column is converted — it does not error and
--      it never had an indeterminate-collation problem.
--
-- EXPLICITLY NOT CHANGED, each for a measured reason:
--   * entity_attributes.attribute — sorted in the memory-browse page, but attribute names
--     are lowercase ASCII tokens (`age`, `height`, `has_ip`), not prose.
--   * entity_aliases.entity_id, facts.subject_id/object_id — TEXT holding UUID surrogates.
--     Never displayed; altering them would rebuild primary/foreign key indexes for nothing.
--   * rel_type, entity_type, status, plan, category, taxonomy_name — lowercase enum-like
--     tokens, ASCII by construction, sorted as keys and not as prose.
--
-- BEHAVIOUR CHANGE, STATED HONESTLY: at the four tiebreaker sites above, a tenant whose
-- aliases differ only by accent may now see a DIFFERENT alias win than it did yesterday.
-- That is the point of the fix — the previous winner was chosen by byte order — but it is a
-- visible change, not a silent no-op, and it is worth knowing before it is observed.
--
-- PER-TENANT + TEMPLATE — BOTH, OR IT SILENTLY REGRESSES
-- -------------------------------------------------------
-- The runtime binds `SET search_path TO {schema}` WITHOUT public, so each tenant owns its own
-- copy of this table and the migration must loop every `faultline\_%` schema. It MUST ALSO be
-- applied in src/provisioning/templates/user_schema.sql, or every tenant provisioned AFTER
-- this migration silently misses it — this repo has a documented case of exactly that failure
-- (migration 197). Both halves ship together.
--
-- IDEMPOTENT: each ALTER is guarded on the column's CURRENT collation, so a re-run is a no-op
-- that rebuilds no index. Verified by running the migration twice and diffing.

DO $$
DECLARE
    _schema TEXT;
    _col    TEXT;
    _target CONSTANT TEXT := 'en-US-x-icu';
    _n      INTEGER := 0;
BEGIN
    -- FAIL LOUD, NEVER SILENT. If the target collation is absent the migration must stop,
    -- not quietly leave production on byte ordering while reporting success. ICU is compiled
    -- into the official postgres:16 images (871 ICU collations present in postgres:16-alpine,
    -- verified in the running image); its absence means an unexpected build and is a real
    -- error worth failing on.
    IF NOT EXISTS (SELECT 1 FROM pg_collation WHERE collname = _target) THEN
        RAISE EXCEPTION
            'migration 216: collation % not present in this PostgreSQL build; refusing to '
            'leave human-readable name columns on byte ordering', _target;
    END IF;

    -- The ENTIRE safety argument rests on the target being DETERMINISTIC: that is what keeps
    -- equality bytewise, and therefore what keeps dedup, the UUID-v5 surrogates and every
    -- ON CONFLICT target intact. Refuse rather than corrupt.
    IF NOT (SELECT collisdeterministic FROM pg_collation WHERE collname = _target LIMIT 1) THEN
        RAISE EXCEPTION
            'migration 216: collation % is NON-DETERMINISTIC; that would change equality and '
            'silently break dedup / UUID surrogates / ON CONFLICT. Refusing.', _target;
    END IF;

    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
        ORDER BY schema_name
    LOOP
        -- Skip a schema with no entity_aliases table (partial/aborted provisioning).
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'entity_aliases'
        ) THEN
            CONTINUE;
        END IF;

        FOREACH _col IN ARRAY ARRAY['alias', 'display_form']
        LOOP
            -- Guard on the column's CURRENT collation. This is what makes the migration
            -- idempotent, and it also tolerates `display_form` being absent on a tenant that
            -- has not yet taken migration 214.
            IF EXISTS (
                SELECT 1 FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_collation co ON co.oid = a.attcollation
                WHERE n.nspname = _schema AND c.relname = 'entity_aliases'
                  AND a.attname = _col AND a.attnum > 0 AND NOT a.attisdropped
                  AND coalesce(co.collname, '') <> _target
            ) THEN
                EXECUTE format(
                    'ALTER TABLE %I.entity_aliases ALTER COLUMN %I TYPE text COLLATE %I',
                    _schema, _col, _target);
                _n := _n + 1;
                RAISE NOTICE 'migration 216: %.entity_aliases.% -> %', _schema, _col, _target;
            END IF;
        END LOOP;
    END LOOP;

    RAISE NOTICE 'migration 216: % column(s) converted to %', _n, _target;
END $$;
