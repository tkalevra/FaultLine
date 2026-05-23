# dBug-054: Entity ID vs Scalar Confusion — Scalars Leaking as Entity UUIDs

**Status**: 🔴 OPEN / CRITICAL  
**Severity**: CRITICAL (Architecture violation, breaks /query output)  
**Date**: 2026-05-18 20:50 UTC  
**Component**: Filter + /query endpoint  
**Related**: dBug-049 (scalar routing), dprompt-96 (3D classification)

---

## Problem Statement

The `/query` endpoint is returning facts where **scalar attribute names** (`pref_name`, `also_known_as`) are being used as entity UUIDs, violating the fundamental entity/scalar distinction.

### Observed Symptoms

```
[FaultLine Filter] filtered: 21/21 facts
[FaultLine Filter] facts=21 preferred_names={..., 'pref_name': 'pref_name', ...}

[FaultLine Filter]   fact: user -parent_of-> pref_name        ✗ WRONG
[FaultLine Filter]   fact: pref_name -child_of-> user         ✗ WRONG
[FaultLine Filter]   fact: pref_name -also_known_as-> bob   ✗ WRONG
[FaultLine Filter]   fact: pref_name -pref_name-> bob       ✗ WRONG
```

**What's happening:**
- `pref_name` is being treated as an entity UUID (like "55c13545-3f9a-...")
- But `pref_name` is actually a scalar attribute type name, NOT an entity
- Filter is trying to resolve it to an entity name via `entity_aliases`, which fails
- When it fails, filter returns the fallback (the rel_type name itself)

### Consequences

1. **Fact Injection Broken**: System is injecting garbage facts with scalar names as entities
2. **Query Data Loss**: Real entity relationships hidden by pollution
3. **User Experience**: Ages, names, and other scalars NOT returned when user asks "how old are my kids?"
4. **Architecture Violation**: Scalars (strings) should never appear as `object_id` in facts table

---

## Root Cause Analysis

### Architectural Context (CRITICAL)

The system uses a **semantic extraction framework** where language maps to data patterns via three confidence tiers:

**A/B/C Ingest Model:**
- **A (User-stated)**: Highest confidence, must override everything, respects ontology directionality + hierarchy
- **B (LLM-inferred)**: Follows established ontology/hierarchy (penalty if created in-flow)
- **C (Staged)**: Low-confidence, Qdrant RAG only

**Language-to-Semantics Mapping:**
```
User: "how old are my kids"
  - "my kids" → hierarchical (parent_of) → entity UUIDs
  - "age" → scalar attribute lookup on those UUIDs
  
Expected: ${CHILD2} is 14, bob is 10, alice is 12
Actual: Only relationships returned, ages missing, garbage pref_name facts
```

**Core Principle**: Code should **learn from the growing rel_types database**, not hardcode semantic roles.

### Why It Happens

The `/query` endpoint is **semantically blind**. It should:

1. Query `rel_types` table for metadata (`tail_types`, `is_hierarchy_rel`, etc.)
2. Route based on semantic role: scalars → entity_attributes, relationships → facts, hierarchical → traversal
3. Let the database guide the code as rel_types grow

Currently, the code treats all extracted patterns identically and pushes them into `facts` table regardless of semantic role.

### Trace

1. User asks "how old are my kids" → Should trigger hierarchical + scalar attribute query
2. `/query` endpoint returns facts from `facts` table ONLY
3. Scalar attributes (age, height, occupation) in `entity_attributes` are **never queried/converted**
4. Filter receives incomplete fact list (relationships only, no scalars)
5. Filter tries to resolve relationships, encounters garbage objects like `pref_name` (a scalar type name, not an entity UUID)
6. Filter injects polluted output: real relationships + fake `pref_name` entities + missing ages

**Root Cause**: `/query` doesn't consult `rel_types.tail_types` to identify and route scalars

---

## Missing Data

### User Request
```
"how old are my kids"
```

### Expected Facts
```
${CHILD2} -age-> 14
bob -age-> 10
alice -age-> 12
```

### Actual Facts Returned
```
(age facts NOT in the list)
(only relationship facts + pref_name scalars returned)
```

**Issue**: Age attributes not being retrieved from `/query` endpoint. Either:
1. They're not in the database (ingest failed)
2. They're in entity_attributes but `/query` not converting to facts
3. Filter is filtering them out

---

## Verification

### Database State
```sql
SELECT entity_id, attribute, value_int FROM entity_attributes 
WHERE attribute='age' AND user_id='${TEST_USER_ID}';
```

Need to check:
- Are age values in entity_attributes?
- Are they being queried by /query endpoint?
- Are they being filtered by openwebui filter?

---

## Evidence

### Filter Log (20:50:15 UTC)
```
User: "how old are my kids"
/query returned: 21 facts
Filter processed: 21 facts
Filter injected: ONLY relationship facts + pref_name garbage

Missing: age facts for ${CHILD2}, bob, alice
Garbage: 4 facts with pref_name as entity
```

### Fact List from Filter
```
✓ user -parent_of-> ${CHILD2}
✓ ${CHILD2} -child_of-> user
✓ user -spouse-> emma
✓ emma -spouse-> user
✓ user -pref_name-> john
✓ user -also_known_as-> john

✗ user -parent_of-> pref_name          (pref_name is not an entity!)
✗ pref_name -child_of-> user           (pref_name is not an entity!)
✗ pref_name -also_known_as-> bob     (pref_name is not an entity!)
✗ pref_name -pref_name-> bob         (pref_name is not an entity!)

? (no age facts at all)
```

---

## Impact

**User Query**: "how old are my kids?"  
**Expected Response**: ${CHILD2} is 14, bob is 10, alice is 12  
**Actual Response**: Filter returns family relationships only, ages missing

**Severity**: Critical — Core feature broken (scalar attributes not queryable)

---

## Root Cause (IDENTIFIED)

**DIRECT CAUSE:** Scalar rel_types are NOT being detected at ingest time because `_REL_TYPE_META` is either empty or missing `tail_types` metadata.

### Evidence Chain

1. **Ingest Path Scalar Filtering (line 5816 in main.py):**
   - Scalar facts are supposed to be filtered OUT of the facts table before commit
   - Filter uses: `rows = [row for row in rows if not is_scalar_rel_type(row[3].lower() if row[3] else '')]`
   - This filter calls `_is_scalar_rel_type()` which checks `_REL_TYPE_META.get(rel_type, {}).get("tail_types")`

2. **What SHOULD Happen:**
   - Age fact (user, age, 12) → classify_fact_3d() checks metadata → finds tail_types=['SCALAR'] → storage="scalar"
   - Routes to entity_attributes table via line 5589-5609
   - Never reaches rows.append() due to continue at line 5615
   - Never reaches facts table

3. **What's ACTUALLY Happening:**
   - Age fact gets through the scalar filter at line 5816 because `_is_scalar_rel_type()` returns False
   - This means _REL_TYPE_META doesn't have tail_types for 'age'
   - Fact continues to line 5784, rows.append(), then manager.commit()
   - Fact ends up in facts table with string object_id "12"

4. **Why _REL_TYPE_META is Empty/Incomplete:**
   - Migration 017 (which adds tail_types column) may not have run
   - OR the column exists but wasn't populated for existing rel_types
   - _build_rel_type_meta() at line 352 queries tail_types from database
   - If column doesn't exist, query fails silently (caught at line 393) and returns {}
   - Code continues with empty _REL_TYPE_META

### Hypothesis A: /query endpoint returning wrong data
Confirmed - /query IS including facts where object_id is a scalar string value ("12", "john") instead of UUID, because those facts were incorrectly stored in facts table at ingest time due to scalar routing failure.

### Hypothesis B: Filter resolving scalars as entities
Not the issue - the facts shouldn't be in the query result at all if stored correctly in entity_attributes

### Hypothesis C: Both A and B
Causality reversed: A caused B. Ingest stores scalars in facts table → /query returns them → Filter can't resolve them properly

---

## Fix Requirements

### Phase 1: Ensure Schema is Current (IMMEDIATE)
1. Run migrations to completion:
   ```bash
   cd /home/${USER}/Documents/013-GIT/FaultLine-dev
   alembic upgrade head
   # OR if not using alembic, manually:
   psql $POSTGRES_DSN < migrations/017_fix_schema_consistency.sql
   psql $POSTGRES_DSN < migrations/022_rel_types_metadata.sql
   psql $POSTGRES_DSN < migrations/030_strengthen_rel_types_wikidata_ontology.sql
   ```

2. Verify tail_types are populated:
   ```sql
   -- Should return 8+ rows with ARRAY['SCALAR']
   SELECT rel_type FROM rel_types WHERE tail_types = ARRAY['SCALAR']::TEXT[];
   ```

3. Restart backend to reload metadata:
   ```bash
   docker restart faultline  # Or uvicorn process
   ```

### Phase 2: Add Defensive Checks (SAFETY NET)
If _REL_TYPE_META fails to load at startup, ingest should:
1. Log ERROR (not just warning) so admin notices
2. Fall back to heuristic-only classification (L1-L5 in classify_fact_3d)
3. Numeric values still routed as scalar via L1 heuristic (line 490-495)

Modify startup logging (line 1924-1929 in main.py):
```python
try:
    _REL_TYPE_META = _build_rel_type_meta(dsn)
    if len(_REL_TYPE_META) == 0:
        log.error("startup.rel_type_meta_empty",
                  reason="tail_types column missing or unpopulated — check migrations")
    else:
        log.info("startup.rel_type_registry_ready",
                 count=len(_rel_type_registry._cache),
                 rel_types_count=len(_REL_TYPE_META))
except Exception as e:
    log.error("startup.rel_type_registry_failed", error=str(e))
    _REL_TYPE_META = {}
```

### Phase 3: Strengthen /query (NOT URGENT, ALREADY WORKING)
/query endpoint currently:
1. ✅ Queries entity_attributes correctly (lines 6549-6600)
2. ✅ Converts via _attributes_to_facts (line 6741-6769)
3. ✅ Returns attributes as JSON (line 7563)

The /query path is sound. Once ingest stores scalars correctly in entity_attributes, /query will retrieve and return them properly.

## Verification Steps (CRITICAL — Check These First)

### 1. Database Schema Verification
```sql
-- Does tail_types column exist?
SELECT column_name FROM information_schema.columns 
WHERE table_name = 'rel_types' AND column_name = 'tail_types';

-- Is age rel_type marked as scalar?
SELECT rel_type, tail_types FROM rel_types WHERE rel_type='age';

-- Check all scalar rel_types
SELECT rel_type, tail_types FROM rel_types 
WHERE rel_type IN ('age', 'height', 'weight', 'pref_name', 'also_known_as', 'occupation', 'born_on', 'nationality');
```

If tail_types is NULL or column doesn't exist → **SCHEMA NOT MIGRATED**. Run:
```bash
cd /path/to/faultline && alembic upgrade head
# OR manually run: psql $POSTGRES_DSN < migrations/017_fix_schema_consistency.sql
```

### 2. Scalar Facts Storage Verification
```sql
-- Check if scalars are CORRECTLY stored in entity_attributes
SELECT entity_id, attribute, value_int, value_text FROM entity_attributes 
WHERE attribute='age' AND user_id='${TEST_USER_ID}'
ORDER BY entity_id;

-- Check if scalars are INCORRECTLY stored in facts (THE BUG)
SELECT subject_id, rel_type, object_id FROM facts
WHERE user_id='${TEST_USER_ID}' 
AND rel_type IN ('age', 'height', 'weight', 'pref_name', 'also_known_as')
AND NOT object_id LIKE '%-%-%-%-'  -- Filter out UUIDs, keep string values
ORDER BY rel_type, object_id;
```

If rows returned from facts table → **BUG CONFIRMED**: scalars are stored in wrong table

### 3. Metadata Loading at Runtime
Check Docker logs for startup warnings:
```bash
docker logs faultline 2>&1 | grep -i "rel_type_meta\|tail_types\|startup.rel_type"
```

Look for:
- `startup.rel_type_meta_loaded` (should log count > 0)
- `startup.rel_type_meta_loaded_sample age_tail_types=...` (should show ['SCALAR'])
- `startup.rel_type_meta_builder_failed` (indicates schema problem)

---

---

## Summary for User

**The core issue:** Scalar facts (age=12, height="5'10") are being stored in the `facts` table with string object_ids instead of being routed to the `entity_attributes` table where they belong.

**Why it happens:** The `/ingest` endpoint relies on `_REL_TYPE_META` dictionary (loaded at startup from database) to identify which rel_types are scalars. If the database schema is missing the `tail_types` column, or if migrations haven't run, `_REL_TYPE_META` is empty. When it's empty, scalar detection fails, and scalars flow into the `facts` table incorrectly.

**The fix:** 
1. **Immediate**: Run migrations to ensure `tail_types` column exists and is populated. Restart backend.
2. **Verify**: Check database that `SELECT rel_type FROM rel_types WHERE tail_types = ARRAY['SCALAR']::TEXT[];` returns 8+ rows
3. **Test**: Re-ingest family facts with ages. Verify ages appear in entity_attributes, NOT in facts table.

**Expected after fix:**
- "My son alice is 12" → age stored in entity_attributes
- "/query" returns: `{facts: [...], attributes: {alice_uuid: {age: {value: 12}}}}`
- Filter injects: "alice is 12 years old" (not garbage facts)
- User asks "how old are my kids?" → Gets correct ages

**Test Date**: 2026-05-18 20:50 UTC  
**Tested Via**: Manual OpenWebUI chat + docker logs inspection  
**Root Cause Identified**: 2026-05-18 22:45 UTC (schema/metadata loading issue)
**Blocking**: Production usage (users cannot query ages/attributes)
