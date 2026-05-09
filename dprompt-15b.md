# Deepseek Validation Prompt: Full-Circle Ingest→Alias→Query→Display (LOCALLY ONLY)

**Scope:** Validate the complete ingest-to-display pipeline end-to-end. Fresh Docker instance, no database manipulation, no external systems.

**Critical constraint:** NO SSH, NO TrueNAS, NO external database access. All testing via API and local Docker only.

**Issue:** Fresh ingest of family member names ("My daughter is Gabby") doesn't register aliases. Query returns partial facts. The ingest pipeline is broken somewhere.

**Goal:** Trace the full cycle locally until it completes end-to-end without breaking.

---

## ⚠️ CRITICAL CONSTRAINTS

**🚫 DO NOT:**
- SSH into TrueNAS or any external system
- Connect to production PostgreSQL directly
- Run SQL queries manually to "fix" data
- Modify any database via psql, CLI, or external tools
- Use any system outside this local environment

**✅ DO:**
- Use only LOCAL Docker instance
- Use only API calls (curl/HTTP)
- Use only local file operations
- Test via prompt-to-response cycle
- If something breaks, fix the CODE, not the data

---

## Part 1: Fresh Local Environment

### Step 1: Start clean Docker instance

```bash
cd /home/chris/Documents/013-GIT/FaultLine

# Stop existing instance (if running)
docker-compose down -v  # -v removes volumes (clean slate)

# Bring up fresh instance
docker-compose up -d

# Wait for services ready
sleep 10
docker-compose ps
```

### Step 2: Verify clean state

```bash
# Query should return 0 facts for a fresh user
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "tell me about myself",
    "user_id": "fresh_test_user_12345"
  }' | jq '.facts | length'

# Expected: 0
```

---

## Part 2: Full-Circle Validation

### Cycle 1: Identity Name Registration

**Step 1: Ingest identity fact**
```bash
USER_ID="fresh_test_user_12345"

curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My name is Chris",
    "user_id": "'$USER_ID'"
  }' | jq '.status'

# Expected: "success"
```

**Step 2: Verify alias registered**
```bash
# Query for a name-related fact to trigger alias lookup
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Who am I?",
    "user_id": "'$USER_ID'"
  }' | jq '{
    facts_count: (.facts | length),
    preferred_names: .preferred_names,
    has_chris: (.preferred_names | to_entries[] | select(.value == "chris") | .key)
  }'

# Expected: preferred_names should include "chris" → some_UUID mapping
# has_chris should show a UUID key
```

**Step 3: Check memory injection**
- LLM response to "Who am I?" should include "Chris", not UUID
- If response says "your name is [redacted]" or shows UUID, alias registration failed

**Decision:**
- ✅ If Chris appears in response → Cycle 1 PASS
- ❌ If Chris doesn't appear → Trace where it broke:
  - Did /ingest succeed?
  - Is alias in preferred_names?
  - Did relevance gate filter it out?

---

### Cycle 2: Spouse Registration

**Step 1: Ingest spouse fact**
```bash
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My wife is Mars",
    "user_id": "'$USER_ID'"
  }' | jq '.status'

# Expected: "success"
```

**Step 2: Verify spouse fact returned**
```bash
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Tell me about my wife",
    "user_id": "'$USER_ID'"
  }' | jq '.facts[] | select(.rel_type == "spouse") | {subject, object, rel_type}'

# Expected: fact with subject="user" (or "chris"), object should be "mars" display name, not UUID
```

**Step 3: Check display name resolution**
```bash
SPOUSE_RESPONSE=$(curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Tell me about my wife",
    "user_id": "'$USER_ID'"
  }')

# Check if Mars appears in preferred_names AND resolves correctly
echo "$SPOUSE_RESPONSE" | jq '.preferred_names | to_entries[] | select(.value == "mars")'

# Expected: {key: "some_uuid", value: "mars"}
```

**Step 4: Check LLM response**
- Query "Who is my wife?" should return "Mars", not UUID
- If it says "[redacted]" or shows UUID → display name resolution broken

**Decision:**
- ✅ If Mars appears in response → Cycle 2 PASS
- ❌ If Mars doesn't appear → Trace where it broke:
  - Did /ingest extract spouse edge?
  - Is spouse fact in /query response?
  - Is alias registered in preferred_names?
  - Did relevance gate filter it?

---

### Cycle 3: Child Registration

**Step 1: Ingest child fact**
```bash
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My daughter is Gabby",
    "user_id": "'$USER_ID'"
  }' | jq '.status'

# Expected: "success"
```

**Step 2: Verify child fact returned**
```bash
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Tell me about my daughter",
    "user_id": "'$USER_ID'"
  }' | jq '.facts[] | select(.rel_type == "child_of" or .rel_type == "parent_of") | {subject, object, rel_type}'

# Expected: parent_of fact with object="gabby" (display name), not UUID
```

**Step 3: Check LLM response**
- Query "Who is my daughter?" should return "Gabby", not UUID

**Decision:**
- ✅ If Gabby appears → Cycle 3 PASS
- ❌ If Gabby doesn't appear → Trace breakpoint (same as Cycle 2)

---

### Cycle 4: "Tell me about my family" — Full Integration

**Step 1: Query full family**
```bash
FAMILY_RESPONSE=$(curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Tell me about my family",
    "user_id": "'$USER_ID'"
  }')

echo "$FAMILY_RESPONSE" | jq '{
  fact_count: (.facts | length),
  has_chris: (.preferred_names | to_entries[] | select(.value == "chris") | .key | length > 0),
  has_mars: (.preferred_names | to_entries[] | select(.value == "mars") | .key | length > 0),
  has_gabby: (.preferred_names | to_entries[] | select(.value == "gabby") | .key | length > 0)
}'
```

**Step 2: Check LLM response**
- Should mention: Chris (user), Mars (wife), Gabby (daughter)
- Should NOT mention any UUIDs

**Decision:**
- ✅ All three names appear → **FULL CYCLE PASS** ✅
- ❌ Missing names → Identify which cycle broke and debug

---

## Part 3: Breakpoint Debugging

If any cycle fails, use this debugging flow:

```bash
# 1. Did /ingest succeed?
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{"text": "My daughter is Gabby", "user_id": "'$USER_ID'"}' | jq '.'

# 2. Does fact exist in /query response?
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query": "daughter", "user_id": "'$USER_ID'"}' | jq '.facts[] | select(.rel_type == "parent_of")'

# 3. Is alias registered in preferred_names?
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query": "family", "user_id": "'$USER_ID'"}' | jq '.preferred_names | keys'

# 4. If alias exists, what does it map to?
GABBY_UUID="[extract from facts response]"
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query": "family", "user_id": "'$USER_ID'"}' | jq '.preferred_names["'$GABBY_UUID'"]'

# Expected: "gabby", not a UUID
```

If preferred_names shows UUID→UUID, the ingest-side alias registration is broken. Fix location: `src/api/main.py`, `/ingest` endpoint, where `registry.register_alias()` is called.

---

### Cycle 5: Age Registration

**Step 1: Ingest age fact**
```bash
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I am 35 years old",
    "user_id": "'$USER_ID'"
  }' | jq '.status'

# Expected: "success"
```

**Step 2: Verify age returned**
```bash
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How old am I?",
    "user_id": "'$USER_ID'"
  }' | jq '.facts[] | select(.rel_type == "age") | {subject, object, rel_type}'

# Expected: fact with rel_type="age", object="35"
```

**Step 3: Check LLM response**
- Query "How old are you?" should return "35", not UUID or raw number

**Decision:**
- ✅ If "35" appears → Cycle 5 PASS
- ❌ If missing → Debug scalar fact extraction and entity_attributes storage

---

### Cycle 6: Birthday Registration

**Step 1: Ingest birthday fact**
```bash
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My birthday is May 3rd",
    "user_id": "'$USER_ID'"
  }' | jq '.status'

# Expected: "success"
```

**Step 2: Verify birthday returned**
```bash
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "When is my birthday?",
    "user_id": "'$USER_ID'"
  }' | jq '.facts[] | select(.rel_type == "born_on" or .event_type == "born_on")'

# Expected: event or fact with born_on, object="may 3rd"
```

**Step 3: Check LLM response**
- Query "When were you born?" should return "May 3rd"

**Decision:**
- ✅ If "May 3rd" appears → Cycle 6 PASS
- ❌ If missing → Debug temporal event extraction and storage

---

### Cycle 7: Home Address Registration

**Step 1: Ingest address fact**
```bash
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I live at 156 Cedar Street South, Kitchener, Ontario",
    "user_id": "'$USER_ID'"
  }' | jq '.status'

# Expected: "success"
```

**Step 2: Verify location returned**
```bash
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Where do I live?",
    "user_id": "'$USER_ID'"
  }' | jq '.facts[] | select(.rel_type == "lives_at") | {subject, object, rel_type}'

# Expected: fact with lives_at, object contains address string
```

**Step 3: Check LLM response**
- Query "Where do you live?" should return address
- **CRITICAL:** Should NOT leak any UUIDs or internal identifiers

**Decision:**
- ✅ If address appears without UUID leak → Cycle 7 PASS
- ❌ If UUID/system ID appears → UUID leakage not fixed

---

### Cycle 8: Fact Correction (Spouse Name)

**Step 1: Correct wife's name**
```bash
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Actually, my wife'\''s real name is Martha, not Mars",
    "user_id": "'$USER_ID'"
  }' | jq '.status'

# Expected: "success" (correction detected and processed)
```

**Step 2: Verify correction applied**
```bash
# Query should now return Martha, not Mars
curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Who is my wife?",
    "user_id": "'$USER_ID'"
  }' | jq '{
    spouse_facts: [.facts[] | select(.rel_type == "spouse")],
    preferred_names: .preferred_names
  }'

# Expected: spouse fact with object="martha" (or uuid mapping to "martha")
```

**Step 3: Check LLM response**
- Query "Who is your wife?" should return "Martha", NOT "Mars"
- Old name should be superseded, not duplicated

**Decision:**
- ✅ If only "Martha" appears (not "Mars") → Cycle 8 PASS
- ❌ If both names appear → Correction not working (fact not superseded)
- ❌ If UUID appears → Display name not updated

---

### Cycle 9: Out-of-Domain Query (Weather Check)

**Step 1: Ask about weather (something system shouldn't know)**
```bash
LLM_RESPONSE=$(curl -s -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is the weather tomorrow?",
    "user_id": "'$USER_ID'"
  }')

# The system shouldn't have weather facts, but should handle gracefully
echo "$LLM_RESPONSE" | jq '{
  facts_count: (.facts | length),
  has_uuids: (.facts | map(.subject, .object) | join(",") | test("[0-9a-f]{8}-")),
  has_user_id: (.facts | map(keys) | flatten | any(. == "user_id"))
}'
```

**Step 2: Check LLM's response to user**
- Should admit it doesn't know the weather
- Should NOT fabricate facts
- Should NOT leak UUIDs, user_ids, or system identifiers
- Should NOT return internal metadata

**Decision:**
- ✅ If response is honest ("I don't know", no UUID leaks) → Cycle 9 PASS
- ❌ If response fabricates weather facts → Hallucination issue
- ❌ If UUID/user_id appears → Critical leak, exposure still present

---

## Full Cycle Summary

| Cycle | Test | Success Criteria | Type |
|-------|------|------------------|------|
| 1 | Identity | "Chris" appears | Identity |
| 2 | Spouse | "Mars" appears | Relationship |
| 3 | Child | "Gabby" appears | Relationship |
| 4 | Family | All three names, zero UUIDs | Integration |
| 5 | Age | "35" appears | Scalar |
| 6 | Birthday | "May 3rd" appears | Temporal |
| 7 | Address | Address appears, NO UUID leak | Scalar + Sensitivity |
| 8 | Correction | "Martha" appears, NOT "Mars" | Correction/Supersede |
| 9 | Weather | No fabrication, no leaks | Out-of-domain |

---

## Done When

**Identity & Relationships:**
- ✅ Cycle 1: "Who am I?" returns "Chris"
- ✅ Cycle 2: "Who is my wife?" returns "Mars"
- ✅ Cycle 3: "Who is my daughter?" returns "Gabby"
- ✅ Cycle 4: "Tell me about my family" returns all three names, zero UUIDs

**Scalars & Temporal:**
- ✅ Cycle 5: "How old am I?" returns "35"
- ✅ Cycle 6: "When is my birthday?" returns "May 3rd"
- ✅ Cycle 7: "Where do I live?" returns address, ZERO UUID/system ID leaks

**Corrections & Integrity:**
- ✅ Cycle 8: After correction, "Who is my wife?" returns "Martha" (NOT "Mars")

**Out-of-Domain Safety:**
- ✅ Cycle 9: "What's the weather tomorrow?" response is honest (no weather facts), no fabrication, no UUID leaks

**General Requirements:**
- ✅ All 9 cycles pass
- ✅ No database manual patching (all via API)
- ✅ No external system access (local Docker only)
- ✅ Zero UUID leakage in any response

**If any cycle fails:**
- ✅ Identify breakpoint (ingest, alias registration, query, relevance, display)
- ✅ Fix code (not data)
- ✅ Re-test locally until cycle completes
- ✅ Report findings

---

## CRITICAL REMINDER

**DO NOT TOUCH:**
- SSH, TrueNAS, external databases
- Production environment
- Manual database modifications

**Test only via:**
- Local Docker instance
- API calls (curl)
- LLM conversation (to validate end-to-end)

If something is broken, **FIX THE CODE**, not the database.

Ship test results.
