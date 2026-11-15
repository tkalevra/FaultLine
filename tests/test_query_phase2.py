"""
Phase 2 Query Tests: DB Lookup & Confidence Gate

Tests for:
1. fetch_facts_from_anchor() — verify correct facts returned with confidence
2. apply_confidence_gate() — verify below-threshold facts excluded
3. apply_confidence_gate() — verify facts ordered by confidence DESC
"""

import pytest
import os
import psycopg2
import uuid
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock


# Import functions to test
from src.api.main import fetch_facts_from_anchor, apply_confidence_gate
from src.api.models import QueryPath


class TestFetchFactsFromAnchor:
    """Test fetch_facts_from_anchor() function."""

    def test_fetch_direct_facts(self):
        """Test fetching direct facts (anchor is subject)."""
        anchor_uuid = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        object_uuid = str(uuid.uuid4())

        # Mock psycopg2 connection and environment
        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
            with patch('src.api.main.psycopg2.connect') as mock_connect:
                mock_conn = MagicMock()
                mock_cursor = MagicMock()
                mock_connect.return_value.__enter__.return_value = mock_conn
                mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

                # Mock direct facts query
                direct_fact = (anchor_uuid, object_uuid, "spouse", 1.0, "A")
                mock_cursor.execute.side_effect = [None] * 4
                mock_cursor.fetchall.side_effect = [
                    [direct_fact],  # direct facts result
                    [],             # inverse facts result
                    [],             # scalar attributes result
                    [],             # staged facts result
                ]

                path = QueryPath(fetch_all_details=True)
                result = fetch_facts_from_anchor(anchor_uuid, user_id, path)

                # Verify direct fact returned
                assert len(result) == 1
                assert result[0]["subject"] == anchor_uuid
                assert result[0]["object"] == object_uuid
                assert result[0]["rel_type"] == "spouse"
                assert result[0]["confidence"] == 1.0
                assert result[0]["fact_class"] == "A"
                assert result[0]["source"] == "db"

    def test_fetch_inverse_facts(self):
        """Test fetching inverse facts (anchor is object)."""
        anchor_uuid = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        subject_uuid = str(uuid.uuid4())

        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
            with patch('src.api.main.psycopg2.connect') as mock_connect:
                mock_conn = MagicMock()
                mock_cursor = MagicMock()
                mock_connect.return_value.__enter__.return_value = mock_conn
                mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

                inverse_fact = (subject_uuid, anchor_uuid, "child_of", 1.0, "A")
                mock_cursor.execute.side_effect = [None] * 4
                mock_cursor.fetchall.side_effect = [
                    [],                 # direct facts result
                    [inverse_fact],     # inverse facts result
                    [],                 # scalar attributes result
                    [],                 # staged facts result
                ]

                path = QueryPath(fetch_all_details=True)
                result = fetch_facts_from_anchor(anchor_uuid, user_id, path)

                # Verify inverse fact returned
                assert len(result) == 1
                assert result[0]["subject"] == subject_uuid
                assert result[0]["object"] == anchor_uuid
                assert result[0]["rel_type"] == "child_of"

    def test_fetch_scalar_attributes(self):
        """Test fetching scalar attributes."""
        anchor_uuid = str(uuid.uuid4())
        user_id = str(uuid.uuid4())

        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
            with patch('src.api.main.psycopg2.connect') as mock_connect:
                mock_conn = MagicMock()
                mock_cursor = MagicMock()
                mock_connect.return_value.__enter__.return_value = mock_conn
                mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

                # Scalar attribute: (entity_id, attribute, value_text, value_int, ...)
                scalar_attr = (anchor_uuid, "age", "12", None, None, None)
                mock_cursor.execute.side_effect = [None] * 4
                mock_cursor.fetchall.side_effect = [
                    [],              # direct facts result
                    [],              # inverse facts result
                    [scalar_attr],   # scalar attributes result
                    [],              # staged facts result
                ]

                path = QueryPath(fetch_all_details=True)
                result = fetch_facts_from_anchor(anchor_uuid, user_id, path)

                # Verify scalar attribute returned
                assert len(result) == 1
                assert result[0]["subject"] == anchor_uuid
                assert result[0]["rel_type"] == "age"
                assert result[0]["object"] == "12"
                assert result[0]["confidence"] == 1.0
                assert result[0]["fact_class"] == "A"
                assert result[0]["source"] == "attributes"

    def test_fetch_staged_facts(self):
        """Test fetching staged facts (Class B + C)."""
        anchor_uuid = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        object_uuid = str(uuid.uuid4())
        expires_at = datetime.now() + timedelta(days=30)

        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
            with patch('src.api.main.psycopg2.connect') as mock_connect:
                mock_conn = MagicMock()
                mock_cursor = MagicMock()
                mock_connect.return_value.__enter__.return_value = mock_conn
                mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

                staged_fact = (anchor_uuid, object_uuid, "has_pet", 0.6, "B", expires_at)
                mock_cursor.execute.side_effect = [None] * 4
                mock_cursor.fetchall.side_effect = [
                    [],             # direct facts result
                    [],             # inverse facts result
                    [],             # scalar attributes result
                    [staged_fact],  # staged facts result
                ]

                path = QueryPath(fetch_all_details=True)
                result = fetch_facts_from_anchor(anchor_uuid, user_id, path)

                # Verify staged fact returned
                assert len(result) == 1
                assert result[0]["subject"] == anchor_uuid
                assert result[0]["object"] == object_uuid
                assert result[0]["rel_type"] == "has_pet"
                assert result[0]["confidence"] == 0.6
                assert result[0]["fact_class"] == "B"
                assert result[0]["source"] == "staged"

    def test_fetch_scalar_rels_only(self):
        """Test path.scalar_rels filtering."""
        anchor_uuid = str(uuid.uuid4())
        user_id = str(uuid.uuid4())

        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
            with patch('src.api.main.psycopg2.connect') as mock_connect:
                mock_conn = MagicMock()
                mock_cursor = MagicMock()
                mock_connect.return_value.__enter__.return_value = mock_conn
                mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

                scalar_attr = (anchor_uuid, "age", "12", None, None, None)
                mock_cursor.execute.side_effect = [None] * 3  # Only 3 queries when not fetch_all_details
                mock_cursor.fetchall.side_effect = [
                    [],              # scalar attributes result
                    [],              # staged facts result
                ]

                # Only scalar rels in path
                path = QueryPath(scalar_rels=["age"])
                result = fetch_facts_from_anchor(anchor_uuid, user_id, path)

                # Verify it queried at least (staged facts is always queried)
                assert mock_cursor.execute.called

    def test_fetch_with_taxonomy_groups(self):
        """Test that taxonomy_groups are queried."""
        anchor_uuid = str(uuid.uuid4())
        user_id = str(uuid.uuid4())

        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
            with patch('src.api.main.psycopg2.connect') as mock_connect:
                mock_conn = MagicMock()
                mock_cursor = MagicMock()
                mock_connect.return_value.__enter__.return_value = mock_conn
                mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

                # Mock queries — taxonomy queries should execute
                mock_cursor.execute.return_value = None
                mock_cursor.fetchall.return_value = []

                path = QueryPath(taxonomy_groups=["family"])
                result = fetch_facts_from_anchor(anchor_uuid, user_id, path)

                # Verify taxonomy query was called
                # With only taxonomy_groups (no fetch_all_details), should call:
                # 1) staged facts, 2) taxonomy members query
                # (taxonomy attributes only query if member_entities found)
                assert mock_cursor.execute.call_count >= 2

    def test_no_dsn_returns_empty(self):
        """Test that missing DSN returns empty list."""
        with patch.dict(os.environ, {"POSTGRES_DSN": ""}):
            path = QueryPath(fetch_all_details=True)
            result = fetch_facts_from_anchor("any-uuid", "user", path)
            assert result == []

    def test_db_error_logged_and_returns_empty(self):
        """Test that DB errors are logged and return empty list."""
        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
            with patch('src.api.main.psycopg2.connect') as mock_connect:
                mock_connect.side_effect = Exception("DB connection failed")

                path = QueryPath(fetch_all_details=True)
                result = fetch_facts_from_anchor("any-uuid", "user", path)
                assert result == []


class TestApplyConfidenceGate:
    """Test apply_confidence_gate() function."""

    def test_confidence_gate_filters_below_threshold(self):
        """Test that facts below min_confidence are excluded."""
        db_facts = [
            {"subject": "user", "rel_type": "parent_of", "object": "child", "confidence": 1.0, "fact_class": "A"},
            {"subject": "user", "rel_type": "spouse", "object": "katya", "confidence": 0.8, "fact_class": "B"},
            {"subject": "user", "rel_type": "has_pet", "object": "fido", "confidence": 0.3, "fact_class": "C"},  # Below threshold
        ]

        result = apply_confidence_gate(db_facts, [], min_confidence=0.4)

        # Should have 2 facts (A + B pass, C fails)
        assert len(result) == 2
        assert result[0]["confidence"] == 1.0  # Class A first
        assert result[1]["confidence"] == 0.8  # Class B second

    def test_confidence_gate_orders_by_confidence_desc(self):
        """Test that results are ordered by confidence DESC."""
        db_facts = [
            {"subject": "user", "rel_type": "has_pet", "object": "fido", "confidence": 0.6, "fact_class": "B"},
            {"subject": "user", "rel_type": "parent_of", "object": "child", "confidence": 1.0, "fact_class": "A"},
            {"subject": "user", "rel_type": "spouse", "object": "katya", "confidence": 0.8, "fact_class": "B"},
        ]

        result = apply_confidence_gate(db_facts, [], min_confidence=0.4)

        # Verify DESC order: 1.0, 0.8, 0.6
        assert result[0]["confidence"] == 1.0
        assert result[1]["confidence"] == 0.8
        assert result[2]["confidence"] == 0.6

    def test_class_c_from_qdrant_contextually_relevant(self):
        """Test that Class C from Qdrant is returned if qdrant_score >= 0.3."""
        db_facts = [
            {"subject": "user", "rel_type": "parent_of", "object": "child", "confidence": 1.0, "fact_class": "A"},
        ]
        qdrant_facts = [
            {
                "subject": "user", "rel_type": "has_pet", "object": "fido", "confidence": 0.4, "fact_class": "C",
                "qdrant_score": 0.35  # Contextually relevant (>= 0.3)
            }
        ]

        result = apply_confidence_gate(db_facts, qdrant_facts, min_confidence=0.4)

        # Both should be returned
        assert len(result) == 2
        # Class A should be first
        assert result[0]["confidence"] == 1.0
        # Class C from Qdrant should be second
        assert result[1]["confidence"] == 0.4
        assert result[1]["fact_class"] == "C"

    def test_class_c_from_qdrant_not_relevant(self):
        """Test that Class C from Qdrant is excluded if qdrant_score < 0.3."""
        db_facts = [
            {"subject": "user", "rel_type": "parent_of", "object": "child", "confidence": 1.0, "fact_class": "A"},
        ]
        qdrant_facts = [
            {
                "subject": "user", "rel_type": "has_pet", "object": "fido", "confidence": 0.4, "fact_class": "C",
                "qdrant_score": 0.25  # Not relevant (< 0.3)
            }
        ]

        result = apply_confidence_gate(db_facts, qdrant_facts, min_confidence=0.4)

        # Only DB fact should be returned
        assert len(result) == 1
        assert result[0]["confidence"] == 1.0

    def test_class_b_from_qdrant_uses_threshold(self):
        """Test that Class B from Qdrant uses standard threshold."""
        db_facts = []
        qdrant_facts = [
            {
                "subject": "user", "rel_type": "spouse", "object": "katya", "confidence": 0.8, "fact_class": "B",
                "qdrant_score": 0.5
            },
            {
                "subject": "user", "rel_type": "has_pet", "object": "fido", "confidence": 0.3, "fact_class": "B",
                "qdrant_score": 0.9  # High qdrant score but low confidence
            }
        ]

        result = apply_confidence_gate(db_facts, qdrant_facts, min_confidence=0.4)

        # Only first (0.8) should pass threshold
        assert len(result) == 1
        assert result[0]["confidence"] == 0.8

    def test_none_qdrant_facts_handled(self):
        """Test that None qdrant_facts is handled gracefully."""
        db_facts = [
            {"subject": "user", "rel_type": "parent_of", "object": "child", "confidence": 1.0, "fact_class": "A"},
        ]

        result = apply_confidence_gate(db_facts, None, min_confidence=0.4)

        assert len(result) == 1
        assert result[0]["confidence"] == 1.0

    def test_empty_facts_lists(self):
        """Test that empty fact lists return empty results."""
        result = apply_confidence_gate([], [], min_confidence=0.4)
        assert result == []

    def test_default_confidence_missing(self):
        """Test that missing confidence defaults to 0.4."""
        db_facts = [
            {"subject": "user", "rel_type": "parent_of", "object": "child", "fact_class": "A"},
            # No confidence key — should default to 0.4
        ]

        result = apply_confidence_gate(db_facts, [], min_confidence=0.4)

        assert len(result) == 1
        # Fact with missing confidence should not be filtered (0.4 >= 0.4)
        assert result[0]["rel_type"] == "parent_of"

    def test_class_a_always_passes(self):
        """Test that Class A facts always pass even if confidence missing."""
        db_facts = [
            {"subject": "user", "rel_type": "parent_of", "object": "child", "fact_class": "A"},
            # No confidence key
        ]

        result = apply_confidence_gate(db_facts, [], min_confidence=0.4)

        assert len(result) == 1
        assert result[0]["fact_class"] == "A"

    def test_mixed_sources_merged_ordered(self):
        """Test that DB + Qdrant facts are merged and properly ordered."""
        db_facts = [
            {"subject": "user", "rel_type": "spouse", "object": "katya", "confidence": 0.8, "fact_class": "B"},
        ]
        qdrant_facts = [
            {
                "subject": "user", "rel_type": "parent_of", "object": "aurora", "confidence": 1.0, "fact_class": "A",
                "qdrant_score": 0.9
            },
            {
                "subject": "user", "rel_type": "has_pet", "object": "fido", "confidence": 0.4, "fact_class": "C",
                "qdrant_score": 0.35
            }
        ]

        result = apply_confidence_gate(db_facts, qdrant_facts, min_confidence=0.4)

        # All should pass
        assert len(result) == 3
        # Verify DESC order
        assert result[0]["confidence"] == 1.0
        assert result[1]["confidence"] == 0.8
        assert result[2]["confidence"] == 0.4


class TestIntegration:
    """Integration tests for Phase 2 functions working together."""

    def test_fetch_and_gate_pipeline(self):
        """Test that fetch_facts_from_anchor output works with apply_confidence_gate."""
        anchor_uuid = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        object_uuid = str(uuid.uuid4())

        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
            with patch('src.api.main.psycopg2.connect') as mock_connect:
                mock_conn = MagicMock()
                mock_cursor = MagicMock()
                mock_connect.return_value.__enter__.return_value = mock_conn
                mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

                # Return mix of confident and low-confidence facts
                facts_result = [
                    (anchor_uuid, object_uuid, "spouse", 1.0, "A"),
                    (anchor_uuid, object_uuid, "has_pet", 0.3, "C"),  # Below threshold
                ]
                mock_cursor.execute.side_effect = [None] * 4
                mock_cursor.fetchall.side_effect = [
                    facts_result,  # direct facts
                    [],            # inverse facts
                    [],            # scalar attributes
                    [],            # staged facts
                ]

                path = QueryPath(fetch_all_details=True)
                db_facts = fetch_facts_from_anchor(anchor_uuid, user_id, path)

                # Apply gate
                gated = apply_confidence_gate(db_facts, [], min_confidence=0.4)

                # Only 1 should pass (spouse 1.0)
                assert len(gated) == 1
                assert gated[0]["rel_type"] == "spouse"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
