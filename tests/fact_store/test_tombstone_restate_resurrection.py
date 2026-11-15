"""TOMBSTONE-RESTATE RESURRECTION — the prod re-statement wound (seat 10d7d879).

A genuine user RE-STATEMENT (remember_facts, fresh /ingest, NO X-FL-Deferred-Replay
marker) of a natural key whose facts row carries the Phase-4 forget tombstone
(deleted_at + archived_at set) must RESURRECT the row: clear deleted_at/archived_at,
keep/advance counts, mark qdrant_synced=false. Before the fix the ON CONFLICT
DO UPDATE in FactStoreManager.commit never touched deleted_at/archived_at at all, so
the re-statement only advanced updated_at while the row stayed dead (prod-measured
2026-10-09: class_a_committed=6, replay=False, deleted_at=2026-08-19 survived).

Fail direction (deterministic, no LLM):
  • a MARKED replay (X-FL-Deferred-Replay, replay=True) keeps the tombstone exactly
    as shipped — the replay-resurrection gauntlet must stay green;
  • llm_inferred / llm_learned / unknown provenance defaults to CURRENT behavior
    (no resurrection) — only user_stated fresh edges resurrect;
  • the staged lane (_commit_staged) gets the identical semantics: its Phase-4
    WHERE tombstone gains the same unmarked-user_stated arm.

Mock-based, same conventions as tests/fact_store/test_replay_guard.py: the SQL text
and the positional params are asserted directly; row-level truth is exercised
end-to-end on pre-prod by the gauntlet battery (DEV/review/gauntlet/replay-transport/).
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from src.fact_store.store import FactStoreManager

# The resurrection CASE the fix added — shared shape, per-table prefix.
_RESURRECT_CASE = (
    "WHEN {t}.deleted_at IS NOT NULL AND (NOT %s)"
    " AND EXCLUDED.fact_provenance = 'user_stated' THEN NULL"
)
_FACTS_DELETED_GUARD = _RESURRECT_CASE.format(t="facts") + " ELSE facts.deleted_at END"
_FACTS_ARCHIVED_GUARD = _RESURRECT_CASE.format(t="facts") + " ELSE facts.archived_at END"
_STAGED_DELETED_GUARD = (
    _RESURRECT_CASE.format(t="staged_facts") + " ELSE staged_facts.deleted_at END"
)


def _run_commit(fact_provenance, **kwargs):
    db = MagicMock()
    FactStoreManager(db).commit(
        [("user1", "alice", "bob", "spouse", "doc")],
        fact_provenance=fact_provenance,
        **kwargs,
    )
    cur = db.cursor.return_value.__enter__.return_value
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    return " ".join(sql.split()), list(params)


def _run_staged(fact_provenance, **kwargs):
    from src.api import main as m  # heavy import, lazy per test (convention of test_replay_guard.py)
    db = MagicMock()
    row = ("user1", "alice", "bob", "spouse", "doc", "", None, False, [],
           fact_provenance, "now", None, None, "affirmed")
    m._commit_staged(db, [row], "B", confidence=0.8, **kwargs)
    cur = db.cursor.return_value.__enter__.return_value
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    return " ".join(sql.split()), list(params)


# ── (a) fresh user_stated re-statement RESURRECTS a tombstoned natural key ─────────


def test_facts_fresh_user_stated_binds_resurrection():
    """Fresh ingest (no replay marker) of a user_stated edge binds False at every
    tombstone guard — the matched tombstoned row clears deleted_at + archived_at and
    the re-embedder reconciles its Qdrant point back (qdrant_synced=false)."""
    sql, params = _run_commit("user_stated")
    assert _FACTS_DELETED_GUARD in sql
    assert _FACTS_ARCHIVED_GUARD in sql
    assert "ELSE facts.qdrant_synced END" in sql
    assert "THEN false" in sql
    # the five replay bindings at the tail: count-freeze, supersede-freeze,
    # deleted_at, archived_at, qdrant_synced — ALL False on a fresh re-statement.
    assert params[-5:] == [False] * 5


def test_staged_fresh_user_stated_reaches_tombstoned_rows():
    """The staged Phase-4 WHERE tombstone now admits the unmarked user_stated arm,
    and the SET clause clears deleted_at on exactly that arm — a fresh re-statement
    resurrects a correction-superseded staged row too."""
    sql, params = _run_staged("user_stated")
    assert _STAGED_DELETED_GUARD in sql
    assert (
        "WHERE staged_facts.deleted_at IS NULL"
        " OR (staged_facts.deleted_at IS NOT NULL AND (NOT %s)"
        " AND EXCLUDED.fact_provenance = 'user_stated')" in sql
    )
    assert params[-4:] == [False] * 4


# ── (b) MARKED replay against the tombstoned row stays FROZEN ──────────────────────


def test_facts_marked_replay_keeps_tombstone():
    """replay=True binds True at the tombstone guards: NOT %s evaluates false, the
    CASE takes the ELSE arm, deleted_at/archived_at survive untouched — the
    replay-resurrection gauntlet contract is unchanged."""
    sql, params = _run_commit("user_stated", replay=True)
    assert _FACTS_DELETED_GUARD in sql
    assert _FACTS_ARCHIVED_GUARD in sql
    assert params[-5:] == [True] * 5


def test_staged_marked_replay_keeps_tombstone():
    """replay=True: the WHERE's OR arm closes (NOT %s false), so a tombstoned staged
    row is again skipped WHOLE — no resurrection, no count bump, no expiry reset."""
    sql, params = _run_staged("user_stated", replay=True)
    assert (
        "WHERE staged_facts.deleted_at IS NULL"
        " OR (staged_facts.deleted_at IS NOT NULL AND (NOT %s)"
        " AND EXCLUDED.fact_provenance = 'user_stated')" in sql
    )
    assert params[-4:] == [True] * 4


# ── (c) llm_inferred fresh ingest does NOT resurrect ───────────────────────────────


def test_facts_llm_inferred_fresh_does_not_resurrect():
    """The resurrection arm is gated on EXCLUDED.fact_provenance = 'user_stated' in
    the SQL itself — an llm_inferred fresh edge can never take the THEN (NULL) arm,
    whatever the replay flag: unknown/inferred provenance defaults to CURRENT
    behavior (tombstone kept). Same for staged."""
    for prov in ("llm_inferred", "llm_learned"):
        sql, params = _run_commit(prov)
        assert "AND EXCLUDED.fact_provenance = 'user_stated' THEN NULL" in sql
        assert params[-5:] == [False] * 5  # False replay binding, but the provenance
        # gate in the CASE text is what keeps the tombstone — deterministic fail-safe.
        ssql, sparams = _run_staged(prov)
        assert "AND EXCLUDED.fact_provenance = 'user_stated' THEN NULL" in ssql
        assert sparams[-4:] == [False] * 4


# ── regression: the shipped replay-freeze guards are byte-identical ────────────────


def test_replay_freeze_guards_unchanged():
    """The pre-existing superseded_at/expiry CASE guards (test_replay_guard.py's
    subject) must be untouched by the resurrection patch — the freeze contract
    shipped for marked replays still holds verbatim."""
    sql, params = _run_commit("user_stated")
    assert (
        "CASE WHEN facts.superseded_at IS NOT NULL AND %s THEN facts.confirmed_count"
        " ELSE facts.confirmed_count + 1 END" in sql
    )
    assert (
        "CASE WHEN facts.superseded_at IS NOT NULL AND %s THEN facts.superseded_at"
        " ELSE NULL END" in sql
    )
    ssql, sparams = _run_staged("user_stated")
    assert (
        "WHEN staged_facts.expires_at <= now() AND %s THEN staged_facts.confirmed_count"
        " ELSE staged_facts.confirmed_count + 1 END" in ssql
    )
    assert (
        "WHEN staged_facts.expires_at <= now() AND %s THEN staged_facts.expires_at"
        " ELSE now() + interval '30 days' END" in ssql
    )
