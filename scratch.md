# scratch.md — FaultLine development dialogue

## INSTRUCTION FOR AGENTS

This file is for **questions and dialogue only**. Do NOT dump code, implementation
plans, or test cases here. Use it to:
- Ask design questions
- Request clarification on requirements
- Confirm decisions before coding
- Preface your entry(s) with your tag in markdown: eg #claude followed by your response/question to allow the human to know who's asking or answering what please.

Code goes directly into source files. This file stays lean.

**LENGTH RULE:** If this file exceeds 150 lines, archive everything between the
"## Archive" section and the "---" separator as `scratch-archive-YYYY-MM-DD.md`,
then condense the remaining content to a concise current state summary. The
instruction header stays; only the dialogue and state sections get archived.

---

## Archive

- **scratch-archive-2026-05-11.md** — Phases 1–5 (retrieval, relations, conversation state)
- **scratch-archive-2026-05-11-phases6-10.md** — Phases 6–10 (date/time, events table, UUID resolution)
- **scratch-archive-2026-05-11-dprompt15b.md** — dprompt-15b full-circle validation (7 code fixes, 9-cycle results)
- **scratch-archive-2026-05-11-dprompt16-17.md** — dprompt-16/17: preference chain, compound extraction, self-building ontology, filter augment fix

---

# claude

## ✓ DONE: CLAUDE.md Update — 2026-05-10

**Changes:**
- Added "Nested Taxonomy Layers: Query Scope Cascading (dprompt-24)" section with architecture, four layers, hard constraints
- Updated Key Principles to include layer-aware guidance: "Query intent determines entry layer", "Graph traversal vs layer containment are separate concerns", "Entity layer assignment is deterministic"
- Consolidated verbose sections: Entity Type Classification (73 → 7 lines), Entity Type Classification in /ingest (48 → 2 lines), /query Endpoint (27 → 6 lines), Entity ID Normalization (35 → 7 lines), Identity Rel_Type & Alias Registration (55 → 12 lines), Entity ID vs Display Name (80 → 15 lines), Fact Corrections (80 → 8 lines)
- **File size optimization:** 730 → 430 lines (41% reduction)
- References dprompt-24.md for full specification, implementation details, and timeline

---

# deepseek

## ✓ DONE: dprompt-22 (LLM-First Pipeline) — compound.py removed from augment loop, types propagated, CLAUDE.md updated. System is self-building.

---

## ✓ FIXED: UUID/Alias Pattern Bug — 2026-05-12

**Root cause:** SQL LIKE pattern bug. `'%-%-%-%-'` has 4 `%` segments but UUIDs have 5 segments (4 dashes). So ALL UUIDs matched `NOT LIKE '%-%-%-%-'`, causing:
- `_cleanup_entity_aliases_startup` → deletes ALL aliases on every restart (corrupted_count=7, then 4)
- `_normalize_entity_ids_startup` → finds "string" IDs that are actually UUIDs, processes none
- Result: entity_aliases always empty → `preferred_names` empty → Filter drops all UUID-object facts

**Fix:** Changed all 16 occurrences of `'%-%-%-%-'` to `'%-%-%-%-%'` across:
- `src/api/main.py` (6 occurrences — cleanup + normalization queries)
- `migrations/018_normalize_entity_ids.py` (6 occurrences)
- `check_entity_ids.py` (4 occurrences)

**Also fixed:** `/query` preferred_names builder now always includes UUID entities even without aliases (defense-in-depth).

**Deployed & verified:** Spouse fact now returns correctly. "Your family includes Mars" ✓

---

## ❌ REVERT: dprompt-24/25 Code — Wrong Architecture, Start Over

**dprompt-24/25 implemented scope layer model.** WRONG. Throws out and rebuild with graph + hierarchy.

**EXPLICIT INSTRUCTIONS FOR DEEPSEEK:**

1. **REVERT all dprompt-24 code changes:**
   - `src/api/main.py`: Remove `_REL_TYPE_LAYER`, `_detect_layer_intent()`, cascade query logic
   - `migrations/020_nested_layers.sql`: Do NOT run this migration
   - Keep `migrations/020` file (for reference), but mark as OBSOLETE

2. **REVERT all dprompt-25 code changes:**
   - `src/fact_store/store.py`: Revert commit() signature change; remove layer parameter
   - `src/api/main.py`: Revert _commit_staged() changes; remove layer from INSERT statements
   - All layer assignments in ingest: DELETE

3. **DELETE these branches/code entirely:**
   - All layer-related columns: DO NOT ADD TO DATABASE
   - All `_REL_TYPE_LAYER` dict usage: DELETE
   - All `_detect_layer_intent()` function: DELETE
   - All cascade query logic: DELETE

4. **Then proceed (in order):**
   - READ dprompt-26.md (architecture clarification)
   - CODE dprompt-27 (query redesign for graph + hierarchy)
   - CODE dprompt-28 (hierarchy traversal implementation)

**DO NOT leave dprompt-24/25 code in place.** It will conflict with dprompt-27/28. Clean slate required.

---

## #deepseek: STOP. READ dprompt-26.md FIRST.

**Before touching code, understand the correct architecture.**

Graph = connectivity (who I'm connected to). Hierarchy = composition (what they are, what they're part of).

This is NOT nested scope layers. This is two orthogonal traversal systems.

Read dprompt-26. Understand it. THEN code dprompt-27.

---

## ✓ DONE: dprompt-23 (Taxonomy-Driven Query Intent) — replaced hardcoded signal sets with intent → taxonomy → selective fetch. _GENERIC_SELF_REF_SIGNALS for fallback.

---

## ❌ INVALID: dprompt-21 (Pet Ingest Failure — Debug & Fix) — 2026-05-12

**Marked invalid 2026-05-10.** Dprompt-21 approach (add more hardcoded patterns) perpetuates the architectural problem. See dprompt-22 instead.

---

## ✓ DONE: dprompt-20 (Entity Taxonomies Refactor) — migration, ingest integration, query expansion. Core taxonomies: family, household, work, location, computer_system.

---

## Current State (2026-05-12)

### What's built and verified

**Ingest pipeline** — LLM-First architecture (dprompt-22). Filter LLM extracts all relationship types. Types propagated to /ingest via EdgeInput. Null-subject resolution, user-id-to-surrogate mapping, auto-synthesized `pref_name` + `also_known_as` from text patterns. compound.py is legacy.

**Preference chain** — `_extract_preferred_name()` with 8 patterns, Qwen prompt allowing first-person `pref_name`, auto-synthesis with correct `is_preferred_label` assignment.

**LLM-First pipeline (dprompt-22)** — Filter LLM trusted for all relationship types. No regex augment. WGM gate is single validation point. compound.py marked legacy; not in critical path.

**Pronoun guards** — `(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )` on first-person preference patterns across `main.py`, `compound.py`, `faultline_tool.py`. Defensive hardening.

**Stopwords** — `_IDENTITY_STOPWORDS` and `_STOPWORDS` expanded with 15+ words falsely captured as names.

**Self-building ontology (dprompt-17)** — Ingest no longer approves novel rel_types via LLM. Unknown types → Class C + `ontology_evaluations`. Re-embedder evaluates asynchronously (frequency ≥ 3 → approve, cosine similarity > 0.85 → map to existing, else reject). Migration 018 applied.

**Gate hardening** — `ON CONFLICT DO NOTHING` on conflict INSERT in `WGMValidationGate`. Novel types return `"unknown"` instead of calling `_try_approve_novel_type()`.

### Known gaps

- **Pet extraction/ingest**: FIXED by dprompt-22 — LLM-First pipeline removed compound.py bottleneck. LLM `has_pet` edges now flow straight to /ingest with entity types. Taxonomy system (dprompt-20) handles transitive grouping. Needs live server test to verify.
- **Domain-agnostic retrieval**: System facts (subject="system") not reachable via graph traversal unless system entity is linked to user.
- **Birthday relevance scoring**: FIXED 2026-05-10 — `"old"`, `"age"`, `"how old"` added to `_SENSITIVE_TERMS` in filter's `calculate_relevance_score()`.
- **entity_aliases cleanup**: Startup deletes/recreates aliases on restart with pre-existing mixed data. Not a regression.
- **Docker bridge**: Firewalld blocks bridge forwarding on dev host. `docker-compose-dev.yml` uses host networking.

### Test suite

109 passed, 5 skipped, 0 regressions, 1 known gap (aurora retrieval).

### Files changed

| File | Key changes |
|------|-------------|
| `src/api/main.py` | `_extract_preferred_name()`, auto-synthesis, `is_pref` fix, age validation, query signals, `unknown` rel_type handling, dprompt-20: `_load_taxonomies`/`_apply_taxonomy_rules`/`_fetch_transitive_members`/`_TAXONOMY_SIGNALS` |
| `src/wgm/gate.py` | `ON CONFLICT DO NOTHING`, `"unknown"` return for novel types |
| `src/re_embedder/embedder.py` | `evaluate_ontology_candidates()`, cosine similarity, main loop |
| `src/entity_registry/registry.py` | `resolve("user")` surrogate, `_is_valid_uuid()` |
| `src/api/models.py` | EdgeInput + subject_type, object_type Optional fields (dprompt-22) |
| `src/extraction/compound.py` | LEGACY (dprompt-22) — no longer in critical path |
| `openwebui/faultline_tool.py` | LLM-First pipeline (dprompt-22): removed compound augment, types propagated, LLM trusted |
| `migrations/018_ontology_evaluations.sql` | NEW |
| `migrations/019_entity_taxonomies.sql` | NEW — entity_taxonomies table + 5 core taxonomies (dprompt-20) |
| `CLAUDE.md` | Key Principle: "Extraction flows LLM → WGM gate → ingest" (dprompt-22) |
| `tests/api/test_query_compound.py` | NEW |
| `dprompt-20.md` | Spec (reference only) |
| `dprompt-21.md` | Marked INVALID |
| `dprompt-22.md` | Spec (reference only) |
| `migrations/020_nested_layers.sql` | NEW — layer columns + composite indexes (dprompt-24) |
| `src/fact_store/store.py` | commit() handles 7-element tuples with layer (dprompt-25) |
| `dprompt-23.md` | Spec (reference only) |
| `dprompt-24.md` | Spec (reference only) |
| `dprompt-25.md` | Spec (reference only) |

---

# deepseek

## ✓ DONE: dprompt-24/25 Revert — 2026-05-12

**All dprompt-24/25 code reverted from `src/api/main.py` and `src/fact_store/store.py`:**
- Deleted `_REL_TYPE_LAYER` dict, `_MAX_LAYER`, `_classify_entity_layer()`
- Deleted `_detect_layer_intent()`, `_layer_rel_types()`, cascade query logic from /query
- Removed layer column from `_commit_staged()` INSERT
- Removed layer from `FactStoreManager.commit()` INSERT
- Removed Migration 020 block from `_ensure_schema()`
- Removed `layer`/`parent_taxonomy_name` from `_load_taxonomies()` SELECT
- Removed `_entity_layer` assignments in ingest path
- `elif` → `if` fix after cascade removal
- `migrations/020_nested_layers.sql` marked OBSOLETE

**Files reverted:** `src/api/main.py`, `src/fact_store/store.py`
**Files marked:** `migrations/020_nested_layers.sql` (OBSOLETE)
**Syntax check:** Both files parse cleanly (ast.parse OK)

---

## #deepseek NEXT: dprompt-27b → dprompt-28b (Graph + Hierarchy Redesign)

**Two-phase prompt sequence. Read and execute in order. Do NOT skip steps.**

### Phase 1: Graph Traversal
- **Prompt:** `dprompt-27b.md`
- **Spec reference:** `dprompt-27.md`
- **Deliverable:** `_graph_traverse()` function + /query integration
- **Completion:** Update scratch with entry from dprompt-27b.md "Upon Completion" template
- **Blocker:** Must be committed before starting Phase 2

### Phase 2: Hierarchy Expansion
- **Prompt:** `dprompt-28b.md`
- **Spec reference:** `dprompt-28.md`
- **Deliverable:** `_hierarchy_expand()` function + /query integration
- **Completion:** Update scratch with entry from dprompt-28b.md "Upon Completion" template

**Foundation reading (read FIRST, before any coding):**
- `dprompt-26.md` — architecture clarification (graph vs hierarchy)

**Test goal for both phases:** "where do mars and fraggle live?" returns both entities + full hierarchical context
