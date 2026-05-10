# dprompt-35 — Edge Case Fixes: UUID Leak, Age Calculation, Entity Attributes Surfacing

## Purpose

Investigate and fix three edge cases discovered during pre-prod live testing (dprompt-34b): UUID leak in display name fallback, age miscalculation (192 instead of 36), and entity_attributes not being surfaced to `/query` responses.

## The Problems

### Problem 1: UUID Leak in Display Name Fallback
**Observed:** Query "How old am I" returned Gabriella's entity as `D4Bf6C7B-A9Ab-5D1C-8612-54D47Fd90Bd7` instead of her name.
**Root cause:** `_resolve_display_names()` fallback to UUID when preferred name missing. But Gabriella has non-preferred alias "gabriella" — should use it instead of UUID.
**Expected:** Return "gabriella" (non-preferred alias) not UUID.

### Problem 2: Age Miscalculation
**Observed:** Input "I was born on January 15, 1990" → stored age=192 instead of 36.
**Root cause:** Age extraction or calculation bug. Possible issues:
- String-to-int parsing error (1990 → 192?)
- Calculation using wrong year (2026 - 1990 should be 36, not 192)
- Regex pattern mismatching date components

### Problem 3: Entity Attributes Not Surfaced to /query
**Observed:** Age stored in `entity_attributes` table but not returned by `/query` on explicit ask ("How old am I").
**Root cause:** `/query` doesn't fetch or include `entity_attributes` in response. Or fetches them but doesn't merge into fact list.
**Expected:** `/query` should include scalar facts from `entity_attributes` (age, height, born_on, etc.) in memory injection.

## Investigation Steps

### For Problem 1 (UUID Leak)
1. **Code review:** Find `_resolve_display_names()` function in `src/api/main.py`
2. **Trace fallback logic:** How does it handle missing preferred names?
3. **Check:** Does it query non-preferred aliases? If yes, why not returned? If no, that's the bug.
4. **Verify:** `get_any_alias()` function exists and returns non-preferred aliases?
5. **Root cause:** Missing fallback to non-preferred aliases in display name resolution

### For Problem 2 (Age Miscalculation)
1. **Find:** Age extraction code in Filter or `/ingest`
2. **Trace:** How is "1990" parsed from "I was born on January 15, 1990"?
3. **Check:** Is age calculated as `current_year - birth_year` or extracted directly?
4. **Test:** Manual calculation: 2026 - 1990 = 36. If stored is 192, where does 192 come from?
5. **Possible causes:**
   - Regex captures wrong group (e.g., "15" doubled = 150? "90" + something else = 192?)
   - Year parsing treats "1990" as string, appends digits
   - Off-by-one in date arithmetic

### For Problem 3 (Entity Attributes Not Surfaced)
1. **Find:** `_fetch_user_facts()` function in `/query`
2. **Check:** Does it query `entity_attributes` table?
3. **Verify:** Are scalar facts merged into response before memory injection?
4. **Test:** Is `entity_attributes` even being queried, or is it skipped?
5. **Root cause:** Likely `/query` doesn't fetch entity_attributes at all, or fetches but doesn't include in final facts list

## Solutions

### Fix 1: UUID Leak → Use Non-Preferred Alias Fallback
**In `_resolve_display_names()` or `get_preferred_name()`:**
```python
# Current: returns UUID if no preferred name
preferred = registry.get_preferred_name(user_id, entity_id)
if not preferred:
    return entity_id  # ← BUG: returns UUID

# Fixed: fall back to non-preferred alias
preferred = registry.get_preferred_name(user_id, entity_id)
if not preferred:
    preferred = registry.get_any_alias(user_id, entity_id)  # fetch non-preferred
return preferred or entity_id  # last resort: UUID
```

### Fix 2: Age Miscalculation → Debug Regex + Calculation
**Find age extraction code:**
- If extracted from text: verify regex doesn't capture wrong groups
- If calculated from birth_year: verify `2026 - birth_year`, not `2026 - (birth_year % 100)` or other mistakes
- Test with known inputs: "born on January 15, 1990" → should extract year=1990 → age=36

### Fix 3: Entity Attributes Not Surfaced → Add to /query
**In `/query` after `_fetch_user_facts()`:**
```python
facts = _fetch_user_facts(user_id)
# NEW: also fetch entity_attributes
for entity_id in entities:
    attrs = _fetch_entity_attributes(user_id, entity_id)  # age, height, etc.
    facts.extend(attrs)  # add to fact list before scoring
```

## Files to Modify

| File | Change |
|------|--------|
| `src/api/main.py` | 1. Fix `_resolve_display_names()` fallback to non-preferred aliases<br>2. Add entity_attributes fetch to `/query` pipeline<br>3. Debug + fix age extraction/calculation |
| `src/entity_registry/registry.py` | Verify `get_any_alias()` exists and works correctly |

## Success Criteria

- UUID leak fixed: Gabriella returned as "gabriella", not UUID ✓
- Age miscalculation fixed: age=36, not 192 ✓
- Entity attributes surfaced: "How old am I" returns age ✓
- Live testing re-run: all 5 scenarios pass ✓
- No regressions ✓

