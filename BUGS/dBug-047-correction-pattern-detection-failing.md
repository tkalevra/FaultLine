# dBug-047: Correction Pattern Detection Failing in Filter (dprompt-113/114)

**Status:** ✅ RESOLVED (2026-05-18)  
**Severity:** High (blocked Step 5 family ingest test)  
**Related:** dBug-041 (Corrections-Ignored), dprompt-113 (Confidence-Aware Correction Detection), dprompt-114 (Pattern Distribution)  
**Discovered:** 2026-05-18  
**Fixed:** 2026-05-18  

---

## Problem Statement

Filter correction pattern detection always returned `False` alicepite patterns stored in database and text containing matching patterns.

**Observable behavior:**
```
Input: "My daughter bob is 8 not 7"
Expected: is_correction=True (pattern "is .+ not" detected)
Actual: is_correction=False (pattern=False, cache empty)
```

---

## Root Cause

**Three layered issues:**

1. **Filter DB access blocked** - Filter runs in open-webui container (docker-host.helpalicekpro.ca) with no POSTGRES_DSN env var
   - Tried to load patterns directly from DB
   - Failed with "no database available"

2. **Startup timing dependency** - Original implementation tried to load at startup via environment variable
   - FAULTLINE_URL env var not set in open-webui container at code push time
   - Even when set, backend might not be ready at startup (connection refused)

3. **Synchronous/async mismatch** - Used blocking httpx.get() in async inlet context
   - Caused connection reset errors

---

## Solution Architecture

**Three-tier fix:**

### Tier 1: Backend Endpoint (dprompt-114+)
- Added `GET /correction-signals` endpoint in FaultLine backend
- Backend queries DB, returns patterns as JSON
- Filter no longer needs direct DB access

### Tier 2: Module-Level Global (Environment Dispatch)
- Read `FAULTLINE_URL` from environment ONCE at module load
- Store in global `_FAULTLINE_URL` (constant throughout lifetime)
- All cache loaders reference this global

### Tier 3: Dynamic Lazy-Load with TTL (No Startup Dependency)
```python
_CORRECTION_SIGNALS_CACHE_WITH_TTL: tuple = (0, [])  # (timestamp, data)
_CORRECTION_SIGNALS_TTL: int = 60  # seconds

def _get_correction_signals_cached() -> list[dict]:
    # Check TTL: if cache fresh, return cached
    # If TTL expired or empty, load from backend
    # Cache result with timestamp
    # Return data (may be empty on error, retry next TTL)
```

**Key insight:** No startup blocking. Loads on first inlet call when backend guaranteed ready.

### Tier 4: Universal Pattern (Applied to All Caches)
- Retraction signals: `_get_retraction_signals_cached()` with TTL
- Correction signals: `_get_correction_signals_cached()` with TTL
- All lazy-load at runtime, zero startup dependencies

---

## Implementation Changes

### Backend (`src/api/main.py`)
- Added `GET /correction-signals` endpoint (lines ~2504-2538)
- Queries `correction_signals` table
- Returns JSON: `[{pattern, type, priority, confidence, category}, ...]`

### Filter (`openwebui/faultline_function.py`)
- Module globals:
  - `_FAULTLINE_URL = os.getenv("FAULTLINE_URL", default)`
  - `_CORRECTION_SIGNALS_CACHE_WITH_TTL = (0, [])`
  - `_RETRACTION_SIGNALS_CACHE_WITH_TTL = (0, {})`

- Functions:
  - `_get_correction_signals_cached()` - TTL-based lazy load
  - `_get_retraction_signals_cached()` - TTL-based lazy load

- Inlet changes:
  - `signals = _get_correction_signals_cached()` (on every inlet call)
  - `_detect_implicit_correction(text, signals)` (pattern matching on live cache)

### Deployment (`Portainer`)
- Environment variable: `FAULTLINE_URL=http://${BACKEND_IP}:8001`
- Read once at filter module load, used globally throughout lifetime

---

## Commits

- `96fa6f8`: fix(dprompt-114+) - Load signals from backend endpoint
- `4c33c9d`: fix - Add missing 'Any' import
- `e4a21e6`: fix - Use async httpx for backend fetch
- `89edef1`: fix - Read FAULTLINE_URL once at module load
- `ba0dd52`: fix(dprompt-114+) - Load at startup, cache global
- `3aafdc1`: fix - Lazy-load at runtime, no startup dependency
- `88ff28d`: fix - All caches dynamic (retraction + correction)

---

## Final Architecture: LLM-Based Pattern Detection

**Correction patterns are now LLM-extracted, not regex-matched.**

### Flow
1. **Filter detects LLM says correction** (confidence ≥ 0.7)
2. **Filter sends text to backend** `POST /evaluate-correction-pattern`
3. **Backend LLM extracts pattern**
   - Calls LLM with prompt: "Extract correction pattern from text"
   - LLM returns: `{"pattern": "is .+ not", "type": "negation", "confidence": 0.85}`
4. **Backend queries correction_signals table**
   - If pattern exists: returns existing record
   - If pattern NEW: commits immediately with LLM-evaluated confidence
5. **Filter receives pattern + confidence**
6. **Pattern available to all filters via cached GET /correction-signals**

### Why This Works
- **No brittleness**: LLM extracts intelligent patterns, not regex guessing
- **Distributed**: All filters load from same cache, benefit from discoveries
- **Smart growth**: New patterns created with LLM confidence, not magic thresholds
- **Per CLAUDE.md**: Filter is dumb, backend is smart; LLM only validates (writes filtered through backend)

## Pattern Discovery: Dynamic Caching Pattern

**For any cache that depends on external service at load time:**

```python
# Module level
_CACHE_WITH_TTL: tuple = (0, {})  # (timestamp, data)
_CACHE_TTL: int = 60  # seconds

# Runtime function
def _get_cache_cached() -> dict:
    global _CACHE_WITH_TTL
    import time
    
    timestamp, cached_data = _CACHE_WITH_TTL
    now = time.time()
    
    # Return if fresh
    if timestamp > 0 and (now - timestamp) < _CACHE_TTL:
        return cached_data
    
    # Load if expired
    try:
        cached_data = fetch_from_service()
    except Exception:
        cached_data = {}  # graceful empty on error
    
    _CACHE_WITH_TTL = (now, cached_data)
    return cached_data

# Usage in inlet
cache = _get_cache_cached()
```

**Benefits:**
- ✅ No startup blocking (loads on first use)
- ✅ Graceful degradation (empty cache on error, retry at TTL)
- ✅ Automatic refresh (TTL-based, no restart needed)
- ✅ Single source of truth (global tuple managed by function)

---

## Test Verification

```bash
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer sk-..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "faultline-test",
    "messages": [{"role": "user", "content": "My daughter bob is 8 not 7"}],
    "stream": false
  }'
```

**Expected:** Model acknowledges correction, recognizes pattern match occurred.

**Filter logs:** `[FaultLine Filter] correction_signals_cache count=12`

---

## Lessons for Future Development

1. **Avoid startup dependencies** - Always lazy-load when external services involved
2. **TTL-based caching over startup loading** - Enables runtime refresh without restart
3. **Environment vars are global constants** - Read once at module load, reference throughout
4. **Graceful degradation beats blocking** - Empty cache + retry is better than startup failure
5. **All related caches should follow same pattern** - Retraction + Correction both use TTL-based lazy-load

---

## Success Criteria (ALL MET ✅)

- ✅ Filter logs visible and showing cache count
- ✅ Correction signals loaded from backend endpoint
- ✅ Pattern matching working ("is .+ not" matches "bob is 8 not 7")
- ✅ is_correction=True when pattern detected + LLM confidence ≥0.65
- ✅ No startup blocking on backend/DB availability
- ✅ Dynamic refresh (60s TTL) without restart
- ✅ Both retraction and correction signals use universal pattern
