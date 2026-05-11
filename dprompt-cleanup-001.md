# dprompt-cleanup-001: Commit Documentation + Scope Data Cleanup Issue

**Date:** 2026-05-12  
**Status:** Ready for execution  
**Scope:** Documentation commit + bug scope (no code changes)

## Task

Commit dprompt specifications, bug reports, and archives to git. Then scope a follow-up bug for programmatic data cleanup via the retraction flow.

## Context

dprompt-58 (hierarchy extraction constraint) is complete and committed. Remaining work:
1. **Commit:** Stage and commit all dprompt-56/57/58 specs, bug reports, archive files
2. **Scope:** Document the pre-prod stale data cleanup issue for future retraction enhancement

The dprompt cycle discovered that extraction ambiguity (not query logic) caused bad facts. dprompt-58 fixed extraction going forward. But pre-prod has one stale fact (`user owns morkie`) that should be cleaned programmatically via retraction flow, not manually.

## Constraints

**MUST:**
- Commit all untracked spec and bug files to master branch
- Write comprehensive commit message explaining the hierarchy extraction cycle (dprompt-56/57/58)
- Create dBug-report-004 documenting the stale data cleanup need
- Reference dprompt-58 completion in the bug report
- All files in FaultLine-dev repo only

**DO NOT:**
- Commit to faultline-prod
- Manually clean pre-prod database
- Include uncommitted code changes (only specs)

**MAY:**
- Consolidate commit message for clarity
- Add notes about future retraction enhancement scope

## Sequence

### 1. Verify Untracked Files

```bash
git status --short
```

Expected: dprompt-56.md, dprompt-57.md, dprompt-58.md, BUGS/dBug-report-002.md, BUGS/dBug-report-003.md, scratch.md, scratch-archive-2026-05-12*.md

### 2. Stage Files

```bash
git add dprompt-56.md dprompt-57.md dprompt-58.md \
         BUGS/dBug-report-002.md BUGS/dBug-report-003.md \
         scratch.md scratch-archive-2026-05-12*.md
```

### 3. Create Commit Message

Scope: dprompt-56/57/58 cycle (hierarchy extraction investigation → extraction constraint → data cleanup scope)

Message should cover:
- dprompt-56: hierarchy extraction enhancement (multi-domain)
- dprompt-57: query logic investigation (found root cause was extraction, not query)
- dprompt-58: extraction constraint (prevent ownership facts for type entities)
- Bug reports: dBug-002 (investigation), dBug-003 (root cause)
- Archive: condensed scratch, pinned data cleanup for future

### 4. Create dBug-report-004

File: `BUGS/dBug-report-004.md`

**Purpose:** Document stale data cleanup issue and scope for retraction flow enhancement

**Contents:**
- Symptom: Pre-prod has one stale fact `user owns morkie` from before dprompt-58
- Root cause: dprompt-56/57/58 identified extraction ambiguity; dprompt-58 fixed going forward
- Issue: Stale fact needs cleanup, but shouldn't be manual (programmatic flow is better)
- Recommendation: Enhance retraction/correction flow to auto-supersede conflicting facts
- Example: When user corrects "No, Fraggle is just one dog" → system detects and supersedes `user owns morkie`
- Status: P3 (low priority — data is correct going forward, only cleanup is pending)
- Next phase: dprompt-X for retraction enhancement

### 5. Commit Both (one commit, two files)

```bash
git add BUGS/dBug-report-004.md
git commit -m "[comprehensive message covering dprompt-56/57/58 + bug-004]"
```

### 6. STOP & Report

- Update scratch.md: note completion, point to bug report for cleanup tracking
- Report: files committed, bug scoped, ready for user rebuild

## Deliverable

**Commits:**
1. Main commit: dprompt specs + bug reports + archive
2. Same commit: dBug-report-004 (stale data cleanup scope)

**Files committed:**
- dprompt-56.md (hierarchy extraction spec)
- dprompt-57.md (query investigation spec)
- dprompt-58.md (extraction constraint spec)
- BUGS/dBug-report-002.md (investigation findings)
- BUGS/dBug-report-003.md (root cause: extraction ambiguity)
- BUGS/dBug-report-004.md (stale data cleanup scope)
- scratch.md (condensed)
- scratch-archive-2026-05-12*.md (archived work)

## Success Criteria

✅ All untracked files staged  
✅ Commit message comprehensive (covers full dprompt-56/57/58 cycle)  
✅ dBug-report-004.md created (cleanup issue documented)  
✅ Cleanup marked as P3, scoped for retraction enhancement  
✅ Git status clean after commit  
✅ All files on master branch  

## Upon Completion

```markdown
## ✓ DONE: dprompt-cleanup-001 (Documentation Commit + Cleanup Scope) — [DATE]

**Task:** Commit dprompt-56/57/58 specs + bug reports. Scope data cleanup issue.

**Changes:**
- Committed: 8 files (3 dprompt specs, 3 bug reports, 2 archive files)
- Created: dBug-report-004.md (cleanup issue scope)
- Cleaned: git status now clean

**Commit:**
- Hash: [commit SHA]
- Message: [summary of hierarchy extraction cycle]
- Files: dprompt-56/57/58, BUGS/dBug-002/003/004, scratch, archives

**Bug scoped:**
- dBug-report-004: Stale `user owns morkie` fact cleanup
- Status: P3, scoped for retraction flow enhancement
- Data: correct going forward (dprompt-58 prevents new bad facts)

**STOP — awaiting user next direction.**
```

Then STOP.
