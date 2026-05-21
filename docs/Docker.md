# FaultLine — Docker Deployment Guide

This guide walks through every step to build, run, verify, and manage the
FaultLine WGM stack using Docker Compose.

The stack contains three containers:

| Container | Image | Port |
|-----------|-------|------|
| `faultline` | local build | `8000` |
| `postgres` | postgres:16-alpine | internal only |
| `qdrant` | qdrant/qdrant | `6334` (host) → `6333` (internal) |

---

## Prerequisites

- **Docker Desktop** 4.x or later (includes Compose v2)
  - Windows: [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
  - Verify: `docker compose version` → should show `v2.x`
- **Git** (to clone the repo)
- **A running Qwen2.5 Coder endpoint** — LM Studio or Ollama on your host machine
  - LM Studio default: `http://localhost:1234/v1/chat/completions`
  - Ollama default: `http://localhost:11434/v1/chat/completions`

> **First-time build warning**: the image downloads the GliNER model weights
> (~500 MB) from HuggingFace. On a fast connection this takes 3–5 minutes.
> Subsequent builds use the Docker layer cache and are near-instant.

---

## Step 1 — Clone and enter the repo

```bash
git clone https://github.com/tkalevra/FaultLine.git
cd FaultLine
```

---

## Step 2 — Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and set the two values:

```env
# Point this at your local Qwen2.5 Coder server.
# Use host.docker.internal instead of localhost so the container can reach the host.
QWEN_API_URL=http://host.docker.internal:11434/v1/chat/completions

# Only used when connecting directly (not via Compose). Compose sets its own DSN internally.
POSTGRES_DSN=postgresql://faultline:faultline@localhost:5432/faultline_test
```

> **Linux users**: `host.docker.internal` is not available by default on Linux Docker.
> Use your host machine's LAN IP instead (e.g. `http://192.168.1.x:11434/...`),
> or add `extra_hosts: ["host.docker.internal:host-gateway"]` to the `faultline`
> service in `docker-compose.yml`.

---

## Step 3 — Build the image

```bash
docker compose build
```

What happens during the build:
1. Installs Python dependencies into `/install`
2. Downloads `urchade/gliner_medium-v2.1` from HuggingFace into the image cache
3. Assembles a slim runtime image (~1.2 GB total)

To watch download progress:

```bash
docker compose build --progress=plain
```

---

## Step 4 — Start the stack

```bash
docker compose up -d
```

Startup order (enforced by `depends_on`):
1. `postgres` starts and passes its healthcheck (`pg_isready`)
2. `qdrant` starts
3. `faultline` starts, runs the SQL migration, then launches uvicorn

Check that all three containers are running:

```bash
docker compose ps
```

Expected output:

```
NAME                    STATUS          PORTS
faultline-faultline-1   running         0.0.0.0:8000->8000/tcp
faultline-postgres-1    running (healthy)
faultline-qdrant-1      running         0.0.0.0:6334->6333/tcp
```

---

## Step 5 — Verify the service is healthy

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"ok": true}
```

If `curl` is not available on Windows, use PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

---

## Step 6 — Send a test ingest request

Extract entities only (no edges — nothing committed to the DB):

```bash
curl -s -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"text": "Alice works for Acme Corp.", "source": "test"}' | python -m json.tool
```

Expected response:

```json
{
  "status": "extracted",
  "committed": 0,
  "entities": [
    {"entity": "Alice",     "label": "Person",       "canonical_id": "person-0"},
    {"entity": "Acme Corp", "label": "Organization", "canonical_id": "organization-0"}
  ],
  "facts": []
}
```

Commit an explicit edge:

```bash
curl -s -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Alice works for Acme Corp.",
    "source": "test",
    "edges": [
      {"subject": "Alice", "object": "Acme Corp", "rel_type": "WORKS_FOR"}
    ]
  }' | python -m json.tool
```

Expected response:

```json
{
  "status": "valid",
  "committed": 1,
  "entities": [...],
  "facts": [
    {"subject": "Alice", "object": "Acme Corp", "rel_type": "WORKS_FOR", "status": "valid"}
  ]
}
```

---

## Step 7 — Wire up the OpenWebUI tool

1. In the OpenWebUI admin panel, go to **Workspace → Tools → Add Tool**.
2. Paste the contents of `openwebui/faultline_tool.py`.
3. Set the environment variable `FAULTLINE_URL` in OpenWebUI to point at the
   running FaultLine service:
   - If OpenWebUI is on the **same machine**: `http://localhost:8000`
   - If OpenWebUI is on a **different host**: `http://<faultline-host-ip>:8000`
4. Enable the tool for the model you want to use it with.

The model can then call:

```
store_fact(
  text="Alice is the CEO of Acme Corp.",
  edges=[{"subject": "Alice", "object": "Acme Corp", "rel_type": "CREATED_BY"}]
)
```

---

## Management commands

### View live logs

```bash
# All services
docker compose logs -f

# FaultLine service only
docker compose logs -f faultline
```

### Stop the stack (preserves data volumes)

```bash
docker compose down
```

### Stop and wipe all test data

```bash
docker compose down -v
```

> This deletes `pg-test-data` and `qdrant-test-data` volumes.
> Use this to reset to a clean state.

### Restart a single service

```bash
docker compose restart faultline
```

### Rebuild after code changes

```bash
docker compose build faultline
docker compose up -d faultline
```

### Open a psql session against the test DB

```bash
docker compose exec postgres psql -U faultline -d faultline_test
```

Useful queries:

```sql
-- See committed facts
SELECT * FROM facts ORDER BY created_at DESC LIMIT 20;

-- See novel types queued for review
SELECT * FROM pending_types ORDER BY flagged_at DESC;
```

### Check the Qdrant test collection

Qdrant's REST API is exposed on port `6334`:

```bash
curl http://localhost:6334/collections
```

---

## Troubleshooting

### `faultline` container exits immediately

Check the logs:

```bash
docker compose logs faultline
```

Common causes:
- **`POSTGRES_DSN` not set`** — confirm `.env` exists and `QWEN_API_URL` is set
- **Migration failed** — run `docker compose exec postgres psql -U faultline -d faultline_test` and check for schema errors
- **Port 8000 already in use** — change the host port in `docker-compose.yml` (`"8001:8000"`)

### `curl: (7) Failed to connect to localhost port 8000`

The container may still be starting (migration + uvicorn init takes ~10 s).
Wait a few seconds and retry. Check status with `docker compose ps`.

### `host.docker.internal` does not resolve (Linux)

Add the following to the `faultline` service in `docker-compose.yml`:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Then `docker compose up -d --force-recreate faultline`.

### GliNER model download fails during build

The build requires internet access to reach HuggingFace. If you are behind a
proxy, pass build args:

```bash
docker compose build \
  --build-arg HTTP_PROXY=http://proxy:3128 \
  --build-arg HTTPS_PROXY=http://proxy:3128
```

### Edge committed with status `novel`

The `rel_type` you passed is not in `SEED_ONTOLOGY`. Current known types:
`IS_A`, `PART_OF`, `KILLS`, `CREATED_BY`, `WORKS_FOR`, `test_type`.

To add a type, edit `src/wgm/gate.py` → `SEED_ONTOLOGY`, rebuild, and redeploy.
