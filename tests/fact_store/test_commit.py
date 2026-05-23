import pytest
from unittest.mock import MagicMock
import psycopg2
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from fact_store import commit_edge
from fact_store.store import FactStoreManager


def test_project_structure():
    assert os.path.exists("src/fact_store/__init__.py")
    assert os.path.exists("src/fact_store/store.py")


def test_commit_valid_edge():
    result = commit_edge("sub", "obj", "rel", "prov")
    assert isinstance(result, dict)
    assert "id" in result


def test_commit_rollback_on_error():
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.execute.side_effect = psycopg2.Error("DB error")

    with pytest.raises(psycopg2.Error):
        commit_edge("sub", "obj", "rel", "prov", db_conn=mock_conn)

    mock_conn.rollback.assert_called_once()


def test_commit_corroboration(mock_db):
    manager = FactStoreManager(mock_db)
    edge = [("user1", "alice", "bob", "spouse", "doc")]
    manager.commit(edge)
    manager.commit(edge)

    cursor = mock_db.cursor.return_value.__enter__.return_value
    assert cursor.execute.call_count == 2
    sql = cursor.execute.call_args_list[0][0][0]
    assert "DO UPDATE SET" in sql
    assert "confirmed_count" in sql
    assert mock_db.commit.call_count == 2


def test_commit_confidence_stored(mock_db):
    manager = FactStoreManager(mock_db)
    manager.commit([("user1", "alice", "corp", "works_for", "doc")], confidence=0.7)

    cursor = mock_db.cursor.return_value.__enter__.return_value
    params = cursor.execute.call_args[0][1]
    assert 0.7 in params
