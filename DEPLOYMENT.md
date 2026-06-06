# FaultLine Deployment Guide

## Prerequisites

- Docker & Docker Compose v2+
- 4GB RAM recommended
- PostgreSQL data volume (~1GB)
- Qdrant data volume (~500MB)
- LLM endpoint (Ollama, LM Studio, or OpenAI-compatible API)

## Quick Start

```bash
git clone https://github.com/tkalevra/FaultLine.git
cd FaultLine

# Copy and edit environment
cp .env.example .env
# Edit .env with your LLM endpoint and any overrides

# Build and start all services
docker compose up -d --build

# Verify health
curl http://localhost:8000/health
```

Expected response: `{"status":"ok","database":"ok","qdrant":"ok","llm":"ok"}`

## Services

| Service | Port | Purpose |
|---------|------|---------|
| faultline | 8000 | FastAPI API — `/ingest`, `/query`, `/health` |
| faultline-mcp | 8002 | MCP server — recall/store/retract/learn for MCP clients |
| postgres | 5432 | PostgreSQL — fact storage |
| qdrant | 6333 | Qdrant — vector index |
| redis | 6379 | Redis — inlet dedup cache |

## Configuration

See `.env.example` for the full list. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BACKEND_TYPE` | `ollama` | LLM protocol: `openwebui`/`ollama`/`lm_studio`/`openai`/`anthropic`/… |
| `LLM_BASE_URL` | `http://host.docker.internal:11434` | LLM host + port (path appended automatically) |
| `LLM_API_KEY` | _(blank)_ | Bearer/API key for the LLM (blank for local servers) |
| `POSTGRES_DSN` | Set in compose | PostgreSQL connection |
| `QDRANT_URL` | `http://qdrant:6333` | Vector store |
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
