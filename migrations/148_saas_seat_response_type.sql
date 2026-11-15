-- Migration 148: per-seat recall VOICE preset (CLOSED-SOURCE SaaS layer).
-- Date: 2026-07-09
--
-- A seat can pin the WORDING of FaultLine's confidence-as-voice recall bands (the framing
-- text that wraps recalled facts on the way back to the consuming LLM) WITHOUT changing which
-- band a fact lands in. The class->band assignment is the epistemic firewall and is NOT touched
-- by this — only the voice/framing wording changes. See src/mcp/response_types.py.
--
-- response_type ∈ {personal, professional, clinical, concise, agent}
--   agent = DE-SELF mode (facts-as-facts, no "user"/"you" anchor).
--
-- NULL (the default) = "no explicit choice" → the runtime DERIVES the voice from the seat TIER
-- (agent/expert seat_type → agent; every other tier → personal). Leaving it NULL, or SAAS off,
-- yields today's wording EXACTLY (personal == the pre-feature default), so this is backwards-
-- compatible and non-regressing.
--
-- Additive, nullable, idempotent, control-plane public.*. NO-OP when SAAS is off.
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS response_type TEXT;

-- Guard the value set (mirrors the seat_type CHECK from migration 139). NULL passes the check.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'account_users_response_type_check'
    ) THEN
        ALTER TABLE public.account_users
            ADD CONSTRAINT account_users_response_type_check
            CHECK (response_type IS NULL OR response_type IN
                   ('personal', 'professional', 'clinical', 'concise', 'agent'));
    END IF;
END $$;
