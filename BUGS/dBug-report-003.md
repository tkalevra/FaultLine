# dBug-report-003: Hierarchy Chain Entities Leak Into Query Results

**Date:** 2026-05-12
**Severity:** P2 (UX issue — confuses user, but doesn't break data integrity)
**Status:** Investigation complete — root cause identified
**Related:** dprompt-56b (hierarchy extraction fix)

## Symptom

User says "I have a dog named Fraggle, a morkie mix." Query "tell me about my family" returns:
- "You also have two dogs you own, Morkie and Fraggle"

Expected: "You have one dog: Fraggle (a Morkie)"

## Investigation Findings

### /query response traces (2026-05-12)

**Facts returned involving morkie/fraggle:**
```
user -owns-> morkie          [Class B, conf 0.8]  ← THE LEAK
user -owns-> fraggle         [Class B, conf 0.8]
user -has_pet-> fraggle      [Class B, conf 0.8]
mars -has_pet-> fraggle      [Class B, conf 0.8]
fraggle -instance_of-> morkie [Class A, conf 1.0]  ← CORRECT
```

**preferred_names:** `morkie: morkie`, `fraggle: fraggle`
**entity_types:** `fraggle: Animal`, `morkie: unknown`

### Database state (verified)

All 9 facts involving fraggle/morkie:
```
fraggle -instance_of-> morkie      [Class A, 1.0]  ✓ correct
fraggle -instance_of-> morkie mix  [Class A, 0.0]    stale (superseded)
fraggle -instance_of-> dog         [Class A, 0.0]    stale (superseded)
fraggle -is_a-> morkie             [Class C, 0.4]    weak
user -owns-> morkie                [Class B, 0.8]  ✗ WRONG
user -owns-> fraggle               [Class B, 0.8]  ✓ correct
user -has_pet-> fraggle            [Class B, 0.8]  ✓ correct
mars -has_pet-> fraggle            [Class B, 0.8]  ✓ correct
person -has_pet-> fraggle          [Class B, 0.8]  ✓ correct
```

### Root Cause: Extraction Ambiguity (NOT query/display logic)

The LLM extracts BOTH:
1. `fraggle instance_of morkie` — correct type classification
2. `user owns morkie` — incorrect; morkie is a breed, not a separate pet

When a user says "I have a dog named Fraggle, a morkie mix," the LLM can interpret:
- **Interpretation A:** "I have a dog named Fraggle, which is a morkie" → one pet, breed classification
- **Interpretation B:** "I have a dog named Fraggle. I also have a morkie." → two pets

The LLM currently picks B for the `owns` extraction but A for the `instance_of` extraction. These conflict. The prompt should bias toward A when `instance_of`/`subclass_of` is also extracted — if an entity is the object of a type classification, it should NOT get ownership/has_pet relationships.

### Fix Direction

**Option 1 (Extraction prompt):** Add rule to `_TRIPLE_SYSTEM_PROMPT`: "When you extract instance_of or subclass_of for an entity, do NOT also extract owns/has_pet for the type-classification entity — it's a breed/type, not a separate pet/entity."

**Option 2 (Post-extraction dedup):** In `_filter_relevant_facts()` or downstream, detect when an entity appears only as the object of `instance_of`/`subclass_of` and has no other relationship to the user, and filter it from ownership results.

**Option 3 (Post-query dedup):** Filter in the query path: if entity is object of `instance_of` and has no `has_pet`/`spouse`/`parent_of` links to user, exclude from entity results.

**Recommended:** Option 1 (fix extraction prompt). It's the root cause and prevents the bad data from being stored in the first place. Options 2/3 are workarounds for already-stored bad data.

### Cleaning existing data

The `user owns morkie` fact in the pre-prod database should be manually superseded.

## Status

- Root cause: Extraction ambiguity — LLM treats type classification as separate entity
- Fix scope: `_TRIPLE_SYSTEM_PROMPT` enhancement (extraction rule)
- Data: One bad `owns` fact to clean in pre-prod
- Not a query/display bug — backend and filter are returning what was stored correctly
