"""Test suite for relation resolver (Phase 4-5) — seed + dynamic entity extraction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "openwebui"))

from faultline_tool import _extract_query_entities


def test_direct_token_match():
    """Tier 1a: query token matches display name directly."""
    preferred_names = {"uuid1": "marla", "uuid2": "fraggle", "uuid3": "des"}
    entities = _extract_query_entities("how is marla?", preferred_names)
    assert "marla" in entities


def test_case_insensitive_match():
    """Token matching is case-insensitive."""
    preferred_names = {"uuid1": "Marla", "uuid2": "Fraggle"}
    entities = _extract_query_entities("How's MARLA?", preferred_names)
    assert "marla" in entities


def test_punctuation_stripped():
    """Trailing punctuation stripped from tokens."""
    preferred_names = {"uuid1": "marla"}
    entities = _extract_query_entities("how's marla?", preferred_names)
    assert "marla" in entities


def test_seed_wife_resolves_via_spouse():
    """Seed: 'wife' → spouse rel_type → lookup in facts."""
    preferred_names = {"marla_uuid": "marla", "fraggle_uuid": "fraggle"}
    facts = [
        {"subject": "user", "object": "marla_uuid", "rel_type": "spouse"},
        {"subject": "user", "object": "fraggle_uuid", "rel_type": "has_pet"},
    ]
    entities = _extract_query_entities("how's my wife?", preferred_names, facts=facts)
    assert "marla" in entities, f"Expected marla, got {entities}"


def test_seed_pet_resolves_via_has_pet():
    """Seed: 'pet' → has_pet rel_type."""
    preferred_names = {"marla_uuid": "marla", "fraggle_uuid": "fraggle"}
    facts = [
        {"subject": "user", "object": "fraggle_uuid", "rel_type": "has_pet"},
    ]
    entities = _extract_query_entities("tell me about my pet", preferred_names, facts=facts)
    assert "fraggle" in entities


def test_seed_son_resolves_via_parent_of():
    """Seed: 'son' → parent_of rel_type."""
    preferred_names = {"des_uuid": "des", "fraggle_uuid": "fraggle"}
    facts = [
        {"subject": "user", "object": "des_uuid", "rel_type": "parent_of"},
    ]
    entities = _extract_query_entities("how old is my son?", preferred_names, facts=facts)
    assert "des" in entities


def test_dynamic_domain_agnostic_resolution():
    """Dynamic: 'team' matches display name from any rel_type."""
    preferred_names = {"team_uuid": "team_alpha"}
    facts = [
        {"subject": "user", "object": "team_alpha", "rel_type": "manages"},
    ]
    entities = _extract_query_entities("how's my team?", preferred_names, facts=facts)
    assert "team_alpha" in entities


def test_fallback_when_relation_doesnt_exist():
    """No matching fact → returns empty, graceful."""
    preferred_names = {"marla_uuid": "marla"}
    facts = [
        {"subject": "user", "object": "marla_uuid", "rel_type": "spouse"},
    ]
    entities = _extract_query_entities("how's my boss?", preferred_names, facts=facts)
    assert len(entities) == 0, f"Expected empty, got {entities}"


def test_no_facts_returns_from_token_match_only():
    """When facts is None, only Tier 1a direct token match runs."""
    preferred_names = {"uuid1": "marla"}
    entities = _extract_query_entities("my wife marla?", preferred_names, facts=None)
    assert "marla" in entities
    assert "wife" not in entities  # not a display name, and no facts to resolve


def test_empty_query_returns_empty():
    """Empty query returns empty set."""
    entities = _extract_query_entities("", {"uuid1": "marla"})
    assert len(entities) == 0
