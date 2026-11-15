# FaultLine Deployment Guide

## Prerequisites

- Docker & Docker Compose v2+
- 4GB RAM recommended
- PostgreSQL data volume (~1GB)
- Qdrant data volume (~500MB)
- LLM endpoint (Ollama, LM Studio, or OpenAI-compatible API)

## Quick Start

For the full, annotated deployment walkthrough see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

```bash
git clone https://github.com/tkalevra/FaultLine.git
cd FaultLine

# Copy and edit environment
cp .env.example .env
# Edit .env with your LLM endpoint and any overrides

# Build and start all services
docker compose up -d --build

# Verify health (backend API on port 8000)
curl http://localhost:8000/health
```

Expected response: `{"status":"ok","database":"ok","qdrant":"ok","llm":"ok"}`

## Services

| Service | Port | Purpose |
|---------|------|---------|
| faultline (backend API) | 8000 | FastAPI — `/ingest`, `/query`, `/health` |
| faultline-mcp | 8002 | MCP server (Streamable HTTP / JSON-RPC) — `/mcp` + REST shorthand (`/recall_memory`, `/remember_facts`, `/ingest_document`, `/learn_facts`, `/retract_fact`, `/forget_fact`) |
| postgres | 5432 | PostgreSQL — authoritative A/B fact storage |
| qdrant | 6333 | Qdrant — Class C short-term vector index only |
| redis | 6379 | Rate limiting + event queues |

> **Note on Portainer deployments:** `docker-compose-portainer-withoutqdrant.yml` maps the backend host port as `8001:8000`. The **container listens on 8000**; 8001 is only a host-side remap on that stack. The MCP server always listens on 8002.

## Configuration

See `.env.example` for the full list. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BACKEND_TYPE` | `ollama` | Backend protocol selector (`openwebui` \| `ollama` \| `lm_studio` \| `openai` \| `anthropic` \| `groq` \| `localai` \| `raw`) |
| `LLM_BASE_URL` | `http://host.docker.internal:11434` | LLM endpoint (host+port only, no path) |
| `POSTGRES_DSN` | Set in compose | PostgreSQL connection |
| `QDRANT_URL` | `http://qdrant:6333` | Vector store (Class C only) |
| `MCP_API_KEY` | (unset = open) | Bearer token on the MCP HTTP transport (port 8002) |
| `FAULTLINE_USER_ID` | (unset) | Single-user dev fallback; MUST be unset in multi-user deploys |
| `REEMBED_INTERVAL` | `10` | Background sync interval (seconds) |
| `RATE_LIMIT_PER_MIN` | `100` | Max requests per user per minute |

## Production Notes

- Use external volumes for PostgreSQL and Qdrant data persistence
- Configure `DB_POOL_SIZE` based on expected concurrency
- Set `LOG_LEVEL=INFO` for production
- Monitor `/health` endpoint for dependency status
- Re-embedder syncs facts every `REEMBED_INTERVAL` seconds

## Troubleshooting

```bash
# View logs
docker compose logs faultline

# Restart a service
docker compose restart faultline

# Check database connectivity
docker compose exec postgres psql -U faultline -d faultline -c "SELECT 1"
```
