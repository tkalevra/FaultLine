# dprompt-34 — Pre-Production Validation: Live OpenWebUI Testing Against Clean Database

## Purpose

Test FaultLine against the **live pre-production OpenWebUI instance** (hairbrush.helpdeskpro.ca) with a freshly wiped database. Validate that the complete end-to-end pipeline works: LLM extraction → ingest → collision detection → re-embedder resolution → query, all via actual API calls with the bearer token.

## The Task

**Database state:** Wiped (schema dropped and recreated on truenas faultline-postgres).
**Test method:** OpenWebUI API calls via curl with bearer token.
**Model:** `faultline-wgm-test-10` (locked, no switching).
**Test scenarios:**
1. Simple family ingest ("We have two kids: Cyrus and Desmonde") → query "What's my family"
2. Name collision scenario ("Third daughter Gabriella who goes by Gabby", user also called Gabby) → wait for re-embedder → query "Tell me about Gabriella"
3. System metadata ("My laptop is named Mars, IP 192.168.1.100") → query "Tell me about my computer"
4. Sensitivity gating ("I was born on January 15") → query without explicit ask → should not return; query with "How old" → should return
5. Transitive relationships ("My friend knows my sister") → query "Who knows my family"

**Expected outcome:**
- All ingest messages acknowledged by Filter LLM
- Facts stored in PostgreSQL (verify via SSH queries)
- Collisions detected (Gabriella + user both "Gabby") and resolved by re-embedder
- Query results include all entities with **preferred names only** (no UUIDs, no fallback aliases visible)
- No missing entities (Gabriella visible despite collision)

## Prerequisites

- OpenWebUI instance running at https://hairbrush.helpdeskpro.ca/?models=faultline-wgm-test-10
- Bearer token: `sk-addb2220bf534bfaa8f78d96e6991989`
- PostgreSQL on truenas wiped and ready
- Re-embedder running (monitors for pending conflicts, resolves via LLM)
- curl available locally for API calls

## Test Execution Flow

### Scenario 1: Family Ingest + Query
```bash
# Step 1: Ingest family
curl -X POST -H "Authorization: Bearer sk-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"We have two kids: Cyrus and Desmonde, and a spouse Mars"}],"stream":false}' \
  https://hairbrush.helpdeskpro.ca/api/chat/completions

# Step 2: Wait 2s for ingest pipeline to complete

# Step 3: Query family
curl -X POST -H "Authorization: Bearer sk-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"What is my family"}],"stream":false}' \
  https://hairbrush.helpdeskpro.ca/api/chat/completions

# Expected: Response mentions Mars (spouse), Cyrus and Desmonde (children) with NAMES, not UUIDs
```

### Scenario 2: Gabriella Name Collision (The Canary Test)
```bash
# Step 1: User has pref_name="gabby"
curl ... -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"I go by Gabby"}],...}'

# Step 2: Gabriella ingested
curl ... -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"We have a third daughter Gabriella who is 10 and goes by Gabby"}],...}'

# Step 3: Collision detected in entity_name_conflicts table (SSH verify)
ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c 'SELECT * FROM entity_name_conflicts WHERE status=\"pending\";'"

# Step 4: Wait 5s for re-embedder cycle (conflict resolution)

# Step 5: Query Gabriella
curl ... -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"Tell me about Gabriella"}],...}'

# Expected: Response mentions Gabriella with her NAME, not UUID. Parent relationship returned.
# Verify: SSH query confirms conflict marked "resolved" with winner/fallback assigned
```

### Scenario 3–5: Other tests
Similar pattern: ingest → wait for pipeline → query → validate results mention entity names (not UUIDs).

## Success Criteria

- All 5 scenarios execute without error ✓
- OpenWebUI Filter acknowledges all ingests ✓
- Facts stored in PostgreSQL ✓
- Gabriella collision detected + resolved ✓
- Query results use preferred names only (no UUIDs, no missing entities) ✓
- All documented in scratch with curl commands + responses ✓

## Key Observations to Document

1. **Ingest response:** Did Filter LLM extract all relationships correctly?
2. **Database state:** PostgreSQL contains expected facts after ingest?
3. **Collision detection:** entity_name_conflicts table populated when collision occurs?
4. **Re-embedder resolution:** Was collision marked resolved? Winner/loser assigned correctly?
5. **Query results:** Do names appear (not UUIDs)? Are all entities returned? Any sensitive facts leaked?

