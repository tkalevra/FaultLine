# dprompt-20 — Entity Taxonomies: Data-Driven Grouping System

## Problem

Extraction patterns are brittle and don't scale. "My wife's dog" isn't recognized as part of family context because no hardcoded pattern exists for transitive relationships.

## Solution: Entity Taxonomies Table

A **declarative taxonomy system** that defines entity groupings, membership rules, and transitivity. LLM learns new taxonomies on-the-fly; system uses DB as source of truth.

## Implementation (Three Phases)

### Phase 1: Create entity_taxonomies Table

```sql
CREATE TABLE entity_taxonomies (
  id BIGSERIAL PRIMARY KEY,
  taxonomy_name VARCHAR(64) NOT NULL UNIQUE,    -- "family", "household", "work", "location", "computer_system"
  description TEXT,
  
  -- Who belongs to this group?
  member_entity_types TEXT[] NOT NULL,          -- ["Person", "Animal"] for household
  
  -- What relationships define membership?
  rel_types_defining_group TEXT[] NOT NULL,     -- ["parent_of", "child_of", "spouse"] for family
  
  -- Transitive knowledge propagation
  has_transitivity BOOLEAN DEFAULT false,       -- If A in group and A→X, then X in group context
  transitive_rel_types TEXT[],                  -- ["lives_in", "lives_at"] propagate through group
  
  -- Hierarchical grouping (for locations, systems)
  is_hierarchical BOOLEAN DEFAULT false,        -- true: apt→building→street→city
  parent_rel_type VARCHAR(64),                  -- "located_in" for hierarchies
  
  -- Provenance
  source VARCHAR(32) DEFAULT 'seeded',          -- "seeded" (pre-loaded), "from_llm", "user_created"
  created_at TIMESTAMP DEFAULT now(),
  
  UNIQUE(taxonomy_name)
);
```

**Pre-seed with:**
- family: {Person}, {parent_of, child_of, spouse, sibling_of}, transitive={lives_in, works_for}
- household: {Person, Animal}, {member_of, lives_at, lives_in}, transitive={location, pets}
- work: {Person, Organization}, {works_for, part_of, reports_to}, transitive={located_in}
- location: {Location}, {located_in}, hierarchical=true, parent_rel=located_in
- computer_system: {System, Component}, {instance_of, has_component}, hierarchical=true

### Phase 2: Update Extraction (Ingest)

**New function in `/ingest`:** `_apply_taxonomy_rules(fact, taxonomies)`

```python
def _apply_taxonomy_rules(fact, taxonomies):
    """
    Given an extracted fact, check if it matches any taxonomy.
    If yes, apply transitivity and grouping rules.
    If no matching taxonomy exists, ask LLM to suggest one.
    """
    subject_id, rel_type, object_id = fact.subject_id, fact.rel_type, fact.object_id
    
    # Check existing taxonomies
    matching_taxonomy = None
    for tax in taxonomies:
        if rel_type in tax.rel_types_defining_group:
            matching_taxonomy = tax
            break
    
    if not matching_taxonomy:
        # No taxonomy exists for this rel_type
        # Ask LLM: "Does this relationship define a group?"
        suggested_taxonomy = llm_suggest_taxonomy(subject_id, rel_type, object_id)
        if suggested_taxonomy:
            # INSERT into entity_taxonomies with source='from_llm'
            db.insert_taxonomy(suggested_taxonomy)
            matching_taxonomy = suggested_taxonomy
    
    # Apply transitivity rules
    if matching_taxonomy and matching_taxonomy.has_transitivity:
        # Mark fact with taxonomy context
        fact.taxonomy_context = matching_taxonomy.taxonomy_name
        fact.is_transitive_member = True
    
    return fact
```

### Phase 3: Update Query (`/query`)

**New function:** `_fetch_transitive_members(user_id, taxonomy_name)`

```python
def _fetch_transitive_members(user_id, taxonomy_name):
    """
    Given a taxonomy (e.g., "family"), return all entities that are members.
    Includes direct relations + transitive relations.
    """
    taxonomy = db.get_taxonomy(taxonomy_name)
    
    # Base: all entities related to user via rel_types_defining_group
    direct = db.query("""
        SELECT DISTINCT object_id FROM facts
        WHERE user_id = %s AND subject_id = %s
          AND rel_type = ANY(%s)
    """, (user_id, user_id, taxonomy.rel_types_defining_group))
    
    members = set(direct)
    
    # Transitive: for each member, fetch entities via transitive_rel_types
    for member_id in members:
        transitive = db.query("""
            SELECT DISTINCT object_id FROM facts
            WHERE subject_id = %s AND rel_type = ANY(%s)
        """, (member_id, taxonomy.transitive_rel_types))
        members.update(transitive)
    
    return members
```

**Update query injection:** When query contains taxonomy signal ("tell me about my family"), use `_fetch_transitive_members()`.

## LLM Taxonomy Suggestion

**Prompt to LLM:**
```
Given this fact: subject="{subject}", rel_type="{rel_type}", object="{object}"

Does this fact define a group membership? If yes, suggest a taxonomy:
{
  "taxonomy_name": "...",
  "description": "...",
  "member_entity_types": ["Person" | "Animal" | "Organization" | "Location" | "System"],
  "rel_types_defining_group": [...],
  "has_transitivity": true/false,
  "transitive_rel_types": [...]
}

Return JSON or "NO_TAXONOMY" if fact doesn't define grouping.
```

Example response for "Mars has a dog":
```json
{
  "taxonomy_name": "household",
  "description": "Entities living in same residence",
  "member_entity_types": ["Person", "Animal"],
  "rel_types_defining_group": ["lives_at", "lives_in", "member_of"],
  "has_transitivity": true,
  "transitive_rel_types": ["has_pet", "works_for"]
}
```

## Test Flow

**User says:** "I have a wife Marla. Marla has a dog named Rex."

**Ingest:**
1. Extract: `spouse(user, marla)` + `has_pet(marla, rex)`
2. Check taxonomies: "family" matches spouse → apply transitivity rules
3. Check taxonomies: "household" matches has_pet → mark as transitive
4. Store facts with `taxonomy_context="household"` and `is_transitive_member=true`

**Query (new chat):** "Tell me about my family"
1. Signal: "family" → call `_fetch_transitive_members(user_id, "family")`
2. Result: marla (via spouse) + rex (transitive via has_pet)
3. Inject: "Your family consists of Marla (spouse) and Rex (dog)"

## Robustness Checklist

- [ ] Pre-seed 5 core taxonomies (family, household, work, location, computer_system)
- [ ] LLM suggestion only triggers if no existing taxonomy matches
- [ ] Suggested taxonomies stored with `source='from_llm'` for auditability
- [ ] Transitivity rules prevent infinite loops (cycle detection)
- [ ] Query respects `is_transitive_member` flag when injecting facts
- [ ] Test: spouse's pet, parent's job, team's location all resolve correctly
- [ ] Test: novel taxonomy suggested by LLM, used for subsequent facts
- [ ] Backward compatibility: pre-refactor facts work without taxonomy context

## Files to Change

1. **migrations/019_entity_taxonomies.sql** — Create table + pre-seed
2. **src/api/main.py** — Add `_apply_taxonomy_rules()`, update ingest loop, add `_fetch_transitive_members()` to query
3. **src/extraction/compound.py** — Remove hardcoded extraction patterns (rely on taxonomies instead)
4. **openwebui/faultline_tool.py** — Add taxonomy signal detection in query

## Outcome

- **No brittle patterns** — All grouping data-driven from taxonomies table
- **Learns over time** — LLM suggests new taxonomies as needed
- **Scales infinitely** — New rel_types automatically handled via taxonomy discovery
- **Transitive reasoning** — "spouse's dog" automatically part of family context
- **Handles everything** — family, work, location, computers, social groups, etc.
