# Changelog

All notable changes to FaultLine will be documented in this file.

## [v1.0.9] - 2026-05-15

### Added
- Unified metadata cache for all rel_types (`_REL_TYPE_CACHE`, dprompt-73b)
- Metadata columns to rel_types table: `storage_target` (facts/events/staged_only), `fact_class` (A/B/C)
- Fallback metadata dict (`_FALLBACK_METADATA`) for DB-unavailable scenarios
- Semantic intent classification in Filter (`_should_skip_extraction()`, dprompt-75b)
- Novel rel_types auto-cache on re-embedder refresh (no restart needed)

### Changed
- **Routing logic:** Now metadata-driven via `storage_target` column query instead of hardcoded `_TEMPORAL_REL_TYPES` frozenset
- **Fact classification:** Now metadata-driven via `fact_class` column query instead of hardcoded `_CLASS_A/B_REL_TYPES` frozensets
- **Medical data routing:** `born_on`, `born_in` now route to `facts` table (was: `events` table)
- **Filter ingest gate:** Replaced brittle `_IS_PURE_QUESTION` regex with grammatical-person heuristic
- Schema migration 024 seeds all 47 existing rel_types with appropriate routing metadata

### Removed
- Hardcoded frozensets: `_TEMPORAL_REL_TYPES`, `_CLASS_A_REL_TYPES`, `_CLASS_B_REL_TYPES`
- `_classify_fact()` helper (replaced by metadata queries via `_get_rel_type_metadata()`)
- `_IS_PURE_QUESTION` regex (replaced by `_should_skip_extraction()`)

### Fixed
- **dBug-013:** User medical context (born_on/born_in) now persists to knowledge graph
- **dBug-014:** Medical/personal-context questions now trigger extraction (was silently skipped)
- Novel rel_types work without system restart
- Correction behavior now metadata-driven via `_get_rel_type_metadata()`

### Testing
- **140 tests passed**, 0 new failures, 0 regressions
- Pre-existing failures: 2 (unrelated to this release)

### Migration
- **024_routing_metadata.sql:** Adds `storage_target` + `fact_class` columns, idempotent, safe to re-run

## [v1.0.8] - 2026-05-12

### Added
- Bidirectional relationship completeness (dprompt-70b)
- Extraction prompt now mandates BIDIRECTIONAL EMISSION for inverse rel_types
- Ingest auto-creates missing inverse facts with same confidence and fact_class

### Fixed
- dBug-012: Incomplete bidirectional relationships in knowledge graph

### Testing
- 141 tests passed, 1 pre-existing failure

## [v1.0.7] - 2026-05-10

- Query deduplication fix (dprompt-66): UUID-based pg_keys
- Metadata-driven validation framework (dprompt-65)
- Documentation audit and consistency sync (dprompt-67)

## [v1.0.6] - 2026-05-09

- Metadata-driven validation framework: zero hardcoded validation constants
- Semantic conflict detection and bidirectional validation improvements

## [v1.0.5] - 2026-05-08

- Bidirectional relationship validation (dprompt-62)
- Self-healing graph: semantic conflicts auto-superseded

## [v1.0.4] - 2026-05-07

- Query deduplication and alias metadata (dprompt-61)
- Name conflict resolution improvements

## [v1.0.3] - 2026-05-06

- Entity type classification and age validation improvements

## [v1.0.2] - 2026-05-05

- Fact classification system (Class A/B/C) and staged facts promotion

## [v1.0.1] - 2026-05-04

- Initial production release with WGM ontology and OpenWebUI integration

## [v1.0.0] - 2026-05-01

- Initial release: FaultLine knowledge graph pipeline
