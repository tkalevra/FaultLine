# FaultLine Environment Reference

This is a quick reference for the environment variables FaultLine reads. The
authoritative, fully-commented source is [`.env.example`](../.env.example) — copy
it to `.env` and edit. Every value here has a plain-English description there.

> Ports are canonical: **backend `:8000`**, **MCP server `:8002`**. PostgreSQL is
> `:5432`, Qdrant is `:6333`.

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

## Storage & vector index

| Variable | Default | Purpose |
|---|---|---|
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant endpoint. Qdrant holds the **Class C short-term tier only** (see [ARCHITECTURE.md](ARCHITECTURE.md)). |
| `QDRANT_COLLECTION` | `faultline-test` | Base collection name. Per-user collections are derived automatically (`faultline-{user_id}`). |
| `SHORT_TERM_MEMORY` | `true` | When extraction yields no structured triples, stash the text as a Class C context fact. |
| `REEMBED_INTERVAL` | `60` | Re-embedder poll interval (seconds) — promotion, expiry, Class-C sync. |

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
| `HTTPX_TIMEOUT` / `DB_TIMEOUT` / `QDRANT_TIMEOUT` | `30` / `30` / `10` | Transport timeouts (seconds). |

See [`.env.example`](../.env.example) for the complete annotated list, including
the per-operation LLM timeout/token-budget overrides and circuit-breaker tuning.
