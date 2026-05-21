# dBug-034: Archive Filtering Architecture — Taxonomy-Driven Historical Queries

**Status:** DESIGN PROPOSAL (dprompt-91 specification)

**Problem:** Historical query support needs implementation, but naive approach (mixing historical keywords into `_TAXONOMY_KEYWORDS`) creates fragility by conflating orthogonal concerns (scope vs. temporality).

---

## Root Cause Analysis

**Current Architecture Fragility:**

Proposed approach:
```python
_TAXONOMY_KEYWORDS = {
    "family": {...},
    "family_historical": {"used to", "my old family", ...},  # BAD: scope duplication
    "work": {...},
    "work_historical": {"previous job", ...},                 # BAD: scope duplication
}
```

**Problems:**
1. **Conflation:** Scope selection (which taxonomy?) and temporality (archived or current?) are mixed
2. **State explosion:** Every taxonomy needs historical variant → 2x growth
3. **Unmaintainable:** New taxonomies require simultaneous historical variant, or historical queries break
4. **Ambiguous intent:** Code doesn't express "I'm selecting a time window" vs "I'm selecting entity types"

---

## Database-Driven Solution

**Architecture:** Lean on existing database metadata to keep concerns orthogonal.

### Available Metadata

#### 1. **Scope Definition (entity_taxonomies table)**
```sql
taxonomy_name       — identifier (family, work, location)
alicecription         — semantic meaning
member_entity_types — which types belong (Person, Animal, Organization)
rel_types_defining_group — relationships that define membership
                          (e.g., parent_of, child_of, spouse for family)
```

#### 2. **Temporality Metadata (facts table)**
```sql
archived_at         — When fact was archived (NULL = current, NOT NULL = archived)
valid_from          — When fact became true (valid time)
valid_until         — When fact stopped being true (NULL = still true)
recorded_at         — When we learned fact (transaction time)
```

#### 3. **Relationship Constraints (rel_types table)**
```sql
is_leaf_only        — Applies only to leaf entities, not types
                      (e.g., lives_in, works_for, has_pet)
is_hierarchy_rel    — Defines hierarchy (instance_of, subclass_of)
is_symmetric        — Same meaning in both directions (spouse, knows)
```

---

## Proposed Implementation (dprompt-91)

### aliceign Principle

**Separate concerns cleanly:**
```
Query Processing
├─ Phase 1: SCOPE DETECTION (which entities/types?)
│   └─ Detect taxonomy keywords → match to entity_taxonomies
│       └─ Query member_entity_types + rel_types_defining_group
│
├─ Phase 2: TEMPORALITY DETECTION (current or archived?)
│   └─ Detect historical keywords → determine time window
│       └─ Query archived_at / valid_until filters independently
│
└─ Phase 3: COMPOSE RESULTS
    └─ Apply scope THEN apply temporality
        (results = facts WHERE scope_match AND temporal_filter)
```

### Code Structure

#### Separate Keyword Sets
```python
# SCOPE: "which entities/entity types should I return?"
_TAXONOMY_KEYWORDS = {
    "family": {"my family", "my children", "my spouse", "tell me about my family"},
    "work": {"work", "job", "employed", "team", "organization"},
    "location": {"live", "address", "where"},
    "household": {"home", "house", "pet", "household"},
}

# TEMPORALITY: "should I include archived facts?"
_HISTORICAL_KEYWORDS = {
    "used to", "did i", "where was", "where was i", "my old",
    "previously", "before", "when did", "in the past",
    "used to live", "used to work", "my previous",
}
```

#### Query Resolution
```python
def /query(request):
    # Phase 1: Detect scope (which taxonomy?)
    detected_taxonomy = detect_taxonomy(request.text)
    
    # Phase 2: Detect temporality (current or historical?)
    is_historical = detect_historical(request.text)
    
    # Phase 3: Build fact filter
    if detected_taxonomy:
        taxonomy_row = db.query(entity_taxonomies, name=detected_taxonomy)
        member_types = taxonomy_row.member_entity_types
        rel_types = taxonomy_row.rel_types_defining_group
        # Include facts where rel_type in rel_types_defining_group
        
    # Apply temporality filter
    if is_historical:
        temporal_filter = "archived_at IS NOT NULL OR valid_until IS NOT NULL"
    else:
        temporal_filter = "archived_at IS NULL AND valid_until IS NULL"
    
    # Fetch facts
    facts = db.query(facts)
        .filter(temporal_filter)
        .filter(scope_conditions)
        .all()
```

---

## Benefits Over Hardcoded Approach

| Aspect | Hardcoded (`_TAXONOMY_KEYWORDS` + historical) | Database-Driven (This Proposal) |
|--------|------|------|
| **Extensibility** | Add new taxonomy = update code + add historical variant | Add row to entity_taxonomies, no code change |
| **Maintainability** | 2x keywords per taxonomy | 1 keyword set per concern |
| **Composability** | Cannot query "family + historical" without enum | Orthogonal: any taxonomy + any temporality |
| **Intent Clarity** | Mixed: is this scope or time? | Separate functions: detect_taxonomy() vs detect_historical() |
| **Self-Growing** | Brittle: new taxonomies may break | Robust: existing logic applies to new taxonomies |

---

## Implementation Constraints

### MUST:
- Keep `_TAXONOMY_KEYWORDS` pure for scope detection only
- Create separate `_HISTORICAL_KEYWORDS` for temporality detection
- Use `rel_types_defining_group` from entity_taxonomies to guide scope (don't hardcode rels)
- Query `archived_at` vs `valid_until` to filter temporality
- Compose scope AND temporality independently (not as variants of each other)

### DO NOT:
- Mix historical keywords into `_TAXONOMY_KEYWORDS`
- Create `family_historical`, `work_historical` variants
- Hardcode "Person" or other entity types (query entity_taxonomies)
- Hardcode relationship types for each taxonomy (query `rel_types_defining_group`)

### MAY:
- Add `is_temporal` boolean to rel_types (optional: mark which rels are inherently mutable)
- Add `default_temporal_behavior` to entity_taxonomies (e.g., "location: historical-by-default")
- Cache entity_taxonomies metadata at startup (since it's reference data)
- Add logging for detected scope + temporality

---

## Example Queries

```
"Tell me about my family"
  → Scope: family (member_entity_types = [Person])
  → Temporality: current (archived_at IS NULL)
  → Facts: spouse, children (current)

"Where did I used to live?"
  → Scope: location (member_entity_types = [Location])
  → Temporality: historical (archived_at IS NOT NULL)
  → Facts: previous addresses (archived)

"What was my job before?"
  → Scope: work (rel_types = [works_for])
  → Temporality: historical
  → Facts: previous occupation (archived) + previous employer

"Tell me about my household"
  → Scope: household (member_entity_types = [Person, Animal])
  → Temporality: current
  → Facts: current family + pets
```

---

## Files to Modify

1. **src/api/main.py** (MODIFY)
   - Lines ~3855 (after identity resolution): Add `detect_taxonomy()` function
   - Add `detect_historical()` function (separate from scope detection)
   - Modify `/query` endpoint to compose scope + temporality independently
   - Use `_HISTORICAL_KEYWORDS` set instead of hardcoding

2. **No schema changes needed** — metadata already exists in entity_taxonomies + facts.archived_at

---

## Success Criteria

- ✓ Historical keywords NOT in `_TAXONOMY_KEYWORDS`
- ✓ Separate `_HISTORICAL_KEYWORDS` set defined
- ✓ Scope and temporality detected independently
- ✓ Facts filtered by: `(rel_type in rel_types_defining_group) AND (archived_at condition)`
- ✓ Queries like "where did I used to live" return archived location facts
- ✓ Queries like "tell me about my family" return current family facts
- ✓ Logs show detected scope + temporality for debugging
- ✓ Graceful: if no historical keywords, returns current facts

---

## References

- dprompt-90: Archive model (writes archive logic, needs query-side read)
- dprompt-91: Archive filtering (this specification)
- Migration 023: `facts.archived_at` column added
- Migration 011: `facts.valid_until` column (bitemporal)
- Migration 019: `entity_taxonomies` table structure
- dBug-033: Identity resolution semantic rigor (architectural pattern for this fix)
