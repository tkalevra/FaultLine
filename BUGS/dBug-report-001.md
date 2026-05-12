# dBug-report-001: Tier 2 Identity Fallback Blocks Tier 3 After Concept Filter

**Date:** 2026-05-12
**Severity:** P1 — blocks category queries ("our pets", "my family")
**Status:** Open
**File:** `openwebui/faultline_tool.py` — `_filter_relevant_facts()`

## Symptom

Query "tell me about our pets" returns identity/family facts only (pref_name, spouse, parent_of). Zero `has_pet` facts reach the model. UUIDs leak in responses.

## Verified pipeline (backend is clean)

| Stage | Result |
|-------|--------|
| Ingest | 3 `has_pet` facts stored in `staged_facts` (Class B), `qdrant_synced = t` |
| Graph traversal | 9 connected entities found, 23 facts |
| Hierarchy expansion | `pets -member_of-> family` chain present |
| Qdrant search | 10 vector hits returned |
| Attributes merge | 2 attribute facts added |
| `/query` response | **30 facts total** including 3 `has_pet` + `entity_types` dict populated |
| Filter receives | 30 facts, `entity_types` with `pets→unknown`, `family→unknown`, `dog→unknown` |
| Filter outputs | **16 facts** — all identity/family, zero `has_pet` |

## Root cause

`_filter_relevant_facts()` Tier flow:

1. **Tier 1**: Entity match. `_extract_query_entities()` matches "pets" as known entity → `entity_types["pets"] = "unknown"` → filtered out by Concept/unknown skip → `entities` set is **empty**. Tier 1 produces no match. ✓ (correct)

2. **Tier 2**: Identity fallback. Fires on empty Tier 1 result. `_TIER2_IDENTITY_RELS = {also_known_as, pref_name, same_as, spouse, parent_of, child_of, sibling_of}` matches 16 facts → **`return` fires here**. ✗ (wrong — should not intercept category queries)

3. **Tier 3**: Graph-proximity pass-through (threshold 0.0). **Unreachable.** The 3 `has_pet` facts never evaluated.

**The logic flaw:** Tier 2 treats "no entity match" identically for two different cases:
- (a) genuinely generic query like "how are you" → identity fallback is correct
- (b) concept-filtered query like "tell me about our pets" → should fall through to Tier 3, not Tier 2

When `entity_types` strips Concept/unknown tokens from the Tier 1 match set, the empty result looks identical to case (a) — no code distinguishes the two.

## Fix direction

In `_filter_relevant_facts()`, after Tier 1 produces `entities`:

```python
# After entity_types filtering, if entities went from non-empty to empty
# due to Concept filtering, skip Tier 2 and go directly to Tier 3.
if entity_types and not entities:
    # Query matched concept entities — fall through to graph-proximity
    pass  # skip Tier 2, proceed to Tier 3
else:
    # Tier 2: identity fallback (genuinely generic query)
    ...
```

Or: make Tier 2 additive (append to result, don't early-return) when preceded by Concept-filtered Tier 1.

## Evidence

**Backend /query for "tell me about our pets":**
- 30 facts: 5 also_known_as, 5 parent_of, 4 pref_name, 3 spouse, **3 has_pet**, 2 child_of, 2 owns, 1 member_of, 1 instance_of, 1 is_a, 1 sibling_of, 1 species, 1 age
- `entity_types`: `pets→unknown`, `family→unknown`, `dog→unknown`, `fraggle→Animal`, `mars→Person`, ...

**Filter debug output:**
```
facts=16 preferred_names={...'pets': 'pets', 'family': 'family', 'dog': 'dog'...}
```
All 16 returned facts are identity/family rel_types. No `has_pet`, `owns`, `member_of`, or `instance_of`.

**Database:**
- 3 `has_pet` in `staged_facts`, all `qdrant_synced = t`, `confirmed_count = 0`
- `pets -member_of-> family` edge exists in `staged_facts`
