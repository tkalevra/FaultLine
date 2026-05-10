# scratch.md — FaultLine development dialogue

## INSTRUCTION FOR AGENTS

This file is for **questions and dialogue only**. Do NOT dump code, implementation
plans, or test cases here. Use it to:
- Ask design questions
- Request clarification on requirements
- Confirm decisions before coding
- Preface your entry(s) with your tag in markdown: eg #claude followed by your response/question to allow the human to know who's asking or answering what please.

Code goes directly into source files. This file stays lean.

**LENGTH RULE:** If this file exceeds 150 lines, archive everything between the
"## Archive" section and the "---" separator as `scratch-archive-YYYY-MM-DD.md`,
then condense the remaining content to a concise current state summary. The
instruction header stays; only the dialogue and state sections get archived.

---

## Archive

- **scratch-archive-2026-05-11.md** — Phases 1–5 (retrieval, relations, conversation state)
- **scratch-archive-2026-05-11-phases6-10.md** — Phases 6–10 (date/time, events table, UUID resolution)
- **scratch-archive-2026-05-11-dprompt15b.md** — dprompt-15b full-circle validation (7 code fixes, 9-cycle results)
- **scratch-archive-2026-05-11-dprompt16-17.md** — dprompt-16/17: preference chain, compound extraction, self-building ontology, filter augment fix

---

---

## Current State (2026-05-11)

### What's built and verified

**Ingest pipeline** — compound text extraction via `src/extraction/compound.py`, null-subject resolution, user-id-to-surrogate mapping, auto-synthesized `pref_name` + `also_known_as` from text patterns.

**Preference chain** — `_extract_preferred_name()` with 8 patterns, Qwen prompt allowing first-person `pref_name`, auto-synthesis with correct `is_preferred_label` assignment.

**Filter augment (critical fix)** — No longer injects regex-extracted `pref_name` edges. Only correction edges are augmented. Qwen handles preferences; regex handles corrections. This was the root cause of false-positive `pref_name(user, mars)` demoting `chris`.

**Pronoun guards** — `(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )` on first-person preference patterns across `main.py`, `compound.py`, `faultline_tool.py`. Defensive hardening.

**Stopwords** — `_IDENTITY_STOPWORDS` and `_STOPWORDS` expanded with 15+ words falsely captured as names.

**Self-building ontology (dprompt-17)** — Ingest no longer approves novel rel_types via LLM. Unknown types → Class C + `ontology_evaluations`. Re-embedder evaluates asynchronously (frequency ≥ 3 → approve, cosine similarity > 0.85 → map to existing, else reject). Migration 018 applied.

**Gate hardening** — `ON CONFLICT DO NOTHING` on conflict INSERT in `WGMValidationGate`. Novel types return `"unknown"` instead of calling `_try_approve_novel_type()`.

### Known gaps

- **Domain-agnostic retrieval**: System facts (subject="system") not reachable via graph traversal unless system entity is linked to user.
- **Birthday relevance scoring**: FIXED 2026-05-11 — `"old"`, `"age"`, `"how old"` added to `_SENSITIVE_TERMS` in filter's `calculate_relevance_score()`. See dprompt-18.
- **entity_aliases cleanup**: Startup deletes/recreates aliases on restart with pre-existing mixed data. Not a regression.
- **Docker bridge**: Firewalld blocks bridge forwarding on dev host. `docker-compose-dev.yml` uses host networking.

### Test suite

109 passed, 5 skipped, 0 regressions, 1 known gap (aurora retrieval).

### Files changed

| File | Key changes |
|------|-------------|
| `src/api/main.py` | `_extract_preferred_name()`, auto-synthesis, `is_pref` fix, age validation, compound augment, query signals, `unknown` rel_type handling, `_clean_preferred_names` |
| `src/wgm/gate.py` | `ON CONFLICT DO NOTHING`, `"unknown"` return for novel types |
| `src/re_embedder/embedder.py` | `evaluate_ontology_candidates()`, cosine similarity, main loop |
| `openwebui/faultline_tool.py` | LLM prompt fix, augment only corrections, compound extractor, pronoun guards |
| `src/extraction/compound.py` | NEW — compound + generic extraction |
| `src/entity_registry/registry.py` | `resolve("user")` surrogate, `_is_valid_uuid()` |
| `migrations/018_ontology_evaluations.sql` | NEW |
| `tests/api/test_query_compound.py` | NEW |
