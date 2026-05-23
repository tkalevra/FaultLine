import pytest
from unittest.mock import MagicMock
import psycopg2
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from fact_store.store import FactStoreManager


def test_project_structure():
    assert os.path.exists("src/fact_store/__init__.py")
    assert os.path.exists("src/fact_store/store.py")


def test_commit_valid_entities():
    mock_conn = MagicMock()
    manager = FactStoreManager(mock_conn)

    result = manager.commit([
        ("test_user", "sub1", "obj1", "IS_A", "prov1"),
        ("test_user", "sub2", "obj2", "KILLS", "prov2"),
    ])

    assert result == 2
    mock_conn.commit.assert_called_once()


def test_commit_single_edge():
    mock_conn = MagicMock()
    manager = FactStoreManager(mock_conn)

    result = manager.commit([("test_user", "alice", "acme", "WORKS_FOR", "doc-1")])

    assert result == 1
    mock_conn.commit.assert_called_once()


def test_commit_rollback_on_error():
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.execute.side_effect = psycopg2.Error("DB write error")

    manager = FactStoreManager(mock_conn)

    with pytest.raises(psycopg2.Error):
        manager.commit([("test_user", "sub1", "obj1", "IS_A", "prov1")])

    mock_conn.rollback.assert_called_once()
    mock_conn.commit.assert_not_called()


def test_mark_contradicted():
    mock_conn = MagicMock()
    manager = FactStoreManager(mock_conn)
    manager.mark_contradicted(old_id=42, new_id=99)

    cursor = mock_conn.cursor.return_value.__enter__.return_value
    sql, params = cursor.execute.call_args[0]
    assert "contradicted_by" in sql
    assert "GREATEST" in sql
    assert params == (0.5, 99, 0.5, 42)
    mock_conn.commit.assert_called_once()


def test_mark_contradicted_reduces_confidence(mock_db):
    manager = FactStoreManager(mock_db)
    manager.mark_contradicted(old_id=42, new_id=99, penalty=0.5)

    cursor = mock_db.cursor.return_value.__enter__.return_value
    sql, params = cursor.execute.call_args[0]
    assert "GREATEST" in sql
    assert "confidence" in sql
    assert "contradicted_by" in sql
    assert "contradiction_confidence_penalty" in sql
    assert 0.5 in params
    assert 99 in params
    assert 42 in params
    mock_db.commit.assert_called_once()


def test_mark_contradicted_floors_at_zero(mock_db):
    manager = FactStoreManager(mock_db)
    manager.mark_contradicted(old_id=1, new_id=2, penalty=0.5)

    cursor = mock_db.cursor.return_value.__enter__.return_value
    sql, _ = cursor.execute.call_args[0]
    assert "GREATEST" in sql
    assert "0.0" in sql


def test_mark_contradicted_twice(mock_db):
    manager = FactStoreManager(mock_db)
    manager.mark_contradicted(old_id=1, new_id=2, penalty=0.5)
    manager.mark_contradicted(old_id=1, new_id=3, penalty=0.5)

    cursor = mock_db.cursor.return_value.__enter__.return_value
    assert cursor.execute.call_count == 2
    assert mock_db.commit.call_count == 2


def test_preferred_label_set_on_also_known_as():
    """Commit fact with is_preferred_label=True should be stored correctly."""
    mock_conn = MagicMock()
    manager = FactStoreManager(mock_conn)

    result = manager.commit([
        ("test_user", "eleanor", "ellie", "also_known_as", "test", True)
    ])

    assert result == 1
    cursor = mock_conn.cursor.return_value.__enter__.return_value
    sql, params = cursor.execute.call_args[0]
    assert "is_preferred_label" in sql
    # is_preferred_label should be the 8th parameter in the VALUES tuple
    assert params[7] is True
    mock_conn.commit.assert_called_once()


def test_also_known_as_accepts_is_preferred_label_field():
    """EdgeInput with is_preferred_label field should be valid."""
    from api.models import EdgeInput
    edge = EdgeInput(
        subject="eleanor",
        object="ellie",
        rel_type="also_known_as",
        is_preferred_label=True
    )
    assert edge.subject == "eleanor"
    assert edge.object == "ellie"
    assert edge.rel_type == "also_known_as"
    assert edge.is_preferred_label is True
