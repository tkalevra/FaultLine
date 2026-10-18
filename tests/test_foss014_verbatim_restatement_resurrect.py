"""FOSS-014 — a VERBATIM re-statement after a retraction must RESURRECT.

The wound (live-measured, lane-C critic, seat a4444444): after "Please forget that I
have a dog named Rex." retired (user, has_pet, rex) via /retract/correct, re-stating
the IDENTICAL sentence returned committed=2 but NO live row — the turn deduped against
the retired row — while a PARAPHRASE ("Rex is my dog and he owns me.") resurrected it.

Root cause: the /ingest Redis idempotency cache is keyed on the request fingerprint
(text+user+endpoint+memory_facts+is_correction+edges), so a verbatim re-statement maps
to the ORIGINAL turn's key for the whole 3600s TTL. The cache-hit validator
(main.py::_idempotency_cached_facts_present) checked row existence WITHOUT a retirement
filter "on purpose" — a retired row counted as "present", the cached committed=N was
served, and the store stayed retracted. A fresh (unmarked) ingest fell into the frozen
class without carrying the X-FL-Deferred-Replay marker.

Fix discipline (house doctrine):
  • the cache may only be trusted when the rows it vouches for are LIVE — a retired row
    (superseded / archived / deleted / promoted-away) busts the hit and the turn
    re-ingests;
  • at the store the replay marker decides: a fresh user_stated upsert UN-RETIRES
    (store.py CASE: superseded_at = NULL, confirmed_count continues — pre-existing),
    a MARKED X-FL-Deferred-Replay replay keeps the retirement freeze (the 2026-09-05
    replay-resurrection wound, seat b3f8a2c4, stays closed);
  • the staged lane's Phase-4 FREEZE TOMBSTONE (WHERE deleted_at IS NULL) skipped the
    UPDATE for a deleted row replay-or-not — the same silent-freeze class. It is now
    replay-aware: marked replay skips (freeze), fresh ingest reaches the DO UPDATE and
    un-retires (deleted_at = NULL). The expires_at CASE guard keeps its replay scope.

Pin conventions follow tests/test_reextract_replay_resurrection.py: the blocks under
test are inline in ~58k-line main.py, so the suite pins by normalized-source/SQL
assertions PLUS scripted-connection functional tests of the validator itself.

Red-check at foss-main-local 68a20854 (the wound head), measured: 8 FAILED / 3 passed.
Every fix-shape pin is red there (no live filters in the validator, single filterless
probe, old bust reason, blanket deleted_at tombstone, validator serves the retired-row
cache hit, tombstone param absent) and green at the fix head. The 3 positive controls
pass at BOTH heads: the facts-side CASE guards (fresh resurrects / replay freezes),
the staged expires_at CASE guards (their replay scope unchanged), and the no-schema
fail-safe — the discipline that must not regress is pinned independently of the fix.
"""

import os
import sys

from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_MAIN_SRC = os.path.join(os.path.dirname(__file__), "..", "src", "api", "main.py")
_STORE_SRC = os.path.join(os.path.dirname(__file__), "..", "src", "fact_store", "store.py")


def _flat(path: str) -> str:
    # normalize the WHOLE source (string literals are split across lines in main.py,
    # so raw substring search misses); suite convention.
    return " ".join(open(path).read().replace('"', " ").split())


def _func_slice(flat: str, def_name: str) -> str:
    # slice one top-level def: from its "def name(" to the next top-level "def ".
    i = flat.index(f"def {def_name}(")
    j = flat.index(" def ", i + 1)
    return flat[i:j]


# -- Source pins: the validator requires LIVE rows (the wound's freeze is gone) --------

def _validator_slice() -> str:
    return _func_slice(_flat(_MAIN_SRC), "_idempotency_cached_facts_present")


def test_validator_requires_live_rows():
    sql = _validator_slice()
    # the facts branch: retirement filter ON (superseded / archived / forgotten)
    assert "AND superseded_at IS NULL AND archived_at IS NULL AND deleted_at IS NULL" in sql
    # the staged branch: tombstoned and promoted-away rows do not vouch for the cache
    assert "AND deleted_at IS NULL AND promoted_at IS NULL" in sql


def test_validator_names_retired_and_absent_apart():
    sql = _validator_slice()
    # both bust causes re-ingest; the EXPECTED re-statement bust is logged apart from
    # the wipe phantom so operators can tell the self-healing path from a phantom.
    assert "ingest.idempotency_cache_row_retired" in sql
    assert "ingest.idempotency_cache_row_absent" in sql
    # the second (existence) probe is the retired-vs-absent discriminator
    assert sql.count("SELECT 1 FROM facts") == 2


def test_cache_bust_call_site_reason_names_the_retired_case():
    flat = _flat(_MAIN_SRC)
    assert "ingest.idempotency_cache_busted_stale" in flat  # event unchanged
    assert "absent-or-retired" in flat
    # the marker decides at the store, and the comment says so
    assert "the replay marker decides" in flat


# -- Source pins: the store halves keep the marker discipline --------------------------

def test_facts_side_case_guards_resurrect_on_fresh_freeze_on_replay():
    sql = _flat(_STORE_SRC)
    # a fresh (unmarked) upsert of a retired row un-retires it …
    assert "superseded_at = CASE WHEN facts.superseded_at IS NOT NULL AND %s THEN facts.superseded_at ELSE NULL END" in sql
    # … and continues the count; a MARKED replay keeps both (the b3f8a2c4 freeze).
    assert "confirmed_count = CASE WHEN facts.superseded_at IS NOT NULL AND %s THEN facts.confirmed_count ELSE facts.confirmed_count + 1 END" in sql
    assert "bool(replay), bool(replay)" in sql  # both CASE params threaded, in order


def test_staged_tombstone_is_replay_aware():
    sql = _func_slice(_flat(_MAIN_SRC), "_commit_staged")
    # the Phase-4 blanket skip is GONE: a fresh re-statement is no longer frozen
    assert "WHERE staged_facts.deleted_at IS NULL OR NOT %s" in sql
    assert "WHERE staged_facts.deleted_at IS NULL," not in sql
    # the DO UPDATE un-retires the row it is now allowed to reach
    assert "deleted_at = NULL" in sql
    # the tombstone flag is threaded as the LAST param (after the two CASE params)
    assert "bool(replay), bool(replay), bool(replay)" in sql


def test_staged_expiry_case_guards_keep_their_replay_scope():
    sql = _func_slice(_flat(_MAIN_SRC), "_commit_staged")
    # expiry-based retirement keeps its CASE guard exactly as the replay guard shipped it
    assert "WHEN staged_facts.expires_at <= now() AND %s THEN staged_facts.confirmed_count" in sql
    assert "WHEN staged_facts.expires_at <= now() AND %s THEN staged_facts.expires_at" in sql


# -- Functional pins: the validator against a scripted tenant DB -----------------------

class _ScriptedCursor:
    def __init__(self, results):
        self._results = results  # one entry per execute() call
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))

    def fetchone(self):
        idx = len(self.executed) - 1
        return self._results[min(idx, len(self._results) - 1)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ScriptedConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


_REL_KEY_RESULTS_LIVE = [(1,), (1,), (1,)]     # SET, live-probe hits
_REL_KEY_RESULTS_RETIRED = [None, None, (1,)]  # SET, live-probe misses, row exists retired
_REL_KEY_RESULTS_ABSENT = [None, None, None]   # SET, live-probe misses, row gone

_CACHED_RESPONSE = {
    "committed": 2,
    "facts": [{"subject": "a" * 8 + "-0000-0000-0000-000000000000",
               "object": "b" * 8 + "-0000-0000-0000-000000000000",
               "rel_type": "has_pet", "status": "valid", "fact_class": "A"}],
}


@pytest.fixture()
def validator(monkeypatch):
    import src.api.main as M

    def _run(results):
        cur = _ScriptedCursor(results)
        monkeypatch.setattr(M.psycopg2, "connect", lambda dsn=None: _ScriptedConn(cur))
        return M._idempotency_cached_facts_present("faultline_testslug", dict(_CACHED_RESPONSE)), cur

    return _run


def test_validator_trusts_a_live_row(validator):
    ok, cur = validator(_REL_KEY_RESULTS_LIVE)
    assert ok is True
    # positive control: the FIRST probe is the live-row probe and carries the filters
    assert "superseded_at IS NULL AND archived_at IS NULL AND deleted_at IS NULL" in cur.executed[1]


def test_frozen_row_busts_the_cache_and_names_it_retired(validator):
    """THE WOUND PIN. At the wound head this returns True (row exists → cache served →
    committed=2, no live row). At the fix head the retired row busts the hit."""
    ok, cur = validator(_REL_KEY_RESULTS_RETIRED)
    assert ok is False
    # the live probe ran FIRST and carried the retirement filter (that is the fix)
    assert "superseded_at IS NULL AND archived_at IS NULL AND deleted_at IS NULL" in cur.executed[1]
    # the discriminator probe (no filters) ran second and found the retired copy
    assert "superseded_at IS NULL" not in cur.executed[2]
    assert "SELECT 1 FROM facts" in cur.executed[2]


def test_absent_row_busts_the_cache(validator):
    ok, cur = validator(_REL_KEY_RESULTS_ABSENT)
    assert ok is False
    assert "SELECT 1 FROM facts" in cur.executed[2]


def test_validator_failsafe_no_schema():
    import src.api.main as M
    assert M._idempotency_cached_facts_present(None, dict(_CACHED_RESPONSE)) is False


# -- Functional pin: the replay flag reaches the staged tombstone ----------------------

def test_commit_staged_replay_flag_reaches_the_tombstone():
    """The marker decides the staged tombstone: the upsert's LAST SQL param is the
    replay flag, so a marked replay freezes a deleted row and a fresh ingest
    un-retires it. Positive control at the wound head too (the flag was already the
    last param of the expiry CASE); the SQL-shape red-check lives in
    test_staged_tombstone_is_replay_aware."""
    import src.api.main as M

    for flag in (True, False):
        db = MagicMock()
        cur = db.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        M._commit_staged(db, [("u" * 32, "s" * 32, "o" * 32, "has_pet", "user_stated")],
                         "B", confidence=0.8, replay=flag)
        sql, params = cur.execute.call_args[0][0], cur.execute.call_args[0][1]
        assert "WHERE staged_facts.deleted_at IS NULL OR NOT %s" in " ".join(sql.split())
        assert params[-1] is flag
        db.commit.assert_called()
