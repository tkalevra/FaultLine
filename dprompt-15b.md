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

## Done When

- ✅ Cycle 1: "Who am I?" returns "Chris"
- ✅ Cycle 2: "Who is my wife?" returns "Mars"
- ✅ Cycle 3: "Who is my daughter?" returns "Gabby"
- ✅ Cycle 4: "Tell me about my family" returns all three names, zero UUIDs
- ✅ LLM response includes family members by display name
- ✅ No database manual patching (all via API)
- ✅ No external system access (local Docker only)

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
