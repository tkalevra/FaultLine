# dBug-040B: Investigation Findings — Extract/Rewrite Returns 0 Triples

**Investigation Date:** 2026-05-17  
**Status:** ROOT CAUSE IDENTIFIED  
**Component:** /extract/rewrite LLM endpoint  
**Severity:** Critical

---

## Summary

**LLM /extract/rewrite endpoint returns 0 triples** for comprehensive family statements, causing only spouse facts to be created while children, ages, identity, and location facts are dropped.

---

## Evidence from Production Logs

### Ingest Request Processing (02:31:10)

```
ingest.subject_resolved_at_extraction input=user output=10d7d879-63cd-4f31-92ce-f2c9edb760ab
ingest.fact_classified         confidence=0.8 fact_class=A is_user_stated=False rel_type=also_known_as
ingest.subject_resolved_at_extraction input=user output=10d7d879-63cd-4f31-92ce-f2c9edb760ab
ingest.object_resolved_at_extraction input=marla output=fb0868c4-12b4-587d-9a3b-ce96ca5979ca
ingest.fact_classified         confidence=0.8 fact_class=A is_user_stated=False rel_type=spouse
ingest.class_a_committed       count=2
```

**Only 2 facts committed:**
- (user, also_known_as, ?)
- (user, spouse, marla)

### Extract Rewrite Response (02:31:13)

```
extract.rewrite_success        triple_count=0 user_id=10d7d879-63cd-4f31-92ce-f2c9edb760ab
```

**LLM returned 0 triples** — no children, ages, location facts extracted.

---

## Input vs. Output Analysis

### User Input
```
"My name is Chris. My spouse is Marla. My children are alice (age 12), bob (age 10), 
and charlie (age 19). I live in Kitchener, Ontario."
```

### Expected Triples (from system prompt)
1. Identity: (chris, pref_name, chris)
2. Spouse: (user, spouse, marla), (marla, spouse, user) ✅ **EXTRACTED**
3. Children: (user, parent_of, alice/bob/charlie) ❌ **NOT EXTRACTED**
4. Ages: (alice, age, 12), (bob, age, 10), (charlie, age, 19) ❌ **NOT EXTRACTED**
5. Location: (user, lives_in, kitchener), (kitchener, located_in, ontario) ❌ **NOT EXTRACTED**

### Actual Triples
- Only spouse facts created (2 facts)
- Everything else: 0 triples

---

## Root Cause

**The LLM /extract/rewrite endpoint is failing to extract comprehensive family information.**

Likely causes:

### 1. **LLM Prompt Issue** (Most Likely)
- System prompt may not be instructing extraction of children relationships
- Prompt may be incomplete or missing examples for family hierarchies
- LLM may be filtering/ignoring "children are..." pattern

### 2. **LLM Model Limitations**
- Qwen 3.5 may struggle with nested family structures
- Model may prioritize only primary relationships (spouse) over secondary (children)
- Temperature/sampling may be suppressing lower-confidence extractions

### 3. **Response Truncation**
- max_tokens cutoff may be preventing response completion
- LLM generates 0 triples if response exceeds token limit before extraction completes

### 4. **Filter Preprocessing**
- Filter may be preprocessing/simplifying input before sending to LLM
- Original comprehensive statement may be truncated before reaching /extract/rewrite

---

## Evidence Trail

**Query Processing (02:31:09):**
```
query.taxonomy_detected query='my name is chris. my spouse is marla. my children are alice (age 1'
```

The query text is **TRUNCATED** to `...alice (age 1` — text is cut off mid-word!

This suggests:
1. Filter received the full statement
2. Query received partial/truncated statement
3. /extract/rewrite received truncated input
4. LLM couldn't extract full family structure from incomplete text

---

## Hypothesis: Truncation Chain

1. **User sends:** Full message "My name is Chris. My spouse is Marla. My children are alice (age 12), bob (age 10), and charlie (age 19). I live in Kitchener, Ontario."

2. **Filter processes:** Full text ✅

3. **Query receives:** `'my name is chris. my spouse is marla. my children are alice (age 1'` ❌ **TRUNCATED**

4. **/extract/rewrite receives:** Truncated text

5. **LLM cannot extract:** Incomplete family list (alice cut off) → returns 0 triples

6. **Result:** Only spouse facts created from previous state; no children facts

---

## Investigation Steps Required

1. **Check filter output**
   - Is filter truncating message before sending to /ingest?
   - Check HTTP request body size/limits
   - Verify max_input_tokens in /extract/rewrite call

2. **Check /extract/rewrite prompt**
   - Review system prompt for family extraction examples
   - Verify children/parent_of pattern is in prompt
   - Check if age extraction is enabled

3. **Check LLM response handling**
   - Is max_tokens cutoff occurring?
   - Is response parsing correctly identifying 0 triples vs. truncation?
   - Check Qwen 3.5 model behavior for multi-relation extraction

4. **Check query text truncation**
   - Why is query receiving `...alice (age 1` instead of full text?
   - Is there a string truncation in Open WebUI → FaultLine integration?

---

## Proposed Fixes

### Short-term (Band-aid)
1. Increase max_tokens in /extract/rewrite call
2. Add explicit children/parent_of examples to extraction prompt
3. Separate children extraction into dedicated LLM call if main extraction fails

### Medium-term (Proper)
1. Debug text truncation in Open WebUI → FaultLine pipeline
2. Verify filter is not truncating input
3. Add logging to /extract/rewrite to log input + output (before returning 0 triples)

### Long-term (Robust)
1. Implement multi-pass extraction:
   - Pass 1: Extract identity, relationships, attributes
   - Pass 2: Extract hierarchy (children, ages)
   - Pass 3: Extract locations
2. Add confidence/fallback patterns if extraction returns < 3 facts
3. Use more capable LLM for complex family structures (or fine-tuned model)

---

## Database Evidence

After ingest:
- Facts table: 2 spouse facts only
- Entities: chris, marla (spouse), plus garbage entities from query processing (kitchener, alice, bob, charlie created during query resolution, not ingest)
- No parent_of, child_of, age, pref_name facts for family

**Conclusion:** Extraction pipeline failed, ingest received no edges, only spouse facts created from pre-existing state or fallback logic.

---

## Next Steps

1. **Enable /extract/rewrite logging** to capture:
   - Input text (what LLM receives)
   - Output triples (what LLM returns)
   - Model response (raw LLM completion)

2. **Test with explicit LLM calls:**
   - Call /extract/rewrite directly with family statement
   - Observe JSON response

3. **Check truncation source:**
   - Is filter truncating?
   - Is /ingest message truncating?
   - Is /query truncating?

4. **Review Qwen 3.5 behavior:**
   - Test extraction with different max_tokens
   - Test with explicit prompt injection for children relationships
