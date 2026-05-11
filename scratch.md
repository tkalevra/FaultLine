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

## #deepseek: EXECUTE dprompt-65b NOW

#claude: Next phase ready. Read dprompt-65b.md and execute metadata-driven validation framework.

**Clear Scope & Expectations:**

1. **Migration (NEW file):** `migrations/0XX_rel_types_metadata.sql`
   - Add columns: is_symmetric, inverse_rel_type, is_leaf_only, is_hierarchy_rel, allows_leaf_rels
   - Pre-populate ALL existing rel_types with metadata
   - Run BEFORE validation code changes

2. **Refactor src/api/main.py:** (~150–200 lines)
   - Add `_get_rel_type_metadata()` helper with caching
   - Replace hardcoded rules in `_detect_semantic_conflicts()` with metadata queries
   - Replace hardcoded rules in `_validate_bidirectional_relationships()` with metadata queries
   - NO hardcoded validation rules remaining in code
   - Applies uniformly to facts + staged_facts

3. **Tests:** 4 new test cases in tests/api/test_ingest.py
   - Leaf-only via metadata
   - Symmetric rel_type bidirectional
   - Bidirectional prevention via metadata
   - Novel rel_type self-describes (no code change)

4. **Local testing:** pytest, 114+ pass, 0 regressions

5. **STOP:** Update scratch.md, do NOT deploy

**Why this approach prevents debt:**
- New rel_types created by LLM → self-describe validation
- No new dprompt needed for each edge case
- Framework scales forever with ontology
- Zero technical debt accumulation

Read dprompt-65b.md, follow sequence exactly. STOP on completion.

---
