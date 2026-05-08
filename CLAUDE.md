# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What FaultLine Is

FaultLine is a **write-validated knowledge graph** pipeline that intercepts OpenWebUI conversations, extracts named entities and relationships, validates them against an ontology, and persists them to PostgreSQL. Qdrant is a derived vector index — facts flow Postgres → Qdrant via the re-embedder and are queried for memory recall during the inlet phase.

## Pipeline Flow
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
│     │                 └─▶ Fact Classification (Phase 4)
│     │                       ├─▶ Class A (identity/structural)
│     │                       │     └─▶ FactStoreManager.commit()  INSERT INTO facts immediately
│     │                       │           └─▶ re_embedder (background) → Qdrant upsert
│     │                       ├─▶ Class B (behavioral/contextual)
│     │                       │     └─▶ _commit_staged()  INSERT INTO staged_facts
│     │                       │           └─▶ re_embedder promotes to facts when confirmed_count >= 3
│     │                       │                 └─▶ staged Qdrant point deleted after promotion commits
│     │                       │                       └─▶ new facts point upserted next poll cycle
│     │                       └─▶ Class C (ephemeral/novel)
│     │                             └─▶ _commit_staged()  INSERT INTO staged_facts
│     │                                   └─▶ re_embedder upserts to Qdrant
│     │                                         └─▶ expires after 30 days if unconfirmed
│     └─▶ POST /store_context (fire-and-forget) [no typed edges]
│           └─▶ Embed text (nomic-embed-text) → direct Qdrant upsert
│                 └─▶ fact_class=C, confidence=0.4, rel_type="context"
│                       └─▶ no WGM gate, no Postgres write, Qdrant only
│
└─▶ POST /query (synchronous, before model sees message)
├─▶ PostgreSQL baseline facts (always returned for known identity)
├─▶ PostgreSQL graph traversal (self-referential signals, 2-hop)
└─▶ Qdrant cosine search (nomic-embed-text, score_threshold: 0.3)
└─▶ relevance scoring gate (calculate_relevance_score, threshold 0.4)
└─▶ merged, deduplicated → injected as system message before last user message
└─▶ ⊢ FaultLine Memory header + event_emitter status notification
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

**Memory injection gate:**
Facts are scored via `calculate_relevance_score()` before injection. Facts scoring below 0.4 are excluded. Identity relationships (`also_known_as`, `pref_name`, `same_as`) always pass regardless of score. Memory is injected only when facts survive the gate, positioned immediately before the last user message for better context proximity. A visible status notification is emitted via `__event_emitter__` showing fact count.

## Relevance Scoring

`calculate_relevance_score(fact, query) -> float [0.0, 1.0]`

Replaces the previous binary category allowlist. Three components:

1. **Query signal match (0.0–0.6):** keyword overlap between lowercased query and `_CAT_SIGNALS[fact.category]`, capped at 0.6
2. **Confidence bonus (0.0–0.3):** `fact.confidence * 0.3`
3. **Sensitivity penalty (-0.5):** applied when `fact.rel_type` is in `_SENSITIVE_RELS` (`born_on`, `lives_at`, `lives_in`, `height`, `weight`, `born_in`) and no explicit request term is found in query

Identity rels (`also_known_as`, `pref_name`, `same_as`) always bypass scoring and are injected unconditionally.

Threshold: `RELEVANCE_THRESHOLD = 0.4` (local constant in `_filter_relevant_facts()`).

Conversation state awareness is planned as a future score contributor (0.0–0.4 range) — see NEXT_STEPS.md.

## Fact Classification (Phase 4)

Facts are classified at ingest time into three classes, each with different write paths and lifecycle:

**Class A — Identity/Structural** (write-through to PostgreSQL immediately)
- pref_name, also_known_as, same_as
- parent_of, child_of, spouse, sibling_of
- born_on, born_in, has_gender, nationality
- instance_of, subclass_of
- **Confidence**: 1.0 if user-stated or correction, 0.8 if llm_inferred
- **Lifecycle**: Committed immediately to `facts` table
- **User corrections always Class A** regardless of rel_type
- **Qdrant**: Synced by re_embedder after insertion

**Class B — Behavioral/Contextual** (staged, promoted on confirmation)
- lives_at, lives_in, works_for, occupation
- educated_at, owns, likes, dislikes, prefers
- friend_of, knows, met, located_in
- related_to, has_pet, part_of, created_by
- **Confidence**: 0.8 if user_stated, 0.6 if llm_inferred
- **Lifecycle**: Staged to `staged_facts` table with `confirmed_count=0`
  - Confirmed facts increment `confirmed_count`
  - Promoted to `facts` when `confirmed_count >= 3`
  - Staged Qdrant point deleted immediately after promotion commits (best-effort, outside transaction)
  - New facts-table Qdrant point upserted next re_embedder poll cycle
  - **TTL**: Promoted facts persist indefinitely
- **Qdrant**: Synced both as staged and after promotion

**Class C — Ephemeral/Novel** (staged, expiring without confirmation)
- Anything not in A or B
- Engine-generated types (rel_type with `engine_generated=true`)
- Confidence < 0.6
- Novel types rejected by LLM
- **Confidence**: 0.4 if llm_inferred
- **Lifecycle**: Staged to `staged_facts` with `expires_at = now() + 30 days`
  - No promotion path
  - Expired facts deleted from `staged_facts` by re_embedder
  - Qdrant points deleted when staged fact expires
  - **TTL**: 30 days from first_seen_at or last confirmation
- **Qdrant**: Synced while active, deleted on expiry

## Query / Retrieval Path & Filtering

`/query` runs three parallel sources and merges them before returning:

1. **Baseline facts** (PostgreSQL, always) — `lives_at`, `lives_in`, `address`, `located_in`, `born_in`, `age`, `height`, `weight`, `works_for`, `occupation`, `nationality`, `has_gender` anchored to the user's canonical identity.
2. **Graph traversal** (PostgreSQL, signal-gated) — when the query contains self-referential signals ("my family", "where do i live", etc.), fetches all facts anchored to the user's identity + 2-hop related entities.
3. **Vector similarity** (Qdrant) — `nomic-embed-text-v1.5` embedding, cosine search, `score_threshold: 0.3`, `limit: 10`.

The three result sets are merged and deduplicated on `(subject, object, rel_type)` with PostgreSQL winning on conflict. Each fact includes a `category` field sourced from `rel_types.category` via the in-memory `_REL_TYPE_META` map, and a `confidence` field for downstream scoring.

**Scalar facts as facts:** Entity attributes (age, height, weight, etc.) are returned in both the separate `attributes` dict AND as fact objects in the `facts` list with `rel_type=attribute_name` and appropriate category.

**Early-exit paths (embed unavailable, Qdrant 404/error):** Entity attributes are fetched before merging baseline/direct facts. Scalar facts are always available even when vector search fails.

## Key Files

| File | Role |
|---|---|
| `src/api/main.py` | FastAPI app — `/ingest`, `/query`, `/retract`, `/store_context` endpoints, GLiNER2 lifecycle, fact classification (_classify_fact, _commit_staged), Qdrant cleanup |
| `src/api/models.py` | Pydantic models — `IngestRequest`, `QueryRequest`, `RetractRequest`, `RetractResponse`, `StoreContextRequest`, `StoreContextResponse`, `IngestResponse.staged`, `FactResult.fact_class` + `provenance` |
| `src/wgm/gate.py` | `WGMValidationGate(db, rel_type_registry)` — ontology check + conflict detection + type constraint validation; validates against `_REL_TYPE_META` loaded at startup |
| `src/fact_store/store.py` | `FactStoreManager` — `commit()` for ingest, `retract()` for user-driven fact removal |
| `src/schema_oracle/oracle.py` | `resolve_entities()`, `LABEL_MAP`, `GLIREL_LABELS` — entity resolution helpers and label maps |
| `src/entity_registry/registry.py` | DB-backed `EntityRegistry` — canonical ID assignment, alias tracking, preferred name resolution |
| `src/re_embedder/embedder.py` | Background poll loop — embeds unsynced facts/staged_facts, upserts to Qdrant, promotes Class B (deletes staged Qdrant point post-promotion), expires Class C, runs reconciliation pass |
| `openwebui/faultline_tool.py` | OpenWebUI **Filter** — retraction detection + LLM extraction, ingest/unstructured fallback, relevance-scored query injection, session cache management, `⊢ FaultLine Memory` branding |
| `openwebui/faultline_function.py` | OpenWebUI **Function** (tool call) — explicit `store_fact()` with LLM rewrite |
| `migrations/001_create_facts.sql` | Schema: `facts` table + `qdrant_synced` column + lowercase trigger |
| `migrations/007_correction_behavior.sql` | Correction behavior enum (supersede/hard_delete/immutable) per rel_type |
| `migrations/012_staged_facts.sql` | Phase 4: `staged_facts` table for Class B/C facts, promotion/expiration indexes, fact_class + fact_provenance columns |
| `migrations/013_rel_type_category.sql` | Ontology enhancement: adds `category TEXT` column to `rel_types` for query intent matching |
| `migrations/014_entity_attributes_unique.sql` | Constraint: unique `(user_id, entity_id, attribute)` on `entity_attributes` table |

## GLiNER2 Extraction

Both `/ingest` and `/extract` use `model.extract_json(text, schema)`. The `rel_type` constraint is built dynamically from the `rel_types` DB table at startup via `_build_rel_type_constraint`; a comprehensive hardcoded fallback is used when the DB is unavailable.

`/ingest` schema — 3 fields:
```python
{
    "facts": [
        "subject::str::The full proper name of the first entity in the relationship. Never a pronoun.",
        "object::str::The full proper name of the second entity in the relationship. Never a pronoun.",
        "rel_type::[<db-loaded constraint>]::str::The relationship type from subject to object.",
    ]
}
```

`/extract` (preflight) schema — 5 fields, adds entity type classification:
```python
{
    "facts": [
        "subject::str::...", "object::str::...", "rel_type::[...]::str::...",
        "subject_type::[Person|Animal|Organization|Location|Object|Concept]::str::...",
        "object_type::[Person|Animal|Organization|Location|Object|Concept]::str::...",
    ]
}
```
The bracket syntax (`[a|b|c]`) is native GLiNER2 choices constraint — do not change it.

**Edge validation:** UUID values (raw strings matching `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` in subject or object) are rejected before entity resolution — they indicate a resolution leak from a prior operation and must be caught early.

**Type persistence:** When `subject_type` or `object_type` are provided by GLiNER2, they are passed through to the WGM gate and persisted to the `entities` table (`UPDATE entities SET entity_type = ? WHERE id = ? AND entity_type = 'unknown'`). This only occurs when the current entity_type is 'unknown', preventing overwrites of previously established types.

When `req.edges` are supplied (from the LLM rewrite in the filter), they override GLiNER2 inferred edges.

## LLM Triple Rewrite

Both `faultline_tool.py` and `faultline_function.py` call an LLM endpoint before `/ingest` to convert natural language into structured JSON triples.

**Model resolution** (`_resolve_llm_config(valves, body)`):
- `LLM_MODEL` valve non-empty → use that model (explicit override)
- `LLM_MODEL` valve empty → passthrough `body.get("model")` (user's selected OpenWebUI model)
- `LLM_URL` valve non-empty → use that endpoint
- `LLM_URL` valve empty → use OpenWebUI's internal endpoint (`http://localhost:3000/api/chat/completions`)

This eliminates cold-load penalties in LM Studio when the user's selected model is already warm.

Key constants:
- `_TRIPLE_SYSTEM_PROMPT` — concise extraction rules and output schema
- `rewrite_to_triples(text, valves, model, url, context, typed_entities, memory_facts)` — async, returns `[]` on any failure

In the filter (`faultline_tool.py`), `rewrite_to_triples` also accepts:
- `context` — prior conversation turns for pronoun resolution
- `typed_entities` — GLiNER2 pre-classifications from `/extract` to guide entity type reasoning
- `memory_facts` — stored facts for pronoun disambiguation; capped to 10 items with relationship facts prioritized

Valve `QWEN_TIMEOUT` controls request timeout (default 10s). Payload always includes `"thinking": {"type": "disabled"}` to suppress Qwen3 chain-of-thought.

## Qdrant Collection Naming

`re_embedder` derives collection names via `derive_collection(user_id)`:
- `"anonymous"`, `""`, or `"legacy"` → env `QDRANT_COLLECTION` (default `"faultline-test"`)
- Any other user_id → `"faultline-{user_id}"`

Both the Filter and Function pass the OpenWebUI user UUID as `user_id` so facts land in the correct per-user collection.

## Re-embedder Promotion & Expiry

**Promotion (`promote_staged_facts`):**
1. Query: `fact_class='B' AND confirmed_count >= 3 AND promoted_at IS NULL AND expires_at > now()`
2. Per-row: INSERT into facts (ON CONFLICT increments confirmed_count) → UPDATE promoted_at → commit
3. Post-commit (outside transaction, best-effort): DELETE staged Qdrant point via `derive_collection(user_id)`
4. Next poll cycle: re_embedder upserts new facts-table point to Qdrant

**Known reconciliation gap:** `reconcile_qdrant()` only queries the `facts` table. Expired `staged_facts` rows that survive a failed Qdrant delete are invisible to reconciliation until the next successful `expire_staged_facts()` run. No fix needed for single-instance deployment.

## WGM Ontology

FaultLine's triple model `(subject_id, rel_type, object_id)` is semantically equivalent to RDF triples. Relationship types are aligned to **Wikidata property PIDs** as the primary reference standard. SKOS and OWL semantics inform naming and behavior where applicable.

**Semantic distinctions:**
- **instance_of** (P31): entity → type class. NOT transitive.
- **subclass_of** (P279): type → type. IS transitive.
- **pref_name**: canonical display name (SKOS prefLabel). Hard-deleted on retraction; entity_aliases cleaned up.
- **also_known_as**: alternate name/alias (SKOS altLabel). Hard-deleted on retraction.
- **same_as**: full identity equivalence (OWL sameAs). Symmetric.

**Symmetric relationships** (A→B implies B→A):
- spouse, sibling_of, same_as, friend_of, knows, met

**Inverse relationships:**
- parent_of ↔ child_of

Novel `rel_type` values trigger an LLM approval call. Confidence ≥ 0.7 → auto-approve and insert into `rel_types`. Lower → queue in `pending_types`, drop edge.

**Type constraints:** `rel_types.head_types` and `tail_types` (ARRAY) enforce semantic constraints at write time. `ARRAY['ANY']` = unconstrained. `ARRAY['SCALAR']` = scalar value. Unknown entity types skip constraint check with warning.

| rel_type | Wikidata PID | Inverse | Symmetric | W3C Mapping | Notes |
|---|---|---|---|---|---|
| is_a | P31/P279 | — | No | rdf:type (dep.) | **deprecated**: use instance_of or subclass_of |
| instance_of | P31 | — | No | rdf:type | entity → type (NOT transitive) |
| subclass_of | P279 | — | No | rdfs:subClassOf | type → type (IS transitive) |
| part_of | P361 | — | No | — | component → whole |
| created_by | P170 (inv) | — | No | — | creation → creator |
| works_for | P108 (inv) | — | No | — | employee → employer |
| parent_of | P40 | child_of | No | — | parent → child |
| child_of | P40 (inv) | parent_of | No | — | child → parent |
| spouse | P26 | spouse | Yes | — | symmetric |
| sibling_of | P3373 | sibling_of | Yes | — | symmetric |
| also_known_as | P742/P1449 | — | No | skos:altLabel | entity → alias |
| pref_name | — | — | No | skos:prefLabel | entity → preferred display name |
| same_as | Q39893449 | same_as | Yes | owl:sameAs | identity, symmetric |
| related_to | P1659 | — | No | skos:related | loose semantic link |
| likes | — | — | No | — | subject preference |
| dislikes | — | — | No | — | subject preference |
| prefers | — | — | No | — | subject preference |
| owns | P1830 (inv) | — | No | — | owner → property |
| located_in | P131 | — | No | — | entity → location |
| educated_at | P69 | — | No | — | student → institution |
| nationality | P27 | — | No | — | person → country |
| occupation | P106 | — | No | — | person → profession |
| born_on | P569 | — | No | — | person → date |
| age | — | — | No | — | person → value |
| knows | P1891 | knows | Yes | — | symmetric |
| friend_of | — | friend_of | Yes | — | symmetric |
| met | — | met | Yes | — | symmetric |
| lives_in | P551 | — | No | — | person → location (residence) |
| born_in | P19 | — | No | — | person → location (birthplace) |
| has_gender | P21 | — | No | — | person → gender |

## Database Schema & Fact Lifecycle

Primary tables:
- `facts(id, user_id, subject_id, object_id, rel_type, provenance, fact_provenance, fact_class, created_at, qdrant_synced, superseded_at, confidence, confirmed_count, last_seen_at, contradicted_by, is_preferred_label)` — unique on `(user_id, subject_id, object_id, rel_type)`. Soft-delete via `superseded_at IS NOT NULL`.
- `staged_facts(id, user_id, subject_id, object_id, rel_type, fact_class, provenance, confidence, confirmed_count, first_seen_at, last_seen_at, expires_at, promoted_at, qdrant_synced)` — Class B promoted when `confirmed_count >= 3`; Class C auto-deleted when `expires_at <= now()`.
- `entity_attributes(user_id, entity_id, attribute, value_text, value_int, value_float, value_date, provenance, sensitivity)` — scalar facts routed here at ingest. Unique on `(user_id, entity_id, attribute)`. `entity_id` always normalized to `"user"` anchor for user-identity scalars.
- `entities(id, user_id, entity_type)` + `entity_aliases(entity_id, user_id, alias, is_preferred)` — canonical entity registry. `entity_type` check constraint: ('Person','Animal','Organization','Location','Object','Concept','unknown'). Note: `entity_aliases` rename to `entity_names` planned.
- `rel_types(rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior, category, head_types, tail_types)` — live ontology. Loaded at startup into `_REL_TYPE_META`.
- `pending_types(id, rel_type, subject_id, object_id, flagged_at)` — novel types awaiting approval.

DB triggers: Lowercase `subject_id`, `object_id`, `rel_type` on every INSERT/UPDATE to `facts` and `staged_facts`. NO RECURSIVE MATCHING — all string comparisons must use pre-lowercased values only.

## Running / Developing

```bash
# Install with test extras
pip install -e ".[test]"

# Run all stable tests
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing

# Run specific modules
pytest tests/schema_oracle/test_oracle.py -v
pytest tests/wgm/test_gate.py -v
pytest tests/fact_store/test_commit.py -v
pytest tests/embedder/test_promotion.py -v
pytest tests/filter/test_relevance.py -v
pytest tests/api/test_retract.py -v

# Run the API locally (requires .env)
uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload

# Run full stack
docker compose up --build
```

## Environment Variables
POSTGRES_DSN=postgresql://user:pass@localhost:5432/faultline
QWEN_API_URL=http://localhost:11434/v1/chat/completions   # used by re_embedder for embeddings
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test    # fallback for anonymous users
REEMBED_INTERVAL=10                 # seconds between re_embedder poll cycles

## OpenWebUI Integration

Two separate artifacts in `openwebui/`:

- **`faultline_tool.py`** — install as an OpenWebUI **Filter** (Admin → Functions → Filters). Inlet flow:
  1. **Retraction check**: LLM extracts `{subject, rel_type?, old_value?}` → `/retract` → entity_aliases cleanup (pref_name) → confirmation injected → short-circuit
  2. **LLM config resolution**: `_resolve_llm_config(valves, body)` — model and URL resolved once per inlet call
  3. **Memory query**: synchronous `/query` → relevance scored (threshold 0.4) → injected before last user message → `⊢ FaultLine Memory` header → `__event_emitter__` status notification
  4. **Ingest flow** (if `will_ingest=True`):
     - **Typed edges**: GLiNER2 + LLM rewrite → fire-and-forget `/ingest` with edges
     - **Unstructured fallback**: fire-and-forget `/store_context`
  5. Outlet: no-op pass-through.

**Filter valve controls:**
- `RETRACTION_ENABLED: bool` — enable/disable fact retraction
- `INGEST_ENABLED: bool` — enable/disable automatic fact extraction
- `QUERY_ENABLED: bool` — enable/disable memory injection
- `MIN_INJECT_CONFIDENCE: float` — facts below this threshold excluded (default 0.5)
- `LLM_MODEL: str` — extraction model override (empty = passthrough user's model)
- `LLM_URL: str` — LLM endpoint override (empty = OpenWebUI internal endpoint)
- `QWEN_TIMEOUT: int` — LLM request timeout in seconds (default 10)
- `ENABLE_DEBUG: bool` — verbose logging

Defaults: Filter (`faultline_tool.py`) defaults to `"http://192.168.40.10:8001"`; Function (`faultline_function.py`) defaults to `"http://faultline:8001"`. Verify these match the running service port (internal 8000; external 8001).

- **`faultline_function.py`** — install as an OpenWebUI **Function/Tool**. Model explicitly calls `store_fact(text, __user__)`. LLM rewrites text to triples, strips low-confidence edges, POSTs to `/ingest` with `user_id`.

## Key Principles (Do Not Violate)

- **LLM never has unsupervised write access** — all writes flow through the WGM validation gate
- **PostgreSQL is authoritative** — Qdrant is a derived read-only view, never the source of truth
- **Write-time normalization** — `entity_id` normalized to `"user"` anchor at write time for user-identity facts
- **No recursive matching** — all string comparisons use pre-lowercased values; guard comments required
- **`entity_aliases` is the authoritative alias registry** — use directly, not chained through `facts`
- **Wren deployment cannot be trusted verbally** — verify edits via `sed` before rebuild
- **`faultline-{user_id}` per-user collection naming is live** — must never be broken

## Do Not Develop Here

`FaultLine/` (nested directory) is a shed-tool artifact and a duplicate. Do not edit files inside it.  
`tests/evaluation/`, `tests/feature_extraction/`, `tests/model_inference/`, `tests/preprocessing/` contain stubs or intentionally failing tests — exclude from standard test runs.