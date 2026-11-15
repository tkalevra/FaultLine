"""CATEGORY-COUNT via WordNet HYPONYMY over the user's captured surfaces (subsumption count).

The cluster (LME c4a1ceb8): "How many different types of citrus fruits have I used in my cocktail
recipes?" gold=3, where the user only ever said "orange bitters" / "lime juice" / "lemon slices".
The citrus fruit is the MODIFIER of a compound Object, never a standalone "citrus fruit" entity, so
neither the exact-alias walk nor the coarse ``entity_type`` membership resolves it (the members are
typed ``Object``, and no "citrus fruit" node exists) — recall FALSE-MISSES ("no citrus fruits on
record"). The growth-native signal is a LEXICAL FACT: WordNet knows ``orange``/``lemon``/``lime`` are
hyponyms of ``citrus fruit`` (``citrus.n.01``).

``_l4_hyponym_membership_count`` resolves the category to its WordNet hyponym set and counts the
DISTINCT member-types present at a WORD BOUNDARY in the user's grounded aliases — the sanctioned
"count resolver walks the hypernym membership" option (subsumption count ``|{x : x is-a* category}|``;
Baader et al. DL Handbook; hyponymy = WordNet's ``~`` pointer, Miller 1995 / Fellbaum 1998).
Deterministic + OFFLINE; NO cosine/LLM/substring-fuzz; independent of the L4_WORDNET_LADDER capture
flag (a pure query-time read over surfaces already captured). Wired MONOTONIC-MAX so it can only LIFT
a miss/undercount, never lower a real count; a GENERIC category (thing/object) yields no hyponym set
→ miss (no over-count).

FAIL-ON-OLD: with ``COUNT_HYPONYM_MEMBERSHIP`` off the helper no-ops to a miss (byte-identical to
pre-fix behaviour) → the count would remain a false-miss; ON it grounds to 3.

Pure: the WordNet hyponym lookup is offline; the DB read is exercised against a MagicMock cursor.
No network, no live services. Requires the NLTK WordNet corpus (present in the dev/test env).
"""

from unittest.mock import MagicMock

import pytest

import src.api.main as main
from src.api.wordnet_ladder import category_hyponym_lemmas

_CLASSIF = frozenset({"instance_of", "subclass_of", "is_a"})


# ── WordNet hyponym resolution (offline lexical fact) ───────────────────────────
def test_citrus_fruit_resolves_its_members():
    mem = category_hyponym_lemmas("citrus fruit")
    # The three the haystack user actually used, plus siblings WordNet subsumes.
    assert {"orange", "lemon", "lime"} <= mem
    assert "grapefruit" in mem


def test_generic_category_yields_no_members_no_overcount():
    # A generic upper-ontology category carries no signal and its closure is the whole lexicon:
    # it MUST return an empty set so "how many things do I have" can never over-count.
    assert category_hyponym_lemmas("thing") == set()
    assert category_hyponym_lemmas("object") == set()
    assert category_hyponym_lemmas("") == set()


def test_cuisines_are_not_wordnet_hyponyms_documented_boundary():
    # HONEST SCOPE: ethnic cuisines (Ethiopian/Indian/Korean) are noun.person demonyms, NOT
    # hyponyms of cuisine.n.01 (whose hyponyms are dim_sum/haute_cuisine/…). This fix does NOT
    # cover the cuisine cluster — that needs a separate demonym→cuisine mechanism.
    mem = category_hyponym_lemmas("cuisine")
    assert "ethiopian" not in mem and "korean" not in mem and "indian" not in mem


# ── _l4_hyponym_membership_count (MagicMock DB) ─────────────────────────────────
def _mock_db(rows):
    db = MagicMock()
    cur = MagicMock()
    db.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = rows
    return db, cur


def test_citrus_count_grounds_to_three_over_buried_modifiers():
    # The user's captured surfaces — the fruit is a buried modifier in each compound Object.
    rows = [
        ("own orange bitters",),
        ("orange peels",),
        ("lime juice",),
        ("lemon slices",),
        ("cucumber gimlet",),   # NOT a citrus member → not counted
    ]
    db, _ = _mock_db(rows)
    res = main._l4_hyponym_membership_count(db, "different types of citrus fruits", _CLASSIF)
    assert res["miss"] is False
    assert res["count"] == 3
    assert res["instance_names"] == ["lemon", "lime", "orange"]


def test_distinct_member_types_not_entity_count():
    # TWO orange-bearing entities → still ONE distinct citrus TYPE ("how many DIFFERENT ...").
    rows = [("orange bitters",), ("orange peels",), ("orange juice",)]
    db, _ = _mock_db(rows)
    res = main._l4_hyponym_membership_count(db, "citrus fruits", _CLASSIF)
    assert res["count"] == 1
    assert res["instance_names"] == ["orange"]


def test_flag_off_is_a_miss_byte_identical(monkeypatch):
    # FAIL-ON-OLD: the pre-fix path (flag off) no-ops to a miss — the count stays a false-miss.
    monkeypatch.setattr(main, "COUNT_HYPONYM_MEMBERSHIP", False)
    rows = [("orange bitters",), ("lime juice",), ("lemon slices",)]
    db, _ = _mock_db(rows)
    res = main._l4_hyponym_membership_count(db, "citrus fruits", _CLASSIF)
    assert res["miss"] is True and res["count"] == 0


def test_generic_head_declines_no_overcount(monkeypatch):
    monkeypatch.setattr(main, "COUNT_HYPONYM_MEMBERSHIP", True)
    rows = [("my blue thing",), ("some object",)]
    db, _ = _mock_db(rows)
    res = main._l4_hyponym_membership_count(db, "things", _CLASSIF)
    assert res["miss"] is True


def test_no_matching_aliases_is_a_miss(monkeypatch):
    monkeypatch.setattr(main, "COUNT_HYPONYM_MEMBERSHIP", True)
    db, _ = _mock_db([])   # Postgres regex matched nothing
    res = main._l4_hyponym_membership_count(db, "citrus fruits", _CLASSIF)
    assert res["miss"] is True
