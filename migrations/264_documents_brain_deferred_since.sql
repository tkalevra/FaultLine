-- Migration 264: documents.brain_deferred_since — the deferral-episode WALL CLOCK
-- Date: 2026-08-18
--
-- WHY
-- ---
-- The brain-unavailable deferral bound was a COUNT of claims (documents.attempts,
-- DOC_MAX_ATTEMPTS=5). A claim count is outage DURATION sized by the drain interval:
-- at one claim per REEMBED_INTERVAL (default 60s) the bound terminated a document ~5
-- minutes into an outage — and 95fb517b measured the assumption behind that sizing
-- empirically wrong ("the bound was sized for an outage lasting MINUTES, and today's
-- logs show brains unavailable for HOURS"). Innocent uploads died as status='error'
-- while their brain was merely down, which is the honest-reporting defect: the system
-- blamed the document for the brain's outage. That commit deliberately deferred the
-- threshold decision to the owner; this migration is the decided answer.
--
-- THE DECISION (see _DOC_BRAIN_DEFER_MAX_AGE in src/re_embedder/embedder.py):
-- bound on WALL CLOCK, measured as ONE CONTINUOUS deferral episode:
--   • set to now() on the first non-paced brain-unavailable defer of an episode;
--   • cleared only on PROGRESS — a terminal finalize, or a PRODUCTIVE deadline
--     requeue (one whose owed-chunk count actually decreased; the batch ran without
--     a fatal AND moved the document). An UNPRODUCTIVE requeue instead keeps/stamps
--     the clock, so the owed-chunks lane is bounded by the same 30-day episode
--     rather than the lifetime attempts count (a queue pass can run ~56 min against
--     a 30-min claim lease — requeues and reclaims alone used to walk a healthy
--     document to terminal 'partial' at attempts>=5). A
--     CLAIM does NOT clear it: a claim is a pure DB write that succeeds while the
--     brain is still down (and PostgreSQL RETURNING yields post-UPDATE values, so
--     clearing at claim could never be observed anyway). Two short outages weeks
--     apart, with no progress between them, never exceed one episode; any real
--     progress resets the clock honestly;
--   • never touched by rate_deferred (paced) defers — a designed daily-budget park
--     must not age a document toward a terminal verdict.
-- Magnitude 30 days: derived, not chosen — it is the horizon this system already
-- reasons with for how long USER CONTENT may sit unprocessed (staged_facts.expires_at
-- = now()+30d, the Class-C clock; the episodic re-mine poison-stamp at >30d), and it
-- strictly exceeds every deferral cause ever observed on this deployment (deploys:
-- minutes; event-loop stalls: ~21 min measured; pacing: <=24h by design; breaker-open
-- brain outages: hours-to-all-day observed). A not-its-fault document therefore always
-- outlives the outage that blocked it; a genuinely malformed document still fails on
-- its own chunks ('partial') and never reaches this clock.
--
-- Additive + idempotent ONLY: ADD COLUMN IF NOT EXISTS, NULL default = "no deferral
-- episode in progress", which is exactly the pre-migration state. No DROP, no UPDATE.
-- Rows written before this migration read back as "never deferred" — correct, because
-- the clock only ever starts at a defer that has not happened yet.
--
-- APPLIED BY the same loop shape as migration 195 (every faultline_% schema that has a
-- documents table; tenants that predate migration 183 are skipped).
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- Skip a tenant that predates migration 183 (no documents table yet).
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'documents'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.documents '
            'ADD COLUMN IF NOT EXISTS brain_deferred_since TIMESTAMPTZ DEFAULT NULL',
            _schema);
    END LOOP;
END $$;
