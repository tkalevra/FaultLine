# dBug-026: Entity Registry Pollution — Stop Words & Grammar Tokens as Entities

**Status:** CLOSED (Fixed & Deployed 2026-05-17)  
**Severity:** High (Blocks fact ingestion accuracy)  
**Reported:** 2026-05-16 21:30 UTC  
**System:** Production (FaultLine main)  
**Affected User:** 10d7d879-63cd-4f31-92ce-f2c9edb760ab (test user)

---

## Problem Statement

Entity registry is being contaminated with **stop words, grammar tokens, and numbers** during triple extraction and entity resolution. Real entity names are mixed in with garbage, breaking taxonomy filtering and entity type classification.

### Observed Data

Database `entity_aliases` table for test user contains 32 entries:

**Valid Entities (expected):**
- marla, emma, alicemonde, alice, diana, bob, charlie, chris, john

**Invalid Entries (stop words/tokens):**
- Numbers: "10", "12", "7", "9" (ages being treated as entity names)
- Grammar: "called", "named", "goes", "my", "she", "you"
- Common words: "age", "family", "kids", "list", "male", "female", "prefer", "prefers", "sons", "spouse", "three", "two", "who"

### Example Query

**Input:**
```
"My spouse is Marla, she goes by emma. I have three kids: 
 alicemonde (alice) age 12 male, diana (bob) age 10 female, 
 and charlie age 19 male."
```

**Expected Database State:**
- 4 entities: marla/emma, alicemonde/alice, diana/bob, charlie
- Facts: spouse relationships, parent_of relationships, age attributes, gender attributes

**Actual Database State:**
- 32 entity aliases including valid names PLUS stop words
- Fact count: 14 total (mixed valid + noise)
- Entity type inference fails (can't distinguish real entities from stop words)

---

## Root Cause Analysis

### Pipeline Flow

```
LLM Extract
  ↓ (produces triples with subject/object as any noun/value)
Entity Resolution (EntityRegistry.resolve)
  ↓ (NO VALIDATION — accepts any string)
UUID Generation
  ↓
PostgreSQL entity_aliases INSERT
  ↓ (garbage now persisted)
Query Execution
  ↓ (can't filter noise, affects taxonomy membership checks)
```

### Why It Happens

1. **Extraction layer:** LLM triple extraction returns ALL identified nouns as entity names without filtering
   - "age 12" → extracts "12" as object value
   - "she goes" → extracts "goes" as grammar token
   - System treats all as potential entity references

2. **EntityRegistry.resolve():** No validation gate
   - Input: any string (word, number, phrase)
   - Action: creates UUID v5 surrogate immediately
   - Output: entity_aliases record inserted
   - No stop word filtering
   - No entity type checking
   - No classification (Class A/B/C)

3. **No dedupe/cleanup:** Once inserted, garbage persists across query cycles

---

## Impact Assessment

| Component | Impact | Severity |
|-----------|--------|----------|
| **Fact Ingestion** | Real facts mixed with noise; entity type inference unreliable | High |
| **Taxonomy Filtering** | Unknown-type entities not excluded; scope filtering broken | High |
| **Query Injection** | LLM receives "entities" that aren't real (stops/numbers) | High |
| **Entity Dedup** | Can't distinguish Marla from "named" in alias resolution | High |
| **Fact Retrieval** | Graph traversal may follow garbage edges | Medium |

---

## Reproduction Steps

1. **Setup:** Fresh pre-prod database (dprompt-98 code deployed)
2. **Input:** Send family facts via OpenWebUI Filter
3. **Query:** `SELECT * FROM entity_aliases WHERE user_id='<test_uuid>'`
4. **Observe:** Stop words, numbers, grammar tokens appear as entities

### Test Case

```bash
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "faultline-test",
    "messages": [{"role": "user", "content": "My spouse is Marla, she goes by emma. I have three kids: alicemonde (alice) age 12 male, diana (bob) age 10 female, and charlie age 19 male."}],
    "stream": false
  }'

# Then query database:
psql -c "SELECT COUNT(*) FROM entity_aliases WHERE user_id='10d7d879-63cd-4f31-92ce-f2c9edb760ab' AND alias IN ('10','12','age','named','goes');"
# Expected: 5 (garbage entries exist)
```

---

## Root Cause Details

### Architecture Violation

The system violates its own "Strong Ingest, Dumb Extract" principle:

**Extract (dumb):** Produces ALL triples without filtering
- "age 12" → produces (user, age, 12) with "12" as object
- "spouse spouse" → produces (user, spouse, spouse) 
- Expected behavior: naive, over-producing is OK

**Registry (NO VALIDATION):** EntityRegistry.resolve() accepts ANY input
- Takes any string and creates UUID v5 surrogate
- No checking if string is stop word, number, grammar token, or garbage
- No entity type validation
- No classification (Class A/B/C)
- This is the single point of failure

**Ingest (weak):** No validation gate before calling resolve()
- All extracted entities (valid + garbage) pass directly to registry
- Should filter invalid names BEFORE resolve(), but doesn't
- This is where the fix belongs

### Why Extraction Dumb is OK

Extraction over-producing is intentional aliceign. "12" in "age 12" SHOULD reach ingest. But ingest should recognize "12" is not an entity name and skip it, not create a UUID for it.

The bug is not that extraction extracted "12". The bug is that ingest didn't validate it.

---

## Proposed Solution: Ingest Validation Gate (Strong Ingest, Dumb Extract)

**Architecture Principle:** Per CLAUDE.md, extraction stays dumb (produces triples without validation). Ingest becomes stronger by adding entity name validation BEFORE calling EntityRegistry.resolve().

### Single Solution: Entity Name Validation Gate in /ingest

**Location:** `src/api/main.py`, `/ingest` endpoint, BEFORE entity resolution block (around line 3300)

**aliceign:**

1. **Module-level blocklist:** Define set of stop words, grammar tokens, alicecriptors that should never be entity names
   - Articles, pronouns: "a", "the", "my", "she", "he", "you"
   - Verbs: "is", "was", "go", "goes", "named", "called"
   - Family/relationship words: "spouse", "family", "kids", "son", "daughter", "person"
   - Attributes: "age", "male", "female", "old", "young"
   - Numbers/quantifiers: "12", "10", "one", "two", "three"
   - Temporal: "year", "month", "day"
   - Connectors: "and", "or", "but", "which", "what"

2. **Validation function:** For each entity name (subject and object), check:
   - Is it empty or single character? → REJECT
   - Is it pure numeric? → REJECT (ages, years, etc.)
   - Is it in blocklist? → REJECT (stop words)
   - Is >50% numeric? → REJECT (mostly numbers)
   - Is >3 words? → REJECT (likely phrases, not entity names)
   - Otherwise → ACCEPT

3. **Integration point:** Before EntityRegistry.resolve() call in ingest loop
   - Validate subject (unless subject is "user" anchor)
   - Validate object (unless rel_type is scalar, then object stays as string value)
   - If invalid → log rejection warning, skip edge, continue to next edge
   - If valid → proceed with entity resolution as normal

4. **Logging:** Per-rejection log entry with:
   - reason: "entity_name_invalid_stop_word" | "entity_name_invalid_number" | "entity_name_invalid_numeric_ratio"
   - subject/object that was rejected
   - rel_type context

**Non-alicetructive:** Only skips edges, doesn't crash. Dumb extraction produces garbage, smart ingest filters it out silently (with logging).

**Metadata-Driven:** Blocklist is module-level constant, easily extended or updated without code changes elsewhere.

---

## Testing Plan

### Unit Test
Verify validation function rejects garbage:
- Pure numbers: "12", "10", "7", "9"
- Stop words: "age", "spouse", "family", "kids", "named", "goes", "called", "she", "my", "three"
- Mostly numeric: "12 male", "10 female"
- Verify accepts real names: "marla", "alicemonde", "alice", "diana", "bob", "charlie", "chris"

### Integration Test
1. Fresh database
2. Ingest family facts: "My spouse is Marla, she goes by emma. I have three kids: alicemonde (alice) age 12 male, diana (bob) age 10 female, and charlie age 19 male."
3. Query entity_aliases count for test user
4. Expected: 9 entries (valid names only)
5. Verify no garbage: "10", "12", "age", "named", "goes", "spouse", "three", "male", "female"

### Regression Test
- Query "tell me about my family" returns only spouse + children facts
- No noise facts from stop word entities polluting results
- Taxonomy filtering works (all entities typed as Person)

---

## Related Issues

- **dBug-025:** Entity duplication blocks taxonomy filtering — Same root cause (no entity validation gate)
- **dBug-027:** pref_name validation accepts alicecriptive phrases — Symptom of registry pollution
- **dBug-034:** Semantic query resolution failing — Masked by dBug-026 noise (can't resolve real entities with garbage present)

---

## Blocking Dependencies

This bug blocks:
- Proper family fact ingestion (users can't add family members without noise)
- Taxonomy filtering accuracy
- Entity type classification
- Semantic query resolution (dBug-034)

---

## Resolution (2026-05-17)

### Fixes Deployed

**1. Entity Name Blocklist Validation (src/api/main.py)**
- Added ENTITY_NAME_BLOCKLIST constant (75+ stop words, numbers, grammar tokens)
- Implemented _is_valid_entity_name() function with multi-layer checks:
  - Rejects empty/single-char names
  - Rejects pure numeric (ages, years)
  - Rejects blocklist entries (pronouns, verbs, articles, etc.)
  - Rejects >50% numeric (mostly numbers)
  - Rejects >3 word phrases (likely not entity names)
- Validation gate in /ingest edge processing loop (before entity resolution)
- Non-alicetructive: skips invalid edges with logging, no crashes

**2. Entity Type Filtering (_filter_extracted_entities)**
- Comprehensive STOP_WORDS set in GLiNER2 output filtering
- Rejects Concept/unknown types
- Enforces valid types only (Person, Organization, Location, Object, Event, Animal)
- Pre-filters bad entities before registry.resolve()

### Test Results
- ✅ No stop words in entity registry (validation working)
- ✅ Numbers rejected as entity names
- ✅ Grammar tokens filtered at extraction stage
- ✅ Valid entities (names, places) pass through
- ✅ Full pipeline test: LLM responses correct, no garbage entities exposed

### CLAUDE.md Compliance
- ✅ "Metadata-driven validation" — uses rel_types ontology + entity name constraints
- ✅ "Strong Ingest, Dumb Extract" — filter extracts raw, ingest validates and gates
- ✅ "Non-alicetructive post-processing" — skips invalid edges without crashing

### Commit
- **dev:** db8da2f feat: Implement dBug-026 entity name validation gate in /ingest
- **prod:** 3be04a5 fix: UUID leakage + entity validation gate (dBug-026, dBug-039, dprompt-100)

## References

- **Scratch Investigation:** scratch.md (dBug-026 section)
- **Architecture:** CLAUDE.md (Entity Registry, Validation Gate sections)
- **Related Memory:** [[dprompt-97-robust-extraction-validation]] — metadata-driven but didn't address entity names
