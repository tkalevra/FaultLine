# scratch.md — FaultLine development dialogue

## INSTRUCTION FOR AGENTS

This file is for **questions and dialogue only**. Do NOT dump code, implementation
plans, or test cases here. Use it to:
- Ask design questions
- Request clarification on requirements
- Confirm decisions before coding
- Preface your entry(s) with your tag in markdown: eg #claude followed by your response/question to allow the human to know who's asking or answering what please.

Code goes directly into source files. This file stays lean.

## Pre-Prod Reference (2026-05-12)

**Instance:** hairbrush.helpdeskpro.ca (truenas)
**Model:** faultline-wgm-test-10
**Bearer token:** sk-addb2220bf534bfaa8f78d96e6991989 (homelab, minimal risk)

**SSH for container logs:**
```bash
ssh truenas -x "sudo docker logs faultline --tail N"           # FaultLine backend
ssh truenas -x "sudo docker logs faultline-postgres --tail N"   # PostgreSQL
ssh truenas -x "sudo docker logs open-webui --tail N"           # OpenWebUI (Filter runs here)
```

**Curl testing (via chat API):**
```bash
curl -s -H "Authorization: Bearer sk-addb2220bf534bfaa8f78d96e6991989" \
  -H "Content-Type: application/json" \
  -d '{"model": "faultline-wgm-test-10", "messages": [{"role": "user", "content": "TEXT"}], "stream": false}' \
  https://hairbrush.helpdeskpro.ca/api/chat/completions
```

**LENGTH RULE:** If this file exceeds 150 lines, archive everything between the
"## Archive" section and the "---" separator as `scratch-archive-YYYY-MM-DD.md`,
then condense the remaining content to a concise current state summary. The
instruction header stays; only the dialogue and state sections get archived.

---

## Archive

- **scratch-archive-2026-05-11.md** — Phases 1–5 (retrieval, relations, conversation state)
- **scratch-archive-2026-05-11-phases6-10.md** — Phases 6–10 (date/time, events table, UUID resolution)
- **scratch-archive-2026-05-11-dprompt15b.md** — dprompt-15b full-circle validation
- **scratch-archive-2026-05-11-dprompt16-17.md** — dprompt-16/17: preference chain, ontology
- **scratch-archive-2026-05-12.md** — May 12 cycle: dprompts 20–56, production deploys v1.0.0→v1.0.2
- **scratch-archive-2026-05-12b.md** — May 12 continuation: hierarchy extraction, dBug-003, dprompt-58

---

## Current State (2026-05-12 evening)

### Production (GitHub: tkalevra/FaultLine)
- **v1.0.2** — Hierarchy extraction enhancement (dprompt-56b deployed)
- **v1.0.1** — Filter simplification, backend-first architecture
- **v1.0.0** — Initial release

### Active Issues

| Issue | Status | Next |
|-------|--------|------|
| dBug-report-001 | Fixed (dprompt-53b) | ✓ Closed |
| dBug-report-002 | Fixed (dprompt-56b) | ✓ Closed |
| dBug-report-003 | Fixed (dprompt-58) | ✓ Closed (prompt constraint added) |

### Pending Work

None. All active issues resolved.

**Data cleanup (pinned):** Pre-prod has one stale `user owns morkie` fact from before dprompt-58. Needs manual supersede or retraction.

---

## ✓ DONE: dprompt-58 (Hierarchy Extraction Constraint) — 2026-05-12

**Fix:** Added HIERARCHY CONSTRAINT to `_TRIPLE_SYSTEM_PROMPT`. When `instance_of`/`subclass_of`/`member_of`/`part_of` is extracted, the object is a type/category — NOT a separate entity. LLM instructed to not also extract `owns`/`has_pet`/`works_for`/`lives_in` for the type entity.

**Example:** "I have a dog named Fraggle, a morkie" → extract `fraggle instance_of morkie` + `user has_pet fraggle`, but NOT `user owns morkie`.

**Cross-domain:** Same constraint for engineer (role), Ontario (province), subnet (container), monitoring (component).

**Validation:** Syntax clean, 114 passed, 0 regressions.

**Deployment:** User rebuild of OpenWebUI container needed. Pre-prod has one stale `user owns morkie` fact to clean.

---

## #deepseek NEXT: dprompt-cleanup-001 (Documentation Commit + Data Cleanup Scope)

**Status:** Ready for execution

**Context:** dprompt-58 (extraction constraint) completed and code-committed. Remaining: commit documentation files and scope the stale data cleanup issue.

**Task:**
1. Stage and commit dprompt-56/57/58 specs, bug reports, archive files
2. Create dBug-report-004 documenting stale `user owns morkie` cleanup need
3. Scope cleanup as P3 issue for future retraction flow enhancement

**Execution:** Follow dprompt-cleanup-001.md sequence. One commit, two purposes (docs + scope). STOP with completion report.

Stand by.
