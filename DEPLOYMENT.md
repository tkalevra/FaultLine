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
# {"status":"ok","database":"ok","qdrant":"not_configured","llm":"ok",...}
```

## Services & ports

What is published where (host ports are overridable, see `.env.example` "STACK NAMING"):

| Service | Published on the host | Purpose |
|---|---|---|
| `faultline-mcp` | **`0.0.0.0:8002`** — the only network-facing port | MCP server, the live integration path (every FaultLine tool) |
| `faultline` | `127.0.0.1:8000` | Backend API + operator console (`/`), `/health` |
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

- Use external volumes for PostgreSQL data persistence.
- **No vector store is needed.** User memory lives in PostgreSQL; the stack has no Qdrant
  service and `QDRANT_URL` is optional (see "Upgrading from a stack that ran Qdrant" below).
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

## Another FaultLine stack on this host

Compose names a project after its directory, so a second checkout (a language branch, an old
clone) starts a second Postgres/Redis set unless told otherwise. `quickstart.py` detects a
FaultLine stack or `*_postgres_data` volume that belongs to another project and asks:

- **Reuse**: writes `COMPOSE_PROJECT_NAME=<that project>` to `.env`, so this checkout runs the
  same containers and data. A database created for another language keeps its collation; the
  startup guard refuses a mismatch.
- **Replace**: prints the commands to remove the old stack first. The wizard never runs them:
  `cd <old checkout> && docker compose down` keeps its data volumes, `docker compose down -v`
  deletes them (or `docker compose -p <project> down [-v]` without the old checkout).
- **Side by side**: its own `COMPOSE_PROJECT_NAME`, `FAULTLINE_PREFIX`, `FAULTLINE_IMAGE` and
  free host ports.

Find what is there with `docker ps -a --filter name=faultline` and
`docker volume ls | grep postgres_data`.

## Upgrading from a stack that ran Qdrant

Earlier releases shipped a `qdrant` service. It held no user memory (Postgres is the system of
record and the user-memory vector lane is off by default), so it was removed (#219). After you
pull this release:

```bash
docker compose up -d --remove-orphans     # stops and removes the old faultline-qdrant container
docker volume ls | grep qdrant_data       # the old volume is left in place
docker volume rm <project>_qdrant_data    # optional: reclaim its disk once you are sure
```

To keep using an existing Qdrant for the legacy `USER_MEMORY_VECTOR_LANE=true`, run it yourself
on the `faultline-net` network and set `QDRANT_URL` explicitly in `.env`, for example
`QDRANT_URL=http://faultline-qdrant:6333`. With `QDRANT_URL` unset, FaultLine makes no Qdrant
calls and `/health` reports `"qdrant": "not_configured"` with an `ok` status.

## Troubleshooting

```bash
docker compose logs faultline
docker compose restart faultline     # the operator token survives this (stored hashed)
docker compose exec postgres psql -U faultline -d faultline -c "SELECT 1"
```

## Disk full

Symptoms: Postgres logs `PANIC: could not write to file ...: No space left on device` (often
on the shutdown checkpoint) and `database system was not properly shut down`; Redis on older
stacks logs `Error trying to save the DB, can't exit`; `docker compose stop` hangs.

Since #210/#211/#212 the stack defends itself: container logs rotate (10m x 3 per service),
Redis keeps nothing on disk, and below 10% free `/health` reads `degraded` with a console
banner, below 3% new memories are refused with HTTP 507 while recall keeps working. Thresholds:
`FAULTLINE_DISK_DEGRADE_PCT`, `FAULTLINE_DISK_REFUSE_PCT`, `FAULTLINE_DISK_PATHS`.

### 1. Diagnose: what is using the space

```bash
df -h /var/lib/docker                       # the disk Docker and its volumes live on
docker system df                            # images, containers, volumes, build cache
docker system df -v | grep -E 'postgres_data|VOLUME NAME'
# per-container json logs (the usual culprit on stacks without log rotation)
sudo sh -c 'du -h /var/lib/docker/containers/*/*-json.log | sort -h | tail'
# map a log file back to its container
docker ps -a --format '{{.ID}} {{.Names}}' | grep faultline
# Postgres data size from inside, when it is up
docker compose exec postgres psql -U faultline -d faultline \
  -c "SELECT pg_size_pretty(pg_database_size(current_database()))"
```

### 2. Free space safely

Safe, nothing of yours is lost:

```bash
docker builder prune -f                     # build cache, often several GB after rebuilds
docker image prune -f                       # dangling images
# truncate oversized container logs IN PLACE (never rm a log Docker has open)
sudo sh -c 'truncate -s 0 /var/lib/docker/containers/<id>/<id>-json.log'
```

Then apply log rotation to every container: it only takes effect when a container is
re-created, so after you have space run `docker compose up -d --force-recreate` once.

Never delete files inside the Postgres volume (`pg_wal`, `base/`, ...) to make room: that
corrupts the database. `docker compose down -v` and `docker volume rm *_postgres_data` delete
all stored memory.

### 3. Restart in order

With space free again, start Postgres FIRST and let it recover. It replays its WAL from the
last checkpoint, which repairs the unclean shutdown on its own:

```bash
docker compose stop                         # if anything is still half-running
docker compose up -d postgres
docker compose logs -f postgres             # wait for: database system is ready to accept connections
docker compose up -d redis
docker compose up -d faultline              # backend: migrations, models (~2 min first boot)
docker compose up -d faultline-mcp
```

If Postgres keeps failing with `No space left on device`, there is still not enough room for
the WAL replay: free more, then start it again. If a Redis container from an older release
refuses to stop, `docker compose kill redis` is safe: it holds no data FaultLine needs.

### 4. Verify

```bash
docker compose ps                           # every service Up (healthy)
curl -s http://localhost:8000/health        # status ok, "disk": {"status": "ok", ...}, "ingest_writes": "accepted"
docker compose logs --tail 50 faultline     # no errors after startup
docker inspect -f '{{.HostConfig.LogConfig}}' faultline-api   # {json-file map[max-file:3 max-size:10m]}
```

Then recall something you stored before the outage to confirm the data is intact.
