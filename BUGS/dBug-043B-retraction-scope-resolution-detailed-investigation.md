# dBug-043B: Retraction Scope Resolution — Detailed Investigation

**Status**: ROOT CAUSE IDENTIFIED  
**Date**: 2026-05-17 21:45 UTC  
**Findings**: Categorical scope is detected correctly, but `rel_types` array is EMPTY when reaching `/retract` endpoint

---

## The Complete Call Chain

### Step 1: Filter → Backend (Retraction Detection)

**File**: `openwebui/faultline_function.py`, line ~1650

```
Filter inlet() 
  ↓
User says: "forget about them" (retraction trigger)
  ↓
Call POST /extract/retraction to FaultLine backend
  ↓
Backend LLM evaluates text against _RETRACTION_PROMPT
  ↓
LLM returns JSON: {
    "is_retraction": true,
    "scope_level": "categorical",
    "category": "pets",
    "subject": null,
    "rel_type": null,
    "confidence": 1.0
    # NOTE: NO "rel_types" in response!
}
```

### Step 2: Filter Processes Retraction Response

**File**: `openwebui/faultline_function.py`, line ~1690

Filter receives the LLM response and passes it through to `/retract`:

```python
result = response.json()  # Contains the LLM JSON above
if result.get("retraction"):
    scope = result["retraction"]  # Now scope has no rel_types!
    
    # Later, Filter calls:
    POST /retract with:
    {
        "user_id": "10d7d879-63cd-4f31-92ce-f2c9edb760ab",
        "scope": {
            "scope_level": "categorical",
            "category": "pets",
            "subject": None,
            "rel_type": None,
            "rel_types": [],  # ← EMPTY! Never populated!
            "entity_types": []  # ← EMPTY!
        }
    }
```

### Step 3: /retract Endpoint Receives Empty rel_types

**File**: `src/api/main.py`, line 6188-6196 (categorical scope handler)

```python
elif scope_level == "categorical":
    # Remove all rels in category + entities + taxonomy membership
    for rel_type in rel_types:  # rel_types is EMPTY ARRAY!
        cur.execute(
            """UPDATE facts SET superseded_at = now()
               WHERE user_id = %s AND rel_type = %s AND superseded_at IS NULL""",
            (req.user_id, rel_type.lower()),
        )
        retracted_count += cur.rowcount  # Loop never executes, rowcount=0
```

**Result**: `rowcount=0` because the loop body never executes.

---

## Root Cause: Two-Step Cascade Failure

### The LLM Response Problem

**File**: `src/api/main.py`, line 2841-2910 (`/extract/retraction` endpoint)

The LLM is instructed to return:
```json
{"is_retraction": bool, "scope_level": "...", "category": "...", "rel_type": "...", "confidence": float}
```

**The prompt at lines 278-313** does NOT ask the LLM to return `rel_types` or `entity_types`. It only asks for:
- `is_retraction`
- `scope_level`
- `subject` (for granular)
- `rel_type` (for granular)
- `category` (for categorical)
- `old_value`
- `confidence`

So the LLM correctly returns what it was asked for, but this response is **incomplete for categorical retraction**.

### The /retract Endpoint's Missing Fallback

**File**: `src/api/main.py`, line 6188-6196 (categorical scope handler)

The endpoint **expects** `rel_types` to be populated in the request, but there's **no fallback to query `entity_taxonomies` if they're missing**.

The correct flow should be:
```python
elif scope_level == "categorical":
    category = req.scope.get("category")
    rel_types = req.scope.get("rel_types", [])
    
    # MISSING: Fallback query!
    if not rel_types and category:
        # Query entity_taxonomies for the category
        with db.cursor() as cur:
            cur.execute(
                "SELECT rel_types_defining_group FROM entity_taxonomies WHERE taxonomy_name = %s",
                (category,)
            )
            row = cur.fetchone()
            if row:
                rel_types = row[0] or []
    
    # NOW execute the retraction with populated rel_types
    for rel_type in rel_types:
        ...
```

---

## Why This Matters Now (Post-dBug-044)

With dBug-044 FIXED:
- ✅ `entity_taxonomies["pets"]` NOW EXISTS with `rel_types_defining_group = ["has_pet"]`
- ✅ Taxonomies are being auto-created during ingest
- ❌ But `/retract` endpoint can't find them because it's not querying the table!

**The irony**: The taxonomies exist in the database, but the `/retract` endpoint isn't asking for them.

---

## Test Evidence

**From our test (2026-05-17 21:29):**

```
Ingest:  has_pet(emma, fraggle) → committed ✓
Verify:  SELECT * FROM entity_taxonomies WHERE taxonomy_name='pets'
Result:  ✓ ONE ROW: taxonomy_name='pets', rel_types_defining_group='{has_pet}'

Retraction:  POST /retract scope={'scope_level': 'categorical', 'category': 'pets'}
Log:         retract.commit_done rowcount=0 scope_level=categorical
Database:    has_pet fact NOT superseded (superseded_at IS NULL)
```

**The gap**: The pet taxonomy EXISTS but `/retract` never queries it.

---

## The Fix (Two Options)

### Option A: LLM Returns rel_types

Make the LLM fetch and return `rel_types`:

```json
{"is_retraction": true, "scope_level": "categorical", "category": "pets", "rel_types": ["has_pet"], ...}
```

**Pros**: Single point of resolution  
**Cons**: Extra LLM latency, LLM needs DB access (network complexity)

### Option B: /retract Endpoint Queries on Demand (Recommended)

The `/retract` endpoint looks up missing `rel_types`:

```python
elif scope_level == "categorical":
    category = req.scope.get("category")
    rel_types = req.scope.get("rel_types", [])
    
    # Fallback: query entity_taxonomies if rel_types empty
    if not rel_types and category:
        with db.cursor() as cur:
            cur.execute(
                "SELECT rel_types_defining_group FROM entity_taxonomies WHERE taxonomy_name = %s",
                (category,)
            )
            row = cur.fetchone()
            if row:
                rel_types = row[0] or []
    
    # Execute retraction with populated rel_types
    for rel_type in rel_types:
        cur.execute(
            """UPDATE facts SET superseded_at = now()
               WHERE user_id = %s AND rel_type = %s AND superseded_at IS NULL""",
            (req.user_id, rel_type.lower()),
        )
        retracted_count += cur.rowcount
```

**Pros**: Cleaner separation, backend responsible for scope resolution, no LLM overhead  
**Cons**: One extra DB query per categorical retraction

### Why Option B is Better

- **Philosophy**: Retraction scope resolution is **backend responsibility**, not LLM responsibility
- **Reliability**: Doesn't depend on LLM returning correct JSON structure
- **Performance**: No LLM call needed; simple DB query
- **Alignment**: Matches dBug-044's aliceign: "Ingest Smart, Extract Dumb" → "Backend Smart, Frontend Dumb"

---

## Code Location to Fix

**File**: `src/api/main.py`  
**Function**: `retract_fact()`  
**Lines**: 6188-6196

Current code:
```python
elif scope_level == "categorical":
    # Remove all rels in category + entities + taxonomy membership
    for rel_type in rel_types:  # rel_types may be empty!
        cur.execute(...)
```

Should be:
```python
elif scope_level == "categorical":
    category = req.scope.get("category")
    rel_types = req.scope.get("rel_types", [])
    
    # ADDED: Fallback query to resolve category → rel_types
    if not rel_types and category:
        with db.cursor() as _cur:
            _cur.execute(
                "SELECT rel_types_defining_group FROM entity_taxonomies WHERE taxonomy_name = %s",
                (category.lower(),)
            )
            row = _cur.fetchone()
            if row:
                rel_types = row[0] or []
                log.info("retract.category_resolved", category=category, rel_types=rel_types)
    
    # Remove all rels in category + entities + taxonomy membership
    for rel_type in rel_types:
        cur.execute(...)
```

---

## Summary

| Component | Status | Details |
|---|---|---|
| **LLM Detection** | ✅ Working | Correctly detects `category='pets'` |
| **Taxonomy Creation** | ✅ Working (dBug-044) | `pets` taxonomy exists in DB |
| **Filter Scope Building** | ❓ Partial | Only returns `category`, not `rel_types` |
| **/retract Endpoint Lookup** | ❌ MISSING | Doesn't query `entity_taxonomies` for rel_types |
| **Database Query** | ✅ Would Work | If rel_types were populated, UPDATE would match facts |

**Critical Gap**: `/retract` endpoint doesn't have fallback logic to query `entity_taxonomies` when `rel_types` is empty.

**Fix Effort**: ~15 lines of code + logging  
**Risk**: Low (isolated to categorical scope handler, no impact on granular/relational scopes)  
**Testing**: Clear test case available (pet retraction should now work)
