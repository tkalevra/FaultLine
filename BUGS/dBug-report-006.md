# dBug-report-006: Query Response Shows Incorrect Pets + Database Integrity Issues

**Date:** 2026-05-13  
**Severity:** P1 (Data integrity + query accuracy both broken)  
**Status:** Investigation complete — root causes identified across multiple layers  

## Symptom

Query "Tell me about my family" returns:

```
You have two dogs named Morkie and Fraggle.
Mars specifically looks after Fraggle, a dog who is actually a Morkie mix 
(sometimes referred to as a 7E4Bff75-706E-mix).
```

Issues:
1. Morkie listed as separate dog (should be type only, not separate entity)
2. UUID exposed in response (7E4Bff75-706E-mix) — should be hidden, display name only
3. Two dogs claimed when only Fraggle should be returned
4. Missing Cyrus from children list (response shows Des + Gabby, should show Des + Gabby + Cyrus)
5. Spouse shown as "Mars (who you also refer to as your wife)" when should be deduplicated

## Investigation Findings

### Database State (Pre-Prod)

**Staged facts (unconfirmed Class B):**
```
has_pet fraggle (conf=0.8) ✓ Correct
owns morkie (conf=0.8) ✗ WRONG — morkie is a type, not entity
owns fraggle (conf=0.8) ✗ Redundant with has_pet
```

**Committed facts (facts table):**

Bidirectional impossible relationships:
```
Des (0638cc40):
  child_of user (conf=0)
  parent_of user (conf=0) ← IMPOSSIBLE (inverse redundancy)

Cyrus (550fc016):
  child_of user (conf=1)
  parent_of user (conf=0) ← IMPOSSIBLE (inverse redundancy)

Gabriella (d4bf6c7b):
  parent_of user (conf=0) ← one-way only, incomplete

Spouse facts (should be 1, found 3):
  wife (3b244a4c) conf=1 ✓ Correct, highest confidence
  mars (54214459) conf=0.5 ✗ Redundant
  unknown (fb331dd4) conf=0 ✗ Weakest
```

Hierarchy (correct):
```
fraggle instance_of morkie (conf=1) ✓ Correct
```

**No has_pet facts in facts table** — user has no committed pet relationships, only staged.

### Root Causes

**Layer 1: Extraction/Staged Facts**
- Extraction created conflicting staged facts: `owns morkie` + `owns fraggle` when only `has_pet fraggle` should exist
- Extraction constraint (dprompt-58) prevented `owns morkie` in facts table, but DID allow it in staged_facts
- **Issue:** Constraint applied to ingest commit, not to staged fact creation

**Layer 2: Conflict Detection**
- dprompt-59 semantic conflict detection runs BEFORE fact class assignment in `/ingest`
- **Issue:** Only catches facts destined for facts table Class A/B/C assignment
- **Issue:** Staged facts inserted via `/ingest` may not trigger conflict detection if they bypass the gate

**Layer 3: Bidirectional Impossibilities**
- Des and Cyrus have BOTH child_of AND parent_of relationships to user
- These are **semantic impossibilities** — inverse relationships shouldn't both exist
- **Root cause:** No validation preventing bidirectional relationships that contradict each other

**Layer 4: Spouse Deduplication**
- dprompt-61 query deduplication should have filtered spouse facts to highest confidence (wife, conf=1)
- Multiple spouse facts still in database and being returned
- **Issue:** Dedup may not be working, or staged facts bypass dedup

**Layer 5: UUID Exposure**
- Response shows "7E4Bff75-706E-mix" — this is the UUID of Fraggle entity
- The text "mix" suggests a concatenation: UUID + rel_type or descriptor
- **Issue:** Response builder exposing entity UUIDs instead of display names

### Test Case

**Input:** "Tell me about my family" (or `/query?user_id=...`)

**Current output:**
```
You have two dogs named Morkie and Fraggle. Mars specifically looks after 
Fraggle, a dog who is actually a Morkie mix (sometimes referred to as a 
7E4Bff75-706E-mix).
```

**Expected output:**
```
Your family includes your spouse Mars and three children: Des, Cyrus, 
and Gabriella. You have one dog named Fraggle, a Morkie mix.
```

## Affected Components

1. **Extraction pipeline** — dprompt-58 constraint not applied to staged facts
2. **Conflict detection** — dprompt-59 may not catch staged fact conflicts
3. **Semantic validation** — No bidirectional relationship prevention (impossible relationships like child_of + parent_of)
4. **Query deduplication** — dprompt-61 may not be filtering staged facts correctly
5. **Response builder** — UUIDs being exposed in natural language output

## Implications

This reveals that:
- **Extraction constraint (dprompt-58)** doesn't fully prevent conflicting facts (only in facts table, not staged)
- **Conflict detection (dprompt-59)** may have gaps for staged facts or certain fact patterns
- **Bidirectional validation** is missing — semantic rules should prevent inverse relationships that contradict
- **Query deduplication (dprompt-61)** may not handle staged facts or UUID resolution correctly
- **Layered validation** has a gap: facts can be stored in different states with different validation rules

## Remediation

### Immediate (Pre-Prod Database Cleanup)

**Delete conflicting staged facts:**
```sql
DELETE FROM staged_facts 
WHERE subject_id = '3f8e6836-72e3-43d4-bbc5-71fc8668b070'
AND rel_type IN ('owns')
AND object_id IN ('e9b7f50c-17a7-5cdc-b7ac-27620160c1bd', '7e4bff75-706e-5feb-b8b5-f4ca1247fd3b');
```

**Delete bidirectional impossible parent_of facts:**
```sql
DELETE FROM facts
WHERE subject_id = '3f8e6836-72e3-43d4-bbc5-71fc8668b070'
AND rel_type = 'parent_of'
AND confidence = 0;
```

**Delete lower-confidence spouse facts:**
```sql
DELETE FROM facts
WHERE subject_id = '3f8e6836-72e3-43d4-bbc5-71fc8668b070'
AND rel_type = 'spouse'
AND confidence < 1;
```

### Short Term (Code Fixes)

**dprompt-62:** Extend conflict detection to staged facts
- Apply semantic validation to staged facts BEFORE insertion
- Prevent `owns type_entity` and `has_pet type_entity` patterns in staged facts

**dprompt-62b:** Bidirectional validation
- Prevent storing both child_of AND parent_of for same entity pair
- If both exist, keep highest confidence version only
- Apply during ingest to facts table

**dprompt-61 Review:** Verify deduplication handles:
- Staged facts (Class B) correctly
- Multiple aliases for same entity
- UUID resolution to display names

## References

- dBug-report-005.md — Alias redundancy, first dedup investigation
- dprompt-58.md/58b.md — Extraction constraint (incomplete)
- dprompt-59.md/59b.md — Conflict detection (incomplete for staged facts)
- dprompt-61.md/61b.md — Query deduplication (may have gaps)
- CLAUDE.md — Staged facts, semantic conflict detection, extraction pipeline

---

**Status:** Root causes identified. Recommend:
1. Clean pre-prod database now (restore query accuracy)
2. dprompt-62: Extend validation to staged facts + bidirectional impossibilities
3. Review dprompt-61 for staged fact handling
