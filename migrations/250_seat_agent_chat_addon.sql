-- 250_seat_agent_chat_addon.sql
--
-- SEAT-SCOPED chat-agent ("agent chat") add-on entitlement, for the SELF-SERVE INDIVIDUAL.
--
-- WHY THIS CANNOT RIDE THE EXISTING ACCOUNT-LEVEL COLUMNS (the whole reason this migration
-- exists). Migration 218 put the chat-agent entitlement on public.accounts:
--     accounts.agent_feature_enabled  BOOL
--     accounts.agent_seat_bucket      INT (1|5|10)
-- That is correct for a TENANT, where the account IS the customer. It is WRONG for a
-- self-serve individual, because every self-serve individual is a SEAT inside ONE shared
-- house account (accounts.account_kind = 'individual_house', the "Singles" account — see
-- saas/accounts.py). There is exactly ONE such row for ALL of them. Consequences of trying
-- to sell the add-on on that row, both measured against the live control plane:
--
--   (a) LEAK — flipping agent_feature_enabled for one paying individual enables the feature
--       for EVERY other individual in the house account that satisfies the seat clause. One
--       person's $40 would buy the feature for the entire self-serve population.
--   (b) MUTUAL DESTRUCTION — the bucket clause in saas/agent_entitlement.serve_allowed is
--       COUNT(active agent seats OF THE ACCOUNT) <= agent_seat_bucket. With bucket=1 and two
--       qualifying individuals in the house, the count is 2 > 1 and BOTH are refused. The
--       second buyer would switch the first buyer off.
--
-- So the individual's entitlement attaches to THEIR OWN SEAT ROW, and nothing about it is
-- shared. account_users is already keyed (account_id, user_id) with a UNIQUE(user_id) —
-- the seat IS the person — which makes it the correct carrier.
--
-- COLUMNS (mirrors the accounts-level consent-audit shape of migration 218 / 160 so the two
-- lanes read alike):
--   agent_addon_enabled     BOOL NOT NULL DEFAULT FALSE — the seat-scoped on/off.
--   agent_addon_bucket      INT NULL (1|5|10)           — provisioned bucket, NULL = none.
--   agent_addon_source      TEXT NULL                   — 'stripe' | 'operator' (who granted).
--   agent_addon_reference   TEXT NULL                   — the Stripe subscription id, so a
--                                                         grant is traceable to the signed
--                                                         event that caused it and a revoke
--                                                         can be matched to the right sub.
--   agent_addon_granted_at  TIMESTAMPTZ NULL
--   agent_addon_revoked_at  TIMESTAMPTZ NULL
--
-- PRICE IS NOT STORED HERE. The add-on is priced by bucket in ONE place
-- (saas/entitlements.agent_addon_price_for_bucket — $40/$160/$320 CAD/mo, env-overridable),
-- and it is deliberately TIER-INDEPENDENT: the same bucket costs the same whether the buyer
-- is an individual, a Team or an Enterprise. A price column here would be a second copy of a
-- number that must not vary, which is the opposite of the seat-rate snapshot problem
-- (migration 249) where the rate legitimately differs per purchase.
--
-- The gate that READS these lives in saas/agent_entitlement.serve_allowed; the write path is
-- saas/agent_entitlement.grant_seat_agent_addon / revoke_seat_agent_addon; the purchase door
-- is saas/user_console.py POST /me/agent-chat/checkout (which grants NOTHING — the
-- signature-verified Stripe event is the sole grant trigger).
--
-- Control-plane public.* ONLY. Idempotent; safe to re-run.

ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS agent_addon_enabled BOOL NOT NULL DEFAULT FALSE;
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS agent_addon_bucket INT NULL;
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS agent_addon_source TEXT NULL;
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS agent_addon_reference TEXT NULL;
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS agent_addon_granted_at TIMESTAMPTZ NULL;
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS agent_addon_revoked_at TIMESTAMPTZ NULL;

-- Bucket CHECK mirrors accounts_agent_seat_bucket_check (migration 218): only the three
-- ratified buckets, or NULL. Keeps a stray write from inventing a bucket the biller cannot
-- price. NOTE the product rule that an INDIVIDUAL may only buy bucket 1 is enforced in
-- saas/agent_entitlement.INDIVIDUAL_ADDON_BUCKETS, NOT here — the column must still accept
-- 5/10 so an operator can grant a multi-agent seat-scoped add-on on a tenant if that is ever
-- wanted, and so this constraint stays a data-integrity rule rather than a pricing policy.
ALTER TABLE public.account_users
    DROP CONSTRAINT IF EXISTS account_users_agent_addon_bucket_check;
ALTER TABLE public.account_users
    ADD CONSTRAINT account_users_agent_addon_bucket_check
    CHECK (agent_addon_bucket IS NULL OR agent_addon_bucket = ANY (ARRAY[1, 5, 10]));

-- An ENABLED add-on must carry a bucket, or the gate would see "on" with nothing provisioned
-- and has to guess. Fail the write instead of leaving the gate to fail closed silently.
ALTER TABLE public.account_users
    DROP CONSTRAINT IF EXISTS account_users_agent_addon_enabled_needs_bucket;
ALTER TABLE public.account_users
    ADD CONSTRAINT account_users_agent_addon_enabled_needs_bucket
    CHECK (agent_addon_enabled = FALSE OR agent_addon_bucket IS NOT NULL);

-- Partial index: only the ON rows are ever interesting (mirrors migration 218's shape).
CREATE INDEX IF NOT EXISTS idx_account_users_agent_addon_enabled
    ON public.account_users (account_id, user_id) WHERE agent_addon_enabled = TRUE;

-- Reconciliation index: the grant is DERIVED from the Stripe subscription snapshot that the
-- signature-verified webhook writes, matched back to the seat by reference.
CREATE INDEX IF NOT EXISTS idx_account_users_agent_addon_reference
    ON public.account_users (agent_addon_reference) WHERE agent_addon_reference IS NOT NULL;

COMMENT ON COLUMN public.account_users.agent_addon_enabled IS
    'Seat-scoped chat-agent add-on. The INDIVIDUAL lane of the entitlement that '
    'accounts.agent_feature_enabled carries for tenants — kept per-seat because every '
    'self-serve individual shares ONE individual_house account row (migration 250).';
