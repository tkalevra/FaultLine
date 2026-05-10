# dprompt-38b — System Metadata & Transitive Extraction: Investigation & Fixes [PROMPT]

## #deepseek NEXT: dprompt-38b — Code Investigation: System Metadata & Transitive Relationships — 2026-05-10

### Task:

Investigate why Filter LLM understands system metadata and transitive relationships but doesn't extract them as structured edges. The model responds conversationally ("I understand your laptop...") but facts don't persist. Audit Filter prompt, ontology, WGM gate, and query logic. Identify root cause(s) and implement fixes. Re-test scenarios 3 & 5.

### Context:

Pre-prod validation (dprompt-37b) found:
- **Scenario 3:** System metadata acknowledged by LLM but not stored/retrieved
- **Scenario 5:** Transitive relationship acknowledged but not extracted

**Key insight:** LLM's conversational understanding proves it processed the data. The extraction gap is likely code, not model. Either:
1. Filter prompt doesn't instruct LLM to extract these rel_types
2. Ontology lacks system property rel_types (has_ip, has_os)
3. WGM gate rejects them
4. /query doesn't surface them

This is worth fixing because system metadata and transitive relationships are valuable.

### Constraints (CRITICAL):

- **Wrong: Assume model can't extract these. It's demonstrably understanding them.**
- **Right: Audit code to find where extraction is gated/blocked**
- **MUST: Investigate BEFORE fixing. Document findings for each of 5 areas (Filter prompt, ontology, WGM gate, EdgeInput, /query)**
- **MUST: If Filter prompt is the gap, modify it to explicitly ask for system metadata + transitive relationship extraction**
- **MUST: If ontology is missing rel_types, add them (has_ip, has_os, has_hostname)**
- **MUST: After fixes, re-test scenarios 3 + 5 against pre-prod. Both must now return expected results.**
- **MUST: Zero code regressions. Run full test suite after changes.**

### Sequence (DO NOT skip or reorder):

1. Read dprompt-38.md spec (all investigation areas)

2. **Investigate Filter Prompt (openwebui/faultline_tool.py):**
   - Find the LLM extraction prompt (search for "Extract" or "relationships")
   - Read the full prompt text
   - Does it mention system metadata? (IP, hostname, OS, properties)
   - Does it mention transitive relationships? (friend_of, knows, met)
   - Document: What rel_types does the current prompt ask for? What's missing?
   - Hypothesis: Prompt likely focuses on family rel_types, not system properties

3. **Check Ontology (rel_types table):**
   - SSH query: `ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c 'SELECT rel_type, head_types, tail_types FROM rel_types WHERE rel_type IN (\"has_ip\", \"has_os\", \"has_hostname\", \"knows\", \"friend_of\", \"met\") ORDER BY rel_type;'"`
   - Document: Which rel_types exist? Which are missing?
   - For `knows`: should be standard. If missing, that's a gap.
   - For `has_ip`, `has_os`, `has_hostname`: likely missing (these are system properties)
   - Hypothesis: System property rel_types don't exist; `knows` might exist but not instructed in prompt

4. **Investigate WGM Gate (src/wgm/gate.py):**
   - Find WGM validation logic
   - How does it handle unknown rel_types?
   - Does it reject edges before storing?
   - Query: What's the default behavior for novel rel_types?
   - Document: Would system metadata edges pass the gate?

5. **Check EdgeInput & /ingest (src/api/models.py, src/api/main.py):**
   - Does EdgeInput schema allow arbitrary rel_types?
   - Does /ingest accept non-family rel_types?
   - Document: Path from Filter → EdgeInput → /ingest for system metadata

6. **Audit /query Retrieval (src/api/main.py):**
   - Find `/query` fact fetching logic
   - Does `_fetch_user_facts()` filter by rel_type?
   - Are system property facts included in response?
   - Document: Would system metadata facts be returned if stored?

7. **Synthesize Findings:**
   - Root cause: Which of the 5 areas is blocking extraction/retrieval?
   - Most likely: Filter prompt + missing ontology rel_types

8. **Implement Fixes:**

   **Fix 1 (Filter Prompt):** If missing, add explicit extraction instructions
   ```
   Extract system metadata facts:
   - "IP is X" → subject → has_ip → X
   - "OS is Y" → subject → has_os → Y
   - "hostname is Z" → subject → has_hostname → Z
   
   Extract transitive relationships:
   - "A knows B" → A → knows → B
   - "A is friend of B" → A → friend_of → B
   ```

   **Fix 2 (Ontology):** If missing rel_types, add them
   ```sql
   INSERT INTO rel_types (rel_type, label, head_types, tail_types, wikidata_pid, confidence)
   VALUES 
     ('has_ip', 'Has IP Address', ARRAY['ANY'], ARRAY['SCALAR'], NULL, 0.8),
     ('has_os', 'Has Operating System', ARRAY['ANY'], ARRAY['SCALAR'], NULL, 0.8),
     ('has_hostname', 'Has Hostname', ARRAY['ANY'], ARRAY['SCALAR'], NULL, 0.8);
   ```

   **Fix 3 (WGM Gate):** Ensure it doesn't reject these rel_types

   **Fix 4 (/query):** Ensure system metadata facts are returned

9. **Re-test:**
   - Scenario 3: "My laptop is named Workstation-X, IP 192.168.1.100, OS Ubuntu 22.04"
   - Query: "What are my computers' properties" (or natural variant)
   - Expected: IP, OS, hostname returned
   - Scenario 5: "My friend Alice knows my sister Sarah"
   - Query: "Who knows my family"
   - Expected: Alice → knows → Sarah returned

10. **Test Suite:**
    - Run full test suite: `pytest tests/api/ --ignore=tests/evaluation -v`
    - Expected: 112+ passed, 0 regressions

11. **Update scratch** with findings and results

### Deliverable:

- **Investigation report** (in scratch) — findings for all 5 areas
- **Code changes** (if needed) — Filter prompt enhancement, ontology additions
- **Re-test results** (in scratch) — scenarios 3 & 5 now pass

### Files to Modify:

- `openwebui/faultline_tool.py` — Filter prompt enhancement (if gap found)
- Database (via SQL) — Add system property rel_types to ontology
- No code changes to gate/query unless critical bug found

### Success Criteria:

- Root causes identified for scenarios 3 & 5 ✓
- Filter prompt reviewed ✓
- Ontology audited ✓
- System property rel_types added (if missing) ✓
- Scenario 3 re-test: system facts returned ✓
- Scenario 5 re-test: transitive relationships returned ✓
- Full test suite: 112+ passed, 0 regressions ✓

### Upon Completion:

**If all fixes work and scenarios 3 & 5 now pass:**

Update scratch.md with:
```
## ✓ DONE: dprompt-38b (System Metadata & Transitive Extraction) — 2026-05-10

**Investigation completed:**

**Root cause 1:** [Filter prompt / Ontology / WGM gate / /query]
- Issue: [what was blocking extraction]
- Fix: [what was changed]

**Root cause 2:** [same areas]
- Issue: [what was blocking]
- Fix: [what was changed]

**Results:**
- Scenario 3 (System Metadata): ✓ PASS — IP, OS, hostname returned
- Scenario 5 (Transitive Relationships): ✓ PASS — Alice knows Sarah returned
- Test suite: 112+ passed, 0 regressions ✓

**All 5 scenarios now fully pass. System is production-ready.**

Next: [awaiting direction]
```

**If any scenario still fails:**

Report findings and stop.

### CRITICAL NOTES:

- **The model understands the data.** It's code blocking extraction, not model capability.
- **Filter prompt is most likely culprit.** It probably doesn't ask for system metadata or transitive relationship extraction.
- **Ontology might be missing rel_types.** has_ip, has_os, has_hostname might not exist.
- **Both scenarios 3 & 5 must pass.** System metadata and transitive relationships are valuable. Worth fixing.
- **Conversational acknowledgment ≠ structured extraction.** LLM needs explicit instructions in the prompt.

### Motivation:

The model proved it understands system metadata and transitive relationships. The failure is in how we ask it to extract. Fix the prompt and ontology, and we unlock valuable data extraction. That's the difference between a system that only handles family facts and one that handles the full knowledge graph.

