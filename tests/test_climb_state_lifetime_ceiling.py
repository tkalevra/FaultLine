"""The `climb_state` attempt cap must actually BIND — a growing ontology cannot reset it forever.

THE BUG (measured on a live deployment). `_climb_state_should_skip` honours an
`unplaceable` verdict only while `attempt_count < _CLIMB_MAX_ATTEMPTS` (default 3) AND the concept's
FINGERPRINT is unchanged. But `_concept_fingerprint` is built from two terms:

    (a) the concept's OWN live hierarchy-edge count      — local, stable
    (b) count(DISTINCT object_id) over ALL live hierarchy edges IN THE TENANT — GLOBAL

Term (b) moves whenever ANY concept anywhere in the tenant gains a parent. On a growing tenant that
happens constantly, so EVERY cached verdict is invalidated at once and the cap is UNREACHABLE.

Production evidence, a live deployment (`climb_state`, cap = 3):

    verdict     | reason                  | attempt_count | concept
    ------------+-------------------------+---------------+---------
    unplaceable | coinstance_undetermined |           189 | another named instance
    unplaceable | coinstance_undetermined |           188 | a named instance
    unplaceable | coinstance_undetermined |           187 | a third named instance
    unplaceable | no_ordered_types        |           150 | f

`a named instance`'s cached fingerprint was `e4:o507` — `e4` = its own four `instance_of` edges
(morkie / morkie dog / dog / pet), `o507` = the tenant-global counter. 47 rows over cap and 2,372
recorded attempts on ONE seat, each attempt spending LLM calls on a concept that cannot resolve
(no classifier can order `dog` vs `pet` — `pet` is a role, not a taxonomic supertype).

THE FIX: a LIFETIME ceiling checked BEFORE the fingerprint comparison, so a global-ontology bump
cannot reset it. `attempt_count` is already a lifetime failure counter (`_climb_state_record` resets
it to 0 only on a `'placed'` verdict), so a concept that ever succeeds starts over.

These tests drive the real `_climb_state_should_skip` against a fake cursor — no DB required.
"""

import pytest

import src.re_embedder.embedder as emb


class _FakeCursor:
    """Minimal psycopg2-cursor stand-in returning one canned climb_state row."""

    def __init__(self, row, freshness=False):
        self._row = row
        self._freshness = freshness
        self._last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last_sql = sql

    def fetchone(self):
        # The backoff-window probe is a separate SELECT returning a single boolean.
        if "make_interval" in self._last_sql:
            return (self._freshness,)
        return self._row


class _FakeConn:
    def __init__(self, row, freshness=False):
        self._row = row
        self._freshness = freshness

    def cursor(self):
        return _FakeCursor(self._row, self._freshness)

    def rollback(self):
        pass


def _row(verdict, attempts, fingerprint, last_at="2026-08-11T00:00:00Z"):
    return (verdict, attempts, fingerprint, last_at)


def test_a_changed_global_fingerprint_no_longer_resets_a_runaway_concept():
    """THE REGRESSION PIN. the named instance's real shape: unplaceable, 188 attempts, fingerprint CHANGED.

    Before the fix this returned False (re-open → another round of LLM calls) on every single
    ontology growth event, which is how one concept reached 188 attempts against a cap of 3."""
    conn = _FakeConn(_row("unplaceable", 188, "e4:o507"))
    # Current fingerprint differs (the tenant-global term moved) — the pre-fix re-open trigger.
    assert emb._climb_state_should_skip(conn, "named-instance-uuid", "e4:o508") is True, (
        "a concept 188 failures deep must stay skipped even though the tenant-global ontology "
        "counter moved — otherwise the attempt cap can never bind"
    )


def test_under_the_ceiling_a_changed_fingerprint_still_re_opens():
    """The additive re-validation must be PRESERVED below the ceiling — this is not a blanket stop."""
    conn = _FakeConn(_row("unplaceable", 2, "e1:o100"))
    assert emb._climb_state_should_skip(conn, "some-uuid", "e2:o101") is False, (
        "genuinely new information (a changed fingerprint) must still re-open a concept that has "
        "only failed twice"
    )


def test_a_placed_verdict_is_still_honoured_and_never_hits_the_ceiling():
    conn = _FakeConn(_row("placed", 0, "e1:o100"))
    assert emb._climb_state_should_skip(conn, "some-uuid", "e1:o100") is True


def test_a_placed_verdict_still_re_opens_on_new_information():
    conn = _FakeConn(_row("placed", 0, "e1:o100"))
    assert emb._climb_state_should_skip(conn, "some-uuid", "e2:o105") is False


def test_the_ceiling_does_not_apply_to_placed_rows():
    """A 'placed' row with a stale high count must follow the normal 'placed' rules, not the ceiling.

    (`_climb_state_record` zeroes the count on 'placed', so this is defensive.)"""
    conn = _FakeConn(_row("placed", 999, "e1:o100"))
    assert emb._climb_state_should_skip(conn, "some-uuid", "e9:o999") is False


def test_ceiling_of_zero_disables_the_feature(monkeypatch):
    """`ENGINE_CLASSIFY_LIFETIME_MAX_ATTEMPTS=0` restores byte-for-byte legacy behavior."""
    monkeypatch.setattr(emb, "_CLIMB_LIFETIME_MAX_ATTEMPTS", 0)
    conn = _FakeConn(_row("unplaceable", 188, "e4:o507"))
    assert emb._climb_state_should_skip(conn, "named-instance-uuid", "e4:o508") is False, (
        "with the ceiling disabled the legacy fingerprint re-open must happen exactly as before"
    )


def test_no_cached_row_still_allows_an_attempt():
    conn = _FakeConn(None)
    assert emb._climb_state_should_skip(conn, "new-uuid", "e1:o1") is False


@pytest.mark.parametrize("attempts", [24, 25, 26])
def test_the_ceiling_boundary_is_inclusive(attempts):
    """>= the ceiling skips; below it the fingerprint rule applies."""
    conn = _FakeConn(_row("unplaceable", attempts, "e1:o100"))
    got = emb._climb_state_should_skip(conn, "some-uuid", "e1:o101")
    expected = attempts >= emb._CLIMB_LIFETIME_MAX_ATTEMPTS
    assert got is expected, f"attempts={attempts} ceiling={emb._CLIMB_LIFETIME_MAX_ATTEMPTS}"
