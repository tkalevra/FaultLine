# WGM Validation Gate - faultline-wgm

## Project Overview
WGM (Write-Grammar-Memory) Validation Gate for faultline-rag. Implements a strict TDD-based pipeline:
- **GliNER Extractor**: CPU-based deterministic entity extraction using GliNER model
- **Context Packager**: Packs extracted entities into structured context
- **Schema Oracle**: Qwen2.5 Coder API for edge classification (classification-only)
- **WGM Validation Gate**: Validates edges against ontology before persistence
- **PostgreSQL Fact Store**: Strict write policy enforcement

## Architecture Flow
```
text -> GliNER (CPU) -> Context Packager -> Schema Oracle (Qwen2.5) -> WGM Validation Gate -> PostgreSQL Fact Store
```

**Note**: Qdrant remains read-only and is out of scope for this implementation.

## Tech Stack
- Python 3.11
- psycopg2-binary
- gliner (package name: `gliner`, import alias `gliener` used in extractor.py)
- httpx (for LLM curl simulation)
- pytest / pytest-mock
- structlog

## Directory Structure
```
conftest.py               # Root-level shared fixtures (mock_qwen, mock_db)
src/
├── gli_ner/
│   ├── __init__.py       # extract_entities() stub — raises ValueError on None model
│   └── extractor.py      # ExtractionService class (wraps gliener model)
├── schema_oracle/
│   ├── __init__.py       # Re-exports from oracle.py
│   └── oracle.py         # EntityRegistry, ClassificationService, classify(), resolve_entities(), invoke_oracle()
├── wgm/
│   └── __init__.py       # validate_edge() stub — returns (True, "VALID")
├── fact_store/
│   └── __init__.py       # commit_edge() stub — returns {"id": 1}
└── context_packager/
    └── __init__.py       # build_audit_context() stub — returns {"context": ..., "metadata": ...}

tests/
├── gli_ner/
│   ├── test_extract.py   # TDD stub tests (intentionally failing — assert False)
│   └── test_extractor.py # ExtractionService mock tests (needs extractor.py alignment)
├── schema_oracle/
│   └── test_oracle.py    # EntityRegistry, classify(), resolve_entities() tests (mostly passing)
├── wgm/
│   └── test_gate.py      # TDD stub tests (intentionally failing — assert False)
├── fact_store/
│   └── test_commit.py    # TDD stub tests (partially failing — assert False)
└── context_packager/
    └── test_bridge.py    # TDD stub tests (partially failing — assert False)

src/tests/                # DUPLICATE — consolidate into tests/ (see NEXT_STEPS.md)
plans/
└── faultline-wgm.md      # Original TDD task plan (Tasks 0-5)
specs/
├── faultline-spec.md     # Canonical architecture spec
└── faultline-design.md   # Design document
prompts/                  # FileShed prompt definitions used to generate initial files
FaultLine/                # NESTED DUPLICATE — shed tool artifact, do not develop here
```

## Component Status

| Component | Src File | Status | Tests Pass? |
|---|---|---|---|
| GliNER `__init__` | `src/gli_ner/__init__.py` | Stub (raises on None model) | Partial |
| GliNER Extractor | `src/gli_ner/extractor.py` | Stub (NotImplementedError body) | Broken |
| Context Packager | `src/context_packager/__init__.py` | Stub (returns mock dict) | Partial |
| Schema Oracle | `src/schema_oracle/oracle.py` | Implemented (EntityRegistry, classify) | Yes |
| WGM Gate | `src/wgm/__init__.py` | Stub (returns VALID always) | Partial |
| Fact Store | `src/fact_store/__init__.py` | Stub (returns {"id": 1}) | Partial |

## Development Workflow
1. Follow TDD: write failing test → implement → verify pass → commit
2. Use mock fixtures (`mock_qwen`, `mock_db`) from root `conftest.py`
3. All external dependencies (Qwen2.5, PostgreSQL, GliNER) must be mocked in tests

## Testing
```bash
pytest tests/ -v
```

Task-specific:
```bash
pytest tests/gli_ner/test_extractor.py -v
pytest tests/schema_oracle/test_oracle.py -v
pytest tests/wgm/test_gate.py -v
pytest tests/fact_store/test_commit.py -v
pytest tests/context_packager/test_bridge.py -v
```

## Key Interface Contracts

### `src/gli_ner/__init__.py` — `extract_entities`
```python
extract_entities(text: str, model_class=None) -> list[dict]
# Returns: [{"entity": str, "label": str}, ...]
# Raises: ValueError if model_class is None
```

### `src/gli_ner/extractor.py` — `ExtractionService`
```python
ExtractionService(model=None)
service.extract(texts: list[str], top_n: int = 5) -> list[dict]
# Returns: [{"entity": str, "type": str, "score": float, "text": str}, ...]
# NOTE: uses "type" not "label" — mismatches __init__.py contract
```

### `src/schema_oracle/oracle.py` — `classify`
```python
classify(query_input: dict, model=None, context: dict = None, enable_resolution: bool = False) -> dict
# query_input: {"entities": [{"entity": str, "type": str}]}
# context: {"known_types": [...], "registry": {}}
# Raises: ValueError on novel/unknown entity type
```

### `src/wgm/__init__.py` — `validate_edge`
```python
validate_edge(subject_id, obj_id, rel_type) -> tuple[bool, str]
# Returns: (is_valid, status) where status in {"VALID", "PENDING_REVIEW", "CONFLICT_FLAGGED"}
```

### `src/fact_store/__init__.py` — `commit_edge`
```python
commit_edge(sub: str, obj: str, rel: str, prov: str) -> dict
# Returns: {"id": int} on success
```

## Security Notes
- Secrets via env vars or vault — never hardcoded
- Use mock fixtures to avoid external dependencies in tests
- Validate all inputs at service boundaries
