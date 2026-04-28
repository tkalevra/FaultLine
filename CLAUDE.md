# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What FaultLine Is

FaultLine is a **write-validated knowledge graph** pipeline that intercepts OpenWebUI conversations, extracts named entities and relationships, validates them against an ontology, and persists them to PostgreSQL. Qdrant is a derived vector index — facts flow Postgres → Qdrant via the re-embedder and are queried for memory recall during the inlet phase.

## Pipeline Flow

```
OpenWebUI inlet filter
  ├─▶ Qwen triple rewrite (LM Studio)   structured edge extraction from raw text
  │     └─▶ POST /ingest (fire-and-forget)
  │           └─▶ GLiNER2 extract_json   typed schema edge extraction (fallback/override)
  │                 └─▶ WGMValidationGate   ontology + conflict check → status
  │                       └─▶ FactStoreManager.commit()  INSERT INTO facts
  │                             └─▶ re_embedder (background) → Qdrant upsert
  │
  └─▶ POST /query (synchronous, before model sees message)
        └─▶ embed text → Qdrant cosine search (score_threshold: 0.3)
              └─▶ inject memory block into user message

OpenWebUI outlet filter
  └─▶ pass-through (no-op)
```

## Inlet Short-Circuit

Before calling Qwen, the inlet checks:
1. Word count ≥ 5
2. Message contains at least one keyword from `_INGEST_KEYWORDS` (is, are, married, likes, etc.) or the phrase "also known"

If neither condition is met AND `QUERY_ENABLED` is false, the inlet returns immediately. If `QUERY_ENABLED` is true, the query step always runs regardless of keyword match.

## Query / Retrieval Path

The `/query` endpoint embeds the request text using `nomic-embed-text-v1.5` via Ollama/LM Studio, then does a Qdrant cosine nearest-neighbour search against the per-user collection (`score_threshold: 0.3`, `limit: 10`). Facts are returned from the Qdrant payload — PostgreSQL is not consulted during retrieval.

Memory injection happens in the **inlet** (before the model sees the message), not the outlet. The injected block includes an identity header derived from `user also_known_as` facts, followed by matching fact triples.

## Key Files

| File | Role |
|---|---|
| `src/api/main.py` | FastAPI app — `/ingest` and `/query` endpoints, GLiNER2 lifecycle |
| `src/api/models.py` | Pydantic request/response models |
| `src/wgm/gate.py` | `WGMValidationGate` — ontology check + conflict detection |
| `src/fact_store/store.py` | `FactStoreManager.commit()` — single-transaction INSERT with ON CONFLICT DO NOTHING |
| `src/schema_oracle/oracle.py` | `EntityRegistry`, `resolve_entities()` — canonical ID assignment |
| `src/re_embedder/embedder.py` | Background poll loop — embeds unsynced facts and upserts to per-user Qdrant collections |
| `openwebui/faultline_tool.py` | OpenWebUI **Filter** — inlet: Qwen rewrite → ingest + memory query/inject; outlet: no-op |
| `openwebui/faultline_function.py` | OpenWebUI **Function** (tool call) — explicit `store_fact()` with Qwen rewrite |
| `migrations/001_create_facts.sql` | Schema: `facts` table + `qdrant_synced` column + lowercase trigger |

## GLiNER2 Extraction

`/ingest` uses `model.extract_json(text, schema)` with a typed JSON schema:
```python
{
    "facts": [
        "subject::str::The full proper name of the first entity...",
        "object::str::The full proper name of the second entity...",
        "rel_type::[parent_of|child_of|spouse|sibling_of|also_known_as|works_for]::str::...",
    ]
}
```
The bracket syntax (`[a|b|c]`) is native GLiNER2 choices constraint — do not change it.

When `req.edges` are supplied (from the Qwen rewrite in the filter), they override GLiNER2 inferred edges.

## Qwen Triple Rewrite

Both `faultline_tool.py` and `faultline_function.py` call an LM Studio endpoint before `/ingest` to convert natural language into structured JSON triples. Key constants at module level in both files:
- `_TRIPLE_SYSTEM_PROMPT` — extraction rules and output schema
- `_INGEST_KEYWORDS` — (tool only) word-level short-circuit set
- `rewrite_to_triples(text, valves)` — async, returns `[]` on any failure

Valves controlling Qwen: `QWEN_URL`, `QWEN_MODEL` (`qwen/qwen3.5-9b@q4_k_m`), `QWEN_TIMEOUT`.
Payload always includes `"thinking": {"type": "disabled"}` to suppress Qwen3 chain-of-thought.

## Qdrant Collection Naming

`re_embedder` derives collection names via `derive_collection(user_id)`:
- `"anonymous"`, `""`, or `"legacy"` → env `QDRANT_COLLECTION` (default `"faultline-test"`)
- Any other user_id → `"faultline-{user_id}"`

Both the Filter and Function pass the OpenWebUI user UUID as `user_id` so facts land in the correct per-user collection.

## WGM Ontology

Hard-coded in `src/wgm/gate.py` `SEED_ONTOLOGY`. Allowed `rel_type` values:
`is_a`, `part_of`, `created_by`, `works_for`, `parent_of`, `child_of`, `spouse`, `sibling_of`, `also_known_as`, `related_to`, `likes`, `dislikes`, `prefers`

Edges with `rel_type` not in the ontology return `status: "novel"` and are **not committed**.

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

- **`faultline_tool.py`** — install as an OpenWebUI **Filter** (Admin → Functions → Filters). Inlet: keyword-gated Qwen rewrite → fire-and-forget `/ingest`, then synchronous `/query` → memory injected into user message before model sees it. Outlet: no-op pass-through.
- **`faultline_function.py`** — install as an OpenWebUI **Function/Tool**. Model explicitly calls `store_fact(text, __user__)`. Qwen rewrites text to triples, strips low-confidence edges, POSTs to `/ingest` with `user_id`.

Both default to `FAULTLINE_URL = "http://192.168.40.10:8001"` — verify this matches the running service port (internal container port is 8000; external is 8001).

## Do Not Develop Here

`FaultLine/` (nested directory) is a shed-tool artifact and a duplicate. Do not edit files inside it.  
`tests/evaluation/`, `tests/feature_extraction/`, `tests/model_inference/`, `tests/preprocessing/` contain stubs or intentionally failing tests — exclude from standard test runs.
