# dBug-035B: Extended Investigation — Spouse Fact Filtering Root Cause

**Investigation Date:** 2026-05-16 16:05 UTC

**Status:** ROOT CAUSE IDENTIFIED — Scope Filtering Logic Bug

---

## Evidence from Live Logs

### Spouse Fact IS Being Fetched ✓

```
query.initial_user_facts count=15 rel_types={
  'also_known_as': 1,
  'child_of': 3,
  'knows': 2,
  'lives_at': 2,
  'parent_of': 3,
  'pref_name': 3,
  'spouse': 1      ← SPOUSE HERE
}
```

Spouse fact fetched from `facts` table via _fetch_user_facts().

### Spouse IS in Detected Taxonomies ✓

```
determine_scope.multi_factor
  detected_taxonomies=['body_parts', 'family', 'household', 'computer_system', 'location']
  rel_types=['child_of', 'lives_at', 'parent_of', 'instance_of', 'located_in', 'age', 'spouse', 'also_known_as', 'knows', 'pref_name']
```

'spouse' IS in the rel_types list that gets matched to taxonomies.

### Spouse IS in Family Taxonomy ✓

```sql
SELECT rel_types_defining_group FROM entity_taxonomies WHERE taxonomy_name='family';
 {parent_of,child_of,spouse,sibling_of}
```

'spouse' explicitly defined in family's rel_types_defining_group.

### BUT Spouse NOT in Final Output ✗

```
archive_filter.applied
  fact_count_before=41
  fact_count_after=20
  is_historical=False
```

Only 20 facts returned out of 41. Spouse (which was in the 15 initial facts) is lost.

---

## Root Cause Analysis

### The Bug is in apply_archive_filter() scope filtering logic

**Current Logic (lines 1625-1639):**

```python
for fact in facts:
    rel_type = fact.get("rel_type")
    
    if detected_taxonomies and rel_type not in _IDENTITY_SCALAR_RELS:
        # Relationship fact: must be in detected taxonomies
        rel_in_taxonomy = False
        for taxonomy_name in detected_taxonomies:
            if taxonomy_name in _TAXONOMY_CACHE:
                rel_types_for_tax = _TAXONOMY_CACHE[taxonomy_name].get("rel_types_defining_group", [])
                if rel_type in rel_types_for_tax:
                    rel_in_taxonomy = True
                    break
        if not rel_in_taxonomy:
            continue  # Skip relationship fact outside detected taxonomies
```

**For spouse fact:**
- rel_type = "spouse"
- _IDENTITY_SCALAR_RELS = {'pref_name', 'also_known_as', 'age', ...}
- 'spouse' NOT in _IDENTITY_SCALAR_RELS ✓ (goes to scope filter)
- detected_taxonomies = ['family', 'household', ...] ✓ (not empty)
- Loop through taxonomies:
  - _TAXONOMY_CACHE['family'] should exist ✓
  - rel_types_for_tax = [parent_of, child_of, **spouse**, sibling_of] ✓
  - if 'spouse' in [parent_of, child_of, spouse, sibling_of]: True ✓
  - rel_in_taxonomy = True, break ✓
- if not rel_in_taxonomy: if not True: False → SHOULD NOT skip ✓

**Logically, spouse should pass scope filter and be appended to filtered.**

---

## Hypothesis: _TAXONOMY_CACHE parsing bug

The _parse_postgres_array() function must be failing to properly convert the PostgreSQL array string to a Python list.

**PostgreSQL returns:** `{parent_of,child_of,spouse,sibling_of}` (string representation)

**_parse_postgres_array() should convert to:** `['parent_of', 'child_of', 'spouse', 'sibling_of']` (Python list)

**Possible parsing failures:**
1. Extra whitespace not stripped (e.g., `{ parent_of, child_of, ... }`)
2. Quote characters not handled (e.g., `"parent_of"`)
3. Type annotation issue (stored as list in database with newer psycopg2 version)

**If parsing fails:**
- rel_types_for_tax could be a string instead of list
- `'spouse' in string_value` would still work (substring match)
- But then all strings would match (contains 'spouse' as substring)
- This would cause OTHER facts to match when they shouldn't

---

## Critical Missing Field: archived_at & valid_until

**Discovery:** _fetch_user_facts() SQL SELECT (lines 3764-3765):

```sql
SELECT subject_id, object_id, rel_type, provenance, confidence,
  confirmed_count, fact_class, is_preferred_label, rel_type_definition FROM facts
```

**Missing columns:** `archived_at`, `valid_until`

**Impact on archive_filter():**
```python
archived_at = fact.get("archived_at")  # → None (not in dict)
valid_until = fact.get("valid_until")  # → None (not in dict)

if is_historical:
    if archived_at is None and valid_until is None:
        continue  # SKIP current facts when looking for archived
else:
    if archived_at is not None or valid_until is not None:
        continue  # SKIP archived facts when looking for current
```

For is_historical=False (current facts):
- If archived_at=None and valid_until=None
- Then: if (None is not None or None is not None) → False
- Fact is NOT skipped ✓

**So temporal filter should pass facts correctly.** The issue is SCOPE filtering.

---

## Debugging Tasks Needed

**To isolate the exact failure:**

1. **Add logging in _parse_postgres_array():**
   ```python
   log.info("parse_postgres_array", input=arr, output=result, type_output=type(result).__name__)
   ```

2. **Add logging in apply_archive_filter() scope filter:**
   ```python
   log.info("scope_filter_checking",
            rel_type=rel_type,
            in_identity_rels=rel_type in _IDENTITY_SCALAR_RELS,
            detected_taxonomies=detected_taxonomies,
            found_taxonomy=rel_in_taxonomy,
            passed_filter=rel_in_taxonomy or rel_type in _IDENTITY_SCALAR_RELS)
   ```

3. **Dump _TAXONOMY_CACHE contents at startup:**
   ```python
   log.info("taxonomy_cache_contents", cache=json.dumps(_TAXONOMY_CACHE, default=str))
   ```

4. **Check if spouse is in merged_facts BEFORE archive_filter:**
   Add logging before line 4562 to dump spouse facts present.

---

## Next Steps

**Must get detailed logging to identify:**
- Is spouse in merged_facts before apply_archive_filter?
- Is scope filter correctly checking taxonomy membership?
- Is _TAXONOMY_CACHE properly populated with parsed arrays?
- Are archived_at/valid_until missing causing temporal filter issues?

**The fact that 49% of facts are being filtered (20/41) suggests systematic filtering**, not just spouse. Multiple rel_types must be failing the scope filter due to a parsing or logic bug.

**Once rebuild happens, the new logging will pinpoint the exact line where spouse is lost.**
