# dBug-015: Medical Rel_Types Unknown to System (Novel Type Rejection)

## alicecription

Medical rel_types (has_medical_condition, has_symptom, has_injury, affected_body_part) are extracted by LLM but rejected by re_embedder as unknown/novel candidates. They never reach staged_facts or facts tables because they fail the novel rel_type approval gate (occurrence_count < 3, no strong semantic match to existing types).

**Root cause:** Medical rel_types are not pre-seeded in the rel_types table. System treats them as novel candidates → re_embedder evaluates asynchronously → rejects after first occurrence.

**Related to dBug-014 but different:** dBug-014 was about extraction being SKIPPED entirely (regex bug, now fixed by dprompt-75b). dBug-015 is about extraction succeeding but RESULTS being rejected as unknown rel_types.

## Reproduction

**Pre-prod test (2026-05-15):**
```
User input: "what did I do to my back?"
Filter: Semantic intent classification triggered (extraction NOT skipped) ✓
LLM: Extracted edges with rel_types: has_medical_condition, has_symptom, has_injury ✓
/ingest: Called with medical edges ✓
Re-embedder: Evaluated rel_types as novel candidates
Re-embedder: REJECTED all 3 medical rel_types (occ=1 < 3, similarity < threshold)
Result: Facts never staged; /query returns empty for medical context
```

## Evidence

**Database validation (2026-05-15):**

Medical facts in staged_facts:
```
user_id             | rel_type              | occurrence_count | status
--------------------|--------------------|----|--------
FaultLine WGM Test  | has_medical_condition | 1   | Class C (staged)
FaultLine WGM Test  | has_symptom           | 1   | Class C (staged)
test-health-fixed-001 | has_injury          | 1   | Class C (staged)
```

Ontology evaluations (re_embedder decisions):
```
candidate_rel_type    | occurrence_count | best_fit_rel_type | best_fit_score | decision
---------------------|---|---|----|---------
has_injury            | 1 | has_pet       | 0.776  | REJECTED (occ < 3, no match)
has_medical_condition | 1 | has_pet       | 0.767  | REJECTED (occ < 3, no match)
has_symptom           | 1 | has_os        | 0.772  | REJECTED (occ < 3, no match)
```

**Query test (real user account 3f8e6836-72e3-43d4-bbc5-71fc8668b070):**
- /query returns 40+ facts (identity, family, location, work, pets)
- ZERO medical facts returned
- No has_medical_condition, has_symptom, or has_injury in staged_facts or facts tables
- Medical context silently lost in conversation

## Impact

**Severity: MEDIUM (data loss, but workaround exists)**

- **Knowledge gap:** System can't persist medical context (medical conditions, symptoms, injuries)
- **User experience:** Health-related feedback ignored, no personalization for medical context
- **Fallback exists:** /store_context RAG fallback captures raw text to Qdrant, but structured facts lost
- **Not a regression:** Medical extraction works; issue is approval gate for unknown rel_types
- **Scope:** Any novel rel_type not pre-seeded faces same rejection (has_medication, affected_body_part, etc.)

## Root Cause Analysis

**System aliceign:** Novel rel_types (LLM-generated) require:
1. Frequency threshold: `occurrence_count >= 3`, OR
2. Strong semantic match: `cosine_similarity > 0.85` to existing rel_type

Medical rel_types extracted from conversation:
- `has_medical_condition` — similarity to has_pet: 0.767 (below threshold)
- `has_symptom` — similarity to has_os: 0.772 (below threshold)
- `has_injury` — similarity to has_pet: 0.776 (below threshold)

All fail both criteria:
- Low frequency (first occurrence)
- Low semantic similarity to existing rel_types (0.77 << 0.85 threshold)
- Re_embedder correctly rejects per aliceign

**Why it didn't fail for test accounts:** Test accounts happened to trigger extraction multiple times for same rel_types (by coincidence or repeated testing), reaching frequency threshold.

## Solution Options

### Option A (RECOMMENDED): Pre-seed medical rel_types in rel_types table

Add medical rel_types to migrations/024_routing_metadata.sql (or new migration 025):

```sql
INSERT INTO rel_types (rel_type, label, wikidata_pid, confidence, storage_target, fact_class)
VALUES
  ('has_medical_condition', 'Medical condition', 'P1050', 0.8, 'facts', 'B'),
  ('has_symptom', 'Symptom', 'P780', 0.8, 'facts', 'B'),
  ('has_injury', 'Injury', NULL, 0.8, 'facts', 'B'),
  ('affected_body_part', 'Affected body part', 'P927', 0.8, 'facts', 'B'),
  ('has_medication', 'Medication', 'P5002', 0.8, 'facts', 'B'),
  ('has_allergy', 'Allergy', 'P1050', 0.8, 'facts', 'B')
ON CONFLICT DO NOTHING;
```

**Trade-off:** Minimal — medical rel_types are universally useful, match WGM ontology semantics, belong in domain.

### Option B: Raise semantic similarity threshold for medical domain

Modify re_embedder logic to accept medical rel_types with lower similarity scores (0.75+).

**Trade-off:** Permissive, increases false positives (non-medical facts matched to medical rel_types).

### Option C: Accept as-is, document as limitation

Medical rel_types work after 3 confirmations (frequency gate). Current aliceign is correct; just need user awareness.

**Trade-off:** Data loss until threshold reached; poor UX for health feedback.

## Validation Plan

After fix (Option A recommended):

1. Pre-seed medical rel_types to rel_types table (migration)
2. Restart system (picks up new rel_types)
3. Re-test user input: "what did I do to my back?"
4. Verify:
   - /ingest accepts medical edges (no longer rejected as novel)
   - staged_facts populated with has_medical_condition, has_symptom, has_injury
   - /query returns medical facts for context injection
   - OpenWebUI conversation reflects medical context (not generic)
5. Non-regression: existing 47 rel_types unchanged, novel eval gate still active for truly unknown types

## Related Issues

- **dBug-014** — Extraction skip (fixed by dprompt-75b semantic intent classification)
- **dBug-015** — Medical rel_types unknown (THIS ISSUE)
- **dprompt-69** — Open-ended extraction (works correctly; medical extraction triggered)
- **dprompt-75b** — Semantic intent classification (works correctly; extraction not skipped)

---

## Investigation Scope for Deepseek

**Code review:**
- `src/re_embedder/embedder.py` — `evaluate_ontology_candidates()` approval threshold logic
- `migrations/024_routing_metadata.sql` — Existing rel_types seeding pattern
- `CLAUDE.md` WGM Ontology section — rel_types structure

**Database queries (pre-prod):**
- Confirm medical rel_types in ontology_evaluations with status=rejected
- Verify similarity scores for medical rel_types vs existing rel_types
- Check if pre-seeding other domains (health, nutrition, etc.) would help

**Recommended fix:**
Option A: Create migration 025 to pre-seed 6 medical rel_types with Wikidata PIDs, storage_target='facts', fact_class='B'. Seed before re-seeding medical extraction from users.

---

## Post-Fix Validation

After deepseek implements Option A:
1. User re-tests: "what did I do to my back?" in OpenWebUI
2. /query returns medical facts
3. LLM response inclualice medical context (not generic)
4. Facts table or staged_facts has has_medical_condition, has_symptom entries
