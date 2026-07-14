"""
Phase 4 Tests for Query Redesign: Natural Language Conversion

Tests cover:
1. Display name resolution (UUID → display name)
2. Prose conversion using rel_types.natural_language
3. Class C facts marked as [staged]
4. No UUIDs in prose output
5. Missing natural_language templates skipped gracefully
"""

import pytest
from uuid import uuid4
from unittest.mock import MagicMock, patch
import re

from src.api.main import resolve_display_name, convert_to_prose


# ── The seams convert_to_prose ACTUALLY reads ─────────────────────────────────────────────
# 1) TEMPLATES: convert_to_prose stopped reading rel_type templates from the passed cursor at
#    `5161ee7` — it resolves them from the per-tenant rel_type OVERLAY (main.py:34301). Every
#    `mock_cursor.fetchall.return_value = [(rel, template)]` below was therefore a DEAD SEAM:
#    silently ignored, so these tests asserted against whatever the real DB happened to hold.
#    Patch the REAL seam — which also makes them DB-independent.
# 2) `fetchone`: these mocks only ever stubbed `fetchall`, so `cur.fetchone()` returned a
#    truthy MagicMock — an "empty" mock DB that answers EVERY lookup with a MagicMock. That is
#    what produced junk like "User and a User named Marla" (the instance_of/named-type probe
#    "found" a MagicMock type). A real empty DB returns None; stub it honestly.
def _meta(**rows):
    return patch("src.api.main.rel_type_overlay.resolve_current", return_value=rows)


def _empty_cursor(mock_db):
    """A cursor for a DB that genuinely holds nothing (fetchone → None, fetchall → [])."""
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_cursor.fetchall.return_value = []
    mock_db.cursor.return_value.__enter__.return_value = mock_cursor
    return mock_cursor


SPOUSE = {"natural_language": "X is married to Y", "natural_language_2p": "You are married to Y",
          "tail_types": ["Person"], "label": "is married to", "is_symmetric": True}
LIKES = {"natural_language": "X likes Y", "natural_language_2p": "You like Y",
         "tail_types": ["ANY"], "label": "likes"}
PARENT_OF = {"natural_language": "X is the parent of Y", "natural_language_2p": None,
             "tail_types": ["Person"], "label": "is the parent of"}
AGE = {"natural_language": "X is Y years old", "natural_language_2p": "You are Y years old",
       "tail_types": ["SCALAR"], "label": "has age"}


class TestResolveDisplayName:
    """Test UUID → display name resolution."""

    def test_resolve_preferred_name(self):
        """Test: resolve_display_name returns preferred name from entity_aliases."""
        entity_id = str(uuid4())
        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_db.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock preferred name lookup
        mock_cursor.fetchone.return_value = ("Aurora",)

        result = resolve_display_name(entity_id, mock_db)

        assert result == "Aurora"
        mock_cursor.execute.assert_called_once()

    def test_resolve_nonpreferred_alias_fallback(self):
        """Test: fall back to non-preferred alias when preferred not found."""
        entity_id = str(uuid4())
        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_db.cursor.return_value.__enter__.return_value = mock_cursor

        # First call (preferred): no result
        # Second call (any alias): returns "aurora"
        mock_cursor.fetchone.side_effect = [None, ("aurora",)]

        result = resolve_display_name(entity_id, mock_db)

        assert result == "aurora"
        assert mock_cursor.execute.call_count == 2

    def test_resolve_uuid_fallback(self):
        """Test: return UUID itself if no alias found."""
        entity_id = str(uuid4())
        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_db.cursor.return_value.__enter__.return_value = mock_cursor

        # Both lookups return no results
        mock_cursor.fetchone.side_effect = [None, None]

        result = resolve_display_name(entity_id, mock_db)

        assert result == entity_id

    def test_resolve_user_special_case(self):
        """Test: 'user' entity returns 'user' string."""
        mock_db = MagicMock()

        result = resolve_display_name("user", mock_db)

        assert result == "user"
        mock_db.cursor.assert_not_called()

    def test_resolve_non_uuid_string(self):
        """Test: non-UUID strings returned as-is."""
        mock_db = MagicMock()

        result = resolve_display_name("Aurora", mock_db)

        assert result == "Aurora"
        mock_db.cursor.assert_not_called()

    def test_resolve_empty_string(self):
        """Test: empty string returns 'unknown'."""
        mock_db = MagicMock()

        result = resolve_display_name("", mock_db)

        assert result == "unknown"


class TestConvertToProse:
    """Test natural language conversion."""

    def test_prose_conversion_spouse(self):
        """Test: fact converted to prose using natural_language template."""
        user_uuid = str(uuid4())
        spouse_uuid = str(uuid4())

        fact = {
            "subject_id": user_uuid,
            "rel_type": "spouse",
            "object_id": spouse_uuid,
            "fact_class": "A"
        }

        mock_db = MagicMock()
        _empty_cursor(mock_db)

        # rel_types template now comes from the OVERLAY, not the cursor (see _meta note).
        with _meta(spouse=SPOUSE), patch("src.api.main.resolve_display_name") as mock_resolve:
            mock_resolve.side_effect = ["User", "Marla"]

            prose_list = convert_to_prose([fact], mock_db)

        assert len(prose_list) == 1
        assert "User" in prose_list[0]
        assert "Marla" in prose_list[0]
        assert "married" in prose_list[0].lower()

    def test_prose_class_c_no_label_leak(self):
        """Test: Class C facts do NOT carry a printed '[staged]' label.

        Per DESIGN-fact-realization-and-voice: stance (confidence-as-voice) is
        carried on the fact's fact_class field and shaped by the recall preamble.
        It must NOT leak an internal label into the user-facing prose string.
        """
        user_uuid = str(uuid4())
        obj_uuid = str(uuid4())

        fact = {
            "subject_id": user_uuid,
            "rel_type": "likes",
            "object_id": obj_uuid,
            "fact_class": "C"  # Staged/provisional
        }

        mock_db = MagicMock()
        _empty_cursor(mock_db)

        # Template via the OVERLAY (X/Y placeholders per natural_language contract).
        # NOTE: this test was passing only because the REAL DB happened to hold the same
        # `likes` template — the cursor rows it mocked were dead. Now it is DB-independent.
        with _meta(likes=LIKES), patch("src.api.main.resolve_display_name") as mock_resolve:
            mock_resolve.side_effect = ["User", "Art"]

            prose_list = convert_to_prose([fact], mock_db)

        assert len(prose_list) == 1
        assert "[staged]" not in prose_list[0]
        assert "[Class" not in prose_list[0]
        assert prose_list[0] == "User likes Art"

    def test_no_uuids_in_prose(self):
        """Test: UUIDs never appear in prose output."""
        user_uuid = str(uuid4())
        obj_uuid = str(uuid4())

        fact = {
            "subject_id": user_uuid,
            "rel_type": "age",
            "object_id": "12",
            "fact_class": "A"
        }

        mock_db = MagicMock()
        _empty_cursor(mock_db)

        with _meta(age=AGE), patch("src.api.main.resolve_display_name") as mock_resolve:
            mock_resolve.side_effect = ["User", "12"]

            prose_list = convert_to_prose([fact], mock_db)

        assert len(prose_list) == 1

        # Check no hex UUIDs (UUID pattern: 8-4-4-4-12 hex chars)
        uuid_pattern = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
        for prose in prose_list:
            assert not re.search(uuid_pattern, prose), f"UUID found in prose: {prose}"

    def test_missing_natural_language_renders_via_label_fallback(self):
        """Test: a rel_type with NO natural_language template is RENDERED, not dropped.

        CONTRACT CHANGE (deliberate, `ff7a7587` "convert_to_prose falls back to label not
        drop when natural_language missing" — 2026-06-01, POST-dating this file). "We don't
        forget": a brand-new rel whose phrasing hasn't been learned yet must still surface —
        re_embedder Job 7 backfills natural_language asynchronously. The old expectation
        (silently DROP the fact) is the behaviour that commit deliberately removed.

        The fallback is HONEST-NEUTRAL: it de-snakes the rel_type and fabricates NO grammar
        (no injected copula) — pinned below.
        """
        user_uuid = str(uuid4())
        obj_uuid = str(uuid4())

        fact = {
            "subject_id": user_uuid,
            "rel_type": "unknown_rel_type",
            "object_id": obj_uuid,
            "fact_class": "C"
        }

        mock_db = MagicMock()
        _empty_cursor(mock_db)

        # Empty overlay → no rel_types row anywhere for this rel (the orphan case).
        with _meta(), patch("src.api.main.resolve_display_name") as mock_resolve:
            mock_resolve.side_effect = ["User", "Widget"]

            prose_list = convert_to_prose([fact], mock_db)

        # Rendered, not dropped — and it did not crash.
        assert len(prose_list) == 1
        assert prose_list[0] == "User unknown rel type Widget"
        # The raw snake_case rel_type token must never reach the reader verbatim.
        assert "unknown_rel_type" not in prose_list[0]

    def test_template_format_error_skipped(self):
        """Test: template format mismatches are skipped gracefully."""
        user_uuid = str(uuid4())

        # Fact with only subject (template expects both subject and object)
        fact = {
            "subject_id": user_uuid,
            "rel_type": "age",
            "object_id": None,  # Missing object
            "fact_class": "A"
        }

        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_db.cursor.return_value.__enter__.return_value = mock_cursor

        # Template that requires Y
        mock_cursor.fetchall.return_value = [("age", "X is Y years old")]

        with patch("src.api.main.resolve_display_name") as mock_resolve:
            mock_resolve.side_effect = ["User", None]

            prose_list = convert_to_prose([fact], mock_db)

            # Should skip the fact due to format error
            # (None object in format string causes KeyError)

    def test_multiple_facts_mixed_classes(self):
        """Test: multiple facts converted, with Class C marked."""
        user_uuid = str(uuid4())
        spouse_uuid = str(uuid4())

        facts = [
            {
                "subject_id": user_uuid,
                "rel_type": "spouse",
                "object_id": spouse_uuid,
                "fact_class": "A"
            },
            {
                "subject_id": user_uuid,
                "rel_type": "likes",
                "object_id": str(uuid4()),
                "fact_class": "C"
            }
        ]

        mock_db = MagicMock()
        _empty_cursor(mock_db)

        # Templates via the OVERLAY (X/Y placeholders per natural_language contract).
        with _meta(spouse=SPOUSE, likes=LIKES), \
                patch("src.api.main.resolve_display_name") as mock_resolve:
            mock_resolve.side_effect = ["User", "Marla", "User", "Art"]

            prose_list = convert_to_prose(facts, mock_db)

        assert len(prose_list) == 2
        # Stance is no longer printed as a label — neither class carries "[staged]".
        assert "[staged]" not in prose_list[0]  # Class A
        assert "[staged]" not in prose_list[1]  # Class C (softened via preamble, not labeled)

    def test_empty_facts_list(self):
        """Test: empty facts list returns empty prose list."""
        mock_db = MagicMock()

        prose_list = convert_to_prose([], mock_db)

        assert prose_list == []
        mock_db.cursor.assert_not_called()

    def test_prose_order_by_confidence_implicit(self):
        """Test: prose list maintains input order (confidence sorting happens in /query)."""
        facts = [
            {"subject_id": str(uuid4()), "rel_type": "parent_of", "object_id": str(uuid4())},
            {"subject_id": str(uuid4()), "rel_type": "spouse", "object_id": str(uuid4())},
        ]

        mock_db = MagicMock()
        _empty_cursor(mock_db)

        with _meta(parent_of=PARENT_OF, spouse=SPOUSE), \
                patch("src.api.main.resolve_display_name") as mock_resolve:
            mock_resolve.side_effect = ["Alice", "Bob", "Chris", "Dana"]

            prose_list = convert_to_prose(facts, mock_db)

        assert len(prose_list) == 2
        # Order should match input facts order
        assert "parent" in prose_list[0].lower()
        assert "married" in prose_list[1].lower()


class TestProseIntegration:
    """Integration tests for prose conversion."""

    def test_family_facts_prose(self):
        """Test: family facts converted to readable prose."""
        user_uuid = "user"
        daughter_uuid = str(uuid4())

        fact = {
            "subject_id": user_uuid,
            "rel_type": "child_of",
            "object_id": user_uuid,  # Self reference for demo
            "fact_class": "A"
        }

        mock_db = MagicMock()
        _empty_cursor(mock_db)

        child_of = {"natural_language": "X is the child of Y", "natural_language_2p": None,
                    "tail_types": ["Person"], "label": "is the child of"}
        with _meta(child_of=child_of), \
                patch("src.api.main.resolve_display_name") as mock_resolve:
            mock_resolve.side_effect = ["Aurora", "User"]

            prose_list = convert_to_prose([fact], mock_db)

        assert len(prose_list) == 1
        assert "Aurora" in prose_list[0]
        assert "child" in prose_list[0].lower()

    def test_scalar_attribute_prose(self):
        """Test: scalar attributes converted correctly.

        ⚠️⚠️ THIS TEST IS RED AND IS *CORRECT*. DO NOT "FIX" IT BY RELAXING THE ASSERTION. ⚠️⚠️

        It has caught a REAL, SHIPPING PRODUCT BUG in user-facing recall prose — the exact
        THIRD-PERSON TWIN of the second-person bug fixed today in `17ebf0f`.

        Shape 4 of the structure-driven composer (`_compose_object_clause`, tail_types ==
        [SCALAR], main.py:34007) fires whenever the object is a scalar and OVERWRITES the prose
        the curated `natural_language` (3p) template already rendered correctly. `17ebf0f` added
        a guard for the 2p side ONLY:

            if subject_is_you and template_2p:   # ← 2p curated template wins
                return None

        There is NO equivalent guard for the 3p template, so every fact about someone OTHER than
        the user is still clobbered. Verified against the REAL seeded rel_types (public.rel_types):

            curated 3p template          SHIPPED prose (Shape 4 clobber)
            ---------------------------  ---------------------------------------
            X is Y years old          →  "Diane's age is 62"           (stiff)
            X was born on Y (date)    →  "Diane's born on is 1980-04-02"   ← UNGRAMMATICAL
            X is also known as Y      →  "Diane's also known as is Di"     ← UNGRAMMATICAL

        "Diane's born on is 1980-04-02" is verbatim the defect `17ebf0f` called a REAL PRODUCT
        BUG ("your born on is 1980-04-02") — it was only fixed for the user's OWN facts. And
        CLAUDE.md's own worked example ("my mother's birthday?") lands squarely on this path.

        The composer's stated purpose is to RESCUE scalar rels whose template is NULL (has_ip,
        has_email, … all have natural_language IS NULL in the seed) or whose label bakes the
        predicate in. A curated 3p template is, by the same contract `17ebf0f` applied to 2p, a
        COMPLETE sentence — the composer has nothing to add. The symmetric guard is the fix.
        """
        user_uuid = str(uuid4())

        fact = {
            "subject_id": user_uuid,
            "rel_type": "age",
            "object_id": "12",  # Scalar value
            "fact_class": "B"
        }

        mock_db = MagicMock()
        _empty_cursor(mock_db)

        with _meta(age=AGE), patch("src.api.main.resolve_display_name") as mock_resolve:
            # For scalar object, resolve_display_name should return "12"
            mock_resolve.side_effect = ["Aurora", "12"]

            prose_list = convert_to_prose([fact], mock_db)

        assert len(prose_list) == 1
        assert "Aurora" in prose_list[0]
        assert "12" in prose_list[0]
        # The curated 3p template ("X is Y years old") must be honoured, exactly as the curated
        # 2p template now is. Shipped behaviour: "Aurora's age is 12" (template discarded).
        assert "years old" in prose_list[0].lower()
