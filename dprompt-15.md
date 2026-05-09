# Deepseek Test Prompt: Direct API Validation + Local Docker Testing (AUTONOMOUS ITERATION)

**Scope:** Validate `/query` baseline retrieval and display name resolution. Test locally before production redeploy.

**Background:** 
- `/query` IS working — returns 25 facts including spouse
- BUT: entity_aliases corrupted (UUID→UUID instead of UUID→display name)
- User sees `user -spouse-> 54214459-...` instead of `user -spouse-> mars`
- Need to validate display name resolution is broken and test fixes locally

**Workflow:** Fix code → test on local Docker instance → validate → report (don't redeploy production without user confirmation)

---

## Part 1: Direct API Validation Script

Write a test script (`scripts/validate_query.sh`) that tests `/query` endpoint directly:

```bash
#!/bin/bash
# validate_query.sh — Direct /query API validation

API_URL="${1:-http://localhost:8001}"
USER_ID="${2:-3f8e6836-72e3-43d4-bbc5-71fc8668b070}"

echo "=== /query Direct API Validation ==="
echo "API: $API_URL"
echo "User: $USER_ID"
echo

# Test 1: Baseline retrieval — spouse fact
echo "Test 1: Spouse fact retrieval"
RESPONSE=$(curl -s -X POST "$API_URL/query" \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"tell me about my family\", \"user_id\": \"$USER_ID\"}")

SPOUSE_FACT=$(echo "$RESPONSE" | jq '.facts[] | select(.rel_type == "spouse")')
if [ -z "$SPOUSE_FACT" ]; then
    echo "❌ FAIL: No spouse fact returned"
else
    echo "✅ PASS: Spouse fact returned"
    echo "  Fact: $(echo "$SPOUSE_FACT" | jq -c '{subject, object, rel_type}')"
fi

# Test 2: Metadata stripping — no user_id in response
echo
echo "Test 2: Metadata stripping (no user_id field)"
USER_ID_COUNT=$(echo "$RESPONSE" | jq '[.facts[] | select(has("user_id"))] | length')
if [ "$USER_ID_COUNT" -eq 0 ]; then
    echo "✅ PASS: No user_id in facts"
else
    echo "❌ FAIL: Found $USER_ID_COUNT facts with user_id field"
fi

# Test 3: UUID leakage — no raw UUIDs in preferred_names values
echo
echo "Test 3: UUID leakage check"
UUID_PATTERN='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
UUID_COUNT=$(echo "$RESPONSE" | jq '.preferred_names | to_entries[] | .value | select(test("'"$UUID_PATTERN"'")) | .value' | wc -l)
if [ "$UUID_COUNT" -eq 0 ]; then
    echo "✅ PASS: No UUID values in preferred_names"
else
    echo "⚠️  WARNING: Found $UUID_COUNT UUID values in preferred_names (should be display names)"
    echo "$RESPONSE" | jq '.preferred_names | to_entries[] | select(.value | test("'"$UUID_PATTERN"'")) | {key: .key, value: .value}'
fi

# Test 4: Display name resolution — spouse should resolve to display name
echo
echo "Test 4: Display name resolution"
SPOUSE_OBJ=$(echo "$SPOUSE_FACT" | jq -r '.object')
PREFERRED_NAMES=$(echo "$RESPONSE" | jq '.preferred_names')
DISPLAY_NAME=$(echo "$PREFERRED_NAMES" | jq -r ".\"$SPOUSE_OBJ\"")

if [ "$DISPLAY_NAME" != "null" ] && [ "$DISPLAY_NAME" != "$SPOUSE_OBJ" ]; then
    echo "✅ PASS: Spouse UUID resolves to display name: '$DISPLAY_NAME'"
elif [ "$DISPLAY_NAME" = "null" ]; then
    echo "❌ FAIL: Spouse UUID not in preferred_names (alias not registered)"
elif [ "$DISPLAY_NAME" = "$SPOUSE_OBJ" ]; then
    echo "❌ FAIL: Spouse UUID maps to itself (UUID→UUID, not UUID→display name)"
fi

# Test 5: Fact count
echo
echo "Test 5: Fact count"
FACT_COUNT=$(echo "$RESPONSE" | jq '.facts | length')
echo "Facts returned: $FACT_COUNT"
if [ "$FACT_COUNT" -gt 0 ]; then
    echo "✅ PASS: Facts are being returned"
    echo "  Sample rel_types: $(echo "$RESPONSE" | jq '[.facts[] | .rel_type] | unique' | jq -c '.')"
else
    echo "❌ FAIL: No facts returned"
fi

echo
echo "=== Summary ==="
echo "Run this against local Docker: $0 http://localhost:8001 $USER_ID"
```

---

## Part 2: Local Docker Test Instance Setup

**Instructions for autonomous testing:**

### Step 1: Spin up local Docker instance

```bash
# In a separate terminal/tmux session:
cd /home/chris/Documents/013-GIT/FaultLine
docker-compose up -d

# Verify services running:
docker-compose ps
# Should show: faultline-api, faultline-postgres, faultline-qdrant
```

### Step 2: Wait for services to be ready

```bash
# Poll until /query endpoint responds:
until curl -s http://localhost:8001/query -X POST \
  -H "Content-Type: application/json" \
  -d '{"query": "test", "user_id": "test"}' > /dev/null 2>&1; do
  echo "Waiting for API..."
  sleep 2
done
echo "API ready!"
```

### Step 3: Ingest test data

```bash
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My wife is Mars",
    "user_id": "test_local_user"
  }'

curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Mars loves gardening",
    "user_id": "test_local_user"
  }'
```

### Step 4: Run validation script

```bash
bash scripts/validate_query.sh http://localhost:8001 test_local_user
```

---

## Part 3: Entity Aliases Fix (on local instance)

**Root cause:** Old ingest resolved display name strings to UUIDs. Need to re-register correct aliases.

### Option A: Manual alias insertion (quick test)

```sql
-- Connect to local Postgres
psql -U faultline -d faultline_test -h localhost

-- Check current state
SELECT entity_id, alias, is_preferred FROM entity_aliases LIMIT 10;

-- Insert correct aliases (replace UUID values with actual ones from your test data)
INSERT INTO entity_aliases (user_id, entity_id, alias, is_preferred)
VALUES
  ('test_local_user', '[MARS_UUID]', 'mars', true),
  ('test_local_user', '[DES_UUID]', 'des', true),
  ('test_local_user', '[GABBY_UUID]', 'gabby', true)
ON CONFLICT (user_id, alias) DO UPDATE SET
  entity_id = EXCLUDED.entity_id,
  is_preferred = true;

-- Verify
SELECT entity_id, alias, is_preferred FROM entity_aliases WHERE user_id = 'test_local_user';
```

### Option B: Code fix (permanent)

In `src/api/main.py`, `/ingest` endpoint, ensure `entity_aliases` registration uses actual display names:

```python
# When registering alias for spouse fact:
# OLD (broken): registry.register_alias(uuid_subject, uuid_object, is_preferred=True)
# NEW (correct): registry.register_alias(uuid_subject, display_name_object, is_preferred=True)

# Example: spouse fact (mars, spouse, uuid_spouse)
registry.register_alias(uuid_mars, "mars", is_preferred=True)  # ← Use display name, not UUID
```

---

## Part 4: Validation Loop

1. **Run validation script** on local instance
2. **Check Test 4 result:** Does spouse UUID resolve to display name?
   - If YES → Issue is production data, not code
   - If NO → Code fix needed
3. **If code fix needed:**
   - Modify `/ingest` alias registration
   - Restart local Docker: `docker-compose restart faultline-api`
   - Ingest fresh test data
   - Run validation script again
4. **Once validation passes locally:**
   - Report findings to user
   - PROMPT: "Ready to apply this to production?" (wait for confirmation)
   - User redeploys production
   - Run validation against production user_id

---

## Success Criteria

- ✅ Test 1: Spouse fact returned from /query baseline
- ✅ Test 2: No user_id field in facts
- ✅ Test 3: No raw UUIDs in preferred_names values
- ✅ Test 4: Spouse UUID resolves to display name (e.g., "mars" not UUID)
- ✅ Test 5: Multiple facts returned

**All 5 pass** = ready to report and ask for production redeploy.

---

## IMPORTANT: Workflow Discipline

⚠️ **DO NOT:**
- Rebuild production Docker without explicit user confirmation
- Redeploy to production without reporting test results first
- Modify production data directly

✅ **DO:**
- Spin up local Docker instance for testing
- Iterate locally on fixes
- Report "Test 4 passes on local instance: spouse UUID → 'mars'"
- PROMPT user: "Ready to apply to production?" and WAIT for response

---

## Done When

- ✅ Local Docker instance running
- ✅ Validation script executes and reports all 5 tests
- ✅ Test 4 demonstrates display name resolution (UUID → display name)
- ✅ If failures, code fix identified and tested locally
- ✅ Report findings with Test 4 result
- ✅ PROMPT user for production redeploy (don't just do it)

Ship test results.
