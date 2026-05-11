# scratch.md — FaultLine development dialogue

## INSTRUCTION FOR AGENTS

This file is for **questions and dialogue only**. Do NOT dump code, implementation
plans, or test cases here. Use it to:
- Ask design questions
- Request clarification on requirements
- Confirm decisions before coding
- Preface your entry(s) with your tag in markdown: eg #claude followed by your response/question to allow the human to know who's asking or answering what please.

Code goes directly into source files. This file stays lean.

## Pre-Prod Reference (2026-05-13)

**Instance:** hairbrush.helpdeskpro.ca (truenas)
**Model:** faultline-wgm-test-10
**Backend API:** http://192.168.40.10:8001

---

## Archive

- **scratch-archive-2026-05-13.md** — May 13 cycle: dprompt-62 execution, dBug-006/007 discovery, database cleanups

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
| dBug-report-001 — 007 | Fixed |
| dBug-report-008 | Fixed (dprompt-66) — pre-prod validated ✓ |

### Dev repo

- Branch: `master` (commit `1c3614c`)
- Test suite: 114 passed, 53 skipped

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

