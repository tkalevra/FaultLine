-- Migration 003: confirmation_source + promotion logic

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

CREATE OR REPLACE FUNCTION promote_staged_fact(p_staged_fact_id BIGINT)
RETURNS VOID AS $$
DECLARE
    v_fact RECORD;
    v_confirmation_source TEXT;
    v_distinct_session_count INTEGER;
BEGIN
    -- Get the staged fact and check its state
    SELECT * INTO v_fact FROM staged_facts WHERE id = p_staged_fact_id;

    IF v_fact IS NULL THEN
        RAISE NOTICE 'Staged fact % not found', p_staged_fact_id;
        RETURN;
    END IF;

    -- Idempotent: if already promoted, return immediately
    IF v_fact.promoted_at IS NOT NULL THEN
        RETURN;
    END IF;

    v_confirmation_source := v_fact.confirmation_source;

    -- Apply promotion rules based on confirmation_source
    IF v_confirmation_source = 'inference_chain' THEN
        RAISE NOTICE 'inference_chain facts never promoted: staged_fact_id %', p_staged_fact_id;
        RETURN;
    ELSIF v_confirmation_source = 'user_explicit' THEN
        IF v_fact.confirmed_count < 1 THEN
            RETURN;
        END IF;
    ELSIF v_confirmation_source = 'llm_repeat' THEN
        SELECT COUNT(DISTINCT session_id) INTO v_distinct_session_count
        FROM staged_fact_confirmations
        WHERE staged_fact_id = p_staged_fact_id;

        IF COALESCE(v_distinct_session_count, 0) < 5 THEN
            RETURN;
        END IF;
    END IF;

    -- Perform the promotion: insert into facts
    INSERT INTO facts (user_id, subject_id, object_id, rel_type, provenance,
                       fact_provenance, fact_class, confidence, confirmed_count,
                       valid_from, last_seen_at, recorded_at, qdrant_synced)
    SELECT v_fact.user_id::text, v_fact.subject_id::text, v_fact.object_id::text,
           v_fact.rel_type, v_fact.provenance, 'llm_promoted', v_fact.fact_class,
           v_fact.confidence, v_fact.confirmed_count, now(), now(), now(), false
    ON CONFLICT (user_id, subject_id, object_id, rel_type) DO UPDATE
    SET confirmed_count = facts.confirmed_count + 1,
        last_seen_at    = now(),
        qdrant_synced   = false;

    -- Mark the staged fact as promoted
    UPDATE staged_facts SET promoted_at = now() WHERE id = p_staged_fact_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION record_confirmation(
    p_staged_fact_id BIGINT,
    p_session_id TEXT,
    p_source TEXT
)
RETURNS VOID AS $$
BEGIN
    -- Only update confirmed_count if this is a new session confirmation
    IF NOT EXISTS (
        SELECT 1 FROM staged_fact_confirmations
        WHERE staged_fact_id = p_staged_fact_id AND session_id = p_session_id
    ) THEN
        -- Record the confirmation
        INSERT INTO staged_fact_confirmations (staged_fact_id, session_id, confirmation_source)
        VALUES (p_staged_fact_id, p_session_id, p_source);

        -- Update the staged fact with new confirmation
        UPDATE staged_facts
        SET confirmed_count = confirmed_count + 1,
            confirmation_source = p_source,
            last_seen_at = now()
        WHERE id = p_staged_fact_id;
    END IF;

    -- Attempt to promote the fact
    PERFORM promote_staged_fact(p_staged_fact_id);
END;
$$ LANGUAGE plpgsql;
