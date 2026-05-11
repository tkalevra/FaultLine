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

## Core Architecture Principle: Filter Dumb, Backend Smart

**The Filter does NOT gate.** The backend handles extraction, ontology, hierarchy, and ranking. The Filter trusts backend results and injects them unchanged.

**Query example:** "Where should my son and I go for dinner tomorrow?"
- Backend extracts: user identity, hierarchy (son = child_of), locations, restaurants, temporal context
- Backend ranks by class (A > B > C) + confidence
- Filter injects facts in returned order. Done.

**Why this matters:** Tier 1/2/3 logic, keyword lists, Concept filtering — all brittle. They break when ontology evolves or new rel_types added. Backend-first design scales. See `docs/ARCHITECTURE_QUERY_DESIGN.md`.

---

## Current State (2026-05-12, v1.0.3 Production)

### What's Built
- **Ingest pipeline** — LLM-First (Filter LLM extracts edges), WGM validation gate, fact classification (A/B/C)
- **Query redesign** — Graph traversal (connectivity) + hierarchy expansion (composition), merged with baseline facts + Qdrant
- **Entity taxonomies** — family, household, work, location, computer_system (dprompt-20)
- **Self-building ontology** — novel rel_types → Class C, re-embedder evaluates asynchronously
- **Validation suite** — 114+ tests passing, 0 regressions
- **Production deployment** — v1.0.3 shipped, live on GitHub (PRODUCTION_DEPLOYMENT_GUIDE.md SOP in place)

### Three-Layer Semantic Validation (Deployed)
1. **Layer 1: Extraction Constraint (dprompt-58)** — LLM prompt rule prevents bad facts at source
   - When `instance_of/subclass_of/member_of/part_of` extracted for entity B, do NOT extract owns/has_pet/works_for for B
   - Multi-domain examples: taxonomic (morkie), organizational (engineer), infrastructure (subnet), hardware (cpu), geographical (state), software (module)
   - Prevents ~80% of conflicts at extraction

2. **Layer 2: Semantic Conflict Detection (dprompt-59)** — At ingest time before fact classification
   - Query: Is this entity object of a hierarchy rel?
   - If yes + new fact violates semantics → auto-supersede with reason logged
   - Catches anything extraction constraint missed

3. **Layer 3: Retraction Flow** — User corrections (forget/delete/wrong) auto-supersede conflicting facts
   - Non-destructive: all names preserved, only preferred status changes

### Fixed Bugs
- **dBug-report-001** (dprompt-53b): Tier 2 blocking Tier 3 → removed three-tier gating, Filter now trusts backend
- **dBug-report-002** (dprompt-56b): Weak hierarchy extraction → moved instance_of/subclass_of to primary extraction
- **dBug-report-003** (dprompt-58): Conflicting fact extraction (morkie ownership) → extraction constraint
- **dBug-report-004** (noted): Stale data cleanup → pinned for retraction flow enhancement
- **dBug-report-005** (dprompt-61 design): Alias redundancy in query results → strategy: filter by is_preferred, deduplicate by entity_id, enrich with alias metadata

### Pending Work
- **dprompt-61** (Query deduplication): Filter query results by is_preferred=true, deduplicate by entity_id, return enriched facts with alias metadata
- **dprompt-62** (Extraction semantic validation): Prevent bidirectional impossible relationships (e.g., entity can't be both child and parent of same entity)
- **dprompt-63** (Name conflict resolution enhancement): Merge mars/marla as potential duplicates

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
| CLAUDE.md | Project reference (architecture, pipeline, principles) | 430+ lines, up-to-date with v1.0.3 |
| scratch.md | Dialogue hub (state, direction, deepseek updates) | Current: tracking dprompt-61 design work |
| dprompt-template.md | Reusable dprompt specification template | Final structure, reference |
| dprompt-templateb.md | DEEPSEEK_INSTRUCTION_TEMPLATE format | Final format, enforced (Task/Context/Constraints/Sequence/Deliverable/Files/Criteria/Upon Completion) |
| PRODUCTION_DEPLOYMENT_GUIDE.md | SOP for production deployments | Final (file paths, commands, sequence, examples, validation) |
| dprompt-56b.md | Hierarchy extraction (multi-domain) | ✓ DEPLOYED (v1.0.2) |
| dprompt-58.md | Extraction constraint (prevent bad facts) | ✓ DEPLOYED (v1.0.2) |
| dprompt-59b.md | Semantic conflict detection at ingest | ✓ DEPLOYED (v1.0.3) |
| dprompt-60.md | Documentation review + production deployment | Specification ready, awaits execution |
| dprompt-61.md | Query deduplication using is_preferred | Design complete, awaits formal prompt |
| reddit-post-ready.md | Copy-paste-ready Reddit post | Final, community-ready |
| src/api/main.py | FastAPI backend | ~3600 lines, includes _detect_semantic_conflicts(), graph + hierarchy query logic |
| openwebui/faultline_tool.py | OpenWebUI Filter | 1594 lines, simplified post-dprompt-53b, hierarchy constraint added (dprompt-58) |
| src/entity_registry/registry.py | Entity ID management | includes get_any_alias() for non-preferred fallback |
| migrations/ | Schema evolution | Current (entity_taxonomies, name_conflicts, staged_facts tables) |

---

## DEEPSEEK_INSTRUCTION_TEMPLATE Format (Enforced Standard)

**Structure (mandatory for all dprompt-Nb.md files):**
```
# dprompt-NB: [Title] — [Execution Model]

## Task
[One-sentence goal]

## Context
[Background, why, impact]

## Constraints
### MUST:
- [Hard requirement 1]
- [Hard requirement 2]

### DO NOT:
- [Explicit prohibition 1]

### MAY:
- [Optional flexibility]

## Sequence
[Ordered steps, no skips — detailed]

## Deliverable
[What changes]

## Files to Modify
[Explicit list with locations]

## Success Criteria
[Verification checklist]

## Upon Completion
[Template for scratch.md update — copy-paste ready]
```

**Why enforced:** Keeps deepseek focused, prevents scope creep, enables reuse across work streams.

---

## Deepseek's Working Pattern

**How he operates:**
- Reads CLAUDE.md for architecture context
- Reads dprompt-Nb.md (DEEPSEEK_INSTRUCTION_TEMPLATE format) for executable instructions
- Follows sequence exactly (no skips, no creativity)
- Asks clarifying questions in scratch.md (brief, prefixed with #deepseek)
- Updates scratch upon completion (copy-paste template provided in prompt)
- Waits for explicit direction before next task

**Work environment boundaries (CRITICAL):**
- **Investigation:** Pre-Prod Only (via SSH: `ssh truenas -x "sudo docker logs [container]"`)
- **Code modifications:** FaultLine-dev Only (`/home/chris/Documents/013-GIT/FaultLine-dev/`)
- **Deployment:** User-triggered (waits for STOP clause, then user rebuilds pre-prod container)
- **Test:** Local test suite first (`pytest tests/ --ignore=tests/evaluation`), live validation after user rebuild
- **Production SOP:** Follow PRODUCTION_DEPLOYMENT_GUIDE.md exactly (identify files, audit secrets, copy, sanitize, validate, commit/tag/push)

**How to keep him productive:**
- Lock scope: "Tests only", "No refactoring", "Do NOT modify src/"
- Lock model: "ONLY use faultline-wgm-test-10"
- Lock constraints: Wrong/Right pairs, MUST/MAY, explicit boundaries
- Lock locations: "Investigation pre-prod only", "Code changes FaultLine-dev only"
- Provide curl examples if API interaction is needed
- Provide test templates if testing is needed
- Always include STOP clause for rebuild/redeploy decisions
- Always tell him what to write back to scratch upon completion

**When he goes off-rails:**
- He tries to optimize/refactor when not asked
- He switches models when told "use X model"
- He implements features beyond the scope
- He tries to deploy to pre-prod directly
- **Fix:** Revert direction in scratch, add "CRITICAL" flag, lock constraints tighter, emphasize boundaries

---

## Next Steps (Priority Order)

### Completed (v1.0.3 shipped)
- ✓ dprompt-53b: Filter simplification (removed Tier 1/2/3 gating)
- ✓ dprompt-56b: Multi-domain hierarchy extraction enhancement
- ✓ dprompt-58: Extraction constraint (prevent bad facts at source)
- ✓ dprompt-59b: Semantic conflict detection at ingest (auto-supersede)
- ✓ Reddit post: Community announcement ready
- ✓ Three-layer semantic validation: fully deployed

### Immediate (Following v1.0.3)
**dprompt-61: Query Deduplication (dBug-report-005 fix)**

Strategy: Use is_preferred flag instead of merging. Return single fact per relationship, enriched with alias metadata.

1. **Filter query results** by `is_preferred=true` aliases only
2. **Deduplicate** facts by entity_id (keep one fact per subject-object-reltype triple)
3. **Enrich facts** with `_aliases` metadata showing all entity names + is_preferred flag
4. **Never expose UUIDs** — always return display names
5. **Respect sensitive data** — don't expose name relationships (mars/marla)

Scope: Modify `/query` response building to use is_preferred aliases and include metadata.

**dprompt-60: Documentation Review & Production Deployment**

1. Review PRODUCTION_DEPLOYMENT_GUIDE.md and docs/ARCHITECTURE_QUERY_DESIGN.md for accuracy against v1.0.3 codebase
2. Copy both to faultline-prod
3. Commit and validate

### Future Considerations
- dprompt-62: Extraction semantic validation (prevent bidirectional impossible relationships)
- dprompt-63: Name conflict resolution enhancement (mars/marla merging)
- Domain-agnostic retrieval (system facts not in graph)
- Performance optimization (indices, caching)
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

This project is a **live learning experience in semantic validation**.

**v1.0.0 learned:** Unit tests claimed production-ready. Gabriella's collision proved otherwise. Lesson: internal testing misses integration failures.

**v1.0.1–v1.0.2 learned:** Extraction is the critical bottleneck. Bad facts at source cascade. Lesson: prevention beats cleanup. Three-layer validation (extraction constraint + conflict detection + retraction) catches problems at all entry points.

**v1.0.3 learned:** Semantic validation requires **graph awareness**. Can't validate facts in isolation. The system must understand structure (hierarchy, composition, relationships) to enforce rules.

**Philosophy:** Write code that:
1. Prevents bad data at write time (extraction constraint, semantic gating)
2. Detects conflicts when they slip through (graph-aware validation)
3. Heals itself when user corrects (retraction flow, non-destructive updates)
4. Understands its own failures and adapts (re-embedder evaluates novel types, resolves collisions via LLM)

**For next phases:** Every new feature (new rel_type, new taxonomy, new extraction pattern) must ask: "How does this integrate with graph traversal, hierarchy expansion, and conflict detection?" If it requires hardcoded gating or brittle rules, it's not ready.

That's the bar: **semantic awareness, not brittle automation.**
