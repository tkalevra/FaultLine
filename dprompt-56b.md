# dprompt-56b: DEEPSEEK_INSTRUCTION_TEMPLATE — Hierarchical Entity Extraction Fix

## Task

Enhance Filter's `_TRIPLE_SYSTEM_PROMPT` to prominently extract hierarchical relationships (`instance_of`, `subclass_of`, `member_of`, `part_of`) with multi-domain examples, ensuring extraction works across any hierarchy type the user describes.

## Context

Filter currently extracts `parent_of` and `child_of` well because they're in the primary extraction list. But hierarchical relationships like `instance_of` (taxonomies), `subclass_of` (class hierarchies), `member_of` (group membership), and `part_of` (composition) are weakly mentioned as "also extract if you see," resulting in missing chains across all domains.

**The problem:** When users say "fraggle is a morkie" (pet hierarchy), "Sarah is a VP in Engineering" (org hierarchy), "my server is in subnet 192.168.1.0/24 in the main network" (infrastructure hierarchy), or "the logger module is in monitoring which is in system" (software hierarchy), the system fails to extract the hierarchical edges. This breaks queries like "what animals do you have?" (can't find fraggle), "who do you work with?" (org chart breaks), "what's in the main network?" (topology lost), or "what's in your system?" (architecture unclear).

**Why it matters:** Real-world hierarchies exist everywhere—breed→species→animal, employee→role→department→company, IP→subnet→network, core→CPU→motherboard→server, city→province→country, module→component→system. The fix must work for ANY of these, not just pets. dprompt-55b investigation confirmed H1 (extraction gap): Filter doesn't ask for hierarchies prominently.

**What we learned:** Parent_of/child_of work because they're PRIMARY in the prompt with good examples. Hierarchy rel_types fail because they're SECONDARY ("also extract if you see"). The fix is straightforward: move instance_of/subclass_of/member_of/part_of to PRIMARY with multi-domain examples, showing the LLM that these are as important as family relationships.

**Read first:** `dprompt-56.md` (specification with 6 test scenarios). `BUGS/dBug-report-002.md` (investigation findings). `openwebui/faultline_tool.py` (Filter logic, around `_TRIPLE_SYSTEM_PROMPT`).

## Constraints

**MUST:**
- Modify `openwebui/faultline_tool.py` — enhance `_TRIPLE_SYSTEM_PROMPT` only, no logic changes
- Move `instance_of`, `subclass_of`, `member_of`, `part_of` from secondary to primary extraction list
- Add multi-domain examples: taxonomic (fraggle/morkie/dog), organizational (employee/role/department/company), infrastructure (IP/subnet/network), hardware (core/CPU/motherboard/server), geographical (city/province/country), software (module/component/system)
- Ensure examples show CHAINS not just individual facts (e.g., "fraggle instance_of morkie, morkie subclass_of dog, dog subclass_of animal")
- Maintain all existing tests (112+ pass, 0 new failures)
- No schema changes (rel_types already defined)
- No changes to `/ingest` logic (WGM gate already validates hierarchy rel_types)
- Investigation only (pre-prod) — traces only, no data modifications
- Code changes FaultLine-dev only
- Deployment user-triggered (wait for STOP)

**DO NOT:**
- Modify the logic of `_filter_relevant_facts()` or any Filter gating (dprompt-53b already fixed that)
- Add new rel_types (use existing: instance_of, subclass_of, member_of, part_of, is_a)
- Change `/ingest` or WGM gate behavior (ontology already supports hierarchies)
- Special-case any domain (fix must be general)
- Commit to faultline-prod (dev only)

**MAY:**
- Optimize prompt length if clarity improves
- Add defensive comments explaining hierarchy extraction intent
- Consolidate similar examples if space is tight
- Add notes about hierarchy chains being as important as family relationships

## Sequence

**CRITICAL: Follow this sequence exactly. Do not skip steps.**

### 1. Read & Understand (No coding yet)

- Read `dprompt-56.md` (specification) — understand all 6 test scenarios
- Read `BUGS/dBug-report-002.md` (investigation findings) — confirm H1 hypothesis
- Read `openwebui/faultline_tool.py` (current Filter code) — locate `_TRIPLE_SYSTEM_PROMPT`
- Read `CLAUDE.md` section on Filter architecture — understand _IDENTITY_RELS and why hierarchy extraction matters
- Ask clarifying questions in scratch.md if needed (prefixed `#deepseek`)

### 2. Investigate (Pre-Prod Only — Read-Only)

- SSH to pre-prod: `ssh truenas -x "sudo docker logs faultline --tail 200 | grep -i 'instance_of\|subclass_of\|member_of\|part_of'"` — verify hierarchy rel_types exist in live logging
- SSH to database: `ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline -c \"SELECT rel_type FROM rel_types WHERE rel_type IN ('instance_of', 'subclass_of', 'member_of', 'part_of', 'is_a');\""` — confirm all 5 rel_types are in ontology
- Document findings: do all hierarchy rel_types exist? Are they properly defined?
- If investigation reveals unexpected state, report in scratch.md before proceeding

### 3. Analyze Local Code (FaultLine-dev)

- Locate `_TRIPLE_SYSTEM_PROMPT` in `openwebui/faultline_tool.py` (search for the string directly)
- Read the entire prompt structure
- Identify:
  - Where is the primary extraction list? (e.g., "Extract: parent_of, spouse, sibling_of [PRIMARY]")
  - Where are hierarchy rel_types currently mentioned? (secondary/weak)
  - What examples are used? (likely family-focused: parent_of, spouse, sibling_of)
  - Where should multi-domain examples be inserted?
- Document your analysis: exact line numbers, current structure

### 4. Implement (FaultLine-dev Only)

**Step A: Restructure PRIMARY extraction list**
- Move `instance_of`, `subclass_of`, `member_of`, `part_of` from secondary to primary (same prominence as `parent_of`, `spouse`, `sibling_of`)
- Example target: `Extract: parent_of, spouse, sibling_of, instance_of, subclass_of, member_of, part_of [PRIMARY]`

**Step B: Add multi-domain examples** (insert after primary list, before secondary section)
- Taxonomic: "fraggle instance_of morkie, morkie subclass_of dog, dog subclass_of animal"
- Organizational: "alice instance_of engineer, engineer member_of engineering, engineering part_of company"
- Infrastructure: "192.168.1.1 part_of subnet_192.168.1.0_24, subnet_192.168.1.0_24 part_of network_main"
- Hardware: "core_0 instance_of cpu_core, cpu_core part_of cpu_1, cpu_1 part_of motherboard_a"
- Geographical: "toronto instance_of city, city part_of ontario, ontario part_of canada"
- Software: "logger part_of monitoring, monitoring part_of system"

**Step C: Clarify importance**
- Add note: "These hierarchies are as important as family relationships — they appear in every domain and enable queries like 'what animals do you have?', 'who do you work with?', 'what's in your system?'"

**Step D: Follow existing code style**
- Preserve indentation, line breaks, existing prompt formatting
- Commit as one logical commit: `git commit -m "dprompt-56: enhance Filter _TRIPLE_SYSTEM_PROMPT — move hierarchy rels to primary list with multi-domain examples"`

### 5. Validate Locally (FaultLine-dev)

- Syntax check: `python -m py_compile openwebui/faultline_tool.py` — must pass
- Run test suite: `pytest tests/filter/test_relevance.py -v` — no new failures
- Run broader test suite: `pytest tests/api/ --ignore=tests/evaluation -v` — confirm 112+ tests pass
- Document results: pass count, skipped count, any regressions
- If failures: diagnose and fix, re-run, report again before STOP

### 6. STOP & Report

- Update scratch.md with completion template (provided below)
- Include: files changed, prompt before/after (line count), test results, findings
- **Do NOT proceed to pre-prod testing** — wait for user rebuild/validation
- **STOP clause is mandatory** — await explicit direction before next steps

## Deliverable

**Modified file:** `openwebui/faultline_tool.py`
- Target: `_TRIPLE_SYSTEM_PROMPT` constant
- Changes:
  - Move `instance_of`, `subclass_of`, `member_of`, `part_of` to PRIMARY extraction list (same prominence as `parent_of`, `spouse`, `sibling_of`)
  - Add 6 multi-domain examples (taxonomic, organizational, infrastructure, hardware, geographical, software)
  - Add clarification note on hierarchy importance
- Before/after: approximate line count change

**Validation:** Local tests pass (112+ tests, 0 regressions)

**Not changed:** /ingest logic, WGM gate, Filter gating architecture (dprompt-53b already fixed that)

## Files to Modify

```
- openwebui/faultline_tool.py
  ├─ Modify: _TRIPLE_SYSTEM_PROMPT constant
  │   ├─ Move instance_of, subclass_of, member_of, part_of to PRIMARY list
  │   ├─ Add 6 multi-domain examples (taxonomic, org, infrastructure, hardware, geo, software)
  │   ├─ Add note on hierarchy importance across domains
  │   └─ Keep all existing family relationship examples and logic
  └─ No changes to: _extract_query_entities(), _filter_relevant_facts(), or any Filter gating
- No changes to: src/api/main.py, migrations/, database schema
```

## Success Criteria

✅ Syntax validation passes: `python -m py_compile openwebui/faultline_tool.py`  
✅ Test suite: 112+ tests pass, 0 new failures, X skipped  
✅ Hierarchy rel_types moved: `instance_of`, `subclass_of`, `member_of`, `part_of` in PRIMARY list (confirm via grep: `grep -n "instance_of" openwebui/faultline_tool.py | head -5`)  
✅ Multi-domain examples present: all 6 domains covered (taxonomic, organizational, infrastructure, hardware, geographical, software)  
✅ Prompt structure preserved: no logic changes, only prompt text enhancement  
✅ Family relationships preserved: `parent_of`, `spouse`, `sibling_of` still in PRIMARY list with existing examples  
✅ No regressions: Filter tests pass locally

## Upon Completion

**⚠️ MANDATORY: Update scratch.md with this exact template (copy-paste, fill in values). This is NON-NEGOTIABLE.**

```markdown
## ✓ DONE: dprompt-56b (Hierarchical Entity Extraction Fix) — [DATE]

**Task:** Enhance Filter's _TRIPLE_SYSTEM_PROMPT to move hierarchical relationships (instance_of, subclass_of, member_of, part_of) to primary extraction list with multi-domain examples.

**Changes ([FaultLine-dev](/home/chris/Documents/013-GIT/FaultLine-dev/)):**
- File: openwebui/faultline_tool.py
  - Modified: _TRIPLE_SYSTEM_PROMPT constant
  - Moved: instance_of, subclass_of, member_of, part_of to PRIMARY extraction list
  - Added: 6 multi-domain examples (taxonomic, organizational, infrastructure, hardware, geographical, software)
  - Added: Clarification note on hierarchy importance
  - Lines: [X] → [Y] (before/after count)

**Validation (Local):**
- Syntax: clean ✓
- Tests: [X] passed, [Y] skipped, 0 regressions ✓
- Prompt structure: preserved, no logic changes ✓
- Hierarchy rel_types: confirmed in primary list ✓

**Investigation findings (Pre-Prod):**
- Hierarchy rel_types exist in ontology: [list of rel_types found]
- All 5 hierarchy rel_types (instance_of, subclass_of, member_of, part_of, is_a) confirmed in database

**Next steps:**
- User rebuilds pre-prod docker image
- User validates live across all 6 scenarios:
  1. Taxonomic: "Tell me about my family. I have a dog named Fraggle, a morkie." → fraggle→morkie→dog chain
  2. Organizational: "I'm a VP in Engineering at TechCorp..." → user→vp→engineering→techcorp chain
  3. Infrastructure: "Our main network has three subnets..." → subnet→network chain
  4. Hardware: "My desktop has 2 CPUs, each with 8 cores..." → core→cpu→motherboard chain
  5. Geographical: "I live in Toronto, which is in Ontario, Canada..." → toronto→ontario→canada chain
  6. Software: "The system has 3 components: Monitoring (Logger, Alerter)..." → module→component→system chain
- User reports: all 6 scenarios show proper hierarchy chains (or issues found)

**AWAITING USER REBUILD AND VALIDATION.**
```

Then **STOP immediately** — do not proceed with live testing, do not attempt to deploy, do not start the next dprompt.

## Critical Rules (Non-Negotiable)

**Environment Isolation:**
1. **Investigation** = Pre-Prod Only (read-only: logs, DB queries)
2. **Code Development** = FaultLine-dev Only (all file modifications)
3. **Deployment** = User Triggered (you ask, user rebuilds, user validates)

**Scope & Discipline:**
- If you notice Filter gating issues → don't fix (dprompt-53b already addressed)
- If you see /ingest changes needed → propose in scratch, don't implement (out of scope for this dprompt)
- If database schema would help → note it, stick to prompt enhancement

**Commit Discipline:**
- One commit per dprompt (logical, self-contained)
- Commit message: `dprompt-56: enhance Filter _TRIPLE_SYSTEM_PROMPT — move hierarchy rels to primary list with multi-domain examples`
- Only commit to master branch (FaultLine-dev)
- Never commit to faultline-prod

**Test-First Validation:**
- Always run local test suite before STOP
- Always report pass/fail/skip counts
- If tests fail: diagnose and fix, re-run, report again
- If tests pass but prompt feels weak: iterate on examples before claiming success

**STOP Clause is Mandatory:**
- Every dprompt ends with STOP
- STOP means: wait for explicit user direction
- If user says "continue", then proceed
- If user says "revise", then revert and start sequence step 1

---

**Template version:** 1.0  
**Date created:** 2026-05-12  
**Status:** Ready for execution by deepseek
