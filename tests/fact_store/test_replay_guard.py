"""DEFERRED-REPLAY GUARD — the store half of the replay-transport wiring.

The transport half lives at the MCP seam (X-FL-Deferred-Replay header — see section 8 of
tests/mcp/test_ingest_retry_dropturn.py); these tests pin the STORE half:
FactStoreManager.commit (facts) and main._commit_staged (staged_facts) must freeze an
ALREADY-RETIRED row when the upsert is a replay (replay=True), and must emit exactly the
legacy SQL for every other case (fresh ingest, live row, replay of a live row — the freeze
is scoped to retired rows only, so a live row still bumps and a genuine re-statement still
resurrects; "replay freeze is not a promotion freeze").

Mock-based, same conventions as tests/fact_store/test_commit.py: the SQL text and the
positional params are asserted directly. The row-level truth (a superseded row KEEPING
superseded_at and confirmed_count under a replayed upsert; a fresh ingest resurrecting it)
is exercised end-to-end on pre-prod by the gauntlet battery
(DEV/review/gauntlet/replay-transport/).
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from src.api import ingest_transport as T
from src.fact_store.store import FactStoreManager


# ── 0. The marker parse is fail-safe toward the user's latest words ───────────────────────

def test_replay_parse_accepts_only_canonical_values():
    assert T.replay_from_raw("1") is True
    assert T.replay_from_raw("true") is True
    assert T.replay_from_raw(" YES ") is True
    # An absent/empty/garbled header must NEVER parse as a replay — a garbled marker
    # would freeze a row the user just re-stated (the exact inverse of the guard's job).
    assert T.replay_from_raw(None) is False
    assert T.replay_from_raw("") is False
    assert T.replay_from_raw("0") is False
    assert T.replay_from_raw("replay?") is False


# ── 1. facts: FactStoreManager.commit freezes a SUPERSEDED row only under replay ──────────

_SUPERSEDED_COUNT_GUARD = (
    "CASE WHEN facts.superseded_at IS NOT NULL AND %s THEN facts.confirmed_count"
    " ELSE facts.confirmed_count + 1 END"
)
_SUPERSEDED_RESURRECT_GUARD = (
    "CASE WHEN facts.superseded_at IS NOT NULL AND %s THEN facts.superseded_at ELSE NULL END"
)


def _run_commit(**kwargs):
    db = MagicMock()
    FactStoreManager(db).commit(
        [("user1", "alice", "bob", "spouse", "doc")], **kwargs)
    cur = db.cursor.return_value.__enter__.return_value
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    return " ".join(sql.split()), list(params)


def test_commit_default_is_not_a_replay():
    """Omitting replay= must bind False at BOTH guard params — the pre-feature behavior
    for every existing caller (byte-identical semantics: superseded_at=NULL, count+1)."""
    sql, params = _run_commit()
    assert _SUPERSEDED_COUNT_GUARD in sql
    assert _SUPERSEDED_RESURRECT_GUARD in sql
    assert params[-2:] == [False, False]


def test_commit_replay_true_binds_the_freeze():
    """replay=True binds True at both guards: a superseded row keeps its count (no
    reinforcement) and its superseded_at (no resurrection) under a replayed upsert."""
    sql, params = _run_commit(replay=True)
    assert params[-2:] == [True, True]


def test_commit_freeze_is_scoped_to_superseded_rows():
    """The guard's own SQL scopes the freeze: a LIVE row (superseded_at IS NULL) takes the
    ELSE arm — count+1, superseded_at=NULL — even under replay. Replay freeze is not a
    promotion freeze."""
    sql, _ = _run_commit(replay=True)
    assert "facts.superseded_at IS NOT NULL AND %s" in sql
    assert "ELSE facts.confirmed_count + 1" in sql
    assert "ELSE NULL" in sql


# ── 2. staged_facts: _commit_staged freezes an EXPIRED row only under replay ──────────────

_STAGED_COUNT_GUARD = (
    "WHEN staged_facts.expires_at <= now() AND %s THEN staged_facts.confirmed_count"
    " ELSE staged_facts.confirmed_count + 1 END"
)
_STAGED_EXPIRY_GUARD = (
    "WHEN staged_facts.expires_at <= now() AND %s THEN staged_facts.expires_at"
    " ELSE now() + interval '30 days' END"
)
_STAGED_TOMBSTONE = "WHERE staged_facts.deleted_at IS NULL"


def _run_staged(**kwargs):
    from src.api import main as m  # heavy import, done lazily per test
    db = MagicMock()
    row = ("user1", "alice", "bob", "spouse", "doc", "", None, False, [],
           "user_stated", "now", None, None, "affirmed")
    m._commit_staged(db, [row], "B", confidence=0.8, **kwargs)
    cur = db.cursor.return_value.__enter__.return_value
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    return " ".join(sql.split()), list(params)


def test_staged_default_is_not_a_replay():
    sql, params = _run_staged()
    assert _STAGED_COUNT_GUARD in sql
    assert _STAGED_EXPIRY_GUARD in sql
    assert _STAGED_TOMBSTONE in sql, "the Phase-4 freeze tombstone must survive"
    assert params[-2:] == [False, False]


def test_staged_replay_true_binds_the_freeze():
    sql, params = _run_staged(replay=True)
    assert params[-2:] == [True, True]


def test_staged_deleted_rows_are_skipped_whole():
    """deleted_at retirement (the correction lane's CORRECTION_SPANS_STAGED supersede) is
    guarded by the PRE-EXISTING Phase-4 tombstone: the UPDATE itself is skipped, replay or
    not — no resurrection, no count bump, no expiry reset. The replay guard covers the
    OTHER retirement (expires_at), which the tombstone does not see."""
    sql, _ = _run_staged(replay=True)
    assert _STAGED_TOMBSTONE in sql
    assert sql.index("DO UPDATE SET") < sql.index(_STAGED_TOMBSTONE)
