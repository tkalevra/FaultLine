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
curl http://localhost:8001/health
```

Expected response: `{"status":"ok","database":"ok","qdrant":"ok","llm":"ok"}`

## Services

| Service | Port | Purpose |
|---------|------|---------|
| faultline | 8001 | FastAPI API — `/ingest`, `/query`, `/health` |
| postgres | 5432 | PostgreSQL — fact storage |
| qdrant | 6333 | Qdrant — vector index |

## Configuration

See `.env.example` for the full list. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `QWEN_API_URL` | `http://host.docker.internal:11434/v1/chat/completions` | LLM endpoint |
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
