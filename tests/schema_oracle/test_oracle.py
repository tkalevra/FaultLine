import pytest
from unittest.mock import Mock, patch
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from schema_oracle import invoke_oracle, resolve_entities, EntityRegistry, classify


@pytest.fixture
def mock_model():
    return Mock()


def test_project_structure():
    assert os.path.exists("src/schema_oracle/__init__.py")
    assert os.path.exists("src/schema_oracle/oracle.py")


def test_oracle_classify_edge():
    with patch("schema_oracle.oracle.httpx") as mock_httpx:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"edge_type": "IS_A"}'}}]
        }
        mock_httpx.post.return_value = mock_response

        result = invoke_oracle("Classify this edge.")
        assert isinstance(result, dict)
        assert "edge_type" in result


def test_oracle_hard_fail():
    with patch("schema_oracle.oracle.httpx") as mock_httpx:
        mock_response = Mock()
        mock_response.status_code = 500
        mock_httpx.post.return_value = mock_response

        with pytest.raises(RuntimeError):
            invoke_oracle("Test")


def test_entity_registry_init():
    registry = EntityRegistry()
    assert hasattr(registry, 'registry')
    assert hasattr(registry, 'variants')
    assert registry.registry == {}
    assert registry.variants == {}


def test_resolve_new_entity():
    registry = EntityRegistry()

    result = registry.resolve("Acme Corp", "Organization")

    assert result["canonical_id"] == "organization-0"
    assert result["is_duplicate"] is False
    assert result["duplicates"] == []
    assert "organization-0" in registry.registry
    assert registry.registry["organization-0"]["name"] == "Acme Corp"
    assert registry.registry["organization-0"]["type"] == "Organization"


def test_resolve_duplicate_entity():
    registry = EntityRegistry()
    registry.resolve("Acme Corp", "Organization")

    result = registry.resolve("acme corp inc.", "Organization")

    assert result["canonical_id"] == "organization-0"
    assert result["is_duplicate"] is True
    assert len(result["duplicates"]) == 1


def test_resolve_entities():
    mock_model = Mock()
    context = {"known_types": ["Person", "Organization"], "registry": {}}
    query_input = {
        "entities": [
            {"entity": "John Doe", "type": "Person"},
            {"entity": "Acme Corp", "type": "Organization"},
        ]
    }

    result = resolve_entities(query_input, mock_model, context)

    assert "classification" in result
    assert "resolution" in result
    assert len(result["resolution"]["resolved"]) == 2


def test_oracle_resolve_duplicates():
    mock_model = Mock()
    context = {"known_types": ["Person", "Organization"], "registry": {}}
    query_input = {
        "entities": [
            {"entity": "Apple Inc.", "type": "Organization"},
            {"entity": "Apple Computer Inc.", "type": "Organization"},
        ]
    }
    result = resolve_entities(query_input, mock_model, context)

    assert len(result["resolution"]["resolved"]) == 2
    assert result["resolution"]["duplicates_found"] >= 0
    assert "canonical_registry" in result["resolution"]


def test_oracle_classify_and_resolve(mock_model):
    mock_model.query_classification.return_value = {
        "classification": {"entity_type": "Organization", "confidence": 0.95}
    }
    context = {"known_types": ["Person", "Organization"], "registry": {}}
    query_input = {
        "entities": [
            {"entity": "Apple Inc.", "type": "Organization"},
            {"entity": "Tech Corp Ltd", "type": "Organization"},
        ]
    }

    result = classify(query_input, mock_model, context, enable_resolution=True)

    assert "classification" in result
    assert "resolution" in result
    assert len(result["resolution"]["resolved"]) == 2
    assert result["resolution"]["resolved"][0]["canonical_id"] == "organization-0"
    assert result["resolution"]["resolved"][1]["canonical_id"] == "organization-1"


def test_oracle_classify_with_duplicates(mock_model):
    mock_model.query_classification.return_value = {
        "classification": {"entity_type": "Organization", "confidence": 0.9}
    }
    context = {"known_types": ["Person", "Organization"], "registry": {}}
    query_input = {
        "entities": [
            {"entity": "Acme Corp", "type": "Organization"},
            {"entity": "ACME CORP INC", "type": "Organization"},
        ]
    }

    result = classify(query_input, mock_model, context, enable_resolution=True)

    assert result["resolution"]["duplicates_found"] == 1
    assert len(result["resolution"]["canonical_registry"]) == 1


def test_oracle_novel_type_rejected(mock_model):
    mock_model.query_classification.return_value = {
        "classification": {"entity_type": "Location", "confidence": 0.95}
    }
    context = {"known_types": ["Person", "Organization"], "registry": {}}
    query_input = {"entities": [{"entity": "New York City", "type": "Location"}]}

    with pytest.raises(ValueError) as excinfo:
        classify(query_input, mock_model, context)

    assert "novel/conflicting" in str(excinfo.value).lower()
