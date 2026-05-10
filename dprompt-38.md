# dprompt-38 — System Metadata & Transitive Relationship Extraction: Code Investigation & Fixes

## Purpose

Investigate why Filter LLM understands system metadata and transitive relationships (acknowledges them conversationally) but doesn't extract them as structured edges. Audit Filter prompt, ontology, WGM gate, and query logic. Identify and fix code gaps. Re-test scenarios 3 & 5.

## The Problem

**Scenario 3:** User says "My laptop is named Workstation-X, IP 192.168.1.100, OS Ubuntu 22.04"
- Filter LLM response: "I understand your laptop..." (conversational understanding ✓)
- Query result: "I don't have any specific information" (no facts stored ✗)
- **Likely cause:** LLM understood but didn't extract structured edges, OR extracted but code dropped them

**Scenario 5:** User says "My friend Alice knows my sister Sarah"
- Filter LLM response: Acknowledged Alice and Sarah (entity recognition ✓)
- Query result: No "knows" relationship (edge not stored ✗)
- **Likely cause:** LLM understood but didn't extract "knows" edge, OR extracted but code dropped it

## Investigation Checklist

### 1. Filter Prompt (openwebui/faultline_tool.py)
- [ ] Find Filter LLM prompt that instructs extraction
- [ ] Does it ask for system metadata? (IP, hostname, OS, properties)
- [ ] Does it ask for transitive relationships? (friend_of, knows, met)
- [ ] Are these rel_types explicitly mentioned in the prompt?
- [ ] **Gap:** If prompt doesn't mention them, LLM won't extract them

### 2. Ontology (rel_types table)
- [ ] Query DB: `SELECT * FROM rel_types WHERE rel_type IN ('has_ip', 'has_os', 'has_hostname', 'knows', 'friend_of', 'met');`
- [ ] Check if system property rel_types exist
- [ ] Check if `knows` is defined (should be, it's standard)
- [ ] Check `head_types` and `tail_types` — are they too restrictive?
- [ ] **Gap:** Missing rel_types won't be extracted

### 3. WGM Gate (src/wgm/gate.py)
- [ ] Does gate reject unknown rel_types before storage?
- [ ] Are system property rel_types marked as `engine_generated=false`?
- [ ] Does gate have confidence threshold that drops low-confidence edges?
- [ ] Query gate logic: what happens to system metadata edges?
- [ ] **Gap:** Gate might reject valid edges

### 4. EdgeInput & Ingest (src/api/models.py, src/api/main.py)
- [ ] Does EdgeInput handle arbitrary rel_types?
- [ ] Does /ingest accept non-standard rel_types?
- [ ] Are edges with unknown rel_types stored as Class C (staged)?
- [ ] **Gap:** Code might not handle system properties

### 5. /query Retrieval (src/api/main.py)
- [ ] Does `/query` fetch facts with non-family rel_types?
- [ ] Does it filter by rel_type or return all?
- [ ] Are system property facts included in response?
- [ ] **Gap:** Query might not surface these rel_types

## Root Cause Hypothesis

**Most likely:** Filter prompt doesn't instruct LLM to extract system metadata and transitive relationships. LLM understands conversationally but only extracts family/relationship rel_types because that's what the prompt asks for.

**Secondary:** Ontology missing system property rel_types (has_ip, has_os, has_hostname) → edges get dropped by WGM gate as unknown types.

## Expected Fixes

### Fix 1: Filter Prompt Enhancement
Add explicit instruction for system metadata extraction:
```
Extract system metadata (IP, hostname, OS, properties) using rel_types: has_ip, has_os, has_hostname
Extract transitive relationships: friend_of, knows, met, related_to
```

### Fix 2: Ontology Expansion
Add system property rel_types:
- `has_ip` — entity → IP address (scalar)
- `has_os` — entity → operating system (scalar)
- `has_hostname` — entity → hostname (scalar)
- `has_property` — entity → property value (generic)

### Fix 3: WGM Gate Audit
Ensure gate doesn't reject valid system property edges.

### Fix 4: /query Enhancement
Ensure query returns system metadata facts alongside family facts.

## Test Plan

**After fixes, re-test:**
1. Scenario 3: "My laptop is named Workstation-X, IP 192.168.1.100, OS Ubuntu 22.04"
   - Expected: Query returns system facts (name, IP, OS)
2. Scenario 5: "My friend Alice knows my sister Sarah"
   - Expected: Query "Who knows my family" returns Alice → knows → Sarah

## Files to Check

| File | Check For |
|------|-----------|
| `openwebui/faultline_tool.py` | Filter LLM prompt (extraction instructions) |
| `src/api/models.py` | EdgeInput schema (rel_type constraints) |
| `src/wgm/gate.py` | WGM validation logic (rejection criteria) |
| `src/api/main.py` | /query retrieval logic (fact filtering) |
| Database: `rel_types` table | System property rel_types existence |

