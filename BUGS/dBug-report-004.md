# dBug-report-004: Stale Data Cleanup — Hierarchy Extraction Ambiguity

**Date:** 2026-05-12
**Severity:** P3 (low priority — data correct going forward, cleanup only)
**Status:** Scoped — awaiting retraction flow enhancement
**Related:** dprompt-58 (extraction constraint fix), dBug-report-003 (root cause)

## Symptom

Pre-prod database contains one stale fact: `user owns morkie` (Class B, confidence 0.8), created before dprompt-58 fixed the extraction ambiguity.

## Root Cause (Fixed)

dprompt-56/57/58 cycle identified that the LLM extracted BOTH:
- `fraggle instance_of morkie` (correct type classification)
- `user owns morkie` (incorrect — morkie is a breed, not a separate pet)

dprompt-58 added a HIERARCHY CONSTRAINT to `_TRIPLE_SYSTEM_PROMPT` preventing this going forward. New extractions will not create `owns`/`has_pet`/`works_for`/`lives_in` facts for entities that are objects of `instance_of`/`subclass_of`/`member_of`/`part_of` relationships.

## Current State

- **Going forward:** Extraction correctly prevents bad facts ✓
- **Existing data:** One stale `user owns morkie` fact remains in pre-prod ✗
- **Impact:** Minimal — query responses may still mention Morkie as separate entity until cleaned

## Cleanup Options

**Option A — Manual supersede (quick fix):**
```sql
UPDATE staged_facts SET superseded_at = NOW(), qdrant_synced = FALSE
WHERE subject_id = (user) AND object_id = (morkie) AND rel_type = 'owns';
```
Fast but not programmatic — doesn't scale to future large datasets.

**Option B — Retraction flow enhancement (proper fix):**
Enhance the retraction/correction flow to detect and auto-supersede conflicting facts. When user provides correction context ("Fraggle is just one dog"), the system detects that `user owns morkie` conflicts with `fraggle instance_of morkie` and supersedes it.

**Option C — Post-extraction dedup (defense-in-depth):**
Add a post-extraction validation step that detects when an entity appears only as the object of a hierarchy relationship and has ownership facts, and auto-supersedes.

## Recommendation

**Option B** — Retraction flow enhancement. This is the right architectural approach:
1. User says "No, Fraggle is just one dog — a morkie"
2. LLM detects correction: `is_correction: true` on the hierarchy fact
3. Retraction flow identifies conflicting `owns` fact for morkie
4. Auto-supersedes: `user owns morkie` → superseded

This generalizes beyond this one case to any situation where extraction ambiguity creates conflicting facts.

## Scope

**P3 — Low priority.** Data is correct going forward (dprompt-58 prevents new bad facts). Only stale data in pre-prod needs eventual cleanup. Does not block production deployment.

**Future dprompt:** dprompt-retraction-enhancement for programmatic correction flow.

## References

- dBug-report-003: Root cause analysis (extraction ambiguity)
- dprompt-58: HIERARCHY CONSTRAINT fix (prevents new bad facts)
- dprompt-56b: Hierarchy extraction enhancement
