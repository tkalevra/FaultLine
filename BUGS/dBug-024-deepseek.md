# dBug-024-deepseek: Child Name Correction — Investigation & Fix

**Status:** RESOLVED (2026-05-15)
**Root cause:** `src/api/main.py:2313` self-referential guard dropped valid identity facts
**Fixed by:** DEEPSEEK-24E (subject==object guard exemption)

## Root Cause Trace

### Symptom
User says "My childrens names are bob, charlie, and alice" — system returns stale "children=Son" instead of "children=charlie".

### Flow trace

```
User input: "My childrens names are bob, charlie, and alice"
  │
  ├─▶ /extract/rewrite (LLM)
  │     Returns 12 triples including:
  │     (bob, pref_name, bob)  ← prompt fix (DEEPSEEK-24A)
  │     (charlie, pref_name, charlie)
  │     (alice, pref_name, alice)
  │
  ├─▶ /ingest pronoun normalizer (line 2068)
  │     Subjects bob/charlie/alice are not pronouns → pass
  │
  ├─▶ /ingest pref_name injector (line 2092)
  │     bob/charlie/alice already have pref_name triples → skip
  │
  ├─▶ raw_inferred → inferred_relations → edges_dict
  │     All 12 triples pass through
  │
  ├─▶ edges loop (line 2313): `if edge.subject == edge.object: continue`
  │     ✗ (bob, pref_name, bob) → subject="bob", object="bob" → DROPPED
  │     ✗ (charlie, pref_name, charlie) → DROPPED
  │     ✗ (alice, pref_name, alice) → DROPPED
  │
  └─▶ Commit: 9 facts (parent_of/child_of/instance_of), 0 pref_name facts
```

### Why the guard exists
Line 2313 prevents truly self-referential facts like `(bob, knows, bob)` — an entity can't know itself. But for identity rel_types `pref_name` and `also_known_as`, subject == object is the NORM: the entity IS its own name.

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
55c13545 | pref_name | charlie | confidence=1.0
55c13545 | pref_name | son   | confidence=0.5 (superseded)
entity_aliases: charlie is_preferred=true, son is_preferred=false
```

### End-to-end test
- Input: "tell me about my family"
- Before fix: "bob, Son, and alice"
- After fix: "bob, charlie, and alice" ✓

## Lessons

1. **Subject==object guards must exempt identity facts.** `(entity, pref_name, name)` and `(entity, also_known_as, alias)` are valid when entity name equals display name.

2. **LLM prompt changes need verification beyond extraction.** The prompt fix worked (LLM emitted pref_name) but a downstream guard silently dropped the facts.

3. **Defense-in-depth requires testing each layer.** The injector (layer 2) looked correct, but layer 3 (the guard) was the actual blocker.
