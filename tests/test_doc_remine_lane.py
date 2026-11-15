"""DOCUMENT RE-MINE LANE (gauntlet R2) — migration-298 markers + the bounded re-mine pass.

The migration-197 analog for the per-tenant `documents` table: a document whose chunks
produced ZERO committed edges, or that terminated 'partial', becomes RE-MINABLE against a
grown ontology — bounded, replay-safe, OFF by default.

WHAT EACH PIN BUYS (mutation-tested: revert the behaviour, the named test goes red):
  • OFF by default — DOC_REMINE_ENABLED unset ⇒ the pass never even opens a connection
    (applying migration 298 changes nothing at runtime; the owner flips the flag).
  • MARKER STAMPED ON INGEST — a terminal finalize with zero committed edges / 'partial'
    stamps reextract_ontology_mark (walkable-place count); happy path untouched.
  • UPDATED ON RE-MINE — the flip bumps reextract_attempts (RETURNING proves it).
  • ATTEMPTS CAP — a doc at DOC_REMINE_MAX_ATTEMPTS is never selected again (no infinite
    re-mining); a doc with attempts < cap but young (age floor) is not either; a happy
    'ready' doc with committed edges is never selected; pending/processing never touched.
  • CHUNK-LEVEL RE-OPEN — done chunks that COMMITTED something are KEPT (re-reading them
    would double-count); zero-edge done chunks (c=0) and failed chunks are re-owed by
    dropping their ledger entries.
  • REPLAY MARKER — a re-mine drain (reextract_attempts > 0) tags EVERY /ingest POST of
    that document with X-FL-Deferred-Replay: 1 + X-FL-Replay-Lane: reextract (the
    retired-value freeze in main.py::ingest MUST engage); a FIRST drain (attempts = 0)
    carries NO marker; the marker rides the HEADER, never the body (the idempotency key
    hashes edges). Same fixture family as test_reextract_replay_resurrection.py.
  • BATCH BOUND + ROUND-ROBIN — at most DOC_REMINE_BATCH docs per pass, ONE per tenant,
    rotation cursor advances to the last served tenant; pass cadence self-paced.
  • SHARED BACKOFF — the flip is byte-for-byte the POST /documents/retry transition
    (status='pending', started_at=NULL): once the drain rate-defers the re-mined doc, the
    EXISTING claim predicate + DOC_RETRY_BACKOFF reclaim gate govern it (pinned in
    test_doc_drain_resilience.py) — a paced re-mine reschedules cheaply, never hammers.
  • PRE-298 SCHEMA FAIL-SAFE — a tenant without the columns self-disables (log-once), the
    drain runs byte-for-byte as shipped (replay=False everywhere, no stamping).

The fakes model the REAL SQL semantics the way test_doc_drain_resilience.py does: SQL text
captured verbatim, eligibility predicates evaluated against modelled row state, and the
flip rechecking its STILL-terminal WHERE guard.
"""
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.re_embedder.embedder as embedder  # noqa: E402
from src.api import ingest_transport  # noqa: E402


# ── OFF by default ───────────────────────────────────────────────────────────────────

def test_remine_disabled_by_default_clean_env(monkeypatch):
    """The flag ships OFF: a clean environment must not arm the lane (the owner flips it
    after deploy — the runbook)."""
    monkeypatch.delenv("DOC_REMINE_ENABLED", raising=False)
    import importlib
    importlib.reload(embedder)
    try:
        assert embedder.DOC_REMINE_ENABLED is False
    finally:
        monkeypatch.undo()
        importlib.reload(embedder)
    # belt-and-braces on the live module too
    assert embedder.DOC_REMINE_ENABLED is False


def test_remine_flag_off_never_connects(monkeypatch):
    monkeypatch.setattr(embedder, "DOC_REMINE_ENABLED", False)

    def _explode(*a, **k):
        raise AssertionError("flag-OFF pass must not open a connection")
    monkeypatch.setattr(embedder.psycopg2, "connect", _explode)
    assert embedder.remine_documents_prepass("dsn", [("u" * 32, "faultline_x")], set()) == 0


def test_remine_default_bounds():
    """The documented defaults: batch=2, attempts=3, age=24h, cadence=900s."""
    assert embedder._DOC_REMINE_BATCH >= 1
    assert embedder._DOC_REMINE_MAX_ATTEMPTS == 3
    assert embedder._DOC_REMINE_MIN_AGE_HOURS >= 0
    assert embedder._DOC_REMINE_INTERVAL_S >= 60


# ── transport: the replay marker on the /ingest POST ─────────────────────────────────

class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _drive_chunk(monkeypatch, replay, edges=None):
    posts = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        posts.append((url, dict(headers or {}), json))
        return _Resp({"committed": 1, "staged": 0})

    monkeypatch.setattr(embedder.httpx, "post", fake_post)
    monkeypatch.setattr(embedder, "_document_chunk_edges",
                        lambda chunk, user_id, backend_url, route, *a, **kw: (
                            edges if edges is not None
                            else [{"subject": "farxen", "rel_type": "weight",
                                   "object": "61 pounds"}]))
    res = embedder._process_document_chunk(
        "My wobbly farxen weighs 61 pounds.", idx=0, doc_id=7, user_id="u" * 32,
        backend_url="http://backend", source_ref=None, statement_route="rewrite",
        replay=replay)
    return res, posts


def test_remine_ingest_post_carries_replay_marker(monkeypatch):
    """THE pin: a re-mine drain (replay=True) marks its /ingest POST as a replay — the
    retired-value freeze in main.py::ingest engages, a /retract/correct-retired value is
    NEVER resurrected by the re-send."""
    _res, posts = _drive_chunk(monkeypatch, replay=True)
    assert _res[2] == 0  # not failed
    ingest_posts = [p for p in posts if p[0].endswith("/ingest")]
    assert len(ingest_posts) == 1
    _url, headers, _body = ingest_posts[0]
    assert (headers.get(ingest_transport.REPLAY_MARKER_HEADER)
            == ingest_transport.REPLAY_MARKER_VALUE)
    assert (headers.get(ingest_transport.REPLAY_LANE_HEADER)
            == ingest_transport.REPLAY_LANE_REEXTRACT)


def test_first_drain_ingest_post_carries_no_marker(monkeypatch):
    """NEVER on a first attempt: replay=False (attempts = 0, pre-298 schema) must produce
    the byte-for-byte shipped POST shape — lane header only, no replay marker."""
    _res, posts = _drive_chunk(monkeypatch, replay=False)
    ingest_posts = [p for p in posts if p[0].endswith("/ingest")]
    assert len(ingest_posts) == 1
    _url, headers, _body = ingest_posts[0]
    assert ingest_transport.REPLAY_MARKER_HEADER not in headers
    assert ingest_transport.REPLAY_LANE_HEADER not in headers
    # the ordinary background-lane header is still there
    assert headers


def test_remine_marker_rides_header_never_body(monkeypatch):
    """Marker contract: HEADER-only. The /ingest body must not grow any replay key — the
    idempotency key hashes edges and byte-identity between attempts is the double-write
    guard."""
    _res, posts = _drive_chunk(monkeypatch, replay=True)
    for url, _headers, body in posts:
        if url.endswith("/ingest"):
            assert not any("replay" in str(k).lower() for k in body)


def test_salvage_ingest_post_also_marked(monkeypatch):
    """DOCLOSS-B salvage (partial edges committed on a failed chunk) is the SAME re-send
    when the drain is a replay — its /ingest POST must carry the marker too."""
    import src.api.errors as _errors

    class _Failed(Exception):
        partial_edges = [{"subject": "a", "rel_type": "b", "object": "c"}]

    def fake_edges(chunk, user_id, backend_url, route, *args, **kwargs):
        _exc = embedder.DocumentChunkExtractionFailed(
            _errors.public_detail(Exception("extractor exploded"), where="t"))
        _exc.partial_edges = [{"subject": "a", "rel_type": "b", "object": "c"}]
        raise _exc

    monkeypatch.setattr(embedder, "_document_chunk_edges", fake_edges)
    posts = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        posts.append((url, dict(headers or {}), json))
        return _Resp({"committed": 1, "staged": 0})

    monkeypatch.setattr(embedder.httpx, "post", fake_post)
    # the failure is converted to a failed-chunk result (DOCLOSS-B), never raised out
    c, s, f, _reason = embedder._process_document_chunk(
        "chunk text", idx=0, doc_id=7, user_id="u" * 32,
        backend_url="http://backend", source_ref=None,
        statement_route="rewrite", replay=True)
    assert (c, s, f) == (0, 0, 1)
    ingest_posts = [p for p in posts if p[0].endswith("/ingest")]
    assert len(ingest_posts) == 1
    _url, headers, _body = ingest_posts[0]
    assert (headers.get(ingest_transport.REPLAY_MARKER_HEADER)
            == ingest_transport.REPLAY_MARKER_VALUE)


# ── the SQL-faithful fake for remine_documents_prepass ───────────────────────────────

class _RemineCursor:
    """Models the prepass's four statements against modelled `documents` rows:

      1. information_schema probe  → (1,) when the schema "has" migration 298
      2. SET search_path           → no-op
      3. the candidate SELECT      → predicate evaluated against modelled rows
      4. count(*) FROM rel_types   → the walkable-place mark (fixed 42)
      5. the flip UPDATE           → rechecks STILL-terminal, bumps reextract_attempts,
                                     RETURNING the new attempt count
    """

    MARK = 42

    def __init__(self, conn):
        self._conn = conn
        self._row = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql_norm = " ".join(str(sql).split())
        self._conn.statements.append((sql_norm, params))
        self._row = None
        self.rowcount = 0
        if "information_schema.columns" in sql_norm:
            self._row = (1,) if self._conn.has_remine_cols else None
        elif sql_norm.startswith("SET search_path"):
            pass
        elif "SELECT id, chunk_state FROM documents" in sql_norm:
            max_attempts, min_age_h = params
            for doc in self._conn.docs.values():
                eligible = (
                    doc["status"] in ("ready", "partial", "error")
                    and (doc["facts_committed"] == 0 or doc["status"] == "partial")
                    and doc["reextract_attempts"] < max_attempts
                    and doc["age_h"] >= min_age_h
                )
                if eligible:
                    self._row = (doc["id"], json.dumps(doc["chunk_state"]))
                    break
        elif "count(*) FROM rel_types" in sql_norm:
            self._row = (self.MARK,)
        elif "UPDATE documents" in sql_norm and "RETURNING reextract_attempts" in sql_norm:
            # litmus fix L5: params carry an attempt DELTA (1 normally, 0 when the
            # prepass refunds a PURE-PACED terminal — every failed chunk was
            # rate_deferred, the brain was never asked). Modelled faithfully here.
            (_mark, _delta, _kept, doc_id, user_id) = params
            doc = self._conn.docs.get(doc_id)
            still_terminal = (
                doc is not None
                and doc["status"] in ("ready", "partial", "error")
                and (doc["facts_committed"] == 0 or doc["status"] == "partial")
                and doc["user_id"] == user_id
            )
            if still_terminal and not self._conn.steal_the_flip:
                doc["status"] = "pending"
                doc["reextract_attempts"] += _delta
                doc["chunk_state"] = json.loads(_kept)
                self.rowcount = 1
                self._row = (doc["reextract_attempts"],)
            else:
                self.rowcount = 0
                self._row = None
        elif sql_norm.startswith("SELECT reextract_attempts FROM documents"):
            doc = self._conn.docs.get(params[0])
            self._row = (doc["reextract_attempts"],) if doc else None

    def fetchone(self):
        return self._row


class _RemineConn:
    def __init__(self, docs, has_remine_cols=True):
        self.docs = docs                # doc_id -> modelled row (mutated by the flip)
        self.has_remine_cols = has_remine_cols
        self.steal_the_flip = False
        self.statements = []

    def cursor(self):
        return _RemineCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _doc(doc_id=1, user_id="u" * 32, status="partial", facts_committed=0,
         reextract_attempts=0, age_h=48.0, chunk_state=None):
    return {"id": doc_id, "user_id": user_id, "status": status,
            "facts_committed": facts_committed, "reextract_attempts": reextract_attempts,
            "age_h": age_h,
            "chunk_state": chunk_state if chunk_state is not None
            else {"0": {"s": "done", "c": 0, "g": 2}, "1": {"s": "failed", "a": 2, "e": "x"},
                  "2": {"s": "done", "c": 4, "g": 1}}}


@pytest.fixture()
def remine_env(monkeypatch):
    """Arm the lane, reset the pass cadence + cursor, silence the suspension probe."""
    monkeypatch.setattr(embedder, "DOC_REMINE_ENABLED", True)
    monkeypatch.setattr(embedder, "_DOC_REMINE_LAST_RUN", [0.0])
    monkeypatch.setattr(embedder, "_DOC_REMINE_CURSOR", [None])
    monkeypatch.setattr(embedder, "_saas_suspended_user_ids", lambda dsn, uids: set())
    yield monkeypatch


def _patch_connect(monkeypatch, conn):
    monkeypatch.setattr(embedder.psycopg2, "connect", lambda dsn=None: conn)


def test_remine_flips_eligible_doc_reopens_zero_edge_and_failed(
        remine_env, monkeypatch, caplog):
    """THE core pass: eligible terminal zero-edge doc → flipped pending, zero-edge (c=0)
    + failed chunks RE-OWED, committing chunks KEPT, attempts bumped, mark stamped."""
    conn = _RemineConn({1: _doc()})
    _patch_connect(monkeypatch, conn)
    n = embedder.remine_documents_prepass("dsn", [("u" * 32, "faultline_x")], set())
    assert n == 1
    doc = conn.docs[1]
    assert doc["status"] == "pending"
    assert doc["reextract_attempts"] == 1
    # c=0 done chunk + failed chunk re-owed (entries dropped); the committing chunk kept
    assert doc["chunk_state"] == {"2": {"s": "done", "c": 4, "g": 1}}
    # the mark bookkeeping rode the flip
    flip = [s for s, _p in conn.statements if s.startswith("UPDATE documents")][0]
    assert "reextract_ontology_mark" in flip
    # litmus fix L5: the flip carries an attempt DELTA param (+1 normal, +0 pure-paced
    # refund) — assert the SQL shape, the paramised delta is modelled by the fake above
    assert "reextract_attempts = reextract_attempts + %s" in flip
    # the shared-backoff contract: the flip is the /documents/retry transition —
    # pending + started_at NULL, so the normal claim predicate + DOC_RETRY_BACKOFF
    # reclaim gate govern every subsequent re-pend (paced, never hammered).
    assert "status = 'pending'" in flip and "started_at = NULL" in flip
    assert "attempts = 0" in flip  # fresh redelivery budget, mirroring retry_document


def test_remine_attempts_cap_no_infinite_remining(remine_env, monkeypatch):
    """A doc AT the cap is never selected again: reextract_attempts == MAX ⇒ no flip, no
    UPDATE against the tenant at all."""
    conn = _RemineConn({1: _doc(reextract_attempts=embedder._DOC_REMINE_MAX_ATTEMPTS)})
    _patch_connect(monkeypatch, conn)
    n = embedder.remine_documents_prepass("dsn", [("u" * 32, "faultline_x")], set())
    assert n == 0
    assert not any(s.startswith("UPDATE documents") for s, _p in conn.statements)
    assert conn.docs[1]["status"] == "partial"  # untouched


def test_remine_age_floor(remine_env, monkeypatch):
    """A just-finalized doc is not re-mined: age floor (DOC_REMINE_MIN_AGE_HOURS) must
    elapse on the terminal state first."""
    conn = _RemineConn({1: _doc(age_h=0.5)})
    _patch_connect(monkeypatch, conn)
    assert embedder.remine_documents_prepass(
        "dsn", [("u" * 32, "faultline_x")], set()) == 0
    assert not any(s.startswith("UPDATE documents") for s, _p in conn.statements)


def test_remine_never_touches_committed_or_active_docs(remine_env, monkeypatch):
    """A happy 'ready' doc with committed edges, and live pending/processing rows, are
    outside the cohort — the pass must not select them."""
    docs = {
        1: _doc(1, status="ready", facts_committed=9),
        2: _doc(2, status="pending", facts_committed=0),
        3: _doc(3, status="processing", facts_committed=0),
    }
    conn = _RemineConn(docs)
    _patch_connect(monkeypatch, conn)
    assert embedder.remine_documents_prepass(
        "dsn", [("u" * 32, "faultline_x")], set()) == 0
    assert not any(s.startswith("UPDATE documents") for s, _p in conn.statements)
    for d in docs.values():
        assert d["reextract_attempts"] == 0


def test_remine_partial_with_committed_edges_reopens_failed_only(remine_env, monkeypatch):
    """A 'partial' doc WITH committed edges is in-cohort by the second arm — but only its
    failed/zero-edge chunks re-open; the committing chunks are kept."""
    state = {"0": {"s": "done", "c": 3, "g": 0}, "1": {"s": "failed", "a": 1, "e": "t/o"}}
    conn = _RemineConn({1: _doc(1, status="partial", facts_committed=3, chunk_state=state)})
    _patch_connect(monkeypatch, conn)
    assert embedder.remine_documents_prepass(
        "dsn", [("u" * 32, "faultline_x")], set()) == 1
    assert conn.docs[1]["chunk_state"] == {"0": {"s": "done", "c": 3, "g": 0}}


def test_remine_batch_bound_and_round_robin(remine_env, monkeypatch):
    """Batch=2, one per tenant: two DIFFERENT tenants served per pass, and the rotation
    cursor advances so the NEXT pass starts AFTER the last tenant served."""
    monkeypatch.setattr(embedder, "_DOC_REMINE_BATCH", 2)
    u1, u2, u3 = "a" * 32, "b" * 32, "c" * 32
    docs = {i: _doc(i, user_id=uid) for i, uid in ((1, u1), (2, u2), (3, u3))}
    conn = _RemineConn(docs)
    _patch_connect(monkeypatch, conn)
    tenants = [(u1, "faultline_a"), (u2, "faultline_b"), (u3, "faultline_c")]
    n1 = embedder.remine_documents_prepass("dsn", tenants, set())
    assert n1 == 2
    flipped1 = sorted(i for i, d in docs.items() if d["status"] == "pending")
    assert len(flipped1) == 2
    assert embedder._DOC_REMINE_CURSOR[0] == docs[flipped1[1]]["user_id"]
    # pass 2 (reset ONLY the cadence — the cursor persists, that's the rotation):
    remine_env.setattr(embedder, "_DOC_REMINE_LAST_RUN", [0.0])
    n2 = embedder.remine_documents_prepass("dsn", tenants, set())
    assert n2 == 1
    # all three docs flipped across the two passes, none twice
    assert all(d["status"] == "pending" for d in docs.values())
    assert all(d["reextract_attempts"] == 1 for d in docs.values())


def test_remine_one_per_tenant_per_pass(remine_env, monkeypatch):
    """Round-robin is PER-TENANT: a tenant with several eligible docs gives up ONE per
    pass — a single corpus never hogs the batch."""
    monkeypatch.setattr(embedder, "_DOC_REMINE_BATCH", 5)
    uid = "u" * 32
    docs = {1: _doc(1, user_id=uid), 2: _doc(2, user_id=uid), 3: _doc(3, user_id=uid)}
    conn = _RemineConn(docs)
    _patch_connect(monkeypatch, conn)
    assert embedder.remine_documents_prepass(
        "dsn", [(uid, "faultline_x")], set()) == 1
    assert [i for i, d in docs.items() if d["status"] == "pending"] == [1]


def test_remine_flip_guard_stolen_row_not_counted(remine_env, monkeypatch):
    """The flip rechecks STILL-terminal: a row a concurrent drain/retry already took makes
    the UPDATE a no-op (rowcount 0) — never double-queued, never mis-counted."""
    conn = _RemineConn({1: _doc()})
    conn.steal_the_flip = True
    _patch_connect(monkeypatch, conn)
    assert embedder.remine_documents_prepass(
        "dsn", [("u" * 32, "faultline_x")], set()) == 0


def test_remine_pre298_schema_self_disables(remine_env, monkeypatch):
    """A tenant without migration 298 is skipped with a log-once warning and its doc is
    untouched (per-tenant isolation — the flag never stops the drain)."""
    conn_old = _RemineConn({9: _doc(9, user_id="b" * 32)}, has_remine_cols=False)
    _patch_connect(monkeypatch, conn_old)
    assert embedder.remine_documents_prepass(
        "dsn", [("b" * 32, "faultline_old")], set()) == 0
    assert "faultline_old" in embedder._remine_columns_missing_schemas
    assert conn_old.docs[9]["status"] == "partial"  # untouched
    assert not any(s.startswith("UPDATE documents") for s, _p in conn_old.statements)


def test_remine_self_disabled_tenant_skipped_without_probe(remine_env, monkeypatch):
    """After self-disabling, the tenant is skipped via the set — it never even probes
    again, and other tenants still get served."""
    conn = _RemineConn({1: _doc()})
    _patch_connect(monkeypatch, conn)
    embedder._remine_columns_missing_schemas.add("faultline_old")
    probes = []
    real_execute = _RemineCursor.execute

    def _counting_execute(self, sql, params=None):
        if "information_schema.columns" in " ".join(str(sql).split()):
            probes.append(sql)
        real_execute(self, sql, params)

    monkeypatch.setattr(_RemineCursor, "execute", _counting_execute)
    n = embedder.remine_documents_prepass(
        "dsn", [("b" * 32, "faultline_old"), ("u" * 32, "faultline_x")], set())
    assert n == 1
    # exactly ONE capability probe happened — the OK tenant's; the self-disabled
    # tenant was skipped via the set without probing.
    assert len(probes) == 1


def test_remine_pass_cadence_self_paced(remine_env, monkeypatch):
    """The pass runs at most once per DOC_REMINE_INTERVAL_SECONDS no matter how often the
    4s daemon ticks — the second immediate call is a no-op."""
    conn = _RemineConn({1: _doc()})
    _patch_connect(monkeypatch, conn)
    assert embedder.remine_documents_prepass(
        "dsn", [("u" * 32, "faultline_x")], set()) == 1
    # immediate re-tick (daemon fires every 4s; interval default 900s): cadence-gated
    remine_env.setattr(embedder, "_DOC_REMINE_LAST_RUN",
                       [embedder._DOC_REMINE_LAST_RUN[0]])
    assert embedder.remine_documents_prepass(
        "dsn", [("u" * 32, "faultline_x")], set()) == 0


def test_remine_never_raises(remine_env, monkeypatch):
    """Prepass contract: any failure logs and returns — it never poisons the caller."""
    def _explode(dsn=None):
        raise RuntimeError("db gone")
    monkeypatch.setattr(embedder.psycopg2, "connect", _explode)
    assert embedder.remine_documents_prepass(
        "dsn", [("u" * 32, "faultline_x")], set()) == 0


# ── drain-side wiring pins (source assertions, the suite's inline-code convention) ───

def test_drain_threads_replay_from_remine_attempts():
    """The drain decides replay-ness from the ROW (reextract_attempts > 0), read via the
    guarded helper — never a claim-ladder fork — and threads it into BOTH chunk lanes."""
    src = open(embedder.__file__, encoding="utf-8").read()
    assert "_read_remine_attempts(db_conn, doc_id, schema_name) > 0" in src
    assert "_has_remine_cols\n                           and _read_remine_attempts" in src
    assert "statement_route=statement_route, replay=_doc_replay)" in src
    assert "replay=_doc_replay)" in src  # the queue-lane call


def test_queue_payload_carries_replay_flag():
    """The queue WorkItem payload carries the flag so a worker in ANOTHER process tags its
    own /ingest POST (an old in-flight worker without the key parses False — safe)."""
    src = open(embedder.__file__, encoding="utf-8").read()
    assert '"replay": bool(replay)' in src
    assert "replay=bool(p.get(\"replay\"))" in src


def test_remine_reader_fails_safe_to_not_a_replay():
    """Pre-298 schema / any read error ⇒ 0 = NOT a replay = the shipped POST shape."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.execute.side_effect = Exception("column does not exist")
    conn.commit = lambda: None
    conn.rollback = lambda: None
    assert embedder._read_remine_attempts(conn, 1, None) == 0


def test_finalize_stamps_mark_on_zero_edge_or_partial_only():
    """'Marker stamped on ingest': the terminal finalize stamps reextract_ontology_mark —
    but ONLY on the re-mine cohort (committed == 0 or partial); the happy path gains
    zero queries."""
    src = open(embedder.__file__, encoding="utf-8").read()
    needle = "if _has_remine_cols and (committed == 0 or _terminal == \"partial\"):"
    assert needle in src
    assert "UPDATE documents SET reextract_ontology_mark = %s " in src


def test_standalone_loop_and_main_loop_call_the_pass():
    """Both drain entry points run the pass alongside fast_drain_pending_documents_prepass
    (separately gated inside the helper — pacing never multiplies)."""
    src = open(embedder.__file__, encoding="utf-8").read()
    assert src.count("remine_documents_prepass(") == 3  # def + standalone loop + main loop


# ── migration + template pins ─────────────────────────────────────────────────────────

def test_migration_298_fans_out_and_template_carries_columns():
    mig = os.path.join(os.path.dirname(__file__), "..", "migrations",
                       "298_documents_ontology_regrowth_marks.sql")
    body = open(mig, encoding="utf-8").read()
    # fan-out to every tenant schema, guard on the documents table, idempotent columns
    assert "LIKE 'faultline\\_%'" in body
    assert "table_name = 'documents'" in body
    assert "ADD COLUMN IF NOT EXISTS reextract_ontology_mark BIGINT" in body
    assert "ADD COLUMN IF NOT EXISTS reextract_attempts      INTEGER NOT NULL DEFAULT 0" in body
    # the partial index matches the selector predicate (planner usability) — the
    # doubled quotes are the format() escape inside the DO block
    assert "idx_documents_regrowth" in body
    assert "facts_committed = 0 OR status = ''partial''" in body
    # the template carries the same shape for NEWLY PROVISIONED tenants (the migration-197
    # lesson: a migration-only change silently skips every new tenant)
    tpl = open(os.path.join(os.path.dirname(__file__), "..",
                            "src/provisioning/templates/user_schema.sql"),
               encoding="utf-8").read()
    assert "reextract_ontology_mark BIGINT  DEFAULT NULL" in tpl
    assert "reextract_attempts      INTEGER NOT NULL DEFAULT 0" in tpl
    assert "idx_documents_regrowth" in tpl


# ── legacy-lane KEEP honor (adversarial round 1, Finding 1) ──────────────────────────
#
# DOC_CHUNK_QUEUE defaults FALSE: the legacy ThreadPool pool is the DEFAULT drain lane,
# and it has no owed filter of its own. The prepass flip's KEEP (done c>0 chunks survive
# in chunk_state) is only real if the legacy lane honours it on a re-mine drain — a kept
# chunk re-read would re-bill the brain for an already-captured fact and re-POST its
# edges (a live-row confirmed_count bump per kept fact per re-mine).

class _DrainCursor:
    """Models drain_pending_documents against ONE modelled documents row (legacy lane):
    capability probes, candidate SELECT, rung-1 claim, the guarded ledger reads, the
    finalize UPDATE and the mark-stamp UPDATE — SQL text captured verbatim."""

    def __init__(self, conn):
        self._conn = conn
        self._row = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql_norm = " ".join(str(sql).split())
        self._conn.statements.append((sql_norm, params))
        self._row = None
        self.rowcount = 0
        C = self._conn
        if "information_schema.columns" in sql_norm:
            self._row = (1,)  # migrated schema: defer clock + chunk_state + remine marks
        elif sql_norm.startswith("SET search_path"):
            pass
        elif "SELECT id FROM documents" in sql_norm:
            self._row = (1,) if C.doc_eligible else None
            C.found = [self._row] if self._row else []
        elif "SET status = 'processing'" in sql_norm:
            self._row = (C.chunks, C.source_ref, C.claim_attempts, None)
        elif "SELECT reextract_attempts FROM documents" in sql_norm:
            self._row = (C.remine_attempts,)
        elif "SELECT chunk_state FROM documents" in sql_norm:
            self._row = (json.dumps(C.chunk_state),)
        elif "count(*) FROM rel_types" in sql_norm:
            self._row = (42,)
        elif "UPDATE documents" in sql_norm and "SET status = %s" in sql_norm:
            # terminal finalize: (status, committed, staged, chunks_failed, ledger, doc)
            C.finalize = {"status": params[0], "committed": params[1],
                          "staged": params[2], "chunks_failed": params[3],
                          "ledger": json.loads(params[4])}
            self.rowcount = 1
        elif "UPDATE documents SET reextract_ontology_mark" in sql_norm:
            C.mark_stamp = params[0]
            self.rowcount = 1
        elif "UPDATE documents" in sql_norm:
            self.rowcount = 1

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row is not None else []


class _DrainConn:
    def __init__(self, chunks, chunk_state, remine_attempts=1, claim_attempts=1,
                 doc_eligible=True, source_ref="doc-ref"):
        self.chunks = chunks
        self.chunk_state = chunk_state
        self.remine_attempts = remine_attempts
        self.claim_attempts = claim_attempts
        self.doc_eligible = doc_eligible
        self.source_ref = source_ref
        self.statements = []
        self.finalize = None
        self.mark_stamp = None
        self.found = []

    def cursor(self):
        return _DrainCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _drive_legacy_drain(monkeypatch, conn, n_chunks=3):
    """Run the REAL drain_pending_documents (legacy lane, DOC_CHUNK_QUEUE off) and return
    the /ingest POST texts + captured finalize."""
    posts = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        posts.append((url, dict(headers or {}), json))
        return _Resp({"committed": 1, "staged": 0})

    monkeypatch.setattr(embedder.httpx, "post", fake_post)
    monkeypatch.setattr(embedder, "_document_chunk_edges",
                        lambda chunk, user_id, backend_url, route, *a, **kw:
                        [{"subject": "s", "rel_type": "r", "object": "o"}])
    monkeypatch.setattr(embedder, "_backend_is_reachable", lambda url: True)
    monkeypatch.setattr(embedder, "release_read_transaction", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "_bh_note_doc_chunks", lambda *a, **k: None)
    n = embedder.drain_pending_documents(
        conn, "http://backend", user_id="u" * 32, schema_name="faultline_x",
        statement_route="rewrite")
    return n, posts


def test_legacy_lane_honors_remine_keep(monkeypatch):
    """A re-mine drain on the DEFAULT (legacy) lane re-reads ONLY the owed chunks: the
    done c>0 chunk the prepass kept is skipped, its captured counts ride the finalize
    tally, and the written ledger still carries its entry. (The fixture models the
    POST-FLIP ledger the drain actually reads: the zero-edge/failed entries were already
    dropped by the prepass — chunks 1 and 2 have no entries, i.e. owed.)"""
    state = {"0": {"s": "done", "c": 4, "g": 1}}         # KEPT by the prepass
    conn = _DrainConn(["c0", "c1", "c2"], state, remine_attempts=1)
    monkeypatch.setattr(embedder, "DOC_CHUNK_QUEUE", False)
    n, posts = _drive_legacy_drain(monkeypatch, conn)
    assert n == 1
    ingest_texts = [b["text"] for u, _h, b in posts if u.endswith("/ingest")]
    assert sorted(ingest_texts) == ["c1", "c2"]          # the KEPT chunk 0 never POSTed
    # the kept chunk's captured counts ride the finalize tally (no understatement)
    assert conn.finalize["committed"] == 4 + 2           # kept c=4 + two new commits
    # the written ledger still carries the kept entry (the finalize write does not wipe it)
    assert conn.finalize["ledger"]["0"] == {"s": "done", "c": 4, "g": 1}
    assert set(conn.finalize["ledger"]) == {"0", "1", "2"}
    # and the drain was a marked replay
    ingest_headers = [h for u, h, _b in posts if u.endswith("/ingest")]
    assert all(ingest_transport.REPLAY_MARKER_HEADER in h for h in ingest_headers)


def test_legacy_lane_unchanged_without_replay(monkeypatch):
    """A NON-replay drain (reextract_attempts = 0) is byte-for-byte the shipped legacy
    behaviour: every chunk re-read, no ledger read, no marker."""
    state = {"0": {"s": "done", "c": 4, "g": 1}}
    conn = _DrainConn(["c0", "c1"], state, remine_attempts=0)
    monkeypatch.setattr(embedder, "DOC_CHUNK_QUEUE", False)
    n, posts = _drive_legacy_drain(monkeypatch, conn)
    assert n == 1
    ingest_texts = [b["text"] for u, _h, b in posts if u.endswith("/ingest")]
    assert sorted(ingest_texts) == ["c0", "c1"]          # both chunks re-read, as shipped
    # ROUND 5 (DOC_TEXT_LANE_BACKPRESSURE): a non-replay drain now READS the
    # ledger (the per-pass chunk budget's owed filter needs it), but an
    # UNSTAMPED done entry — the re-mine prepass keep shape above — is still
    # re-read, so the keep-honor doctrine is preserved. Only entries the budget
    # lane itself terminalized (bp-stamped) are skipped; see
    # tests/test_doc_text_lane_backpressure.py for both pins.
    assert any("SELECT chunk_state FROM documents" in s for s, _p in conn.statements)
    for u, h, _b in posts:
        if u.endswith("/ingest"):
            assert ingest_transport.REPLAY_MARKER_HEADER not in h


def test_episodic_append_carries_deterministic_turn_key(monkeypatch):
    """Adversarial round 1, Finding 2: the chunk's episodic retention is IDEMPOTENT per
    (doc, chunk) — a re-mine ≥24h later must dedupe against the original row via
    turn_key (the 30s keyless content window cannot see that repeat), so a re-mine never
    re-seeds duplicate rows into the episodic backfill."""
    _res, posts = _drive_chunk(monkeypatch, replay=True)
    appends = [b for u, _h, b in posts if u.endswith("/episodic/append")]
    assert len(appends) == 1
    assert appends[0]["turn_key"] == "doc:7:chunk:0"


def test_episodic_append_turn_key_present_on_first_drain_too(monkeypatch):
    """The key rides EVERY append (the dedup must exist before the first re-send): a
    first drain inserts exactly as before — same row, now addressable."""
    _res, posts = _drive_chunk(monkeypatch, replay=False)
    appends = [b for u, _h, b in posts if u.endswith("/episodic/append")]
    assert len(appends) == 1
    assert appends[0]["turn_key"] == "doc:7:chunk:0"
    # a first drain still carries NO replay marker on /ingest
    for u, h, _b in posts:
        if u.endswith("/ingest"):
            assert ingest_transport.REPLAY_MARKER_HEADER not in h
