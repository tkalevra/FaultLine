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
ssh truenas -x "sudo docker logs faultline --tail N"
ssh truenas -x "sudo docker logs open-webui --tail N"
```

**Curl testing:**
```bash
curl -s -H "Authorization: Bearer sk-addb2220bf534bfaa8f78d96e6991989" \
  -H "Content-Type: application/json" \
  -d '{"model": "faultline-wgm-test-10", "messages": [{"role": "user", "content": "TEXT"}], "stream": false}' \
  https://hairbrush.helpdeskpro.ca/api/chat/completions
```

**LENGTH RULE:** If this file exceeds 150 lines, archive everything between the
"## Archive" section and the "---" separator as `scratch-archive-YYYY-MM-DD.md`,
then condense the remaining content to a concise current state summary.

---

## Archive

- **scratch-archive-2026-05-11.md** — Phases 1–5
- **scratch-archive-2026-05-11-phases6-10.md** — Phases 6–10
- **scratch-archive-2026-05-11-dprompt15b.md** — dprompt-15b
- **scratch-archive-2026-05-11-dprompt16-17.md** — dprompt-16/17
- **scratch-archive-2026-05-12.md** — May 12 cycle: dprompts 20–56, v1.0.0→v1.0.2
- **scratch-archive-2026-05-12b.md** — May 12b: hierarchy extraction, dBug-003, dprompt-58
- **scratch-archive-2026-05-13.md** — May 13: dprompts 59–61, v1.0.3→v1.0.4

---

## Current State (2026-05-13)

### Production (GitHub: tkalevra/FaultLine)
- **v1.0.5** — Bidirectional relationship validation (dprompt-62)
- **v1.0.4** — Query deduplication + alias metadata (dprompt-61)
- **v1.0.3** — Semantic conflict detection (dprompt-59)
- **v1.0.2** — Hierarchy extraction enhancement (dprompt-56b)
- **v1.0.1** — Filter simplification, backend-first (dprompt-53b)
- **v1.0.0** — Initial release

### Architecture

- **Filter:** Dumb — trusts backend /query ranking. Identity rels always pass, confidence threshold on rest.
- **Backend /query:** Smart — graph traversal, hierarchy expansion, taxonomy filtering, entity dedup + alias metadata.
- **Ingest:** LLM-first extraction → WGM gate → semantic conflict detection → bidirectional validation → fact classification (A/B/C).
- **Self-healing:** Semantic conflicts auto-superseded at ingest. Impossible bidirectional relationships prevented. Graph stays valid.

### Active Issues

| Bug | Status |
|-----|--------|
| dBug-report-001 | Fixed (dprompt-53b) |
| dBug-report-002 | Fixed (dprompt-56b) |
| dBug-report-003 | Fixed (dprompt-58) |
| dBug-report-004 | P3 — stale data cleanup scoped |
| dBug-report-005 | Fixed (dprompt-61) |
| dBug-report-006 | Fixed (dprompt-62) — pre-prod cleaned + validation deployed |

### Dev repo

- Branch: `master` (commit `e5c7a60`)
- Test suite: 114 passed, 53 skipped
- Lines: `src/api/main.py` 3992

---


## ✓ DONE: dprompt-62 (Staged Fact Validation + Bidirectional Rules) — 2026-05-13

**Implementation:** `src/api/main.py`

### Changes

1. `_detect_semantic_conflicts()` already checks both `facts` and `staged_facts` (verified from dprompt-59) ✓
2. `_validate_bidirectional_relationships()` — new function (97 lines)
   - Prevents `child_of` + `parent_of` coexistence for same entity pair
   - Keeps higher-confidence version, supersedes lower
   - Checks both `facts` and `staged_facts` tables
3. Integrated into `/ingest` pipeline — runs after conflict detection, before Class A/B/C assignment

### Pipeline order

```
extract → WGM gate → _detect_semantic_conflicts → _validate_bidirectional_relationships → Class A/B/C → commit
```

### Validation

- Syntax: clean ✓
- Tests: 114 passed, 0 regressions ✓
- Lines: 3839 → 3992 (+153 lines)
- Deployed: v1.0.5 (`ec75cf4`) ✓


## 🐛 CRITICAL: dBug-report-007 (Bidirectional Validation Failed + UUID Exposure)

#claude: Reviewed pre-prod OpenWebUI logs post-dprompt-62. Found **genuine bugs**:

### Issues Found
1. **Bidirectional validation incomplete:** `user -child_of-> gabby` still in DB (dprompt-62 didn't catch it)
2. **UUID exposure:** "7E4Bff75-706E-5Feb-B8B5-F4Ca1247Fd3B is species: morkie mix" (should be display names)
3. **dprompt-62 bidirectional logic has bug:** Not catching child_of + parent_of coexistence

### Database Cleanup (Done)
```
DELETE 1 impossible child_of fact (user -child_of-> gabby)
```

### Status
- dBug-report-007.md written + committed
- Database cleaned (impossible relationship removed)
- Ready for user retest

**Next:** dprompt-63 (fix bidirectional validation logic) + dprompt-64 (UUID resolution in query response).

---

## #deepseek: WAIT FOR USER RETEST + NEXT DIRECTION

#claude: Post-cleanup, waiting for user to test family query again. Pre-prod database has been cleaned (impossible child_of removed).

Two bugs remain unfixed in code:
1. dprompt-62 bidirectional validation logic incomplete
2. Query response UUID resolution broken

After user retests and reports results, next steps will be:
- dprompt-63: Fix bidirectional validation in _validate_bidirectional_relationships()
- dprompt-64: Fix UUID resolution in /query response building

STOP and wait for user direction.


## Correction: Cyrus works_for → educated_at (User Feedback)

#claude: User corrected extraction: Cyrus "studies at" (educated_at) not "works for" (works_for) University of Guelph.

**Database cleanup:**
- Deleted: cyrus -works_for-> university_of_guelph (staged_facts, conf=0.8)
- Retained: cyrus -educated_at-> university_of_guelph (staged_facts, conf=0.8)

Note: Both facts in staged_facts (unconfirmed Class B). Should have been caught by conflict detection (similar to dBug-006/007 pattern).

Ready for user retest.

