# dprompt-30 — QA Stress Suite: Real-World Extraction & Query

## Purpose

Comprehensive QA testing simulating real usage patterns: messy natural language input, complex familial relationships, multi-attribute objects, corrections, sensitive info, and performance under typical loads. Validate system is production-ready ("shippable").

## Test Scenarios (15 Total)

### EXTRACTION ACCURACY (Natural Language Parsing)

#### Scenario 1: Complex Family Prose
**Input (single message):**
```
"My wife and I have three kids. My son Cyrus is 19. 
My daughter Gabriella, she goes by Gabby, she's 10. 
My son Desmonde, he prefers Des, he's 12."
```

**Expected extraction:**
- Facts: (user, spouse, wife), (wife, pref_name, "wife"), (wife, also_known_as, [wife name if stated])
- Facts: (user, parent_of, Cyrus), (Cyrus, age, 19)
- Facts: (user, parent_of, Gabriella), (Gabriella, pref_name, Gabby), (Gabriella, age, 10)
- Facts: (user, parent_of, Desmonde), (Desmonde, pref_name, Des), (Desmonde, age, 12)
- No crashes on aliases, natural prose, multiple entities per message

**Validate:**
- All 3 children extracted ✓
- Ages assigned correctly ✓
- Nicknames → pref_name (Gabby, Des) ✓
- All relationships inferred ✓

#### Scenario 2: Complex System Metadata
**Input (single message):**
```
"My work server is named prod-api-01.acme.com, IP 10.0.1.42, 
runs on Linux Fedora 43, has 32GB RAM, 4-core Xeon CPU, 
500GB NVMe disk. SSL cert expires 2026-12-15."
```

**Expected extraction:**
- Entity: prod-api-01.acme.com (hostname, system)
- Attributes: hostname, FQDN, IP, OS, RAM, CPU, disk, SSL expiry
- Facts: (system, located_in, acme), (system, has_attribute, [OS/RAM/CPU/disk/SSL])
- No crashes on technical jargon, dates, multiple attributes

**Validate:**
- All technical attributes extracted ✓
- Date parsed (SSL expiry) ✓
- System entity created ✓
- No attribute type mismatches ✓

#### Scenario 3: Alias Resolution Under Query
**Setup:** Gabriella ingested with pref_name=Gabby
**Query:** "What does Gabby like?"

**Expected:**
- System resolves "Gabby" → Gabriella UUID
- Returns facts for Gabriella (likes, dislikes, preferences)
- Works even though query uses nickname, not canonical name

**Validate:**
- Alias → UUID resolution ✓
- Query returns facts for aliased entity ✓

---

### CORRECTION & SUPERSEDE BEHAVIOR

#### Scenario 4: Age Update (Fact Supersede)
**Ingest 1:** "Gabriella is 10"
**Ingest 2:** "Gabriella is 11" (one year later)

**Expected:**
- First fact: (user, child_of, Gabriella), (Gabriella, age, 10)
- Second fact: (Gabriella, age, 11) supersedes first
- Query returns age=11, not both
- No duplicate facts

**Validate:**
- Fact superseded (not duplicated) ✓
- Query returns latest value ✓
- Confidence updated ✓

#### Scenario 5: Relationship Change (Spouse Update)
**Ingest 1:** "I was married to [Ex]"
**Ingest 2:** "I'm now married to [Current]"

**Expected:**
- First fact: (user, spouse, Ex) → superseded_at set
- Second fact: (user, spouse, Current) → active
- Query returns only Current spouse
- No stale relationship in results

**Validate:**
- Old relationship superseded ✓
- New relationship active ✓
- Query returns current spouse ✓

#### Scenario 6: Triple Correction (A → B → A)
**Ingest 1:** "I'm 30"
**Ingest 2:** "I'm 31"
**Ingest 3:** "Actually, I'm 30"

**Expected:**
- Final state: age=30 (ingest 3 wins)
- No duplication, no conflicting states
- Confidence reflects corrections

**Validate:**
- Final state correct ✓
- No duplicate facts ✓
- Correction chain handled ✓

---

### SENSITIVITY GATING

#### Scenario 7: Mixed Sensitive Query
**Facts ingested:**
- (user, age, 30) — not sensitive by default
- (user, lives_at, "123 Main St") — SENSITIVE
- (user, works_for, "ACME Corp") — not sensitive

**Query 1:** "Tell me about myself"
- Expected: age + works_for returned, lives_at EXCLUDED (no explicit ask)

**Query 2:** "Where do I live?"
- Expected: lives_at INCLUDED (explicit sensitive term in query)

**Validate:**
- Sensitivity penalty applied when no explicit ask ✓
- Explicit query terms override penalty ✓
- Threshold 0.4 enforced ✓

#### Scenario 8: Birthday Gating
**Facts:** (user, born_on, "1990-05-15"), (user, age, 30)

**Query 1:** "Tell me about me"
- Expected: age returned, born_on EXCLUDED

**Query 2:** "When was I born?" or "What's my birthday?"
- Expected: born_on INCLUDED (explicit ask)

**Validate:**
- Birthday gated without explicit ask ✓
- Explicit terms bypass gate ✓

---

### NOVEL TYPE HANDLING

#### Scenario 9: Unknown Rel_Type Graceful Degradation
**Ingest:** "I mentor Cyrus in chess" (rel_type="mentor" not in ontology)

**Expected:**
- Edge extracted: (user, mentor, Cyrus)
- WGM gate: "mentor" unknown → Class C (staged)
- Ingest succeeds, no crash
- Fact stored with rel_type="unknown"
- Query doesn't return fact (confidence 0.4, below threshold)
- No system error logs

**Validate:**
- Unknown type → Class C ✓
- No crash ✓
- Graceful degradation ✓
- Staged entry created ✓

---

### GRAPH DEPTH & HIERARCHY EXPANSION

#### Scenario 10: Extended Family Transitive Discovery
**Setup:**
```
User: Alice
Facts:
  - (alice, spouse, Bob)
  - (alice, parent_of, Cyrus)
  - (alice, parent_of, Gabriella)
  - (alice, parent_of, Desmonde)
  - (Cyrus, instance_of, Person, subclass_of, Child)
  - (Gabriella, instance_of, Person, subclass_of, Child)
  - (Desmonde, instance_of, Person, subclass_of, Child)
```

**Query:** "Tell me about my family"

**Expected:**
- Graph traversal: {alice, bob, cyrus, gabriella, desmonde}
- Hierarchy expand: all → Person → Family (if Family taxonomy exists)
- Results: all 5 people + relationships + classification
- No missing family members

**Validate:**
- All family members discovered ✓
- Transitive relationships followed ✓
- Hierarchy expansion included ✓

#### Scenario 11: "My Kids" Auto-Discovery
**Setup:** Same as Scenario 10 (Cyrus, Gabriella, Desmonde all instance_of Child)

**Query:** "What do my kids do?"

**Expected:**
- Graph traversal finds direct children (Cyrus, Gabriella, Desmonde)
- Hierarchy finds all instance_of Child entities
- Returns occupations/work for all kids
- No manual listing required

**Validate:**
- Children auto-discovered ✓
- Hierarchy expands Child class ✓

#### Scenario 12: 3-Hop Transitive Query
**Setup:**
```
User → spouse → sibling → niece
```

**Query:** "Who are my nieces and nephews?"

**Expected:**
- Graph traversal (2 hops): spouse → spouse.siblings → spouse.siblings.children
- Returns extended family
- No infinite loops or missed entities

**Validate:**
- Multi-hop traversal works ✓
- Extended family discovered ✓

---

### RE-INGEST & IDEMPOTENCY

#### Scenario 13: Duplicate Ingest (10x same fact)
**Ingest same fact 10 times in rapid succession:**
```
"Cyrus is 19"
"Cyrus is 19"
... (8 more times)
```

**Expected:**
- Single fact in `facts` table (not 10)
- confirmed_count increments to 10 (if promoted)
- No Qdrant point duplication
- Query returns fact once

**Validate:**
- Deduplication works ✓
- confirmed_count accurate ✓
- No Qdrant bloat ✓

#### Scenario 14: Partial Re-Ingest (subset of entities)
**Ingest 1:** "My wife and I have three kids: Cyrus (19), Gabby (10), Des (12)"
**Ingest 2:** "Cyrus is now 20"

**Expected:**
- First ingest: 7 facts (spouse, 3 parent_of, 3 ages)
- Second ingest: updates Cyrus age to 20, supersedes old fact
- No duplication of spouse or other kids
- Final state: correct

**Validate:**
- Partial updates work ✓
- No re-creation of unchanged facts ✓
- Age update applied ✓

---

### EDGE CASES & ROBUSTNESS

#### Scenario 15: Circular Relationships (Defensive)
**Setup:**
```
Facts:
  - (A, spouse, B)
  - (B, spouse, A)  [symmetric, correct]
  - (C, sibling_of, D)
  - (D, sibling_of, C)  [symmetric, correct]
  BUT manually add:
  - (E, parent_of, F)
  - (F, parent_of, E)  [circular, incorrect]
```

**Query:** "Tell me about E"

**Expected:**
- Hierarchy expansion detects cycle (depth-limited recursion)
- No infinite loop
- Returns up to max_depth without hanging
- System stable

**Validate:**
- Circular detection works ✓
- No hang/crash ✓
- Depth limit respected ✓

---

## Performance Baselines

**Not hard requirements, but measure:**
- Single query < 500ms (baseline)
- Hierarchy expand on 5-level chain < 200ms
- Duplicate ingest (10x) completes < 1s
- 100+ facts for single user retrieves all without timeout
- Qdrant reconciliation completes < 5s

## Expected Outcome

All 15 scenarios pass. System handles:
- ✓ Messy natural language with aliases
- ✓ Complex multi-attribute objects
- ✓ Fact corrections without duplication
- ✓ Sensitive info gating with explicit overrides
- ✓ Novel types gracefully (no crashes)
- ✓ Deep graph transitive discovery
- ✓ Re-ingest idempotency
- ✓ Circular relationship safety
- ✓ Performance within acceptable bounds

**Result:** System is production-ready ("shippable").
