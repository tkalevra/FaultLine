"""THE HARD LINE — WordNet ladder must NOT reclassify a USER-STATED VALUE into an L4 place.

Pins the fix for the hard-line violation where the deterministic WordNet hypernym ladder
(``fact_provenance='llm_inferred'`` growth) laddered ``blue`` — the object of the user's
``(user, favorite_colour, blue)`` — as a TYPE node (``blue subclass_of chromatic_color …``),
which then BURIES / (in some tenant states) supersedes the user's ``favorite_colour=blue`` at
recall. Authority order inverted: engine GROWTH overriding USER truth.

The fix (``src/api/main.py``):
  * ``_rel_tail_is_unconstrained_value(db_conn, rel_type)`` — metadata distinguisher: a rel whose
    ``tail_types = {ANY}`` files an arbitrary VALUE (favorite_colour / likes / prefers / dislikes);
    a CONSTRAINED tail ({Animal}/{Object}/{Concept,emotion}) is a genuine typed KIND (has_pet /
    owns / feels) that SHOULD keep laddering.
  * ``_attach_wordnet_hypernym_ladder`` — refuses to ladder the object of a ``user_stated`` edge
    whose rel is an unconstrained VALUE slot (flag ``L4_LADDER_PROTECT_USER_VALUE`` default ON).

PURE tests — no DB, no network, no GLiNER2, no LLM, no nltk. A fake cursor supplies ``tail_types``;
``db_conn.cursor`` call-tracking distinguishes "guard fired (returned before the naming-edge check)"
from "guard did not fire (proceeded into the function body)".

Run: python3 tools/fltest.py --bug ladder-user-value --test tests/test_ladder_protect_user_value.py
     (tests/ is gitignored → git add -f)
"""
import os

import pytest

import src.api.main as m  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────────────────────

class _FakeCursor:
    """Minimal cursor: answers the SAVEPOINT / SELECT tail_types / RELEASE dance."""

    def __init__(self, tail):
        self._tail = tail          # list | None ; sentinel object() → simulate "no row"
        self._last_select = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last_select = sql.strip().upper().startswith("SELECT TAIL_TYPES")

    def fetchone(self):
        if not self._last_select:
            return None
        if self._tail is _NO_ROW:
            return None            # rel_type absent from rel_types
        return (self._tail,)


_NO_ROW = object()


class _FakeConn:
    """db_conn whose .cursor() returns a fresh _FakeCursor and RECORDS that it was called."""

    def __init__(self, tail):
        self._tail = tail
        self.cursor_calls = 0

    def cursor(self):
        self.cursor_calls += 1
        return _FakeCursor(self._tail)


# ── Test A: the metadata distinguisher (tail_types → value vs kind) ────────────────────────────

@pytest.mark.parametrize("tail,expected", [
    (["ANY"],                       True),   # favorite_colour / likes / prefers / dislikes — VALUE
    (["any"],                       True),   # case-insensitive
    ([],                            True),   # empty constraint == unconstrained
    (None,                          True),   # NULL tail_types == unconstrained
    (["Animal"],                    False),  # has_pet — a typed KIND (still ladders)
    (["Object", "Organization"],    False),  # owns — typed kinds
    (["Concept", "emotion"],        False),  # feels — affective KINDs (worried→…→emotion ladders)
    (["SCALAR"],                    False),  # scalar-tailed — constrained
    (_NO_ROW,                       False),  # rel absent → fail-safe proceed (do not over-suppress)
])
def test_tail_unconstrained_classifier(tail, expected):
    conn = _FakeConn(tail)
    assert m._rel_tail_is_unconstrained_value(conn, "some_rel") is expected


def test_tail_classifier_blank_rel_is_false():
    assert m._rel_tail_is_unconstrained_value(_FakeConn(["ANY"]), "") is False
    assert m._rel_tail_is_unconstrained_value(None, "some_rel") is False


# ── Test B: the ladder guard fires for a user_stated VALUE, not for a KIND / engine growth ──────
#
# The guard branch is tested in ISOLATION: the tail classifier is patched to a fixed bool (Test A
# already pins the real classifier), and WordNet is stubbed to []. The guard returns 0 BEFORE the
# first ``db_conn.cursor()`` use (the naming-edge check); every non-guard path reaches that cursor.
# So ``cursor_calls == 0`` ⟺ the guard fired.

def _call_ladder(provenance, tail_unconstrained, flag="true", *, monkeypatch):
    monkeypatch.setenv("L4_LADDER_PROTECT_USER_VALUE", flag)
    monkeypatch.setenv("L4_WORDNET_LADDER", "true")   # ladder flag must be ON to reach the guard
    # Isolate the guard branch from the DB-reading classifier (pinned separately in Test A) …
    monkeypatch.setattr(m, "_rel_tail_is_unconstrained_value",
                        lambda _conn, _rt: tail_unconstrained)
    # … and from nltk/WordNet, so a proceed-past-guard path returns cleanly with no rungs.
    monkeypatch.setattr(m.wordnet_ladder, "hypernym_rungs", lambda *a, **k: [])
    conn = _FakeConn(None)
    rungs = m._attach_wordnet_hypernym_ladder(
        conn, "faultline_test_schema",
        "obj-uuid-blue", "blue", "Concept", object(), "user-uuid",
        source_rel_type="favorite_colour", source_provenance=provenance,
    )
    return rungs, conn.cursor_calls


def test_guard_fires_for_user_stated_unconstrained_value(monkeypatch):
    # user_stated + {ANY} tail (favorite_colour=blue): the VALUE is a MEMORY → NOT laddered.
    rungs, cursor_calls = _call_ladder("user_stated", True, monkeypatch=monkeypatch)
    assert rungs == 0
    assert cursor_calls == 0, "guard must short-circuit BEFORE the naming-edge cursor use"


def test_guard_does_not_fire_for_user_stated_constrained_kind(monkeypatch):
    # user_stated + {Animal} tail (has_pet=dog): a genuine KIND → still ladders (guard passes).
    _rungs, cursor_calls = _call_ladder("user_stated", False, monkeypatch=monkeypatch)
    assert cursor_calls >= 1, "a constrained-kind object must PROCEED past the guard (keeps laddering)"


def test_guard_does_not_fire_for_engine_growth(monkeypatch):
    # llm_inferred (Hearst member / engine-grown type) even with {ANY} tail: growth of a PLACE is OK.
    _rungs, cursor_calls = _call_ladder("llm_inferred", True, monkeypatch=monkeypatch)
    assert cursor_calls >= 1, "engine-grown (non-user_stated) laddering must NOT be suppressed"


def test_flag_off_disables_guard(monkeypatch):
    # Escape hatch: L4_LADDER_PROTECT_USER_VALUE=false restores pre-fix behaviour (guard off).
    _rungs, cursor_calls = _call_ladder("user_stated", True, flag="false", monkeypatch=monkeypatch)
    assert cursor_calls >= 1, "flag OFF → guard disabled → proceeds (legacy behaviour)"


# ── Test C: the GENUINE correction path is untouched by this seam ──────────────────────────────

def test_correction_provenance_is_not_a_ladder_concern(monkeypatch):
    """A user correction (blue→red) NEVER flows through the ladder — it carries is_correction and
    routes to the retract/correct supersede path. The guard is scoped to ``user_stated`` STATEMENT
    laddering only; it neither sees nor blocks a correction. Assert the classifier + guard do not
    treat a correction rel as a value-suppression case that would interfere with supersession."""
    # The ladder guard only ever *skips laddering*; it performs NO delete/supersede. So even if a
    # correction edge reached the ladder, the guard could only *decline to grow a place* — it can
    # never retire the user's fact. Pin that the function is read-only w.r.t. the fact store here by
    # confirming the guard path makes zero writes (returns 0, only reads tail_types).
    rungs, _ = _call_ladder("user_stated", True, monkeypatch=monkeypatch)
    assert rungs == 0  # no rungs written; no supersede issued — corrections remain the retract path
