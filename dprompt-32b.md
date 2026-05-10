# dprompt-32b — Conflict Resolution System: Non-Destructive Name Collision Handling [PROMPT]

## #deepseek NEXT: dprompt-32b — Entity Name Conflict Resolution System — 2026-05-10

### Task:
Implement self-healing conflict resolution for entity name collisions (e.g., both user and Gabriella have pref_name="gabby"). When collisions occur, store them in a conflicts table and let the re-embedder resolve via LLM context.

### Context:

dprompt-31b revealed a critical production issue: **name collisions break query display resolution.** When both user and Gabriella claim the same preferred name "gabby", only one can be marked preferred in entity_aliases. The other loses all resolvable names → /query can't display her facts.

Current system is brittle: destructive, silent failures, no recovery path.

Required: **non-destructive, self-healing** conflict resolution:
1. Detect collision at ingest (don't overwrite)
2. Store conflict with both entities' context
3. Re-embedder evaluates: "User is Christopher, Gabriella is 10-year-old child. Both 'Gabby'. Disambiguate."
4. LLM resolves: assign unique names, mark resolved
5. Query succeeds with all entities visible

dprompt-32.md contains the full schema, ingest changes, re-embedder logic, and test scenarios.

### Constraints (CRITICAL):

- **Wrong: Overwrite aliases when collision detected (current broken behavior)**
- **Right: Store conflict, let re-embedder resolve with LLM context**
- **MUST: Non-destructive. All names preserved; only preferred status changes.**
- **MUST: Implement in order: schema → ingest changes → re-embedder logic → query fallback**
- **MUST: entity_name_conflicts table with (entity_id_1, entity_id_2, disputed_name) UNIQUE constraint**
- **MAY: Implement LLM resolver with fallback to frequency-based heuristics if time tight**

### Sequence (DO NOT skip or reorder):

1. Read dprompt-32.md spec (all sections)
2. Create `migrations/021_name_conflicts.sql`:
   - entity_name_conflicts table (id, user_id, entity_id_1, entity_name_1, entity_id_2, entity_name_2, disputed_name, status, resolution_method, resolution_detail, resolved_at, created_at, updated_at)
   - UNIQUE(user_id, entity_id_1, entity_id_2, disputed_name)
   - Indexes on (user_id, status) and (created_at DESC)
3. Update `src/entity_registry/registry.py`:
   - Modify `register_alias()` to detect collision: "Is alias already preferred for a DIFFERENT entity?"
   - If yes: INSERT to entity_name_conflicts (pending), INSERT alias as non-preferred
   - If no: proceed normally
4. Update `src/re_embedder/embedder.py`:
   - Add `resolve_name_conflicts(db_conn, user_id)` function
   - Query entity_name_conflicts WHERE status='pending'
   - For each conflict: call `llm_resolve_conflict(entity1_facts, entity2_facts, disputed_name)`
   - LLM returns: {winner: eid, reason: str, fallback: str}
   - Update aliases: winner gets is_preferred=true, loser gets fallback alias (is_preferred=false)
   - Update conflict: status='resolved', resolution_detail=json
   - Integrate into main re-embedder loop (call after unsynced facts cycle)
5. Update `src/api/main.py`:
   - Modify `_resolve_display_names()` to fall back to non-preferred aliases when preferred_name missing
6. Test: Full path integration test (ingest collision → detect → re-embedder resolves → query succeeds)

### Deliverable:

- `migrations/021_name_conflicts.sql` — schema with conflict tracking
- `src/entity_registry/registry.py` — collision detection in `register_alias()`
- `src/re_embedder/embedder.py` — `resolve_name_conflicts()` function with LLM resolver
- `src/api/main.py` — fallback alias resolution in `_resolve_display_names()`
- No data loss; all names preserved with updated preferred flags

### Files to Modify:

- `migrations/021_name_conflicts.sql` — NEW
- `src/entity_registry/registry.py` — collision detection
- `src/re_embedder/embedder.py` — conflict resolution loop + LLM resolver
- `src/api/main.py` — display name fallback

### Success Criteria:

- Collision detection works ✓
- entity_name_conflicts populated on collision ✓
- Re-embedder resolves conflicts via LLM ✓
- Loser entities get fallback aliases (non-preferred) ✓
- Query display resolution uses fallback aliases ✓
- Full path test: Gabriella scenario (ingest → conflict → resolve → query returns her) ✓
- Zero data loss ✓
- Code parses cleanly ✓

### Upon Completion:

**Update scratch.md with this entry (COPY EXACTLY):**
```
## ✓ DONE: dprompt-32b (Conflict Resolution System) — 2026-05-12

- Implemented entity_name_conflicts table + collision detection
- Updated ingest: register_alias() detects collisions, stores pending
- Updated re-embedder: resolve_name_conflicts() evaluates via LLM, assigns fallback aliases
- Updated /query: _resolve_display_names() falls back to non-preferred aliases
- Migration 021 applied
- Full path tested: Gabriella collision detected → LLM resolved → query returns ✓
- No data loss; all names preserved ✓

**System is now self-healing for name collisions.**

Next: Re-write test suite to use full path (end-to-end ingest → conflict → resolve → query cycles) for production-grade validation.
```

Then STOP. Do not propose next work. Wait for direction.

### CRITICAL NOTES:

- **Non-destructive is non-negotiable.** When collision detected, NEVER overwrite existing preferred alias. Store conflict, let re-embedder decide.
- **LLM resolver is the gateway.** This is where the system makes intelligent decisions about entity disambiguation. Make the prompt clear.
- **Fallback alias assignment is critical.** Loser entity MUST get a fallback (e.g., "gabriella" when "gabby" goes to user). Without it, entity remains invisible.
- **This fixes the Gabriella bug AND makes the system production-ready.** Once resolved, the test suite can validate full end-to-end cycles.

### Motivation:

FaultLine just exposed that unit tests don't catch integration failures. Gabriella was "ingested" but invisible in queries because of a silent name collision. This implementation makes the system **self-aware and self-healing**: collisions are detected, logged, evaluated, and resolved autonomously. That's production-ready.
