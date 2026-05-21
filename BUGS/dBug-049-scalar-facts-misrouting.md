# dBug-049: Scalar Facts Misrouted to facts Table Instead of entity_attributes

**Status:** ✅ FIXED  
**Severity:** CRITICAL (Breaks fact storage architecture)  
**Related:** dBug-048, dprompt-96 (three-dimensional classification)  
**Discovered:** 2026-05-18  
**Fixed:** 2026-05-18 via metadata-driven scalar routing (dprompt-102/103)  
**Production Deployed:** 2026-05-18 commit 0437f1a  

---

## Problem Statement

Scalar facts (age, height, occupation, pref_name, etc.) are being stored in the `facts` table as relationship facts with UUID objects, instead of being stored in `entity_attributes` table as scalar values.

**Observable symptoms:**
```
facts table contains:
  subject_id: 6965ba7e-3b7b-5f6c-af21-0d254c2c5484 (bob UUID)
  object_id: caa95289-c8ac-5f01-8c91-cd5ed0cc4927 (a UUID for value "10")
  rel_type: age
  
Should be in entity_attributes instead:
  entity_id: 6965ba7e-3b7b-5f6c-af21-0d254c2c5484
  attribute: age
  value_int: 10
```

**Consequences:**
- Corrections don't work (age updates don't persist because they're stored in wrong table)
- Retractions don't work properly (facts table structure wrong for scalar values)
- Schema violation (scalar rel_types MUST use entity_attributes per CLAUDE.md dprompt-96)
- Startup normalization fails with duplicate key errors

---

## Root Cause

Scalar rel_type facts are being:
1. Extracted by LLM as edges: `(subject, rel_type="age", object="10")`
2. Passed to ingest pipeline as-is
3. Appended to `rows` list for commit to facts table
4. Scalar routing filter (line 4729-4748) catches some, but NOT ALL cases
5. **Missing: Early routing before rows.append() to entity_attributes**

**Why it's happening:**
- LLM produces age edges with object="10" (string value)
- Ingest code resolves object="10" to a UUID (incorrectly)
- UUID object bypasses scalar detection (looks like a relationship fact)
- Fact gets committed to `facts` table instead of `entity_attributes`

**Where scalar detection SHOULD happen:**
- **Before** rows.append() is called
- When processing LLM edge output
- Check rel_type metadata → if SCALAR → route to entity_attributes immediately
- **Never** let scalar rel_types reach the rows list

---

## Solution

**Two-part fix:**

### Part 1: Route scalar facts to entity_attributes BEFORE rows.append()

When an edge is produced with `rel_type` that has `tail_types=SCALAR`:
```python
if _is_scalar_rel_type(edge.rel_type.lower(), _REL_TYPE_META, db):
    # Don't append to rows → route to entity_attributes directly
    # object is the scalar value (age=10, height="5'11", occupation="Engineer")
    # INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text, value_int, ...)
    continue  # Skip rows.append()
```

### Part 2: Validate scalar facts never reach facts table commit

Keep existing guard at line 4729-4748 as final safety check:
```python
rows = [row for row in rows if not is_scalar_rel_type(row[3].lower())]
```

---

## Implementation Steps

1. **Find where edges are appended to rows** (in /ingest endpoint)
   - Search for `rows.append((` calls
   - Check which ones process age/height/occupation/etc.

2. **Add scalar routing BEFORE append**
   ```python
   if _is_scalar_rel_type(edge.rel_type.lower(), _REL_TYPE_META, db):
       # Insert into entity_attributes
       # Use _coerce_scalar() to convert "10" → (None, 10, None, None)
       entity_id = edge.subject or canonical_subject
       value_text, value_int, value_float, value_date = _coerce_scalar(edge.object)
       
       cur.execute("""
           INSERT INTO entity_attributes 
           (user_id, entity_id, attribute, value_text, value_int, value_float, value_date, provenance)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (user_id, entity_id, attribute)
           DO UPDATE SET
               value_text = EXCLUDED.value_text,
               value_int = EXCLUDED.value_int,
               value_float = EXCLUDED.value_float,
               value_date = EXCLUDED.value_date
       """, (user_id, entity_id, edge.rel_type.lower(), value_text, value_int, value_float, value_date, "openwebui"))
       continue  # Don't append to rows
   ```

3. **Test:** Correction "bob is 11 not 10" should:
   - UPDATE entity_attributes age from 10 to 11 ✓
   - Return confirmation in response ✓
   - Database shows age=11 ✓

---

## Test Cases

**Before fix:**
- bob age in facts table with UUID object ✗
- Correction doesn't update (wrong table) ✗

**After fix:**
- bob age in entity_attributes with value_int=10 ✓
- Correction: "bob is 11 not 10" → entity_attributes.value_int=11 ✓
- Retraction works on facts table only (scalars not retractable) ✓

---

## References

- CLAUDE.md: dprompt-96 (three-dimensional fact classification)
- CLAUDE.md: "Fact Storage & Processing: Three Co-Equal Paths"
- Schema: entity_attributes table has (user_id, entity_id, attribute, value_text, value_int, value_float, value_date)
