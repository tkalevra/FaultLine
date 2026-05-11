# Production Deployment Guide: FaultLine-dev → faultline-prod

**Purpose:** Standard Operating Procedure for deploying code from development to production.  
**Audience:** Claude Code, Deepseek, Christopher Thompson  
**Version:** 1.0  
**Last Updated:** 2026-05-12

---

## Overview

Every production deployment follows this sequence:

```
Code Complete (FaultLine-dev)
    ↓
Identify Changed Files
    ↓
Audit for Sensitive Data
    ↓
Copy to faultline-prod
    ↓
Update Production Docs (ABOUT.md, etc.)
    ↓
Validate Build
    ↓
Commit & Tag
    ↓
Push to GitHub
    ↓
VERIFY (user confirms)
```

---

## Pre-Deployment Checklist

### 1. Code Complete in FaultLine-dev

- [ ] All dprompt-Nb.md changes implemented
- [ ] Local tests pass (pytest)
- [ ] Syntax validates (python -m py_compile)
- [ ] Committed to master branch with clear message

**Verification:**
```bash
cd /home/chris/Documents/013-GIT/FaultLine-dev
git log --oneline -1
git status  # should be clean
pytest tests/api/ --ignore=tests/evaluation -v  # should pass
```

### 2. Identify Changed Files

**List all files modified in this dprompt.** Example for dprompt-53b:
```
openwebui/faultline_tool.py  # Filter simplification
```

**Commands to identify:**
```bash
# Since last tag or commit
git diff [previous-tag]..HEAD --name-only

# Or: see what's in this dprompt's commit
git show --name-only [commit-hash]
```

---

## Deployment Steps

### Step 1: Audit for Sensitive Data

**Scan all changed files for:**

| Category | Examples | Action |
|----------|----------|--------|
| Bearer Tokens | `sk-addb2220bf534bfaa8f78d96e6991989` | DELETE or replace with `<bearer-token>` |
| Personal Names | Gabriella, Mars, Cyrus, Desmonde, Chris | Keep in test fixtures only; remove from config/docs |
| Server Names | `hairbrush.helpdeskpro.ca`, `truenas` | Replace with `<hostname>`, `<server>`, `example.com` |
| API Keys | Any `sk-`, `api_`, `secret_` patterns | DELETE |
| Email Addresses | `ct8ball@gmail.com` | Replace with `user@example.com` |
| IP Addresses | `192.168.40.10`, `172.16.18.3` | Keep only if generic (localhost, 0.0.0.0) |
| SSH Commands | Commands with auth tokens | Sanitize example commands |
| Debug Comments | Comments with personal context | Generalize or remove |

**Audit commands:**
```bash
# Search for bearer tokens
grep -r "sk-[a-f0-9]\{32\}" /home/chris/Documents/013-GIT/FaultLine-dev/openwebui/

# Search for email addresses
grep -r "[a-z]*@[a-z]*\.[a-z]*" /home/chris/Documents/013-GIT/FaultLine-dev/openwebui/

# Search for personal names
grep -r "Gabriella\|Gabby\|Mars\|Cyrus\|Desmonde" /home/chris/Documents/013-GIT/FaultLine-dev/src/

# Search for server names
grep -r "hairbrush\|truenas\|192\.168\|172\.16" /home/chris/Documents/013-GIT/FaultLine-dev/
```

**If found:** Do NOT sanitize in FaultLine-dev. Instead, create a parallel-sanitized version during copy step (see Step 2).

### Step 2: Copy Files to faultline-prod

**Copy each changed file, applying sanitization inline.**

**Template:**
```bash
# Source: FaultLine-dev
SRC="/home/chris/Documents/013-GIT/FaultLine-dev/openwebui/faultline_tool.py"

# Destination: faultline-prod
DEST="~/faultline-prod/openwebui/faultline_tool.py"

# Copy with inspection
cp "$SRC" "$DEST"

# Inspect for issues
grep -n "sk-\|hairbrush\|Gabriella\|truenas\|192.168\|172.16" "$DEST"
# If matches found: manually edit, OR restore and abort deployment
```

**Sanitization Rules (apply during copy if needed):**

| Pattern | Replace With | Reason |
|---------|--------------|--------|
| `sk-addb2220bf534bfaa8f78d96e6991989` | `<bearer-token>` | Bearer token exposure |
| `Gabriella`, `Gabby`, `Mars`, `Cyrus`, `Desmonde`, `Chris` | Generic names in comments | Personal data |
| `hairbrush.helpdeskpro.ca` | `<hostname>` or `example.com` | Server exposure |
| `truenas`, `192.168.40.10`, `172.16.18.3` | `<server>`, localhost defaults | Infrastructure exposure |
| `ct8ball@gmail.com` | `user@example.com` | Email exposure |
| `ssh truenas -x "sudo docker..."` | `ssh <server> -x "sudo docker..."` | Command examples sanitized |

**Example for dprompt-53b (openwebui/faultline_tool.py):**
```bash
cp /home/chris/Documents/013-GIT/FaultLine-dev/openwebui/faultline_tool.py \
   ~/faultline-prod/openwebui/faultline_tool.py

# Verify no secrets
grep -E "sk-[a-f0-9]{32}|Gabriella|Mars|Cyrus|Desmonde|truenas|hairbrush" \
  ~/faultline-prod/openwebui/faultline_tool.py

# Expected: no output (clean)
```

**Files to Check for Sanitization:**
- `src/api/main.py` (hardcoded examples, debug code)
- `openwebui/faultline_tool.py` (Filter logic, comments)
- `src/re_embedder/embedder.py` (LLM examples)
- `migrations/` (SQL comments, data fixtures)
- `.env.example` (should be sanitized already)

### Step 3: Update Production Documentation

**ABOUT.md** — Add feature summary from this dprompt
```markdown
## v1.0.1 (dprompt-53b: Filter Simplification)

**Changes:**
- Simplified OpenWebUI Filter to remove three-tier gating logic
- Filter now trusts backend /query ranking (backend-first architecture)
- Removed entity-type filtering and Concept-entity gating
- Result: Category queries ("our pets", "my family") now return correct facts

**Impact:**
- Zero UUID leaks in query responses
- All entity types flow through unchanged
- Backend ontology/hierarchy changes no longer require Filter updates
```

**CHANGELOG.md** (if exists, or create)
```markdown
# Changelog

## [1.0.1] - 2026-05-12

### Changed
- Filter simplification: removed three-tier relevance gating
- Filter now backend-first: injects /query results in returned order
- Removed entity-type parameter passing between backend and Filter

### Fixed
- dBug-report-001: Tier 2 identity fallback no longer blocks Tier 3 after Concept filtering
- Category queries ("our pets", "my family") now return all relevant facts

### Architecture
- Paradigm shift: Filter is dumb, backend is smart
- See docs/ARCHITECTURE_QUERY_DESIGN.md
```

**README.md** (if needed)
- Update feature list to reflect new Filter behavior
- Update quick-start examples if needed

### Step 4: Validate Production Build

**Before committing, ensure faultline-prod can build and start:**

```bash
cd ~/faultline-prod

# Verify file integrity
ls -la src/api/main.py openwebui/faultline_tool.py migrations/

# Syntax check (if Python files)
python -m py_compile openwebui/faultline_tool.py

# Docker validation
docker compose config  # validates docker-compose.yml

# Optional: test build (careful — uses resources)
docker compose build --no-cache  # builds images without cache
```

**If validation fails:**
- Check error message (syntax, missing files, etc.)
- Revert changes to faultline-prod
- Investigate root cause in FaultLine-dev
- Fix and re-copy

### Step 5: Commit & Tag

**Commit in faultline-prod:**
```bash
cd ~/faultline-prod

git add -A
git commit -m "dprompt-53b: Filter simplification — backend-first architecture

- Removed three-tier gating logic from Filter
- Removed entity-types parameter passing
- Simplified _filter_relevant_facts() to confidence + identity gating
- Filter now trusts backend /query ranking

See: docs/ARCHITECTURE_QUERY_DESIGN.md
Fixes: dBug-report-001"
```

**Tag the release:**
```bash
git tag -a v1.0.1 -m "Filter simplification (dprompt-53b)

Features:
- Simplified OpenWebUI Filter (backend-first architecture)
- Category queries now return correct facts
- Zero UUID leaks in responses

Fixes dBug-report-001: Tier 2 fallback blocking Tier 3"

git tag -l  # verify tag created
```

### Step 6: Push to GitHub

```bash
cd ~/faultline-prod

# Push commits
git push origin main

# Push tags
git push origin --tags

# Verify
git log --oneline -3  # see recent commits
git tag -l | head -5  # see recent tags
```

**Expected output:**
```
To https://github.com/tkalevra/FaultLine.git
   [hash]..[hash] main -> main
 * [new tag] v1.0.1 -> v1.0.1
```

---

## Post-Deployment Verification

### Manual Checks

**1. Repository State**
```bash
cd ~/faultline-prod
git status  # should be clean
git log --oneline -1  # should show deployment commit
git describe --tags  # should show v1.0.1
```

**2. File Integrity**
```bash
# No secrets leaked
grep -r "sk-[a-f0-9]" ~/faultline-prod/ 2>/dev/null | grep -v ".git"
# Expected: no output

# No personal names in code
grep -r "Gabriella\|Mars\|Cyrus" ~/faultline-prod/src/ ~/faultline-prod/openwebui/ 2>/dev/null
# Expected: no output (or only in tests/fixtures)
```

**3. Documentation Updated**
```bash
# Check ABOUT.md mentions this release
head -20 ~/faultline-prod/ABOUT.md | grep -i "Filter\|v1.0.1"

# Check CHANGELOG.md exists and has entry
head -20 ~/faultline-prod/CHANGELOG.md | grep "Filter simplification"
```

---

## Rollback Procedure

**If deployment breaks production:**

```bash
cd ~/faultline-prod

# Revert to previous tag
git revert HEAD  # creates undo commit
# OR
git reset --hard v1.0.0  # hard reset to previous release

# Push rollback
git push origin main
```

**Then investigate:** What failed? Why? Fix in FaultLine-dev, re-validate locally, then re-deploy.

---

## Environment-Specific Notes

### faultline-prod Differences from FaultLine-dev

| Aspect | FaultLine-dev | faultline-prod |
|--------|---------------|----------------|
| Purpose | Development & testing | Production deployment |
| `.env` | `.env.example` (no secrets) | `.env` (with real secrets) |
| Debug code | Allowed | None |
| Personal data | In fixtures/comments | Sanitized/removed |
| Git history | Full dprompt history | Clean, release-focused |
| Tests | Run locally before commit | No test suite (prod only) |
| Documentation | CLAUDE.md, dprompt-*.md | ABOUT.md, DEPLOYMENT.md, README.md |

### Files NOT to Copy

- ❌ `scratch.md` (dialogue, not production)
- ❌ `dprompt-*.md` (internal specs, not user-facing)
- ❌ `compact-instruct.md` (development instructions)
- ❌ `CLAUDE.md` (internal architecture notes)
- ❌ `tests/` (development only)
- ❌ `Archive/`, `BUGS/` (internal only)
- ❌ `.env` (secrets, not committed)

### Files to Maintain/Update

- ✅ `src/`, `openwebui/`, `migrations/` (core code)
- ✅ `ABOUT.md` (user-facing feature summary)
- ✅ `CHANGELOG.md` or release notes
- ✅ `README.md` (quick start)
- ✅ `LICENSE` (MIT, unchanging)
- ✅ `.env.example` (template, no secrets)
- ✅ `docker-compose.yml` (deployment config)
- ✅ `Dockerfile` (build instructions)

---

## Checklist for Deepseek

When told "Deploy dprompt-XX to production":

1. [ ] **Code Complete** — dprompt implemented, tested, committed in FaultLine-dev
2. [ ] **Files Identified** — know exactly which files changed
3. [ ] **Audit for Secrets** — grep for tokens, names, IPs, emails
4. [ ] **Copy with Sanitization** — copy files, inspect, sanitize inline if needed
5. [ ] **Update Docs** — ABOUT.md, CHANGELOG.md with feature summary
6. [ ] **Validate Build** — `docker compose config`, `python -m py_compile`
7. [ ] **Commit & Tag** — commit with clear message, tag as `v[VERSION]`
8. [ ] **Push** — `git push origin main && git push origin --tags`
9. [ ] **Verify Post-Deploy** — check repository state, no secrets, docs updated
10. [ ] **Report** — update scratch.md with results, **STOP** for user verification

---

## Example: Deploying dprompt-53b

**Step 1: Code Complete**
- dprompt-53b implemented ✓
- Local tests pass ✓
- Committed to FaultLine-dev ✓

**Step 2: Changed Files**
- `openwebui/faultline_tool.py` (1621 → 1579 lines)

**Step 3: Audit**
```bash
grep -r "sk-\|Gabriella\|truenas\|hairbrush" openwebui/faultline_tool.py
# (No output — clean)
```

**Step 4: Copy**
```bash
cp /home/chris/Documents/013-GIT/FaultLine-dev/openwebui/faultline_tool.py \
   ~/faultline-prod/openwebui/faultline_tool.py
```

**Step 5: Update Docs**
```markdown
# ABOUT.md entry
## v1.0.1 (Filter Simplification)

Filter now implements backend-first architecture:
- Removed three-tier gating logic
- Trusts /query ranking (A > B > C + confidence)
- Category queries work correctly
```

**Step 6: Validate**
```bash
cd ~/faultline-prod
python -m py_compile openwebui/faultline_tool.py
docker compose config
```

**Step 7: Commit & Tag**
```bash
cd ~/faultline-prod
git add -A
git commit -m "dprompt-53b: Filter simplification — backend-first architecture"
git tag -a v1.0.1 -m "Filter simplification (dprompt-53b)"
```

**Step 8: Push**
```bash
git push origin main
git push origin --tags
```

**Step 9: Verify**
```bash
git status  # clean
git describe --tags  # v1.0.1
grep -r "sk-" ~/faultline-prod/ 2>/dev/null | grep -v ".git"  # no output
```

**Step 10: Report**
```markdown
## ✓ DONE: dprompt-54b (Production Deployment) — 2026-05-12

**Deployed:**
- openwebui/faultline_tool.py (Filter simplification)

**Validation:**
- Syntax: clean ✓
- Docker: valid ✓
- Secrets: none found ✓
- Documentation: ABOUT.md updated ✓

**Tagging:**
- Commit: [hash] "dprompt-53b: Filter simplification..."
- Tag: v1.0.1

**Status:** AWAITING USER VERIFICATION

STOP — user should verify GitHub repo and run live tests.
```

---

## Summary

**Three-step deployment cycle:**
1. **Identify Changed Files** — know exactly what's moving from dev to prod
2. **Sanitize** — no secrets, no personal data, no debug code
3. **Validate** — build succeeds, docs updated, tagged and pushed

**Deepseek's role:** Follow this guide step-by-step. STOP before verification. User confirms.

---

**Version:** 1.0  
**Last Updated:** 2026-05-12  
**Reference:** docs/DEV_CYCLE_GUIDE.md, docs/ARCHITECTURE_QUERY_DESIGN.md, dprompt-template.md
