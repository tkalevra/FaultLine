-- Migration 013 (was 003): staged_facts.confirmation_source + staged_fact_confirmations
--
-- RENAMED 003 → 013 (gauntlet first-boot-migration-corpus, 2026-09-16). WHY: this file
-- ALTERs `staged_facts`, which migration 012_staged_facts.sql creates — nine files LATER in
-- sort order. On every FRESH database the first boot rejected all three DO blocks
-- (SQLSTATE 42P01 undefined_table: relation "staged_facts" does not exist), the ledger recorded
-- the file `failed`, and it only applied on boot #2. A one-boot box (every throwaway, every
-- fresh install until its second restart) therefore carried no `confirmation_source` column and
-- no `staged_fact_confirmations` table. Sorting it after 012 lets it apply on the first pass.
--
-- LEDGER SEMANTICS OF THE RENAME: the ledger keys on the file basename, so `013_confirmation_
-- source` is a NEW migration id that runs ONCE on every already-migrated box. Every statement
-- below is guarded (IF NOT EXISTS / CREATE ... IF NOT EXISTS), so that single run is a no-op
-- where 003 already landed.
--
-- THE TWO PL/pgSQL FUNCTIONS 003 CARRIED ARE NOT HERE. `promote_staged_fact` and
-- `record_confirmation` were dead (zero call sites — the re_embedder does direct SQL) and
-- migration 073_provenance_cleanup.sql DROPs both. Re-creating them here would RESURRECT them on
-- every already-migrated box (this file runs after 073 there, once), while a fresh box would
-- still end with them dropped — the two paths would diverge on exactly the objects 073 retired.
-- End state on both paths is identical without them: absent. (017 still CREATE OR REPLACEs
-- `promote_staged_fact` on a fresh path; 073 drops it again — unchanged.)

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'staged_facts' AND column_name = 'confirmation_source'
    ) THEN
        ALTER TABLE staged_facts ADD COLUMN confirmation_source TEXT NOT NULL DEFAULT 'llm_repeat';
        ALTER TABLE staged_facts ADD CONSTRAINT chk_staged_facts_confirmation_source
            CHECK (confirmation_source IN ('user_explicit', 'llm_repeat', 'inference_chain'));
    END IF;
END $$;

DO $$ BEGIN
    CREATE TABLE IF NOT EXISTS staged_fact_confirmations (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        staged_fact_id BIGINT NOT NULL REFERENCES staged_facts(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        confirmation_source TEXT NOT NULL CHECK (confirmation_source IN ('user_explicit', 'llm_repeat', 'inference_chain')),
        confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (staged_fact_id, session_id)
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE INDEX IF NOT EXISTS idx_staged_fact_confirmations_staged_fact_id
    ON staged_fact_confirmations(staged_fact_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
