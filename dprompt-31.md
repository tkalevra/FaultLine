# dprompt-31 — Live Pipeline Debugging: Gabriella Ingest Failure

## Purpose

Debug why Gabriella/Gabby doesn't persist through the ingest pipeline despite Filter LLM acknowledging her. Test against live OpenWebUI instance with real model, capture logs, identify failure point.

## The Issue

**User prompt:** "We have a third Daughter, Gabriella who's 10 and goes by Gabby"

**Filter response:** "Got it, Des! It sounds like your family is even closer than I realized with the addition of Gabriella, or "Gabby," who is 10 years old."

**Query response:** "It sounds like your family unit consists of you, Mars (your spouse), and your two children, Cyrus and Desmonde." ← Gabriella missing

**Expected:** Gabriella facts should persist and be retrievable in /query

**Actual:** Gabriella acknowledged by Filter but missing from query results

## Debug Path

### 1. Filter Extraction
- Does Filter LLM extract Gabriella as a fact edge?
- What rel_types are extracted? (parent_of, pref_name, age, etc.)
- Does extraction include "Gabby" as alias?

### 2. Ingest Pipeline
- Do extracted edges reach `/ingest`?
- Do they pass WGM validation?
- Are they classified (Class A/B/C)?
- Do they appear in `staged_facts` or `facts` table?

### 3. Query Retrieval
- Does `/query` return facts for Gabriella?
- Are they filtered out by relevance scoring?
- Are they missing from baseline, graph, or Qdrant?

### 4. Qdrant Reconciliation
- Does re-embedder sync Gabriella facts?
- Are there orphaned Qdrant points?

## Test Scenario

**Setup:**
```
Model: faultline-wgm-test-10
User: Christopher (or test user)
Already ingested: Mars (spouse), Cyrus (19), Desmonde/Des (12)
```

**Ingest:**
```
Prompt: "We have a third Daughter, Gabriella who's 10 and goes by Gabby"
```

**Expected extraction:**
- (user, parent_of, Gabriella)
- (Gabriella, pref_name, Gabby)
- (Gabriella, age, 10)
- (Gabriella, instance_of, Person) [from entity typing]

**Expected query result:**
```
Prompt: "tell me about my family"
Returns: Mars, Cyrus, Desmonde, AND Gabriella
```

## Log Collection Points

1. **Filter extraction log:**
   - LLM input/output
   - Extracted edges
   - Types assigned

2. **Ingest logs:**
   - POST /ingest payload
   - WGM validation result
   - Fact classification
   - Entity registration

3. **Query logs:**
   - Graph traversal results
   - Hierarchy expansion results
   - Baseline facts fetch
   - Qdrant search results
   - Relevance scoring
   - Final merged result

4. **Re-embedder logs:**
   - Unsynced facts found
   - Qdrant upserts
   - Reconciliation check

## Debugging Steps

1. Start fresh conversation (new chat)
2. Ingest family scenario with Gabriella
3. Capture docker logs while testing: `ssh truenas -x "sudo docker logs -f open-webui" > gabriella_debug.log`
4. Query "tell me about my family"
5. Check PostgreSQL directly:
   ```sql
   SELECT * FROM facts WHERE object_id LIKE '%gabriella%' OR subject_id LIKE '%gabriella%';
   SELECT * FROM staged_facts WHERE object_id LIKE '%gabriella%' OR subject_id LIKE '%gabriella%';
   SELECT * FROM entities WHERE entity_type = 'Person' LIMIT 20;
   ```
6. Check Qdrant collection for Gabriella points
7. Trace failure point (extraction, ingest, query, or embedding)

## Expected Outcome

Identify which stage Gabriella is lost:
- **Filter stage:** Filter doesn't extract her (LLM issue)
- **Ingest stage:** Extraction works but ingest rejects/drops her (WGM/classification issue)
- **Query stage:** Fact stored but query doesn't return her (retrieval/scoring issue)
- **Embedding stage:** Facts exist but Qdrant reconciliation fails

## Success Criteria

- Gabriella facts are stored in PostgreSQL ✓
- Gabriella facts are queryable ✓
- Query "tell me about my family" returns all 4 family members ✓
- Logs clearly show where failure occurred (if still failing)
- Root cause identified for next phase
