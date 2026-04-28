import pytest
from unittest.mock import MagicMock
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from wgm.gate import WGMValidationGate


def test_project_structure():
    assert os.path.exists("src/wgm/__init__.py")
    assert os.path.exists("src/wgm/gate.py")


def test_validate_novel_type():
    """Novel rel_type not in ontology → novel; no DB query issued."""
    mock_conn = MagicMock()
    gate = WGMValidationGate(mock_conn)

    result = gate.validate_edge(1, 2, "unknown_relationship")

    assert result == {"status": "novel"}
    mock_conn.cursor.assert_not_called()


def test_validate_conflict():
    """Known rel_type but a different rel already exists for same pair → conflict."""
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.fetchall.return_value = [(42, 1.0)]  # Existing fact with id=42, confidence=1.0
    cursor_mock.fetchone.return_value = (99,)  # New fact id from RETURNING

    gate = WGMValidationGate(mock_conn)
    result = gate.validate_edge(1, 2, "is_a", user_id="user1", provenance="doc")

    assert result["status"] == "conflict"


def test_validate_valid_edge():
    """Known rel_type, no existing edges for this pair → valid."""
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.fetchall.return_value = []

    gate = WGMValidationGate(mock_conn)
    result = gate.validate_edge(1, 2, "IS_A")

    assert result == {"status": "valid"}


def test_validate_same_rel_existing():
    """Known rel_type, same rel already exists for pair → valid (idempotent)."""
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.fetchall.return_value = [("IS_A",)]

    gate = WGMValidationGate(mock_conn)
    result = gate.validate_edge(1, 2, "IS_A")

    assert result == {"status": "valid"}


def test_conflict_inserts_new_fact():
    """Conflicting edge (same user+subject+rel, different object) gets inserted, not dropped."""
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.fetchall.return_value = [(42, 1.0)]  # old fact: id=42, confidence=1.0
    cursor_mock.fetchone.return_value = (99,)  # new_id from RETURNING

    gate = WGMValidationGate(mock_conn)
    result = gate.validate_edge("christopher", "christophe", "also_known_as",
                                user_id="user1", provenance="doc")

    assert result["status"] == "conflict"
    all_sql = [str(c) for c in cursor_mock.execute.call_args_list]
    assert any("INSERT" in s for s in all_sql)


def test_conflict_penalizes_old_fact():
    """After conflict INSERT, old fact confidence drops by penalty and gets linked."""
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.fetchall.return_value = [(42, 1.0)]  # old: id=42, confidence=1.0
    cursor_mock.fetchone.return_value = (99,)

    gate = WGMValidationGate(mock_conn)
    result = gate.validate_edge("christopher", "christophe", "also_known_as",
                                user_id="user1", provenance="doc")

    assert result["superseded_fact_id"] == 42
    assert result["new_fact_id"] == 99
    assert result["old_confidence_after_penalty"] == pytest.approx(0.5)
    all_sql = [str(c) for c in cursor_mock.execute.call_args_list]
    assert any("GREATEST" in s for s in all_sql)


def test_conflict_marks_old_superseded():
    """After a conflict INSERT, old fact's contradicted_by is updated to new fact id."""
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.fetchall.return_value = [(42, 1.0)]
    cursor_mock.fetchone.return_value = (99,)

    gate = WGMValidationGate(mock_conn)
    gate.validate_edge("alice", "corp_b", "works_for",
                       user_id="user1", provenance="doc")

    all_sql = [str(c) for c in cursor_mock.execute.call_args_list]
    assert any("contradicted_by" in s for s in all_sql)


def test_valid_path_unaffected():
    """Valid edge with no prior conflict returns status=valid, no contradicted_by update."""
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.fetchall.return_value = []  # no existing conflicting fact

    gate = WGMValidationGate(mock_conn)
    result = gate.validate_edge("alice", "corp", "works_for", user_id="user1")

    assert result == {"status": "valid"}
    all_sql = [str(c) for c in cursor_mock.execute.call_args_list]
    assert not any("contradicted_by" in s for s in all_sql)


def test_owns_in_ontology():
    """New rel_type 'owns' is in the ontology."""
    from wgm.gate import SEED_ONTOLOGY
    assert "owns" in SEED_ONTOLOGY


def test_educated_at_in_ontology():
    """New rel_type 'educated_at' is in the ontology."""
    from wgm.gate import SEED_ONTOLOGY
    assert "educated_at" in SEED_ONTOLOGY


def test_occupation_in_ontology():
    """New rel_type 'occupation' is in the ontology."""
    from wgm.gate import SEED_ONTOLOGY
    assert "occupation" in SEED_ONTOLOGY


def test_rel_type_registry_loads_from_db():
    """RelTypeRegistry can load types from mock DB."""
    from wgm.gate import RelTypeRegistry
    from unittest.mock import MagicMock, patch

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.__exit__.return_value = None
    mock_cursor.fetchall.return_value = [("is_a",), ("works_for",), ("spouse",)]
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = None
    mock_conn.cursor.return_value = mock_cursor

    with patch("wgm.gate.psycopg2.connect", return_value=mock_conn):
        registry = RelTypeRegistry("fake_dsn")
        types = registry.get_valid_types()

    assert "is_a" in types
    assert "works_for" in types
    assert "spouse" in types


def test_rel_type_registry_is_valid():
    """RelTypeRegistry.is_valid checks membership correctly."""
    from wgm.gate import RelTypeRegistry
    from unittest.mock import MagicMock, patch

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.__exit__.return_value = None
    mock_cursor.fetchall.return_value = [("is_a",), ("works_for",)]
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = None
    mock_conn.cursor.return_value = mock_cursor

    with patch("wgm.gate.psycopg2.connect", return_value=mock_conn):
        registry = RelTypeRegistry("fake_dsn")
        assert registry.is_valid("is_a") is True
        assert registry.is_valid("unknown_type") is False


def test_novel_type_approved_by_qwen():
    """When Qwen approves a novel type with confidence >= 0.7, it gets inserted to rel_types."""
    from wgm.gate import WGMValidationGate
    from unittest.mock import MagicMock, patch

    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.__exit__.return_value = None
    mock_db.cursor.return_value = mock_cursor

    qwen_response = MagicMock()
    qwen_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"valid": true, "label": "influenced by", "wikidata_pid": "P1891", "confidence": 0.85, "reason": "Valid relationship"}'
                }
            }
        ]
    }
    qwen_response.raise_for_status.return_value = None

    with patch("wgm.gate.httpx.post", return_value=qwen_response):
        with patch("wgm.gate.os.getenv", return_value="http://qwen:8000/v1/chat/completions"):
            gate = WGMValidationGate(mock_db)
            result = gate._try_approve_novel_type("influenced_by")

    assert result is True
    mock_db.cursor.assert_called()


def test_novel_type_rejected_by_qwen():
    """When Qwen rejects a novel type (confidence < 0.7), it gets inserted to pending_types."""
    from wgm.gate import WGMValidationGate
    from unittest.mock import MagicMock, patch

    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.__exit__.return_value = None
    mock_db.cursor.return_value = mock_cursor

    qwen_response = MagicMock()
    qwen_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"valid": false, "label": "unknown", "wikidata_pid": null, "confidence": 0.2, "reason": "Not a standard relationship"}'
                }
            }
        ]
    }
    qwen_response.raise_for_status.return_value = None

    with patch("wgm.gate.httpx.post", return_value=qwen_response):
        with patch("wgm.gate.os.getenv", return_value="http://qwen:8000/v1/chat/completions"):
            gate = WGMValidationGate(mock_db)
            result = gate._try_approve_novel_type("nonsense_type")

    assert result is False


def test_novel_type_qwen_timeout():
    """When Qwen times out, the edge does not get approved (graceful failure)."""
    from wgm.gate import WGMValidationGate
    from unittest.mock import MagicMock, patch
    import httpx

    mock_db = MagicMock()

    with patch("wgm.gate.httpx.post", side_effect=httpx.TimeoutException("timeout")):
        with patch("wgm.gate.os.getenv", return_value="http://qwen:8000/v1/chat/completions"):
            gate = WGMValidationGate(mock_db)
            result = gate._try_approve_novel_type("some_type")

    assert result is False


def test_symmetric_duplicate_suppressed():
    """For symmetric rel_types, storing A→B prevents duplicate B→A insertion."""
    from wgm.gate import WGMValidationGate
    from unittest.mock import MagicMock

    mock_db = MagicMock()
    cursor_mock = mock_db.cursor.return_value.__enter__.return_value

    # Check 1: exact duplicate (A→B) — not found
    # Check 2: symmetric duplicate (B→A) — not found
    # Check 3: conflicting facts with different object — not found
    cursor_mock.fetchone.side_effect = [None, None]
    cursor_mock.fetchall.return_value = []

    gate = WGMValidationGate(mock_db)
    result = gate.validate_edge("alice", "bob", "spouse", user_id="user1", provenance="doc")

    # Should be valid (no duplicates, no conflicts)
    assert result == {"status": "valid"}


def test_symmetric_duplicate_detected():
    """When B→A already exists and A→B is attempted for a symmetric type, return symmetric_duplicate."""
    from wgm.gate import WGMValidationGate
    from unittest.mock import MagicMock

    mock_db = MagicMock()
    cursor_mock = mock_db.cursor.return_value.__enter__.return_value

    # Check 1: exact duplicate (A→B) — not found
    # Check 2: symmetric duplicate (B→A) — found!
    cursor_mock.fetchone.side_effect = [None, (42,)]

    gate = WGMValidationGate(mock_db)
    result = gate.validate_edge("alice", "bob", "spouse", user_id="user1", provenance="doc")

    assert result["status"] == "valid"
    assert result["note"] == "symmetric_duplicate"


def test_instance_of_valid():
    """instance_of (P31) is a valid rel_type."""
    from wgm.gate import SEED_ONTOLOGY
    assert "instance_of" in SEED_ONTOLOGY


def test_subclass_of_valid():
    """subclass_of (P279) is a valid rel_type."""
    from wgm.gate import SEED_ONTOLOGY
    assert "subclass_of" in SEED_ONTOLOGY


def test_pref_name_valid():
    """pref_name (skos:prefLabel) is a valid rel_type."""
    from wgm.gate import SEED_ONTOLOGY
    assert "pref_name" in SEED_ONTOLOGY


def test_same_as_valid():
    """same_as (owl:sameAs) is a valid rel_type."""
    from wgm.gate import SEED_ONTOLOGY
    assert "same_as" in SEED_ONTOLOGY


def test_is_a_deprecated_but_valid():
    """is_a is deprecated but remains valid for backward compatibility."""
    from wgm.gate import SEED_ONTOLOGY
    assert "is_a" in SEED_ONTOLOGY
