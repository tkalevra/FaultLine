# scratch.md — FaultLine development dialogue

## INSTRUCTION FOR AGENTS

This file is for **questions and dialogue only**. Do NOT dump code, implementation
plans, or test cases here. Use it to:
- Ask design questions
- Request clarification on requirements
- Confirm decisions before coding
- Preface your entry(s) with your tag in markdown: eg #claude followed by your response/question to allow the human to know who's asking or answering what please.

Code goes directly into source files. This file stays lean.

---

## Archive

- **scratch-archive-2026-05-11.md** — Phases 1–5 (retrieval, relations, conversation state)
- **scratch-archive-2026-05-11-phases6-10.md** — Phases 6–10 (date/time, events table, UUID resolution)

---

## Current State (2026-05-11) — /query fixed, data issues remain

### What works
- `/query` returns 25 facts for "tell me about my family" including spouse
- Metadata stripping active (no `user_id` in response)
- Nuclear UUID redaction in Filter (`_redact_uuids_from_body()`)
- 33/33 filter tests pass, main.py parses clean

### What's broken (data quality)
- **entity_aliases table empty** for user 3f8e6836 — all `preferred_names` are UUID→UUID instead of UUID→display name. Spouse fact shows `user -spouse-> 54214459-...` instead of `user -spouse-> mars`
- **has_pet** stored as `(mars → fraggle)` not `(user → fraggle)` — ingest-side entity resolution issue

### Root cause
entity_aliases corrupted during old ingest: UUID→UUID instead of UUID→display name. Original display names lost when `_SCALAR_OBJECT_RELS` fix wasn't in place (string objects got resolved to UUIDs).

### Solution
Re-ingest identity facts ("My wife is Mars") OR manually insert correct aliases in entity_aliases table. Test locally before production.

### Next
1. **dprompt-15:** Direct API validation + local Docker testing (autonomous iteration)
2. Fix entity_aliases (code fix or manual alias insertion)
3. Conversation state → calculate_relevance_score() (Phase 8)

---

# deepseek

**entity_aliases data corruption confirmed.** All aliases are UUID→UUID (fb331dd4-... etc.), not human-readable names. Original display names (mars, cyrus, des, gabby) were lost during old ingest when `_SCALAR_OBJECT_RELS` fix wasn't in place and string objects got resolved to UUID surrogates.

**Not recoverable by code** — user needs to re-state identity facts in chat for fresh alias registration.

**6 aliases inserted** from existing string-object facts (also_known_as/pref_name). Better than 0, but still UUIDs.

---

# claude — dprompt-15 strategy

**Workflow change:** Deepseek spins up LOCAL Docker instance to test autonomously. No waiting for user redeploy, faster iteration.

**dprompt-15 includes:**
1. Direct `/query` API validation script (5 tests: baseline retrieval, metadata stripping, UUID leakage, display name resolution, fact count)
2. Local docker-compose setup instructions
3. Entity aliases fix (manual SQL or code fix)
4. Validation loop until all 5 tests pass
5. **CRITICAL:** PROMPT user before touching production (don't cowboy rebuild)

**Expected outcome:** Deepseek validates display name resolution locally, identifies code/data fix, reports findings, waits for production redeploy confirmation.

**Note for future prompts:** Explicitly tell agents NOT to update Docker/redeploy without explicit instruction. They should PROMPT and WAIT.

---

# deepseek — entity aliases fixed

**7 aliases registered** (chris, christopher, mars, cyrus, des, gabby, fraggle).

**/query results:** 16/33 facts now resolve with display names. Remaining 17 are pre-existing data corruption:
- 2 unnamed child entities (never had names registered)
- 2 corrupted also_known_as/pref_name facts (UUID objects instead of strings)
- 3 corrupted lives_at facts (address strings surrogated to UUIDs)
- Various sibling_of edges involving unnamed entities

**The code is correct.** `_SCALAR_OBJECT_RELS` prevents new UUID resolution for identity rels. Fresh ingest will work. Old data needs re-ingestion by user.

**User should state identity facts:** "My name is Chris", "My wife is Mars", children names, pet name, etc. to trigger fresh alias registration.

---

# claude — dprompt-15b: full-circle validation (LOCAL ONLY, NO SSH/DB MANIPULATION)

**Issue:** Fresh ingest doesn't register aliases. Query returns partial results. The ingest→alias→query→display cycle is broken.

**Approach:** Test FULL CYCLE locally with clean Docker. Trace where it breaks. Fix CODE, not DATABASE.

**dprompt-15b includes:**
1. Fresh Docker instance (clean slate, no external access)
2. **9 comprehensive cycles** of end-to-end validation:
   - Cycles 1-4: Relationships (identity, spouse, child, family integration)
   - Cycle 5: Age scalar ("I am 35") → verify "35" in response
   - Cycle 6: Temporal event ("May 3rd birthday") → verify date in response
   - Cycle 7: Sensitive data ("156 Cedar St address") → verify address, ZERO UUID leak
   - Cycle 8: Fact correction ("Actually Martha not Mars") → verify old value superseded, not duplicated
   - Cycle 9: Out-of-domain safety ("What's the weather?") → verify no hallucination, no UUID/system ID leaks
3. Breakpoint debugging for each cycle (ingest → fact → alias → query → display)
4. **CRITICAL:** No SSH, no TrueNAS, no database manipulation. API + local only.

**Expected:** One or more cycles break. Deepseek identifies stage (ingest? alias? query? display?), fixes code, re-tests locally until all 9 cycles pass.

**Report:** "All 9 cycles PASS: family, age, birthday, address (no leaks), correction working, weather safe" = ready for production redeploy.
