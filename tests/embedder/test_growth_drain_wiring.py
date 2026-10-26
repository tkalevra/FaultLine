"""BUG-08 (adversarial bench 20261024) — markers never drain, uplift inert.

FOSS-port note (2026-10-26): this engine ships no brain-lane machinery, so the
lane verdicts are pinned through an INSTALLED lane check (delegation seam); the
default-no-lane behaviour is pinned separately (trivially open).

THE BREAK (measured on private prod 2026-10-25, owner seat): 8 recall_miss_reexamine
markers sat pending 3+ hours (and counting) while the growth cycle ran — 135
``re_embedder.llm_skip`` lines / 24h (``tenant_brain_unavailable_failclosed``,
15 cycles x 9 subsystems) and **zero** ``growth_work_drain`` lines. The drain
sat behind a BLANKET brain-lane gate (outer ``_reembedder_llm_lane_open`` +
zero-arg ``lane_probe``), and the document drain (BUG-04) keeps the tenant
brain's lane permanently closed — so the drain NEVER ran, and the
DETERMINISTIC phase it was blocking (R9 spine + GLiNER2, in-process, zero
brain) starved behind a brain verdict it does not need.

Pins:

  (W1) ``_growth_drain_lane_open`` is op-aware: a deterministic operation
       (recall_miss_reexamine) passes with the brain lane HARD-CLOSED and the
       lane gate is not even consulted for it; brain-bearing operations
       (promotion / enrichment / synonym_convergence / whatis_climb /
       recall_miss_brain) keep the closed-lane verdict.
  (W2) the full C->B uplift chain with the brain lane closed the whole time:
       marker written -> drain (production-shaped wiring: op-aware probe) ->
       _growth_work_reattempt -> _recall_miss_uplift -> in-process spine
       (mocked) -> /ingest commit -> marker completed — while a brain-bearing
       item in the SAME batch stays pending (its lane is closed).
  (W3) the drain's op-aware skip is not a head-of-line block: a priority-2
       brain op that probes closed does not stop the priority-2 deterministic
       op behind it (the old ``break`` shape).

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

CHUNK_TEXT = ("Marcus owns two vintage synthesizers. "
              "His sister Nora works at the hospital in Millbrook.")


def _queue_ddl(schema_name: str) -> str:
    text = _TEMPLATE.read_text()
    start = text.index("CREATE TABLE IF NOT EXISTS {schema_name}.growth_work_queue")
    last = text.rindex("WHERE completed_at IS NULL;", start)
    return text[start:last + len("WHERE completed_at IS NULL;")].replace(
        "{schema_name}", schema_name)


@pytest.fixture()
def tenant(requires_postgres):
    schema = f"faultline_bug8_{uuid.uuid4().hex[:12]}"
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
    embedder._remine_columns_missing_schemas.clear()
    monkeypatch.setattr(embedder, "_gliner2_model_cache", None)
    monkeypatch.setattr(embedder, "_gliner2_failed_until", 0.0)
    yield


@pytest.fixture()
def lane_hard_closed(monkeypatch):
    """The production wound, minimally reproduced: the tenant brain's lane is
    CLOSED for every LLM-bearing subsystem (what the doc drain's appetite does
    to the growth loop on a paced seat). Records the consultations so the pins
    can prove the deterministic op never needed one."""
    consultations = []

    def _closed(user_id, subsystem, schema_name=""):
        consultations.append(subsystem)
        return False

    # FOSS adaptation: this lineage has no _reembedder_llm_lane_open in production
    # (raising=False) — the fixture INSTALLS one to pin _growth_drain_lane_open's
    # delegation seam: when lane machinery exists, brain-bearing ops consult it.
    monkeypatch.setattr(embedder, "_reembedder_llm_lane_open", _closed, raising=False)
    return {"consultations": consultations}


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"status": "success", "committed": 2, "staged": 0}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


# ── pin W1: the gate is op-aware ───────────────────────────────────────────────

def test_deterministic_op_passes_with_lane_hard_closed(lane_hard_closed):
    assert embedder._growth_drain_lane_open(
        "u", "s", "recall_miss_reexamine") is True
    # the brain-lane gate was never even consulted for the deterministic op
    assert lane_hard_closed["consultations"] == []


@pytest.mark.parametrize("op", [
    "promotion", "enrichment", "synonym_convergence",
    "whatis_climb", "recall_miss_brain",
])
def test_brain_bearing_ops_keep_the_closed_lane_verdict(lane_hard_closed, op):
    assert embedder._growth_drain_lane_open("u", "s", op) is False
    assert "ontology_eval" in lane_hard_closed["consultations"]


def test_no_lane_machinery_means_trivially_open(monkeypatch):
    """FOSS shape: with no lane check installed (this engine ships none), the gate
    is OPEN for every operation — the drain always ran ungated here, so BUG-08's
    starvation could not occur; the op-aware seam exists for a future lane."""
    monkeypatch.delattr(embedder, "_reembedder_llm_lane_open", raising=False)
    for op in ("promotion", "enrichment", "synonym_convergence",
               "whatis_climb", "recall_miss_brain", "recall_miss_reexamine"):
        assert embedder._growth_drain_lane_open("u", "s", op) is True


def test_deterministic_registry_covers_the_uplift_runner():
    """Every operation whose runner is the deterministic uplift must be in
    DETERMINISTIC_OPERATIONS — the registry is what the pinned gate reads."""
    assert "recall_miss_reexamine" in GWQ.DETERMINISTIC_OPERATIONS


# ── pin W2: the full chain, lane closed, production-shaped wiring ─────────────

def _seed_doc(conn, user, doc_id=1):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (id, user_id, status, chunk_state, chunks,"
            " source_ref, reextract_attempts, ready_at)"
            " VALUES (%s, %s, 'ready', %s::jsonb, %s::jsonb, 'doc.pdf', 0, now())",
            (doc_id, user, json.dumps({"0": {"s": "done"}}),
             json.dumps([CHUNK_TEXT])))
    conn.commit()


def _seed_marker(conn, schema, doc_id=1, chunk_idx=0):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
            " VALUES ('recall_miss_reexamine', %s, 2)",
            (GWQ.recall_miss_target_id(schema, doc_id, chunk_idx),))
    conn.commit()


def test_full_c_to_b_chain_drains_with_the_lane_closed(
        tenant, bound_conn, lane_hard_closed, monkeypatch):
    """THE BUG-08 PIN — the exact chain that never fired on prod: marker ->
    growth cycle drain (production-shaped wiring: runner + item_done +
    _growth_drain_lane_open probe) -> deterministic extraction (in-process
    spine mocked to the prod-equivalent seam) -> /ingest commit -> marker
    completed. The brain lane is HARD-CLOSED for the entire drain, and a
    brain-bearing item in the same batch stays pending."""
    _seed_doc(bound_conn, tenant["user"])
    _seed_marker(bound_conn, tenant["schema"])
    with bound_conn.cursor() as cur:  # a brain-bearing sibling in the same batch
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
            " VALUES ('enrichment', 'ontology_eval', 5)")
    bound_conn.commit()

    _det_edges = [{"subject": "marcus", "rel_type": "works_for",
                   "object": "hospital", "confidence": 0.8,
                   "fact_provenance": "user_stated"}]
    monkeypatch.setattr(embedder, "_deterministic_chunk_edges",
                        lambda chunk: list(_det_edges))

    posts = []

    def _fake_post(url, **kwargs):
        posts.append({"url": url, "kwargs": kwargs})
        return _FakeResp()

    monkeypatch.setattr(embedder.httpx, "post", _fake_post)

    res = GWQ.drain(
        bound_conn,
        schema_name=tenant["schema"],
        runner=lambda op, tgt: embedder._growth_work_reattempt(
            op, tgt, bound_conn, tenant["user"], tenant["schema"],
            "http://brain", tenant["dsn"], "http://qdrant",
            backend_url=_DSN_BACKEND),
        item_done=lambda op, tgt: embedder._growth_work_item_done(
            op, tgt, bound_conn),
        lane_probe=lambda op: embedder._growth_drain_lane_open(
            tenant["user"], tenant["schema"], op),
    )

    # the deterministic marker drained; the brain-bearing sibling did not
    assert res["drained"] == 1, res
    assert res["by_type"] == {"enrichment": 1}

    # the C->B commit happened: ONE /ingest POST carrying the deterministic
    # edges under the replay marker (a recall-miss re-send is a re-attempt)
    ingest = [p for p in posts if p["url"].endswith("/ingest")]
    assert len(ingest) == 1 and len(posts) == 1
    body = ingest[0]["kwargs"]["json"]
    assert body["edges"] == _det_edges and body["source"] == "document"
    headers = ingest[0]["kwargs"]["headers"]
    assert headers.get(ingest_transport.REPLAY_MARKER_HEADER) \
        == ingest_transport.REPLAY_MARKER_VALUE

    # the marker is completed; the sibling is still pending for a cycle when
    # the brain lane reopens
    with bound_conn.cursor() as cur:
        cur.execute(
            "SELECT operation_type FROM growth_work_queue"
            " WHERE completed_at IS NULL")
        assert [r[0] for r in cur.fetchall()] == ["enrichment"]


# ── pin W3: the closed lane is a skip, not a head-of-line block ───────────────

def test_closed_brain_op_does_not_block_the_deterministic_op(
        bound_conn, lane_hard_closed):
    """Priority order puts promotion(2) ahead of or beside
    recall_miss_reexamine(2); the old blanket probe `break`ed the whole drain
    on the first closed verdict. The op-aware skip must let the deterministic
    op behind it run in the SAME drain."""
    with bound_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
            " VALUES ('promotion', '901', 2),"
            "       ('recall_miss_reexamine', 's:1:0', 2)")
    bound_conn.commit()

    ran = []
    res = GWQ.drain(
        bound_conn,
        runner=lambda op, tgt: ran.append(op) or True,
        schema_name="s",
        lane_probe=lambda op: embedder._growth_drain_lane_open("u", "s", op))
    assert "recall_miss_reexamine" in ran
    assert "promotion" not in ran
    assert res["by_type"] == {"promotion": 1}
    with bound_conn.cursor() as cur:
        cur.execute(
            "SELECT operation_type FROM growth_work_queue"
            " WHERE completed_at IS NULL")
        assert [r[0] for r in cur.fetchall()] == ["promotion"]
