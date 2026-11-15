"""
Regression: SCALAR SIBLING-FRAGMENT LANE in fetch_facts_from_anchor.

LME qid=76d63226 — "What size is my new Samsung TV?" (gold: "55-inch").

Ingest is clean but FRAGMENTED: the one real TV lands as several user-linked
fragment entities — "samsung tv" (no scalar), "smart tv", "55-inch smart tv"
(carries height=55-inch). The attribute question anchors (longest n-gram) on the
scalar-LESS fragment "samsung tv", so the captured value is unreachable from the
resolved anchor. The shared token "tv" is too short for the query-word
entity-centric expansion (len>=4) and no alias bridges the two names.

The sibling-fragment lane reaches the value deterministically: from the anchor's
OWN preferred-alias tokens ({samsung, tv}) it finds user-linked fragments sharing a
whole content token ("tv" -> "55-inch smart tv") and surfaces their SCALAR
attributes only.

FAIL-ON-OLD: without the lane the anchor "samsung tv" carries no scalar, so
"55-inch" never surfaces. PASS-ON-FIX: the sibling's height=55-inch is surfaced.

Robust to query SEQUENCE: the fake cursor dispatches on SQL TEXT (not a fragile
fetchall side_effect list), so it does not go dead if the query order shifts.
"""

import os
import uuid
from unittest.mock import patch, MagicMock

from src.api.main import fetch_facts_from_anchor
from src.api.models import QueryPath


ANCHOR = "fb3a5121-50fc-5baf-9ffe-b73991ce847c"   # "samsung tv" — NO scalar (resolved anchor)
SIBLING = "b0b7dea1-9304-5e91-80fe-6dc90d10076e"  # "55-inch smart tv" — height=55-inch
USER = "04658f0e-c7b5-4725-9ccf-f426ff13c3e5"


class _DispatchCursor:
    """A cursor that answers by SQL TEXT, not call order."""

    def __init__(self):
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())  # normalise whitespace

    def _matches(self, *needles):
        return all(n in self._last for n in needles)

    def fetchone(self):
        # Anchor's OWN preferred alias (the token source for the sibling bridge).
        if self._matches("SELECT alias FROM entity_aliases",
                          "is_preferred = true", "LIMIT 1"):
            return ("samsung tv",)
        return None

    def fetchall(self):
        # Sibling-fragment SCALAR query (entity_attributes JOIN preferred alias,
        # user-linked subselect). Return the height-bearing fragment.
        if self._matches("FROM entity_attributes ea", "JOIN entity_aliases al"):
            # 8th column = ea.provenance (added by the scalar-retract-recall-leak
            # gauntlet, bar RL4: the lane reports the tier the row's stored
            # provenance earns instead of a flat Class A / 1.0). "user_stated"
            # keeps this fixture's surfacing behaviour byte-identical.
            return [(SIBLING, "55-inch smart tv", "height", "55-inch",
                     None, None, None, "user_stated")]
        # Anchor's OWN scalars — none (it is the scalar-less fragment).
        if self._matches("FROM entity_attributes", "WHERE entity_id"):
            return []
        # Everything else (direct/inverse/staged/taxonomy walks): empty.
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(query_text):
    with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
        with patch("src.api.main.psycopg2.connect") as mock_connect:
            cur = _DispatchCursor()
            conn = MagicMock()
            conn.cursor.return_value = cur          # `with conn.cursor() as cur`
            conn.cursor.return_value.__enter__ = lambda s: cur
            conn.cursor.return_value.__exit__ = lambda s, *a: False
            mock_connect.return_value.__enter__.return_value = conn
            # non-user concrete anchor -> the anchored-scalar lane runs, finds none
            path = QueryPath(fetch_all_details=True)
            return fetch_facts_from_anchor(ANCHOR, USER, path, query_text=query_text)


def _surfaced_55inch(facts):
    return any(
        f.get("source") == "attributes"
        and f.get("subject") == SIBLING
        and str(f.get("object", "")).lower() == "55-inch"
        for f in facts
    )


def test_sibling_fragment_scalar_surfaces_on_value_question():
    """FAIL-ON-OLD / PASS-ON-FIX: the value question reaches the sibling scalar."""
    facts = _run("What size is my Samsung TV?")
    assert _surfaced_55inch(facts), (
        "sibling-fragment lane did not surface height=55-inch from '55-inch smart tv' "
        f"for the anchor 'samsung tv' — facts={facts!r}"
    )


def test_sibling_fragment_gated_off_non_interrogative():
    """GATE: a non-value ('tell me about') query must NOT reach out to siblings."""
    facts = _run("Tell me about my Samsung TV")
    assert not _surfaced_55inch(facts), (
        "sibling-fragment lane over-fired on a non-interrogative query — "
        f"facts={facts!r}"
    )
