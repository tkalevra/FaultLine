import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from api.main import app, get_gliner_model


@pytest.fixture
def mock_model():
    model = MagicMock()
    model.predict.return_value = [
        {"entity": "Alice", "label": "Person"},
        {"entity": "Acme Corp", "label": "Organization"},
    ]
    return model


@pytest.fixture
def client(mock_model):
    app.dependency_overrides[get_gliner_model] = lambda: mock_model
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_ingest_extract_only(client):
    r = client.post("/ingest", json={
        "text": "Alice works for Acme Corp.",
        "source": "test",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "extracted"
    assert data["committed"] == 0
    assert len(data["entities"]) == 2
    assert data["facts"] == []


def test_ingest_with_valid_edge(client):
    mock_db = MagicMock()
    cursor = mock_db.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []

    with patch("api.main.psycopg2.connect", return_value=mock_db), \
         patch.dict(os.environ, {"POSTGRES_DSN": "mock://dsn"}):
        r = client.post("/ingest", json={
            "text": "Alice works for Acme Corp.",
            "source": "test",
            "edges": [{"subject": "Alice", "object": "Acme Corp", "rel_type": "WORKS_FOR"}],
        })

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "valid"
    assert data["committed"] == 1
    assert data["facts"][0]["status"] == "valid"


def test_ingest_with_novel_edge(client):
    mock_db = MagicMock()
    cursor = mock_db.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []

    with patch("api.main.psycopg2.connect", return_value=mock_db), \
         patch.dict(os.environ, {"POSTGRES_DSN": "mock://dsn"}):
        r = client.post("/ingest", json={
            "text": "Alice works for Acme Corp.",
            "source": "test",
            "edges": [{"subject": "Alice", "object": "Acme Corp", "rel_type": "INVENTED_BY"}],
        })

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "novel"
    assert data["committed"] == 0
    assert data["facts"][0]["status"] == "novel"


def test_ingest_edges_no_dsn(client):
    env = {k: v for k, v in os.environ.items() if k != "POSTGRES_DSN"}
    with patch.dict(os.environ, env, clear=True):
        r = client.post("/ingest", json={
            "text": "Alice works for Acme Corp.",
            "source": "test",
            "edges": [{"subject": "Alice", "object": "Acme Corp", "rel_type": "WORKS_FOR"}],
        })
    assert r.status_code == 503


def test_bracket_constraint_built_from_db():
    """_build_rel_type_constraint should load types from DB and create pipe-separated string."""
    from api.main import _build_rel_type_constraint
    from unittest.mock import MagicMock, patch

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.__exit__.return_value = None
    mock_cursor.fetchall.return_value = [("is_a",), ("works_for",), ("spouse",)]
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = None
    mock_conn.cursor.return_value = mock_cursor

    with patch("api.main.psycopg2.connect", return_value=mock_conn):
        constraint = _build_rel_type_constraint("fake_dsn")

    assert "is_a" in constraint
    assert "works_for" in constraint
    assert "spouse" in constraint
    assert "|" in constraint
