# dBug-035A: Investigation Findings — Spouse Fact Filtering Mystery

**Investigation Date:** 2026-05-16 16:02 UTC

**Status:** ROOT CAUSE IDENTIFIED — Spouse fact IS in database but NOT being returned by /query

---

## Key Findings

### 1. Spouse Fact EXISTS in PostgreSQL ✓
```sql
SELECT * FROM facts WHERE user_id='10d7d879...' AND rel_type='spouse'
```

**Result:**
- Fact ID: 48
- subject_id: ${TEST_USER_ID} (user)
- rel_type: spouse
- object_id: fb0868c4-12b4-587d-9a3b-ce96ca5979ca (Person entity)
- confidence: 1.0
- created_at: 2026-05-14 20:32:00
- archived_at: NULL (not archived)
- superseded_at: NULL (not superseded)
- hard_delete_flag: false

**The spouse entity:**
- Type: Person
- Aliases:
  - emma (is_preferred = TRUE)
  - marla (is_preferred = FALSE)

### 2. Spouse Fact SHOULD Pass dprompt-91 Filtering ✓

**Scope Check:**
- Family taxonomy rel_types_defining_group: {parent_of, child_of, **spouse**, sibling_of}
- spouse rel_type IS in family's defining group
- Should NOT be filtered out ✓

**Temporal Check:**
- archived_at = NULL
- valid_until = NULL
- is_historical = False (current query)
- Should NOT be filtered out ✓

### 3. Spouse Fact IS NOT Being Returned by /query ✗

**Evidence:**
- Direct /query call returns: `canonical_identity=null, facts=[]`
- OpenWebUI queries (15:57-15:59 UTC) show 40→18/19/20/24 fact filtering
- But spouse (rel_type='spouse') not in any response
- LLM correctly states: "I cannot add this fact because there's no record"

---

## ROOT CAUSE: Unknown Filtering Point BEFORE Archive Filter

The spouse fact exists in database but is removed **before** it reaches `apply_archive_filter()`.

**Pipeline sequence:**
```
1. _fetch_user_facts() — UNION facts + staged_facts
   [Should include spouse fact with hard_delete_flag=false]
   
2. _graph_traverse() — single-hop rel_type graph
   
3. _hierarchy_expand() — walk instance_of/subclass_of chains
   
4. Qdrant search — vector similarity
   
5. Merge + deduplicate — pg_keys based on UUID triples
   
6. apply_archive_filter() — scope + temporal filtering
   [Spouse SHOULD pass both filters]
```

**Missing spouse at which point?**
- **Option A:** Not fetched by _fetch_user_facts() (unlikely - hard_delete_flag check passed)
- **Option B:** Filtered out by graph traversal logic (identity rels handling?)
- **Option C:** Filtered out by deduplication logic (UUID collision?)
- **Option D:** Never reaches archive_filter (short-circuit somewhere?)

---

## Critical Observation

**Timeline mismatch:**
- Spouse fact created: 2026-05-14 20:32:00
- User statement: "My wifes name is Marla..." (2026-05-16 15:55 UTC)
- System response: "I cannot add this"

**This suggests:** The spouse DID get ingested 2 days ago, but was recently lost from /query output. Either:
1. A recent change to /query broke spouse fact retrieval
2. Or spouse was filtered by dprompt-91 (new code)
3. Or something else deleted/superseded the spouse relationship

---

## Questions for Next Step

1. **Is spouse in the baseline 40 facts BEFORE apply_archive_filter?**
   - Add logging to print fact count after merge/dedup step
   
2. **Was spouse ever present in recent /query output?**
   - Check git history for recent changes to /query that might have broken it
   
3. **Is dprompt-91 filtering correctly?**
   - spouse IS in family.rel_types_defining_group
   - spouse IS NOT in _IDENTITY_SCALAR_RELS
   - spouse with archived_at=NULL should pass temporal filter
   - Logic looks correct; needs execution trace

4. **Is deduplication removing spouse fact?**
   - Check if multiple spouse facts exist (duplicate UUIDs?)
   - Or if merge logic is consolidating too aggressively

---

## Hypothesis: dprompt-91 Filtering Regressed

**Possible bug in apply_archive_filter():**

```python
for fact in facts:
    rel_type = fact.get("rel_type")
    
    if detected_taxonomies and rel_type not in _IDENTITY_SCALAR_RELS:
        # Relationship fact: check if in detected taxonomies
        rel_in_taxonomy = False
        for taxonomy_name in detected_taxonomies:
            if taxonomy_name in _TAXONOMY_CACHE:
                rel_types_for_tax = _TAXONOMY_CACHE[taxonomy_name].get("rel_types_defining_group", [])
                if rel_type in rel_types_for_tax:
                    rel_in_taxonomy = True
                    break
        if not rel_in_taxonomy:
            continue  # SKIP if not in any taxonomy
```

**Possible issue:**
- What if `detected_taxonomies` is EMPTY?
- Then the condition `if detected_taxonomies and ...` is FALSE
- So spouse fact would NOT be checked
- And would NOT be filtered
- But then it should still be in output!

**Alternative possibility:**
- What if `rel_types_for_tax` is a STRING `"{spouse,parent_of,...}"` instead of LIST?
- The `_parse_postgres_array()` function might fail
- Then `if rel_type in rel_types_for_tax` would look for "spouse" in string
- This would work (substring match), but maybe with bugs?

---

## Next Actions Required

**To complete investigation:**
1. Add detailed logging to _fetch_user_facts() → baseline_facts count
2. Add logging before/after graph_traverse() → count
3. Add logging before/after merge/deduplicate() → count
4. Add logging before apply_archive_filter() → log actual facts, not just count
5. Verify spouse fact shape (all fields present?)
6. Verify _TAXONOMY_CACHE['family']['rel_types_defining_group'] contains 'spouse'

**This will isolate exactly where spouse is lost.**
