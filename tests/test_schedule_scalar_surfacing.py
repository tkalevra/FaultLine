"""Schedule/companion SCALAR surfacing — LME ce6d2d27 (day-of-week) query/render half.

CAPTURE is proven clean (test_schedule_recurrence_scalar): "I take a cocktail-making class on
Fridays" commits the anchor scalar ``(user, take_on, "fridays")`` in entity_attributes. But the
RECALL did not SURFACE it: a "what day do I take …" question resolved NO scope (ATTRSCOPE matched
the WHOLE compound attribute name "take_on" against the query words and missed — "take_on" is never
a query word), so the day value was projected out / the query firehosed the profile.

FIX (lean-query, ATTRSCOPE tokenisation — the scalar twin of RELSCOPE's rel-name tokenisation):
determine_path now also matches a query aspect word against the TOKENISED anchor-attribute name
("take_on" → {take}, the functor half "on" dropped via _tmpl_stop), so the query verb "take" grounds
the ``take_on`` scalar into path.scalar_rels — scalar-only + anchor-grounded, no relationship edge,
no cross-group leak. The anchor entity_attributes projection then surfaces it and convert_to_prose
renders the verbatim value.

Deterministic (word-boundary tokenisation, no cosine/LLM/substring); subject-agnostic (the token
comes from the query + the stored attribute, no day/verb/domain literal). Two proofs: (1) scope
resolution admits the compound scalar; (2) the render seam emits the value verbatim.

FAILS on the pre-fix code (ATTRSCOPE whole-name match → take_on not admitted → scope_active False).
"""
import re

import pytest

ANCHOR = "11111111-1111-1111-1111-111111111111"


class _FakeCur:
    """Minimal SQL-pattern cursor: returns the anchor's entity_attributes for the ATTRSCOPE
    read, empty for every other query determine_path issues."""

    def __init__(self, attr_rows):
        self._attr_rows = attr_rows
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = re.sub(r"\s+", " ", sql).strip().lower()
        # ATTRSCOPE anchor-attribute read: (attribute, natural_language)
        if "from entity_attributes ea" in s and "left join rel_types" in s:
            self._rows = list(self._attr_rows)
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, attr_rows):
        self._attr_rows = attr_rows

    def cursor(self):
        return _FakeCur(self._attr_rows)


def _resolve(query, attr_rows):
    from src.api.main import determine_path
    return determine_path(query, _FakeDB(attr_rows), user_id=None,
                          anchor_resolved_uuid=ANCHOR)


# ── (1) SCOPE RESOLUTION admits the compound schedule scalar via the activity verb ────────────────
def test_what_day_grounds_compound_schedule_scalar():
    # anchor holds (user, take_on, fridays); "take_on" has no natural_language template.
    path = _resolve("what day of the week do I take a cocktail-making class",
                    attr_rows=[("take_on", None)])
    assert "take_on" in [r.lower() for r in path.scalar_rels], path.scalar_rels
    # scope binds (no firehose) — the aspect-precision path, not fetch_all.
    assert path.scope_active is True


def test_what_time_still_grounds_plain_and_companion_scalars():
    # the wake-time HIT must keep working: "wake" (time) grounds by whole-name; the day
    # companion "wake_on" grounds by the new tokenisation. Both admitted.
    path = _resolve("what time do I wake up on Tuesdays and Thursdays",
                    attr_rows=[("wake", "x is y"), ("wake_on", None)])
    admitted = [r.lower() for r in path.scalar_rels]
    assert "wake" in admitted, admitted
    assert "wake_on" in admitted, admitted


def test_unrelated_query_admits_nothing_no_overscope():
    # a query naming none of the anchor's attributes must NOT admit them (fetch-all boundary
    # preserved — the tokeniser never widens on an unrelated word).
    path = _resolve("what is the capital of France",
                    attr_rows=[("take_on", None), ("wake_on", None)])
    assert "take_on" not in [r.lower() for r in path.scalar_rels]
    assert "wake_on" not in [r.lower() for r in path.scalar_rels]


# ── (2) RENDER seam emits the verbatim schedule value (no-template compound rel) ──────────────────
def test_prose_renders_schedule_value():
    from src.api.main import convert_to_prose

    class _NullCur(_FakeCur):
        def __init__(self):
            super().__init__([])

    class _NullDB:
        def cursor(self):
            return _NullCur()

    fact = {
        "subject": ANCHOR, "_subject_id": ANCHOR,
        "rel_type": "take_on", "object": "fridays",
        "fact_class": "A", "source": "attributes",
        "confidence": 1.0, "category": None,
    }
    prose = convert_to_prose([fact], _NullDB(), anchor=ANCHOR, user_id=ANCHOR)
    assert any("fridays" in p.lower() for p in prose), prose


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
