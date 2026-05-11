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

## ✓ DONE: dprompt-cleanup-001 (Documentation Commit + Cleanup Scope) — 2026-05-12

**Commit:** `9309fce` — 11 files (3 dprompt specs, 3 bug reports, 2 archives, scratch)
- dBug-report-004 created: stale `user owns morkie` cleanup scoped as P3 for retraction flow enhancement
- Git status clean, all docs archived

**All issues closed.** No pending work. Awaiting direction.

---

## ✓ DONE: dprompt-59 (Conflict Detection & Auto-Superseding) — 2026-05-12

**Implementation:** Added `_detect_semantic_conflicts()` to `src/api/main.py`. Runs before Class A/B/C commit in `/ingest` pipeline.

### How it works

1. When a new fact arrives, checks if the `object` entity already appears as object of a hierarchy relationship (`instance_of`, `subclass_of`, `is_a`, `member_of`, `part_of`)
2. If the object IS a type/category/component, and the new fact's rel_type is a leaf-only rel (`owns`, `has_pet`, `works_for`, `lives_in`, `lives_at`), the new fact is auto-superseded
3. The existing hierarchy relationship is preserved — the graph stays semantically valid

### Example

- `fraggle instance_of morkie` exists → `user owns morkie` is auto-superseded with reason: "type_conflict: morkie is object of instance_of"
- `alice instance_of engineer` exists → `alice works_for engineer` is auto-superseded

### Validation

- Syntax: clean, lines: 3580 → 3674 (+94 lines)
- Tests: 114 passed, 0 regressions

**Deployment needed:** Rebuild faultline backend container on truenas.
