# dBug-018: /extract Endpoint Missing Context — GLiNER2 Operating in Isolation

**STATUS: ANALYSIS PHASE — FINDINGS & SOLUTIONS NEEDED**

## alicecription

The `/extract` endpoint (GLiNER2 entity extraction) accepts `user_id` and other context via `IngestRequest`, but **does not use it**. GLiNER2 operates on raw text alone, without:
- User's entity registry (known names, aliases)
- Ontology context (rel_types, medical taxonomy, body part mappings)
- Hierarchy information (instance_of chains, classification trees)
- Query results (existing user facts for pronoun/entity disambiguation)

This causes extraction to fail on implicit pronouns ("I" → user), context-dependent entities ("back" → body_part), and disambiguation scenarios.

**Example failure:**
```
Input: "what'd I do to my back?"
Expected: subject="user", object="back" (or body_part entity)
Actual: subject=null, object=null (GLiNER2 can't disambiguate without context)
```

## Evidence

**Code review (src/api/main.py, lines 1491-1534):**
- ✓ Endpoint accepts `IngestRequest` (has user_id, source, edges)
- ✓ Fetches GLiNER2 model
- ✗ Does NOT query entity_aliases for known entities
- ✗ Does NOT fetch ontology (rel_types table)
- ✗ Does NOT fetch user's existing facts from /query
- ✗ Does NOT build enriched schema with context
- ✗ Passes ONLY: raw text, constraint, bare schema

**Post-processing (lines 1516-1530):**
- Hardcoded null-subject resolution: null subject → "user"
- Hardcoded null-object flip for child_of: child_of with null object → parent_of
- Does NOT use ontology or entity registry
- Does NOT handle context-dependent entities like "back"

**Contrast with /query (lines ~1620+):**
- ✓ Uses entity registry for disambiguation
- ✓ Uses ontology for hierarchy expansion
- ✓ Uses taxonomy for filtering
- ✓ Uses graph traversal for context
- ✓ Returns enriched facts with metadata

## Root Cause

**aliceign mismatch:** The extraction pipeline assumes GLiNER2 can work standalone, but modern entity extraction requires:
1. Entity registry awareness (what entities already exist?)
2. Ontology awareness (what are valid entity types, relationships?)
3. User context awareness (what facts do we already know?)

Without this, GLiNER2 is forced to make guesses on ambiguous inputs.

## Impact

**Severity: HIGH**

- **Scope:** Medical context, implicit pronouns, context-dependent entities (all real-world queries)
- **User-facing:** Medical facts not extracted; personal context lost
- **Data loss:** Implicit relationships never persisted
- **Blocker:** Medical extraction fundamentally non-functional alicepite dBug-017 crash fix

**Examples that fail:**
- "what'd I do to my back?" → null entities
- "I have a rash on my shoulder" → shoulder not recognized as body_part
- "my knee pain is worse" → knee, pain ambiguous without ontology
- "charlie hurt himself" → "himself" unresolved (needs user context + family ontology)

## Architecture Problem

**Current flow:**
```
User message → Filter calls /extract → GLiNER2 (bare text) → null entities → LLM rewrite fails → no facts
```

**Should be:**
```
User message → Filter enriches with context → /extract receives (text + user_id + ontology + existing_facts) 
→ GLiNER2 (context-aware) → resolved entities → LLM rewrite succeeds → facts staged
```

## Investigation Scope for Deepseek

**You are NOT writing code. You are analyzing and recommending.**

### Phase 1: Context Requirements Analysis

**Questions to answer:**

1. **Entity Registry Context:**
   - What entities already exist for this user? (from entity_aliases)
   - How should they be passed to GLiNER2? (as entity list in schema? in system prompt?)
   - Should we prioritize preferred names or accept all aliases?

2. **Ontology Context:**
   - Which rel_types are relevant to this query? (all? or filtered by user activity?)
   - Should we pass rel_type constraints with metadata (inverse_rel_type, is_symmetric)?
   - How does ontology context help GLiNER2? (better entity type inference? relationship validation?)

3. **Body Part / Medical Taxonomy:**
   - Is body_part a taxonomy? (in entity_taxonomies table?)
   - How should GLiNER2 be told about body_parts? (list in schema? example triples?)
   - What about hierarchies? (back → spine → vertebrae)

4. **User Context / Pronoun Resolution:**
   - Should GLiNER2 receive a system message about user identity? ("This is Chris, age 45, systems_analyst")
   - Should we pass user's existing facts? (family, location, occupation, medical history)
   - What level of context is too much (token budget, relevance)?

5. **Query Integration:**
   - Should /extract call /query first to get user context? (performance impact?)
   - Or should Filter pass cached query results to /extract?
   - Should context be cached per-user-per-session?

### Phase 2: Schema Enrichment aliceign

**Analyze and recommend:**

1. **Enriched Schema Format:**
   - Current: bare text + rel_type constraint
   - Proposed: text + user context + entity list + ontology + example triples
   - How should this be structured for GLiNER2.extract_json()?

2. **Entity Name Injection:**
   - Pre-populate schema with known entity names?
   - Example: `"facts": [{"subject": "chris", "object": "back", "rel_type": "injury", ...}]`
   - Does this help or confuse GLiNER2?

3. **Ontology in Schema:**
   - Include rel_type metadata (inverse_rel_type, is_symmetric)?
   - Include entity type constraints (subject_type must be Person, object_type must be BodyPart)?
   - Include taxonomy membership (back is_member_of body_parts)?

4. **System Prompt for GLiNER2:**
   - Should /extract build a system prompt? (like /ingest does with _TRIPLE_SYSTEM_PROMPT)
   - Should it mention: user identity, ontology rules, medical context, body parts?
   - Trade-off: clarity vs token overhead

### Phase 3: Post-Processing Strategy

**Analyze and recommend:**

1. **Hardcoded vs Ontology-Driven:**
   - Current: hardcoded null-subject → "user", hardcoded child_of flip
   - Proposed: use rel_types metadata to guide resolution
   - Example: if rel_type.inverse_rel_type exists, should null subjects/objects swap roles?

2. **Entity Resolution:**
   - If object is "back" (not matched to entity), how should it be resolved?
   - Option A: Query body_part ontology, find "back" → spine_segment
   - Option B: Create new entity "back" with type=body_part
   - Option C: Leave as-is, let LLM rewrite handle it
   - Which is non-brittle?

3. **Confidence & Fallbacks:**
   - Should resolved entities be marked with confidence levels?
   - Example: "back" resolved to body_part with confidence=0.7
   - Should low-confidence resolutions be rejected?

### Phase 4: Robustness & Data Flow

**Critical questions:**

1. **Circular Dependencies:**
   - If /extract calls /query to get context, but /ingest calls /extract, is there a loop?
   - How do we avoid this? (cache? session state? separate context endpoint?)

2. **Token Budget:**
   - How much context can we safely pass to GLiNER2 without hitting token limits?
   - Should we truncate old facts? Filter by relevance?

3. **Consistency:**
   - If /extract now uses ontology, must all extraction go through /extract?
   - What about Filter's current LLM-first approach? Does it still work?
   - Must Filter and /extract use the same context?

4. **Scope Creep:**
   - We DON'T want /extract to be smarter than /query (but richer context)
   - We DON'T want /extract to duplicate /query logic
   - How do we keep concerns separated?

## Findings Needed (NOT Code)

Before any implementation, provide:

1. **Context Requirements Specification:**
   - What data should /extract receive?
   - In what format?
   - From where (entity_aliases, rel_types, staged_facts, query results)?

2. **Schema Enrichment aliceign:**
   - Proposed structure for enriched GLiNER2 schema
   - Examples of how current schema would change
   - Rationale for each change

3. **Post-Processing Strategy:**
   - How should null entities be resolved?
   - When should we use ontology vs hardcoded logic?
   - How do we handle entities not in registry?

4. **Data Flow Diagram:**
   - Show how context flows from /query → /extract
   - Identify any circular dependencies or bottlenecks
   - Show caching/optimization opportunities

5. **Risk Assessment:**
   - What could go wrong with enriched extraction?
   - How do we keep it non-brittle as ontology evolves?
   - What are token budget constraints?

6. **Recommended Approach:**
   - Among options analyzed, which is most robust?
   - What's the MVP (minimum viable context)?
   - What's the full solution?

## Success Criteria (Findings Phase)

- [ ] All four investigation questions answered
- [ ] Schema enrichment strategy documented (with examples)
- [ ] Post-processing logic clearly specified (not coded)
- [ ] Data flow diagram shows no circular dependencies
- [ ] Risk assessment identifies brittle patterns and mitigations
- [ ] Recommendation inclualice MVP and full scope
- [ ] Findings are actionable (deepseek knows exactly what to build)

---

## Upon Completion (Findings Phase)

Update `scratch.md` with:

```
## #deepseek: dBug-018 Analysis — Context Enrichment for /extract

**Status:** FINDINGS COMPLETE

**Key Findings:**
- /extract needs entity registry context (user_id → known entities)
- /extract needs ontology context (body_parts, medical rel_types)
- /extract needs user facts context (for pronoun/entity disambiguation)
- Post-processing should use ontology, not hardcoded logic
- [your key findings here]

**Recommended Approach:**
[MVP alicecription + full scope]

**Next Step:** Await specification. Then implement enriched /extract endpoint.

**See:** BUGS/dBug-018-extract-missing-context.md for full analysis.
```

Then commit findings (no code changes).
