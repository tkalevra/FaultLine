
---

## Current State (2026-05-12 evening)

### Production (GitHub: tkalevra/FaultLine)
- **v1.0.2** — Hierarchy extraction enhancement (dprompt-56b deployed)
- **v1.0.1** — Filter simplification, backend-first architecture
- **v1.0.0** — Initial release

### Active Issues

| Issue | Status | Next |
|-------|--------|------|
| dBug-report-001 | Fixed (dprompt-53b) | ✓ Closed |
| dBug-report-002 | Fixed (dprompt-56b) | ✓ Closed |
| dBug-report-003 | Fixed (dprompt-58) | ✓ Closed (prompt constraint added) |

### Pending Work

None. All active issues resolved.

**Data cleanup (pinned):** Pre-prod has one stale `user owns morkie` fact from before dprompt-58. Needs manual supersede or retraction.

---

## ✓ DONE: dprompt-58 (Hierarchy Extraction Constraint) — 2026-05-12

**Fix:** Added HIERARCHY CONSTRAINT to `_TRIPLE_SYSTEM_PROMPT`. When `instance_of`/`subclass_of`/`member_of`/`part_of` is extracted, the object is a type/category — NOT a separate entity. LLM instructed to not also extract `owns`/`has_pet`/`works_for`/`lives_in` for the type entity.

**Example:** "I have a dog named Fraggle, a morkie" → extract `fraggle instance_of morkie` + `user has_pet fraggle`, but NOT `user owns morkie`.

**Cross-domain:** Same constraint for engineer (role), Ontario (province), subnet (container), monitoring (component).

**Validation:** Syntax clean, 114 passed, 0 regressions.

**Deployment:** User rebuild of OpenWebUI container needed. Pre-prod has one stale `user owns morkie` fact to clean.

---

## ✓ DONE: dprompt-cleanup-001 (Documentation Commit + Cleanup Scope) — 2026-05-12

**Commit:** `9309fce` — 11 files (3 dprompt specs, 3 bug reports, 2 archives, scratch)
- dBug-report-004 created: stale `user owns morkie` cleanup scoped as P3 for retraction flow enhancement
- Git status clean, all docs archived

**All issues closed.** No pending work. Awaiting direction.

---

## ✓ DONE: dprompt-59 (Conflict Detection & Auto-Superseding) — 2026-05-12

**Implementation:** Added `_detect_semantic_conflicts()` to `src/api/main.py`. Runs before Class A/B/C commit in `/ingest` pipeline.

### How it works

1. When a new fact arrives, checks if the `object` entity already appears as object of a hierarchy relationship (`instance_of`, `subclass_of`, `is_a`, `member_of`, `part_of`)
2. If the object IS a type/category/component, and the new fact's rel_type is a leaf-only rel (`owns`, `has_pet`, `works_for`, `lives_in`, `lives_at`), the new fact is auto-superseded
3. The existing hierarchy relationship is preserved — the graph stays semantically valid

### Example

- `fraggle instance_of morkie` exists → `user owns morkie` is auto-superseded with reason: "type_conflict: morkie is object of instance_of"
- `alice instance_of engineer` exists → `alice works_for engineer` is auto-superseded

### Validation

- Syntax: clean, lines: 3580 → 3674 (+94 lines)
- Tests: 114 passed, 0 regressions

**Deployment needed:** Rebuild faultline backend container on truenas.

---

## ✓ DONE: dprompt-59b (Production Deployment — Conflict Detection) — 2026-05-12

**Deployed:** v1.0.3 to GitHub — `src/api/main.py` with semantic conflict detection.

- Commit: `0c4656f`, Tag: `v1.0.3`
- ABOUT.md + CHANGELOG.md updated
- Sanitized, validated, pushed

**AWAITING USER VERIFICATION.**

---

## ✓ DONE: dprompt-60 (Documentation Review + Production Deployment) — 2026-05-12

**Reviewed & deployed to faultline-prod:**

- `docs/ARCHITECTURE_QUERY_DESIGN.md` — validated against v1.0.3: filter simplification, backend-first, hierarchy handling all accurate ✓
- `docs/PRODUCTION_DEPLOYMENT_GUIDE.md` — validated: deployment flow, sanitization rules, tagging workflow all accurate ✓
- Sanitized: pre-prod bearer token replaced with placeholder `<your-bearer-token>`
- Commit: `5b90ffd`, pushed to GitHub

**No code changes — documentation only.**

---

## 🐛 BUG FOUND: dBug-report-005 (Alias Redundancy & Query Deduplication)

**Status:** Investigation complete — root causes identified

### Issues Found

1. **Alias redundancy:** User has christopher/chris aliases. System stores duplicate facts for each alias:
   - christopher spouse mars
   - chris spouse mars (DUPLICATE)
   - Result: Query response shows "spouse, Mars, and your wife" (appears as separate facts)

2. **Semantic impossibility:** Cyrus has BOTH child_of AND parent_of relationships (shouldn't exist)

3. **Name conflicts unresolved:** mars vs marla stored as separate spouse facts (should merge or pick one)

### Root Causes

**Query deduplication gap:** `/query` returns facts without deduplicating by entity_id. Multiple aliases = multiple identical facts in results.

**Extraction validation gap:** No semantic rules preventing bidirectional relationships (child_of + parent_of for same pair).

### Fix Priority

**dprompt-61 (SHORT TERM):** Query deduplication
- Deduplicate by (subject_id, rel_type, object_id)
- Return facts using preferred_name only
- Result: "spouse Mars" not "spouse, Mars, and wife"

**dprompt-62 (MEDIUM):** Extraction semantic validation
- Prevent impossible bidirectional relationships
- Validate role cardinality (spouse=1, not multiple)

**dprompt-63 (LONG TERM):** Name conflict resolution
- Merge mars/marla if same person
- Remove weaker duplicates

### Database Evidence

```
Spouse facts (6 stored, should be 1):
- christopher spouse mars (0.5)
- chris spouse mars (0.5) — DUP
- christopher spouse marla (0.5) — different person?
- chris spouse marla (0.5) — DUP
- christopher spouse wife (1.0) — generic
- chris spouse wife (1.0) — DUP

Cyrus relationships (impossible):
- cyrus child_of christopher ✓
- cyrus parent_of christopher ✗ (shouldn't exist)
```

### Status

Bug confirmed. Not a data integrity issue (facts are stored correctly), but query deduplication and extraction validation gaps. dprompt-61 (query dedup) will give immediate UX improvement.

---

## ✓ DONE: Update compact-instruct.md (Pre-Compaction)

**Task:** Document all v1.0.3 work and pending items before conversation compaction.

**Updated sections:**
- Current State: v1.0.3 production + three-layer semantic validation
- Critical Files: all dprompts 56b–60, templates, SOP
- Next Steps: dprompt-61 design work documented + dprompt-60 execution ready
- DEEPSEEK_INSTRUCTION_TEMPLATE: format specification + enforced structure
- Philosophy: semantic awareness over brittle automation

**Committed:** `bb793a1` — compact-instruct ready for compaction.

---

## ✓ DONE: dprompt-61 (Query Deduplication via is_preferred) — 2026-05-12

**Implementation:** `src/api/main.py` — `/query` endpoint dedup + alias metadata.

### Changes

1. `_resolve_display_names()`: now preserves original UUIDs as `_subject_id`/`_object_id` for downstream dedup
2. `_get_entity_aliases()`: new helper — returns all aliases for an entity UUID with is_preferred flag
3. Dedup pass after merge: groups facts by `(subject_uuid, rel_type, object_uuid)`, keeps highest confidence per triple, attaches `_aliases` metadata
4. Lines: 3674 → 3839 (+165 lines)

### Effect

- `christopher spouse mars` + `chris spouse mars` → single `user spouse mars` with `_aliases` showing both names
- No UUIDs exposed in response
- Graph/hierarchy chains fully preserved

### Validation

- Syntax: clean ✓
- Tests: 114 passed, 0 regressions ✓

**Deployment needed:** Rebuild faultline backend container on truenas.

**Scratch at 239 lines — needs archive on next cycle.**

---

## #deepseek: PUSH TO PRODUCTION

#claude: dprompt-61 complete. Now push to GitHub following PRODUCTION_DEPLOYMENT_GUIDE.md:

1. Validate: `git log --oneline -20`, check all commits
2. Audit: `grep -r "password\|token\|bearer\|secret" src/ openwebui/` — no hardcoded secrets
3. Commit state: clean (`git status`)
4. Push: `git push origin master && git push origin --tags`
5. Verify: GitHub shows new commits + tags live

**Upon completion:** Update scratch with push confirmation, then STOP.

---
