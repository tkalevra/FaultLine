# dBug-065: Extraction Layer Produces Noise Entities Instead of Semantic Entities

## Severity
**CRITICAL** — Core pipeline broken. All domains affected.

## Summary
The extraction layer (`/extract` endpoint) is generating entities from arbitrary text fragments (noise words) instead of identifying semantic entities and their relationships. This violates CLAUDE.md's core constraint: **"Extract is dumb, ingest must be smart"** — extract should produce structured semantic triples `(subject, rel_type, object)`, not bag-of-words noise.

## Scope
**DOMAIN-AGNOSTIC** — This affects extraction of ANY domain (family, medical, work, pets, etc.). Not a family-specific issue; the extraction framework itself is broken.

## Evidence

### Reproduction
1. Clear database (all tables empty)
2. Send natural language input via `/api/chat/completions`:
   ```
   My name is ${USER}, I prefer to be called ${USER}
   My spouses name is Marla, she prefers ${SPOUSE}
   We have 3 children, ${CHILD1}(M,12) who goes by ${CHILD1}, ${CHILD3}(F,10) prefers ${CHILD3}, and ${CHILD2} 19.
   ${SPOUSE} has a pet dog named Fraggle
   ```

### Observable Database State
**entity_aliases table (should be ~8 entities: ${USER}, Marla, ${CHILD1}, ${CHILD3}, ${CHILD2}, Fraggle + ${USER}'s user anchor):**

ACTUAL (corrupted):
```
children, ${CHILD3}(f,10, named, prefer, pet, she, has, spouses, my, called, goes, ${CHILD1}(m,12, we, dog
```

**facts table (should contain spouse, parent_of, child_of, has_pet relationships):**

ACTUAL: 0 rows (empty)

**entity_attributes table:**
```
✓ Correct: ${USER}, ${SPOUSE}, ${CHILD2}, ${CHILD3}, fraggle (pref_names)
✓ Correct: ages 12, 19, 10 (${CHILD1}, ${CHILD2}, ${CHILD3})
✗ Wrong: also_known_as stored as "${USER}" (not the original display name)
```

## Root Cause
The extraction endpoint (`/extract/rewrite` in `src/api/main.py`) is not producing semantically structured triples. Instead, it's:

1. **Treating input as a bag-of-words** — Every word/phrase becomes an entity
2. **Not inferring relationships** — No `(subject, rel_type, object)` triples are generated
3. **No relationship metadata** — Relationships (spouse, parent_of, child_of, has_pet) are missing entirely
4. **Violating the extraction contract** — LLM should return structured `EdgeInput` objects with `subject`, `rel_type`, `object`, not noise

## Impact

### What's Broken
- **Relationship extraction** — spouse, parent_of, child_of, has_pet, works_for, etc. not extracted
- **Semantic entity identification** — Proper entities buried in noise words
- **All domains** — Not just family; any domain using extraction fails the same way
- **Pipeline flow** — Facts never reach ingest because relationships don't exist
- **User experience** — LLM says it understands ("I've got the full picture locked in") but no data flows through

### What Still Works
- **Simple scalars** — Ages stored correctly (metadata-driven routing to entity_attributes)
- **Name aliases** — Some correct pref_names survive despite noise
- **Basic entity creation** — Entities are created (just with wrong aliases)

## CLAUDE.md Constraints Violated

### Constraint #1: Extract Produces Structured Triples
**VIOLATED** — Extraction should return `(subject, rel_type, object)` tuples. Currently returns noise.

### Constraint #4: Metadata-Driven Routing
**NOT VIOLATED** — Ingest correctly routes scalars to entity_attributes. Problem is upstream in extract.

### Constraint #10: Database as Source of Truth
**VIOLATED** — rel_types table should define what rel_type values are valid. Extraction ignores this and produces unstructured output.

## Investigation Questions

1. **LLM Extraction Prompt** — Does `/extract/rewrite` prompt ask the LLM to return structured `EdgeInput` objects?
   - Current prompt structure in `src/api/main.py` line ~??
   - Does it follow dprompt-127 pattern-based framework (Relationship, Scalar, Identity patterns)?
   
2. **EdgeInput Parsing** — Is the LLM output being parsed into `EdgeInput` objects correctly?
   - `subject`, `rel_type`, `object` fields populated?
   - JSON schema validation working?

3. **Relationship Classification** — Why are relationship rel_types missing?
   - spouse, parent_of, child_of, has_pet should be Class A facts
   - Are they being filtered somewhere in the pipeline?

4. **Entity Resolution** — Why are noise words becoming entities?
   - `registry.resolve()` being called for every word?
   - No validation that resolved IDs correspond to semantic entities?

## Test Plan

### Minimal Test Case
```bash
curl -X POST "https://${OPENWEBUI_DOMAIN}/api/chat/completions" \
  -H "Authorization: Bearer $BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "faultline-test",
    "messages": [{"role": "user", "content": "My wife is named Sarah"}]
  }'
```

**Expected:**
- facts table: 1 row `(user, sarah, spouse, 1.0, Class A)`
- entity_aliases: 2 rows (user, sarah)
- NO noise entities

**Actual:**
- facts table: 0 rows
- entity_aliases: ~5+ rows including "wife", "named", "sarah" (correct), etc.

### Domain-Agnostic Tests
1. **Family domain** — spouse, parent_of, child_of, has_pet
2. **Work domain** — works_for, employed_at, has_job
3. **Medical domain** — diagnosed_with, treated_at, takes_medication
4. **Pet domain** — species, breed, has_pet
5. **Location domain** — lives_in, born_in, located_at

Each should produce structured triples, not noise.

## Solution Requirements

Must be **metadata-driven** (per CLAUDE.md):

1. **Extraction Prompt** — Reference `rel_types` table for valid relationships
2. **Output Validation** — Validate returned triples against rel_types schema
3. **Noise Filtering** — Skip entities that don't match known patterns or head_types/tail_types
4. **Relationship Inference** — LLM should infer ALL relationships mentioned, not just keywords

No hardcoding of rel_type lists or entity patterns.

## Files to Review

- `src/api/main.py` — `/extract/rewrite` endpoint (line ~?)
- `src/api/models.py` — `EdgeInput` Pydantic model
- `openwebui/faultline_function.py` — If extraction happens in filter
- `src/wgm/gate.py` — Validation gate (may be filtering valid relationships)

## Related Issues
- dBug-062 — Extraction confusion (rel_type names as object values)
- dBug-127 — Prerequisite extraction (attempted fix, may not address core issue)
- dBug-126 — Directionality validation (doesn't help if relationships never extracted)

## Next Steps
1. **Inspect `/extract/rewrite` implementation** — What prompt is being sent to LLM?
2. **Test LLM output directly** — What is the raw response from LLM?
3. **Validate EdgeInput parsing** — Are triples being extracted from LLM response?
4. **Trace entity resolution** — Why are noise words becoming entities?
5. **Review rel_types filtering** — Is WGM gate filtering valid relationships?

---

**Status:** Blocking all end-to-end testing. **Priority:** Resolve before continuing.
