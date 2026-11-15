-- Migration 202: PER-SEAT BYO-brain config — closes a shared-credential exposure on the
--                individual/house account. CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-31
--
-- THE DEFECT THIS CLOSES
-- ----------------------
-- Self-serve individuals are not accounts. They are SEATS under ONE shared house account
-- ("Singles", accounts.account_kind = 'individual_house' — saas/accounts.py:65-120), each with
-- its own faultline_<uuid> data schema but all pointing at the SAME control-plane account row.
--
-- The BYO engine-brain config is keyed by account (migration 135: account_llm_config.account_id
-- is the PRIMARY KEY), and the individual console read and wrote it with that shared key:
--     saas/user_console.py:2138  _llm_config.get_config_redacted(individual["account_id"])
--     saas/user_console.py:2160  _llm_config.upsert_config(individual["account_id"], fields)
-- So every individual on the house account shared ONE brain row. Individual A sets an endpoint;
-- individual B's ingest then runs on A's endpoint, and B can overwrite it. The sharp end is that
-- the row carries `api_key_ciphertext` — a Fernet-encrypted API KEY. That is one stranger's paid
-- credential being spent by another stranger, which is a credential-scope violation independent
-- of intent or cost.
--
-- STATE AT WRITE TIME (verified live on pre-prod, read-only):
--     singles | individual_house | active | 0 active seats | has_brain=f | has_key=f
-- So the exposure is LATENT, not an active breach — it materialises the first time an individual
-- binds an endpoint. The backfill below is therefore a no-op on THIS database, but it is written
-- to be correct on a deployment where it is not.
--
-- WHAT IS DELIBERATELY *NOT* CHANGED
-- ----------------------------------
-- A real corporate tenant (account_kind 'tenant' / 'team' / 'clinical') binding ONE brain for all
-- of its seats is CORRECT and is untouched. Those seats belong to one organisation that owns the
-- credential; `individual_users.can_control_endpoint` is the existing governance lever there. Only
-- the house grouping — where the co-tenants are unrelated strangers — moves to per-seat.
--
-- WHY public.* AND NOT A PER-TENANT TABLE (deliberate, and the opposite of the usual rule)
-- ---------------------------------------------------------------------------------------
-- The house rule in this repo is "tenant data lives in the tenant schema". A credential is NOT
-- tenant data — it is control-plane state, and putting it in the tenant schema would make it
-- WORSE, not better:
--   1. The per-tenant request search_path is `SET search_path TO faultline_<uuid>` WITHOUT public.
--      A ciphertext sitting in that schema would be inside the reach of the data-plane connection
--      that runs metadata-driven ingest/query SQL. Today NO credential is reachable from any
--      tenant search_path at all. That property is worth keeping.
--   2. Every reader of a brain credential is the control plane (saas/db.py), whose DSN can read
--      every schema anyway — so schema placement buys no isolation against the actual readers.
--      The isolation that does the work is the KEY (user_id) plus a server-derived principal,
--      which is exactly how account_llm_config is protected today.
--   3. `/me/*` and the backend request-brain bind both run BEFORE any tenant search_path is bound
--      (saas/llm_context._account_for_user opens its own public connection for precisely this
--      reason) — a tenant-schema table would add a schema lookup to the hot resolve path.
-- Consequence: NO per-tenant object is added, so src/provisioning/templates/user_schema.sql is
-- correctly untouched by this migration. (The migration-197 defect — a fanout migration that
-- shipped without the template and therefore silently skips every NEW tenant — cannot apply here;
-- there is nothing to fan out.)
--
-- `account_id` IS STORED ON THE ROW ON PURPOSE. The resolver matches on (user_id, account_id), so
-- if a seat is later transferred to a corporate account its personal brain STOPS resolving and the
-- org's governed brain takes over — which is the correct governance outcome of a transfer, reached
-- fail-closed rather than by remembering to clean up.
--
-- Secrets: api_key_ciphertext / embedding_key_ciphertext are Fernet blobs (saas/crypto.py, master
-- key SAAS_LLM_CRED_KEY). Plaintext is NEVER stored and NEVER logged. base_url / embedding_url are
-- SSRF-guarded at save AND at resolve (saas/llm_config.validate_egress_url) — unchanged, reused.
--
-- Additive; idempotent (IF NOT EXISTS everywhere); safe to re-run.

-- ── Part 1: the per-seat config table ─────────────────────────────────────────────────
--
-- Column set MIRRORS public.account_llm_config (migrations 135 + 188 + 201) so one storage layer
-- serves both scopes and neither can drift from the other.
--
-- FK on user_id → public.account_users(user_id): that column carries `UNIQUE (user_id)`
-- (migration 131:61, the cross-account BOLA firewall), so it is a valid FK target AND it means a
-- seat row cannot exist for a user who is not a seat. ON DELETE CASCADE: deprovisioning a seat
-- destroys its stored credential with it, rather than orphaning a ciphertext.

CREATE TABLE IF NOT EXISTS public.seat_llm_config (
    user_id                  UUID PRIMARY KEY
                                REFERENCES public.account_users(user_id) ON DELETE CASCADE,
    account_id               UUID NOT NULL
                                REFERENCES public.accounts(account_id) ON DELETE CASCADE,

    -- ── engine-brain LLM (this SEAT's own; overrides the account row) ─────────────
    backend_type             TEXT,
    base_url                 TEXT,
    model                    TEXT,
    api_key_ciphertext       TEXT,           -- Fernet ciphertext (write-only, never echoed)
    extra_headers_json       JSONB,
    extra_params_json        JSONB,

    -- ── BYO-embedding (storage + resolution ONLY — not wired, same as migration 135) ──
    embedding_backend        TEXT DEFAULT 'internal',
    embedding_url            TEXT,
    embedding_model          TEXT,
    embedding_key_ciphertext TEXT,           -- Fernet ciphertext (write-only)
    embedding_dim            INT,

    -- ── per-brain outbound RPM ceiling (mirrors migration 188) ────────────────────
    max_rpm                  INT,

    -- ── calibration / latency profile (mirrors migration 201) ─────────────────────
    latency_profile          TEXT,
    latency_profile_source   TEXT,
    latency_p50_ms           INTEGER,
    latency_p95_ms           INTEGER,
    latency_samples          INTEGER,
    latency_measured_at      TIMESTAMPTZ,

    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_tested_at           TIMESTAMPTZ
);

-- The account-scoped index supports the tenant console's per-seat health roll-up (which is
-- account-scoped from the session) without a sequential scan.
CREATE INDEX IF NOT EXISTS idx_seat_llm_config_account
    ON public.seat_llm_config (account_id);

COMMENT ON TABLE public.seat_llm_config IS
    'PER-SEAT BYO engine-brain config. Takes precedence over public.account_llm_config for seats '
    'of a house/individual account (accounts.account_kind = ''individual_house''), where the seats '
    'are unrelated strangers and MUST NOT share one credential. Corporate accounts '
    '(tenant/team/clinical) keep the shared account row and are unaffected. Control-plane public.* '
    'only; NEVER seeded into a tenant schema and NEVER read from a tenant search_path.';

COMMENT ON COLUMN public.seat_llm_config.account_id IS
    'The account this seat belonged to when the brain was saved. The resolver matches on '
    '(user_id, account_id), so a seat transferred to another account stops resolving its personal '
    'brain and falls under that account''s governance — fail-closed, no cleanup step to forget.';

COMMENT ON COLUMN public.seat_llm_config.api_key_ciphertext IS
    'Fernet ciphertext of THIS SEAT''s brain key (saas/crypto.py). Write-only: never returned by '
    'any read API, never rendered in any console, never logged.';


-- ── Part 2: MIGRATE EXISTING STATE — never leave a house brain silently shared ─────────
--
-- For every house/individual account that already has a brain row:
--   * EXACTLY ONE active seat → the config is unambiguously that person's. Move it (including the
--     ciphertext) to their seat row, then DELETE the shared account row.
--   * ZERO or MORE THAN ONE active seat → ownership is NOT determinable. The row is DELETED, not
--     inherited: leaving it means the next individual to sign in silently spends whoever's key that
--     is. A RAISE WARNING names the account so the operator can see it happened; the affected
--     individual simply re-enters their endpoint in their own console.
--
-- Either way the shared row is GONE afterwards, which is the invariant the resolver relies on.
-- The DELETE is also the reason this is safe to re-run: after the first pass there is nothing left
-- to match.
--
-- NOTE: this migrates only 'individual_house' accounts. Corporate account rows are not read or
-- written by this block at all.

DO $$
DECLARE
    _acct      RECORD;
    _seat      UUID;
    _seats     INTEGER;
    _moved     INTEGER := 0;
    _cleared   INTEGER := 0;
BEGIN
    FOR _acct IN
        SELECT c.account_id
        FROM   public.account_llm_config c
        JOIN   public.accounts a ON a.account_id = c.account_id
        WHERE  a.account_kind = 'individual_house'
    LOOP
        SELECT COUNT(*) INTO _seats
        FROM   public.account_users au
        WHERE  au.account_id = _acct.account_id AND au.status = 'active';

        IF _seats = 1 THEN
            SELECT au.user_id INTO _seat
            FROM   public.account_users au
            WHERE  au.account_id = _acct.account_id AND au.status = 'active'
            LIMIT  1;

            INSERT INTO public.seat_llm_config (
                user_id, account_id, backend_type, base_url, model, api_key_ciphertext,
                extra_headers_json, extra_params_json, embedding_backend, embedding_url,
                embedding_model, embedding_key_ciphertext, embedding_dim, max_rpm,
                latency_profile, latency_profile_source, latency_p50_ms, latency_p95_ms,
                latency_samples, latency_measured_at, updated_at, last_tested_at)
            SELECT _seat, c.account_id, c.backend_type, c.base_url, c.model, c.api_key_ciphertext,
                   c.extra_headers_json, c.extra_params_json, c.embedding_backend, c.embedding_url,
                   c.embedding_model, c.embedding_key_ciphertext, c.embedding_dim, c.max_rpm,
                   c.latency_profile, c.latency_profile_source, c.latency_p50_ms, c.latency_p95_ms,
                   c.latency_samples, c.latency_measured_at, NOW(), c.last_tested_at
            FROM   public.account_llm_config c
            WHERE  c.account_id = _acct.account_id
            ON CONFLICT (user_id) DO NOTHING;   -- a seat row already set wins; never clobber it

            DELETE FROM public.account_llm_config WHERE account_id = _acct.account_id;
            _moved := _moved + 1;
            RAISE NOTICE 'seat_llm_config: moved house brain from account % to its ONE seat %',
                         _acct.account_id, _seat;
        ELSE
            DELETE FROM public.account_llm_config WHERE account_id = _acct.account_id;
            _cleared := _cleared + 1;
            RAISE WARNING 'seat_llm_config: house account % had a SHARED brain config with % '
                          'active seat(s) — ownership is not determinable, so the shared config '
                          '(including its encrypted key) was DELETED rather than left shared. '
                          'Affected individuals must re-enter their endpoint in their own console.',
                          _acct.account_id, _seats;
        END IF;
    END LOOP;

    RAISE NOTICE 'seat_llm_config backfill complete: % house config(s) moved to a seat, % cleared',
                 _moved, _cleared;
END $$;
