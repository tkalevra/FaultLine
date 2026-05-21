# dBug-043: dprompt-107 Inverted Primary/Fallback — LLM Should Be Primary

**Severity:** Critical — architectural inversion violates core principle

**Status:** OPEN — requires aliceign revert/realiceign

**Date:** 2026-05-17

**Type:** Architectural aliceign flaw

---

## Summary

**Core principle** (from Extract path): LLM is primary (confidence-weighted, ontology-aware), pattern matching is fallback (when LLM unavailable).

**dprompt-107 implementation**: Inverted this — made pattern matching primary, semantic LLM detection fallback.

**Result**: System tries pattern matching (fast but brittle, DB-dependent) first, cascaalice to semantic LLM detection (authoritative, but slower) only if patterns don't match. This violates the principle that **user statements are truth**.

**Evidence**: Logs show `semantic_retraction_detection_failed` — the system was correctly attempting the LLM semantic path, but the inversion meant it wasn't prioritized.

---

## Architecture Principle: User Is Truth

From CLAUDE.md and established aliceign:

> **Extract path**: LLM primary (confidence-weighted) → pattern fallback (regex when LLM unavailable)
>
> **Retraction path**: Should follow same principle
>
> **User statements are authoritative**: What user says about their own memory (relationships, facts, corrections) is truth. System must respect this with highest confidence.

**Why LLM is primary**:
1. LLM understands natural language intent ("I don't have a son named Sam" vs "I have no pets")
2. LLM respects relational definitions (knows parent_of ≠ has_pet)
3. LLM respects ontological definitions (knows "pets" is a category)
4. LLM is confidence-weighted (can express uncertainty, respect valve settings)
5. User's direct statement is Class A (1.0 confidence) — highest authority

**Why pattern matching is fallback**:
1. Fast, no LLM call needed
2. Works when LLM unavailable
3. But brittle — can't distinguish intent nuance
4. Requires pre-seeded DB data (adds dependency)

---

## Current Implementation (dprompt-107)

```python
def _detect_retraction_intent(self, text: str) -> tuple[bool, dict]:
    # Layer 1: Pattern matching (fast, NO LLM)
    is_retraction, signal_category = _detect_retraction_pattern(text, _RETRACTION_SIGNALS_CACHE)
    if not is_retraction:
        return (False, {})
    
    # Layer 2: Scope extraction (DB query, NO LLM)
    scope = _extract_categorical_scope(text, self.db_url)
    return (True, scope)
```

**Problem**: Pattern matching is Layer 1 (primary). No LLM call at all — semantic detection (which WAS in dprompt-106) is completely removed or relegated to fallback.

---

## Test Cases Showing the Flaw

### Test 1: Granular Retraction — "I don't have a son named Sam"

**User intent**: Remove the relationship `parent_of(user, sam)` because user corrects: they don't have a son named Sam.

**What should happen**:
1. LLM semantic detection: Understands "I don't have a son" = negation of parent_of relationship + entity (Sam)
2. Extracts: `{subject: "sam", rel_type: "parent_of", scope_level: "granular"}`
3. /retract removes `parent_of(user, sam)` + inverse `child_of(sam, user)`
4. Optionally cascaalice: if Sam has no other relationships to user, entity can be archived

**What dprompt-107 does**:
1. Pattern matching: looks for "I don't have any X" patterns (categorical)
2. "I don't have a son named Sam" doesn't match "don't have any" pattern (too specific)
3. Pattern matching returns False (no match)
4. Semantic detection (if still present) never fires
5. Result: **No retraction happens**

### Test 2: Categorical Retraction — "I have no pets"

**User intent**: Remove ALL pet relationships because user definitively states they don't have any pets.

**What should happen**:
1. LLM semantic detection: Understands "I have no pets" = categorical negation of pet ownership
2. Extracts: `{category: "pets", scope_level: "categorical"}`
3. /retract cascaalice:
   - Supersede ALL `has_pet(user, *)` facts
   - Archive pet entities (now orphaned)
   - Update `entity_taxonomies`: remove pets from user's household membership
4. Result: **All pet facts gone, query "tell me about my pets" returns nothing**

**What dprompt-107 does**:
1. Pattern matching: looks for "don't have any" patterns
2. Text contains "no pets" → matches if pattern DB is populated
3. But scope extraction is DB-only (no LLM contextual understanding)
4. Extracts scope but loses nuance from user's statement
5. Result: **Works IF patterns seeded, fails IF DB empty** (fragile)

### Test 3: LLM Nuance Loss

**User statement**: "Actually, I don't have a son. We're not in contact anymore."

**What LLM would understand**:
- Primary reason: relationship dissolved (not just name correction)
- Emotional context: estrangement (why user no longer claims "son")
- Action needed: remove parent_of relationship

**What pattern matching does**:
- Looks for "I don't have a son" pattern
- If not in DB, returns False
- No understanding of context

**Result**: Pattern matching is too brittle for real user language.

---

## Why dprompt-107 Was Wrong

1. **Inverted the hierarchy**: Made fast/brittle method primary, authoritative method fallback
2. **Removed LLM understanding**: Lost semantic intent detection
3. **Added DB dependency**: Pattern matching requires seeded `retraction_signals` table
4. **Broke user authority principle**: System doesn't respect what user knows about themselves

---

## Correct Architecture (Should Be)

### Layer 1: LLM Semantic Detection (PRIMARY)

**When**: Always attempt first (if LLM available and valve allows)

**How**:
- Send retraction prompt to LLM: "User wants to remove/correct a fact. Extract intent."
- LLM returns: `{subject, rel_type, old_value, scope_level}`
- Respects relational + ontological definitions
- User statement = Class A (1.0 confidence)

**Handles**: All natural language variations (granular, categorical, contextual)

### Layer 2: Pattern Matching (FALLBACK)

**When**: LLM unavailable (timeout, error, valve disabled) or as optimization hint

**How**:
- Fast regex/DB lookup for common patterns
- Returns confidence score, not authoritative answer
- Used only if LLM fails

**Handles**: Speed optimization when LLM overhead unacceptable

### Scope Extraction (ALWAYS)

**After detection**: Extract scope (granular vs categorical) via:
1. LLM context (what entity, what category)
2. DB lookup (entity_taxonomies for category metadata)
3. Cascading rules (if category, remove all rels + entities)

---

## Fix: Revert to LLM Primary

**File**: `openwebui/faultline_function.py`

```python
def _detect_retraction_intent(self, text: str) -> tuple[bool, dict]:
    """Retraction detection: LLM semantic + scope extraction.
    
    PRIMARY: LLM semantic detection (user is truth)
    FALLBACK: Pattern matching (when LLM unavailable)
    """
    # Layer 1: LLM Semantic Detection (PRIMARY)
    try:
        is_retraction, scope = await self._detect_retraction_semantic(
            text, 
            self.valves.LLM_MODEL, 
            self.valves.LLM_URL
        )
        if is_retraction:
            return (True, scope)
    except Exception as e:
        if self.valves.ENABLE_DEBUG:
            print(f"[FaultLine] semantic_retraction_detection_fallback: {e}")
        # Fall through to pattern matching
    
    # Layer 2: Pattern Matching (FALLBACK - only if LLM failed)
    is_retraction, signal_category = _detect_retraction_pattern(text, _RETRACTION_SIGNALS_CACHE)
    if is_retraction:
        scope = _extract_categorical_scope(text, self.db_url)
        return (True, scope)
    
    return (False, {})
```

---

## Test Cases (Correct)

### Granular: "I don't have a son named Sam"
- LLM detects: negation of parent_of with entity "Sam"
- Scope: `{scope_level: "granular", subject: "sam", rel_type: "parent_of"}`
- /retract: removes parent_of(user, sam) + child_of(sam, user)
- Result: ✅ Fact superseded

### Categorical: "I have no pets"
- LLM detects: categorical negation of pet ownership
- Scope: `{scope_level: "categorical", category: "pets", rel_types: ["has_pet"]}`
- /retract: removes ALL has_pet facts + archives pet entities
- Result: ✅ All pet facts superseded, query "my pets" returns nothing

### LLM Fallback to Pattern: "forget X"
- LLM called but timeout
- Pattern matching: "forget" signal matches
- Scope: extracted via DB or LLM fallback
- Result: ✅ Pattern catches explicit keywords when LLM unavailable

---

## Success Criteria

1. ✅ LLM semantic detection is Layer 1 (primary)
2. ✅ Pattern matching is Layer 2 (fallback, only when LLM unavailable)
3. ✅ Test "I don't have a son named Sam" → granular rel removed
4. ✅ Test "I have no pets" → entire category + rels superseded
5. ✅ Scope extraction aware of granular vs categorical distinction
6. ✅ /retract endpoint handles cascading removal by scope_level
7. ✅ User statements treated as Class A (1.0 confidence)
8. ✅ Logs show semantic detection attempted first, pattern fallback only on error

---

## Related

**dprompt-107 (REVERT)**: Pattern-primary aliceign, needs realiceign to LLM-primary

**dprompt-108 (NEW)**: LLM semantic + scope extraction, correct architecture

**dBug-042**: Natural negation patterns (addressed by LLM semantic, not patterns)

**CLAUDE.md principle**: "User is truth" — user's statement about their own memory is authoritative, Class A confidence.
