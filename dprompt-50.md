# dprompt-50: Filter Concept Entities from `preferred_names` in `/query`

**Status:** Specification
**Date:** 2026-05-12
**Goal:** Prevent concept/taxonomy entities ("pets", "family", "dog") from polluting `preferred_names`, which causes the Filter's entity-name matching to return empty results for category queries.

## The Problem

When the LLM extracts taxonomy edges like `(pets, member_of, family)`, `/ingest` creates entities for "pets" and "family" — these are concept labels, not named entities. `/query` returns them in `preferred_names`, and the Filter's `_extract_query_entities()` treats them as known entity names.

**Failure chain:**
1. User says "my pets are family" → `(pets, member_of, family)` stored
2. `/query` returns `preferred_names` including `'pets': 'pets'`, `'family': 'family'`
3. User queries "tell me about our pets" → Filter matches "pets" as Tier 1 entity
4. Tier 1 returns only the `member_of` fact (the only fact where "pets" is subject/object)
5. All actual `has_pet` facts are excluded → user sees nothing useful

**Live confirmation (pre-prod):**
```
preferred_names from /query: {chris, mars, cyrus, des, gabby, fraggle, pets, family, dog, morkie}
Query "tell me about our pets" → entity match on "pets" → only member_of fact returned
```

## The Solution

**Filter `preferred_names` at the source — the `/query` endpoint.**

When building the `preferred_names` dict, exclude entities whose `entity_type` is `'Concept'` or `'unknown'`. Only Person, Animal, Organization, and Location entities belong in the name resolution dict. Taxonomy group names, species labels, and untyped entities are not named entities — they don't need display-name resolution.

This is a one-condition guard at the single choke point. All downstream consumers (Filter's entity matching, display name resolution, memory block building, conversation context) are protected without changes.

## Changes

| File | Change |
|------|--------|
| `src/api/main.py` | In the `/query` handler's `preferred_names` builder, skip entities where `entity_type IN ('Concept', 'unknown')` |

## Success Criteria

1. ✓ "pets", "family", "dog", "morkie" excluded from `preferred_names`
2. ✓ Query "tell me about our pets" returns has_pet facts (not just member_of)
3. ✓ Person/Animal/Organization/Location entities still resolved normally
4. ✓ Test suite passes, no regressions
5. ✓ Live test: family query excludes pets, pet query includes pets
