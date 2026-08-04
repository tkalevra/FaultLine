---
name: clear-faultline-db
description: Fully wipe a FaultLine tenant on pre-prod (truenas) for a clean test — drop the per-tenant Postgres schema, delete the tenant's public.* rows, delete the per-user Qdrant collection, then restart faultline-mcp + faultline-redis. Use when asked to clear/reset/wipe the FaultLine database, start a test from a clean slate, or re-provision a tenant from scratch. Destructive — confirms exact targets before dropping.
---

# Clear FaultLine DB (per-tenant wipe)

Wipes ONE tenant back to nothing so the next request re-provisions a fresh schema. This is the live procedure used before an oracle/E2E test run. **Destructive — verify the target user_id and that you're on pre-prod (truenas) before running.**

Four moving parts, in order:
1. **Drop the tenant Postgres schema** `faultline_<uuid_underscores>` (CASCADE) — all facts/entities/staged.
2. **Delete the tenant's `public.*` rows** (confidence gate, intent feedback, provisioning + user registry). `public` is the seed template; only per-user *rows* are deleted, never the template tables.
3. **Delete the per-user Qdrant collection** `faultline-<uuid-dashes>` — never touch `faultline-test` / `faultline-preprod`.
4. **Restart `faultline-mcp` + `faultline-redis`** — the MCP restart clears the in-memory `_provisioned_users` set (CLAUDE.md hard rule); without it, re-provisioning will NOT re-fire after the wipe.

## How to use

1. Open `reference.yaml`. Set `user_id` (defaults to the standard test user).
2. Run `steps` in order — they're copy-paste `ssh truenas` commands.
3. Mind the **gotchas** block: `docker exec` without `-i` silently drops a heredoc into psql (no error, no effect) — always use `-c "..."` statements, never `psql <<SQL`.
4. Run the `verify` block, then (optional) `trigger_fresh_provision` to confirm the schema rebuilds clean with seeds.

## Critical derivations
- Schema name = `faultline_` + user_id with dashes → **underscores**.
- Qdrant collection = `faultline-` + user_id **with dashes** (unchanged).
- DB role `faultline`, database `faultline_test`, schema is the tenant boundary (tenant tables have **no `user_id` column**; only `public.*` does).
