"""Unit tests for the re-embedder EXECUTION GATE (`_tenant_has_reembed_work`) and the
loop-level skip/isolation contract it drives.

Fast, DB-free: a faked psycopg2-style connection/cursor answers the gate's indexed
EXISTS/LIMIT-1 probes from an in-memory set of "present work types". No live Postgres,
no Qdrant, no embedding model.

Covers (per task):
  * gate returns True when EACH work-type is present (one per probe),
  * gate returns False ONLY when ALL work-types are absent,
  * a gate-probe raising an exception → treated as "has work" (fail-safe / we-don't-forget),
  * the idle tenant is skipped while a tenant with one pending Class-C row is processed,
  * per-tenant exception isolation: one tenant raising does not stop the loop.
"""

import re

from src.re_embedder import embedder
from src.re_embedder.embedder import _tenant_has_reembed_work


# ── Fake DB plumbing ────────────────────────────────────────────────────────────────────

# Map a distinctive fragment of each probe's SQL to a work-type token. The gate SELECTs `1`
# for a matching row; we return (1,) when that work-type is "present", else no row (None).
_PROBE_SIGNATURES = [
    # (work_token, needle that uniquely identifies the probe SQL)
    ("staged_unsynced", "qdrant_synced = false and promoted_at is null"),
    ("staged_b_promote", "fact_class = 'b' and confirmed_count >= 3"),
    ("staged_c_hits", "fact_class = 'c' and hit_count >= 3"),
    ("staged_c_expiry", "fact_class = 'c' and expires_at <= now()"),
    ("facts_unsynced", "from facts"),
    ("ontology", "from ontology_evaluations"),
    ("name_conflicts", "from entity_name_conflicts"),
]


def _classify(sql: str) -> str | None:
    s = re.sub(r"\s+", " ", sql.strip().lower())
    for token, needle in _PROBE_SIGNATURES:
        if needle in s:
            return token
    return None


class FakeCursor:
    def __init__(self, present: set, raise_on: str | None = None):
        self._present = present
        self._raise_on = raise_on
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        token = _classify(sql)
        if self._raise_on is not None and token == self._raise_on:
            raise RuntimeError(f"simulated probe failure on {token}")
        if token is not None:
            self._result = (1,) if token in self._present else None
        else:
            # Non-probe statements (e.g. SET search_path on the fail-safe path) — no row.
            self._result = None

    def fetchone(self):
        return self._result


class FakeConn:
    def __init__(self, present: set, raise_on: str | None = None):
        self._present = present
        self._raise_on = raise_on
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self._present, self._raise_on)

    def rollback(self):
        self.rolled_back = True


# ── Gate: True for each work-type present ────────────────────────────────────────────────

def test_gate_true_for_each_work_type_present():
    for token, _ in _PROBE_SIGNATURES:
        if token == "facts_unsynced":
            # Only counted as work in legacy (VECTOR_CLASS_C_ONLY OFF) mode — tested below.
            continue
        conn = FakeConn(present={token})
        assert _tenant_has_reembed_work(conn, "faultline_t") is True, (
            f"gate should report work when {token} is present"
        )


def test_gate_false_only_when_all_absent():
    conn = FakeConn(present=set())
    assert _tenant_has_reembed_work(conn, "faultline_idle") is False


# ── Gate: facts-table probe is legacy-only (VECTOR_CLASS_C_ONLY) ──────────────────────────

def test_facts_unsynced_ignored_when_class_c_only(monkeypatch):
    # Default C-only mode: an unsynced A/B facts row is NOT work (the facts sync loop is skipped).
    monkeypatch.setattr(embedder, "_VECTOR_CLASS_C_ONLY", True)
    conn = FakeConn(present={"facts_unsynced"})
    assert _tenant_has_reembed_work(conn, "faultline_t") is False


def test_facts_unsynced_is_work_when_legacy_mode(monkeypatch):
    # Legacy mode (flag OFF): the facts-table sync runs, so unsynced A/B IS work.
    monkeypatch.setattr(embedder, "_VECTOR_CLASS_C_ONLY", False)
    conn = FakeConn(present={"facts_unsynced"})
    assert _tenant_has_reembed_work(conn, "faultline_t") is True


# ── Gate: fail-safe on probe error → has work, never raises ───────────────────────────────

def test_gate_probe_exception_is_failsafe_true():
    # An idle tenant whose FIRST probe raises must still be PROCESSED (never skipped on error).
    conn = FakeConn(present=set(), raise_on="staged_unsynced")
    assert _tenant_has_reembed_work(conn, "faultline_broken") is True
    assert conn.rolled_back is True, "fail-safe path should roll back the aborted txn"


def test_gate_never_raises():
    # Even a totally broken connection returns True rather than propagating.
    class ExplodingConn:
        def cursor(self):
            raise RuntimeError("connection is dead")

        def rollback(self):
            raise RuntimeError("rollback also dead")

    assert _tenant_has_reembed_work(ExplodingConn(), "faultline_dead") is True


# ── Loop contract: idle skipped, active processed, per-tenant isolation ───────────────────

def _run_gated_loop(tenants):
    """Faithful reproduction of main()'s PHASE 2b gate+isolation contract:
    per-tenant try/except+continue, gate-skip before the expensive pass, and an
    activity counter that drives the reconcile cadence.

    `tenants` is a list of (name, FakeConn, work_fn) where work_fn() is the (mocked)
    expensive pass and may raise. Returns (processed, skipped, active_count, errors).
    """
    processed, skipped, errors = [], [], []
    active_count = 0
    for name, conn, work_fn in tenants:
        try:
            if not _tenant_has_reembed_work(conn, name):
                skipped.append(name)
                continue
            active_count += 1
            work_fn()  # the expensive pass (mocked)
            processed.append(name)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            errors.append(name)
            continue
    return processed, skipped, active_count, errors


def test_idle_tenant_skipped_active_tenant_processed():
    calls = {"idle": 0, "busy": 0}

    idle = FakeConn(present=set())                        # nothing due
    busy = FakeConn(present={"staged_unsynced"})          # one pending Class-C row

    tenants = [
        ("idle", idle, lambda: calls.__setitem__("idle", calls["idle"] + 1)),
        ("busy", busy, lambda: calls.__setitem__("busy", calls["busy"] + 1)),
    ]
    processed, skipped, active, errors = _run_gated_loop(tenants)

    assert skipped == ["idle"]
    assert processed == ["busy"]
    assert active == 1, "only the busy tenant counts toward reconcile activity"
    assert calls["idle"] == 0, "the expensive pass must NOT run for an idle tenant"
    assert calls["busy"] == 1, "the expensive pass runs for a tenant with work"
    assert errors == []


def test_per_tenant_exception_isolation_does_not_stop_loop():
    def boom():
        raise RuntimeError("tenant B's pass blew up (e.g. incomplete schema)")

    a = FakeConn(present={"staged_unsynced"})
    b = FakeConn(present={"ontology"})
    c = FakeConn(present={"name_conflicts"})

    ran = []
    tenants = [
        ("a", a, lambda: ran.append("a")),
        ("b", b, boom),                       # raises mid-loop
        ("c", c, lambda: ran.append("c")),
    ]
    processed, skipped, active, errors = _run_gated_loop(tenants)

    assert errors == ["b"], "the failing tenant is recorded but isolated"
    assert ran == ["a", "c"], "tenants after the failing one still process"
    assert processed == ["a", "c"]
    assert active == 3, "all three had work (gate fired) even though one raised"
    assert b.rolled_back is True, "the failing tenant's txn is rolled back"
