# dprompt-52: Entity-Type-Aware Tier 1 Matching — Prevent Concept Hijack

**Status:** Specification
**Date:** 2026-05-12
**Goal:** Extend the `/query` response with `entity_types` metadata so the Filter's Tier 1 entity-name matching can distinguish named entities (Person, Animal, Organization, Location) from taxonomy group labels (Concept). This prevents Tier 1 from matching "pets"/"family"/"dog" and returning only `member_of` facts for category queries.

## The Problem

**dprompt-51b made the Filter trust the backend.** But Tier 1 entity-name matching runs BEFORE Tier 3's trust-the-backend pass-through. When a query contains a word that happens to be a concept entity name ("pets", "family", "dog"), Tier 1 matches it and returns only facts where that concept is subject or object — typically a single `member_of` or `is_a` taxonomy edge. All actual relationship facts (`has_pet`, `spouse`, `parent_of`) are excluded.

**Failure chain (verified in pre-prod 2026-05-12):**
1. Backend `/query` returns 30 facts: `has_pet` (fraggle, morkie), `spouse`, `parent_of`, `member_of`, etc.
2. `preferred_names` includes concept entities: `pets`, `family`, `dog`
3. Filter Tier 1 finds "pets" in query → matches "pets" in `preferred_names` → returns only `pets -member_of-> family` (1 fact)
4. All `has_pet` facts dropped → user sees UUIDs and taxonomy labels, not their actual pets

**Why dprompt-50's backend-only approach was insufficient:** Filtering Concept entities from `preferred_names` at the backend level is a layering violation (name resolution shouldn't also be a relevance gate) AND it only handles UUID-keyed entries — string-keyed entries pass through. A robust solution must give the Filter the data it needs to make the distinction.

## Architecture

The ontology already encodes the distinction:

| entity_type | Examples | Tier 1 should match? |
|-------------|----------|---------------------|
| Person | mars, gabby, des | ✓ Named entity |
| Animal | fraggle, morkie | ✓ Named entity |
| Organization | TechCorp | ✓ Named entity |
| Location | Paris, kitchener | ✓ Named entity |
| Concept | pets, family, dog | ✗ Taxonomy group label |
| unknown | (untyped) | ✗ Not classified |

The backend already has this data in the `entities` table. It just needs to pass it through to the Filter.

## The Solution

**Extend `/query` response with an `entity_types` map** — paralleling `preferred_names`:

```json
{
  "preferred_names": {"fraggle": "fraggle", "pets": "pets", ...},
  "entity_types": {"fraggle": "Animal", "pets": "Concept", "family": "Concept", "dog": "Animal", ...}
}
```

The Filter's `_extract_query_entities()` checks `entity_types` before matching: if a token matches a name whose entity_type is Concept or unknown, it's skipped. Only Person, Animal, Organization, Location entities trigger Tier 1.

## Changes

| File | Change |
|------|--------|
| `src/api/main.py` | In `/query` handler, build `entity_types` dict alongside `preferred_names`. Populate from `entities` table for UUID keys, from fact metadata for string keys. Add `"entity_types"` to response JSON. |
| `openwebui/faultline_tool.py` | `_extract_query_entities()` accepts optional `entity_types` dict. Skips tokens matching entity names with type Concept/unknown. `_filter_relevant_facts()` passes `entity_types` through. `inlet()` extracts `entity_types` from `/query` response. |

## Design Principles

1. **UUID separation preserved** — `entity_types` is metadata, not an identifier. No UUIDs leak.
2. **Self-growing** — new entity types flow through automatically. No hardcoded allowlists.
3. **No new DB queries in Filter** — the backend does one batched query at response-build time.
4. **Backward compatible** — missing `entity_types` key → Filter behaves as today (no breakage).
5. **No backend name resolution changes** — `preferred_names` stays as-is. The distinction is additive.

## Success Criteria

1. ✓ Query "tell me about our pets" returns `has_pet` facts (Fraggle, Morkie), not just `member_of`
2. ✓ Query "tell me about fraggle" still works (named entity match → Tier 1)
3. ✓ Query "tell me about my family" returns family facts (Person entities), not taxonomy labels
4. ✓ Concept entities still appear in memory block (they're valid facts), just don't hijack Tier 1
5. ✓ Test suite passes, no regressions
6. ✓ Live test: pre-prod returns correct facts for "our pets", "her family", "their kids"
