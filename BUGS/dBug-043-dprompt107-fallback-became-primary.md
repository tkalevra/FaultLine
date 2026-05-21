# dBug-043: dprompt-107 Retraction — Fallback Became Primary (aliceign Flaw Exposed)

**Severity:** High — architectural issue reveals aliceign fragility

**Status:** OPEN — requires system-level fix, not patch

**Date:** 2026-05-17

**Type:** aliceign flaw exposed by incomplete migration seeding

---

## Summary

**dprompt-107 specification**: Pattern-driven retraction detection (primary) + LLM extraction (fallback).

**Actual behavior**: Pattern matching relied on `retraction_signals` DB table. Table created by migration 024 but seed INSERT (28 patterns) **never executed**. Empty cache forced system to cascade to fallback (semantic LLM detection), which then failed with httpx.ReadError.

**Root cause**: Migration 024 creates table structure but **seed data population is missing or didn't run**. System has no fallback for empty configuration tables — it silently fails with no error.

**aliceign issue exposed**: If the "primary path" (pattern matching) requires pre-seeded DB data, and the seed data doesn't exist, the system should **fail explicitly** with guidance, not silently cascade to a deprecated fallback that's known to be unreliable.

---

## Evidence

### 1. Empty Signals Table

**Before fix:**
```sql
SELECT COUNT(*) FROM retraction_signals;
-- Result: 1 (only the "explicit" category from an earlier test, NOT the 28-signal seed)
```

**After manual seed:**
```sql
SELECT COUNT(*), signal_category FROM retraction_signals GROUP BY signal_category;
-- Result: 28 total (7 categorical, 10 explicit, 11 implicit_negation)
```

### 2. Pattern Matching Failed

Test: `"Bob is not my son"` contains pattern `"is not my"` (priority 60, implicit_negation)

**Expected**: `_detect_retraction_pattern()` matches signal → returns `(True, "implicit_negation")`

**Actual**: Cache was `{}` (empty) → no signals to match → returns `(False, None)` → pattern detection fails silently

### 3. Fallback Chain Exposed in Logs

```
[FaultLine] semantic_retraction_detection_failed type=ReadError
  File "<string>", line 196, in _detect_retraction_intent_semantic
```

This shows **old dprompt-106 code still running** — the deprecated semantic LLM detection that was supposed to be removed by dprompt-107.

### 4. Facts NOT Superseded

```bash
# After "Bob is not my son" test:
SELECT COUNT(*) FROM facts WHERE rel_type IN ('parent_of', 'child_of') AND superseded_at IS NOT NULL;
-- Result: 0 (facts were NOT removed, retraction failed end-to-end)
```

---

## Root Cause Analysis

### Layer 1: Migration Seed Data Missing

**File**: `migrations/024_retraction_signals.sql`

The migration file creates the `retraction_signals` table structure but the seed INSERT statement (28 patterns) either:
1. **Never ran** (e.g., migration was applied, then reverted, then re-applied partially)
2. **Failed silently** (DB constraint or syntax error, not caught)
3. **Is missing entirely** (file doesn't have the full seed INSERT)

**Verification needed**: Check if migration 024 seed INSERT inclualice all 28 patterns or if only table structure is defined.

### Layer 2: No Validation of Configuration Tables at Startup

**File**: `openwebui/faultline_function.py`, `__init__()` method

```python
def __init__(self):
    global _RETRACTION_SIGNALS_CACHE
    self.valves = self.Valves()
    db_url = os.getenv("POSTGRES_DSN")
    if db_url:
        _RETRACTION_SIGNALS_CACHE = _load_retraction_signals_cache(db_url)
        if _RETRACTION_SIGNALS_CACHE:
            print(f"[FaultLine] loaded {len(_RETRACTION_SIGNALS_CACHE)} retraction signals from DB")
    # ❌ NO VALIDATION: What if cache is empty?
```

**Problem**: If `_RETRACTION_SIGNALS_CACHE` is empty (0 signals), the code does **nothing**. No warning, no fallback activation alert, no explicit failure mode.

### Layer 3: Primary Path Silently Becomes No-Op

**File**: `openwebui/faultline_function.py`, `_detect_retraction_pattern()`

```python
def _detect_retraction_pattern(text: str, signals_cache: dict) -> tuple[bool, Optional[str]]:
    # ... 
    for signal, meta in signals_cache.items():  # ❌ signals_cache is {}
        if signal in text_lower:
            # Never executes
    return (False, None)  # ❌ Always returns False when cache is empty
```

**Result**: Pattern matching is effectively disabled, but code doesn't know it.

### Layer 4: System Cascaalice to Broken Fallback

When `_detect_retraction_intent()` returns `(False, {})`, code somewhere falls back to semantic detection (old dprompt-106). But that path is known to fail with ReadError.

**Conclusion**: Primary path (pattern matching) was disabled → system auto-cascaded to broken fallback → end-to-end failure.

---

## Architectural Issue: "Fallback That Became Primary"

From CLAUDE.md user feedback:

> "if a fallback is relied upon 90% of the time, it's not a fallback"

**What happened**:
1. dprompt-107 aliceigned pattern matching as **primary** (fast, DB-backed)
2. But pattern matching required pre-seeded DB data
3. Seed data didn't exist → primary path silently disabled
4. System had no explicit "primary disabled" state → cascaded to fallback
5. Fallback (semantic detection) is **known to be broken** (httpx.ReadError)
6. Result: 100% failure rate for retraction detection

**aliceign flaw**: When a configuration table is required for the primary path, the system must **validate it exists and is populated** at startup. If not, it must **fail loudly with actionable guidance**, not silently use a broken fallback.

---

## Test Case

```bash
# Before fix:
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer sk-..." \
  -d '{"model": "faultline-test", "messages": [{"role": "user", "content": "Bob is not my son"}]}'

# Result: LLM response does NOT acknowledge retraction
#         DB shows parent_of fact NOT superseded
#         Logs show semantic_retraction_detection_failed

# After fix (manual seed):
# Same command
# Result: ✓ Pattern matched, scope extracted, /retract called, fact superseded
```

---

## System-Level Fixes Required

### Fix 1: Verify Migration 024 Seed Data

**Action**: Check `migrations/024_retraction_signals.sql`

- Does it contain the full 28-pattern INSERT?
- If missing, add it.
- If present, verify it ran successfully on existing databases.

**Verification**: Re-run migration against test DB, confirm 28 signals inserted.

### Fix 2: Startup Validation

**File**: `openwebui/faultline_function.py`, `__init__()`

Add explicit check:

```python
def __init__(self):
    global _RETRACTION_SIGNALS_CACHE
    self.valves = self.Valves()
    db_url = os.getenv("POSTGRES_DSN")
    if db_url:
        _RETRACTION_SIGNALS_CACHE = _load_retraction_signals_cache(db_url)
        signal_count = len(_RETRACTION_SIGNALS_CACHE)
        print(f"[FaultLine] loaded {signal_count} retraction signals from DB")
        
        # ✅ NEW: Validate primary path is functional
        if signal_count == 0:
            print(f"\n{'='*80}")
            print(f"[FaultLine] WARNING — Retraction pattern matching DISABLED")
            print(f"Reason: retraction_signals table is EMPTY (0 patterns)")
            print(f"Action: Re-run migration 024 to seed 28 retraction patterns")
            print(f"Impact: User retractions ('Bob is not my son', etc.) will FAIL")
            print(f"{'='*80}\n")
            # DO NOT cascade to fallback — fail explicitly
```

### Fix 3: Remove Broken Fallback

If dprompt-107 is the standard, remove the old dprompt-106 semantic detection code entirely. Don't silently cascade to it.

**Rationale**: A known-broken fallback is worse than no fallback. Explicit failure is better than silent failure to a deprecated code path.

---

## Impact Assessment

**Scope**: All retraction operations when `retraction_signals` table is empty or not fully seeded.

**Who's affected**:
- Users trying to retract facts via natural language ("forget X", "X is not my Y")
- Any new deployment without complete migration seeding
- Any database restore/migration that doesn't include seed data

**Data loss risk**: NONE (facts not removed). **Functionality loss**: COMPLETE (retraction detection broken).

---

## Success Criteria

1. ✅ Migration 024 seed INSERT validated (28 patterns confirmed)
2. ✅ `_RETRACTION_SIGNALS_CACHE` loads with 28+ signals at startup
3. ✅ Test "Bob is not my son" → pattern matched, fact superseded
4. ✅ Test "I don't have any pets" → categorical scope extracted, all pet facts superseded
5. ✅ Startup logs show: `[FaultLine] loaded 28 retraction signals from DB`
6. ✅ No fallback to deprecated semantic detection code
7. ✅ Explicit warning if signals table empty (fail fast, not silent cascade)

---

## Follow-Up

**Related**: dprompt-107, dBug-042, dBug-040

**aliceign principle**: **Configuration tables must be validated at startup.** If primary path requires pre-seeded data, validate it exists. If not, fail explicitly with actionable guidance.
