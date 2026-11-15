"""
Phase 3 Query Tests: Qdrant Semantic Search

Tests for:
1. qdrant_semantic_search() — verify Qdrant search returns facts
2. qdrant_collection_naming() — verify collection format "faultline-{user_id}"
3. qdrant_down_returns_empty() — verify graceful error handling
4. embedding_fails_returns_empty() — verify fallback on embed error
5. fact_class_preserved() — verify fact_class (A/B/C) in results
6. qdrant_score_attached() — verify qdrant_score in results
"""

import pytest
import os
import uuid
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
import httpx

# Import functions to test
from src.api.main import qdrant_semantic_search
from src.re_embedder.embedder import derive_collection


class TestQdrantSemanticSearch:
    """Test qdrant_semantic_search() function.

    THE REMOVAL (REMOVE_QDRANT_MEMORY_LANE, default ON): the user-memory Qdrant read lane
    is REMOVED (owner ruling 2026-11-14 — the vector store is for the cortex memory only).
    The legacy body survives verbatim behind the removal's early-exit as THE ROLLBACK ARM,
    so this class specs the legacy body against flag OFF; the removed contract (lane dark,
    zero contact, alerts gone) is pinned in test_removal_qdrant_memory_lane.py.
    """

    @pytest.fixture(autouse=True)
    def _rollback_arm(self):
        import src.api.main as _main
        with patch.object(_main, "REMOVE_QDRANT_MEMORY_LANE", False):
            yield

    def test_qdrant_search_returns_facts(self):
        """Test basic Qdrant search returns facts with required fields."""
        user_id = str(uuid.uuid4())
        query_text = "Tell me about my family"

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                # Mock embedding
                mock_embed.return_value = [0.1] * 768

                # Mock Qdrant response
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "result": [
                        {
                            "score": 0.85,
                            "payload": {
                                "subject": "Aurora",
                                "rel_type": "instance_of",
                                "object": "Person",
                                "confidence": 0.8,
                                "fact_class": "B"
                            }
                        },
                        {
                            "score": 0.72,
                            "payload": {
                                "subject": "Christopher",
                                "rel_type": "parent_of",
                                "object": "Aurora",
                                "confidence": 0.4,
                                "fact_class": "C"
                            }
                        }
                    ]
                }
                mock_post.return_value = mock_response

                results = qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    conversation_history=None,
                    qdrant_url="http://qdrant:6333"
                )

                # Verify results
                assert len(results) == 2
                assert results[0]["subject"] == "Aurora"
                assert results[0]["rel_type"] == "instance_of"
                assert results[0]["object"] == "Person"
                assert results[0]["confidence"] == 0.8
                assert results[0]["fact_class"] == "B"
                assert results[0]["qdrant_score"] == 0.85
                assert results[0]["source"] == "qdrant"

                # Verify second result
                assert results[1]["subject"] == "Christopher"
                assert results[1]["rel_type"] == "parent_of"
                assert results[1]["fact_class"] == "C"
                assert results[1]["qdrant_score"] == 0.72

    def test_qdrant_collection_naming(self):
        """Test that collection naming follows faultline-{user_id} format."""
        user_id = "test-user-12345"
        query_text = "Test query"

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"result": []}
                mock_post.return_value = mock_response

                qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333"
                )

                # Verify collection name in POST call
                call_args = mock_post.call_args
                expected_collection = derive_collection(user_id)
                assert expected_collection in call_args[0][0]
                assert f"/collections/{expected_collection}/points/search" in call_args[0][0]

    def test_qdrant_down_returns_empty(self):
        """Test graceful handling when Qdrant is unavailable."""
        user_id = str(uuid.uuid4())
        query_text = "Test query"

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768

                # Simulate Qdrant down (500 error)
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_post.return_value = mock_response

                results = qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333"
                )

                # Should return empty list, not crash
                assert results == []

    def test_embedding_fails_returns_empty(self):
        """Test graceful handling when embedding fails."""
        user_id = str(uuid.uuid4())
        query_text = "Test query"

        with patch("src.api.main.embed_text") as mock_embed:
            # Simulate embedding failure (returns None when fallback=False)
            mock_embed.return_value = None

            with patch("src.api.main.httpx.post") as mock_post:
                results = qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333"
                )

                # Should return empty list, not call Qdrant
                assert results == []
                assert not mock_post.called

    def test_conversation_history_included(self):
        """Test that conversation history is included in embedding context."""
        user_id = str(uuid.uuid4())
        query_text = "How old is she?"
        conversation_history = [
            {"role": "user", "content": "I have a daughter named Aurora"},
            {"role": "assistant", "content": "That's wonderful!"},
            {"role": "user", "content": "She's very smart"}
        ]

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"result": []}
                mock_post.return_value = mock_response

                qdrant_semantic_search(
                    query_text=query_text,
                    conversation_history=conversation_history,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333"
                )

                # Verify embed_text was called with combined context
                call_args = mock_embed.call_args
                embedded_text = call_args[0][0]

                # Should include both query and recent messages
                assert "How old is she?" in embedded_text
                assert "Aurora" in embedded_text
                assert "smart" in embedded_text

    def test_fact_class_preserved_in_results(self):
        """Test that fact_class (A/B/C) is preserved in results."""
        user_id = str(uuid.uuid4())
        query_text = "Test"

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768

                # Mock results with different fact classes
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "result": [
                        {
                            "score": 0.9,
                            "payload": {
                                "subject": "User",
                                "rel_type": "pref_name",
                                "object": "Christopher",
                                "confidence": 1.0,
                                "fact_class": "A"
                            }
                        },
                        {
                            "score": 0.8,
                            "payload": {
                                "subject": "Aurora",
                                "rel_type": "age",
                                "object": "12",
                                "confidence": 0.8,
                                "fact_class": "B"
                            }
                        },
                        {
                            "score": 0.5,
                            "payload": {
                                "subject": "Luna",
                                "rel_type": "instance_of",
                                "object": "Cat",
                                "confidence": 0.4,
                                "fact_class": "C"
                            }
                        }
                    ]
                }
                mock_post.return_value = mock_response

                results = qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333"
                )

                assert len(results) == 3
                assert results[0]["fact_class"] == "A"
                assert results[1]["fact_class"] == "B"
                assert results[2]["fact_class"] == "C"

    def test_qdrant_score_attached(self):
        """Test that qdrant_score (cosine similarity) is attached to results."""
        user_id = str(uuid.uuid4())
        query_text = "Test"

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "result": [
                        {
                            "score": 0.95,
                            "payload": {
                                "subject": "Aurora",
                                "rel_type": "child_of",
                                "object": "Christopher",
                                "confidence": 0.8,
                                "fact_class": "B"
                            }
                        }
                    ]
                }
                mock_post.return_value = mock_response

                results = qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333"
                )

                assert len(results) == 1
                assert results[0]["qdrant_score"] == 0.95
                assert results[0]["source"] == "qdrant"

    def test_score_threshold_respected(self):
        """Test that only results above score_threshold are returned."""
        user_id = str(uuid.uuid4())
        query_text = "Test"
        threshold = 0.6

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768

                # Qdrant handles filtering, we just verify parameter is passed
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"result": []}
                mock_post.return_value = mock_response

                qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333",
                    score_threshold=threshold
                )

                # Verify score_threshold was passed to Qdrant
                call_args = mock_post.call_args
                payload = call_args[1]["json"]
                assert payload["score_threshold"] == threshold

    def test_limit_respected(self):
        """Test that result limit is respected."""
        user_id = str(uuid.uuid4())
        query_text = "Test"
        limit = 5

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"result": []}
                mock_post.return_value = mock_response

                qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333",
                    limit=limit
                )

                # Verify limit was passed to Qdrant
                call_args = mock_post.call_args
                payload = call_args[1]["json"]
                assert payload["limit"] == limit

    def test_missing_payload_fields_handled(self):
        """Test graceful handling when payload fields are missing."""
        user_id = str(uuid.uuid4())
        query_text = "Test"

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768

                # Sparse payload (missing some fields)
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "result": [
                        {
                            "score": 0.8,
                            "payload": {
                                "subject": "Aurora",
                                "rel_type": "age"
                                # Missing object, confidence, fact_class
                            }
                        }
                    ]
                }
                mock_post.return_value = mock_response

                results = qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333"
                )

                # Should handle missing fields gracefully
                assert len(results) == 1
                assert results[0]["subject"] == "Aurora"
                assert results[0]["rel_type"] == "age"
                assert results[0]["object"] is None  # Missing field
                assert results[0]["confidence"] == 0.4  # Default
                assert results[0]["fact_class"] == "C"  # Default

    def test_empty_results_returned(self):
        """Test handling of empty Qdrant results."""
        user_id = str(uuid.uuid4())
        query_text = "Test"

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768

                # Empty result
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"result": []}
                mock_post.return_value = mock_response

                results = qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333"
                )

                assert results == []

    def test_network_error_returns_empty(self):
        """Test graceful handling of network errors."""
        user_id = str(uuid.uuid4())
        query_text = "Test"

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768

                # Simulate network error
                mock_post.side_effect = httpx.ConnectError("Connection refused")

                results = qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333"
                )

                # Should return empty list, not crash
                assert results == []

    def test_multiple_facts_ordered_preserved(self):
        """Test that fact order from Qdrant is preserved."""
        user_id = str(uuid.uuid4())
        query_text = "Test"

        with patch("src.api.main.embed_text") as mock_embed:
            with patch("src.api.main.httpx.post") as mock_post:
                mock_embed.return_value = [0.1] * 768

                # Multiple results in specific order
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "result": [
                        {"score": 0.95, "payload": {"subject": "A", "rel_type": "rel1"}},
                        {"score": 0.85, "payload": {"subject": "B", "rel_type": "rel2"}},
                        {"score": 0.75, "payload": {"subject": "C", "rel_type": "rel3"}},
                    ]
                }
                mock_post.return_value = mock_response

                results = qdrant_semantic_search(
                    query_text=query_text,
                    user_id=user_id,
                    qdrant_url="http://qdrant:6333"
                )

                # Verify order preserved
                assert len(results) == 3
                assert results[0]["subject"] == "A"
                assert results[1]["subject"] == "B"
                assert results[2]["subject"] == "C"
                assert results[0]["qdrant_score"] == 0.95
                assert results[1]["qdrant_score"] == 0.85
                assert results[2]["qdrant_score"] == 0.75
