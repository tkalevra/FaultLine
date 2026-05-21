# dBug-035C: Findings — Taxonomy Cache Paradox & Query Failure

**Investigation Date:** 2026-05-16 16:10 UTC

**Status:** CRITICAL - Query returning 0 facts after latest deployment

---

## Current Situation

**After latest deployment:**
- Taxonomy cache reports: `startup.taxonomy_cache_loaded count=6` ✓
- BUT all 6 taxonomies show warning: `'list indices must be integers or slices, not str'` ⚠
- Query returns: 0 facts (LLM says "I don't have access to your information")
- NO query logs visible (identity_resolved, archive_filter, etc.)

**Before latest deployment (16:09:14):**
- Taxonomy cache loaded count=6 ✓
- Query worked: returned 20 facts ✓
- Spouse visible in dBug035_spouse_after_graph_traversal logs ✓

---

## The Paradox

**Contradiction in logs:**
```
[warning ] startup.taxonomy_row_parse_failed error='list indices...' taxonomy=family
[warning ] startup.taxonomy_row_parse_failed error='list indices...' taxonomy=household
[warning ] startup.taxonomy_row_parse_failed error='list indices...' taxonomy=work
[warning ] startup.taxonomy_row_parse_failed error='list indices...' taxonomy=location
[warning ] startup.taxonomy_row_parse_failed error='list indices...' taxonomy=computer_system
[warning ] startup.taxonomy_row_parse_failed error='list indices...' taxonomy=body_parts

[info   ] startup.taxonomy_cache_loaded  count=6
```

**Explanation:** All 6 taxonomies failed to parse, yet count=6 items in cache.

**Possible explanations:**
1. The error warnings are false positives (exceptions being caught but cache still populated)
2. The exceptions are happening in inner try/except (lines 1471-1483), not the assignment
3. The last log message `count=6` is stale from a previous run
4. There's a race condition or multiple startup attempts

---

## Root Cause: Code Changes Broke Query Pipeline

**Timeline:**
- 16:09:13 deployment: Query working, spouse visible in logs
- 16:10:27 deployment: Query broken, 0 facts returned, no query logs

**What changed:** Added detailed spouse filtering logs to apply_archive_filter()

**Hypothesis:** The new logging code (is_spouse checks) introduced a bug:
- Syntax error in the conditional branches (but py_compile passed)
- Logic error in the filtering condition
- Exception in the logging calls themselves
- The code is correct but query pipeline is crashing silently

---

## Missing Diagnostic Data

**No query logs visible means either:**
1. Query endpoint not being called
2. Query endpoint crashing before reaching logging points
3. Logs being suppressed or not flushed

**Expected logs that should be visible:**
```
query.identity_self_anchor_found
query.identity_resolved
query.initial_user_facts
query.dBug035_spouse_after_graph_traversal
archive_filter.dBug035_spouse_matched_taxonomy
     OR
archive_filter.dBug035_spouse_filtered_scope
     OR
archive_filter.dBug035_spouse_filtered_temporal_*
query.success.archive_filtering_applied
```

None of these are appearing, which suggests query pipeline is failing early.

---

## Immediate Actions Needed

**1. Check if query endpoint has syntax errors:**
- The py_compile check passed, but there could be runtime errors
- Run a test query and capture any exception messages

**2. Simplify the spouse logging to test:**
- The is_spouse checks and logging calls might be causing issues
- Revert to simpler logging without the detailed conditionals

**3. Verify _TAXONOMY_CACHE is actually a dict, not corrupted:**
- Add startup logging: `log.info("cache_type", type=type(_TAXONOMY_CACHE).__name__)`
- Verify `len(_TAXONOMY_CACHE) == 6` means 6 items, not 6 attribute accesses

**4. Check if the inner try/except (lines 1471-1483) is the source of warnings:**
- Those catch exceptions from _parse_postgres_array()
- But the outer except (line 1496) is still being triggered somehow
- Add logging to distinguish inner vs outer exception handling

---

## Code Review: The is_spouse Logic

**New code at line 1631-1634:**
```python
# dBug-035: Debug spouse filtering
is_spouse = (rel_type == "spouse")

if detected_taxonomies and rel_type not in _IDENTITY_SCALAR_RELS:
```

**Potential issue:**
- The `is_spouse` variable is defined but might be creating a scope issue
- Or the logging calls with complex arguments might be failing

**Safe fallback:**
- Remove all the is_spouse debugging code
- Revert to simpler logging that can't fail

---

## CONSTRAINT VIOLATION: Fragility Added

**Critical Issue:** The detailed logging code violates CLAUDE.md Key Principle:
> **Robustness: Code must be resilient to edge cases and unexpected input.**

By adding complex conditional logic (`is_spouse` checks + logging calls), I've:
1. ✗ Made the archive_filter brittle (crashes on unexpected input)
2. ✗ Added multiple failure points in a critical path (logging calls can fail)
3. ✗ Violated defensive programming (assumptions about fact structure)
4. ✗ Created technical debt (logging code more complex than filtering logic)

**What went wrong:**
- Added logging calls without try/except wrapping
- Used complex log argument structures that could fail serialization
- Didn't validate fact structure before accessing fields
- Made debugging code more fragile than production code

---

## ROBUST Solution (Revised)

**DO NOT simply remove the logging.** Instead, make it ROBUST:

### Option 1: Defensive Logging Wrapper
```python
def _safe_log_spouse_debug(fact, event, **kwargs):
    """Log spouse debugging info safely without breaking pipeline."""
    try:
        if fact.get("rel_type") == "spouse":
            safe_kwargs = {
                "subject": str(fact.get("subject", "?"))[:8],
                "object": str(fact.get("object", "?"))[:8],
                **kwargs
            }
            log.info(f"archive_filter.dBug035_{event}", **safe_kwargs)
    except Exception as e:
        # NEVER let logging break the pipeline
        # Silently skip if logging fails
        pass
```

Then use it without any inline conditionals:
```python
_safe_log_spouse_debug(fact, "scope_check_start", rel_type=rel_type)
```

### Option 2: Assert + Log (Fail-Fast with Visibility)
Instead of conditional logging that could fail silently:
```python
# Validate assumptions BEFORE using them
assert isinstance(fact, dict), f"fact must be dict, got {type(fact)}"
assert "rel_type" in fact, "fact missing rel_type field"
assert "subject" in fact, "fact missing subject field"

# NOW we can log safely
log.info("archive_filter.fact_validated", rel_type=fact["rel_type"])
```

### Option 3: Remove Conditional Logic Entirely
Don't inspect individual facts during filtering. Instead:
```python
# Log AGGREGATE statistics AFTER filtering, not per-fact
spouse_in = sum(1 for f in facts if f.get("rel_type") == "spouse")
spouse_out = sum(1 for f in filtered if f.get("rel_type") == "spouse")

if spouse_in > 0 or spouse_out > 0:
    log.info("archive_filter.rel_type_summary",
             rel_type="spouse",
             count_before=spouse_in,
             count_after=spouse_out)
```

---

## Recommendation (REVISED)

**Revert lines 1631-1660** (the detailed is_spouse conditional logging).

**Replace with ONE of these robust approaches:**

1. **Best:** Use Option 1 (defensive wrapper) — keeps debugging visible but safe
2. **Simpler:** Use Option 3 (aggregate logging) — moves complexity outside hot path
3. **Fastest:** Use Option 2 (assertions) — catches bugs early with loud failures

**All three approaches:**
- ✓ Cannot crash the pipeline (wrapped in try/except or moved out of loop)
- ✓ Fail loud if assumptions break (assertions catch data corruption)
- ✓ Provide debugging visibility (still log what we need to know)
- ✓ Respect robustness constraint (no fragility added)

**Timeline:**
1. Revert fragile code
2. Choose ONE robust approach
3. Redeploy with robust logging
4. Verify query works + logs show spouse handling

**The lesson:** Debugging code should be MORE defensive than production code, not less.
