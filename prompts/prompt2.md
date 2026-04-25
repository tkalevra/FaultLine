# Step 2: GliNER Extraction Logic Implementation

**Source Spec**: `storage/projects/FaultLine/specs/faultline-spec.md` (Task 2)

## Objective
Implement the actual extraction logic for GliNER, handling both single and batch text inputs, with proper error handling and result formatting.

## File Locations

- Source spec: `storage/projects/FaultLine/specs/faultline-spec.md`
- Test file: `documents/tests/gli_ner/test_extractor.py` (will update tests)
- Implementation: `documents/src/gli_ner/extractor.py`

## Commands (FileShed)

All commands use FileShed. Files live in `documents/`.

### 1. Read current test file to update tests

```bash
zone=documents
path=prompts/prompt2.md

# Read existing tests to understand what needs updating
read zone=path
```

### 2. Update test file with proper assertions (TDD Step 5 → 6)

Replace the failing assertion with proper validation:

```python
def test_extract_entities_success(mock_gliener):
    from src.gli_ner import extractor
    
    text = "The system failed."
    entities = [{"entity": "system", "label": "Entity"}, {"entity": "failed", "label": "Event"}]
    model_mock.predict.return_value = entities
    
    result = extractor.extract(text, mock_gliener.GliNERModel)
    
    # Assert correct structure and content
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["entity"] == "system"
    assert result[1]["entity"] == "failed"
```

### 3. Implement extraction logic in extractor.py

Update `extract` method to:
- Accept both single strings and lists of strings
- Iterate over texts and call model.predict() for each
- Handle the GliNER response format properly
- Return unified list of entities with entity_name, entity_type, score fields

```python
def extract(texts: str | list[str], model=None, top_n: int = 5) -> list[dict]:
    service = ExtractionService(model=model)
    
    results = []
    if isinstance(texts, str):
        texts = [texts]
    
    for text in texts:
        predictions = service.model.predict(text, top_k=top_n)
        
        # GliNER returns dict with entities and scores
        for pred in predictions:
            results.append({
                "entity": pred.get("entity"),
                "type": pred.get("label"),
                "score": pred.get("score", 0.0),
                "text": text
            })
    
    return results
```

### 4. Verify tests pass (TDD Step 7)

Run pytest on updated test suite:

```bash
zone=documents
cmd="pytest tests/gli_ner/test_extractor.py -v"
exec(zone, cmd, ["--no-redirect"])
```

### 5. Commit to documents repo

Copy files from storage → documents and commit:

```bash
src=storage/projects/FaultLine/src/gli_ner/extractor.py
dest=documents/src/gli_ner/extractor.py
copy(src, dest, overwrite=True)

src=storage/projects/FaultLine/tests/gli_ner/test_extractor.py
dest=documents/tests/gli_ner/test_extractor.py
copy(src, dest, overwrite=True)

commit zone=documents message="feat: implement GliNER extraction logic"
```

## Expected Outputs

1. **Tests updated**: Proper assertions validating result structure and content
2. **Implementation completed**: Full extraction logic with batch support
3. **Tests passing**: All test cases green in pytest output
4. **Git commit completed** to documents repo with message: `"feat: implement GliNER extraction logic"`

## Notes for Agentic Workers

- Implement actual GliNER model prediction calls (not mocked)
- Handle both string and list[str] inputs gracefully
- Return normalized entity format with score inclusion
- If CLI fails, use shed_read/shed_patch_text for file operations
- No external dependencies beyond gliener library