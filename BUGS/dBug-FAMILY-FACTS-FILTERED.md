# dBug-FAMILY-FACTS-FILTERED

**Status:** OPEN  
**Severity:** HIGH (scope-detected facts discarded)  
**Component:** OpenWebUI Filter (`openwebui/faultline_function.py`)  
**Discovered:** 2026-05-22 (dprompt-130 validation)  
**Blocks:** dBug-WEATHER-SCOPE (partially — weather works, family doesn't)

---

## Problem Statement

When user asks "Tell me about my family?", the query scope detection works correctly (detects `family` taxonomy), backend `/query` returns 12 family facts (5 child_of, 5 parent_of, 2 spouse), but **filter discards them before LLM sees them**.

LLM response: "I don't have any verified information about your family members."

### Logs Show Correct Detection → Incorrect Filtering

```
Backend /query (correct):
- determine_query_scope detected_taxonomies=['family']
- query.scope_filter_applied after=12 before=15 query_scope=['family']
- query.initial_user_facts count=12 rel_types={'child_of': 5, 'parent_of': 5, 'spouse': 2}

Filter (incorrect):
- /query status=200 ✓ (facts received)
- [no log showing facts injected] ✗ (facts filtered before injection)
```

---

## Root Cause

Filter's `_apply_confidence_gate()` function in `openwebui/faultline_function.py` lines 1567-1573:

```python
def _apply_confidence_gate(candidates: list[dict]) -> list[dict]:
    if self.valves.MIN_INJECT_CONFIDENCE > 0:
        high_conf = [f for f in candidates
                     if f.get("confidence", 0.0) >= self.valves.MIN_INJECT_CONFIDENCE]
        if high_conf:
            return high_conf  # ← DISCARDS lower-confidence facts
    return candidates
```

**Problem:** Family facts (parent_of, child_of, spouse) are marked as `_IDENTITY_RELS` and pass the first filter (lines 1592-1594), but then `_apply_confidence_gate()` discards them if:

- Family fact confidence = 0.8 (Class B, LLM-inferred)
- Location fact confidence = 1.0 (Class A, user-stated)
- `MIN_INJECT_CONFIDENCE` valve = 0.5 (default)

Since location confidence (1.0) >= threshold (0.5), gate returns ONLY location facts. Family facts (0.8) never reach LLM.

### Confidence Thresholds

| Fact Type | Confidence | Passes Gate? |
|-----------|-----------|------------|
| Location (lives_at, user-stated) | 1.0 | ✓ |
| Family (parent_of, LLM-inferred) | 0.8 | ✗ |
| MIN_INJECT_CONFIDENCE | 0.5 | threshold |

When high-confidence facts exist, ALL facts below threshold are discarded—even identity rels.

---

## Impact

- ✅ Weather query works (location facts only, high confidence)
- ✗ Family query fails (family facts discarded by confidence gate)
- ✗ Work query likely fails (work facts discarded if other high-confidence facts present)
- ✗ ANY query with mixed-confidence facts fails

**Scope-detected facts are authoritative** but discarded by overly aggressive confidence gating.

---

## Solution Options

### Option A: Exempt Identity Rels from Confidence Gate (Recommended)

**IMPLEMENTED (Revised: Metadata-Driven Approach)**

Modify `_apply_confidence_gate()` to skip Class A facts by using `fact_class` field from backend:

```python
def _apply_confidence_gate(candidates: list[dict]) -> list[dict]:
    if self.valves.MIN_INJECT_CONFIDENCE > 0:
        # Class A facts (identity/structural) always pass — backend classified them
        # Class B/C facts (behavioral/contextual) gated by confidence threshold
        class_a = [f for f in candidates if f.get("fact_class") == "A"]
        class_bc = [f for f in candidates if f.get("fact_class") != "A"]

        # Gate only Class B/C facts
        high_conf = [f for f in class_bc
                     if f.get("confidence", 0.0) >= self.valves.MIN_INJECT_CONFIDENCE]

        if high_conf:
            # Return Class A + high-confidence Class B/C
            return class_a + high_conf
        else:
            return candidates  # Fallback: all candidates
    return candidates
```

**Rationale:** Backend classifies facts at ingest time (Class A = identity/structural). Use this metadata instead of hardcoding rel_type names. Respects HARD CONSTRAINT (CLAUDE.md): "rel_types come from DB, not hardcoded." Class A facts are authoritative by definition and always pass through.

### Option B: Lower Threshold

Change `MIN_INJECT_CONFIDENCE` default from 0.5 to 0.0:

```python
MIN_INJECT_CONFIDENCE: float = Field(
    default=0.0,  # was 0.5
    ...
)
```

**Downside:** No confidence filtering at all. Allows low-confidence speculative facts (Class C) to be injected.

### Option C: Backend Signal

Backend should indicate scope-detected facts in response. Filter should skip confidence gating for them.

**Complexity:** Requires API change (add `scope_detected: bool` field to facts).

---

## Testing

### Test 1: Family Query (Currently Fails)
```
Query: "Tell me about my family?"
Expected: 12 family facts injected (parent_of, child_of, spouse)
Actual: 0 facts injected
Root: Confidence gate discards 0.8-confidence facts in favor of 1.0-confidence location facts
```

### Test 2: Family Query After Fix
```
Query: "Tell me about my family?"
Expected: 12 family facts injected
After Option A: ✓ PASS (identity rels exempt from gate)
After Option B: ✓ PASS (no gate)
```

### Test 3: Weather Query (Works, Verify Not Broken)
```
Query: "What's the weather like tomorrow?"
Expected: 1 location fact (lives_at)
After Option A: ✓ PASS (identity rels still returned)
After Option B: ✓ PASS (no gate difference)
```

### Test 4: Mixed-Confidence Query
```
Query: "Tell me about my work and location"
Facts: 3 work (0.8) + 1 location (1.0)
Expected: All 4 facts injected (scope detection matched both)
After Option A: ✓ PASS (work rels non-identity, gate applies, but location rels pass)
After Option B: ✓ PASS (no gate)
```

---

## Files to Modify

- `openwebui/faultline_function.py` — lines 1567-1573 (`_apply_confidence_gate`)

## Recommendation

**Implement Option A.** Identity rels are structural (family, names, identity). They should not be gated by confidence. The confidence threshold should only apply to behavioral/contextual facts (work, location, preferences).

This respects the semantic separation between:
- **Structural facts** (identity, family) — must be injected if detected
- **Contextual facts** (work, location, preferences) — can be gated by confidence

---

## Related

- dBug-WEATHER-SCOPE — Prevented PII leakage in weather queries (dprompt-130 fixed)
- dprompt-130 — Query-scope detection (correctly detects, incorrectly filtered)

---

## Trace

**2026-05-22 02:50:19 UTC**
1. User: "Tell me about my family?"
2. Filter: inlet called, will_query=True
3. Backend: /query 200 OK, detects family scope, returns 12 facts
4. Filter: _filter_relevant_facts() called
   - Family rels pass identity check ✓
   - _apply_confidence_gate() called
   - Location fact (1.0) > MIN_INJECT_CONFIDENCE (0.5) ✓
   - Family facts (0.8) < MIN_INJECT_CONFIDENCE (0.5) ✗
   - Returns only location facts
5. LLM: No family facts in context
6. LLM response: "I don't have information about your family"
