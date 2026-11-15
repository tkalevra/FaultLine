"""Unit tests for the re-embedder PER-TENANT ISOLATION contract:

  * ``tenant_schema_is_live`` — the deterministic GHOST-TENANT health probe that drops a
    dropped/empty schema (still flagged status='ready') from ready_schemas ONCE per cycle,
    so the "one bad tenant aborts the loop" cascade can never stall promotion for healthy
    tenants. (cortex bug-reembedder-cascade-and-tenant-hygiene.)
  * ``_rollback_and_reapply_search_path`` — the shared-connection isolation helper: on a
    per-tenant error it rolls the connection back + re-applies the tenant search_path so the
    NEXT tenant on the SAME connection proceeds clean (no "current transaction is aborted").

Fast, DB-free: fake psycopg2-style connection/cursor. No live Postgres, no Qdrant, no model.
Mirrors the style of ``tests/test_reembedder_execution_gate.py``.
"""

import re

from src.re_embedder import embedder
from src.re_embedder.embedder import (
    tenant_schema_is_live,
    _rollback_and_reapply_search_path,
)


# ── Fake DB plumbing ────────────────────────────────────────────────────────────────────

def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().lower())


class FakeCursor:
    """Answers the health probe's ``SELECT to_regclass(%s)`` from a set of LIVE schemas.

    ``live_schemas`` holds the schema names whose ``<schema>.staged_facts`` "exists"; a probe
    for any other schema returns (None,) — the ghost signal. ``raise_on_regclass`` makes the
    probe raise (simulate a catalog hiccup / aborted admin txn) to exercise the fail-safe path.
    """

    def __init__(self, live_schemas: set, raise_on_regclass: bool = False):
        self._live = live_schemas
        self._raise = raise_on_regclass
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = _norm(sql)
        if "to_regclass" in s:
            if self._raise:
                raise RuntimeError("simulated catalog probe failure")
            # params[0] is like "faultline_alice.staged_facts"; live iff the schema is live.
            arg = (params[0] if params else "")
            schema = arg.split(".", 1)[0]
            self._result = (arg,) if schema in self._live else (None,)
        elif s.startswith("set search_path"):
            self._result = None
        else:
            self._result = None

    def fetchone(self):
        return self._result


class FakeConn:
    def __init__(self, live_schemas: set, raise_on_regclass: bool = False):
        self._live = live_schemas
        self._raise = raise_on_regclass
        self.rolled_back = 0
        self.search_path_reapplied = []

    def cursor(self):
        return _RecordingCursor(self)

    def rollback(self):
        self.rolled_back += 1


class _RecordingCursor(FakeCursor):
    """Cursor that also records SET search_path targets on its parent conn (for the
    _rollback_and_reapply_search_path re-apply assertion)."""

    def __init__(self, conn: FakeConn):
        super().__init__(conn._live, conn._raise)
        self._conn = conn

    def execute(self, sql, params=()):
        s = _norm(sql)
        if s.startswith("set search_path"):
            # "set search_path to faultline_x" → capture the target token.
            self._conn.search_path_reapplied.append(s.split("to", 1)[-1].strip())
            self._result = None
            return
        super().execute(sql, params)


# ── tenant_schema_is_live: live vs ghost vs fail-safe ─────────────────────────────────────

def test_live_schema_is_live():
    conn = FakeConn(live_schemas={"faultline_alice"})
    assert tenant_schema_is_live(conn, "faultline_alice") is True


def test_ghost_schema_is_not_live():
    # Dropped / 0-table schema: to_regclass(...) → NULL → NOT live.
    conn = FakeConn(live_schemas={"faultline_alice"})
    assert tenant_schema_is_live(conn, "faultline_ghost") is False


def test_empty_schema_name_is_not_live():
    conn = FakeConn(live_schemas=set())
    assert tenant_schema_is_live(conn, "") is False
    assert tenant_schema_is_live(conn, None) is False


def test_probe_error_is_failsafe_live_and_rolls_back():
    # A catalog hiccup must NEVER mass-skip a real tenant: fail-safe → True, and the aborted
    # admin txn is rolled back so the next tenant's probe runs clean.
    conn = FakeConn(live_schemas=set(), raise_on_regclass=True)
    assert tenant_schema_is_live(conn, "faultline_alice") is True
    assert conn.rolled_back == 1


def test_never_raises_on_dead_connection():
    class ExplodingConn:
        def cursor(self):
            raise RuntimeError("connection is dead")

        def rollback(self):
            raise RuntimeError("rollback also dead")

    assert tenant_schema_is_live(ExplodingConn(), "faultline_x") is True


# ── The chokepoint contract: ghosts dropped, live kept, ORDER preserved ───────────────────

def _filter_ready_schemas(admin_conn, ready_schemas):
    """Faithful reproduction of main()'s ghost-tenant chokepoint filter: keep only schemas the
    deterministic health probe reports live; a probe error keeps the tenant (fail-safe)."""
    live, ghosts = [], []
    for u, s in ready_schemas:
        if tenant_schema_is_live(admin_conn, s):
            live.append((u, s))
        else:
            ghosts.append((u, s))
    return live, ghosts


def test_chokepoint_drops_ghosts_keeps_live_in_order():
    admin = FakeConn(live_schemas={"faultline_a", "faultline_c"})
    ready = [
        ("u_a", "faultline_a"),   # live
        ("u_b", "faultline_b"),   # GHOST (dropped)
        ("u_c", "faultline_c"),   # live
        ("u_d", "faultline_d"),   # GHOST (dropped)
    ]
    live, ghosts = _filter_ready_schemas(admin, ready)

    assert live == [("u_a", "faultline_a"), ("u_c", "faultline_c")], \
        "only live schemas survive, original order preserved"
    assert ghosts == [("u_b", "faultline_b"), ("u_d", "faultline_d")]


def test_chokepoint_failsafe_keeps_all_when_probe_errors():
    # If the probe itself errors for every schema, NONE are dropped (fail-safe toward inclusion).
    admin = FakeConn(live_schemas=set(), raise_on_regclass=True)
    ready = [("u_a", "faultline_a"), ("u_b", "faultline_b")]
    live, ghosts = _filter_ready_schemas(admin, ready)
    assert live == ready
    assert ghosts == []


# ── Shared-connection isolation: a raising tenant does not poison the next ────────────────

def _run_shared_conn_loop(conn, tenants):
    """Reproduction of the SHARED-connection per-tenant pattern (fast_doc_drain_prepass / job7):
    all tenants run on ONE connection; on any per-tenant error we roll back + re-apply the
    tenant search_path via _rollback_and_reapply_search_path, then continue. Proves a broken
    tenant cannot leave the connection aborted for the next tenant.

    ``tenants`` is a list of (schema, work_fn); work_fn may raise. Returns (processed, errored).
    """
    processed, errored = [], []
    for schema, work_fn in tenants:
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
            work_fn()
            processed.append(schema)
        except Exception:
            _rollback_and_reapply_search_path(conn, schema)
            errored.append(schema)
            continue
    return processed, errored


def test_shared_connection_isolation_rolls_back_and_continues():
    conn = FakeConn(live_schemas={"faultline_a", "faultline_b", "faultline_c"})

    def boom():
        raise RuntimeError("tenant B: relation \"staged_facts\" does not exist")

    ran = []
    tenants = [
        ("faultline_a", lambda: ran.append("a")),
        ("faultline_b", boom),                       # dropped-schema style failure
        ("faultline_c", lambda: ran.append("c")),
    ]
    processed, errored = _run_shared_conn_loop(conn, tenants)

    assert errored == ["faultline_b"], "the failing tenant is isolated"
    assert ran == ["a", "c"], "tenants after the failing one still process on the same conn"
    assert processed == ["faultline_a", "faultline_c"]
    assert conn.rolled_back == 1, "the failing tenant's aborted txn was rolled back"
    # After the rollback the helper re-applies the tenant search_path so the connection is clean.
    assert "faultline_b" in conn.search_path_reapplied, \
        "search_path is re-applied after rollback (psycopg2 resets it) so the next unit is clean"
