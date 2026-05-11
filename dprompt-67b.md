# dprompt-67b: DEEPSEEK_INSTRUCTION_TEMPLATE — Documentation Audit & Consistency Sync

## Task

Audit all documentation files (CLAUDE.md, ABOUT.md, README.md, CHANGELOG.md, .env.example) across FaultLine-dev and faultline-prod for consistency and accuracy against v1.0.7 implementation. Update both repos with synchronized, factually correct versions.

## Context

v1.0.7 deployment complete. Documentation was updated in production (ABOUT.md) but consistency across both repos and CLAUDE.md accuracy need verification.

**Why:** Documentation is the source of truth for architecture, features, and usage. Stale docs cause:
- Incorrect architecture understanding
- Misleading feature descriptions
- Confusion about version capabilities
- Integration issues for new team members

**Integration:** Maintenance task. No code changes. Pure documentation updates.

**Reference:** `dprompt-67.md`

## Constraints

### MUST:
- Audit CLAUDE.md (dev) for accuracy vs v1.0.7 implementation — query dedup, metadata validation, bug fixes
- Audit ABOUT.md (prod) for accuracy and completeness (v1.0.1 through v1.0.7)
- Verify README.md, CHANGELOG.md, .env.example consistency across repos
- Update any stale documentation (no sensitive data, no personal examples)
- Commit updates to respective repos:
  - Changes to CLAUDE.md → FaultLine-dev
  - Changes to ABOUT.md, README.md, CHANGELOG.md → faultline-prod
  - Changes to .env.example → both repos (identical)
- Zero documentation will claim features that don't exist
- Zero stale version references
- Pass syntax/markdown checks

### DO NOT:
- Modify code
- Add speculation (only document actual implementation)
- Leave personal names in examples (use generic names)
- Break any existing documentation structure

### MAY:
- Reorganize sections for clarity
- Add examples if they aid understanding
- Clarify ambiguous descriptions

## Sequence

### 1. Read & Compare (No changes)

**Read both CLAUDE.md versions:**
- FaultLine-dev: `/home/chris/Documents/013-GIT/FaultLine-dev/CLAUDE.md`
- (Production has none — CLAUDE.md is dev-only internal reference)

**Read production docs:**
- `~/faultline-prod/ABOUT.md`
- `~/faultline-prod/README.md`
- `~/faultline-prod/CHANGELOG.md`
- `~/faultline-prod/.env.example`

**Check dev equivalents (if exist):**
- `FaultLine-dev/README.md`
- `FaultLine-dev/.env.example`

**Compare for:**
- Factual accuracy (features described actually exist in v1.0.7)
- Consistency (same information described same way in both)
- Completeness (all v1.0.7 changes documented)
- Stale references (old version numbers, deprecated features)
- Personal data (names, emails, IPs, server names — should be generic)

**Confirm:** What inconsistencies or stale info did you find?

### 2. Update CLAUDE.md (FaultLine-dev)

**File:** `CLAUDE.md` (architecture reference)

**Check/Update sections:**

1. **Query deduplication (dprompt-66):**
   - Current state: Likely mentions dprompt-61 but not UUID-based dedup fix
   - Update: Add section explaining pg_keys now use `_subject_id`/`_object_id` instead of display names
   - Impact: Eliminates duplicate facts with different aliases

2. **Metadata-driven validation (dprompt-65):**
   - Current state: May reference hardcoded validation constants
   - Update: Confirm all refs to `_LEAF_ONLY_RELS`, `_BIDIRECTIONAL_INVERSES` removed
   - New state: Validation rules in `rel_types` table, `_get_rel_type_metadata()` queries at runtime

3. **Bug fixes section:**
   - Add: dBug-report-008 (query deduplication) — now fixed in v1.0.7
   - Confirm: dBug-001 through dBug-007 marked as closed

4. **Version references:**
   - Update: Current production = v1.0.7
   - Update: Pipeline description reflects metadata-driven validation
   - Update: Query path describes UUID-based dedup

### 3. Verify Production Docs (ABOUT.md, README.md, CHANGELOG.md)

**Files:** `~/faultline-prod/ABOUT.md`, `~/faultline-prod/README.md`, `~/faultline-prod/CHANGELOG.md`

**Check v1.0.7 entry:**
- ABOUT.md: v1.0.7 section describes query dedup fix ✓ (already correct)
- CHANGELOG.md: Should have v1.0.7 with dprompt-66 details
- README.md: Should mention v1.0.7 (or be version-agnostic)

**Check all entries (v1.0.1 → v1.0.7):**
- Feature descriptions match actual implementation
- No stale promises or features that don't exist
- Dates consistent

### 4. Sync .env.example

**Files:** Both repos should have identical `.env.example`

**Verify:**
- Same variables listed
- Same defaults
- Same descriptions
- No secrets, no personal data

**If different:** Update both to match.

### 5. Commit Changes

**FaultLine-dev changes:**
```bash
cd /home/chris/Documents/013-GIT/FaultLine-dev
git add CLAUDE.md
git commit -m "docs: CLAUDE.md — update for v1.0.7 (metadata-driven validation, UUID dedup, bug fixes)

- Query deduplication: UUID-based pg_keys (dprompt-66)
- Metadata-driven validation: rel_types table, _get_rel_type_metadata() (dprompt-65)
- dBug-008 documented (query dedup fix)
- Version references updated to v1.0.7"
```

**faultline-prod changes:**
```bash
cd ~/faultline-prod
git add ABOUT.md README.md CHANGELOG.md .env.example
git commit -m "docs: production documentation audit — consistency sync v1.0.7

- ABOUT.md: verified v1.0.1 → v1.0.7 accuracy
- README.md: updated version references
- CHANGELOG.md: verified all releases documented
- .env.example: synced with dev version"
```

**Push both:**
```bash
# FaultLine-dev
git push origin master

# faultline-prod
git push origin main
```

### 6. STOP & Report

Update `scratch.md` with template below. Do NOT proceed further.

## Deliverable

**Modified files:**
- `CLAUDE.md` (FaultLine-dev) — architecture updated for v1.0.7
- `ABOUT.md` (faultline-prod) — verified accurate
- `README.md` (faultline-prod) — consistency check
- `CHANGELOG.md` (faultline-prod) — completeness verified
- `.env.example` (both) — synced

**Validation:**
- All docs factually accurate vs v1.0.7 implementation
- No stale version references
- No sensitive data
- Commits pushed to respective repos

## Success Criteria

✅ CLAUDE.md updated with v1.0.7 architecture (UUID dedup, metadata-driven validation)  
✅ All version references consistent (v1.0.7 current)  
✅ Bug fixes documented (dBug-001 through dBug-008)  
✅ Production docs verified accurate against implementation  
✅ .env.example synced across repos  
✅ Changes committed and pushed (dev→dev, prod→prod)  
✅ No sensitive data in any documentation  

## Upon Completion

**⚠️ MANDATORY: Update scratch.md with this template, then STOP:**

```markdown
## ✓ DONE: dprompt-67 (Documentation Audit & Consistency Sync) — [DATE]

**Task:** Audit all documentation across FaultLine-dev and faultline-prod for consistency and v1.0.7 accuracy.

**Changes:**

**CLAUDE.md (FaultLine-dev):**
- Updated query deduplication section: UUID-based pg_keys (dprompt-66)
- Updated metadata-driven validation: rel_types table architecture (dprompt-65)
- Added dBug-008 resolution documentation
- Version references updated to v1.0.7

**ABOUT.md, README.md, CHANGELOG.md (faultline-prod):**
- Verified v1.0.1 through v1.0.7 entries accurate
- Confirmed no stale version references
- Checked all feature descriptions match implementation

**.env.example:**
- Synced across both repos (identical)

**Validation:**
- All docs factually accurate vs v1.0.7 ✓
- No sensitive data ✓
- Consistency verified ✓

**Commits:**
- FaultLine-dev: [hash] "docs: CLAUDE.md — update for v1.0.7"
- faultline-prod: [hash] "docs: production documentation audit — consistency sync v1.0.7"
- Both pushed ✓

**Status:** AWAITING USER REVIEW

Ready to work from here.
```

Then **STOP immediately** — do not proceed, user will review and give next direction.

## Critical Rules (Non-Negotiable)

**Factual accuracy:** Only document what exists in v1.0.7. No speculation or promises.

**Consistency:** Same info described same way in both repos (modulo dev-only vs prod-only sections).

**No sensitive data:** Generic names, no personal examples, no credentials.

**Repo separation:** Dev changes to dev, prod changes to prod, both synced where appropriate.

**STOP clause mandatory:** Report findings, then STOP. User reviews documentation state and gives next direction.

---

**Template version:** 1.0 (follows DEEPSEEK_INSTRUCTION_TEMPLATE)  
**Philosophy:** Documentation is source of truth. Keep it factual, consistent, and current.  
**Status:** Ready for execution by deepseek

