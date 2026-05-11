# dprompt-55b: DEEPSEEK_INSTRUCTION_TEMPLATE — Hierarchical Entity Extraction Investigation

## Task

Investigate where hierarchical entity relationships (instance_of, subclass_of, is_a, member_of, part_of) are being lost in the extraction → ingest → storage pipeline. Identify systematic patterns and root cause.

## Context

**Observation:** When users describe hierarchical relationships (e.g., "Fraggle is a morkie"; "I work in the Engineering department"; "I live in Toronto, Ontario, Canada"), the system doesn't consistently establish those relationships in the knowledge graph.

**Example (but not the problem):** User says "I have a dog named Fraggle, a morkie mix." Result: Three entities (fraggle, morkie, dog) exist as isolated nodes. Missing: fraggle `instance_of` morkie; morkie `subclass_of` dog.

**Question:** Is this systematic? Does the Filter extract hierarchy facts? Does ingest accept them? Does the database store them? Where does the chain break?

**Why it matters:** Without hierarchy chains, the system can't handle real-world hierarchies robustly. Every domain has nested types: org hierarchies (VP → Engineer), locations (city → state → country), roles, taxonomies, components. We need a general solution that works for ANY hierarchy the user describes.

**Read first:** `dprompt-55.md` (specification). `docs/ARCHITECTURE_QUERY_DESIGN.md` (entity hierarchy principles).

## Constraints

**MUST:**
- Investigate pre-prod database (read-only): query what's actually stored
- Inspect Filter logs: identify what was extracted from user input
- Check ingest pipeline: trace hierarchy facts from extraction → storage
- Document examples: not just dog/morkie, but across domains (people, orgs, locations)
- Identify root cause hypothesis: one or more of H1–H5 in dprompt-55.md
- Report findings: clear, factual, hypothesis-driven
- **Investigation only:** NO code changes, NO data modifications

**DO NOT:**
- Fix extraction code before understanding scope
- Assume this is only about "type inference" (broader pattern)
- Special-case dog breeds (need general solution)
- Skip checking other hierarchies (only dog example is not enough)
- Commit or modify pre-prod database

**MAY:**
- Query pre-prod via SSH (read-only)
- Check test fixtures for hierarchy test coverage
- Inspect Filter prompt for what it asks for

## Sequence

**CRITICAL: This is investigation. Follow the sequence exactly. Do NOT jump to fixing.**

### Phase 1: Establish Hierarchy Scope (Pre-Prod)

1. **Connect to pre-prod database**
   ```bash
   ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline \
     -c \"SELECT rel_type, label FROM rel_types \
          WHERE category IN ('hierarchy', 'taxonomy', 'composition') \
          OR rel_type IN ('instance_of', 'subclass_of', 'is_a', 'member_of', 'part_of');\" "
   ```
   
   Document: What hierarchy rel_types are defined? Are they in the ontology?

2. **Check: do any hierarchy facts exist in the database?**
   ```bash
   ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline \
     -c \"SELECT rel_type, COUNT(*) FROM facts \
          WHERE rel_type IN ('instance_of', 'subclass_of', 'is_a', 'member_of', 'part_of') \
          GROUP BY rel_type;\" "
   ```
   
   Document: How many hierarchy facts are stored? Which rel_types have facts?

3. **Check staged_facts too**
   ```bash
   ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline \
     -c \"SELECT rel_type, COUNT(*) FROM staged_facts \
          WHERE rel_type IN ('instance_of', 'subclass_of', 'is_a', 'member_of', 'part_of') \
          GROUP BY rel_type;\" "
   ```

### Phase 2: Specific Example (dog/morkie/fraggle)

4. **Check: what entities exist for fraggle, morkie, dog?**
   ```bash
   ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline \
     -c \"SELECT e.id, e.entity_type, a.alias, a.is_preferred \
          FROM entities e \
          LEFT JOIN entity_aliases a ON e.id = a.entity_id \
          WHERE a.alias IN ('fraggle', 'morkie', 'dog') \
          ORDER BY a.alias, a.is_preferred DESC;\" "
   ```
   
   Document: Entity IDs, types, aliases. What's the entity_type for each?

5. **Check: what facts connect them?**
   ```bash
   ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline \
     -c \"SELECT f.subject_id, f.rel_type, f.object_id, f.fact_class, f.confidence \
          FROM facts f \
          WHERE rel_type IN ('instance_of', 'subclass_of', 'is_a', 'member_of', 'has_pet') \
          AND (f.subject_id IN (SELECT id FROM entities WHERE entity_aliases.alias IN ('fraggle', 'morkie', 'dog')) \
               OR f.object_id IN (SELECT id FROM entities WHERE entity_aliases.alias IN ('fraggle', 'morkie', 'dog'))); \" "
   ```
   
   Document: What hierarchy facts exist? What's MISSING (expected but not found)?

### Phase 3: Extraction Trace (Pre-Prod Logs)

6. **Check: what did Filter LLM extract?**
   ```bash
   ssh truenas -x "sudo docker logs open-webui --tail 500 | grep -i 'fraggle\|morkie\|dog\|instance_of' | head -20"
   ```
   
   Document: What extraction patterns did the LLM follow? Were hierarchy edges included?

7. **Check: did WGM gate accept or reject hierarchy facts?**
   ```bash
   ssh truenas -x "sudo docker logs faultline --tail 500 | grep -i 'instance_of\|subclass_of\|hierarchy\|rejected'"
   ```
   
   Document: Were hierarchy facts validated? Were any rejected?

### Phase 4: Pattern Check (Other Hierarchies)

8. **Check: other hierarchy examples**
   ```bash
   ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline \
     -c \"SELECT rel_type, COUNT(*) FROM facts WHERE rel_type = 'parent_of' GROUP BY rel_type; \
         SELECT rel_type, COUNT(*) FROM facts WHERE rel_type = 'member_of' GROUP BY rel_type; \
         SELECT rel_type, COUNT(*) FROM facts WHERE rel_type = 'part_of' GROUP BY rel_type;\" "
   ```
   
   Document: Do similar hierarchies work in other domains? (parent_of chains in family, member_of in orgs)

9. **Pattern analysis: which rel_types have facts, which don't?**
   ```
   Comparison table:
   | rel_type      | Expected | Found | Gap? |
   |----------------|----------|-------|------|
   | instance_of    | 5+       | 0     | YES  |
   | subclass_of    | 3+       | 0     | YES  |
   | parent_of      | 10+      | 10    | NO   |
   | member_of      | 2+       | 0     | YES  |
   | ...            |          |       |      |
   ```

### Phase 5: Root Cause Analysis

10. **Hypothesis evaluation: which H# fits?**

    Based on findings:
    - **H1 (Extraction gap):** If Filter logs show names only (no instance_of edges), extraction didn't ask for hierarchies
    - **H2 (Type confusion):** If subject_type=Dog but no edge created, type metadata isn't converted to facts
    - **H3 (Ingest rejection):** If facts in logs but not in DB, WGM gate rejected them
    - **H4 (Registry limitation):** If entity aliases created but no hierarchy inferred, registry doesn't understand pattern
    - **H5 (Classification loss):** If facts in staged_facts but not facts table, they were classified as Class C and expired

    Document: Which H# is most likely based on evidence?

### Phase 6: Report Findings

11. **Write to BUGS/dBug-report-002.md**

    Structure:
    ```markdown
    # dBug-report-002: Hierarchical Entity Relationships Missing

    ## Symptom
    [Observation: user describes hierarchy, but relationships not stored]

    ## Investigation Findings

    ### Database State
    [Query results: what exists, what's missing]

    ### Extraction State
    [Filter logs: what was extracted]

    ### Pattern Analysis
    [Table: which rel_types work, which don't]

    ### Root Cause
    [Hypothesis H#: [reason]. Evidence: [specific log/query evidence].]

    ### Recommendation
    To validate: [specific test or code path to check]
    To fix: [potential approach based on H#]
    ```

### Phase 7: STOP & Report

12. **Update scratch.md** (using template below)
13. **STOP** — await direction before any fixes

## Deliverable

**Investigation report:** `BUGS/dBug-report-002.md`
- Symptom (example)
- Database findings (queries, results)
- Extraction findings (logs)
- Pattern analysis (which hierarchies work, which don't)
- Root cause hypothesis (H1–H5)
- Recommendations

**Not fixed:** Code unchanged. Data unchanged. Investigation only.

## Files to Modify

- `BUGS/dBug-report-002.md` — create or update with findings

## Success Criteria

✅ Pre-prod database queries executed and documented  
✅ Hierarchy rel_types confirmed: instance_of, subclass_of, is_a, member_of, part_of exist in ontology  
✅ Specific example traced: fraggle/morkie/dog facts checked (what exists, what's missing)  
✅ Filter logs inspected: what did LLM extract for hierarchical statements?  
✅ Pattern analysis done: at least 3 different hierarchy examples checked (not just dog)  
✅ Root cause hypothesis identified: evidence-based (H1, H2, H3, H4, or H5)  
✅ dBug-report-002.md written with findings, evidence, recommendations  
✅ No code changes made  
✅ No database modifications made  

## Upon Completion

**Update `scratch.md` with this template:**

```markdown
## ✓ DONE: dprompt-55b (Hierarchical Entity Extraction Investigation) — [DATE]

**Task:** Investigate where hierarchical entity relationships (instance_of, subclass_of, etc.) are lost in the pipeline.

**Database Findings:**
- Hierarchy rel_types defined: [list of rel_types found]
- Facts in storage: [count by rel_type, comparing instance_of vs parent_of vs member_of, etc.]
- Specific example (fraggle/morkie/dog): [X facts exist, Y expected, Z missing]

**Extraction Findings:**
- Filter LLM extraction: [does it extract hierarchy edges? evidence from logs]
- WGM gate: [accepts or rejects hierarchy facts?]
- Examples: [specific user input → what was extracted]

**Pattern Analysis:**
- Hierarchies that work: [rel_types with facts]
- Hierarchies that don't: [rel_types missing facts]
- Consistent across domains: [yes/no, evidence]

**Root Cause Hypothesis:**
- Most likely: H# [reason]
- Evidence: [specific log line, query result, or code reference]
- Secondary: H# [if multiple factors]

**Recommendation:**
To validate: [specific code path or test]
To fix: [potential approach]

**Status:** AWAITING USER DIRECTION (investigation complete, no code changes)

STOP — Do not proceed to fixing until user approves approach.
```

Then **STOP immediately**. Do not modify code, do not change data, do not proceed further. Wait for user direction.

## Critical Rules

**Investigation Only:**
- Read-only database queries only
- No schema changes
- No data modifications
- No code fixes (yet)

**Be Thorough:**
- Check multiple hierarchy types, not just one example
- Look at both facts and staged_facts tables
- Inspect logs (what was extracted?)
- Find systematic patterns (not random gaps)

**Be Precise:**
- Reference actual query results (counts, values)
- Quote log lines as evidence
- Identify specific code paths (Filter prompt, WGM gate, ingest logic)
- Back up every hypothesis with evidence
