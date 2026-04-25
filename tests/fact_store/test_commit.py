import pytest
from unittest.mock import MagicMock
import psycopg2
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from fact_store import commit_edge


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
