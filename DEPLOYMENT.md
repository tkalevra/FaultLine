# FaultLine Deployment Guide

For the full, annotated deployment walkthrough see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).
This is the quick start.

## Prerequisites

- Docker & Docker Compose v2+
- ~4 GB RAM (8 GB recommended)
- An LLM backend you already run (Ollama, LM Studio, OpenWebUI, or an
  OpenAI-compatible API)

## Quick start

```bash
git clone https://github.com/tkalevra/FaultLine.git
cd FaultLine

cp .env.example .env
# Set LLM_BACKEND_TYPE + LLM_BASE_URL to point at the LLM you already run.

docker compose up -d --build

# Verify the backend
curl http://localhost:8000/health
# {"status":"ok","database":"ok","qdrant":"ok","llm":"ok"}
```

## Services & ports

| Service | Port | Purpose |
|---|---|---|
| `faultline` | **8000** | Backend API — `/ingest`, `/query`, `/health` |
| `faultline-mcp` | **8002** | MCP server — `recall_memory`, `remember_facts`, `learn_facts`, `retract_fact` (the live integration path) |
| `postgres` | 5432 | PostgreSQL — authoritative fact storage (per-tenant schemas) |
| `qdrant` | 6333 | Qdrant — Class-C short-term vector index |

The **MCP server on `:8002`** is the production integration path. The OpenWebUI
Filter in `openwebui/` is intentionally disabled and is not the live path.

## Configuration

See [`docs/ENV-REFERENCE.md`](docs/ENV-REFERENCE.md) for the variable summary and
[`.env.example`](.env.example) for the full annotated list. The three you must set:

| Variable | Purpose |
|---|---|
| `POSTGRES_DSN` | PostgreSQL connection |
| `LLM_BACKEND_TYPE` | LLM protocol (`ollama` / `lm_studio` / `openwebui` / `openai` / …) |
| `LLM_BASE_URL` | Host + port of your LLM (no path) |

## Production notes

- Use external volumes for PostgreSQL and Qdrant data persistence.
- Set `MCP_API_KEY` to a secret token (the MCP HTTP transport on `:8002` is
  network-accessible — leaving it blank is dev-only).
- Tune `DB_POOL_SIZE` to expected concurrency; set `FAULTLINE_LOG_LEVEL=INFO`.
- Monitor `/health` for dependency status.

## Troubleshooting

```bash
docker compose logs faultline
docker compose restart faultline
docker compose exec postgres psql -U faultline -d faultline -c "SELECT 1"
```
