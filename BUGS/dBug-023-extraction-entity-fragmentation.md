# dBug-023: Extraction Creates Orphaned Entities (Entity Fragmentation)

**Status:** FIXED — Pronoun normalization guard implemented

**Severity:** HIGH — Facts created but unreachable by query pipeline

## Problem

Relationship facts (lives_at, spouse) are stored in database but invisible to /query because the subject entities lack identity anchors (pref_name/also_known_as).

### Evidence

When extraction processes first-person statements like "I live at X", the LLM may output:
- subject: "i" or "me" (literal pronoun, not "user")
- rel_type: "lives_at"
- object: address or location

This causes `/ingest` to create a surrogate UUID for the literal pronoun string via `registry.resolve()`, orphaning the fact since the new entity has no identity anchors.

**Query behavior:** 
```sql
SELECT DISTINCT subject_id FROM facts 
WHERE rel_type IN ('pref_name', 'also_known_as') 
-- Returns entities with identity anchors only
-- Missing: any entities created from first-person pronouns (no pref_name, unreachable)
```

**Result:** Relationship facts exist in database but never surfaced to /query because their subject entities lack identity facts.

## Root Cause

1. `/extract/rewrite` LLM prompt example used "I" as subject: "For 'I live at address...': (I, lives_at, address)"
2. LLM follows the example → outputs `{"subject": "i", ...}` despite general instructions
3. `/ingest` had no pronoun guard → `registry.resolve("i")` creates orphan UUID v5 surrogate
4. Entity registry assigns `entity_id`, but no `pref_name` or `also_known_as` is registered
5. `/query` initial_user_facts query filters by `pref_name/also_known_as` → orphaned entity never included

## Impact

- Relationship facts (lives_at, spouse, works_for, etc.) created but invisible
- User queries for "where do I live?" return incomplete results
- Data fragmentation grows with each extraction
- `/query` graph traversal never reaches facts anchored to orphaned entities

## Solution Implemented

**Three-layer defense in `/ingest` (src/api/main.py):**

1. **Prompt fix** (lines 1680): Added explicit rule:
   ```
   FIRST-PERSON RULE: For first-person statements, ALWAYS use 'user' as the subject — never 'I', 'me', 'my', or 'we'.
   ```

2. **LLM rewrite path normalizer** (lines 2067-2074): Safety net for /extract/rewrite output:
   ```python
   _FIRST_PERSON_PRONOUNS = {"i", "me", "my", "myself", "we", "us", "our", "ourselves"}
   for t in rewrite_data.get("triples", []):
       subj = (t.get("subject") or "").lower().strip()
       if subj in _FIRST_PERSON_PRONOUNS:
           t["subject"] = "user"
   ```

3. **External edges path normalizer** (lines 2135-2141): Catches pronouns from req.edges:
   ```python
   for edge in (req.edges or []):
       subj = (edge.subject or "").lower().strip()
       if subj in _FIRST_PERSON_PRONOUNS:
           edge.subject = "user"
   ```

All three layers ensure pronouns never create orphaned entities:
- Layer 1: LLM should output "user"
- Layer 2: If LLM outputs pronoun from /extract/rewrite, normalize before entity resolution
- Layer 3: If external edge source sends pronoun, normalize before entity resolution

## Files Modified

- `src/api/main.py`
  - Lines 1680: Extraction prompt explicit first-person rule
  - Lines 2012: `_FIRST_PERSON_PRONOUNS` definition
  - Lines 2067-2074: LLM rewrite path normalizer
  - Lines 2135-2141: External edges path normalizer

## Verification

After fix, relationship facts from first-person statements anchor to user's canonical identity entity (the one with pref_name), not an orphaned UUID.

## Related Issues

- dBug-022: Preferred fact exposure (privacy/agency concerns with multi-entity fragments)
- dprompt-88d: Fetch all identity entities (workaround to include orphaned entity facts in query)
