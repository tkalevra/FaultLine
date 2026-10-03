# FaultLine Environment Reference

This is a quick reference for the environment variables FaultLine reads. The
authoritative, fully-commented source is [`.env.example`](../.env.example) — copy
it to `.env` and edit. Every value here has a plain-English description there.

> Ports are canonical: **backend `:8000`**, **MCP server `:8002`**. PostgreSQL is
> `:5432`. There is no vector store (Qdrant is optional and not shipped).

## Required

| Variable | Example | Purpose |
|---|---|---|
| `POSTGRES_DSN` | `postgresql://faultline:faultline@postgres:5432/faultline` | PostgreSQL connection — the authoritative store for all facts, entities, and per-tenant schemas. |
| `LLM_BACKEND_TYPE` | `ollama` | Selects the LLM protocol: `openwebui` \| `ollama` \| `lm_studio` \| `openai` \| `anthropic` \| `groq` \| `localai` \| `raw`. |
| `LLM_BASE_URL` | `http://host.docker.internal:11434` | Host + port of the LLM you already run (no path — the API path is appended per backend type). |

## LLM

| Variable | Default | Purpose |
|---|---|---|
| `LLM_API_KEY` | *(empty)* | Bearer token for hosted backends. Leave blank for local servers. |
| `WGM_LLM_MODEL` | `qwen/qwen3.5-9b` | Model name as it appears on your backend (used for extraction/validation). |
| `LLM_TIMEOUT_<OP>` | per-op defaults | Override per-operation timeouts (e.g. `LLM_TIMEOUT_EXTRACTION`). Never hardcoded. |
| `LLM_MAX_TOKENS_<OP>` | per-op defaults | Override per-operation token budgets (e.g. `LLM_MAX_TOKENS_EXTRACT`). |

## Storage

| Variable | Default | Purpose |
|---|---|---|
| `QDRANT_URL` | *(empty)* | Optional. Empty = no vector store, the default: all user facts (A/B/C) live in PostgreSQL and no Qdrant call is made. Set it only to keep an existing Qdrant for the legacy `USER_MEMORY_VECTOR_LANE=true`. |
| `QDRANT_COLLECTION` | `faultline-test` | Legacy lane only (with `QDRANT_URL` set). Kept at its historical value so an existing Qdrant keeps its collection. |
| `SHORT_TERM_MEMORY` | `true` | When extraction yields no structured triples, stash the text as a Class C context fact. |
| `REEMBED_INTERVAL` | `60` | Re-embedder poll interval (seconds) — promotion, expiry. |

## Provisioning (per-tenant isolation)

| Variable | Default | Purpose |
|---|---|---|
| `PROVISIONING_POLL_INTERVAL` | `5` | How often the background worker checks for new tenants to provision. |
| `PROVISIONING_BATCH_SIZE` | `10` | Tenant schemas created per batch. |
| `SCHEMA_NAME_PREFIX` | `faultline` | Per-tenant schema name prefix: `{PREFIX}_{user_slug}`. |

## MCP server

| Variable | Default | Purpose |
|---|---|---|
| `MCP_API_KEY` | *(empty)* | Bearer token enforced on the MCP HTTP transport (`:8002`). Empty = open (dev only). |
| `FAULTLINE_USER_ID` | *(empty)* | Single-user fallback for the MCP server. Omit in multi-user deployments — the per-request `X-OpenWebUI-User-Id` header is authoritative. |
| `FAULTLINE_API_URL` | `http://faultline:8000` | Where the MCP sidecar reaches the backend. |

## Operational

| Variable | Default | Purpose |
|---|---|---|
| `DB_POOL_SIZE` | `10` | Connection-pool size — tune to expected concurrency. |
| `RATE_LIMIT_PER_MIN` | `100` | Per-user request ceiling. |
| `FAULTLINE_LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL`. |
| `FAULTLINE_LOG_MAX_SIZE` / `FAULTLINE_LOG_MAX_FILE` | `10m` / `3` | Docker log rotation applied to every compose service (json-file driver). Takes effect on container re-create. |
| `FAULTLINE_DISK_PATHS` | `/` | Paths whose free space the backend watches (comma-separated). The tightest one decides. |
| `FAULTLINE_DISK_DEGRADE_PCT` | `10` | Below this % free: `/health` status `degraded`, console banner. |
| `FAULTLINE_DISK_REFUSE_PCT` | `3` | Below this % free: ingest writes refused with HTTP 507; recall keeps working. |
| `HTTPX_TIMEOUT` / `DB_TIMEOUT` / `QDRANT_TIMEOUT` | `30` / `30` / `10` | Transport timeouts (seconds). |

See [`.env.example`](../.env.example) for the complete annotated list, including
the per-operation LLM timeout/token-budget overrides and circuit-breaker tuning.
