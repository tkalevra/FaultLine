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

**Full dialogue history:**
- **scratch-archive-2026-05-11-phases6-10.md** — Phases 6–10 (date/time → events table → UUID resolution)
- **scratch-archive-2026-05-11.md** — Phases 1–5 (retrieval, relations, conversation state)

---

## Current Session (Phase 11+)

Standing by for dprompt-9 Part C re-validation (UUID leakage fix live).

Next: Validate display names in events, then move to systematic test coverage (NEXT_STEPS.md #1).

---

# deepseek

**dprompt-10 fix was broken on deploy.** The `_resolve_display_names()` in `main.py` expects `(facts, registry, user_id, entity_id)` — but the sed used `(events, preferred_names, identity)`. Dict passed as registry → `AttributeError: get_preferred_name` on every /query call.

**Fixed locally** — all 3 merge points now use `(events, registry, user_id, user_entity_id_for_query)`. main.py parses clean.

**Needs redeploy.**
