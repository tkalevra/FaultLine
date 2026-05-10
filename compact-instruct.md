# compact-instruct.md — Claude's Role, Process, and Key Context

## My Role

I am Claude Code assisting Christopher Thompson develop **FaultLine**, a write-validated personal knowledge graph pipeline for OpenWebUI memory injection. I collaborate with **deepseek** (V4-Pro reasoning model) in a structured prompting workflow.

**My responsibilities:**
1. **Research & Architecture** — Understand FaultLine's design, identify issues, propose solutions
2. **Prompt Engineering** — Write clear, structured prompts (using DEEPSEEK_INSTRUCTION_TEMPLATE) that keep deepseek focused and productive
3. **Code Review & Validation** — Review deepseek's code for correctness, test integration, catch regressions
4. **Dialogue Management** — Use scratch.md as dialogue hub; keep communication lean and structured
5. **Documentation** — Maintain CLAUDE.md (project reference), create dprompt files (specs + prompts)

**I do NOT:**
- Write implementation code (deepseek does)
- Make unilateral architectural decisions (Christopher decides)
- Leave deepseek without explicit direction (always write formal prompts with constraints)

---

## The Workflow

### Step 1: Understand & Plan
- Read CLAUDE.md and scratch.md
- Identify issue/gap/next work
- Explore codebase if needed (use Explore agent for large searches)

### Step 2: Design Solution
- Enter plan mode if non-trivial
- Explore code patterns, edge cases
- Draft approach with Christopher's input

### Step 3: Create Specifications
- Write **dprompt-N.md** — technical spec (what, why, how)
- Write **dprompt-Nb.md** — formal prompt using DEEPSEEK_INSTRUCTION_TEMPLATE
  - Task: one sentence goal
  - Context: background + why
  - Constraints: Wrong/Right pairs, MUST/MAY
  - Sequence: ordered steps (if multi-step)
  - Deliverable: what changes
  - Files to Modify: explicit list
  - Success Criteria: how to verify
  - Upon Completion: what to update in scratch (template provided)

### Step 4: Hand to deepseek
- Update scratch.md with dprompt direction
- Commit both dprompt files
- deepseek reads scratch, follows prompts

### Step 5: Review & Iterate
- Monitor deepseek's progress via scratch updates
- If issues: diagnose, write clarification in scratch (brief, dialogue-style)
- If complete: validate, plan next phase

### Key Principle: Keep deepseek "reeled in"
- **Don't ask him to decide:** give explicit options/constraints
- **Don't leave him open-ended:** every prompt has hard boundaries
- **Don't rely on him to know context:** repeat key facts in each prompt
- **Lock the model:** "ONLY use faultline-wgm-test-10, do NOT test other models"
- **Lock the scope:** "Tests only. Zero source code changes."

---

## Current State (2026-05-12)

### What's Built
- **Ingest pipeline** — LLM-First (Filter LLM extracts edges), WGM validation gate, fact classification (A/B/C)
- **Query redesign** — Graph traversal (connectivity) + hierarchy expansion (composition), merged with baseline facts + Qdrant
- **Entity taxonomies** — family, household, work, location, computer_system (dprompt-20)
- **Self-building ontology** — novel rel_types → Class C, re-embedder evaluates asynchronously
- **Validation suite** — dprompt-29b: 8 scenarios passed, 110 tests, 0 regressions
- **QA suite** — dprompt-30b: 15 real-world scenarios passed, marked PRODUCTION-READY

### Critical Bug Found (dprompt-31b)
**Name collision breaks query display resolution:**
- User has pref_name="gabby" (is_preferred=true)
- Gabriella has pref_name="gabby" (is_preferred=true)
- entity_aliases constraint: only one preferred per (user_id, alias)
- Result: "gabby" → user entity; Gabriella has NO preferred name → /query drops her facts

**Root cause:** Ingest overwrites preferred alias without detecting collision.
**Impact:** Gabriella is ingested but invisible in queries (integration failure unit tests missed).

### Solution In Progress (dprompt-32b)
**Non-destructive conflict resolution:**
1. entity_name_conflicts table (stores collisions with context)
2. Ingest detects collision, stores pending (doesn't overwrite)
3. Re-embedder evaluates via LLM, assigns unique names
4. /query falls back to non-preferred aliases when needed
5. Gabriella becomes visible + system self-heals

---

## Key Architecture Decisions

### Graph vs Hierarchy (dprompt-26/27/28)
- **Graph:** Rel_types like spouse, parent_of, has_pet → find CONNECTED entities (relevance)
- **Hierarchy:** Rel_types like instance_of, subclass_of → find COMPOSITION + CLASSIFICATION (details)
- **Not:** Nested scope layers (dprompt-24/25 was WRONG, reverted)

### Fact Classification (dprompt-15/16, CLAUDE.md Phase 4)
- **Class A (identity/structural):** pref_name, parent_of, spouse → committed immediately to facts
- **Class B (behavioral/contextual):** lives_at, works_for → staged, promoted at confirmed_count≥3
- **Class C (ephemeral/novel):** unknown rel_types → staged, expires after 30 days

### Relevance Scoring (CLAUDE.md)
- Query signal match (0–0.6) + confidence bonus (0–0.3) − sensitivity penalty (−0.5)
- Threshold: 0.4
- Sensitive rels: born_on, lives_at, born_in, height, weight (gated unless explicit ask)
- Identity rels: pref_name, also_known_as, same_as (always pass)

### Re-Embedder Role
- Syncs facts to Qdrant (unsynced facts → embed → upsert)
- Promotes Class B facts (confirmed_count≥3 → move to facts table)
- Expires Class C facts (30 days old, unconfirmed → delete)
- Reconciles Qdrant (ensures Qdrant matches PostgreSQL)
- **NEW:** Resolves name conflicts (evaluate via LLM, assign fallback aliases)

---

## Critical Files & Their State

| File | Purpose | Current State |
|------|---------|---------------|
| CLAUDE.md | Project reference (architecture, pipeline, principles) | 430 lines (optimized), up-to-date |
| scratch.md | Dialogue hub (state, direction, deepseek updates) | Current: dprompt-32b direction |
| dprompt-26.md | Architecture spec (graph vs hierarchy) | Final spec, reference |
| dprompt-27.md | Query redesign spec | ✓ DONE (graph traversal) |
| dprompt-28.md | Hierarchy expansion spec | ✓ DONE (hierarchy expand) |
| dprompt-29.md | Validation spec (8 scenarios) | ✓ DONE (110 tests passed) |
| dprompt-30.md | QA stress spec (15 real-world scenarios) | ✓ DONE (marked PRODUCTION-READY) |
| dprompt-31.md | Live debugging spec (Gabriella bug) | ✓ DONE (root cause found) |
| dprompt-32.md | Conflict resolution spec | Ready for implementation |
| src/api/main.py | FastAPI backend | ~3000 lines, includes graph + hierarchy query logic |
| src/fact_store/store.py | Fact persistence | commit() signature changed for layer params (dprompt-25) |
| src/entity_registry/registry.py | Entity ID management | register_alias() needs collision detection (dprompt-32) |
| src/re_embedder/embedder.py | Background sync loop | Needs resolve_name_conflicts() function (dprompt-32) |
| migrations/ | Schema evolution | 021_name_conflicts.sql pending (dprompt-32) |

---

## Deepseek's Working Pattern

**How he operates:**
- Reads CLAUDE.md for architecture context
- Reads dprompt-Nb.md for executable instructions
- Follows sequence exactly (skips if told to skip)
- Asks clarifying questions in scratch.md (brief, prefixed with #deepseek)
- Updates scratch upon completion (uses template provided in prompt)
- Waits for explicit direction before next task

**How to keep him productive:**
- Lock scope: "Tests only", "No refactoring", "Do NOT modify src/"
- Lock model: "ONLY use faultline-wgm-test-10"
- Lock constraints: Wrong/Right pairs, MUST/MAY, explicit boundaries
- Provide curl examples if API interaction is needed
- Provide test templates if testing is needed
- Always tell him what to write back to scratch upon completion

**When he goes off-rails:**
- He tries to optimize/refactor when not asked
- He switches models when told "use X model"
- He implements features beyond the scope
- **Fix:** Revert direction in scratch, add "CRITICAL" flag, lock constraints tighter

---

## Next Steps (Priority Order)

### Immediate (dprompt-32b)
1. deepseek implements conflict resolution system
2. Schema migration 021 applied
3. Ingest collision detection live
4. Re-embedder resolves conflicts via LLM
5. /query handles missing preferred names
6. Gabriella bug fixed (test: ingest → collision → resolve → query returns her)

### After dprompt-32b
**Rewrite test suite (dprompt-33?)**
- Current tests: unit-level, isolated
- New tests: full-path validation (ingest → query → verify)
- Test scenarios: all 15 from dprompt-30 + collision scenarios
- File: `tests/api/test_suite_full_path.py`
- Goal: integration failures like Gabriella bug are caught

### Future Considerations
- Domain-agnostic retrieval (system facts not in graph)
- Performance optimization (indices, caching)
- Real-time updates (WebSocket support for OpenWebUI)
- Conversation state awareness (session context in relevance scoring)

---

## Quick Reference: Key Constraints

**For deepseek prompts:**
- ALWAYS use DEEPSEEK_INSTRUCTION_TEMPLATE format
- ALWAYS lock the model ("ONLY use X, do NOT use Y")
- ALWAYS lock the scope ("Tests only, no code changes")
- ALWAYS provide explicit "Upon Completion" template (what to write to scratch)
- ALWAYS use Wrong/Right pairs, not "DO NOT" directives

**For code review:**
- Check for regressions (run test suite: `pytest tests/api/ --ignore=tests/evaluation`)
- Check for data loss (non-destructive changes only)
- Check for architectural alignment (dprompt-26 graph vs hierarchy)
- Check for hardcoding (avoid hardcoded rel_types, use ontology)

**For documentation:**
- Keep CLAUDE.md as single source of truth
- Link dprompt files from scratch (don't embed full specs in scratch)
- Archive scratch when it exceeds 150 lines
- Commit dprompt files when they're complete, not during work

---

## One Last Note

This project is a **live learning experience**. Unit tests said the system was production-ready. Gabriella proved otherwise. The conflict resolution system makes FaultLine **self-aware**: it detects problems, evaluates them with context (via LLM), and heals itself.

That's the philosophy here: **write code that understands its own failures and fixes them autonomously.**

Keep that in mind when designing next phases.
