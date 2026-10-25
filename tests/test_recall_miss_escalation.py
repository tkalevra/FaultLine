"""R7 — miss-triggered escalation + growth feedback (the "learn from the miss" loop).

Pins (plan DEV/BUGS/plan-100pct-addressability-20261024.md §R7, QUERY side):

  (a) graph-walk miss + passage hit → the passage still surfaces (R6),
      the response is tagged ``escalated: True``, and a recall_miss marker
      lands in growth_work_queue ('recall_miss_reexamine', target
      '{schema}:{document_id}:{chunk_index}', priority 2) — written by the
      REAL /query wrapper with the walk + passage seams stubbed at their exact
      attach points, against a real tenant schema.
  (b) graph-walk hit → NO marker and no escalation tag — even when the
      passage lane ALSO hits (nothing to learn: the graph answered).
  (c) both lanes miss → the current honest empty: no tag, no marker.
  (f) marker write failure (DB down / writer raising) → the query response
      is unaffected.

Marker SQL semantics (hourly rate limit, pending dedup, GROWTH_QUEUE_MAX cap)
and the growth-drain pickup are pinned against the real database in
tests/embedder/test_recall_miss_reexamine.py — this file owns the DECISION
table and the /query door.
"""

import asyncio
import uuid
from pathlib import Path

import psycopg2
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.usefixtures("requires_postgres")

from src.api.models import QueryRequest, QueryResponse  # noqa: E402


# ── decision table (pure): _recall_escalation_plan ────────────────────────────

def test_plan_graph_miss_plus_passage_hits_escalates_and_names_markers():
    import src.api.main as M
    hits = [
        {"document_id": 7, "chunk_index": 3, "chunk_text": "the answer"},
        {"document_id": 7, "chunk_index": 5, "chunk_text": "also matched"},
    ]
    escalated, markers = M._recall_escalation_plan([], hits)
    assert escalated is True
    assert markers == hits  # both chunk-bearing passages are marker targets


def test_plan_graph_hit_writes_no_markers_even_with_passage_hits():
    """PIN (b): the graph SUCCEEDED — no escalation tag, no markers."""
    import src.api.main as M
    hits = [{"document_id": 7, "chunk_index": 3, "chunk_text": "the answer"}]
    escalated, markers = M._recall_escalation_plan([{"rel": "owns"}], hits)
    assert escalated is False
    assert markers == []


def test_plan_topic_gate_hidden_walk_hit_is_not_a_miss():
    """ROUND-3 CRITIC GAP: a walk that SUCCEEDED but whose rendered facts the
    lexical topic gate entirely removed (before=13 after=0 — a measured live
    shape) is NOT an extraction miss. The response still escalates (the USER's
    answer came from the document lane), but NO marker is written — the
    marker's premise ("extraction missed something in THIS chunk") would be
    false and its spend non-self-curing (re-examine → same facts → gate hides
    them again → re-arm hourly)."""
    import src.api.main as M
    hits = [{"document_id": 7, "chunk_index": 3, "chunk_text": "the answer"}]
    escalated, markers = M._recall_escalation_plan([], hits, walk_facts_found=13)
    assert escalated is True          # the fallback genuinely happened for the user
    assert markers == []              # but extraction is NOT at fault
    # zero/None pre-gate counts (true miss / early-return path) still mark
    _esc, markers0 = M._recall_escalation_plan([], hits, walk_facts_found=0)
    assert markers0 == hits
    _esc, markers_n = M._recall_escalation_plan([], hits, walk_facts_found=None)
    assert markers_n == hits


def test_plan_both_miss_is_the_honest_empty():
    """PIN (c): zero facts + zero hits → no tag, no markers, no fabrication."""
    import src.api.main as M
    escalated, markers = M._recall_escalation_plan([], [])
    assert escalated is False and markers == []
    escalated, markers = M._recall_escalation_plan([], None)
    assert escalated is False and markers == []


def test_plan_title_only_discovery_row_escalates_but_marks_nothing():
    """A title-only row (chunk_index NULL) HAS no chunk to re-examine: the
    response escalates (the document text answered, not the graph) but the
    marker set excludes it."""
    import src.api.main as M
    hits = [{"document_id": 7, "chunk_index": None, "chunk_text": None, "rank": None}]
    escalated, markers = M._recall_escalation_plan([], hits)
    assert escalated is True and markers == []
    # mixed: only the chunk-bearing row becomes a marker
    mixed = [hits[0], {"document_id": 8, "chunk_index": 1, "chunk_text": "x"}]
    _esc, markers = M._recall_escalation_plan([], mixed)
    assert markers == [mixed[1]]


def test_plan_tolerates_junk_hit_rows():
    import src.api.main as M
    escalated, markers = M._recall_escalation_plan([], [None, 42, "junk", {}])
    # {} is a dict with chunk_index None → escalates (a hit), no marker
    assert escalated is True and markers == []
    escalated, markers = M._recall_escalation_plan([{"a": 1}], [None, 42])
    assert escalated is False and markers == []


def test_query_response_carries_escalated_default_false():
    r = QueryResponse(anchor="x")
    assert r.escalated is False
    assert r.walk_facts_found is None
    r2 = QueryResponse(anchor="x", escalated=True)
    assert r2.escalated is True


def test_walk_count_is_stamped_at_the_walk_boundary():
    """ROUND-4 CRITIC GAP seam pin: the count the marker gate reads is the
    WALK'S OWN resolved_facts — NOT _tg_before. _tg_before is stamped AFTER
    the synthetic tail (named-winner, temporal_calc, sum/count-over-walk,
    recursive-walk metacog, and the no-answer LOCATION grounding fallback), so
    on a true miss with a stored location it reads 1 and would silently
    suppress the marker (pin (a) violated). resolved_facts is composed before
    any of those injections — counting it is immune in both directions
    (synthetic re-inflation AND presentation-gate hiding)."""
    import re
    src = (_REPO_ROOT / "src" / "api" / "main.py").read_text()
    # the main render return's walk_facts_found expression (the _query_impl
    # QueryResponse construction — not the plan function's default arg)
    assert re.search(r"walk_facts_found\s*=\s*_walk_found_n", src), (
        "the render return no longer stamps walk_facts_found=_walk_found_n")
    m2 = re.search(r"_rf_walk\s*=\s*([^\n]+)", src)
    assert m2 and "resolved_facts" in m2.group(1), m2.group(1) if m2 else None
    assert "_tg_before" not in src[src.index("walk_facts_found=_walk_found_n") - 2000:
                                   src.index("walk_facts_found=_walk_found_n")], (
        "the marker gate's count must not include synthetic rows "
        "(location-grounding re-inflation, round-4 GAP)")


# ── DB fixture: a real tenant schema carrying the growth_work_queue DDL ───────

_TEMPLATE = _REPO_ROOT / "src" / "provisioning" / "templates" / "user_schema.sql"


def _queue_ddl(schema_name: str) -> str:
    text = _TEMPLATE.read_text()
    start = text.index("CREATE TABLE IF NOT EXISTS {schema_name}.growth_work_queue")
    last = text.rindex("WHERE completed_at IS NULL;", start)
    return text[start:last + len("WHERE completed_at IS NULL;")].replace(
        "{schema_name}", schema_name)


@pytest.fixture()
def tenant(requires_postgres):
    """A tenant uid + schema carrying the queue table (template shape)."""
    import src.api.main as M
    uid = str(uuid.uuid4())
    schema = M._derive_tenant_schema(uid)
    assert schema, "uuid user must derive a tenant schema"
    conn = psycopg2.connect(requires_postgres)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(_queue_ddl(schema))
        conn.commit()
        yield {"uid": uid, "schema": schema, "dsn": requires_postgres}
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()


def _pending_markers(dsn, schema):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                "SELECT operation_type, target_id, priority FROM growth_work_queue"
                " WHERE completed_at IS NULL")
            return cur.fetchall()
    finally:
        conn.close()


_PASSAGE_HITS = [
    {"document_id": 7, "title": "Wind Energy Handbook", "status": "ready",
     "chunk_index": 3, "chunk_text": "Modern turbines achieve 20-40% efficiency.",
     "chunk_count": 8, "rank": 0.09},
]


async def _ready_async(user_id, endpoint):
    return None


def _patch_wrapper_seams(monkeypatch, *, facts, hits):
    """Stub the walk + passage seams at their EXACT wrapper attach points, on the
    real module, via context-manager-free monkeypatch (call inside the test)."""
    import src.api.main as M
    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready_async)
    monkeypatch.setattr(M, "_require_resolvable_user_id", lambda u, e: u)
    monkeypatch.setattr(M, "_document_hits_for_query",
                        lambda uid, text, limit=3: list(hits))
    monkeypatch.setattr(
        M, "_query_impl",
        lambda request: QueryResponse(
            anchor="00000000-0000-0000-0000-000000000000",
            facts=list(facts), document_hits=list(hits)))


async def _settle_bg_tasks():
    """Let the fire-and-forget marker task run to completion (threadpool hop)."""
    for _ in range(100):
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)


async def test_wrapper_miss_plus_hit_tags_and_writes_marker(tenant, monkeypatch):
    """PIN (a): the REAL /query door — facts==[], passage hit → escalated True,
    passage still attached, ONE marker per chunk-bearing hit in the queue."""
    import src.api.main as M
    _patch_wrapper_seams(monkeypatch, facts=[], hits=_PASSAGE_HITS)
    req = QueryRequest(text="how efficient are wind turbines", user_id=tenant["uid"])
    resp = await M.query(req)

    assert isinstance(resp, QueryResponse)
    assert resp.facts == []
    assert resp.document_hits == _PASSAGE_HITS      # the passage still surfaces
    assert resp.escalated is True                   # the recall knows it fell back
    await _settle_bg_tasks()
    rows = _pending_markers(tenant["dsn"], tenant["schema"])
    assert rows == [("recall_miss_reexamine", f"{tenant['schema']}:7:3", 2)], rows


async def test_wrapper_graph_hit_writes_no_marker(tenant, monkeypatch):
    """PIN (b): graph walk SUCCEEDED → no tag, no marker — even with a passage hit."""
    import src.api.main as M
    _patch_wrapper_seams(monkeypatch, facts=[{"rel": "owns"}], hits=_PASSAGE_HITS)
    req = QueryRequest(text="tell me about my turbines", user_id=tenant["uid"])
    resp = await M.query(req)

    assert resp.escalated is False
    assert resp.facts and resp.document_hits   # both lanes' payloads intact
    await _settle_bg_tasks()
    assert _pending_markers(tenant["dsn"], tenant["schema"]) == []


async def test_wrapper_both_miss_is_honest_empty_no_marker(tenant, monkeypatch):
    """PIN (c): zero facts + zero hits → the empty response stands, untagged."""
    import src.api.main as M
    _patch_wrapper_seams(monkeypatch, facts=[], hits=[])
    req = QueryRequest(text="solar panel photovoltaic", user_id=tenant["uid"])
    resp = await M.query(req)

    assert resp.escalated is False
    assert resp.facts == [] and resp.document_hits == []
    await _settle_bg_tasks()
    assert _pending_markers(tenant["dsn"], tenant["schema"]) == []


async def test_wrapper_walk_error_is_not_a_graph_miss(tenant, monkeypatch):
    """Round-2 critic MINOR 1: a walk that FAILED (error set) returned facts=[]
    for infrastructure reasons — tagging escalated:true or writing markers
    whose premise ("extraction missed something in THIS chunk") is false would
    mislabel the fault AND spend re-extraction on it. Neither happens."""
    import src.api.main as M
    from src.api.models import QueryRequest, QueryResponse

    async def _ready(user_id, endpoint):
        return None

    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready)
    monkeypatch.setattr(M, "_require_resolvable_user_id", lambda u, e: u)
    monkeypatch.setattr(M, "_document_hits_for_query",
                        lambda uid, text, limit=3: _PASSAGE_HITS)
    monkeypatch.setattr(
        M, "_query_impl",
        lambda request: QueryResponse(anchor="a", facts=[],
                                      error="walk infrastructure failure"))
    resp = await M.query(QueryRequest(text="how efficient are wind turbines",
                                      user_id=tenant["uid"]))
    assert resp.facts == [] and resp.document_hits == _PASSAGE_HITS
    assert resp.escalated is False          # an error is not a miss
    await _settle_bg_tasks()
    assert _pending_markers(tenant["dsn"], tenant["schema"]) == []


async def test_wrapper_topic_gate_hidden_hit_tags_but_never_marks(tenant, monkeypatch):
    """ROUND-3 CRITIC GAP at the /query door: walk found 13 facts pre-gate,
    the topic gate removed them all from the render, the passage lane hit →
    escalated True (the user's answer came from the document lane) but ZERO
    markers (extraction is not at fault — the guard rides the real
    walk_facts_found field through the wrapper)."""
    import src.api.main as M
    from src.api.models import QueryRequest, QueryResponse

    async def _ready(user_id, endpoint):
        return None

    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready)
    monkeypatch.setattr(M, "_require_resolvable_user_id", lambda u, e: u)
    monkeypatch.setattr(M, "_document_hits_for_query",
                        lambda uid, text, limit=3: _PASSAGE_HITS)
    monkeypatch.setattr(
        M, "_query_impl",
        lambda request: QueryResponse(anchor="a", facts=[],
                                      walk_facts_found=13))
    resp = await M.query(QueryRequest(text="how efficient are wind turbines",
                                      user_id=tenant["uid"]))
    assert resp.escalated is True
    assert resp.document_hits == _PASSAGE_HITS
    await _settle_bg_tasks()
    assert _pending_markers(tenant["dsn"], tenant["schema"]) == []


async def test_wrapper_marker_write_failure_never_breaks_the_response(
        tenant, monkeypatch):
    """PIN (f): the marker writer RAISES → the response (tag included) is
    unaffected — the fire-and-forget containment holds."""
    import src.api.main as M
    from src.re_embedder import growth_work_queue as GWQ
    GWQ.reset_recorder_hygiene()

    def _boom(*a, **k):
        raise RuntimeError("marker DB exploded")

    monkeypatch.setattr(GWQ, "record_recall_miss", _boom)
    _patch_wrapper_seams(monkeypatch, facts=[], hits=_PASSAGE_HITS)
    req = QueryRequest(text="how efficient are wind turbines", user_id=tenant["uid"])
    resp = await M.query(req)   # must not raise

    assert resp.escalated is True           # the tag is set BEFORE the write
    assert resp.document_hits == _PASSAGE_HITS
    await _settle_bg_tasks()
    assert _pending_markers(tenant["dsn"], tenant["schema"]) == []
    GWQ.reset_recorder_hygiene()


def test_record_recall_miss_unreachable_db_returns_false(monkeypatch):
    """PIN (f), SQL half: an unreachable DB is a False, never an exception."""
    from src.re_embedder import growth_work_queue as GWQ
    GWQ.reset_recorder_hygiene()
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://nobody@127.0.0.1:1/nowhere")
    assert GWQ.record_recall_miss("faultline_x", 7, 3) is False
    GWQ.reset_recorder_hygiene()


def test_record_recall_miss_rejects_degenerate_shapes():
    from src.re_embedder import growth_work_queue as GWQ
    assert GWQ.record_recall_miss("", 7, 3) is False
    assert GWQ.record_recall_miss("faultline_x", None, 3) is False
    assert GWQ.record_recall_miss("faultline_x", 7, None) is False
    assert GWQ.record_recall_miss("faultline_x", "junk", 3) is False
    assert GWQ.record_recall_miss("faultline_x", 7, "junk") is False


def test_record_recall_miss_marker_writer_never_raises(monkeypatch):
    """The /query helper is fire-and-forget: even its own seams stay contained."""
    import src.api.main as M
    monkeypatch.setattr(M, "_derive_tenant_schema", lambda uid: None)
    M._record_recall_miss_markers("some-uid", [{"document_id": 1, "chunk_index": 0}])
    M._record_recall_miss_markers("some-uid", [])   # no rows → immediate return
    M._record_recall_miss_markers("", [{"document_id": 1, "chunk_index": 0}])
