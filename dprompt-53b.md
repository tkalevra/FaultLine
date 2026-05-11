# dprompt-53b: DEEPSEEK_INSTRUCTION_TEMPLATE — Filter Simplification

## Task

Simplify the OpenWebUI Filter to remove brittle three-tier gating logic. The Filter should trust backend `/query` ranking and inject facts without re-gating. This unblocks category queries ("our pets", "my family") and reduces code from 700+ to <500 lines.

## Context

**Current problem:** Filter implements Tier 1 (entity match) → Tier 2 (identity fallback) → Tier 3 (graph pass-through) with Concept entity filtering. When `entity_types` strips Concept/unknown tokens from Tier 1 matches, the empty result triggers Tier 2's identity fallback, which returns early before Tier 3 can run. This blocks facts that the backend correctly returned.

**Root cause:** Filter tries to be smart. It applies keyword lists, entity-type checks, and tier logic — all brittle.

**Correct architecture:** Backend extraction, ontology, and hierarchy are strong. Backend returns facts ranked by class (A > B > C) + confidence. Filter trusts that ranking and injects facts unchanged.

**Example:** Query "Where should my son and I go for dinner tomorrow?" should return facts about user (identity), son (hierarchy), locations (graph), and restaurants (contextual). Backend does this well. Filter should NOT filter it.

**Read first:** `docs/ARCHITECTURE_QUERY_DESIGN.md` — explains the principle with the dinner example. `dprompt-53.md` — specification of changes.

## Constraints

**MUST:**
- Remove all `_TIER1_*`, `_TIER2_*`, `_TIER3_*` constants and tier-based filtering logic
- Remove `entity_types` parameter from Filter function signatures
- Remove Concept/unknown entity filtering (the `Concept` type checks)
- Simplify `_filter_relevant_facts()` to: identity rels always pass, everything else passes if confidence ≥ 0.4
- **INVESTIGATION ONLY in pre-prod via SSH:** `ssh truenas -x "sudo docker logs open-webui --tail 50"` or `sudo docker exec open-webui python -c "import openwebui.faultline_tool; print('OK')"`
- **CODE MODIFICATIONS ONLY in FaultLine-dev:** All changes to `openwebui/faultline_tool.py` must be in the local dev repo (`/home/chris/Documents/013-GIT/FaultLine-dev/`)
- Maintain all existing test suite passes (112+ tests)
- Ensure sensitivity gating still works (birthday, address blocked unless explicit ask)

**DO NOT:**
- Touch backend `/query` endpoint (no `entity_types` removal — that's a separate follow-up)
- Modify ingestion pipeline
- Refactor Filter beyond the scope of this prompt
- Change database schema
- Add new parameters or configuration options
- **DO NOT DEPLOY** code changes to pre-prod yourself — wait for STOP clause and user rebuild

**MAY:**
- Reorder functions for clarity
- Consolidate helper functions if it improves readability
- Add comments explaining the simplified logic

## Sequence

1. **Read and understand** `docs/ARCHITECTURE_QUERY_DESIGN.md` (architectural principle)
2. **Read specification** `dprompt-53.md` (what/why/how)
3. **Investigate current Filter** (Pre-Prod Only):
   - SSH: `ssh truenas -x "sudo docker logs open-webui --tail 100 | grep -i tier"`
   - Check for active Tier logic in live logs
   - Identify Filter version deployed (git commit or date)
4. **Analyze current Filter code** `openwebui/faultline_tool.py` (FaultLine-dev local repo):
   - Identify all `_TIER1_*`, `_TIER2_*`, `_TIER3_*` definitions
   - Identify `_categorize_query()` function
   - Identify `_extract_query_entities()` calls
   - Identify `entity_types` parameter passing
   - Identify Concept/unknown type checks
5. **Implement simplification (FaultLine-dev only):**
   - Delete tier constants and `_categorize_query()` function
   - Delete `_extract_query_entities()` or repurpose for identity-fact-only matching
   - Rewrite `_filter_relevant_facts()` to:
     ```python
     def _filter_relevant_facts(facts, query, confidence_threshold=0.4):
         """Simplified: identity rels always pass; others pass if confidence >= threshold."""
         filtered = []
         for fact in facts:
             # Identity rels (also_known_as, pref_name, same_as, spouse, parent_of, child_of, sibling_of) always pass
             if fact['rel_type'] in IDENTITY_RELS:
                 filtered.append(fact)
             # Sensitive rels (born_on, lives_at, born_in, height, weight)
             elif fact['rel_type'] in SENSITIVE_RELS:
                 if "old" in query or "age" in query or "when" in query:
                     filtered.append(fact)  # Explicit ask
             # Everything else: pass if confidence >= threshold
             elif fact.get('confidence', 0.0) >= confidence_threshold:
                 filtered.append(fact)
         return filtered
     ```
   - Remove `entity_types` from all function signatures
   - Remove all `Concept`/`unknown` type checks
6. **Validate syntax (FaultLine-dev):** `python -m py_compile openwebui/faultline_tool.py`
7. **Run test suite (FaultLine-dev):** `pytest tests/api/ --ignore=tests/evaluation -v`
8. **Verify behavior:**
   - No regressions (all previously passing tests still pass)
   - Code is cleaner and shorter

**STOP POINT:** After code changes are complete and local tests pass, update scratch.md with results, then **STOP and wait for user to rebuild and re-deploy pre-prod** before any live testing.

## Deliverable

**Modified file:**
- `openwebui/faultline_tool.py` (in FaultLine-dev) — simplified Filter with tier logic removed, Concept filtering removed, entity_types parameter removed

**What changed:**
- Removed `_TIER1_ENTITY_MATCH_SIGNAL`, `_TIER1_EXCLUDE_PATTERNS`, `_TIER2_IDENTITY_RELS`, `_TIER3_THRESHOLD` constants
- Removed `_categorize_query()` function
- Deleted or repurposed `_extract_query_entities()` (no longer needed for Tier 1)
- Rewrote `_filter_relevant_facts()` to simple confidence + identity gating
- Removed `entity_types` parameter from `inlet()` and `_filter_relevant_facts()` signatures
- Removed all `entity_types` dict passing and filtering logic
- Removed Concept/unknown type checks throughout

**Code size:**
- Before: ~700 lines
- After: ~500 lines (report actual count)

## Files to Modify

- `openwebui/faultline_tool.py` (FaultLine-dev only)

## Success Criteria

✅ Python syntax check passes: `python -m py_compile openwebui/faultline_tool.py`  
✅ Test suite: 112+ tests pass, 0 new failures  
✅ No UUID leaks in Filter output logic  
✅ Sensitivity gating logic preserved: birthday/address in SENSITIVE_RELS  
✅ Filter code ~500 lines (down from 700+)  
✅ Tier logic completely removed (no references to `_TIER*` anywhere)  
✅ entity_types parameter removed from all signatures  

## Upon Completion

**Update `scratch.md` with this template:**

```markdown
## ✓ DONE: dprompt-53b (Filter Simplification — Remove Brittle Gating) — [DATE]

**Changes (FaultLine-dev):**
- Removed `_TIER1_*`, `_TIER2_*`, `_TIER3_*` constants from `openwebui/faultline_tool.py`
- Deleted `_categorize_query()` and `_extract_query_entities()` (no longer needed)
- Simplified `_filter_relevant_facts()` to: identity rels always pass, everything else if confidence ≥ 0.4
- Removed `entity_types` parameter passing throughout Filter
- Removed all Concept/unknown entity type checks
- Code size: [X] lines → ~500 lines (down from 700+)

**Local validation (FaultLine-dev):**
- Syntax: clean ✓
- Test suite: [X] passed, [Y] skipped, 0 regressions ✓

**Architecture:** Filter no longer applies brittle gating. Trusts backend `/query` ranking and injects facts in returned order.

**Pre-Prod Rebuild Required:**
- User must rebuild OpenWebUI docker image on truenas to deploy changes
- User must run live validation: "tell me about our pets" (should return has_pet facts)
- User must run: "how are you" (should return identity facts only)

**AWAITING USER REBUILD AND RE-VALIDATION.**
```

Then **STOP and wait for user to rebuild pre-prod** before attempting live testing.
