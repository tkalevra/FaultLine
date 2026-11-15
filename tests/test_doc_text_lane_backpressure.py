"""Pins for the LEGACY TEXT-LANE clog fix (wound-map round 5).

THE WOUND (tools/regression_results/doc_clog_litmus_report.md, wound 1): a ~1MB
ingest_document chunks to ~3.6k chunks and the drain schedules ALL of them at
once — every one a brain extraction call on the event loop serving live traffic.
Measured under co-load: healthcheck timeouts, doc_drain.backend_stalled, a
container restart to relieve the box. The snapshot-store lane capped its own
upstream (CORPUS_UPLIFT_QUEUE_CAP); this lane, the one the rework deliberately
deferred, had NO cap anywhere.

THE BUILD UNDER TEST:
  1. THE TEXT LANE IS WIRED INTO THE SNAPSHOT STORE: ingest_document POSTs the
     ORIGINAL TEXT (utf-8 bytes, sha256, 8k-char page split — the API derives
     pages from the bytes) to /corpus/documents BEFORE any chunking decision,
     under DOC_INGEST_SNAPSHOT_STORE — the store-of-record + family/dedupe/
     supersede contract covers text documents exactly as files.
  2. DOC_TEXT_LANE_BACKPRESSURE (default ON, OFF = byte-parity):
       (a) ADMISSION CAP — chunk_pass_admission, aligned with the corpus uplift
           cap (DOC_SEAT_CHUNK_BUDGET defaults to CORPUS_UPLIFT_QUEUE_CAP): at
           most `budget` chunks scheduled per drain pass; the rest stay owed in
           the chunk_state ledger and the document RE-PENDS (defer, never
           refuse).
       (b) BOUNDED RETRIES — retry_ceiling_verdict: a HARD ceiling on
           UNPRODUCTIVE passes (DOC_TEXT_LANE_RETRY_CEILING, default 6); at the
           ceiling the document finalizes terminally with an honest bound note.
           Progress (owed count decreased) never counts toward the ceiling.
       (c) chunk batching — deliberately NOT taken (chunk identity is
           load-bearing: episodic turn_key doc:<id>:chunk:<idx>, chunk_state
           indices, /documents/retry re-owes BY INDEX); documented in the flag
           block in src/re_embedder/embedder.py.

Hermetic legs run everywhere. DB legs boot a throwaway database with the real
migration corpus (the r6pass pattern, tests/test_doc_ingest_snapshot_store.py).
"""

import base64
import json
import os

import pytest

from src.ingest import corpus_store as cs
import src.re_embedder.embedder as embedder


# ── helpers ───────────────────────────────────────────────────────────────────────────

class _StubResponse:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _RecordingClient:
    """Records POSTs; answers the endpoints ingest_document_tool touches."""

    def __init__(self):
        self.posts = []       # (url, body)
        self.corpus_body = None

    async def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/documents/enqueue"):
            return _FakeResp({"status": "pending", "document_id": 41,
                              "chunk_count": 1, "eta_seconds": 5})
        if url.endswith("/corpus/documents"):
            self.corpus_body = json
            return _FakeResp({"status": "ready", "document_id": 9,
                              "page_count": 1, "fidelity": {"pages": 1}})
        raise AssertionError(url)


async def _async_retain(**k):
    # Keys chosen NOT to collide with the summary's own (the real retain report
    # carries retention keys; a "status" here would overwrite the lane status).
    return {"artefact_id": 7, "retained": True}


async def _async_none(**k):
    return None


# A fake connection whose capability probes are keyed on the SQL text (the
# fltest rule: a mock cursor must never mis-answer a NEW probe).
class _CapCursor:
    def __init__(self, conn):
        self._conn = conn
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.statements.append((" ".join(sql.split()), params))
        self._last = (sql, params)

    def fetchone(self):
        sql = (self._last or ("", None))[0]
        if "column_name = 'chunk_state'" in sql:
            return (1,) if self._conn.has_chunk_state else None
        if "column_name = 'attempts'" in sql:
            return (1,) if self._conn.has_attempts else None
        if "column_name = 'brain_deferred_since'" in sql:
            return (1,) if self._conn.has_defer_clock else None
        if "column_name = 'reextract_attempts'" in sql:
            return (1,) if self._conn.has_remine else None
        if "SET status = 'processing'" in sql:
            # (chunks, source_ref, attempts[, brain_deferred_since])
            row = (self._conn.chunks, "ref", self._conn.attempts)
            return row + ((None,) if self._conn.has_defer_clock else ())
        if "SELECT chunk_state FROM documents" in sql:
            return (json.dumps(self._conn.ledger),)
        return None

    def fetchall(self):
        sql = (self._last or ("", None))[0]
        if "SELECT id FROM documents" in sql:
            return [(7,)]
        return []


class _CapConn:
    """Drain-shape fake: one pending document, keyable capability columns."""

    def __init__(self, chunks, *, attempts=1, chunk_state=True, attempts_col=True,
                 defer_clock=False, remine=False, ledger=None):
        self.chunks = chunks
        self.attempts = attempts
        self.has_chunk_state = chunk_state
        self.has_attempts = attempts_col
        self.has_defer_clock = defer_clock
        self.has_remine = remine
        self.ledger = ledger or {}
        self.statements = []

    def cursor(self):
        return _CapCursor(self)

    def commit(self):
        return None

    def rollback(self):
        return None


def _requeue_statement(conn):
    for sql, params in conn.statements:
        if "SET status = 'pending'" in sql:
            return sql, params
    return None, None


def _finalize_statement(conn):
    for sql, params in conn.statements:
        if "SET status = %s, ready_at = now()" in sql:
            return sql, params
    return None, None


@pytest.fixture(autouse=True)
def _backend_up(monkeypatch):
    monkeypatch.setattr(embedder, "_backend_is_reachable", lambda _url: True)


# ── hermetic: the cap math ────────────────────────────────────────────────────────────

def test_chunk_pass_admission_cap_math():
    """The pure decision: admit min(owed, cap - active), clamped to [0, owed];
    over budget DEFERS (admit < owed), never refuses; negative cap = no cap."""
    # The wound's exact shape: 3.6k owed chunks against the default 200 cap.
    out = cs.chunk_pass_admission(3600, 0, cap=200)
    assert out["admit"] == 200 and out["deferred"] == 3400
    # Under the cap: everything admits, nothing defers.
    out = cs.chunk_pass_admission(50, 0, cap=200)
    assert out["admit"] == 50 and out["deferred"] == 0
    # Active downstream work eats the budget (aligned with the corpus uplift cap).
    out = cs.chunk_pass_admission(50, 190, cap=200)
    assert out["admit"] == 10 and out["deferred"] == 40
    # A cap of 0 defers everything — the envelope is defer-not-refuse, admit=0.
    out = cs.chunk_pass_admission(5, 0, cap=0)
    assert out["admit"] == 0 and out["deferred"] == 5
    # Negative cap = operator override, byte-parity with the uncapped lane.
    out = cs.chunk_pass_admission(3600, 500, cap=-1)
    assert out["admit"] == 3600 and out["deferred"] == 0


def test_chunk_budget_defaults_to_the_corpus_uplift_cap(monkeypatch):
    """ALIGNMENT PIN: DOC_SEAT_CHUNK_BUDGET unset → the budget IS the corpus
    uplift cap (one number, one doctrine); the env override wins when set."""
    monkeypatch.delenv("DOC_SEAT_CHUNK_BUDGET", raising=False)
    monkeypatch.delenv("CORPUS_UPLIFT_QUEUE_CAP", raising=False)
    assert cs.seat_chunk_budget() == cs.CORPUS_UPLIFT_QUEUE_CAP == 200
    monkeypatch.setenv("CORPUS_UPLIFT_QUEUE_CAP", "77")
    assert cs.seat_chunk_budget() == 77
    monkeypatch.setenv("DOC_SEAT_CHUNK_BUDGET", "12")
    assert cs.seat_chunk_budget() == 12


# ── hermetic: the retry ceiling ───────────────────────────────────────────────────────

def test_retry_ceiling_verdict():
    """PROGRESS NEVER TERMINATES: a pass that reduced the owed count is draining,
    not looping, and is never exhausted — however many passes it takes. An
    UNPRODUCTIVE pass at/over the ceiling exhausts. Negative ceiling disables."""
    assert cs.retry_ceiling_verdict(100, made_progress=True, ceiling=6)[
        "exhausted"] is False
    assert cs.retry_ceiling_verdict(5, made_progress=False, ceiling=6)[
        "exhausted"] is False
    assert cs.retry_ceiling_verdict(6, made_progress=False, ceiling=6)[
        "exhausted"] is True
    assert cs.retry_ceiling_verdict(9, made_progress=False, ceiling=6)[
        "exhausted"] is True
    assert cs.retry_ceiling_verdict(99, made_progress=False, ceiling=-1)[
        "exhausted"] is False
    # Late-bound env resolution (operator override honored at call time).
    import os
    old = os.environ.get("DOC_TEXT_LANE_RETRY_CEILING")
    try:
        os.environ["DOC_TEXT_LANE_RETRY_CEILING"] = "2"
        assert cs.retry_ceiling_verdict(2, made_progress=False)["exhausted"] is True
    finally:
        if old is None:
            os.environ.pop("DOC_TEXT_LANE_RETRY_CEILING", None)
        else:
            os.environ["DOC_TEXT_LANE_RETRY_CEILING"] = old


# ── hermetic: the defer-not-refuse envelope on the LEGACY drain ──────────────────────

def _green_drain(monkeypatch, conn):
    monkeypatch.setattr(embedder, "DOC_CHUNK_FAILURE_LOUD", True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    monkeypatch.setattr(embedder.httpx, "post",
                        lambda *a, **k: _StubResponse({"committed": 1, "staged": 0}))
    calls = []

    def _edges(chunk, *a, **k):
        calls.append(chunk)
        return [{"subject": "a", "rel_type": "r", "object": "b"}]

    monkeypatch.setattr(embedder, "_document_chunk_edges", _edges)
    embedder.drain_pending_documents(conn, "http://backend", "u", schema_name=None)
    return calls


def test_over_budget_pass_defers_not_refuses(monkeypatch):
    """THE CLOG KILL: 5 owed chunks, budget 2 → the pass schedules ONLY 2, the
    rest stay owed in the ledger, and the document RE-PENDS (status='pending'
    with the ledger) — never a terminal finalize, never a refusal."""
    monkeypatch.setattr(embedder, "DOC_TEXT_LANE_BACKPRESSURE", True)
    monkeypatch.setenv("DOC_SEAT_CHUNK_BUDGET", "2")
    conn = _CapConn(["c0", "c1", "c2", "c3", "c4"])
    calls = _green_drain(monkeypatch, conn)

    assert len(calls) == 2, f"exactly the admitted slice runs, got {len(calls)}"
    sql, params = _requeue_statement(conn)
    assert sql is not None, "the document must RE-PEND (defer, never refuse)"
    assert "chunk_state = %s::jsonb" in sql
    ledger = json.loads(params[0])
    chunk_entries = {k: v for k, v in ledger.items() if k != "_bp"}
    assert len(chunk_entries) == 2 and all(v["s"] == "done" for v in chunk_entries.values()), \
        "the admitted slice is terminal in the ledger; the rest carry NO entry"
    assert all(v.get("bp") == 1 for v in chunk_entries.values()), \
        "capped-pass entries carry the budget stamp (the next pass's owed filter)"
    assert ledger["_bp"]["u"] == 0, "a productive pass resets the episode counter"
    assert _finalize_statement(conn) == (None, None), \
        "no terminal finalize on a capped partial pass"


def test_next_pass_admits_the_next_slice_via_the_ledger(monkeypatch):
    """The re-pended document's NEXT claim reads the ledger and admits the NEXT
    slice — progress is cumulative and the drain converges (defer ≠ drop)."""
    monkeypatch.setattr(embedder, "DOC_TEXT_LANE_BACKPRESSURE", True)
    monkeypatch.setenv("DOC_SEAT_CHUNK_BUDGET", "2")
    # Pass 1 already landed chunks 0-1 (the ledger the re-pend wrote).
    conn = _CapConn(["c0", "c1", "c2", "c3", "c4"],
                    ledger={"0": {"s": "done", "c": 1, "g": 0, "bp": 1},
                            "1": {"s": "done", "c": 1, "g": 0, "bp": 1}})
    calls = _green_drain(monkeypatch, conn)
    assert [c for c in calls if c in ("c0", "c1")] == [], \
        "done chunks are NEVER re-spent (the ledger is the owed filter)"
    assert len(calls) == 2, "the next slice admits"

    # Final pass: only chunk 4 left → everything fits under the budget → terminal.
    conn2 = _CapConn(["c0", "c1", "c2", "c3", "c4"],
                     ledger={"0": {"s": "done", "c": 1, "g": 0, "bp": 1},
                             "1": {"s": "done", "c": 1, "g": 0, "bp": 1},
                             "2": {"s": "done", "c": 1, "g": 0, "bp": 1},
                             "3": {"s": "done", "c": 1, "g": 0, "bp": 1}})
    calls2 = _green_drain(monkeypatch, conn2)
    assert calls2 == ["c4"]
    sql, params = _finalize_statement(conn2)
    assert sql is not None and params[0] == "ready", \
        "the last slice finalizes terminal once nothing is owed"


def test_ceiling_stops_the_loop_with_an_honest_terminal(monkeypatch):
    """(b) BOUNDED RETRIES: unproductive passes at the ceiling → TERMINAL, not
    another re-pend. The counter is EPISODE-scoped (the ledger's reserved "_bp"
    key), NOT lifetime attempts — keying on attempts is the 95fb517b defect the
    round-3 defer-clock pin retired, so this ceiling never reads `attempts`."""
    monkeypatch.setattr(embedder, "DOC_TEXT_LANE_BACKPRESSURE", True)
    monkeypatch.setenv("DOC_SEAT_CHUNK_BUDGET", "0")   # admit nothing: no progress
    monkeypatch.setenv("DOC_TEXT_LANE_RETRY_CEILING", "3")
    conn = _CapConn(["c0", "c1"], attempts=1,
                    ledger={"_bp": {"s": "passes", "u": 2}})  # 3rd unproductive pass
    calls = _green_drain(monkeypatch, conn)

    assert calls == [], "nothing admitted (cap 0)"
    sql, params = _finalize_statement(conn)
    assert sql is not None, "the ceiling finalizes — the loop DIES"
    assert params[0] == "partial"
    assert params[3] == 2, "the still-owed chunks are honestly counted failed"


def test_ceiling_never_fires_on_a_progressing_document(monkeypatch):
    """A productive capped pass (owed decreased) re-pends even at/past the
    ceiling — draining a 3.6k-chunk document in 200-chunk passes is the DESIGN,
    never a loop."""
    monkeypatch.setattr(embedder, "DOC_TEXT_LANE_BACKPRESSURE", True)
    monkeypatch.setenv("DOC_SEAT_CHUNK_BUDGET", "2")
    monkeypatch.setenv("DOC_TEXT_LANE_RETRY_CEILING", "3")
    # The episode counter sits PAST the ceiling (5 unproductive passes) — only
    # the progress key can save it, and it must (the counter resets to 0).
    conn = _CapConn(["c0", "c1", "c2"], attempts=1,
                    ledger={"_bp": {"s": "passes", "u": 5}})
    calls = _green_drain(monkeypatch, conn)
    assert len(calls) == 2
    rsql, rparams = _requeue_statement(conn)
    assert rsql is not None, \
        "progress re-pends for the next slice; the ceiling does not fire"
    assert json.loads(rparams[0])["_bp"]["u"] == 0, \
        "a productive pass RESETS the unproductive-episode counter"
    assert _finalize_statement(conn) == (None, None)


def test_unstamped_done_entries_keep_legacy_reread_semantics(monkeypatch):
    """The re-mine keep-honor doctrine survives the ledger read: a done entry
    WITHOUT the budget stamp (written by another lane, e.g. the re-mine
    prepass keep) is RE-READ on a plain non-replay drain, exactly as shipped."""
    monkeypatch.setattr(embedder, "DOC_TEXT_LANE_BACKPRESSURE", True)
    monkeypatch.setenv("DOC_SEAT_CHUNK_BUDGET", "200")   # no cap pressure
    conn = _CapConn(["c0", "c1"],
                    ledger={"0": {"s": "done", "c": 4, "g": 1}})  # NO bp stamp
    calls = _green_drain(monkeypatch, conn)
    assert sorted(calls) == ["c0", "c1"], \
        "unstamped done chunks are re-read (legacy semantics preserved)"
    sql, params = _finalize_statement(conn)
    assert sql is not None and params[0] == "ready"


def test_flag_off_is_byte_parity_uncapped_single_pass(monkeypatch):
    """OFF = byte-parity with the legacy lane: the budget env is set but the
    flag is OFF → every chunk schedules in ONE pass, terminal finalize, and the
    pass never reads the chunk_state ledger (the legacy non-replay shape)."""
    monkeypatch.setattr(embedder, "DOC_TEXT_LANE_BACKPRESSURE", False)
    monkeypatch.setenv("DOC_SEAT_CHUNK_BUDGET", "2")
    conn = _CapConn(["c0", "c1", "c2", "c3", "c4"])
    calls = _green_drain(monkeypatch, conn)

    assert len(calls) == 5, "OFF: one uncapped pass, exactly as today"
    sql, params = _finalize_statement(conn)
    assert sql is not None and params[0] == "ready"
    assert not any("SELECT chunk_state FROM documents" in s
                   for s, _ in conn.statements), \
        "OFF never reads the ledger on a non-replay drain (byte-parity)"


# ── hermetic: the queue lane enqueues at most the budget ─────────────────────────────

def test_queue_lane_enqueues_at_most_the_budget(monkeypatch):
    """The 3.6k-queued-brain-chunks wound was THIS enqueue: the Redis queue
    received every owed chunk at once. Under the flag it receives at most the
    budget; the rest stay owed (the caller's owed branch re-pends)."""
    from src.ingest import chunk_queue as cq

    enqueued = []

    class _FakeQueue:
        def __init__(self, *a, **k):
            pass

        def purge(self, tenant, batch):
            return None

        def enqueue(self, items):
            enqueued.extend(items)
            return len(items)

    def _fake_run_batch(queue, tenant, batch, conc, on_result=None,
                        deadline_seconds=None):
        return {"ok": 0, "failed": 0, "dead": 0, "nohandler": 0, "fatal": 0,
                "reaped": 0, "concurrency": conc, "fatal_error": None}

    monkeypatch.setattr(embedder, "DOC_TEXT_LANE_BACKPRESSURE", True)
    monkeypatch.setenv("DOC_SEAT_CHUNK_BUDGET", "3")
    monkeypatch.setattr(cq, "RedisWorkQueue", _FakeQueue)
    monkeypatch.setattr(cq, "run_batch", _fake_run_batch)
    chunks = [f"chunk {i}" for i in range(100)]
    embedder._drain_document_via_queue(
        None, 7, chunks, {}, user_id="u", backend_url="http://b",
        source_ref="r", statement_route="rewrite", schema_name=None)
    assert len(enqueued) == 3, \
        f"exactly the budget enqueues, got {len(enqueued)} of {len(chunks)}"

    # OFF parity: the whole owed list rides, exactly as today.
    enqueued.clear()
    monkeypatch.setattr(embedder, "DOC_TEXT_LANE_BACKPRESSURE", False)
    embedder._drain_document_via_queue(
        None, 7, chunks, {}, user_id="u", backend_url="http://b",
        source_ref="r", statement_route="rewrite", schema_name=None)
    assert len(enqueued) == 100


# ── hermetic: the text lane is wired into the snapshot store ─────────────────────────

@pytest.mark.asyncio
async def test_text_lane_snapshots_before_any_chunking(monkeypatch):
    """Flag ON: ingest_document POSTs the ORIGINAL utf-8 text to
    /corpus/documents BEFORE the chunking-driven enqueue, pages=None (the API
    derives the 8k pages from the bytes), and the response carries the honest
    corpus report."""
    import src.mcp.server as S

    cli = _RecordingClient()
    monkeypatch.setattr(S, "_client", lambda: cli)
    monkeypatch.setattr(S, "DOC_INGEST_SNAPSHOT_STORE", True)
    monkeypatch.setattr(S, "_retain_document_source", lambda **k: _async_retain())
    monkeypatch.setattr(S, "_mint_document_stub_facts", lambda **k: _async_none())

    text = "Paragraph one of a pasted document.\n\nParagraph two."
    out = await S.ingest_document_tool(
        text=text, user_id="11111111-1111-1111-1111-111111111111",
        source_ref="notes.md", title="notes")
    corpus_posts = [u for u, _ in cli.posts if u.endswith("/corpus/documents")]
    assert corpus_posts == ["%s/corpus/documents" % S.FAULTLINE_API_URL], \
        "exactly ONE bounded snapshot POST"
    body = cli.corpus_body
    import base64 as b64
    assert b64.b64decode(body["data_b64"]).decode("utf-8") == text, \
        "the artifact is the ORIGINAL TEXT, verbatim"
    assert body["pages"] is None, "the API derives pages from the bytes itself"
    assert body["source_ref"] == "notes.md"
    # BEFORE any chunking decision: the snapshot precedes the registry enqueue.
    urls = [u for u, _ in cli.posts]
    assert urls.index(corpus_posts[0]) < next(
        i for i, u in enumerate(urls) if u.endswith("/documents/enqueue"))
    assert out["corpus"]["status"] == "ready"
    assert out["status"] == "pending"


@pytest.mark.asyncio
async def test_text_lane_flag_off_is_byte_parity(monkeypatch):
    """Flag OFF: no /corpus/documents POST and NO `corpus` key — the response
    shape is byte-identical to the pre-wiring text lane."""
    import src.mcp.server as S

    cli = _RecordingClient()
    monkeypatch.setattr(S, "_client", lambda: cli)
    monkeypatch.setattr(S, "DOC_INGEST_SNAPSHOT_STORE", False)
    monkeypatch.setattr(S, "_retain_document_source", lambda **k: _async_retain())
    monkeypatch.setattr(S, "_mint_document_stub_facts", lambda **k: _async_none())
    out = await S.ingest_document_tool(
        text="pasted text body", user_id="11111111-1111-1111-1111-111111111111",
        source_ref="notes.md")
    assert not any(u.endswith("/corpus/documents") for u, _ in cli.posts)
    assert "corpus" not in out
    assert out["status"] == "pending"


@pytest.mark.asyncio
async def test_snapshot_failure_never_breaks_the_text_ingest(monkeypatch):
    """The snapshot handoff NEVER raises into the ingest: a failed POST logs
    crit and the legacy lane continues — the artefact/document safety nets hold."""
    import src.mcp.server as S

    class _BoomClient:
        async def post(self, url, json=None, timeout=None):
            if url.endswith("/corpus/documents"):
                raise RuntimeError("corpus door down")
            return _FakeResp({"status": "pending", "document_id": 41,
                              "chunk_count": 1, "eta_seconds": 5})

    monkeypatch.setattr(S, "_client", lambda: _BoomClient())
    monkeypatch.setattr(S, "DOC_INGEST_SNAPSHOT_STORE", True)
    monkeypatch.setattr(S, "_retain_document_source", lambda **k: _async_retain())
    monkeypatch.setattr(S, "_mint_document_stub_facts", lambda **k: _async_none())
    out = await S.ingest_document_tool(
        text="still ingests", user_id="11111111-1111-1111-1111-111111111111",
        source_ref="s")
    assert out["status"] == "pending"
    assert out["corpus"] is None, "honest None: the snapshot did NOT land"


# ── DB-gated legs: throwaway database + the real migration corpus ─────────────────────

_DSN = os.environ.get("POSTGRES_DSN", "")
_db = pytest.mark.skipif(
    not _DSN, reason="POSTGRES_DSN unset — the DB gate stays dark")


@pytest.fixture(scope="module")
def corpus_db():
    """Boot ONE throwaway database with the real migration corpus (incl.
    327/328), mint ONE fresh tenant schema, hand back (dsn, uid, schema)."""
    if not _DSN:
        pytest.skip("POSTGRES_DSN not set")
    mig_dir = os.environ.get("FAULTLINE_MIGRATIONS_DIR", "migrations")
    if not os.path.isdir(mig_dir):
        pytest.skip(f"migrations dir {mig_dir!r} not found")
    from urllib.parse import urlsplit, urlunsplit
    import uuid
    import psycopg2
    from src.provisioning import boot_migrations as bm
    from src.provisioning.schema_manager import create_user_schema

    p = urlsplit(_DSN)
    base = p.path.lstrip("/")
    assert base.endswith("_test"), \
        "refusing to derive a throwaway from a non-*_test database"
    name = f"{base}_txtln{uuid.uuid4().hex[:6]}"[:63]
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    dsn = urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))
    saved = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = dsn
    try:
        boot = bm.run_boot_migrations(dsn, mig_dir, echo=False)
        assert boot.fatal is None and boot.ledger_active, (boot.fatal, boot.ledger_error)
        uid = str(uuid.uuid4())
        schema, st = create_user_schema(uid, uid.replace("-", "_"))
        assert st == "ready", st
        yield {"dsn": dsn, "uid": uid, "schema": schema,
               "mig_dir": mig_dir}
    finally:
        if saved is None:
            os.environ.pop("POSTGRES_DSN", None)
        else:
            os.environ["POSTGRES_DSN"] = saved
        drop = psycopg2.connect(_DSN)
        drop.autocommit = True
        try:
            with drop.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            drop.close()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@_db
def test_db_text_snapshot_lands_with_the_family_contract(corpus_db, monkeypatch):
    """THE WIRE, END TO END ON A FIXTURE: the exact payload the wired text lane
    sends (utf-8 text, pages=None) lands in corpus_documents — sha256 fidelity,
    8k page split — and the versioning family contract applies: same bytes
    dedupe, changed bytes mint version 2 and supersede."""
    import src.api.main as M

    monkeypatch.setenv("POSTGRES_DSN", corpus_db["dsn"])
    monkeypatch.setattr(M, "_INGEST_ENABLED", True, raising=False)
    # >8000 chars so the API's budget split produces multiple text pages.
    raw = ("Text-lane fixture paragraph.\n\n" * 700).encode("utf-8")
    assert len(raw) > 8000

    out1 = M.corpus_documents_store(M.CorpusDocumentRequest(
        user_id=corpus_db["uid"], data_b64=_b64(raw),
        original_filename="notes.md", source_ref="notes.md"))
    assert out1["status"] == "ready", out1
    assert out1["page_count"] >= 2, "the 8k budget split ran (pages=None)"
    assert out1["document_family"], "a NAMED text artefact derives a family"

    import psycopg2
    from src.ingest.corpus_store import decompress_snapshot, content_sha256
    conn = psycopg2.connect(corpus_db["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {corpus_db['schema']}")
            cur.execute(
                "SELECT raw_snapshot, snapshot_codec, content_sha256, status, "
                "version_seq FROM corpus_documents WHERE id = %s",
                (out1["document_id"],))
            blob, codec, sha, status, seq = cur.fetchone()
            assert status == "ready" and seq == 1
            assert decompress_snapshot(bytes(blob), codec) == raw  # byte-fidelity
            assert content_sha256(raw) == sha

            # SAME bytes re-ingested (the pasted-again document): DEDUPE, no new row.
            out2 = M.corpus_documents_store(M.CorpusDocumentRequest(
                user_id=corpus_db["uid"], data_b64=_b64(raw),
                original_filename="notes.md", source_ref="notes.md"))
            assert out2.get("duplicate") is True
            assert out2["document_id"] == out1["document_id"]

            # CHANGED bytes (the edited paste): version 2, version 1 superseded.
            out3 = M.corpus_documents_store(M.CorpusDocumentRequest(
                user_id=corpus_db["uid"], data_b64=_b64(raw + b"\n\nEdited tail."),
                original_filename="notes.md", source_ref="notes.md"))
            assert out3.get("duplicate") is False and out3["version_seq"] == 2
            cur.execute(
                "SELECT count(*), max(version_seq), "
                "count(*) FILTER (WHERE superseded_at IS NOT NULL) "
                "FROM corpus_documents WHERE document_family = %s",
                (out1["document_family"],))
            n, max_seq, superseded = cur.fetchone()
            assert (n, max_seq, superseded) == (2, 2, 1), \
                "two live generations; the archived one is superseded"
    finally:
        conn.close()
