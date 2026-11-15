"""Pins: REL_TYPE_CATEGORY_BULK_PRIME (BUG-R4-3 lane, the 11.6s prod family walk).

THE WOUND (measured live on prod, owner seat 10d7d879, 2026-11-14, cProfile on the
TTL-expired walk): ``_get_rel_type_category`` opens ONE fresh psycopg2 connection per
distinct rel_type once the 60s TTL memo expires — and a heavy seat carries hundreds of
distinct rel_types in a single walk's fact set (627 measured; 5,275 live on the seat).
627 connects × ~10ms = 6.4s of pure connect/auth inside ``_fetch_entity_facts``' per-row
category lookups — the "~11 unprofiled seconds" between ``descended_layered_facts`` and
``recall_walk_done``. Every human-paced conversation turn arrives after the TTL expired,
so the owner paid it on EVERY family query (11.6s vs a 2.0s warm floor).

THE FIX: one connection per TTL window bulk-primes the whole rel_types→category table
into the memo. Same table, same answers, same ≤TTL staleness bound; only the connection
COUNT changes (627 → 1). OFF = the per-key connect path, byte-for-byte.

Pins here:
  1. hermetic connect-storm collapse (ON: 1 connect; OFF: one per key) + answer parity;
  2. OFF byte-parity of the per-key SQL shape;
  3. prime-fault backoff + keyword-inference fallthrough;
  4. TTL window re-prime;
  5. DB-gated latency-shape pin (synthetic heavy schema: ON completes the 600-rel sweep
     under threshold with a bounded connect count; OFF pays per-key connects).
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import src.api.main as M  # noqa: E402


# ── shared fake psycopg2 ──────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, table, statements):
        self._table = table          # {rel_type_lower: category or None}
        self._statements = statements
        self._result = []

    def execute(self, sql, params=None):
        self._statements.append((sql.strip(), params))
        sql_l = " ".join(sql.lower().split())
        if sql_l.startswith("select rel_type, category from rel_types"):
            self._result = sorted(M._REL_TYPE_CATEGORY_MEMO and self._table.items()) \
                if False else sorted(self._table.items())
        elif sql_l.startswith("select category from rel_types where rel_type"):
            key = str(params[0]).lower() if params else ""
            cat = self._table.get(key)
            self._result = [(cat,)] if cat is not None else []
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
    def __init__(self, table, statements, connect_delay_s):
        self._table = table
        self._statements = statements
        self._delay = connect_delay_s
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._table, self._statements)

    def close(self):
        self.closed = True

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConnectError(Exception):
    pass


def _install_fake_psycopg2(monkeypatch, table, connect_delay_s=0.0, fail_connects=False):
    """Replace M.psycopg2.connect with a counting fake; returns (counts, statements)."""
    connects = {"n": 0}
    statements: list = []

    def fake_connect(dsn, **kw):
        connects["n"] += 1
        time.sleep(connect_delay_s)
        if fail_connects:
            raise _FakeConnectError("simulated outage")
        return _FakeConn(table, statements, connect_delay_s)

    monkeypatch.setattr(M.psycopg2, "connect", fake_connect)
    return connects, statements


def _reset_memo(monkeypatch):
    monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_MEMO", {})
    monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_WINDOW_PRIMED_AT", -1.0e18)
    monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_PRIME_FAILED_AT", -1.0e18)


# ── 1. the storm collapse + answer parity ─────────────────────────────────────

def test_bulk_prime_collapses_the_connect_storm(monkeypatch):
    """600 distinct rel_types after TTL expiry: ON = ONE connect; OFF = one per key.
    Answers identical in both modes (known → DB category, unknown → static/keyword)."""
    known = {f"rel_known_{i}": ("cat_a" if i % 2 else None) for i in range(40)}
    rels = list(known) + [f"rel_unknown_{i}" for i in range(560)]
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake/relcat")
    monkeypatch.setattr(M, "_REL_TYPE_META", {})
    _reset_memo(monkeypatch)

    # ON
    monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_BULK_PRIME", True)
    connects_on, _ = _install_fake_psycopg2(monkeypatch, known, connect_delay_s=0.01)
    t0 = time.monotonic()
    answers_on = {r: M._get_rel_type_category(r) for r in rels}
    wall_on = time.monotonic() - t0
    assert connects_on["n"] == 1, f"flag ON must prime in ONE connect, got {connects_on['n']}"
    assert wall_on < 1.0, f"600 lookups must be memo-fast, took {wall_on:.2f}s"

    # OFF (byte-parity control)
    _reset_memo(monkeypatch)
    monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_BULK_PRIME", False)
    connects_off, _ = _install_fake_psycopg2(monkeypatch, known, connect_delay_s=0.0)
    answers_off = {r: M._get_rel_type_category(r) for r in rels}
    assert connects_off["n"] >= len(rels), (
        f"flag OFF must pay the per-key storm (>= {len(rels)} connects), "
        f"got {connects_off['n']}")
    assert answers_on == answers_off, "flag ON/OFF must answer identically"
    # the measured wound shape: known rels answer from the DB tier
    assert answers_on["rel_known_1"] == "cat_a"


# ── 2. OFF byte-parity of the SQL shape ───────────────────────────────────────

def test_off_keeps_per_key_sql_shape(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake/relcat")
    _reset_memo(monkeypatch)
    monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_BULK_PRIME", False)
    connects, statements = _install_fake_psycopg2(monkeypatch, {"owns": "family"})
    M._get_rel_type_category("owns")
    M._get_rel_type_category("owns")  # memo hit — no second connect
    assert connects["n"] == 1
    assert statements == [("SELECT category FROM rel_types WHERE rel_type = %s", ("owns",))], (
        f"OFF must issue exactly today's per-key SQL, got {statements}")


def test_on_issues_the_bulk_prime_sql(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake/relcat")
    _reset_memo(monkeypatch)
    monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_BULK_PRIME", True)
    connects, statements = _install_fake_psycopg2(monkeypatch, {"owns": "family"})
    assert M._get_rel_type_category("owns") == "family"
    assert connects["n"] == 1
    assert statements == [("SELECT rel_type, category FROM rel_types", None)], (
        f"ON must prime with the single bulk read, got {statements}")


# ── 3. prime fault: backoff + keyword fallthrough ─────────────────────────────

def test_prime_failure_backs_off_and_falls_through(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake/relcat")
    monkeypatch.setattr(M, "_REL_TYPE_META", {})
    _reset_memo(monkeypatch)
    monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_BULK_PRIME", True)
    connects, _ = _install_fake_psycopg2(monkeypatch, {}, fail_connects=True)
    # keyword-inference fallbacks must not raise, and connect attempts are backed off
    assert M._get_rel_type_category("family_rites") == "family"  # keyword tier
    first_attempts = connects["n"]
    assert first_attempts >= 1
    M._get_rel_type_category("family_rites")
    M._get_rel_type_category("family_ties")
    assert connects["n"] == first_attempts, "backoff must bound retry cadence after a fault"


# ── 4. TTL window re-prime ────────────────────────────────────────────────────

def test_window_reprimes_after_ttl(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake/relcat")
    monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_TTL_S", 0.05)
    _reset_memo(monkeypatch)
    monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_BULK_PRIME", True)
    connects, _ = _install_fake_psycopg2(monkeypatch, {"owns": "family"})
    M._get_rel_type_category("owns")
    assert connects["n"] == 1
    time.sleep(0.08)
    assert M._get_rel_type_category("owns") == "family"
    assert connects["n"] == 2, "an expired window must re-prime (novel rel_types ≤ TTL)"


# ── 5. DB-gated latency-shape pin (synthetic heavy schema) ────────────────────

@pytest.mark.usefixtures("requires_postgres")
class TestHeavySchemaLatencyShape:
    def test_heavy_schema_sweep_under_threshold(self, postgres_dsn, monkeypatch):
        """A synthetic heavy schema (600 rel_types) must complete the category sweep
        in ONE connect and under the threshold with the flag ON; OFF pays the storm.
        Throwaway schema, dropped in finally."""
        import psycopg2 as real_psycopg2

        schema = f"fl_relcat_pin_{uuid.uuid4().hex[:8]}"
        n_rels = 600
        conn = real_psycopg2.connect(postgres_dsn, connect_timeout=5)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA {schema}")
                cur.execute(f"CREATE TABLE {schema}.rel_types "
                            "(rel_type TEXT PRIMARY KEY, category TEXT)")
                cur.executemany(
                    f"INSERT INTO {schema}.rel_types (rel_type, category) VALUES (%s, %s)",
                    [(f"rel_pin_{i}", "cat_pin" if i % 3 else None) for i in range(n_rels)])
            # scoped DSN: the helper's fresh connections resolve rel_types in the
            # throwaway schema first (options search_path), exactly like a tenant.
            scoped = ("postgresql://" + postgres_dsn.split("://", 1)[1]
                      + "?options=-csearch_path%3D" + schema)
            monkeypatch.setenv("POSTGRES_DSN", scoped)
            monkeypatch.setattr(M, "_REL_TYPE_META", {})
            rels = [f"rel_pin_{i}" for i in range(n_rels)]

            counts = {"n": 0}
            _orig_connect = M.psycopg2.connect  # capture BEFORE patching (same module!)

            def counting_connect(dsn, **kw):
                counts["n"] += 1
                return _orig_connect(dsn, **kw)

            # ON
            _reset_memo(monkeypatch)
            monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_BULK_PRIME", True)
            monkeypatch.setattr(M.psycopg2, "connect", counting_connect)
            t0 = time.monotonic()
            answers_on = {r: M._get_rel_type_category(r) for r in rels}
            wall_on = time.monotonic() - t0
            connects_on = counts["n"]
            # OFF
            _reset_memo(monkeypatch)
            monkeypatch.setattr(M, "_REL_TYPE_CATEGORY_BULK_PRIME", False)
            counts["n"] = 0
            t0 = time.monotonic()
            answers_off = {r: M._get_rel_type_category(r) for r in rels}
            wall_off = time.monotonic() - t0
            connects_off = counts["n"]

            assert connects_on <= 2, f"ON must be bulk (≤1 prime + slack), got {connects_on}"
            assert connects_off >= n_rels, (
                f"OFF must pay one connect per key ({n_rels}), got {connects_off}")
            assert wall_on < max(1.0, wall_off / 3), (
                f"ON sweep {wall_on:.2f}s must be far under OFF {wall_off:.2f}s")
            assert answers_on == answers_off
            assert answers_on["rel_pin_1"] == "cat_pin"
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                conn.close()
