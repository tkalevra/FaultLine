"""BUG-005 drain resilience — lease reclaim visibility, startup gate, rate_deferred backoff.

Prod evidence (DEV/BUGS/OWUI_test/BUG-005-doc-drain-wedge-reframe-rate-deferred.md,
2026-10-20): the doc-drain daemon wedged twice over.
  (a) a worker death mid-doc strands the doc at status='processing' FOREVER (the atomic
      claim only took 'pending') — doc_id=10 orphaned 2h+, manual SQL flip required;
  (b) at boot the drain thread works before uvicorn binds the API port, spamming
      document_drain_backend_unreachable for ~60s;
  (c) a rate_deferred re-pend was immediately claimable, so the drain re-ran the full
      harvest-turn timeout ladder against the very budget that had just refused it —
      129/133 chunks died that way while the retry ladder consumed the 40 RPM budget.

WHAT EACH PIN BUYS (mutation-tested: revert the behaviour, the named test goes red):
  • the reclaim arms in the candidate SELECT + claim SQL, and the prepass probe widened
    to 'processing' — without the probe widening the reclaim is UNREACHABLE from the
    daemon lane for a tenant whose only row is stranded (probe sees nothing to drain);
  • the startup gate: waits (bounded) for /health before the first claim, proceeds
    loudly on expiry, and is skipped entirely when DOC_BACKEND_READY_PROBE is OFF;
  • the rate_deferred re-pend stamps started_at=now() (the backoff) instead of NULL,
    and the claim refuses pending rows inside the backoff window — while a FRESH
    ingest (started_at NULL) and a NON-paced defer (breaker paces itself) keep their
    exact prior claimability.

The fakes model the REAL claim semantics the way test_document_brain_defer_clock.py
does: SQL-text is captured verbatim, the eligibility predicates (pending/backoff,
processing/lease) are evaluated against modelled row state, and the claim recheck can
simulate a steal between SELECT and claim. Where a statement names a column a modelled
schema lacks, the fake raises the REAL psycopg2.errors.UndefinedColumn — a fake that
"answers" SQL the real DB would reject is how a dead guard once passed six tests.
"""
import logging
from datetime import datetime, timezone

import psycopg2  # the fakes raise its real exception types — SQL-faithful
import pytest

import src.re_embedder.embedder as embedder


# ── the SQL-faithful fake ────────────────────────────────────────────────────────────

class _Cursor:
    """Models the drain's claim ladder against ONE modelled documents row."""

    def __init__(self, conn):
        self._conn = conn
        self._last = None
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql_norm = " ".join(str(sql).split())
        self._conn.statements.append((sql_norm, params))
        self._last = (sql_norm, params)
        self._row = None
        if "information_schema.columns" in sql_norm:
            # capability probes: (1,) = migrated (this suite models a current schema)
            self._row = (1,)
        elif "SELECT id FROM documents" in sql_norm:
            # candidate SELECT — params: (backoff_s, lease_s, batch)
            backoff_s, lease_s = params[0], params[1]
            self._conn.listed = self._conn.eligible(backoff_s, lease_s)
        elif "SET status = 'processing'" in sql_norm:
            # a claim (rung 1/2 shape: names attempts + the reclaim/backoff arms).
            if "attempts = attempts + 1" in sql_norm and \
                    not getattr(self._conn, "has_attempts", True):
                raise psycopg2.errors.UndefinedColumn(
                    "column documents.attempts does not exist")
            # the claim RECHECKS eligibility (params: doc_id, backoff_s, lease_s) —
            # and may lose a race to another worker:
            if self._conn.steal_between_select_and_claim or \
                    not self._conn.eligible(params[1], params[2]):
                self._row = None  # rowcount 0 → the drain skips, never crashes
                return
            self._conn.attempts += 1
            self._row = (self._conn.chunks, "ref", self._conn.attempts, None)

    def fetchall(self):
        return [(7,)] if getattr(self._conn, "listed", False) else []

    def fetchone(self):
        return self._row


class _Conn:
    """One modelled documents row: status + how long ago started_at was stamped.

    eligible() is the SEMANTICS the SQL is supposed to implement — the SQL-text pins
    below prove the predicates are actually in the statements, these tests prove the
    drain honours them. started_at=None models a fresh ingest (never claimed).
    """

    def __init__(self, status="pending", started_age_s=None, chunks=("chunk",),
                 steal_between_select_and_claim=False):
        self.status = status
        self.started_age_s = started_age_s
        self.chunks = list(chunks)
        self.attempts = 0
        self.steal_between_select_and_claim = steal_between_select_and_claim
        self.statements = []
        self.listed = False

    def eligible(self, backoff_s, lease_s):
        if self.status == "pending":
            if self.started_age_s is None:
                return True  # the NULL arm: a fresh ingest is always claimable
            return self.started_age_s > self._as_seconds(backoff_s)
        if self.status == "processing":
            # the lease arm: started_at IS NOT NULL AND older than the lease
            if self.started_age_s is None:
                return False
            return self.started_age_s > self._as_seconds(lease_s)
        return False

    @staticmethod
    def _as_seconds(v):
        """The lease rides SQL as a '<N> <unit>' interval string, the backoff as float
        seconds — mirror both shapes the real types would arrive in."""
        if isinstance(v, (int, float)):
            return float(v)
        n, _, unit = str(v).partition(" ")
        mult = {"second": 1.0, "seconds": 1.0, "minute": 60.0, "minutes": 60.0,
                "hour": 3600.0, "hours": 3600.0, "day": 86400.0, "days": 86400.0}
        return float(n) * mult[unit.strip()]

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _drain_one_doc(monkeypatch, conn, chunk_error=None):
    """Drive the REAL drain against the modelled row; chunk_error simulates the brain."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)

    def _edges(*a, **k):
        if chunk_error is not None:
            raise chunk_error
        return None

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: None)
    return embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)


def _stmts(conn, needle):
    return [(s, p) for s, p in conn.statements if needle in s]


# ── (a) stale-lease reclaim: a stuck 'processing' row is re-claimable ────────────────

def test_stale_processing_row_is_reclaimed(monkeypatch):
    """A row stranded in 'processing' past the lease (worker died mid-doc) is listed by
    the candidate SELECT and TAKEN by the claim — the drain actually processes it.
    RED if the reclaim arm is dropped from either statement (doc #10 strands forever)."""
    conn = _Conn(status="processing", started_age_s=3600)  # lease default is 30 min
    monkeypatch.setattr(embedder, "_DOC_CLAIM_LEASE", "30 minutes")
    _drain_one_doc(monkeypatch, conn,
                   chunk_error=embedder.DocumentBrainUnavailable("circuit_breaker_open"))
    selects = _stmts(conn, "SELECT id FROM documents")
    assert selects, "the drain must consult the candidate list"
    sel_sql = selects[0][0]
    assert "status = 'processing'" in sel_sql and "%s::interval" in sel_sql, (
        "the candidate SELECT must carry the processing-lease reclaim arm")
    claims = _stmts(conn, "SET status = 'processing'")
    assert claims, "a stale 'processing' row must be CLAIMED, not skipped"
    assert conn.attempts == 1, "the reclaim must consume the bounded-redelivery budget"
    assert _stmts(conn, "SET status = 'pending'"), (
        "the reclaimed document must have been processed (then re-pended by the outage "
        "handler) — proof the claim really handed it to the drain")


def test_fresh_processing_row_is_not_stolen_midflight(monkeypatch):
    """A row claimed seconds ago by a LIVE worker is NOT eligible: the lease protects
    in-flight drains. RED if the lease arm loses its started_at-age comparison."""
    conn = _Conn(status="processing", started_age_s=60)  # way inside a 30-min lease
    _drain_one_doc(monkeypatch, conn,
                   chunk_error=embedder.DocumentBrainUnavailable("circuit_breaker_open"))
    assert not _stmts(conn, "SET status = 'processing'"), (
        "a freshly-claimed 'processing' row (live worker) must never be re-claimed")


def test_prepass_probe_covers_stranded_processing_rows(monkeypatch):
    """THE BUG-005 REACHABILITY HOLE: the reclaim arm in the claim is unreachable from
    the daemon lane when the only stranded row is 'processing' — the prepass's cheap
    probe must look at BOTH statuses or the tenant is skipped before the drain runs."""
    captured = []
    calls = []

    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            captured.append(" ".join(str(sql).split()))

        def fetchone(self):
            return (1,)  # "this tenant has a drainable row"

    class _ConnP:
        def cursor(self):
            return _C()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(embedder.psycopg2, "connect", lambda dsn: _ConnP())
    monkeypatch.setattr(embedder, "_reembedder_bind_tenant_brain", lambda s: None)
    monkeypatch.setattr(embedder, "_reembedder_clear_tenant_brain", lambda: None)

    def _drain_recorder(*a, **k):
        calls.append(1)
        return 0

    monkeypatch.setattr(embedder, "drain_pending_documents", _drain_recorder)
    embedder.fast_drain_pending_documents_prepass(
        "dsn", [("user1", "schema1")], set(), "http://backend")
    assert calls, "a tenant with only a stuck 'processing' row must still be drained"
    probes = [s for s in captured if "SELECT 1 FROM documents" in s]
    assert probes and "status IN ('pending', 'processing')" in probes[0], (
        "the cheap probe must cover the reclaimable status pair")


def test_prepass_probe_skips_empty_tenants(monkeypatch):
    """The widened probe must not turn the prepass into an unconditional per-tenant
    drain: nothing to drain → no bind, no drain call."""
    calls = []

    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return None  # no pending, no processing rows

    class _ConnP:
        def cursor(self):
            return _C()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(embedder.psycopg2, "connect", lambda dsn: _ConnP())
    monkeypatch.setattr(embedder, "_reembedder_bind_tenant_brain",
                        lambda s: pytest.fail("must not bind a brain for an empty tenant"))
    monkeypatch.setattr(embedder, "drain_pending_documents",
                        lambda *a, **k: calls.append(1) or 0)
    embedder.fast_drain_pending_documents_prepass(
        "dsn", [("user1", "schema1")], set(), "http://backend")
    assert not calls


# ── (b) startup gate: no claim before /health, bounded, loud on expiry ───────────────

def test_startup_gate_waits_until_health_answers(monkeypatch):
    """The gate re-probes until the backend answers READY — the first claim cycle waits
    out the uvicorn bind window instead of spamming unreachable-WARNs. RED if the gate
    returns after a single failed probe."""
    states = iter([embedder.BACKEND_DOWN, embedder.BACKEND_DOWN, embedder.BACKEND_READY])
    monkeypatch.setattr(embedder, "_backend_state", lambda url: next(states))
    slept = []
    monkeypatch.setattr(embedder.time, "sleep", lambda s: slept.append(s))
    embedder._doc_drain_startup_gate("http://backend")
    assert len(slept) == 2, "must keep waiting while /health does not answer"


def test_startup_gate_is_bounded_and_proceeds_loudly(monkeypatch, caplog):
    """NEVER hang at boot: with the budget exhausted the gate returns anyway and says so
    at CRITICAL — an unstarted drain is silent loss, worse than any log line."""
    monkeypatch.setattr(embedder, "_backend_state", lambda url: embedder.BACKEND_DOWN)
    monkeypatch.setattr(embedder, "_DOC_DRAIN_STARTUP_GATE_TIMEOUT", 0.0)
    monkeypatch.setattr(embedder.time, "sleep",
                        lambda s: pytest.fail("must not sleep past a zero budget"))
    with caplog.at_level(logging.CRITICAL, logger="src.re_embedder.embedder"):
        embedder._doc_drain_startup_gate("http://backend")
    assert "doc_drain_startup_gate_expired" in caplog.text, (
        "the expiry must be LOUD — one CRIT, not silence")


def test_startup_gate_respects_probe_off(monkeypatch):
    """An operator who disabled DOC_BACKEND_READY_PROBE asked for claim-anyway; the gate
    keys on the same probe and must not silently reinstate it."""
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder, "_backend_state",
                        lambda url: pytest.fail("no probe may run when the flag is OFF"))
    embedder._doc_drain_startup_gate("http://backend")  # returns immediately, no hang


def test_standalone_loop_gates_before_its_first_drain(monkeypatch):
    """The daemon must run the gate BEFORE its first pre-pass — that ordering is the
    whole fix for the boot-spam window. RED if the gate call is dropped or moved after
    the loop."""
    order = []
    monkeypatch.setattr(embedder, "_doc_drain_startup_gate",
                        lambda url: order.append("gate"))
    monkeypatch.setattr(embedder, "_saas_suspended_user_ids", lambda dsn, ids: set())

    def _prepass(*a, **k):
        order.append("drain")
        raise RuntimeError("one pass is all this test needs")

    monkeypatch.setattr(embedder, "fast_drain_pending_documents_prepass", _prepass)

    class _LoopCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [("user1", "schema1")]

    class _LoopConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _LoopCursor()

    monkeypatch.setattr(embedder.psycopg2, "connect", lambda dsn: _LoopConn())

    sleeps = {"n": 0}

    def _sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise KeyboardInterrupt()  # exits the daemon loop; not an Exception subclass

    monkeypatch.setattr(embedder.time, "sleep", _sleep)
    with pytest.raises(KeyboardInterrupt):
        embedder._standalone_doc_drain_loop("dsn", "http://backend")
    assert order[0] == "gate", "the gate must run before the first drain cycle"
    assert "drain" in order


# ── (c) rate_deferred backoff: the re-pend is NOT immediately claimable ──────────────

def test_rate_deferred_repend_stamps_a_backoff_not_null(monkeypatch):
    """A rate_deferred failure re-pends with started_at = now() — the backoff stamp.
    RED if the old started_at = NULL (immediately claimable) comes back: that is the
    ladder that burned 129/133 chunks against a 40 RPM budget."""
    conn = _Conn(status="pending", started_age_s=None)
    _drain_one_doc(
        monkeypatch, conn,
        chunk_error=embedder.DocumentBrainUnavailable("rate_deferred: daily budget"))
    repends = _stmts(conn, "SET status = 'pending', started_at = now()")
    assert repends, "the paced re-pend must stamp the backoff timestamp"
    repend_sql, repend_params = repends[0]
    assert "attempts = GREATEST(attempts - 1, 0)" in repend_sql, (
        "the pace refund must ride the same statement — a paced defer is not a failure")
    assert not _stmts(conn, "SET status = 'pending', started_at = NULL"), (
        "the paced re-pend must NOT leave the row immediately claimable (no NULL stamp)")


def test_backoff_window_gates_the_claim(monkeypatch):
    """A pending row inside its backoff window is invisible to the candidate SELECT and
    therefore never claimed. RED if the backoff predicate is dropped from the SELECT."""
    conn = _Conn(status="pending", started_age_s=10)
    _drain_one_doc(monkeypatch, conn,
                   chunk_error=embedder.DocumentBrainUnavailable("circuit_breaker_open"))
    assert not _stmts(conn, "SET status = 'processing'"), (
        "a pending row inside DOC_RETRY_BACKOFF_SECONDS must not be claimed")
    selects = _stmts(conn, "SELECT id FROM documents")
    assert selects and "interval '1 second'" in selects[0][0], (
        "the candidate SELECT must carry the backoff predicate")
    assert selects[0][1][0] == embedder._DOC_RETRY_BACKOFF_S, (
        "the backoff window must flow from the module constant into the SQL parameter")


def test_expired_backoff_and_fresh_ingest_are_claimable(monkeypatch):
    """The backoff self-expires (age > window → claimable) and a FRESH ingest
    (started_at NULL) is never caught by it — the NULL arm must survive."""
    conn = _Conn(status="pending", started_age_s=embedder._DOC_RETRY_BACKOFF_S + 1)
    _drain_one_doc(monkeypatch, conn,
                   chunk_error=embedder.DocumentBrainUnavailable("circuit_breaker_open"))
    assert _stmts(conn, "SET status = 'processing'"), (
        "a pending row whose backoff has expired must be claimable")


def test_fresh_ingest_is_immediately_claimable(monkeypatch):
    conn = _Conn(status="pending", started_age_s=None)  # fresh ingest: started_at NULL
    _drain_one_doc(monkeypatch, conn,
                   chunk_error=embedder.DocumentBrainUnavailable("circuit_breaker_open"))
    assert _stmts(conn, "SET status = 'processing'"), (
        "a fresh ingest (started_at NULL) must never be delayed by the backoff")


def test_non_paced_defer_keeps_the_immediate_repend(monkeypatch):
    """SCOPE GUARD: a plain brain-unavailable defer (breaker open) re-pends with
    started_at = NULL — the breaker paces itself and the bounded-redelivery machinery
    owns that lane. The backoff is for rate_deferred (and the backend freeze) ONLY."""
    conn = _Conn(status="pending", started_age_s=None)
    _drain_one_doc(monkeypatch, conn,
                   chunk_error=embedder.DocumentBrainUnavailable("circuit_breaker_open"))
    repends = _stmts(conn, "SET status = 'pending', started_at = NULL")
    assert repends, "a non-paced defer must keep the legacy immediately-claimable re-pend"


def test_claim_recheck_loses_the_race_gracefully(monkeypatch):
    """Between the candidate SELECT and the claim, another worker may take the row: the
    claim recheck must yield rowcount 0 and the drain must SKIP — not crash, not
    double-process."""
    conn = _Conn(status="pending", started_age_s=None, steal_between_select_and_claim=True)
    n = _drain_one_doc(monkeypatch, conn,
                       chunk_error=embedder.DocumentBrainUnavailable("circuit_breaker_open"))
    assert n == 0
    assert conn.attempts == 0, "a lost race must not consume the redelivery budget"
    assert not _stmts(conn, "SET status = 'pending'"), (
        "a skipped row must not be re-pended by THIS worker")


# ── the env contract ─────────────────────────────────────────────────────────────────

def test_env_contract_defaults():
    """The three knobs the brief names, with their agreed defaults — a silent default
    drift here would change prod retry/lease cadence with no code change to point at."""
    assert embedder._DOC_RETRY_BACKOFF_S == 60.0
    assert embedder._DOC_CLAIM_LEASE == "30 minutes"
    assert embedder._DOC_DRAIN_STARTUP_GATE_TIMEOUT == 120.0


def test_backoff_constant_flows_live_into_the_claim(monkeypatch):
    """The knob must be read per call from the module constant (which env sets at
    import), not baked into a literal — prove the plumbing with a non-default value."""
    conn = _Conn(status="pending", started_age_s=10)
    monkeypatch.setattr(embedder, "_DOC_RETRY_BACKOFF_S", 5.0)
    _drain_one_doc(monkeypatch, conn,
                   chunk_error=embedder.DocumentBrainUnavailable("circuit_breaker_open"))
    selects = _stmts(conn, "SELECT id FROM documents")
    assert selects and selects[0][1][0] == 5.0, "the backoff parameter must be live"
    assert _stmts(conn, "SET status = 'processing'"), (
        "with the window at 5s, a 10s-old stamp must be claimable")
