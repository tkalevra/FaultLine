# dprompt-55: Investigate Hierarchical Entity Extraction — General Pattern

**Date:** 2026-05-12  
**Author:** Christopher Thompson  
**Status:** Investigation phase  
**Severity:** P1 (data quality — hierarchical relationships often missing)  
**Related:** dBug-report-002 (hierarchical entity chains incomplete)

## Problem Statement

When users describe hierarchical relationships (type-of, instance-of, part-of, member-of), the extraction and ingestion logic doesn't consistently establish those relationships.

**Example (but not THE problem):**
- User: "Tell me about my family. I have a dog named Fraggle, a morkie mix."
- Extracted: Three entities (fraggle, morkie, dog) with no hierarchy chain
- Expected: fraggle `instance_of` morkie; morkie `subclass_of` dog (or equivalent)
- Result: Semantic relationships lost; queries like "what animals do you have?" might not find fraggle

**But this pattern repeats:** Wherever users describe nested types (breed→species, role→department, subcomponent→component, member→group), the hierarchy chain may be incomplete or missing.

**Question:** Is this systematic? Does extraction consistently miss type-hierarchy facts, or is it inconsistent? Where in the pipeline does the relationship get lost?

## Investigation Goals

Understand the **general pattern** of hierarchical entity extraction:
1. **Extraction scope:** What hierarchy patterns does Filter LLM extract? (instance_of, subclass_of, is_a, member_of, part_of, etc.)
2. **Ingest handling:** What happens to extracted hierarchy facts in the ingest pipeline? (accepted, rejected, rewritten?)
3. **Database state:** Are hierarchy relationships stored? If not, where are they lost?
4. **Query behavior:** Do hierarchy relationships affect retrieval? (graph expansion, taxonomy filtering)
5. **Systematic bias:** Is there a pattern? (e.g., all instance_of facts drop, but subclass_of preserved?)

**End goal:** Identify where the pipeline is fragile and propose robust fixes that work for *any* hierarchy scenario, not just dog breeds.

## Scope Definition

### MUST Do
- Audit ingest pipeline for hierarchy handling:
  - Filter extraction: what rel_types are extracted for hierarchical statements?
  - WGM gate: are hierarchy facts validated and accepted?
  - Entity registry: does it infer or establish hierarchy chains?
  - Database: are hierarchy facts committed to facts/staged_facts tables?
- Check multiple examples: not just dog/morkie/fraggle, but other hierarchies (people, organizations, locations, taxonomies)
- Document: where relationships are established, where they're lost, why
- Identify: is this a prompt issue (Filter doesn't ask for hierarchies), a logic issue (ingest discards them), or a data issue (they're not stored)?

### MUST NOT Do
- Fix the extraction code yet (investigate first)
- Modify Filter prompts (need to understand scope first)
- Change database schema (premature)
- Assume this is only a "type inference" problem (might be broader)

### MAY Do
- Query pre-prod database (read-only, via SSH)
- Trace Filter logs for specific user inputs
- Check test fixtures: do unit tests cover hierarchy extraction?
- Inspect ontology: what rel_types are defined as hierarchy types? (instance_of, subclass_of, part_of, member_of, is_a)

## Design Details

### Investigation Questions

**1. What hierarchy rel_types exist?**
```sql
-- Check rel_types table
SELECT rel_type, label, category FROM rel_types 
WHERE category IN ('hierarchy', 'taxonomy', 'composition')
OR rel_type IN ('instance_of', 'subclass_of', 'is_a', 'member_of', 'part_of');
```

**2. Are hierarchy facts being extracted?**
```sql
-- Check facts table for hierarchy types
SELECT rel_type, COUNT(*) FROM facts 
WHERE user_id = ? 
AND rel_type IN ('instance_of', 'subclass_of', 'is_a', 'member_of', 'part_of')
GROUP BY rel_type;

-- Check staged_facts too
SELECT rel_type, COUNT(*) FROM staged_facts 
WHERE user_id = ? 
AND rel_type IN ('instance_of', 'subclass_of', 'is_a', 'member_of', 'part_of')
GROUP BY rel_type;
```

**3. Are they being stored with proper confidence?**
```sql
-- Check: are hierarchy facts weak (low confidence) or missing?
SELECT subject_id, rel_type, object_id, fact_class, confidence FROM facts 
WHERE user_id = ? 
AND rel_type IN ('instance_of', 'subclass_of', 'is_a')
ORDER BY confidence;
```

**4. Are they being filtered out anywhere?**
- Filter prompt: does it explicitly ask for instance_of/subclass_of facts?
- WGM gate: does it reject novel hierarchy rel_types?
- Entity registry: does it infer hierarchies from text patterns?
- Ingest: does it classify hierarchy facts as Class A/B/C (or discard)?

**5. Do similar hierarchies work in other contexts?**
- Example: "I am the VP of Engineering" — does this extract instance_of or role relationship?
- Example: "I live in Canada, Ontario, Toronto" — does this extract nested locations?
- Example: "I work at TechCorp, Engineering Department" — does this extract org hierarchy?

### Root Cause Hypotheses

**H1 — Extraction Gap:** Filter LLM extracts entity names but not hierarchy relationships. Example: "a morkie" → pref_name=morkie, but NOT instance_of fact. **Fix:** Enhance Filter prompt to explicitly request hierarchy extraction.

**H2 — Type Confusion:** Filter extracts types (subject_type, object_type) but doesn't create instance_of/subclass_of facts. Example: subject_type=Dog, object_type=Pet, but no edge created. **Fix:** Add post-extraction rule that converts type metadata to facts.

**H3 — Ingest Rejection:** Hierarchy facts are extracted but rejected by WGM gate (unknown rel_type, type constraint violation). **Fix:** Ensure hierarchy rel_types are in ontology and constraints are loose enough.

**H4 — Registry Limitation:** Entity registry doesn't infer hierarchies from text. Example: "Fraggle is a morkie" parses as two entities but no type relationship inferred. **Fix:** Add hierarchical inference rules to entity resolution.

**H5 — Classification Loss:** Hierarchy facts are classified as Class C (ephemeral) and expire before confirmation threshold. **Fix:** Classify hierarchy facts as Class A or B (high confidence).

## Implementation Boundaries

### Investigation Phase (This dprompt)
- Database queries (read-only, pre-prod)
- Log inspection (what was extracted?)
- Gap analysis (what's missing, where?)
- Root cause hypothesis
- **NO code changes, NO data modifications**

### Potential Fix Phase (Future dprompt — dprompt-56?)
Depending on findings:
- **If H1:** Enhance Filter prompt (`_TRIPLE_SYSTEM_PROMPT`) with explicit hierarchy extraction rules
- **If H2:** Add post-extraction rule: type metadata → facts
- **If H3:** Verify ontology has hierarchy rel_types; loosen type constraints
- **If H4:** Add inference rules to entity_registry.py (entity name patterns → hierarchy)
- **If H5:** Reclassify hierarchy facts to Class A (identity-like importance)

## Success Criteria

✅ Hierarchy rel_types identified: confirmed instance_of, subclass_of, is_a, etc. exist in ontology  
✅ Multiple examples checked: not just dog/morkie, but across other domains  
✅ Extraction scope documented: what does Filter LLM actually extract for hierarchy statements?  
✅ Ingest scope documented: what happens to extracted facts? Accepted, rejected, lost?  
✅ Database state checked: are hierarchy facts stored? If not, at what stage are they lost?  
✅ Root cause hypothesis identified (one or more of H1–H5)  
✅ Findings written to BUGS/dBug-report-002.md with examples and recommendations  

## References

- PRODUCTION_DEPLOYMENT_GUIDE.md — SSH pre-prod queries
- dprompt-20.md — entity taxonomies (member_of, etc.)
- dprompt-28b.md — hierarchy expansion (_hierarchy_expand function)
- src/api/main.py — `_REL_TYPE_HIERARCHY` frozenset
- CLAUDE.md — Key Principles (entity ID normalization, etc.)

## Notes for Deepseek

**This is investigation, not fixing.** Get the data, find patterns, propose hypothesis.

**Key insight:** Don't special-case dog breeds. Find the GENERAL pattern. The system should handle ANY hierarchy the user throws at it: org hierarchies, location nesting, role trees, taxonomy chains, etc. Once you understand the general weakness, the fix will be general too.

**Report structure:**
1. **What we extract:** Specific examples with evidence (logs showing Filter output)
2. **What we store:** Database queries showing what made it to facts/staged_facts tables
3. **The gap:** What's missing, quantified (e.g., "instance_of facts: 0 stored, estimated 5+ should exist")
4. **Pattern:** Is the gap systematic (all hierarchy types missing) or sporadic (some work, some don't)?
5. **Hypothesis:** Which H# is most likely? Why? What specific code path suggests that?
6. **Recommendation:** "To validate H#, check [specific code] or test with [specific input]"

Be precise. Reference actual database values, log snippets, code locations.
