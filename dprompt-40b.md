# dprompt-40b — Production Cleanup: Legacy Code & Personal Data Removal [PROMPT]

## #deepseek NEXT: dprompt-40b — Production Cleanup (Legacy Code & Personal Data) — 2026-05-10

### Task:

Create a clean production-ready branch by removing all legacy code and sanitizing personal information. Final deliverable: a sanitized codebase suitable for deployment without exposing personal details or dead code.

### Context:

FaultLine is production-ready (all 5 scenarios passing). Before deployment to production or shared repo, need to clean:
1. **Legacy code** — compound.py, debug scripts, obsolete migrations, test artifacts
2. **Personal data** — names (Gabriella, Mars, etc.), emails, server names, URLs, paths, tokens
3. **Configuration** — hardcoded values → env vars

End goal: public/shared repo with zero personal exposure and zero dead code.

### Constraints (CRITICAL):

- **Wrong: Delete files without checking imports. Mess up test suite. Leave personal data.**
- **Right: Carefully remove legacy, comprehensively sanitize personal data, verify test suite still 100% passes.**
- **MUST: Create new branch, do NOT modify master.**
- **MUST: Test suite must pass 100% (112+) after cleanup.**
- **MUST: No personal data remains (names, emails, URLs, tokens, paths).**
- **MUST: All hardcoded values converted to env vars or removed.**
- **MUST: Verify no secrets in git history before final push.**

### Sequence (DO NOT skip or reorder):

1. **Create new branch from master:**
   ```bash
   git checkout -b cleanup/remove-legacy-and-personal-info
   ```

2. **Phase 1 — Legacy Code Removal:**
   - Delete these files (no longer needed):
     ```bash
     rm src/extraction/compound.py
     rm check_entity_ids.py
     rm gabriella_debug.log
     rm migrations/020_nested_layers.sql
     rm -rf .deepseek/state/
     ```
   - Search for imports of deleted files:
     ```bash
     grep -r "compound" src/ tests/ --include="*.py"
     grep -r "check_entity_ids" . --include="*.py"
     ```
   - If any imports found, remove those import lines
   - Commit: `git add -A && git commit -m "cleanup: remove legacy code (compound.py, debug scripts, obsolete migrations)"`

3. **Phase 2 — Personal Data Sanitization:**

   **Replace example names** (in code, comments, test data):
   - `gabriella` / `Gabriella` → `carol` / `Carol`
   - `cyrus` / `Cyrus` → `bob` / `Bob`
   - `desmonde` / `Desmonde` → `david` / `David`
   - `mars` / `Mars` → `alice` (as partner) or `device-x` (as laptop)
   - `sarah` / `Sarah` → `emma` / `Emma`
   - `alice` / `Alice` → `frank` / `Frank`
   - Pattern: search-replace across entire repo:
     ```bash
     find . -name "*.py" -o -name "*.md" -o -name "*.sql" | xargs grep -l "gabriella\|Gabriella" | head -20
     ```
   - Use `sed` or editor to replace (be careful with case sensitivity)
   - Check files: `openwebui/faultline_tool.py`, `src/api/main.py`, `dprompt-*.md`, `tests/`, `CLAUDE.md`

   **Remove/sanitize URLs and servers:**
   - `hairbrush.helpdeskpro.ca` → `<hostname>` in comments/examples
   - `helpdeskpro.ca` → remove or mark as `<domain>`
   - `http://192.168.40.10:8001` → use env var `${FAULTLINE_API_URL:-http://localhost:8001}`
   - Files: `openwebui/faultline_tool.py` (check defaults and comments)

   **Remove email addresses:**
   - `ct8ball@gmail.com` → remove from code/comments
   - Check: `CLAUDE.md`, any code comments, test fixtures
   - Command: `grep -r "ct8ball\|@gmail" . --include="*.py" --include="*.md"`

   **Sanitize paths:**
   - `/home/chris/` → `$HOME/` or relative path
   - `/mnt/tank/` → `<data-dir>` in comments
   - Files to check: `CLAUDE.md`, `docker-compose*.yml`, any shell scripts
   - Command: `grep -r "/home/chris\|/mnt/tank" . --include="*.py" --include="*.md" --include="*.yml"`

   **Sanitize credentials in examples:**
   - `sk-addb2220bf534bfaa8f78d96e6991989` (bearer token) → `<bearer-token>` in examples
   - Database credentials in examples → `<user>`, `<password>`, or env var reference
   - Files: `dprompt-34b.md`, `CLAUDE.md`, any curl examples

   **Remove personal context from CLAUDE.md:**
   - Section "System Environment" — remove personal hardware details, keep only OS type
   - Section "User Profile" — remove name, location, company affiliation, keep only technical skills
   - Remove personal anecdotes, keep technical reference
   - Rewrite "Current Projects" as generic example project types

   **Commit:** `git add -A && git commit -m "cleanup: sanitize personal data (names, emails, URLs, paths, tokens)"`

4. **Phase 3 — Configuration Hardening:**
   - Search for hardcoded values:
     ```bash
     grep -r "localhost:3000\|localhost:8001\|localhost:6333" src/ openwebui/ --include="*.py"
     grep -r "postgresql://" . --include="*.py" --include="*.yml"
     grep -r "faultline-wgm" . --include="*.py" --include="*.md"
     ```
   - Check `openwebui/faultline_tool.py` for default URLs:
     - Should use env vars with sensible defaults
     - Example: `FAULTLINE_API_URL = os.getenv("FAULTLINE_API_URL", "http://faultline:8001")`
   - Check `src/api/main.py` for hardcoded endpoints, model names
   - Create/update `.env.example`:
     ```
     # FaultLine API Configuration
     FAULTLINE_API_URL=http://faultline:8001
     
     # PostgreSQL
     POSTGRES_DSN=postgresql://faultline:faultline@postgres:5432/faultline
     
     # Qdrant
     QDRANT_URL=http://qdrant:6333
     QDRANT_COLLECTION=faultline-default
     
     # LLM Configuration
     WGM_LLM_MODEL=qwen/qwen3.5-9b
     CATEGORY_LLM_MODEL=qwen2.5-coder
     QWEN_API_URL=http://localhost:11434/v1/chat/completions
     
     # OpenWebUI
     OPENWEBUI_API_URL=http://host.docker.internal:3000/api
     ```
   - Commit: `git add -A && git commit -m "cleanup: hardening configuration (env vars, .env.example)"`

5. **Phase 4 — Documentation Review:**
   - Open `CLAUDE.md` and review for remaining personal context
   - Remove: personal stories, specific names, local paths, personal projects
   - Keep: technical architecture, design decisions, key principles
   - Check all `dprompt-*.md` files for example names — replace with generic
   - Check `README.md` (if exists) for personal deployment instructions
   - Verify: no hardcoded URLs or paths in README

6. **Phase 5 — Final Verification:**
   - Run full test suite:
     ```bash
     pytest tests/api/ --ignore=tests/evaluation -v
     ```
     Expected: 112+ passed, 0 failures
   - Verify no secrets in git log:
     ```bash
     git log --all --oneline | head -20
     git log -p | grep -i "ct8ball\|sk-addb\|/home/chris" | wc -l
     ```
     Should return 0 lines
   - Search entire repo for remaining personal data:
     ```bash
     grep -r "gabriella\|Gabriella\|chris\|Chris\|ct8ball" . --include="*.py" --include="*.md" | grep -v "node_modules\|.git"
     ```
     Should return 0 (except where intentionally generic example names)

7. **Create cleanup summary:**
   - Document all files removed
   - Document all replacements made
   - Document all env vars added
   - Save as `CLEANUP_SUMMARY.md` (or similar)
   - Commit: `git add CLEANUP_SUMMARY.md && git commit -m "docs: cleanup summary (legacy removal, personal data sanitization)"`

8. **Push branch to origin:**
   ```bash
   git push -u origin cleanup/remove-legacy-and-personal-info
   ```

9. **Update scratch.md** with completion entry (use template below)

### Deliverable:

- **New branch:** `cleanup/remove-legacy-and-personal-info` (pushed to origin)
- **Master branch:** Untouched (can verify with `git log origin/master`)
- **Cleanup summary:** All changes documented
- **Test suite:** 112+ passed, 0 failures ✓
- **Personal data:** 0 instances remaining ✓
- **Hardcoded values:** All → env vars or removed ✓

### Success Criteria:

- Legacy code removed (compound.py, debug scripts, obsolete migrations) ✓
- All personal names replaced with generic equivalents ✓
- All emails, URLs, paths, tokens sanitized/removed ✓
- Configuration hardened (env vars with defaults) ✓
- .env.example complete and documented ✓
- Test suite: 112+ passed, 0 regressions ✓
- No personal data in git history ✓
- New branch created and pushed ✓
- Master untouched ✓

### Upon Completion:

**If cleanup passes all checks:**

Update scratch.md with:
```
## ✓ DONE: dprompt-40b (Production Cleanup) — 2026-05-10

**Cleanup completed successfully:**

**Legacy Code Removed:**
- src/extraction/compound.py (deprecated)
- check_entity_ids.py (debug utility)
- gabriella_debug.log (test artifact)
- migrations/020_nested_layers.sql (obsolete)
- .deepseek/state/ (internal state)

**Personal Data Sanitized:**
- Example names: Gabriella→Carol, Mars→Device-X, etc.
- Email addresses: removed
- Server names: sanitized to placeholders/env vars
- API tokens in examples: replaced with <bearer-token>
- Paths: sanitized to env vars

**Configuration Hardened:**
- Hardcoded URLs → env vars with sensible defaults
- .env.example created with all required variables
- No hardcoded secrets remain

**Validations:**
- Test suite: 112+ passed, 0 failures ✓
- Personal data scan: 0 instances found ✓
- Git history clean: no secrets detected ✓
- All files reviewed and sanitized ✓

**Deliverables:**
- New branch: cleanup/remove-legacy-and-personal-info
- Master branch: untouched, production-ready
- CLEANUP_SUMMARY.md: detailed changelog

**System ready for production deployment. Clean, sanitized, no personal exposure.**

Next: [awaiting direction on final deployment]
```

**If cleanup finds issues:**

Update scratch.md with:
```
## ⚠️ IN PROGRESS: dprompt-40b (Production Cleanup) — Cleanup phase X in progress

**Progress:**
- [Phase 1: Complete / In Progress / Blocked]
- [Phase 2: Complete / In Progress / Blocked]
- [Phase 3: Complete / In Progress / Blocked]

**Current issue:**
[What's being worked on / What's blocked]

**Status:** [Continue with next phase / Awaiting clarification]
```

### CRITICAL NOTES:

- **Master branch must remain untouched.** Cleanup happens on new branch only.
- **Test suite must pass 100%.** If any tests fail after cleanup, investigate and fix.
- **Personal data removal is comprehensive.** Every file, every comment, every example.
- **Hardcoded values are converted to env vars.** No hardcoded paths, URLs, or credentials.
- **Git history is clean.** No secrets in any commit (verify with grep search).
- **This is the final step before production.** Clean branch = safe to share/deploy.

### Motivation:

FaultLine is production-ready technically. But the codebase contains personal information (names, emails, server names, tokens) and dead code (compound.py, obsolete migrations). Before shipping to production or open source, clean slate: remove legacy, sanitize personal data, harden configuration. Result: a professional, shareable, deployable product.
