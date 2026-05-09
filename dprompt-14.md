# Deepseek Debug Prompt: /query Baseline Path Validation (CRITICAL)

**Scope:** Validate why spouse fact exists in database but `/query` doesn't return it.

**Evidence:**
- Fact in DB: `(user_id: 3f8e6836-72e3-43d4-bbc5-71fc8668b070, subject_id: 3f8e6836..., object_id: 54214459..., rel_type: spouse)`
- Fact properties: fact_class=A (immediate commit), confidence=1.0, qdrant_synced=t
- LLM response to "tell me about my family": generic, no spouse fact injected
- Query `/query` endpoint should return this fact via baseline retrieval

**Problem:** `/query` baseline facts path is not returning the spouse fact despite it existing in the database.

---

## Root Cause Hypothesis

The `/query` endpoint has multiple retrieval paths:
1. **Baseline facts** — identity-anchored facts (should find spouse)
2. **Graph traversal** — self-referential signals triggered on specific queries
3. **Qdrant vector search** — semantic similarity

**The spouse fact should be caught by baseline retrieval.** If it's not, investigate:

### Hypothesis 1: Baseline query is not executing correctly

In `src/api/main.py`, `/query` endpoint, the baseline query looks like:

```python
baseline_facts = _fetch_user_facts(db, user_id, user_entity_id, rel_types=None)
```

This should return ALL facts for the user. Check:
- Is `_fetch_user_facts()` being called?
- What does it return for this user?
- Does the result include the spouse fact?

**Debug:** Add logging before/after baseline query:
```python
log.info("query.baseline_start", user_id=user_id)
baseline_facts = _fetch_user_facts(db, user_id, user_entity_id, rel_types=None)
log.info("query.baseline_done", count=len(baseline_facts), 
         has_spouse=any(f.get("rel_type") == "spouse" for f in baseline_facts))
```

If `has_spouse=False`, the baseline query is broken.

### Hypothesis 2: Metadata stripping removing facts accidentally

After the nuclear UUID redaction fix, the metadata stripping might be over-zealous. Check:

```python
# Before stripping
log.info("query.before_strip", count=len(merged_facts), 
         sample_rel_types=[f.get("rel_type") for f in merged_facts[:3]])

# Strip metadata
_INTERNAL_KEYS = (...)
for _f in merged_facts:
    for _k in _INTERNAL_KEYS:
        _f.pop(_k, None)

# After stripping
log.info("query.after_strip", count=len(merged_facts),
         sample_rel_types=[f.get("rel_type") for f in merged_facts[:3]])
```

If fact count drops, stripping is removing facts incorrectly.

### Hypothesis 3: Relevance gate filtering out the spouse fact

Even if baseline returns the spouse fact, it must pass the relevance gate. The gate is:

```python
scored = []
for fact in facts_list:
    score = calculate_relevance_score(fact, query)
    if score >= RELEVANCE_THRESHOLD:  # 0.4
        scored.append(fact)
```

**Check:** What score does the spouse fact get for query "tell me about my family"?

```python
spouse_fact = {...}  # The spouse fact
score = calculate_relevance_score(spouse_fact, "tell me about my family")
log.info("query.relevance_spouse", score=score, threshold=0.4, passes=(score >= 0.4))
```

If score < 0.4, the relevance gate is rejecting it. The gate logic is:
- Query signal match (0.0–0.6): keyword overlap between query and fact signals
- Confidence bonus (0.0–0.3): fact.confidence * 0.3
- Sensitivity penalty (-0.5): applied for sensitive rels (born_on, lives_at, etc.) unless explicitly queried

**For spouse fact:**
- Query "tell me about my family" should have signal match on "family" keyword
- spouse rel_type is NOT in `_SENSITIVE_RELS` → no penalty
- Expected score: ~0.3–0.6 (should pass 0.4 threshold)

If it's scoring < 0.4, debug `calculate_relevance_score()`.

---

## Validation Plan

### Step 1: Manual `/query` curl test

```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "tell me about my family",
    "user_id": "3f8e6836-72e3-43d4-bbc5-71fc8668b070"
  }' | jq '.facts | length, [.[] | .rel_type]'
```

Output should show:
- Fact count > 0
- Array includes "spouse"

If not:
- Output count=0 → facts not returned from /query (baseline or merging broken)
- Output doesn't include "spouse" → specific fact filtered out

### Step 2: Debug logging strategy

Add instrumentation at each retrieval stage:

```python
# /query endpoint
log.info("query.start", user_id=user_id[:8], query_text=query_text[:50])

# After baseline fetch
log.info("query.baseline", count=len(baseline_facts), 
         rel_types=[f.get("rel_type") for f in baseline_facts[:5]])

# After graph traversal
log.info("query.graph", count=len(direct_facts), 
         rel_types=[f.get("rel_type") for f in direct_facts[:5]])

# After Qdrant merge
log.info("query.qdrant", count=len(qdrant_facts))

# After relevance scoring
log.info("query.scored", count=len(scored), 
         threshold=0.4, sample_scores=...)

# After stripping
log.info("query.stripped", count=len(merged_facts))

# Final return
log.info("query.return", fact_count=len(merged_facts))
```

Run `/query` and check logs. Identify at which stage the spouse fact is lost.

### Step 3: Relevance gate debug

If spouse fact makes it to scoring, debug why it might score < 0.4:

```python
def calculate_relevance_score_debug(fact, query):
    signal_score = ...
    confidence_score = ...
    sensitivity_penalty = ...
    total = signal_score + confidence_score + sensitivity_penalty
    
    log.info("relevance_debug", 
             rel_type=fact.get("rel_type"),
             signal=signal_score,
             confidence=confidence_score,
             penalty=sensitivity_penalty,
             total=total)
    
    return total
```

### Step 4: Check for entity resolution issues

The spouse fact has `object_id = 54214459-3d2e-5ff5-8c6c-a541667d93aa`. This should resolve to a display name.

```python
if registry:
    spouse_display = registry.get_preferred_name(user_id, object_id)
    log.info("query.spouse_display", uuid=object_id[:12], display=spouse_display)
```

If `spouse_display` is None or UUID, the entity alias isn't registered correctly.

---

## Done When

- ✅ `/query` curl test returns spouse fact for "tell me about my family" query
- ✅ Logs show spouse fact at each stage: baseline → merge → scoring → stripping → return
- ✅ If lost at relevance gate, relevance score >= 0.4 (threshold passed)
- ✅ If lost at entity resolution, spouse display name resolves correctly
- ✅ LLM response includes spouse information in memory injection
- ✅ Fact flows end-to-end: DB → /query → Filter memory → LLM

Ship debug logs and fix.

---

## Notes

- This is purely a retrieval validation. The fact is correctly stored in the database.
- The issue is somewhere in the `/query` path between database fetch and return.
- Baseline retrieval should be the fastest path — debug there first.
- Relevance gate is the second most likely culprit — very conservative threshold of 0.4 could be filtering out valid facts.
- If both baseline and gate pass, the issue is in the merge/stripping logic.
