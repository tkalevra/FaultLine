# dprompt-32 — Conflict Resolution System: Non-Destructive Name Collision Handling

## Purpose

Implement self-healing conflict resolution for entity name collisions. When two entities claim the same preferred name (e.g., both user and Gabriella have pref_name="gabby"), store the conflict in a dedicated table and let the re-embedder evaluate and resolve it with LLM context.

## The Problem

**Current behavior (broken):**
1. User ingest: `pref_name = "gabby"` (is_preferred_label=true)
2. Gabriella ingest: `pref_name = "gabby"` (is_preferred_label=true)
3. entity_aliases constraint: only one preferred alias per (user_id, alias)
4. Result: "gabby" → user entity; Gabriella has no preferred name
5. /query display resolution fails → Gabriella facts don't return

**Required behavior (self-healing):**
1. Detect collision at ingest time (non-destructive)
2. Store both claims in conflicts table (pending resolution)
3. Re-embedder evaluates with context: "User is Christopher, Gabriella is a 10-year-old child. Both called 'Gabby'. Disambiguate."
4. LLM resolves: user keeps "gabby" or "christopher", Gabriella gets "gabby" or "gabriella" based on frequency/context
5. Aliases updated, conflicts marked resolved
6. /query re-runs successfully with all entities visible

---

## Schema

### New Table: entity_name_conflicts

```sql
CREATE TABLE IF NOT EXISTS entity_name_conflicts (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    entity_id_1 UUID NOT NULL,
    entity_name_1 VARCHAR NOT NULL,
    entity_id_2 UUID NOT NULL,
    entity_name_2 VARCHAR NOT NULL,
    disputed_name VARCHAR NOT NULL,
    conflict_type VARCHAR DEFAULT 'pref_name_collision',  -- pref_name_collision / also_known_as_collision / etc
    status VARCHAR DEFAULT 'pending',  -- pending / resolved / ignored / escalated
    resolution_method VARCHAR,  -- 're_embedder' / 'user_manual' / 'frequency_based' / 'none'
    resolution_detail TEXT,  -- JSON: {"winner": "entity_id", "reason": "...", "fallback": "..."}
    resolved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE(user_id, entity_id_1, entity_id_2, disputed_name)
);

CREATE INDEX idx_conflicts_status ON entity_name_conflicts(user_id, status);
CREATE INDEX idx_conflicts_created ON entity_name_conflicts(created_at DESC);
```

---

## Ingest Changes

### In FactStoreManager.register_alias() / entity_aliases INSERT:

**Current logic:**
```python
# If alias already marked preferred, update; otherwise insert
cur.execute("""
    INSERT INTO entity_aliases (user_id, alias, entity_id, is_preferred)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (user_id, alias) DO UPDATE SET
        is_preferred = EXCLUDED.is_preferred
""", (user_id, alias, entity_id, is_preferred))
```

**New logic (collision detection):**
```python
# 1. Check if alias is already preferred for a DIFFERENT entity
cur.execute("""
    SELECT entity_id FROM entity_aliases
    WHERE user_id = %s AND alias = %s AND is_preferred = true
""", (user_id, alias))

existing = cur.fetchone()

if existing and existing[0] != entity_id:
    # Collision detected: different entity claims same preferred name
    # Store conflict, do NOT overwrite alias
    cur.execute("""
        INSERT INTO entity_name_conflicts 
        (user_id, entity_id_1, entity_name_1, entity_id_2, entity_name_2, disputed_name, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending')
        ON CONFLICT DO NOTHING
    """, (user_id, existing[0], get_entity_display_name(existing[0]), 
          entity_id, display_name, alias))
    
    # Insert for new entity WITHOUT is_preferred (fallback to non-preferred)
    cur.execute("""
        INSERT INTO entity_aliases (user_id, alias, entity_id, is_preferred)
        VALUES (%s, %s, %s, false)
        ON CONFLICT (user_id, alias) DO NOTHING
    """, (user_id, alias, entity_id))
else:
    # No collision: proceed as normal
    cur.execute("""
        INSERT INTO entity_aliases (user_id, alias, entity_id, is_preferred)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, alias) DO UPDATE SET
            is_preferred = EXCLUDED.is_preferred
    """, (user_id, alias, entity_id, is_preferred))
```

---

## Re-Embedder Changes

### New loop in re_embedder.py:

```python
def resolve_name_conflicts(db_conn, user_id):
    """
    Evaluate pending conflicts via LLM context.
    """
    with db_conn.cursor() as cur:
        # Find all pending conflicts for this user
        cur.execute("""
            SELECT id, entity_id_1, entity_name_1, entity_id_2, entity_name_2, disputed_name
            FROM entity_name_conflicts
            WHERE user_id = %s AND status = 'pending'
            LIMIT 10  -- batch process
        """, (user_id,))
        
        conflicts = cur.fetchall()
        
        for conflict_id, eid1, name1, eid2, name2, disputed in conflicts:
            # Get context facts for both entities
            facts1 = get_entity_facts(eid1, limit=5)  # age, occupation, etc
            facts2 = get_entity_facts(eid2, limit=5)
            
            # LLM decides: who should own the disputed name?
            resolution = llm_resolve_conflict(
                entity1=(name1, facts1),
                entity2=(name2, facts2),
                disputed_name=disputed
            )
            # resolution = {"winner": eid1/eid2, "reason": "...", "fallback": "..."}
            
            # Update aliases based on resolution
            winner_eid = resolution["winner"]
            loser_eid = eid1 if winner_eid == eid2 else eid2
            
            # Winner keeps the preferred name
            cur.execute("""
                UPDATE entity_aliases
                SET is_preferred = true, updated_at = now()
                WHERE user_id = %s AND entity_id = %s AND alias = %s
            """, (user_id, winner_eid, disputed))
            
            # Loser gets fallback alias (non-preferred)
            fallback = resolution.get("fallback")
            if fallback:
                cur.execute("""
                    INSERT INTO entity_aliases (user_id, entity_id, alias, is_preferred)
                    VALUES (%s, %s, %s, false)
                    ON CONFLICT (user_id, alias) DO NOTHING
                """, (user_id, loser_eid, fallback))
            
            # Mark conflict resolved
            cur.execute("""
                UPDATE entity_name_conflicts
                SET status = 'resolved', resolution_method = 're_embedder',
                    resolution_detail = %s, resolved_at = now()
                WHERE id = %s
            """, (json.dumps(resolution), conflict_id))
            
            db_conn.commit()
            
            logger.info(f"Resolved conflict {conflict_id}: {disputed} → {winner_eid}")
```

### LLM Conflict Resolver:

```python
def llm_resolve_conflict(entity1, entity2, disputed_name):
    """
    Use LLM to decide which entity should own the disputed name.
    """
    name1, facts1 = entity1
    name2, facts2 = entity2
    
    prompt = f"""
    Two entities in a personal knowledge graph claim the same preferred name: "{disputed_name}"
    
    Entity 1: {name1}
    Facts: {facts1}
    
    Entity 2: {name2}
    Facts: {facts2}
    
    Who should own the preferred name "{disputed_name}"? Why?
    Respond in JSON: {{"winner": "Entity 1" / "Entity 2", "reason": "...", "fallback": "..."}}
    Example: {{"winner": "Entity 1", "reason": "Entity 2 is a child, nickname 'Gabby' should resolve to parent", "fallback": "gabriella"}}
    """
    
    response = call_llm(prompt)
    return json.loads(response)
```

---

## /query Changes

### Display Name Resolution (already exists, but now safer):

```python
def _resolve_display_names(preferred_names_dict, entities):
    """
    Resolve entity UUIDs to display names.
    Now handles collisions: if preferred_name missing, fall back to non-preferred aliases.
    """
    resolved = {}
    for entity_id in entities:
        # Try preferred name first
        preferred = preferred_names_dict.get(entity_id)
        if preferred:
            resolved[entity_id] = preferred
        else:
            # Fallback: query entity_aliases for ANY non-preferred alias
            alias = query_any_alias(entity_id)
            resolved[entity_id] = alias or entity_id
    
    return resolved
```

---

## Test Scenarios

### Scenario 1: Name Collision Detection
```
Ingest: User with pref_name="gabby"
Ingest: Child with pref_name="gabby"
Expected: entity_name_conflicts has 1 pending row
```

### Scenario 2: Re-Embedder Resolution
```
Setup: Conflict pending (user "gabby" vs child "gabby")
Run re-embedder cycle
Expected: Conflict marked resolved, winner determined, fallback alias assigned to loser
```

### Scenario 3: Full Path (Ingest → Conflict → Resolve → Query)
```
Ingest: User "Christopher" with nickname "gabby"
Ingest: Child "Gabriella" with nickname "gabby" and pref_name="gabby"
Conflict detected, stored pending
Re-embedder resolves: child gets "gabriella" fallback
Query: "Tell me about my family" returns Gabriella ✓
```

---

## Files to Modify

| File | Change |
|------|--------|
| `migrations/021_name_conflicts.sql` | NEW — create entity_name_conflicts table + indexes |
| `src/entity_registry/registry.py` | Update `register_alias()` to detect and store collisions |
| `src/re_embedder/embedder.py` | Add `resolve_name_conflicts()` function, integrate into main loop |
| `src/api/main.py` | Update `_resolve_display_names()` to fall back to non-preferred aliases |

---

## Success Criteria

- Collision detection works at ingest time ✓
- Conflicts stored in entity_name_conflicts table ✓
- Re-embedder resolves conflicts via LLM ✓
- Loser entities get fallback aliases (non-preferred) ✓
- /query display resolution handles missing preferred names ✓
- Full path test: "Gabriella missing → conflict detected → resolved → returned in query" ✓
- No data loss; all names preserved (just marked non-preferred) ✓
