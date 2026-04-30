# FaultLine

**Write-validated knowledge graph** pipeline that intercepts OpenWebUI conversations, extracts named entities and relationships, validates them against an ontology, and persists them to PostgreSQL. Qdrant is a derived vector index — facts flow Postgres → Qdrant via the re-embedder and are queried for memory recall.

## Architecture

```
OpenWebUI inlet filter (faultline_tool.py)
  ├─▶ Qwen triple rewrite        structured edge extraction from text
  │     └─▶ POST /ingest (fire-and-forget)
  │           └─▶ GLiNER2 extract_json   typed schema edge extraction
  │                 └─▶ WGMValidationGate   ontology + conflict check
  │                       └─▶ FactStoreManager.commit()   INSERT INTO facts
  │                             └─▶ re_embedder (background)   → Qdrant upsert
  │
  └─▶ POST /query (sync, before model sees message)
        ├─▶ PostgreSQL — baseline personal facts (location, age, etc.) always returned
        ├─▶ PostgreSQL — graph traversal for self-referential queries (2-hop)
        └─▶ Qdrant — cosine similarity search (nomic-embed-text, score ≥ 0.3)
              └─▶ merged result → injected as system message into body["messages"]
```

Facts are validated against a Wikidata-aligned ontology (RDF/SKOS/OWL semantics) and stored with unique constraint `(user_id, subject_id, object_id, rel_type)`.

## Components

| Module | Path | Role |
|--------|------|------|
| FastAPI App | `src/api/main.py` | `/ingest` and `/query` endpoints, GLiNER2 lifecycle |
| Schema Oracle | `src/schema_oracle/` | Entity registry & canonical ID assignment |
| WGM Gate | `src/wgm/gate.py` | Ontology check + conflict detection state machine |
| Fact Store | `src/fact_store/store.py` | Single-transaction INSERT with ON CONFLICT DO NOTHING |
| Re-embedder | `src/re_embedder/embedder.py` | Background poll loop — embeds unsynced facts to Qdrant |
| OpenWebUI Filter | `openwebui/faultline_tool.py` | Inlet: Qwen rewrite → ingest + query/inject; Outlet: pass-through |
| OpenWebUI Function | `openwebui/faultline_function.py` | Explicit `store_fact()` tool call with Qwen rewrite |

## Quick Start

```bash
# Install with test extras
pip install -e ".[test]"

# Run stable tests (excludes eval/feature/inference/preprocessing stubs)
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing

# Start API server (requires .env)
uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload

# Run full stack with Docker
docker compose up --build
```

## Configuration

Copy `.env.example` to `.env`:

```
POSTGRES_DSN=postgresql://user:pass@localhost:5432/faultline
QWEN_API_URL=http://localhost:11434/v1/chat/completions
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test
REEMBED_INTERVAL=10
```

## Tech Stack

- Python 3.11+
- [FastAPI](https://fastapi.tiangolo.com/) — async HTTP API
- [GLiNER2](https://github.com/urchade/gliner) — typed schema entity extraction
- [psycopg](https://www.psycopg.org/) — async PostgreSQL driver
- [qdrant-client](https://github.com/qdrant/qdrant-client) — vector search
- [Pydantic](https://docs.pydantic.dev/) — request/response models
- [pytest](https://pytest.org/) + [pytest-asyncio](https://pytest-asyncio.readthedocs.io/) — testing

## License

Copyright 2026 tkalevra

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
