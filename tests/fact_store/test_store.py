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
        ("sub1", "obj1", "IS_A", "prov1"),
        ("sub2", "obj2", "KILLS", "prov2"),
    ])

    assert result == 2
    mock_conn.commit.assert_called_once()


def test_commit_single_edge():
    mock_conn = MagicMock()
    manager = FactStoreManager(mock_conn)

    result = manager.commit([("alice", "acme", "WORKS_FOR", "doc-1")])

    assert result == 1
    mock_conn.commit.assert_called_once()


def test_commit_rollback_on_error():
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.execute.side_effect = psycopg2.Error("DB write error")

    manager = FactStoreManager(mock_conn)

    with pytest.raises(psycopg2.Error):
        manager.commit([("sub1", "obj1", "IS_A", "prov1")])

    mock_conn.rollback.assert_called_once()
    mock_conn.commit.assert_not_called()
