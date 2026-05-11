# dprompt-59b: DEEPSEEK_INSTRUCTION_TEMPLATE — Deploy Conflict Detection to Production

## Task

Deploy dprompt-59 (conflict detection & auto-superseding) from FaultLine-dev to faultline-prod using the standard production deployment flow.

## Context

dprompt-59 adds semantic conflict detection to the ingest pipeline. When new facts contradict existing graph structure (e.g., `fraggle instance_of morkie` exists, new fact tries to extract `user owns morkie`), the system now auto-supersedes the conflicting fact.

**Why it matters:** Keeps graph semantically valid. Works alongside dprompt-58 extraction constraint (prevent bad facts) + retraction flow (explicit user corrections). Three-layer defense for data quality.

**Files changed:** `src/api/main.py` — added `_detect_semantic_conflicts()` function (~94 lines) that runs before fact class assignment in `/ingest` pipeline.

**Read first:** `dprompt-59.md` (specification), `PRODUCTION_DEPLOYMENT_GUIDE.md` (SOP), `CLAUDE.md` (semantic principles).

## Constraints

**MUST:**
- Follow PRODUCTION_DEPLOYMENT_GUIDE.md exactly (identify files, audit secrets, copy, sanitize, validate, commit/tag/push)
- Copy only changed files from FaultLine-dev (no refactoring)
- Audit for: bearer tokens, personal names, server IPs, emails
- Sanitize: any hardcoded IPs → localhost
- Validate: `docker compose config` clean, `python -m py_compile` clean
- Update ABOUT.md (v1.0.3 release notes) and CHANGELOG.md
- Commit message: clear, references dprompt-59
- Tag: v1.0.3 (semantic versioning: feature addition)
- Push: origin/main + --tags
- Environment isolation: investigation pre-prod only (read-only), code FaultLine-dev only, deployment user-triggered (wait for STOP)
- Maintain all existing tests (114+ pass, 0 new failures)

**DO NOT:**
- Commit to faultline-prod repo yourself (only copy/sanitize/update docs)
- Modify code beyond what dprompt-59 changed
- Deploy to pre-prod yourself (wait for user)
- Skip validation steps
- Revert dprompt-58 or earlier changes

**MAY:**
- Add notes about conflict detection in ABOUT.md/CHANGELOG.md
- Include examples in release notes (fraggle/morkie, alice/engineer)
- Reference dBug-003/004 in commit message for context

## Sequence

**CRITICAL: Follow this sequence exactly. Do not skip steps.**

### 1. Read & Understand (No coding)
- Read `dprompt-59.md` (specification, examples, scope)
- Read `PRODUCTION_DEPLOYMENT_GUIDE.md` (deployment SOP, checklist)
- Read `CLAUDE.md` section on semantic principles + ingest pipeline
- Confirm: what changed from dprompt-59 implementation?

### 2. Investigate (Pre-Prod Only — Read-Only)

**Pre-prod sanity check:**
```bash
ssh truenas -x "sudo docker logs faultline --tail 100 | grep -i 'conflict\|supersed\|detect'"
```

Document: Is conflict detection running? Any errors in logs?

**Pre-prod test (optional):**
- If system online, test: "I have a dog named Fraggle, a morkie" 
- Verify: no `user owns morkie` fact created (conflict detection working)
- If offline, skip to next section

### 3. Analyze Local Code (FaultLine-dev)

**Identify changed file:**
- `src/api/main.py` — locate `_detect_semantic_conflicts()` function
- Verify: added before Class A/B/C assignment in `/ingest`
- Check: no other files modified beyond what dprompt-59 specified

**Verify no secrets:**
```bash
grep -i "password\|token\|api.key\|secret" src/api/main.py | head -5
```

Expected: no output (no hardcoded secrets)

### 4. Prepare for Copy (FaultLine-dev → faultline-prod)

**Step A: Verify file syntax**
```bash
python -m py_compile src/api/main.py
```

Expected: clean, no errors

**Step B: Audit for IPs/emails**
```bash
grep -E "[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|@.*\.com" src/api/main.py
```

Document any hardcoded IPs/emails found (should be none, but verify)

### 5. Copy & Sanitize (FaultLine-dev → faultline-prod)

**Step A: Identify the faultline-prod repo location**
- If local: typically `~/FaultLine` or similar
- Verify it exists: `ls -la ~/FaultLine/src/api/main.py`

**Step B: Copy the file with sanitization**
```bash
cp src/api/main.py ~/FaultLine/src/api/main.py
```

**Step C: Sanitize (check & fix any pre-prod IPs)**
```bash
# Check for any IPs in the copied file
grep -n "192.168\|10\.0\|172.16" ~/FaultLine/src/api/main.py
```

If found, change to `localhost` (example: `192.168.40.10:8001` → `localhost:8001`)

### 6. Update Documentation (faultline-prod)

**Step A: Update ABOUT.md**
```
## v1.0.3 (2026-05-12)

**Conflict Detection & Auto-Superseding**

Added semantic conflict detection to the ingest pipeline. When new facts contradict existing graph structure, they are automatically superseded with reasons logged.

Example: "I have a dog named Fraggle, a morkie" no longer creates conflicting `user owns morkie` fact. System detects morkie is a type and keeps only the correct `fraggle instance_of morkie`.

Works with dprompt-58 (extraction constraint) and retraction flow (explicit corrections) for comprehensive data quality.

References: dprompt-59, dBug-report-003, dBug-report-004
```

**Step B: Update CHANGELOG.md**
```
## [1.0.3] — 2026-05-12

### Added
- Semantic conflict detection at ingest time
- Auto-superseding of conflicting facts based on graph structure
- Conflict reason logging (audit trail)

### Fixed
- Fixes dBug-report-003 (hierarchy chain leakage at root cause)
- Prevents bad fact storage when conflicts detected

### Related
- dprompt-58: Extraction constraint (prevent conflicts at source)
- dprompt-59: Conflict detection (catch at ingest gate)
```

### 7. Validate Build (faultline-prod)

**Step A: Docker compose check**
```bash
cd ~/FaultLine
docker compose config > /dev/null 2>&1 && echo "Docker config clean" || echo "ERROR in docker-compose.yml"
```

**Step B: Python syntax check**
```bash
python -m py_compile src/api/main.py && echo "Syntax clean" || echo "ERROR in main.py"
```

Expected: both output "clean" / success

### 8. Commit, Tag, Push (faultline-prod)

**Step A: Stage files**
```bash
cd ~/FaultLine
git add src/api/main.py ABOUT.md CHANGELOG.md
```

**Step B: Commit with clear message**
```bash
git commit -m "dprompt-59: add semantic conflict detection & auto-superseding

- Conflict detection runs before fact class assignment in /ingest
- Auto-supersedes facts that violate graph hierarchy semantics
- Examples: type/ownership conflicts, role/type conflicts, component/container conflicts
- Works with dprompt-58 (extraction constraint) and retraction flow
- Keeps graph semantically valid and consistent
- Audit trail: conflict decisions logged with reasons

Fixes: dBug-report-003 (hierarchy leakage root cause)
Related: dprompt-58, dBug-004 (stale data cleanup)"
```

**Step C: Tag release**
```bash
git tag -a v1.0.3 -m "v1.0.3: Conflict Detection & Auto-Superseding"
```

**Step D: Push to GitHub**
```bash
git push origin main
git push origin --tags
```

**Step E: Verify**
```bash
git status  # should be clean
git log --oneline -5  # verify commit is there
```

### 9. STOP & Report

Update `scratch.md` in FaultLine-dev with completion template (below). Then STOP — await user verification.

## Deliverable

**Modified file:** `src/api/main.py` (faultline-prod)
- Added: `_detect_semantic_conflicts()` function (~94 lines)
- No logic changes to existing functions
- Integrated into `/ingest` pipeline before Class A/B/C assignment

**Documentation updated:**
- ABOUT.md: v1.0.3 release summary
- CHANGELOG.md: v1.0.3 entry

**Released:**
- Tag: v1.0.3
- Pushed to GitHub: origin/main + tags

## Files to Modify

```
faultline-prod/:
├─ src/api/main.py (conflict detection function added)
├─ ABOUT.md (v1.0.3 release notes)
└─ CHANGELOG.md (v1.0.3 entry)

No changes to FaultLine-dev (already committed in dprompt-59)
No schema changes (uses existing fact columns: superseded_at, contradicted_by)
```

## Success Criteria

✅ File syntax: `python -m py_compile` passes  
✅ Docker config: `docker compose config` clean  
✅ Git status: clean after commit  
✅ Commit message: clear, references dprompt-59  
✅ Tag: v1.0.3 created and pushed  
✅ GitHub: commit visible at origin/main, tag visible  
✅ Documentation: ABOUT.md + CHANGELOG.md updated with v1.0.3 entry  
✅ No hardcoded secrets in deployed code  

## Upon Completion

**⚠️ MANDATORY: Update scratch.md in FaultLine-dev with this exact template (copy-paste, fill in values):**

```markdown
## ✓ DONE: dprompt-59b (Production Deployment — Conflict Detection) — [DATE]

**Task:** Deploy dprompt-59 (semantic conflict detection) to faultline-prod.

**Changes (faultline-prod):**
- File: src/api/main.py
  - Added: _detect_semantic_conflicts() function (~94 lines)
  - Runs: before fact class assignment in /ingest pipeline
  - Behavior: auto-supersedes facts conflicting with graph hierarchy
  - Lines: 3580 → 3674 (+94)
- File: ABOUT.md
  - Added: v1.0.3 release summary (conflict detection feature)
- File: CHANGELOG.md
  - Added: v1.0.3 entry (feature, fixes, related)

**Validation (Pre-Prod):**
- Syntax: clean ✓
- Docker: `docker compose config` clean ✓
- Logs: conflict detection running (if pre-prod online) ✓

**Release:**
- Commit: [commit SHA] — dprompt-59: conflict detection...
- Tag: v1.0.3 created ✓
- Push: origin/main + --tags ✓
- GitHub: https://github.com/tkalevra/FaultLine — v1.0.3 visible ✓

**Next steps:**
- User rebuilds faultline backend container on truenas
- User tests: "I have a dog Fraggle, a morkie" → no `user owns morkie` fact created
- User validates: graph remains semantically valid
- Report results in scratch

**AWAITING USER REBUILD AND VALIDATION.**
```

Then **STOP immediately** — do not proceed with live testing, do not deploy to pre-prod, do not start another task.

## Critical Rules (Non-Negotiable)

**Environment Isolation:**
1. **Investigation** = Pre-Prod Only (read-only: logs, queries)
2. **Code Development** = FaultLine-dev Only (already done in dprompt-59)
3. **Deployment** = Copy to faultline-prod, validate, commit/tag/push, then STOP for user rebuild

**No Scope Creep:**
- If you notice bugs beyond dprompt-59 scope → report, don't fix
- Only deploy what dprompt-59 changed

**Commit Discipline:**
- One commit (logical, self-contained)
- Clear message with context
- Only to faultline-prod master branch
- Tag with semantic version (v1.0.3)

**STOP Clause is Mandatory:**
- Every deployment ends with STOP
- STOP means: wait for explicit user direction
- User must rebuild and validate before any next steps

---

**Template version:** 1.0 (follows PRODUCTION_DEPLOYMENT_GUIDE.md)  
**Status:** Ready for execution
