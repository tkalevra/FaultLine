# FaultLine

**WGM Validation Gate** for [faultline-rag](https://github.com/tkalevra/faultline-rag) — a strict
TDD-based pipeline for deterministic entity extraction, schema classification, edge validation,
and PostgreSQL persistence.

## Architecture

```
text
  └─▶ GliNER (CPU)          entity extraction, deterministic
         └─▶ Context Packager    bundles entities + source span
                └─▶ Schema Oracle (Qwen2.5 Coder)   classification-only, no generation
                       └─▶ WGM Validation Gate       novel / conflict / valid state machine
                              └─▶ PostgreSQL Fact Store   strict write policy
```

Qdrant remains a read-only derived view, updated only by a separate Re-embedder service.

## Components

| Module | Path | Role |
|--------|------|------|
| GliNER Extractor | `src/gli_ner/` | CPU-based NER, returns `{entity, label}` pairs |
| Context Packager | `src/context_packager/` | Wraps entities + source span into audit dict |
| Schema Oracle | `src/schema_oracle/` | Qwen2.5 Coder via httpx, classification-only |
| WGM Gate | `src/wgm/` | Ontology novelty + DB conflict detection |
| Fact Store | `src/fact_store/` | Single-transaction INSERT with rollback |

## Quick Start

```bash
pip install -e ".[test]"
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing
```

## Configuration

Copy `.env.example` to `.env` and set:

```
QWEN_API_URL=http://localhost:11434/v1/chat/completions
POSTGRES_DSN=postgresql://user:pass@localhost:5432/faultline
```

## Tech Stack

- Python 3.11
- [gliner](https://github.com/urchade/GLiNER) — entity extraction
- [httpx](https://www.python-httpx.org/) — async-ready HTTP client for Qwen2.5
- [psycopg2-binary](https://pypi.org/project/psycopg2-binary/) — PostgreSQL driver
- [structlog](https://www.structlog.org/) — structured logging
- [pytest](https://pytest.org/) + [pytest-mock](https://github.com/pytest-dev/pytest-mock)

## License

Copyright 2026 tkalevra

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
