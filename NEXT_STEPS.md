# FaultLine — Pending Actions

**Current Status (2026-05-08):** Full write-validated knowledge graph pipeline operational. Relevance scoring, LLM passthrough, and injection bug fixes complete. Focus is on test coverage expansion and prompt robustness.

## Completed

- ✅ **Dual-path query** — baseline personal facts + vector similarity merge
- ✅ **Fact classification (Phase 4)** — Class A/B/C lifecycle with staging and promotion
- ✅ **Retraction flow** — user-driven fact removal with correction behavior enums
- ✅ **OpenWebUI Filter** — retraction detection, pronoun resolution via memory facts, confidence gating
- ✅ **WGM ontology** — Wikidata-aligned rel_types with type constraints and correction behavior
- ✅ **Re-embedder** — background service for Qdrant sync, fact promotion/expiry
- ✅ **Streamlined Qwen prompt** — concise extraction rules (ENTITY, RELATIONSHIP, REL_TYPE, UNITS sections)
- ✅ **Memory facts prioritization** — cap to 10 items, relationship facts prioritized
- ✅ **Deadname prevention** — pref_name gate removed, also_known_as gate kept, preferred name flip flow validated
- ✅ **entity_attributes normalization** — entity_id normalized to "user" anchor by construction; confirmed by audit
- ✅ **Promotion pipeline** — orphaned Qdrant point fix (delete after commit, outside transaction); tests implemented
- ✅ **pref_name retraction alias cleanup** — entity_aliases hard-deleted on pref_name retraction; 3 tests passing
- ✅ **provenance="manual" cleanup** — malformed fact + orphaned entity hard-deleted from DB
- ✅ **Relevance scoring** — replaced binary category filtering with continuous scoring; PII sensitivity penalty (-0.5); query-signal match (0–0.6); confidence bonus (0–0.3); threshold 0.4; 7 tests passing
- ✅ **Memory block branding** — `⊢ FaultLine Memory` header; visible status event via `__event_emitter__`
- ✅ **LLM model passthrough (filter)** — replaced `QWEN_MODEL`/`QWEN_URL` with `LLM_MODEL`/`LLM_URL`; empty = passthrough user's selected model; eliminates cold-load penalty
- ✅ **Relevance scoring bug fixes** — three bugs fixed: fallback leak returning all facts when scored empty; entity attributes bypassing scoring; "tall" not matching height sensitivity terms; 10 tests passing
- ✅ **Backend LLM model env vars** — replaced hardcoded `"qwen/qwen3.5-9b"` (WGM gate) and `"qwen2.5-coder"` (category inference) with `WGM_LLM_MODEL` and `CATEGORY_LLM_MODEL` env vars; `.env.example` and docker-compose updated

## Pending

### High Priority

1. **Test coverage expansion** — `tests/` currently excludes evaluation, feature_extraction, model_inference, preprocessing (intentional stubs). Complete test suite for:
   - `src/api/main.py` endpoints (_classify_fact, _commit_staged, retraction paths)
   - `src/fact_store/` commit and retraction flows
   - `src/wgm/` type constraint validation
   - OpenWebUI filter inlet logic (cache hit/miss, filtering, injection positioning)
   - `tests/embedder/test_promotion.py` — promote_staged_facts, expire_staged_facts, poll cycle (implemented)
   - `tests/filter/test_relevance.py` — relevance scoring (10 tests, implemented)
   - `tests/api/test_retract.py` — pref_name retraction alias cleanup (3 tests, implemented)

2. **Qwen prompt robustness** — expand date/time extraction:
   - Birthday patterns ("born on X", "my birthday is May 3rd")
   - Recurring events ("our anniversary is X")
   - Meeting dates ("we met on X")
   - Relative dates ("next week", "last month")
   - Currently prompt supports UNITS (age, height, weight) but needs DATES AND EVENTS section

3. **Entity type persistence** — `/extract` pre-classification only updates `entity_type='unknown'` entities. Verify:
   - Type overwrites don't corrupt existing classifications
   - Cascade rules (Person → can't be owned, Animal → can't be spouse) enforce correctly at write time
   - Fallback for entities where type is ambiguous

4. **Conversation state awareness** — next phase of relevance scoring. Slot into `calculate_relevance_score()` as additional score contributor (0.0–0.4):
   - Mid-operation detection (active command sequence, SSH session, etc.)
   - Operational fact surfacing when context demands
   - Does not require touching existing scoring structure

### Medium Priority

5. **Session memory cache optimization** — `_SESSION_MEMORY_CACHE` in faultline_tool.py uses 30s TTL. Evaluate:
   - Cache miss frequency under real OpenWebUI workload
   - Whether 30s is appropriate for conversation patterns
   - Cache invalidation on successful ingest (currently implemented)

6. **Edge validation tightening** — UUID leak detection in place (`_UUID_RE` check). Ensure:
   - No entity aliases persisted as raw subject/object strings
   - Entity resolution at `/ingest` and `/extract` doesn't regress to UUIDs

7. **Qdrant collection scoping** — per-user collections via `derive_collection(user_id)`. Verify:
   - Anonymous/legacy user routing to `QDRANT_COLLECTION` env default
   - Multi-tenant isolation (no cross-user query leaks)
   - Deletion on user exit (if applicable)

### Low Priority

8. **Schema documentation** — update database schema references:
   - `entity_aliases` planned rename to `entity_names`
   - `rel_types.category` field usage in query intent matching
   - `rel_types.head_types` and `tail_types` constraints (ARRAY of entity types)

9. **Code cleanup** — remove artifacts:
   - Delete `FaultLine/` (nested directory, shed-tool artifact)
   - Confirm test stubs in `tests/evaluation/`, `tests/feature_extraction/`, etc. are intentional

10. **Integration testing** — full end-to-end flow:
    - OpenWebUI inlet → retraction, ingest, query → outlet
    - /ingest → WGM gate → fact classification → re-embedder sync → Qdrant
    - /query baseline + graph traversal + vector merge → memory injection

11. **Reconciliation gap** — `reconcile_qdrant()` only queries `facts` table. Expired `staged_facts` rows surviving a failed Qdrant delete are invisible to reconciliation until next successful `expire_staged_facts()` run. Known limitation, no fix needed for single-instance deployment.

## Notes

- **Qwen prompt (2026-05-07):** Simplified from 73 to 34 lines. Next: expand DATES section for birthday/anniversary/meeting extraction.
- **Memory facts capping:** Prioritizes relationship facts (6 rel_types) before others, capped at 10 total.
- **Filter ingest gate:** ≥5 words OR identity pattern OR third-person preference signals.
- **Promotion pipeline (2026-05-07):** Qdrant orphan fix post-commit, best-effort. Reconciliation handles stragglers via not_in_pg. See Low Priority #11 for known gap.
- **Relevance scoring (2026-05-08):** Threshold 0.4. Identity rels always pass. PII penalty -0.5 unless explicitly queried. Entity attributes now scored before injection. Fallback leak fixed. Conversation state awareness deferred to High Priority #4.
- **LLM passthrough (2026-05-08):** Filter uses `LLM_MODEL`/`LLM_URL` valves (empty = passthrough). Backend uses `WGM_LLM_MODEL` and `CATEGORY_LLM_MODEL` env vars. Embedding model (`nomic-embed-text-v1.5`) remains hardcoded — infrastructure, not user-configurable.