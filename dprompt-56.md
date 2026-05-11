# dprompt-56: Fix Hierarchical Entity Extraction — Multi-Domain Scope

**Date:** 2026-05-12  
**Author:** Christopher Thompson  
**Status:** Ready for implementation  
**Severity:** P1 (data quality — affects all hierarchical domains)  
**Related:** dprompt-55b (investigation), dBug-report-002.md (findings)

## Problem Statement

Filter's `_TRIPLE_SYSTEM_PROMPT` doesn't prominently ask for hierarchical relationship extraction. Result: `instance_of`, `subclass_of`, `member_of`, `part_of` facts are rarely extracted, while `parent_of`/`child_of` work well (they're in the primary list).

**This breaks hierarchies everywhere:**

| Domain | Example | Missing Chain | Impact |
|--------|---------|----------------|--------|
| **Taxonomic** | "Fraggle is a morkie" | fraggle → morkie → dog | Can't find all pets, retrieve wrong animal context |
| **Organizational** | "Sarah is a VP in Engineering at TechCorp" | sarah → vp → engineering → techcorp | Org chart broken, team queries fail |
| **Infrastructure** | "Server 192.168.1.1 is in subnet 192.168.1.0/24 which is in the Main network" | ip → subnet → network | Network queries incomplete, topology lost |
| **Hardware** | "CPU core 0 is in CPU 1 on motherboard A in server X" | core → cpu → motherboard → server | Hardware queries fail, resource mapping broken |
| **Geographical** | "I live in Toronto, Ontario, Canada" | toronto → ontario → canada | Location hierarchy lost, can't query "people in Ontario" |
| **Software** | "The Logger module is in the Monitoring component of the System" | logger → monitoring → system | Module dependency broken, architecture unclear |

## Solution Overview

Enhance Filter extraction to treat hierarchical relationships with the same prominence as family relationships. Instead of:
```
Extract: parent_of, spouse, sibling_of [PRIMARY]
Also extract if you see: instance_of, subclass_of, part_of, member_of [SECONDARY/WEAK]
```

Do:
```
Extract: parent_of, spouse, sibling_of, instance_of, subclass_of, part_of, member_of [ALL PRIMARY]
Include examples for EACH showing multi-domain hierarchies
```

## Scope Definition

### MUST Do (Fix Implementation)

1. **Enhance Filter Prompt (`_TRIPLE_SYSTEM_PROMPT`)**
   - Move `instance_of`, `subclass_of`, `member_of`, `part_of` to PRIMARY extraction list (same prominence as `parent_of`)
   - Add multi-domain examples (not just pet breeds):
     - **Taxonomic:** fraggle instance_of morkie, morkie subclass_of dog, dog subclass_of animal
     - **Organizational:** alice instance_of vp, vp member_of engineering, engineering part_of company
     - **Infrastructure:** 192.168.1.1 part_of subnet_192.168.1.0_24, subnet part_of network_main
     - **Hardware:** core_0 instance_of cpu_core, cpu_core part_of cpu_1, cpu_1 part_of motherboard_a
     - **Geographical:** toronto instance_of city, city part_of ontario, ontario part_of canada
     - **Software:** logger part_of monitoring, monitoring part_of system
   - Show that these hierarchies are as important as family relationships

2. **Strengthen Ingest Handling**
   - Ensure hierarchy facts are classified as Class A (identity-like importance, not ephemeral)
   - Infer hierarchy chains from type metadata (if entity_type=Dog, infer instance_of animal)
   - Validate hierarchy edges through WGM gate (ensure rel_types are in ontology)

3. **Verify Database Schema**
   - Confirm `instance_of`, `subclass_of`, `member_of`, `part_of` are in `rel_types` table
   - Confirm they're in `_REL_TYPE_HIERARCHY` frozenset (used by `_hierarchy_expand()`)
   - No schema changes needed (rel_types already defined)

### MUST NOT Do

- Special-case any domain (fix must work for all)
- Add new rel_types (use existing: instance_of, subclass_of, member_of, part_of, is_a)
- Change database schema (ontology already supports hierarchies)
- Limit hierarchy extraction to specific user contexts (should work everywhere)

### MAY Do

- Add more examples if clarity helps
- Optimize prompt length if needed (keep it clear)
- Add defensive comments to code

## Design Details

### Multi-Domain Test Scenarios

**After fix is deployed, validate with these real-world queries:**

1. **Taxonomic:** "Tell me about my family. I have a dog named Fraggle, a morkie."
   - Expected: fraggle → morkie → dog chain stored
   - Validate: Query "what animals do you have?" returns fraggle with proper context

2. **Organizational:** "I'm a VP in Engineering at TechCorp. My team includes Alice (Engineer), Bob (PM)."
   - Expected: user → vp, vp → engineering, engineering → techcorp; alice → engineer, bob → pm
   - Validate: Query "who do you work with?" returns team with roles

3. **Infrastructure:** "Our main network has three subnets: 192.168.1.0/24 (office), 192.168.2.0/24 (lab), 192.168.3.0/24 (storage)."
   - Expected: subnet chains to network, each subnet has multiple IPs
   - Validate: Query "what's in the main network?" returns all subnets and IPs

4. **Hardware:** "My desktop has 2 CPUs, each with 8 cores. CPU 1 has cores 0-7, CPU 2 has cores 8-15."
   - Expected: core → cpu → machine hierarchies
   - Validate: Query "what CPUs do you have?" returns hierarchy

5. **Geographical:** "I live in Toronto, which is in Ontario, Canada. The office is in Vancouver, BC."
   - Expected: toronto → ontario → canada; vancouver → bc → canada
   - Validate: Query "where are you?" includes full location chain

6. **Software:** "The system has 3 components: Monitoring (includes Logger, Alerter), Storage (includes DB, Cache), Compute (includes Scheduler)."
   - Expected: module → component → system chains
   - Validate: Query "what's in your system?" shows full architecture

## Implementation Boundaries

### Investigation Phase (DONE — dprompt-55b)
- Identified root cause: H1 (extraction gap)
- Confirmed secondary issue: H2 (type metadata not converted)

### Implementation Phase (dprompt-56b)
- Modify Filter prompt: add multi-domain examples, move hierarchy rel_types to primary list
- Strengthen ingest: classify hierarchy facts as Class A/B, infer from types
- Validate: syntax, no regressions
- Test: verify fix works for all 6 scenarios above

### Validation Phase (User responsibility)
- Rebuild pre-prod
- Test the 6 scenarios
- Confirm hierarchies work across all domains

## Success Criteria

✅ Filter prompt enhanced: `instance_of`, `subclass_of`, `member_of`, `part_of` in primary extraction list  
✅ Multi-domain examples added (at least taxonomic, organizational, infrastructure)  
✅ Hierarchy facts classified as Class A/B (not ephemeral)  
✅ Type metadata → hierarchy inference implemented (or verified working)  
✅ Test suite: no regressions (local tests pass)  
✅ Manual testing: all 6 scenarios show proper hierarchy chains (after user rebuild)  

## References

- dprompt-55b.md — investigation results (root cause H1 confirmed)
- BUGS/dBug-report-002.md — findings and analysis
- docs/ARCHITECTURE_QUERY_DESIGN.md — hierarchy principles
- src/api/main.py — `_REL_TYPE_HIERARCHY` frozenset
- openwebui/faultline_tool.py — `_TRIPLE_SYSTEM_PROMPT` (to be enhanced)

## Notes for Deepseek

**Scope:** This fix is NOT about pet breeds. It's about making hierarchy extraction work for **any domain** — orgs, infrastructure, software, geography, hardware, taxonomies. 

**Test broadly:** Don't just verify fraggle→morkie works. Verify subnet→network works. Verify core→cpu→motherboard works. Verify role→department→company works. That's how you know the fix is truly general.

**Example-driven design:** The Filter prompt gets better by showing it what hierarchies look like across domains. Fraggle example is relatable. Infrastructure example shows technical hierarchy. Org example is common. Together, they prime the LLM to extract hierarchies broadly.

**Key insight:** `parent_of` works because it's in the primary list with good examples. Everything else is weak because it's mentioned as "also allowed." Moving hierarchy rel_types to primary with good examples = generalized fix that scales.
