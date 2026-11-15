-- Migration 147: Agent Cortex — firewalled per-seat `flagent_<uuid>` schema
--                + `cortex_episodes` table, backfilled into every provisioned tenant.
-- Date: 2026-07-08
--
-- WHY
-- ---
-- The Agent Cortex is the operating-agent's OWN persistent OPERATIONAL memory — the
-- gotchas, failed commands, pitfalls, corrections, how-tos, and command overruns it
-- learns while serving, so it stops repeating its own mistakes. Think "Claude's
-- .claude memory, but Postgres-backed, per-seat, and separate from user memory."
-- MANUAL capture only (via /iremember); NOT auto-scraped from the tenant query path.
--
-- THE FIREWALL: the cortex lives in a SEPARATE schema `flagent_<uuid_slug>`, DISJOINT
-- from the user-fact schema `faultline_<uuid_slug>`. The user-fact walk derives only the
-- `faultline_` name and binds `SET search_path` to it WITHOUT `public`, so it can NEVER
-- reference `cortex_episodes`. The distinct schema-name string IS the truth firewall.
--
-- This is ADDITIVE and fail-safe: it touches no user table, no WGM gate, no query scope.
-- NEW tenants get the cortex schema from provisioning_job.create_cortex_schema(); this
-- migration applies the same DDL to every ALREADY-provisioned tenant.
--
-- DETERMINISTIC: `embedding` is RESERVED (NULL for now; recall is keyword+tag, no cosine).
-- Only `category`/`source`/`lifecycle_state` are fixed enums; `note`/`tags`/`context`
-- are captured content.
--
-- Idempotent: CREATE SCHEMA / TABLE / INDEX IF NOT EXISTS. No DROP, no destructive SQL.

-- ============================================================================
-- For every existing user schema `faultline_<slug>`, create the sibling cortex
-- schema `flagent_<slug>` + its `cortex_episodes` table. The slug is shared, only
-- the prefix differs (mirrors derive_cortex_schema_name()).
-- ============================================================================
DO $$
DECLARE
    _user_schema   TEXT;
    _cortex_schema TEXT;
BEGIN
    FOR _user_schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        _cortex_schema := 'flagent_' || substring(_user_schema FROM length('faultline_') + 1);

        EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', _cortex_schema);

        EXECUTE format($t$
            CREATE TABLE IF NOT EXISTS %I.cortex_episodes (
                id                BIGSERIAL   PRIMARY KEY,
                user_id           TEXT        NOT NULL,
                category          TEXT        NOT NULL,
                note              TEXT        NOT NULL,
                tags              TEXT[]      NOT NULL DEFAULT '{}',
                context           JSONB,
                source            TEXT        NOT NULL DEFAULT 'agent_manual',
                embedding         BYTEA,
                lifecycle_state   TEXT        NOT NULL DEFAULT 'observed',
                observation_count INTEGER     NOT NULL DEFAULT 1,
                hit_count         INTEGER     NOT NULL DEFAULT 0,
                first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at        TIMESTAMPTZ
            )
        $t$, _cortex_schema);

        EXECUTE format($t$
            CREATE UNIQUE INDEX IF NOT EXISTS cortex_episodes_dedup
                ON %I.cortex_episodes
                (category, md5(lower(note)))
        $t$, _cortex_schema);

        EXECUTE format($t$
            CREATE INDEX IF NOT EXISTS idx_cortex_episodes_tags
                ON %I.cortex_episodes USING GIN (tags)
        $t$, _cortex_schema);

        EXECUTE format($t$
            CREATE INDEX IF NOT EXISTS idx_cortex_episodes_lifecycle
                ON %I.cortex_episodes (lifecycle_state, last_seen_at DESC)
        $t$, _cortex_schema);

        RAISE NOTICE 'Migration 147: created % (cortex) for %', _cortex_schema, _user_schema;
    END LOOP;
END $$;
