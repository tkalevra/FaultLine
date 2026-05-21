# dBug-064: Correction Filter Incomplete — Doesn't Handle All Correction Scenarios

**Status**: OPEN  
**Severity**: HIGH (pipeline accepts both old and new values, causing data overwrites)  
**Affects**: dprompt-128 P4 (extraction filtering), `extract_rewrite()` function  
**Root Cause**: Filter logic too narrow — only handles explicit negation markers in definition field  
**Impact**: Identity corrections (pref_name, also_known_as) and relationship negations don't deduplicate properly  

---

## Problem Statement

The extraction filter (dprompt-128 P4) is aliceigned to remove negated/old values when `is_correction=true`, keeping only positive assertions. Testing revealed three scenarios:

### Scenario 1: Scalar Corrections (Age) ✅ WORKS
**Input**: "Correction: I am 46 years old, not 12 years old."  
**Extraction**: 
- `(user, age, 46, is_correction=true, definition="user is 46 years old")`
- `(user, age, 12, is_correction=true, definition="user was previously thought to be 12 years old (corrected)")`

**Filter behavior**: 
- Detected "previously" in age=12 definition → marked as negated
- Kept only age=46 → triple_count=1 ✅
- Result: Database updated with age=46 ✓

### Scenario 2: Identity Corrections (Name) ⚠️ PARTIALLY BROKEN
**Input**: "Actually, I prefer to be called Chris, not John."  
**Extraction**: 
- `(user, pref_name, Chris, **NO is_correction flag**)`
- `(user, also_known_as, John, **NO is_correction flag**)`

**Filter behavior**: 
- Both triples lacked is_correction=true → filter didn't apply
- Both INSERTed via ON CONFLICT
- Result: pref_name=John (last INSERT wins) ✗
- Database still shows "john" instead of "chris"

**Root cause**: LLM didn't mark preference statements with is_correction=true (extraction prompt doesn't guide LLM to detect preference statements as corrections)

### Scenario 3: Relationship Negations ✗ BROKEN
**Input**: "Sorry, I don't have a daughter named bob. That was a mistake."  
**Extraction**: 
- `(user, pref_name, bob)` only
- **NO explicit negation triple** like `(user, NOT parent_of, bob)`

**Filter behavior**: 
- Single entity mention (bob) triggered implicit parent_of inference
- No negation triple to filter
- Result: parent_of relationship created alicepite user's explicit denial ✗
- Database still shows parent_of relationship (confidence=0 from earlier, but not deleted)

**Root cause**: LLM extracts entities but doesn't model negations as structured facts; system infers relationships from entity presence

---

## Hard Constraints Violated

Per CLAUDE.md:

### 1. **Metadata-Driven, Not Hardcoded** ❌ VIOLATED
```
Current: Filter checks hardcoded negation markers: "not", "was", "previously", "wrong", "mistake", "incorrect"
Better: Store negation patterns in correction_signals table; query at runtime
Risk: Adding "was" to age definition breaks other contexts ("was born in..."). Adding "wrong" breaks scientific contexts.
```

### 2. **Validation is Metadata-Driven via rel_types Table** ❌ VIOLATED
```
Current: Filter only works when rel_type extraction follows current prompt patterns
Missing: rel_types.negation_behavior metadata column (e.g., suppresses_old_values: true for pref_name)
```

### 3. **No Brittle Hardcoding** ❌ VIOLATED
```
Current code (src/api/main.py, line ~3498):
  if tail_types == text_lower or " not " in definition_lower
  elif any(word in definition for word in ["not", "was", "previously", ...])
Problem: Word list grows with edge cases, becomes unmaintainable. Different rel_types need different patterns.
```

### 4. **Architecture: Filter is Dumb, Backend is Smart** ❌ VIOLATED
```
Current: Filter has domain-specific knowledge of what negation looks like
Better: Backend (WGM gate) decialice which triples to accept/reject based on semantics
This should happen at INGEST time, not EXTRACTION time.
```

### 5. **UUID and Identity Hard Constraints** ⚠️ SIDE EFFECT
```
When both pref_name="Chris" and pref_name="John" are stored:
  - ON CONFLICT (user_id, entity_id, attribute) DO UPDATE SET
  - Last INSERT wins (john)
  - No audit trail of which was the correction
  - No timestamp of when correction occurred (updated_at shows both writes same second)
```

---

## Why Filter Approach Fails to Scale

The current filter attempts to handle corrections at **extraction time** by analyzing definition strings. This approach breaks down because:

1. **LLM doesn't always produce correction context** 
   - Age correction: LLM marks both with is_correction=true ✓
   - Name correction: LLM marks neither ✗ (extraction prompt doesn't guide it)
   - Relationship negation: LLM extracts entities, not negations ✗

2. **Different rel_types need different negation semantics**
   - Scalar (age): "previously" means old value → filter negation
   - Identity (pref_name): "not X" means X is wrong, prefer Y → both valid, need ordering
   - Relationship (parent_of): "don't have" is explicit negation → supersede or delete
   - Status (location): "no longer at" means archive, not delete

3. **Definition field is unreliable**
   - Not all LLM outputs include definitions
   - Definition text varies by model, prompt, context
   - String matching fragile across languages/phrasings

4. **Correction semantics belong at ingest, not extraction**
   - Extract should be simple: return all triples from LLM
   - Ingest (WGM gate) should apply correction logic based on rel_type metadata
   - This is where we already have metadata-driven validation

---

## Proper Solution (Metadata-Driven, No Hardcoding)

### Step 1: Add rel_types Metadata Column
```sql
ALTER TABLE rel_types ADD COLUMN correction_behavior VARCHAR(20);
  -- 'overwrite': new value replaces old (age, height, weight)
  -- 'append': both values valid, order doesn't matter (friend_of, knows)
  -- 'archive': old value superseded_at=now() (lives_at, location)
  -- 'ignore': corrections not applicable (instance_of, creation facts)
  -- 'explicit_only': only accept if is_correction=true from LLM
```

### Step 2: Move Filter Logic to Ingest Validation Gate
Instead of filtering at extraction, apply correction semantics during WGM validation:

```python
# src/wgm/gate.py - WGMValidationGate._apply_correction_semantics()
def _apply_correction_semantics(self, edge, existing_facts, db):
    """
    Apply correction logic based on rel_type.correction_behavior metadata.
    This is where OLD values are superseded, not at extraction time.
    """
    rel_meta = self._get_rel_type_metadata(edge.rel_type, db)
    behavior = rel_meta.get('correction_behavior', 'append')
    
    if behavior == 'overwrite' and edge.is_correction:
        # Find old values for same subject+rel_type
        old_facts = self._find_old_values(edge.subject, edge.rel_type)
        for old in old_facts:
            # Mark superseded, don't delete
            old.superseded_at = datetime.now()
            old.save()
    
    elif behavior == 'archive' and edge.is_correction:
        # Soft-delete for location-like facts
        old_facts.update(archived_at=datetime.now())
    
    # Return: should this triple be accepted? (True/False)
    return edge.confidence >= 0.3  # Normal gate logic
```

### Step 3: Remove Extraction Filter Entirely
Delete the hardcoded filter at lines 3498-3536 in extract_rewrite(). Let extraction be dumb:
- Return ALL triples from LLM without filtering
- Let ingest apply the correction semantics via metadata

### Step 4: Enhance Extraction Prompt (Metadata-Driven)
Instead of hardcoded examples, query rel_types to understand which rel_types support corrections:

```python
# src/api/main.py - _build_extraction_prompt()
cur.execute("""
    SELECT rel_type, label, correction_behavior
    FROM rel_types
    WHERE correction_behavior IS NOT NULL
    ORDER BY correction_behavior, rel_type
""")
correction_rels = cur.fetchall()

if correction_rels:
    base_prompt += "\nCORRECTION-SUPPORTING REL_TYPES (metadata from DB):\n"
    for rel_type, label, behavior in correction_rels:
        base_prompt += f"  - {rel_type} ({behavior}): {label}\n"
    base_prompt += "If message signals correction to these rel_types, mark with is_correction=true\n"
```

### Step 5: Database Versioning for Audit Trail
Preserve correction history (memory system requirement):

```python
# When superseding old values in ingest:
old_fact.superseded_by = edge_id  # FK to new fact
old_fact.superseded_at = datetime.now()
old_fact.superseded_reason = 'user_correction'
old_fact.save()

# Query can choose: current only (superseded_at IS NULL) or historical
```

---

## Testing Plan (Metadata-Driven)

### Test Case 1: Scalar Overwrite (age)
```sql
INSERT INTO rel_types (rel_type, correction_behavior) 
VALUES ('age', 'overwrite')
```
**Input**: "I am 46, not 12"  
**Expected**: Age 12 superseded_at ≠ NULL, age 46 is current  
**Verify**: Query current facts shows age=46

### Test Case 2: Identity Append (pref_name, also_known_as)
```sql
INSERT INTO rel_types (rel_type, correction_behavior) 
VALUES ('pref_name', 'append'), ('also_known_as', 'append')
```
**Input**: "Call me Chris, not John"  
**Expected**: Both stored, query returns both (or ordered by preference)  
**Verify**: Both pref_name="john" AND pref_name="chris" exist

### Test Case 3: Relationship Archive (parent_of, child_of)
```sql
INSERT INTO rel_types (rel_type, correction_behavior) 
VALUES ('parent_of', 'archive'), ('child_of', 'archive')
```
**Input**: "I don't have a daughter bob"  
**Expected**: parent_of marked archived_at, child_of marked archived_at  
**Verify**: Query with include_archived=false doesn't show bob relationship

### Test Case 4: Unknown rel_type (Default Behavior)
**Input**: LLM extracts novel rel_type with is_correction=true  
**Expected**: No crash, falls back to 'append' behavior (safe default)  
**Verify**: Both old and new values stored, no data loss

---

## Growability Guarantees

### New rel_types Automatically Supported
When a new rel_type is added to ontology:
1. Set `correction_behavior='append'` (safe default)
2. Extract continues working (returns all triples)
3. Ingest applies behavior (no code changes needed)
4. No filter hardcoding required

### Correction Patterns Grow via DB
Re-embedder already learns correction_signals from corrections. Now:
1. Extraction prompt queries rel_types corrections metadata
2. Prompt inclualice which rel_types support corrections (not hardcoded)
3. New correction_signals approved by re_embedder appear in next extraction prompt
4. System improves without code deployment

### Historical Queries Work
With `superseded_at` and `archived_at` columns:
```sql
-- Current facts only (default)
SELECT * FROM facts WHERE superseded_at IS NULL AND archived_at IS NULL

-- Historical: "Where did I used to live?"
SELECT * FROM facts WHERE rel_type='lives_at' AND archived_at IS NOT NULL

-- Full audit trail
SELECT * FROM facts WHERE (superseded_by IS NOT NULL OR archived_at IS NOT NULL)
```

---

## Implementation Checklist

- [ ] Add `correction_behavior` column to rel_types table (migration)
- [ ] Initialize correction_behavior for existing rel_types (data migration)
- [ ] Remove hardcoded negation filter from `extract_rewrite()` (src/api/main.py:3498-3536)
- [ ] Move correction logic to `WGMValidationGate._apply_correction_semantics()` (src/wgm/gate.py)
- [ ] Update extraction prompt to query rel_types metadata (src/api/main.py:_build_extraction_prompt)
- [ ] Add superseded_at, archived_at columns to facts table if not present
- [ ] Update query path to respect superseded_at/archived_at by default
- [ ] Add comprehensive tests for all correction_behavior values
- [ ] Update CLAUDE.md Hard Constraints section

---

## Risk Assessment

**Before Fix**: 
- ✗ Name corrections silently fail (both values stored, wrong one wins)
- ✗ Relationship negations not captured (system infers relationships user denied)
- ✗ Hardcoded word list unmaintainable (grows with edge cases)
- ✗ Different rel_types need different logic (no metadata to drive it)

**After Fix**:
- ✓ All correction types handled via metadata
- ✓ Historical data preserved (no deletes, audit trail via superseded_at)
- ✓ System grows when ontology grows (no code changes)
- ✓ Extraction stays dumb (returns all LLM output)
- ✓ Ingest is smart (applies semantics from metadata)

---

## References

- CLAUDE.md: Hard Constraints section
- dprompt-128: Correction detection and filtering
- dprompt-128-P4: Current extraction filter (to be removed)
- WGMValidationGate: Where correction logic belongs
- correction_signals table: Self-learning pattern database
- Test results: Age (✓), Name (⚠️), Relationship (✗)
