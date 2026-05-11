# dprompt-69: Open-Ended Extraction + RAG Fallback (Eliminate Silent Failures)

**Date:** 2026-05-15  
**Severity:** Architecture  
**Status:** Specification complete

## Problem

Currently, the extraction prompt in the Filter has a **whitelist of rel_types**. Facts that don't fit (health status, ephemeral events, novel relationships) are **silently dropped** — never extracted, never ingested, never stored.

Example: "I pulled my back, visited the chiropractor, in bed resting"
- Result: ❌ Zero health facts stored, user thinks system "forgot"
- Reality: ✅ Facts were never extracted or ingested

This violates the **self-building ontology** principle. The LLM is smart enough to understand health facts, but the prompt constrains it.

## Solution

**Three-part refactor:**

1. **Loosen extraction prompt** — Remove whitelist constraints, let LLM attempt ANY rel_type
2. **Add RAG fallback** — If extraction returns empty, store raw text to Qdrant as unstructured context
3. **Trust the pipeline** — WGM gate + re-embedder handles novel rel_types (already does, just needs permission)

Result: **No silent failures.** Every message → captured (structured as fact or semantic as context).

## Architecture

```
User message: "I pulled my back, visited chiropractor, in bed resting"
│
├─▶ LLM extraction attempt (open-ended)
│     ├─▶ Success: novel rel_types (has_injury, health_status, visited_X)
│     │     └─▶ WGM gate → Class C staging
│     │           └─▶ re-embedder evaluates: approve/map/reject
│     │
│     └─▶ Empty return: []
│           └─▶ RAG fallback: /store_context (raw text to Qdrant)
│                 └─▶ Confidence 0.4, fact_class=C, rel_type="context"
│                       └─▶ /query semantic search captures meaning
│
└─▶ /query (synchronous, before model sees message)
      ├─▶ PostgreSQL baseline + graph + hierarchy (existing)
      ├─▶ Qdrant semantic search (captures both facts AND context)
      └─▶ Injected before model sees conversation
```

## Scope

### 1. Loosen Extraction Prompt

**File:** `openwebui/faultline_tool.py` (lines 86–171, `_TRIPLE_SYSTEM_PROMPT`)

**Current state:**
```python
REL_TYPE REFERENCE:
- also_known_as: nickname...
- pref_name: explicitly preferred...
- has_pet: person owns animal...
Common: spouse, parent_of, ..., instance_of, subclass_of, member_of, part_of.
- Use snake_case. Other types allowed if none fit.
```

**Problem:** "Other types allowed" is timid. LLM hesitates to attempt novel types.

**Solution:** Make novel extraction explicit and encouraged:
```python
REL_TYPE REFERENCE:
[keep existing examples]

NOVEL & EPHEMERAL REL_TYPES — extract whenever you see them:
- Health/status: has_injury, health_status, symptom_of, experiencing, recovering_from
- Ephemeral location: currently_at, is_visiting, temporarily_in, at_location_right_now
- Activity/state: is_doing, is_attempting, is_resting, is_sleeping, busy_with
- Transient events: visited_X, attended_X, spoke_with_X (one-time interactions)
- Relationships not yet formalized: considering_X, thinking_about_X, planning_with_X

CONFIDENCE for novel types: 0.4 (low confidence, Class C staging for evaluation)

RULE: If an entity or relationship exists but no standard rel_type fits, CREATE a novel rel_type in snake_case. Examples:
- "I pulled my back" → (user, has_injury, back, 0.4)
- "Visiting chiropractor" → (user, currently_at, chiropractor, 0.4) OR (user, visited, chiropractor, 0.4)
- "In bed resting" → (user, is_resting, bed, 0.4) OR (user, health_status, recovering, 0.4)
- "Thinking about a career change" → (user, considering, career_change, 0.4)

Novel rel_types go to Class C (30-day TTL). Re-embedder evaluates: approve, map to existing, or reject.
```

**Changes:**
- Add explicit novel rel_type examples with low-confidence defaults
- Change tone from "Other types allowed if none fit" → "Attempt novel rel_types for anything relevant"
- Document that novel types → Class C staging automatically

### 2. Add RAG Fallback

**File:** `openwebui/faultline_tool.py` (inlet filter logic, around line 350–400 where extraction is called)

**Current flow:**
```python
edges = await rewrite_to_triples(text, ...)  # returns [] if extraction fails
if edges:
    # POST /ingest
else:
    # silent drop, message continues to model
```

**New flow:**
```python
edges = await rewrite_to_triples(text, ...)
if edges:
    # POST /ingest (existing)
    await httpx.post(f"{FAULTLINE_API_URL}/ingest", json={...})
elif len(text.strip()) > 10:  # RAG fallback: only for substantial messages
    # POST /store_context (raw text → Qdrant as semantic context)
    await httpx.post(f"{FAULTLINE_API_URL}/store_context", json={
        "text": text,
        "user_id": user_id
    })
    # Log: "No structured facts extracted, stored as semantic context"
else:
    # Too short to be meaningful, silent drop OK
```

**Behavior:**
- Short messages (< 10 chars): silent drop (fine)
- Extraction succeeds: `/ingest` (structured facts)
- Extraction fails but text is substantial: `/store_context` (semantic context)
- Result: No meaningful information is lost

### 3. WGM Gate (No Changes)

Already handles novel rel_types correctly:
- Accepts novel rel_types
- Assigns Class C confidence 0.4
- Stages in `staged_facts` with 30-day TTL
- Re-embedder evaluates asynchronously

Just needs **permission** via loosened extraction prompt.

## Changes Summary

| File | Lines | Change | Impact |
|------|-------|--------|--------|
| `openwebui/faultline_tool.py` | 103–126 | Add novel rel_type examples + encouragement | Extraction becomes open-ended |
| `openwebui/faultline_tool.py` | ~350–400 | Add `/store_context` fallback | RAG captures what extraction misses |
| `tests/filter/test_relevance.py` | TBD | Add novel rel_type extraction tests | Verify new behavior |
| `tests/filter/test_relevance.py` | TBD | Add RAG fallback tests | Verify context storage |

## Success Criteria

✅ Extraction prompt explicitly encourages novel rel_types  
✅ Novel rel_type examples documented (health, ephemeral, transient)  
✅ RAG fallback implemented (if extraction fails, `/store_context` called)  
✅ Health facts (back injury example) now extracted as Class C  
✅ Ephemeral facts (currently at X, visiting Y) now extracted as Class C  
✅ No silent failures — every substantial message → captured somewhere  
✅ Tests pass (new + existing, no regressions)  
✅ dBug-009 example ("back injury") now works  

## Testing

**Pre-prod validation:**
1. Repeat health facts conversation: "I pulled my back, visited chiropractor, in bed"
   - Expected: Facts stored in staged_facts as Class C with low confidence
   - Expected: `/query` injects health context before model sees message
2. Query for health facts: "Tell me how I'm feeling"
   - Expected: System recalls back injury, recovery status
3. Novel rel_type evaluation:
   - Expected: re-embedder evaluates has_injury, health_status, etc. in next poll cycle
   - Expected: Some approved, some mapped to existing types, some rejected

## Notes

- This refactor **enables** the self-building ontology principle — LLM now attempts extraction without fear of constraint
- Worst case: RAG fallback ensures information isn't lost
- Best case: Novel rel_types are evaluated and approved for re-use
- No change to WGM gate, validation, or re-embedder logic — they already handle this
- Risk: Slightly more novel rel_types created → re-embedder workload up, but acceptable

---

**Reference:** dprompt-69.md (specification)  
**Next:** dprompt-69b.md (execution template for deepseek)
