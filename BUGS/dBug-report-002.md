# dBug-report-002: Hierarchical Entity Relationships Missing

**Date:** 2026-05-12
**Severity:** P1 — data quality (hierarchy chains incomplete across all domains)
**Status:** Open — investigation complete, awaiting fix direction
**Hypothesis:** H1 (Extraction Gap) — Filter LLM prompt doesn't strongly request hierarchy rel_types

## Symptom

Users describe hierarchical relationships (breed→species, role→department, location nesting, component→whole) but the knowledge graph doesn't consistently capture them. Queries relying on hierarchy chains return incomplete results.

**Example:** User says "I have a dog named Fraggle, a morkie mix." Result: `fraggle instance_of dog` stored (Class A), but no `morkie subclass_of dog` or `morkie instance_of dog`. The morkie entity floats unattached to any classification chain.

## Investigation Findings

### Database State

**Cross-domain hierarchy facts (facts + staged_facts combined):**

| rel_type | facts | staged_facts | Total | Class |
|----------|-------|-------------|-------|-------|
| parent_of | 3 | 0 | 3 | A |
| child_of | 2 | 0 | 2 | A |
| instance_of | 1 | 0 | 1 | A |
| is_a | 0 | 1 | 1 | C |
| member_of | 0 | 1 | 1 | C |
| subclass_of | 0 | 0 | **0** | — |
| part_of | 0 | 0 | **0** | — |

**Pattern:** `parent_of`/`child_of` (explicit family) → 5 facts, all Class A. `instance_of`/`subclass_of`/`is_a`/`member_of`/`part_of` (classification hierarchies) → only 3 facts across ALL types, 2 of which are weak Class C.

### Specific Example: fraggle/morkie/dog

**Entities:**
| Entity | entity_type | Alias | UUID |
|--------|------------|-------|------|
| fraggle | Animal | fraggle | 7e4bff75... |
| morkie | **unknown** | morkie | e9b7f50c... |
| dog | **unknown** | dog | a77e330d... |

**Hierarchy facts found:**
- `fraggle instance_of dog` — facts table, Class A, confidence 1.0 ✓
- `fraggle is_a morkie` — staged_facts, Class C, confidence 0.4 ✗ (weak, may expire)
- `fraggle species single dog` — facts table (non-standard rel_type)

**Missing (expected but absent):**
- `morkie subclass_of dog` — chain gap
- `morkie instance_of dog` — alternate chain gap
- Entity types for morkie and dog are both `unknown`

### Extraction Trace (Filter Logs)

**What the Filter LLM DID extract:**
- `fraggle -instance_of-> dog` ✓
- `fraggle -species-> single dog` ✓
- `user -owns-> morkie` ✓

**What the Filter LLM did NOT extract:**
- `morkie -subclass_of-> dog` ✗
- `morkie -instance_of-> dog` ✗
- `is_a` never appears in LLM extraction logs (the one stored `is_a` fact likely came from GLiNER2 fallback, not LLM)

**WGM Gate:** No hierarchy facts rejected in backend logs. Gate is not the blocker.

### Filter Prompt Analysis

The `_TRIPLE_SYSTEM_PROMPT` in `openwebui/faultline_tool.py` mentions hierarchy types but weakly:

- **Primary extraction list (strongly prompted):** `spouse, parent_of, child_of, sibling_of, works_for, lives_at, likes, dislikes, owns, age, height, weight, born_on, anniversary_on, met_on`
- **Hierarchy types (mentioned as "other"):** `is_a: type or category`, `member_of: entity belongs to a taxonomy group`
- `instance_of` mentioned only in pronoun resolution logic, not in extraction instructions
- `subclass_of` not mentioned AT ALL in the prompt
- `part_of` not mentioned AT ALL in the prompt

**Result:** The LLM extracts hierarchy facts when the relationship is explicit in text (e.g., "Fraggle is a dog" → `instance_of`) but misses implicit chains (e.g., "a morkie mix" → no `subclass_of` extracted).

## Root Cause Hypothesis

**H1 — Extraction Gap (PRIMARY):** Filter LLM prompt doesn't explicitly and prominently instruct extraction of hierarchy rel_types (`instance_of`, `subclass_of`, `is_a`, `member_of`, `part_of`). These are mentioned as afterthoughts ("Other types allowed") rather than primary targets.

**Evidence:**
1. `parent_of`/`child_of` (in primary list) → 5 facts, all Class A
2. `instance_of`/`subclass_of`/`is_a` (not in primary list) → 3 facts total, 2 are Class C
3. `subclass_of` and `part_of` (not mentioned at all) → 0 facts
4. Filter logs show LLM extracted `instance_of` when obvious (direct "is a" pattern) but missed `subclass_of` (implicit "a morkie mix" pattern)
5. WGM gate doesn't reject — facts that ARE extracted make it through

**H2 — Type Confusion (SECONDARY):** Entity types for `morkie` and `dog` are `unknown` even though hierarchy context suggests `morkie=breed`, `dog=species/Animal`. Type metadata exists but isn't converted to `subclass_of` edges. This is downstream of H1 — if extraction were stronger, types would be inferred.

**H3–H5 eliminated:** WGM gate not rejecting (no rejection logs). Entity registry doesn't block. Facts that exist aren't expiring (Class A instance_of persists).

## Pattern Summary

**Works:** Explicitly named relationships (`parent_of`, `child_of`, `has_pet`) — these are in the prompt's primary extraction list.

**Fails:** Classification/type hierarchies (`instance_of`, `subclass_of`, `is_a`, `member_of`, `part_of`) — these are weakly prompted or missing from extraction instructions.

**This is systematic, not sporadic.** Every domain with hierarchy chains (breeds, org charts, location nesting, component trees) will have the same gap.

## Recommendation

**To fix (dprompt-56):**
1. Add hierarchy rel_types to the Filter prompt's primary extraction list with explicit examples:
   - `instance_of`: "Fraggle is a dog" → `(fraggle, instance_of, dog)`
   - `subclass_of`: "a morkie, which is a type of dog" → `(morkie, subclass_of, dog)`
   - `member_of`: "my pets are family" → `(pets, member_of, family)`
   - `part_of`: "the Engineering department of TechCorp" → `(engineering, part_of, techcorp)`
2. After extraction, infer hierarchy chains from type metadata: if `subject_type=X` and `object_type=Y` and `Y subclass_of X` exists, create the edge
3. Classify hierarchy facts as Class A or B (not C) — they're structural, not ephemeral

**To validate:** Test with "I have a cat named Goose, a Siamese mix" — should create `goose instance_of siamese` + `siamese subclass_of cat`.
