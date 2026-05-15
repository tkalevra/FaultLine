# FaultLine System Prompt for Claude Code

## TRUST THE DATA

- **Database queries are authoritative** — trust psql output over assumptions
- **Container logs are ground truth** for behavior verification
- **Git state and commits define what's actually deployed** — verify against `git log`
- **Test results override mental models** — evidence beats intuition
- **Logs speak louder than code comments** — follow what actually happens, not what code says should happen

## WORKFLOW

- **Read scratch.md FIRST** — follow its CLAUDE/DEEPSEEK structure, NOT your own judgment
  - CLAUDE entries: request investigation with files/steps/expected responses
  - DEEPSEEK entries: respond with findings/analysis/recommendations
  - Both require proper globbing (CLAUDE-N, DEEPSEEK-NA/B/C)
- **File bugs in BUGS/** with proper globbing (`dBug-NNN-name.md`), not in chat
- **Commit to dev only** — user handles prod sync via deploy process
- **Never rebuild containers or push to remote** — user owns those actions
- **Test through production endpoint** (hairbrush.helpdeskpro.ca) not localhost

## COMMUNICATION

- **Verify before claiming** — check logs/DB for evidence before reporting success/failure
- **Follow instructions exactly** — scratch.md is law, not suggestion
- **Use proper formatting:**
  - Scratch.md: `###################################CLAUDE-<glob>##################################`
  - Bug reports: `dBug-NNN-description.md`
  - Prompts: `dprompt-NNN-description.md`
- **Keep scratch.md lean** — questions/dialogue only, no code dumps
- **Don't assume** — verify state with commands before taking action

## ARCHITECTURE DECISIONS

- **Graph traversal + hierarchy are separate systems** — don't conflate connectivity with classification
- **Backend is authoritative, Filter is dumb** — trust `/query` ranking over frontend filters
- **UUID-based dedup, never display names** — prevents entity loss when aliases vary
- **Metadata-driven validation, no hardcoded rules** — rel_types table is the source of truth
- **Write-through for Class A, staging for Class B/C** — lifecycle matters for visibility
- **Entity resolution happens at write time, not query time** — normalize early, dedupe late

## ANTI-PATTERNS TO AVOID

- ❌ Trying to fix things without reading logs first
- ❌ Assuming code does what the comment says (verify in logs)
- ❌ Using localhost:8001 for testing (use hairbrush.helpdeskpro.ca with bearer token)
- ❌ Creating scratch.md entries in chat instead of proper format
- ❌ Implementing code changes before scratch.md approval
- ❌ Hardcoding rules instead of using metadata-driven queries
- ❌ Rebuilding containers or pushing to remote yourself

## DECISION TREE

**I found a problem. What now?**
1. ✓ Verify with database/logs (don't assume)
2. ✓ Write CLAUDE entry in scratch.md asking for investigation
3. ✓ Wait for analysis (don't implement solo)
4. ✓ Review findings, approve approach
5. ✓ Implement only after approval
6. ✓ Verify fix via logs/DB
7. ✓ Commit to dev only
8. ✓ Wait for user rebuild/test

**Code says X but logs show Y**
- Logs win. Verify what's actually deployed, not what code claims.

**Test passes locally but fails in prod**
- Local ≠ prod. Test against hairbrush.helpdeskpro.ca. User handles container state.

**Should I rebuild the container?**
- No. User rebuilds. Your job: code changes only.

**Where does this go?**
- Code: directly into source files
- Bugs: BUGS/dBug-NNN.md
- Design: scratch.md as CLAUDE entry
- Prompts: dprompt-NNN.md
- Docs: CLAUDE.md or project README
