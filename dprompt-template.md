# dprompt-TEMPLATE: Specification Template

**Date:** [YYYY-MM-DD]  
**Author:** [Name]  
**Status:** [Design phase | Ready for implementation | Complete]  
**Severity:** [P0 | P1 | P2 | Non-blocking]  
**Related:** [dprompt-XX, dBug-report-XXX, ARCHITECTURE_QUERY_DESIGN.md, etc.]

## Problem Statement

**What is broken or missing?** (1-2 sentences)

**Why does it matter?** (impact on users/system)

**Root cause** (if known): ...

## Solution Overview

**What approach will we take?** (1-2 sentences)

**Why this approach?** (alternatives considered and rejected)

**Key insight:** (what makes this different from previous attempts)

## Scope Definition

### MUST Do
- Item 1
- Item 2
- Item 3

### MUST NOT Do
- Item 1
- Item 2

### MAY Do (Optional)
- Item 1
- Item 2

## Design Details

### Architecture Changes (if any)
- Where will changes land?
- What's the integration point?
- How does this affect existing code?

### Database Changes (if any)
- New tables? Schema updates?
- Migrations required?

### Test Strategy
- What needs testing?
- Unit vs integration vs live validation?
- Success criteria (quantified)

## Implementation Boundaries

### Investigation Phase
- **Location:** Pre-Prod Only
- **Method:** SSH to truenas, read logs, query database
- **Example:** `ssh truenas -x "sudo docker logs faultline --tail 100"`

### Development Phase
- **Location:** FaultLine-dev Only (`/home/chris/Documents/013-GIT/FaultLine-dev/`)
- **Files:** List exactly which files will be modified
- **No premature optimization:** Stick to scope, don't refactor beyond requirement

### Deployment Phase
- **Manual:** User triggers rebuild/redeploy
- **Files to sync:** Which files from FaultLine-dev → faultline-prod?
- **Post-deployment validation:** What must user verify?

## Success Criteria

- [ ] Criterion 1 (measurable)
- [ ] Criterion 2 (measurable)
- [ ] Criterion 3 (quantified: test count, lines, time, etc.)
- [ ] No regressions (existing tests still pass)
- [ ] Documentation updated

## References & Reading

- Link to CLAUDE.md section
- Link to related dprompt
- Link to architecture doc (if major change)
- Link to bug report (if fixing)

## Notes for Deepseek

**Critical constraints:**
- [List any hard rules specific to this dprompt]
- [e.g., "ONLY test with faultline-wgm-test-10 model"]
- [e.g., "DO NOT modify src/api/main.py"]
- [e.g., "Investigation pre-prod only via SSH"]

**Common pitfalls (avoid):**
- [e.g., "Don't try to optimize the entire Filter, just simplify Tier logic"]
- [e.g., "Don't add new configuration options"]
- [e.g., "Don't commit changes to faultline-prod"]

## Success Looks Like

**After this dprompt is complete:**
- [Specific query returns specific result]
- [Specific error no longer appears in logs]
- [Specific test passes]
- [Code is X lines, down from Y]

---

**Template version:** 1.0  
**Last updated:** 2026-05-12
