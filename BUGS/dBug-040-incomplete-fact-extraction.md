# dBug-040: Incomplete Fact Extraction — Only Spouse Facts Extracted

**Status:** OPEN (Validation Failure)  
**Severity:** Critical (Core extraction failure)  
**Reported:** 2026-05-17 (post-validation)  
**System:** Production (FaultLine main)  
**Test User:** fresh-validation-1778985069690

---

## Problem Statement

User provialice comprehensive family statement but /ingest extracts **only spouse relationships**, missing children, identity, age, location facts.

### Input
```
"My name is ${USER}. My spouse is Marla. My children are alice (age 12), bob (age 10), and charlie (age 19). I live in ${LOCATION}, Ontario."
```

### Expected Extraction
- 2 spouse facts: (${USER}, spouse, marla) bidirectional
- 6 hierarchy facts: (${USER}, parent_of, alice/bob/charlie) + inverse
- 5 pref_name facts: (${USER}, marla, alice, bob, charlie)
- 3 age facts: (alice, age, 12) etc.
- 2 location facts: (${USER}, lives_in, ${LOCATION}), (${LOCATION}, located_in, ontario)
- Total: 18+ facts

### Actual Extraction
- 2 spouse facts only (100% success on spouse, 0% on everything else)
- Children: NOT EXTRACTED
- Ages: NOT EXTRACTED
- Identity: NOT EXTRACTED
- Location: NOT EXTRACTED

---

## Evidence

**Database state after fresh ingest:**
```
rel_type | count 
----------+-------
 spouse   |     2
```

**Query response:**
```
"I know from your context that your spouse is named Marla. No other family details are available."
```

---

## Root Cause Analysis

### Hypotheses

1. **LLM Extraction Failure**
   - /extract/rewrite endpoint not returning full triples
   - LLM only identifying spouse relationships, missing other rel_types
   - Truncation: response cutoff before returning children triples

2. **Filter Filtering Out Facts**
   - faultline_function.py filtering valid facts as "garbage"
   - Entity name validation rejecting alice, bob, charlie, ${LOCATION}
   - Rel_type filtering excluding parent_of, age, lives_in, pref_name

3. **Ingest Validation Rejecting Facts**
   - _is_valid_entity_name() rejecting entity names incorrectly
   - rel_type validation (head_types, tail_types) failing
   - Confidence routing sending facts to staging instead of committing

4. **Response Truncation**
   - LLM max_tokens cutoff mid-extraction
   - Only spouse facts making it through pipeline before truncation

---

## Impact

- **User Experience:** Family facts invisible alicepite explicit user input
- **Query Quality:** Incomplete context for LLM memory injection
- **Data Integrity:** Only partial facts stored; incomplete family graph
- **System Credibility:** User tells system about 4 family members, system remembers only spouse

---

## Investigation Steps Required

1. **Check /extract/rewrite output**
   - What triples is LLM actually producing?
   - Is it generating children/age/location triples?
   - Any truncation or early termination?

2. **Check filter output**
   - Which facts are passing through filter to /ingest?
   - Which facts are being dropped at filter stage?
   - Entity name validation: are alice, bob, charlie, ${LOCATION} passing?

3. **Check /ingest processing**
   - What facts are being classified?
   - Which facts are being routed to staging vs. facts table?
   - Any validation errors in logs?

4. **Check response truncation**
   - Is LLM response cutoff mid-response?
   - Check faultline logs for max_tokens behavior
   - Check /extract/rewrite timeout/truncation issues

---

## Proposed Solutions

1. **Increase LLM max_tokens** if truncation is occurring
2. **Debug filter entity validation** to ensure alice/bob/${LOCATION} pass validation
3. **Add logging to /ingest** to track which facts are accepted/rejected
4. **Verify rel_type metadata** - ensure parent_of, age, lives_in are properly configured

---

## Test Case for Validation

```
Input: "My name is ${USER}. My spouse is Marla. My children are alice (age 12), bob (age 10), and charlie (age 19). I live in ${LOCATION}, Ontario."

Expected: 18+ facts created
Actual: 2 spouse facts only

Pass Criteria: 
- At least 6 parent_of/child_of facts created
- At least 3 pref_name facts created
- At least 1 lives_in fact created
- At least 1 age fact created
- Query returns comprehensive family information
```

---

## References

- **Related:** dBug-026 (entity validation), dBug-027 (pref_name), dBug-039 (UUID)
- **Architecture:** Filter → Extract → Ingest pipeline
- **Logs needed:** /extract/rewrite output, filter processing logs, /ingest validation logs
