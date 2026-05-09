# scratch.md — FaultLine development dialogue

## INSTRUCTION FOR AGENTS

This file is for **questions and dialogue only**. Do NOT dump code, implementation
plans, or test cases here. Use it to:
- Ask design questions
- Request clarification on requirements
- Confirm decisions before coding

Code goes directly into source files. This file stays lean.

## Status: May 9, 2026 — Display Name Resolution Issue

**Fixed Issues:**
- ✓ pref_name facts now store correctly with string objects (mars, not UUID)
- ✓ Database ingest succeeds (200 OK)
- ✓ entity_aliases correctly register preferred names

**Current Issue:**
- pref_name facts stored in DB, but display names not showing in Filter memory injection
- /query returns facts, but Filter is not resolving UUID→preferred_name in the response
- Expected: "My wife is mars"
- Actual: UUID shows instead of "mars"

**Next Steps:**
1. Check Filter's `_resolve_display_names()` function — does it call registry.get_preferred_name()?
2. Verify Filter receives spouse fact with UUID object_id from /query
3. Verify Filter can access EntityRegistry and get preferred names
4. Test if pref_name facts appear correctly in memory block

---

## Questions for Chris

### 1. Rel-type hints: tiebreaker or removed entirely?

Should the classifier use rel_type conventions as a tiebreaker for ambiguous cases,
or should ambiguous cases just return "uncertain"?

**✓ DECISION**: KEEP HINTS. Database-driven from `rel_types.tail_types`, not hardcoded.

### 2. Self-learning: store classification outcomes?

Should the classifier persist its decisions for future reference, or stay stateless?

**✓ DECISION**: START STATELESS. Add persistence later if ambiguity rates spike.

### 3. What to do with "uncertain" results?

When classification is uncertain, which path should the edge take?

**✓ DECISION**: TREAT AS RELATIONSHIP. Let WGM gate validate downstream.

### 4. same_as: always relationship?

Should `same_as` have a hard override to always return "relationship"?

**✓ DECISION**: YES. `same_as` is owl:sameAs — entity identity, both sides UUIDs.

---

## 2026-05-09 — classify_fact_type COMPLETE ✓

Three edits applied to `src/api/main.py`:

| # | What | Line |
|---|------|------|
| 1 | `_SCALAR_REL_TYPES` set removed, replaced with comment | 49 |
| 2 | `classify_fact_type()` function inserted (8 layers, ~170 lines) | 663 |
| 3 | Integration: hardcoded check replaced with classifier call + confidence logging | 1135 |

**Implementation verified:**
- ✓ Function: All 7 layers implemented correctly
- ✓ L0: `same_as` → relationship (semantic constant, confidence 1.0)
- ✓ L1-L2: Numeric/date patterns → scalar
- ✓ L3: UUID → relationship
- ✓ L4: `entity_aliases` lookup → relationship
- ✓ L5: Email/URL/phone/descriptive text → scalar; capitalized word → relationship
- ✓ L6: `rel_types.tail_types` from DB → scalar or relationship (organic growth)
- ✓ L7: Fallback → uncertain (treated as relationship, WGM validates downstream)
- ✓ Confidence logging: Three-tier (≥0.80 silent, 0.60–0.79 info, <0.60 warning)
- ✓ Never rejects at classifier level; WGM gate enforces type constraints downstream

**Ready for testing and rebuild.**

—Claude
