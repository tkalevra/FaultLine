import pytest
from unittest.mock import MagicMock, patch, call
from fastapi.testclient import TestClient

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from api.main import app


@pytest.fixture
def mock_db():
    """Shared mock database connection for retract tests."""
    db = MagicMock()
    cursor = db.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("hard_delete",)  # correction_behavior
    return db


@pytest.fixture
def client():
    """FastAPI test client."""
    with TestClient(app) as c:
        yield c


def test_pref_name_retraction_cleans_entity_aliases(mock_db):
    """
    POST /retract with rel_type="pref_name" and mode="hard_delete"
    should DELETE from entity_aliases with correct (entity_id, user_id, alias, is_preferred=true) parameters.
    """
    retract_cursor = mock_db.cursor.return_value.__enter__.return_value
    retract_cursor.fetchall.return_value = [
        (1,), (2,)  # Two fact IDs
    ]

    with patch("api.main.psycopg2.connect", return_value=mock_db), \
         patch.dict(os.environ, {"POSTGRES_DSN": "mock://dsn", "QDRANT_URL": "http://qdrant:6333"}), \
         patch("api.main.FactStoreManager") as MockManager, \
         patch("api.main._delete_from_qdrant") as mock_qdrant:

        MockManager.return_value.retract.return_value = [1, 2]

        with TestClient(app) as client:
            response = client.post("/retract", json={
                "user_id": "test-user-123",
                "subject": "entity-uuid-123",
                "rel_type": "pref_name",
                "old_value": "Old Name",
            })

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["retracted"] == 2
        assert data["mode"] == "hard_delete"

        # Verify entity_aliases DELETE was called with correct parameters
        calls = mock_db.cursor.return_value.__enter__.return_value.execute.call_args_list
        # NO RECURSIVE MATCHING
        delete_call_found = False
        for call_args in calls:
            if call_args[0][0] and "DELETE FROM entity_aliases" in call_args[0][0]:
                delete_call_found = True
                # Verify parameters
                assert call_args[0][1] == ("entity-uuid-123", "test-user-123", "Old Name")
                break
        assert delete_call_found, "DELETE FROM entity_aliases was not called"


def test_pref_name_retraction_entity_aliases_failure_does_not_raise(mock_db):
    """
    When entity_aliases DELETE raises an exception, the retraction should still return success
    and log a warning.
    """
    retract_cursor = mock_db.cursor.return_value.__enter__.return_value
    retract_cursor.fetchall.return_value = [(1,)]

    # Make the DELETE call raise an exception
    def side_effect(sql, *args):
        if "DELETE FROM entity_aliases" in sql:
            raise Exception("Database error on entity_aliases cleanup")

    retract_cursor.execute.side_effect = side_effect

    with patch("api.main.psycopg2.connect", return_value=mock_db), \
         patch.dict(os.environ, {"POSTGRES_DSN": "mock://dsn", "QDRANT_URL": "http://qdrant:6333"}), \
         patch("api.main.FactStoreManager") as MockManager, \
         patch("api.main._delete_from_qdrant") as mock_qdrant, \
         patch("api.main.log") as mock_log:

        MockManager.return_value.retract.return_value = [1]

        with TestClient(app) as client:
            response = client.post("/retract", json={
                "user_id": "test-user-123",
                "subject": "entity-uuid-456",
                "rel_type": "pref_name",
                "old_value": "Stale Name",
            })

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["retracted"] == 1
        assert data["mode"] == "hard_delete"

        # Verify warning was logged
        mock_log.warning.assert_called()
        call_args = mock_log.warning.call_args
        assert "retract.entity_aliases_cleanup_failed" in call_args[0]


def test_also_known_as_retraction_does_not_touch_entity_aliases(mock_db):
    """
    POST /retract with rel_type="also_known_as" should NOT call DELETE on entity_aliases.
    Entity_aliases cleanup is only for pref_name, not for other relationship types.
    """
    retract_cursor = mock_db.cursor.return_value.__enter__.return_value
    retract_cursor.fetchall.return_value = [(1,)]
    retract_cursor.fetchone.return_value = ("hard_delete",)  # correction_behavior for also_known_as

    with patch("api.main.psycopg2.connect", return_value=mock_db), \
         patch.dict(os.environ, {"POSTGRES_DSN": "mock://dsn", "QDRANT_URL": "http://qdrant:6333"}), \
         patch("api.main.FactStoreManager") as MockManager, \
         patch("api.main._delete_from_qdrant") as mock_qdrant:

        MockManager.return_value.retract.return_value = [1]

        with TestClient(app) as client:
            response = client.post("/retract", json={
                "user_id": "test-user-123",
                "subject": "entity-uuid-789",
                "rel_type": "also_known_as",
                "old_value": "Nickname",
            })

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["retracted"] == 1

        # Verify entity_aliases DELETE was NOT called
        calls = mock_db.cursor.return_value.__enter__.return_value.execute.call_args_list
        for call_args in calls:
            if call_args[0][0]:
                assert "DELETE FROM entity_aliases" not in call_args[0][0], \
                    "DELETE FROM entity_aliases should not be called for also_known_as"
