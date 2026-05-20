![FaultLine](docs/faultline_logo.svg)
> Every AI memory system trusts the LLM to write correctly. FaultLine doesn't.
> Backed by [arxiv 2603.15994](https://arxiv.org/abs/2603.15994).

# FaultLine

Write-validated knowledge graph pipeline for OpenWebUI. Extracts facts from conversations, validates them against your ontology, stores them in PostgreSQL, and injects them back for context-aware responses.

**In plain terms:** A fact-checking system that remembers what users tell you, prevents contradictions, and uses those facts to give better responses.

---

## Quick Start (5 minutes)

### Prerequisites
- Docker and Docker Compose
- ~1GB available memory for containers

### 1. Start the backend

```bash
docker-compose up -d
```

This starts:
- FaultLine backend (port 8001)
- PostgreSQL database
- Qdrant vector search
- Redis cache

### 2. Verify it's running

```bash
curl http://localhost:8001/health
```

Expected: `{"status":"ok"}`

### 3. Connect to OpenWebUI

**In OpenWebUI Settings:**
1. Go to Settings > Functions
2. Create new Filter from `openwebui/faultline_tool.py`
3. Set valve: `FAULTLINE_URL` to `http://faultline:8001`
4. Enable the filter and save

### 4. Test it

```
User:   My name is Chris, I work as a systems analyst
System: [stores fact]

User:   What's my job?
System: You're a systems analyst.
```

---

## How It Works

**Pipeline:**

1. **Extract** - LLM identifies facts (names, relationships, attributes)
2. **Validate** - WGM gate checks against ontology, prevents contradictions
3. **Store** - Facts route to PostgreSQL as Class A/B/C based on confidence
4. **Recall** - Next turn, relevant facts injected as conversation context

**Result:** Accurate context that's always available, never stale, never contradictory.

---

## Configuration

### Required Environment Variables

```bash
POSTGRES_DSN=postgresql://faultline:faultline@postgres:5432/faultline
QDRANT_URL=http://qdrant:6333
OPENWEBUI_URL=http://open-webui:8080
```

### Optional Settings

```bash
REEMBED_INTERVAL=60              # Background re-embedding frequency (seconds)
RATE_LIMIT_PER_MIN=100           # API rate limit
DB_POOL_SIZE=15                  # PostgreSQL connection pool size
EMBEDDING_CACHE_TTL=86400        # Cache duration (seconds)
```

See `.env.example` for complete reference.

---

## Documentation

- **[About](ABOUT.md)** - Design principles and philosophy
- **[Architecture](docs/ARCHITECTURE.md)** - System design and data model
- **[Deployment](DEPLOYMENT.md)** - Production configuration
- **[Changelog](CHANGELOG.md)** - Version history

---

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Service status |
| `/ingest` | POST | Store extracted facts |
| `/query` | POST | Retrieve relevant facts |
| `/retract` | POST | Remove/correct facts |

---

## Fact Storage

Facts route to three storage types automatically:

| Type | Use Case | Example |
|------|----------|---------|
| **Scalar** | Single values | age=42, name="Chris" |
| **Relational** | Relationships between entities | spouse, parent_of, works_for |
| **Hierarchical** | Classification and taxonomy | instance_of, subclass_of |

Routing is **metadata-driven**: each relation type has built-in rules determining where and how it's stored.

---

## Testing

Run the test suite:

```bash
pytest tests/ --ignore=tests/evaluation --ignore=tests/preprocessing
```

Run integration test with a real OpenWebUI instance:

```bash
bash /tmp/TESTS/comprehensive_family_pipeline_test.sh
```

---

## Key Features

- **Write-validated facts** - Every fact passes ontology gates before storage
- **Per-user isolation** - Memory is never shared across users
- **Staged promotion** - Facts move from ephemeral → behavioral → permanent based on confirmation
- **Semantic conflict detection** - Auto-resolves contradictions
- **Metadata-driven** - New relation types self-describe their constraints
- **OpenWebUI native** - Uses official inlet filters, no hacks

---

## Production Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for production setup with Portainer, SSL, and scaling guidance.

---

## Support

- Issues: See `BUGS/` directory
- Architecture questions: Read `docs/ARCHITECTURE.md`
- Configuration help: Check `.env.example`

---

## License

MIT License - See LICENSE file for details

---

## What's New

See [CHANGELOG.md](CHANGELOG.md) for recent updates and features.
