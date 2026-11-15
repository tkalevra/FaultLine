-- 324_growth_signals.sql — THE ERROR/RECALL ENHANCEMENT LOOP: the durable
-- capture store for recall misses and ingest errors (the growth-signal ledger).
--
-- THE PRINCIPLE (owner)
-- ---------------------
-- Every recall miss (a question the store should answer but abstains on or
-- serves wrong) and every ingest error (a turn that degraded / refused /
-- dropped) is a GROWTH SIGNAL: "the engine growth seeds are intended to catch
-- these things — any subject missed is grown into by the spine/engine and not
-- dropped." Before this migration the miss classes were observed only as
-- scattered log lines — the recall abstention/honest-empty, the mint guard's
-- type_refused, the extraction_degraded envelopes, the BUG-RECALL-15
-- served-vs-asked gap — and none of them fed the growth machinery
-- systematically. This table is the DURABLE, READABLE RECORD: the miss class,
-- the sentence, the missed-shape tokens, the served-vs-expected gap, and the
-- growth-candidate state of every captured miss.
--
--   signal_kind     'recall_miss' | 'ingest_error'
--   miss_class      'honest_empty' | 'served_vs_asked_gap' | 'escalated' |
--                   'walk_shed' | 'type_refused' | 'edge_dropped' |
--                   'nothing_captured' | 'extraction_degraded' |
--                   'chunk_failures'
--   fingerprint     the dedup key: sha1(kind | miss_class | normalized tokens)
--                   — one row per distinct miss SHAPE, occurrence-counted on
--                   re-occurrence (a re-asked miss bumps, never spams)
--   query_text      the sentence (the recall question or the ingest turn)
--   query_tokens    the normalized missed-shape tokens
--   expected/served the gap: what the door should have served vs what it served
--   context         door-specific extras (alerts, refusal dicts, counts)
--   growth_state    'fed' (the missed shape was queued into ontology_evaluations
--                   via the ingest_miss_pushback seam — classify_unknown_concepts
--                   grows the is-a ladder into the missed subject) |
--                   'capture_only' (recorded; no admissible missed shape)
--   growth_target   where the shape was fed (the marker namespace)
--
-- CAPTURE IS ADDITIVE AND FLAG-GATED: the writers (src/growth_signals.py,
-- hooked at the /query wrapper, the /ingest funnel, and the degraded envelopes)
-- run under GROWTH_SIGNAL_CAPTURE / GROWTH_SIGNAL_FEED, both default ON; flags
-- OFF = not one row, not one log line — today's behavior exactly. The table
-- itself gates nothing: no ingest/recall path reads it; it is the operator's
-- record (GET /growth-signals/report) and the feed's own bookkeeping.
--
-- BOUNDED: TTL-pruned (GROWTH_SIGNAL_TTL_DAYS, default 30) and row-capped
-- (GROWTH_SIGNALS_MAX, default 2000, oldest last_seen evicted) on every capture.
--
-- PER-TENANT: the runtime binds `SET search_path TO {schema}` WITHOUT public —
-- the table MUST exist inside every tenant schema. New tenants get it from the
-- provisioning template (src/provisioning/templates/user_schema.sql), updated in
-- this same change — the 197/273/298/299/300/301 rule: template + migration,
-- never one without the other. Rides the boot-migration ledger
-- (public.schema_migration_status): applied once per schema, never re-swept.
--
-- GUARDED FANOUT: migration 300's exact loop (information_schema.schemata join
-- — a stale registry row naming a dropped schema cannot abort the DO block).
-- Additive + idempotent only (CREATE TABLE / CREATE INDEX IF NOT EXISTS): no
-- DROP, no UPDATE, no DELETE, no backfill. Safe to re-run. The cortex schemas
-- (flagent_<uuid>) do not match the faultline_% glob — the truth firewall is
-- untouched.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.growth_signals ('
            '    id             BIGSERIAL   PRIMARY KEY,'
            '    signal_kind    TEXT        NOT NULL,'
            '    miss_class     TEXT        NOT NULL,'
            '    fingerprint    TEXT        NOT NULL,'
            '    query_text     TEXT,'
            '    query_tokens   TEXT,'
            '    expected       TEXT,'
            '    served         TEXT,'
            '    context        JSONB       DEFAULT ''{}''::jsonb,'
            '    growth_state   TEXT        NOT NULL DEFAULT ''pending'','
            '    growth_target  TEXT,'
            '    occurrence_count INTEGER   NOT NULL DEFAULT 1,'
            '    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),'
            '    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now()'
            ')',
            _schema);

        -- The hammering guard: ONE row per distinct miss shape (kind, class,
        -- token fingerprint); re-occurrence bumps occurrence_count, never adds
        -- rows. Plain UNIQUE — the fingerprint is NOT NULL by construction.
        EXECUTE format(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_growth_signals_fingerprint '
            'ON %I.growth_signals (fingerprint)',
            _schema);

        -- The operator's access path: per kind + growth state, most recent first.
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_growth_signals_kind_state '
            'ON %I.growth_signals (signal_kind, growth_state, last_seen_at DESC)',
            _schema);

        EXECUTE format(
            'COMMENT ON TABLE %I.growth_signals IS %L',
            _schema,
            'Migration 324 (the error/recall enhancement loop): durable capture '
            'store for recall misses and ingest errors — the miss class, the '
            'sentence, the missed-shape tokens, the served-vs-expected gap, and '
            'the growth-candidate state of every captured miss. One row per '
            'distinct miss fingerprint (kind|class|tokens), occurrence-counted. '
            'growth_state fed = the missed shape was queued into '
            'ontology_evaluations (ingest_miss_pushback seam) so '
            'classify_unknown_concepts grows the is-a ladder into the missed '
            'subject. Flag-gated additive capture (GROWTH_SIGNAL_CAPTURE / '
            'GROWTH_SIGNAL_FEED, default ON); gates nothing; TTL + cap bounded. '
            'Operator view: GET /growth-signals/report.');
    END LOOP;
END $$;
