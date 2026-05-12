"""Shared fixtures for faultline-wgm tests."""
import sys
import os
import pytest
from unittest.mock import Mock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))


def pytest_configure(config):
    pass


@pytest.fixture
def mock_qwen():
    """Mock Qwen2.5 Coder model with query_classification method."""
    return Mock()


@pytest.fixture
def mock_db():
    """Mock psycopg2 connection for fact store tests."""
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    return conn
