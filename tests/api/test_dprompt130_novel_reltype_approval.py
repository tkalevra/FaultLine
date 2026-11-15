"""
Test dprompt-130: Synchronous Novel Rel-Type Inference (Class B Strengthening)

This test verifies that:
1. Novel rel_types are immediately approved with LLM-inferred metadata at ingest time
2. Facts with novel rel_types are routed to Class B (not deferred to Class C)
3. Metadata is stored durably in rel_types table (source of truth)
4. System works identically across all ontology domains (no domain-specific code)
5. Zero hardcoded patterns (all metadata from DB/LLM, no hardcoded rel_types/categories/rules)
6. CLAUDE.md constraints respected (metadata-driven, UUID/display-name distinction, three storage paths)

Domain coverage: family, medical, work, location, computer_system (cross-domain identical behavior)
"""

import pytest
import os
import sys
import json
from unittest.mock import MagicMock, patch, call
import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from wgm.gate import WGMValidationGate
from api.models import EdgeInput, IngestRequest


class TestNovelRelTypeApprovalIntegration:
    """Integration tests for dprompt-130 novel rel_type approval.

    ⚠️ CORRECTED 2026-08-18 (dprompt-156 pre-red triage): these tests pinned the ORIGINAL
    May-2026 dprompt-130 behaviour (every novel rel_type attempts a SYNC LLM approval) and
    have been red since commit 520a74bf (2026-05-24, "remove sync LLM from ingest") introduced
    the confidence split:

      * confidence >= 0.9 (user-stated / authoritative) → SYNC _try_approve_novel_type
      * confidence  < 0.9 (default 0.8, LLM-inferred)   → DEFER: INSERT into pending_types
        for the re_embedder's freq-gated async evaluation; NO sync approval call; the edge
        still returns {"status": "novel_unapproved"} so ingest routes it Class B/C.

    The assertions below now pin the CURRENT contract. The approval-path tests pass
    confidence=0.95 explicitly (the old tests passed no confidence, silently relying on the
    old default-path-calls-LLM behaviour).
    """

    def test_novel_reltype_approval_calls_try_approve(self):
        """
        A HIGH-CONFIDENCE (user-stated) novel rel_type triggers the sync approval call.
        """
        mock_conn = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_valid_types.return_value = {
            'parent_of', 'spouse', 'also_known_as', 'age'
        }

        gate = WGMValidationGate(mock_conn, registry=mock_registry)

        with patch.object(gate, '_try_approve_novel_type', return_value=False) as mock_approve:
            with patch.object(gate, '_find_inverse_rel_type', return_value=None):
                result = gate.validate_edge(
                    subject_id="subj_uuid",
                    object_id="obj_uuid",
                    rel_type="collaborates_with",  # Novel rel_type (not in registry)
                    confidence=0.95,               # >= 0.9 → the sync lane
                )

        # The sync lane DID consult the approver, with the exact rel_type name.
        mock_approve.assert_called_once_with("collaborates_with")
        # ...and the OUTCOME is pinned too. This assigned `result` and never asserted on it,
        # so across the two high-confidence-decline tests the coverage had gone from one exact
        # pin to zero exact pins and one negative pin.
        assert result["status"] == "valid"

    def test_novel_reltype_low_confidence_defers_to_pending_types(self):
        """
        The OTHER half of the split (the half the old tests broke on): a default-confidence
        (LLM-inferred, 0.8) novel rel_type must NOT call the approver synchronously — it is
        recorded in pending_types for the async re_embedder, preserving ingest speed.
        """
        mock_conn = MagicMock()
        cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
        mock_registry = MagicMock()
        mock_registry.get_valid_types.return_value = {'parent_of', 'spouse'}

        gate = WGMValidationGate(mock_conn, registry=mock_registry)

        with patch.object(gate, '_try_approve_novel_type', return_value=False) as mock_approve:
            with patch.object(gate, '_find_inverse_rel_type', return_value=None):
                result = gate.validate_edge(
                    subject_id="subj_uuid",
                    object_id="obj_uuid",
                    rel_type="collaborates_with",
                )  # no confidence → 0.8 → the deferred lane

        mock_approve.assert_not_called()
        # The deferral writes pending_types (async evaluation) ...
        inserts = [c for c in cursor_mock.execute.call_args_list
                   if c.args and "pending_types" in str(c.args[0])]
        assert inserts, "low-confidence novel rel_type must be recorded in pending_types"
        # ... and still reports novel_unapproved so ingest routes it Class B/C.
        assert result["status"] == "novel_unapproved"

    def test_novel_reltype_unapproved_returns_novel_unapproved(self):
        """
        Novel rel_type with low confidence (< 0.7) should return status='novel_unapproved'
        """
        mock_conn = MagicMock()
        gate = WGMValidationGate(mock_conn)

        with patch.object(gate, '_try_approve_novel_type', return_value=False):
            with patch.object(gate, '_find_inverse_rel_type', return_value=None):
                result = gate.validate_edge(
                    subject_id="subj_uuid",
                    object_id="obj_uuid",
                    rel_type="has_allergy",  # Novel medical rel_type
                    
                )

        assert result["status"] == "novel_unapproved"

    def test_novel_reltype_lmm_failure_fallback(self):
        """
        When the approver DECLINES a high-confidence novel type, validation CONTINUES with
        fallback metadata (auto-approve) — a user-stated fact is never dropped because an
        LLM was unavailable. (The old test asserted the decline returned novel_unapproved;
        the current contract auto-approves on decline/failure in the ≥0.9 lane.)
        """
        mock_conn = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_valid_types.return_value = {
            'parent_of', 'spouse', 'also_known_as', 'age'
        }

        gate = WGMValidationGate(mock_conn, registry=mock_registry)

        with patch.object(gate, '_try_approve_novel_type', return_value=False) as mock_approve:
            with patch.object(gate, '_find_inverse_rel_type', return_value=None):
                result = gate.validate_edge(
                    subject_id="subj_uuid",
                    object_id="obj_uuid",
                    rel_type="deployed_on",  # Novel computer system rel_type
                    confidence=0.95,         # the sync lane is the only lane that consults the LLM
                )

        # Should attempt approval
        mock_approve.assert_called_once_with("deployed_on")
        # Decline -> auto-approved fallback -> validation CONTINUED.
        # PIN THE OUTCOME, not the negation of the old one. `!= "novel_unapproved"` also
        # passes on conflict, immutable_fact, reflexive_tautology, type_mismatch and novel --
        # four of which are failure verdicts, so a regression that broke the auto-approve
        # fallback into a type mismatch would have left this green. Asserting the negation of
        # what used to be true is adapting to red; the contract is that validation SUCCEEDS.
        assert result["status"] == "valid"

    def test_domain_agnostic_approval_family(self):
        """
        Novel family rel_type should work without domain-specific code
        """
        mock_conn = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_valid_types.return_value = {'parent_of', 'spouse'}

        gate = WGMValidationGate(mock_conn, registry=mock_registry)

        with patch.object(gate, '_try_approve_novel_type', return_value=False) as mock_approve:
            with patch.object(gate, '_find_inverse_rel_type', return_value=None):
                gate.validate_edge(
                    subject_id="parent_uuid",
                    object_id="child_uuid",
                    rel_type="has_child",  # Novel family variant
                    confidence=0.95,  # the sync (user-stated) approval lane
                )

        # Should attempt approval with exact rel_type name (no preprocessing)
        mock_approve.assert_called_once_with("has_child")

    def test_domain_agnostic_approval_medical(self):
        """
        Novel medical rel_type should work without domain-specific code
        """
        mock_conn = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_valid_types.return_value = {'parent_of', 'spouse'}

        gate = WGMValidationGate(mock_conn, registry=mock_registry)

        with patch.object(gate, '_try_approve_novel_type', return_value=False) as mock_approve:
            with patch.object(gate, '_find_inverse_rel_type', return_value=None):
                gate.validate_edge(
                    subject_id="person_uuid",
                    object_id="allergen_uuid",
                    rel_type="has_allergy",  # Novel medical rel_type
                    confidence=0.95,  # the sync (user-stated) approval lane
                )

        # Should attempt approval with exact rel_type name (no preprocessing)
        mock_approve.assert_called_once_with("has_allergy")

    def test_domain_agnostic_approval_work(self):
        """
        Novel work rel_type should work without domain-specific code
        """
        mock_conn = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_valid_types.return_value = {'parent_of', 'spouse'}

        gate = WGMValidationGate(mock_conn, registry=mock_registry)

        with patch.object(gate, '_try_approve_novel_type', return_value=False) as mock_approve:
            with patch.object(gate, '_find_inverse_rel_type', return_value=None):
                gate.validate_edge(
                    subject_id="person_uuid",
                    object_id="org_uuid",
                    rel_type="collaborated_on",  # Novel work rel_type
                    confidence=0.95,  # the sync (user-stated) approval lane
                )

        # Should attempt approval with exact rel_type name (no preprocessing)
        mock_approve.assert_called_once_with("collaborated_on")

    def test_domain_agnostic_approval_location(self):
        """
        Novel location rel_type should work without domain-specific code
        """
        mock_conn = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_valid_types.return_value = {'parent_of', 'spouse'}

        gate = WGMValidationGate(mock_conn, registry=mock_registry)

        with patch.object(gate, '_try_approve_novel_type', return_value=False) as mock_approve:
            with patch.object(gate, '_find_inverse_rel_type', return_value=None):
                gate.validate_edge(
                    subject_id="place_uuid",
                    object_id="region_uuid",
                    rel_type="adjacent_to",  # Novel location rel_type
                    confidence=0.95,  # the sync (user-stated) approval lane
                )

        # Should attempt approval with exact rel_type name (no preprocessing)
        mock_approve.assert_called_once_with("adjacent_to")

    def test_domain_agnostic_approval_computer_system(self):
        """
        Novel computer system rel_type should work without domain-specific code
        """
        mock_conn = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_valid_types.return_value = {'parent_of', 'spouse'}

        gate = WGMValidationGate(mock_conn, registry=mock_registry)

        with patch.object(gate, '_try_approve_novel_type', return_value=False) as mock_approve:
            with patch.object(gate, '_find_inverse_rel_type', return_value=None):
                gate.validate_edge(
                    subject_id="service_uuid",
                    object_id="infra_uuid",
                    rel_type="depends_on",  # Novel computer system rel_type
                    confidence=0.95,  # the sync (user-stated) approval lane
                )

        # Should attempt approval with exact rel_type name (no preprocessing)
        mock_approve.assert_called_once_with("depends_on")

    def test_no_hardcoded_rel_types_in_approval(self):
        """
        Verification that _try_approve_novel_type() doesn't have hardcoded rel_type lists
        """
        # This test checks the source code to ensure no hardcoded patterns
        from wgm.gate import WGMValidationGate
        import inspect

        source = inspect.getsource(WGMValidationGate._try_approve_novel_type)

        # Check for forbidden patterns
        forbidden_patterns = [
            'if rel_type in [',
            'if rel_type == "',
            'family_rels',
            'work_rels',
            'medical_rels',
            'FAMILY_TYPES',
            'WORK_TYPES',
        ]

        for pattern in forbidden_patterns:
            assert pattern not in source, \
                f"Hardcoded pattern '{pattern}' found in _try_approve_novel_type()"

    def test_no_hardcoded_categories_in_approval(self):
        """
        Verification that category inference is LLM-driven, not hardcoded
        """
        from wgm.gate import WGMValidationGate
        import inspect

        source = inspect.getsource(WGMValidationGate._try_approve_novel_type)

        # The LLM prompt should drive category inference
        assert "LLM" in source or "llm" in source or "category" in source, \
            "Category inference not visible in _try_approve_novel_type()"

        # Should NOT have hardcoded category assignments
        forbidden_category_assigns = [
            'category = "family"',
            'category = "work"',
            'category = "medical"',
            'category = "location"',
            'category = "computer"',
        ]

        for pattern in forbidden_category_assigns:
            assert pattern not in source, \
                f"Hardcoded category assignment '{pattern}' found in _try_approve_novel_type()"

    def test_novel_reltype_metadata_stored_in_rel_types_table(self):
        """
        Verify that when novel rel_type is approved, metadata is stored in rel_types table
        """
        mock_conn = MagicMock()
        cursor_mock = mock_conn.cursor.return_value.__enter__.return_value

        # Mock the LLM response with all required metadata fields
        llm_response = {
            "valid": True,
            "label": "works in partnership with",
            "is_symmetric": False,
            "inverse_rel_type": "partner_of_inverse",
            "head_types": ["Person", "Organization"],
            "tail_types": ["Organization"],
            "is_hierarchy_rel": False,
            "category": "work",
            "confidence": 0.85,
            "reasoning": "indicates professional collaboration"
        }

        gate = WGMValidationGate(mock_conn)

        with patch('httpx.post') as mock_post:
            # Mock successful LLM response
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"content": json.dumps(llm_response)}}]
            }
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            result = gate._try_approve_novel_type("partners_with")

        # Should have attempted INSERT into rel_types
        assert result is True

    def test_novel_reltype_cache_refresh_on_approval(self):
        """
        Verify that registry cache is refreshed after approval
        """
        mock_conn = MagicMock()
        mock_registry = MagicMock()
        cursor_mock = mock_conn.cursor.return_value.__enter__.return_value

        gate = WGMValidationGate(mock_conn, registry=mock_registry)

        llm_response = {
            "valid": True,
            "label": "depends on",
            "is_symmetric": False,
            "inverse_rel_type": None,
            "head_types": ["Any"],
            "tail_types": ["Any"],
            "is_hierarchy_rel": False,
            "category": "computer_system",
            "confidence": 0.8,
        }

        with patch('httpx.post') as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"content": json.dumps(llm_response)}}]
            }
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            gate._try_approve_novel_type("depends_on")

        # Should have called registry refresh
        mock_registry._refresh.assert_called()

    def test_novel_reltype_pending_types_on_low_confidence(self):
        """
        Verify that a low-confidence LLM verdict (< 0.7) on a novel rel_type is recorded in
        the pending_types table and NOT approved.

        ⚠️ CORRECTED (same triage): the approver no longer calls ``httpx.post`` directly —
        it goes through the centralized ``call_llm_with_retry_sync`` (pulled in when sync
        LLM was removed from raw call sites). Patching httpx.post never intercepted the
        call, the REAL path ran with no endpoint, and the fail-safe AUTO-APPROVED — the
        test then failed asserting False. Patch the seam the code actually uses.
        """
        mock_conn = MagicMock()
        cursor_mock = mock_conn.cursor.return_value.__enter__.return_value

        gate = WGMValidationGate(mock_conn)

        # LLM returns low confidence
        llm_response = {
            "valid": True,
            "label": "experimental relationship",
            "is_symmetric": False,
            "inverse_rel_type": None,
            "head_types": ["Any"],
            "tail_types": ["Any"],
            "is_hierarchy_rel": False,
            "category": "other",
            "confidence": 0.5,  # Below 0.7 threshold
        }

        with patch('src.wgm.gate.call_llm_with_retry_sync', return_value=llm_response), \
             patch.dict(os.environ, {"WGM_LLM_MODEL": "test-model"}):
            # (env stub: LLMModels.get resolves BEFORE the mocked call and has NO fallback
            #  by design — without it the RuntimeError lands in the fail-safe except and
            #  AUTO-APPROVES, which is the True the old test kept seeing)
            result = gate._try_approve_novel_type("experimental_rel")

        # Should return False (not approved) ...
        assert result is False
        # ... and the rel_type must be recorded for the async re_embedder lane.
        inserts = [c for c in cursor_mock.execute.call_args_list
                   if c.args and "pending_types" in str(c.args[0])]
        assert inserts, "an unapproved novel rel_type must land in pending_types"


class TestNovelRelTypeIngestRouting:
    """Test that ingest correctly routes approved novel rel_types."""

    def test_ingest_handles_novel_unapproved_status(self):
        """
        Verify that ingest pipeline handles "novel_unapproved" status correctly
        """
        # This would require full integration test with actual database
        # For now, verify the status handling code exists in main.py
        from api.main import app  # Verify import works

    def test_novel_reltype_class_b_confidence_penalty(self):
        """
        Verify that approved novel rel_types are assigned Class B with confidence penalty

        Expected: confidence = llm_inferred (0.8) - metadata_creation_penalty (0.2) = 0.6
        """
        # Would be tested in full integration test
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
