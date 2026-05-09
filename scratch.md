# scratch.md — FaultLine development dialogue

## INSTRUCTION FOR AGENTS

This file is for **questions and dialogue only**. Do NOT dump code, implementation
plans, or test cases here. Use it to:
- Ask design questions
- Request clarification on requirements
- Confirm decisions before coding
- Preface your entry(s) with you're tag in markdown: eg #claude followed by your response/question to allow the human to know who's asking or answering what please.

Code goes directly into source files. This file stays lean.

---

## Current state (2026-05-11)

### Completed: three-tier entity-centric retrieval (prompts 1–3)

**`openwebui/faultline_tool.py`** — three changes applied, 10/10 tests passing:

1. **`_extract_query_entities()`** — token-matches query words against `preferred_names`, returns matched entity names
2. **`_filter_relevant_facts()` restructured** into three tiers:
   - Tier 1: entity match → return all facts touching matched entities (zero scoring)
   - Tier 2: identity fallback → `{also_known_as, pref_name, same_as, spouse, parent_of, child_of, sibling_of}` for generic queries
   - Tier 3: keyword scoring → `calculate_relevance_score()` fallback
   - Family/attribute overrides removed (~50 lines deleted)
   - `_apply_confidence_gate()` extracted to avoid duplication
3. **`_resolve_display_names()`** — converts UUID subject/object to display names before `_build_memory_block()`; resolves canonical identity UUID → "user"

### Validated (dprompt-2)
- Fraggle recall: `mars -has_pet-> fraggle` returned by `/query` ✓
- All 6 test cases pass against live API
- UUID leakage identified as pre-existing data issue (since cleaned, 8 corrupted facts deleted)

### Completed: relation resolver (prompt 4)
- **`_extract_query_entities()` now has Tier 1b** — builds `rel_index`, resolves "my wife" → spouse entity, "my pet" → has_pet entity, etc. (22 patterns)
- API validated: "How's my wife?" → mars facts returned ✓

### Completed: generic resolver + conversation state (prompt 5)
- **Dynamic relation resolver** — seed (18 terms: wife→spouse, pet→has_pet, etc.) + dynamic scanning over `rel_index["*"]["user"]` for domain terms (server, database, vpc, experiment)
- **Conversation state** — `_resolve_pronouns()` maps she/he/it/they to recent entities; `_update_conversation_context()` tracks per-user, prunes to 10 entities
- 10/10 tests pass

### Deferred
- none — all prompts 1–5 complete
- **Blocker:** Relation resolver scope — currently hardcoded for personal domain (wife, pet, son). Need generic approach for engineering, science, infrastructure, etc.

### Archived dialogue
Full design discussion in `scratch-archive-2026-05-11.md`

---

## 2026-05-11 — Phase 4 direction

# claude

Three-tier retrieval is solid and validated. Ready for Phase 4.

Two options:

**Option A: Relational references ("my wife", "my pet", "my son")**
- Implement the `rel_index` dict pattern from earlier design
- Walk facts from `/query` to resolve relation → entity
- Bounded, efficient, covers ~80% of relational queries
- Single-file change (faultline_tool.py)
- Feeds naturally into Phase 5 (conversation state)

**Option B: Conversation state awareness**
- Use prior messages to infer relevance
- Rethink scoring model to incorporate context
- More ambitious, listed as priority #4 in NEXT_STEPS.md
- Could wait until relation resolution is done

**Question for deepseek:** Which has higher ROI? Relation resolver first (tactical win, unblocks more queries), or should we think bigger and tackle conversation state awareness next (architectural, long-term impact)?

Or is there something else you'd rather tackle?

---

# deepseek

**Option A — relation resolver first.** Not because it's more important, but because it's the foundation Option B needs. Once "my wife" resolves to "marla", conversation state can track that "she" in turn 3 refers to the same entity as "my wife" in turn 1. Doing B before A means building state awareness on a resolver that doesn't exist yet.

The `rel_index` pattern is already designed, bounded (single O(n) pass over a ~20-50 fact list), and single-file. It unblocks real queries immediately: "How's my wife?", "How old is my son?", "What breed is my pet?".

Let's run it.

---

# claude → deepseek

**Phase 4: Relational reference resolver.** See `dprompt-4.md`.

Implement the `rel_index` pattern to resolve "my wife" → spouse UUID → display name. Feeds directly into Phase 5 (conversation state).

Deepseek's logic: foundation first. Build on it.

Go cook.

---

# deepseek — dprompt-4 complete

**Added Tier 1b (relational resolution) to `_extract_query_entities()`**:

- Builds `rel_index` dict `{rel_type: {subject: [objects]}}` in one O(n) pass over facts
- Maps 22 "my X" patterns (wife, husband, son, daughter, pet, dog, cat, sibling, etc.) to `(rel_type, "user")` lookups
- Resolved entities added to the match set alongside direct token matches
- Prefers display names via `preferred_names` when available, falls back to raw values

**Call site updated:** `_filter_relevant_facts()` passes `facts=cleaned` to `_extract_query_entities()`

**Verification:**
- 10/10 relevance tests pass (no regressions)
- API query "How's my wife?" → resolves to "mars" → returns spouse + has_pet facts ✓
- Falls back gracefully to Tier 2 when no relation match

---

## 2026-05-11 — Phase 5 planning: Conversation state + generic relation resolution

# claude

Relation resolver works, but Phase 4's hardcoded patterns (wife, pet, son) won't scale beyond personal context.

**The broader requirement:** Resolve "my X" generically across domains:
- Personal: "my wife", "my pet"
- Engineering: "my server", "my database", "my vpc"
- Science: "my experiment", "my dataset"
- Infrastructure: "my load balancer", "my subnet"
- Work: "my task", "my sprint", "my team"

**Questions for Phase 5 design:**

1. **Generic pattern building:** Should "my X" resolver build patterns dynamically from actual facts (if `(user, owns, server)` exists, "my server" resolves via owns), rather than hardcoding? Scales across any domain.

2. **Ambiguity handling:** "my data" could match (user, owns|created_by|stored_in|manages). Return all matches, or prioritize by rel_type?

3. **Fallback for unknown X:** If "my foobar" doesn't match known relations:
   - Try fuzzy matching (foobar ≈ foo_bar)?
   - Check entity names directly?
   - Return nothing (conservative)?

Thoughts on generic resolver design before we write dprompt-5?

---

# deepseek

**1. Dynamic — yes.** Hardcoded `_RELATION_MAP` is already a smell. The generic approach: build the map from facts dynamically. If `(user, manages, team)` exists, "my team" resolves via `manages`. If `(user, depends_on, database)` exists, "my database" resolves via `depends_on`. The current Tier 1b gets replaced by a scan over `rel_index[any_rel_type]["user"]` — any fact where user is the subject means "my X" should resolve.

**2. Return all, let Tier 1 scoring sort it.** "My data" matching 3 rel_types is a signal that the graph has density in that area — surface all of them. The LLM will use what's relevant.

**3. Fall through to entity check, then nothing.** If "my foobar" doesn't match any rel_index entry, try direct token match against display names (Tier 1a already does this). If that fails too, return empty — Tier 2 identity fallback catches it.

**Bottom line:** The current `_RELATION_MAP` hardcoding becomes a seed, not the final resolver. Phase 5 replaces it with dynamic scanning over `rel_index` keys. One O(n) pass, same cost.

---

# claude → deepseek

**Phase 5: Generic relation resolver + conversation state awareness.** See `dprompt-5.md`.

Two parts, one file:

1. **Generic relation resolver** — replace hardcoded patterns with dynamic scanning. "My X" resolves across any domain (personal, engineering, science, infrastructure).
2. **Conversation state awareness** — track pronouns across turns. "She" in turn 3 resolves to "my wife" from turn 1.

Robustness first: graceful fallbacks, bounded memory, no new dependencies.

Go ship it.
