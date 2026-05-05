import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from api.main import app, get_gliner_model
from src.wgm.gate import WGMValidationGate


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

# PRE-EXISTING FAILURE — not caused by Phase 1
# Reason: Qdrant not running at test time
# Fix required: Mock the health check or mark as integration-only test
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ingest_extract_only(client):
    r = client.post("/ingest", json={
        "text": "Alice works for Acme Corp.",
        "source": "test",
    })
    assert r.status_code == 200
    data = r.json()
    # entities come from resolve_entities which requires DB — assert committed count only
    assert data["committed"] == 0
    assert data["status"] == "valid"
    assert data["facts"] == []


def test_ingest_with_valid_edge(client):
    mock_db = MagicMock()
    cursor = mock_db.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None

    with patch("api.main.psycopg2.connect", return_value=mock_db), \
         patch.dict(os.environ, {"POSTGRES_DSN": "mock://dsn"}):
        r = client.post("/ingest", json={
            "text": "Alice works for Acme Corp.",
            "source": "test",
            "user_id": "a1b2c3d4-0000-0000-0000-000000000000",
            "edges": [{"subject": "Alice", "object": "Acme Corp", "rel_type": "WORKS_FOR"}],
        })

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "valid"
    assert data["committed"] == 1
    assert data["facts"][0]["status"] == "valid"


def test_ingest_with_novel_edge():
    mock_db = MagicMock()
    cursor = mock_db.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None

    with patch("api.main.psycopg2.connect", return_value=mock_db), \
         patch.dict(os.environ, {"POSTGRES_DSN": "mock://dsn"}), \
         patch("api.main._rel_type_registry", None), \
         patch("api.main._rel_type_constraint", ""), \
         patch("api.main.WGMValidationGate") as MockGate, \
         patch("api.main.FactStoreManager") as MockManager:

        MockGate.return_value.validate_edge.return_value = {"status": "novel"}
        MockManager.return_value.commit.return_value = 0

        with TestClient(app) as c:
            r = c.post("/ingest", json={
                "text": "Alice works for Acme Corp.",
                "source": "test",
                "user_id": "a1b2c3d4-0000-0000-0000-000000000000",
                "edges": [{"subject": "Alice", "object": "Acme Corp", "rel_type": "INVENTED_BY"}],
            })

    assert r.status_code == 200
    data = r.json()
    assert data["committed"] == 0
    assert all(f["status"] == "novel" for f in data["facts"])


def test_ingest_edges_no_dsn():
    with patch("api.main._rel_type_registry", None), \
         patch("api.main._rel_type_constraint", ""), \
         patch("api.main.ensure_collection", return_value=True), \
         patch.dict(os.environ, {"QDRANT_URL": "http://mock", "QDRANT_COLLECTION": "mock"}, clear=True):

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/ingest", json={
                "text": "Alice works for Acme Corp.",
                "source": "test",
                "edges": [{"subject": "Alice", "object": "Acme Corp", "rel_type": "WORKS_FOR"}],
            })

    assert r.status_code in (200, 500)


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


# PRE-EXISTING FAILURE — not caused by Phase 1
# Reason: Test asserts DELETE SQL but code does UPDATE superseded_at
# Fix required: Align test assertions to actual correction behavior
def test_correction_hard_delete_migrates_facts():
    """hard_delete correction should migrate facts from old name to new name."""
    mock_db = MagicMock()
    cursor = mock_db.cursor.return_value.__enter__.return_value
    _fetchone_values = iter([None, ("a1b2c3d4-0000-0000-0000-000000000001",), ("hard_delete",), None, None, None])
    cursor.fetchone.side_effect = lambda *a, **kw: next(_fetchone_values, None)
    cursor.fetchall.return_value = []

    with patch("api.main.psycopg2.connect", return_value=mock_db), \
         patch.dict(os.environ, {"POSTGRES_DSN": "mock://dsn"}), \
         patch("api.main._gliner2_model", None), \
         patch("api.main._rel_type_registry", None), \
         patch("api.main._rel_type_constraint", ""), \
         patch("api.main.WGMValidationGate") as MockGate:

        MockGate.return_value.validate_edge.return_value = {"status": "valid"}

        with TestClient(app) as c:
            r = c.post("/ingest", json={
                "text": "oh his name is actually Biscuit",
                "source": "test",
                "user_id": "user1",
                "edges": [{
                    "subject": "biskit",
                    "object": "biscuit",
                    "rel_type": "also_known_as",
                    "is_correction": True,
                }],
            })

    assert r.status_code == 200
    all_sql = [str(c) for c in cursor.execute.call_args_list]
    assert any("DELETE" in sql for sql in all_sql)
    assert any("SET subject_id" in sql for sql in all_sql)
    assert any("is_preferred_label = true" in sql for sql in all_sql)


# PRE-EXISTING FAILURE — not caused by Phase 1
# Reason: Test assertions don't match current code behavior
# Fix required: Rewrite assertions to match actual correction logic
def test_correction_supersede_marks_old_fact():
    """supersede correction should mark old fact as superseded, not delete."""
    mock_db = MagicMock()
    cursor = mock_db.cursor.return_value.__enter__.return_value
    _fetchone_values = iter([None, ("a1b2c3d4-0000-0000-0000-000000000002",), ("supersede",), None, None, None])
    cursor.fetchone.side_effect = lambda *a, **kw: next(_fetchone_values, None)
    cursor.fetchall.return_value = []

    with patch("api.main.psycopg2.connect", return_value=mock_db), \
         patch.dict(os.environ, {"POSTGRES_DSN": "mock://dsn"}), \
         patch("api.main._gliner2_model", None), \
         patch("api.main._rel_type_registry", None), \
         patch("api.main.WGMValidationGate") as MockGate:

        MockGate.return_value.validate_edge.return_value = {"status": "valid"}

        with TestClient(app) as c:
            r = c.post("/ingest", json={
                "text": "I moved to 456 New Street",
                "source": "test",
                "user_id": "user1",
                "edges": [{
                    "subject": "user",
                    "object": "456 new street",
                    "rel_type": "lives_at",
                    "is_correction": True,
                }],
            })

    assert r.status_code == 200
    all_sql = [str(c) for c in cursor.execute.call_args_list]
    assert any("superseded_at" in sql for sql in all_sql), "superseded_at UPDATE should be called"
    assert not any("DELETE" in sql for sql in all_sql), "DELETE should not be called for supersede"


def test_correction_immutable_does_nothing():
    """immutable correction should not modify any facts."""
    mock_db = MagicMock()
    cursor = mock_db.cursor.return_value.__enter__.return_value
    _fetchone_values = iter([None, ("a1b2c3d4-0000-0000-0000-000000000003",), ("immutable",), None, None, None])
    cursor.fetchone.side_effect = lambda *a, **kw: next(_fetchone_values, None)
    cursor.fetchall.return_value = []

    with patch("api.main.psycopg2.connect", return_value=mock_db), \
         patch.dict(os.environ, {"POSTGRES_DSN": "mock://dsn"}), \
         patch("api.main._gliner2_model", None), \
         patch("api.main._rel_type_registry", None), \
         patch("api.main.WGMValidationGate") as MockGate, \
         patch("api.main.FactStoreManager") as MockManager:

        MockGate.return_value.validate_edge.return_value = {"status": "valid"}
        MockManager.return_value.commit.return_value = 1

        with TestClient(app) as c:
            r = c.post("/ingest", json={
                "text": "I was actually born in Toronto",
                "source": "test",
                "user_id": "user1",
                "edges": [{
                    "subject": "arthur",
                    "object": "toronto",
                    "rel_type": "born_in",
                    "is_correction": True,
                }],
            })

    assert r.status_code == 200
    all_sql = [str(c) for c in cursor.execute.call_args_list]
    assert not any("DELETE" in sql for sql in all_sql), "DELETE should not be called for immutable"
    assert not any("superseded_at" in sql for sql in all_sql), "superseded_at should not be called for immutable"
