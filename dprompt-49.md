# dprompt-49: Taxonomy Membership via `member_of` + Hierarchy Chains

**Status:** Specification  
**Date:** 2026-05-12  
**Goal:** Bridge the gap between user statements ("my pets are family") and taxonomy-aware query filtering, without modifying the `entity_taxonomies` table.

## The Problem

**`entity_taxonomies` is read-only from conversation.** The Filter LLM cannot create, modify, or extend taxonomy groups. When a user says "my pets are family," the system has no path to learn this:

- No `member_of` rel_type exists in the ontology
- The `entity_taxonomies` table has no ingest write path
- `_TAXONOMY_CACHE` is static (loaded once at startup)

**Result:** The taxonomy filter (dprompt-47/47c) correctly groups entities by type, but the user cannot change what belongs to a group through conversation.

## The Solution

**One rel_type bridges the gap: `member_of`.**

The hierarchy chain already exists. The Filter already extracts `instance_of`/`subclass_of`. We add `member_of` which links an entity to a taxonomy group, and the hierarchy-chain-aware filter walks the chain:

```
User: "my pets are family"
LLM extracts:  animal → member_of → family
Filter prompts: fraggle → instance_of → animal → member_of → family
Query filter:   walks chain, finds "family" → member_entity_types match → includes pet
```

No schema changes to `entity_taxonomies`. No cache invalidation. No new tables. Just facts flowing through the existing pipeline.

## Architecture

1. **Ingest path:** `member_of` edges flow like any other rel_type → WGM gate → classification → facts/staged_facts
2. **Query path:** `_hierarchy_expand()` already walks `instance_of`/`subclass_of` chains — extend `_REL_TYPE_HIERARCHY` to include `member_of` so the chain resolves all the way to taxonomy groups
3. **Taxonomy filter:** dprompt-47c checks if any node in the chain matches `member_entity_types` — already implemented

## Changes

| File | Change |
|------|--------|
| `src/api/main.py` | Add `member_of` to `_REL_TYPE_HIERARCHY` frozenset; add `member_of` to `_MISSING_TYPES` seed list |
| `openwebui/faultline_tool.py` | Add `member_of` to `_TRIPLE_SYSTEM_PROMPT` extraction instructions |
| DB: `rel_types` | Insert `member_of` (on next restart via `_ensure_schema()`) |

## Success Criteria

1. ✓ `member_of` exists in `rel_types` table
2. ✓ `member_of` is in `_REL_TYPE_HIERARCHY` frozenset
3. ✓ Filter prompt instructs LLM to extract `member_of` edges
4. ✓ User says "pets are family" → `animal → member_of → family` fact stored
5. ✓ Query walks hierarchy chain → pet entity included in family results
6. ✓ No schema changes to `entity_taxonomies`
7. ✓ Test suite passes, no regressions
