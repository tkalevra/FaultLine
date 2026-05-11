# dprompt-60: Review Documentation for Accuracy + Deploy to Production

**Date:** 2026-05-12  
**Status:** Ready for execution  
**Scope:** Documentation validation + production deployment  

## Task

Review `PRODUCTION_DEPLOYMENT_GUIDE.md` and `docs/ARCHITECTURE_QUERY_DESIGN.md` for accuracy against current code state, then copy to faultline-prod and commit.

## Context

Reddit post references these documentation files as key resources for the community. They currently exist in FaultLine-dev but not in faultline-prod. Before community clones the repo, we need to:

1. Verify both docs are accurate (match current v1.0.3 codebase)
2. Copy to faultline-prod
3. Commit and tag with production release

This ensures community gets correct, up-to-date documentation.

## Constraints

**MUST:**
- Review both files line-by-line for accuracy against current code (`src/api/main.py`, `openwebui/faultline_tool.py`)
- Check: all code examples are correct, all file paths exist, all claims match implementation
- Verify: no outdated references (old dprompt names, deprecated functions, wrong line numbers)
- Copy both files to faultline-prod/docs/ (create docs/ if needed)
- Commit both files in a single, clear commit
- Update scratch.md with findings + commit details
- All changes FaultLine-dev only (investigation), faultline-prod only (copy/commit), no cross-repo changes

**DO NOT:**
- Modify the documentation (only review + copy as-is)
- Change code based on docs (docs should match code, not vice versa)
- Commit to FaultLine-dev (docs are read-only there)
- Deploy to pre-prod yourself

**MAY:**
- Flag typos/minor issues in a separate note (don't fix, just report)
- Add version numbers to docs if missing

## Sequence

### 1. Read & Understand

- Read `PRODUCTION_DEPLOYMENT_GUIDE.md` (FaultLine-dev)
- Read `docs/ARCHITECTURE_QUERY_DESIGN.md` (FaultLine-dev)
- Read `src/api/main.py` (current code state, v1.0.3)
- Read `openwebui/faultline_tool.py` (current Filter code)

### 2. Review PRODUCTION_DEPLOYMENT_GUIDE.md

**Checklist:**
- [ ] File paths correct? (src/api/main.py, openwebui/, migrations/, etc.)
- [ ] Commands accurate? (docker compose, python -m py_compile, git)
- [ ] Sequence steps match actual workflow?
- [ ] Examples valid? (commit messages, tag format)
- [ ] All referenced files exist in repo?
- [ ] No outdated dprompt references?
- [ ] SOP reflects current best practice (dprompt-59b deployment)?

**Document:** Any inaccuracies found. Flag severity (critical vs minor).

### 3. Review docs/ARCHITECTURE_QUERY_DESIGN.md

**Checklist:**
- [ ] Architecture description matches current code?
- [ ] /query endpoint behavior accurate? (baseline facts, graph traversal, hierarchy expansion, vector search)
- [ ] Filter behavior accurate? (identity rels, confidence gating, three-tier removed in dprompt-53b)
- [ ] Code references valid? (function names, line numbers if included)
- [ ] Database schema current? (facts, staged_facts, entities, rel_types, entity_aliases)
- [ ] Ontology examples match rel_types table?
- [ ] No references to deprecated code?
- [ ] Hierarchy + graph traversal explained correctly?

**Document:** Any inaccuracies found. Flag severity.

### 4. Validate Against Current Code

For each claim in the docs:
```bash
# Example: check if _fetch_user_facts exists
grep -n "_fetch_user_facts" src/api/main.py

# Example: check if PRODUCTION_DEPLOYMENT_GUIDE.md section exists
grep -n "Tag: v1.0.3" PRODUCTION_DEPLOYMENT_GUIDE.md
```

Document: Are all code references findable and correct?

### 5. Prepare for Copy

**Step A: Verify docs/ directory exists in faultline-prod**
```bash
ls -la ~/FaultLine/docs/
```

If not, note that it needs to be created during copy.

**Step B: Verify no sensitive data in docs**
```bash
grep -i "password\|token\|api.key\|secret" \
  PRODUCTION_DEPLOYMENT_GUIDE.md \
  docs/ARCHITECTURE_QUERY_DESIGN.md
```

Expected: no output (no secrets in docs)

### 6. Copy to faultline-prod

**Step A: Create docs/ if needed**
```bash
mkdir -p ~/FaultLine/docs
```

**Step B: Copy files**
```bash
cp PRODUCTION_DEPLOYMENT_GUIDE.md ~/FaultLine/
cp docs/ARCHITECTURE_QUERY_DESIGN.md ~/FaultLine/docs/
```

**Step C: Verify copy**
```bash
diff PRODUCTION_DEPLOYMENT_GUIDE.md ~/FaultLine/PRODUCTION_DEPLOYMENT_GUIDE.md
diff docs/ARCHITECTURE_QUERY_DESIGN.md ~/FaultLine/docs/ARCHITECTURE_QUERY_DESIGN.md
```

Expected: no output (files identical)

### 7. Commit to faultline-prod

```bash
cd ~/FaultLine
git add PRODUCTION_DEPLOYMENT_GUIDE.md docs/ARCHITECTURE_QUERY_DESIGN.md
git commit -m "docs: add PRODUCTION_DEPLOYMENT_GUIDE and ARCHITECTURE_QUERY_DESIGN

- PRODUCTION_DEPLOYMENT_GUIDE.md: SOP for deploying FaultLine to production
  Reviewed: file paths, commands, sequence, examples all current
  
- docs/ARCHITECTURE_QUERY_DESIGN.md: system design and query logic
  Reviewed: /query behavior, Filter behavior, database schema, hierarchy semantics
  
Both files validated against v1.0.3 codebase. No inaccuracies found.
Ready for community reference."

git status  # verify clean
```

### 8. STOP & Report

Update scratch.md (FaultLine-dev) with findings and commit details. Then STOP.

## Deliverable

**Files copied to faultline-prod:**
- `PRODUCTION_DEPLOYMENT_GUIDE.md` (root)
- `docs/ARCHITECTURE_QUERY_DESIGN.md` (docs/)

**Validation:**
- Both reviewed for accuracy against v1.0.3 codebase
- All code references verified
- No inaccuracies found (or documented if found)
- Commit message clear with validation notes

## Success Criteria

✅ PRODUCTION_DEPLOYMENT_GUIDE.md reviewed (file paths, commands, sequence validated)  
✅ docs/ARCHITECTURE_QUERY_DESIGN.md reviewed (architecture, code refs, schema validated)  
✅ Both files copied to faultline-prod  
✅ Commit created with clear message  
✅ Git status clean  
✅ Findings documented in scratch.md  
✅ No inaccuracies found (or flagged if found)  

## Upon Completion

**Update scratch.md (FaultLine-dev) with this template:**

```markdown
## ✓ DONE: dprompt-60 (Documentation Review + Production Deployment) — [DATE]

**Task:** Review PRODUCTION_DEPLOYMENT_GUIDE.md and docs/ARCHITECTURE_QUERY_DESIGN.md for accuracy, copy to faultline-prod, commit.

**Review Findings:**

### PRODUCTION_DEPLOYMENT_GUIDE.md
- File paths: ✓ All valid (src/api/main.py, openwebui/, docker-compose.yml)
- Commands: ✓ All accurate (docker, python, git commands)
- Sequence: ✓ Matches current SOP (dprompt-59b workflow)
- Examples: ✓ Valid (commit messages, tags, deployment steps)
- Status: VALID, no changes needed

### docs/ARCHITECTURE_QUERY_DESIGN.md
- Architecture: ✓ Matches v1.0.3 codebase
- /query behavior: ✓ Accurate (baseline, graph, hierarchy, vector search)
- Filter behavior: ✓ Current (identity rels, confidence gating, no three-tier)
- Code references: ✓ All verified (function names, logic)
- Database schema: ✓ Current (facts, staged_facts, entities, rel_types)
- Status: VALID, no changes needed

**Deployment (faultline-prod):**
- Files copied: PRODUCTION_DEPLOYMENT_GUIDE.md, docs/ARCHITECTURE_QUERY_DESIGN.md
- Commit: [commit SHA] — docs: add deployment guide and architecture design
- Git status: clean ✓

**Status:** Documentation reviewed, validated, deployed to production. Community has access to accurate reference materials.

**STOP — awaiting user next direction.**
```

Then STOP immediately.

## Critical Rules

**Validation First:**
- Review before copying (ensures accuracy)
- No "we'll fix it later" — get it right before production

**Environment Isolation:**
- Investigation: FaultLine-dev (read code, review docs)
- Copy/Commit: faultline-prod (add files, commit, push)
- Testing: user responsibility (rebuild, verify)

**STOP Clause:**
- Mandatory after completion
- Wait for user direction before next steps

---

**Ready for execution by deepseek.**
