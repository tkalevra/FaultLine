# dBug-report-005: Alias Redundancy & Query Deduplication

**Date:** 2026-05-12  
**Severity:** P1 (UX — confuses user, breaks query accuracy)  
**Status:** Investigation complete — root cause identified

## Symptom

Query response "Tell me about my family" returns duplicate/conflicting information:

```
"Your family includes your spouse, Mars, and your wife..."
"...your household also includes... a Morkie named Fraggle... and another pet named morkie."
"Cyrus is also one of your children."
```

Issues:
1. Spouse listed twice (Mars and Wife as separate entities)
2. Morkie appears as separate pet (should only be Fraggle)
3. Cyrus listed redundantly
4. Family relationships show duplicate facts

## Investigation Findings

### Alias Redundancy Problem

User entity has multiple aliases: `christopher`, `chris`, `user`

Database stores duplicate facts for EACH alias:

```
christopher spouse mars        (conf 0.5, Class A)
chris spouse mars              (DUPLICATE — same entity, different alias)
christopher spouse wife        (conf 1.0, Class A)
chris spouse wife              (DUPLICATE)
```

Query returns both facts separately, appearing as duplicate information to user.

### Semantic Impossibility

Cyrus entity has conflicting relationships:
```
cyrus child_of christopher     (correct — Cyrus is a child)
cyrus parent_of christopher    (WRONG — Cyrus can't be a parent of himself)
```

This shouldn't exist. Extraction logic allowed a semantic impossibility to be stored.

### Name Conflict Unresolved

Multiple spouse facts pointing to different entity names:
```
christopher spouse mars        (conf 0.5)
christopher spouse marla       (conf 0.5) — DIFFERENT PERSON?
christopher spouse wife        (conf 1.0) — GENERIC ALIAS
```

Should be: one spouse entity with all aliases, highest-confidence preferred name.

### Root Cause

**Query deduplication gap:** `/query` returns facts without deduplicating by entity_id. When entities have multiple aliases, duplicate facts appear in results.

**Extraction gap:** `child_of` and `parent_of` extraction doesn't validate semantic rules (e.g., no entity can be both child and parent of another).

## Fix Direction

### Short term (Query deduplication)
Modify `/query` response to:
1. Deduplicate facts by `(subject_id, rel_type, object_id)` — only return unique triples
2. When multiple facts exist (via different aliases), prefer highest confidence
3. Return facts using entity's **preferred_name only**, not all aliases

Result: "Your spouse is Mars" (not "...and your wife, both of whom...")

### Medium term (Extraction validation)
Add semantic validation to extraction:
- Prevent bidirectional relationships that don't make sense (child_of + parent_of for same pair)
- Validate role cardinality (spouse = 1, not 3)
- Use existing ontology constraints to block impossible relationships

### Long term (Alias consolidation)
Name conflict resolution (dprompt-32b logic) should:
- Detect `mars` vs `marla` as potential duplicates
- Merge into single entity OR remove weaker fact
- Use preferred_name consistently across queries

## Test Case

**Input:** "Tell me about my family"

**Current output:**
```
Your family includes your spouse, Mars, and your wife...
```

**Expected output (after fix):**
```
Your family includes your spouse Mars and three children: Des, Gabby, and Cyrus...
```

**Database state (for testing):**
- User has aliases: christopher, chris, user
- Spouse: mars/marla (multiple facts, should be one)
- Children: Des, Gabby, Cyrus (no bidirectional parent_of/child_of confusion)
- Pets: Fraggle (morkie type, not separate pet — conflict detection works)

## Affected Components

- `/query` endpoint — needs deduplication logic
- Extraction prompt — needs semantic validation rules
- Entity name conflict resolution — needs improvement

## Next Steps

- **dprompt-61:** Query deduplication (short-term fix)
- **dprompt-62:** Extraction semantic validation (medium-term)
- **dprompt-63:** Name conflict resolution enhancement (long-term)

Recommend tackling dprompt-61 first (highest impact on UX).

## References

- CLAUDE.md — /query path, deduplication notes
- dBug-report-032b.md (future) — name conflict resolution
- src/api/main.py — `/query` endpoint
- openwebui/faultline_tool.py — Filter deduplication
