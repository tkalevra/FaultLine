# dprompt-58: Fix Extraction Ambiguity — Hierarchy Constraint

**Date:** 2026-05-12  
**Author:** Christopher Thompson  
**Status:** Ready for implementation  
**Severity:** P2 (UX — prevents conflicting extractions)  
**Related:** dBug-report-003 (findings), dprompt-56b (hierarchy extraction baseline)

## Problem Statement

When users describe hierarchy/composition relationships, the LLM extracts conflicting interpretations:

**Example:** "I have a dog named Fraggle, a morkie mix"
- ✓ Extracts: `fraggle instance_of morkie` (correct — type classification)
- ✗ Also extracts: `user owns morkie` (wrong — morkie is a type, not a separate entity)

**Pattern across domains:**
- "Alice is an Engineer" → `alice instance_of engineer` ✓ BUT `user owns engineer` ✗
- "Server 192.168.1.1 is in subnet 192.168.1.0/24" → `ip part_of subnet` ✓ BUT `user owns subnet` ✗
- "Logger is in Monitoring component" → `logger part_of monitoring` ✓ BUT `system owns monitoring` ✗
- "I live in Toronto, Ontario" → `toronto part_of ontario` ✓ BUT two separate `lives_in` facts ✗

**Root cause:** The extraction prompt doesn't have a constraint preventing `instance_of`/`subclass_of`/`part_of`/`member_of` objects from also getting ownership/relationship facts. The LLM treats them as separate entities.

**The semantic principle:** When an entity appears as the OBJECT of a hierarchy/composition relationship, it's a type/classification/component—NOT a separate entity with independent relationships to the subject.

## Solution Overview

Add a **hierarchy constraint rule** to `_TRIPLE_SYSTEM_PROMPT` that explicitly forbids dual extraction:

```
HIERARCHY CONSTRAINT:
When you extract instance_of, subclass_of, member_of, part_of, or is_a for an entity,
do NOT also extract owns, has_pet, works_for, lives_in, lives_at, or similar 
ownership/relationship facts for that SAME entity. 

If something is a type, category, role, or component (appears as object of hierarchy rel),
it's not a separate entity with independent relationships to the user.

Examples:
- "Fraggle is a morkie" → extract: fraggle instance_of morkie [YES]
                        → extract: user owns morkie [NO] ← it's a breed type, not separate dog
- "Alice is an Engineer in Finance" → extract: alice instance_of engineer [YES]
                                    → extract: user owns engineer [NO] ← it's a role type
- "Server is in subnet 192.168.1.0/24" → extract: server part_of subnet [YES]
                                        → extract: user owns subnet [NO] ← it's a network component
```

## Scope Definition

### MUST Do (Fix Implementation)

1. **Enhance Filter Prompt (`_TRIPLE_SYSTEM_PROMPT`)**
   - Add HIERARCHY CONSTRAINT section (after PRIMARY extraction list)
   - Explicitly forbid: `owns`, `has_pet`, `works_for`, `lives_in`, `lives_at`, `spouse`, `friend_of` on hierarchy rel type objects
   - Include 4+ multi-domain examples showing the constraint in action
   - Clarify: "Types/classifications/components are not separate entities"

2. **Scope it broadly:**
   - Apply constraint to ALL hierarchy rel_types: `instance_of`, `subclass_of`, `member_of`, `part_of`, `is_a`
   - Apply constraint to ALL prohibited rel_types: `owns`, `has_pet`, `works_for`, `lives_in`, `lives_at`, `spouse`, `friend_of`, `created_by`
   - This prevents the same mistake across taxonomies, roles, locations, components, software architecture

3. **Validate locally:**
   - Syntax: `python -m py_compile openwebui/faultline_tool.py`
   - Tests: `pytest tests/filter/test_relevance.py -v` (no new failures)
   - Broader suite: `pytest tests/api/ --ignore=tests/evaluation -v` (112+ pass, 0 regressions)

### MUST NOT Do

- Modify `/ingest` or WGM gate (not needed — fix is extraction-only)
- Add new rel_types (use existing)
- Change database schema
- Manually clean pre-prod data (user correction flow should handle that)

### MAY Do

- Add defensive comments explaining the constraint rationale
- Consolidate examples if prompt gets long
- Add edge cases (e.g., "what if the type is also an entity in another context?")

## Design Details

### The Constraint Rule

**Core principle:** Hierarchy/composition relationships (`instance_of`, `subclass_of`, `part_of`, `member_of`, `is_a`) define what an entity IS or IS PART OF. The OBJECT of these relationships is a **category/type/component**, not a separate entity.

Therefore, when an entity E appears as the object of a hierarchy rel:
- ✓ Extract hierarchy facts (instance_of, subclass_of, part_of, is_a)
- ✗ Do NOT extract ownership facts (owns, has_pet, works_for, lives_in, etc.)

**Examples (all correct after fix):**

1. **Taxonomic:** "Fraggle is a morkie, a dog breed"
   - ✓ fraggle instance_of morkie
   - ✓ morkie subclass_of dog
   - ✗ user owns morkie ← NO, morkie is a type

2. **Organizational:** "Alice is an Engineer in the Finance department at TechCorp"
   - ✓ alice instance_of engineer
   - ✓ engineer member_of finance
   - ✓ finance part_of techcorp
   - ✗ user works_for engineer ← NO, engineer is a role type

3. **Infrastructure:** "Server 192.168.1.1 is in subnet 192.168.1.0/24, which is in network MainNet"
   - ✓ 192.168.1.1 part_of subnet_192.168.1.0_24
   - ✓ subnet part_of network_main
   - ✗ user owns subnet ← NO, subnet is a network component

4. **Geographic:** "I live in Toronto, Ontario, Canada"
   - ✓ toronto part_of ontario
   - ✓ ontario part_of canada
   - ✓ user lives_in toronto ← YES, toronto is where user lives (leaf entity)
   - ✗ user lives_in ontario ← NO, ontario is a container, user lives IN toronto

### Multi-Domain Test Scenarios (after fix)

1. "Tell me about my family. I have a dog named Fraggle, a morkie."
   - Expected extractions: fraggle instance_of morkie, user has_pet fraggle
   - NOT: user owns morkie ← the constraint prevents this

2. "I'm Alice, an Engineer in Finance at TechCorp. My manager is Bob."
   - Expected: alice instance_of engineer, engineer member_of finance, finance part_of techcorp, bob manager_of alice
   - NOT: user owns engineer ← the constraint prevents this

3. "Our network has three subnets: 192.168.1.0/24 (office), 192.168.2.0/24 (lab), 192.168.3.0/24 (storage)."
   - Expected: subnet part_of network, ip part_of subnet chains
   - NOT: user owns subnet ← the constraint prevents this

## Implementation Boundaries

### Fix Phase (This dprompt)
- Modify `_TRIPLE_SYSTEM_PROMPT` in `openwebui/faultline_tool.py`
- Add HIERARCHY CONSTRAINT section with multi-domain examples
- Validate locally (syntax, tests pass)
- Document in scratch.md before STOP

### Validation Phase (User responsibility)
- User rebuilds pre-prod docker image
- User tests the 3+ scenarios above
- Verify no new `owns/works_for` extractions for hierarchy type objects
- If pre-prod has stale bad facts, user correction flow should auto-supersede them (future: retraction enhancement)

## Success Criteria

✅ Syntax validation passes: `python -m py_compile openwebui/faultline_tool.py`  
✅ Test suite: 114+ tests pass, 0 new failures, X skipped  
✅ Hierarchy constraint added to `_TRIPLE_SYSTEM_PROMPT` (confirm via grep)  
✅ Multi-domain examples present (taxonomic, organizational, infrastructure, geographic)  
✅ Prohibited rel_types listed explicitly (owns, has_pet, works_for, lives_in, etc.)  
✅ No logic changes — prompt enhancement only  
✅ Prompt structure preserved (no accidental removals)  

## References

- dBug-report-003.md — root cause analysis (extraction ambiguity)
- dprompt-56.md — hierarchy extraction (baseline for this constraint)
- CLAUDE.md — semantic principles
- openwebui/faultline_tool.py — `_TRIPLE_SYSTEM_PROMPT` (to be enhanced)

## Notes for Deepseek

**Scope this broadly.** The issue isn't specific to dog breeds. It's about the fundamental semantic distinction between:
- **Leaf entities** (Fraggle, Alice, IP 192.168.1.1, Toronto) — real things with relationships
- **Type/category/component entities** (Morkie, Engineer, Subnet, Ontario) — abstract classifications, not separate entities

When the LLM extracts a hierarchy relationship (X instance_of Y, X part_of Y), it's saying "Y is a type/category/component, not a separate entity." So Y should NOT get ownership facts.

Add the constraint to prevent this confusion. Make it clear with examples across all domains. This fixes the extraction logic so it doesn't create conflicting data in the first place.

The user correction flow will eventually handle stale bad facts, but the real fix is preventing them during extraction.
