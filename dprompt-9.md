# Deepseek Validation Prompt: Temporal Events End-to-End (curl + SQL + logs)

**Scope:** Validate temporal events work end-to-end: extraction → storage → retrieval → memory injection. Use curl, SQL, and Docker logs.

**Why:** Phase 7 code is live. Need definitive proof temporal flow works before moving to systematic test coverage.

---

## Environment

- Docker running: `docker compose up` (FaultLine API on port 8001)
- PostgreSQL accessible: `psql -U faultline -d faultline_test`
- Logs accessible: `docker logs faultline-api` and `docker logs faultline-postgres`
- Test user: `test_temporal_123`

---

## Setup: Baseline Facts for Regression Testing

Before temporal tests, establish baseline facts (non-temporal):

```bash
# Baseline facts
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My wife Marla loves gardening. We have a dog named Fraggle who is a Golden Retriever.",
    "user_id": "test_temporal_123"
  }'

# Expected baseline facts in DB:
# - (user, spouse, marla)
# - (user, has_pet, fraggle)
# - (marla, likes, gardening)
# - (fraggle, instance_of, golden_retriever)
```

SQL verify:
```sql
SELECT COUNT(*) FROM facts WHERE user_id = 'test_temporal_123';
-- Expected: 4+ baseline facts
```

---

## Part A: Regression Tests (Existing Retrieval)

### Regression 1: Entity Match (Tier 1)

```bash
# Query: "How's my wife?"
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How'\''s my wife?",
    "user_id": "test_temporal_123"
  }'

# Expected:
# - Returns Marla facts (spouse, likes gardening, etc.)
# - NO temporal events yet (we haven't added dates for Marla)
# - Tier 1 entity resolution works
```

Verify:
```bash
curl ... | jq '.facts[] | select(.subject == "marla" or .object == "marla")'
# Should return spouse + marla's facts
```

### Regression 2: Pet Recall (has_pet)

```bash
# Query: "Tell me about my pet"
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Tell me about my pet",
    "user_id": "test_temporal_123"
  }'

# Expected:
# - Returns Fraggle facts (has_pet relationship)
# - Memory shows: "fraggle is a golden retriever"
# - Relational resolution works ("my pet" → has_pet)
```

### Regression 3: Identity Fallback (Tier 2)

```bash
# Query: "Tell me about myself"
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Tell me about myself",
    "user_id": "test_temporal_123"
  }'

# Expected:
# - Returns all user-anchored facts (spouse, pet, likes, etc.)
# - Keyword scoring for relevance
# - No errors from new events table
```

### Regression 4: Three-Tier Retrieval (all tiers)

```bash
# Query with multiple keywords (spouse + pet + likes)
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What do my wife and pet like?",
    "user_id": "test_temporal_123"
  }'

# Expected:
# - Tier 1: matches "wife" (marla), "pet" (fraggle)
# - Returns marla + fraggle facts
# - Likes relationships included
```

### Regression 5: No Temporal Events in Baseline

```sql
-- Verify baseline facts didn't accidentally create events
SELECT COUNT(*) FROM events WHERE user_id = 'test_temporal_123';
-- Expected: 0 (no temporal rel_types in baseline)
```

---

## Part B: Temporal Events Tests (NEW)

### Step 1: Extract Birthday (curl + SQL)

```bash
# POST /ingest: "I was born on May 3rd, 1990"
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I was born on May 3rd, 1990",
    "user_id": "test_temporal_123"
  }'

# Expected response:
# { "status": "success", "facts_committed": 1, ... }
```

**Then validate SQL:**

```sql
-- Check events table
SELECT * FROM events 
WHERE user_id = 'test_temporal_123' 
  AND event_type = 'born_on';

-- Expected:
-- id | user_id | subject_id | event_type | occurs_on | recurrence | confidence | created_at
-- 1  | test... | user       | born_on    | may 3rd... | yearly     | 0.9        | [now]
```

**Check logs:**

```bash
docker logs faultline-api | grep -i "born_on\|temporal\|event"
# Should show: routing to events table, insertion success
```

---

### Step 2: Extract Appointment (curl + SQL)

```bash
# POST /ingest: "I have a dentist appointment on July 16, 2026"
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I have a dentist appointment on July 16, 2026",
    "user_id": "test_temporal_123"
  }'
```

**SQL validation:**

```sql
SELECT * FROM events 
WHERE user_id = 'test_temporal_123' 
  AND event_type = 'appointment_on';

-- Expected:
-- event_type: appointment_on, occurs_on: july 16, 2026, recurrence: once
```

---

### Step 3: Extract Other's Birthday (curl + SQL)

```bash
# POST /ingest: "My wife Marla was born on June 15, 1992"
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My wife Marla was born on June 15, 1992",
    "user_id": "test_temporal_123"
  }'
```

**SQL validation:**

```sql
-- Should create two things:
-- 1. Fact: (user, spouse, marla_uuid)
SELECT * FROM facts 
WHERE user_id = 'test_temporal_123' 
  AND rel_type = 'spouse';

-- 2. Event: (marla_uuid, born_on, "june 15, 1992")
SELECT * FROM events 
WHERE user_id = 'test_temporal_123' 
  AND event_type = 'born_on' 
  AND occurs_on = 'june 15, 1992';

-- Expected:
-- Fact entry + Event entry (two rows)
```

---

### Step 4: Query and Merge (curl)

```bash
# POST /query: "When was I born?"
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "When was I born?",
    "user_id": "test_temporal_123"
  }'

# Expected response includes:
# {
#   "facts": [
#     {
#       "subject": "user",
#       "object": "may 3rd, 1990",
#       "rel_type": "born_on",
#       "source": "events_table",
#       "recurrence": "yearly"
#     },
#     ...
#   ]
# }
```

**Validate events merged:**

```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Tell me about my wife",
    "user_id": "test_temporal_123"
  }' | jq '.facts[] | select(.rel_type == "born_on" or .rel_type == "spouse")'

# Should return both:
# - spouse: marla
# - born_on for marla: june 15, 1992
```

---

### Step 5: Memory Injection (curl + logs)

```bash
# Simulate OpenWebUI inlet with /query + memory injection
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How old is my wife?",
    "user_id": "test_temporal_123"
  }' | jq '.facts'
```

**Check Filter logs:**

```bash
docker logs faultline-api | grep -i "⭐\|📅\|memory\|event"
# Should show formatted events in memory block
```

---

## Part C: Mixed Queries (Temporal + Regular Facts)

### Mixed 1: Spouse with Birthday

```bash
# We already have: (user, spouse, marla)
# Now add: (marla, born_on, "june 15, 1992")
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Marla was born on June 15, 1992",
    "user_id": "test_temporal_123"
  }'

# Query: "When was my wife born?"
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "When was my wife born?",
    "user_id": "test_temporal_123"
  }'

# Expected:
# - Returns spouse fact (marla)
# - Returns temporal event (marla, born_on, "june 15, 1992")
# - Both in single response
# - Memory shows: "marla's born_on: june 15, 1992 (annually)"
```

SQL verify:
```sql
SELECT * FROM facts WHERE user_id = 'test_temporal_123' AND rel_type = 'spouse';
SELECT * FROM events WHERE user_id = 'test_temporal_123' AND event_type = 'born_on';
-- Expected: 1 spouse fact + 1 born_on event
```

### Mixed 2: Pet with Age + Attributes

```bash
# We have: (user, has_pet, fraggle)
# Add: age + temporal info
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Fraggle is 5 years old, born on March 10, 2021",
    "user_id": "test_temporal_123"
  }'

# Query: "How old is my pet and when was he born?"
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How old is my pet and when was he born?",
    "user_id": "test_temporal_123"
  }'

# Expected:
# - has_pet fact (fraggle)
# - age attribute (5)
# - temporal event (born_on, march 10, 2021)
# - All three in response
```

### Mixed 3: Conversation State + Temporal

```bash
# Turn 1: Establish temporal facts + entities
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My wife Marla was born June 15, 1992",
    "user_id": "test_temporal_123"
  }'

# Turn 2: Query using pronoun
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "When was she born?",
    "user_id": "test_temporal_123"
  }'

# Expected:
# - Pronoun "she" resolves to marla via conversation state
# - Returns marla's born_on event
# - Memory shows: "marla's born_on: june 15, 1992"
```

---

## Edge Cases to Validate

1. **Fuzzy date:**
   ```bash
   curl -X POST http://localhost:8001/ingest \
     -H "Content-Type: application/json" \
     -d '{
       "text": "I was born sometime in 1990",
       "user_id": "test_temporal_123"
     }'
   ```
   SQL: Check `confidence < 1.0` for fuzzy dates

2. **Correction:**
   ```bash
   # Turn 1: "I was born May 3rd"
   curl ... -d '{"text": "I was born May 3rd", "user_id": "test_temporal_123"}'
   
   # Turn 2: "Actually June 3rd"
   curl ... -d '{"text": "Actually born June 3rd, not May 3rd", "user_id": "test_temporal_123"}'
   ```
   SQL: Check events table has only ONE row for born_on (updated, not duplicate)
   ```sql
   SELECT COUNT(*) FROM events 
   WHERE user_id = 'test_temporal_123' 
     AND event_type = 'born_on' 
     AND occurs_on = 'june 3rd';
   -- Expected: 1 (correction overwrote)
   ```

3. **Multiple events:**
   ```bash
   curl ... -d '{"text": "My birthday is May 3rd and our anniversary is June 20th", "user_id": "test_temporal_123"}'
   ```
   SQL: Check both events in table
   ```sql
   SELECT event_type, occurs_on FROM events 
   WHERE user_id = 'test_temporal_123';
   -- Expected: born_on + anniversary_on (2 rows)
   ```

4. **No relative dates:**
   ```bash
   curl ... -d '{"text": "I am going on vacation next week", "user_id": "test_temporal_123"}'
   ```
   SQL: Check NO events created
   ```sql
   SELECT COUNT(*) FROM events 
   WHERE user_id = 'test_temporal_123' 
     AND occurs_on LIKE '%next%';
   -- Expected: 0
   ```

---

## Checklist

### Regression (Existing Functionality)
- ✅ Baseline facts extract to facts table (spouse, has_pet, likes)
- ✅ Entity match (Tier 1) retrieves wife/pet facts correctly
- ✅ Relational resolution ("my wife", "my pet") still works
- ✅ Identity fallback (Tier 2) returns user-anchored facts
- ✅ Keyword scoring (Tier 3) ranks relevant facts
- ✅ Three-tier retrieval doesn't regress with events table present
- ✅ No baseline facts accidentally become events
- ✅ Display name resolution works for existing facts

### Mixed Queries (Temporal + Regular)
- ✅ Spouse + birthday returns both fact + event
- ✅ Pet + age + born_on returns all three
- ✅ Conversation state pronouns resolve with temporal events
- ✅ Memory injection shows mixed facts + events
- ✅ Memory formatting: facts as bullets, events as ⭐/📅

### Extraction
- ✅ Birthday patterns extract to events table
- ✅ Anniversaries extract with recurrence="yearly"
- ✅ Appointments extract with recurrence="once"
- ✅ Other entities' birthdays routed correctly
- ✅ Compound patterns (age + date) create both fact + event
- ✅ Corrections UPDATE existing event (no duplicates)
- ✅ Fuzzy dates marked low_confidence
- ✅ No relative dates extracted

### Storage
- ✅ Events table has correct schema (occurs_on, recurrence, confidence)
- ✅ UNIQUE constraint works (no duplicate events per entity/type)
- ✅ Confidence values reflect extraction quality
- ✅ Recurrence field populated correctly

### Query
- ✅ `/query` merges events from events table
- ✅ Events appear in facts response with source="events_table"
- ✅ Preferred display names applied to event subjects
- ✅ Recurrence metadata included

### Memory
- ✅ Filter formats yearly events as "⭐ X's birthday: date (annually)"
- ✅ Filter formats one-time events as "📅 X date: date"
- ✅ Events inject naturally into memory block

### Logs
- ✅ No errors in Docker logs
- ✅ Event routing visible in logs
- ✅ Query merge visible in logs
- ✅ Memory injection visible in logs

---

## Done When

All checks pass:

**Regression (baseline must not break):**
- ✅ 5 regression tests pass (entity match, pet recall, identity fallback, three-tier, no temporal in baseline)
- ✅ Baseline facts still in facts table (spouse, has_pet, likes)
- ✅ Query retrieval unchanged for non-temporal facts

**Temporal events:**
- ✅ 5 basic curl tests succeed (birthday, appointment, spouse+birthday, query, memory)
- ✅ SQL validates storage (events table has correct rows, schema correct)
- ✅ 4 edge cases pass (fuzzy, correction, multiple, no-relative)

**Mixed queries:**
- ✅ 3 mixed tests pass (spouse+birthday, pet+age+born_on, pronouns+temporal)
- ✅ Memory injection shows both facts and events correctly formatted
- ✅ Conversation state works with temporal events

**Logs & validation:**
- ✅ Logs show clean execution (no errors, event routing visible)
- ✅ Fact count and formatting correct in memory injection
- ✅ No regressions on query retrieval

---

## Report Back

Return:
1. **Summary table:** Part | Test | Status | Details
   - Part A (Regression): 5 tests
   - Part B (Temporal): 5 basic + 4 edge cases
   - Part C (Mixed): 3 tests
2. **SQL snapshots:** 
   - Baseline facts count
   - Events table (1-2 sample rows)
   - Mixed query results
3. **Logs:**
   - Event extraction success
   - Query merge (events in response)
   - Memory formatting (⭐/📅 visible)
4. **Any errors** from curl/SQL/logs
5. **Regression validation:** Confirm no breakage of existing retrieval

Ship comprehensive validation report.
