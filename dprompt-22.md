# dprompt-22 — Remove compound.py Bottleneck: LLM-First Extraction Pipeline

## Problem

Pet ingest is failing despite:
- ✅ Filter LLM successfully extracting pet facts ("Mars has a dog named Fraggle")
- ✅ Entity Taxonomies live and ready to classify `has_pet` relationships
- ❌ compound.py lacks pet patterns, so extracted facts don't reach DB
- ❌ System still treating compound.py as mandatory augment layer

Result: hardcoded patterns become the gatekeeper. Every new relationship type (pet, hobby, skill, device, location) requires manual pattern addition. This defeats the data-driven, self-building vision.

**User feedback:** "I thought we programmatically handled this to be able to adjust on the fly and grow as needed?"

## Root Cause

The extraction pipeline currently has three sources:
1. **Filter LLM** (primary) — extracts triples, types, confidence
2. **compound.py** (fallback/augment) — regex patterns for hardcoded rel_types
3. **GLiNER2** (final fallback) — fallback schema extraction

compound.py was designed to augment LLM extraction with high-confidence patterns. But it became a blocker:
- Missing patterns → facts extracted by LLM don't get augmented → WGM gate rejects as "low confidence" or "unknown type"
- Each missing pattern requires code change (dprompt-21 approach)
- System can't grow: new relationships can't flow through until patterns added

## Solution: LLM-First, WGM-Validated Pipeline

**New architecture:** Trust LLM extraction + WGM validation gate + taxonomies.

### Phase 1: Skip compound.py Augment for Novel Relationships

When Filter LLM extracts a relationship:
1. If rel_type is in the local ontology (facts' rel_types table):
   - Pass to WGM validation gate (confidence: LLM-provided, typically 0.7+)
   - WGM checks type constraints; if pass → ingest
   - If fail → log and skip (user can correct)

2. If rel_type is novel (not in ontology):
   - confidence is auto-marked as 0.4
   - WGM gates to `pending_types` (dprompt-17 already handles this)
   - No compound.py augment needed

**Code change:** `openwebui/faultline_tool.py`, around line 450:
```python
# BEFORE: Always call compound_extractor to augment
edges = llm_extracted_edges
if not edges:
    edges = compound_extractor(text)  # Fallback
if not edges:
    edges = gliner_fallback(text)     # Final fallback

# AFTER: Skip compound augment; let WGM validate
edges = llm_extracted_edges
if not edges:
    edges = gliner_fallback(text)     # Skip compound entirely
# LLM extraction is trusted; WGM gate decides validity
```

**Why this works:**
- Filter LLM has full context (conversation, entity types, prior facts)
- `has_pet(marla, fraggle)` is extracted correctly
- Edge has confidence=0.7, subject_type=Person, object_type=Animal
- WGM checks: has_pet expects (Person|Animal, Animal) → pass
- Fact ingests as Class A or B depending on classification
- Taxonomies handle transitive membership: has_pet → household category

### Phase 2: Deprecate compound.py

Once LLM-first pipeline is confirmed working:
1. Remove compound.py from ingest augment loop
2. Keep compound.py in repo as legacy (don't delete, mark deprecated)
3. Add to `CLAUDE.md`: "compound.py is legacy. Do not add new patterns. All extraction flows through Filter LLM + WGM gate."

### Phase 3: Monitor & Iterate

Track metrics in ingest logs:
- `ingest.llm_extracted_count` — edges from Filter LLM
- `ingest.gliner_fallback_count` — edges from GLiNER2 fallback
- `ingest.wgm_rejected_count` — edges rejected by WGM gate
- `ingest.novel_type_count` — edges with unknown rel_type (go to pending_types)

Example expected output after fix:
```
New chat: "I have wife Marla. Marla has a dog named Fraggle."
ingest: edges_received=2, llm_extracted=2, gliner_fallback=0, wgm_pass=2, class=A+B
facts: spouse(user, marla)=A, has_pet(marla, fraggle)=B
taxonomy: has_pet in household → transitive member flag set
query: "Tell me about my family" → returns marla + fraggle via _fetch_transitive_members(household)
```

## Implementation Checklist

- [ ] Remove compound_extractor call from faultline_tool.py ingest flow
- [ ] Verify Filter LLM extraction still returns typed edges (subject_type, object_type)
- [ ] Test: "I have wife Marla. Marla has a dog named Fraggle" → facts table has has_pet
- [ ] Test: "Tell me about my family" in new chat → returns marla + fraggle
- [ ] Test: Novel rel_type (e.g., "hobby", "skill") → goes to pending_types, no crash
- [ ] Add deprecation note to compound.py header
- [ ] Update CLAUDE.md Key Principles: "Extraction flows LLM → WGM gate → ingest. compound.py is legacy, not a required augment layer."

## Why This Fixes the Architectural Problem

1. **Data-driven**: New rel_types don't require code changes; LLM extracts, WGM evaluates, taxonomies classify
2. **Scalable**: System grows by user data + LLM feedback, not hardcoded patterns
3. **Robust**: WGM gate is the single point of validation (not scattered regex patterns)
4. **Self-building**: ontology_evaluations + taxonomies learn new relationships over time
5. **Aligns with vision**: "Memory machine that just works, and scales over time"

## Outcome

After this fix:
- Pet ingest works immediately (has_pet exists in ontology, WGM passes)
- Any new relationship type flows through without code change
- compound.py is legacy (documented, not blocking)
- System is truly self-building and data-driven

## Files to Change

- `openwebui/faultline_tool.py` — Remove compound_extractor call from ingest flow
- `CLAUDE.md` — Add "Extraction flows LLM → WGM gate → ingest" principle; mark compound.py as legacy
- `src/extraction/compound.py` — Add deprecation header comment

## Timeline

Implementation: 1-2 hours to remove compound.py call + test end-to-end.
Testing: "wife Marla with dog Fraggle" → family query should return both.
