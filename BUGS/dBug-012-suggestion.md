# dBug-012: Incomplete Bidirectional Relationships

**Status:** Investigation complete — fix approach scoped  
**Severity:** P2 — semantic completeness (data is correct directionally but graph traversal misses some paths)  
**Date:** 2026-05-15

## Summary

The knowledge graph is missing inverse relationships for bidirectional rel_types (parent_of/child_of, spouse/spouse). This causes `/query` graph traversal to miss some connected entities when walking only one direction.

## Pre-Prod Database State (Verified)

### parent_of / child_of gaps

| Subject | Rel | Object | Conf | Inverse Status |
|---------|-----|--------|------|---------------|
| charlie | child_of | chris | 1.0 | ✓ `chris parent_of charlie` exists |
| **bob** | child_of | chris | 1.0 | ✗ **MISSING** `chris parent_of bob` |
| **alice** | parent_of | chris | 1.0 | ✗ **WRONG DIRECTION** — should be `alice child_of chris` |
| chris | parent_of | alice | 0.5 | ✓ Correct direction, but low conf |
| chris | parent_of | charlie | 1.0 | ✓ Correct |

### spouse gap

| Subject | Rel | Object | Conf | Inverse Status |
|---------|-----|--------|------|---------------|
| chris | spouse | emma | 1.0 | ✗ **MISSING** `emma spouse chris` (symmetric) |

### sibling_of (not bidirectional but worth noting)

Both directions exist for charlie↔alice↔bob — sibling_of facts look complete.

## Impact

- `/query` returns bob via `child_of` traversal but NOT via `parent_of` — family walk that only uses parent_of misses her
- alice has wrong-direction fact (`alice parent_of chris` at conf=1.0) — if ingested fresh, bidirectional validation would supersede the lower-conf version, but stale data predates the fix
- emma has only one spouse direction — symmetric rel_type should have both

## Root Causes

1. **LLM extraction prompt** (dprompt-69b scope): The `_TRIPLE_SYSTEM_PROMPT` doesn't instruct the LLM to emit both directions for inverse rel_types. Lines 96-99 define direction semantics but don't mandate bidirectional emission.

2. **Ingest pipeline** (dprompt-62 scope): `_validate_bidirectional_relationships()` only handles CONFLICTS (both directions exist, pick higher confidence). It does NOT auto-create missing inverses.

## Suggested Fix (Two-Phased per dprompt-69b Recommendation)

**Phase A: Prompt change (openwebui/faultline_tool.py, line 99):**
```diff
+ - BIDIRECTIONAL EMISSION: For parent_of/child_of and spouse, ALWAYS emit BOTH
+   directions. If (user, parent_of, alice), also emit (alice, child_of, user).
```

**Phase B: Ingest auto-create (src/api/main.py, _validate_bidirectional_relationships):**
Return "create_inverse" when no inverse exists, and at the call site, append an auto-created inverse row with the same confidence and fact_class.

**Stale data cleanup:** Existing gaps (`bob child_of chris` → missing parent_of, `alice parent_of chris` → wrong direction, `emma spouse chris` → missing inverse) need manual DB correction or a one-time migration.

## References

- dBug-report-011: Original identification of bidirectional gap for bob
- dprompt-62: Bidirectional validation (conflict handling only)
- dprompt-69b: Recommended combined approach (prompt + ingest auto-create)
