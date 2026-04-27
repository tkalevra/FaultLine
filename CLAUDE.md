# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What FaultLine Is

FaultLine is a **write-validated knowledge graph** pipeline that intercepts OpenWebUI conversations, extracts named entities and relationships, validates them against an ontology, and persists them to PostgreSQL. Qdrant is a **write-only derived index** — facts flow Postgres → Qdrant via the re-embedder; Qdrant is **never queried for retrieval** in the current implementation.

## Pipeline Flow

```
OpenWebUI (inlet filter)
  └─▶ POST /ingest
        └─▶ GLiNER2 (CPU)           entity + relation extraction
              └─▶ resolve_entities()   canonical ID assignment (EntityRegistry)
                    └─▶ WGMValidationGate   ontology + conflict check → status
                          └─▶ FactStoreManager.commit()  INSERT INTO facts
                                └─▶ re_embedder (background) → Qdrant upsert

OpenWebUI (outlet filter)
  └─▶ POST /query
        └─▶ Token-split keyword match on PostgreSQL facts table only
              └─▶ Appends memory block to assistant message (if facts found)
```

## Known Bug: Query Returns No Results

**Symptom:** Asking the AI about stored entities yields no memory block / no call to Qdrant.

**Root causes (in `src/api/main.py` `/query` endpoint, lines 114–122):**

1. **Qdrant is never queried.** The `/query` endpoint does a raw SQL keyword match against PostgreSQL. Qdrant contains embeddings but has no retrieval path — it is write-only.

2. **Naive token split misses multi-word entities.** The query splits `request.text` on spaces and checks `subject_id = ANY(tokens)`. A stored fact for `"alice johnson"` will never match a token `"alice"` because the stored value is `"alice johnson"` (space-joined), not split by word.

3. **Outlet queries the assistant's response, not the user's message.** In `openwebui/faultline_tool.py` `outlet()`, `text = self._last_message(messages, "assistant")`. If the model's response doesn't happen to include entity names verbatim, no facts are retrieved. The user's question is a better retrieval signal.

**Fix direction:**
- Replace token-split SQL with a full-text or ILIKE search, or implement a `/query` path that embeds the query text and does a Qdrant nearest-neighbor lookup.
- In the outlet, use the **user's** last message as the query text, not the assistant's response.

## Key Files

| File | Role |
|---|---|
| `src/api/main.py` | FastAPI app — `/ingest` and `/query` endpoints, GLiNER2 lifecycle |
| `src/api/models.py` | Pydantic request/response models |
| `src/wgm/gate.py` | `WGMValidationGate` — ontology check + conflict detection |
| `src/fact_store/store.py` | `FactStoreManager.commit()` — single-transaction INSERT with ON CONFLICT DO NOTHING |
| `src/schema_oracle/oracle.py` | `EntityRegistry`, `resolve_entities()` — canonical ID assignment |
| `src/re_embedder/embedder.py` | Background poll loop — embeds unsynced facts and upserts to per-user Qdrant collections |
| `openwebui/faultline_tool.py` | OpenWebUI **Filter** (inlet + outlet) — fire-and-forget ingest on user message, query on outlet |
| `openwebui/faultline_function.py` | OpenWebUI **Function** (tool call) — explicit `store_fact()` with edges required |
| `migrations/001_create_facts.sql` | Schema: `facts` table + `qdrant_synced` column + lowercase trigger |

## Qdrant Collection Naming

`re_embedder` derives collection names via `derive_collection(user_id)`:
- `"anonymous"`, `""`, or `"legacy"` → env `QDRANT_COLLECTION` (default `"faultline-test"`)
- Any other user_id → `"faultline-{user_id}"`

The outlet filter sends the OpenWebUI user's UUID as `user_id`, so queries must target the correct per-user collection.

## WGM Ontology

Hard-coded in `src/wgm/gate.py` `SEED_ONTOLOGY`. Allowed `rel_type` values:
`is_a`, `part_of`, `created_by`, `works_for`, `parent_of`, `child_of`, `spouse`, `sibling_of`, `also_known_as`, `related_to`

Edges with `rel_type` not in the ontology return `status: "novel"` and are **not committed** (they are silently dropped — there is no `pending_types` write path in the current gate implementation, despite the table existing in the schema).

## Database Schema

Single table: `facts(id, user_id, subject_id, object_id, rel_type, provenance, created_at, qdrant_synced)`.  
Unique constraint: `(user_id, subject_id, object_id, rel_type)`.  
A DB trigger lowercases `subject_id`, `object_id`, and `rel_type` on every INSERT/UPDATE.

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

- **`faultline_tool.py`** — install as an OpenWebUI **Filter** (Admin → Functions → Filters). Automatically ingests every user message (fire-and-forget) and appends a memory block to assistant responses after querying `/query`.
- **`faultline_function.py`** — install as an OpenWebUI **Function/Tool**. Requires the model to explicitly call `store_fact()` with structured edges; used for intentional, model-directed fact storage.

Both default to `FAULTLINE_URL = "http://192.168.40.10:8001"` / `"http://faultline:8001"` — verify this matches the running service port.

## Do Not Develop Here

`FaultLine/` (nested directory) is a shed-tool artifact and a duplicate. Do not edit files inside it.  
`tests/evaluation/`, `tests/feature_extraction/`, `tests/model_inference/`, `tests/preprocessing/` contain stubs or intentionally failing tests — exclude from standard test runs.
