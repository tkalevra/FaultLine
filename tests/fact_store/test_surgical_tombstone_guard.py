"""SURGICAL TOMBSTONE GUARD — a surgical forget can never become a rel-class sweep.

WOUND (prod seat 3f9c21ae, 2026-08-19): a surgical_scalar retraction ("I don't have a
laptop") resolved subject=USER with NO object bound; FactStoreManager.retract's object-side
fallback matched object_id=user_uuid AND rel_type=child_of — every live kin edge in the
seat — and mode="hard_delete" stamped archived_at = deleted_at = now() on all of them in
ONE UPDATE (shared-timestamp mass tombstone, confirmed_counts 8-11, no contradicted_by).

These pins hold the fail-closed contract of the guard in FactStoreManager.retract:

1. hard_delete + unbound object + >1 matching row  → retracts NOTHING (no UPDATE runs).
2. hard_delete + unbound object + exactly 1 match  → proceeds (a genuine single target).
3. hard_delete + BOUND object (fully named target) → proceeds even multi-row (surgical).
4. Broad path preserved: mode="supersede" (plain-language /retract) still sweeps.
5. unforget (restore) is non-destructive → never guarded.

Mock-based, same conventions as tests/fact_store/test_replay_guard.py.
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from src.fact_store.store import FactStoreManager

USER = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"
DAX = "33333333-3333-3333-3333-333333333333"
LAPTOP = "44444444-4444-4444-4444-444444444444"


class _FakeCursor:
    """Answers the two SELECT id probes from configured id pools; records every SQL."""

    def __init__(self, facts_ids, staged_ids):
        self.facts_ids = facts_ids
        self.staged_ids = staged_ids
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        sql = self.executed[-1][0]
        if "FROM staged_facts" in sql:
            return [(i,) for i in self.staged_ids]
        return [(i,) for i in self.facts_ids]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(mode, facts_ids, staged_ids=(), rel_type="child_of", old_value=None,
         subject=USER, user_id=USER):
    db = MagicMock()
    cur = _FakeCursor(facts_ids, list(staged_ids))
    db.cursor.return_value.__enter__.return_value = cur
    out = FactStoreManager(db).retract(
        cur, user_id, subject, rel_type, old_value, mode)
    return out, cur


def _update_sqls(cur):
    return [sql for sql, _ in cur.executed
            if sql.upper().startswith("UPDATE")]


# ── 1. THE WOUND: speaker-anchored unbound-object tombstone over a kin class ─────────

def test_mass_tombstone_of_child_of_class_is_refused():
    """The wound signature: subject=user, rel=child_of, no object, 6+5 live matches →
    hard_delete must retract NOTHING and must not run any UPDATE."""
    (ids, table), cur = _run("hard_delete", facts_ids=[1, 2, 3, 4, 5, 6],
                             staged_ids=[7, 8, 9, 10, 11])
    assert ids == [] and table == "facts"
    assert _update_sqls(cur) == []


def test_two_row_sweep_is_also_refused():
    """The guard is not a >5 heuristic — ANY unbound-object tombstone resolving to more
    than one row is a class sweep and is refused."""
    (ids, _), cur = _run("hard_delete", facts_ids=[1, 2])
    assert ids == []
    assert _update_sqls(cur) == []


def test_single_match_still_tombstones():
    """A forget that resolves to exactly ONE row is surgical — proceeds unchanged."""
    (ids, _), cur = _run("hard_delete", facts_ids=[42])
    assert ids == [42]
    updates = _update_sqls(cur)
    assert any("archived_at = now(), deleted_at = now()" in s for s in updates)


def test_staged_only_single_match_still_tombstones():
    (ids, _), cur = _run("hard_delete", facts_ids=[], staged_ids=[9])
    assert ids == [9]
    assert any("UPDATE staged_facts" in s for s in _update_sqls(cur))


# ── 2. Fully-bound targets are never blocked ─────────────────────────────────────────

def test_bound_object_tombstone_proceeds_even_multi_row():
    """old_value bound = the retraction NAMES the value: every matching row IS a target
    (e.g. dual-write copies of the same named fact). Never guarded."""
    (ids, _), cur = _run("hard_delete", facts_ids=[1, 2, 3], old_value=LAPTOP)
    assert ids == [1, 2, 3]
    assert _update_sqls(cur)


def test_bound_object_staged_sweep_proceeds():
    (ids, _), cur = _run("hard_delete", facts_ids=[], staged_ids=[7, 8], old_value=DAX)
    assert sorted(ids) == [7, 8]


# ── 3. The legitimate broad-retract path is preserved ────────────────────────────────

def test_broad_supersede_retract_still_sweeps():
    """"Forget everything about X" routes through /retract in supersede mode: soft, and
    never subject to the surgical-tombstone guard. (Non-dual modes are facts-first: the
    staged side is only probed when facts matched nothing — original first-match semantics.)"""
    (ids, _), cur = _run("supersede", facts_ids=[1, 2, 3, 4, 5, 6],
                         staged_ids=[7, 8, 9, 10, 11])
    assert len(ids) == 6
    assert any("superseded_at = now()" in s for s in _update_sqls(cur))


# ── 4. unforget (restore) is non-destructive and never guarded ───────────────────────

def test_unforget_restore_is_never_guarded():
    (ids, _), cur = _run("unforget", facts_ids=[1, 2, 3], staged_ids=[4, 5])
    assert len(ids) == 5
    assert any("deleted_at = NULL" in s for s in _update_sqls(cur))


# ── 5. Guard keys on the TOMBSTONE mode, not the rel ─────────────────────────────────

def test_guard_independent_of_rel_type():
    """Any rel class swept by an unbound-object tombstone is refused — the guard is
    mode-keyed, not rel-name-keyed (subject-agnostic: no rel literals)."""
    for rel in ("child_of", "parent_of", "owns", "feels"):
        (ids, _), cur = _run("hard_delete", facts_ids=[1, 2], rel_type=rel)
        assert ids == [], rel
