![FaultLine Logo](./faultline_logo.svg)

# FaultLine

**A write-validated knowledge graph pipeline for OpenWebUI.** FaultLine intercepts conversations, extracts named entities and relationships, validates them against an ontology, and persists them to PostgreSQL as a personal knowledge base—enabling memory recall during future conversations via Qdrant semantic search.

## Why FaultLine?

Large language models have no persistent memory of your identity, relationships, or personal facts. FaultLine solves this by:
- **Extracting facts** from conversation (using GLiNER2 + LLM)
- **Validating** facts against a metadata-driven ontology (no hardcoded rules)
- **Storing** facts in PostgreSQL with confidence scores
- **Injecting** relevant facts back into your prompts (before the model responds)

Result: Your LLM remembers who you are, who matters to you, and what you've told it—across all conversations.

## Architecture

```
OpenWebUI Inlet Filter
  ├─ Intent Classification (GLiNER2 → QUERY/RETRACTION/CORRECTION/STATEMENT)
  │
  ├─ POST /query (if QUERY intent)
  │  └─ Five-Phase Resolution:
  │     ├─ Phase 1: Anchor → WHO (resolve pronouns → user UUID)
  │     ├─ Phase 2: Path → WHAT (scalar/relationship/taxonomy query)
  │     ├─ Phase 3: DB Facts (PostgreSQL baseline + 1-hop graph + hierarchy)
  │     ├─ Phase 4: Vector Search (Qdrant semantic, threshold 0.3)
  │     └─ Phase 5: Fact Resolution (UUID→display names, dedup, return prose)
  │
  └─ Inject Facts → LLM Context (if facts found)

FaultLine Ingest Pipeline (POST /ingest)
  ├─ Stage 1: Intent Classification
  ├─ Stage 2: LLM Extraction (or GLiNER2 if known rel_types)
  ├─ Stage 3: WGM Validation Gate
  │  ├─ Semantic conflict detection (auto-supersede type/ownership conflicts)
  │  ├─ Bidirectional validation (prevent impossible rel_type pairs)
  │  ├─ Type constraint validation (head_types/tail_types from rel_types table)
  │  └─ Fact Classification (Class A/B/C routing)
  │
  └─ PostgreSQL Commit + Qdrant Sync (re-embedder background)

Three-Dimensional Classification Model
  ├─ DIMENSION 1: Storage Path (SCALAR|RELATIONAL|HIERARCHICAL)
  │  └─ Determined by rel_type metadata (deterministic on create)
  │
  ├─ DIMENSION 2: Confidence Class (A|B|C)
  │  ├─ Class A: User-stated (1.0, always authoritative)
  │  ├─ Class B: LLM-inferred following established ontology (0.8)
  │  └─ Class C: Novel patterns awaiting approval (0.4)
  │
  └─ DIMENSION 3: Directionality (via rel_type metadata)
     ├─ Ontology: is_symmetric + inverse_rel_type (parent_of ↔ child_of)
     └─ Hierarchy: composition chains (instance_of, subclass_of, member_of)
```

## Key Features

- **Metadata-Driven Validation:** Zero hardcoded rel_types or entity types. All validation reads from `rel_types` table at runtime.
- **Write-Time Normalization:** Entity UUIDs are v5 surrogates, normalized at ingest. Query layer works with canonical forms.
- **Non-Destructive Archival:** User corrections soft-delete conflicting facts (preserved for historical queries).
- **Three-Layer Intent Classification:** GLiNER2 + negation patterns + dynamic confidence gating.
- **Self-Building Ontology:** Novel rel_types staged as Class C, promoted by re-embedder via cosine similarity + frequency heuristics.
- **Per-User Collections:** Qdrant indexed as `faultline-{user_id}` for multi-tenant deployments.

## Quick Start

```bash
# Install dependencies
pip install -e ".[test]"

# Run migrations
docker compose up postgres qdrant
python -m alembic upgrade head

# Start backend
uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload

# Start re-embedder (background)
python src/re_embedder/embedder.py

# Deploy to OpenWebUI (via Docker Compose)
docker compose up --build
```

## Environment Variables

```env
POSTGRES_DSN=postgresql://user:pass@localhost:5432/faultline
QWEN_API_URL=http://localhost:11434/v1/chat/completions  # or OpenWebUI endpoint
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test
REEMBED_INTERVAL=10  # seconds
```

## Key Files

| File | Role |
|---|---|
| `src/api/main.py` | FastAPI app — `/ingest`, `/query`, `/retract` endpoints |
| `src/wgm/gate.py` | `WGMValidationGate` — ontology + conflict detection |
| `src/re_embedder/embedder.py` | Background poll loop — promotes Class B, expires Class C, learns ontology |
| `openwebui/faultline_function.py` | OpenWebUI Filter — inlet intent classification, fact injection |
| `src/schema_oracle/oracle.py` | Entity type resolution (GLiNER2 wrapper) |
| `src/entity_registry/registry.py` | UUID v5 surrogates, alias tracking, preferred name resolution |

## Testing

```bash
# Unit tests (exclude evaluation/feature_extraction/model_inference/preprocessing)
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing

# Integration test with OpenWebUI
curl -X POST "https://your-openwebui.com/api/chat/completions" \
  -H "Authorization: Bearer sk-YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "FaultLine-Test",
    "messages": [{"role": "user", "content": "tell me about my family"}]
  }' | jq '.'
```

## Known Issues

- **dBug-016:** OpenWebUI crashes on missing `chat_id`. Workaround applied.
- **Per-User Collections:** Qdrant naming is live (`faultline-{user_id}`). Never break this invariant.

---

## Built With: Open Source Software

FaultLine is built on the shoulders of excellent open-source projects:

### Core Infrastructure
- **[PostgreSQL](https://www.postgresql.org/)** — Authoritative fact storage with ACID guarantees
- **[Qdrant](https://qdrant.tech/)** — Vector database for semantic fact search
- **[Redis](https://redis.io/)** — Rate limiting, caching, event queues
- **[FastAPI](https://fastapi.tiangolo.com/)** — High-performance Python web framework
- **[Uvicorn](https://www.uvicorn.org/)** — ASGI server

### Named Entity & Relation Extraction
- **[GLiNER2](https://github.com/urchade/gliner)** — Zero-shot relation extraction with semantic constraints
- **[nomic-embed-text](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)** — Lightweight semantic embeddings

### Frontend & Integration
- **[OpenWebUI](https://openwebui.com/)** — Self-hosted LLM interface (compatible with any OpenAI-compatible endpoint)
- **[Hugging Face Hub](https://huggingface.co/)** — Model hosting and API

### Development & Testing
- **[pytest](https://pytest.org/)** — Unit testing framework
- **[structlog](https://www.structlog.org/)** — Structured logging
- **[psycopg2](https://www.psycopg.org/)** — PostgreSQL Python driver
- **[httpx](https://www.python-httpx.org/)** — Async HTTP client

---

## Research & Academic References

FaultLine is grounded in peer-reviewed research on knowledge graphs, information extraction, and semantic understanding:

### Named Entity & Relation Extraction
- **GLiNER: Generalist Model for NER and RE** — [arXiv:2311.08526](https://arxiv.org/abs/2311.08526) — Zero-shot NER and relation extraction with semantic constraints; the foundation for FaultLine's entity typing and relationship classification
- **GLiNER 2.0** — [GitHub](https://github.com/urchade/gliner) — Improved zero-shot extraction with confidence scoring
- **Relation Extraction with Self-Supervision** — [arXiv:2305.12278](https://arxiv.org/abs/2305.12278) — Techniques for learning new relation types from minimal supervision

### Knowledge Graphs & Semantic Networks
- **Knowledge Graphs** (Fensel et al., 2020) — [ACM Digital Library](https://dl.acm.org/doi/10.1145/3418294) — Comprehensive survey on KG construction, validation, and querying
- **Wikidata: A Free and Open Knowledge Base** — [arXiv:1407.6552](https://arxiv.org/abs/1407.6552) — Community-curated open knowledge graph; FaultLine's relation types are aligned to Wikidata PIDs (P40, P26, P31, etc.)
- **Knowledge Graph Embedding by Translating on Hyperplanes** — [AAAI-14](https://ojs.aaai.org/index.php/AAAI/article/view/8870) — TransH model for learning semantic relationships

### Information Extraction & Confidence Estimation
- **Deep Learning for Generic Object Detection** — [arXiv:1506.02640](https://arxiv.org/abs/1506.02640) — Foundational deep learning techniques reused in entity extraction
- **Confident Learning: Estimating Uncertainty in Dataset Labels** — [JMLR](https://jmlr.org/papers/v23/21-0889.html) — Techniques for confidence scoring applied to fact classification

### Ontology Learning & Self-Building Systems
- **Ontology Learning from Text** (Maedche & Staab, 2001) — [IEEE TKDE](https://ieeexplore.ieee.org/document/918630) — Foundational work on learning ontologies from conversational text; informs FaultLine's self-building ontology mechanism
- **An Unsupervised Method for Automatic Language Identification** — [Computational Linguistics](https://aclanthology.org/J97-1003/) — Pattern frequency analysis reused in novel rel_type evaluation

### Vector Similarity & Semantic Search
- **Nomic: Powerful text embeddings for your use case** — [Blog](https://www.nomic.ai/blog/nomic-embed-text-v1) — Technical overview of nomic-embed-text-v1.5 embeddings used for Qdrant semantic search
- **Dense Passage Retrieval for Open-Domain Question Answering** — [arXiv:2004.04906](https://arxiv.org/abs/2004.04906) — Dense retrieval techniques applied to fact ranking

### Conflict Detection & Semantic Validation
- **Detecting and Resolving Inconsistencies in Ontologies** — [Semantic Web Journal](http://www.semantic-web-journal.net/) — Techniques for auto-superseding conflicting facts (Dimension 1 of FaultLine's classification model)
- **Bidirectional Relation Validation** — [ACM Transactions on Knowledge Discovery from Data](https://dl.acm.org/journal/tkdd) — Methods for preventing semantically impossible relationship pairs

---

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](./docs/LICENSE) for details.

## Contributing

Contributions welcome. Please ensure:
- All tests pass (`pytest tests/`)
- New rel_types are metadata-driven (added to `rel_types` table, not hardcoded)
- No hardcoded validation constants (use `_get_rel_type_metadata()`)
- No UUIDs in LLM output (filter at injection time)
