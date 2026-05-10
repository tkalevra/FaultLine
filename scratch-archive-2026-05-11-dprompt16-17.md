# scratch archive — dprompt-15b through dprompt-17 (May 9–11, 2026)

Combined dialogue from scratch.md covering dprompt-15b validation execution,
compound extraction, pronoun guards, preference demotion debugging,
dprompt-17 self-building ontology implementation, and the final
filter augment architectural fix.

Full details preserved in source files. See:
- src/extraction/compound.py — compound + domain-agnostic extractor
- src/re_embedder/embedder.py — ontology evaluator
- src/wgm/gate.py — unknown rel_type handling
- migrations/018_ontology_evaluations.sql — async ontology table
