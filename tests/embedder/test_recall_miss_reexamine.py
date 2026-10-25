"""R7 — recall-miss markers: SQL semantics + growth-drain pickup (embedder side).

Pins (plan DEV/BUGS/plan-100pct-addressability-20261024.md §R7):

  (d) marker dedup/rate-limit: the same (document, chunk) re-queried inside
      ONE HOUR writes exactly ONE row — while pending (ON CONFLICT), AND after
      a fast drain completed it (the hourly NOT EXISTS). After the window the
      marker re-arms.
  (e) the growth drain picks the marker up PRIORITY-FIRST and
      `_recall_miss_reexamine_chunk` re-ows EXACTLY the named chunk of a
      terminal document (chunk-scoped R2 re-mine): the flip is atomic on
      still-terminal, bumps reextract_attempts (which is what arms the
      X-FL-Deferred-Replay retired-value freeze on the drain's re-send), and
      leaves every OTHER chunk's ledger entry untouched.
  (g) markers respect GROWTH_QUEUE_MAX (oldest pending evicted) — the same
      cap rule as every other queue write.

Safety shapes: attempts ceiling (no infinite re-examination — a chunk at the
R2 re-mine bound is retired, not flipped), already-pending document (the
normal drain owns it), missing document, pre-298 schema (self-disable, retire,
no transaction poisoning), foreign/malformed target ids.

DB-backed against POSTGRES_DSN (repo convention: requires_postgres skips
honestly without one) — the partial-unique and hourly-guard semantics are
pinned against the REAL database, not a mock.
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

from src.re_embedder import growth_work_queue as GWQ

pytestmark = pytest.mark.usefixtures("requires_postgres")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO_ROOT / "src" / "provisioning" / "templates" / "user_schema.sql"


def _queue_ddl(schema_name: str) -> str:
    text = _TEMPLATE.read_text()
    start = text.index("CREATE TABLE IF NOT EXISTS {schema_name}.growth_work_queue")
    last = text.rindex("WHERE completed_at IS NULL;", start)
    return text[start:last + len("WHERE completed_at IS NULL;")].replace(
        "{schema_name}", schema_name)


@pytest.fixture()
def tenant(requires_postgres):
    """A fresh schema with the queue table AND a minimal documents table shaped
    for the chunk re-examination flip (the migration-298/205 columns the
    runner's SQL names)."""
    schema = f"faultline_r7_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(requires_postgres)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(_queue_ddl(schema))
            cur.execute(f"""
                CREATE TABLE {schema}.documents (
                    id BIGINT PRIMARY KEY,
                    user_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    chunk_state JSONB NOT NULL DEFAULT '{{}}',
                    started_at TIMESTAMPTZ,
                    ready_at TIMESTAMPTZ,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    brain_deferred_since TIMESTAMPTZ,
                    reextract_attempts INTEGER NOT NULL DEFAULT 0,
                    reextract_ontology_mark INTEGER,
                    facts_committed INTEGER NOT NULL DEFAULT 0
                )
            """)
        conn.commit()
        yield {"schema": schema, "dsn": requires_postgres,
               "user": "11111111-2222-4333-8444-555555555555"}
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()


@pytest.fixture()
def bound_conn(tenant):
    conn = psycopg2.connect(tenant["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {tenant['schema']}")
        conn.commit()
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_state():
    GWQ.reset_recorder_hygiene()
    from src.re_embedder import embedder
    embedder._remine_columns_missing_schemas.clear()  # module state — no cross-test leak
    yield
    GWQ.reset_recorder_hygiene()
    embedder._remine_columns_missing_schemas.clear()


def _seed_doc(conn, user, doc_id=1, status="ready", chunk_state=None,
              reextract_attempts=0):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (id, user_id, status, chunk_state,"
            " reextract_attempts, ready_at)"
            " VALUES (%s, %s, %s, %s::jsonb, %s, now())",
            (doc_id, user, status, json.dumps(chunk_state or {}),
             reextract_attempts))
    conn.commit()


def _pending_rows(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation_type, target_id, priority FROM growth_work_queue"
            " WHERE completed_at IS NULL ORDER BY priority, created_at, id")
        return cur.fetchall()


# ── pin (d): hourly rate limit + pending dedup — REAL SQL ─────────────────────

def test_marker_dedup_while_pending(tenant, bound_conn):
    """Same chunk re-queried N times while the marker is PENDING → ONE row.
    (The SQL guard is the pin — the 55s connection throttle is bypassed so each
    call reaches the database, as a second WORKER would.)"""
    def _force(doc, chunk):
        GWQ._last_record.clear()   # cross the throttle; exercise the SQL dedup
        return GWQ.record_recall_miss(tenant["schema"], doc, chunk)
    assert _force(7, 3) is True
    for _ in range(3):
        again = _force(7, 3)
    assert again is True   # pending row still exists → the probe says so
    rows = _pending_rows(bound_conn)
    assert rows == [("recall_miss_reexamine", f"{tenant['schema']}:7:3", 2)]


def test_marker_hourly_guard_survives_a_fast_drain(tenant, bound_conn):
    """The dedup must hold EVEN AFTER the row completes — a fast drain inside
    the hour must not re-arm the marker on the next identical miss."""
    GWQ._last_record.clear()
    assert GWQ.record_recall_miss(tenant["schema"], 7, 3) is True
    with bound_conn.cursor() as cur:
        cur.execute("SELECT id FROM growth_work_queue WHERE completed_at IS NULL")
        GWQ.mark_completed(bound_conn, [cur.fetchone()[0]])
    # same chunk misses again inside the hour → NOTHING new (hourly NOT EXISTS)
    GWQ._last_record.clear()   # cross the throttle — the SQL guard is the pin
    assert GWQ.record_recall_miss(tenant["schema"], 7, 3) is False
    assert _pending_rows(bound_conn) == []
    with bound_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM growth_work_queue")
        assert cur.fetchone()[0] == 1   # the completed row, no second row


def test_marker_rearms_after_the_hour_window(tenant, bound_conn):
    """After the window the marker re-arms — the rate limit is hourly, not forever."""
    GWQ._last_record.clear()
    assert GWQ.record_recall_miss(tenant["schema"], 7, 3) is True
    with bound_conn.cursor() as cur:
        cur.execute("UPDATE growth_work_queue"
                    " SET created_at = created_at - INTERVAL '2 hours'")
    bound_conn.commit()
    GWQ._last_record.clear()   # cross the throttle — the window is the pin
    assert GWQ.record_recall_miss(tenant["schema"], 7, 3) is True
    assert len(_pending_rows(bound_conn)) == 1  # a NEW pending row exists


def test_marker_connection_throttle_skips_the_connect(tenant, monkeypatch):
    """The 55s process throttle: a re-queried miss inside the window costs NO
    database connection (the SQL guard is authoritative; this only bounds
    churn) — prove no connect by making psycopg2.connect a tripwire."""
    import psycopg2
    GWQ.reset_recorder_hygiene()
    assert GWQ.record_recall_miss(tenant["schema"], 9, 1) is True

    def _tripwire(*a, **k):
        raise AssertionError("throttled refusal must not open a connection")

    monkeypatch.setattr(psycopg2, "connect", _tripwire)
    assert GWQ.record_recall_miss(tenant["schema"], 9, 1) is False
    GWQ.reset_recorder_hygiene()


def test_marker_distinct_chunks_are_distinct_markers(tenant, bound_conn):
    """The limit is per (document, chunk) — sibling chunks of one miss query
    each earn their own marker."""
    for chunk in (3, 5):
        assert GWQ.record_recall_miss(tenant["schema"], 7, chunk) is True
    rows = sorted(t for _op, t, _p in _pending_rows(bound_conn))
    assert rows == sorted([f"{tenant['schema']}:7:3", f"{tenant['schema']}:7:5"])


# ── pin (g): GROWTH_QUEUE_MAX bounds the markers ─────────────────────────────

def test_markers_respect_growth_queue_max(tenant, monkeypatch):
    """The marker write path evicts the OLDEST pending rows beyond the cap —
    the same bound as every other queue write (one rule, not two)."""
    monkeypatch.setenv("GROWTH_QUEUE_MAX", "3")
    for i in range(5):
        assert GWQ.record_recall_miss(tenant["schema"], 42, i) is True
    conn = psycopg2.connect(tenant["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {tenant['schema']}")
            cur.execute("SELECT target_id FROM growth_work_queue"
                        " WHERE completed_at IS NULL ORDER BY created_at, id")
            targets = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    assert targets == [f"{tenant['schema']}:42:{i}" for i in (2, 3, 4)]


# ── pin (e): the growth drain picks the marker up and re-ows ONE chunk ────────

def _marker(tenant, doc_id=1, chunk=2):
    return ("recall_miss_reexamine", f"{tenant['schema']}:{doc_id}:{chunk}", 2)


def test_drain_runs_recall_miss_marker_priority_first_and_reows_the_chunk(
        tenant, bound_conn):
    """PIN (e): drain → _growth_work_reattempt → _recall_miss_reexamine_chunk:
    the marker completes, the doc flips terminal→pending with ONLY the named
    chunk re-owed, reextract_attempts bumps (the replay arm), and a priority-5
    sibling op is attempted AFTER the priority-2 marker."""
    from src.re_embedder import embedder

    _seed_doc(bound_conn, tenant["user"], doc_id=1, status="ready", chunk_state={
        "0": {"s": "done", "c": 3},   # captured — MUST be kept
        "2": {"s": "done", "c": 0},   # the missed chunk — MUST be re-owed
        "5": {"s": "failed"},         # not our business here — MUST be kept
    })
    with bound_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
            " VALUES (%s, %s, 2), ('enrichment', 'ontology_eval', 5)",
            _marker(tenant)[:2])
    bound_conn.commit()

    attempted = []

    def runner(op, tgt):
        attempted.append((op, tgt))
        if op == "recall_miss_reexamine":
            return embedder._growth_work_reattempt(
                op, tgt, bound_conn, tenant["user"], tenant["schema"],
                "http://brain", tenant["dsn"], "http://qdrant")
        return True   # the enrichment sibling resolves cleanly

    res = GWQ.drain(bound_conn, runner, schema_name=tenant["schema"])

    assert attempted[0] == ("recall_miss_reexamine", _marker(tenant)[1])  # priority 2 first
    assert res["drained"] == 2 and res["remaining"] == 0
    with bound_conn.cursor() as cur:
        cur.execute("SELECT status, chunk_state, reextract_attempts, attempts"
                    " FROM documents WHERE id = 1")
        status, state, rex_attempts, attempts = cur.fetchone()
    assert status == "pending"
    assert attempts == 0                       # deliberate fresh re-read (R2 shape)
    assert rex_attempts == 1                   # arms X-FL-Deferred-Replay on re-send
    state = json.loads(state) if isinstance(state, str) else state
    assert set(state) == {"0", "5"}            # ONLY chunk 2's entry dropped
    assert state["0"] == {"s": "done", "c": 3}
    # the replay linkage the retired-value freeze rides on: attempts > 0 → the
    # document drain re-sends under the marker (embedder _doc_replay rule)
    assert embedder._read_remine_attempts(bound_conn, 1, tenant["schema"]) == 1


def test_reexamine_nonterminal_splits_by_owed_state(tenant, bound_conn):
    """Non-terminal document (a concurrent claim / the drain owns it — its
    finalize flushes the ledger IT built, so a chunk_state write here would be
    lost): a DONE **or FAILED** entry is unmet work → DEFER (False, retry when
    terminal — the queue lane's owed filter never re-reads either terminal
    state); an entry that is neither (absent/in-flight) is already owed →
    retire (True). Either way the document row is untouched."""
    from src.re_embedder import embedder
    # done entry → DEFER (round-1 critic GAP: retiring here would silently
    # complete a sibling marker without re-owing its chunk)
    _seed_doc(bound_conn, tenant["user"], doc_id=1, status="processing",
              chunk_state={"2": {"s": "done", "c": 0}})
    ok = embedder._recall_miss_reexamine_chunk(
        _marker(tenant)[1], bound_conn, tenant["user"], tenant["schema"])
    assert ok is False
    with bound_conn.cursor() as cur:
        cur.execute("SELECT status, chunk_state FROM documents WHERE id = 1")
        status, state = cur.fetchone()
    assert status == "processing"              # untouched — the drain owns it now
    state = json.loads(state) if isinstance(state, str) else state
    assert state == {"2": {"s": "done", "c": 0}}
    # failed entry → ALSO DEFER (round-2 critic GAP: the queue lane's owed
    # filter excludes 'failed', so retiring would complete without effect)
    _seed_doc(bound_conn, tenant["user"], doc_id=2, status="pending",
              chunk_state={"0": {"s": "failed"}})
    assert embedder._recall_miss_reexamine_chunk(
        f"{tenant['schema']}:2:0", bound_conn, tenant["user"], tenant["schema"]) is False
    # absent entry → already owed → retire
    _seed_doc(bound_conn, tenant["user"], doc_id=3, status="pending",
              chunk_state={"0": {"s": "done", "c": 1}})
    assert embedder._recall_miss_reexamine_chunk(
        f"{tenant['schema']}:3:5", bound_conn, tenant["user"], tenant["schema"]) is True


def test_sibling_markers_converge_across_cycles(tenant, bound_conn):
    """ROUND-1 CRITIC GAP PIN: ONE miss query names chunks 2 AND 5 of the SAME
    document. Drain cycle 1: marker(2) flips the doc (chunk 2 re-owed);
    marker(5) finds the doc non-terminal with a DONE entry → DEFERS (stays
    pending — never stamped completed without effect). The document drain
    re-reads chunk 2 and lands terminal again. Drain cycle 2: marker(5) now
    flips and re-owes chunk 5. BOTH chunks re-examined, no silent completion."""
    from src.re_embedder import embedder

    _seed_doc(bound_conn, tenant["user"], doc_id=1, status="ready", chunk_state={
        "2": {"s": "done", "c": 0},
        "5": {"s": "done", "c": 1},   # committed something once — still kept
    })
    with bound_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
            " VALUES (%s, %s, 2), (%s, %s, 2)",
            ("recall_miss_reexamine", f"{tenant['schema']}:1:2",
             "recall_miss_reexamine", f"{tenant['schema']}:1:5"))
    bound_conn.commit()

    def _drain():
        return GWQ.drain(
            bound_conn,
            runner=lambda op, tgt: embedder._growth_work_reattempt(
                op, tgt, bound_conn, tenant["user"], tenant["schema"],
                "http://brain", tenant["dsn"], "http://qdrant"),
            schema_name=tenant["schema"])

    # ── cycle 1: marker(2) flips; marker(5) defers ──
    res1 = _drain()
    assert res1["drained"] == 1, res1
    assert res1["by_type"].get("recall_miss_reexamine") == 1   # marker(5) pending
    with bound_conn.cursor() as cur:
        cur.execute("SELECT status, chunk_state, reextract_attempts"
                    " FROM documents WHERE id = 1")
        status, state, rex = cur.fetchone()
    assert status == "pending" and rex == 1
    state = json.loads(state) if isinstance(state, str) else state
    assert set(state) == {"5"}                 # only chunk 2 re-owed so far

    # ── the document drain does its work and lands terminal (chunk 2 re-read,
    #    still zero-edge; chunk 5's entry untouched) ──
    with bound_conn.cursor() as cur:
        cur.execute("UPDATE documents SET status='ready', ready_at=now(),"
                    " chunk_state = %s::jsonb WHERE id = 1",
                    (json.dumps({"2": {"s": "done", "c": 0},
                                 "5": {"s": "done", "c": 1}}),))
    bound_conn.commit()

    # ── cycle 2: marker(5) now flips and re-owes chunk 5 ──
    res2 = _drain()
    assert res2["drained"] == 1 and res2["remaining"] == 0, res2
    with bound_conn.cursor() as cur:
        cur.execute("SELECT status, chunk_state, reextract_attempts"
                    " FROM documents WHERE id = 1")
        status, state, rex = cur.fetchone()
    assert status == "pending" and rex == 2
    state = json.loads(state) if isinstance(state, str) else state
    assert set(state) == {"2"}                 # NOW chunk 5 is re-owed too
    # both drain cycles left the queue fully consumed — no marker completed
    # without its chunk being re-owed
    assert _pending_rows(bound_conn) == []


def test_reexamine_respects_the_attempts_ceiling(tenant, bound_conn):
    """A chunk at the R2 re-mine attempts bound is RETIRED, not flipped — a
    user hammering the same unanswerable question cannot re-trigger extraction
    forever (the marker's own bounds: hourly limit + this ceiling)."""
    from src.re_embedder import embedder
    _seed_doc(bound_conn, tenant["user"], doc_id=1, status="ready",
              chunk_state={"2": {"s": "done", "c": 0}},
              reextract_attempts=embedder._DOC_REMINE_MAX_ATTEMPTS)
    ok = embedder._recall_miss_reexamine_chunk(
        _marker(tenant)[1], bound_conn, tenant["user"], tenant["schema"])
    assert ok is True                            # resolved (retired), not retried
    with bound_conn.cursor() as cur:
        cur.execute("SELECT status, reextract_attempts FROM documents WHERE id = 1")
        status, rex = cur.fetchone()
    assert status == "ready" and rex == embedder._DOC_REMINE_MAX_ATTEMPTS


def test_reexamine_retires_on_missing_document_and_bad_targets(
        tenant, bound_conn):
    from src.re_embedder import embedder
    s, u = tenant["schema"], tenant["user"]
    # document gone
    assert embedder._recall_miss_reexamine_chunk(f"{s}:99:0", bound_conn, u, s) is True
    # foreign schema segment / malformed ids / empty
    assert embedder._recall_miss_reexamine_chunk(
        f"faultline_other:1:2", bound_conn, u, s) is True
    assert embedder._recall_miss_reexamine_chunk("junk", bound_conn, u, s) is True
    assert embedder._recall_miss_reexamine_chunk("", bound_conn, u, s) is True
    assert embedder._recall_miss_reexamine_chunk(f"{s}:x:y", bound_conn, u, s) is True
    assert embedder._recall_miss_reexamine_chunk(f"{s}:1:-3", bound_conn, u, s) is True


def test_reexamine_self_disables_on_pre298_schema(tenant, bound_conn):
    """No reextract_attempts column → the breadcrumb RETIRES (never wedges,
    never poisons the shared drain transaction) and the lane self-disables for
    the tenant — the same rule as remine_documents_prepass."""
    from src.re_embedder import embedder
    with bound_conn.cursor() as cur:
        cur.execute("ALTER TABLE documents DROP COLUMN reextract_attempts")
    bound_conn.commit()
    try:
        ok = embedder._recall_miss_reexamine_chunk(
            _marker(tenant)[1], bound_conn, tenant["user"], tenant["schema"])
        assert ok is True
        assert tenant["schema"] in embedder._remine_columns_missing_schemas
        # the shared connection is still USABLE (no aborted transaction)
        with bound_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM growth_work_queue")
            assert cur.fetchone() is not None
    finally:
        embedder._remine_columns_missing_schemas.discard(tenant["schema"])


def test_reexamine_missing_chunk_entry_still_flips(tenant, bound_conn):
    """A chunk with NO ledger entry (never reached — a 'partial' doc) is already
    owed; the flip still happens so the normal drain collects it."""
    from src.re_embedder import embedder
    _seed_doc(bound_conn, tenant["user"], doc_id=1, status="partial", chunk_state={
        "0": {"s": "done", "c": 2}})
    assert embedder._recall_miss_reexamine_chunk(
        _marker(tenant)[1], bound_conn, tenant["user"], tenant["schema"]) is True
    with bound_conn.cursor() as cur:
        cur.execute("SELECT status, chunk_state FROM documents WHERE id = 1")
        status, state = cur.fetchone()
    assert status == "pending"
    state = json.loads(state) if isinstance(state, str) else state
    assert state == {"0": {"s": "done", "c": 2}}   # pass-through, nothing dropped


# ── registry: the operation is a first-class queue citizen ────────────────────

def test_recall_miss_operation_registered_with_priority_2():
    assert GWQ.RECALL_MISS_OPERATION in GWQ.OPERATION_TYPES
    assert GWQ.OPERATION_PRIORITY[GWQ.RECALL_MISS_OPERATION] == 2
    # target shape: '{schema}:{document_id}:{chunk_index}'
    assert GWQ.recall_miss_target_id("faultline_x", 7, 3) == "faultline_x:7:3"
