# Audit: All Internal LLM Prompts — Loop-Back Risk Assessment

**Status:** AUDIT COMPLETE  
**Date:** 2026-05-20  
**Question:** Which internal prompts need `_FAULTLINE_INTERNAL_PREFIX` marking?

---

## Summary

**Found 4 locations generating LLM prompts:**

| File | Function | Prompt | Needs Marker? |
|------|----------|--------|---------------|
| openwebui/faultline_function.py | `_extract_retraction()` | "You are a retraction/correction detection system..." | ✅ YES — marked |
| src/wgm/gate.py | ontology validation | "You are a knowledge graph ontology validator..." | ⚠️ MAYBE |
| src/re_embedder/embedder.py | `_query_llm_for_rel_type_metadata()` | "You are an ontology expert analyzing..." | ⚠️ MAYBE |
| src/api/main.py | `/extract/rewrite` | Extraction prompt from `_build_extraction_prompt()` | ⚠️ MAYBE |

---

## Detailed Analysis

### 1. Filter: `_extract_retraction()` (openwebui/faultline_function.py:1710)

**Prompt:**
```
"You are a retraction/correction detection system.
Analyze the user's message for retraction intent..."
```

**Loop-back risk:** ✅ **HIGH — CONFIRMED**
- Filter is entry point to OpenWebUI pipeline
- Inlet intercepts ALL messages (including system re-injections)
- Prompt CONFIRMED to loop back (seen 6000+ times in logs)
- **Status:** Already marked with `_FAULTLINE_INTERNAL_PREFIX`

**Mitigation:** ✅ COMPLETE

---

### 2. WGM Gate: Ontology Validation (src/wgm/gate.py)

**Prompt:**
```
"You are a knowledge graph ontology validator. 
Respond only with valid JSON..."
```

**Loop-back risk:** ❓ **UNKNOWN**

**Analysis:**
- WGM gate is called by `/ingest` endpoint (in backend)
- WGM calls LLM directly (not through OpenWebUI pipeline)
- LLM response returns to WGM, processed locally
- Response does NOT go through OpenWebUI chat pipeline
- **Conclusion:** Prompt does NOT loop back to inlet

**Could it indirectly?**
- If validation prompt somehow gets stored and later retrieved → possibly
- If validation response is user-facing and re-injected → possibly
- **Unlikely but possible:** If validation prompt appears in logs and gets fed back as test data

**Mitigation:** **OPTIONAL** — Low risk, but could mark for consistency

---

### 3. Re-embedder: Ontology Expert (src/re_embedder/embedder.py:736)

**Prompt:**
```
"You are an ontology expert analyzing a relationship pattern 
from conversation data.

Pattern: [rel_type]
..."
```

**Loop-back risk:** ❓ **UNKNOWN**

**Analysis:**
- Re-embedder runs in background poll loop (not directly in request path)
- Calls LLM directly (not through OpenWebUI pipeline)
- Response is parsed and stored in DB (not user-facing)
- Response does NOT go through inlet

**Could it indirectly?**
- If re-embedder response is stored as text and later becomes a message → possible but unlikely
- If metadata is somehow re-injected as chat message → unlikely

**Mitigation:** **OPTIONAL** — Very low risk, but could mark for defensive consistency

---

### 4. Backend: Extraction Prompt (src/api/main.py:2724-2732)

**Prompt:** Built dynamically via `_build_extraction_prompt(db)`
```
"You are a relationship fact extractor for a personal knowledge graph.
Output ONLY a raw JSON array..."
```

**Loop-back risk:** ❌ **VERY LOW**

**Analysis:**
- `/extract/rewrite` endpoint called by filter
- Backend calls LLM directly (not through OpenWebUI)
- LLM response returns to `/extract/rewrite` endpoint
- Response is parsed as JSON (edges), sent back to filter
- Response does NOT go through OpenWebUI chat pipeline as a message
- Filter does NOT re-inject extraction response as a chat message

**Mitigation:** **NOT NEEDED** — No loop-back path

---

## Risk Assessment Matrix

| Location | Insertion Point | Loop-Back Path | Risk Level | Action |
|----------|-----------------|----------------|-----------|--------|
| Filter retraction | OpenWebUI inlet | Through chat pipeline | 🔴 HIGH | ✅ Mark (done) |
| WGM validation | Backend /ingest | Local processing only | 🟡 MEDIUM | 🔶 Mark (recommended) |
| Re-embedder ontology | Background loop | Local + DB storage | 🟡 MEDIUM | 🔶 Mark (recommended) |
| Backend extraction | Backend /extract/rewrite | Parsed as JSON, not message | 🟢 LOW | ❌ No need |

---

## Recommendation

**Tier 1 (CRITICAL — Already Done):**
- ✅ Filter retraction prompt — marked and deployed

**Tier 2 (RECOMMENDED — For Defensive Consistency):**
- 🔶 WGM validation prompt — mark with same prefix
- 🔶 Re-embedder ontology prompt — mark with same prefix

**Tier 3 (NOT NEEDED):**
- ❌ Backend extraction prompt — doesn't loop back, skip

---

## Implementation: Mark Tier 2 (Defensive)

To mark WGM and re-embedder prompts, we need to:

1. **Export marker from filter** → Create shared constant
   - Move `_FAULTLINE_INTERNAL_PREFIX` to a shared location
   - OR duplicate it in gate.py and embedder.py with comment

2. **Update WGM gate prompt:**
   ```python
   from src.utils import FAULTLINE_INTERNAL_PREFIX  # if shared
   # OR
   _FAULTLINE_INTERNAL_PREFIX = "[FaultLine-Internal]"
   
   payload = build_llm_payload(
       messages=[
           {"role": "system", "content": f"{_FAULTLINE_INTERNAL_PREFIX} You are a knowledge graph..."}
       ]
   )
   ```

3. **Update re-embedder prompt:**
   ```python
   _FAULTLINE_INTERNAL_PREFIX = "[FaultLine-Internal]"
   
   prompt = f"""{_FAULTLINE_INTERNAL_PREFIX} You are an ontology expert analyzing..."""
   ```

---

## Decision Points

**Option A (Conservative):** Mark only filter (already done)
- ✅ Fixes confirmed loop-back (retraction cascade)
- ✅ Minimal changes
- ⚠️ Tier 2 remains unprotected (low risk but possible)

**Option B (Defensive):** Mark filter + Tier 2 (WGM + re-embedder)
- ✅ Comprehensive coverage
- ✅ Future-proof (any prompt marked automatically)
- ✅ Consistent architecture
- ⚠️ Slightly more code (3 locations)

**Recommendation:** Option B (mark Tier 2 as well)
- Cost: 6 lines of code (2 per file)
- Benefit: Comprehensive defense + consistency
- Risk: None (marking is additive, doesn't change behavior if not looped back)

---

## Conclusion

**Current state (already deployed):**
- ✅ Filter retraction prompt marked
- ✅ Confirmed loop-back eliminated
- ⚠️ WGM + re-embedder unprotected but low-risk

**Recommended next step:**
- Mark WGM validation + re-embedder ontology prompts for defensive consistency
- Takes ~10 minutes
- Future-proofs against any indirect loop-back paths
