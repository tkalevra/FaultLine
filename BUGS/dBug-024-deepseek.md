# dBug-024-deepseek: Child Name Correction — Investigation & Fix

**Status:** RESOLVED (2026-05-15)
**Root cause:** `src/api/main.py:2313` self-referential guard dropped valid identity facts
**Fixed by:** DEEPSEEK-24E (subject==object guard exemption)

## Root Cause Trace

### Symptom
User says "My childrens names are Alice, Bob, and Carol" — system returns stale "children=Son" instead of "children=Bob".

### Flow trace

```
User input: "My childrens names are Alice, Bob, and Carol"
  │
  ├─▶ /extract/rewrite (LLM)
  │     Returns 12 triples including:
  │     (gabby, pref_name, gabby)  ← prompt fix (DEEPSEEK-24A)
  │     (cyrus, pref_name, cyrus)
  │     (des, pref_name, des)
  │
  ├─▶ /ingest pronoun normalizer (line 2068)
  │     Subjects gabby/cyrus/des are not pronouns → pass
  │
  ├─▶ /ingest pref_name injector (line 2092)
  │     gabby/cyrus/des already have pref_name triples → skip
  │
  ├─▶ raw_inferred → inferred_relations → edges_dict
  │     All 12 triples pass through
  │
  ├─▶ edges loop (line 2313): `if edge.subject == edge.object: continue`
  │     ✗ (gabby, pref_name, gabby) → subject="gabby", object="gabby" → DROPPED
  │     ✗ (cyrus, pref_name, cyrus) → DROPPED
  │     ✗ (des, pref_name, des) → DROPPED
  │
  └─▶ Commit: 9 facts (parent_of/child_of/instance_of), 0 pref_name facts
```

### Why the guard exists
Line 2313 prevents truly self-referential facts like `(gabby, knows, gabby)` — an entity can't know itself. But for identity rel_types `pref_name` and `also_known_as`, subject == object is the NORM: the entity IS its own name.

### Fix
```python
# Before (line 2313):
if edge.subject == edge.object: continue

# After:
if edge.subject == edge.object:
    if edge.rel_type.lower() not in ("pref_name", "also_known_as"):
        continue
```

## All Changes Applied

| # | Change | Location | Purpose |
|---|--------|----------|---------|
| 1 | Prompt: name-list examples | `/extract/rewrite` L1667 | LLM now emits pref_name for name lists |
| 2 | Injector: entity-centric pref_name | `/ingest` L2092 | Safety net — catches missed entities |
| 3 | Resolver trigger: log marker | `/ingest` L3277 | Integration point for resolve_name_conflicts() |
| 4 | **FIX: subject==object guard** | `/ingest` L2313 | **Root cause — allow identity facts** |

## Verification

### Database (post-fix)
```
55c13545 | pref_name | cyrus | confidence=1.0
55c13545 | pref_name | boy   | confidence=0.5 (superseded)
entity_aliases: cyrus is_preferred=true, boy is_preferred=false
```

### End-to-end test
- Input: "tell me about my family"
- Before fix: "Alice, Son, and Carol"
- After fix: "Alice, Bob, and Carol" ✓

## Lessons

1. **Subject==object guards must exempt identity facts.** `(entity, pref_name, name)` and `(entity, also_known_as, alias)` are valid when entity name equals display name.

2. **LLM prompt changes need verification beyond extraction.** The prompt fix worked (LLM emitted pref_name) but a downstream guard silently dropped the facts.

3. **Defense-in-depth requires testing each layer.** The injector (layer 2) looked correct, but layer 3 (the guard) was the actual blocker.
