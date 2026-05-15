# dBug-024: Child Name Correction Not Extracted — "Son" vs "Bob"

**Severity:** High — user corrections are silently dropped, stale data returned

**Status:** CONFIRMED

**Date Reported:** 2026-05-15

**User Context:** user_id=`10d7d879-63cd-4f31-92ce-f2c9edb760ab`

## Problem Summary

User attempts to correct child's name via natural language:
```
User: "My chilrens names are Alice, Bob, and Carol"
```

System still returns old fact in memory injection:
```
- family: spouse=Jane, children=Son
```

Database state shows entity `55c13545-3f9a-5798-8827-c35e7c9cfa70` has:
- `pref_name = "boy"` (confidence 1.0, created 2026-05-15 02:33:08)
- Alias "cyrus" in entity_aliases (is_preferred=false)
- NO `pref_name = "cyrus"` fact exists

## Root Cause

Entity alias conflict + extraction failure:

1. **Initial extraction** (2026-05-15 02:33:08): System extracted `pref_name("entity", "boy")` with confidence 1.0
2. **Name conflict resolution** (async): Re-embedder registered "cyrus" as non-preferred alias during conflict detection
3. **User correction attempt** (2026-05-15 11:52:36): User sends name list: "Alice, Bob, and Carol"
4. **Extraction failure** (2026-05-15 11:52:41): `/extract/rewrite` produced 6 triples, but NONE were `pref_name("55c13545", "cyrus")`
5. **Stale data returned**: `/query` injects old memory with "children=Son" (from original pref_name fact)

## Evidence

### OpenWebUI logs (11:52:36–11:52:42)
```
[FaultLine Filter] user_id=[redacted] text='My chilrens names are Alice, Bob, and Carol'
[FaultLine Filter] calling /query url=http://192.168.40.10:8001/query
[FaultLine Filter] /query status=200
[FaultLine Filter] filtered: 43/43 facts
```

At 11:52:36, Filter receives user's message, calls `/query`, gets back old family facts.

FaultLine logs show `/extract/rewrite` returned `triple_count=6` at 11:52:41, but DB shows no `pref_name = "cyrus"` fact created.

### Database state
```sql
SELECT entity_id, alias, is_preferred FROM entity_aliases 
WHERE alias IN ('son', 'cyrus') AND entity_id = '55c13545-3f9a-5798-8827-c35e7c9cfa70';
```

Result:
```
            entity_id              | alias | is_preferred 
--------------------------------------+-------+----------
 55c13545-3f9a-5798-8827-c35e7c9cfa70 | cyrus | f
 55c13545-3f9a-5798-8827-c35e7c9cfa70 | boy   | t
```

Facts table:
```sql
SELECT subject_id, rel_type, object_id, confidence FROM facts 
WHERE subject_id = '55c13545-3f9a-5798-8827-c35e7c9cfa70' AND rel_type = 'pref_name';
```

Result: Only `("55c13545...", "pref_name", "boy", 1.0)` — no "cyrus" fact.

## Classification

**Extraction bug:** LLM prompt in `/extract/rewrite` does not handle name lists in natural language as pref_name corrections.

**Input pattern:**
```
"My children's names are <name1>, <name2>, and <name3>"
```

**Expected behavior:** Extract `(child_entity_uuid, pref_name, "cyrus")` with high confidence (user-stated).

**Actual behavior:** Extraction produces only relationship/type facts, no pref_name update.

## Impact

- User corrections are silently dropped — no error, no warning
- System returns stale data (old preferred name) in memory injection
- User may assume system has learned the correction when it has not
- Becomes unrecoverable without manual database intervention or explicit retraction

## Related Issues

- dBug-022: Preferred fact exposure (dead-naming context) — related to preferred_name selection
- dBug-023: Entity fragmentation (pronoun resolution) — similar extraction gap

## Recommended Fix

**Option A: Strengthen LLM prompt**
- File: `src/api/main.py` (~line 1675 in `/extract/rewrite`)
- Add example: `"For 'My children are Alice and Bob': (me, pref_name, alice), (me, pref_name, bob)"`
- Add rule: "Name lists like 'X, Y, and Z' are pref_name corrections — extract as individual facts"

**Option B: Add classification rule**
- Post-process extracted triples
- If input contains "are" + name list pattern → flag as potential pref_name corrections
- Elevate confidence or force classification

**Option C: Retraction first**
- User explicitly says "Not 'son' — his name is 'cyrus'"
- System processes as retraction + new fact
- Simpler but requires user to be aware of the system's state

## Test Case (Verification)

```bash
curl -X POST http://localhost:8001/query \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "My childrens names are Alice, Bob, and Carol",
    "user_id": "10d7d879-63cd-4f31-92ce-f2c9edb760ab"
  }'
```

**Expected:** After ingest, `/query` returns `pref_name=cyrus` for entity `55c13545`, and subsequent "tell me about my family" responses use "Bob" instead of "Son".

**Current:** `/query` still returns `pref_name=son`.

## Fix

**dprompt-086:** User-stated entity name corrections via comprehensive pref_name extraction

Approach: Extract pref_name for every entity mentioned + trigger name conflict resolver when user-stated facts arrive. Leverages existing Class A infrastructure (confidence=1.0, write-through) and `re_embedder.resolve_name_conflicts()`.

## Next Steps

1. Implement dprompt-086
2. Verify with test case above
3. Document in CLAUDE.md if new extraction pattern established
