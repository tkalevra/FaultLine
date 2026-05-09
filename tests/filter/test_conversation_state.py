"""Test suite for conversation state (Phase 5) — pronoun resolution + context tracking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "openwebui"))

from faultline_tool import (
    _resolve_pronouns,
    _update_conversation_context,
    _CONVERSATION_CONTEXT,
)


def setup_module():
    """Clear conversation context before tests."""
    _CONVERSATION_CONTEXT.clear()


def teardown_module():
    _CONVERSATION_CONTEXT.clear()


def test_pronoun_she_resolves_to_spouse():
    """'she' in query → spouse entity from prior context."""
    user_id = "test_user_1"
    facts = [
        {"subject": "user", "object": "marla_uuid", "rel_type": "spouse"},
    ]
    preferred_names = {"marla_uuid": "marla"}
    _update_conversation_context(user_id, facts, preferred_names)

    entities = _resolve_pronouns("what does she do?", user_id)
    assert "marla" in entities, f"Expected 'marla', got {entities}"


def test_pronoun_he_resolves_to_spouse():
    """'he' resolves same as 'she'."""
    user_id = "test_user_2"
    facts = [
        {"subject": "user", "object": "john_uuid", "rel_type": "spouse"},
    ]
    preferred_names = {"john_uuid": "john"}
    _update_conversation_context(user_id, facts, preferred_names)

    entities = _resolve_pronouns("what does he do?", user_id)
    assert "john" in entities


def test_pronoun_it_resolves_to_recent_entity():
    """'it' → most recent non-person entity."""
    user_id = "test_user_3"
    facts = [
        {"subject": "user", "object": "fraggle_uuid", "rel_type": "has_pet"},
    ]
    preferred_names = {"fraggle_uuid": "fraggle"}
    _update_conversation_context(user_id, facts, preferred_names)

    entities = _resolve_pronouns("how old is it?", user_id)
    assert "fraggle" in entities, f"Expected 'fraggle', got {entities}"


def test_they_resolves_to_family():
    """'they' → family member from context."""
    user_id = "test_user_4"
    facts = [
        {"subject": "user", "object": "des_uuid", "rel_type": "parent_of"},
    ]
    preferred_names = {"des_uuid": "des"}
    _update_conversation_context(user_id, facts, preferred_names)

    entities = _resolve_pronouns("how old are they?", user_id)
    assert "des" in entities


def test_pronoun_without_prior_mention_returns_empty():
    """No prior context → pronoun resolution returns empty."""
    user_id = "test_user_5"
    _CONVERSATION_CONTEXT.pop(user_id, None)
    entities = _resolve_pronouns("what is she doing?", user_id)
    assert len(entities) == 0, f"Expected empty, got {entities}"


def test_multiple_pronouns_in_one_turn():
    """'she and it' → both resolved."""
    user_id = "test_user_6"
    facts = [
        {"subject": "user", "object": "marla_uuid", "rel_type": "spouse"},
        {"subject": "user", "object": "fraggle_uuid", "rel_type": "has_pet"},
    ]
    preferred_names = {"marla_uuid": "marla", "fraggle_uuid": "fraggle"}
    _update_conversation_context(user_id, facts, preferred_names)

    entities = _resolve_pronouns("how are she and it doing?", user_id)
    assert "marla" in entities
    assert "fraggle" in entities


def test_context_prunes_old_mentions():
    """After 12 entities, oldest fall off, pronouns for old ones fail."""
    user_id = "test_user_7"
    for i in range(12):
        facts = [
            {"subject": "user", "object": f"entity_{i}_uuid", "rel_type": "knows"},
        ]
        preferred_names = {f"entity_{i}_uuid": f"entity_{i}"}
        _update_conversation_context(user_id, facts, preferred_names)

    # Oldest entities should be gone
    ctx = _CONVERSATION_CONTEXT.get(user_id, {})
    mentions = ctx.get("entity_mentions", [])
    assert len(mentions) <= 10, f"Expected ≤10, got {len(mentions)}"


def test_context_isolated_per_user():
    """Different users have separate contexts."""
    _CONVERSATION_CONTEXT.clear()
    facts_a = [{"subject": "user", "object": "marla_uuid", "rel_type": "spouse"}]
    facts_b = [{"subject": "user", "object": "john_uuid", "rel_type": "spouse"}]
    preferred_a = {"marla_uuid": "marla"}
    preferred_b = {"john_uuid": "john"}

    _update_conversation_context("user_a", facts_a, preferred_a)
    _update_conversation_context("user_b", facts_b, preferred_b)

    entities_a = _resolve_pronouns("how is she?", "user_a")
    entities_b = _resolve_pronouns("how is he?", "user_b")

    assert "marla" in entities_a
    assert "john" in entities_b
    assert "john" not in entities_a
