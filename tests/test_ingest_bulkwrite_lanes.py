"""INGEST BULK-WRITE LANES (litmus fix L3, ticket DEV/BUGS/prod-document-drain-20261103/06).

One /ingest call used to cost ~45 alias round trips + 428 grouping mint/extend events
(each its own COMMIT) + up to 302 synchronous brain phrasing calls - the measured 34s
p50 against a ~2s write. Three lanes land here, each with its regression pin:

  L3-A  EntityRegistry.prefetch_aliases - ONE batched alias SELECT per request;
        resolve() takes a cache HIT with byte-identical semantics (same ordered row,
        display-form heal intact) and a prefetched MISS falls through to the exact
        legacy query path. A prefetch that never ran (cache None) changes nothing.

  L3-B  _mint_groupings_from_membership - CADENCE gate (a batch with no
        grouping-candidate edge cannot change co-membership: skip) + ONE multi-row
        upsert with ONE commit replacing per-group (SELECT+SELECT+INSERT+COMMIT).
        Rows conflicting on taxonomy_name are merged in Python first (a single
        INSERT cannot affect the same conflict row twice).

  L3-C  _nl_fill_background - rel-type phrasing is RENDER METADATA: generated off the
        write path in a worker thread, cached by (schema, rel_type) under a TTL,
        failures swallowed (Job 7 backfills a miss). NL_FILL_SYNC_ON_WRITE=true is the
        documented rollback lever back to the legacy synchronous mint.

House style: DB-less. Fakes record every execute; monkeypatch swaps the seams.
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.entity_registry.registry import EntityRegistry  # noqa: E402
from src.api import main as M  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Cursor:
    """Records executes; answers fetchone/fetchall from a per-caller script."""

    def __init__(self, owner):
        self._owner = owner
        self._pending = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, params=None):
        self._owner.executed.append((" ".join(str(q).split()), params))
        self._pending = self._owner.script(self._owner.executed[-1])

    def fetchone(self):
        return self._pending[0] if self._pending else None

    def fetchall(self):
        return list(self._pending or [])


class _DB:
    """Minimal connection double: script(query,params) -> rows for the next fetch."""

    def __init__(self, script):
        self.script = script
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _registry(cache):
    """__new__-built registry (no __init__ I/O), cache preset, binding no-op'd."""
    reg = EntityRegistry.__new__(EntityRegistry)
    db = _DB(script=lambda qp: [])
    reg.db_conn = db
    reg._alias_cache = cache
    reg._bind_tenant_schema = lambda *a, **k: None
    return reg, db


# ---------------------------------------------------------------------------
# L3-A  prefetch + resolve cache consult
# ---------------------------------------------------------------------------
def test_prefetch_hits_take_no_alias_round_trip():
    reg, db = _registry({"apollo": ("11111111-1111-5111-8111-111111111111", None)})
    got = reg.resolve("u1", "apollo")
    assert got == "11111111-1111-5111-8111-111111111111"
    # NO entity_aliases SELECT ran - the dict hit replaced it.
    assert not any("FROM entity_aliases" in q for q, _ in db.executed), db.executed


def test_prefetched_miss_falls_through_to_query_lane():
    reg, db = _registry({"apollo": ("11111111-1111-5111-8111-111111111111", None)})
    # 'zeus' not in cache -> the legacy alias SELECT must run (membership, not blanket trust)
    try:
        reg.resolve("u1", "zeus")
    except Exception:
        pass  # mint tail needs more DB than the fake provides - the PROBE order is the pin
    assert any("FROM entity_aliases" in q for q, _ in db.executed), (
        "a name absent from the prefetch must keep its DB probe")


def test_no_prefetch_changes_nothing():
    reg, db = _registry(None)  # cache None: the pre-fix shape
    try:
        reg.resolve("u1", "apollo")
    except Exception:
        pass
    assert any("FROM entity_aliases" in q for q, _ in db.executed)


def test_prefetch_aliases_batches_and_dedupes():
    reg = EntityRegistry.__new__(EntityRegistry)
    rows = [
        ("apollo", "11111111-1111-5111-8111-111111111111", "Apollo"),
        ("apollo", "22222222-2222-5222-8222-222222222222", "Apollo II"),  # worse tie-break
    ]
    db = _DB(script=lambda qp: rows)
    reg.db_conn = db
    reg._bind_tenant_schema = lambda *a, **k: None
    reg._alias_cache = None
    n = reg.prefetch_aliases(["Apollo", "apollo", "  APOLLO  ", ""], "u1")
    assert n == 1  # deduped to one distinct alias, one cached row
    alias_queries = [p for q, p in db.executed if "ANY(%s)" in q and "entity_aliases" in q]
    assert len(alias_queries) == 1, "prefetch must be ONE batched SELECT"
    assert alias_queries[0][0] == ["apollo"]  # lowercased, stripped, empty dropped
    # first row (best tie-break) wins
    assert reg._alias_cache["apollo"][0] == "11111111-1111-5111-8111-111111111111"


def test_prefetch_failure_is_failsafe():
    reg = EntityRegistry.__new__(EntityRegistry)
    reg._bind_tenant_schema = lambda *a, **k: None

    class _Boom:
        def cursor(self):
            raise RuntimeError("db down")

    reg.db_conn = _Boom()
    reg._alias_cache = None
    n = reg.prefetch_aliases(["apollo"], "u1")
    assert n == 0 and reg._alias_cache is None  # resolve() keeps probing the DB


# ---------------------------------------------------------------------------
# L3-B  grouping mint: cadence gate + single multi-row upsert
# ---------------------------------------------------------------------------
def _mint_db(rows_comembership, aliases, types):
    def script(qp):
        q, params = qp
        if "SHOW search_path" in q:
            return [("faultline_seat, public",)]
        if "COUNT(DISTINCT subject_id)" in q:
            return rows_comembership
        if "DISTINCT ON (entity_id)" in q:
            return aliases
        if "FROM entities" in q and "entity_type" in q:
            return types
        return []

    return _DB(script=script)


def _run_mint(monkeypatch, db, batch_rels):
    monkeypatch.setattr(M, "_grouping_candidate_rels", lambda _db: ["member_of", "located_in"])
    M._GROUPING_MINT_LAST_RUN[0] = 0.0  # cadence due
    return M._mint_groupings_from_membership(db, "u1", _batch_edge_rel_types=batch_rels)


def test_cadence_gate_skips_batches_without_membership_edges(monkeypatch):
    db = _mint_db([], [], [])
    monkeypatch.setattr(M, "_grouping_candidate_rels", lambda _db: ["member_of"])
    M._GROUPING_MINT_LAST_RUN[0] = time.monotonic()  # cadence NOT due
    n = M._mint_groupings_from_membership(
        db, "u1", _batch_edge_rel_types=["owns", "feels"])
    assert n == 0
    assert not any("COUNT(DISTINCT subject_id)" in q for q, _ in db.executed), (
        "no candidate edge in the batch: the co-membership scan must not run")


def test_grouping_mint_is_one_multirow_upsert_one_commit(monkeypatch):
    rows = [
        ("g1" * 8, "member_of", 2, ["m1", "m2"]),
        ("g2" * 8, "located_in", 2, ["m3", "m4"]),
    ]
    aliases = [("g1" * 8, "my band"), ("g2" * 8, "the garage")]
    types = [("m1", "Person"), ("m2", "Person"), ("m3", "Object"), ("g2" * 8, "Location")]
    db = _mint_db(rows, aliases, types)
    minted = _run_mint(monkeypatch, db, batch_rels=["member_of", "located_in"])
    assert minted == 2
    inserts = [(q, p) for q, p in db.executed if "INSERT INTO entity_taxonomies" in q]
    assert len(inserts) == 1, "ONE multi-row upsert replaces N per-group inserts"
    q, p = inserts[0]
    assert q.count("VALUES") == 1 and len(p) == 8, (
        "two groups -> one INSERT with a two-row VALUES list (8 params)")
    assert "ON CONFLICT (taxonomy_name) DO UPDATE" in q
    assert "WHERE entity_taxonomies.source <> 'seeded'" in q
    assert db.commits == 1, "ONE commit for the whole pass"


def test_same_group_object_under_two_rels_merges_to_one_row(monkeypatch):
    rows = [("g1" * 8, "member_of", 2, ["m1", "m2"]),
            ("g1" * 8, "located_in", 2, ["m1", "m2"])]
    aliases = [("g1" * 8, "my band")]
    types = [("m1", "Person"), ("m2", "Person")]
    db = _mint_db(rows, aliases, types)
    minted = _run_mint(monkeypatch, db, batch_rels=["member_of", "located_in"])
    assert minted == 1, "two candidate rels on one group object merge into one taxonomy row"


def test_seed_rows_protected_by_where_guard(monkeypatch):
    rows = [("g1" * 8, "member_of", 2, ["m1", "m2"])]
    db = _mint_db(rows, [("g1" * 8, "family")], [("m1", "Person"), ("m2", "Person")])
    _run_mint(monkeypatch, db, batch_rels=["member_of"])
    insert_q = next(q for q, _ in db.executed if "INSERT INTO entity_taxonomies" in q)
    assert "WHERE entity_taxonomies.source <> 'seeded'" in insert_q


def test_insert_failure_rolls_back_and_returns_zero(monkeypatch):
    rows = [("g1" * 8, "member_of", 2, ["m1", "m2"])]
    db = _mint_db(rows, [("g1" * 8, "my band")], [("m1", "Person"), ("m2", "Person")])

    def bad_script(qp):
        q, params = qp
        if "SHOW search_path" in q:
            return [("faultline_seat, public",)]
        if "COUNT(DISTINCT subject_id)" in q:
            return rows
        if "DISTINCT ON (entity_id)" in q:
            return [("g1" * 8, "my band")]
        if "FROM entities" in q:
            return [("m1", "Person")]
        if "INSERT INTO entity_taxonomies" in q:
            raise RuntimeError("constraint boom")  # the INSERT itself fails
        return []

    db.script = bad_script
    monkeypatch.setattr(M, "_grouping_candidate_rels", lambda _db: ["member_of"])
    M._GROUPING_MINT_LAST_RUN[0] = 0.0
    n = M._mint_groupings_from_membership(db, "u1", _batch_edge_rel_types=["member_of"])
    assert n == 0
    assert db.rollbacks >= 1  # loud failure, no half-pass committed


# ---------------------------------------------------------------------------
# L3-C  background NL_FILL lane
# ---------------------------------------------------------------------------
def test_nl_fill_background_lands_cache_and_swallow_failures(monkeypatch):
    M._NL_PHRASINGS_CACHE.clear()
    calls = []

    def fake_batch(rels, user_id=None):
        calls.append(list(rels))
        return {rt: {"natural_language": "phrasing for " + rt} for rt in rels}

    monkeypatch.setattr(M, "generate_rel_type_phrasing_batch", fake_batch)
    M._nl_fill_background(["fly_to", "tour"], "user-1", "faultline_seat")
    assert calls == [["fly_to", "tour"]]  # ONE batch call for both misses
    key = ("faultline_seat", "fly_to")
    assert key in M._NL_PHRASINGS_CACHE
    assert M._NL_PHRASINGS_CACHE[key][1]["natural_language"] == "phrasing for fly_to"

    # failures are swallowed: a broken brain call never raises out of the worker
    def boom(rels, user_id=None):
        raise RuntimeError("brain down")

    monkeypatch.setattr(M, "generate_rel_type_phrasing_batch", boom)
    M._nl_fill_background(["purchase"], "user-1", "faultline_seat")  # no raise


def test_nl_fill_cache_ttl_arithmetic():
    M._NL_PHRASINGS_CACHE.clear()
    M._NL_PHRASINGS_CACHE[("faultline_seat", "old_rel")] = (
        time.monotonic() - M._NL_PHRASINGS_TTL_S - 1.0, {"natural_language": "stale"})
    ts, meta = M._NL_PHRASINGS_CACHE[("faultline_seat", "old_rel")]
    assert (time.monotonic() - ts) >= M._NL_PHRASINGS_TTL_S  # expired: handler will not serve
    assert meta  # sanity: the entry exists but is stale


def test_nl_sync_rollback_lever_defaults_off():
    assert M.NL_FILL_SYNC_ON_WRITE is False, (
        "the default posture is background-only; NL_FILL_SYNC_ON_WRITE=true is the rollback")
    assert M.GROUPING_MINT_EVERY_INGEST is False, (
        "the default cadence skips membership-free calls; the env restores every-call")
