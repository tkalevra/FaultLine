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
- **v1.0.4** — Query deduplication + alias metadata (dprompt-61)
- **v1.0.3** — Semantic conflict detection (dprompt-59)

### Investigation Complete
- dBug-report-006: Staged facts bypass conflict detection
- dBug-report-007: dprompt-62 bidirectional validation incomplete + UUID exposure
- Pre-prod cleaned: impossible relationships removed, staged facts corrected
- User retest: "flawless now"

### Architecture Decision: dprompt-65
User feedback: No technical debt. Metadata-driven validation framework instead of tactical fixes.

**Philosophy:** Validation rules in database, not code. rel_types table stores properties (symmetric, inverse, leaf_only, hierarchy). LLM defines metadata when creating novel rel_types. Validation queries metadata at runtime. Scales with dynamic ontology forever.

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
