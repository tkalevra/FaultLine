# dprompt-53: Filter Simplification — Remove Brittle Gating, Trust Backend

**Date:** 2026-05-12  
**Author:** Christopher Thompson  
**Status:** Design phase  
**Severity:** P1 (blocks category queries, UUID leaks)

## Problem Statement

The Filter implements three-tier relevance gating (Tier 1 entity match → Tier 2 identity fallback → Tier 3 graph pass-through) with Concept entity filtering. This creates a fundamental mismatch:

- Backend returns facts ranked by class (A > B > C) + confidence
- Filter re-gates based on entity type, keywords, and tier logic
- Result: Concept filtering causes Tier 2 to fire when it shouldn't → Tier 3 blocked

**Root cause:** The Filter tries to be smart. It shouldn't.

## Architectural Principle

**Filter is dumb. Backend is smart.**

The Filter does NOT:
- Gate facts based on entity type or category
- Implement multi-tier fallback logic
- Re-rank facts based on heuristics

The Filter DOES:
- Call `/query` once
- Inject returned facts in backend-returned order
- Optionally reorder by confidence (optional)
- Trust backend ranking as authoritative

## Why This Matters

**Query:** "Where should my son and I go for dinner tomorrow?"

Backend must recognize:
- "my" + "I" → user identity anchors
- "son" → hierarchy traversal (`child_of` relationship)
- Locations + restaurants → graph traversal (`lives_in`, `works_at`, `likes`)
- "dinner" + "tomorrow" → contextual signals

Backend returns facts ranked by provenance. Filter injects them. Done.

**Current behavior:** Filter sees concept entities ("family", "pets"), applies Tier logic, blocks valid facts. Wrong.

## Implementation Goals

1. **Remove Tier Logic:** Delete `_TIER1_*`, `_TIER2_*`, `_TIER3_*` constants and tier-based filtering
2. **Remove Concept Filtering:** Delete `entity_types` parameter passing between backend and Filter
3. **Simplify Relevance Score:** Keep confidence bonus, sensitivity penalty; drop keyword matching
4. **Trust Backend:** Call `/query` once, inject results, rely on backend ranking
5. **Validate:** Real-world queries return correct facts in sensible order

## Scope: What Changes Where

### Backend (`src/api/main.py`)

- Remove `entity_types` from `/query` response JSON (no longer needed)
- Keep fact ranking by class + confidence (already correct)
- Ensure graph/hierarchy traversal captures all valid edges (no gating)

### Filter (`openwebui/faultline_tool.py`)

- Remove `_TIER1_*`, `_TIER2_*`, `_TIER3_*` constants
- Remove `_categorize_query()` function
- Remove `_extract_query_entities()` entity matching logic
- Simplify `_filter_relevant_facts()` to:
  - Identity facts (also_known_as, pref_name, same_as) → always pass
  - Everything else → pass if confidence ≥ threshold (0.4 or tunable)
- Remove `entity_types` parameter from function signatures
- Remove concept filtering (`Concept`, `unknown` type checks)

### Tests

- Validate 4 real-world queries return correct facts in order
- Verify no UUID leaks
- Verify sensitivity gating still works (birthday, address)

## Success Criteria

✅ Query "where should my son and I go for dinner?" returns facts about user, son, locations, preferences  
✅ Query "tell me about our pets" returns `has_pet` facts  
✅ Query "how are you" returns identity facts (user pref_name, age, location)  
✅ No UUID leaks in any response  
✅ Sensitive facts (birthday, address) gated unless explicit ask  
✅ Filter code is < 500 lines (down from 700+)  
✅ Test suite passes (112+ tests)  

## Non-Goals

- Fixing ingestion (concept entities, entity types) — separate issue
- Rewriting extraction — separate issue
- Changing database schema — not needed

## Implications

This change assumes the backend's extraction, ontology, and hierarchy are strong enough that the Filter needs no gating logic. If tests fail, the problem is backend ingestion, not Filter logic. We fix ingestion, not Filter.

## References

- `docs/ARCHITECTURE_QUERY_DESIGN.md` — architectural principle with dinner example
- dBug-report-001.md — Tier 2 blocks Tier 3 after Concept filter
- dprompt-52b — Entity-type-aware Tier 1 (symptom patch, not root fix)
