# dBug-032: Entity Scalar Attributes Ingested But Not Returned in /query

**Status:** OPEN (investigation complete, root cause identified)  
**Priority:** P1 (data loss — facts ingested but invisible to user)  
**Date:** 2026-05-16

---

## Problem Statement

When user provialice age information for entities (e.g., "bob is 12 years old, alice is 16 years old"), the ingest pipeline successfully stores the values in `entity_attributes` table, but `/query` endpoint does not return them to the user.

**Observed behavior:**
```
User: "bob is 12 years old. alice is 16 years old."
System response: "I have noted your information..."
/query response: No ages returned for bob or alice
```

**Root cause:** Entity scalar attributes are ingested and stored in `entity_attributes` table, but `/query` endpoint's `_attributes_to_facts()` function returns facts with:
- `subject_id = UUID` (not display name)
- `rel_type = 'age'`
- `object_id = '12'` (string representation)

These facts are then deduplicated against UUID-keyed facts, but because they're not in the `facts` table, they're excluded from the final response.

---

## Evidence

**Ingest log (14:38:48) shows successful storage:**
```
ingest.scalar_stored attribute=age entity=fbb0eca9-709a-5517-b645-595a75ecf866 raw_input=12 user_id=${TEST_USER_ID} value_int=12
ingest.scalar_stored attribute=age entity=2e0d4a79-9e76-5288-adc9-2a0d5e9be7f7 raw_input=16 user_id=${TEST_USER_ID} value_int=16
ingest.class_a_committed count=4
```

**Database verification shows facts NOT created:**
```sql
SELECT subject_id, rel_type, object_id FROM facts 
WHERE subject_id IN ('fbb0eca9-709a-5517-b645-595a75ecf866', '2e0d4a79-9e76-5288-adc9-2a0d5e9be7f7')
AND rel_type = 'age';
-- (0 rows) — facts NOT in facts table, only in entity_attributes
```

**Entity attributes verified:**
```sql
SELECT entity_id, attribute, value_int FROM entity_attributes 
WHERE entity_id IN ('fbb0eca9-709a-5517-b645-595a75ecf866', '2e0d4a79-9e76-5288-adc9-2a0d5e9be7f7')
AND attribute = 'age';
-- Returns: bob age=12, alice age=16 ✓ (successfully stored)
```

---

## Architecture Issue

The problem is inconsistency in scalar attribute storage:

**Current behavior:**
- User-scoped scalars (age, height, etc.) → stored in `entity_attributes` 
- Query path → converts `entity_attributes` to facts with UUID subject_id
- Deduplication → UUID-based dedup doesn't match display-name-based facts
- Result → attributes invisible to user

**Why charlie's age worked (temporary fix):**
- Manually migrated charlie's age from `entity_attributes` to `facts` table
- `/query` now returns it properly because it's in facts table
- But this is not scalable — new ages will have same problem

---

## Solutions

### Option A: Store all scalars as facts (recommended)
- Modify ingest pipeline to create facts table entries instead of entity_attributes for entity scalars
- Benefits: Consistent with other facts, proper deduplication, /query returns immediately
- Trade-off: Deprecates entity_attributes for non-user-anchor scalars

### Option B: Convert entity_attributes to facts in /query
- Modify `_attributes_to_facts()` to create proper facts entries with display-name subject_id
- Benefits: Bac${LOCATION}ard compatible, no schema changes
- Trade-off: Conversion happens at query time (minor perf impact)

### Option C: Fix deduplication to match UUID scalars to display-name facts
- Modify `/query` deduplication logic to recognize UUID-keyed scalar facts
- Benefits: Minimal changes
- Trade-off: Complex dedup logic, doesn't fix root issue

---

## Success Criteria

- ✓ "bob is 12 years old" → fact created in database
- ✓ /query returns bob's age alongside other facts
- ✓ alice's age (16) returns properly
- ✓ Deduplication doesn't create duplicates
- ✓ Scalar storage consistent across all entity types

---

## Files Involved

- `src/api/main.py` — `/ingest` endpoint scalar storage + `/query` _attributes_to_facts()
- Database schema — `entity_attributes` vs `facts` table storage decision

---

## Investigation Notes

- Scalar storage detection: `ingest.scalar_stored` log entries confirm successful ingestion
- Class A commitment: 4 facts committed (including scalars)
- Entity resolution: bob and alice entities resolved correctly
- /query conversion issue: UUID-based scalar facts not reaching user
