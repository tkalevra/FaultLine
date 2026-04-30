# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What FaultLine Is

FaultLine is a **write-validated knowledge graph** pipeline that intercepts OpenWebUI conversations, extracts named entities and relationships, validates them against an ontology, and persists them to PostgreSQL. Qdrant is a derived vector index — facts flow Postgres → Qdrant via the re-embedder and are queried for memory recall during the inlet phase.

## Pipeline Flow

```
OpenWebUI inlet filter
  ├─▶ POST /extract (preflight)   GLiNER2 entity typing (subject/object type context)
  ├─▶ Qwen triple rewrite   entity-typed structured edge extraction from text
  │     └─▶ POST /ingest (fire-and-forget)
  │           └─▶ GLiNER2 extract_json   typed schema edge extraction (fallback/override)
  │                 └─▶ WGMValidationGate   ontology + conflict check → status
  │                       └─▶ FactStoreManager.commit()  INSERT INTO facts
  │                             └─▶ re_embedder (background) → Qdrant upsert
  │
  └─▶ POST /query (synchronous, before model sees message)
        ├─▶ PostgreSQL baseline facts (always returned for known identity)
        ├─▶ PostgreSQL graph traversal (self-referential signals, 2-hop)
        └─▶ Qdrant cosine search (nomic-embed-text, score_threshold: 0.3)
              └─▶ merged, deduplicated → injected as system message

OpenWebUI outlet filter
  └─▶ pass-through (no-op)
```

## Inlet Short-Circuit

Before calling Qwen for ingest:
1. Word count ≥ 5, OR message matches a self-identification pattern (`my name is`, `I am`, `call me`, etc.)

If neither condition is met, `will_ingest = False`. `will_query` is always `True` when `QUERY_ENABLED` is set. If both are False the inlet returns immediately with no work done.

Memory is injected whenever `/query` returns facts, a `canonical_identity`, or `preferred_names` — there is no secondary relevance gate. The model decides what is relevant to the current message.

## Query / Retrieval Path

`/query` runs three parallel sources and merges them before returning:

1. **Baseline facts** (PostgreSQL, always) — `lives_at`, `lives_in`, `address`, `located_in`, `born_in`, `age`, `height`, `weight`, `works_for`, `occupation`, `nationality`, `has_gender` anchored to the user's canonical identity. These are returned regardless of query text — vector similarity is too low to surface them for unrelated queries like "what's the weather tomorrow?".
2. **Graph traversal** (PostgreSQL, signal-gated) — when the query contains self-referential signals ("my family", "where do i live", etc.), fetches all facts anchored to the user's identity + 2-hop related entities.
3. **Vector similarity** (Qdrant) — `nomic-embed-text-v1.5` embedding, cosine search, `score_threshold: 0.3`, `limit: 10`. Adds associative context not captured by the other two paths.

The three result sets are merged and deduplicated on `(subject, object, rel_type)` with PostgreSQL winning on conflict.

Memory injection happens in the **inlet** (before the model sees the message). The filter appends a `{"role": "system", "content": memory_block}` to `body["messages"]` — this is the safe documented OpenWebUI pattern. Injecting as a system message avoids a known v0.9.x regression where user message content modifications can be overruled downstream in the filter chain.

## Key Files

| File | Role |
|---|---|
| `src/api/main.py` | FastAPI app — `/ingest` and `/query` endpoints, GLiNER2 lifecycle |
| `src/api/models.py` | Pydantic request/response models |
| `src/wgm/gate.py` | `WGMValidationGate` — ontology check + conflict detection |
| `src/fact_store/store.py` | `FactStoreManager.commit()` — single-transaction INSERT with ON CONFLICT DO NOTHING |
| `src/schema_oracle/oracle.py` | `resolve_entities()`, `LABEL_MAP`, `GLIREL_LABELS` — entity resolution helpers and label maps |
| `src/entity_registry/registry.py` | DB-backed `EntityRegistry` — canonical ID assignment, alias tracking, preferred name resolution |
| `src/re_embedder/embedder.py` | Background poll loop — embeds unsynced facts and upserts to per-user Qdrant collections |
| `openwebui/faultline_tool.py` | OpenWebUI **Filter** — inlet: Qwen rewrite → ingest + memory query/inject; outlet: no-op |
| `openwebui/faultline_function.py` | OpenWebUI **Function** (tool call) — explicit `store_fact()` with Qwen rewrite |
| `migrations/001_create_facts.sql` | Schema: `facts` table + `qdrant_synced` column + lowercase trigger |

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

When `req.edges` are supplied (from the Qwen rewrite in the filter), they override GLiNER2 inferred edges.

## Qwen Triple Rewrite

Both `faultline_tool.py` and `faultline_function.py` call an LM Studio endpoint before `/ingest` to convert natural language into structured JSON triples. Key constants at module level in both files:
- `_TRIPLE_SYSTEM_PROMPT` — extraction rules and output schema
- `rewrite_to_triples(text, valves)` — async, returns `[]` on any failure

In the filter (`faultline_tool.py`), `rewrite_to_triples` also accepts `context` (prior conversation turns) and `typed_entities` (GLiNER2 pre-classifications from `/extract`) to guide Qwen's entity type reasoning.

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

## Database Schema

Primary tables:
- `facts(id, user_id, subject_id, object_id, rel_type, provenance, created_at, qdrant_synced, superseded_at, confidence, confirmed_count, last_seen_at, contradicted_by, is_preferred_label)` — relationship edges. Unique on `(user_id, subject_id, object_id, rel_type)`.
- `entity_attributes(user_id, entity_id, attribute, value_text, value_int, value_float, value_date, provenance, sensitivity)` — scalar facts (`age`, `height`, `weight`, `born_on`, `born_in`, `nationality`, `occupation`, `has_gender`) routed here at ingest instead of `facts`.
- `entities(id, user_id, entity_type)` + `entity_aliases(entity_id, user_id, alias, is_preferred)` — canonical entity registry.
- `rel_types(rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior)` — live ontology, loaded at startup.
- `pending_types(id, rel_type, subject_id, object_id, flagged_at)` — novel types awaiting approval.

A DB trigger lowercases `subject_id`, `object_id`, and `rel_type` on every INSERT/UPDATE to `facts`.

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

- **`faultline_tool.py`** — install as an OpenWebUI **Filter** (Admin → Functions → Filters). Inlet: keyword-gated Qwen rewrite → fire-and-forget `/ingest`, then synchronous `/query` → memory injected into user message before model sees it. Outlet: no-op pass-through.
- **`faultline_function.py`** — install as an OpenWebUI **Function/Tool**. Model explicitly calls `store_fact(text, __user__)`. Qwen rewrites text to triples, strips low-confidence edges, POSTs to `/ingest` with `user_id`.

Defaults differ: Filter (`faultline_tool.py`) defaults to `"http://192.168.40.10:8001"`; Function (`faultline_function.py`) defaults to `"http://faultline:8001"` (Docker service DNS). Verify these match the running service port (internal container port is 8000; external is 8001).

## Do Not Develop Here

`FaultLine/` (nested directory) is a shed-tool artifact and a duplicate. Do not edit files inside it.  
`tests/evaluation/`, `tests/feature_extraction/`, `tests/model_inference/`, `tests/preprocessing/` contain stubs or intentionally failing tests — exclude from standard test runs.
