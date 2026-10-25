"""R9 — deterministic-first uplift on recall-miss re-examination (owner ruling:
GLiNER2 + spaCy PRIMARY, BYO brain FALLBACK).

Pins (gauntlet R9, DEV/DESIGN-cba-tiered-recall-architecture.md §"deterministic-first
uplift gap"):

  (a) chunk with deterministic-extractable content → the spine+GLiNER2 edges are
      committed through the NORMAL /ingest door (source="document", replay
      markers ON) with ZERO brain calls — a tripwire on EVERY httpx.post proves
      the only wire traffic is the /ingest commit (no /harvest-spans harvest
      turn, no /extract/rewrite), and a tripwire on _document_chunk_edges proves
      the extraction lane never fired. The document is NOT flipped (no brain
      re-examination owed) and the marker completes.
  (b) zero deterministic edges on REAL content → brain-deferred: a
      growth_work_queue row with operation_type='recall_miss_brain' (priority 5)
      is pending, the spawning marker is completed, no flip, no /ingest POST.
  (c) trivially short / header-only fragmentary chunk → completed honestly
      (nothing to extract): no brain row, no POST, no flip.
  (d) spaCy/linguistics layer unavailable → falls through to the R7 brain-first
      flip (current behavior preserved, logged); same when no commit door
      (backend_url unset — the legacy/test caller shape).
  (e) replay markers on the deterministic path: the /ingest POST carries
      X-FL-Deferred-Replay + the reextract lane header — the retired-value
      freeze engages on every re-send (R2), and the idempotency body stays
      byte-stable so a replayed marker dedupes.
  (f) the two-phase chain end-to-end through GWQ.drain: cycle 1 runs the
      deterministic phase (zero edges → brain row written), cycle 2 runs the
      brain phase (the R7 flip: reextract_attempts+1 arms the drain's replay
      marker) — priority order reexamine(2) before brain(5), and the brain row
      is never attempted in the cycle that created it.
  (g) /ingest transport failure → the marker stays pending (retry), no brain
      row; a 4xx deterministic refusal → brain-deferred; frozen store
      (ingest_disabled) → stays pending.
  (h) the REAL deterministic layer (no monkeypatch of the deriver, GLiNER2 seam
      off → spine-only): a contentful chunk yields ≥1 /ingest-shaped edge with
      ZERO network (httpx tripwire wired to raise) — the deterministic-first
      promise is real extraction, not a stub.
  (i) the typed-doc mirror's drift canaries: machine-value veto,
      coordinated-surface split, longest-first overlap — the three behaviours
      pinned against main.py::_build_typed_doc's documented fixtures.

The R7 escalation/marker/dedup chain itself is pinned unchanged in
tests/embedder/test_recall_miss_reexamine.py + tests/test_recall_miss_escalation.py
(those call the reattempt WITHOUT a commit door — the byte-for-byte R7 path).

DB-backed against POSTGRES_DSN (repo convention: requires_postgres skips
honestly without one).
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

from src.re_embedder import embedder
from src.re_embedder import growth_work_queue as GWQ
from src.api import ingest_transport

pytestmark = pytest.mark.usefixtures("requires_postgres")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO_ROOT / "src" / "provisioning" / "templates" / "user_schema.sql"

_DSN_BACKEND = "http://backend.test"


def _queue_ddl(schema_name: str) -> str:
    text = _TEMPLATE.read_text()
    start = text.index("CREATE TABLE IF NOT EXISTS {schema_name}.growth_work_queue")
    last = text.rindex("WHERE completed_at IS NULL;", start)
    return text[start:last + len("WHERE completed_at IS NULL;")].replace(
        "{schema_name}", schema_name)


@pytest.fixture()
def tenant(requires_postgres):
    """Fresh schema with the queue table AND a documents table shaped for the
    deterministic uplift (the migration-298/205 columns the R7 runner names,
    PLUS the retained `chunks` JSONB + `source_ref` the uplift door reads)."""
    schema = f"faultline_r9_{uuid.uuid4().hex[:12]}"
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
                    chunks JSONB NOT NULL DEFAULT '[]'::jsonb,
                    source_ref TEXT,
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
def _clean_state(monkeypatch):
    GWQ.reset_recorder_hygiene()
    embedder._remine_columns_missing_schemas.clear()  # module state — no cross-test leak
    # GLiNER2 loader state: never let one test's load attempt/backoff leak.
    monkeypatch.setattr(embedder, "_gliner2_model_cache", None)
    monkeypatch.setattr(embedder, "_gliner2_failed_until", 0.0)
    GWQ.reset_recorder_hygiene()
    embedder._remine_columns_missing_schemas.clear()
    yield


CHUNK_TEXT = ("Marcus owns two vintage synthesizers. "
              "His sister Nora works at the hospital in Millbrook.")


def _seed_doc(conn, user, doc_id=1, status="ready", chunk_state=None,
              reextract_attempts=0, chunks=None, source_ref="doc.pdf"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (id, user_id, status, chunk_state, chunks,"
            " source_ref, reextract_attempts, ready_at)"
            " VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, now())",
            (doc_id, user, status, json.dumps(chunk_state or {"0": {"s": "done"}}),
             json.dumps(chunks if chunks is not None else [CHUNK_TEXT]),
             source_ref, reextract_attempts))
    conn.commit()


def _seed_marker(conn, schema, doc_id=1, chunk_idx=0, op="recall_miss_reexamine",
                 priority=2):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
            " VALUES (%s, %s, %s)",
            (op, GWQ.recall_miss_target_id(schema, doc_id, chunk_idx), priority))
    conn.commit()


def _queue_rows(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation_type, target_id, priority, completed_at"
            " FROM growth_work_queue ORDER BY priority, created_at, id")
        return cur.fetchall()


def _doc_state(conn, doc_id=1):
    with conn.cursor() as cur:
        cur.execute("SELECT status, reextract_attempts FROM documents WHERE id = %s",
                    (doc_id,))
        return cur.fetchone()


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {"status": "success", "committed": 2, "staged": 0}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture()
def wire(monkeypatch):
    """The tripwire: records EVERY httpx.post the embedder makes. The brain-call
    assertions read this — a /harvest-spans or /extract/rewrite POST is a BRAIN
    call; the deterministic path must show exactly one /ingest and nothing else."""
    calls = []

    def _fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return _FakeResp()

    monkeypatch.setattr(embedder.httpx, "post", _fake_post)

    def _brain_calls():
        return [c for c in calls if any(
            s in c["url"] for s in ("/harvest-spans", "/extract/rewrite"))]

    return {"calls": calls, "brain_calls": _brain_calls,
            "ingest_calls": lambda: [c for c in calls if c["url"].endswith("/ingest")]}


@pytest.fixture()
def no_extract_lane(monkeypatch):
    """REGRESSION tripwire (round-3 critic: neither the uplift NOR the R7 flip
    calls it today — the doc drain extracts, not this lane): if a future edit
    ever routes the recall-miss path through _document_chunk_edges (the door to
    /harvest-spans + /extract/rewrite), these pins fail loudly — the
    deterministic path must not borrow the brain-bearing extraction lane."""
    def _must_not_run(*a, **k):
        raise AssertionError("_document_chunk_edges (brain extraction lane) fired")
    # raising=False: this lineage's embedder carries no _document_chunk_edges at
    # all (no brain-bearing document extraction lane) — the attribute is planted
    # as the tripwire and the REAL zero-brain enforcement is the wire() httpx
    # recorder above (no /harvest-spans, no /extract/rewrite).
    monkeypatch.setattr(embedder, "_document_chunk_edges", _must_not_run,
                        raising=False)


def _reattempt(bound_conn, tenant, op="recall_miss_reexamine",
               backend=_DSN_BACKEND, target=None):
    from src.re_embedder.embedder import _growth_work_reattempt
    schema = tenant["schema"]
    target = target or GWQ.recall_miss_target_id(schema, 1, 0)
    return _growth_work_reattempt(
        op, target, bound_conn, tenant["user"], schema,
        "http://brain", tenant["dsn"], "http://qdrant", backend_url=backend)


# ── pin (a)+(e): deterministic edges → /ingest commit, zero brain ─────────────

def test_deterministic_edges_commit_through_ingest_with_zero_brain_calls(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])

    _det_edges = [{"subject": "marcus", "rel_type": "works_for",
                   "object": "hospital", "confidence": 0.8,
                   "fact_provenance": "user_stated"}]
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: list(_det_edges))

    assert _reattempt(bound_conn, tenant) is True

    # ZERO brain calls: no /harvest-spans harvest turn, no /extract/rewrite —
    # and the ONLY wire traffic is the single /ingest commit.
    assert wire["brain_calls"]() == []
    ingest = wire["ingest_calls"]()
    assert len(ingest) == 1 and len(wire["calls"]) == 1

    body = ingest[0]["kwargs"]["json"]
    assert body["edges"] == _det_edges
    assert body["source"] == "document"          # provenance router → llm_inferred/staged B
    assert body["source_ref"] == "doc.pdf"       # the row's own citation
    assert body["user_id"] == tenant["user"]
    assert CHUNK_TEXT[:30] in body["text"]

    # (e) replay markers: the retired-value freeze engages on the deterministic
    # re-send (a recall-miss re-send is a re-attempt of already-attempted content).
    headers = ingest[0]["kwargs"]["headers"]
    assert headers.get(ingest_transport.REPLAY_MARKER_HEADER) \
        == ingest_transport.REPLAY_MARKER_VALUE
    assert headers.get(ingest_transport.REPLAY_LANE_HEADER) \
        == ingest_transport.REPLAY_LANE_REEXTRACT

    # No flip, no drain involvement, no brain row; the runner's True verdict is
    # what the drain translates into completed_at.
    assert _doc_state(bound_conn) == ("ready", 0)
    rows = _queue_rows(bound_conn)
    assert len(rows) == 1 and rows[0][0] == "recall_miss_reexamine"
    assert not [r for r in rows if r[0] == "recall_miss_brain"]


def test_deterministic_commit_body_is_byte_stable(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    """The second marker for the same chunk posts a BYTE-IDENTICAL body — which
    is what the backend's idempotency key hashes (server-side dedupe is
    conditional on the idempotency cache, exactly as for the house doc lane;
    this pin guarantees the CLIENT half of that contract)."""
    _seed_doc(bound_conn, tenant["user"])
    _det_edges = [{"subject": "sister", "rel_type": "works_for",
                   "object": "hospital", "confidence": 0.8}]
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: list(_det_edges))
    assert _reattempt(bound_conn, tenant) is True
    _seed_marker(bound_conn, tenant["schema"])    # a LATER miss on the same chunk
    assert _reattempt(bound_conn, tenant) is True
    bodies = [c["kwargs"]["json"] for c in wire["ingest_calls"]()]
    assert len(bodies) == 2 and bodies[0] == bodies[1]


# ── pin (b): zero edges on real content → brain-deferred ──────────────────────

def test_zero_edges_real_content_defers_to_brain_lane(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", lambda chunk: [])

    assert _reattempt(bound_conn, tenant) is True

    # The brain row: pending, priority 5 (below every deterministic phase).
    rows = _queue_rows(bound_conn)
    brain = [r for r in rows if r[0] == "recall_miss_brain"]
    assert len(brain) == 1
    assert brain[0][2] == 5 and brain[0][3] is None        # pending, prio 5
    assert brain[0][1] == GWQ.recall_miss_target_id(tenant["schema"], 1, 0)
    # Nothing was committed; no flip; the spawning marker's True verdict is the
    # drain's completion signal.
    assert wire["calls"] == []                              # zero wire traffic at all
    assert _doc_state(bound_conn) == ("ready", 0)


def test_brain_defer_row_dedupes_on_conflict(
        tenant, bound_conn, monkeypatch):
    """A duplicate pending brain row for the same target is collapsed by the
    partial unique (the ON CONFLICT must name the identical COALESCE form)."""
    _seed_doc(bound_conn, tenant["user"])
    target = GWQ.recall_miss_target_id(tenant["schema"], 1, 0)
    assert GWQ.record_brain_defer_on_conn(bound_conn, tenant["schema"], target) is True
    assert GWQ.record_brain_defer_on_conn(bound_conn, tenant["schema"], target) is False
    rows = [r for r in _queue_rows(bound_conn) if r[0] == "recall_miss_brain"]
    assert len(rows) == 1


# ── pin (c): trivial/fragmentary chunk → honest no-op ─────────────────────────

@pytest.mark.parametrize("chunks,why", [
    (["[Date: 2026-03-22 (Mon) 17:50]"], "header-only scaffolding"),
    ([""], "empty chunk"),
    (["   "], "whitespace chunk"),
])
def test_trivial_chunk_completes_without_brain_or_defer(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch, chunks, why):
    _seed_doc(bound_conn, tenant["user"], chunks=chunks)
    _seed_marker(bound_conn, tenant["schema"])

    def _must_extract(chunk):
        raise AssertionError(f"extraction ran on a {why}")
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", _must_extract)

    assert _reattempt(bound_conn, tenant) is True
    assert wire["calls"] == []
    assert _queue_rows(bound_conn)[0][3] is None            # drain stamps completion
    assert _doc_state(bound_conn) == ("ready", 0)           # no flip


def test_chunk_index_past_retained_chunks_retires(tenant, bound_conn, wire, monkeypatch):
    _seed_doc(bound_conn, tenant["user"], chunks=[CHUNK_TEXT])
    _seed_marker(bound_conn, tenant["schema"], chunk_idx=7)
    assert _reattempt(
        bound_conn, tenant, target=GWQ.recall_miss_target_id(tenant["schema"], 1, 7)
    ) is True
    assert wire["calls"] == []


# ── pin (d): layer unavailable → brain-first R7 path ──────────────────────────

def test_spacy_unavailable_falls_through_to_brain_first_flip(
        tenant, bound_conn, wire, monkeypatch):
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", lambda chunk: None)

    assert _reattempt(bound_conn, tenant) is True

    # The R7 flip verbatim: document re-owed to the normal drain under the
    # replay marker (reextract_attempts+1), chunk_state entry dropped.
    status, rex = _doc_state(bound_conn)
    assert status == "pending" and rex == 1
    with bound_conn.cursor() as cur:
        cur.execute("SELECT chunk_state FROM documents WHERE id = 1")
        state = cur.fetchone()[0]
    state = json.loads(state) if isinstance(state, str) else state
    assert "0" not in state
    assert wire["calls"] == []                              # no /ingest — the DRAIN commits


def test_no_commit_door_keeps_r7_shape_byte_for_byte(
        tenant, bound_conn, wire, monkeypatch):
    """backend_url unset (legacy callers, the R7 pin suite's own shape): the
    brain-first flip runs — deterministic-first never engages without a commit
    door, so the existing R7 pins hold on the same seam."""
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])

    def _must_extract(chunk):
        raise AssertionError("deterministic pass must not run without a commit door")
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", _must_extract)

    from src.re_embedder.embedder import _growth_work_reattempt
    assert _growth_work_reattempt(
        "recall_miss_reexamine",
        GWQ.recall_miss_target_id(tenant["schema"], 1, 0),
        bound_conn, tenant["user"], tenant["schema"],
        "http://brain", tenant["dsn"], "http://qdrant") is True
    assert _doc_state(bound_conn) == ("pending", 1)


def test_recall_miss_brain_op_runs_the_r7_flip(
        tenant, bound_conn, wire, monkeypatch):
    """Phase 2: the brain row's runner IS the R7 flip (one house implementation
    — replay markers, retired-value freeze arming, attempts ceiling, all R7
    semantics)."""
    _seed_doc(bound_conn, tenant["user"], reextract_attempts=0)
    target = GWQ.recall_miss_target_id(tenant["schema"], 1, 0)
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", lambda chunk: [])
    assert GWQ.record_brain_defer_on_conn(bound_conn, tenant["schema"], target)
    assert _reattempt(bound_conn, tenant, op="recall_miss_brain", target=target) is True
    assert _doc_state(bound_conn) == ("pending", 1)


# ── pin (f): the two-phase chain end-to-end through the drain ─────────────────

def test_deterministic_success_completes_through_the_drain(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    """ROUND-3 critic (pin-(a) coverage): the drain stamps the marker COMPLETED
    on the deterministic-success path (previously only the runner's True verdict
    was asserted there)."""
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: [{"subject": "a", "rel_type": "b",
                                        "object": "c", "confidence": 0.8}])
    res = GWQ.drain(
        bound_conn,
        runner=lambda op, tgt: embedder._growth_work_reattempt(
            op, tgt, bound_conn, tenant["user"], tenant["schema"],
            "http://brain", tenant["dsn"], "http://qdrant",
            backend_url=_DSN_BACKEND),
        schema_name=tenant["schema"])
    assert res["drained"] == 1 and res["remaining"] == 0
    assert [r for r in _queue_rows(bound_conn) if r[0] == "recall_miss_brain"] == []


def test_two_phase_chain_deterministic_then_brain_via_drain(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", lambda chunk: [])

    def _drain():
        return GWQ.drain(
            bound_conn,
            runner=lambda op, tgt: embedder._growth_work_reattempt(
                op, tgt, bound_conn, tenant["user"], tenant["schema"],
                "http://brain", tenant["dsn"], "http://qdrant",
                backend_url=_DSN_BACKEND),
            schema_name=tenant["schema"])

    # ── cycle 1: the deterministic phase (priority 2). Zero edges on real
    #    content → marker completed + brain row written THIS cycle but never
    #    attempted in it (the drain batch was claimed before the write). ──
    res1 = _drain()
    assert res1["drained"] == 1, res1
    assert res1["by_type"].get("recall_miss_brain") == 1        # pending for next cycle
    assert _doc_state(bound_conn) == ("ready", 0)               # no flip yet

    # ── cycle 2: the brain phase (priority 5) — the R7 flip. ──
    res2 = _drain()
    assert res2["drained"] == 1, res2
    assert res2["remaining"] == 0
    assert _doc_state(bound_conn) == ("pending", 1)             # re-owed + replay armed
    assert wire["calls"] == []   # BOTH phases together burned zero brain HTTP from here


def test_drain_order_reexamine_before_brain(tenant, bound_conn, monkeypatch):
    """Priority order: a pending recall_miss_brain(5) NEVER runs before a
    pending recall_miss_reexamine(2) in the same batch."""
    _seed_doc(bound_conn, tenant["user"])
    target = GWQ.recall_miss_target_id(tenant["schema"], 1, 0)
    with bound_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
            " VALUES ('recall_miss_brain', %s, 5),"
            "       ('recall_miss_reexamine', %s, 2)", (target, target))
    bound_conn.commit()

    order = []

    def _runner(op, tgt):
        order.append(op)
        return True

    GWQ.drain(bound_conn, _runner, schema_name=tenant["schema"])
    assert order == ["recall_miss_reexamine", "recall_miss_brain"]


# ── pin (g): /ingest fault shapes ─────────────────────────────────────────────

def test_ingest_transport_failure_leaves_marker_pending(
        tenant, bound_conn, no_extract_lane, monkeypatch):
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: [{"subject": "a", "rel_type": "b",
                                        "object": "c", "confidence": 0.8}])

    def _down(url, **kwargs):
        raise embedder.httpx.ConnectError("backend down")
    monkeypatch.setattr(embedder.httpx, "post", _down)

    assert _reattempt(bound_conn, tenant) is False             # retry next cycle
    rows = _queue_rows(bound_conn)
    assert rows[0][0] == "recall_miss_reexamine" and rows[0][3] is None
    assert not [r for r in rows if r[0] == "recall_miss_brain"]  # no brain defer on a fault
    assert _doc_state(bound_conn) == ("ready", 0)


def test_ingest_4xx_refusal_defers_to_brain_not_a_retry_loop(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: [{"subject": "a", "rel_type": "b",
                                        "object": "c", "confidence": 0.8}])
    monkeypatch.setattr(embedder.httpx, "post",
                        lambda url, **kw: _FakeResp(status_code=400,
                                                    payload={"detail": "nope"}))
    assert _reattempt(bound_conn, tenant) is True              # resolved — via the brain row
    rows = _queue_rows(bound_conn)
    assert [r for r in rows if r[0] == "recall_miss_brain"
            and r[3] is None]                                  # deferred, pending


def test_brain_defer_write_fault_keeps_marker_pending(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    """ROUND-3 critic MAJOR: a FAULT on the brain-defer write (dead conn, aborted
    txn) must NOT complete the marker — that was the silent-complete-without-
    effect hole. Three-state contract: True=fresh, False=conflict (row exists),
    None=fault (nothing written)."""
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", lambda chunk: [])

    def _fault(conn, schema, target):
        assert conn is bound_conn and schema == tenant["schema"]
        return None                                     # the FAULT verdict
    monkeypatch.setattr(GWQ, "record_brain_defer_on_conn", _fault)

    assert _reattempt(bound_conn, tenant) is False      # retry — never retire
    rows = _queue_rows(bound_conn)
    assert rows[0][0] == "recall_miss_reexamine" and rows[0][3] is None
    assert not [r for r in rows if r[0] == "recall_miss_brain"]


def test_brain_defer_write_conflict_completes(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    """The CONFLICT arm (a pending brain row already exists) IS the desired end
    state — the marker completes."""
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", lambda chunk: [])
    monkeypatch.setattr(GWQ, "record_brain_defer_on_conn",
                        lambda conn, schema, target: False)   # conflict
    assert _reattempt(bound_conn, tenant) is True


def test_all_duplicate_edges_defer_to_brain_not_silent_complete(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    """SELF-REVIEW PIN: when /ingest 2xx's with committed=0 AND staged=0 (every
    deterministic edge was already present — the dedup), determinism changed
    NOTHING, so the marker's premise is unmet: completing would be the
    silent-complete-without-effect class. The brain lane is deferred instead."""
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: [{"subject": "a", "rel_type": "b",
                                        "object": "c", "confidence": 0.8}])
    monkeypatch.setattr(embedder.httpx, "post",
                        lambda url, **kw: _FakeResp(payload={"status": "success",
                                                             "committed": 0,
                                                             "staged": 0}))
    assert _reattempt(bound_conn, tenant) is True
    brain = [r for r in _queue_rows(bound_conn) if r[0] == "recall_miss_brain"]
    assert len(brain) == 1 and brain[0][3] is None     # deferred, pending
    assert _doc_state(bound_conn) == ("ready", 0)


def test_ingest_5xx_leaves_marker_pending(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: [{"subject": "a", "rel_type": "b",
                                        "object": "c", "confidence": 0.8}])
    monkeypatch.setattr(embedder.httpx, "post",
                        lambda url, **kw: _FakeResp(status_code=503,
                                                    payload={"detail": "later"}))
    assert _reattempt(bound_conn, tenant) is False
    assert not [r for r in _queue_rows(bound_conn) if r[0] == "recall_miss_brain"]


def test_frozen_store_keeps_marker_pending(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: [{"subject": "a", "rel_type": "b",
                                        "object": "c", "confidence": 0.8}])
    monkeypatch.setattr(embedder.httpx, "post",
                        lambda url, **kw: _FakeResp(payload={"status": "ingest_disabled"}))
    assert _reattempt(bound_conn, tenant) is False             # freeze waits it out
    assert not [r for r in _queue_rows(bound_conn) if r[0] == "recall_miss_brain"]


# ── R7 semantics held through the NEW door ────────────────────────────────────

def test_nonterminal_document_defers_through_uplift_door(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    """A done/failed ledger entry on a NON-terminal doc DEFERS (retry when
    terminal) — the R7 round-1/round-2 critic rule, unchanged by the new door."""
    _seed_doc(bound_conn, tenant["user"], status="processing",
              chunk_state={"0": {"s": "done"}})
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: [{"subject": "a", "rel_type": "b",
                                        "object": "c", "confidence": 0.8}])
    assert _reattempt(bound_conn, tenant) is False
    assert wire["calls"] == []
    assert _doc_state(bound_conn) == ("processing", 0)


def test_attempts_ceiling_retires_through_uplift_door(
        tenant, bound_conn, wire, no_extract_lane, monkeypatch):
    from src.re_embedder.embedder import _DOC_REMINE_MAX_ATTEMPTS
    _seed_doc(bound_conn, tenant["user"],
              reextract_attempts=_DOC_REMINE_MAX_ATTEMPTS)
    _seed_marker(bound_conn, tenant["schema"])
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges", lambda chunk: [])
    # Deterministic phase still runs (it commits directly — no attempts budget),
    # and its zero-edge outcome still defers to the brain lane; the BRAIN flip
    # will refuse at the ceiling. The deterministic phase is not ceiling-bound.
    assert _reattempt(bound_conn, tenant) is True
    assert [r for r in _queue_rows(bound_conn) if r[0] == "recall_miss_brain"]


# ── pin (h): the REAL deterministic layer (no deriver monkeypatch) ────────────

def test_real_spine_extracts_in_process_with_zero_network(
        tenant, bound_conn, wire, monkeypatch):
    """The deterministic-first promise is REAL extraction: the actual spine
    deriver runs on the chunk IN-PROCESS. GLiNER2 seam forced off (spine-only —
    no model download in the test env) and the httpx tripwire wired to RAISE —
    proving extraction itself makes zero network calls."""
    monkeypatch.setattr(embedder, "_recall_uplift_gliner", lambda: None)

    def _no_net(url, **kwargs):
        raise AssertionError(f"network call during deterministic extraction: {url}")
    monkeypatch.setattr(embedder.httpx, "post", _no_net)

    edges = embedder._deterministic_chunk_edges(CHUNK_TEXT)
    assert edges, "the real spine deriver extracted nothing from a contentful chunk"
    for e in edges:
        assert e["subject"] and e["rel_type"] and e["object"]
        assert e["confidence"] == 0.8
        assert e["fact_provenance"] == "user_stated"


def test_real_layer_returns_none_when_parse_layer_down(monkeypatch):
    """PIN (d), real-seam variant: the spine layer's own no-op signal (a parse
    miss — linguistics' contract when the model cannot load, e.g. SPACY_MODEL
    unset at import) surfaces as None so the caller falls through to the
    brain-first path, never a fake zero-edge 'success'."""
    from src.extraction import linguistics as L
    monkeypatch.setattr(L, "_parse", lambda text: None)
    assert embedder._deterministic_chunk_edges("Any text at all.") is None


def test_deterministic_pass_is_replay_stable(tenant, bound_conn, monkeypatch):
    """Same chunk → same edges (byte-stable): /ingest's edge-hash idempotency
    key must see an identical list on a replayed marker."""
    monkeypatch.setattr(embedder, "_recall_uplift_gliner", lambda: None)
    a = embedder._deterministic_chunk_edges(CHUNK_TEXT)
    b = embedder._deterministic_chunk_edges(CHUNK_TEXT)
    assert a == b


# ── pin (i): the typed-doc mirror's drift canaries ────────────────────────────

class _FakeGliner:
    """extract_entities shape: {'entities': {label: [{'text','confidence'}]}}."""
    def __init__(self, mapping):
        self._mapping = mapping

    def extract_entities(self, text, labels, threshold=0.5,
                         include_confidence=True, max_len=None):
        return {"entities": self._mapping}


def _parse_sent(text):
    from src.extraction.linguistics import _parse
    return _parse(text)


def test_typed_doc_veto_machine_value_spans():
    """ROUND-7 canary: a machine value (MAC/email/URL) is never an entity span —
    the veto must hold on the mirror, not just main.py."""
    doc = _parse_sent("Ping the host at sam@example.com okay.")
    gmodel = _FakeGliner({"Person": [{"text": "sam@example.com", "confidence": 0.9}]})
    out = embedder._gliner2_type_doc(doc, gmodel, embedder._RECALL_UPLIFT_LABELS)
    assert [e.text for e in out.ents] == []


def test_typed_doc_coordinated_surface_splits():
    """A comma/"and"-joined surface seeds EACH member as its own span (the
    "Mia, Theo" glue-defect fixture main.py documents)."""
    doc = _parse_sent("Mia and Theo visited Millbrook today.")
    gmodel = _FakeGliner({"Person": [{"text": "Mia and Theo", "confidence": 0.9}]})
    out = embedder._gliner2_type_doc(doc, gmodel, embedder._RECALL_UPLIFT_LABELS)
    surfaces = sorted(e.text.lower() for e in out.ents)
    assert surfaces == ["theo", "mia"]


def test_typed_doc_longest_span_wins_overlap():
    """Overlapping spans resolve longest-first (the containing span wins)."""
    doc = _parse_sent("Marcus Vogel arrived this morning.")
    gmodel = _FakeGliner({"Person": [
        {"text": "Marcus Vogel", "confidence": 0.95},
        {"text": "Marcus", "confidence": 0.9},
    ]})
    out = embedder._gliner2_type_doc(doc, gmodel, embedder._RECALL_UPLIFT_LABELS)
    assert [e.text for e in out.ents] == ["Marcus Vogel"]


def test_typed_doc_non_verbatim_span_never_seeds():
    """A span GLiNER2 "found" but the text does not contain is dropped (exact
    case-insensitive locate only — never fuzzy)."""
    doc = _parse_sent("Nora works at the hospital.")
    gmodel = _FakeGliner({"Person": [{"text": "Elena", "confidence": 0.9}]})
    out = embedder._gliner2_type_doc(doc, gmodel, embedder._RECALL_UPLIFT_LABELS)
    assert [e.text for e in out.ents] == []


def test_typed_doc_fail_safe_on_gliner_error():
    doc = _parse_sent("Nora works at the hospital.")

    class _Boom:
        def extract_entities(self, *a, **k):
            raise RuntimeError("boom")

    out = embedder._gliner2_type_doc(doc, _Boom(), embedder._RECALL_UPLIFT_LABELS)
    assert out is doc and list(out.ents) == []
