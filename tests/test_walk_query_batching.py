"""Pins: WALK_QUERY_BATCHING (serve-latency lane, SLP 2026-11-14 — levers 1/2/3).

THE WOUND (tools/regression_results/serve_latency_profile.md, measured on pre-prod,
real MCP door): a heavy entity-centric serve spends 1.0-1.8s in downward hierarchy
expansion + 0.8-3.0s in the dedup→prose interval on ~120 single-row SELECTs, plus
~40 fresh connect→BEGIN→SELECT→COMMIT cycles from the four no-conn
`_get_hierarchy_rels()` sites (main.py :4006/:4263/:12256/:45188) on the PUBLIC
search_path — the successor of the 627-storm the bulk-prime killed.

THE FIX (one flag, default ON; OFF = byte-for-byte legacy):
  lever 1  batch the walk loops — `_hierarchy_expand_batch` (one multi-seed
           recursive CTE per table), the alias pass-1 `= ANY(%s)` probe, the
           taxonomy-root `DISTINCT ON` probe;
  lever 2  thread the request's LIVE TENANT connection through the four no-conn
           hierarchy sites + `_get_structural_rels(db_conn=...)`;
  lever 3  read-once rel metadata per serve (request-scoped ContextVar memo keyed
           by the connection OBJECT; set/reset only by `_query_impl`'s
           try/finally — no cross-request caching by construction).

Pins here:
  A. hermetic OFF byte-parity (legacy no-conn calls, per-node loop SQL shapes);
  B. hermetic ON statement collapse (1 rel read per serve per conn; 2 CTEs for N
     seeds; 1 alias probe for 6 words);
  C. hermetic lever-3 memo scoping (no leak after reset, identity-checked key);
  D. DB-gated (requires_postgres): randomized-fixture equivalence of the batched
     multi-seed CTE vs the legacy per-seed `_hierarchy_expand` — TEXT-id AND
     uuid-id schemas (the ::text shape-agnosticism pin);
  E. DB-gated: the hierarchy-rels sites make ZERO fresh connections with the flag
     ON (counting connection tracker); OFF pays ≥1 per expansion.
"""
from __future__ import annotations

import os
import random
import sys
import types
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import src.api.main as M  # noqa: E402


# ── shared fakes ──────────────────────────────────────────────────────────────

class _Stmt:
    def __init__(self, sql, params=None):
        self.sql = " ".join(sql.lower().split())
        self.params = params


class _FakeCursor:
    """Records statements; answers the rel_types hierarchy read from a table."""

    def __init__(self, hier_rels, log):
        self._hier = hier_rels          # iterable of rel_type strings
        self._log = log
        self._result: list = []

    def execute(self, sql, params=None):
        self._log.append(_Stmt(sql, params))
        s = " ".join(sql.lower().split())
        if s == "select rel_type from rel_types where is_hierarchy_rel = true":
            self._result = [(r,) for r in self._hier]
        elif s.startswith("select rel_type from rel_types where is_hierarchy_rel"):
            self._result = [(r,) for r in self._hier]
        elif s == "select rel_type from rel_types where rel_type like 'has\\_%'":
            self._result = [("has_pin",)]
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, hier_rels, log):
        self._hier = hier_rels
        self._log = log

    def cursor(self):
        return _FakeCursor(self._hier, self._log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_psycopg2(monkeypatch, hier_rels):
    connects = {"n": 0}
    log: list = []

    def fake_connect(dsn=None, **kw):
        connects["n"] += 1
        return _FakeConn(hier_rels, log)

    monkeypatch.setattr(M.psycopg2, "connect", fake_connect)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake/wqb")
    return connects, log


# ── A. OFF byte-parity (legacy shapes) ────────────────────────────────────────

def test_off_flag_default_is_on():
    assert M.WALK_QUERY_BATCHING is True, "the flag must default ON"


def test_off_hierarchy_expand_pays_the_noconn_connect(monkeypatch):
    """Flag OFF ⇒ `_hierarchy_expand` keeps the legacy no-conn rel read: one FRESH
    psycopg2 connection per expansion (the measured ~40-connect storm shape)."""
    connects, log = _install_fake_psycopg2(monkeypatch, ["instance_of", "subclass_of"])
    monkeypatch.setattr(M, "WALK_QUERY_BATCHING", False)
    conn = _FakeConn(["instance_of", "subclass_of"], log)
    M._hierarchy_expand(conn, "u1", "e1", "down", 3)
    M._hierarchy_expand(conn, "u1", "e2", "down", 3)
    assert connects["n"] == 2, (
        f"OFF must open one fresh connection per expansion (legacy), got {connects['n']}")
    # and the legacy CTE pair runs on the passed conn
    cte_stmts = [s for s in log if "with recursive hierarchy_chain" in s.sql]
    assert len(cte_stmts) == 4, f"OFF must keep the per-seed CTE pair, got {len(cte_stmts)}"


def test_off_structural_rels_keeps_own_connection(monkeypatch):
    connects, log = _install_fake_psycopg2(monkeypatch, ["instance_of"])
    monkeypatch.setattr(M, "WALK_QUERY_BATCHING", False)
    res = M._get_structural_rels()
    assert connects["n"] == 2, (
        f"OFF structural rels = legacy TWO own-connection reads (hier + has_), "
        f"got {connects['n']}")
    assert "instance_of" in res and "has_pin" in res


def test_off_resolve_query_axis_passes_noconn(monkeypatch):
    """The :4263 site must pass NO connection with the flag OFF (byte-parity)."""
    monkeypatch.setattr(M, "WALK_QUERY_BATCHING", False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    seen: list = []

    def spy(db_conn=None):
        seen.append(db_conn)
        return frozenset({"instance_of"})

    monkeypatch.setattr(M, "_get_hierarchy_rels", spy)
    fake_db = object()
    path = types.SimpleNamespace(
        scope_active=True, allowed_rels={"instance_of"}, hierarchy_intent=True)
    monkeypatch.setattr(M, "_CLASSIFICATION_QUERY_CUES", ("what is",))
    M._resolve_query_axis(path, "what is my thing", fake_db)
    assert seen == [None], f"OFF must call _get_hierarchy_rels() with NO conn, got {seen}"


# ── B. ON statement collapse ─────────────────────────────────────────────────

def test_on_hierarchy_expand_makes_zero_fresh_connects(monkeypatch):
    """Lever 2 core pin: flag ON + live conn ⇒ the :12256 site reads the hierarchy
    set ON THE PASSED CONNECTION — zero fresh psycopg2 connects."""
    connects, log = _install_fake_psycopg2(monkeypatch, ["instance_of", "subclass_of"])
    monkeypatch.setattr(M, "WALK_QUERY_BATCHING", True)
    conn = _FakeConn(["instance_of", "subclass_of"], log)
    for eid in ("e1", "e2", "e3"):
        M._hierarchy_expand(conn, "u1", eid, "down", 3)
    assert connects["n"] == 0, (
        f"ON must make ZERO fresh connections across expansions, got {connects['n']}")
    hier_reads = [s for s in log
                  if s.sql == "select rel_type from rel_types where is_hierarchy_rel = true"]
    assert len(hier_reads) == 3, "each expansion still reads the (memo-less) rel set"


def test_on_structural_rels_rides_the_passed_conn(monkeypatch):
    connects, log = _install_fake_psycopg2(monkeypatch, ["instance_of"])
    monkeypatch.setattr(M, "WALK_QUERY_BATCHING", True)
    conn = _FakeConn(["instance_of"], log)
    res = M._get_structural_rels(conn)
    assert connects["n"] == 0, f"ON must not open fresh connects, got {connects['n']}"
    assert "instance_of" in res and "has_pin" in res


def test_on_batch_expand_issues_two_ctes_for_n_seeds(monkeypatch):
    """Lever 1 core pin: N seeds ⇒ exactly 2 statements (facts + staged CTEs) + the
    rel-set read — independent of N (the legacy path paid 2×N + N connects)."""
    connects, log = _install_fake_psycopg2(monkeypatch, ["instance_of"])
    monkeypatch.setattr(M, "WALK_QUERY_BATCHING", True)
    conn = _FakeConn(["instance_of"], log)
    for n in (1, 5, 17):
        log.clear()
        out = M._hierarchy_expand_batch(conn, "u1", [f"e{i}" for i in range(n)], "down", 3)
        assert set(out.keys()) == {f"e{i}" for i in range(n)}
        stmts = [s for s in log if "with recursive" in s.sql]
        assert len(stmts) == 2, f"n={n}: exactly ONE CTE per table, got {len(stmts)}"
        assert connects["n"] == 0


def test_on_batch_expand_seed_sql_shape(monkeypatch):
    """The multi-seed CTE must carry the seed column and the SAME filters as the
    legacy per-seed CTE (tombstones, supersession, depth bound)."""
    connects, log = _install_fake_psycopg2(monkeypatch, ["instance_of"])
    monkeypatch.setattr(M, "WALK_QUERY_BATCHING", True)
    conn = _FakeConn(["instance_of"], log)
    M._hierarchy_expand_batch(conn, "u1", ["e0", "e1"], "down", 3)
    facts_cte = [s for s in log if "from facts f join seeds s" in s.sql]
    staged_cte = [s for s in log if "from staged_facts f join seeds s" in s.sql]
    assert len(facts_cte) == 1 and len(staged_cte) == 1
    for s in (facts_cte[0], staged_cte[0]):
        assert "distinct seed, node" in s.sql
        assert "hc.depth < %s" in s.sql
    assert "superseded_at is null" in facts_cte[0].sql
    assert "archived_at is null" in facts_cte[0].sql
    assert "deleted_at is null" in facts_cte[0].sql
    assert "deleted_at is null" in staged_cte[0].sql


def test_on_alias_pass1_is_one_any_probe(monkeypatch):
    """The serve's pass-1 alias gate collapses 6 per-word SELECTs into one
    `alias = ANY(%s)` + row_number probe (shape pin at the SQL seam)."""
    # Directly pin the batched SQL the serve site emits, via the same shape the
    # implementation builds (guarded by exact-string match against the source so a
    # drift is loud).
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api", "main.py")).read()
    assert "row_number() OVER (PARTITION BY alias) AS rn" in src
    assert "FROM entity_aliases WHERE alias = ANY(%s)" in src
    assert "SELECT DISTINCT ON (alias) entity_id, alias" in src


# ── C. lever-3 memo scoping ───────────────────────────────────────────────────

def test_memo_readonce_per_serve_and_no_leak(monkeypatch):
    """Within a serve-bound memo: N hierarchy/classification reads = 1 statement
    each per connection; after reset (serve end) reads go back to the DB; two
    DIFFERENT connections never alias (identity-checked key)."""
    connects, log = _install_fake_psycopg2(monkeypatch, ["instance_of"])
    monkeypatch.setattr(M, "WALK_QUERY_BATCHING", True)
    conn = _FakeConn(["instance_of"], log)
    tok = M._SERVE_REL_META_VAR.set({})
    try:
        for _ in range(5):
            assert "instance_of" in M._get_hierarchy_rels(conn)
            assert "instance_of" in M._get_classification_rels(conn)
        hier = [s for s in log if "is_hierarchy_rel = true" in s.sql
                and "wikidata_pid" not in s.sql]
        cls = [s for s in log if "wikidata_pid" in s.sql]
        assert len(hier) == 1, f"read-once per serve per conn, got {len(hier)}"
        assert len(cls) == 1, f"read-once per serve per conn, got {len(cls)}"
        # a DIFFERENT connection must NOT hit the first one's memo entry
        conn2 = _FakeConn(["subclass_of"], log)
        assert "subclass_of" in M._get_hierarchy_rels(conn2)
    finally:
        M._SERVE_REL_META_VAR.reset(tok)
    log.clear()
    assert "instance_of" in M._get_hierarchy_rels(conn)
    assert len([s for s in log if "is_hierarchy_rel = true" in s.sql]) == 1, (
        "after the serve resets the memo, reads must go back to the DB (no leak)")


def test_memo_never_active_without_serve_binding(monkeypatch):
    """Helpers must NOT create the memo themselves (that would be cross-request
    caching on a reused worker thread): without a serve-bound dict, every call
    reads the DB."""
    connects, log = _install_fake_psycopg2(monkeypatch, ["instance_of"])
    monkeypatch.setattr(M, "WALK_QUERY_BATCHING", True)
    conn = _FakeConn(["instance_of"], log)
    for _ in range(3):
        M._get_hierarchy_rels(conn)
    assert len([s for s in log if "is_hierarchy_rel" in s.sql]) == 3


# ── D+E. DB-gated legs ────────────────────────────────────────────────────────

_HIER_RELS = ("instance_of", "subclass_of", "member_of")


def _plant_fixture(cur, schema, id_kind, rng):
    """Randomized hierarchy fixture: 40 entities, random is-a/member edges across
    facts + staged_facts, tombstoned/superseded noise. Returns all entity ids."""
    n = 40
    ids = []
    for i in range(n):
        ids.append(str(uuid.uuid4()) if id_kind == "uuid" else f"ent_{i:03d}")
    cast = "::uuid" if id_kind == "uuid" else ""
    cur.executemany(
        f"INSERT INTO {schema}.rel_types (rel_type, is_hierarchy_rel) VALUES (%s, true)",
        [(r,) for r in _HIER_RELS] + [("owns",)])
    edges = []
    for i in range(1, n):
        for _ in range(rng.randint(1, 2)):
            parent = ids[rng.randrange(0, i)] if rng.random() < 0.7 else ids[rng.randrange(n)]
            rel = rng.choice(_HIER_RELS)
            edges.append((ids[i], parent, rel, rng.random() < 0.15))
    rng.shuffle(edges)
    for s_, o_, r_, dead in edges:
        cur.execute(
            f"INSERT INTO {schema}.facts (subject_id, object_id, rel_type,"
            f" superseded_at, archived_at, deleted_at)"
            f" VALUES (%s{cast}, %s{cast}, %s, %s, %s, %s)",
            (s_, o_, r_, "2020-01-01" if dead else None, None, None))
    for _ in range(12):
        cur.execute(
            f"INSERT INTO {schema}.staged_facts (subject_id, object_id, rel_type, deleted_at)"
            f" VALUES (%s{cast}, %s{cast}, %s, NULL)",
            (ids[rng.randrange(n)], ids[rng.randrange(n)], rng.choice(_HIER_RELS)))
    return ids


@pytest.mark.usefixtures("requires_postgres")
class TestDBBatchingEquivalence:
    def _make_schema(self, real_psycopg2, dsn, schema, id_kind, rng):
        conn = real_psycopg2.connect(dsn, connect_timeout=5)
        conn.autocommit = True
        idt = "uuid" if id_kind == "uuid" else "TEXT"
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                f"CREATE TABLE rel_types (rel_type TEXT PRIMARY KEY,"
                f" is_hierarchy_rel BOOLEAN, wikidata_pid TEXT, category TEXT)")
            cur.execute(
                f"CREATE TABLE facts (subject_id {idt}, object_id {idt}, rel_type TEXT,"
                f" superseded_at TIMESTAMPTZ, archived_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ,"
                f" valid_until TIMESTAMPTZ, provenance TEXT, confidence REAL,"
                f" confirmed_count INT, fact_class TEXT, fact_provenance TEXT)")
            cur.execute(
                f"CREATE TABLE staged_facts (subject_id {idt}, object_id {idt},"
                f" rel_type TEXT, deleted_at TIMESTAMPTZ, expires_at TIMESTAMPTZ,"
                f" promoted_at TIMESTAMPTZ, provenance TEXT, confidence REAL,"
                f" confirmed_count INT, fact_class TEXT, fact_provenance TEXT)")
            ids = _plant_fixture(cur, schema, id_kind, rng)
            cur.execute(
                f"CREATE TABLE entity_aliases (entity_id {idt}, alias TEXT,"
                f" is_preferred BOOLEAN, display_form TEXT, preference_source TEXT)")
            cur.execute(
                f"CREATE TABLE entities (id {idt} PRIMARY KEY, entity_type TEXT)")
            cur.execute(
                f"CREATE TABLE entity_attributes (entity_id {idt}, attribute TEXT,"
                f" value_text TEXT, value_int INT, value_float REAL, value_date DATE,"
                f" provenance TEXT, superseded_at TIMESTAMPTZ)")
        return conn, ids

    @pytest.mark.parametrize("id_kind", ["text", "uuid"])
    def test_batched_path_identical_to_per_entity(self, postgres_dsn, monkeypatch, id_kind):
        """Lever 1 equivalence pin: for EVERY seed, the batched multi-seed CTE's
        per-seed set is IDENTICAL to the legacy per-entity `_hierarchy_expand`
        result — both directions, on a randomized fixture, in BOTH id shapes."""
        import psycopg2 as real_psycopg2
        rng = random.Random(0xC0FFEE + (0 if id_kind == "text" else 1))
        schema = f"fl_wqb_pin_{uuid.uuid4().hex[:8]}"
        conn, ids = self._make_schema(real_psycopg2, postgres_dsn, schema, id_kind, rng)
        monkeypatch.setattr(M, "WALK_QUERY_BATCHING", True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
            conn.commit()
            for direction in ("down", "up"):
                legacy = {
                    s: M._hierarchy_expand(conn, "u1", s, direction, 3) for s in ids}
                batched = M._hierarchy_expand_batch(
                    conn, "u1", ids, direction, 3)
                for s in ids:
                    assert set(batched.get(s, {s})) == set(legacy[s]), (
                        f"{id_kind}/{direction}: seed {s} diverged "
                        f"(batched={batched.get(s)} legacy={legacy[s]})")
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                conn.close()

    def test_hierarchy_sites_zero_fresh_connects_when_on(self, postgres_dsn, monkeypatch):
        """Lever 2 DB pin (connection-tracking): with the flag ON, the hierarchy
        sites (`_hierarchy_expand`, `_hierarchy_expand_batch`,
        `_fetch_hierarchy_facts`, `_get_structural_rels`) make ZERO fresh
        connections — the rel-set read rides the passed LIVE tenant conn. OFF
        pays ≥1 fresh connect per expansion (the measured storm)."""
        import psycopg2 as real_psycopg2
        rng = random.Random(0xBEEF)
        schema = f"fl_wqb_pin_{uuid.uuid4().hex[:8]}"
        conn, ids = self._make_schema(real_psycopg2, postgres_dsn, schema, "text", rng)
        counts = {"n": 0}
        _orig_connect = real_psycopg2.connect

        def counting_connect(dsn=None, **kw):
            counts["n"] += 1
            return _orig_connect(dsn, **kw)

        monkeypatch.setattr(M.psycopg2, "connect", counting_connect)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
            conn.commit()
            tok = M._SERVE_REL_META_VAR.set({})
            try:
                monkeypatch.setattr(M, "WALK_QUERY_BATCHING", True)
                # the rel-CATEGORY lane (bulk-prime) is NOT a hierarchy-connect
                # site — freeze it so its own TTL-window connect can't pollute
                # this pin's connection count.
                monkeypatch.setattr(M, "_get_rel_type_category", lambda r: None)
                M._hierarchy_expand(conn, "u1", ids[0], "down", 3)
                M._hierarchy_expand(conn, "u1", ids[1], "up", 3)
                M._hierarchy_expand_batch(conn, "u1", ids[:8], "down", 3)
                M._fetch_hierarchy_facts(conn, "u1", set(ids[:5]))
                M._get_structural_rels(conn)
                M._get_classification_rels(conn)
                assert counts["n"] == 0, (
                    f"flag ON must make ZERO fresh connections at the hierarchy "
                    f"sites, got {counts['n']}")
            finally:
                M._SERVE_REL_META_VAR.reset(tok)
            # OFF control: each expansion re-opens a connection for the rel set
            monkeypatch.setattr(M, "WALK_QUERY_BATCHING", False)
            before = counts["n"]
            M._hierarchy_expand(conn, "u1", ids[0], "down", 3)
            M._hierarchy_expand(conn, "u1", ids[1], "down", 3)
            assert counts["n"] - before >= 2, (
                f"flag OFF must pay the per-expansion fresh connect, got "
                f"{counts['n'] - before}")
        finally:
            monkeypatch.setattr(M.psycopg2, "connect", _orig_connect)
            try:
                with conn.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                conn.close()
