-- Migration 264: restore the CLASS-C CLOCK on staged_facts for every existing tenant.
--
-- WHAT WAS WRONG. Migration 012 created staged_facts.expires_at as
--     expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 days'
-- but the tenant template `src/provisioning/templates/user_schema.sql` declared it as a bare
-- `expires_at TIMESTAMPTZ` with NO default. Every tenant schema is built from that template
-- (schema_manager.create_user_schema reads it verbatim), and the template is applied ONLY at
-- creation — so from the day the template diverged, every new tenant lost the default and
-- migration 012 never reached them. Measured on pre-production: 39 of 39 tenant schemas had
-- NO default on the column, and the owner's own seat carried 313 of 642 staged rows with a
-- NULL expiry.
--
-- WHY IT MATTERS. Short-term memory is short-term BECAUSE it expires; the clock is what makes
-- Class C a tier rather than a second permanent store. A row born with a NULL expiry is outside
-- the lifecycle in BOTH directions at once:
--   * expire_staged_facts / decay_class_c_hits key on `expires_at <= now()` — NULL never
--     satisfies it, so the row can never decay and can never be reaped; and
--   * fetch_unsynced_staged and several recall lanes filter `expires_at > now()` — NULL never
--     satisfies that either, so the row is invisible to them.
-- Neither exit, and reduced visibility. Not a staging area — a landfill.
--
-- WHAT THIS DOES. Two things, both idempotent, neither destructive:
--   1. Sets the DEFAULT on every existing tenant's staged_facts.expires_at. `ALTER COLUMN …
--      SET DEFAULT` has no IF NOT EXISTS but is naturally idempotent; it is guarded on the
--      table/column actually existing so a partially-provisioned schema is skipped, not failed.
--   2. Backfills rows that were already born clockless — STARTING NOW, deliberately not
--      backdated from first_seen_at. A row that never had a clock has not had its thirty days
--      in any meaningful sense, and backdating would make this repair itself reap history,
--      which is the opposite of the intent. Every repaired row gets a full, fair window.
--
-- The paired template change is in src/provisioning/templates/user_schema.sql (the CREATE TABLE
-- default). A migration-only change silently skips every newly-provisioned tenant; a
-- template-only change silently skips every existing one. Both halves are required.
--
-- NOTE: this migration re-runs on every container start (docker-entrypoint.sh globs
-- migrations/*.sql with no ledger), which is why step 2 is written to be a no-op once there are
-- no NULL rows left rather than something that would re-stamp live rows on each boot.

DO $$
DECLARE
    _schema  TEXT;
    _n_sch   INTEGER := 0;
    _n_rows  BIGINT  := 0;
    _rows    BIGINT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
        ORDER BY schema_name
    LOOP
        -- Skip a schema with no staged_facts table (partial / aborted provisioning).
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = _schema
              AND table_name   = 'staged_facts'
              AND column_name  = 'expires_at'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.staged_facts ALTER COLUMN expires_at SET DEFAULT (now() + interval ''30 days'')',
            _schema);

        EXECUTE format(
            'UPDATE %I.staged_facts SET expires_at = now() + interval ''30 days'' WHERE expires_at IS NULL',
            _schema);
        GET DIAGNOSTICS _rows = ROW_COUNT;

        _n_sch  := _n_sch + 1;
        _n_rows := _n_rows + _rows;
        IF _rows > 0 THEN
            RAISE NOTICE 'migration 264: % — default restored, % clockless rows given a fresh 30-day window',
                         _schema, _rows;
        END IF;
    END LOOP;

    RAISE NOTICE 'migration 264: staged_facts.expires_at default restored on % tenant schema(s); % row(s) backfilled',
                 _n_sch, _n_rows;
END $$;
