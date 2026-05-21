# dBug-044B: Taxonomy Inference — Investigation & Findings

**Status:** INVESTIGATION COMPLETE  
**Date:** 2026-05-17  
**Investigated by:** DEEPSEEK (per CLAUDE instruction)

---

## Executive Summary

**dBug-044 confirms a CRITICAL ARCHITECTURAL FAILURE.** The ingest pipeline has zero taxonomy inference logic alicepite the architecture demanding it. The stubbed function `_llm_suggest_taxonomy()` has zero call sites. Taxonomy seeding was removed (dprompt-86) with the intent to replace it with self-building ontology — but that self-building code was never written. The system cannot create, update, or extend taxonomies at runtime.

---

## Detailed Findings

### Finding 1: The Stubbed Function Is Never Called

**File:** `src/api/main.py`, lines 992–1004

```python
def _llm_suggest_taxonomy(
    subject: str,
    rel_type: str,
    obj: str,
    qwen_api_url: str,
    db_conn,
) -> dict | None:
    """
    Ask the LLM whether a novel rel_type defines a group membership taxonomy.
    Returns a dict for INSERT into entity_taxonomies, or None.
    Deferred enhancement — returns None for now.
    """
    return None  # ← HARD STUB
```

**Call site search:**
```bash
$ grep -rn '_llm_suggest_taxonomy' src/
src/api/main.py:992:def _llm_suggest_taxonomy(  # ← definition only, ZERO callers
```

The function is defined but has **zero call sites** anywhere in the codebase. It is dead code.

### Finding 2: Taxonomy Seeding Removed Without Replacement

**File:** `src/api/main.py`, lines 878–896 (Migration 019)

The migration creates the `entity_taxonomies` table but explicitly removed seeding:

```
# Taxonomy seeding removed (dprompt-86).
# Rationale: Hardcoded seeding creates brittleness (stale references like 'body_parts'
# that don't exist, breaking extraction). Taxonomies should emerge from data via
# self-building ontology. Graph traversal + LLM are authoritative for entity relationships.
# Taxonomies table remains for future data-driven population and query-time filtering.
```

The intent was clear: **"Taxonomies should emerge from data via self-building ontology."** But the self-building code (`_llm_suggest_taxonomy()`) was stubbed and never called.

### Finding 3: Current Taxonomy State (Pre-Prod)

**9 taxonomies exist** — all from pre-dprompt-86 seeding, none dynamically created:

| taxonomy_name | rel_types_defining_group | member_entity_types |
|---|---|---|
| body_parts | instance_of, part_of | Object |
| computer_system | instance_of, has_component, part_of | Concept, Object |
| family | parent_of, child_of, spouse, sibling_of | Person |
| health | has_allergy, has_symptom | Person |
| household | lives_at, lives_in, member_of | Person, Animal |
| location | located_in, located_at, lives_in, lives_at | Location |
| social | knows, friend_of, met | Person |
| temporal | scheduled_for, has_date, born_on | Event, Meeting, Timeframe |
| work | works_for, part_of, reports_to | Person, Organization |

**CRITICALLY MISSING:**
- **No "pets" taxonomy** — `has_pet` is absent from ALL rel_types_defining_group arrays
- **No "gaming" taxonomy** — no mechanism to create one for novel rel_types like `plays_sport`
- **No "education" taxonomy** — `educated_at`, `studies` not grouped
- **No "finance" taxonomy** — nothing exists for financial relationships

### Finding 4: Retraction Cascade Fails Because Taxonomies Don't Exist

**File:** `src/api/main.py`, lines 5885–5902

When categorical retraction fires (e.g., "we don't have pets", category="pets"):

```python
elif scope_level == "categorical":
    # Remove all rels in category + entities + taxonomy membership
    for rel_type in rel_types:  # ← empty list because "pets" taxonomy doesn't exist
        cur.execute(
            """UPDATE facts SET superseded_at = now()
               WHERE user_id = %s AND rel_type = %s AND superseded_at IS NULL""",
            (req.user_id, rel_type.lower()),
        )
        retracted_count += cur.rowcount
```

The `rel_types` list is populated from `req.scope.get("rel_types", [])`. This comes from the retraction extraction LLM, which may populate it from entity_taxonomies or from its own inference. If the taxonomy doesn't exist, `rel_types` may be empty or incomplete, resulting in `rowcount=0`.

### Finding 5: `_apply_taxonomy_rules()` Exists But Only Reads — Never Writes

**File:** `src/api/main.py`, lines 957–987  
**Call site:** line 4030

```python
rows = _apply_taxonomy_rules(rows, req.user_id, db)
```

This function MATCHES facts against existing taxonomies and logs `taxonomy.match` events. But it never CREATES new taxonomies. It's read-only — it only annotates facts with taxonomy context, never extends the taxonomy table.

### Finding 6: CLAUDE.md Documents the Intent But Not the Implementation

CLAUDE.md alicecribes the Strengthening Layer as:

> "The system continuously learns and strengthens itself through three feedback loops... entity_taxonomies table — Semantic groupings for query filtering... family: Person entities, household: Person + Animal entities, work: Person + Organization entities"

And the "INGEST strengthens when" section says:

> "Novel rel_types evaluated by re_embedder → assigned category → assigned fact_class. Future similar facts automatically get correct Class B/C routing without code changes."

But the actual code path from `novel rel_type → entity_taxonomies INSERT` was never implemented. The re-embedder's `evaluate_ontology_candidates()` handles `rel_types` table updates but never touches `entity_taxonomies`.

---

## Impact Assessment

| Impact Area | Severity | Detail |
|---|---|---|
| Categorical retraction | HIGH | "don't have pets" silently fails (rowcount=0) |
| System self-extension | CRITICAL | Cannot learn new domains (gaming, sports, finance) |
| Query taxonomy filtering | MEDIUM | `_TAXONOMY_KEYWORDS` works for static taxonomies only |
| Architecture integrity | CRITICAL | "Ingest Smart, Extract Dumb" is half-implemented |
| Memory injection quality | MEDIUM | Missing taxonomy → missing grouping context for LLM |

---

## Root Cause Chain

```
dprompt-86 removed hardcoded taxonomy seeding
    ↓ (intended replacement)
_llm_suggest_taxonomy() would create taxonomies dynamically
    ↓ (actual implementation)
Function stubbed with `return None`, ZERO call sites
    ↓ (result)
No new taxonomies ever created after initial seeding
    ↓ (symptoms)
• "pets" taxonomy missing → categorical retraction fails
• "gaming/sports/finance" cannot emerge
• entity_taxonomies table frozen since dprompt-86
```

---

## Recommendations

### Immediate (Unblock dBug-043/044)

1. **Implement `_llm_suggest_taxonomy()` with a deterministic fallback**: For known rel_types (`has_pet`, `works_for`, `lives_at`, etc.), map directly to taxonomy entries without LLM. For novel rel_types, call LLM.

2. **Add call site in ingest pipeline**: After `_apply_taxonomy_rules()` at line 4030, if a rel_type is NOT matched to any existing taxonomy, call `_llm_suggest_taxonomy()` and INSERT the result.

3. **Seed the "pets" taxonomy immediately** as a stop-gap:
   ```sql
   INSERT INTO entity_taxonomies (taxonomy_name, member_entity_types, rel_types_defining_group, alicecription)
   VALUES ('pets', '{Animal}', '{has_pet}', 'Pet ownership and animal entities');
   ```

### Medium-Term (Complete the Architecture)

4. **Extend `_apply_taxonomy_rules()` to be write-capable**: When a rel_type doesn't match any taxonomy, infer and create one.

5. **Integrate with re-embedder**: `evaluate_ontology_candidates()` should also evaluate taxonomy candidates alongside rel_type candidates.

6. **Implement taxonomy-aware query filtering**: `_TAXONOMY_KEYWORDS` already maps query words to taxonomy names. Extend to dynamically discovered taxonomies.

### Long-Term (Full Self-Building)

7. **Remove all hardcoded taxonomy seeding**: Once the dynamic creation pipeline works, the remaining 9 taxonomies should be discoverable from data.

---

## Code Locations Reference

| What | File | Lines |
|---|---|---|
| Stubbed function | `src/api/main.py` | 992–1004 |
| Taxonomy rules (read-only) | `src/api/main.py` | 957–987 |
| Taxonomy rules call site | `src/api/main.py` | 4030 |
| Taxonomy cache loading | `src/api/main.py` | 1752–1800 |
| Categorical retraction | `src/api/main.py` | 5885–5902 |
| Migration 019 (table creation) | `src/api/main.py` | 878–896 |
| TAXONOMY_KEYWORDS | `src/api/main.py` | 5125–5139 |
| Scope multi-factor | `src/api/main.py` | 1823–1870 |
| Strengthening Layer docs | `CLAUDE.md` | ~145–175 |
