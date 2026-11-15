-- Migration 276: remove the migration-060 pattern rows that 184 / 198 corrected and a broken
--                060 re-ran back into existence beside the corrections
-- Date: 2026-09-16 (gauntlet first-boot-migration-corpus)
--
-- THE MECHANISM (measured on a three-boot database, and it is the state of every box that
-- booted more than once after 184 / 198 shipped)
-- ---------------------------------------------------------------------------------------
-- Migration 060's LAST statement (a rel_types INSERT with `source = 'bootstrap'`, a value the
-- source CHECK never admitted — SQLSTATE 23514) failed on EVERY boot of EVERY box, so the boot
-- ledger recorded the whole file `failed` and re-ran it on every start (and the pre-ledger
-- entrypoint re-ran every file on every start anyway). Each re-run re-executed 060's
-- `INSERT INTO extraction_patterns ... ON CONFLICT (pattern_regex, rel_type) DO NOTHING`.
-- That DO NOTHING keys on the REGEX TEXT — and two later migrations CORRECT rows by UPDATING
-- the regex text in place:
--     184_phone_atomic_pattern_nanp.sql   has_phone  E.164-only  →  E.164 | NANP
--     198_atomic_port_pattern_not_a_clock_time.sql   has_port  `(?:port\s+|:)([1-9]\d{0,4})\b`
--                                                    (matches the clock time "10:30")  →  host-
--                                                    qualified / port-marker form
-- so after the correction the ORIGINAL (regex, rel_type) pair no longer exists, the next boot's
-- 060 re-run inserts it again, and the box carries BOTH rows, both `is_active = true`:
--     has_port | (?:port\s+|:)([1-9]\d{0,4})\b            | bootstrap | t   ← 198's false positive, back
--     has_port | (?:(?<=[Pp]ort )|(?<=[Pp]orts )| ...     | bootstrap | t   ← 198's fix
-- Every consumer reads `WHERE is_active = true`, so the clock-time false positive 198 removed
-- has been live again on every multi-boot box. On a pre-ledger (legacy-sweep) box the loop is
-- worse: 184's UPDATE then hits BOTH has_phone rows, tries to make the old one equal the new
-- one, and fails the UNIQUE (SQLSTATE 23505) on every boot after the first.
-- Tenants provisioned while the duplicate stood copied it (provisioning copies
-- public.extraction_patterns, ON CONFLICT DO NOTHING), so the fan-out below is required.
--
-- THE FIX HAS THREE PARTS; THIS FILE IS THE THIRD
-- ------------------------------------------------
-- (1) 060 no longer carries the dead rel_types INSERT, so it applies and stops re-running;
-- (2) 275 seeds `has_port`, the one rel of that INSERT nothing else seeds;
-- (3) THIS FILE removes the resurrected originals — exactly the rows 184 / 198 had already
--     rewritten — wherever the corrected row exists beside them. A row is targeted ONLY by the
--     exact original regex text AND rel_type AND category AND `source = 'bootstrap'` (060's
--     stamp) AND the presence of the corrected sibling: nothing user-grown, nothing engine-grown,
--     nothing on a box where the correction never applied, is touched.
--
-- WHY DELETE AND NOT `is_active = false`: 184's UPDATE is unguarded (`WHERE category =
-- 'scalar_atomic' AND rel_type = 'has_phone'`), so a retired-but-present original would still
-- collide with the corrected row on any box where 184 re-runs (every legacy-sweep box) and 184
-- would keep failing forever. The UNIQUE key is the regex text; the original must not exist.
-- Match telemetry is preserved: `extraction_pattern_matches` rows on the original are re-pointed
-- at the corrected row BEFORE the delete (the FK is ON DELETE CASCADE — a bare DELETE would drop
-- them). Idempotent: on a box with only the corrected rows every statement matches zero rows.

-- ── 1. public (the seed template) ─────────────────────────────────────────────
DO $$
DECLARE
    _pairs CONSTANT TEXT[][] := ARRAY[
        ['has_phone', '\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}\b'],
        ['has_port',  '(?:port\s+|:)([1-9]\d{0,4})\b']
    ];
    _rel   TEXT;
    _old   TEXT;
    _old_id INT;
    _new_id INT;
    _moved INT;
    _i     INT;
BEGIN
    FOR _i IN 1 .. array_length(_pairs, 1) LOOP
        _rel := _pairs[_i][1];
        _old := _pairs[_i][2];
        SELECT id INTO _old_id FROM public.extraction_patterns
         WHERE rel_type = _rel AND category = 'scalar_atomic' AND source = 'bootstrap'
           AND pattern_regex = _old;
        SELECT id INTO _new_id FROM public.extraction_patterns
         WHERE rel_type = _rel AND category = 'scalar_atomic' AND source = 'bootstrap'
           AND pattern_regex <> _old
         ORDER BY id LIMIT 1;
        IF _old_id IS NOT NULL AND _new_id IS NOT NULL THEN
            UPDATE public.extraction_pattern_matches SET pattern_id = _new_id WHERE pattern_id = _old_id;
            GET DIAGNOSTICS _moved = ROW_COUNT;
            DELETE FROM public.extraction_patterns WHERE id = _old_id;
            RAISE NOTICE 'Migration 276: public.% original (id %) removed beside corrected id %; % match row(s) re-pointed',
                _rel, _old_id, _new_id, _moved;
        END IF;
    END LOOP;
END $$;

-- ── 2. every already-provisioned tenant schema ─────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
    _pairs CONSTANT TEXT[][] := ARRAY[
        ['has_phone', '\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}\b'],
        ['has_port',  '(?:port\s+|:)([1-9]\d{0,4})\b']
    ];
    _rel   TEXT;
    _old   TEXT;
    _old_id INT;
    _new_id INT;
    _moved INT;
    _i     INT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'extraction_patterns'
        ) THEN
            CONTINUE;
        END IF;
        FOR _i IN 1 .. array_length(_pairs, 1) LOOP
            _rel := _pairs[_i][1];
            _old := _pairs[_i][2];
            _old_id := NULL; _new_id := NULL; _moved := 0;
            EXECUTE format(
                'SELECT id FROM %I.extraction_patterns WHERE rel_type = $1 AND category = ''scalar_atomic'' '
                'AND source = ''bootstrap'' AND pattern_regex = $2', _schema)
               INTO _old_id USING _rel, _old;
            EXECUTE format(
                'SELECT id FROM %I.extraction_patterns WHERE rel_type = $1 AND category = ''scalar_atomic'' '
                'AND source = ''bootstrap'' AND pattern_regex <> $2 ORDER BY id LIMIT 1', _schema)
               INTO _new_id USING _rel, _old;
            IF _old_id IS NOT NULL AND _new_id IS NOT NULL THEN
                IF EXISTS (SELECT 1 FROM information_schema.tables
                           WHERE table_schema = _schema AND table_name = 'extraction_pattern_matches') THEN
                    EXECUTE format('UPDATE %I.extraction_pattern_matches SET pattern_id = $1 WHERE pattern_id = $2', _schema)
                       USING _new_id, _old_id;
                    GET DIAGNOSTICS _moved = ROW_COUNT;
                END IF;
                EXECUTE format('DELETE FROM %I.extraction_patterns WHERE id = $1', _schema) USING _old_id;
                RAISE NOTICE 'Migration 276: %.% original (id %) removed beside corrected id %; % match row(s) re-pointed',
                    _schema, _rel, _old_id, _new_id, _moved;
            END IF;
        END LOOP;
    END LOOP;
END $$;
