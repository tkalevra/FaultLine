# FaultLine

A write-validated knowledge graph pipeline that extracts entities and relationships from natural language, validates them against an ontology, and persists them to PostgreSQL with Qdrant vector indexing for semantic memory recall.

## Features

- **LLM-First Extraction** — Filter LLM extracts typed edges from conversation text with GLiNER2 entity typing
- **Context-Enriched /extract** — GLiNER2 receives fresh database context (entity registry, ontology metadata, user facts) enabling resolution of implicit pronouns and context-dependent entities
- **Ontology Validation** — WGM (Write-Gate-Model) validates every edge against a live ontology with type constraints and conflict detection
- **Fact Classification** — Facts classified as Identity/Structural (Class A), Behavioral/Contextual (Class B), or Ephemeral/Novel (Class C) with appropriate lifecycle management
- **Graph + Hierarchy Traversal** — Two orthogonal query systems: connectivity graph (who am I connected to?) and composition hierarchy (what are they?)
- **Self-Building Ontology** — Novel relationship types evaluated asynchronously by re-embedder (frequency-based approval, cosine similarity mapping)
- **Name Collision Resolution** — Conflict detection at ingest with LLM-powered re-embedder resolution for disambiguation
- **Sensitivity Gating** — Sensitive facts (birthday, address) gated from generic queries, revealed on explicit request
- **Entity-Type-Aware Validation** — Age validation respects entity types (Person: 0–150, Planet: unlimited)
- **Per-User Isolation** — Facts and collections scoped per user_id (`faultline-{user_id}`)
- **Health Monitoring** — `/health` endpoint with DB, Qdrant, LLM dependency status
- **Rate Limiting** — Per-user configurable rate limiting
- **Graceful Degradation** — Query falls back to PostgreSQL when Qdrant/embedding unavailable

## Architecture

FaultLine operates as a FastAPI service with a background re-embedder and an OpenWebUI filter integration:

```
OpenWebUI inlet filter
├─▶ Retraction detection → POST /retract
├─▶ POST /extract (preflight) → GLiNER2 entity typing
├─▶ LLM triple rewrite → POST /ingest
│     ├─▶ WGM Validation Gate → ontology + conflict check → Fact Classification
│     │     ├─▶ Class A → facts table (immediate)
│     │     ├─▶ Class B → staged_facts (promoted at confirmed_count ≥ 3)
│     │     └─▶ Class C → staged_facts (expires 30 days)
│     └─▶ re_embedder (background) → Qdrant upsert
└─▶ POST /query (synchronous)
      ├─▶ PostgreSQL baseline facts
      ├─▶ Graph traversal (1-hop connectivity)
      ├─▶ Hierarchy expansion (SQL CTE, max_depth=3)
      └─▶ Qdrant cosine search → merge → score → inject
```

PostgreSQL is authoritative. Qdrant is a derived vector index for semantic search.

## Tech Stack

- **Language:** Python 3.11+
- **Framework:** FastAPI (uvicorn)
- **Database:** PostgreSQL (psycopg2)
- **Vector Store:** Qdrant (httpx REST API)
- **LLM:** Configurable (Qwen, Ollama, OpenAI-compatible)
- **Entity Extraction:** GLiNER2
- **Embedding:** nomic-embed-text-v1.5
- **Containerization:** Docker, Docker Compose

## Quick Start

### Prerequisites

- Docker & Docker Compose
- PostgreSQL 16+
- Qdrant
- LLM endpoint (Ollama, LM Studio, or OpenAI-compatible API)

### Installation

```bash
git clone https://github.com/your-org/FaultLine.git
cd FaultLine

# Copy and configure environment
cp .env.example .env
# Edit .env with your settings

# Start with Docker Compose
docker compose up -d
```

The API will be available at `http://localhost:8001`.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_DSN` | *required* | PostgreSQL connection string |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant server URL |
| `QDRANT_COLLECTION` | `faultline-test` | Default Qdrant collection name |
| `QWEN_API_URL` | `http://localhost:11434/v1/chat/completions` | LLM API endpoint |
| `WGM_LLM_MODEL` | `qwen/qwen3.5-9b` | Model for novel type validation |
| `CATEGORY_LLM_MODEL` | `qwen2.5-coder` | Model for category inference |
| `FAULTLINE_API_URL` | `http://localhost:8001` | API URL (used by OpenWebUI filter) |
| `REEMBED_INTERVAL` | `10` | Re-embedder poll interval (seconds) |
| `HTTPX_TIMEOUT` | `10` | Timeout for LLM/Qdrant HTTP calls (seconds) |
| `DB_TIMEOUT` | `30` | Timeout for PostgreSQL queries (seconds) |
| `QDRANT_TIMEOUT` | `10` | Timeout for Qdrant operations (seconds) |
| `DB_POOL_SIZE` | `10` | PostgreSQL connection pool size |
| `RATE_LIMIT_PER_MIN` | `100` | Max requests per user per minute |
| `LOG_LEVEL` | `INFO` | Logging level |

## API Endpoints

### `GET /health`

Health check with dependency status. Returns JSON:

```json
{
  "status": "ok",
  "timestamp": "2026-05-10T12:34:56Z",
  "database": "ok",
  "qdrant": "ok",
  "llm": "ok",
  "re_embedder": {
    "last_run": "2026-05-10T12:34:50Z",
    "facts_synced": 127,
    "facts_promoted": 3,
    "facts_expired": 0,
    "error_count": 0,
    "last_error": null
  },
  "model_loaded": true
}
```

Status values: `ok` (all healthy), `degraded` (some dependencies down), `unhealthy` (database unreachable).

### `POST /ingest`

Ingest facts from text. Accepts edges from LLM extraction.

**Request:**
```json
{
  "text": "I'm married to Alex and we live in Toronto",
  "user_id": "user-uuid",
  "source": "chat",
  "edges": [
    {"subject": "user", "object": "alex", "rel_type": "spouse", "subject_type": "Person", "object_type": "Person"},
    {"subject": "user", "object": "toronto", "rel_type": "lives_in", "subject_type": "Person", "object_type": "Location"}
  ]
}
```

**Response:**
```json
{
  "status": "valid",
  "committed": 2,
  "facts": [...]
}
```

### `POST /query`

Query facts for memory recall. Returns merged results from PostgreSQL (baseline + graph + hierarchy) and Qdrant (vector similarity).

**Request:**
```json
{
  "text": "tell me about my family",
  "user_id": "user-uuid"
}
```

**Response:**
```json
{
  "status": "ok",
  "facts": [{...}],
  "preferred_names": {"alex": "Alex", "toronto": "Toronto"},
  "canonical_identity": "Alex",
  "attributes": {...}
}
```

Falls back to PostgreSQL-only when Qdrant or embedding service is unavailable.

### `POST /retract`

Retract (soft-delete or hard-delete) facts. Behavior controlled by the relationship type's `correction_behavior` setting (supersede, hard_delete, or immutable).

### `POST /store_context`

Store raw text context directly to Qdrant (bypasses WGM gate and PostgreSQL). For unstructured text that doesn't fit the fact model.

### `POST /extract`

Preflight entity extraction using GLiNER2. Returns typed entities before full ingest.

## Production Considerations

### Connection Pooling
Configure `DB_POOL_SIZE` to match your expected concurrency. Default is 10 connections.

### Timeouts
All external service calls have configurable timeouts. Adjust `HTTPX_TIMEOUT`, `DB_TIMEOUT`, and `QDRANT_TIMEOUT` for your environment.

### Rate Limiting
Per-user rate limiting is active by default (100 req/min). Configure via `RATE_LIMIT_PER_MIN`.

### Health Monitoring
Use the `/health` endpoint for monitoring. It returns real-time dependency status with a 5-second cache.

### Qdrant Fallback
If Qdrant or the embedding service is unavailable, `/query` gracefully degrades to PostgreSQL-only results. No queries fail due to Qdrant downtime.

### Per-User Collections
Qdrant collections are named `faultline-{user_id}` for isolation. The default collection (`faultline-test`) is used for anonymous/unauthenticated users.

### Re-Embedder
The background re-embedder syncs unsynced facts, promotes Class B staged facts at confirmed_count ≥ 3, expires stale Class C facts, and resolves name collisions. It runs every `REEMBED_INTERVAL` seconds.

## Deployment

### Docker Compose (Recommended)

```yaml
# docker-compose.yml
services:
  faultline:
    build: .
    ports:
      - "8001:8001"
    environment:
      - POSTGRES_DSN=postgresql://faultline:faultline@postgres:5432/faultline
      - QDRANT_URL=http://qdrant:6333
      - QWEN_API_URL=http://ollama:11434/v1/chat/completions
    depends_on:
      - postgres
      - qdrant

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: faultline
      POSTGRES_PASSWORD: faultline
      POSTGRES_DB: faultline

  qdrant:
    image: qdrant/qdrant:latest
```

### OpenWebUI Integration

FaultLine integrates with OpenWebUI as a **Filter** (`openwebui/faultline_filter.py`). The filter intercepts conversations for fact extraction and memory injection. Deploy the filter file to your OpenWebUI instance's filter directory.

## Development

### Running Tests

```bash
pip install -e ".[test]"
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction
```

### Project Structure

```
FaultLine/
├── src/
│   ├── api/          # FastAPI app and endpoints
│   ├── wgm/          # Validation gate and ontology
│   ├── fact_store/   # PostgreSQL fact storage
│   ├── re_embedder/  # Background Qdrant sync service
│   ├── entity_registry/  # Canonical entity ID management
│   └── schema_oracle/    # Entity resolution
├── migrations/       # PostgreSQL schema migrations
├── openwebui/        # OpenWebUI filter and function
├── tests/            # Test suite
└── docker-compose.yml
```

## License

[License information]
