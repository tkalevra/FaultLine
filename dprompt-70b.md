# dprompt-70b: Bidirectional Relationship Completeness — DEEPSEEK_INSTRUCTION_TEMPLATE

**Template version:** 1.0  
**Philosophy:** Semantic graph completeness via two-phase fix (extraction + ingest)

---

## Task

Implement bidirectional relationship auto-creation: update extraction prompt to mandate both-directional emission AND update ingest pipeline to auto-create missing inverses.

---

## Context

**Background:** Knowledge graph has incomplete bidirectional relationships (dBug-012). Example: `gabby -child_of-> chris` exists but inverse `chris -parent_of-> gabby` is missing. Causes graph traversal to miss entities when walking only one direction.

**Why:** LLM extraction doesn't emit both directions (parent_of AND child_of); ingest validates conflicts but doesn't auto-create missing inverses. Result: incomplete graph with gaps in family/relationship traversal.

**Impact:** /query can find Gabby via child_of walk but NOT via parent_of walk. Spouse relationships incomplete (one direction only). Breaks semantic completeness for bidirectional rel_types.

**Scope:** Two files, two changes (extraction prompt + ingest validation). No WGM, re-embedder, or query logic changes. No schema changes.

---

## Constraints

### MUST:
- Update `_TRIPLE_SYSTEM_PROMPT` to explicitly instruct bidirectional emission for parent_of/child_of/spouse/sibling_of
- Update `_validate_bidirectional_relationships()` to auto-create missing inverses
- Run full test suite; 0 regressions allowed
- No changes to WGM gate, re-embedder, or /query endpoint logic
- No schema changes
- ONLY modify openwebui/faultline_tool.py and src/api/main.py

### DO NOT:
- Commit to git until user approves
- Touch validation rules, rel_types table, or ontology
- Change /ingest or /query endpoint contracts
- Modify re-embedder promotion/expiry logic
- Clean up stale data (out of scope — separate migration task)
- Refactor or optimize unrelated code

### MAY:
- Add tests for new bidirectional behavior
- Update docstrings in modified functions
- Add comments explaining auto-inverse logic

---

## Sequence

### 1. Verify Pre-Prod Database State

```bash
# Confirm gaps exist (from deepseek's investigation)
ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline -c \
  \"SELECT subject_id, rel_type, object_id, confidence FROM facts \
   WHERE rel_type IN ('parent_of', 'child_of', 'spouse', 'sibling_of') \
   ORDER BY rel_type, subject_id LIMIT 20;\""
```

Expected: At least 1 missing inverse per bidirectional rel_type.

### 2. Modify `openwebui/faultline_tool.py` — Extraction Prompt

**File:** `openwebui/faultline_tool.py`  
**Section:** `_TRIPLE_SYSTEM_PROMPT` (lines 86–171)  
**Location:** After line 99 (after existing RELATIONSHIP RULES)

**Add new rule block:**
```python
BIDIRECTIONAL EMISSION: For inverse rel_types (parent_of/child_of, spouse, sibling_of),
ALWAYS emit BOTH directions as separate facts.
- If (user, parent_of, des), ALSO emit (des, child_of, user).
- If (user, spouse, mars), ALSO emit (mars, spouse, user).
- If (des, sibling_of, cyrus), ALSO emit (cyrus, sibling_of, des).
Example: "I have a son named Des, my husband Mars" →
  (user, parent_of, des) + (des, child_of, user) + (user, spouse, mars) + (mars, spouse, user)
```

**Why:** Makes bidirectional emission explicit, not optional. Prevents future extraction gaps.

### 3. Modify `src/api/main.py` — Ingest Auto-Create

**File:** `src/api/main.py`  
**Function:** `_validate_bidirectional_relationships()` (around line 2590)  
**Task:** After conflict handling, add missing-inverse auto-creation logic.

**Current logic (simplified):**
```python
def _validate_bidirectional_relationships(facts, ...):
    # Check both directions exist
    # If only one: pick higher confidence, supersede lower
    # Return modified facts
```

**Add after conflict handling:**
```python
# Auto-create missing inverses
for fact in facts:
    rel_type = fact.get('rel_type')
    
    # Get metadata for this rel_type
    meta = _get_rel_type_metadata(rel_type)
    inverse_rel_type = meta.get('inverse_rel_type') if meta else None
    
    if not inverse_rel_type:
        continue  # Not a bidirectional rel_type
    
    # Check if inverse exists
    subject_id = fact['subject_id']
    object_id = fact['object_id']
    
    inverse_exists = any(
        f.get('subject_id') == object_id and 
        f.get('object_id') == subject_id and 
        f.get('rel_type') == inverse_rel_type
        for f in facts
    )
    
    if not inverse_exists:
        # Create missing inverse with same confidence and fact_class
        inverse_fact = {
            'subject_id': object_id,
            'object_id': subject_id,
            'rel_type': inverse_rel_type,
            'confidence': fact.get('confidence', 0.8),
            'fact_class': fact.get('fact_class', 'B'),
            'provenance': f"auto-created inverse of {fact.get('provenance', 'unknown')}"
        }
        facts.append(inverse_fact)
```

**Why:** Resilient to extraction gaps. If LLM misses one direction, ingest auto-creates it. Handles both current and future data.

### 4. Run Tests

```bash
cd /home/chris/Documents/013-GIT/FaultLine-dev

# Full test suite
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing -v

# Expected: All pass, 0 regressions
```

**Report test results in scratch.md.**

### 5. Manual Validation (Optional)

If pre-prod is running:

```bash
# Test extraction emits both directions
curl -X POST http://192.168.40.10:8001/ingest \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-...' \
  -d '{
    "text": "I have a daughter named Gabby",
    "user_id": "user-123",
    "edges": [
      {"subject": "user", "rel_type": "parent_of", "object": "gabby", "confidence": 1.0}
    ],
    "source": "test"
  }' | jq '.facts[]|select(.rel_type|IN("parent_of","child_of"))'

# Expected: Both (user, parent_of, gabby) and (gabby, child_of, user) in response
```

### 6. Update `scratch.md`

Add entry under "## Current Status":

```markdown
## 🔄 IN PROGRESS: dprompt-70b (Bidirectional Relationship Completeness)

**Status:** Implementation in progress

**What:** Fix incomplete bidirectional relationships (dBug-012)
- Phase A: Extract prompt now mandates both-directional emission
- Phase B: Ingest auto-creates missing inverses

**Files changed:**
- openwebui/faultline_tool.py (extraction prompt)
- src/api/main.py (_validate_bidirectional_relationships)

**Tests:** [test results here]

**Validation:** [pre-prod results here, if tested]

**Next:** Awaiting user review and approval before commit.
```

### 7. STOP & Report

**Do NOT commit.** Code complete, tests pass.

Update scratch.md with final summary:

```markdown
## ✓ DONE: dprompt-70b (Bidirectional Relationship Completeness) — 2026-05-15

**Task:** Fix incomplete bidirectional relationships (dBug-012)

**Changes:**
- openwebui/faultline_tool.py: Added bidirectional emission instruction to `_TRIPLE_SYSTEM_PROMPT`
- src/api/main.py: Updated `_validate_bidirectional_relationships()` to auto-create missing inverses

**Result:** 
- New facts: both directions created automatically
- Existing gaps: handled by ingest auto-create on next ingestion
- No stale data cleaned (separate migration task)

**Tests:** [number] passed, 0 regressions ✓

**Validation:** 
- Extract → emit both directions ✓
- Ingest → auto-create missing inverse ✓
- Graph traversal → complete bidirectional paths ✓

**Status:** AWAITING USER REVIEW — ready to commit when approved
```

Then **STOP immediately**. Await user decision: commit, refine, or adjust.

---

## Deliverable

**Files modified:**
- `openwebui/faultline_tool.py` — extraction prompt updated
- `src/api/main.py` — ingest validation enhanced

**Code state:** Compiles, tests pass, 0 regressions, no commits yet.

---

## Files to Modify

| File | Location | Change |
|------|----------|--------|
| `openwebui/faultline_tool.py` | Lines 96–110 (after existing RELATIONSHIP RULES) | Add BIDIRECTIONAL EMISSION rule block |
| `src/api/main.py` | Lines ~2590–2620 (_validate_bidirectional_relationships) | Add auto-inverse creation logic after conflict handling |

---

## Success Criteria

✅ Extraction prompt explicitly instructs bidirectional emission  
✅ Ingest auto-creates missing inverses with same confidence + fact_class  
✅ All tests pass (new + existing)  
✅ 0 regressions  
✅ No commits made yet  
✅ Summary in scratch.md  
✅ Pre-prod manual validation confirms behavior (optional but recommended)

---

## Critical Rules

**NO COMMITS.** Code complete, tests pass, user reviews and approves.

**TWO FILES ONLY.** faultline_tool.py + main.py. No schema changes.

**TESTS PASS.** Zero regressions, no skipped tests.

**STOP CLAUSE MANDATORY.** Report completion, await user decision.

---

**Template version:** 1.0  
**Philosophy:** Semantic completeness via extraction + ingest resilience  
**Status:** Ready for deepseek execution
