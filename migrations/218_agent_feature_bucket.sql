-- 218_agent_feature_bucket.sql — the chat-agent feature on/off + seat-bucket (SPEC §11).
--
-- This is the OPERATOR-facing control surface for the through-FaultLine chat-agent (the
-- `faultline-agent` appliance / `/v1/agent/chat` channel). Two account-level columns:
--
--   agent_feature_enabled  BOOL (default false) — the ON/OFF switch. OFF → the /v1/agent/chat
--                          entitlement gate refuses serve, FAIL-CLOSED (SPEC §11). This is
--                          DISTINCT from the per-seat `ingest_enabled` train/serve lever: a
--                          frozen (serve) seat may STILL chat (chat is a read of the brain);
--                          only this account switch can kill chat outright.
--
--   agent_seat_bucket      INT NULL — simple provisioning bucket 1 / 5 / 10 (the operator picks
--                          a bucket, never an arbitrary count). The entitlement gate enforces
--                          COUNT(active agent seats) <= agent_seat_bucket. Kept in sync with
--                          account_entitlements(seat_type='agent').quantity at provision time so
--                          register_seat (which reads the entitlement) and the gate (which reads
--                          the bucket) agree. NULL = not provisioned → gate refuses.
--
-- The add-on BILLING line ($40 / $160 / $320 CAD/mo for 1 / 5 / 10) is DISTINCT from the
-- agent SEAT charge ($120/$100/$80) — this column is the feature add-on, billed via
-- entitlements.record_agent_addon_change. Consent-audit columns mirror api_addon (migration 160).
--
-- Idempotent (ADD COLUMN IF NOT EXISTS). The gate that reads these lives in
-- saas/agent_entitlement.py; the operator write path in saas/accounts.py + saas/admin.py.

ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS agent_feature_enabled BOOL NOT NULL DEFAULT FALSE;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS agent_seat_bucket INT NULL;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS agent_feature_enabled_at TIMESTAMPTZ NULL;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS agent_feature_enabled_by TEXT NULL;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS agent_feature_terms_version TEXT NULL;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS agent_feature_disabled_at TIMESTAMPTZ NULL;

-- Partial index mirroring api_addon / patient_provisioning: only the ON rows are interesting.
CREATE INDEX IF NOT EXISTS idx_accounts_agent_feature_enabled
    ON public.accounts (account_id) WHERE agent_feature_enabled = TRUE;

-- Bucket CHECK: only the three ratified buckets (or NULL = not provisioned). Keeps a stray
-- write from inventing a 7-seat tier the billing model does not price.
ALTER TABLE public.accounts
    DROP CONSTRAINT IF EXISTS accounts_agent_seat_bucket_check;
ALTER TABLE public.accounts
    ADD CONSTRAINT accounts_agent_seat_bucket_check
    CHECK (agent_seat_bucket IS NULL OR agent_seat_bucket = ANY (ARRAY[1, 5, 10]));
