# dprompt-39 — Filter Prompt Deployment & Re-Validation: Spec

## Purpose

Deploy the enhanced Filter prompt from dprompt-38b to the OpenWebUI container on truenas, then re-test scenarios 3 & 5 to confirm system metadata and transitive relationship extraction now work end-to-end.

## Problem

dprompt-38b investigation identified the root causes for scenarios 3 & 5 failures:

1. **Filter prompt** (openwebui/faultline_tool.py, lines 103–187) doesn't instruct LLM to extract:
   - System metadata (has_ip, has_os, has_hostname, fqdn, has_ram, has_storage, expires_on)
   - Transitive relationships (knows, friend_of, met, related_to)
   
   The LLM understands conversationally ("I understand your laptop...") but doesn't emit structured edges because the prompt never asks.

2. **Ontology** was missing system property rel_types — FIXED in dprompt-38b, already deployed to truenas DB (has_ip, has_os, has_hostname added).

**The gap:** Filter prompt enhancements are LOCAL ONLY (not deployed to container). Pre-prod OpenWebUI still runs the old prompt, which is why test results didn't improve despite ontology fix.

## Solution

1. **Commit local changes** (Filter prompt + ontology seed list + dprompt files)
2. **Push to origin/master**
3. **Rebuild OpenWebUI container on truenas** (`docker-compose up --build`)
4. **Re-test scenarios 3 & 5** to confirm extraction now works
5. **Verify database state** (system facts + transitive relationships stored)
6. **Run full test suite** to confirm no regressions

## Enhanced Filter Prompt (dprompt-38b)

Lines 163–178 of openwebui/faultline_tool.py add two new extraction sections:

### SYSTEM METADATA
```
- "IP is X", "IP address is X" → {subject, object=X, rel_type="has_ip"}
- "OS is Y", "running Y", "operating system is Y" → {subject, object=Y, rel_type="has_os"}
- "hostname is Z", "named Z" (for devices, NOT people) → {subject, object=Z, rel_type="has_hostname"}
- "FQDN is X" → {subject, object=X, rel_type="fqdn"}
- "RAM", "memory", "GB" → {subject, object="32GB", rel_type="has_ram"}
- "disk", "storage" → {subject, object="500GB NVMe", rel_type="has_storage"}
- "certificate expires on X", "SSL expires X" → {subject, object=X, rel_type="expires_on"}
- Subject: device/computer name, NOT "user"
```

### TRANSITIVE RELATIONSHIPS
```
- "A knows B", "A is friends with B" → {subject=A, object=B, rel_type="knows"}
- "A is friend of B" → {subject=A, object=B, rel_type="friend_of"}
- "A met B" → {subject=A, object=B, rel_type="met"}
- "A is related to B" → {subject=A, object=B, rel_type="related_to"}
- These are symmetric — both directions equivalent
```

## Expected Outcomes

### Scenario 3 (System Metadata)
**Input:** "My laptop is named Workstation-X, IP 192.168.1.100, OS Ubuntu 22.04"

**Before (dprompt-37b):** LLM confused (Mars collision), no facts stored

**After (dprompt-39b):** LLM extracts system facts:
- workstation-x → has_hostname → "Workstation-X"
- workstation-x → has_ip → "192.168.1.100"
- workstation-x → has_os → "Ubuntu 22.04"

**Query:** "Tell me about my computer" returns system properties

### Scenario 5 (Transitive Relationships)
**Input:** "My friend Alice knows my sister Sarah"

**Before (dprompt-37b):** LLM acknowledged, no "knows" edge extracted

**After (dprompt-39b):** LLM extracts relationship:
- alice → knows → sarah (symmetric)

**Query:** "Who knows my family" returns Alice connected to Sarah

## Test Plan

1. Verify Filter prompt locally (lines 163–178 present)
2. Commit all changes to git
3. Push to origin/master
4. SSH to truenas, rebuild container
5. Verify container started (curl /api/models)
6. Scenario 3: Ingest → 2s wait → Query → verify response + database facts
7. Scenario 5: Ingest → 2s wait → Query → verify response + database facts
8. Run full test suite locally (pytest tests/api/)
9. Update scratch.md with results

## Files to Verify / Modify

| File | Change | Status |
|------|--------|--------|
| `openwebui/faultline_tool.py` | Added SYSTEM METADATA + TRANSITIVE RELATIONSHIPS sections (lines 163–178) | ✓ Ready |
| `src/api/main.py` | Added 6 system rel_types to `_MISSING_TYPES` seed list | ✓ Ready |
| `dprompt-38.md` | Investigation spec | ✓ Reference |
| `dprompt-38b.md` | Investigation prompt | ✓ Reference |

## Deployment Checklist

- [ ] Filter prompt verified locally (lines 163–178 present)
- [ ] Changes committed to git (openwebui/faultline_tool.py, src/api/main.py, dprompt files)
- [ ] Pushed to origin/master
- [ ] OpenWebUI container rebuilt on truenas
- [ ] Container started successfully (curl test)
- [ ] Scenario 3 re-tested (system facts stored + returned)
- [ ] Scenario 5 re-tested (transitive relationships stored + returned)
- [ ] Full test suite passed (112+, 0 regressions)
- [ ] scratch.md updated with results

## Success Criteria

- Filter prompt deployed and active in container ✓
- Scenario 3: System metadata facts extracted and returned ✓
- Scenario 5: Transitive relationships extracted and returned ✓
- No UUID leaks in either scenario ✓
- Full test suite: 112+ passed, 0 regressions ✓
- All 5 scenarios (1–5) now fully pass ✓
- System production-ready ✓
