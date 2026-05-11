# dprompt-54: Production Deployment — dprompt-53b (Filter Simplification)

**Date:** 2026-05-12  
**Author:** Christopher Thompson  
**Status:** Ready for execution  
**Severity:** Release  
**Related:** dprompt-53b (Filter simplification)

## Problem Statement

Code for dprompt-53b (Filter simplification — backend-first architecture) is complete, tested, and committed in FaultLine-dev. Now it must be deployed to production (faultline-prod repo) and pushed to GitHub.

This requires:
1. Identifying changed files
2. Auditing for sensitive data (bearer tokens, personal names, server names, API keys)
3. Copying to production with sanitization
4. Updating production documentation
5. Validating the build
6. Committing, tagging, and pushing to GitHub

## Solution Overview

Follow `docs/PRODUCTION_DEPLOYMENT_GUIDE.md` as the standard operating procedure. This is a repeatable process that will be used for every production deployment.

**For dprompt-53b specifically:**
- Changed file: `openwebui/faultline_tool.py` (1621 → 1579 lines)
- Sanitization: Check for any hardcoded server names, tokens (unlikely, but verify)
- Documentation: Update ABOUT.md with v1.0.1 release notes
- Validation: Ensure docker-compose.yml and syntax are valid
- Tag: `v1.0.1`

## Scope Definition

### MUST Do
- Identify all files changed in dprompt-53b
- Audit each file for sensitive data (tokens, names, IPs, emails)
- Copy files to faultline-prod with sanitization applied
- Update ABOUT.md with v1.0.1 feature summary
- Update CHANGELOG.md if it exists
- Validate production build (docker compose config, python -m py_compile)
- Commit with clear message "dprompt-53b: Filter simplification — backend-first architecture"
- Tag as `v1.0.1`
- Push to GitHub (main branch + tags)

### MUST NOT Do
- Copy non-production files (scratch.md, dprompt-*.md, tests/, etc.)
- Leave any secrets in committed code (bearer tokens, personal emails, server names)
- Commit to faultline-prod without tagging
- Force-push (all pushes should be clean merges)
- Skip validation step (build must work)

### MAY Do
- Create CHANGELOG.md if it doesn't exist
- Add deployment notes to README.md if helpful
- Organize git history with clear commit messages

## Design Details

### Files Changed in dprompt-53b

**Source (FaultLine-dev):**
- `openwebui/faultline_tool.py` — Filter simplification

**Destination (faultline-prod):**
- `openwebui/faultline_tool.py` (copy, no sanitization needed)

### Sanitization Scope

Search changed files for:
- Bearer tokens: `sk-[a-f0-9]{32}` pattern
- Personal names: Gabriella, Gabby, Mars, Cyrus, Desmonde, Chris, Christopher
- Server names: hairbrush.helpdeskpro.ca, truenas, etc.
- IP addresses: 192.168.*, 172.16.*
- Email addresses: ct8ball@gmail.com, etc.

**For openwebui/faultline_tool.py:** Unlikely to have secrets (code logic only), but verify.

### Documentation Updates

**ABOUT.md** — Add release notes:
```markdown
## v1.0.1 (2026-05-12) — Filter Simplification

**Architecture:** Filter now implements backend-first approach
- Removed three-tier relevance gating logic
- Simplified to: identity rels always pass + confidence threshold
- Filter trusts backend /query ranking (A > B > C + confidence)

**Result:** Category queries now return correct facts
- "tell me about our pets" → returns has_pet facts ✓
- "tell me about my family" → returns spouse + kids + pets ✓
- No UUID leaks in responses ✓

**Related:** docs/ARCHITECTURE_QUERY_DESIGN.md
**Fixes:** dBug-report-001 (Tier 2 blocking Tier 3)
```

**CHANGELOG.md** — Record changes (if file exists)

### Validation Steps

1. **File Integrity:**
   ```bash
   ls -la ~/faultline-prod/openwebui/faultline_tool.py
   diff /home/chris/Documents/013-GIT/FaultLine-dev/openwebui/faultline_tool.py \
        ~/faultline-prod/openwebui/faultline_tool.py
   ```

2. **Secrets Scan:**
   ```bash
   grep -r "sk-[a-f0-9]" ~/faultline-prod/openwebui/
   grep -r "Gabriella\|Mars\|Cyrus" ~/faultline-prod/
   # Expected: no output
   ```

3. **Syntax Check:**
   ```bash
   python -m py_compile ~/faultline-prod/openwebui/faultline_tool.py
   # Expected: no output
   ```

4. **Docker Config:**
   ```bash
   cd ~/faultline-prod
   docker compose config > /dev/null
   # Expected: success
   ```

### Git Workflow

**Commit message:**
```
dprompt-53b: Filter simplification — backend-first architecture

Implement architectural shift from three-tier gating to backend-first ranking.

Changes:
- Removed _TIER1_*, _TIER2_*, _TIER3_* tier logic from Filter
- Removed entity_types parameter passing (Filter no longer re-gates)
- Simplified _filter_relevant_facts() to confidence + identity rels only
- Filter trusts backend /query ranking as authoritative

Result:
- Category queries ("our pets", "my family") now return correct facts
- No UUID leaks in responses
- Backend ontology/hierarchy changes no longer require Filter updates

See: docs/ARCHITECTURE_QUERY_DESIGN.md
Fixes: dBug-report-001 (Tier 2 Identity Fallback Blocks Tier 3)
```

**Tag:**
```
v1.0.1 — Filter simplification (dprompt-53b)

Implement backend-first architecture for Filter. Category queries now
return complete fact sets. See ABOUT.md for full details.
```

## Implementation Boundaries

### Investigation Phase
- Not needed (code already complete in FaultLine-dev)
- Only verify: changed files, audit for secrets, validate build

### Development Phase
- Located in: `~/faultline-prod`
- Actions: copy files, update docs, validate, commit, tag, push
- No code changes (copying, not coding)

### Deployment Phase
- Manual: User verifies repo state and runs live tests (post-push)
- Files synced: All from openwebui/ directory
- **STOP before live testing** — wait for user verification

## Success Criteria

✅ Changed files identified (openwebui/faultline_tool.py)  
✅ Sanitization audit passed (no secrets found or removed)  
✅ Files copied to faultline-prod  
✅ ABOUT.md updated with v1.0.1 summary  
✅ Docker validation passed (docker compose config clean)  
✅ Syntax validation passed (python -m py_compile clean)  
✅ Commit created with clear message  
✅ Tag v1.0.1 created  
✅ Push to GitHub succeeded (main branch + tags)  
✅ Git status clean: no uncommitted changes in faultline-prod  

## References

- `docs/PRODUCTION_DEPLOYMENT_GUIDE.md` — Standard operating procedure
- `dprompt-53b.md` — Original Filter simplification prompt
- `docs/ARCHITECTURE_QUERY_DESIGN.md` — Architecture principle
- dBug-report-001.md — Bug being fixed
- `faultline-prod` repo: https://github.com/tkalevra/FaultLine.git

## Notes for Deepseek

**Critical constraints:**
- Copy only changed files (don't re-implement or refactor)
- Sanitize inline during copy (don't modify FaultLine-dev)
- Follow docs/PRODUCTION_DEPLOYMENT_GUIDE.md step-by-step
- Investigation + validation only (no new code)
- STOP before live testing — wait for user verification

**Common pitfalls to avoid:**
- Forgetting to push tags (`git push origin --tags`)
- Copying files without verifying no secrets (grep for tokens)
- Skipping docker compose config validation
- Not updating ABOUT.md (users need to know what changed)
- Committing without meaningful commit message

**Success looks like:**
- faultline-prod GitHub repo has v1.0.1 tag
- v1.0.1 shows in `git tag -l`
- User can see ABOUT.md mentions "Filter Simplification"
- No secrets or personal data in pushed code
- Build validates (docker compose config passes)
