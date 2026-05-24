# FaultLine Workflow Enforcement Map

**Where the bug validation + deepseek execution flow is enforced, even in compact mode.**

---

## Flow Overview

```
[Bug Identified]
    ↓
[Create BUGS/dBug-NNN-<slug>.md] ← ENFORCED (I create this first, always)
    ↓
[Deepseek Review & Analysis] ← ENFORCED (deepseek-system-prompt.md)
    ↓
[Claude Validation & Decision] ← ENFORCED (compact-instruct.md Phase 3)
    ↓
[Write dprompt-Nb.md] ← ENFORCED (DEEPSEEK_INSTRUCTION_TEMPLATE format)
    ↓
[Deepseek Executes Fix] ← ENFORCED (deepseek-system-prompt.md role boundaries)
    ↓
[Code Review & Merge] ← ENFORCED (compact-instruct.md Phase 5)
```

---

## Enforcement Points

### 1. Bug Template Creation (Me — Always First)

**File:** `compact-instruct.md` → "Bug Validation & Execution Flow" → "Bug Template"

**Enforcement:**
- I create `BUGS/dBug-NNN-<slug>.md` immediately when any bug surfaces
- Template mandatory: Description, Reproduction, Impact, Root Cause, Proposed Fix, Validation Plan
- **Even in compact mode:** I do NOT skip this. Template creation is synchronous.

**Verification:**
- Check `BUGS/` directory for active `dBug-*.md` files
- Verify each has: reproduction steps, impact scope, root cause analysis

---

### 2. Deepseek Analysis Phase (Deepseek — Behavioral Boundaries)

**File:** `deepseek-system-prompt.md` → "How You Receive Direction" + "Investigation Boundaries"

**Enforcement:**
- Deepseek reads bug template fully
- Deepseek validates independently (pre-prod SSH investigation only, or accepts validation template if already complete)
- Deepseek reports findings with evidence (not narrative)
- Deepseek updates `scratch.md` under `#deepseek-analysis` section with template:
  ```
  #deepseek-analysis: dBug-NNN
  - Confirmed: [yes/no + evidence]
  - Affected code: [file paths + line ranges]
  - Other breakages: [related issues]
  - Root cause: [technical explanation]
  - Fix options: [1-3 options with trade-offs]
  - Recommended: [which option, why]
  ```

**Verification:**
- Check `scratch.md` for `#deepseek-analysis` sections
- Verify evidence is provided (line numbers, code references, test results)
- Deepseek does NOT write code without formal dprompt-Nb.md

---

### 3. Claude Decision & Prompt Writing (Me — Synchronous)

**File:** `compact-instruct.md` → "Bug Validation & Execution Flow" → "Phase 3"

**Enforcement:**
- I read deepseek's analysis
- I discuss with ${USER} (scope, approach, constraints)
- I write formal `dprompt-Nb.md` using DEEPSEEK_INSTRUCTION_TEMPLATE
- I commit both dBug-NNN and dprompt-Nb.md together
- I update `scratch.md` with direction pointer: "See dprompt-Nb.md for execution"

**Verification:**
- Check `scratch.md` for clear pointer to dprompt file
- Verify dprompt has all sections: Task/Context/Constraints/Sequence/Deliverable/Files/Success Criteria/Upon Completion

---

### 4. Deepseek Code Execution (Deepseek — Rigid Sequence)

**File:** `deepseek-system-prompt.md` → "How You Receive Direction" + "Investigation Boundaries"

**Enforcement:**
- Deepseek reads dprompt-Nb.md from top to bottom
- Deepseek follows Sequence section exactly (no skips, no reordering)
- Deepseek respects Constraints (MUST = non-negotiable, DO NOT = explicit prohibition)
- Deepseek tests locally first (`pytest tests/api/ --ignore=tests/evaluation`)
- Deepseek updates `scratch.md` with completion template (copy-paste from dprompt)

**Verification:**
- dprompt Sequence is ordered and complete
- Deepseek's code changes match Files list in dprompt
- Tests pass before reporting completion
- Scratch update uses template format

**In compact mode:**
- I still write the formal dprompt (even if using fast Opus or working terse)
- Deepseek still reads it in full
- Deepseek still reports findings with evidence
- No verbal instructions bypass the written prompt

---

### 5. Code Review & Merge (Me — Validation Gate)

**File:** `compact-instruct.md` → "Bug Validation & Execution Flow" → "Phase 5"

**Enforcement:**
- I review changes locally (`git diff`, test suite)
- I validate: no scope creep, no hardcoding, architecture-aligned
- I check: Class A/B/C classification correct, deduplication on UUIDs, validation metadata-driven
- I create single merge-ready commit with dBug-NNN reference
- I update `scratch.md`: "dBug-NNN RESOLVED"

**Verification:**
- Commit message references dBug number: "fix: [description] (fixes dBug-NNN)"
- Test suite passes without regressions
- Code aligns with CLAUDE.md principles

---

## Hard Rules (Non-Negotiable Even in Compact)

### Rule 1: No Code Without dprompt-Nb.md
- Deepseek NEVER codes based on verbal direction
- If asked "Can you fix X?" without a dprompt, deepseek responds: "Is there a dprompt-Nb.md for this?"
- **Compact mode does NOT bypass this.** I write the prompt even if concise.

### Rule 2: Bug Template First
- I ALWAYS create dBug-NNN before asking deepseek to investigate
- Template is mandatory, even if brief (can be 1-2 lines of reproduction)
- **Compact mode does NOT bypass this.** Template creation is my responsibility.

### Rule 3: Deepseek Analysis Before Fix Prompt
- Deepseek validates bug independently (or confirms validation template)
- I read his findings before writing the fix dprompt
- Fix dprompt is informed by his analysis, not by ad-hoc direction
- **Compact mode does NOT bypass this.** Analysis phase is separate from code phase.

### Rule 4: Investigation Limits
- Deepseek investigates pre-prod only (no container modifications)
- Deepseek changes code in FaultLine-dev only
- Deepseek does NOT deploy (waits for STOP clause, user rebuilds)
- **Compact mode respects these.** Boundaries are written in deepseek-system-prompt.md.

### Rule 5: Scratch Is Authoritative
- All direction to deepseek goes through formal dprompt-Nb.md files
- Scratch tracks state and completion status
- deepseek-system-prompt.md documents communication protocol (always #deepseek prefix)
- **Compact mode respects this.** No off-the-cuff instructions in chat.

---

## Compact Mode Specifics

**What compact mode does NOT change:**
- Bug template creation (I still make dBug-NNN)
- Deepseek analysis phase (still happens, still documented)
- Formal prompt writing (still dprompt-Nb.md, still DEEPSEEK_INSTRUCTION_TEMPLATE)
- Test validation (still local before reporting)
- Scratch updates (still copy-paste template format)

**What compact mode MAY optimize:**
- Terse scratch updates (bullets instead of prose)
- Concise dprompt reasoning (but structure unchanged)
- Parallel reading of multiple files (but order preserved)

**What compact mode NEVER changes:**
- Flow sequence (Bug → Analysis → Prompt → Execute → Review)
- Deepseek role boundaries (investigation limits, no direct code without prompt)
- Constraint enforcement (MUST/DO NOT are hard, not guidelines)

---

## Audit Trail

To verify this workflow is being followed:

1. **Check BUGS/ directory**
   - Every bug has a template
   - Each template has: reproduction, root cause, proposed fix
   - Order: template created, then analyzed, then fixed

2. **Check scratch.md**
   - Bugs referenced with `dBug-NNN` when mentioned
   - Deepseek analysis sections have `#deepseek-analysis: dBug-NNN` prefix
   - Deepseek completion sections use template format with evidence

3. **Check dprompt files**
   - Each dprompt follows DEEPSEEK_INSTRUCTION_TEMPLATE
   - All sections present (no missing Constraints or Success Criteria)
   - "Upon Completion" template is copy-paste ready

4. **Check git history**
   - Commits reference dBug numbers in message
   - Commits have clear scope (bug fix only, no refactoring)
   - Test suite passing at every commit

5. **Check codebase**
   - No hardcoded constants (all validation metadata-driven)
   - Deduplication uses UUIDs, not display names
   - No temporal coupling of changes

---

## Questions to Deepseek

**If deepseek gets unclear direction, he asks:**
1. "Is there a dprompt-Nb.md for this task?" (prompts without formal specification)
2. "Which investigation mode am I in?" (pre-prod vs. code change vs. test)
3. "Should I update scratch, or wait for Claude's review?" (when ambiguous)
4. "Which constraint takes precedence?" (if constraints conflict)

**Expected response format** (in scratch.md under #deepseek prefix):
```
#deepseek-question: [brief question]
[Context from prompt if applicable]
[What I'm blocked on]
```

---

## Final Enforcement: This Document

**This file (WORKFLOW_ENFORCEMENT.md) is the audit standard.**

- I reference it when deepseek deviates from flow
- ${USER} can verify the flow is being respected by checking against this map
- Compact mode explicitly commits to these hard rules
- Future deepseek sessions read this alongside deepseek-system-prompt.md

**TL;DR:** The flow is not optional. Even in compact mode, even under pressure, the sequence is: Bug Template → Analysis → Formal Prompt → Code → Review. No shortcuts.
