# dBug-061: Entity Registry Pollution — Wrong Confidence Assignment on Extraction Noise

**Severity:** CRITICAL  
**Status:** OPEN  
**Reporter:** Claude Code  
**Date Discovered:** 2026-05-19  
**Environment:** Production (docker-host.helpalicekpro.ca)

---

## Problem Statement

**Extraction noise (pronouns, articles, verbs, generic nouns) is being classified as Class A/B and stored in facts table, instead of Class C (staged/ephemeral).**

The system's strength is self-learning. The problem is NOT that extraction is permissive — it's that LOW-CONFIDENCE extraction is being treated as HIGH-CONFIDENCE and stored immediately instead of staged.

When ingesting family data:
```
My name is John, I prefer to be called ${USER}. My spouses name is Marla, she prefers emma. 
We have 3 children, alicemonde(M,12) who goes by alice, diana(F,10) prefers bob, and charlie 19. 
emma has a pet dog named Fraggle
```

Expected entities: ~11 (John, ${USER}, Marla, emma, alicemonde, alice, diana, bob, charlie, ${CHILD2}, Fraggle)

**Actual entities created: 40+**, including:
- Real names: john, ${USER}, marla, emma, alice, alicemonde, diana, bob, charlie, ${CHILD2}, fraggle ✓
- **Garbage words**: called, children, detected, entities, goes, has, my, named, person, pet, prefer, prefers, she, spouses, we, alicemonde(m,12, diana(f,10
- **From corrections**: 10, 12, 19 (numbers as entities), actually, any, call, f, m, not, sorry, pets

## Impact

### Entity Aliases Polluted (40 rows instead of ~11)
```
 alias      | is_preferred 
-------------+--------------
 called      | t
 children    | t
 detected    | t
 entities    | t
 goes        | t
 has         | t
 my          | t
 named       | t
 person      | t
 pet         | t
 prefer      | t
 prefers     | t
 she         | t
 spouses     | t
 we          | t
```

### UUID Surrogates Wasted
Each garbage word gets a UUID v5. For 1000-word conversations, this creates 1000s of spurious UUID entries.

### Query Degradation
Every word collision causes:
- False positive graph traversal (user says "I prefer cats" → "prefer" entity created → traversal crosses spurious edges)
- Qdrant vector index bloated with garbage
- Memory system cannot distinguish signal from noise

### Database Bloat
- entity_aliases table grows linearly with conversation length, not entity count
- Lookups slower (more aliases to resolve)
- Deduplication fails (same word in different conversations creates different UUIDs)

---

## Root Cause Analysis

**Extraction is NOT scoped to real entities.** Pipeline accepts ALL extracted values without validation:

1. **GLiNER2 or LLM extraction** returns too-broad entity list (every noun, adjective, word)
2. **No validation gate** filters/blocks non-entity words before UUID creation
3. **EntityRegistry.resolve()** auto-creates UUID for any string passed to it
4. **`_ingest` endpoint** doesn't validate extracted entities before storing aliases

CLAUDE.md states:
```
We're building a MEMORY PIPELINE! NOT a family or other hard detection system! 
Solutions must scope properly!
```

Current extraction is **unscoped** — it treats every word as a potential entity.

---

## Detailed Reproduction Steps

### Step 1: Clear Database
```bash
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c 'TRUNCATE entities, entity_aliases RESTART IDENTITY CASCADE;'"
```

### Step 2: Ingest Family Statement
```bash
curl -s -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer ${BEARER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "faultline-test",
    "messages": [{"role": "user", "content": "My name is John, I prefer to be called ${USER}. My spouses name is Marla, she prefers emma. We have 3 children, alicemonde(M,12) who goes by alice, diana(F,10) prefers bob, and charlie 19. emma has a pet dog named Fraggle"}],
    "stream": false
  }'
```

### Step 3: Validate Pollution
```bash
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c 'SELECT COUNT(*) as entity_count FROM entity_aliases;'"
```

**Result:** 40+ rows (expected: ~11)

---

## Database State Evidence

### Garbage Aliases Created
```
entity_aliases (FINAL STATE):
 alias      | is_preferred 
-------------+--------------
 called      | t          ← garbage
 children    | t          ← garbage
 detected    | t          ← garbage
 entities    | t          ← garbage
 goes        | t          ← garbage
 has         | t          ← garbage
 my          | t          ← garbage
 named       | t          ← garbage
 person      | t          ← garbage
 pet         | t          ← garbage
 prefer      | t          ← garbage
 prefers     | t          ← garbage
 she         | t          ← garbage
 spouses     | t          ← garbage
 we          | t          ← garbage
 alicemonde(m,12  | t       ← malformed
 diana(f,10 | t       ← malformed
```

### Real vs Fake Entity Count
```
Real (intended):
 - john, ${USER} (1 person, 2 aliases)
 - marla, emma (1 person, 2 aliases)
 - alicemonde, alice (1 person, 2 aliases)
 - diana, bob (1 person, 2 aliases)
 - charlie, ${CHILD2} (1 person, 2 aliases)
 - fraggle (1 pet)
Total expected: 11 aliases

Actual: 40+ aliases (280%+ bloat)
```

---

## Secondary Issues Found During Testing

### Missing Attributes
- **diana's age:** Expected 10, stored but not linked to correct entity
- **Marla's info:** pref_name stored as "emma" instead of "marla"
- **Numbers as entities:** 10, 12, 19 registered as entity aliases (lines 85-87 in final state)

### Malformed Extractions
- `alicemonde(m,12` stored as alias (should parse gender/age, not entity name)
- `diana(f,10` stored as alias (should parse gender/age, not entity name)

### API Response Parsing Failures
All curl requests return: `jq: parse error: Invalid numeric literal at line 1, column 7`
- Indicates API responses are malformed JSON or starting with `:1` (invalid)
- May mask actual extraction errors in test output

---

## Constraints Violated (CLAUDE.md)

### NO BRITTLE HARDCODED BULLSHIT
✗ Extraction has no runtime validation of entity names  
✗ No metadata-driven entity filtering  
✗ No guard function to block common words (pronouns, articles, verbs)

### MEMORY PIPELINE, NOT FAMILY DETECTION SYSTEM
✗ Current extraction treats user input as "extract every noun/entity-like word"  
✓ Should treat it as "extract only real-world entities (people, pets, places, organizations)"

### SCOPE PROPERLY
✗ Extraction scope is unbounded (any word → entity)  
✓ Should be: Named entities (NER) → actual real-world things, not grammar words

### NO ENTITY REGISTRY POLLUTION
✗ dBug-026 was marked fixed, but pollution still occurs at extraction stage  
✓ Should have validation gate before EntityRegistry.resolve() accepts values

---

## Suspected Root Cause Locations

### In `/ingest` endpoint (src/api/main.py)
- **Issue:** Extracts rel_types from LLM output without filtering entity names
- **Line:** Somewhere in `POST /ingest` handler where `edges` are processed
- **Fix:** Add entity validation before `EntityRegistry.resolve(subject_name, subject_type)` and `resolve(object_name, object_type)`

### In GLiNER2 extraction (src/api/main.py)
- **Issue:** `extract_json()` may return too-broad entity list
- **Line:** Lines where GLiNER2 schema is applied
- **Fix:** Post-process GLiNER2 output to filter out non-entity words (pronouns, articles, conjunctions, verbs)

### In LLM extraction prompt (src/api/main.py)
- **Issue:** Extraction prompt may not constrain entity types
- **Line:** Somewhere in `/extract/rewrite` or extraction call
- **Fix:** Add constraint: "Extract only real-world entities: person names, pet names, places, organizations. DO NOT extract pronouns (she, we, he), articles (a, the), or alicecriptive words."

### In EntityRegistry.resolve() (src/entity_registry/registry.py)
- **Issue:** No validation before UUID creation
- **Line:** Where UUID v5 is generated
- **Fix:** Add guard: reject if entity_name is < 2 chars or matches common words list

---

## Metadata-Driven Solution (CLAUDE.md Compliant)

Instead of hardcoded entity validation, use database-driven approach:

### 1. Add `entity_validation_rules` table
```sql
CREATE TABLE entity_validation_rules (
    rule_id UUID PRIMARY KEY,
    entity_type VARCHAR NOT NULL,
    min_length INT DEFAULT 2,
    max_length INT DEFAULT 100,
    pattern VARCHAR,  -- regex for valid names
    blocklist TEXT[],  -- ["he", "she", "it", "the", "a", ...]
    requires_capitalization BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT now()
);
```

### 2. Load rules at startup
```python
_ENTITY_VALIDATION = {}  # entity_type -> rules dict
```

### 3. Validate before EntityRegistry.resolve()
```python
def is_valid_entity_name(name: str, entity_type: str) -> bool:
    """Check against metadata-driven rules."""
    rules = _ENTITY_VALIDATION.get(entity_type, {})
    if len(name) < rules.get('min_length', 2):
        return False
    if name.lower() in rules.get('blocklist', []):
        return False
    # ... check pattern, capitalization, etc.
    return True
```

### 4. Pre-seed blocklist
```sql
INSERT INTO entity_validation_rules (entity_type, blocklist) VALUES
  ('Person', ARRAY['i', 'he', 'she', 'we', 'they', 'you', 'me', 'him', 'her', 'us', 'them', ...]),
  ('Animal', ARRAY['a', 'an', 'the', 'pet', 'dog', 'cat', 'animal', ...]),
  ('Location', ARRAY['place', 'location', 'here', 'there', ...]);
```

---

## Expected Behavior (Post-Fix)

Same family ingest should produce:

```
entity_aliases (EXPECTED):
 alias       | is_preferred 
--------------+--------------
 john  | f
 ${USER}        | t
 marla        | f
 emma         | t
 alicemonde     | f
 alice          | t
 diana    | f
 bob        | t
 charlie        | f
 ${CHILD2}           | t
 fraggle      | t
(11 rows)  ← NOT 40+
```

**No garbage words. No numbers as entities. No malformed strings.**

---

## Next Steps

1. **Identify extraction stage** — GLiNER2 or LLM prompt or both?
2. **Add entity validation gate** — metadata-driven rules before UUID creation
3. **Test with word-heavy input** — verify blocklist catches pronouns/articles/verbs
4. **Validate dBug-026 fix** — check if entity validation that was supposedly fixed is actually running
5. **Add integration test** — prevent regression (extract family data, assert entity count = 11)

---

## Testing Checklist

- [ ] Clear database, ingest family statement
- [ ] Verify entity_aliases count ≤ 15 (allowing for minor variation)
- [ ] Verify no pronouns (i, he, she, we, they) in aliases
- [ ] Verify no numbers (10, 12, 19) as pref_name values
- [ ] Verify all relationships stored correctly (spouse, parent_of, child_of)
- [ ] Verify all attributes stored (ages, genders, preferred names)
- [ ] Run with word-heavy statement (50+ words) — should not create 50+ entities
- [ ] Verify corrections work without creating new garbage entities

---

## Related Issues

- **dBug-026** — Entity Registry Pollution (marked FIXED, but still occurring)
- **dBug-027** — pref_name Validation (marked FIXED)
- **dBug-040** — Incomplete Fact Extraction (may be related to extraction scope)
- **CLAUDE.md** — "NO BRITTLE HARDCODED BULLSHIT", "MEMORY PIPELINE NOT FAMILY DETECTION SYSTEM"

---

## Sign-Off

**Discoverer:** Claude Code  
**Test Environment:** Production (docker-host.helpalicekpro.ca)  
**Reproducibility:** 100% (consistent across all test runs)  
**Urgency:** HIGH — Entity pollution grows linearly with conversation length, eventually making system unusable  
**Blocking:** YES — Cannot validate pipeline correctness until extraction is scoped properly
