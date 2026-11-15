"""CATEGORY-COUNT GROUNDING via the GROWN GLiNER2 classification (``entities.entity_type``).

The cluster (LME a3838d2b): "How many charity events did I participate in …" gold=4, but the
members were captured as HEAD-typed instances — ``charity gala`` / ``charity walk`` / ``charity
5K``, each ``instance_of`` its OWN head noun, with NO shared "charity event"/"event" L4 node —
so exact-alias grounding of the category phrase resolves to NOTHING and the instance walk
FALSE-MISSES ("no charity events on record"). The growth-native signal is already present:
GLiNER2 typed each of those instances ``entity_type='Event'`` at ingest.

``_l4_direct_entity_type_count`` reads that grown classification — DISTINCT user-grounded
entities whose ``entity_type`` equals the category HEAD (the engine's OWN coarse type
vocabulary, not a domain word-list), SCOPED by the phrase MODIFIER (word-boundary alias match,
so "charity" keeps ``charity gala``/``charity auction`` and drops mis-typed ``hospital``/
``museum``). Deterministic; NO cosine/LLM/substring-fuzz. Wired MONOTONIC-MAX + modifier-
required so it can only LIFT a miss/undercount, never lower a real count nor over-count a bare
"how many events".

Pure-Python: the head/modifier split takes no DB; the entity_type resolver is exercised against
a MagicMock cursor. No network, no live services.
"""

from unittest.mock import MagicMock

import pytest

import src.api.main as main
from src.api.main import (
    _count_head_and_modifiers,
    _l4_direct_entity_type_count,
)

_CLASSIF = frozenset({"instance_of", "subclass_of", "is_a"})


# ── _count_head_and_modifiers (pure) ────────────────────────────────────────────
@pytest.mark.parametrize("phrase, head, mods", [
    ("charity events", "event", ["charity"]),
    ("charity event", "event", ["charity"]),
    ("art-related events", "event", ["art"]),          # relational connector stripped
    ("charity-themed galas", "gala", ["charity"]),      # connector 'themed' stripped
    ("different charity events", "event", ["charity"]),  # distinctness quantifier stripped
    ("types of citrus fruits", "fruit", ["citrus"]),     # 'types of' peeled, head singular
    ("events", "event", []),                            # BARE type label → no modifier
    ("tanks", "tank", []),
    ("", "", []),
])
def test_head_and_modifier_split(phrase, head, mods):
    assert _count_head_and_modifiers(phrase) == (head, mods)


# ── _l4_direct_entity_type_count (MagicMock DB) ─────────────────────────────────
def _mock_db(rows):
    db = MagicMock()
    cur = MagicMock()
    db.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = rows
    return db, cur


def test_modifier_scoped_entity_type_count_grounds_the_category():
    # "charity events": entity_type='Event' rows scoped to modifier 'charity' → 4 distinct.
    rows = [
        ("u-gala", "charity gala"),
        ("u-dance", "charity dance"),
        ("u-walk", "charity walk"),
        ("u-5k", "charity 5k"),
    ]
    db, cur = _mock_db(rows)
    res = _l4_direct_entity_type_count(db, "charity events", _CLASSIF)
    assert res["miss"] is False
    assert res["count"] == 4
    assert set(res["instance_ids"]) == {"u-gala", "u-dance", "u-walk", "u-5k"}
    # the SQL bound the head to the type label and used a word-boundary modifier regex.
    params = cur.execute.call_args[0][1]
    assert params["head"] == "event"
    assert params["modrx"] == r"\m(charity)\M"


def test_bare_type_label_declines_without_touching_db():
    # "how many events" → no modifier → miss, and the DB is never queried (no over-count).
    db, cur = _mock_db([("x", "some event")])
    res = _l4_direct_entity_type_count(db, "events", _CLASSIF)
    assert res["miss"] is True and res["count"] == 0
    cur.execute.assert_not_called()


def test_no_matching_entity_type_rows_is_a_clean_miss():
    db, cur = _mock_db([])   # no entity carries this type / modifier
    res = _l4_direct_entity_type_count(db, "charity events", _CLASSIF)
    assert res["miss"] is True and res["count"] == 0


def test_flag_off_is_a_no_op_miss(monkeypatch):
    # Flag OFF → helper never queries and returns miss → caller leaves recall byte-identical.
    monkeypatch.setattr(main, "COUNT_ENTITY_TYPE_MEMBERSHIP", False)
    db, cur = _mock_db([("u-gala", "charity gala")])
    res = _l4_direct_entity_type_count(db, "charity events", _CLASSIF)
    assert res["miss"] is True and res["count"] == 0
    cur.execute.assert_not_called()


def test_error_is_failsafe_miss():
    db = MagicMock()
    db.cursor.side_effect = RuntimeError("boom")
    res = _l4_direct_entity_type_count(db, "charity events", _CLASSIF)
    assert res["miss"] is True and res["count"] == 0


def test_art_related_multitoken_modifier_matches_on_core():
    # "art-related events" → modifier core 'art' → the regex matches an 'art' word boundary.
    rows = [("u-open", "art gallery opening"), ("u-exh", "art exhibit")]
    db, cur = _mock_db(rows)
    res = _l4_direct_entity_type_count(db, "art-related events", _CLASSIF)
    assert res["miss"] is False and res["count"] == 2
    params = cur.execute.call_args[0][1]
    assert params["head"] == "event"
    assert params["modrx"] == r"\m(art)\M"
