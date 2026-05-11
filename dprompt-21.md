# dprompt-21 — Pet Ingest Failure: Debug & Fix [INVALID — see dprompt-22]

## Problem

User says: "I have a wife Marla. Marla has a dog named Rex, a golden retriever."

Result:
- ✅ User identity extracted
- ✅ Marla (spouse) extracted
- ❌ Rex (pet) NOT extracted or ingested

2 entities in DB (user + marla), 0 pet entities, 0 has_pet facts.

The pet is mentioned (system responded), but ingest failed for that specific fact type.

## Why This Matters Now

With **taxonomies live**, pets SHOULD work via `household` taxonomy:
- `household` defines rel_types: `lives_at`, `lives_in`, `member_of`
- `household` includes `has_pet` as transitive rel_type
- When `has_pet(marla, rex)` is stored, rex becomes transitive family member
- Query "tell me about my family" should return marla + rex via `_fetch_transitive_members()`

But taxonomies can't help if `has_pet` facts never reach the DB.

## Debug Steps

### 1. Check Extraction Layer

**Does compound.py extract pet patterns?**
```bash
grep -n "has_pet\|pet\|animal" src/extraction/compound.py
```

Look for:
- Pet clause detection (like children clause)
- Patterns for "has a [animal] named [name]"
- Species/breed extraction (descriptor patterns)

If NOT found → compound.py is missing pet extraction entirely.

### 2. Check Ingest Reception

**Does `/ingest` receive pet edges?**

Add temporary debug logging to `/ingest` (around line 1000):
```python
if any(edge.rel_type.lower() == "has_pet" for edge in edges):
    log.info("ingest.pet_edge_received", 
             count=sum(1 for e in edges if e.rel_type.lower() == "has_pet"),
             edges=[{"subj": e.subject, "obj": e.object} for e in edges if e.rel_type.lower() == "has_pet"])
```

Then re-ingest and check logs. If no log appears → pet facts aren't reaching `/ingest`.

### 3. Check Validation Gate

**Is WGMValidationGate rejecting pet facts?**

Check WGM gate logic for `has_pet`:
- Is `has_pet` in rel_types table? (Should be, from Wikipedia ontology)
- Are type constraints satisfied? (subject: Person/Animal, object: Animal)
- Is the entity type classification working? (Is rex classified as Animal?)

If type constraint fails → log should show "head_type mismatch" or similar.

### 4. Check Entity Type Classification

**Is rex being classified as Animal?**

When `has_pet(marla, rex)` is ingested:
- GLiNER2 should classify rex as Animal (from "dog")
- Compound descriptor extraction should capture species="dog"
- Entity type should be stored as Animal

Check:
```sql
SELECT id, entity_type FROM entities WHERE id IN (
  SELECT object_id FROM facts WHERE rel_type = 'has_pet'
);
```

If rex is type='unknown' → type classification broke.

## Fix Roadmap

**If compound.py missing pet patterns:**
- Add pet clause detection (similar to children clause, lines 94-111)
- Add patterns: "has a [species] named [name]", "[name] is a [species]", etc.
- Extract species/breed as entity_attributes (descriptor extraction)
- Wire into ingest loop (already done at line 1035)

**If WGM gate rejecting for type:**
- Ensure `has_pet` has head_type=["Person", "Animal"], tail_type=["Animal"]
- Verify entity type classification runs before WGM check
- Check: Is descriptor extraction (species="dog") populating entity_attributes?

**If extraction works but facts still not stored:**
- Check if facts are going to Class C (RAG only) instead of facts table
- Verify `_apply_taxonomy_rules()` isn't marking pet facts as "unknown" rel_type
- Check if taxonomy expansion is accidentally filtering has_pet

## Test

After fix:
1. New chat: "I have a wife Marla. Marla has a dog named Rex, a golden retriever."
2. Check DB:
   ```sql
   SELECT * FROM facts WHERE rel_type = 'has_pet';
   SELECT * FROM entities WHERE id LIKE '%rex%' OR entity_type = 'Animal';
   SELECT * FROM entity_attributes WHERE attribute = 'species' AND value_text = 'dog';
   ```
3. Expected: 
   - `has_pet(marla_uuid, rex_uuid)` fact exists
   - `rex_uuid:Animal` entity exists
   - `species="dog"` attribute exists
4. New chat: "Tell me about my family"
5. Expected: System returns "You have Marla (spouse) and her dog Rex" (transitive via household taxonomy)

## Files to Check/Modify

- `src/extraction/compound.py` — Add pet patterns if missing
- `src/api/main.py` — Add debug logging to `/ingest`, verify type classification runs before WGM
- `src/wgm/gate.py` — Verify `has_pet` rel_type constraints
- `migrations/001_create_facts.sql` or rel_types seed — Verify `has_pet` exists with correct types

## Why It Matters

Pet extraction is the first real test of the **taxonomy system**. If pets aren't stored, taxonomies can't provide transitive reasoning. Once pet ingest works, the household taxonomy immediately enables:
- User's family includes spouse's pets (has_pet transitive)
- Query "family" returns all household members + pets
- No hardcoded patterns needed (all data-driven via taxonomies)
