# scratch-archive-2026-05-13.md — May 13 cycle: dprompt-62 execution + bug discovery

## dprompt-62 Execution & Results

dprompt-62 (staged fact validation + bidirectional rules) executed by deepseek.
Implementation added _detect_semantic_conflicts() extension and _validate_bidirectional_relationships().

Post-deployment discovery: bidirectional validation incomplete.

## dBug-report-006 Discovery (Pre-dprompt-62)

Query response showed incorrect pets + alias redundancy.
Database had staged facts with conflicting owns/has_pet patterns.
Root cause: extraction constraint (dprompt-58) incomplete for staged_facts.

Pre-prod cleanup: deleted 2 conflicting staged facts.

## dBug-report-007 Discovery (Post-dprompt-62)

Bidirectional validation failed — user -child_of-> gabby still in database (should not exist).
UUID exposure in response: "7E4Bff75-706E-5Feb-B8B5-F4Ca1247Fd3B is species: morkie mix"

Pre-prod cleanup: deleted impossible child_of fact.

## Corrections Applied

Cyrus "works_for" → "educated_at" (user feedback, deleted wrong rel_type from staged_facts).
Tense correction: Cyrus "studies" (present) not "studied" (past).

## Ready for Next Steps

All cleanups complete. Awaiting user retest before dprompt-63/64 (fix bidirectional logic + UUID resolution).

User feedback: Don't add technical debt. Propose metadata-driven validation framework instead of tactical fixes.

## Decision: dprompt-65 (Metadata-Driven Validation)

Rather than dprompt-63/64 tactical fixes, create unified validation framework:
- rel_types table stores validation metadata (is_symmetric, inverse_rel_type, leaf_only, etc.)
- LLM defines metadata when creating novel rel_types
- Validation queries metadata at runtime, not hardcoded rules
- Applies uniformly to all rel_types, current and future

This eliminates technical debt and scales with dynamic ontology.
