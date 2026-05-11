# dprompt-TEMPLATE-B: DEEPSEEK_INSTRUCTION_TEMPLATE Format

**Use this template for all executable prompts to deepseek.**

---

## Task

[One-sentence goal. No ambiguity. Examples:
- "Simplify the Filter to remove three-tier gating logic"
- "Add entity-type-aware validation to ingest pipeline"
- "Fix UUID leak in query display name resolution"
]

## Context

[2-3 paragraphs establishing:
1. What problem are we solving?
2. Why does it matter? (impact)
3. What have we learned from prior attempts?
4. Where can deepseek read detailed background?

Example:
"Filter implements Tier 1/2/3 logic with Concept filtering. When Tier 1 returns empty due to Concept entity filtering, Tier 2 fallback fires (intended for generic queries like 'how are you'), blocking Tier 3 from running. Category queries like 'tell me about our pets' hit this path and lose all has_pet facts.

Read: docs/ARCHITECTURE_QUERY_DESIGN.md (why Filter should be dumb), dprompt-53.md (specification)."
]

## Constraints

**MUST:**
- [Hard requirement 1]
- [Hard requirement 2]
- Environment: Investigation in pre-prod via SSH, code in FaultLine-dev, deployment user-triggered
- Maintain all existing tests (112+ pass, 0 new failures)
- No premature optimization beyond scope
- [Lock the model if API involved: "ONLY test with faultline-wgm-test-10"]
- [Lock the scope: "Tests only, no production code changes"]

**DO NOT:**
- [Explicitly prohibited action 1]
- [Explicitly prohibited action 2]
- Deploy to pre-prod yourself (wait for STOP clause)
- Commit changes to faultline-prod repo
- Refactor beyond this dprompt's scope
- Add new configuration options unless asked
- Switch models or backends without explicit approval

**MAY:**
- [Optional improvement 1, if it helps clarity]
- [Optional enhancement 2]
- Consolidate helper functions if it improves readability
- Add defensive comments explaining non-obvious logic

## Sequence

**CRITICAL: Follow this sequence exactly. Do not skip steps.**

1. **Read & Understand (No coding yet)**
   - Read `dprompt-[X].md` (specification)
   - Read `CLAUDE.md` section on [relevant topic]
   - Read any architecture docs referenced
   - Ask clarifying questions in scratch.md if needed (prefixed #deepseek)

2. **Investigate (Pre-Prod Only)**
   - SSH to truenas: `ssh truenas -x "sudo docker logs [container] --tail N"`
   - Query database if needed: `ssh truenas -x "sudo docker exec faultline-postgres psql..."`
   - Document findings in local notes (don't commit)
   - If investigation reveals unexpected state, report in scratch.md before proceeding

3. **Analyze Local Code (FaultLine-dev)**
   - Read the file(s) to be modified
   - Identify: what needs to change, what stays, what gets deleted
   - Document your analysis (mental or brief notes — don't commit analysis)

4. **Implement (FaultLine-dev Only)**
   - Make changes to identified files
   - [Implementation-specific steps, if multi-phase]
   - Follow existing code style (indent, naming, comments)
   - Commit changes as one logical commit (don't split artificially)

5. **Validate Locally (FaultLine-dev)**
   - Syntax check: `python -m py_compile [file.py]`
   - Run test suite: `pytest tests/api/ --ignore=tests/evaluation -v`
   - Document results: pass count, skipped count, any new failures
   - If failures: diagnose and fix before proceeding to STOP

6. **STOP & Report**
   - Update scratch.md with completion template (provided below)
   - Include: files changed, lines before/after, test results, any findings
   - **Do NOT proceed to live testing** — wait for user rebuild/redeploy
   - **STOP clause is mandatory** — await explicit direction before next steps

## Deliverable

[Describe what will be delivered. Examples:
- "Modified file: openwebui/faultline_tool.py — simplified _filter_relevant_facts() function"
- "New file: migrations/023_entity_validation.sql — add age constraint"
- "Updated: src/api/main.py — integrated confidence-based ranking into /query"

Include:
- Exact file paths (from FaultLine-dev repo root)
- What changed (added, removed, modified, refactored)
- Before/after: line count, complexity, scope]

## Files to Modify

**List ALL files that will be touched. Be explicit.**

Example:
```
- openwebui/faultline_tool.py
  ├─ Remove: _TIER1_*, _TIER2_*, _TIER3_* constants
  ├─ Remove: _categorize_query() function
  ├─ Modify: _filter_relevant_facts() → simplify to confidence gating
  └─ Remove: entity_types parameter passing
- No changes to src/api/main.py (pre-prod only)
- No changes to migrations/ (not needed for this dprompt)
```

## Success Criteria

[Quantified, measurable, verifiable by running commands. Examples:]

✅ Syntax validation passes: `python -m py_compile openwebui/faultline_tool.py`  
✅ Test suite: 112+ tests pass, 0 new failures, X skipped  
✅ Code reduction: [X] lines → [Y] lines (confirm with `wc -l`)  
✅ Tier logic removed: 0 references to `_TIER` in file  
✅ entity_types removed: 0 function signatures with `entity_types` parameter  
✅ Sensitivity gating preserved: `born_on`, `lives_at` still in SENSITIVE_RELS  
✅ No UUID leaks in Filter output logic (manual code review)  

## Upon Completion

**⚠️ MANDATORY: Update scratch.md with this exact template (copy-paste, fill in values). This is NON-NEGOTIABLE.**

Your completion report in scratch.md IS your proof of work. No report = task incomplete. Every dprompt execution MUST update scratch.md before STOP.

**Update scratch.md with this exact template (copy-paste, fill in values):**

```markdown
## ✓ DONE: dprompt-[X]b ([Title]) — [DATE]

**Task:** [One-sentence goal from dprompt-[X]b.md]

**Changes ([FaultLine-dev](/home/chris/Documents/013-GIT/FaultLine-dev/)):**
- File: openwebui/faultline_tool.py
  - Removed: _TIER1_*, _TIER2_*, _TIER3_* constants
  - Removed: _categorize_query() function
  - Modified: _filter_relevant_facts() → simplified to confidence gating
  - Lines: [X] → [Y]

**Validation (Local):**
- Syntax: clean ✓
- Tests: [X] passed, [Y] skipped, 0 regressions ✓
- Code review: [specific findings, e.g., "Tier logic completely removed"] ✓

**Investigation findings (Pre-Prod):**
- [If any — what did you learn from pre-prod logs/state?]
- [Or: "No investigation needed for this dprompt"]

**Next steps:**
- User rebuilds pre-prod docker image
- User validates live: "tell me about our pets" → has_pet facts returned
- User validates live: "how are you" → identity facts only
- Report results in scratch

**AWAITING USER REBUILD AND VALIDATION.**
```

Then **STOP immediately** — do not proceed with live testing, do not attempt to deploy to pre-prod, do not start the next dprompt.

## Critical Rules (Non-Negotiable)

**Environment Isolation:**
1. **Investigation** = Pre-Prod Only (read-only: logs, DB queries)
2. **Code Development** = FaultLine-dev Only (all file modifications)
3. **Deployment** = User Triggered (you ask, user rebuilds, user validates)

**No Scope Creep:**
- If you notice a bug outside this dprompt's scope → report in scratch, don't fix
- If you see optimization opportunity → note it, stick to scope
- If database schema would help → propose in scratch, wait for direction

**Commit Discipline:**
- One commit per dprompt (logical, self-contained)
- Commit message: "dprompt-[X]: [descriptive title]"
- Only commit to master branch (FaultLine-dev)
- Never commit to faultline-prod

**Test-First Validation:**
- Always run local test suite before STOP
- Always report pass/fail/skip counts
- If tests fail: diagnose and fix, re-run, report again
- If tests pass but feel wrong: investigate before claiming success

**STOP Clause is Mandatory:**
- Every dprompt ends with STOP
- STOP means: wait for explicit user direction
- If user says "continue", then proceed
- If user says "revise", then revert and start sequence step 1

---

## Section-by-Section Guidance for Deepseek

### Writing the Task
✅ **Good:** "Simplify Filter to remove three-tier gating logic"  
❌ **Bad:** "Make the filter better"

### Writing the Context
✅ **Good:** "Tier 2 fires on empty Tier 1, blocking Tier 3. Category queries hit this path and lose facts."  
❌ **Bad:** "The filter has some issues with gating"

### Writing the Constraints
✅ **Good:** "MUST remove _TIER1_*, _TIER2_*, _TIER3_* constants; DO NOT modify src/api/main.py; Investigation pre-prod only"  
❌ **Bad:** "Be careful with changes"

### Writing the Sequence
✅ **Good:** Six numbered steps, each with sub-bullets, ending with STOP  
❌ **Bad:** "Do the work and let me know"

### Writing Success Criteria
✅ **Good:** "Tests: 112+ pass, 0 new failures; Code: [X] → [Y] lines; Tier logic: 0 _TIER references"  
❌ **Bad:** "Code should be cleaner"

### Writing Upon Completion
✅ **Good:** Exact template to copy-paste into scratch  
❌ **Bad:** "Just let me know when done"

---

**Template version:** 1.0  
**Last updated:** 2026-05-12  
**Purpose:** Enforce consistency, clarity, and discipline across all dprompts
