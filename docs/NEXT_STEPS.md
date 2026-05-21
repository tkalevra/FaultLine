# Next Steps — 2026-05-14

## Current State

**Deployed:**
- dBug-018: Context-enriched /extract (dprompt-80/81) ✓ Production
- dBug-019: Hierarchy facts in /query (dprompt-82) ✓ Production
- dBug-022: Semantic definitions in facts (dprompt-85) ✓ Implementation complete
- Tests: 141 passed, 0 regressions

**In Progress (Critical):**
- **dprompt-86: Remove Taxonomy Seeding Constraints** ✓ COMPLETE
  - Issue: Address extraction failed because (user, lives_at, address) was rejected
  - Root cause: Hardcoded taxonomy seeding + is_leaf_only constraint too restrictive
  - Solution: Removed _CORE_TAXONOMIES seeding, removed is_leaf_only constraint
  - Status: READY FOR TESTING
  - Next: Rebuild container, test address extraction with "I live at 156 Cedar Street S"

**Discovered (previous session):**
- Temporal medical facts not being extracted/stored
- Example: "I hurt my back last **Sunday**" → extracts has_injury but loses the date
- Recommendation: Option A (pre-seed medical temporal rel_types)

---

## dprompt-86: Taxonomy Seeding Fix

### Problem
- User extracted "I live at 156 Cedar Street S, Kitchener, ON"
- LLM produced 2 triples: (address, instance_of, location) + (user, lives_at, address)
- Only 1 committed to database (the lives_at triple was rejected)
- Root cause: Semantic conflict detector saw address as TYPE (due to instance_of), and lives_at is_leaf_only=TRUE prevented leaf rels on types

### Why It Happened
Hardcoded taxonomy seeding created two problems:
1. **Brittle references:** body_parts taxonomy referenced but never seeded, causing extract context building to fail silently
2. **Over-strict validation:** is_leaf_only constraint prevented legitimate facts where typed entities (like addresses) can still be objects of leaf relationships

### Solution
Removed both constraints:
- Deleted _CORE_TAXONOMIES seeding (family, household, work, location, computer_system)
- Removed is_leaf_only=TRUE UPDATE statement
- Removed body_parts taxonomy references in extract context and null resolution

### Architecture Principle
Backend graph traversal + LLM are authoritative. System should self-build ontology from data, not enforce hardcoded taxonomy constraints at ingest time. Taxonomies table remains available for data-driven population and query-time filtering, but seeding is gone.

### Verification
After rebuild: Test "I live at 156 Cedar Street S, Kitchener, ON" extraction. Both triples should commit.

### Debug Logging (dprompt-88b)
Added comprehensive logging to /query endpoint:
- `query.initial_user_facts`: Shows all facts where user is subject/object after first fetch
- `query.connected_entity_facts`: Shows facts retrieved for each connected entity via graph traversal
- `query.facts_summary`: Shows final rel_types being returned in response

This logging will help identify if lives_at facts are:
- Extracted correctly (captured in initial_user_facts)
- Properly added during graph traversal (shown in connected_entity_facts)
- Present in final response (shown in facts_summary)

Test steps:
1. Rebuild container (migrations will update is_leaf_only=FALSE)
2. Submit "I live at 156 Cedar Street S, Kitchener, ON" to OpenWebUI
3. Check logs for query.initial_user_facts — should show lives_at if extraction+ingest worked
4. Query "where do I live?" and check logs for rel_types returned
5. If lives_at still missing, logs will show where it was filtered out

---

## Root Cause Analysis

### What Happens Now

1. LLM sees "I hurt my back last Sunday" — detects temporal + medical context
2. LLM has `has_injury` in prompt, but it's non-temporal (object = body_part/condition, not date)
3. LLM recognizes the gap and **creates** `injury_on` (temporal rel_type, object = date)
4. Re_embedder evaluates `injury_on`:
   - Semantic similarity to `has_injury`: 0.897 (high)
   - Maps it: `injury_on` → `has_injury`
5. Temporal structure lost: date discarded, fact becomes `(user, has_injury, <nothing>)`

### The Problem

**Mapping is too blunt.** It uses cosine similarity without checking if object types are compatible:
- `injury_on`: expects DATE object
- `has_injury`: expects CONCEPT/BODYPART object
- Mapping destroys the temporal structure

### Why It Matters

- Medical timeline lost ("when did you get injured?" can't be answered)
- User context degraded (LLM doesn't know injury timing)
- Self-building ontology is *working correctly* — LLM is creating what's needed, system is just mangling it

---

## Options for Tomorrow

### Option A: Pre-seed Medical Temporal Rel_types
**Approach:** Add to rel_types table + _TRIPLE_SYSTEM_PROMPT:
- `injury_on` (Person → Date)
- `hurt_on` (Person → Date)
- `symptom_onset_on` (Person → Date)
- `treatment_started_on` (Person → Date)

**Pro:** Simple, deterministic, LLM uses them directly (no mapping)
**Con:** Requires knowing medical temporal rel_types in advance

**Effort:** Low (migration + prompt update)

### Option B: Smart Mapping (Type-Aware)
**Approach:** Enhance re_embedder mapping logic to check object type compatibility:
```python
if novel_rel_type.tail_types != existing_rel_type.tail_types:
    reject_mapping()  # Don't map if object types incompatible
    approve_novel_rel_type()  # Let it through instead
```

**Pro:** Respects LLM's semantic decisions, handles future novelty
**Con:** Complex, novel rel_types accumulate, needs approval policy

**Effort:** Medium (re_embedder changes + approval workflow)

### Option C: Hybrid
**Approach:** Pre-seed common medical temporal rel_types (Option A) + implement smart mapping (Option B) for truly novel cases

**Pro:** Covers known cases + future-proof
**Con:** More code, more complexity

**Effort:** Medium-High

---

## Recommended: Option A (Pre-seed)

**Why:**
- Medical temporal facts are predictable (injury, symptom, treatment)
- Simple, deterministic, no approval bottleneck
- Immediate fix (LLM sees types in prompt, uses them)
- Can upgrade to Option B later if needed

**Implementation:**
1. Create migration 027: add medical temporal rel_types to rel_types table
   - `injury_on` (Person → SCALAR date)
   - `hurt_on` (Person → SCALAR date)
   - `symptom_onset_on` (Person → SCALAR date)
   - `treatment_started_on` (Person → SCALAR date)
   - Wikidata PIDs if available, source='builtin'

2. Update _TRIPLE_SYSTEM_PROMPT in faultline_tool.py:
   - Add medical temporal examples under "DATES AND EVENTS"
   - "I hurt my back last Sunday" → emit (user, hurt_on, "last sunday") + (user, has_injury, back)

3. Test:
   - "I hurt my back last Sunday" → extracts both temporal + relationship
   - /query returns (user, hurt_on, "last sunday") + (user, has_injury, back)
   - OpenWebUI memory injection includes: "hurt_on: last sunday"

4. Verify no regressions (141+ tests pass)

---

## Files to Modify

- `migrations/027_medical_temporal_rel_types.sql` (new)
- `openwebui/faultline_tool.py` — Update _TRIPLE_SYSTEM_PROMPT DATES AND EVENTS section
- `scratch.md` — Log task assignment

---

## Priority Stack

### 🔴 CRITICAL — dBug-020/021 (Family Facts Visibility)

**dprompt-85 — Extract Hierarchy + Relational with Semantic Definitions**

**What:** Family facts ("We have three children") extracted, stored in PostgreSQL, but invisible because:
1. Hierarchy facts missing (instance_of, member_of, same_as not extracted)
2. Filter cannot match "we" (UUID d010884b...) to user (UUID 10d7d879...)

**Solution:** Enhance /extract/rewrite to extract BOTH hierarchy + relational using typed_entities. Inject semantic definitions for relationship types at fact creation time. Definitions enable downstream models to understand hierarchy (vertical layers: Kitchener ⊂ Ontario ⊂ Canada) vs relational (horizontal connections: User parent_of Gabriella) without confusion.

**Implementation:** See dprompt-85.md
- /extract/rewrite prompt: Add hierarchy extraction instructions + typed_entity usage
- /ingest: Accept and store `rel_type_definition` on each fact
- /query: Return facts WITH definitions in JSON
- migrations/028: Add `rel_type_definition` column to facts table

**HARD CONSTRAINT:** No hard-coded regex, string lists, or pronoun patterns. All pronoun/collective resolution via typed_entities + LLM + graph traversal (data-driven, not brittle).

**Assigned to:** deepseek  
**DO NOT:** Rebuild container, redeploy Filter (user handles)

---

### 🟡 MEDIUM — Medical Temporal Facts (dBug-?? — Temporal Medical Context)

**dprompt-83 (pending) — Pre-seed Medical Temporal Rel_types**

**What:** "I hurt my back last Sunday" extracts has_injury but loses the date. Re-embedder maps novel `injury_on` → `has_injury`, destroying temporal structure.

**Solution (Option A — Recommended):** Pre-seed medical temporal rel_types (injury_on, hurt_on, symptom_onset_on, treatment_started_on) so LLM uses them directly.

**Status:** Design complete, awaiting implementation after dBug-020/021 fixes

---

## Key Insight (Family Facts Resolution)

The LLM receives typed_entities from GLiNER2 but /extract/rewrite prompt doesn't instruct extraction of hierarchy facts. Infrastructure exists (member_of rel_type, household/family taxonomies, _hierarchy_expand()), but extraction pipeline doesn't populate it. Solution: relationship type **definitions** injected at creation time enable reliable semantic disambiguation without manual encoding.

This bridges the gap between two orthogonal systems:
- **Graph (Relational):** who am I connected to? (parent_of, spouse, works_for)
- **Hierarchy (Classification):** what am I, what do I belong to? (instance_of, member_of, subclass_of)

Extraction must populate BOTH. Definitions ensure downstream models understand the distinction.
