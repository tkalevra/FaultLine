# FaultLine — Persistent Memory for OpenWebUI and Self-Hosted LLMs

> **Not the band. Not the game engine. Not the geology term.**  
> FaultLine is an open-source, self-hosted **persistent memory layer** for [OpenWebUI](https://openwebui.com/) and local LLMs.

FaultLine gives your local AI assistant long-term memory. It intercepts every conversation, extracts named entities and relationships, validates them against a write gate, and stores them in PostgreSQL as a personal knowledge graph. When you return to a conversation, relevant facts are injected before the model responds — so it already knows who you are, who matters to you, and what you've told it before.

**No cloud. No subscriptions. No vendor lock-in. Runs entirely on your hardware.**

---

## What It Does

| Without FaultLine | With FaultLine |
|---|---|
| LLM forgets everything between sessions | LLM remembers your name, family, preferences, history |
| You repeat yourself every conversation | Facts accumulate and strengthen over time |
| Model hallucinates personal details | All stored facts pass a validation gate before storage |
| One-size-fits-all memory | Per-user isolated knowledge graphs |

**Example:**

> You: *"My daughter Gabby just started high school."*  
> *(FaultLine stores: user → parent_of → Gabby, Gabby → instance_of → Person)*

> Three weeks later...  
> You: *"What do you know about my family?"*  
> LLM: *"You have a daughter named Gabby who recently started high school..."*

---

## How It Compares

| Capability | FaultLine | ChatGPT Memory | MemGPT / Letta | Mem0 | OpenWebUI Native RAG |
|---|---|---|---|---|---|
| Self-hosted / fully private | ✅ | ❌ Cloud only | ✅ | ✅ | ✅ |
| Works with any LLM | ✅ | ❌ OpenAI only | ✅ | ✅ | ✅ |
| MCP native server | ✅ | ❌ | ❌ | Partial | ❌ |
| Structured knowledge graph | ✅ PostgreSQL | ❌ Opaque | Partial | Partial | ❌ Vector only |
| Write-validated ontology | ✅ WGM gate | ❌ | ❌ | ❌ | ❌ |
| Fact confidence classes (A/B/C) | ✅ | ❌ | ❌ | Partial | ❌ |
| Non-destructive correction | ✅ Soft-archive | ❌ Overwrites | ❌ | ❌ | ❌ |
| Graph traversal (relationships) | ✅ 1-hop + hierarchy | ❌ | ❌ | ❌ | ❌ |
| Self-building ontology | ✅ Novel rel_types → staged → approved | ❌ | ❌ | ❌ | ❌ |
| Per-user schema isolation | ✅ PostgreSQL schema | ✅ Account-level | ✅ | ✅ | ❌ Shared index |
| Prompt injection protection | ✅ Input validation + framing | ❌ | ❌ | ❌ | ❌ |
| Open source | ✅ Apache 2.0 | ❌ | ✅ MIT | Partial (core closed) | ✅ MIT |
| Dead-naming prevention | ✅ Preferred name flags | ❌ | ❌ | ❌ | ❌ |

FaultLine's differentiation is write-time validation — most memory systems are append-only RAG stores with no ontology enforcement. FaultLine knows the difference between `parent_of` and `child_of`, validates both directions, and never stores a UUID where a display name should appear.

---

## Requirements

- Docker & Docker Compose
- [OpenWebUI](https://openwebui.com/) (v0.9.5+)
- A local LLM endpoint — [Ollama](https://ollama.ai/) or [LM Studio](https://lmstudio.ai/) with a Qwen2.5 model
- 8 GB RAM minimum (16 GB recommended — GLiNER2 model loads at startup)

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/tkalevra/FaultLine.git
cd FaultLine

# 2. Configure your LLM endpoint
cp .env.example .env
# Edit .env — set QWEN_API_URL to your Ollama/LM Studio endpoint

# 3. Start the stack
docker compose -f config/docker-compose.yml up -d

# 4. Verify
curl http://localhost:8000/health
# → {"status": "ok", ...}
```

> First build downloads the GLiNER2 model weights (~500 MB). Allow 3–5 minutes.  
> Full setup guide: **[docs/Docker.md](./docs/Docker.md)**

---

## Connecting to OpenWebUI

1. In OpenWebUI → **Workspace → Functions → +**
2. Paste the contents of `openwebui/faultline_function.py`
3. Save, then open **Valves** and set `FAULTLINE_URL`:
   - Same machine: `http://localhost:8000`
   - Docker-internal: `http://faultline:8000`
4. Enable the filter. Start a conversation — FaultLine begins learning.

---

## MCP Server (Claude Desktop + OpenWebUI Tools)

FaultLine includes a Model Context Protocol server exposing three tools:

- `recall_memory` — query the knowledge graph
- `remember_facts` — store new facts from conversation
- `retract_fact` — remove or correct a stored fact

The MCP server runs on port `8002` and is included in the Docker Compose stack automatically.

### OpenWebUI (OpenAPI connection)

OpenWebUI connects via its **OpenAPI** connection type. In **Settings → Connections → Tools → Add Connection**:

| Field | Value |
|-------|-------|
| Type | `OpenAPI` |
| URL | `http://faultline-mcp:8002` |
| Auth | `Bearer <MCP_API_KEY>` |
| OpenAPI Spec | `URL` → `openapi.json` (default) |

OpenWebUI discovers the tools automatically from `/openapi.json`.

### Claude Desktop (MCP native)

```json
{
  "mcpServers": {
    "faultline": {
      "url": "http://YOUR-HOST:8002/mcp",
      "headers": { "Authorization": "Bearer YOUR_MCP_API_KEY" }
    }
  }
}
```

---

## Architecture

```
OpenWebUI Conversation
  │
  ▼
FaultLine Filter (OpenWebUI inlet)
  ├─ Intent Classification  →  QUERY / STATEMENT / CORRECTION / RETRACTION
  ├─ POST /query            →  inject relevant facts before LLM responds
  └─ POST /ingest           →  extract + validate + store new facts

FaultLine Backend (port 8000)
  ├─ GLiNER2               →  zero-shot named entity + relation extraction
  ├─ WGM Validation Gate   →  ontology check, conflict detection, type constraints
  ├─ Three-Class Storage   →  Class A (user-stated) / B (LLM-inferred) / C (speculative)
  └─ Re-Embedder           →  background: promote facts, sync Qdrant, evolve ontology

Storage
  ├─ PostgreSQL            →  authoritative fact store (per-user schemas)
  └─ Qdrant                →  semantic vector index (derived from PostgreSQL)
```

**Key principle:** The LLM extracts facts; the backend validates and stores them. The LLM never has direct write access to the knowledge graph.

---

## Environment Variables

```env
# Required
POSTGRES_DSN=postgresql://faultline:faultline@postgres:5432/faultline
QWEN_API_URL=http://host.docker.internal:11434/v1/chat/completions

# Optional (defaults shown)
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test
REEMBED_INTERVAL=60
MCP_API_KEY=                    # set to require auth on MCP server
FAULTLINE_USER_ID=              # pin MCP server to a single user
```

---

## Key Files

| File | Role |
|---|---|
| `openwebui/faultline_function.py` | OpenWebUI Filter — inlet classification, fact injection |
| `src/api/main.py` | FastAPI backend — `/ingest`, `/query`, `/retract` endpoints |
| `src/wgm/gate.py` | Write-Validated Memory gate — ontology + conflict detection |
| `src/re_embedder/embedder.py` | Background worker — fact promotion, Qdrant sync, ontology growth |
| `src/mcp/http_server.py` | MCP HTTP server — Claude Desktop integration |
| `config/docker-compose.yml` | Full stack (faultline + postgres + qdrant + redis + mcp) |

---

## Testing

```bash
# Unit tests
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing

# Live integration test (requires running stack)
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"text": "My name is Chris", "user_id": "test-user", "edges": [], "source": "test"}'
```

---

## Documentation

| Document | Contents |
|---|---|
| [docs/Docker.md](./docs/Docker.md) | Step-by-step Docker deployment guide |
| [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) | OpenWebUI filter setup + valve configuration |
| [DEV/CONTAINER-ARCHITECTURE.md](./DEV/CONTAINER-ARCHITECTURE.md) | Container topology, networking, data flow |
| [docs/RESEARCH-AND-INNOVATION.md](./docs/RESEARCH-AND-INNOVATION.md) | Research foundations, peer-reviewed references, comparison to prior work |

---

## Built With

- **[PostgreSQL](https://www.postgresql.org/)** — Authoritative fact storage
- **[Qdrant](https://qdrant.tech/)** — Vector similarity search
- **[Redis](https://redis.io/)** — Rate limiting and caching
- **[FastAPI](https://fastapi.tiangolo.com/)** + **[Uvicorn](https://www.uvicorn.org/)** — API backend
- **[GLiNER2](https://github.com/urchade/gliner)** — Zero-shot named entity and relation extraction
- **[nomic-embed-text-v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)** — Semantic embeddings
- **[OpenWebUI](https://openwebui.com/)** — Self-hosted LLM interface

---

## License

Apache License 2.0 — see [docs/LICENSE](./docs/LICENSE).

## Contributing

Contributions welcome. Please ensure:
- All tests pass (`pytest tests/`)
- New relationship types are metadata-driven (added to `rel_types` table, not hardcoded)
- No UUIDs in LLM output (filter at injection time)
- No hardcoded validation constants (use `_get_rel_type_metadata()`)
