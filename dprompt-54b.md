# dprompt-54b: DEEPSEEK_INSTRUCTION_TEMPLATE — Production Deployment

## Task

Deploy dprompt-53b changes from FaultLine-dev to production (faultline-prod repo) with sanitization, documentation updates, validation, and release tagging.

## Context

**Current state:** dprompt-53b (Filter simplification — backend-first architecture) is complete, tested, and committed in FaultLine-dev. The Filter no longer implements three-tier gating logic; it trusts backend /query ranking. Category queries ("our pets", "my family") now return correct facts.

**What needs to happen:** Move this code to production safely. This means:
1. Copy changed files from FaultLine-dev to faultline-prod
2. Audit for sensitive data (bearer tokens, personal names, server names, API keys)
3. Update production documentation (ABOUT.md, CHANGELOG.md)
4. Validate the build (syntax, docker-compose config)
5. Commit, tag, and push to GitHub

**Why this matters:** Users need production code updated. But we must ensure no secrets leak and documentation is clear about what changed.

**Read first:** `docs/PRODUCTION_DEPLOYMENT_GUIDE.md` (standard operating procedure). `dprompt-54.md` (specification). `docs/ARCHITECTURE_QUERY_DESIGN.md` (architecture principle).

## Constraints

**MUST:**
- Follow `docs/PRODUCTION_DEPLOYMENT_GUIDE.md` step-by-step (your bible for this task)
- Identify ALL changed files (from dprompt-53b commit in FaultLine-dev)
- Audit each file for sensitive data: bearer tokens (`sk-[a-f0-9]{32}`), personal names (Gabriella, Mars, Cyrus, etc.), server names (hairbrush, truenas, 192.168.*), email addresses
- Copy files to `~/faultline-prod` with sanitization applied inline (don't modify FaultLine-dev)
- Update ABOUT.md with v1.0.1 release summary (feature, impact, fixes)
- Validate: docker compose config, python -m py_compile for Python files
- Commit with message: "dprompt-53b: Filter simplification — backend-first architecture"
- Tag as `v1.0.1`
- Push to GitHub: `git push origin main && git push origin --tags`
- No secrets remain in committed code (grep verify post-push)

**DO NOT:**
- Copy non-production files (scratch.md, dprompt-*.md, CLAUDE.md, tests/, Archive/, BUGS/)
- Leave any secrets in code (bearer tokens, personal emails, hardcoded server names)
- Commit to faultline-prod without tagging
- Force-push or rewrite history
- Skip validation (docker, python, git status)
- Forget to push tags (separate command from main push)
- Deploy to actual running pre-prod/production instance (just code, no docker rebuild)

**MAY:**
- Create CHANGELOG.md if it doesn't exist
- Update README.md if needed for clarity
- Add helpful comments to commit message

## Sequence

**CRITICAL: Follow this sequence exactly. Do not skip steps.**

### Phase 1: Identify & Audit (No copying yet)

1. **Identify changed files from dprompt-53b**
   ```bash
   cd /home/chris/Documents/013-GIT/FaultLine-dev
   git log --oneline | grep "dprompt-53b\|Filter simplification" | head -1
   git show [commit-hash] --name-only
   ```
   Expected output: `openwebui/faultline_tool.py` (and potentially others)

2. **Audit each file for sensitive data**
   ```bash
   # For each changed file:
   grep -n "sk-[a-f0-9]\{32\}" /home/chris/Documents/013-GIT/FaultLine-dev/openwebui/faultline_tool.py
   grep -n "Gabriella\|Gabby\|Mars\|Cyrus\|Desmonde\|Chris\|Christopher" ...
   grep -n "hairbrush\|truenas\|192\.168\|172\.16" ...
   grep -n "[a-z]*@[a-z]*\.[a-z]*" ...
   ```
   Expected: No output (clean) OR document findings for Step 2

3. **Document findings**
   - If secrets found: list them (don't commit yet)
   - If clean: note "No secrets found" for reporting

### Phase 2: Copy with Sanitization

4. **Copy changed file to faultline-prod**
   ```bash
   cp /home/chris/Documents/013-GIT/FaultLine-dev/openwebui/faultline_tool.py \
      ~/faultline-prod/openwebui/faultline_tool.py
   ```

5. **Inspect copied file for secrets**
   ```bash
   grep -E "sk-[a-f0-9]{32}|Gabriella|Mars|Cyrus|Desmonde|truenas|hairbrush|192\.168|172\.16" \
     ~/faultline-prod/openwebui/faultline_tool.py
   ```
   If matches found: manually edit file in faultline-prod to sanitize OR restore and investigate

6. **Verify file integrity**
   ```bash
   diff /home/chris/Documents/013-GIT/FaultLine-dev/openwebui/faultline_tool.py \
        ~/faultline-prod/openwebui/faultline_tool.py
   # Should show: no output (identical) unless sanitization applied
   ```

### Phase 3: Documentation Updates

7. **Update ABOUT.md in faultline-prod**
   ```bash
   # Open ~/faultline-prod/ABOUT.md
   # Add section at top (or update if v1.0.1 section exists):
   
   ## v1.0.1 (2026-05-12) — Filter Simplification
   
   **Architecture:** Filter now implements backend-first approach
   - Removed three-tier relevance gating logic
   - Simplified to: identity rels always pass + confidence threshold
   - Filter trusts backend /query ranking (A > B > C + confidence)
   
   **Result:** Category queries now return correct facts
   - "tell me about our pets" → returns has_pet facts ✓
   - "tell me about my family" → returns spouse + kids + pets ✓
   - No UUID leaks in responses ✓
   
   **Related:** See docs/ARCHITECTURE_QUERY_DESIGN.md for design principle.
   **Fixes:** dBug-report-001 (Tier 2 Identity Fallback Blocks Tier 3)
   ```

8. **Check/update CHANGELOG.md**
   ```bash
   # If ~/faultline-prod/CHANGELOG.md exists: add entry
   # If not: create it with basic structure
   
   # Entry should match commit message
   ```

### Phase 4: Validation

9. **Syntax validation (Python files)**
   ```bash
   python -m py_compile ~/faultline-prod/openwebui/faultline_tool.py
   # Expected: no output (clean)
   ```

10. **Docker validation**
    ```bash
    cd ~/faultline-prod
    docker compose config > /dev/null
    # Expected: no output (valid)
    ```

11. **Git status check (before committing)**
    ```bash
    cd ~/faultline-prod
    git status
    # Expected: shows openwebui/faultline_tool.py as modified, ABOUT.md as modified
    ```

### Phase 5: Commit & Tag

12. **Commit with clear message**
    ```bash
    cd ~/faultline-prod
    git add openwebui/faultline_tool.py ABOUT.md [CHANGELOG.md if updated]
    git commit -m "dprompt-53b: Filter simplification — backend-first architecture
    
    Implement architectural shift from three-tier gating to backend-first ranking.
    
    Changes:
    - Removed _TIER1_*, _TIER2_*, _TIER3_* tier logic from Filter
    - Removed entity_types parameter passing (Filter no longer re-gates)
    - Simplified _filter_relevant_facts() to confidence + identity rels only
    - Filter trusts backend /query ranking as authoritative
    
    Result:
    - Category queries ('our pets', 'my family') now return correct facts
    - No UUID leaks in responses
    - Backend ontology/hierarchy changes no longer require Filter updates
    
    See: docs/ARCHITECTURE_QUERY_DESIGN.md
    Fixes: dBug-report-001 (Tier 2 Identity Fallback Blocks Tier 3)"
    ```

13. **Create annotated tag**
    ```bash
    git tag -a v1.0.1 -m "Filter simplification (dprompt-53b)
    
    Implement backend-first architecture for Filter. Category queries now
    return complete fact sets. See ABOUT.md for full details."
    ```

14. **Verify commit and tag**
    ```bash
    git log --oneline -3
    git tag -l | head -5
    # Verify v1.0.1 appears
    ```

### Phase 6: Push to GitHub

15. **Push commits**
    ```bash
    cd ~/faultline-prod
    git push origin main
    # Expected: "... main -> main"
    ```

16. **Push tags**
    ```bash
    git push origin --tags
    # Expected: "* [new tag] v1.0.1 -> v1.0.1"
    ```

### Phase 7: Verification

17. **Verify GitHub state**
    ```bash
    cd ~/faultline-prod
    git status
    # Expected: "nothing to commit, working tree clean"
    
    git log --oneline -1
    # Expected: shows dprompt-53b commit
    
    git describe --tags
    # Expected: v1.0.1
    ```

18. **Final secrets audit (post-push)**
    ```bash
    # Verify no secrets leaked to GitHub
    grep -r "sk-[a-f0-9]" ~/faultline-prod/ 2>/dev/null | grep -v ".git"
    grep -r "Gabriella\|Mars\|Cyrus" ~/faultline-prod/ 2>/dev/null | grep -v ".git"
    # Expected: no output
    ```

### Phase 8: STOP & Report

19. **STOP and report to scratch.md** (do NOT proceed to live testing)
    - All 18 steps completed successfully
    - Report findings in template below
    - **AWAITING USER VERIFICATION**

## Deliverable

**Modified/created files in faultline-prod:**
- `openwebui/faultline_tool.py` — copied from FaultLine-dev (no code changes, only file transfer)
- `ABOUT.md` — updated with v1.0.1 release summary
- `CHANGELOG.md` — created or updated with v1.0.1 entry (optional but recommended)

**GitHub state:**
- Commit hash: [will be generated]
- Tag: v1.0.1
- Branch: main (pushed)
- Tags: pushed (separate command)

**No code changes.** Only file copying, documentation updates, and release metadata.

## Files to Modify

```
~/faultline-prod/
├─ openwebui/faultline_tool.py        (copy from FaultLine-dev)
├─ ABOUT.md                            (update with v1.0.1 summary)
└─ CHANGELOG.md                        (create or update, optional)
```

**Do NOT touch:**
- src/, migrations/ (no changes in dprompt-53b)
- docker-compose.yml, Dockerfile (no changes)
- .env.example (no changes)

## Success Criteria

✅ Changed files identified: `openwebui/faultline_tool.py` (only file in dprompt-53b)  
✅ Sanitization audit passed: No secrets (bearer tokens, personal names, server names)  
✅ File copied to faultline-prod  
✅ ABOUT.md updated with v1.0.1 summary  
✅ Syntax validation passed: `python -m py_compile` clean  
✅ Docker validation passed: `docker compose config` clean  
✅ Commit created: message includes "dprompt-53b: Filter simplification"  
✅ Tag created: `v1.0.1` exists locally and on GitHub  
✅ Push succeeded: `git push origin main` and `git push origin --tags` both succeeded  
✅ Git status clean: `git status` shows "nothing to commit"  
✅ No secrets in repo: grep finds no tokens, personal names, or server names  

## Upon Completion

**Update `scratch.md` with this template (copy-paste, fill in values):**

```markdown
## ✓ DONE: dprompt-54b (Production Deployment — dprompt-53b) — [DATE]

**Task:** Deploy dprompt-53b (Filter simplification) from FaultLine-dev to faultline-prod.

**Files Copied (faultline-prod):**
- openwebui/faultline_tool.py (FaultLine-dev → faultline-prod, no code changes)

**Documentation Updated:**
- ABOUT.md: Added v1.0.1 summary (Filter simplification, backend-first architecture)
- CHANGELOG.md: [Created/Updated] with v1.0.1 entry

**Sanitization Audit:**
- Bearer tokens: None found ✓
- Personal names (Gabriella, Mars, Cyrus, etc.): None in code ✓
- Server names (hairbrush, truenas, IPs): None in code ✓
- Email addresses: None found ✓

**Validation:**
- Syntax: python -m py_compile clean ✓
- Docker: docker compose config clean ✓
- Git status: working tree clean ✓

**Release:**
- Commit: [hash] "dprompt-53b: Filter simplification — backend-first architecture"
- Tag: v1.0.1 created ✓
- Push: origin/main + tags pushed to GitHub ✓

**GitHub Verification:**
- Commit visible in faultline-prod history ✓
- Tag v1.0.1 visible in tags list ✓
- ABOUT.md shows v1.0.1 summary ✓

**Status:** AWAITING USER VERIFICATION

User should:
1. Verify GitHub shows v1.0.1 tag
2. Confirm ABOUT.md updated with new features
3. Run live tests (if needed) against pre-prod

STOP — Do not proceed further.
```

Then **STOP immediately**. Do not run live tests, do not rebuild containers, do not proceed to the next dprompt. Wait for user verification and direction.

## Critical Rules (Non-Negotiable)

**Environment Isolation:**
1. **Copy, don't code** — Transfer files, don't reimplement
2. **Sanitize during copy** — If secrets found, clean them inline in faultline-prod (don't modify FaultLine-dev)
3. **No force-push** — All git operations must be clean pushes

**Git Discipline:**
1. **Tag every release** — `v1.0.1` must be created and pushed
2. **Clear commit messages** — Include dprompt reference and what changed
3. **Clean status before push** — `git status` must be clean

**Validation First:**
1. **Syntax before commit** — `python -m py_compile` must pass
2. **Docker before push** — `docker compose config` must pass
3. **Secrets before push** — grep verify no tokens/names in code

**STOP Clause is Mandatory:**
- Every deployment ends with STOP
- Do not attempt live testing without user direction
- Do not rebuild containers
- Await explicit user approval before next phase
