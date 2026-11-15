"""
Canonical, deduplicated, preferred-name recall rendering tests.

Covers the four levers of the presentation/lean-query fix:
  1. One centralized display resolver (resolve_entity_display) — "you" vs preferred
     vs safe fallback; NEVER a non-preferred alias when a preferred one exists.
  2. Dedup on UUID before render — same-UUID DB+Qdrant copies collapse to one
     (the "you works for guelph" AND "chris works for guelph" duplication).
  3. Preferred-name facts kept and rendered cleanly (pref_name survives).
  4. Non-preferred (also_known_as) aliases held in reserve — gated on name-intent.
"""

from unittest.mock import MagicMock, patch

from src.api.main import (
    deduplicate_facts,
    resolve_entity_display,
    convert_to_prose,
    _query_has_name_intent,
)

USER_UUID = "10d7d879-63cd-4f31-92ce-f2c9edb760ab"
ORG_UUID = "43033395-0000-4000-8000-000000000001"


# ── Lever 1: one centralized resolver ────────────────────────────────────────

def test_resolve_entity_display_you_for_identity():
    """UUID in the identity set always renders as 'you' (perspective)."""
    out = resolve_entity_display(USER_UUID, {USER_UUID.lower(), "user"}, {}, MagicMock())
    assert out == "you"


def test_resolve_entity_display_prefers_preferred_map():
    """Preferred alias from the pre-fetched map wins (no DB round-trip needed)."""
    out = resolve_entity_display(ORG_UUID, {USER_UUID.lower()}, {ORG_UUID: "Guelph"}, MagicMock())
    assert out == "Guelph"


def test_resolve_entity_display_never_nonpreferred_when_preferred_exists():
    """When a preferred alias exists, a non-preferred alias must NEVER surface."""
    db = MagicMock()
    # DB preferred lookup returns the preferred alias first.
    cur = MagicMock()
    cur.fetchone.return_value = ("Chris",)
    db.cursor.return_value.__enter__.return_value = cur
    # No map entry → falls to DB preferred-first resolve_display_name path.
    out = resolve_entity_display(ORG_UUID, set(), None, db)
    assert out == "Chris"


def test_resolve_entity_display_none_is_safe():
    assert resolve_entity_display(None, set(), {}, MagicMock()) == "unknown"


# ── Lever 2: dedup on UUID, before render ────────────────────────────────────

def test_dedup_collapses_same_uuid_db_and_qdrant():
    """The duplication source: a DB copy (UUID slots) and its derived Qdrant copy
    of the SAME fact must collapse to ONE after the Qdrant lane is UUID-grounded.
    DB copy is kept (PostgreSQL-authoritative tiebreak)."""
    db_fact = {
        "subject": USER_UUID, "object": ORG_UUID, "rel_type": "works_for",
        "confidence": 0.8, "fact_class": "B", "source": "db",
    }
    # Qdrant copy AFTER UUID grounding carries the same UUIDs (was display names).
    qdrant_fact = {
        "subject": USER_UUID, "object": ORG_UUID, "rel_type": "works_for",
        "_subject_id": USER_UUID, "_object_id": ORG_UUID,
        "confidence": 0.4, "fact_class": "C", "source": "qdrant",
    }
    out = deduplicate_facts([db_fact, qdrant_fact])
    assert len(out) == 1
    # PostgreSQL authoritative: the surviving copy is the DB one.
    assert out[0]["source"] == "db"
    assert out[0]["fact_class"] == "B"


def test_dedup_keeps_distinct_uuid_facts():
    a = {"subject": USER_UUID, "object": ORG_UUID, "rel_type": "works_for", "source": "db"}
    b = {"subject": USER_UUID, "object": ORG_UUID, "rel_type": "knows", "source": "db"}
    assert len(deduplicate_facts([a, b])) == 2


def test_dedup_ungrounded_qdrant_would_not_collapse():
    """Sanity: this is exactly the bug the grounding step prevents. With a display
    name in the Qdrant slot (NOT grounded), the keys differ and dedup can't collapse
    — proving why UUID grounding must run before dedup."""
    db_fact = {"subject": USER_UUID, "object": ORG_UUID, "rel_type": "works_for", "source": "db"}
    ungrounded = {"subject": "chris", "object": "guelph", "rel_type": "works_for", "source": "qdrant"}
    # Two distinct keys → NOT collapsed (the pre-fix duplication).
    assert len(deduplicate_facts([db_fact, ungrounded])) == 2


# ── Lever 3: preferred-name facts kept and rendered ──────────────────────────

def test_pref_name_fact_rendered_not_dropped():
    """pref_name is the preferred-name MEMORY — it must render, not be filtered."""
    db = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = [("pref_name", "X's preferred name is Y")]
    db.cursor.return_value.__enter__.return_value = cur
    facts = [{"_subject_id": USER_UUID, "object": "Chris", "rel_type": "pref_name", "fact_class": "A"}]
    prose = convert_to_prose(
        facts, db, anchor=USER_UUID, user_id=USER_UUID,
        preferred_alias_map={USER_UUID: "Chris"},
    )
    assert len(prose) == 1
    assert "Chris" in prose[0]


# ── Lever 4: non-preferred aliases gated on name-intent ──────────────────────

def test_name_intent_detects_name_queries():
    assert _query_has_name_intent("what is my full name?")
    assert _query_has_name_intent("what is Chris actually called")
    assert _query_has_name_intent("what's his real name")
    assert _query_has_name_intent("is he also known as anything")
    assert _query_has_name_intent("what name does she go by")


def test_name_intent_ignores_ordinary_queries():
    assert not _query_has_name_intent("where do I work?")
    assert not _query_has_name_intent("tell me about my family")
    assert not _query_has_name_intent("how old is my dog")
    assert not _query_has_name_intent("")
