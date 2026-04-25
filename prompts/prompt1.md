# Step 1: GliNER Deterministic Extraction Service

**Source Spec**: `storage/projects/FaultLine/specs/faultline-spec.md` (Task 1)

## Objective
Write minimal failing test, verify failure, write stub implementation, verify pass.

## File Locations

- Source spec: `storage/projects/FaultLine/specs/faultline-spec.md`
- Test file: `storage/projects/FaultLine/tests/gli_ner/test_extractor.py`
- Implementation: `storage/projects/FaultLine/src/gli_ner/extractor.py`

## Commands (FileShed)

All commands below use FileShed. Files live in `storage/`.

### 1. Create test file with failing assertion

```bash
zone=storage
path=projects/FaultLine/tests/gli_ner/test_extractor.py

cat > $path << 'EOF'
import pytest
from unittest.mock import patch, MagicMock

@pytest.fixture
def mock_gliener():
    with patch('src.gli_ner.extractor.gliener') as mock_gli:
        model_mock = MagicMock()
        mock_gli.GliNERModel.from_pretrained.return_value = model_mock
        yield mock_gli

def test_extract_entities_success(mock_gliener, capsys):
    from src.gli_ner import extractor
    
    text = "The system failed."
    entities = [{"entity": "system", "label": "Entity"}, {"entity": "failed", "label": "Event"}]
    model_mock.predict.return_value = entities
    
    result = extractor.extract(text, mock_gliener.GliNERModel)
    
    assert False, "not yet implemented"

def test_extract_entities_invalid_model(mock_gliener):
    from src.gli_ner import extractor
    
    text = "Test text."
    extractor.extract(text, None)
EOF
```

### 2. Verify test fails (TDD Step 1 → 2)

```bash
zone=storage
path=projects/FaultLine/tests/gli_ner/test_extractor.py

# Execute pytest
exec(zone, f"pytest tests/gli_ner/test_extractor.py::test_extract_entities_success -v", ["--no-redirect"])
```

### 3. Create minimal implementation stub

```bash
zone=storage
path=projects/FaultLine/src/gli_ner/extractor.py

cat > $path << 'EOF'
import gliener

class ExtractionService:
    def __init__(self, model=None):
        self.model = model or gliener.GliNERModel.from_pretrained("Urchade/GLiNER")
    
    def extract(self, texts: list[str], top_n: int = 5) -> list[dict]:
        raise NotImplementedError("Extraction logic pending TDD")
EOF
```

### 4. Verify test passes (TDD Step 3 → 4)

```bash
zone=storage
path=projects/FaultLine/src/gli_ner/extractor.py

# Execute pytest again
exec(zone, f"pytest tests/gli_ner/test_extractor.py -v", ["--no-redirect"])
```

### 5. Commit via FileShed → Documents (TDD Step 5)

```bash
src=storage/projects/FaultLine/src/gli_ner/extractor.py
dest=documents/projects/FaultLine/src/gli_ner/extractor.py

# Copy implementation
copy(src, dest, overwrite=True)

src=storage/projects/FaultLine/tests/gli_ner/test_extractor.py
dest=documents/projects/FaultLine/tests/gli_ner/test_extractor.py

# Copy test
copy(src, dest, overwrite=True)

# Commit to documents repo
exec(zone=documents, cmd="git add src/gli_ner/extractor.py tests/gli_ner/test_extractor.py && git commit -m 'feat: GliNER deterministic extraction stub'", ["--no-redirect"])
```

## Expected Outputs

1. **Test file created**: `storage/projects/FaultLine/tests/gli_ner/test_extractor.py`
2. **Failure verified**: pytest output shows 1 FAILED test
3. **Implementation created**: `storage/projects/FaultLine/src/gli_ner/extractor.py` (stub only)
4. **Pass verified**: pytest output shows 2 PASSED tests
5. **Git commit completed** to documents repo with message: `"feat: GliNER deterministic extraction stub"`

---

## Notes for Agentic Workers

- Use FileShed `shed_exec`, `shed_read`, `shed_create_file` for all operations.
- Files in `storage/` are raw; use `documents/` as version-controlled workspace.
- Do NOT run real GliNER model during testing—mock it fully.
- If CLI fails, read file via `shed_read`, edit via `shed_patch_text`, write via `shed_patch_text`.
