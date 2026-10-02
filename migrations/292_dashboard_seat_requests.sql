-- Migration: users waiting for a seat (FOSS #148)
-- Purpose: once any seat is minted, only seated user_ids are admitted, and every OpenWebUI
--          user (each signs in with its own X-OpenWebUI-User-Id on the shared MCP key)
--          occupies one of the FOSS_MAX_SEATS seats. A user_id the MCP seat gate refuses for
--          lack of a seat is recorded HERE so the operator console can list it and seat it in
--          one click, instead of the operator having to dig the id out of OpenWebUI.
--
-- Bounded: the MCP keeps only the most recent rows (see provisioning_status.record_seat_request).
-- A row is removed when a seat is minted for that user_id. Operator control-plane data in
-- public, never a tenant schema. Records an id only; no memory content. Idempotent.

CREATE TABLE IF NOT EXISTS public.dashboard_seat_requests (
    user_id     UUID        PRIMARY KEY,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempts    INTEGER     NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_dashboard_seat_requests_last_seen
    ON public.dashboard_seat_requests (last_seen DESC);
