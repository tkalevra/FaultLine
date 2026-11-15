-- 225_agent_settings.sql
-- Per-account chat-agent CONTROL-PLANE settings (SPEC §13).
--
-- A single JSONB column on public.accounts holds the tenant-configured knobs for the
-- through-FaultLine chat agent that are NOT entitlement state: the agent's SYSTEM PROMPT and
-- the MEMORY-SIDE selection (faultline | other | none). Additive + nullable default; does NOT
-- touch the entitlement data model (agent_feature_enabled / agent_seat_bucket / the trial
-- clocks) and does NOT change the serve gate. Closed-source (saas branch only — never FOSS).
--
-- Shape read/written by saas/accounts.get_agent_settings / set_agent_settings, surfaced in the
-- tenant console's Chat Agent control-plane tab (GET/POST /tenant/agent/prompt).
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS agent_settings JSONB NOT NULL DEFAULT '{}'::jsonb;
