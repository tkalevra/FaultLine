"""
Phase 5 Query Endpoint Tests

Test the complete /query endpoint orchestration:
1. Resolve anchor (WHO?)
2. Determine path (WHAT?)
3. Fetch DB facts
4. Qdrant semantic search
5. Apply confidence gate
6. Convert to prose
7. Return QueryResponse with prose facts only (NO UUIDs/rel_type names)
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from src.api.models import QueryRequest, QueryResponse, ConversationMessage, QueryPath
from src.api.main import (
    resolve_anchor,
    determine_path,
    fetch_facts_from_anchor,
    apply_confidence_gate,
    qdrant_semantic_search,
    convert_to_prose
)


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures
# ─────────────────────────────────────────────────────────────────────────────

# ── The seams these tests ACTUALLY exercise ───────────────────────────────────────────────
# 1) TEMPLATES: convert_to_prose stopped reading rel_type templates from the passed cursor at
#    `5161ee7` — it resolves them from the per-tenant rel_type OVERLAY (main.py:34301). The
#    `mock_cursor.fetchall.return_value = [(rel, template)]` rows below were a DEAD SEAM.
# 2) `fetchone`: these mocks stubbed only `fetchall`, so `cur.fetchone()` returned a truthy
#    MagicMock — a mock DB that answers EVERY lookup with a MagicMock instead of None. That is
#    what broke the determine_path tests (a MagicMock landed in path.taxonomy_groups, then
#    main.py:31260 called .lower() on it → TypeError). A real empty DB returns None.
def _meta(**rows):
    return patch("src.api.main.rel_type_overlay.resolve_current", return_value=rows)


def _empty_cursor(mock_db):
    """A cursor for a DB that genuinely holds nothing (fetchone → None, fetchall → [])."""
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_cursor.fetchall.return_value = []
    mock_db.cursor.return_value.__enter__.return_value = mock_cursor
    return mock_cursor


@pytest.fixture
def user_id():
    return "user-uuid-12345"


@pytest.fixture
def aurora_uuid():
    return "aurora-uuid-67890"


@pytest.fixture
def mock_db():
    """Mock PostgreSQL connection"""
    return Mock()


@pytest.fixture
def conversation_history():
    """Sample conversation history for pronoun resolution tests"""
    return [
        ConversationMessage(
            role="user",
            content="My daughter Aurora is 12 years old",
            timestamp=datetime.now()
        ),
        ConversationMessage(
            role="assistant",
            content="That's wonderful! Aurora sounds delightful.",
            timestamp=datetime.now()
        ),
    ]


@pytest.fixture
def query_path_all():
    """QueryPath that fetches all details"""
    return QueryPath(
        scalar_rels=[],
        relationship_rels=[],
        taxonomy_groups=[],
        traversal_depth=1,
        fetch_all_details=True
    )


@pytest.fixture
def sample_db_facts():
    """Sample facts from database"""
    return [
        {
            "subject": "user-uuid",
            "object": "aurora-uuid",
            "rel_type": "child_of",
            "confidence": 1.0,
            "fact_class": "A",
            "source": "db"
        },
        {
            "subject": "user-uuid",
            "rel_type": "age",
            "object": "45",
            "confidence": 1.0,
            "fact_class": "A",
            "source": "attributes"
        }
    ]


@pytest.fixture
def sample_qdrant_facts():
    """Sample facts from Qdrant semantic search"""
    return [
        {
            "subject": "aurora-uuid",
            "object": "art",
            "rel_type": "likes",
            "confidence": 0.4,
            "fact_class": "C",
            "qdrant_score": 0.65,
            "source": "qdrant"
        }
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Test: Anchor Resolution
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_anchor_possessive(mock_db, user_id):
    """Test resolving anchor with possessive keyword"""
    result = resolve_anchor("my family", [], user_id, mock_db)
    assert result == user_id


def test_resolve_anchor_direct_match(user_id, aurora_uuid):
    """Test resolving anchor with direct entity name match"""
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (aurora_uuid,)
    mock_db.cursor.return_value.__enter__.return_value = mock_cursor

    result = resolve_anchor("Tell me about Aurora", [], user_id, mock_db)
    # Should resolve to Aurora UUID or default to user_id (depends on DB mock)
    assert result is not None


def test_resolve_anchor_default(user_id):
    """Test resolving anchor defaults to user_id"""
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_db.cursor.return_value.__enter__.return_value = mock_cursor

    result = resolve_anchor("unknown entity", [], user_id, mock_db)
    assert result == user_id


def test_resolve_anchor_no_db(user_id):
    """Test anchor resolution with no database connection"""
    result = resolve_anchor("my family", [], user_id, None)
    assert result == user_id


# ─────────────────────────────────────────────────────────────────────────────
# Test: Path Determination
# ─────────────────────────────────────────────────────────────────────────────

# ── determine_path against an EMPTY ontology ──────────────────────────────────────────────
# These three tests mock a DB whose rel_types/entity_taxonomies hold NOTHING, so no keyword can
# possibly resolve. The old assertions (`fetch_all_details or len(rels) > 0`) are unsatisfiable
# under that premise on the CURRENT contract, and they only ever "worked" because the mock left
# `fetchone` unstubbed — it returned a truthy MagicMock, so every keyword "matched" a taxonomy
# named <MagicMock>. That junk match is now a hard TypeError at main.py:31260.
#
# The DELIBERATE branch (main.py:31100, verbatim):
#     "fetch_all only when there is truly no signal — vague/empty query. If keywords existed but
#      matched nothing in the ontology (topic not yet in the knowledge graph), return the empty
#      path so Qdrant semantic search handles it without a DB fact flood."
# So: keywords ∧ no ontology match → EMPTY, UNSCOPED path (Qdrant's job).
#     no keywords at all          → fetch_all_details=True.
# Both branches are pinned below — stricter than the old `or` chain, and DB-independent.

def _assert_unscoped_no_flood(path):
    """Keywords present but nothing matched: empty path, no scope, and NO fetch-all flood."""
    assert isinstance(path, QueryPath)
    assert path.scalar_rels == []
    assert path.relationship_rels == []
    assert path.taxonomy_groups == []
    assert not path.scope_active        # nothing to project → scoping is inert
    assert not path.fetch_all_details   # and deliberately NOT a DB fact flood
    # Whatever lands in taxonomy_groups must be real taxonomy NAMES (strings) — the walk
    # lowercases them (main.py:31260). Pins the mock's honesty against the MagicMock fossil.
    assert all(isinstance(t, str) for t in path.taxonomy_groups)


def test_determine_path_scalar_query_unresolvable_against_empty_ontology():
    """A scalar query ('how old am i') whose rel is absent from the ontology → unscoped path."""
    mock_db = MagicMock()
    _empty_cursor(mock_db)  # empty DB: fetchone → None (see _empty_cursor note)

    path = determine_path("how old am i", mock_db)
    _assert_unscoped_no_flood(path)


def test_determine_path_relationship_query_unresolvable_against_empty_ontology():
    """A relationship query ('my spouse') whose rel is absent from the ontology → unscoped."""
    mock_db = MagicMock()
    _empty_cursor(mock_db)

    path = determine_path("tell me about my spouse", mock_db)
    _assert_unscoped_no_flood(path)


def test_determine_path_all_details_only_when_no_signal_keywords():
    """fetch_all_details fires ONLY for a query with no signal keywords at all.

    'tell me everything' keeps the keyword 'everything', which matches nothing → unscoped path.
    A query that is pure noise words has NO keywords → the genuine fetch-all fallback.
    """
    mock_db = MagicMock()
    _empty_cursor(mock_db)

    # Has a signal keyword ('everything') that matches nothing → no flood.
    path = determine_path("tell me everything", mock_db)
    _assert_unscoped_no_flood(path)

    # Pure noise ('tell me about my') → no keywords survive → fetch-all fallback fires.
    mock_db_2 = MagicMock()
    _empty_cursor(mock_db_2)
    vague = determine_path("tell me about my", mock_db_2)
    assert vague.fetch_all_details
    assert not vague.scope_active


# ─────────────────────────────────────────────────────────────────────────────
# Test: Confidence Gate
# ─────────────────────────────────────────────────────────────────────────────

def test_apply_confidence_gate_above_threshold():
    """Test confidence gate allows facts above threshold"""
    db_facts = [
        {"confidence": 1.0, "fact_class": "A"},  # Pass
        {"confidence": 0.8, "fact_class": "B"},  # Pass
    ]
    qdrant_facts = []

    gated = apply_confidence_gate(db_facts, qdrant_facts, min_confidence=0.4)
    assert len(gated) == 2
    assert all(f["confidence"] >= 0.4 for f in gated)


def test_apply_confidence_gate_never_floors_db_facts():
    """DB facts are NEVER floored by the confidence gate, however low their confidence.

    CONTRACT CHANGE (deliberate, `3837385b` "confidence floor applies to Qdrant results only —
    never floor staged/Class C DB facts", 2026-06-02 — POST-dating this file). The old
    expectation (a 0.3-confidence DB fact is BLOCKED) is precisely the behaviour that commit
    removed, and removing it is load-bearing: a DB fact was already validated and committed by
    the ingest pipeline, and a low-confidence Class C staged fact MUST surface so a recall hit
    can promote it (C→B at hit_count >= 3). Flooring it here would bury the promotion lane.

    `min_confidence` now floors QDRANT noise ONLY, and that floor is applied upstream in /query
    before this function is reached — so apply_confidence_gate itself filters nothing.
    """
    db_facts = [
        {"confidence": 0.3, "fact_class": "C"},  # Well below the floor — must still survive.
    ]
    qdrant_facts = []

    gated = apply_confidence_gate(db_facts, qdrant_facts, min_confidence=0.4)
    assert len(gated) == 1
    assert gated[0]["confidence"] == 0.3
    assert gated[0]["fact_class"] == "C"


def test_apply_confidence_gate_class_c_from_qdrant():
    """Test confidence gate returns Class C facts from Qdrant if contextually relevant"""
    db_facts = []
    qdrant_facts = [
        {"confidence": 0.4, "fact_class": "C", "qdrant_score": 0.65},  # Pass (contextual)
    ]

    gated = apply_confidence_gate(db_facts, qdrant_facts, min_confidence=0.4)
    assert len(gated) == 1
    assert gated[0]["fact_class"] == "C"


def test_apply_confidence_gate_ordering():
    """Test confidence gate orders facts by confidence DESC"""
    db_facts = [
        {"confidence": 0.6, "fact_class": "B"},
        {"confidence": 1.0, "fact_class": "A"},
        {"confidence": 0.8, "fact_class": "B"},
    ]
    qdrant_facts = []

    gated = apply_confidence_gate(db_facts, qdrant_facts, min_confidence=0.4)
    assert len(gated) == 3
    assert gated[0]["confidence"] == 1.0  # Class A first
    assert gated[1]["confidence"] == 0.8  # Then Class B high
    assert gated[2]["confidence"] == 0.6  # Then Class B low


# ─────────────────────────────────────────────────────────────────────────────
# Test: Prose Conversion
# ─────────────────────────────────────────────────────────────────────────────

def test_convert_to_prose_no_uuids():
    """Test prose conversion produces NO UUIDs in output"""
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("spouse", "{subject} is married to {object}"),
        ("age", "{subject} is {object} years old"),
    ]
    mock_db.cursor.return_value.__enter__.return_value = mock_cursor

    # Mock resolve_display_name
    with patch('src.api.main.resolve_display_name') as mock_resolve:
        mock_resolve.side_effect = lambda eid, db: {
            "user-uuid": "you",
            "marla-uuid": "marla",
        }.get(eid, "unknown")

        facts = [
            {"subject_id": "user-uuid", "object_id": "marla-uuid", "rel_type": "spouse", "fact_class": "A"},
        ]

        prose = convert_to_prose(facts, mock_db)

        assert len(prose) > 0
        for p in prose:
            # No UUIDs in output
            assert "uuid" not in p.lower()
            assert "-" not in p or "-" in p and not any(c in p for c in "0123456789abcdef")


def test_convert_to_prose_class_c_no_label_leak():
    """Class C facts must NOT carry a printed '[staged]' label.

    Per DESIGN-fact-realization-and-voice: stance (confidence-as-voice) is carried
    on the fact's fact_class field and shaped by the recall preamble, never printed
    as an internal label in the user-facing prose string.
    """
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("spouse", "X is married to Y"),
    ]
    mock_db.cursor.return_value.__enter__.return_value = mock_cursor

    with patch('src.api.main.resolve_display_name') as mock_resolve:
        mock_resolve.side_effect = lambda eid, db: {
            "user": "you",
            "marla": "marla",
        }.get(eid, "unknown")

        facts = [
            {"subject_id": "user", "object_id": "marla", "rel_type": "spouse", "fact_class": "C"},
        ]

        # No anchor passed → plain name resolution (back-compat path).
        prose = convert_to_prose(facts, mock_db)

        assert len(prose) > 0
        assert "[staged]" not in prose[0]
        assert "[Class" not in prose[0]


def test_convert_to_prose_missing_template_renders_via_label_fallback():
    """A rel_type with NO natural_language template is RENDERED, not dropped.

    CONTRACT CHANGE (deliberate, `ff7a7587` "convert_to_prose falls back to label not drop when
    natural_language missing", 2026-06-01 — POST-dating this file). "We don't forget": a novel
    rel whose phrasing has not been learned yet must still surface; re_embedder Job 7 backfills
    natural_language asynchronously. Dropping the fact — the old expectation — is exactly what
    that commit removed.

    The fallback is HONEST-NEUTRAL: de-snake the rel_type, fabricate NO grammar (no injected
    copula — that produced wrong renders like "you are favorite color teal").
    """
    mock_db = MagicMock()
    _empty_cursor(mock_db)

    facts = [
        {"subject_id": "user", "object_id": "obj", "rel_type": "unknown_rel", "fact_class": "A"},
    ]

    with _meta():  # empty overlay → no rel_types row anywhere (the orphan case)
        prose = convert_to_prose(facts, mock_db)

    assert len(prose) == 1
    assert prose[0] == "User unknown rel obj"
    # The raw snake_case rel_type token must never reach the reader verbatim.
    assert "unknown_rel" not in prose[0]


# ─────────────────────────────────────────────────────────────────────────────
# Test: Complete Integration
# ─────────────────────────────────────────────────────────────────────────────

# ── QueryResponse.facts is list[dict], NOT list[str] ──────────────────────────────────────
# CONTRACT CHANGE (deliberate, `c57cc044` "restore backward compat to /query endpoint",
# 2026-05-26 — landed the SAME DAY as, and directly in response to, the commit that created
# this file). c31c2827 had made /query prose-only, which broke the Filter layer (it depends on
# fact_class / rel_type / metadata). The fix restored STRUCTURED facts and kept the prose on
# `facts[].definition`. So the prose these tests guard now lives in the `definition` key.
def _fact(definition, rel_type, fact_class="A", confidence=1.0):
    """A /query fact in its CURRENT shape: structured metadata + prose in `definition`."""
    return {"definition": definition, "rel_type": rel_type,
            "fact_class": fact_class, "confidence": confidence}


def test_query_response_structure():
    """Test QueryResponse has all required fields"""
    response = QueryResponse(
        anchor="user-uuid",
        facts=[
            _fact("You are married to Marla", "spouse"),
            _fact("You are 45 years old", "age"),
        ],
        confidence_applied=True,
        staged_facts_count=0,
        error=None
    )

    assert response.anchor == "user-uuid"
    assert len(response.facts) == 2
    assert response.confidence_applied is True
    assert response.staged_facts_count == 0
    assert response.error is None
    # The prose the user reads rides on `definition`; the Filter's metadata rides alongside.
    assert [f["definition"] for f in response.facts] == [
        "You are married to Marla", "You are 45 years old"]
    assert all("fact_class" in f and "rel_type" in f for f in response.facts)


def test_query_response_with_staged_facts():
    """Test QueryResponse includes staged facts count"""
    response = QueryResponse(
        anchor="user-uuid",
        facts=[
            _fact("You are married to Marla", "spouse", fact_class="A"),
            _fact("You like painting", "likes", fact_class="C", confidence=0.4),
        ],
        confidence_applied=True,
        staged_facts_count=1,
        error=None
    )

    assert response.staged_facts_count == 1
    # Stance is carried on fact_class and shaped by the recall preamble — it is NEVER printed
    # as a "[staged]" label in the prose (DESIGN-fact-realization-and-voice). The old fixture
    # hand-wrote "[staged] You like painting" into the prose; that label no longer exists.
    staged = [f for f in response.facts if f["fact_class"] == "C"]
    assert len(staged) == 1
    assert "[staged]" not in staged[0]["definition"]


def test_query_response_error_handling():
    """Test QueryResponse handles errors gracefully"""
    response = QueryResponse(
        anchor="",
        facts=[],
        confidence_applied=False,
        staged_facts_count=0,
        error="Database connection failed"
    )

    assert response.error is not None
    assert len(response.facts) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test: No UUID/rel_type Leakage
# ─────────────────────────────────────────────────────────────────────────────

def _leak_probe_response():
    """A QueryResponse in the CURRENT shape, carrying the prose a user would read."""
    return QueryResponse(
        anchor="user-uuid",
        facts=[
            _fact("You are married to Marla", "spouse"),
            _fact("Aurora is 12 years old", "age"),
            _fact("Fraggle is a dog", "has_pet", fact_class="C", confidence=0.4),
        ],
        confidence_applied=True,
        staged_facts_count=1,
        error=None
    )


def test_no_uuid_in_response():
    """Test CRITICAL CONSTRAINT: No UUIDs in the prose the user reads."""
    response = _leak_probe_response()

    # The constraint is on the PROSE (`definition`) — the structured metadata alongside it is
    # for the Filter, not the reader.
    uuid_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    import re
    for fact in response.facts:
        prose = fact["definition"]
        assert not re.search(uuid_pattern, prose), f"UUID found in fact: {prose}"


def test_no_rel_type_names_in_response():
    """Test CRITICAL CONSTRAINT: No rel_type names in the prose the user reads."""
    response = _leak_probe_response()

    # Check no rel_type names in the PROSE. Note the rel_type is deliberately CARRIED as a
    # structured key (`facts[].rel_type`) for the Filter — the contract is that it never leaks
    # into the reader-facing `definition` string.
    rel_types = ["spouse", "parent_of", "child_of", "has_pet", "instance_of", "works_for"]
    for fact in response.facts:
        prose = fact["definition"]
        for rel_type in rel_types:
            assert rel_type not in prose.lower(), f"rel_type '{rel_type}' found in fact: {prose}"
        # …and it IS present in the structure, which is where it belongs.
        assert fact["rel_type"]


# ─────────────────────────────────────────────────────────────────────────────
# Test: Promotion Loop Integration (Phase 6 interface)
# ─────────────────────────────────────────────────────────────────────────────

def test_staged_facts_visible_for_confirmation():
    """Test Class C facts are visible in response (for confirmation/promotion)"""
    response = QueryResponse(
        anchor="user-uuid",
        facts=[
            _fact("Aurora likes art", "likes", fact_class="C", confidence=0.4),
            _fact("Fraggle is shy", "has_state", fact_class="C", confidence=0.4),
        ],
        confidence_applied=True,
        staged_facts_count=2,
        error=None
    )

    assert response.staged_facts_count == 2
    # Both staged facts present for user to confirm (a recall hit is what promotes C→B).
    assert len(response.facts) == 2
    # Visible AND identifiable as Class C via the structured field — never via a "[staged]"
    # label printed into the prose.
    assert all(f["fact_class"] == "C" for f in response.facts)
    assert all("[staged]" not in f["definition"] for f in response.facts)


# ─────────────────────────────────────────────────────────────────────────────
# Main Test Execution
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
