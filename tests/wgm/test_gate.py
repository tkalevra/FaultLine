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
    cursor_mock.fetchall.return_value = [("IS_A",)]

    gate = WGMValidationGate(mock_conn)
    result = gate.validate_edge(1, 2, "KILLS")

    assert result == {"status": "conflict"}


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
