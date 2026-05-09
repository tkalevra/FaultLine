"""Test suite for display name resolution (Phase 3) — UUID → readable names."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "openwebui"))

from faultline_tool import _resolve_display_names


def test_uuid_resolved_to_display_name():
    """UUID in subject/object → display name from preferred_names."""
    facts = [
        {"subject": "54214459-3d2e-5ff5-8c6c-a541667d93aa", "object": "7e4bff75-706e-5feb-b8b5-f4ca1247fd3b", "rel_type": "has_pet"},
    ]
    preferred_names = {
        "54214459-3d2e-5ff5-8c6c-a541667d93aa": "mars",
        "7e4bff75-706e-5feb-b8b5-f4ca1247fd3b": "fraggle",
    }
    identity = "3f8e6836-72e3-43d4-bbc5-71fc8668b070"

    resolved = _resolve_display_names(facts, preferred_names, identity)
    assert resolved[0]["subject"] == "mars"
    assert resolved[0]["object"] == "fraggle"


def test_canonical_identity_uuid_resolved_to_user():
    """Canonical identity UUID → 'user'."""
    facts = [
        {"subject": "3f8e6836-72e3-43d4-bbc5-71fc8668b070", "object": "fraggle", "rel_type": "has_pet"},
    ]
    preferred_names = {}
    identity = "3f8e6836-72e3-43d4-bbc5-71fc8668b070"

    resolved = _resolve_display_names(facts, preferred_names, identity)
    assert resolved[0]["subject"] == "user"


def test_string_values_preserved():
    """Non-UUID string values pass through unchanged."""
    facts = [
        {"subject": "user", "object": "may 3rd", "rel_type": "born_on"},
    ]
    preferred_names = {}
    identity = "test_user"

    resolved = _resolve_display_names(facts, preferred_names, identity)
    assert resolved[0]["object"] == "may 3rd"


def test_missing_display_name_keeps_original():
    """UUID not in preferred_names → keeps UUID as-is."""
    facts = [
        {"subject": "unknown_uuid", "object": "fraggle", "rel_type": "has_pet"},
    ]
    preferred_names = {"fraggle_uuid": "fraggle"}
    identity = "test_user"

    resolved = _resolve_display_names(facts, preferred_names, identity)
    assert resolved[0]["subject"] == "unknown_uuid"


def test_multiple_facts_all_resolved():
    """All facts in list get resolved."""
    facts = [
        {"subject": "uuid_mars", "object": "uuid_fraggle", "rel_type": "has_pet"},
        {"subject": "uuid_user", "object": "uuid_mars", "rel_type": "spouse"},
    ]
    preferred_names = {"uuid_mars": "mars", "uuid_fraggle": "fraggle"}
    identity = "uuid_user"

    resolved = _resolve_display_names(facts, preferred_names, identity)
    assert resolved[0]["subject"] == "mars"
    assert resolved[0]["object"] == "fraggle"
    assert resolved[1]["subject"] == "user"
    assert resolved[1]["object"] == "mars"
