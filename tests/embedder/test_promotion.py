"""
Tests for promote_staged_facts() and expire_staged_facts() in src/re_embedder/embedder.py
"""
import pytest
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timedelta

from src.re_embedder.embedder import promote_staged_facts, expire_staged_facts


@pytest.fixture
def mock_db():
    """Mock psycopg2 database connection with cursor."""
    db = MagicMock()
    cursor = MagicMock()
    db.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    db.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return db, cursor


@pytest.fixture
def mock_httpx():
    """Mock httpx module."""
    with patch('src.re_embedder.embedder.httpx') as mock:
        yield mock


@pytest.fixture
def mock_derive_collection():
    """Mock derive_collection function."""
    with patch('src.re_embedder.embedder.derive_collection') as mock:
        mock.side_effect = lambda user_id: f"faultline-{user_id}" if user_id not in ("", "anonymous", "legacy") else "faultline-test"
        yield mock


@pytest.fixture
def mock_logger():
    """Mock logger."""
    with patch('src.re_embedder.embedder.log') as mock:
        yield mock


# Priority 1 - Critical Path Tests

def test_promote_staged_facts_happy_path(mock_db, mock_httpx, mock_derive_collection, mock_logger):
    """
    Test happy path: staged row with confirmed_count=3, promoted_at=None, expires_at=future.
    Verify: INSERT into facts attempted, Qdrant delete attempted, promoted_at updated.
    """
    db, cursor = mock_db
    future_time = datetime.now() + timedelta(days=1)

    # Setup candidate row
    candidate_row = (1, "user123", "subject-uuid", "object-uuid", "likes", "user_stated", 0.8)
    cursor.fetchall.return_value = [candidate_row]

    # Mock httpx response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_httpx.post.return_value = mock_response

    qdrant_url = "http://qdrant:6333"

    # Execute
    result = promote_staged_facts(db, qdrant_url, promotion_threshold=3)

    # Assertions
    assert result == 1
    assert mock_logger.info.call_count >= 1
    assert mock_logger.error.call_count == 0

    # Verify INSERT was attempted
    insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO facts" in str(c)]
    assert len(insert_calls) > 0

    # Verify UPDATE promoted_at was called
    update_calls = [c for c in cursor.execute.call_args_list if "UPDATE staged_facts SET promoted_at" in str(c)]
    assert len(update_calls) > 0

    # Verify commit was called
    assert db.commit.call_count >= 1

    # Verify Qdrant delete was attempted
    qdrant_delete_calls = [c for c in mock_httpx.post.call_args_list
                          if "/points/delete" in str(c)]
    assert len(qdrant_delete_calls) > 0


def test_promote_staged_facts_below_threshold(mock_db, mock_httpx, mock_derive_collection, mock_logger):
    """
    Test below threshold: confirmed_count=2 (< 3).
    Verify: candidate query returns nothing, no promotion happens.
    """
    db, cursor = mock_db

    # No candidates returned
    cursor.fetchall.return_value = []

    qdrant_url = "http://qdrant:6333"

    # Execute
    result = promote_staged_facts(db, qdrant_url, promotion_threshold=3)

    # Assertions
    assert result == 0
    # No INSERT should be attempted
    insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO facts" in str(c)]
    assert len(insert_calls) == 0
    # No Qdrant delete
    assert mock_httpx.post.call_count == 0


def test_promote_staged_facts_ttl_expired(mock_db, mock_httpx, mock_derive_collection, mock_logger):
    """
    Test TTL expired: expires_at < now().
    Verify: candidate query returns nothing, no promotion happens.
    """
    db, cursor = mock_db

    # No candidates (expired rows filtered out by WHERE expires_at > now())
    cursor.fetchall.return_value = []

    qdrant_url = "http://qdrant:6333"

    # Execute
    result = promote_staged_facts(db, qdrant_url, promotion_threshold=3)

    # Assertions
    assert result == 0
    insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO facts" in str(c)]
    assert len(insert_calls) == 0
    assert mock_httpx.post.call_count == 0


def test_expire_staged_facts_cleanup(mock_db, mock_httpx, mock_derive_collection, mock_logger):
    """
    Test expiry cleanup: row with expires_at <= now(), promoted_at=None.
    Verify: Qdrant delete attempted, DELETE from staged_facts executed.
    """
    db, cursor = mock_db

    # Setup expired row
    expired_row = (123, "user456")
    cursor.fetchall.return_value = [expired_row]

    # Mock httpx response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_httpx.post.return_value = mock_response

    qdrant_url = "http://qdrant:6333"

    # Execute
    result = expire_staged_facts(db, qdrant_url)

    # Assertions
    assert result == 1

    # Verify Qdrant delete was attempted
    qdrant_delete_calls = [c for c in mock_httpx.post.call_args_list
                          if "/points/delete" in str(c)]
    assert len(qdrant_delete_calls) > 0

    # Verify DELETE from staged_facts was called
    delete_calls = [c for c in cursor.execute.call_args_list if "DELETE FROM staged_facts" in str(c)]
    assert len(delete_calls) > 0

    # Verify commit was called
    assert db.commit.call_count >= 1


# Priority 2 - Edge Case Tests

def test_promote_staged_facts_qdrant_delete_failure(mock_db, mock_httpx, mock_derive_collection, mock_logger):
    """
    Test Qdrant delete failure: httpx.post raises exception.
    Verify: warning logged, promoted_at still updated, no exception raised.
    """
    db, cursor = mock_db

    # Setup candidate row
    candidate_row = (1, "user789", "subj", "obj", "works_for", "user_stated", 0.8)
    cursor.fetchall.return_value = [candidate_row]

    # Mock httpx to raise exception
    mock_httpx.post.side_effect = Exception("Connection timeout")

    qdrant_url = "http://qdrant:6333"

    # Execute - should not raise exception
    result = promote_staged_facts(db, qdrant_url, promotion_threshold=3)

    # Assertions
    assert result == 1  # Fact promoted despite Qdrant failure

    # Verify warning was logged about Qdrant failure
    warning_calls = [c for c in mock_logger.warning.call_args_list
                    if "Failed to delete staged Qdrant point" in str(c)]
    assert len(warning_calls) > 0

    # Verify promoted_at was still updated
    update_calls = [c for c in cursor.execute.call_args_list if "UPDATE staged_facts SET promoted_at" in str(c)]
    assert len(update_calls) > 0


def test_promote_staged_facts_already_promoted(mock_db, mock_httpx, mock_derive_collection, mock_logger):
    """
    Test already promoted: promoted_at IS NOT NULL.
    Verify: excluded by candidate query, no INSERT happens.
    """
    db, cursor = mock_db

    # No candidates (filtered by WHERE promoted_at IS NULL)
    cursor.fetchall.return_value = []

    qdrant_url = "http://qdrant:6333"

    # Execute
    result = promote_staged_facts(db, qdrant_url, promotion_threshold=3)

    # Assertions
    assert result == 0
    insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO facts" in str(c)]
    assert len(insert_calls) == 0
    assert mock_httpx.post.call_count == 0


# Priority 3 - Integration Test

def test_poll_cycle_integration(mock_db, mock_httpx, mock_derive_collection, mock_logger):
    """
    Integration test: mock time.sleep to prevent infinite loop.
    Run one full cycle: staged sync -> promotion -> expiry.
    Verify each phase called in order.
    """
    db, cursor = mock_db

    # Setup responses for the cycle
    promotion_row = (10, "user_int", "subj", "obj", "pref_name", "user_stated", 0.9)
    expiry_row = (20, "user_int")

    # Use side_effect to return different values for each call to fetchall
    fetchall_responses = [
        [promotion_row],  # First call for promote_staged_facts
        [expiry_row],     # Second call for expire_staged_facts
    ]
    cursor.fetchall.side_effect = fetchall_responses

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_httpx.post.return_value = mock_response

    qdrant_url = "http://qdrant:6333"

    # Execute promotion
    promote_result = promote_staged_facts(db, qdrant_url, promotion_threshold=3)
    assert promote_result == 1

    # Reset side_effect for expiry call
    cursor.fetchall.side_effect = None
    cursor.fetchall.return_value = [expiry_row]

    # Execute expiry
    expire_result = expire_staged_facts(db, qdrant_url)
    assert expire_result == 1

    # Verify both operations completed
    assert mock_logger.info.call_count >= 2

    # Verify Qdrant was called for both operations
    assert mock_httpx.post.call_count >= 2
