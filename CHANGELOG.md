# Changelog

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
