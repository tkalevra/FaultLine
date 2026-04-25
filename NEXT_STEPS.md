# FaultLine WGM — Next Steps

**Status as of 2026-04-25** | All 38 unit + integration tests passing. Initial commit pushed to GitHub (private).

---

## Completed

| # | Item | Status |
|---|------|--------|
| 0 | Delete `FaultLine/FaultLine/` nested duplicate files | ✅ Done |
| 0 | Consolidate `src/tests/` → `tests/` | ✅ Done |
| 0 | Remove duplicate code blocks from all src modules | ✅ Done |
| 0 | Fix `conftest.py` location and `import pytest` | ✅ Done |
| 1 | Align `extractor.py` ↔ `__init__.py` interface (`"label"` key) | ✅ Done |
| 1 | Wire `extract_entities()` through `ExtractionService` | ✅ Done |
| 2 | Implement `invoke_oracle` with httpx POST to Qwen2.5 | ✅ Done |
| 2 | Implement `parse_oracle_response` | ✅ Done |
| 3 | Create `src/wgm/gate.py` — `WGMValidationGate` state machine | ✅ Done |
| 4 | Create `src/fact_store/store.py` — `FactStoreManager` transaction + rollback | ✅ Done |
| 5 | Context Packager schema validation tests | ✅ Done |
| 6 | `pyproject.toml` full dependencies + pytest config | ✅ Done |
| 6 | `.env.example` | ✅ Done |
| 7 | `tests/test_pipeline.py` end-to-end integration test | ✅ Done |
| 8 | `ABOUT.md`, `LICENSE` (Apache 2.0), `.gitignore` | ✅ Done |
| 9 | Private GitHub repo `tkalevra/FaultLine` created and pushed | ✅ Done |

---

## Phase 2 — Docker + OpenWebUI Integration

**Goal**: Expose the WGM pipeline as a containerised HTTP service and wire it into
OpenWebUI as a Tool function, using a dedicated test Qdrant collection so no
production data is touched during development.

### Step 1 — FastAPI service layer

**New files**: `src/api/main.py`, `src/api/models.py`

Expose the 5-stage pipeline behind a single endpoint:

```
POST /ingest          { "text": str, "source": str }
  → extract_entities
  → build_audit_context
  → resolve_entities (classify)
  → validate_edge (WGM gate)
  → commit (Fact Store)
  ← { "status": "valid"|"novel"|"conflict", "committed": int, "facts": [...] }

GET  /health          → { "ok": true }
```

- Use `fastapi` + `uvicorn`
- All pipeline dependencies injected at startup (GliNER model, DB pool, oracle URL)
- Pydantic request/response models in `models.py`
- Tests in `tests/api/test_ingest.py` using `httpx.AsyncClient` + `TestClient`

### Step 2 — PostgreSQL migration

**New file**: `migrations/001_create_facts.sql`

```sql
CREATE TABLE IF NOT EXISTS facts (
    id          SERIAL PRIMARY KEY,
    subject_id  TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    rel_type    TEXT NOT NULL,
    provenance  TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_facts_pair
    ON facts (subject_id, object_id);
```

Run at container startup via entrypoint script.

### Step 3 — GliNER real model factory

**File**: `src/gli_ner/extractor.py`

Add a startup factory so the container loads the real model once:

```python
def load_default_model():
    from gliner import GLiNER
    return GLiNER.from_pretrained("urchade/gliner_medium-v2.1")
```

The API layer calls this once on startup and injects the instance into `ExtractionService`.
Unit tests continue to inject mocks — no change needed there.

### Step 4 — Dockerfile

**New file**: `Dockerfile`

Multi-stage build:
- Stage 1 (`builder`): install deps into a venv
- Stage 2 (`runtime`): slim Python 3.11 image, copy venv + src, expose port 8000

```
ENTRYPOINT: run migrations → start uvicorn src.api.main:app
```

### Step 5 — Docker Compose (test stack)

**New file**: `docker-compose.yml`

Three services, all on an isolated `faultline-test` network:

| Service | Image | Purpose |
|---------|-------|---------|
| `faultline` | local build | FastAPI WGM service |
| `postgres` | postgres:16-alpine | Fact store (test DB) |
| `qdrant` | qdrant/qdrant | Test-only vector store (separate from production) |

Named volumes: `pg-test-data`, `qdrant-test-data` — easy to nuke with `docker compose down -v`.

Env vars passed through from `.env`:
```
QWEN_API_URL, POSTGRES_DSN, QDRANT_URL, QDRANT_COLLECTION=faultline-test
```

### Step 6 — OpenWebUI Tool function

**New file**: `openwebui/faultline_tool.py`

A single Python file dropped into OpenWebUI's Tools directory. OpenWebUI calls it when
the model decides to store a memory/fact.

```python
"""
Tool name:   FaultLine WGM
Description: Store validated facts through the FaultLine WGM pipeline.
"""
import httpx

async def store_fact(text: str, source: str = "openwebui") -> str:
    """Store a fact via FaultLine. Returns validation status."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "http://faultline:8000/ingest",
            json={"text": text, "source": source},
            timeout=15.0,
        )
    r.raise_for_status()
    data = r.json()
    return f"[FaultLine] status={data['status']} committed={data['committed']}"
```

The tool is **additive** — OpenWebUI's existing memory agent is untouched. The model
calls `store_fact` explicitly when it wants a fact validated and persisted.

### Step 7 — Structured logging

Wire `structlog` into key pipeline boundaries in `src/api/main.py`:
- Request received (text length, source)
- Extraction complete (entity count)
- Validation result (status per edge)
- Commit complete (row count)

### Step 8 — CI/CD

**New file**: `.github/workflows/test.yml`

```yaml
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[test]"
      - run: pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction
                           --ignore=tests/model_inference --ignore=tests/preprocessing
```

---

## Backlog (post Phase 2)

| Item | Notes |
|------|-------|
| WGM ontology from config file / DB table | `SEED_ONTOLOGY` is hardcoded in `gate.py` |
| Qdrant re-embedder service | Re-embed `facts` table into `qdrant-test` collection after each commit |
| OpenWebUI memory agent modification | Deeper integration once Tool path is validated |
| Stub modules (`evaluation`, `feature_extraction`, etc.) | Out-of-scope artifacts; implement or remove |

---

## Test Summary (current)

```
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction
              --ignore=tests/model_inference --ignore=tests/preprocessing

38 passed in 0.21s
```
