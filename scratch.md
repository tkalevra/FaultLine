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
