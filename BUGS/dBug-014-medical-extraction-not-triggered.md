# dBug-014: Medical Context Not Extracted or Ingested (Filter Ingest Gate Failure)

## alicecription

User provialice concrete medical feedback ("I hurt my back") in OpenWebUI conversation. Expected behavior: Filter extracts medical facts and calls `/ingest` to store them. Actual behavior: Filter calls `/query` (retrieval) but skips `/ingest` entirely. Medical context lost silently.

**Related to dBug-013 but different root cause:** dBug-013 was about temporal routing (born_on). dBug-014 is about extraction/ingest gating not triggering at all.

## Reproduction

**Setup:**
```bash
# Pre-prod running v1.0.7 + dprompt-72b/73b fixes
ssh docker-host -x "curl -s http://192.168.1.10:8001/health | jq ."
# Returns: ok
```

**Step 1: User provialice medical feedback in OpenWebUI**

User says: "what did I do to my back?" (6 words, should trigger ingest)

**Expected:**
- Filter calls `/ingest` with medical edges (has_injury, has_symptom, affected_body_part, etc.)
- Facts table updated with medical context
- `/query` returns medical facts for context injection

**Actual:**
- Filter calls `/query` (retrieves existing facts)
- NO `/ingest` call in logs
- Medical facts NOT stored in facts or staged_facts tables
- Response inclualice only existing family/identity facts, not medical context

## Evidence

**Pre-prod database (UUID: 3f8e6836-72e3-43d4-bbc5-71fc8668b070):**

Facts stored (existing):
```
rel_type      | count
--------------|-------
also_known_as | 3
child_of      | 2
has_pet       | 1
instance_of   | 6
parent_of     | 3
pref_name     | 4
sibling_of    | 3
spouse        | 1
```

Staged facts (no medical rel_types):
```
rel_type   | fact_class | count
-----------|-----------|-------
dislikes   | B          | 1 (meaningless: "dislikes back")
likes      | B          | 2
part_of    | B          | 3
lived_at   | B          | 3
works_for  | B          | 2
member_of  | C          | 3
has_pet    | B          | 5
is_a       | C          | 1
```

**No medical rel_types:** has_injury, has_medical_condition, has_symptom, affected_body_part, etc.

**OpenWebUI logs (user message):**
```
[FaultLine Filter] user_id=[redacted] text='what did I do to my back?'
[FaultLine Filter] calling /query url=http://192.168.1.10:8001/query
[FaultLine Filter] /query status=200
[FaultLine Filter] filtered: 47/47 facts
[FaultLine Filter]   fact: user -pref_name-> chris
[FaultLine Filter]   fact: user -also_known_as-> chris
...
```

**Missing:** `/ingest` call completely absent from logs for this conversation.

## Impact

**Severity: CRITICAL (data loss path)**

- **Knowledge graph incomplete:** Real user context (medical, health) not persisted
- **Non-recoverable:** No fallback; medical context silently discarded
- **User experience:** System ignores health feedback, generates generic advice
- **Regression risk:** Affects all users with health/medical context
- **Scope:** All ingest messages that should trigger extraction gate but don't

## Root Cause Analysis (Hypotheses)

Per CLAUDE.md "Ingest gate" section:

> Before calling the LLM for fact extraction:
> 1. Word count ≥ 3, OR message matches a self-identification pattern
> 
> If neither condition is met, `will_ingest = False`.

**Hypothesis 1 (MOST LIKELY): Ingest gate condition not met**
- User message: "what did I do to my back?" (6 words)
- Should trigger: word_count >= 3 ✓
- But Filter might have a bug: filtering out questions vs statements
- Or: message preprocessing strips words before counting
- Check: `openwebui/faultline_tool.py` — lines where `will_ingest` is set

**Hypothesis 2: Message routing in Filter**
- Filter might bypass ingest for certain message patterns (questions vs statements)
- "What did I do" is interrogative, not declarative
- Check: Filter LLM prompt or keyword-based routing logic

**Hypothesis 3: WGM gate rejecting silently**
- Medical edges (has_injury, etc.) might be unknown rel_types hitting unknown logic
- But: word count should trigger attempt; rejection would be logged
- Check: FaultLine API logs for ingest attempts (may be happening and failing)

**Hypothesis 4: Filter LLM not extracting edges**
- Filter has LLM that extracts edges from text (per CLAUDE.md pipeline)
- LLM might not be trained on health/medical extraction patterns
- Check: Filter's `_TRIPLE_SYSTEM_PROMPT` or extraction LLM config

## Proposed Fix (TBD)

Deepseek's investigation must determine:

1. **Is ingest gate condition being checked?** (word count >= 3)
   - Trace Filter code: `openwebui/faultline_tool.py` lines for `will_ingest` assignment
   - Add logging if missing

2. **Is ingest being called but failing?**
   - Check FaultLine API logs for recent /ingest calls
   - Check for 400/500 errors on /ingest endpoint

3. **Is Filter LLM extracting medical edges?**
   - Check `openwebui/faultline_tool.py` extraction prompt
   - Verify health/medical rel_types are in training vocabulary
   - May need prompt update (per dprompt-69: "Open-ended extraction + RAG fallback")

4. **Is there a fallback mechanism?**
   - Per dprompt-69: `/store_context` fallback should embed text to Qdrant
   - Check if this is being called when ingest skipped
   - Verify Qdrant has medical context (even if facts table doesn't)

## Validation Plan

After fix:
1. Reproduce user scenario: "I hurt my back"
2. Verify `/ingest` called in OpenWebUI logs
3. Verify facts/staged_facts have medical rel_types (has_injury, affected_body_part, etc.)
4. Verify `/query` returns medical facts
5. Verify Qdrant contains medical context (fallback validation)
6. Non-regression: existing family facts still retrieved

## Related Issues

- **dBug-013** — Temporal routing (born_on). FIXED by dprompt-72b/73b.
- **dBug-014** — Medical extraction/ingest gating (THIS ISSUE).
- **dprompt-69** — Open-ended extraction + RAG fallback (already deployed).

---

## Investigation Scope for Deepseek

**Investigation boundaries:**
- `openwebui/faultline_tool.py` — Filter ingest gating logic
- `openwebui/faultline_tool.py` — LLM extraction prompt + vocab
- FaultLine API logs — `/ingest` call history
- `src/api/main.py` — Ingest gate implementation (if called)
- Qdrant collections — fallback context storage

**Pre-prod testing:**
- OpenWebUI logs: check for `/ingest` calls + responses
- FaultLine API logs: check for ingest errors
- Database queries: verify facts/staged_facts empty for medical rel_types
- Test `/ingest` directly via curl with medical edges

