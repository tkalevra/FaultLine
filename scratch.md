# scratch.md — FaultLine development dialogue

## INSTRUCTION FOR AGENTS

This file is for **questions and dialogue only**. Do NOT dump code, implementation
plans, or test cases here. Use it to:
- Ask design questions
- Request clarification on requirements
- Confirm decisions before coding
- Preface your entry(s) with your tag in markdown: eg #claude followed by your response/question to allow the human to know who's asking or answering what please.

Code goes directly into source files. This file stays lean.

## Pre-Prod Reference (2026-05-15)

**Instance:** hairbrush.helpdeskpro.ca (truenas)
**Model:** faultline-wgm-test-10
**Backend API:** http://192.168.40.10:8001
**Version:** v1.0.7 (Query deduplication fix, metadata-driven validation, MCP server)

---

## Current Status (2026-05-15)

### Completed This Cycle
- ✓ dprompt-65: Metadata-driven validation framework (zero hardcoded constants)
- ✓ dprompt-66: Query deduplication fix (dBug-008, UUID-based pg_keys)
- ✓ dprompt-67: Documentation audit (CLAUDE.md, README, ABOUT.md synchronized)
- ✓ dprompt-68: MCP server wrapper for FaultLine endpoints (13 tests passing)
- ✓ dprompt-69: Open-ended extraction + RAG fallback (novel rel_types)
- ✓ dBug-010: Novel rel_types silently dropped (fixed line 2378, status check)
- ✓ dBug-012: Incomplete bidirectional relationships (validation + proposal complete)

### Test Suite Status
- 127 tests passing (114 existing + 13 MCP)
- 53 skipped
- 0 failures
- Full regression validation complete

---

## Archive

- **scratch-archive-2026-05-13.md** — May 13 cycle: dprompt-62 execution, dBug-006/007 discovery, database cleanups

---

## 🟢 READY: dprompt-70b (Bidirectional Relationship Fix) — 2026-05-15

**Execution prompt ready:** `dprompt-70b.md` (DEEPSEEK_INSTRUCTION_TEMPLATE format)

**dBug-012 Investigation Summary:**
- ✓ Pre-prod database audited: 3 concrete gaps found
- ✓ Root causes identified: LLM prompt doesn't mandate both directions, ingest doesn't auto-create missing inverses
- ✓ Two-phase fix proposed: extraction prompt + ingest auto-create
- ✓ Formal execution prompt created with MUST/DO NOT/MAY constraints

**Gaps verified:**
- `gabby -child_of-> chris` (1.0) → **MISSING** `chris -parent_of-> gabby`
- `des -parent_of-> chris` (1.0) → should be `des -child_of-> chris` (wrong direction)
- `chris -spouse-> mars` (1.0) → **MISSING** `mars -spouse-> chris` (symmetric)
- `cyrus -child_of-> chris` (1.0) ✓ complete

**Recommendation:** Implement both phases (extract prompt fix + ingest auto-create for resilience)

**Next:** Execute dprompt-70b → implement fix → test → review

---

## ✓ VALIDATION: dBug-009/010 Status — 2026-05-15

**dBug-009 (Health/ephemeral facts not persisting): FIXED ✓**
NOVEL & EPHEMERAL REL_TYPES section in `_TRIPLE_SYSTEM_PROMPT`. Pre-prod validated: `has_injury`, `currently_at`, `is_resting` flow through pipeline.

**dBug-010 (Novel rel_types not staged): FIXED ✓**
Line 2378 now includes `"unknown"` in status check. Pre-prod validated: staged_facts has Class C rows.

**dBug-012 (Incomplete bidirectional relationships): Validation Complete — Fix Proposal Ready**
Database verified: 3 gaps found (Gabby missing parent_of, Des wrong direction, Mars missing spouse inverse). Root causes identified: prompt doesn't mandate bidirectional emission, ingest doesn't auto-create missing inverses. Two-phase fix scoped in BUGS/dBug-012-suggestion.md. Awaiting decision: implement Phase A (prompt), Phase B (ingest auto-create), or both.

---

## ✓ INVESTIGATION & FIX: dprompt-69 Pre-Prod Validation (dBug-010 Root Cause) — 2026-05-15

**Finding:** dprompt-69 extraction works ✓, but novel rel_types silently dropped from ingest pipeline due to routing bug (dBug-010).

**Root Cause:** Line 2378 in `src/api/main.py` — status condition `if status in ("valid", "conflict")` excludes "unknown" → novel rel_types never reach staged_facts despite being classified as Class C.

**Evidence:**
- /ingest returns facts with status="unknown", fact_class="C" ✓
- But committed=0, staged=0 ✗ (facts skipped from rows)
- staged_facts empty for has_injury/currently_at/is_resting ✗
- ontology_evaluations has entries ✓ (WGM working)

**Fix:** 1-line change (commit b6b5d67)
- Line 2378: Add "unknown" to status check
- Result: Novel rel_types now route → Class C staging → re-embedder evaluation

**VALIDATION (Post-Deploy):**
- /ingest response: staged=3 ✓ (was 0 before fix)
- staged_facts query: 3 rows with has_injury/currently_at/is_resting, fact_class='C', confidence=0.4 ✓
- /query response: Health facts injected {has_injury: back, currently_at: chiropractor, is_resting: bed} ✓

**RESULT:** dprompt-69 + dBug-010 fix = Complete success. Novel rel_types now flow through full pipeline.

---

## ✓ DONE: dprompt-69 (Open-Ended Extraction + RAG Fallback) — 2026-05-15

**Task:** Eliminate silent failures — loosen extraction prompt, verify RAG fallback.

**Findings:**
- RAG fallback already existed: `_fire_store_context` at line 1338 caches raw text to Qdrant upfront before extraction
- Extraction prompt was missing encouragement for novel/ephemeral rel_types

**Changes:**
- `openwebui/faultline_tool.py`: Added NOVEL & EPHEMERAL REL_TYPES section to `_TRIPLE_SYSTEM_PROMPT`
  - 6 categories: health/status, ephemeral location, activity/state, transient events, uncertain/exploratory
  - Confidence 0.4, Class C staging, 4 concrete examples
- `tests/filter/test_relevance.py`: 3 new tests (prompt validation, fallback method existence, edge formatting)

**Tests:** 15 passed (12 existing + 3 new), 0 regressions, no commits.

**Next:** User review + manual pre-prod validation.

---

## ✓ DONE: dprompt-68 (MCP Server Wrapper) — 2026-05-14

**Task:** Create MCP server wrapper for FaultLine endpoints.

### Files created + committed to dev

| File | Lines | Purpose |
|------|-------|---------|
| `src/mcp/__init__.py` | 0 | Module init |
| `src/mcp/tools.py` | 175 | 5 tool schemas + validation helpers |
| `src/mcp/server.py` | 244 | MCP stdio server + async API call handlers |
| `mcp_server.py` | 41 | Root entry point (CLI) |
| `tests/mcp/test_server.py` | 314 | 13 tests with mocked FaultLine API |

### Tool coverage

| Tool | Endpoint | Schema | Tests |
|------|----------|--------|-------|
| extract | POST /extract | text, user_id | success + invalid |
| ingest | POST /ingest | text, user_id, edges, source | success + invalid edges |
| query | POST /query | text, user_id, top_k | success + timeout + 500 |
| retract | POST /retract | user_id, subject, rel_type?, old_value?, behavior? | success + invalid subject |
| store_context | POST /store_context | text, user_id | success + invalid |

### Tests: 13 passed, 0 failed ✓

- Schema compliance: 5 tool schemas valid ✓
- Success cases: all 5 tools return correct response ✓
- Error handling: timeout, HTTP 500, invalid input → error dicts ✓
- User_id isolation: different user_ids → different params ✓
- Unknown tool: error returned ✓

### Existing tests: 114 passed, 0 regressions ✓

**Committed:** `ad02200` (server+tools), `cd8f49e` (test suite).

**Next:** User reviews code, approves, then decides on commit/merge strategy.

---

## Current State (2026-05-13 evening)

### Production (GitHub: tkalevra/FaultLine)
- **v1.0.7** — Query deduplication fix — UUID keys (dprompt-66)
- **v1.0.6** — Metadata-driven validation framework (dprompt-65)
- **v1.0.5** — Bidirectional relationship validation (dprompt-62)
- **v1.0.4** — Query deduplication + alias metadata (dprompt-61)

### Active Issues

| Bug | Status |
|-----|--------|
| dBug-report-001 — 008 | Fixed |
| dBug-report-009 | Fixed (dprompt-69 prompt) — validated ✓ |
| dBug-report-010 | Fixed (b6b5d67) — validated ✓ |
| dBug-report-011 | Resolved — /query returns 46 facts, can close ✓ |

### Dev repo

- Branch: `master` (commit `ad02200`)
- Test suite: 127 passed (114 existing + 13 MCP), 53 skipped
- MCP server: `src/mcp/` + `tests/mcp/` committed
- Lines: `src/api/main.py` 4006 + new MCP module

### Architecture
- **Ingest pipeline:** LLM extract → WGM gate → semantic conflict detection → bidirectional validation → fact classification (A/B/C) — all validation now metadata-driven via `rel_types` table
- **Self-healing:** Semantic conflicts + bidirectional impossibilities auto-superseded. Validation scales with dynamic ontology.
- **Zero hardcoded validation rules** — all replaced with `_get_rel_type_metadata()` queries

### Bugs Closed
dBug-report-001 through dBug-report-007 all resolved. One P3 cleanup (dBug-004) scoped for future.

### Dev repo
- Branch: `master` (commit `b34b3be`)
- Test suite: 114 passed, 53 skipped
- Lines: `src/api/main.py` 4006

---

## ✓ DONE: dprompt-65 (Metadata-Driven Validation Framework) — 2026-05-13

**Task:** Replace hardcoded validation rules with metadata-driven framework.

### Changes

**Migration:** `migrations/022_rel_types_metadata.sql`
- Added columns: `is_symmetric`, `inverse_rel_type`, `is_leaf_only`, `is_hierarchy_rel`, `allows_leaf_rels`
- Pre-populated: all existing rel_types with metadata
- Idempotent — uses `IF NOT EXISTS` for columns

**`src/api/main.py`:**
- `_get_rel_type_metadata()` — new helper with module-level cache
- `_detect_semantic_conflicts()` — now queries `is_leaf_only` from metadata instead of hardcoded frozenset
- `_validate_bidirectional_relationships()` — now queries `inverse_rel_type` from metadata instead of hardcoded dict
- Removed: `_HIERARCHY_DEFINING_RELS`, `_LEAF_ONLY_RELS`, `_BIDIRECTIONAL_INVERSES` constants
- Migration 022 executed at startup via `_ensure_schema()`
- Lines: 3992 → 4006 (+14 lines net after removing hardcoded constants + adding metadata logic)

### Philosophy

Validation rules live in `rel_types` table, not code. New rel_types self-describe their validation properties. LLM provides metadata when creating novel rel_types. System scales without new dprompts.

### Validation

- Syntax: clean ✓
- Tests: 114 passed, 0 regressions ✓
- Zero hardcoded validation constants remaining ✓

**Deployment:** v1.0.6 pushed to GitHub (`65d5ef4`). Validated per SOP: syntax clean, docker valid, secrets audit clean, docs updated.

**Status:** AWAITING USER VERIFICATION per PRODUCTION_DEPLOYMENT_GUIDE.md Step 10.

---

## ✓ DONE: dprompt-66 (Query Deduplication Fix — dBug-008) — 2026-05-14

**Task:** Fix `/query` duplicate facts from display-name-based dedup keys.

**Fix (3 lines, `src/api/main.py` 3732–3743):**
- `pg_keys` now built from UUIDs (`_subject_id`/`_object_id`) instead of display names
- Same-entity facts with different aliases (chris/user) now correctly deduplicated
- dprompt-61 final dedup loop unchanged

**Why:** Display names vary by alias ("chris" vs "user" for same UUID), causing separate pg_keys entries. UUIDs are stable — one entry per fact regardless of alias.

**Validation:**
- Syntax: clean ✓
- Tests: 114 passed, 0 regressions ✓

**Pre-prod validation pending:** User must rebuild faultline backend container. Current pre-prod still shows 4 parent_of facts (old code). After rebuild: expect 2 parent_of (des, cyrus only).
- **Deployed:** v1.0.7 (`915b0c8`) ✓ — pre-prod validated, 2 parent_of facts confirmed.


---

## ✓ DONE: dprompt-67 (Documentation Audit & Consistency Sync) — 2026-05-14

**Task:** Audit all documentation across dev and prod repos for v1.0.7 accuracy.

**CLAUDE.md (FaultLine-dev) — Updated:**
- Replaced stale three-tier gating section → simplified filter (dprompt-53b)
- Added Ingest Validation Pipeline (semantic conflicts, bidirectional, metadata-driven)
- Added Query Deduplication section (UUID pg_keys, _aliases metadata)
- Updated pipeline flow diagram, Key Files table, Key Principles
- Removed dBug-report-001 "known bug" reference (fixed v1.0.1)

**Production docs — Verified accurate:**
- ABOUT.md ✓ — v1.0.1 through v1.0.7 entries correct
- README.md ✓ — accurate against v1.0.7
- CHANGELOG.md ✓ — complete release history
- .env.example ✓ — identical across repos

**Commit:** `d4be941` (dev only — prod docs were already accurate)

