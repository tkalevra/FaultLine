"""DOC-DRAIN FIFO STARVATION — zombie docs monopolize the batch (BUG 2026-10-26).

MEASURED live on pre-prod (stack 55, brain hard-down): the drain claims
DOC_DRAIN_BATCH=2 docs per cycle in strict FIFO order; two brain-deferred zombies
(attempts=104 and 72 — far past any reasonable retry) were the two oldest eligible
rows EVERY cycle, so a NEW ingest (doc 9, attempts=0, enqueued 18:59) was never
claimed in 34+ minutes. Status stayed pending, attempts=0, started_at NULL — starved
behind dead weight that is not going to succeed on attempt 105 either.

THE FIX, three sub-fixes, all pure/deterministic SQL (no LLM anywhere):
  A — attempt-weighted claim ORDER: fresh (attempts=0) first, low-attempt (1-2) next,
      zombies LAST; created_at ASC stays the tie-break inside each band.
  B — zombie quarantine: a pending doc at lifetime attempts >= DOC_DRAIN_HARD_CEILING
      (default DOC_REMINE_MAX_ATTEMPTS × 3 = 9) flips terminal 'error' with a loud
      CRIT BEFORE any slot is spent, and is never claimable again.
  C — fresh-doc guarantee: at least ONE batch slot goes to the oldest eligible
      attempts=0 doc whenever one exists — even on the FIFO fail-safe path.

The CLAIM ELIGIBILITY predicate (DOC_RETRY_BACKOFF_SECONDS backoff + the
'processing' lease reclaim) is UNCHANGED — this is batch SELECTION, not claim gating.

Every fake here MODELS the real selector/claim semantics (eligibility, banding,
atomicity) rather than scripting outcomes; the SQL text itself was additionally
verified against a real throwaway PostgreSQL 16 with the measured repro rows
(old FIFO → [7,8], fresh doc 9 starved; weighted → [9,8]) before these pins were
written — a fake-conn SQL-text pin alone proves nothing about the SQL (the lesson
from the migration-264 live probe).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import psycopg2  # the fakes raise its real exception types — SQL-faithful

import src.re_embedder.embedder as embedder


_NOW = datetime.now(timezone.utc)

_UNIT_S = {"second": 1.0, "seconds": 1.0, "minute": 60.0, "minutes": 60.0,
           "hour": 3600.0, "hours": 3600.0, "day": 86400.0, "days": 86400.0}


def _parse_interval(value) -> float:
    """Parse the selector's interval params: the backoff arrives as a FLOAT seconds
    count, the lease as an 'N unit' string — handle both."""
    if isinstance(value, (int, float)):
        return float(value)
    n, unit = str(value).split()
    return float(n) * _UNIT_S[unit]


def _band(attempts: int) -> int:
    """The SQL's CASE band, mirrored in Python: fresh=0, low-attempt=1, zombie=2."""
    if attempts == 0:
        return 0
    if attempts <= 2:
        return 1
    return 2


class _Doc:
    """One documents row. created_at ordering is explicit per test for determinism."""

    def __init__(self, doc_id, *, status="pending", attempts=0, started_at=None,
                 created_age_s=0.0, chunks=("c0",), brain_deferred_since=None):
        self.id = doc_id
        self.status = status
        self.attempts = attempts
        self.started_at = started_at
        self.created_at = _NOW - timedelta(seconds=created_age_s)
        self.chunks = list(chunks)
        self.brain_deferred_since = brain_deferred_since
        self.error = None


class _FakeCursor:
    """Models the REAL drain SQL semantics: capability probes, the weighted/FIFO
    batch selector, the zombie quarantine probe+flip, and the rung-1 atomic claim
    (backoff + lease predicate evaluated against the row, attempts incremented,
    POST-UPDATE values returned)."""

    def __init__(self, conn):
        self._conn = conn
        self._row = None
        self._rows = None
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # ── eligibility: the claim/batch WHERE predicate, evaluated per row ────────────
    @staticmethod
    def _eligible(doc, backoff_s, lease_s):
        if doc.status == "pending":
            return doc.started_at is None or (
                _NOW - doc.started_at).total_seconds() > backoff_s
        if doc.status == "processing":
            return doc.started_at is not None and (
                _NOW - doc.started_at).total_seconds() > lease_s
        return False

    def execute(self, sql, params=None):
        sql_norm = " ".join(str(sql).split())
        self._conn.statements.append((sql_norm, params))
        self._row = None
        self._rows = None
        self.rowcount = -1
        conn = self._conn

        if "information_schema.columns" in sql_norm:
            m = re.search(r"column_name = '(\w+)'", sql_norm)
            col = m.group(1) if m else ""
            self._row = (1,) if col in conn.columns else None
            return

        if "SET search_path" in sql_norm:
            return

        # ── zombie quarantine probe: TWO-KEYED (count ceiling AND episode age) ──────
        if ("SELECT id, attempts FROM documents" in sql_norm
                and "attempts >= %s" in sql_norm):
            ceiling, min_age_s = params[0], params[1]
            self._rows = [
                (d.id, d.attempts) for d in conn.docs
                if d.status == "pending" and d.attempts >= ceiling
                and d.brain_deferred_since is not None
                and (_NOW - d.brain_deferred_since).total_seconds() >= min_age_s]
            return

        # ── zombie quarantine flip (idempotent WHERE re-check, rowcount-driven) ────
        if ("SET status = 'error'" in sql_norm
                and "AND attempts >= %s" in sql_norm):
            doc = conn.by_id.get(params[1])
            if (doc is not None and doc.status == "pending"
                    and doc.attempts >= params[2]):
                doc.status = "error"
                doc.error = params[0]
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # ── batch selector (weighted / FIFO) and the fresh-guarantee probe ─────────
        if "SELECT id FROM documents" in sql_norm:
            backoff_s, lease_s = _parse_interval_pair(params)
            eligible = [d for d in conn.docs if self._eligible(d, backoff_s, lease_s)]
            if "AND attempts = 0" in sql_norm:
                # the fresh-doc guarantee probe: oldest eligible attempts=0, LIMIT 1
                eligible = [d for d in eligible if d.attempts == 0]
                eligible.sort(key=lambda d: d.created_at)
                limit = 1
            elif "CASE WHEN attempts" in sql_norm:
                if conn.fail_weighted:
                    raise psycopg2.OperationalError("synthetic weighted-order failure")
                eligible.sort(key=lambda d: (_band(d.attempts), d.created_at))
                limit = params[2]
            else:
                eligible.sort(key=lambda d: d.created_at)
                limit = params[2]
            self._rows = [(d.id,) for d in eligible[:limit]]
            self._row = self._rows[0] if self._rows else None
            return

        # ── the atomic claim ladder (rungs decided by which columns exist, exactly
        # as real Postgres rejects a statement naming an absent column) ────────────
        if "SET status = 'processing'" in sql_norm:
            if ("brain_deferred_since" in sql_norm
                    and "brain_deferred_since" not in conn.columns):
                raise psycopg2.errors.UndefinedColumn(
                    "column documents.brain_deferred_since does not exist")
            if ("attempts = attempts + 1" in sql_norm
                    and "attempts" not in conn.columns):
                raise psycopg2.errors.UndefinedColumn(
                    "column documents.attempts does not exist")
            doc = conn.by_id.get(params[0])
            if len(params) >= 3:
                eligible = doc is not None and self._eligible(
                    doc, _parse_interval(params[1]), _parse_interval(params[2]))
            else:  # rung 3 (pre-195): WHERE id = %s AND status = 'pending' — no
                eligible = doc is not None and doc.status == "pending"  # backoff/lease
            if eligible:
                doc.status = "processing"
                doc.started_at = _NOW
                doc.attempts += 1  # the DB-side increment, faithfully modelled
                conn.claimed.append(doc.id)  # the CLAIM THAT LANDED (not failed rungs)
                if "RETURNING chunks, source_ref, attempts, brain_deferred_since" in sql_norm:
                    self._row = (doc.chunks, "ref", doc.attempts,
                                 doc.brain_deferred_since)
                elif "RETURNING chunks, source_ref, attempts" in sql_norm:
                    self._row = (doc.chunks, "ref", doc.attempts)
                else:  # rung 3: pre-195 — neither column
                    self._row = (doc.chunks, "ref")
            else:
                self._row = None
            return

        # ── terminal finalize / other UPDATEs: applied by shape ────────────────────
        if "SET status = %s, ready_at = now()" in sql_norm:
            doc = conn.by_id.get(params[-1])
            if doc is not None:
                doc.status = params[0]
            return

    def fetchall(self):
        return self._rows or []

    def fetchone(self):
        return self._row


def _parse_interval_pair(params):
    """(backoff_s, lease_s) from the selector param tuples (float, 'N unit')."""
    return _parse_interval(params[0]), _parse_interval(params[1])


class _FakeConn:
    def __init__(self, docs, *, columns=("brain_deferred_since", "chunk_state",
                                         "reextract_attempts", "attempts"),
                 fail_weighted=False):
        self.docs = list(docs)
        self.by_id = {d.id: d for d in self.docs}
        self.columns = set(columns)
        self.fail_weighted = fail_weighted
        self.statements: list = []
        self.claimed: list = []
        # release_read_transaction() contract
        self.closed = 0
        self.autocommit = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        return None

    def rollback(self):
        return None

    def get_transaction_status(self):
        return 0  # TRANSACTION_STATUS_IDLE — the barrier is a no-op on this fake


def _run_drain(monkeypatch, conn, batch_size=2):
    """Healthy-brain drain: every chunk extracts to zero edges, each claimed doc
    finalizes 'ready' and the loop CONTINUES to the next doc (so claim ORDER is
    observable across the whole batch)."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder, "DOC_CHUNK_QUEUE", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "_document_chunk_edges", lambda *a, **k: [])
    return embedder.drain_pending_documents(
        conn, "http://backend", "u1234567", schema_name=None, batch_size=batch_size)


def _claim_order(conn):
    """Doc ids in the order the drain ATOMICALLY CLAIMED them (claims that LANDED —
    the ladder's failed rungs raise in the fake exactly as on real Postgres and are
    recorded as statements but never as claims)."""
    return list(conn.claimed)


def _quarantine_flips(conn):
    return [(p[1], p[0]) for sql, p in conn.statements
            if "SET status = 'error'" in sql and "AND attempts >= %s" in sql]


# ── pin (a): fresh doc + zombie both pending → fresh doc claimed FIRST ───────────────


def test_pin_a_fresh_doc_claimed_before_older_zombie(monkeypatch):
    """THE HEADLINE REPRO SHAPE, compressed: zombie doc 5 (attempts=104, enqueued
    Oct-25) + fresh doc 9 (attempts=0, enqueued now), both pending and eligible.
    Old FIFO claimed doc 5 first every cycle — doc 9 starved for 34+ minutes live.
    RED at parent (claim order is [5]); green with the weighted selector ([9])."""
    zombie = _Doc(5, attempts=5, created_age_s=2 * 86400)   # band-2, CLAIMABLE
    fresh = _Doc(9, attempts=0, created_age_s=60)
    conn = _FakeConn([zombie, fresh])
    _run_drain(monkeypatch, conn)
    assert not _quarantine_flips(conn), (
        "attempts=5 is below the hard ceiling — ordering must decide, not quarantine")
    assert _claim_order(conn)[0] == 9, (
        "the fresh (attempts=0) document must be claimed FIRST — FIFO let the "
        "oldest high-attempt doc monopolize the head of the queue")


def test_pin_a2_processing_lease_reclaim_is_attempt_weighted_too(monkeypatch):
    """A FRESH doc stranded in 'processing' past the lease (worker died on its first
    pass) is band-0 under the weighted order — it outranks even older zombies, where
    FIFO made it wait behind them."""
    stranded_fresh = _Doc(11, status="processing", attempts=0,
                          started_at=_NOW - timedelta(minutes=40),
                          created_age_s=3600)
    zombie = _Doc(5, attempts=5, created_age_s=2 * 86400)
    conn = _FakeConn([zombie, stranded_fresh])
    _run_drain(monkeypatch, conn)
    assert _claim_order(conn)[0] == 11, (
        "a lease-expired attempts=0 row is the freshest work in the queue — the "
        "weighted order must rank it above an old high-attempt zombie")


# ── pin (b): zombie at the hard ceiling → terminal 'error', loud, unclaimable ───────


def test_pin_b_zombie_at_hard_ceiling_quarantined_loud_and_unclaimable(
        monkeypatch, caplog):
    """A pending doc at attempts == DOC_DRAIN_HARD_CEILING (default 9) AND at least
    DOC_DRAIN_ZOMBIE_MIN_AGE (default 24h) into a continuous unproductive episode is
    flipped terminal 'error' BEFORE any slot is spent, with a loud CRIT naming BOTH
    bounds, and is never claimed — in this cycle or the next. RED if the quarantine
    probe, the flip, or the loudness is dropped."""
    zombie = _Doc(10, attempts=104, created_age_s=3 * 86400,
                  brain_deferred_since=_NOW - timedelta(days=2))
    healthy = _Doc(12, attempts=1, created_age_s=600)
    conn = _FakeConn([zombie, healthy])
    with caplog.at_level(logging.CRITICAL, logger="src.re_embedder.embedder"):
        _run_drain(monkeypatch, conn)
    flips = _quarantine_flips(conn)
    assert flips and flips[0][0] == 10, "the two-key zombie must be flipped 'error'"
    assert "DOC_DRAIN_HARD_CEILING" in flips[0][1], (
        "the terminal reason must NAME the bound that decided (the house rule from "
        "the defer-clock pins)")
    assert "DOC_DRAIN_ZOMBIE_MIN_AGE" in flips[0][1], (
        "the age key is what makes the verdict about the document, not the brain — "
        "it must be named too")
    assert "document_zombie_quarantined" in caplog.text, (
        "quarantine is a LOUD terminal verdict, not a silent status edit")
    assert 10 not in _claim_order(conn), "a quarantined zombie must not be claimed"
    assert zombie.status == "error"
    # and it stays unclaimable on the NEXT cycle
    conn.statements.clear()
    _run_drain(monkeypatch, conn)
    assert 10 not in _claim_order(conn)
    assert not _quarantine_flips(conn), "quarantine is idempotent — no re-flip"


def test_pin_b2_below_ceiling_is_never_quarantined(monkeypatch):
    """The count key alone never fires: a doc at attempts=8 (one under the default 9)
    with an OLD episode clock stays pending and claimable — it is slow, not dead.
    RED if quarantine fires early."""
    slow = _Doc(13, attempts=8, created_age_s=7200,
                brain_deferred_since=_NOW - timedelta(days=3))
    conn = _FakeConn([slow])
    _run_drain(monkeypatch, conn)
    assert not _quarantine_flips(conn), (
        "attempts=8 < ceiling=9 must not quarantine regardless of episode age")
    assert 13 in _claim_order(conn)


def test_pin_b3_ceiling_default_is_the_documented_derivation():
    """DOC_DRAIN_HARD_CEILING defaults to DOC_REMINE_MAX_ATTEMPTS × 3 (=9 with the
    default remine budget) and the episode-age floor to '24 hours' — one full pacing
    window, sized between the short outage classes and the 30-day wall (see the
    two-key rationale). RED if someone retunes either default without touching
    this pin."""
    assert embedder._DOC_DRAIN_HARD_CEILING == embedder._DOC_REMINE_MAX_ATTEMPTS * 3
    assert embedder._DOC_DRAIN_HARD_CEILING == 9
    assert embedder._DOC_DRAIN_ZOMBIE_MIN_AGE == "24 hours"
    assert embedder._DOC_DRAIN_ZOMBIE_MIN_AGE_S == 24 * 3600.0


def test_pin_b4_count_key_alone_never_terminates_outage_recovery(monkeypatch):
    """THE 95fb517b GUARD (gauntlet round 1): a brain-down defer does NOT refund its
    claim and re-pends instantly claimable, so an INNOCENT document reaches the
    ceiling count MINUTES into an hours-to-all-day outage. At attempts=104 but only
    2h into the episode (< DOC_DRAIN_ZOMBIE_MIN_AGE=24h) the doc is NOT a zombie:
    no quarantine, and when the brain returns it is claimed and drains to terminal
    'ready'. RED if the count key is ever allowed to decide alone — that is the
    exact defect class migration 264 retired."""
    mid_outage = _Doc(14, attempts=104, created_age_s=2 * 86400,
                      brain_deferred_since=_NOW - timedelta(hours=2))
    conn = _FakeConn([mid_outage])
    _run_drain(monkeypatch, conn)   # healthy brain in this harness = the outage ended
    assert not _quarantine_flips(conn), (
        "count key satisfied, age key NOT — an in-flight document minutes/hours into "
        "an outage must never be terminally errored by a claim count")
    assert 14 in _claim_order(conn), "the recovered document must still drain"
    assert mid_outage.status == "ready"


def test_pin_b5_old_episode_at_ceiling_is_quarantined(monkeypatch):
    """Both keys satisfied — attempts=104 AND a 25h continuous unproductive episode
    (past DOC_DRAIN_ZOMBIE_MIN_AGE) — is the measured pre-prod zombie shape: terminal
    'error' before any slot is spent. RED if the age gate makes quarantine
    unreachable."""
    zombie = _Doc(15, attempts=104, created_age_s=3 * 86400,
                  brain_deferred_since=_NOW - timedelta(hours=25))
    conn = _FakeConn([zombie])
    _run_drain(monkeypatch, conn)
    flips = _quarantine_flips(conn)
    assert flips and flips[0][0] == 15, "both keys turned — this IS a zombie"
    assert 15 not in _claim_order(conn)


def test_pin_b6_null_clock_is_not_a_zombie(monkeypatch):
    """No episode clock (NULL brain_deferred_since) = no zombie, whatever the count:
    lifetime attempts can be earned through healthy-but-slow requeues that PROGRESS
    (which clear the clock), and a doc that has not deferred is not in an outage
    episode. The wall clock owns that document; quarantine never sees it."""
    no_episode = _Doc(16, attempts=104, created_age_s=3 * 86400,
                      brain_deferred_since=None)
    conn = _FakeConn([no_episode])
    _run_drain(monkeypatch, conn)
    assert not _quarantine_flips(conn), (
        "a NULL episode clock means the count key says nothing about the brain — "
        "not a zombie")
    assert 16 in _claim_order(conn)


def test_pin_b7_pre264_schema_never_quarantines(monkeypatch):
    """No brain_deferred_since column → no episode age can be judged → the two-key
    quarantine self-disables (the capability probe, not a SQL error) and the legacy
    count bound that already exists on those schemas remains their only terminator.
    RED if quarantine SQL names the absent clock column there."""
    zombie_shaped = _Doc(17, attempts=104, created_age_s=3 * 86400)
    conn = _FakeConn([zombie_shaped],
                     columns=("chunk_state", "reextract_attempts", "attempts"))
    _run_drain(monkeypatch, conn)
    assert not _quarantine_flips(conn), "pre-264: no clock column, no quarantine"
    assert not any("SELECT id, attempts FROM documents" in sql
                   for sql, _ in conn.statements), (
        "the quarantine probe must not even be SENT on a pre-264 schema")


# ── pin (c): batch=2, 1 fresh + 3 zombies → fresh takes a slot, best zombie the other ─


def test_pin_c_batch_two_fresh_gets_a_slot_plus_best_zombie(monkeypatch):
    """The exact live starvation: batch=2, three zombies ahead in FIFO order, one
    fresh doc behind. OLD selector: [z1, z2] — fresh starved forever. NEW: the
    fresh doc takes one slot and the OLDEST zombie the other."""
    z1 = _Doc(5, attempts=6, created_age_s=2 * 86400)  # band-2 zombies, CLAIMABLE
    z2 = _Doc(7, attempts=5, created_age_s=2 * 86400 - 3600)
    z3 = _Doc(8, attempts=1, created_age_s=8 * 3600)   # band 1, not a zombie yet
    fresh = _Doc(9, attempts=0, created_age_s=60)
    conn = _FakeConn([z1, z2, z3, fresh])
    _run_drain(monkeypatch, conn)
    order = _claim_order(conn)
    assert order == [9, 8], (
        "batch=2 must be [fresh(9), lowest-band-oldest(8)] — the fresh doc is "
        f"guaranteed a slot; got {order}")


def test_pin_c2_fresh_guarantee_survives_the_fifo_fallback(monkeypatch, caplog):
    """FIX C as a MECHANISM, not a corollary of the weighted order: the weighted
    selector errors (fake raises) → the drain falls back to today's FIFO selector,
    which alone would claim the two OLDEST zombies — the guarantee then INJECTS the
    fresh doc into the last batch slot. Starvation survives neither path. RED if
    the guarantee probe or the fallback disappears."""
    z1 = _Doc(5, attempts=6, created_age_s=2 * 86400)   # claimable zombies: the
    z2 = _Doc(7, attempts=5, created_age_s=1 * 86400)   # ceiling must NOT decide here
    fresh = _Doc(9, attempts=0, created_age_s=60)
    conn = _FakeConn([z1, z2, fresh], fail_weighted=True)
    with caplog.at_level(logging.WARNING, logger="src.re_embedder.embedder"):
        _run_drain(monkeypatch, conn)
    assert "document_drain_weighted_order_failed" in caplog.text, (
        "the fallback must be OBSERVABLE — a silent degradation of the priority "
        "order is exactly the kind of seam that rots unnoticed")
    order = _claim_order(conn)
    assert order == [5, 9], (
        "on the FIFO fallback the oldest zombie still leads, but the fresh doc "
        f"MUST take the second slot via the guarantee; got {order}")


def test_pin_c3_weighted_success_does_not_double_inject(monkeypatch, caplog):
    """With the weighted order healthy the fresh doc is already at the head — the
    guarantee probe must be a NO-OP (no injection, no reordering). RED if the
    guarantee ever disturbs a healthy weighted batch."""
    z1 = _Doc(5, attempts=5, created_age_s=2 * 86400)
    fresh = _Doc(9, attempts=0, created_age_s=60)
    conn = _FakeConn([z1, fresh])
    with caplog.at_level(logging.INFO, logger="src.re_embedder.embedder"):
        _run_drain(monkeypatch, conn)
    assert "document_fresh_guaranteed" not in caplog.text, (
        "the guarantee is a no-op when the weighted order already placed the "
        "fresh doc — injecting again would reorder a healthy batch")
    assert _claim_order(conn) == [9, 5]


# ── pin (d): all zombies, no fresh → oldest zombie claimed (today's behavior) ────────


def test_pin_d_all_zombies_oldest_first_as_today(monkeypatch):
    """No fresh doc exists → nothing to protect; the weighted band is uniform and
    the selector must degrade to exactly today's FIFO outcome (oldest first)."""
    z1 = _Doc(5, attempts=6, created_age_s=2 * 86400)   # band-2, all below the
    z2 = _Doc(7, attempts=5, created_age_s=1 * 86400)   # hard ceiling: ordering
    z3 = _Doc(8, attempts=4, created_age_s=4 * 3600)    # must decide, not quarantine
    conn = _FakeConn([z1, z2, z3])
    _run_drain(monkeypatch, conn)
    assert not _quarantine_flips(conn), "no doc here is at the ceiling"
    assert _claim_order(conn) == [5, 7], (
        "an all-zombie queue keeps today's FIFO behavior — the fix protects FRESH "
        "work, it does not punish old work when nothing fresh is waiting")


# ── pin (e): backoff still gates CLAIM ELIGIBILITY regardless of priority ────────────


def test_pin_e_backoff_window_blocks_claim_at_any_priority(monkeypatch):
    """The priority bands order the ELIGIBLE set; they never widen it. A zombie
    (or any doc) inside its DOC_RETRY_BACKOFF_SECONDS window — started_at stamped
    by the rate_deferred refund — is NOT claimable, no matter how its attempts
    would band, and the fresh doc claims alone. RED if the new ORDER BY ever leaks
    into the WHERE predicate."""
    in_backoff = _Doc(8, attempts=3, started_at=_NOW - timedelta(seconds=10),
                      created_age_s=2 * 86400)   # old zombie, 10s into a 60s window
    fresh = _Doc(9, attempts=0, created_age_s=60)
    conn = _FakeConn([in_backoff, fresh])
    _run_drain(monkeypatch, conn)
    order = _claim_order(conn)
    assert order == [9], (
        "a doc inside its backoff window is not claimable regardless of priority "
        f"(the WHERE clause is unchanged); got {order}")


def test_pin_e2_fresh_doc_inside_backoff_is_not_guaranteed_a_slot(monkeypatch):
    """The guarantee respects eligibility too: an attempts=0 doc still inside its
    backoff window (paced refund on its first pass) must NOT be injected into the
    batch — the guarantee is 'oldest ELIGIBLE attempts=0', not 'any attempts=0'."""
    zombie = _Doc(5, attempts=5, created_age_s=2 * 86400)
    paced_fresh = _Doc(9, attempts=0, started_at=_NOW - timedelta(seconds=5),
                       created_age_s=120)
    conn = _FakeConn([zombie, paced_fresh])
    _run_drain(monkeypatch, conn)
    assert _claim_order(conn) == [5], (
        "backoff gates the guarantee probe as well — an unclaimable fresh doc "
        "takes no slot")


# ── fail-safe: any error in the weighting falls back to today's FIFO ─────────────────


def test_fail_safe_weighted_error_falls_back_to_fifo_and_still_drains(monkeypatch):
    """The brief's fail-safe constraint: a psycopg2 error on the weighted selector
    (planner surprise, transient state) logs a warning and the drain STILL claims
    via the byte-identical FIFO selector — the batch is never lost to the
    optimization. RED if the fallback re-raises or returns early."""
    z1 = _Doc(5, attempts=5, created_age_s=2 * 86400)
    conn = _FakeConn([z1], fail_weighted=True)
    n = _run_drain(monkeypatch, conn)
    assert _claim_order(conn) == [5], "the FIFO fallback must still claim"
    assert n == 1, "and still finalize the document"
    fifo_selects = [sql for sql, _ in conn.statements
                    if "SELECT id FROM documents" in sql
                    and "CASE WHEN attempts" not in sql
                    and "AND attempts = 0" not in sql]
    assert fifo_selects, "the legacy FIFO selector SQL must be the fallback"


def test_pre195_schema_keeps_byte_identical_fifo_no_quarantine_no_guarantee(monkeypatch):
    """Pre-migration-195 compatibility: no attempts column → no weighted ORDER BY,
    no quarantine, no fresh guarantee — the selector SQL is exactly today's, and
    the drain still works. RED if any attempts-naming SQL reaches a pre-195 schema
    (the capability probe, not try/except, must be what protects it). information_
    schema probes are exempt (probing FOR the column is how the drain knows it is
    absent) and `reextract_attempts` does not match \battempts\b."""
    docs = [_Doc(5, attempts=0, created_age_s=2 * 86400),   # attempts column absent:
            _Doc(9, attempts=0, created_age_s=60)]          # banding impossible
    conn = _FakeConn(docs, columns=("brain_deferred_since", "chunk_state"))
    _run_drain(monkeypatch, conn)
    # The SELECTOR must be capability-gated: no documents SELECT may name attempts
    # (the capability probe — not try/except — is what protects a pre-195 schema).
    # The CLAIM LADDER is deliberately exempt: its rungs PROBE by raising
    # UndefinedColumn, exactly as on real Postgres, and rung 3 claims without it.
    for sql, _ in conn.statements:
        if sql.startswith("SELECT") and "information_schema" not in sql:
            assert not re.search(r"\battempts\b", sql), (
                "no documents SELECT may name the absent attempts column on a "
                f"pre-195 schema: {sql[:90]}")
    rung3 = [sql for sql, _ in conn.statements
             if "SET status = 'processing'" in sql
             and "attempts" not in sql
             and "brain_deferred_since" not in sql]
    assert rung3, "the rung-3 claim (no attempts, no clock) must be the one that lands"
    assert _claim_order(conn) == [5, 9], "plain FIFO by created_at, as today"
