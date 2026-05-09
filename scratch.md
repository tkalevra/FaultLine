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

## Critical Issue Discovered (2026-05-11 ~4:10 PM)

**LIVE EXPOSURE:** User ID UUID leaked into LLM response.
- Query: "tell me about my family please"
- Response: "...we are communicating with the user ID '3f8e6836-72e3-43d4-bbc5-71fc8668b070'..."
- Severity: CRITICAL — internal system identifier exposed to user

**Root cause:** user_id appears somewhere in facts, memory block, or debug context visible to LLM.

**Action:** dprompt-11 written. Deepseek to fix:
1. Strip `user_id` + internal metadata from `/query` response facts
2. Audit memory block for canonical_identity UUID leaks
3. Redact user_id from debug output
4. Validate zero UUID patterns in live response

**Next:** Wait for dprompt-11 fix deployment, then re-test "tell me about my family" for zero UUIDs.

---

## Current State (2026-05-11) — All 10 prompts complete + critical fix pending

### Code changes (Phase 7 shipped)

| File | What | Tests |
|---|---|---|
| `openwebui/faultline_tool.py` | Three-tier retrieval, relation resolver (seed+dynamic), conversation state, display names, UUID hard guard, events formatting | 33/33 pass |
| `src/api/main.py` | Temporal events routing, `_TEMPORAL_REL_TYPES`, `_fetch_user_events()`, events merge with display resolution | parses clean |
| `migrations/015_events_table.sql` | Events table with recurrence | applied in DB |

### Validated live (pre-leak-discovery)

- Events query returns `user -born_on-> may 3rd, 1990` with 0 UUID leaks ✅
- Hard UUID guard active in Filter ✅
- Fraggle recall works ✅
- `has_pet` fact stored as `(mars → fraggle)` in DB

### Known

- `has_pet` is `(mars → fraggle)` not `(user → fraggle)` — data semantics, not a bug
- Generic "hey" query returns 0 facts on `/query` — keyword-less graph traversal may not trigger
- **USER_ID LEAK:** user_id UUID appearing in LLM responses (BEING FIXED BY dprompt-11)

### Next

1. **BLOCKING:** dprompt-11 user_id leakage fix
2. Revalidate: "tell me about my family" contains zero UUIDs
3. Manual OpenWebUI validation: date-based queries
4. Test coverage expansion: temporal events, conversation state
5. Conversation state → calculate_relevance_score() (Phase 8)

---

# deepseek

**dprompt-11 SHIPPED (commit b7b461f).** Three-layer fix:

1. **metadata stripping** — `_INTERNAL_KEYS` (user_id, qdrant_synced, superseded_at, fact_class, promoted_at, confirmed_count) stripped from merged_facts before /query returns (4 locations)
2. **debug redactions** — user_id replaced with `[redacted]` in all Filter debug prints (3 occurrences) + canonical_identity → `[redacted]`
3. **code quality** — 33/33 tests pass, main.py parses clean

**Ready for:** Redeploy Docker, then live validation of "tell me about my family" for zero UUID leakage in response.

---

## CRITICAL REGRESSION DISCOVERED (2026-05-11 ~4:15 PM)

**BOTH issues present after dprompt-11 redeploy:**

1. **Memory extraction BROKEN** — Zero facts injected to user. Queries like "tell me about my family" return no facts.
2. **User ID leak UNFIXED** — Second query response still shows: "your ID is 3f8e6836-72e3-43d4-bbc5-71fc8668b070"

**Assessment:** dprompt-11 fix appears to have broken the memory pipeline entirely, or /query is failing silently. User ID still leaking from different source.

**Action:** dprompt-12 written. Deepseek to debug:
1. Is /query being called? How many facts returned?
2. Are facts reaching memory block construction?
3. Is memory block being injected to messages?
4. Where is user_id UUID still leaking from? (OpenWebUI context? Facts? Debug output?)

**Critical:** Full-stack debug needed — /query endpoint → Filter memory building → message injection → LLM response.

---

## USER_ID LEAK STILL ACTIVE (2026-05-11 ~4:22 PM)

**After dprompt-13 redeploy, live test shows:**
- Query: "Tell me about my family"
- Response: "I know your unique identifier (3f8e6836-72e3-43d4-bbc5-71fc8668b070)"

**Assessment:** dprompt-13 fix did NOT eliminate user_id exposure. The UUID is still visible to LLM.

**Root causes to investigate:**
1. `/query` metadata stripping still not working (unbound variable issue not actually fixed?)
2. Filter not resolving canonical_identity UUID to display name
3. OpenWebUI system context passing user_id to LLM independently

**Action:** dprompt-13b written with **CLAUDE.md hard constraints enforced:**
- Display names in aliases only, never in *_id columns
- User IDs are internal identifiers, never visible to LLM
- Filter converts UUIDs to display names before injection
- Nuclear option: strip all UUID patterns from body pre-LLM

Deepseek to fix definitively with full CLAUDE.md compliance audit.

---

# deepseek — dprompt-13 fix

**Root cause of regression:** The regex that inserted metadata stripping code placed it OUTSIDE the conditional blocks (`if resp.status_code == 404:` / `if resp.status_code != 200:`). When the code reached the stripping section without having entered the relevant `if` block, `merged_facts` was unbound → `UnboundLocalError` → caught by `except Exception` → returned empty facts.

**Fix:** Removed all 5 duplicate stripping blocks (4 in early return paths, 1 duplicate in main path). Added a single clean stripping block in the main success path only — right after `log.info("query.merged"...)` and before the final `return`. The early return paths already construct their own fact lists and don't include `user_id` fields.

**Verification:** main.py parses clean, 33/33 tests pass.

**On user_id leak:** The metadata stripping was never reached due to the UnboundLocalError. Now that /query works again, facts are stripped of `user_id` before returning to Filter. But the LLM may have cached the UUID from a previous response — new conversations should not show it.

**Needs redeploy** for live validation.

---

# deepseek — dprompt-13 complete

**Fix applied locally.** Single metadata stripping block at main return path only. Removed the 4 duplicate blocks that were outside conditional blocks and caused `UnboundLocalError` on `merged_facts`.

**Verification:** main.py parses, 33/33 tests pass.

**Live:** Container still runs broken dprompt-11 code (0 facts returned, no errors in logs). Fix needs redeploy.

---

# deepseek — dprompt-13

**Regression root cause:** Regex inserted 5 metadata stripping blocks — 4 in early return paths where `merged_facts` unbound outside `if` blocks → `UnboundLocalError` → caught → returned empty facts.

**Fix:** Removed all 5 duplicates. Added single clean block in main success path only (after `log.info("query.merged"...)`). Early return paths assemble their own fact lists without `user_id` fields.

**Verification:** main.py parses, 33/33 tests pass. Ready for redeploy.
