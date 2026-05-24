# dBug-055: Scalar Schema & Correction Flow — Comprehensive Memory Engine Gaps

**Status:** 🔴 OPEN / BLOCKING  
**Severity:** CRITICAL (Blocks correction matching, temporal facts, pattern learning)  
**Date:** 2026-05-19  
**Component:** entity_attributes schema + correction matching + growth model architecture  
**Related:** dBug-054 (scalar routing), dprompt-117 (correction early exit), dprompt-119 (growth model)

---

## Problem Statement

`entity_attributes` table (where scalar facts like age are stored) is missing critical metadata for correction validation and temporal facts:

1. **No timestamps** - Cannot determine which value is current or track correction history (12→14→16)
2. **No temporal context** - Time-bound facts like "in 4 days I need to shit" cannot be stored with their time context
3. **No correction sequence** - Pattern learning requires knowing correction order/history

### Current Schema (INCOMPLETE)
```
entity_id | attribute | value_text | value_int | value_float | value_date | ...
${CHILD1} UUID  | age       | NULL       | 12        | NULL        | NULL       |
${CHILD1} UUID  | age       | NULL       | 14        | NULL        | NULL       | (overwrites previous)
```

**Problem:** When value_int=14 overwrites 12, there's no record of the correction sequence (12→14→16) or when each update occurred.

### Needed Schema (COMPLETE)
```
entity_id | attribute | value_text | value_int | value_float | value_date | created_at | updated_at | temporal_context | provenance
${CHILD1} UUID  | age       | NULL       | 12        | NULL        | NULL       | 2026-05-19 | 2026-05-19 | NULL             | user_stated
${CHILD1} UUID  | age       | NULL       | 14        | NULL        | NULL       | 2026-05-19 | 2026-05-19 | NULL             | correction
${CHILD1} UUID  | age       | NULL       | 16        | NULL        | NULL       | 2026-05-19 | 2026-05-19 | NULL             | correction
---
user UUID | action    | NULL       | NULL      | NULL        | NULL       | 2026-05-19 | 2026-05-22 | "in 4 days"      | user_stated
```

---

## Why This Matters

### Correction Validation Flow
When user says "${CHILD1} is 14 not 12":
1. Parse: entity=${CHILD1}, action=correction, rel_type=age, old=12, new=14
2. **Match correction pattern** against database
3. If pattern exists (12→14): use it to update
4. If pattern doesn't exist: create new pattern with weight
5. When threshold met: pattern picked up by ingest

**Current blocker:** No way to:
- Verify old value matches (12)
- Track correction sequence (12→14→16)
- Determine "latest" value when multiple corrections exist
- Match correction pattern to existing corrections

### Temporal Facts
Facts that are time-dependent:
- "In 4 days I need to go to the dentist"
- "Next Tuesday I have a meeting"
- "Tomorrow is my birthday"

**Current blocker:** No temporal_context column, so these facts cannot be stored meaningfully.

---

## Solution

Add columns to `entity_attributes` table:

```sql
ALTER TABLE entity_attributes
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now(),
  ADD COLUMN IF NOT EXISTS temporal_context TEXT,  -- "in 4 days", "next Tuesday", etc.
  ADD COLUMN IF NOT EXISTS provenance TEXT;        -- "user_stated", "correction", "inferred"
```

Update ON CONFLICT logic to preserve correction history:
- Don't overwrite, append new row with new updated_at
- OR use versioning: mark old value as superseded_at, insert new value
- OR keep history table separate

---

## Correction Matching Logic (Once Schema Fixed)

```
User: "${CHILD1} is 14 not 12"
Parse: entity_id=${CHILD1}, rel_type=age, old_value=12, new_value=14

Query corrections table:
  SELECT * FROM correction_patterns 
  WHERE rel_type='age' AND old_value=12 AND new_value=14
  
If found: Use pattern (weight, confidence) to update
If not: Create new pattern with base weight, evaluate when threshold met

Track sequence: 
  SELECT value_int, updated_at FROM entity_attributes
  WHERE entity_id=${CHILD1} AND attribute='age'
  ORDER BY updated_at
  RESULT: 12 (2026-05-19 01:00) → 14 (2026-05-19 01:05) → 16 (2026-05-19 01:10)
  PATTERN: Ascending corrections, increases by 2 each time
  MATCH: Next correction 14→18 would match pattern
```

---

## Impact

- ✅ Correction validation strengthened (can match old→new)
- ✅ Temporal facts supported (time-bound facts with context)
- ✅ Correction history tracked (12→14→16 shows learning curve)
- ✅ Pattern matching on correction sequences enabled
- ✅ Scales to memory engine: user corrections teach the system

---

---

## Issue 2: Growth Model Wrongly Designed (dprompt-119 Architectural Flaw)

**Current state:** dprompt-119 implements anomaly confidence penalties for scalar values (e.g., age=969 gets 0.3 penalty)

**Problem:** This treats FaultLine as a **validator** instead of a **memory engine**.

### Why This Is Wrong

Memory engine philosophy:
- User says "Methuselah is 969" → **Store it** (confidence from source, not validation)
- User later says "Actually he's from biblical times, lived 969 years" → **Correction supersedes**
- System **learns** from correction pattern (not rejects anomalies)

Validator philosophy (what we built):
- User says "Methuselah is 969" → **Flag as anomaly** (confidence penalty)
- System **rejects high anomalies** (brittle, hardcoded rules)
- Doesn't scale to "in 4 days I need to shit" or other non-numeric temporal facts

### The Fix

**Remove:**
- Anomaly confidence penalty logic (lines 5750-5770 in main.py)
- Growth model validation in WGM gate (gate._validate_against_growth_model calls)
- Anomaly threshold from rel_types metadata

**Keep:**
- Simple distribution learning (what values exist for each rel_type)
- Used for pattern recognition, not rejection

**Result:** Ingest stores facts as-is. Correction flow updates them. Growth model learns from corrections (12→14→16 pattern).

---

## Issue 3: Correction Matching Logic Not Implemented

**Current state:** dprompt-117 adds correction early exit (returns after correction) but doesn't implement **pattern matching**.

**Missing:** When user says "${CHILD1} is 14 not 12", the system should:

1. **Parse correction** → entity=${CHILD1}, action=correction, rel_type=age, old=12, new=14
2. **Query correction_patterns** → Does pattern (12→14) exist?
3. **If found:** Use pattern metadata (weight, confidence) to update fact
4. **If not found:** Create new pattern, queue for evaluation when threshold met

### Why This Matters

Without pattern matching:
- Every correction is treated as a one-off (no learning)
- Corrections (12→14→16) don't build into recognizable sequences
- System can't anticipate next correction based on pattern

With pattern matching + correction history (temporal context):
- Sequence 12→14→16 becomes a learned pattern
- Next correction 14→18 matches pattern (ascending by 2)
- System recognizes correction pattern and applies it

### What Needs Implementation

```
correction_patterns table:
  rel_type | old_value | new_value | weight | confidence | occurrence_count | approved

Correction flow:
  1. Parse correction from text
  2. Query: SELECT * FROM correction_patterns WHERE rel_type=? AND old=? AND new=?
  3. If found: Apply pattern confidence to update
  4. If not found: INSERT into correction_patterns with weight=base, evaluate when occurrence_count >= threshold
  5. When threshold met: Pattern becomes active for matching

Correction history:
  Query entity_attributes with created_at/updated_at to build sequence (12→14→16)
  Use sequence to match against learned patterns
  Update entity_attributes with new value + provenance='correction'
```

---

## Issue 4: Growth Model Not Learning From Corrections

**Current state:** re_embedder calculates value_distribution from confirmed facts but doesn't use correction patterns.

**Problem:** Distribution should reflect **learned patterns from corrections**, not just final values.

### Example

User corrections for ${CHILD1} age:
```
2026-05-19 01:00: 12 (initial)
2026-05-19 01:05: 14 (user correction: "he's 14 not 12")
2026-05-19 01:10: 15 (user correction: "actually 15")
```

**Pattern learned:** Age tends toward higher values (user refining estimate)

**Distribution should track:**
- Correction sequence (12→14→15)
- Temporal spacing (5 min, 5 min)
- Direction (ascending)
- Frequency (multiple corrections suggest uncertainty)

**Current approach:** Just stores final value (15), loses context.

### What Needs Implementation

```
correction_patterns evaluation:
  1. Track correction sequences (12→14→16)
  2. Calculate: direction, rate of change, frequency
  3. Weight: User making repeated corrections = important pattern
  4. Approval: When pattern_occurrence_count >= threshold, mark as "learned"
  5. Future: Extract similar patterns from conversation (e.g., "my kids are 12 and 10" shows sibling age gap)
```

---

## Interconnected Issues

All four issues are **tightly coupled**:

```
Entity stores age=12 → User corrects "14 not 12" → Correction matching looks up pattern
                                                    ↓
                                          Pattern doesn't exist → Create new one
                                                    ↓
                                          re_embedder evaluates correction sequence (12→14→16)
                                                    ↓
                                          Pattern approved when threshold met
                                                    ↓
                                          Distribution learns: ascending correction pattern
                                                    ↓
                                          Next similar correction recognized (14→18)
```

**Sequence:**
1. Fix schema (add timestamps + provenance) → Enable history tracking
2. Remove growth model penalties → Simplify to learning, not validation
3. Implement correction matching → Parse, match, create patterns
4. Implement pattern learning → re_embedder evaluates correction sequences

---

## Implementation Order

### Phase 1: Schema Fix (BLOCKING)
- Add created_at, updated_at, temporal_context, provenance to entity_attributes
- Add versioning: Keep correction history, don't overwrite

### Phase 2: Simplify Growth Model (UNBLOCK)
- Remove anomaly_confidence_penalty logic
- Keep simple distribution learning
- Update re_embedder to learn from correction sequences instead of calculating anomalies

### Phase 3: Correction Matching (CORE FEATURE)
- Implement correction parsing (entity, action, rel_type, old, new)
- Build correction_patterns table + matching logic
- Create pattern evaluation framework (weight, confidence, threshold)

### Phase 4: Pattern Learning (SCALE)
- re_embedder evaluates correction sequences
- Track direction, rate, frequency
- Approve patterns when threshold met
- Use patterns to recognize similar corrections in future

---

## Testing Strategy

### Unit: Correction Parsing
```
Input: "${CHILD1} is 14 not 12"
Expected: {entity: "${CHILD1}", rel_type: "age", old: 12, new: 14, action: "correction"}
```

### Integration: Correction Matching
```
Store: ${CHILD1} age=12
Correct: "${CHILD1} is 14 not 12"
Query: correction_patterns → (age, 12→14) → Found/Not found
Action: Update ${CHILD1} age=14 with provenance='correction'
```

### E2E: Pattern Learning
```
Correction 1: 12→14
Correction 2: 14→16
Pattern emerges: +2 increment
Correction 3: 16→18 → System recognizes pattern (high confidence match)
```

---

## Summary

**Current state:** Scalars stored correctly but correction flow is incomplete. Growth model misdesigned as validator instead of learner.

**Blocking issue:** No temporal context or correction history in entity_attributes.

**Critical gap:** Correction pattern matching not implemented (standardized translation is weak).

**Next phase:** Fix schema, simplify growth model, build correction matching + learning.

---

**Status: Four interdependent issues identified. Schema fix unblocks everything else. Ready for Phase 1 implementation.**
