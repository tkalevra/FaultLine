# dprompt-34 — Pre-Production Validation: Full-Path Test Suite Against Live Instance

## Purpose

Run the full-path integration test suite (dprompt-33b) against the pre-production instance on truenas with fresh database state. Validate that all 23 scenarios pass, including end-to-end queries like "What's my family" that exercise the complete ingest → collision detection → re-embedder resolution → query pipeline.

## The Task

**Database state:** Wiped (schema dropped and recreated).
**Tests to run:** `tests/api/test_suite_full_path.py` — all 23 scenarios.
**Expected outcome:** All 23 pass, 0 failures.
**Validation:** After test suite passes, manually test natural queries ("What's my family", "Tell me about Gabriella", etc.) via `/query` endpoint to confirm end-to-end pipeline works.

## Prerequisites

- PostgreSQL running on truenas (`faultline-postgres` container healthy)
- Database wiped and ready (already done)
- Access to truenas via SSH (available via `ssh truenas -x "..."`)
- Python test environment available locally

## Connection Details

**Database:** PostgreSQL on truenas faultline-postgres container
- User: `faultline`
- Password: `faultline`
- Database: `faultline_test`
- Direct TCP (5432) not exposed; use SSH tunnel: `ssh -L 5433:localhost:5432 truenas`

**LLM:** Qwen endpoint on truenas (used by re-embedder for conflict resolution)
- Endpoint: `http://truenas:11434/v1/chat/completions` (or via SSH tunnel if needed)

## Test Scenarios (Summary)

**Group A (5):** Family prose, system metadata, fact correction, alias resolution, fact promotion
**Group B (6):** Name collision detection, LLM resolution, Gabriella reproduction, triple collision, scalar facts, re-ingest handling
**Group C (4):** Graph + hierarchy integration, depth with collision, mixed types, transitive discovery
**Group D (4):** Sensitivity gating, novel rel_type handling, confidence variation, entity type propagation
**Group E (4):** 10x duplicate ingest, partial re-ingest, circular relationships, empty query

## Success Criteria

- All 23 full-path scenarios pass ✓
- No connection errors (PostgreSQL tunnel working)
- No regressions from previous test runs ✓
- Gabriella scenario (test 8) passes (collision detected + resolved) ✓
- Manual validation: natural queries work end-to-end ✓

## Files to Verify

| File | Status |
|------|--------|
| `tests/api/test_suite_full_path.py` | Exists, 23 scenarios defined |
| `src/api/main.py` | Latest code from pre-prod (graph, hierarchy, conflict resolution) |
| `src/entity_registry/registry.py` | Latest code (collision detection in `register_alias()`) |
| `src/re_embedder/embedder.py` | Latest code (LLM-powered conflict resolution) |
| `migrations/021_name_conflicts.sql` | Applied (entity_name_conflicts table) |

