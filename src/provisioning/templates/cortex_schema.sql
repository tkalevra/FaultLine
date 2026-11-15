-- ============================================================================
-- cortex_schema.sql — the Agent Cortex (operating-agent's OWN operational memory).
--
-- SEPARATE per-seat schema `flagent_<uuid>` — NOT `faultline_<slug>`. The DISTINCT
-- schema-name string IS the truth firewall: the user-fact walk derives only
-- `faultline_<slug>` and binds `SET search_path` to it WITHOUT `public`, so it can
-- NEVER reference `cortex_episodes`. The cortex is the serving-agent's OWN persistent
-- OPERATIONAL memory — gotchas, failed commands, pitfalls, corrections, how-tos,
-- command overruns — so the agent stops repeating its own mistakes. It is NEVER a
-- user fact and is NEVER served as one. Think "Claude's .claude memory, but
-- Postgres-backed, per-seat, and firewalled from user memory."
--
-- MANUAL capture only (via /iremember) — NOT auto-scraped from the tenant query path.
-- DETERMINISTIC recall — the `embedding` column is RESERVED and stays NULL for now
-- (semantic is a later layer; recall is keyword + tag matching, no cosine).
-- Only `category`/`source`/`lifecycle_state` are FIXED enums; `note`/`tags`/`context`
-- are CAPTURED content (no enumerated domain vocab in code).
--
-- Per-seat: bound `SET search_path TO flagent_<uuid>` WITHOUT `public`, so this table
-- MUST live inside the seat's own cortex schema. NO public seed — cortex rows are
-- inherently seat-specific (created empty). `user_id` is stored for traceability only;
-- isolation is provided by the schema, never by user_id scoping.
-- (migration 147 backfills this schema+table into existing provisioned tenants.)
-- ============================================================================
CREATE TABLE IF NOT EXISTS {schema_name}.cortex_episodes (
    id                BIGSERIAL   PRIMARY KEY,
    user_id           TEXT        NOT NULL,               -- traceability; isolation is the schema
    category          TEXT        NOT NULL,               -- FIXED enum: gotcha | failed_command | pitfall | correction | howto | overrun
    note              TEXT        NOT NULL,               -- the lesson, in the agent's own voice
    tags              TEXT[]      NOT NULL DEFAULT '{}',  -- the triggers/topics this note guards (docker, llm-endpoint, git, deploy, tests, ...)
    context           JSONB,                              -- optional: {command, fix, file, path, ...}
    source            TEXT        NOT NULL DEFAULT 'agent_manual',  -- FIXED enum: agent_manual (only door in v1)
    -- WHO WROTE IT (migration 217): which MCP client / LLM added this note. Same seat, just
    -- tagged — so a multi-model seat can tell its own notes apart and a "mine" recall lane can
    -- filter to them. NULL on legacy rows and on any note the model didn't tag (recall's
    -- `client_source IS NULL` lane keeps them visible to everyone — never lose a lesson).
    client_source     TEXT        DEFAULT NULL,
    -- WHERE the lesson was learned (migration 261): the branch/line of work — a git branch,
    -- a matter number, a ward name; an opaque label the caller knows. RANKING SIGNAL ONLY:
    -- a recall that states the branch being worked gives same-branch notes an additive
    -- bonus. It NEVER gates, filters, or excludes anything; NULL (legacy/untagged) notes
    -- surface unchanged with no bonus and no penalty.
    branch            TEXT        DEFAULT NULL,
    -- WHERE this lesson is true (migration 262): an environment binding — 'truenas',
    -- 'p70', 'local', a ward, a lab. RANKING signal only: same-env notes rank first,
    -- other-env notes demote slightly and are STAMPED at render; untagged (conceptual)
    -- notes stay neutral. Never gates, never hides.
    env               TEXT        DEFAULT NULL,
    -- WHO / WHAT / WHICH-PROJECT (schema uplift, owner directive; migration 301 back-fills
    -- existing seats): the configured MODEL doing the recording, a free-form owner-facing
    -- NOTE TYPE ("code", "bug", "operational" — LOOSE by design, never an enum; `category`
    -- stays the internal engine enum), and the PROJECT the note relates to ("faultline",
    -- a repo name). All OPTIONAL free strings, bounded 64 at the write seam; provenance
    -- rendered at recall (model=<x> · project=<x> · (<N>d)), never gates anything.
    model             TEXT        DEFAULT NULL,
    note_type         TEXT        DEFAULT NULL,
    project           TEXT        DEFAULT NULL,
    -- EXPLICIT SUPERSESSION (migration 262): set on an outdated procedural note to point
    -- at the correction that replaced it. Tombstone, never a delete — the row stays
    -- queryable and renders "OUTDATED — superseded: <new rule>".
    superseded_by     BIGINT      DEFAULT NULL,
    embedding         BYTEA,                              -- RESERVED, NULL for now (semantic recall is a later layer; NO cosine now)
    lifecycle_state   TEXT        NOT NULL DEFAULT 'observed',  -- FIXED enum: observed | hardened | decayed
    observation_count INTEGER     NOT NULL DEFAULT 1,     -- a repeated note hardens into a rule at >= 3
    hit_count         INTEGER     NOT NULL DEFAULT 0,     -- recall relevance hits
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ,                         -- manual notes are durable (NULL); reserved for future decay-eligible sources
    -- ── EVIDENCE (migration 173) — a claim with no proof rots into a TRAP ────────────────
    -- A note is a CLAIM about the world. Twice on 2026-07-13 stale knowledge manufactured
    -- phantom work (a "Known Bug" that had been fixed weeks earlier; tests for deleted code).
    -- `evidence_sha` makes staleness DECIDABLE: an agent can ask
    -- `git merge-base --is-ancestor <evidence_sha> HEAD` and know the claim is unverified since.
    -- A note WITH evidence outranks one without at recall. Evidence is OPTIONAL — a note without
    -- it is still a note (never lose the lesson), it just carries less weight and can't self-date.
    evidence_sha      TEXT,                               -- the commit the claim was checked against
    evidence          JSONB,                              -- {ref, command, output, artifact, at}
    verified_at       TIMESTAMPTZ                         -- when the claim was last CONFIRMED true
);

-- Dedup / hardening: the SAME lesson (same category + normalized note) strengthens ONE
-- row (UPSERT bumps observation_count). A UNIQUE expression index on md5(lower(note))
-- keeps the key compact and case-insensitive; the UPSERT's ON CONFLICT targets this
-- exact expression.
CREATE UNIQUE INDEX IF NOT EXISTS cortex_episodes_dedup
    ON {schema_name}.cortex_episodes
    (category, md5(lower(note)));

-- Tag-trigger recall: fast overlap lookup for just-in-time note retrieval.
CREATE INDEX IF NOT EXISTS idx_cortex_episodes_tags
    ON {schema_name}.cortex_episodes
    USING GIN (tags);

-- Recall scan (most-relevant/recent, non-decayed).
CREATE INDEX IF NOT EXISTS idx_cortex_episodes_lifecycle
    ON {schema_name}.cortex_episodes (lifecycle_state, last_seen_at DESC);
