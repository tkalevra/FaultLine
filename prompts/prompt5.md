# Step 5: Integration with Classification Flow
**Source Spec**: `storage/projects/FaultLine/specs/faultline-spec.md` (Task 3)  
**Previous Prompt**: `FaultLine/prompts/prompt4.md`
## Objective
Integrate the entity resolution layer into the main classification flow to enable end-to-end entity classification with duplicate detection. The schema oracle should now classify AND resolve entities in a single pass. Supports only classification mode.
## File Locations
- Source spec: `storage/projects/FaultLine/specs/faultline-spec.md`
- Test file: `documents/FaultLine/tests/schema_oracle/test_oracle.py` (will extend)
- Implementation: `documents/FaultLine/src/schema_oracle/oracle.py` (extend)
## Commands (FileShed)
All commands use FileShed. Files live in `documents/`.
### 1. Read current test file to extend tests
```bash
zone=documents
path=prompts/prompt5.md
read zone=path
```
### 2. Extend test file with integration tests (TDD Step 7 → 8)
Add new test cases for integrated classification + resolution:
```python
def test_oracle_classify_and_resolve(mock_model):
    """Test full classification and resolution flow"""
    mock_model.query_classification.return_value = {
        "classification": {"entity_type": "Organization", "confidence": 0.95}
    }
    
    context = {
        "known_types": ["Person", "Organization"],
        "registry": {}
    }
    
    query_input = {
        "entities": [
            {"entity": "Apple Inc.", "type": "Organization"},
            {"entity": "Tech Corp Ltd", "type": "Organization"}
        ]
    }
    
    result = classify(query_input, mock_model, context)
    
    assert "classification" in result
    assert "resolution" in result
    assert len(result["resolution"]["resolved"]) == 2
    
    # Verify canonical IDs assigned
    assert result["resolution"]["resolved"][0]["canonical_id"] == "organization-0"
    assert result["resolution"]["resolved"][1]["canonical_id"] == "organization-1"


def test_oracle_classify_with_duplicates(mock_model):
    """Test that duplicate entities are detected during classification"""
    mock_model.query_classification.return_value = {
        "classification": {"entity_type": "Organization", "confidence": 0.9}
    }
    
    context = {
        "known_types": ["Person", "Organization"],
        "registry": {}
    }
    
    query_input = {
        "entities": [
            {"entity": "Acme Corp", "type": "Organization"},
            {"entity": "ACME CORP INC", "type": "Organization"}  # duplicate variant
        ]
    }
    
    result = classify(query_input, mock_model, context)
    
    # Verify duplicates detected
    assert result["resolution"]["duplicates_found"] == 1
    
    # Verify canonical registry populated
    assert len(result["resolution"]["canonical_registry"]) == 1


def test_oracle_novel_type_rejected(mock_model):
    """Test that novel entity types are rejected"""
    mock_model.query_classification.return_value = {
        "classification": {"entity_type": "Location", "confidence": 0.95}
    }
    
    context = {
        "known_types": ["Person", "Organization"],  # Location not in known types
        "registry": {}
    }
    
    query_input = {
        "entities": [
            {"entity": "New York City", "type": "Location"}
        ]
    }
    
    # Should raise ValueError for novel type
    with pytest.raises(ValueError) as excinfo:
        classify(query_input, mock_model, context)
    
    assert "novel/conflicting" in str(excinfo.value).lower()
```
### 3. Implement integrated flow in oracle.py
Extend `oracle.py` with integration of classification and resolution:
- Add optional resolution parameter to `classify()` function
- When resolution=True, call resolve_entities() after validation
- Maintain backward compatibility with existing classify() behavior
- Ensure registry persists across calls via context parameter

```python
def classify(
    query_input: dict, 
    model=None, 
    context: dict = None,
    enable_resolution: bool = False  # NEW PARAMETER
) -> dict:
    """
    Classify entities against known schema types.
    
    Args:
        query_input: Dictionary with "entities" list where each entity is
                     {"entity": str, "type": str}
        model: Qwen2.5 Coder model instance for classification queries
        context: Dictionary containing {"known_types": [list of allowed types]}
        enable_resolution: If True, also perform duplicate detection and canonicalization
        
    Returns:
        If enable_resolution=False (default):
            {"classification": {"entity_type": str, "confidence": float}}
        
        If enable_resolution=True:
            {
                "classification": {},  # Empty per spec - classification-only mode
                "resolution": {
                    "resolved": [...],
                    "duplicates_found": int,
                    "canonical_registry": {...}
                }
            }
            
    Raises:
        ValueError: If novel/conflicting entity type detected not in schema
    """
    service = ClassificationService(model=model)

    if context is None:
        context = {"known_types": []}

    known_types = set(context.get("known_types", []))

    # Validate each entity has a type that's not novel/conflicting
    for entity in query_input.get("entities", []):
        entity_name = entity.get("entity", "")
        entity_type = entity.get("type", "")

        # Check if type is novel or conflicting
        if entity_type not in known_types:
            raise ValueError(
                f"Novel/conflicting entity type detected: {entity_type}. "
                f"Known types: {known_types}"
            )

    # Call classification prompt (no generation)
    response = service.model.query_classification(query_input, context)

    if enable_resolution:
        from .oracle import resolve_entities
        return resolve_entities(query_input, model, context)
    
    return {
        "classification": {
            "entity_type": response.get("entity_type", ""),
            "confidence": float(response.get("confidence", 0.0))
        }
    }
```

### 4. Verify tests pass (TDD Step 9)
Run pytest on extended test suite:
```bash
zone=documents
path=FaultLine/tests/schema_oracle/test_oracle.py
exec(zone, f"pytest {path} -v", ["--no-redirect"])
```
### 5. Commit to documents repo
Copy files from storage → documents and commit:
```bash
src=storage/projects/FaultLine/src/schema_oracle/oracle.py
dest=documents/FaultLine/src/schema_oracle/oracle.py
copy(src, dest, overwrite=True)
commit zone=documents message="feat: Integration with classification flow"
```
## Expected Outputs
1. **Tests extended**: New integration tests validating classification+resolution behavior
2. **Implementation completed**: Modified `classify()` function with enable_resolution parameter
3. **Tests passing**: All test cases green in pytest output (8 base + 3 new = 11 total)
4. **Git commit completed** to documents repo with message: `"feat: Integration with classification flow"`
## Notes for Agentic Workers
- Maintain backward compatibility: classify() works with/without resolution enabled
- Classification-only mode remains the default (enable_resolution=False)
- Registry state must persist across resolutions when passed via context
- Novel type validation happens BEFORE resolution (security check)
- No external dependencies beyond qwen2.5-coder library
- If CLI fails, use shed_read/shed_patch_text for file operations
