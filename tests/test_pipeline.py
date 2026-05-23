"""
End-to-end pipeline test: text → GliNER → Context Packager → Schema Oracle
→ WGM Validation Gate → Fact Store.
All external calls (GliNER model, httpx, PostgreSQL) are mocked.
"""
import pytest
from unittest.mock import MagicMock
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../src'))

from gli_ner import extract_entities
from context_packager import build_audit_context
from schema_oracle import resolve_entities
from wgm.gate import WGMValidationGate
from fact_store.store import FactStoreManager


@pytest.fixture
def gliner_model():
    model = MagicMock()
    model.predict.return_value = [
        {"entity": "Alice", "label": "Person"},
        {"entity": "Acme Corp", "label": "Organization"},
    ]
    return model


@pytest.fixture
def mock_db():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    return conn


def test_extract_and_package(gliner_model):
    """GliNER output feeds directly into Context Packager."""
    entities = extract_entities("Alice works for Acme Corp.", gliner_model)

    assert len(entities) == 2
    assert all("entity" in e and "label" in e for e in entities)

    context = build_audit_context(entities, "Alice works for Acme Corp.")
    assert context["context"] == entities
    assert context["metadata"]["source"] == "Alice works for Acme Corp."


def test_classify_and_validate(mock_db):
    """Schema Oracle resolution feeds into WGM Validation Gate."""
    query_input = {
        "entities": [
            {"entity": "Alice", "type": "Person"},
            {"entity": "Acme Corp", "type": "Organization"},
        ]
    }
    context = {"known_types": ["Person", "Organization"], "registry": {}}

    result = resolve_entities(query_input, None, context)
    assert result["resolution"]["duplicates_found"] == 0
    assert len(result["resolution"]["resolved"]) == 2

    gate = WGMValidationGate(mock_db)
    validation = gate.validate_edge("alice-0", "acme-0", "WORKS_FOR")
    assert validation == {"status": "valid"}


def test_validate_and_commit(mock_db):
    """Valid edge flows from WGM gate to Fact Store commit."""
    gate = WGMValidationGate(mock_db)
    validation = gate.validate_edge("alice-0", "acme-0", "WORKS_FOR")
    assert validation["status"] == "valid"

    manager = FactStoreManager(mock_db)
    count = manager.commit([("test-user", "alice-0", "acme-0", "WORKS_FOR", "pipeline-test")])
    assert count == 1
    mock_db.commit.assert_called_once()


def test_full_pipeline(gliner_model, mock_db):
    """Complete pipeline: text in, fact committed out."""
    text = "Alice works for Acme Corp."

    # Step 1: Extract
    entities = extract_entities(text, gliner_model)
    assert len(entities) == 2

    # Step 2: Package
    audit = build_audit_context(entities, text)
    assert "context" in audit

    # Step 3: Classify — resolve entities to canonical IDs
    oracle_input = {
        "entities": [
            {"entity": e["entity"], "type": e["label"]}
            for e in audit["context"]
        ]
    }
    classification = resolve_entities(
        oracle_input,
        model=None,
        context={"known_types": ["Person", "Organization"], "registry": {}},
    )
    resolved = classification["resolution"]["resolved"]
    assert len(resolved) == 2

    # Step 4: Validate
    gate = WGMValidationGate(mock_db)
    sub_id = resolved[0]["canonical_id"]
    obj_id = resolved[1]["canonical_id"]
    validation = gate.validate_edge(sub_id, obj_id, "WORKS_FOR")
    assert validation["status"] == "valid"

    # Step 5: Commit
    manager = FactStoreManager(mock_db)
    count = manager.commit([("test-user", sub_id, obj_id, "WORKS_FOR", text)])
    assert count == 1
