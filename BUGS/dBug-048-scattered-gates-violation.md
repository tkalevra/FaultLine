# dBug-048: Scattered Correction/Retraction Gates Violate Single Entry Point Architecture

**Status:** 🔴 OPEN (Analysis Complete, Ready for Implementation)  
**Severity:** High (Architectural violation, brittle, duplicate LLM calls)  
**Related:** dBug-047 (Correction Pattern Detection), dBug-043 (Retraction), CLAUDE.md (Filter dumb, backend smart)  
**Discovered:** 2026-05-18  
**Analysis:** Complete — Implementation ready via dprompt-115

---

## Problem Statement

Current architecture violates **single entry point principle** and **CLAUDE.md constraints**:

```
Current (BROKEN):
Filter → /retract (direct)
Filter → /evaluate-correction-pattern (direct) 
Filter → /ingest (normal flow)

Result:
- Three separate endpoints, three separate LLM calls
- Retraction gate outside ingest pipeline
- Correction gate outside ingest pipeline
- Filter routing logic (not dumb)
- Duplicate LLM infrastructure
```

**Observable behavior:**
- Filter must detect retraction AND correction AND ingest separately
- Three distinct code paths for what should be one unified pipeline
- Each gate has own LLM prompt (no shared definitions)
- No consistent metadata-driven routing

---

## Root Cause

**Architectural drift from CLAUDE.md principle:**
> "Filter is dumb, backend is smart. All writes flow through WGM validation gate."

**Current state:**
1. Retraction detection hardcoded in filter, `/retract` endpoint separate
2. Correction detection scattered across filter + attempted `/evaluate-correction-pattern`
3. Normal fact ingest flows through `/ingest`
4. Three parallel pipelines instead of one unified gate

**Why this is broken:**
- ❌ Filter has routing logic (violates "filter is dumb")
- ❌ Multiple LLM entry points (inefficient, inconsistent prompts)
- ❌ No single validation gate (facts/retractions/corrections treated differently)
- ❌ Metadata-driven definitions not reused across gates (LLM sees no context)

---

## Solution Architecture

**Single entry point: `/ingest` encompasses all gates**

```
Filter (dumb): 
  - Detects model response
  - Sends TEXT → /ingest (one call, that's it)
  - Loads facts from /query
  - Injects memory

Backend /ingest (smart):
  1. Load correction + retraction definitions from DB
  2. Call unified LLM with ALL definitions
     Input: text + definitions
     Output: {type: "retraction|correction|normal", action: {...}}
  3. Route based on LLM decision:
     - retraction → apply retraction logic → /retract handler (internal)
     - correction → extract pattern → store to correction_signals
     - normal → extract facts → WGM gate → commit
  4. Return unified response
```

**Key insight:** 
- Retraction gate INSIDE ingest (not separate endpoint)
- Correction gate INSIDE ingest (not separate endpoint)
- Single LLM call with unified prompt + DB definitions
- Filter calls ONE endpoint with ONE piece of data
- Backend handles routing internally

---

## Implementation Changes

### Filter (`openwebui/faultline_function.py`)
**REMOVE:**
- Retraction detection logic (`_detect_retraction_intent()`, calls to `/retract`)
- Correction pattern detection (`_detect_implicit_correction()`, calls to `/evaluate-correction-pattern`)
- Candidate recording (`_record_correction_signal_candidate()`)
- All endpoint-specific logic

**KEEP/SIMPLIFY:**
- `inlet()` receives user message
- One call: `await self._fire_ingest(text, source, user_id)` 
- Load facts: `await self._fire_query(user_id, text)`
- Inject memory block
- Return

### Backend (`src/api/main.py`)
**New unified `/ingest` logic:**
1. Query `correction_signals` table → get pattern definitions
2. Query `retraction_signals` table → get retraction signal definitions
3. Call LLM once with prompt:
   ```
   "Text: {text}
    
    Correction signal types (patterns + confidence thresholds):
    {from correction_signals table}
    
    Retraction signal types (keywords + priorities):
    {from retraction_signals table}
    
    Decide: Is this a retraction? A correction? Normal facts?
    Return JSON: {type, confidence, action}"
   ```
4. Route based on `type`:
   - **retraction:** Apply retraction (supersede facts), return early with confirmation
   - **correction:** Extract pattern, store to `correction_signals`, continue to normal ingest
   - **normal:** Standard fact extraction → WGM gate → commit

**REMOVE:**
- `/retract` public endpoint (move logic internal)
- `/evaluate-correction-pattern` endpoint (move logic internal)
- `/correction-signal-candidate` endpoint (handled by ingest now)

**KEEP/REFACTOR:**
- `_detect_retraction_pattern()` → internal helper
- `_detect_implicit_correction()` → becomes part of unified LLM call
- Retraction handler logic → internal function called by ingest
- Correction pattern extraction → internal function called by ingest

### Growth Architecture (dprompt-115)
Both retraction + correction signals feed the growth table:
- **Uncertain retractions** → `retraction_signal_evaluations`
- **Uncertain corrections** → `correction_signal_evaluations`
- **Re_embedder evaluates both** using same LLMOutputValidator framework

---

## Commits Required

1. **Remove scattered endpoints** (filter + backend)
   - Delete `/retract`, `/evaluate-correction-pattern`, `/correction-signal-candidate`
   - Remove filter-side detection logic

2. **Implement unified /ingest gate**
   - LLM prompt with definitions
   - Routing logic (retraction | correction | normal)
   - Handlers for each type

3. **Simplify filter to single entry point**
   - One `/ingest` call per message
   - One `/query` call for facts
   - Memory injection
   - Done

4. **Growth architecture integration** (dprompt-115)
   - Both uncertain retractions + corrections → evaluations tables
   - Re_embedder evaluates unified

---

## Test Verification

**Happy path — Correction:**
```bash
curl -X POST docker-host/api/chat/completions \
  -d '{"model": "faultline-test", "messages": [{"role": "user", "content": "My daughter bob is 8 not 7"}]}'
```
Expected:
1. Filter sends text to `/ingest`
2. LLM detects correction pattern ("is .+ not")
3. Pattern stored to `correction_signals` (confidence = 0.9)
4. Facts extracted + committed
5. Model responds acknowledging correction
6. Next message uses updated pattern from cache

**Happy path — Retraction:**
```bash
curl -X POST docker-host/api/chat/completions \
  -d '{"model": "faultline-test", "messages": [{"role": "user", "content": "forget everything I said about bob"}]}'
```
Expected:
1. Filter sends text to `/ingest`
2. LLM detects retraction ("forget")
3. Retraction handler supersealice bob facts
4. Response returns early with confirmation
5. No ingest/query happens

---

## Success Criteria

- ✅ Filter calls `/ingest` once per message (dumb filter)
- ✅ `/ingest` internally handles retraction + correction + normal (smart backend)
- ✅ Single LLM call with unified prompt + DB definitions
- ✅ Correction patterns stored to `correction_signals` on first detection
- ✅ Growth architecture feeds both uncertain retractions + corrections to evaluation tables
- ✅ No `/retract`, `/evaluate-correction-pattern`, `/correction-signal-candidate` endpoints
- ✅ Filter routing logic removed (all routing in backend)
- ✅ CLAUDE.md compliance: "Filter is dumb, backend is smart"

---

## dprompt Reference

**dprompt-115:** Implement unified /ingest gate with retraction + correction + normal routing
