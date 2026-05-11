# scratch-archive-2026-05-12b — May 12 continuation: hierarchy extraction bug fix cycle

## Completed Work (2026-05-12 continued)

### dprompt-56b (Hierarchy Extraction Fix) — DEPLOYED
- Enhanced `_TRIPLE_SYSTEM_PROMPT`: moved instance_of/subclass_of/member_of/part_of to primary extraction list
- Added 6 multi-domain examples (taxonomic, organizational, infrastructure, hardware, geographical, software)
- Live testing: 3/6 scenarios confirmed working (taxonomic, org, geo)
- Local tests: 114 passed, 0 regressions
- Deployed to production as v1.0.2

### Data Quality Fix (Pre-Prod)
- Found 3 stale instance_of entries for Fraggle
- Superseded 2 bad entries (morkie mix, dog)
- Kept 1 correct entry (fraggle instance_of morkie, conf 1.0)

### Bug Investigation (dBug-report-003)
- Initial hypothesis: query/display logic leaked Morkie as separate entity
- Investigation revealed: ROOT CAUSE IS EXTRACTION AMBIGUITY, NOT QUERY LOGIC
- LLM extracted both `fraggle instance_of morkie` (✓) AND `user owns morkie` (✗)
- These conflict — Morkie is a breed type, not separate entity

### Root Cause Analysis
**Semantic principle:** Hierarchy relationships (instance_of, subclass_of, part_of, member_of) define WHAT something IS or IS PART OF. Objects of these relationships are TYPES/CATEGORIES/COMPONENTS, not separate entities.

**The issue:** Extraction prompt doesn't forbid extracting ownership facts (owns, has_pet, works_for, lives_in) for entities appearing as hierarchy rel objects.

**Broad scope:** Pattern repeats across domains:
- Taxonomic: "Fraggle is a morkie" → morkie is a breed type, not separate pet
- Organizational: "Alice is an engineer" → engineer is a role type, not separate person
- Infrastructure: "Server in subnet" → subnet is a component, not separate entity
- Geographic: "City in province" → province is a container, not where you live
- Software: "Module in component" → component is a structural type, not entity

### Data Cleanup (PINNED)
- Pre-prod has one bad fact: `user owns morkie` (Class B, conf 0.8)
- Decision: DON'T manually clean. Let programmatic correction flow handle it (future: retraction enhancement)
- Focus: Fix extraction to prevent NEW bad facts

---

## Next Task: dprompt-58

**Task:** Add HIERARCHY CONSTRAINT rule to `_TRIPLE_SYSTEM_PROMPT` forbidding extraction of ownership/relationship facts for entities appearing as hierarchy rel objects.

**Examples:**
- `fraggle instance_of morkie` → YES. `user owns morkie` → NO
- `alice instance_of engineer` → YES. `user owns engineer` → NO
- `ip part_of subnet` → YES. `user owns subnet` → NO

**Execution:** Ready for deepseek. Local validation (syntax + tests), STOP for user rebuild/validation.
