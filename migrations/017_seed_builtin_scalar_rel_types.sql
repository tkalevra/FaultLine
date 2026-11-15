-- Migration 017b (was 016): builtin scalar rel_types — typed head/tail + category + source
--
-- RENAMED 016 → 017_seed_… (gauntlet first-boot-migration-corpus, 2026-09-16). WHY: this
-- INSERT names `head_types` / `tail_types`, columns that do not exist until
-- 017_fix_schema_consistency.sql RENAMES `allowed_head` / `allowed_tail` (005's names) one file
-- LATER in sort order. On every FRESH database the first boot rejected the whole statement
-- (SQLSTATE 42703 undefined_column), the ledger recorded it `failed`, and it only applied on
-- boot #2 — by which time every later file had already run, so its ON CONFLICT DO UPDATE then
-- CLOBBERED the category / head_types / tail_types / source that 024, 030_strengthen, 033 and
-- 101 had refined (the ledger re-runs ONLY the failed file). `017_fix…` < `017_seed…` in bytewise
-- sort, so this now runs right after the rename and before anything that refines these rows —
-- the order every pre-ledger box applied it in.
--
-- LEDGER SEMANTICS OF THE RENAME: a new migration id runs ONCE on every already-migrated box.
-- The DO UPDATE is therefore GUARDED to the skeleton-fill convention (cf. 085 B2b, 091, 110):
-- it types a row only while that row is still UNTYPED (`head_types IS NULL OR '{}'`). On a
-- fresh first boot that is exactly the state 016 always met (height/weight were just typed by
-- 017 with these same values; age/has_gender/born_on/nationality/occupation are untyped from
-- 005/007), so the landed rows are byte-identical to the unguarded original. On a migrated box
-- every row is typed → zero rows touched → nothing a later migration set is overwritten.
INSERT INTO rel_types (rel_type, label, engine_generated, confidence, source, correction_behavior, category, head_types, tail_types)
VALUES
    ('height',      'Height',      false, 1.0, 'builtin', 'supersede', 'physical', ARRAY['Person'], ARRAY['SCALAR']),
    ('weight',      'Weight',      false, 1.0, 'builtin', 'supersede', 'physical', ARRAY['Person'], ARRAY['SCALAR']),
    ('age',         'Age',         false, 1.0, 'builtin', 'supersede', 'physical', ARRAY['Person'], ARRAY['SCALAR']),
    ('has_gender',  'Gender',      false, 1.0, 'builtin', 'supersede', 'physical', ARRAY['Person'], ARRAY['SCALAR']),
    ('born_on',     'Born On',     false, 1.0, 'builtin', 'supersede', 'temporal', ARRAY['Person'], ARRAY['SCALAR']),
    ('nationality', 'Nationality', false, 1.0, 'builtin', 'supersede', 'identity', ARRAY['Person'], ARRAY['SCALAR']),
    ('occupation',  'Occupation',  false, 1.0, 'builtin', 'supersede', 'work',     ARRAY['Person'], ARRAY['SCALAR'])
ON CONFLICT (rel_type) DO UPDATE SET
    category   = EXCLUDED.category,
    head_types = EXCLUDED.head_types,
    tail_types = EXCLUDED.tail_types,
    source     = EXCLUDED.source
WHERE rel_types.head_types IS NULL OR rel_types.head_types = '{}';
