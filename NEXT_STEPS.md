# FaultLine WGM — Next Steps

**Status as of 2026-04-25** | All 38 unit + integration tests passing.

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

---

## Remaining Work

### 1. Stub modules in `tests/evaluation`, `tests/feature_extraction`, etc.

The following test files were migrated from `src/tests/` but reference modules that don't
exist yet in this project (`evaluation`, `feature_extraction`, `model_inference`,
`preprocessing`). They are skipped from the default test run.

```
tests/evaluation/test_evaluate.py       → needs src/evaluation/__init__.py
tests/feature_extraction/test_feature_extract.py → needs src/feature_extraction/__init__.py
tests/model_inference/test_inference.py → needs src/model_inference/__init__.py
tests/preprocessing/test_preprocess.py  → needs src/preprocessing/__init__.py
```

These are out-of-scope for the current WGM gate milestone. Decide whether to keep,
implement, or remove them before running `pytest tests/` without `--ignore` flags.

### 2. GliNER Real Model Integration

`src/gli_ner/extractor.py` always requires a model to be injected. For production use,
add a factory that loads the real GliNER model from HuggingFace:

```python
# Option: add to src/gli_ner/extractor.py
def load_default_model():
    from gliner import GLiNER
    return GLiNER.from_pretrained("urchade/gliner_medium-v2.1")
```

Note: The original code used `import gliener` (typo); the real package is `gliner`
and its class is `GLiNER`, not `GliNERModel`.

### 3. WGM Ontology Configuration

`SEED_ONTOLOGY` in `src/wgm/gate.py` is hardcoded. For production, load it from:
- A config file (YAML/JSON), or
- A PostgreSQL `ontology_types` table at startup.

### 4. Schema Oracle Endpoint Configuration

`invoke_oracle` reads `QWEN_API_URL` from env (defaults to `localhost:11434`).
Ensure this is set in deployment config and `.env` for local dev.

### 5. Structured Logging

`structlog` is installed but not wired into any component. Add structured log calls at
key pipeline boundaries (extraction, classification, validation, commit) to support
observability in production.

### 6. PostgreSQL Schema Migration

No SQL migration files exist. Create:
```sql
-- migrations/001_create_facts.sql
CREATE TABLE IF NOT EXISTS facts (
    id SERIAL PRIMARY KEY,
    subject_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    provenance TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 7. CI/CD

No `.github/workflows/` or equivalent. Add a minimal GitHub Actions workflow:
```yaml
# .github/workflows/test.yml
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

## Test Summary (current)

```
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction
              --ignore=tests/model_inference --ignore=tests/preprocessing

38 passed in 0.21s
```
