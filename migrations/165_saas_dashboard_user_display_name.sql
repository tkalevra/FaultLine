-- Migration 165: SaaS dashboard-user self-service DISPLAY NAME (CLOSED-SOURCE SaaS layer)
-- Date: 2026-07-11
-- Purpose: give a human dashboard login (tenant admin OR operator) an optional, self-editable
--          DISPLAY NAME / label, distinct from the immutable UNIQUE username (which is the login
--          email for tenants). The "My Account" self-service panel lets the signed-in principal
--          set it; it is cosmetic (how the console greets them), never an auth identifier.
--
-- Additive + idempotent (IF NOT EXISTS). NULL = fall back to the username, so every existing
-- login keeps its current behavior with zero backfill. Control-plane public.* table only —
-- NEVER seeded into a tenant schema, never on a tenant search_path (isolation rule intact).
--
-- Read with: migration 136 (dashboard_users), migration 159 (can_view_billing).

ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS display_name TEXT;
