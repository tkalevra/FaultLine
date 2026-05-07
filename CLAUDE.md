# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What FaultLine Is

FaultLine is a **write-validated knowledge graph** pipeline that intercepts OpenWebUI conversations, extracts named entities and relationships, validates them against an ontology, and persists them to PostgreSQL. Qdrant is a derived vector index — facts flow Postgres → Qdrant via the re-embedder and are queried for memory recall during the inlet phase.

## Pipeline Flow

```
OpenWebUI inlet filter
  ├─▶ Retraction detection (if "forget", "delete", "wrong", etc.)
  │     └─▶ Qwen retraction extraction → POST /retract (inline Qdrant cleanup)
  │           └─▶ confirmation system message → short-circuit (skip ingest/query)
  │
  ├─▶ POST /extract (preflight)   GLiNER2 entity typing (subject/object type context)
  ├─▶ Qwen triple rewrite   entity-typed structured edge extraction from text
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
  │     │                       │                 └─▶ then upserts to Qdrant
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
              └─▶ filtered by intent (location, family, work, etc.) + confidence gate
                    └─▶ merged, deduplicated → injected as system message before last user message

OpenWebUI outlet filter
  └─▶ pass-through (no-op)
```

## Inlet Short-Circuit & Retraction

**Retraction check (first):**
If text contains retraction signals ("forget", "delete", "wrong", "no longer", etc.), Qwen extracts `{subject, rel_type?, old_value?}` and POSTs to `/retract`. If successful, a confirmation system message is injected and inlet returns early — no ingest or query happens.

**Ingest gate:**
Before calling Qwen for fact extraction:
1. Word count ≥ 3, OR message matches a self-identification pattern (`my name is`, `I am`, `call me`, etc.)

If neither condition is met, `will_ingest = False`. `will_query` is always `True` when `QUERY_ENABLED` is set. If both are False the inlet returns immediately with no work done.

**Memory injection gate:**
Facts are filtered by query intent category (location, family, work, pets, physical, identity) before the memory block is built. Additionally, facts below `MIN_INJECT_CONFIDENCE` (default 0.5) are silently excluded. Memory is injected only when facts survive the gate, positioned immediately before the last user message (not appended) for better context proximity.

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
  - Confirmed facts (same subject/object/rel_type) increment `confirmed_count`
  - Promoted to `facts` when `confirmed_count >= 3`
  - Then synced to Qdrant
  - **TTL**: Promoted facts persist indefinitely
- **Qdrant**: Synced both as staged and after promotion

**Class C — Ephemeral/Novel** (staged, expiring without confirmation)
- Anything not in A or B
- Engine-generated types (rel_type with `engine_generated=true`)
- Confidence < 0.6
- Novel types rejected by Qwen
- **Confidence**: 0.4 if llm_inferred
- **Lifecycle**: Staged to `staged_facts` with `expires_at = now() + 30 days`
  - No promotion path
  - Expired facts deleted from `staged_facts` by re_embedder
  - Qdrant points deleted when staged fact expires
  - **TTL**: 30 days from first_seen_at or last confirmation
- **Qdrant**: Synced while active, deleted on expiry

Confidence values determine class assignment:
- User corrections (`is_correction=True`) → always Class A (confidence=1.0)
- User-stated facts (`fact_provenance="user_stated"`) → higher confidence (0.8)
- LLM-inferred facts (`fact_provenance="llm_inferred"`) → lower confidence (0.6)
- Engine-generated types or confidence < 0.6 → Class C

## Query / Retrieval Path & Filtering

`/query` runs three parallel sources and merges them before returning:

1. **Baseline facts** (PostgreSQL, always) — `lives_at`, `lives_in`, `address`, `located_in`, `born_in`, `age`, `height`, `weight`, `works_for`, `occupation`, `nationality`, `has_gender` anchored to the user's canonical identity. These are returned regardless of query text — vector similarity is too low to surface them for unrelated queries like "what's the weather tomorrow?".
2. **Graph traversal** (PostgreSQL, signal-gated) — when the query contains self-referential signals ("my family", "where do i live", etc.), fetches all facts anchored to the user's identity + 2-hop related entities.
3. **Vector similarity** (Qdrant) — `nomic-embed-text-v1.5` embedding, cosine search, `score_threshold: 0.3`, `limit: 10`. Adds associative context not captured by the other two paths.

The three result sets are merged and deduplicated on `(subject, object, rel_type)` with PostgreSQL winning on conflict. Each fact includes a `category` field sourced from `rel_types.category` via the in-memory `_REL_TYPE_META` map (loaded at startup from the database and used throughout the request path for category lookups and type constraint validation), and a `confidence` field for downstream filtering.

**Scalar facts as facts:** Entity attributes (age, height, weight, etc.) are returned in both the separate `attributes` dict AND as fact objects in the `facts` list with `rel_type=attribute_name` and appropriate category. This simplifies filter logic by treating all knowledge uniformly as facts.

**Filtering gate (in the filter, not the API):**
Before building the memory block, `_categorize_query(text, facts)` detects relevant categories from query text keyword signals, but only returns categories that have actual facts in the response (no phantom matches). For realtime queries (weather, news, stock, etc.), only location facts + identity facts are injected, plus a tool-agnostic directive: use whatever tools or capabilities are available. For specific category matches, only that category + identity facts are injected. Baseline behavior injects location + work + identity. Additionally, any fact below `MIN_INJECT_CONFIDENCE` is excluded (default threshold: 0.5).

Memory injection happens in the **inlet** (before the model sees the message). The filter **inserts** a `{"role": "system", "content": memory_block}` immediately before the last user message — this provides better context proximity than appending. Injecting as a system message avoids a known v0.9.x regression where user message content modifications can be overruled downstream in the filter chain.

**Early-exit paths (embed unavailable, Qdrant 404/error):** When embedding fails or Qdrant is unreachable, entity attributes are fetched before merging baseline/direct facts. This ensures scalar facts are always available even when vector search fails.

## Key Files

| File | Role |
|---|---|
| `src/api/main.py` | FastAPI app — `/ingest`, `/query`, `/retract`, `/store_context` endpoints, GLiNER2 lifecycle, fact classification (_classify_fact, _commit_staged), Qdrant cleanup |
| `src/api/models.py` | Pydantic models — `IngestRequest`, `QueryRequest`, `RetractRequest`, `RetractResponse`, `StoreContextRequest`, `StoreContextResponse`, `IngestResponse.staged`, `FactResult.fact_class` + `provenance` |
| `src/wgm/gate.py` | `WGMValidationGate(db, rel_type_registry)` — ontology check + conflict detection + type constraint validation; validates against `_REL_TYPE_META` loaded at startup |
| `src/fact_store/store.py` | `FactStoreManager` — `commit()` for ingest, `retract()` for user-driven fact removal |
| `src/schema_oracle/oracle.py` | `resolve_entities()`, `LABEL_MAP`, `GLIREL_LABELS` — entity resolution helpers and label maps |
| `src/entity_registry/registry.py` | DB-backed `EntityRegistry` — canonical ID assignment, alias tracking, preferred name resolution |
| `src/re_embedder/embedder.py` | Background poll loop — embeds unsynced facts/staged_facts, upserts to Qdrant, promotes Class B, expires Class C, deletes superseded facts |
| `openwebui/faultline_tool.py` | OpenWebUI **Filter** — retraction detection + Qwen extraction, ingest/unstructured fallback, query with category-based filtering, session cache management |
| `openwebui/faultline_function.py` | OpenWebUI **Function** (tool call) — explicit `store_fact()` with Qwen rewrite |
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

When `req.edges` are supplied (from the Qwen rewrite in the filter), they override GLiNER2 inferred edges.

## Qwen Triple Rewrite

Both `faultline_tool.py` and `faultline_function.py` call an LM Studio endpoint before `/ingest` to convert natural language into structured JSON triples. Key constants at module level in both files:
- `_TRIPLE_SYSTEM_PROMPT` — concise extraction rules and output schema (prioritizes clarity over examples)
- `rewrite_to_triples(text, valves)` — async, returns `[]` on any failure

In the filter (`faultline_tool.py`), `rewrite_to_triples` also accepts:
- `context` — prior conversation turns for pronoun resolution
- `typed_entities` — GLiNER2 pre-classifications from `/extract` to guide entity type reasoning
- `memory_facts` — stored facts for pronoun disambiguation; capped to 10 items with relationship facts (spouse, parent_of, child_of, also_known_as, pref_name, sibling_of) prioritized first to reduce token overhead

Valves controlling Qwen: `QWEN_URL`, `QWEN_MODEL` (default `qwen/qwen3.5-9b`), `QWEN_TIMEOUT`.
Payload always includes `"thinking": {"type": "disabled"}` to suppress Qwen3 chain-of-thought.

## Qdrant Collection Naming

`re_embedder` derives collection names via `derive_collection(user_id)`:
- `"anonymous"`, `""`, or `"legacy"` → env `QDRANT_COLLECTION` (default `"faultline-test"`)
- Any other user_id → `"faultline-{user_id}"`

Both the Filter and Function pass the OpenWebUI user UUID as `user_id` so facts land in the correct per-user collection.

## WGM Ontology

FaultLine's triple model `(subject_id, rel_type, object_id)` is semantically equivalent to RDF triples. Relationship types are aligned to **Wikidata property PIDs** as the primary reference standard. SKOS (Simple Knowledge Organization System) and OWL (Web Ontology Language) semantics inform naming and behavior where applicable, without adopting RDF URI syntax (which would be overkill for a personal memory system and would break GLiNER2 bracket constraints).

### Ontology Standards Alignment

**Semantic distinctions:**
- **instance_of** (P31): A named entity belongs to a type class (e.g., "Biscuit instance_of dog"). NOT transitive for type inference.
- **subclass_of** (P279): A type class is a subtype of another class (e.g., "dog subclass_of animal"). IS transitive.
- **pref_name**: The canonical display name for an entity (SKOS prefLabel semantics). Enforced via `is_preferred_label` column.
- **also_known_as**: Alternate name, alias, or nickname (SKOS altLabel semantics). Multiple may exist per entity.
- **same_as**: Full identity equivalence between two entity references (OWL sameAs semantics). Symmetric.

**Symmetric relationships** (storing A→B implies B→A; duplicates suppressed at write time):
- spouse, sibling_of, same_as, friend_of, knows, met

**Inverse relationships** (OWL inverseOf pairs):
- parent_of ↔ child_of
- All others are unidirectional or symmetric

Edges with `rel_type` not in the ontology trigger a Qwen approval call. If Qwen approves with confidence ≥ 0.7, the type is inserted into `rel_types` and the edge is committed immediately. Otherwise the type is queued in `pending_types` and the edge is dropped (`status: "novel"`).

**Type constraints:** The `rel_types` table includes optional `head_types` and `tail_types` columns (ARRAY of entity type strings) to enforce semantic constraints at write time. The WGM gate calls `_check_type_constraints()` after ontology membership check. `ARRAY['ANY']` = unconstrained (default), `ARRAY['SCALAR']` = scalar value (skip object type check, use value string directly). Unknown entity types skip the constraint check with a warning and proceed. This prevents typing errors like "person → owns → person" when the object should be a non-person entity.

| rel_type       | Wikidata PID | Inverse   | Symmetric | W3C Mapping      | Notes                           |
|----------------|--------------|-----------|-----------|------------------|---------------------------------|
| is_a           | P31/P279     | —         | No        | rdf:type (dep.)  | **deprecated**: use instance_of or subclass_of |
| instance_of    | P31          | —         | No        | rdf:type         | entity → type (NOT transitive) |
| subclass_of    | P279         | —         | No        | rdfs:subClassOf  | type → type (IS transitive)     |
| part_of        | P361         | —         | No        | —                | component → whole               |
| created_by     | P170 (inv)   | —         | No        | —                | creation → creator              |
| works_for      | P108 (inv)   | —         | No        | —                | employee → employer             |
| parent_of      | P40          | child_of  | No        | —                | parent → child                  |
| child_of       | P40 (inv)    | parent_of | No        | —                | child → parent                  |
| spouse         | P26          | spouse    | Yes       | —                | partner → partner (symmetric)   |
| sibling_of     | P3373        | sibling_of| Yes       | —                | sibling → sibling (symmetric)   |
| also_known_as  | P742/P1449   | —         | No        | skos:altLabel    | entity → alias (alternate name) |
| pref_name      | —            | —         | No        | skos:prefLabel   | entity → name (preferred display) |
| same_as        | Q39893449    | same_as   | Yes       | owl:sameAs       | entity → entity (identity, symmetric) |
| related_to     | P1659        | —         | No        | skos:related     | loose semantic link             |
| likes          | —            | —         | No        | —                | domain-specific (subject preference) |
| dislikes       | —            | —         | No        | —                | domain-specific (subject preference) |
| prefers        | —            | —         | No        | —                | domain-specific (subject preference) |
| owns           | P1830 (inv)  | —         | No        | —                | owner → property                |
| located_in     | P131         | —         | No        | —                | entity → location               |
| educated_at    | P69          | —         | No        | —                | student → institution           |
| nationality    | P27          | —         | No        | —                | person → country                |
| occupation     | P106         | —         | No        | —                | person → profession             |
| born_on        | P569         | —         | No        | —                | person → date (date of birth)   |
| age            | —            | —         | No        | —                | domain-specific (person → value) |
| knows          | P1891        | knows     | Yes       | —                | person → person (symmetric)     |
| friend_of      | —            | friend_of | Yes       | —                | domain-specific (symmetric)     |
| met            | —            | met       | Yes       | —                | domain-specific (symmetric)     |
| lives_in       | P551         | —         | No        | —                | person → location (residence)   |
| born_in        | P19          | —         | No        | —                | person → location (birthplace)  |
| has_gender     | P21          | —         | No        | —                | person → gender                 |

## Database Schema & Fact Lifecycle

Primary tables:
- `facts(id, user_id, subject_id, object_id, rel_type, provenance, fact_provenance, fact_class, created_at, qdrant_synced, superseded_at, confidence, confirmed_count, last_seen_at, contradicted_by, is_preferred_label)` — relationship edges. Unique on `(user_id, subject_id, object_id, rel_type)`. Soft-delete via `superseded_at IS NOT NULL`. `fact_class` tracks which tier fact came from (always 'A' or promoted 'B'). `fact_provenance` is 'user_stated' or 'llm_inferred'.
- `staged_facts(id, user_id, subject_id, object_id, rel_type, fact_class, provenance, confidence, confirmed_count, first_seen_at, last_seen_at, expires_at, promoted_at, qdrant_synced)` — short-term memory for Class B and C facts. Unique on `(user_id, subject_id, object_id, rel_type)`. Class B promoted when `confirmed_count >= 3`; Class C auto-deleted when `expires_at <= now()`.
- `entity_attributes(user_id, entity_id, attribute, value_text, value_int, value_float, value_date, provenance, sensitivity)` — scalar facts (`age`, `height`, `weight`, `born_on`, `born_in`, `nationality`, `occupation`, `has_gender`) routed here at ingest instead of `facts`. Unique constraint on `(user_id, entity_id, attribute)` prevents duplicates. Values stored as raw strings (no entity resolution on the value side). The "user" anchor entity is created on first scalar write.
- `entities(id, user_id, entity_type)` + `entity_aliases(entity_id, user_id, alias, is_preferred)` — canonical entity registry. `entity_type` check constraint: ('Person','Animal','Organization','Location','Object','Concept','unknown'). **Note**: `entity_aliases` stores all known names for an entity (not secondary aliases), with one marked `is_preferred`. Rename to `entity_names` planned.
- `rel_types(rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior, category, head_types, tail_types)` — live ontology with correction behavior (supersede/hard_delete/immutable), optional `category` for query intent matching (location, family, work, pets, physical, identity, temporal), and optional `head_types`/`tail_types` (entity type constraints as ARRAY). Loaded at startup into `_REL_TYPE_META` in-memory map.
- `pending_types(id, rel_type, subject_id, object_id, flagged_at)` — novel types awaiting approval.

**Fact lifecycle (Phase 4):**

**Class A (Identity/Structural)**
1. **Classification**: `_classify_fact()` assigns Class A based on rel_type or correction flag
2. **Creation**: `FactStoreManager.commit()` → `INSERT INTO facts` with `confidence=0.8|1.0`, `fact_class='A'`, `qdrant_synced=false`, `superseded_at=NULL`
3. **Confirmation**: Re-confirm same `(user_id, subject_id, object_id, rel_type)` → `confirmed_count += 1`, `last_seen_at = now()`, `qdrant_synced` unchanged
4. **Retraction**: `/retract` → behavior determined by `rel_types.correction_behavior`:
   - `"supersede"`: `superseded_at = now()`, `qdrant_synced = false` (async Qdrant delete)
   - `"hard_delete"`: DELETE from facts, inline Qdrant delete
   - `"immutable"`: no-op, user rejection
5. **Sync**: re_embedder polls `qdrant_synced = false`, upserts to Qdrant, marks `qdrant_synced = true`

**Class B (Behavioral/Contextual)**
1. **Classification**: `_classify_fact()` assigns Class B based on rel_type in `_CLASS_B_REL_TYPES`
2. **Staging**: `_commit_staged()` → `INSERT INTO staged_facts` with `confidence=0.8`, `fact_class='B'`, `expires_at=now()+30d`
3. **Confirmation**: Re-confirm same surrogate edge → `confirmed_count += 1`, `last_seen_at = now()`, `expires_at = now()+30d` (reset TTL)
4. **Promotion** (re_embedder): When `confirmed_count >= 3` → `promote_staged_facts()` → INSERT into facts, mark `promoted_at = now()`
5. **Sync**: re_embedder upserts to Qdrant as staged, then again after promotion
6. **Expiry**: If `promoted_at IS NULL` after 30 days, fact is auto-deleted by `expire_staged_facts()`

**Class C (Ephemeral/Novel)**
1. **Classification**: `_classify_fact()` assigns Class C (engine_generated, confidence < 0.6, or unknown rel_type)
2. **Staging**: `_commit_staged()` → `INSERT INTO staged_facts` with `confidence=0.4`, `fact_class='C'`, `expires_at=now()+30d`
3. **No promotion path**: Stays in staged_facts or expires
4. **Sync**: re_embedder upserts to Qdrant while active
5. **Expiry**: re_embedder deletes from both `staged_facts` and Qdrant when `expires_at <= now()`

DB triggers:
- Lowercase `subject_id`, `object_id`, `rel_type` on every INSERT/UPDATE to `facts` and `staged_facts`

## Running / Developing

```bash
# Install with test extras
pip install -e ".[test]"

# Run all stable tests
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing

# Run a single module
pytest tests/schema_oracle/test_oracle.py -v
pytest tests/wgm/test_gate.py -v
pytest tests/fact_store/test_commit.py -v

# Run the API locally (requires .env)
uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload

# Run full stack
docker compose up --build
```

## Environment Variables

```
POSTGRES_DSN=postgresql://user:pass@localhost:5432/faultline
QWEN_API_URL=http://localhost:11434/v1/chat/completions   # used by re_embedder for embeddings
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test    # fallback for anonymous users
REEMBED_INTERVAL=10                 # seconds between re_embedder poll cycles
```

## OpenWebUI Integration

Two separate artifacts in `openwebui/`:

- **`faultline_tool.py`** — install as an OpenWebUI **Filter** (Admin → Functions → Filters). Inlet flow:
  1. **Retraction check**: if text contains "forget", "delete", "wrong", etc., Qwen extracts `{subject, rel_type?, old_value?}` → `/retract` → confirmation injected → short-circuit return
  2. **Memory query**: synchronous `/query` → categorized by intent + confidence threshold → injected immediately before last user message (with session cache for 30s)
  3. **Ingest flow** (if `will_ingest=True`):
     - **Typed edges**: GLiNER2 + Qwen rewrite → fire-and-forget `/ingest` with edges
     - **Unstructured fallback**: no typed edges → fire-and-forget `/store_context` to preserve unstructured text in Qdrant
  4. Outlet: no-op pass-through.

**Key methods:**
- `_categorize_query(text, facts)` — keyword signals + fact authority; returns only categories with actual facts in the response
- `_filter_relevant_facts(facts, categories, identity)` — DB-driven: uses `fact.get("category")` from payload as authority, confidence gating
- `_build_realtime_context(text, facts, identity)` — tool-agnostic directive (uses whatever tools/capabilities available) with location resolution for immediate action
- `_build_memory_block(text, facts, ...)` — text is first param; builds conversational or realtime memory block
- `_fire_store_context(text, user_id)` — fire-and-forget POST to `/store_context` for unstructured text
- Session cache busted on every successful ingest to fetch fresh facts immediately

**Filter valve controls** (Admin → Functions → Filters → Settings):
- `RETRACTION_ENABLED: bool` — enable/disable fact retraction
- `INGEST_ENABLED: bool` — enable/disable automatic fact extraction
- `QUERY_ENABLED: bool` — enable/disable memory injection
- `MIN_INJECT_CONFIDENCE: float` — facts below this threshold are not injected (default 0.5)
- `ENABLE_DEBUG: bool` — verbose logging

Defaults differ: Filter (`faultline_tool.py`) defaults to `"http://192.168.40.10:8001"`; Function (`faultline_function.py`) defaults to `"http://faultline:8001"` (Docker service DNS). Verify these match the running service port (internal container port is 8000; external is 8001).

- **`faultline_function.py`** — install as an OpenWebUI **Function/Tool**. Model explicitly calls `store_fact(text, __user__)`. Qwen rewrites text to triples, strips low-confidence edges, POSTs to `/ingest` with `user_id`.

## Do Not Develop Here

`FaultLine/` (nested directory) is a shed-tool artifact and a duplicate. Do not edit files inside it.  
`tests/evaluation/`, `tests/feature_extraction/`, `tests/model_inference/`, `tests/preprocessing/` contain stubs or intentionally failing tests — exclude from standard test runs.
