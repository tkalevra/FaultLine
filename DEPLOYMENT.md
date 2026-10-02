# FaultLine Deployment Guide

For the full, annotated deployment walkthrough see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).
This is the quick start.

## Prerequisites

- Docker & Docker Compose v2.24+
- About 4 GB of RAM free for the stack (it uses ~3 GB after boot); 8 GB recommended
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

What is published where (host ports are overridable, see `.env.example` "STACK NAMING"):

| Service | Published on the host | Purpose |
|---|---|---|
| `faultline-mcp` | **`0.0.0.0:8002`** — the only network-facing port | MCP server, the live integration path (every FaultLine tool) |
| `faultline` | `127.0.0.1:8000` | Backend API + operator console (`/`), `/health` |
| `qdrant` | `127.0.0.1:6333` | Qdrant — present in the stack; retired for user memory (holds no user facts) |
| `postgres` | not published (`postgres:5432` on `faultline-net`) | PostgreSQL — authoritative fact storage (per-tenant schemas) |
| `redis` | not published | Coordination / dedup cache |
| `ollama` (profile) | `127.0.0.1:11434` | Optional bundled LLM |

The **MCP server on `:8002`** is the production integration path. The OpenWebUI
Filter in `openwebui/` is intentionally disabled and is not the live path.

## First login and seats

- Operator console: `http://localhost:8000/` (loopback only; `ssh -L 8000:localhost:8000 <host>`).
- Operator token: printed once on first boot,
  `docker compose logs faultline | grep -A1 FAULTLINE_ADMIN_TOKEN`; it survives restarts.
  Replace it with `docker compose exec faultline python -m src.api.operator_token --rotate`.
- FOSS = one instance, up to **5 seats** (`FOSS_MAX_SEATS`, a source constant). Mint a seat in
  the console; the seat token (shown once) is the client's Bearer on `:8002` and is the identity.
- The shared `MCP_API_KEY` admits any user id only until the first seat exists. After that, every
  user id needs a seat, including each OpenWebUI user (seat them on the console's OpenWebUI tab).
  Rotating the MCP key in the console supersedes the `.env` value.
- The backend secret protects `:8000`; it is auto-generated (see Production notes).

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
  network-accessible — leaving it blank is dev-only). Rotating the key in the operator
  console supersedes it: from then on only the rotated key is accepted and the `.env` value
  is refused.
- **The backend `:8000` is not a network service.** It trusts the `user_id` its caller
  sends (the MCP server authenticates people and resolves the tenant), so
  `docker-compose.yml` publishes it on `127.0.0.1` only. The operator console
  (`http://localhost:8000/`) and `curl localhost:8000/health` work on the host; from
  another machine use `ssh -L 8000:localhost:8000 <host>`. Do not change the binding to
  `0.0.0.0` or put `:8000` behind a public proxy.
- Defence in depth: every backend API request must carry `X-FaultLine-Backend-Secret`.
  Exempt: `/health`, `/api/dashboard/*`, the console files and the operator bearer. No
  source address is trusted, loopback included. The MCP server, the re-embedder and the
  backend's own self-calls send it automatically. With `FAULTLINE_BACKEND_SECRET` unset, the
  backend generates the secret on first boot and stores it in the shared database
  (`public.backend_service_secret`), so the MCP service needs `POSTGRES_DSN`, which every
  compose file sets. Set the variable to pin a value of your own. If neither resolves, the
  API refuses (fail closed). The MCP seat gate also fails closed (503) when the seat store
  is unreachable.
- Every `/admin/*` and operator `/internal/*` route requires the operator bearer
  `FAULTLINE_ADMIN_TOKEN`. If unset, the backend generates it on its first boot, stores only
  its hash in the database and prints it once (`docker compose logs faultline | grep -A1
  FAULTLINE_ADMIN_TOKEN`). It survives restarts. Lost it? `docker compose exec faultline
  python -m src.api.operator_token --rotate` prints a replacement.
- Tune `DB_POOL_SIZE` to expected concurrency; set `FAULTLINE_LOG_LEVEL=INFO`.
- Monitor `/health` for dependency status.

## Troubleshooting

```bash
docker compose logs faultline
docker compose restart faultline     # the operator token survives this (stored hashed)
docker compose exec postgres psql -U faultline -d faultline -c "SELECT 1"
```
