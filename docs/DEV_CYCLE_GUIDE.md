# Development Cycle Guide: From Problem to Production

**Purpose:** Establish consistent, disciplined development cycles for FaultLine improvements.  
**Audience:** Claude Code, Deepseek, Christopher Thompson  
**Version:** 1.0  
**Last Updated:** 2026-05-12

---

## Overview: The Cycle

Every feature, fix, or architectural change follows this cycle:

```
Problem Identified
    ↓
Specification (dprompt-N.md)
    ↓
Formal Prompt (dprompt-Nb.md)
    ↓
Investigation (Pre-Prod)
    ↓
Development (FaultLine-dev)
    ↓
Local Validation
    ↓
STOP
    ↓
User Rebuilds Pre-Prod
    ↓
Live Validation (Pre-Prod)
    ↓
Next Iteration
```

**Key principle:** Separation of concerns. Investigation, development, and deployment are three distinct phases, each in its own environment.

---

## Phase 1: Problem & Specification

### Who Does This: Claude Code

**Input:** User describes a problem or desired change.

**Output:** Two files
- `dprompt-N.md` — specification (what/why/how)
- `dprompt-Nb.md` — formal executable prompt (using template)

**Use templates:** `dprompt-template.md` and `dprompt-templateb.md`

### Checklist

- [ ] Problem is clearly stated (root cause identified if known)
- [ ] Solution approach is explained (why this approach?)
- [ ] Scope is bounded (MUST/MUST NOT/MAY sections completed)
- [ ] Success criteria are quantified (not subjective)
- [ ] All constraints are explicit (environment boundaries, no scope creep)
- [ ] References are provided (other dprompts, architecture docs, bug reports)
- [ ] Template is correctly used (task, context, constraints, sequence, etc.)

### Example: Problem to Specification

**Problem:** "Tier 2 identity fallback blocks Tier 3 when Tier 1 is empty due to Concept filtering."

**Specification:**
- File: `dprompt-53.md`
- Explains: why Tier 1/2/3 logic is brittle, how Concept filtering creates the condition, why architecture should shift
- Proposes: simplify Filter to remove tier logic entirely

**Formal Prompt:**
- File: `dprompt-53b.md`
- Task: "Simplify Filter to remove brittle three-tier gating logic"
- Constraints: Investigation pre-prod only, code FaultLine-dev only, tests pass, STOP before live validation
- Sequence: read, investigate, analyze, implement, validate, STOP

---

## Phase 2: Investigation (Read-Only, Pre-Prod)

### Who Does This: Deepseek (or Claude Code)

**Environment:** Pre-Prod only (`hairbrush.helpdeskpro.ca` or wherever live instance is)

**Method:** SSH, read-only queries, log inspection

**Examples:**
```bash
# Check container logs
ssh truenas -x "sudo docker logs faultline --tail 100"
ssh truenas -x "sudo docker logs open-webui --tail 100"
ssh truenas -x "sudo docker logs faultline-postgres --tail 50"

# Query database (read-only)
ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline \
  -c 'SELECT COUNT(*) FROM facts WHERE rel_type = '\''has_pet'\'';'"

# Check health
ssh truenas -x "sudo docker exec open-webui curl -s http://faultline:8001/health"

# Send test message via API
curl -H "Authorization: Bearer [token]" \
  -H "Content-Type: application/json" \
  -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"Test message"}],"stream":false}' \
  https://hairbrush.helpdeskpro.ca/api/chat/completions
```

**Output:** Findings documented in scratch.md (prefixed #deepseek)

**What NOT to do:**
- ❌ Modify pre-prod container or database
- ❌ Deploy code changes to pre-prod
- ❌ Restart containers
- ❌ Commit anything from pre-prod

---

## Phase 3: Development (FaultLine-dev Only)

### Who Does This: Deepseek

**Environment:** Local dev repo (`/home/chris/Documents/013-GIT/FaultLine-dev/`)

**Method:** Follow dprompt-Nb.md sequence exactly

**Workflow:**
1. Read dprompt-N.md (specification)
2. Read dprompt-Nb.md (formal prompt)
3. Analyze code in FaultLine-dev (files to be modified)
4. Implement changes
5. Validate locally (syntax, tests)
6. Commit to master branch with message "dprompt-X: [descriptive title]"
7. **STOP**

**What NOT to do:**
- ❌ Commit to faultline-prod repo
- ❌ Deploy to pre-prod
- ❌ Skip local validation (test suite, syntax check)
- ❌ Scope creep (fix other bugs you discover)
- ❌ Optimize beyond requirement ("this function could be faster")

### Commit Message Format

```
dprompt-X: [Descriptive Title — Keep to One Line]

[Optional body: what changed, why, any non-obvious decisions]

[Example:]
dprompt-53: Filter simplification — remove three-tier gating logic

- Removed _TIER1_*, _TIER2_*, _TIER3_* constants
- Simplified _filter_relevant_facts() to confidence + identity gating
- Removed entity_types parameter passing
- Lines: 700 → 500 (28% reduction)

Tests: 112 passed, 0 regressions
```

### Test Suite Expectations

**Before making changes:**
```bash
cd /home/chris/Documents/013-GIT/FaultLine-dev
pytest tests/api/ --ignore=tests/evaluation -v
# Expected: 112+ passed, 53 skipped, 0 failed
```

**After making changes:**
```bash
pytest tests/api/ --ignore=tests/evaluation -v
# Expected: 112+ passed, 53 skipped, 0 failed (same or better)
# If any NEW failures: diagnose and fix before proceeding
```

**Syntax validation:**
```bash
python -m py_compile src/api/main.py
python -m py_compile openwebui/faultline_tool.py
python -m py_compile src/re_embedder/embedder.py
# Expected: no output (clean)
```

---

## Phase 4: Local Validation & STOP

### Who Does This: Deepseek

**Checklist:**
- [ ] All files modified are listed in dprompt-Nb.md "Files to Modify"
- [ ] Syntax check passes (python -m py_compile)
- [ ] Test suite passes (pytest — same number of tests, 0 new failures)
- [ ] Code review: spot-check for obvious issues (no hardcoding, no dead code, no regressions)
- [ ] Commit message follows format
- [ ] Scratch.md updated with completion template

**Validation Output (to scratch.md):**

```markdown
## ✓ DONE: dprompt-53b (Filter Simplification) — 2026-05-12

**Task:** Simplify Filter to remove three-tier gating logic

**Changes (FaultLine-dev):**
- File: openwebui/faultline_tool.py
  - Removed: _TIER1_*, _TIER2_*, _TIER3_* constants
  - Removed: _categorize_query() function
  - Modified: _filter_relevant_facts() → confidence gating
  - Lines: 700 → 500 (28% reduction)

**Local Validation:**
- Syntax: clean ✓ (python -m py_compile openwebui/faultline_tool.py)
- Tests: 112 passed, 53 skipped, 0 regressions ✓ (pytest tests/api/ --ignore=tests/evaluation)
- Code review: Tier logic completely removed, entity_types parameter removed, sensitivity gating preserved ✓

**Next Steps:**
- User rebuilds pre-prod docker image
- User validates: "tell me about our pets" → has_pet facts
- User validates: "how are you" → identity facts only

**AWAITING USER REBUILD AND LIVE VALIDATION.**
```

**Then STOP immediately.** Do not proceed to live testing, do not attempt deployment, do not start the next dprompt.

---

## Phase 5: User Rebuild & Redeploy

### Who Does This: Christopher Thompson (User)

**When:** After deepseek completes Phase 4 and STOPS

**Action:** Rebuild and redeploy pre-prod container

**Commands (example):**
```bash
cd ~/faultline-prod
git pull origin main  # (if changes synced to prod repo)
docker compose down
docker compose up -d --build
curl http://localhost:8001/health  # verify running
```

Or if changes are only in FaultLine-dev (not yet synced to prod):
```bash
# Copy changes from FaultLine-dev to faultline-prod
cp /home/chris/Documents/013-GIT/FaultLine-dev/openwebui/faultline_tool.py \
   ~/faultline-prod/openwebui/

# Rebuild
cd ~/faultline-prod
docker compose down
docker compose up -d --build
curl http://localhost:8001/health
```

---

## Phase 6: Live Validation (Pre-Prod)

### Who Does This: Christopher Thompson or Deepseek (with direction)

**Environment:** Pre-Prod (`hairbrush.helpdeskpro.ca`)

**Validation Queries:** Specified in dprompt-Nb.md "Success Criteria"

**Example for dprompt-53b:**
```bash
# Test 1: Category query returns relationship facts
curl -H "Authorization: Bearer [token]" \
  -H "Content-Type: application/json" \
  -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"Tell me about our pets"}],"stream":false}' \
  https://hairbrush.helpdeskpro.ca/api/chat/completions
# Expected: Response mentions has_pet facts (fraggle, morkie, etc.)

# Test 2: Generic query returns identity facts
curl ... -d '{"messages":[{"role":"user","content":"How are you?"}],...}'
# Expected: Identity facts only (pref_name, age, location, etc.)

# Test 3: Explicit ask overrides sensitivity gating
curl ... -d '{"messages":[{"role":"user","content":"When was I born?"}],...}'
# Expected: Birthday returned (explicit ask)
```

**Report Results:** Update scratch.md with findings

**If tests fail:** Report issue, investigate root cause (code vs. backend state), don't attempt fix

---

## Phase 7: Next Iteration (Loop Back)

### Who Does This: Christopher Thompson

**Options:**
1. **Approve & Merge:** "Results look good. Next dprompt?"
   - Deepseek can start next dprompt-Nb.md
2. **Request Revision:** "Test 3 failed. Investigate and revert."
   - Deepseek reviews, identifies issue, creates revision dprompt
3. **Pause & Learn:** "Let's understand why [test] failed before proceeding"
   - Add findings to ARCHITECTURE_QUERY_DESIGN.md or CLAUDE.md
   - Refine next dprompt based on learning

---

## Common Pitfalls & How to Avoid Them

### Pitfall 1: Scope Creep

**Problem:** Deepseek sees a related bug and fixes it while implementing dprompt-X.

**Fix:** 
- Dprompt-Nb.md explicitly states: "DO NOT fix other bugs. Report in scratch.md, wait for direction."
- Claude Code reviews dprompt before sending to deepseek: "This dprompt fixes [X] only. Bug [Y] is separate."

### Pitfall 2: Premature Optimization

**Problem:** Deepseek refactors code to be "cleaner" while implementing dprompt-X.

**Fix:**
- Constraints section: "MAY consolidate helper functions IF it improves readability. DO NOT refactor beyond scope."
- Template reminds: "No premature optimization. Stick to scope."

### Pitfall 3: Skipped Local Testing

**Problem:** Deepseek implements changes but doesn't run test suite before STOP.

**Fix:**
- Sequence section is mandatory: "5. **Validate Locally** ... pytest ... if failures: diagnose and fix"
- Upon Completion section requires: "Tests: [X] passed, [Y] skipped, 0 regressions ✓"

### Pitfall 4: Forgotten STOP Clause

**Problem:** Deepseek implements, validates, AND attempts live testing (without user rebuild).

**Fix:**
- Sequence section ends with: "6. **STOP & Report** ... **STOP clause is mandatory**"
- Upon Completion template ends with: "**AWAITING USER REBUILD AND VALIDATION.**"
- Claude Code emphasizes in summary: "STOPS and waits for your rebuild."

### Pitfall 5: Mixing Environments

**Problem:** Deepseek investigates in FaultLine-dev repo instead of pre-prod, or commits to faultline-prod instead of FaultLine-dev.

**Fix:**
- Dprompt-Nb.md has explicit section: "Implementation Boundaries"
- Constraints section repeats: "Investigation pre-prod only, code FaultLine-dev only"
- Each phase has "What NOT to do" section

---

## Templates Reference

### For Specification Writers (Claude Code)

**Start with:** `dprompt-template.md`

**Fill in:**
1. Problem Statement
2. Solution Overview
3. Scope Definition (MUST/MUST NOT/MAY)
4. Design Details
5. Implementation Boundaries (Investigation / Development / Deployment)
6. Success Criteria
7. References & Reading

**Then create:** `dprompt-Nb.md` (formal executable prompt)

### For Prompt Writers (Claude Code)

**Start with:** `dprompt-templateb.md`

**Fill in:**
1. Task (one sentence)
2. Context (2-3 paragraphs + reading list)
3. Constraints (MUST/DO NOT/MAY + environment boundaries)
4. Sequence (six numbered steps, each with sub-bullets)
5. Deliverable (what will change)
6. Files to Modify (explicit list with sub-bullets)
7. Success Criteria (quantified)
8. Upon Completion (exact template to copy-paste)

**Critical sections to get right:**
- Task: Must be crystal clear, no ambiguity
- Constraints: Must be exhaustive (what should be locked down?)
- Sequence: Must be step-by-step, linear, no skips
- Success Criteria: Must be measurable (test count, line count, specific queries)

---

## Metrics & Health Checks

### Development Cycle Velocity

**Healthy indicators:**
- [ ] Dprompt-Nb.md execution takes 1-2 hours (investigation + dev + validation)
- [ ] Local test suite runs in < 5 minutes
- [ ] Commit is logical and self-contained (not split into 10 small commits)
- [ ] No regressions (test count same or increases)

### Code Quality Checks

**After each dprompt:**
- [ ] Code is < 500 lines shorter OR clearer (not the same length with different bugs)
- [ ] No hardcoded values (all config via env vars or constants)
- [ ] No dead code (removed, not commented out)
- [ ] Comments explain WHY, not WHAT (code shows what it does)
- [ ] Syntax passes (python -m py_compile)

### Validation Completeness

**Before STOP:**
- [ ] Syntax validated
- [ ] Test suite passed (report numbers)
- [ ] Commit message clear
- [ ] Scratch.md updated with template
- [ ] No uncommitted changes in FaultLine-dev

---

## Quick Reference: File Locations

```
/home/chris/Documents/013-GIT/FaultLine-dev/     ← Development (code changes here)
├─ dprompt-template.md                           ← Use for specifications
├─ dprompt-templateb.md                          ← Use for formal prompts
├─ docs/DEV_CYCLE_GUIDE.md                       ← This file
├─ docs/ARCHITECTURE_QUERY_DESIGN.md             ← Design principles
├─ src/                                          ← Source code
├─ openwebui/                                    ← Filter code
├─ migrations/                                   ← Database migrations
├─ tests/                                        ← Test suite
├─ scratch.md                                    ← Dialogue hub
├─ compact-instruct.md                           ← This project's principles
└─ CLAUDE.md                                     ← Architecture reference

~/faultline-prod/                                 ← Production (synced from dev)
├─ src/
├─ openwebui/
├─ migrations/
├─ docker-compose.yml
└─ .env.example

https://hairbrush.helpdeskpro.ca                 ← Pre-Prod (live instance)
├─ Investigation via SSH: ssh truenas -x "sudo docker ..."
├─ API: https://hairbrush.helpdeskpro.ca/api/chat/completions
└─ Model: faultline-wgm-test-10
```

---

## Summary

**Three-environment cycle:**
1. **FaultLine-dev** (local) — investigation + code changes + local validation
2. **Pre-Prod** (live test instance) — investigation (read-only), then live validation (post-rebuild)
3. **faultline-prod** (production repo) — synced code, ready to deploy to any environment

**Three-phase dprompt flow:**
1. **dprompt-N.md** — specification (problem, solution, scope)
2. **dprompt-Nb.md** — formal executable prompt (task, context, constraints, sequence, success criteria)
3. **Execution** — investigation → development → validation → STOP → user rebuild → live validation

**Discipline:**
- Every dprompt has explicit STOP clause (no live testing without user rebuild)
- Every dprompt has environment boundaries (pre-prod investigation, FaultLine-dev code, user-triggered deployment)
- Every dprompt has quantified success criteria (tests, lines, specific queries)
- Every phase has a checklist (nothing skipped)

**Result:** Consistent, repeatable, low-risk development cycles that scale with team size and project complexity.

---

**Version:** 1.0  
**Last Updated:** 2026-05-12  
**Template Version:** See dprompt-template.md (1.0) and dprompt-templateb.md (1.0)
