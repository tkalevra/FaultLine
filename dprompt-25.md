# dprompt-25 — Layer Cascade Bug: Facts Not Assigned Layers During Ingest

## The Problem

dprompt-24 (nested taxonomy layers) was deployed and the cascade logic works correctly, BUT **facts are all being inserted with `layer=1` (the default)**, so layer 2+ queries never see layer 2+ facts.

**Result:** "tell me about my family" query detects entry_layer=2, cascade logic runs, but:
1. Looks for facts WHERE `rel_type IN layer_2_rel_types` (spouse, has_pet, etc.)
2. But those facts have `layer=1` (the default from migration)
3. So cascade finds: spouse fact (layer=1), but NOT has_pet fact for Fraggle

Fraggle exists, Mars → has_pet → Fraggle fact is in staged_facts, but the fact itself has `layer=1` because:
- Migration 020 added `layer INT DEFAULT 1` to facts/staged_facts tables
- Entity layer assignments work (entities.layer gets updated via `GREATEST(layer, %s)`)
- **But facts are inserted WITHOUT specifying the layer column**, so they all default to 1

## Root Cause

**File: `src/fact_store/store.py`, lines 25-35**

```python
cur.execute(
    "INSERT INTO facts"
    " (user_id, subject_id, object_id, rel_type, provenance, confidence, source_weight, is_preferred_label)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    ...
)
```

The `commit()` method doesn't include `layer` in the column list. No layer is passed from ingest, so all facts default to `layer=1`.

Same issue in staged_facts insertion (inline in `src/api/main.py` around line 1700+).

## The Fix

### Part 1: Update FactStoreManager.commit()

**File: `src/fact_store/store.py`**

Change the signature to accept layer:
```python
def commit(self, connections: list[tuple], confidence: float = 1.0, source_weight: float = 1.0, layer: int = 1) -> int:
```

Update the INSERT to include layer:
```python
cur.execute(
    "INSERT INTO facts"
    " (user_id, subject_id, object_id, rel_type, provenance, confidence, source_weight, is_preferred_label, layer)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (user_id, subject_id, object_id, rel_type)"
    " DO UPDATE SET"
    "   confirmed_count = facts.confirmed_count + 1,"
    "   last_seen_at    = now(),"
    "   updated_at      = now(),"
    "   layer = GREATEST(facts.layer, EXCLUDED.layer)",
    (user_id, sub, obj, rel, prov, confidence, source_weight, is_preferred, layer),
)
```

**Why `GREATEST(facts.layer, EXCLUDED.layer)`?** If a fact is re-ingested with a different rel_type context (rare), promote its layer up, never down.

### Part 2: Pass layer from ingest to commit()

**File: `src/api/main.py`, around line 1590+**

When calling `commit()`, pass the layer:
```python
fact_store_manager.commit(
    fact_edges_list,
    confidence=confidence_value,
    source_weight=1.0,
    layer=_entity_layer  # ← ADD THIS
)
```

`_entity_layer` is already calculated via `_classify_entity_layer(edge.rel_type)`.

### Part 3: Fix staged_facts insertion

**File: `src/api/main.py`, around line 1700+**

When inserting to staged_facts, also add layer:

Find the direct `INSERT INTO staged_facts` call and update it:
```python
cur.execute(
    "INSERT INTO staged_facts"
    " (user_id, subject_id, object_id, rel_type, fact_class, provenance, confidence, first_seen_at, last_seen_at, layer)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s)"
    " ON CONFLICT (user_id, subject_id, object_id, rel_type)"
    " DO UPDATE SET"
    "   confirmed_count = staged_facts.confirmed_count + 1,"
    "   last_seen_at = now(),"
    "   layer = GREATEST(staged_facts.layer, EXCLUDED.layer)",
    (..., _entity_layer)  # ← ADD at the end
)
```

## Test After Fix

```
DELETE FROM facts WHERE rel_type IN ('spouse', 'has_pet', 'lives_in') AND user_id = 'test-user';
DELETE FROM staged_facts WHERE rel_type IN ('spouse', 'has_pet', 'lives_in') AND user_id = 'test-user';

POST /ingest: "I have wife Mars. Mars has dog Fraggle."
POST /query: "tell me about my family"

Expected log output:
- query.layer_cascade layer=1 rel_types=(pref_name, age, ...) hits=X
- query.layer_cascade layer=2 rel_types=(..., spouse, has_pet, ...) hits=Y
- spouse fact returned with layer=2 ✓
- has_pet fact returned with layer=2 ✓
- transitive expansion via household taxonomy: fraggle included ✓

Expected response: "Your family includes Mars (spouse) and Fraggle (pet)"
```

## Files to Change

1. `src/fact_store/store.py` — commit() signature and INSERT statement
2. `src/api/main.py` — update commit() calls to pass layer parameter; update staged_facts INSERT to include layer

## Why This Matters

Dprompt-24's cascade logic is correct. The bug is in the data layer: facts aren't being marked with their correct scope. Once facts have the right layer, cascade queries will filter correctly and return the right scoped results.

This is a 15-minute fix; fixes the whole system.
