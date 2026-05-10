# dprompt-40 — Production Cleanup: Legacy Code & Personal Data Removal

## Purpose

Review the codebase, identify and remove all legacy/debug code, strip out personal information and references, and prepare a clean production-ready branch. Final deliverable: a sanitized repo suitable for deployment without exposing personal details or dead code.

## Scope

### What to Remove/Clean

**Legacy Code:**
- `src/extraction/compound.py` — marked as legacy in CLAUDE.md (dprompt-22), no longer in critical path
- `check_entity_ids.py` — appears to be a debug/utility script, not core
- `gabriella_debug.log` — test artifact from dprompt-31b
- `migrations/020_nested_layers.sql` — marked OBSOLETE (dprompt-24/25 revert)
- Any `*.pyc`, `__pycache__`, `.pytest_cache` artifacts
- `.deepseek/state/` directory (internal agent state)

**Personal Information to Remove/Sanitize:**
- Names in example data: Gabriella, Cyrus, Desmonde, Mars, Sarah, Alice, etc.
  - Replace with: Alice, Bob, Carol, Device-X, etc. (generic)
- Email addresses: `ct8ball@gmail.com`
  - Remove from code, tests, comments
- Personal user references: "Christopher", "Chris"
  - Replace with: "User", "Admin" where appropriate
- Server names: "truenas", "hairbrush.helpdeskpro.ca"
  - Replace with: `<server>`, `<hostname>`, placeholders
- Bearer tokens and API keys in examples
  - Replace with: `<bearer-token>`, `<api-key>` placeholders
- SSH/local paths: `/mnt/tank/`, `/home/chris/`
  - Replace with: `/data/`, `$HOME/` or placeholders
- Company/business references: helpdeskpro.ca, Optimus, etc.
  - Decide: keep if public info, remove if internal

**Documentation Cleanup:**
- CLAUDE.md: Remove personal context stories, keep technical reference
- dprompt files: Replace personal example names with generic equivalents
- Test files: Sanitize test data (names, emails, addresses)
- README/setup docs: Remove personal deployment paths, use env vars
- Code comments: Remove personal context ("Christopher's system", etc.)

**Configuration:**
- Hardcoded URLs/endpoints in code → env vars
- Hardcoded paths → env vars or relative paths
- Local development settings → defaults + env override

### What to Keep

- Core business logic (all of src/)
- Schema migrations (production-critical)
- Test suite (dprompt-29b, dprompt-30b, dprompt-33b suites)
- All dprompt-N.md files (reference specs — valuable for understanding)
- CLAUDE.md (project reference — valuable, just sanitize personal context)
- All recent commits (git history is clean)

## Cleanup Strategy

### Phase 1: Legacy Code Removal
1. Delete obsolete files
2. Verify no imports reference deleted files
3. Run test suite to confirm no breakage

### Phase 2: Personal Data Removal
1. Scan all files for personal keywords
2. Replace names in code, tests, comments
3. Replace URLs/paths with placeholders or env vars
4. Sanitize dprompt files (replace example names)
5. Review CLAUDE.md for personal context, rewrite as generic reference

### Phase 3: Configuration Hardening
1. Identify hardcoded values (URLs, paths, tokens)
2. Convert to env vars with defaults
3. Update .env.example with all required vars

### Phase 4: Final Review
1. Run test suite (must pass 100%)
2. Verify no secrets in git history
3. Verify no personal identifiers remain
4. Create summary of all changes

## Files Affected

| File | Action | Reason |
|------|--------|--------|
| `src/extraction/compound.py` | DELETE | Legacy (dprompt-22), not in critical path |
| `check_entity_ids.py` | DELETE | Debug utility, not production code |
| `gabriella_debug.log` | DELETE | Test artifact |
| `migrations/020_nested_layers.sql` | DELETE | Marked OBSOLETE (reverted in dprompt-24/25) |
| `.deepseek/state/` | DELETE | Internal agent state, not production |
| All `*.py` in src/ | SANITIZE | Remove personal context from comments |
| `tests/` | SANITIZE | Replace test data names (Gabriella → Carol, etc.) |
| `dprompt-*.md` | SANITIZE | Replace example names with generic |
| `CLAUDE.md` | SANITIZE | Remove personal stories, keep technical ref |
| `openwebui/faultline_tool.py` | SANITIZE | Remove personal URLs from comments/examples |
| `.env.example` | CREATE/UPDATE | All env vars documented |

## Personal Data Checklist

**Names to replace:**
- [ ] Gabriella, Cyrus, Desmonde, Mars, Sarah, Alice → generic names (Carol, Bob, Device-X, etc.)
- [ ] Christopher, Chris → "User" or "Admin"

**URLs/Domains to sanitize:**
- [ ] hairbrush.helpdeskpro.ca → `<hostname>` or env var
- [ ] helpdeskpro.ca → remove or generic
- [ ] http://localhost → env var with default

**Email addresses:**
- [ ] ct8ball@gmail.com → remove or `<user-email>`

**Paths:**
- [ ] /mnt/tank/ → `<data-dir>` or env var
- [ ] /home/chris/ → $HOME or relative
- [ ] /truenas → `<server>` or env var

**Credentials/Tokens:**
- [ ] Bearer tokens in examples → `<bearer-token>`
- [ ] API keys → `<api-key>`
- [ ] Database credentials → env vars only

**Company/Business references:**
- [ ] Optimus → remove if internal
- [ ] University of Guelph → remove if personal context
- [ ] Any internal business names → remove or generic

## Success Criteria

- All legacy code removed ✓
- All personal information sanitized ✓
- Test suite: 112+ passed, 0 failures ✓
- No hardcoded secrets or personal URLs ✓
- .env.example complete and documented ✓
- Git history clean (no dangling references) ✓
- New branch created from master ✓
- Master branch left untouched ✓

## Output

**Files:**
- New branch: `cleanup/remove-legacy-and-personal-info`
- .env.example (complete)
- Cleanup summary (what was removed, what was changed)

**Verification:**
- Test suite passes
- All personal data removed
- No hardcoded values remain
- Ready for public/shared deployment
