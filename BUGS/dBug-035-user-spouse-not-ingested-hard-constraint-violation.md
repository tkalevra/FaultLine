# dBug-035: User Self-Reported Spouse Not Ingested — HARD CONSTRAINT Violation

**Status:** OPEN — CRITICAL

**Severity:** CRITICAL (violates HARD CONSTRAINT: "User is the ultimate source of truth")

**Reporter:** John (2026-05-16 15:55 UTC)

**Observed Behavior:**
```
User Input 1: "My wifes name is Marla, she prefers to be called emma"

System Response: "I cannot add 'Marla' or 'emma' to your family records as the system 
only contains facts for alice (16), bob (12), and charlie (19). There is no existing 
record of a spouse named Marla or an alias 'emma' in the current dataset."

User Input 2 (same query): "tell me about my family?"

System Response: "Based on the data available, your family consists of three people... 
charlie (Age: 19), bob (Age: 12), alice (Age: 16)"

[No spouse Marla/emma returned]
```

**Expected Behavior:**
1. User makes self-reported claim: "My wife's name is Marla, she prefers to be called emma"
2. Inlet filter detects this as ingestible content (word count ≥ 3)
3. LLM extracts: `(user, spouse, marla)` + `(marla, pref_name, emma)`
4. POST /ingest processes both edges (Class A, confidence=1.0 for user-stated)
5. /query returns Marla/emma in next query
6. System acknowledges: "Your wife Marla (who goes by emma) is recorded"

**What Actually Happened:**
- Ingest pipeline either failed to extract the spouse relationship OR rejected it
- User's self-reported fact was not stored
- System told user "you are wrong about your own family" (opposite of HARD CONSTRAINT)
- Next query still shows only 3 family members (alice, bob, charlie)

---

## Root Cause Analysis

### Investigation Points

**1. Ingest Gating (faultline_tool.py:1447-1451)**
```python
will_ingest = self.valves.INGEST_ENABLED and (
    len(text.split()) >= 3           # ← 12 words: PASS ✓
    or bool(_IDENTITY_RE.search(text))  # ← "my wife's name is" ≠ "my name is": FAIL
    or _has_third_person_pref        # ← "she prefers to be called" ≠ exact signals: FAIL
)
```

**Word count check passes:** "My wifes name is Marla, she prefers to be called emma" = 12 words ≥ 3

**Expected result:** will_ingest = True → /extract endpoint should be called

**Problem Statement:** If will_ingest=True but spouse was not ingested, failure occurred in one of:
1. **LLM extraction failed** to emit `(user, spouse, marla)` edge
2. **LLM extraction succeeded but /ingest rejected it** during validation (WGMValidationGate, conflict detection, etc.)
3. **Query endpoint not returning the ingested fact** (filtering, deduplication, or temporal filtering error)

---

### Known System Behavior (dprompt-91)

Recent dprompt-91 testing shows archive filtering correctly handles:
- **Scope filtering:** rel_type checked against taxonomy's rel_types_defining_group
- **Spouse rel_type:** Belongs to `family` and `household` taxonomies
- **Identity/scalar exemption:** spouse should NOT be filtered by scope since it's a relationship type

**Hypothesis:** Spouse fact WAS ingested but is being filtered out by dprompt-91 scope filtering.

**Test:** Check if spouse is in PostgreSQL `facts` table but being filtered by `determine_scope_multi_factor()`.

---

## HARD CONSTRAINT Violation

**CLAUDE.md Key Principle:**
> User is the ultimate source of truth. When a user makes a self-reported claim about their reality, the system must ingest it as authoritative fact, not reject it based on database state.

**Violation Details:**
- User stated: "My wife is Marla"
- System response: "I cannot add this because there's no record"
- **This is bac${LOCATION}ards.** The user IS the source of truth. The absence of a database record is irrelevant.

**Impact:** This violates the entire value proposition of FaultLine. If the system rejects user self-reported facts, the knowledge graph becomes unreliable (controlled by database state, not user reality).

---

## Test Case for Reproduction

```bash
# Setup: User has children alice (16), bob (12), charlie (19)

# Test 1: State spouse relationship
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer sk-1cf72f713e884a06b3dab80a8a003669" \
  -d '{
    "model": "qwen/qwen3.5-9b",
    "messages": [
      {"role": "user", "content": "My wifes name is Marla, she prefers to be called emma"}
    ],
    "stream": false
  }'

# Expected: System acknowledges ingestion
# Actual: System rejects ("I cannot add...")

# Test 2: Query family facts
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer sk-1cf72f713e884a06b3dab80a8a003669" \
  -d '{
    "model": "qwen/qwen3.5-9b",
    "messages": [
      {"role": "user", "content": "tell me about my family?"}
    ],
    "stream": false
  }'

# Expected: Spouse Marla/emma returned in facts
# Actual: Only alice, bob, charlie returned
```

---

## Diagnostic Logs Required

**From FaultLine startup:**
```
startup.taxonomy_cache_loaded count=6
```
✓ Confirmed: 6 taxonomies loaded (family, household, work, location, computer_system, body_parts)

**From recent /query on "tell me about my family":**
```
query.temporality_detection confidence=0.0 is_historical=False
determine_scope.multi_factor detected_taxonomies=['body_parts', 'family', 'household', 'computer_system', 'location']
archive_filter.applied fact_count_before=40 fact_count_after=18 is_historical=False
```

**Missing from logs:** No `/ingest` or `/extract` logging visible for the spouse statement

**This suggests:** Either:
1. /extract was never called (ingest gating failed)
2. /extract was called but LLM didn't extract spouse edge
3. /ingest was called but facts were superseded/rejected upstream

---

## Next Steps

1. **Check PostgreSQL `facts` table** for user_id with rel_type=spouse
   ```sql
   SELECT id, subject_id, rel_type, object_id, confidence, created_at 
   FROM facts 
   WHERE user_id='${TEST_USER_ID}' 
   AND rel_type='spouse'
   ORDER BY created_at DESC;
   ```
   - If found: Problem is in /query filtering (dprompt-91 scope or other)
   - If not found: Problem is in /ingest pipeline (extraction or validation)

2. **Check FaultLine logs** for /extract and /ingest calls with spouse relation

3. **Test LLM extraction directly** with the user's statement to verify it emits spouse edge

4. **Review dprompt-91 scope filtering** to verify spouse rel_type is NOT being filtered out for family taxonomy

---

## Files to Review

- `openwebui/faultline_tool.py` — ingest gating + extraction pipeline
- `src/api/main.py` — /ingest, /extract endpoints + dprompt-91 scope filtering
- `src/wgm/gate.py` — WGMValidationGate (may reject spouse for validation reasons)
- `src/fact_store/store.py` — FactStoreManager.commit() (may supersede spouse)

---

## HARD CONSTRAINT Reference

**From CLAUDE.md Key Principles:**

> **User is the ultimate source of truth.** When a user makes a self-reported claim about their reality, the system must ingest it, not reject it based on database state. If a user says "My wife is Marla," that IS a fact, regardless of prior database contents. Corrections supersede old facts; self-identification is never wrong.

This bug violates that principle directly.
