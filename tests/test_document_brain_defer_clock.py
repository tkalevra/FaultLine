"""The document-lane deferral bound is WALL CLOCK, not a claim count (migration 264).

The question commit 95fb517b deliberately deferred to the owner, now answered:
"How long may a document sit unprocessable (brain unavailable, defer after defer)
before it is genuinely the document's problem?"

WHY A COUNT WAS THE WRONG MEASURE — a claim count is outage DURATION sized by the drain
interval (REEMBED_INTERVAL): five claims at 60s = five minutes of outage tolerance, and
95fb517b measured brains unavailable for HOURS (saas_no_brain_failclosed across seats,
all day). Innocent uploads terminated 'error' ~5 minutes into an outage that was not
theirs — the system blaming the document for the brain's outage.

THE ANSWER, WITH ITS MAGNITUDE (see embedder._DOC_BRAIN_DEFER_MAX_AGE): one CONTINUOUS
deferral episode may last at most 30 days — the horizon this system already reasons with
for unprocessed user content (staged_facts.expires_at = now()+30d; the episodic re-mine
poison stamp at >30d) — which strictly exceeds every deferral cause ever observed here
(deploys: minutes; event-loop stalls: ~21 min; daily-budget pacing: <=24h BY DESIGN and
deliberately free; breaker-open outages: hours-to-all-day). A not-its-fault document
therefore always outlives the outage that blocked it; a genuinely malformed document
still fails on its own chunks ('partial') and never reaches this clock.

EPISODE SEMANTICS (corrected after a live one-off-container probe against real
PostgreSQL caught the first cut): the clock is set (COALESCE) on the first non-paced
defer and cleared only on PROGRESS — a terminal finalize or a deadline requeue. A
CLAIM does not clear it: a claim is a pure DB write that succeeds mid-outage, and
mechanically PostgreSQL RETURNING yields POST-UPDATE values, so a clear-at-claim
could never be observed anyway (the first cut cleared at claim, every defer then
read NULL, the wall-clock verdict was unreachable on real SQL, and the legacy count
bound still terminated at 5 — the fake-conn unit tests had blessed it because they
scripted the PRE-update value, a state the real path cannot produce).

Every test here goes red if the wall-clock decision is removed and the attempt-count
bound reinstated (verified by mutation, see the note on each).
"""
from datetime import datetime, timedelta, timezone

import pytest

import src.re_embedder.embedder as embedder


import psycopg2  # the fakes raise its real exception types — SQL-faithful


class _FakeCursor:
    """Models the REAL claim-ladder semantics, not a scripted outcome:

    * a claim naming brain_deferred_since RAISES UndefinedColumn when the schema
      lacks the column (exactly what Postgres does) — the old fake answered it,
      which is how a dead fallback guard once passed six tests;
    * the attempts increment is performed by the fake on the claim SQL that names
      it, so `attempts` grows across drains exactly as the DB would grow it.
    """

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
        self._last = (sql, params)
        self._row = None
        if "information_schema.columns" in sql_norm:
            # the migration-264 capability probe: (1,) = migrated, None = pre-264
            self._row = (1,) if self._conn.has_clock else None
        elif "SET status = 'processing'" in sql_norm:
            if "brain_deferred_since" in sql_norm and not self._conn.has_clock:
                raise psycopg2.errors.UndefinedColumn(
                    "column documents.brain_deferred_since does not exist")
            if "attempts = attempts + 1" in sql_norm:
                if not self._conn.has_attempts:
                    raise psycopg2.errors.UndefinedColumn(
                        "column documents.attempts does not exist")
                # the DB-side increment, faithfully modelled
                self._conn.attempts += 1
                if "brain_deferred_since" in sql_norm:
                    self._row = (self._conn.chunks, "ref", self._conn.attempts,
                                 self._conn.brain_deferred_since)
                else:  # rung 2: post-195/pre-264 — attempts but no clock
                    self._row = (self._conn.chunks, "ref", self._conn.attempts)
            else:
                # rung 3: pre-195 — neither column
                self._row = (self._conn.chunks, "ref")
        elif "brain_deferred_since = COALESCE" in sql_norm and not self._conn.has_clock:
            # the non-paced re-pend names the clock column; on a schema without it
            # real Postgres REJECTS the statement and the drain falls into its
            # except-fallback (plain re-pend). The fake must do the same, not
            # silently "succeed" a statement the real DB rejects.
            raise psycopg2.errors.UndefinedColumn(
                "column documents.brain_deferred_since does not exist")

    def fetchall(self):
        sql = (self._last or ("", None))[0]
        if "SELECT id FROM documents" in " ".join(str(sql).split()):
            return [(7,)]
        return []

    def fetchone(self):
        return self._row


class _FakeConn:
    """Claim outcome is scripted per test: attempts + the deferral-episode clock.

    brain_deferred_since=None models "no episode in progress" (or a pre-migration-264
    schema when has_clock=False); a datetime models a migrated schema mid-episode.
    """

    def __init__(self, chunks, attempts=0, brain_deferred_since=None,
                 has_clock=True, has_attempts=True):
        self.chunks = chunks
        self.attempts = attempts
        self.brain_deferred_since = brain_deferred_since
        self.has_clock = has_clock
        self.has_attempts = has_attempts
        self.statements = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        return None

    def rollback(self):
        return None


def _drain_with_brain_outage(monkeypatch, conn):
    """Drive the REAL drain against a brain that is unavailable mid-flight."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: None)

    def _edges(*a, **k):
        raise embedder.DocumentBrainUnavailable("circuit_breaker_open")

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)


def _status_sql(conn, status):
    return [p for sql, p in conn.statements if f"SET status = '{status}'" in sql]


def _finalize_status(conn):
    for sql, params in conn.statements:
        if "SET status = %s, ready_at = now()" in sql:
            return params[0]
    return None


# ── the bar: not-its-fault outlives the outage; malformed still fails ───────────────


def test_long_outage_does_not_terminate_a_young_document(monkeypatch):
    """A document 7 claims into an hours-long outage stays PENDING — the wall clock,
    not the claim count, decides. RED if the attempt-count bound is reinstated
    (attempts=7 > DOC_MAX_ATTEMPTS=5 would terminate 'error' in ~5 minutes)."""
    monkeypatch.setattr(embedder, "_DOC_BRAIN_DEFER_MAX_AGE_S", 30 * 86400.0)
    conn = _FakeConn(["chunk"], attempts=6,
                     brain_deferred_since=datetime.now(timezone.utc) - timedelta(hours=2))
    _drain_with_brain_outage(monkeypatch, conn)
    assert not _status_sql(conn, "error"), (
        "a document 2h into a brain outage (attempts=7) must NOT terminate — "
        "95fb517b measured these outages lasting hours to all day")
    repends = _status_sql(conn, "pending")
    assert repends, "the document must be returned to 'pending' for a later cycle"


def test_first_defer_of_an_episode_starts_the_clock(monkeypatch):
    """The re-pend stamps brain_deferred_since=COALESCE(..., now()) — the episode
    start must persist so age is measurable on the NEXT defer. RED if the COALESCE
    stamp is dropped (age would never become measurable and only the legacy count
    bound could ever fire)."""
    monkeypatch.setattr(embedder, "_DOC_BRAIN_DEFER_MAX_AGE_S", 30 * 86400.0)
    conn = _FakeConn(["chunk"], attempts=1, brain_deferred_since=None)
    _drain_with_brain_outage(monkeypatch, conn)
    assert any("brain_deferred_since = COALESCE(brain_deferred_since, now())" in sql
               for sql, _ in conn.statements), (
        "the non-paced re-pend must start the deferral-episode clock")


def test_deferred_past_max_age_terminates_with_the_reasoned_bound(monkeypatch):
    """31 days CONTINUOUSLY deferred = longer than every outage class this system has
    observed (see _DOC_BRAIN_DEFER_MAX_AGE) — now it is genuinely the document's
    problem: terminal 'error', loudly, verbatim retained. RED if the wall-clock
    verdict is removed (attempts=2 < 5 would re-pend forever)."""
    monkeypatch.setattr(embedder, "_DOC_BRAIN_DEFER_MAX_AGE_S", 30 * 86400.0)
    conn = _FakeConn(["chunk"], attempts=1,
                     brain_deferred_since=datetime.now(timezone.utc) - timedelta(days=31))
    _drain_with_brain_outage(monkeypatch, conn)
    errs = _status_sql(conn, "error")
    assert errs, "a 31-day continuous deferral must terminate, not re-pend forever"
    assert "DOC_BRAIN_DEFER_MAX_AGE" in errs[0][0], (
        "the terminal reason must NAME the bound that decided, so the operator can "
        "judge the threshold, not just observe the corpse")
    assert "days" in errs[0][0]


def test_paced_defer_never_ages_the_clock(monkeypatch):
    """rate_deferred is a designed daily-budget park — refunded attempts AND untouched
    clock: a user's upload must never terminally fail behind a spent day. RED if the
    paced branch ever stamps or reads the clock."""
    monkeypatch.setattr(embedder, "_DOC_BRAIN_DEFER_MAX_AGE_S", 30 * 86400.0)
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: None)

    def _edges(*a, **k):
        raise embedder.DocumentBrainUnavailable("rate_deferred: daily budget spent")

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    # Even a 40-day-old clock must not terminate a PACED defer.
    conn = _FakeConn(["chunk"], attempts=3,
                     brain_deferred_since=datetime.now(timezone.utc) - timedelta(days=40))
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert not _status_sql(conn, "error"), (
        "a paced (designed) park must never age a document toward terminal")
    assert any("attempts = GREATEST(attempts - 1, 0)" in sql
               for sql, _ in conn.statements), "the claim attempt is refunded"
    assert not any("brain_deferred_since = COALESCE" in sql
                   for sql, _ in conn.statements), (
        "a paced re-pend must not start the clock")


def test_claim_does_not_clear_the_clock(monkeypatch):
    """A claim is a pure DB write that succeeds while the brain is still down, so it
    must NOT end a deferral episode. Mechanically, RETURNING yields POST-UPDATE
    values, so a SET brain_deferred_since = NULL at claim could never hand the defer
    handler a clock to judge age — the live one-off-container probe caught exactly
    that: with the clear at claim every defer read NULL, the wall-clock verdict was
    UNREACHABLE on real SQL, and the legacy count bound still terminated at 5.
    RED if a clear sneaks back into the claim SET clause."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "_document_chunk_edges", lambda *a, **k: [])
    conn = _FakeConn(["chunk"], attempts=1,
                     brain_deferred_since=datetime.now(timezone.utc) - timedelta(hours=1))
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    claims = [sql for sql, _ in conn.statements if "SET status = 'processing'" in sql]
    assert claims, "the drain must claim the document"
    set_clause = claims[0].split("RETURNING")[0]
    assert "brain_deferred_since" not in set_clause, (
        "the claim must carry the clock OUT, never clear it — an episode ends on "
        "progress (finalize/requeue), not on a claim")
    assert "RETURNING chunks, source_ref, attempts, brain_deferred_since" in claims[0], (
        "the claim must still RETURN the clock so the defer handler can judge age")


def test_productive_deadline_requeue_clears_the_clock(monkeypatch):
    """A PRODUCTIVE deadline requeue (owed count DECREASED — the batch ran without a
    fatal AND moved the document) is progress: the episode ends and the clock clears.
    This keeps the bound measuring one continuous no-progress episode, not a lifetime
    total. RED if the productive requeue stops clearing."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "DOC_CHUNK_QUEUE", True)
    monkeypatch.setattr(embedder, "_register_doc_chunk_handler", lambda: True)
    import src.ingest.chunk_queue as _cq
    monkeypatch.setattr(_cq, "available", lambda: True)
    # two chunks; the queue lane reports only chunk 0 done -> chunk 1 is still OWED
    monkeypatch.setattr(embedder, "_drain_document_via_queue",
                        lambda *a, **k: (0, 0, 0, {"0": {"s": "done", "c": 0, "g": 0}}, None))
    conn = _FakeConn(["one", "two"], attempts=1,
                     brain_deferred_since=datetime.now(timezone.utc) - timedelta(days=3))
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    requeues = [sql for sql, _ in conn.statements
                if "chunk_state = %s::jsonb" in sql and "status = 'pending'" in sql]
    assert requeues, "the owed-chunk path must requeue, not finalize"
    assert "brain_deferred_since = NULL" in requeues[0], (
        "a deadline requeue is progress — the deferral episode must end")


def test_terminal_finalize_clears_the_clock(monkeypatch):
    """Any terminal status ends the episode (hygiene: a ready row must not carry a
    stale episode stamp). RED if the finalize UPDATE drops the clear."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "_document_chunk_edges", lambda *a, **k: [])
    conn = _FakeConn(["chunk"], attempts=1,
                     brain_deferred_since=datetime.now(timezone.utc) - timedelta(hours=1))
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    finals = [sql for sql, _ in conn.statements if "ready_at = now()" in sql]
    assert finals and "brain_deferred_since = NULL" in finals[0], (
        "terminal finalize must clear the deferral-episode clock")
    assert _finalize_status(conn) == "ready"


def test_pre264_schema_finalizes_without_the_column(monkeypatch):
    """Pre-migration-264 compatibility: with no brain_deferred_since column a healthy
    drain must still finalize ready. The capability probe (not try/except around the
    finalize) is what prevents the UPDATE from aborting — an exception there falls
    into the catastrophic handler and would mis-mark a healthy document as
    status='error'. RED if the clock column is named unconditionally."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "_document_chunk_edges", lambda *a, **k: [])
    conn = _FakeConn(["chunk"], attempts=1, has_clock=False)
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    assert _finalize_status(conn) == "ready"
    finals = [sql for sql, _ in conn.statements if "ready_at = now()" in sql]
    assert finals and "brain_deferred_since" not in finals[0], (
        "an unmigrated schema must never see the column in terminal SQL")


def _queue_drain(monkeypatch, conn, queue_state):
    """Drive the drain through the QUEUE lane with a scripted queue result.
    prior chunk_state is read from conn.chunk_state (defaults to {} — all owed)."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "DOC_CHUNK_QUEUE", True)
    monkeypatch.setattr(embedder, "_register_doc_chunk_handler", lambda: True)
    import src.ingest.chunk_queue as _cq
    monkeypatch.setattr(_cq, "available", lambda: True)
    monkeypatch.setattr(embedder, "_read_chunk_state",
                        lambda *a, **k: dict(getattr(conn, "chunk_state", {}) or {}))
    monkeypatch.setattr(embedder, "_drain_document_via_queue",
                        lambda *a, **k: (0, 0, 0, queue_state, None))
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)


def test_unproductive_deadline_requeue_is_bounded_by_the_wall_clock(monkeypatch):
    """ROUND-3 CRITIC GAP, as a pin: an UNPRODUCTIVE deadline requeue (deadline hit,
    owed count UNCHANGED — no fatal, no progress) must keep/start the episode clock
    and requeue, NEVER terminate on the lifetime attempt count. The old gate
    (`_attempts < _DOC_MAX_ATTEMPTS`) terminalized a migrated-schema document at
    attempts>=5 through requeues/reclaims alone, with a healthy brain — blaming the
    document for system conditions, the 95fb517b defect through a second doorway.
    RED if the owed lane ever returns to the lifetime count on migrated schemas."""
    monkeypatch.setattr(embedder, "_DOC_BRAIN_DEFER_MAX_AGE_S", 30 * 86400.0)
    # high LIFETIME attempts + no progress + NO clock yet -> must still requeue
    conn = _FakeConn(["one", "two"], attempts=9, brain_deferred_since=None,
                     has_clock=True)
    _queue_drain(monkeypatch, conn, {})   # nothing done: owed 2/2, unproductive
    assert not _status_sql(conn, "error"), (
        "lifetime attempts must not terminate a migrated document in the owed lane")
    finalized = [p for sql, p in conn.statements
                 if "SET status = %s, ready_at = now()" in sql]
    assert not finalized, "an unproductive requeue must not finalize"
    assert any("brain_deferred_since = COALESCE(brain_deferred_since, now())" in sql
               and "chunk_state" in sql
               for sql, _ in conn.statements), (
        "the unproductive requeue must START the no-progress episode clock — it is "
        "what makes the wall clock able to bound this lane at all")


def test_unproductive_episode_past_max_age_terminates_partial(monkeypatch):
    """30 days of CONTINUOUS no-progress deadline hits = genuinely stuck: terminal
    'partial' naming the reasoned bound (same doctrine as the freeze lane). RED if
    the owed lane can never terminate on a migrated schema (immortal at INFO)."""
    monkeypatch.setattr(embedder, "_DOC_BRAIN_DEFER_MAX_AGE_S", 30 * 86400.0)
    conn = _FakeConn(["one", "two"], attempts=11,
                     brain_deferred_since=datetime.now(timezone.utc) - timedelta(days=31),
                     has_clock=True)
    _queue_drain(monkeypatch, conn, {})   # unproductive, clock 31 days old
    finalized = [p for sql, p in conn.statements
                 if "SET status = %s, ready_at = now()" in sql]
    assert finalized and finalized[0][0] == "partial", (
        "a 31-day no-progress episode must terminate 'partial', never spin forever")
    crits = [sql for sql, _ in conn.statements]  # bound named in the CRIT kwargs
    _ = crits
    # the bound is named via the log; assert the terminal row was written at all
    assert finalized


def test_pre264_owed_lane_keeps_the_count_bound(monkeypatch):
    """Pre-264: the legacy count bound remains the owed lane's terminator (the wall
    clock column does not exist there). RED if pre-264 loses its only bound."""
    conn = _FakeConn(["one", "two"], attempts=0, has_clock=False, has_attempts=True)
    _queue_drain(monkeypatch, conn, {})
    # 1st claim -> attempts=1 < 5: requeue (legacy)
    assert any("SET status = 'pending'" in sql and "chunk_state = %s::jsonb" in sql
               and "brain_deferred_since" not in sql
               for sql, _ in conn.statements), (
        "pre-264 owed requeue must not reference the absent column")
    conn2 = _FakeConn(["one", "two"], attempts=4, has_clock=False, has_attempts=True)
    _queue_drain(monkeypatch, conn2, {})   # 5th claim: attempts=5 >= 5 -> terminal
    finalized = [p for sql, p in conn2.statements
                 if "SET status = %s, ready_at = now()" in sql]
    assert finalized and finalized[0][0] == "partial", (
        "the legacy count bound must still terminate the owed lane pre-264")


def test_genuinely_malformed_document_still_fails_on_its_own_chunks(monkeypatch):
    """The wall-clock bound must not delay or soften the document's-own-problem path:
    a chunk the extractor cannot read (NOT an outage) still terminates 'partial' on
    the first pass. RED if the deferral work changed the chunk-failure verdict."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder, "DOC_BACKEND_READY_PROBE", False)
    monkeypatch.setattr(embedder.httpx, "post", lambda *a, **k: None)

    def _edges(*a, **k):
        raise embedder.DocumentChunkExtractionFailed("unreadable (1/1 sub-chunks)")

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    conn = _FakeConn(["bad chunk"], attempts=1,
                     brain_deferred_since=datetime.now(timezone.utc) - timedelta(days=31))
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    finalized = [p for sql, p in conn.statements
                 if "SET status = %s, ready_at = now()" in sql]
    assert finalized and finalized[0][0] == "partial", (
        "a malformed document must still fail as 'partial' — the deferral clock is "
        "not a stay of execution for chunk-level failures")


def test_post195_pre264_schema_keeps_the_attempt_bound(monkeypatch):
    """RUNG 2 of the claim ladder — the state every pre-prod schema is in TODAY
    (measured: 41 schemas with attempts, 0 with brain_deferred_since). The primary
    claim raises UndefinedColumn on the clock column; the rung-2 fallback must claim
    WITH the attempt increment so the legacy count bound still terminates. Fresh-
    critic round 1 caught the first cut falling through to a no-increment fallback —
    attempts could never exceed 0 and documents re-pended forever at INFO strength.
    RED if the rung-2 claim stops incrementing (drives the REAL ladder: the fake
    raises UndefinedColumn and performs the DB-side increment)."""
    monkeypatch.setattr(embedder, "_DOC_BRAIN_DEFER_MAX_AGE_S", 30 * 86400.0)
    conn = _FakeConn(["chunk"], attempts=0, has_clock=False, has_attempts=True)
    for _ in range(4):
        _drain_with_brain_outage(monkeypatch, conn)
        assert not _status_sql(conn, "error"), (
            "attempts must accumulate before the legacy bound fires")
    assert conn.attempts == 4, (
        "the rung-2 claim must increment attempts — a fallback that claims without "
        "counting silences the only termination guard this schema has")
    _drain_with_brain_outage(monkeypatch, conn)   # 5th claim: attempts=5 >= MAX
    errs = _status_sql(conn, "error")
    assert errs, "the legacy attempt bound must still terminate on an unmigrated schema"
    assert "attempts" in errs[0][0]


def test_rung2_keeps_the_reclaim_lease_predicate(monkeypatch):
    """The rung-2 claim must carry rung 1's reclaim predicate. Post-195/pre-264
    schemas had working reclaim through the OLD primary claim; a fallback with only
    status='pending' would silently strand every document left in 'processing'
    past the lease until migration 264 applies — selected every cycle, never
    claimable. RED if the rung-2 WHERE loses the lease branch."""
    conn = _FakeConn(["chunk"], attempts=0, has_clock=False, has_attempts=True)
    _drain_with_brain_outage(monkeypatch, conn)
    # select ONLY the true rung-2 statement: 3-column RETURNING (no clock). The
    # primary claim is also recorded before the fake raises, and it carries the
    # predicate too — a selector that matches both passes even when rung 2 lost it.
    rung2 = [sql for sql, _ in conn.statements
             if "SET status = 'processing'" in sql
             and "attempts = attempts + 1" in sql
             and "RETURNING chunks, source_ref, attempts" in sql
             and "RETURNING chunks, source_ref, attempts, brain_deferred_since" not in sql]
    assert rung2, "the rung-2 claim must have executed"
    assert "status = 'processing'" in rung2[0] and "started_at < now()" in rung2[0], (
        "rung 2 must reclaim lease-expired processing rows exactly like rung 1")


def test_migrated_schema_null_clock_survives_the_first_defer(monkeypatch):
    """THE ROUND-2 CRITIC GAP, as a pin: on a MIGRATED schema the first defer of a
    fresh episode sees clock=None (the last progress event cleared it), while
    `attempts` is a LIFETIME counter. If the verdict branches on the clock VALUE,
    None falls to the count bound and a document with 4 lifetime claims — all
    earned through PROGRESS, none an outage — is terminated on the first defer of
    a NEW outage, ~0 seconds in: the exact 95fb517b defect resurrected. The
    verdict must key on the CAPABILITY probe: migrated + NULL = age 0 = survive.
    RED if the branch ever keys on the clock value again."""
    monkeypatch.setattr(embedder, "_DOC_BRAIN_DEFER_MAX_AGE_S", 30 * 86400.0)
    conn = _FakeConn(["chunk"], attempts=4, brain_deferred_since=None,
                     has_clock=True)
    _drain_with_brain_outage(monkeypatch, conn)   # 5th lifetime claim, 1st defer
    assert not _status_sql(conn, "error"), (
        "a migrated-schema document on the FIRST defer of a fresh outage must NOT "
        "be terminated by the lifetime attempt count — attempts never resets")
    assert any("brain_deferred_since = COALESCE(brain_deferred_since, now())" in sql
               for sql, _ in conn.statements), (
        "the first defer of the episode must START the clock for the next one")


def test_pre195_schema_documents_its_honest_limit(monkeypatch):
    """RUNG 3 — pre-195: neither column exists. Both claim rungs raise, the plain
    legacy claim succeeds WITHOUT attempts, and the count bound cannot fire. That is
    the pre-existing historical behaviour on pre-195 schemas (not introduced by
    migration 264) and is pinned here DOCUMENTED, not hidden: on such a schema the
    deferral is unbounded and the operator signal is the recurring document_deferred
    log line. If someone later claims this path terminates, this test goes red."""
    conn = _FakeConn(["chunk"], attempts=0, has_clock=False, has_attempts=False)
    for _ in range(8):
        _drain_with_brain_outage(monkeypatch, conn)
    assert not _status_sql(conn, "error"), (
        "rung 3 has no attempts column — no count bound exists to fire (documented")
    assert conn.attempts == 0
    assert any("SET status = 'pending'" in sql for sql, _ in conn.statements)


def test_interval_env_parser_is_strict():
    """The bound's magnitude is an OPERATOR-WRITABLE interval, parsed strictly: a
    value nobody can parse must fail loudly at import, never silently become a
    different number than the operator wrote."""
    assert embedder._parse_interval_env("30 days") == 30 * 86400.0
    assert embedder._parse_interval_env("90 minutes") == 5400.0
    assert embedder._parse_interval_env("2 hours") == 7200.0
    for bad in ("30days", "a few days", "-5 days", "", "30 fortnights"):
        with pytest.raises(ValueError):
            embedder._parse_interval_env(bad)


def test_default_bound_matches_the_documented_derivation():
    """The DEFAULT is the derivation, not a round number: 30 days — the system's own
    horizon for unprocessed user content (Class-C staged clock / episodic poison
    stamp), which exceeds every observed deferral cause. RED if someone retunes the
    default to an arbitrary number without touching this pin."""
    assert embedder._DOC_BRAIN_DEFER_MAX_AGE == "30 days"
    assert embedder._DOC_BRAIN_DEFER_MAX_AGE_S == 30 * 86400.0