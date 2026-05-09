# Deepseek Implementation Prompt: End-to-End Validation & Regression Testing

**Scope:** Verify the three-tier retrieval fix works end-to-end. Test that facts (especially Class B behavioral like `has_pet`) are now recalled correctly. Identify any regressions.

**Why:** We fixed the Filter's relevance scoring, but the real test is: does it work in practice? Does the Fraggle/Mars fact get recalled in a new chat now?

---

## Test plan

You don't need to code. Just validate manually against these cases:

### Case 1: Fraggle recall (the original problem)
- Chat 1: Tell the system "Mars has a pet dog named Fraggle, Fraggle is a Morkie"
  - Expected: System acknowledges, ingest succeeds
  - Verify: Check that `has_pet` fact is in database (should be in `facts` or `staged_facts`)
- Chat 2: New chat, same user, say "Hey, what's up?"
  - Expected: Memory injection includes Mars + Fraggle facts (or at least identity facts)
- Chat 3: New chat, ask "Do I have any pets?"
  - Expected: Fraggle fact is returned and injected

### Case 2: Generic query (Tier 2 fallback)
- Say: "hello"
- Expected: Identity facts injected (name, spouse, children) — enough context for LLM
- NOT expected: All Class B behavioral facts dumped

### Case 3: Entity query (Tier 1)
- Say: "How's Cyrus?"
- Expected: All facts about Cyrus returned (parent_of, sibling_of, age if stored, etc.)
- Verify: Facts display with correct names ("cyrus", not UUIDs)

### Case 4: Location/temporal query (Tier 3 fallback)
- Say: "Where do I live?"
- Expected: Location facts returned via keyword scoring (should still work)
- Say: "When was I born?"
- Expected: `born_on` facts returned via keyword scoring

### Case 5: Spouse + children context
- Say: "Tell me about my family"
- Expected: Spouse, children, sibling rels injected
- Verify: Names are display names, not UUIDs

### Case 6: No regression on existing behavior
- Try queries that worked before (weather, work info, etc.)
- Verify: No silent failures, no missing facts that were previously recalled

---

## Validation checklist

For each test case:
- [ ] Query runs without errors
- [ ] Memory block injects (or doesn't, as expected)
- [ ] Display names are correct (mars, not UUID)
- [ ] Fact counts are reasonable (not dumping 100+ facts)
- [ ] LLM can reference facts in response

---

## If something breaks

1. **Note the case** — which query, what was expected, what happened
2. **Check the logs** — any errors from the Filter?
3. **Verify `/query` endpoint** — does it return the fact correctly? (curl test if needed)
4. **Check `_filter_relevant_facts()` logic** — is Tier 1/2/3 being hit as expected?
5. **Report back** — don't try to fix it yourself; describe the failure and we'll iterate

---

## Success criteria

- ✅ Fraggle fact recalled in new chat (the original problem is fixed)
- ✅ Generic queries return identity facts only (no dumping)
- ✅ Entity queries return all facts for that entity
- ✅ Topic queries (location, temporal) still work via Tier 3
- ✅ No regressions on previously working queries
- ✅ No errors in Filter logs

---

## Timeline

- Test all 6 cases
- Note any failures with exact query/response
- Report findings

This is validation, not implementation. If it all works, we move to the next phase (relation resolution for "my wife" patterns). If not, we debug and iterate.

**No code changes in this phase.**

---

## HARD CONSTRAINTS (live environment)

**YOU CANNOT:**
- `docker restart`, `docker stop`, `docker kill` any container
- `docker-compose up/down/restart/rebuild` — FORBIDDEN
- Modify any config files (docker-compose.yml, environment, etc.)
- Delete or truncate database tables
- Force-push to git or rewrite history
- Change any file in the codebase

**YOU CAN:**
- `ssh truenas` and run read-only commands:
  - `curl` to test endpoints (localhost:8001/query, etc.)
  - `docker exec faultline-postgres psql` — query the database (SELECT only)
  - `docker logs faultline --tail 100` — check logs for errors
  - `grep` through logs
- Create test cases in a comment/scratch file (don't commit)
- Report findings clearly

**If the container crashes or logs show errors:**
- Do NOT restart it
- Document the error + query that caused it
- Report back — we'll investigate

**If code needs changes:**
- Do NOT modify the live code
- Report what failed and why
- We'll iterate on the prompt and redeploy via proper channels

This is validation, not remediation. Stay in read-only mode except for testing via curl.

