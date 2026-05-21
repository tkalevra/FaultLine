# dBug-036A: Scalar Objects Being Resolved to UUIDs

**Status:** INVESTIGATION COMPLETE  
**Severity:** CRITICAL — Violates hard constraint, breaks user correction detection  
**Filed:** 2026-05-16  

---

## Hard Constraints Violated

From CLAUDE.md **Entity ID vs Display Name: Semantic Distinction**:

> **SCALAR REL_TYPES (object must be STRING):**  
> pref_name, also_known_as, age, height, weight, born_on, occupation, nationality
> 
> **RELATIONSHIP REL_TYPES (object must be UUID or user_id):**  
> has_pet, spouse, parent_of, child_of, friend_of, knows, met, works_for, educated_at, located_in, lives_in, lives_at, born_in, likes, dislikes, prefers, same_as

> **Never store display names in `*_id` columns.**

**VIOLATION:** Age facts stored with UUID objects instead of STRING values:
```
55c13545-3f9a-5798-8827-c35e7c9cfa70 | b18ee291-1e2f-56b4-97ac-147137be584c | age
```

Should be:
```
55c13545-3f9a-5798-8827-c35e7c9cfa70 | "12" | age
```

---

## Impact: USER IS NOT ULTIMATE SOURCE OF TRUTH

When user says "My son alice is 12 years old":

1. **Expected:** Extract (alice, age, "12") → Find old fact (alice, age, "16") → Archive old → Write new
2. **Actual:** Extract (alice, age, "12") → old fact has object_id="16" → new fact gets object_id=<uuid> → NO MATCH → old fact NOT archived

**Result:** System contradicts user instead of accepting correction.

Evidence from integration test:
```
User: "My son alice is 12 years old"
System: "alice is 16 years old, not 12"
```

---

## Root Cause

**Location:** `src/api/main.py` lines 2774-2778

```python
if (canonical_object in _user_aliases and canonical_object != user_entity_id and
    edge.rel_type.lower() not in ("also_known_as", "pref_name")):
    log.info("ingest.object_normalized_to_user_id",
             original=canonical_object, user_id=user_entity_id)
    canonical_object = user_entity_id  # ← CONVERTS "12" to UUID
```

**Problem:** This code normalizes user aliases to UUID, but guards ONLY identity rels (`also_known_as`, `pref_name`), not ALL scalar rels.

**Flow:**
1. Line 2666-2667: Correctly keeps age="12" as STRING
2. Line 2774-2778: **INCORRECTLY** converts "12" to UUID if it matches a user alias

**Result:** age facts stored as:
- `(alice_uuid, age, "16")` — correct (STRING)
- `(alice_uuid, age, <uuid>)` — wrong (UUID from line 2778)

---

## Fix Required

See dprompt-95-dBug-036A-scalar-object-uuid-fix.md for implementation details.

Guard line 2774 to exclude ALL scalar rel_types:

```python
# Add to condition:
edge.rel_type.lower() not in _SCALAR_OBJECT_RELS
```

---

## Test Case (After Fix)

**User input:** "My son alice is 12 years old"  
**Current behavior:** "alice is 16 years old, not 12" (contradicts user)  
**Expected behavior:** "Updated: alice is 12 years old" (accepts correction, archives old fact)

---

## Constraint References

- **CLAUDE.md:** "Scalar rel_types have STRING objects, relationship rel_types have UUID objects"
- **CLAUDE.md:** "Never store display names in `*_id` columns"
- **CLAUDE.md:** "User is ultimate source of truth — User corrections are authoritative"
- **CLAUDE.md Archive Model:** "User corrections are authoritative. When user corrects a fact, conflicting facts are archived at write-time in WGMValidationGate"

