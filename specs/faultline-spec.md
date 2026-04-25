# FaultLine Spec

**Generated from**: `faultline-design.md`  
**Goal**: Build the WGM Validation Gate service with deterministic GliNER extraction, classification-only Schema Oracle, and strict write policy enforcement against a PostgreSQL fact store.

## Architecture

Microservice ingesting text -> GliNER (CPU) -> Context Packager -> Schema Oracle (Qwen2.5 Coder) -> WGM Validation Gate -> PostgreSQL Fact Store. Qdrant remains read-only derived view updated solely by Re-embedder service.

**Tech Stack**: Python 3.11, psycopg2-binary, gliener, httpx, pytest-mock, structlog.

---

## Task 1: GliNER Deterministic Extraction Service

**Files**: `src/gli_ner/extractor.py`, `tests/gli_ner/test_extractor.py`

- [ ] Write failing test
- [ ] Verify it fails (`pytest tests/gli_ner/test_extractor.py::test_extract_entities_success -v`)
- [ ] Write minimal implementation
- [ ] Verify it passes
- [ ] Commit: `git add src/gli_ner/extractor.py tests/gli_ner/test_extractor.py && git commit -m "feat: GliNER deterministic extraction stub"`

---

## Task 2: Classification-Only Schema Oracle Service

**Files**: `src/schema_oracle/oracle.py`, `tests/schema_oracle/test_oracle.py`

- [ ] Write failing test
- [ ] Verify it fails (`pytest tests/schema_oracle/test_oracle.py::test_oracle_classify_edge -v`)
- [ ] Write minimal implementation
- [ ] Verify it passes
- [ ] Commit: `git add src/schema_oracle/oracle.py tests/schema_oracle/test_oracle.py && git commit -m "feat: Classification-only Schema Oracle stub"`

---

## Task 3: WGM Validation Gate Service

**Files**: `src/wgm/gate.py`, `tests/wgm/test_gate.py`

- [ ] Write failing test
- [ ] Verify it fails (`pytest tests/wgm/test_gate.py::test_gate_validate_edge_novel_type -v`)
- [ ] Write minimal implementation
- [ ] Verify it passes
- [ ] Commit: `git add src/wgm/gate.py tests/wgm/test_gate.py && git commit -m "feat: WGM Validation Gate with novel/conflict detection"`

---

## Task 4: Fact Store Persistence Service

**Files**: `src/fact_store/store.py`, `tests/fact_store/test_store.py`

- [ ] Write failing test
- [ ] Verify it fails (`pytest tests/fact_store/test_store.py::test_commit_valid_entities -v`)
- [ ] Write minimal implementation
- [ ] Verify it passes
- [ ] Commit: `git add src/fact_store/store.py tests/fact_store/test_store.py && git commit -m "feat: Fact Store persistence with transaction rollback"`
