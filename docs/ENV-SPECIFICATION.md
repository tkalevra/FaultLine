# FaultLine Environment Variable Specification

This document specifies the environment variables FaultLine reads, with the
canonical LLM-backend configuration. For the full annotated list see
[`.env.example`](../.env.example); for deployment walkthroughs see
[`DEPLOYMENT.md`](./DEPLOYMENT.md).

> All example values below are placeholders. Never commit real API keys,
> hostnames, or tokens — keep them in your local `.env` / Portainer secrets.

## The LLM hook (canonical)

FaultLine connects to an LLM you already run; it does not host one. The endpoint
is configured with three variables — `LLM_BACKEND_TYPE` selects the protocol and
the correct API path is appended automatically:

| Variable | Purpose | Example |
|----------|---------|---------|
| `LLM_BACKEND_TYPE` | Protocol: `openwebui` / `ollama` / `lm_studio` / `openai` / `anthropic` / `groq` / `localai` / `raw` | `ollama` |
| `LLM_BASE_URL` | Host + port only, no path | `http://host.docker.internal:11434` |
| `LLM_API_KEY` | Bearer/API key (blank for local servers that need none) | `sk-...` |
| `WGM_LLM_MODEL` | Extraction model — must match a name the backend reports | `qwen2.5` |
| `CATEGORY_LLM_MODEL` | Category-inference model | `qwen2.5` |

Path appended per backend type:

| `LLM_BACKEND_TYPE` | Appended path | Auth header |
|--------------------|---------------|-------------|
| `openwebui` | `/api/chat/completions` | `Authorization: Bearer` |
| `ollama` / `lm_studio` / `openai` / `localai` | `/v1/chat/completions` | `Authorization: Bearer` |
| `groq` | `/openai/v1/chat/completions` | `Authorization: Bearer` |
| `anthropic` | `/v1/messages` | `x-api-key` + `anthropic-version` |
| `raw` | none (full URL is taken from `LLM_BASE_URL`) | `Authorization: Bearer` |

> **Legacy fallback.** If `LLM_BASE_URL` is unset, the backend falls back to the
> older `OPENWEBUI_INTERNAL_URL` → `OPENWEBUI_URL` → `QWEN_API_URL` chain
> (`src/api/llm_client.py:get_endpoint_list`). New deployments should set
> `LLM_BACKEND_TYPE` + `LLM_BASE_URL` and ignore the legacy variables.

## Deployment scenarios

### Scenario 1 — Docker Compose (default)

**File:** [`docker-compose.yml`](../docker-compose.yml)

```env
LLM_BACKEND_TYPE=ollama
LLM_BASE_URL=http://host.docker.internal:11434
LLM_API_KEY=
WGM_LLM_MODEL=qwen2.5
CATEGORY_LLM_MODEL=qwen2.5

POSTGRES_DSN=postgresql://faultline:faultline@postgres:5432/faultline
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test
REDIS_URL=redis://redis:6379/0
REEMBED_INTERVAL=10
DB_POOL_SIZE=10
```

If you don't already run a model, start a bundled one with the `ollama` profile:

```bash
docker compose --profile ollama up -d
```

### Scenario 2 — Portainer Stack

**File:** [`config/docker-compose-portainer.yml`](../config/docker-compose-portainer.yml)

Build and tag the image on the Docker host first (`docker build -t faultline:latest .`),
then set these in Portainer's **Environment variables** tab:

```env
LLM_BACKEND_TYPE=openwebui
LLM_BASE_URL=http://open-webui:8080
LLM_API_KEY=<your-bearer-token>
WGM_LLM_MODEL=<model-name-the-backend-reports>
CATEGORY_LLM_MODEL=<model-name-the-backend-reports>
POSTGRES_PASSWORD=<strong-password>
```

## Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_DSN` | _(required)_ | `postgresql://user:pass@host:5432/db` |
| `QDRANT_URL` | `http://qdrant:6333` | Vector index endpoint |
| `QDRANT_COLLECTION` | `faultline-test` | Collection name |
| `REDIS_URL` | `redis://redis:6379/0` | Inlet dedup cache (optional but recommended) |

## MCP server

The MCP server (`tools/mcp_server.py`, exposed on port 8002 in compose) gives
MCP clients like Claude Desktop access to the same store.

| Variable | Default | Description |
|----------|---------|-------------|
| `FAULTLINE_API_URL` | `http://faultline:8000` | Backend the MCP server proxies to |
| `MCP_API_KEY` | _(blank)_ | Bearer token required on HTTP transport (set one — port 8002 is network-accessible) |
| `FAULTLINE_USER_ID` | _(blank)_ | Pins the MCP server to a single user (Claude Desktop single-user mode) |

## Validation checklist

Before deploying, verify:

- [ ] `LLM_BACKEND_TYPE` matches your backend and `LLM_BASE_URL` is reachable from the container
- [ ] `LLM_API_KEY` is set when the backend requires auth
- [ ] `WGM_LLM_MODEL` matches a model the backend actually serves (mismatch → HTTP 400)
- [ ] `curl http://localhost:8000/health` returns `{"status":"ok",...}`
- [ ] `POSTGRES_DSN` connects and `QDRANT_URL` is reachable
- [ ] `MCP_API_KEY` is set if the MCP server is exposed on the network

## Known issues

### HTTP 400 from the LLM endpoint

Usually a model-name mismatch — `WGM_LLM_MODEL` / `CATEGORY_LLM_MODEL` must match
exactly what the backend reports. Check the available models (e.g.
`curl $LLM_BASE_URL/v1/models`) and confirm `LLM_API_KEY` is valid.
