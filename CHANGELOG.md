# Changelog

## v1.0.7 (2026-05-14) — Query Deduplication Fix

**Query path:** pg_keys dedup uses UUIDs (`_subject_id`/`_object_id`) instead of display names. Fixes duplicate facts when same entity has multiple aliases (chris/user → single deduplicated fact).

**Fixes:** dBug-report-008

## v1.0.6 (2026-05-13) — Metadata-Driven Validation

**rel_types table:** Added metadata columns (`is_symmetric`, `inverse_rel_type`, `is_leaf_only`, `is_hierarchy_rel`). `_get_rel_type_metadata()` with caching replaces all hardcoded validation constants. New rel_types self-describe constraints without code changes.

**Migration:** 022_rel_types_metadata.sql (idempotent column expansion + metadata pre-population).

## v1.0.5 (2026-05-13) — Bidirectional Relationship Validation

**Ingest pipeline:** `_validate_bidirectional_relationships()` prevents impossible bidirectional relationships (`child_of` + `parent_of`). Keeps higher confidence, supersedes lower. Checks `facts` + `staged_facts`.

**Fixes:** dBug-report-006 (bidirectional impossibilities, staged gap)

## v1.0.4 (2026-05-12) — Query Deduplication & Alias Metadata

**Query path:** `/query` deduplicates facts by `(subject_uuid, rel_type, object_uuid)` after merge, keeping highest confidence per triple. `_aliases` metadata attached to each fact with all entity names and `is_preferred` flag.

**Effect:** No more duplicate facts from alias redundancy (christopher spouse mars + chris spouse mars → single deduplicated fact). Filter gets clean results.

**Fixes:** dBug-report-005 (alias redundancy in query results)

## v1.0.3 (2026-05-12) — Semantic Conflict Detection

**Ingest pipeline:** `_detect_semantic_conflicts()` added before Class A/B/C commit. Auto-supersedes ownership/relationship facts when the object entity is already defined as a type/category via hierarchy rels.

**Principle:** The graph IS the source of truth. If `X instance_of Y`, Y is a TYPE — don't allow `owns`/`has_pet`/`works_for` on type entities. Graph self-heals.

**Fixes:** dBug-report-003/004 (type/ownership conflict cleanup)

## v1.0.2 (2026-05-12) — Hierarchy Extraction Enhancement

**Filter prompt:** Hierarchy relationships (`instance_of`, `subclass_of`, `member_of`, `part_of`) moved to primary extraction list in `_TRIPLE_SYSTEM_PROMPT` with 6 multi-domain examples.

**Changes:**
- Added HIERARCHY RELATIONSHIPS section to Filter prompt with definitions and chain examples
- Moved `instance_of`, `subclass_of`, `member_of`, `part_of` from absent/weak to "Common" (primary) extraction list
- 6 multi-domain examples: taxonomic, organizational, infrastructure, hardware, geographical, software

**Fixes:**
- dBug-report-002: Hierarchical Entity Relationships Missing
- `part_of` previously had 0 facts — now extracted with Class B confidence
- `instance_of` and `member_of` now extracted with complete multi-link chains

## v1.0.1 (2026-05-12) — Filter Simplification

**Architecture:** Backend-first query design. Filter no longer implements three-tier gating — it trusts backend /query ranking (Class A > B > C + confidence).

**Changes:**
- Removed Tier 1/2/3 relevance gating logic from Filter (`openwebui/faultline_tool.py`)
- Removed `entity_types` parameter passing (Filter no longer re-gates on entity type)
- Removed Concept/unknown entity filtering from Filter
- Simplified `_filter_relevant_facts()` to: identity rels always pass + confidence threshold

**Fixes:**
- dBug-report-001: Tier 2 Identity Fallback Blocks Tier 3 after Concept Filter
- Category queries ("our pets", "my family") now return complete fact sets
- No UUID leaks in responses for category queries

**See:** docs/ARCHITECTURE_QUERY_DESIGN.md

## v1.0.0 (2026-05-12) — Initial Production Release

Initial production release of FaultLine — a write-validated knowledge graph pipeline with GLiNER2 entity typing, WGM ontology validation, three-class fact classification (A/B/C), graph + hierarchy traversal, self-building ontology, and OpenWebUI filter/function integration.
