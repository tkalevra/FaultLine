# dprompt-52b: Entity-Type-Aware Tier 1 Matching — Implementation

**For: deepseek (V4-Pro)**

---

## Task

Extend the `/query` response with `entity_types` metadata and update the Filter's Tier 1 entity-name matching to skip Concept/unknown entity types. This prevents taxonomy group labels ("pets", "family", "dog") from hijacking Tier 1 and returning only `member_of` facts for category queries.

---

## Context

**dprompt-51b is deployed and working** — the Filter trusts the backend's graph-proximity ranking. But Tier 1 entity-name matching runs BEFORE Tier 3's pass-through. When `preferred_names` contains concept entities (ingested via `member_of`/`instance_of` edges), a query like "tell me about our pets" matches "pets" as a named entity, triggering Tier 1 to return only the `member_of` fact — all `has_pet` facts are excluded.

**The ontology already encodes the distinction:** `entities.entity_type` is Person, Animal, Organization, Location, Concept, or unknown. The backend has this data; it just needs to pass it to the Filter so Tier 1 can make smarter matching decisions.

**Live failure (pre-prod 2026-05-12):**
```
Backend /query → 30 facts (has_pet, spouse, parent_of, member_of, ...)
preferred_names includes: 'pets': 'pets', 'family': 'family', 'dog': 'dog'
Filter Tier 1 matches "pets" → returns 1 fact: pets -member_of-> family
Result: UUID leak + "single dog named 7E4Bff75-706E-..." ← garbage
```

---

## Constraints

**DO:**
- Add `entity_types` dict to `/query` response JSON (parallel to `preferred_names`)
- Build `entity_types` from the `entities` table for UUID-keyed entries, using a batched query
- For non-UUID keys (display-name strings), resolve entity_type via `entity_aliases` → `entities` lookup
- Update Filter's `_extract_query_entities()` to accept optional `entity_types` parameter
- Skip tokens in Tier 1a matching when the matched entity's type is Concept or unknown
- Pass `entity_types` from `inlet()` → `_filter_relevant_facts()` → `_extract_query_entities()`
- Keep dprompt-50's `_clean_preferred_names` changes (Concept filtering of UUID keys) as defense-in-depth
- Keep Tier 1b (relational resolution) unchanged — it resolves "my wife" → spouse entity, which is always a named entity
- Keep Tier 2 and Tier 3 unchanged

**DO NOT:**
- Modify the database schema
- Remove `_clean_preferred_names` — it remains as defense-in-depth
- Change `preferred_names` format or keys
- Add per-request DB queries in the Filter
- Change the memory injection format or `_build_memory_block()`
- Modify `/ingest` or `/retract` endpoints
- Change how Tier 1b relational resolution works
- Touch PROD code or pre-prod configuration

---

## Sequence

### 1. Backend: Add `entity_types` to `/query` response

**File:** `src/api/main.py`

In the `/query` handler, after building `preferred_names`, build an `entity_types` dict. All `/query` response paths that return `preferred_names` must also return `entity_types`.

**Building strategy:**

```python
# Build entity_types dict (parallel to preferred_names)
# Maps entity_id (UUID or display string) → entity_type
entity_types = {}

# For UUID-keyed entries in preferred_names: batch query entities table
_uuid_keys_in_pns = [k for k in preferred_names if _UUID_PATTERN.match(str(k))]
if _uuid_keys_in_pns and db:
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, entity_type FROM entities "
                "WHERE user_id = %s AND id = ANY(%s)",
                (user_id, _uuid_keys_in_pns),
            )
            for entity_id, etype in cur.fetchall():
                entity_types[entity_id] = etype or "unknown"
    except Exception:
        pass  # graceful degradation — missing entity_types is non-fatal

# For non-UUID keys (display-name strings): resolve via entity_aliases → entities
_non_uuid_keys = [k for k in preferred_names if not _UUID_PATTERN.match(str(k))]
if _non_uuid_keys and db:
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT ea.alias, COALESCE(e.entity_type, 'unknown') "
                "FROM entity_aliases ea "
                "LEFT JOIN entities e ON e.id = ea.entity_id AND e.user_id = ea.user_id "
                "WHERE ea.user_id = %s AND ea.alias = ANY(%s)",
                (user_id, _non_uuid_keys),
            )
            for alias, etype in cur.fetchall():
                entity_types[alias] = etype or "unknown"
    except Exception:
        pass
```

**Add `entity_types` to every response path that includes `preferred_names`:**

All locations where `"preferred_names": ...` appears in the `/query` handler must also include `"entity_types": entity_types`.

### 2. Filter: Update `_extract_query_entities()`

**File:** `openwebui/faultline_tool.py`

Add optional `entity_types` parameter. In Tier 1a (direct token match), skip tokens whose matched entity has type Concept or unknown.

```python
def _extract_query_entities(
    query: str,
    preferred_names: dict,
    facts: list[dict] = None,
    entity_types: dict = None,
) -> set[str]:
    # ... existing Tier 1a token matching ...
    
    # NEW: Skip tokens matching Concept/unknown entities
    if entity_types:
        _CONCEPT_TYPES = {"concept", "unknown"}
        entities = {
            token for token in entities
            if entity_types.get(token, "").lower() not in _CONCEPT_TYPES
        }
    
    # ... existing Tier 1b relational resolution (unchanged) ...
```

Note: Tier 1b relational resolution ("my wife" → spouse) resolves to named entities by definition (spouse is always a Person), so it doesn't need the entity_types check.

### 3. Filter: Thread `entity_types` through the call chain

**File:** `openwebui/faultline_tool.py`

- `_filter_relevant_facts()`: Add optional `entity_types` parameter, forward to `_extract_query_entities()`
- `inlet()`: Extract `entity_types` from `/query` response JSON, pass to `_filter_relevant_facts()`

```python
# In inlet(), extract from /query response:
entity_types = data.get("entity_types", {})

# Pass through:
facts = self._filter_relevant_facts(
    facts, canonical_identity,
    preferred_names=preferred_names, query=text,
    entity_types=entity_types,
)
```

### 4. Backend: Remove dprompt-50 `_clean_preferred_names` changes (revert)

The dprompt-50 approach of filtering Concept entities from `preferred_names` is superseded by dprompt-52. However, keep it as defense-in-depth — it filters UUID-keyed Concept entities which prevents them from appearing in memory block display names. The `entity_types` approach is the primary gate for Tier 1 matching.

**Decision:** Keep `_clean_preferred_names` as-is. It's additive defense, not harmful.

### 5. Test cases

- `_extract_query_entities("tell me about our pets", pns, facts, entity_types)` → "pets" skipped (Concept), no Tier 1 match → Tier 3 pass-through returns `has_pet` facts ✓
- `_extract_query_entities("tell me about fraggle", pns, facts, entity_types)` → "fraggle" matched (Animal) → Tier 1 returns facts about Fraggle ✓
- `_extract_query_entities("how are you", pns, facts, entity_types)` → no tokens match → Tier 2 identity fallback ✓
- Missing `entity_types` key from `/query` → Filter behaves as today (backward compat) ✓

### 6. Validate in pre-prod

**STOP after code changes. Request rebuild of faultline backend container + OpenWebUI filter update.**

**Validation sequence (after rebuild):**
```bash
# 1. Check /query response has entity_types
curl -s -X POST http://192.168.40.10:8001/query ... | jq '.entity_types'

# 2. Test "tell me about our pets" via chat API
curl -s -H "Authorization: Bearer sk-..." \
  -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"tell me about our pets"}],"stream":false}' \
  https://hairbrush.helpdeskpro.ca/api/chat/completions

# 3. Verify has_pet facts returned (not just member_of)
# 4. Verify no UUID leaks
# 5. Verify "tell me about fraggle" still works
```

---

## Deliverables

| File | Change |
|------|--------|
| `src/api/main.py` | Add `entity_types` dict construction in `/query` handler; add to all response paths with `preferred_names` |
| `openwebui/faultline_tool.py` | `_extract_query_entities()` accepts `entity_types`, skips Concept/unknown; `_filter_relevant_facts()` and `inlet()` thread it through |
| `tests/filter/test_relevance.py` | Add test: Concept entities skipped in Tier 1 |
| `tests/filter/test_relation_resolver.py` | Add test: entity_types gate on Concept tokens |

---

## Success

- ✓ "tell me about our pets" returns has_pet facts (Fraggle, Morkie) — not member_of
- ✓ "tell me about fraggle" still works (named Animal entity → Tier 1)
- ✓ "tell me about my family" returns family facts (Person entities)
- ✓ Concept entities still appear in memory block (they're facts, not garbage)
- ✓ Missing `entity_types` → backward compatible (no crash)
- ✓ Tests pass, no regressions
- ✓ UUID separation preserved — entity_types is metadata only

---

## Upon Completion

```markdown
## ✓ DONE: dprompt-52 (Entity-Type-Aware Tier 1) — 2026-05-12

**Implementation:**
- Backend `/query` now returns `entity_types` dict alongside `preferred_names`
- `entity_types` built from `entities` table (UUID keys) and `entity_aliases`→`entities` (string keys)
- Filter's `_extract_query_entities()` skips tokens matching Concept/unknown entity types
- `entity_types` threaded through `inlet()` → `_filter_relevant_facts()` → `_extract_query_entities()`

**Result:** Tier 1 no longer hijacked by taxonomy labels. "our pets" returns
has_pet facts. Named entity queries ("fraggle") still work. Self-growing:
new entity types flow through without code changes.

**Next:** Deploy backend + filter to pre-prod → validate → STOP.
```
