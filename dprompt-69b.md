# dprompt-69b: Open-Ended Extraction + RAG Fallback — DEEPSEEK_INSTRUCTION_TEMPLATE

**Template version:** 1.0  
**Philosophy:** Eliminate silent failures. Trust LLM to attempt extraction, pipeline gates it. No information lost.

---

## CRITICAL: EXPECTATIONS & SCOPE

**⚠️ READ THIS FIRST:**

1. **Two files, two changes** — extraction prompt + RAG fallback logic
2. **Extraction prompt is philosophical** — "permit novel rel_types" + examples
3. **RAG fallback is mechanical** — "if extraction fails, call /store_context"
4. **Tests verify both** — novel type extraction + fallback behavior
5. **No changes to WGM, validation, or re-embedder** — they already handle this

This is a **medium-effort refactor** (1–2 hours). Straightforward changes, no architectural rework.

---

## Task

Refactor FaultLine extraction to eliminate silent failures by:
1. Loosening extraction prompt to encourage novel rel_type attempts
2. Adding RAG fallback for when extraction returns empty
3. Verifying no information is lost (structured or semantic)

**Read:** dprompt-69.md for full context. This prompt tells you HOW to execute it.

---

## Execution Sequence

### 1. Verify Current State

```bash
cd /home/chris/Documents/013-GIT/FaultLine-dev

# Confirm files exist
ls -la openwebui/faultline_tool.py
ls -la tests/filter/test_relevance.py

# Confirm current tests pass
pytest tests/filter/test_relevance.py -v
```

**Expected output:** All tests pass, no errors.

**Report in scratch.md:** Current test count + baseline established.

### 2. Modify `openwebui/faultline_tool.py` — Extraction Prompt

**Location:** Lines 103–126, within `_TRIPLE_SYSTEM_PROMPT`

**Current block:**
```python
Common: spouse, parent_of, child_of, sibling_of, works_for, lives_at, likes, dislikes, owns, age, height, weight, born_on, anniversary_on, met_on, instance_of, subclass_of, member_of, part_of.
- Use snake_case. Other types allowed if none fit.
```

**Replace with:**
```python
Common: spouse, parent_of, child_of, sibling_of, works_for, lives_at, likes, dislikes, owns, age, height, weight, born_on, anniversary_on, met_on, instance_of, subclass_of, member_of, part_of.

NOVEL & EPHEMERAL REL_TYPES — extract whenever you see them:
- Health/status: has_injury, health_status, symptom_of, experiencing, recovering_from
- Ephemeral location: currently_at, is_visiting, temporarily_in, at_location_right_now
- Activity/state: is_doing, is_attempting, is_resting, is_sleeping, busy_with
- Transient events: visited_X, attended_X, spoke_with_X (one-time interactions)
- Relationships not formalized: considering_X, thinking_about_X, planning_with_X

CONFIDENCE for novel types: Set confidence to 0.4 (low confidence, triggers Class C staging automatically).

RULE: If an entity or relationship exists but no standard rel_type fits, CREATE a novel rel_type in snake_case. Examples:
- "I pulled my back" → (user, has_injury, back, 0.4)
- "Visiting chiropractor" → (user, currently_at, chiropractor, 0.4)
- "In bed resting" → (user, is_resting, bed, 0.4)
- "Thinking about a career change" → (user, considering, career_change, 0.4)

Novel rel_types go to Class C (30-day TTL). Re-embedder evaluates: approve, map to existing, or reject.

- Use snake_case. Other types allowed if none fit.
```

**Why:** Makes novel extraction explicit and encouraged, not apologetic.

### 3. Modify `openwebui/faultline_tool.py` — Add RAG Fallback

**Location:** Find the inlet filter's message processing loop (around line 350–400, in the `inlet` function)

**Current pattern:**
```python
# Extract facts
edges = await rewrite_to_triples(text, model=model, url=url, ...)
if edges:
    # Fire-and-forget ingest
    await httpx.post(f"{FAULTLINE_API_URL}/ingest", json={
        "text": text,
        "user_id": user_id,
        "edges": edges,
        "source": "filter"
    })
```

**Add fallback after the `if edges:` block:**
```python
# Extract facts
edges = await rewrite_to_triples(text, model=model, url=url, ...)
if edges:
    # Fire-and-forget ingest
    await httpx.post(f"{FAULTLINE_API_URL}/ingest", json={
        "text": text,
        "user_id": user_id,
        "edges": edges,
        "source": "filter"
    })
elif len(text.strip()) > 10:
    # RAG fallback: extraction failed, but message is substantial
    # Store raw text to Qdrant as semantic context (fact_class=C, confidence=0.4)
    try:
        await httpx.post(f"{FAULTLINE_API_URL}/store_context", json={
            "text": text,
            "user_id": user_id
        }, timeout=5)
    except Exception as e:
        # Log but don't fail — /query still works without Qdrant
        pass
```

**Why:** Ensures no meaningful information is lost. Short messages (< 10 chars) are OK to drop (noise).

### 4. Update Tests

**File:** `tests/filter/test_relevance.py`

**Add test cases:**

1. **Novel rel_type extraction** (health facts example):
```python
def test_extraction_novel_health_facts():
    """Verify LLM attempts novel health-related rel_types"""
    text = "I pulled my back over the weekend, visited the chiropractor"
    edges = extract_triples(text)  # Mock LLM call
    
    # Expected: novel rel_types extracted with low confidence
    assert any(e.get("rel_type") in ("has_injury", "health_status", "visited") for e in edges)
    assert all(e.get("confidence", 0.6) <= 0.4 for e in edges if "novel" in str(e))
    assert len(edges) > 0, "Should extract health facts, not return empty"
```

2. **RAG fallback triggers**:
```python
def test_rag_fallback_on_empty_extraction():
    """Verify /store_context called when extraction returns []"""
    with patch("httpx.post") as mock_post:
        # Simulate extraction returning empty
        with patch("rewrite_to_triples", return_value=[]):
            result = process_inlet_message(
                text="Some health observation that extraction fails on",
                user_id="user-123"
            )
        
        # Expected: /store_context called with raw text
        calls = mock_post.call_args_list
        store_context_calls = [c for c in calls if "/store_context" in str(c)]
        assert len(store_context_calls) > 0, "/store_context should be called on empty extraction"
```

3. **No fallback for short messages**:
```python
def test_no_fallback_for_short_messages():
    """Verify short noise is not stored"""
    with patch("httpx.post") as mock_post:
        with patch("rewrite_to_triples", return_value=[]):
            process_inlet_message(text="ok", user_id="user-123")
        
        # Expected: /store_context NOT called
        calls = mock_post.call_args_list
        store_context_calls = [c for c in calls if "/store_context" in str(c)]
        assert len(store_context_calls) == 0, "Short noise should not trigger fallback"
```

**Run tests:**
```bash
pytest tests/filter/test_relevance.py -v
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction --ignore=tests/model_inference --ignore=tests/preprocessing
```

**Expected:** All tests pass (existing + new).

### 5. Manual Pre-Prod Validation (If Time)

If FaultLine is running on pre-prod:

```bash
# Test novel health fact extraction
curl -X POST http://192.168.40.10:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I pulled my back visiting the chiropractor",
    "user_id": "test-user",
    "edges": [
      {"subject": "user", "rel_type": "has_injury", "object": "back", "confidence": 0.4},
      {"subject": "user", "rel_type": "currently_at", "object": "chiropractor", "confidence": 0.4}
    ],
    "source": "test"
  }'

# Verify facts stored in staged_facts (Class C)
ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c \
  \"SELECT subject_id, rel_type, object_id, fact_class, confidence FROM staged_facts WHERE rel_type IN ('has_injury', 'currently_at');\""

# Expected: 2 rows, both Class C, confidence 0.4
```

### 6. Update `scratch.md`

Add entry under "## Current State":

```markdown
## #deepseek NEXT TASK: dprompt-69 (Open-Ended Extraction + RAG Fallback)

**Status:** In progress

**What deepseek is doing:**
- Loosening extraction prompt to encourage novel rel_types (health, ephemeral, transient)
- Adding /store_context RAG fallback when extraction returns empty
- Adding tests for novel extraction + fallback behavior

**Expected outcome:**
- No more silent failures
- Health facts now extracted as Class C with confidence 0.4
- Ephemeral events (currently at X, visiting Y) captured
- Worst case (extraction fails): RAG stores semantic context
- Best case (extraction succeeds): Facts staged for re-embedder evaluation

**Files changed:**
- openwebui/faultline_tool.py (extraction prompt + RAG fallback)
- tests/filter/test_relevance.py (new tests)

**When done:** deepseek updates scratch.md with completion status, then STOP.
```

### 7. STOP & Report

Do NOT commit. Do NOT push. Code complete, tests pass.

**Update `scratch.md` with:**

```markdown
## ✓ DONE: dprompt-69 (Open-Ended Extraction + RAG Fallback) — 2026-05-15

**Task:** Eliminate silent failures by loosening extraction prompt and adding RAG fallback.

**Changes:**
- openwebui/faultline_tool.py: Extraction prompt now explicitly encourages novel rel_types (has_injury, health_status, currently_at, etc.)
- openwebui/faultline_tool.py: Added /store_context RAG fallback when extraction returns empty
- tests/filter/test_relevance.py: Added tests for novel rel_type extraction, RAG fallback behavior, short message handling

**Tests:** 
- New tests: 3 (novel health extraction, fallback trigger, fallback silence on short messages)
- Existing: 114+ passed, 0 regressions ✓
- Total: 117+ passed

**Validation:**
- Novel rel_types (has_injury, health_status, etc.) now extracted with confidence 0.4 ✓
- Ephemeral location/activity facts extracted ✓
- RAG fallback stores unextracted text to Qdrant when substantial ✓
- Short noise (< 10 chars) silently dropped as before ✓
- No silent failures — everything captured somewhere ✓

**Next:** User reviews code, approves, then decides on deployment.

**Status:** AWAITING USER REVIEW
```

Then **STOP immediately**. Do not commit or push. Await user approval.

---

## Success Criteria (All Required)

✅ Extraction prompt updated (novel rel_type examples + encouragement)  
✅ RAG fallback implemented (if extraction fails, /store_context called)  
✅ Tests added (novel extraction, fallback trigger, edge cases)  
✅ All tests pass (new + existing, no regressions)  
✅ No git commits made (dev-only work)  
✅ Summary in scratch.md  

## Do NOT

- Commit to git (any branch)
- Modify WGM gate, validation, or re-embedder
- Change /ingest or /query endpoint logic
- Break existing extraction behavior (only extend it)
- Change rel_type metadata or validation rules

## Critical Rules

**NO COMMITS.** Code complete, tests pass, user decides.

**TWO FILES ONLY.** faultline_tool.py + test_relevance.py.

**TESTS PASS.** No regressions.

**STOP CLAUSE MANDATORY.** Report completion, await user review.

---

**Template version:** 1.0  
**Philosophy:** Extraction open-ended, pipeline gates it. Trust + verification.  
**Status:** Ready for execution by deepseek
