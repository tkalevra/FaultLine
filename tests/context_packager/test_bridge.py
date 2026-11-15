import sys
import os
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from context_packager import build_audit_context


def test_project_structure():
    assert os.path.exists("src/context_packager/__init__.py")


def test_build_context_from_entities():
    entities = [{"entity": "test", "label": "rel"}]
    result = build_audit_context(entities, "source_span")

    assert isinstance(result, dict)
    assert "context" in result
    assert "metadata" in result
    assert result["context"] == entities
    assert result["metadata"]["source"] == "source_span"


def test_packager_json_schema():
    entities = [
        {"entity": "Alice", "label": "Person"},
        {"entity": "Acme Corp", "label": "Organization"},
    ]
    result = build_audit_context(entities, "Alice works for Acme Corp.")

    serialized = json.dumps(result)
    parsed = json.loads(serialized)

    assert parsed["context"] == entities
    assert "source" in parsed["metadata"]


def test_packager_empty_entities():
    result = build_audit_context([], "empty span")
    assert result["context"] == []
    assert result["metadata"]["source"] == "empty span"


def test_packager_metadata_source():
    span = "Test sentence for extraction."
    result = build_audit_context([{"entity": "X", "label": "T"}], span)
    assert result["metadata"]["source"] == span
