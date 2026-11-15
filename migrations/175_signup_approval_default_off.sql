-- 175 — signup approval DEFAULT OFF (no human at the entrance).
--
-- WHY: migration 174 shipped `require_signup_approval DEFAULT TRUE` (new accounts held for a
-- human to approve). That contradicts the product's own promise — the landing copy says
-- "Self-serve, prices published — no sales call to get started" — and "we claim no human to
-- entrance". A human approving every signup IS a human at the entrance. So the DEFAULT flips to
-- OFF: self-serve signups activate immediately.
--
-- The operator TOGGLE stays: an admin can still turn approval ON from the console (e.g. to throttle
-- an abuse wave). This only changes the DEFAULT and the current value — it does not remove the lever.
--
-- Invite-gating is a SEPARATE mechanism (a self-issued code, no human acting at signup time) and is
-- deliberately left untouched here — that is a product-positioning decision, not the human gate.
--
-- Idempotent: safe to re-run. public.* only; no tenant schema touched.

ALTER TABLE public.platform_settings
    ALTER COLUMN require_signup_approval SET DEFAULT FALSE;

-- Flip the existing singleton row too (174 inserted it TRUE).
UPDATE public.platform_settings
    SET require_signup_approval = FALSE
    WHERE require_signup_approval IS TRUE;
