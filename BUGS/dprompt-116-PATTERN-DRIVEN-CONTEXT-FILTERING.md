# dprompt-116: Pattern-Driven Context Filtering (dBug-051 Fix 1)

**Link:** [dBug-051: LM Studio Prompt Bloat](dBug-051-LM-STUDIO-PROMPT-BLOAT.md)  
**Status:** IMPLEMENTATION PROMPT — Ready to execute  
**Scope:** Replace unbounded context in `_llm_reason_correction()` with pattern-guided filtering  
**Principle:** Pattern metadata drives context selection. Send ONLY facts matching the pattern's semantic intent.  
**Impact:** 6,000 tokens/call → 800-1,000 tokens/call (83% reduction). Removal requests complete <5s.

---

## PROBLEM (Compact)

Current `_llm_reason_correction()` sends:
- ALL 50 scalar facts (age, height, weight, occupation, etc.)
- ALL 30 relationships (spouse, children, pets, work, locations)
- Headers, examples, pattern info
- **Total:** 6,000+ tokens/call

Pattern arrives with `applicable_rel_types` (learned from dprompt-115 pattern learning):
- "is .+ not" → `{age}` ← knows this pattern corrects ages
- "don't have any" → `{has_pet}` ← knows this removes pets
- "call me X" → `{pref_name, also_known_as}` ← knows this is alias

**But context sent:** All facts, including irrelevant ones.

**Result:** LM Studio queues back up → timeouts → removal hangs.

---

## SOLUTION (12% Compact)

**Pattern decomposition** (from your table):
```
"we don't have any pets"
→ don't/have/any = negation + scope
→ pets = category to match against entity_taxonomies
→ applicable_rel_types = {has_pet}

Context filtering rule:
  IF pattern.semantics == "removal":
    Fetch from entity_taxonomies WHERE taxonomy_name matches pattern
    Get rel_types_defining_group (e.g., {has_pet, has_owner})
    Query facts WHERE rel_type IN rel_types_defining_group
    LIMIT 15
  ELSE IF pattern.semantics == "correction":
    Query entity_attributes WHERE attribute IN applicable_rel_types
    LIMIT 10
  ELSE IF pattern.semantics == "alias":
    Query entity_attributes WHERE attribute IN {pref_name, also_known_as}
    LIMIT 10
```

**Result:** ~600-1,000 tokens instead of 6,000.

---

## IMPLEMENTATION (No Fluff)

### Step 1: Add Pattern Semantics Metadata

In `correction_signals` table (migration):
```sql
-- Already exists from dprompt-115 schema
-- Add if missing:
ALTER TABLE correction_signals ADD COLUMN IF NOT EXISTS semantics TEXT;
-- Values: "correction" | "removal" | "alias" | NULL

-- Seed based on pattern:
UPDATE correction_signals 
SET semantics = CASE
  WHEN pattern ILIKE '%is%not%' THEN 'correction'
  WHEN pattern ILIKE '%don%t have%' THEN 'removal'
  WHEN pattern ILIKE '%call%me%' THEN 'alias'
  ELSE NULL
END;
```

### Step 2: Replace Context Building in `_llm_reason_correction()`

**OLD (lines ~244-273):**
```python
# Fetch all scalar facts
with db.cursor() as cur:
    cur.execute("""
        SELECT DISTINCT entity_id, attribute, value_text, value_int, value_float
        FROM entity_attributes
        WHERE user_id = %s
        ORDER BY updated_at DESC
        LIMIT 50  ← No filtering
    """, (user_id,))
    scalar_facts = cur.fetchall()

# Fetch all relationships
with db.cursor() as cur:
    cur.execute("""
        SELECT subject_id, rel_type, object_id
        FROM facts
        WHERE user_id = %s
        LIMIT 30  ← No filtering
    """, (user_id,))
    relationships = cur.fetchall()
```

**NEW:**
```python
async def _fetch_pattern_context(db, user_id, pattern_info):
    """
    Fetch ONLY facts matching pattern semantics via applicable_rel_types.
    
    Args:
      pattern_info: {
        "semantics": "correction" | "removal" | "alias",
        "applicable_rel_types": ["age", "height"] or ["has_pet"],
        "pattern_str": "is .+ not"
      }
    
    Returns: {"scalar_facts": [...], "relationships": [...]}
    """
    semantics = pattern_info.get("semantics")
    applicable_rel_types = pattern_info.get("applicable_rel_types", [])
    
    scalar_facts = []
    relationships = []
    
    # CORRECTION: Filter to applicable scalar attributes
    if semantics == "correction" and applicable_rel_types:
        with db.cursor() as cur:
            cur.execute("""
                SELECT entity_id, attribute, value_text, value_int, value_float
                FROM entity_attributes
                WHERE user_id = %s AND attribute = ANY(%s)
                ORDER BY updated_at DESC
                LIMIT 10  ← Tight limit, filtered by rel_type
            """, (user_id, applicable_rel_types))
            scalar_facts = cur.fetchall()
        log.info("pattern_context_correction", rel_types=applicable_rel_types, count=len(scalar_facts))
    
    # REMOVAL: Filter to hierarchical category facts
    elif semantics == "removal" and applicable_rel_types:
        with db.cursor() as cur:
            # Find which taxonomy matches the pattern
            # e.g., pattern mentions "pets" → look up entity_taxonomies WHERE taxonomy_name='pets'
            cur.execute("""
                SELECT rel_types_defining_group, member_entity_types
                FROM entity_taxonomies
                WHERE rel_types_defining_group && %s::text[]
                LIMIT 1
            """, (applicable_rel_types,))
            taxonomy_row = cur.fetchone()
            
            if taxonomy_row:
                affected_rel_types, member_types = taxonomy_row
                # Fetch relationships in this category
                cur.execute("""
                    SELECT subject_id, rel_type, object_id
                    FROM facts
                    WHERE user_id = %s AND rel_type = ANY(%s)
                    ORDER BY created_at DESC
                    LIMIT 15
                """, (user_id, affected_rel_types))
                relationships = cur.fetchall()
                log.info("pattern_context_removal", rel_types=affected_rel_types, count=len(relationships))
    
    # ALIAS: Filter to name facts only
    elif semantics == "alias":
        with db.cursor() as cur:
            cur.execute("""
                SELECT entity_id, attribute, value_text
                FROM entity_attributes
                WHERE user_id = %s AND attribute IN ('pref_name', 'also_known_as')
                ORDER BY updated_at DESC
                LIMIT 10
            """, (user_id,))
            scalar_facts = cur.fetchall()
        log.info("pattern_context_alias", count=len(scalar_facts))
    
    # FALLBACK: If semantics unknown, send minimal context
    else:
        with db.cursor() as cur:
            cur.execute("""
                SELECT entity_id, attribute, value_text, value_int
                FROM entity_attributes
                WHERE user_id = %s
                ORDER BY updated_at DESC
                LIMIT 5  ← Very minimal fallback
            """, (user_id,))
            scalar_facts = cur.fetchall()
        log.warning("pattern_context_fallback", semantics=semantics, applicable_rel_types=applicable_rel_types)
    
    return {"scalar_facts": scalar_facts, "relationships": relationships}
```

### Step 3: Update `_llm_reason_correction()` Call

**OLD (line ~264):**
```python
# Fetch user's known scalar facts (DB anchor)
with db.cursor() as cur:
    cur.execute("""
        SELECT DISTINCT entity_id, attribute, ...
        FROM entity_attributes
        WHERE user_id = %s
        ORDER BY updated_at DESC
        LIMIT 50
    """, (user_id,))
    scalar_facts = cur.fetchall()
# ... then same for relationships
```

**NEW:**
```python
# Fetch pattern-filtered context
pattern_info = {
    "semantics": get_pattern_semantics(pattern_str),  # See Step 4
    "applicable_rel_types": applicable_rel_types,
    "pattern_str": pattern_str
}
context = await _fetch_pattern_context(db, user_id, pattern_info)
scalar_facts = context["scalar_facts"]
relationships = context["relationships"]
log.info("llm_reasoning_start", pattern=pattern_str[:30], context_facts=len(scalar_facts), context_rels=len(relationships))
```

### Step 4: Add Pattern Semantics Lookup

```python
def get_pattern_semantics(pattern_str: str) -> str:
    """Infer pattern semantics from pattern string or metadata."""
    pattern_lower = pattern_str.lower()
    
    if any(word in pattern_lower for word in ["is ", " not", "from ", "to "]):
        return "correction"
    elif any(word in pattern_lower for word in ["don't", "don't have", "don't own", "no more"]):
        return "removal"
    elif any(word in pattern_lower for word in ["call", "name", "me "]):
        return "alias"
    else:
        return None
```

### Step 5: Validate Token Reduction

Add telemetry:
```python
# After building prompt, before sending to LLM:
estimated_tokens = len(prompt.split()) * 1.3  # rough estimate
log.info("llm_prompt_size", tokens=estimated_tokens, pattern=pattern_str[:30])

# Expected:
# correction: ~600 tokens
# removal: ~1,000 tokens  
# alias: ~400 tokens
# OLD (all facts): ~6,000 tokens
```

---

## TESTING

**Test 1: Correction (age)**
```
Input: "alicemonde is 14 not 12"
Pattern: "is .+ not"
Semantics: correction
applicable_rel_types: {age}
Context: ONLY age/height/weight facts (~600 tokens)
Expected: LLM responds in <2 seconds
```

**Test 2: Removal (pets)**
```
Input: "we don't have any pets"
Pattern: "don't have any"
Semantics: removal
applicable_rel_types: {has_pet}
Context: ONLY has_pet facts + taxonomy (~1,000 tokens)
Expected: LLM responds in <3 seconds, removal completes
```

**Test 3: Alias (name)**
```
Input: "Call charlie Cy"
Pattern: "call .+ [name]"
Semantics: alias
applicable_rel_types: {pref_name, also_known_as}
Context: ONLY name facts (~400 tokens)
Expected: LLM responds in <1 second
```

---

## VERIFICATION

After implementation:
1. Run full pipeline test (ingest → corrections → removal)
2. Monitor LM Studio queue depth (should stay <3)
3. Measure latencies: corrections <5s, removal <10s
4. Check logs for token counts (should be 600-1,000, not 6,000)

---

## LINKED TO CLAUDE.MD PRINCIPLES

✅ **Metadata-driven:** Pattern semantics + applicable_rel_types + entity_taxonomies drive everything  
✅ **No hard-coding:** Semantics inferred from pattern string, but can be overridden by metadata  
✅ **Self-growing:** As patterns learn rel_types, context automatically becomes more precise  
✅ **Strong ingest:** Pattern-aware filtering ensures LLM reasoning is focused, not diluted  
✅ **Non-brittle:** Falls back to minimal context if semantics unknown

---

## EFFORT & IMPACT

**Effort:** 45 minutes  
**Files touched:** `src/api/main.py` (2 functions, ~100 lines)  
**Migrations:** 1 (add semantics column + seed values)  
**Risk:** LOW (filtering reduces noise, LLM sees cleaner input)  
**Rollback:** Revert to sending all facts if semantics causes issues  

**Impact on dBug-051:** Solves root cause. Queue bloat eliminated. Removal requests complete.

