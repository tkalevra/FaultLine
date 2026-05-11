# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What FaultLine Is

FaultLine is a **write-validated knowledge graph** pipeline that intercepts OpenWebUI conversations, extracts named entities and relationships, validates them against an ontology, and persists them to PostgreSQL. Qdrant is a derived vector index — facts flow Postgres → Qdrant via the re-embedder and are queried for memory recall during the inlet phase.

## Architecture (v1.0.7)

### Filter (openwebui/faultline_tool.py)
**Filter is dumb, backend is smart.** Filter no longer implements three-tier gating. It trusts backend `/query` ranking (Class A > B > C + confidence) and injects facts in returned order. Identity relationships always pass; everything else passes if confidence ≥ threshold (0.4 default). Sensitivity penalty still applies to PII facts.

### Ingest Pipeline (src/api/main.py)
```
LLM extract → WGM gate → semantic conflict detection → bidirectional validation → Class A/B/C → commit
```
All validation is **metadata-driven** via `rel_types` table (`is_leaf_only`, `is_hierarchy_rel`, `inverse_rel_type`, `is_symmetric`). `_get_rel_type_metadata()` queries metadata at runtime — no hardcoded validation constants. New rel_types self-describe their constraints. Graph self-heals through semantic conflict auto-superseding.

### Query Path (src/api/main.py)
```
baseline facts → graph traversal → hierarchy expansion → Qdrant search → attributes → UUID-based dedup → _aliases metadata → return
```
- `pg_keys` uses `_subject_id`/`_object_id` (UUIDs), not display names — prevents duplicate facts from alias variation
- `_get_entity_aliases()` attaches `_aliases` metadata to each fact
- Merged and deduplicated on `(subject_uuid, rel_type, object_uuid)` with PostgreSQL winning on conflict

## Pipeline Flow (detailed)
OpenWebUI inlet filter
├─▶ Retraction detection (if "forget", "delete", "wrong", etc.)
│     └─▶ LLM retraction extraction → POST /retract (inline Qdrant cleanup)
│           └─▶ entity_aliases cleanup (pref_name hard-delete only)
│                 └─▶ confirmation system message → short-circuit (skip ingest/query)
│
├─▶ POST /extract (preflight)   GLiNER2 entity typing (subject/object type context)
├─▶ LLM triple rewrite   entity-typed structured edge extraction from text
│     ├─▶ POST /ingest (fire-and-forget) [typed edges]
│     │     └─▶ GLiNER2 extract_json   typed schema edge extraction (fallback/override)
│     │           └─▶ WGMValidationGate   ontology + conflict check → status
│     │                 └─▶ _detect_semantic_conflicts   auto-supersedes type/ownership conflicts
│     │                       └─▶ _validate_bidirectional_relationships   prevents child_of + parent_of coexistence
│     │                             └─▶ Fact Classification (Phase 4)
│     │                                   ├─▶ Class A (identity/structural)
│     │                                   │     └─▶ FactStoreManager.commit()  INSERT INTO facts immediately
│     │                                   │           └─▶ re_embedder (background) → Qdrant upsert
│     │                                   ├─▶ Class B (behavioral/contextual)
│     │                                   │     └─▶ _commit_staged()  INSERT INTO staged_facts
│     │                                   │           └─▶ immediate Qdrant sync (no poll delay)
│     │                                   │           └─▶ re_embedder promotes when confirmed_count >= 3
│     │                                   │                 └─▶ staged Qdrant point deleted after commit
│     │                                   │                       └─▶ new facts point upserted next poll cycle
│     │                                   └─▶ Class C (ephemeral/novel)
│     │                                         └─▶ _commit_staged()  INSERT INTO staged_facts
│     │                                               └─▶ re_embedder upserts to Qdrant
│     │                                                     └─▶ expires after 30 days if unconfirmed
│     └─▶ POST /store_context (fire-and-forget) [no typed edges]
│           └─▶ Embed text (nomic-embed-text) → direct Qdrant upsert
│                 └─▶ fact_class=C, confidence=0.4, rel_type="context"
│                       └─▶ no WGM gate, no Postgres write, Qdrant only
│
└─▶ POST /query (synchronous, before model sees message)
├─▶ PostgreSQL baseline facts (always returned for known identity)
├─▶ PostgreSQL graph traversal   `_graph_traverse()` single-hop across facts + staged_facts
├─▶ PostgreSQL hierarchy expansion   `_hierarchy_expand()` upward classification chains
├─▶ Taxonomy-aware entity filtering   `_TAXONOMY_KEYWORDS` → `member_entity_types` gate
├─▶ Qdrant cosine search (nomic-embed-text, score_threshold: 0.3, limit: 10)
├─▶ entity_types metadata   `_build_entity_types()` parallel to preferred_names
├─▶ UUID-based deduplication   `pg_keys` uses `_subject_id`/`_object_id`, not display names
├─▶ _aliases metadata   `_get_entity_aliases()` attaches all entity names with is_preferred flag
├─▶ merged, deduplicated → injected as system message before last user message
├─▶ ⊢ FaultLine Memory header + event_emitter status notification
OpenWebUI outlet filter
└─▶ pass-through (no-op)

## Inlet Short-Circuit & Retraction

**Retraction check (first):**
If text contains retraction signals ("forget", "delete", "wrong", "no longer", etc.), the LLM extracts `{subject, rel_type?, old_value?}` and POSTs to `/retract`. If successful:
- `hard_delete` behavior (pref_name, also_known_as): DELETE from facts + DELETE from entity_aliases where is_preferred=true
- `supersede` behavior: superseded_at = now(), qdrant_synced = false
- `immutable` behavior: no-op, user rejection
A confirmation system message is injected and inlet returns early — no ingest or query happens.

**Ingest gate:**
Before calling the LLM for fact extraction:
1. Word count ≥ 3, OR message matches a self-identification pattern (`my name is`, `I am`, `call me`, etc.)

If neither condition is met, `will_ingest = False`. `will_query` is always `True` when `QUERY_ENABLED` is set.

## Filter Relevance Gating (Simplified — dprompt-53b)

Filter is **dumb** — trusts backend `/query` ranking. Backend graph-proximity is authoritative.

**Identity facts** (`also_known_as`, `pref_name`, `same_as`, `spouse`, `parent_of`, `child_of`, `sibling_of`) always pass.

**Everything else** passes if confidence ≥ threshold (default 0.4 via `MIN_INJECT_CONFIDENCE` valve).

**Sensitivity penalty** still applies per-fact via `calculate_relevance_score()` — PII facts gated unless explicitly asked.

**No tier gating, no entity-type filtering, no Concept/unknown checks.** Filter injects backend-ranked facts in returned order.

## Relevance Scoring (simplified — dprompt-51b)

`calculate_relevance_score(fact, query) -> float [0.0, 1.0]`

Two components (keyword match component removed — graph structure is the signal):

1. **Confidence bonus (0.0–0.3):** `fact.confidence * 0.3`
2. **Sensitivity penalty (-0.5):** applied when `fact.rel_type` is in `_SENSITIVE_RELS` (`born_on`, `lives_at`, `lives_in`, `height`, `weight`, `born_in`) and no explicit request term found in query

Identity rels (`also_known_as`, `pref_name`, `same_as`) always bypass scoring.

## Ingest Validation Pipeline (dprompt-59/62/65)

All validation runs before Class A/B/C assignment. Pipeline order:

```
extract → WGM gate → _detect_semantic_conflicts → _validate_bidirectional_relationships → Class A/B/C → commit
```

### Semantic Conflict Detection (dprompt-59)

`_detect_semantic_conflicts()` auto-supersedes ownership/relationship facts when the object entity is already defined as a type/category/component via hierarchy relationships. Checks both `facts` and `staged_facts` tables.

**Principle:** If `X instance_of Y`, Y is a TYPE, not a separate entity — don't allow `owns`/`has_pet`/`works_for` on type entities.

### Bidirectional Validation (dprompt-62)

`_validate_bidirectional_relationships()` prevents impossible bidirectional relationships (`child_of` + `parent_of` for same entity pair). Keeps higher-confidence version, supersedes lower.

### Metadata-Driven Validation (dprompt-65)

All validation is **metadata-driven** via `rel_types` table columns: `is_symmetric`, `inverse_rel_type`, `is_leaf_only`, `is_hierarchy_rel`. `_get_rel_type_metadata()` queries metadata at runtime with module-level cache. Zero hardcoded validation constants remain — all replaced with metadata queries. New rel_types created by LLM self-describe their constraints without code changes.

## Query Deduplication (dprompt-61/66)

`/query` deduplicates facts by entity UUID, not display names:

1. **Pg_keys:** Uses `_subject_id`/`_object_id` (UUIDs preserved by `_resolve_display_names`) instead of display names — prevents duplicates when same entity has multiple aliases (chris/user → single fact)
2. **Final dedup pass:** Groups facts by `(subject_uuid, rel_type, object_uuid)`, keeps highest confidence
3. **Alias metadata:** `_get_entity_aliases()` attaches `_aliases` dict with all entity names and `is_preferred` flag

## Fact Classification (Phase 4)

Facts are classified at ingest time into three classes:

**Class A — Identity/Structural** (write-through to PostgreSQL immediately)
- pref_name, also_known_as, same_as, parent_of, child_of, spouse, sibling_of, born_on, born_in, has_gender, nationality, instance_of, subclass_of
- **Confidence**: 1.0 if user-stated or correction, 0.8 if llm_inferred
- **Lifecycle**: Committed immediately to `facts` table
- **User corrections always Class A** regardless of rel_type
- **Qdrant**: Synced by re_embedder after insertion

**Class B — Behavioral/Contextual** (staged, promoted on confirmation)
- lives_at, lives_in, works_for, occupation, educated_at, owns, likes, dislikes, prefers, friend_of, knows, met, located_in, related_to, has_pet, part_of, created_by
- **Confidence**: 0.8 if user_stated, 0.6 if llm_inferred
- **Lifecycle**: Staged to `staged_facts`; immediate Qdrant sync at ingest time (no poll delay); promoted when `confirmed_count >= 3`; staged Qdrant point deleted after promotion commits (best-effort, outside transaction); new facts point upserted next poll cycle
- **TTL**: Promoted facts persist indefinitely
- **Query visibility**: Immediately visible via `_fetch_user_facts()` UNION (PostgreSQL) and immediate Qdrant sync (vector search) — no 3-confirmation wait for retrieval

**Class C — Ephemeral/Novel** (staged, expiring without confirmation)
- Anything not in A or B; engine-generated types; confidence < 0.6
- **Confidence**: 0.4 if llm_inferred
- **Lifecycle**: Staged with `expires_at = now() + 30 days`; no promotion path; deleted by re_embedder on expiry

## Query / Retrieval Path & Filtering

`/query` runs multiple parallel sources:
1. **Baseline facts** (PostgreSQL, always) — identity-anchored scalar and relationship facts via `_fetch_user_facts()`
2. **Graph traversal** — `_graph_traverse(db, user_id, entity_id, max_hops=1)` single-hop across `_REL_TYPE_GRAPH` rels, fetching from both `facts` and `staged_facts`
3. **Hierarchy expansion** — `_hierarchy_expand(db, user_id, entity_id, direction="up", max_depth=3)` walks `instance_of`, `subclass_of`, `part_of`, `is_a`, `member_of` chains via SQL `WITH RECURSIVE` CTE
4. **Vector similarity** (Qdrant) — `nomic-embed-text-v1.5`, cosine, `score_threshold: 0.3`, `limit: 10`
5. **Entity attributes** — `_attributes_to_facts()` converts `entity_attributes` rows to fact dicts, merged into the fact list

**Merging:** PostgreSQL facts are authoritative, Qdrant adds associative context. Deduplicated on `(subject_uuid, rel_type, object_uuid)` using `_subject_id`/`_object_id` UUID keys (not display names).

**Final pass:** Facts grouped by UUID triple, highest confidence kept. `_aliases` metadata attached to each fact.

### Graph + Hierarchy Traversal (dprompt-27/28)

Two orthogonal traversal systems:

- **Graph (`_REL_TYPE_GRAPH`):** connectivity — who am I connected to? spouse, parent_of, child_of, sibling_of, has_pet, knows, friend_of, met, works_for, lives_at, lives_in, located_in, owns, educated_at, member_of + identity anchors (pref_name, also_known_as, same_as, age, height, weight, born_on, nationality, has_gender, occupation)
- **Hierarchy (`_REL_TYPE_HIERARCHY`):** composition + classification — what are they, what do they belong to? instance_of, subclass_of, part_of, is_a, member_of

`_hierarchy_expand()` supports bidirectional traversal:
- `direction="up"`: entity → class chain (e.g., fraggle → instance_of → dog → subclass_of → animal)
- `direction="down"`: class → members

Cycle protection via depth tracking in the CTE.

### Taxonomy-Aware Query Filtering (dprompt-47/47c)

`_TAXONOMY_KEYWORDS` maps query keywords to taxonomy groups (family→Person, household→Person+Animal, work→Person+Organization, location→Location, computer_system→Concept+Object). After graph traversal, connected entities are filtered by `member_entity_types` from the matching taxonomy.

Hierarchy-chain-aware: entities with unknown type walk `_hierarchy_expand()` upward to validate membership (e.g., entity type "unknown" but chain resolves to "Animal" → passes household filter).

### Entity Type Metadata in /query (dprompt-52)

`_build_entity_types()` builds an `entity_types` dict parallel to `preferred_names`:
- UUID keys: batched query of `entities` table
- String (display-name) keys: `entity_aliases` → `entities` JOIN

Returned in `/query` response JSON as `"entity_types"`.

### `_fetch_user_facts()` UNION helper

Defined before the `try` block in `/query`. UNIONs `facts` and `staged_facts` tables. Ensures Class B/C staged facts are immediately visible to all PostgreSQL query paths without waiting for the 3-confirmation promotion cycle. Call sites must follow the definition — do not move below callers.

## Name Conflict Resolution (dprompt-32b)

When two entities claim the same preferred name ("gabby" for both user and child), the system detects and resolves collisions:

- `entity_name_conflicts` table: stores pending disputes with UNIQUE constraint
- `registry.register_alias()`: detects collisions, inserts as non-preferred for new entity, stores as pending
- `registry.get_any_alias()`: fallback to non-preferred aliases when preferred name missing
- `re_embedder.resolve_name_conflicts()`: evaluates pending conflicts via LLM context, assigns winner/loser with fallback aliases
- `_resolve_display_names()` in `/query`: falls back to non-preferred aliases via `get_any_alias()` when preferred name is a UUID

Non-destructive: all names preserved, only preferred status changes.

## Entity Type Classification

Three-layer type inference via GLiNER2 extraction → relationship semantics fallback → descriptor context.

**Entity type persistence:** `subject_type`/`object_type` persisted to `entities` table only when current `entity_type = 'unknown'`.

**Type flow:** GLiNER2 extracts types → Filter passes to LLM as context → LLM includes in output → `/ingest` receives edges with types → `entity_type` UPDATE executes.

**Age validation (dprompt-36b):** Entity-type-aware — Person ages 0–150 (strict), non-Person any non-negative (no upper limit). Negative ages rejected for all types.

## Qdrant Collection Naming

`re_embedder` derives collection names via `derive_collection(user_id)`:
- `"anonymous"`, `""`, or `"legacy"` → env `QDRANT_COLLECTION` (default `"faultline-test"`)
- Any other user_id → `"faultline-{user_id}"`

Both Filter and Function pass the OpenWebUI user UUID as `user_id`.

## Re-embedder Promotion & Expiry

**Promotion (`promote_staged_facts`):**
1. Query: `fact_class='B' AND confirmed_count >= 3 AND promoted_at IS NULL AND expires_at > now()`
2. Per-row: INSERT into facts (ON CONFLICT increments confirmed_count) → UPDATE promoted_at → commit
3. Post-commit (outside transaction, best-effort): DELETE staged Qdrant point
4. Next poll cycle: upsert new facts-table point to Qdrant

**Expiry:** Class C rows with `expires_at <= now()` are deleted from `staged_facts`.

**Ontology evaluation:** `evaluate_ontology_candidates()` — frequency ≥ 3 → approve novel rel_type, cosine similarity > 0.85 → map to existing.

**Name conflict resolution:** `resolve_name_conflicts()` — LLM-powered entity disambiguation integrated into main loop.

## Production Hardening (dprompt-41b)

- **Startup validation:** `_validate_startup_config()` checks POSTGRES_DSN, QDRANT_URL, QWEN_API_URL. Logs warning if missing (non-fatal).
- **Health endpoint:** `/health` returns JSON with database, qdrant, llm, re_embedder status. 5s cache.
- **Timeouts:** Configurable via `HTTPX_TIMEOUT` (10s), `DB_TIMEOUT` (30s), `QDRANT_TIMEOUT` (10s).
- **Rate limiting:** `_check_rate_limit()` per-user_id tracking, 100 req/min default (`RATE_LIMIT_PER_MIN`).
- **Query fallback:** PostgreSQL-only response when embedding/Qdrant unavailable.

## WGM Ontology

Triple model `(subject_id, rel_type, object_id)` aligned to Wikidata PIDs. SKOS/OWL semantics where applicable.

**Symmetric:** spouse, sibling_of, same_as, friend_of, knows, met

**Inverse:** parent_of ↔ child_of

**Self-building (dprompt-17):** Novel `rel_type` values → Class C + `ontology_evaluations`. Re-embedder evaluates asynchronously (frequency ≥ 3 → approve, cosine > 0.85 → map, else reject). No LLM approval calls at ingest time.

**Type constraints:** `rel_types.head_types` and `tail_types` (ARRAY). `ARRAY['ANY']` = unconstrained. `ARRAY['SCALAR']` = scalar value.

**Metadata columns (dprompt-65):** `is_symmetric`, `inverse_rel_type`, `is_leaf_only`, `is_hierarchy_rel`, `allows_leaf_rels` — validation framework queries these at runtime. No hardcoded rules.

| rel_type | Wikidata PID | Inverse | Symmetric | W3C Mapping | Notes |
|---|---|---|---|---|---|
| instance_of | P31 | — | No | rdf:type | NOT transitive |
| subclass_of | P279 | — | No | rdfs:subClassOf | IS transitive |
| part_of | P361 | — | No | — | component → whole |
| member_of | — | — | No | — | entity → group taxonomy |
| created_by | P170 (inv) | — | No | — | creation → creator |
| works_for | P108 (inv) | — | No | — | employee → employer |
| parent_of | P40 | child_of | No | — | parent → child |
| child_of | P40 (inv) | parent_of | No | — | child → parent |
| spouse | P26 | spouse | Yes | — | symmetric |
| sibling_of | P3373 | sibling_of | Yes | — | symmetric |
| also_known_as | P742/P1449 | — | No | skos:altLabel | entity → alias |
| pref_name | — | — | No | skos:prefLabel | entity → preferred name |
| same_as | Q39893449 | same_as | Yes | owl:sameAs | identity, symmetric |
| related_to | P1659 | — | No | skos:related | loose link |
| likes/dislikes/prefers | — | — | No | — | subject preference |
| owns | P1830 (inv) | — | No | — | owner → property |
| located_in | P131 | — | No | — | entity → location |
| lives_in | P551 | — | No | — | person → location (residence) |
| lives_at | — | — | No | — | person → address |
| born_in | P19 | — | No | — | person → location (birthplace) |
| has_pet | — | — | No | — | person → animal |
| has_gender | P21 | — | No | — | person → gender |
| educated_at | P69 | — | No | — | student → institution |
| nationality | P27 | — | No | — | person → country |
| occupation | P106 | — | No | — | person → profession |
| born_on | P569 | — | No | — | person → date |
| age | — | — | No | — | person → value |
| height/weight | — | — | No | — | physical measurements |
| knows | P1891 | knows | Yes | — | symmetric |
| friend_of | — | friend_of | Yes | — | symmetric |
| met | — | met | Yes | — | symmetric |

## Database Schema & Fact Lifecycle

Primary tables:
- `facts(id, user_id, subject_id, object_id, rel_type, provenance, fact_provenance, fact_class, created_at, qdrant_synced, superseded_at, confidence, confirmed_count, last_seen_at, contradicted_by, is_preferred_label)` — unique on `(user_id, subject_id, object_id, rel_type)`. Soft-delete via `superseded_at IS NOT NULL`.
- `staged_facts(id, user_id, subject_id, object_id, rel_type, fact_class, provenance, confidence, confirmed_count, first_seen_at, last_seen_at, expires_at, promoted_at, qdrant_synced)` — Class B promoted when `confirmed_count >= 3`; Class C auto-deleted when `expires_at <= now()`.
- `entity_attributes(user_id, entity_id, attribute, value_text, value_int, value_float, value_date, provenance, sensitivity, category)` — scalar facts. Unique on `(user_id, entity_id, attribute)`. `entity_id` always normalized to `"user"` anchor for user-identity scalars.
- `entities(id, user_id, entity_type)` + `entity_aliases(entity_id, user_id, alias, is_preferred)` — canonical entity registry. Note: `entity_aliases` rename to `entity_names` planned.
- `entity_name_conflicts(id, user_id, alias, entity_id_a, entity_id_b, status, resolved_by, resolved_at, created_at)` — pending name collision disputes. UNIQUE on `(user_id, alias)`.
- `entity_taxonomies(id, taxonomy_name, description, member_entity_types, rel_types_defining_group, has_transitivity, transitive_rel_types, is_hierarchical, parent_rel_type)` — data-driven grouping system. Pre-seeded with family, household, work, location, computer_system.
- `rel_types(rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior, category, head_types, tail_types, is_symmetric, inverse_rel_type, is_leaf_only, is_hierarchy_rel, allows_leaf_rels)` — live ontology with validation metadata (dprompt-65). Loaded at startup into `_REL_TYPE_META`. `_get_rel_type_metadata()` queries at runtime.
- `pending_types(id, rel_type, subject_id, object_id, flagged_at)` — novel types awaiting approval.
- `ontology_evaluations` — frequency + cosine similarity tracking for self-building ontology.

DB triggers: Lowercase `subject_id`, `object_id`, `rel_type` on every INSERT/UPDATE. **NO RECURSIVE MATCHING** — all string comparisons must use pre-lowercased values only.

## Entity ID vs Display Name: Semantic Distinction

**CRITICAL: Violations cause entity loss.**

- **Entity UUIDs:** UUID v5 surrogates. Stored in `facts.subject_id`, `facts.object_id` (relationship facts), `entities.id`, `entity_aliases.entity_id`. Never store display names in `*_id` columns.
- **Display Names:** Human-readable strings. Stored in `entity_aliases.alias` (lowercased), `entity_attributes.value_*`.

### Which Rel_Types Have UUID Objects vs String Objects

**SCALAR REL_TYPES (object must be STRING):** pref_name, also_known_as, age, height, weight, born_on, occupation, nationality

**RELATIONSHIP REL_TYPES (object must be UUID or user_id):** has_pet, spouse, parent_of, child_of, friend_of, knows, met, works_for, educated_at, located_in, lives_in, lives_at, born_in, likes, dislikes, prefers, same_as

The `/ingest` validation block has `_SCALAR_OBJECT_RELS` — objects for scalar rels are NEVER resolved to UUIDs. Objects for relationship rels are ALWAYS resolved.

## Key Files

| File | Role |
|---|---|
| `src/api/main.py` | FastAPI app — `/ingest`, `/query`, `/retract`, `/store_context` endpoints, GLiNER2 lifecycle, `_graph_traverse()`, `_hierarchy_expand()`, `_build_entity_types()`, `_clean_preferred_names()`, `_fetch_user_facts()` UNION helper, `_get_entity_aliases()`, `_detect_semantic_conflicts()`, `_validate_bidirectional_relationships()`, `_get_rel_type_metadata()`, fact classification, `_TAXONOMY_KEYWORDS`, startup normalization, age validation |
| `src/api/models.py` | Pydantic models — EdgeInput (with subject_type/object_type), IngestRequest, QueryRequest, RetractRequest, StoreContextRequest |
| `src/wgm/gate.py` | `WGMValidationGate` — ontology check + conflict detection + type constraint validation |
| `src/fact_store/store.py` | `FactStoreManager` — `commit()` for ingest, `retract()` for user-driven fact removal |
| `src/schema_oracle/oracle.py` | `resolve_entities()`, `LABEL_MAP`, `GLIREL_LABELS` |
| `src/entity_registry/registry.py` | DB-backed `EntityRegistry` — UUID v5 surrogates, alias tracking, preferred name resolution, conflict detection |
| `src/re_embedder/embedder.py` | Background poll loop — embeds unsynced facts/staged_facts, promotes Class B, expires Class C, evaluates ontology candidates, resolves name conflicts, Qdrant reconciliation |
| `openwebui/faultline_tool.py` | OpenWebUI **Filter** — retraction + LLM extraction, simplified confidence gating (identity rels always pass), `/query` caching, `⊢ FaultLine Memory` injection |
| `openwebui/faultline_function.py` | OpenWebUI **Function** — explicit `store_fact()` with LLM rewrite |
| `migrations/012_staged_facts.sql` | `staged_facts` table, promotion/expiration indexes |
| `migrations/019_entity_taxonomies.sql` | `entity_taxonomies` table + 5 core taxonomies |
| `migrations/021_name_conflicts.sql` | `entity_name_conflicts` table |
| `migrations/022_rel_types_metadata.sql` | `rel_types` validation metadata columns (dprompt-65) |
| `docker-compose.yml` | Docker orchestration — `network: host` build, env-var-driven configuration |
| `docker-entrypoint.sh` | Migration runner + uvicorn startup + re-embedder background launch |
| `BUGS/` | Bug reports — dBug-001 through dBug-008 |

## Key Principles (Do Not Violate)

- **LLM never has unsupervised write access** — all writes flow through the WGM validation gate
- **PostgreSQL is authoritative** — Qdrant is a derived read-only view
- **Write-time normalization** — `entity_id` normalized to `"user"` anchor at write time
- **No recursive matching** — all string comparisons use pre-lowercased values; guard comments required where `# NO RECURSIVE MATCHING` appears
- **`entity_aliases` is the authoritative alias registry**
- **`faultline-{user_id}` per-user collection naming is live** — must never be broken
- **Nested function definitions must precede call sites** — `_fetch_user_facts()` is defined before the `try` block in `/query`; do not move it below its callers
- **ON CONFLICT must match actual unique constraints** — `entity_aliases` uses `UNIQUE (user_id, alias)`, so all ON CONFLICT clauses must target `(user_id, alias)`, never `(entity_id, user_id, alias)`
- **No name-based entity pre-creation** — entities are created exclusively via `EntityRegistry.resolve()` which generates UUID v5 surrogates
- **Alias sync uses display names, not UUIDs** — `_canonical_to_display` dict maps canonical UUID → original display name
- **All entity_ids must be UUIDs or user_id** — `/ingest` validates this; startup normalization converts legacy string IDs
- **Scalar rel_types have STRING objects, relationship rel_types have UUID objects** — `_SCALAR_OBJECT_RELS` defines the split. Never resolve objects for scalar rels; always resolve for relationship rels.
- **Alias registration must use ON CONFLICT DO UPDATE** — ensures stale preferred flags are corrected
- **Graph + hierarchy are separate traversal systems** — `_REL_TYPE_GRAPH` (connectivity) and `_REL_TYPE_HIERARCHY` (composition) are orthogonal. Do not conflate them.
- **Backend graph-proximity is authoritative for relevance** — Filter trusts backend ranking. No keyword-based re-scoring.
- **Validation is metadata-driven** — `rel_types` table stores validation properties. `_get_rel_type_metadata()` queries at runtime. No hardcoded validation constants. New rel_types self-describe.
- **Deduplication uses UUIDs, not display names** — `pg_keys` built from `_subject_id`/`_object_id`. Display names vary by alias, UUIDs are stable.

## Running / Developing

```bash
pip install -e ".[test]"

pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing

uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
docker compose up --build
```

## Environment Variables
```
POSTGRES_DSN=postgresql://user:pass@localhost:5432/faultline
POSTGRES_USER=faultline
POSTGRES_PASSWORD=faultline
POSTGRES_DB=faultline
QWEN_API_URL=http://localhost:11434/v1/chat/completions
WGM_LLM_MODEL=qwen/qwen3.5-9b
CATEGORY_LLM_MODEL=qwen2.5-coder
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test
REEMBED_INTERVAL=10
HTTPX_TIMEOUT=10
DB_TIMEOUT=30
QDRANT_TIMEOUT=10
DB_POOL_SIZE=10
RATE_LIMIT_PER_MIN=100
FAULTLINE_API_URL=http://localhost:8001
```

## Do Not Develop Here

`FaultLine/` (nested directory) is a shed-tool artifact. Do not edit files inside it.
`tests/evaluation/`, `tests/feature_extraction/`, `tests/model_inference/`, `tests/preprocessing/` — exclude from standard test runs.
