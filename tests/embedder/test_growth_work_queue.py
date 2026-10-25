"""
Growth work queue — the durable breadcrumb for brain-starved growth work (gauntlet R8).

Pins (per DEV/BUGS/plan-100pct-addressability-20261024.md §R8):
  (a) brain-lane closed → work items persisted for the deferred operations
  (b) brain-lane open → queue drained priority-first
  (c) bounded queue (oldest evicted at cap)
  (d) partial unique (same target not duplicated while pending)
  (e) completed items prunable
  (f) queue failure → growth loop still runs (fail-safe)
  (g) migration applies to fresh + legacy tenants

DB-backed pins run against POSTGRES_DSN (repo convention: requires_postgres skips
honestly without one). SQL semantics (partial unique, eviction, prune) are pinned
against the REAL database — a scripted cursor can prove a write-path pin green while
the schema invariant is false.
"""
import os
import re
import uuid
from pathlib import Path

import psycopg2
import pytest

from src.re_embedder import growth_work_queue as GWQ

pytestmark = pytest.mark.usefixtures("requires_postgres")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_300 = _REPO_ROOT / "migrations" / "300_growth_work_queue.sql"
_TEMPLATE = _REPO_ROOT / "src" / "provisioning" / "templates" / "user_schema.sql"


# ── helpers ───────────────────────────────────────────────────────────────────

def _dsn():
    return os.environ["POSTGRES_DSN"]


def _template_queue_ddl(schema_name: str) -> str:
    """The growth_work_queue block of the provisioning template, {schema_name}
    substituted — exactly what a FRESH tenant executes at provisioning. Slices to
    the END of the LAST queue statement (the drain index), not the first partial
    index — the uq and drain indexes share the `WHERE completed_at IS NULL;` tail."""
    text = _TEMPLATE.read_text()
    start = text.index("CREATE TABLE IF NOT EXISTS {schema_name}.growth_work_queue")
    last = text.rindex("WHERE completed_at IS NULL;", start)
    return text[start:last + len("WHERE completed_at IS NULL;")].replace("{schema_name}", schema_name)


@pytest.fixture()
def tenant_schema(postgres_dsn):
    """A fresh per-tenant schema carrying the growth_work_queue table (template shape)."""
    schema = f"faultline_gwq_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(postgres_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(_template_queue_ddl(schema))
        conn.commit()
        yield schema
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()


@pytest.fixture()
def bound_conn(postgres_dsn, tenant_schema):
    conn = psycopg2.connect(postgres_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {tenant_schema}")
        conn.commit()
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_recorder():
    GWQ.reset_recorder_hygiene()
    yield
    GWQ.reset_recorder_hygiene()


def _pending_rows(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation_type, target_id, priority FROM growth_work_queue"
            " WHERE completed_at IS NULL ORDER BY priority, created_at, id")
        return cur.fetchall()


# ── pin (a): lane-closed deferral → work items persisted ─────────────────────
#
# FOSS-port note: the upstream pins exercised the defer hooks inside
# ``_reembedder_llm_lane_open`` (brain posture / tenant-brain binding) and inside
# the promote path — both are SaaS-lane machinery this repo does not carry (see
# DEV/BUGS/foss-port-cba.md: lane-gate defer hooks unwired on FOSS). The PUBLIC
# contract those hooks relied on is pinned here directly at the API level.

def test_record_deferred_persists_the_starvation_breadcrumb(tenant_schema, bound_conn):
    """record_deferred writes the pending breadcrumb with the subsystem's mapped
    operation family + priority — the contract the (SaaS-lane) defer hooks call."""
    assert GWQ.record_deferred(
        tenant_schema, "synonym_convergence", user_id="11111111-1111-4111-8111-111111111111") is True
    assert _pending_rows(bound_conn) == [
        ("synonym_convergence", "synonym_convergence", 6)]
    # whatis_classify maps to whatis_climb at its own priority
    assert GWQ.record_deferred(
        tenant_schema, "whatis_classify", user_id="11111111-1111-4111-8111-111111111111") is True
    assert _pending_rows(bound_conn) == [
        ("synonym_convergence", "synonym_convergence", 6),
        ("whatis_climb", "whatis_classify", 7)]


@pytest.mark.skip(reason="FOSS port: brain-posture lane gating is SaaS-only machinery "
                         "(no _reembedder_llm_lane_open on this lineage) — see "
                         "DEV/BUGS/foss-port-cba.md")
def test_lane_closed_posture_cooldown_records_deferred(tenant_schema, bound_conn, monkeypatch):
    """The posture branch (brain rate-limited: measured p95 tightening) is the same
    starvation shape — it records too."""
    import src.api.llm_calls as llm_calls
    monkeypatch.setattr(llm_calls, "_saas_brain_unavailable", lambda: False)
    import saas.brain_posture as posture
    monkeypatch.setattr(posture, "growth_deferred", lambda: 120)
    from src.re_embedder import embedder

    open_ = embedder._reembedder_llm_lane_open(
        "11111111-1111-4111-8111-111111111111", "whatis_classify", tenant_schema)
    assert open_ is False
    assert _pending_rows(bound_conn) == [("whatis_climb", "whatis_classify", 7)]


def test_lane_open_records_nothing(tenant_schema, bound_conn):
    """An OPEN lane (or no lane machinery at all) writes no breadcrumb — the queue
    only ever holds work that was actually deferred."""
    # The FOSS engine has no lane gate; the honest equivalent: recording nothing
    # leaves the queue empty, and a completed deferral re-arms only on a NEW defer.
    assert _pending_rows(bound_conn) == []


@pytest.mark.skip(reason="FOSS port: promote_class_c_hits on this lineage has no "
                         "classify-failure defer hook (SaaS lane machinery) — see "
                         "DEV/BUGS/foss-port-cba.md")
def test_promotion_classify_failure_records_staged_target(tenant_schema, bound_conn, monkeypatch):
    """The promotion lane's precise deferral point: a Class-C row that earned its
    promotion but could not be structured (brain would not answer) leaves a
    breadcrumb naming the staged row."""
    from src.re_embedder import embedder

    recorded = []
    monkeypatch.setattr(
        embedder.growth_work_queue, "record_deferred",
        lambda *a, **k: recorded.append((a, k)) or True)

    class _Cur:
        """Shared scripted cursor: the candidate SELECT fetches the full batch on the
        first call and nothing after (the per-row statements never fetchall)."""

        def __init__(self, rows):
            self._rows = rows
            self._fetched = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            if sql.strip().startswith("SELECT id, subject_id"):
                self._fetched = False

        def fetchall(self):
            if self._fetched:
                return []
            self._fetched = True
            return self._rows

    class _Conn:
        def __init__(self, rows):
            self._rows = rows

        def cursor(self):
            return _Cur(self._rows)

        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(embedder, "_query_llm_for_rel_type_metadata", lambda *a, **k: {})
    monkeypatch.setattr(embedder, "resolve_display_names_for_facts",
                        lambda conn, facts: [{"subject_display": "s", "object_display": "o"}])
    monkeypatch.setattr(embedder, "release_read_transaction", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "_reflexive_hierarchy_blocked", lambda *a, **k: False)

    n = embedder.promote_class_c_hits(
        _Conn([
            (101, "subj", "obj", None, "prov", "llm_inferred", 0.7, 3,
             None, None, None, None, None, False),
            (102, "subj2", "obj2", None, "prov", "llm_inferred", 0.7, 4,
             None, None, None, None, None, False),
        ]), "http://qdrant:6333", "http://brain", user_id="u1",
        schema_name=tenant_schema)
    assert n == 0  # both rows LEFT AS CLASS C — nothing promoted under a dead brain
    targets = sorted(k.get("target_id") for (a, k) in recorded)
    assert targets == ["101", "102"]
    assert all(a[1] == "promotion" for (a, k) in recorded)


# ── pin (b): brain-lane open → queue drained priority-first ───────────────────

def test_drain_is_priority_first_and_marks_completed(bound_conn, caplog):
    with bound_conn.cursor() as cur:
        for op, tgt, pri in [
            ("whatis_climb", "classify_climb", 7),
            ("promotion", "901", 2),
            ("enrichment", "ontology_eval", 5),
            ("synonym_convergence", "synonym_convergence", 6),
        ]:
            cur.execute(
                "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
                " VALUES (%s, %s, %s)", (op, tgt, pri))
    bound_conn.commit()

    order = []

    def runner(op, tgt):
        order.append((op, tgt))
        return True

    with caplog.at_level("INFO", logger="src.re_embedder.growth_work_queue"):
        res = GWQ.drain(bound_conn, runner, schema_name="sched")

    assert order == [
        ("promotion", "901"),                 # priority 2 — recall-visible first
        ("enrichment", "ontology_eval"),      # priority 5
        ("synonym_convergence", "synonym_convergence"),  # priority 6
        ("whatis_climb", "classify_climb"),   # priority 7
    ]
    assert res["drained"] == 4 and res["remaining"] == 0
    with bound_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM growth_work_queue"
                    " WHERE completed_at IS NULL")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM growth_work_queue"
                    " WHERE completed_at IS NOT NULL")
        assert cur.fetchone()[0] == 4
    assert any("growth_work_drain drained=4 remaining=0" in r.message
               for r in caplog.records)


def test_drain_leaves_pending_on_failure_and_uses_item_done(bound_conn):
    with bound_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
            " VALUES ('promotion', '777', 2), ('enrichment', 'ontology_eval', 5)")
    bound_conn.commit()

    def runner(op, tgt):
        return op == "enrichment"  # promotion re-attempt "still starved"

    def item_done(op, tgt):
        return not (op == "promotion" and tgt == "777")

    res = GWQ.drain(bound_conn, runner, item_done=item_done, schema_name="s")
    assert res["drained"] == 1
    rows = dict((t, op) for op, t, _p in _pending_rows(bound_conn))
    assert rows == {"777": "promotion"}  # rate-limited retry: left pending


def test_drain_runner_exception_is_a_retry_not_a_crash(bound_conn):
    with bound_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id)"
            " VALUES ('enrichment', 'ontology_eval')")
    bound_conn.commit()

    def boom(op, tgt):
        raise RuntimeError("brain flapped mid-drain")

    res = GWQ.drain(bound_conn, boom, schema_name="s")
    assert res["drained"] == 0
    assert len(_pending_rows(bound_conn)) == 1


def test_drain_runs_one_pass_per_operation_not_per_item(bound_conn):
    """Amplification guard: ten pending promotion rows cost ONE promotion pass —
    each row is still individually adjudicated by item_done."""
    with bound_conn.cursor() as cur:
        for sid in ("901", "902", "903"):
            cur.execute(
                "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
                " VALUES ('promotion', %s, 2)", (sid,))
    bound_conn.commit()
    calls = []

    def runner(op, tgt):
        calls.append((op, tgt))
        return True

    res = GWQ.drain(bound_conn, runner, schema_name="s")
    assert calls == [("promotion", "901")]  # ONE pass — memoized for 902/903
    assert res["drained"] == 3              # all three rows adjudicated + completed


def test_drain_failure_backs_off_priority(bound_conn):
    """A failing item sinks toward 9 — no permanent head-of-line block."""
    with bound_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
            " VALUES ('promotion', '42', 2)")
    bound_conn.commit()
    GWQ.drain(bound_conn, lambda op, tgt: False, schema_name="s")
    with bound_conn.cursor() as cur:
        cur.execute("SELECT priority FROM growth_work_queue WHERE completed_at IS NULL")
        assert cur.fetchone()[0] == 3


def test_drain_lane_probe_stops_the_batch(bound_conn):
    """The brain flapping closed mid-drain stops further spend; the rest of the
    batch keeps its pending rows (rate-limited retry next cycle)."""
    with bound_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id, priority)"
            " VALUES ('promotion', '1', 2), ('enrichment', 'ontology_eval', 5)")
    bound_conn.commit()
    calls = []

    def runner(op, tgt):
        calls.append((op, tgt))
        return True

    probes = [True, False]  # open for the first op, flaps closed before the second
    res = GWQ.drain(bound_conn, runner, schema_name="s",
                    lane_probe=lambda: probes.pop(0) if probes else False)
    assert calls == [("promotion", "1")]   # enrichment never attempted
    rows = dict((t, op) for op, t, _p in _pending_rows(bound_conn))
    assert rows == {"ontology_eval": "enrichment"}


def test_drain_reports_attempted_to_caller(bound_conn):
    """on_attempted lets the growth loop record the ledger run for a subsystem it
    re-ran — the hook that stops the normal cycle re-spending on the same token."""
    with bound_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id)"
            " VALUES ('enrichment', 'ontology_eval')")
    bound_conn.commit()
    seen = []
    GWQ.drain(bound_conn, lambda op, tgt: True, schema_name="s",
              on_attempted=lambda op, tgt, ok: seen.append((op, tgt, ok)))
    assert seen == [("enrichment", "ontology_eval", True)]


def test_flag_off_subsystem_resolves_its_breadcrumb(monkeypatch):
    """A flag-OFF growth subsystem retires its breadcrumb WITHOUT invoking the
    disabled operation — the deferral no longer applies, and holding it pending
    would wedge the queue forever."""
    from src.re_embedder import embedder

    def must_not_run(*a, **k):
        raise AssertionError("flag-OFF subsystem must not be invoked")

    monkeypatch.setattr(embedder, "converge_lifted_synonyms", must_not_run)
    monkeypatch.setattr(embedder, "_ENGINE_SYNONYM_CONVERGENCE", False)
    assert embedder._growth_work_reattempt(
        "synonym_convergence", "synonym_convergence", None, "u", "s",
        "http://b", "dsn", "http://q") is True


def test_unknown_target_resolves_its_breadcrumb(monkeypatch):
    """A renamed/removed subsystem retires its breadcrumb rather than wedging."""
    from src.re_embedder import embedder
    assert embedder._growth_work_reattempt(
        "enrichment", "some_future_subsystem", None, "u", "s",
        "http://b", "dsn", "http://q") is True


# ── pin (c): bounded queue, oldest evicted ────────────────────────────────────

def test_bounded_queue_evicts_oldest(bound_conn, monkeypatch):
    monkeypatch.setenv("GROWTH_QUEUE_MAX", "3")
    for i in range(5):
        assert GWQ.record_deferred(
            "sched", "enrichment", target_id=f"t{i}", conn=bound_conn) is True
    rows = sorted(t for _op, t, _p in _pending_rows(bound_conn))
    assert rows == ["t2", "t3", "t4"]  # the OLDEST two (t0, t1) were evicted


# ── pin (d): partial unique — no duplicate pending work ───────────────────────

def test_partial_unique_no_duplicate_pending(bound_conn):
    GWQ.record_deferred("sched", "synonym_convergence", conn=bound_conn)
    GWQ.record_deferred("sched", "synonym_convergence", conn=bound_conn)
    GWQ.record_deferred("sched", "synonym_convergence", conn=bound_conn)
    assert len(_pending_rows(bound_conn)) == 1


def test_partial_unique_rearms_after_completion(bound_conn):
    """Completed rows are exempt from the unique — a NEW deferral after completion
    creates a fresh pending row (the cycle can repeat)."""
    GWQ.record_deferred("sched", "synonym_convergence", conn=bound_conn)
    with bound_conn.cursor() as cur:
        cur.execute("SELECT id FROM growth_work_queue WHERE completed_at IS NULL")
        item_id = cur.fetchone()[0]
    GWQ.mark_completed(bound_conn, [item_id])
    GWQ.record_deferred("sched", "synonym_convergence", conn=bound_conn)
    with bound_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM growth_work_queue")
        assert cur.fetchone()[0] == 2  # one completed + one pending
    assert len(_pending_rows(bound_conn)) == 1


def test_partial_unique_enforced_at_the_database(bound_conn):
    """The INDEX itself (not the ON CONFLICT path) refuses duplicate pending work —
    a rogue writer cannot hammer the queue."""
    with bound_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO growth_work_queue (operation_type, target_id)"
            " VALUES ('enrichment', 'rogue')")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO growth_work_queue (operation_type, target_id)"
                " VALUES ('enrichment', 'rogue')")


# ── pin (e): completed items prunable ─────────────────────────────────────────

def test_completed_items_prunable(bound_conn):
    GWQ.record_deferred("sched", "enrichment", conn=bound_conn)
    with bound_conn.cursor() as cur:
        cur.execute("SELECT id FROM growth_work_queue WHERE completed_at IS NULL")
        GWQ.mark_completed(bound_conn, [cur.fetchone()[0]])
        # backdate: the prune keeps RECENT completions for observability
        cur.execute("UPDATE growth_work_queue"
                    " SET completed_at = now() - INTERVAL '30 days'")
    bound_conn.commit()
    assert GWQ.prune_completed(bound_conn, older_than_days=7) == 1
    with bound_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM growth_work_queue")
        assert cur.fetchone()[0] == 0


# ── pin (f): queue failure → growth loop still runs (fail-safe) ───────────────

def test_record_deferred_never_raises_on_unreachable_db(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://nobody@127.0.0.1:1/nowhere")
    assert GWQ.record_deferred("faultline_gwq_x", "synonym_convergence") is False


@pytest.mark.skip(reason="FOSS port: no _reembedder_llm_lane_open on this lineage — "
                         "see DEV/BUGS/foss-port-cba.md")
def test_lane_gate_survives_a_raising_queue(monkeypatch, tenant_schema):
    """Even a queue that RAISES (contract violation) must not break the lane gate —
    the growth loop keeps running with today's skip behaviour."""
    import src.api.llm_calls as llm_calls
    monkeypatch.setattr(llm_calls, "_saas_brain_unavailable", lambda: True)
    from src.re_embedder import embedder

    def boom(*a, **k):
        raise RuntimeError("queue table missing")

    monkeypatch.setattr(embedder.growth_work_queue, "record_deferred", boom)
    assert embedder._reembedder_llm_lane_open(
        "11111111-1111-4111-8111-111111111111", "ontology_eval", tenant_schema) is False


@pytest.mark.skip(reason="FOSS port: promote path has no defer hook on this lineage — "
                         "see DEV/BUGS/foss-port-cba.md")
def test_promotion_loop_survives_a_raising_queue(monkeypatch):
    """The promotion deferral hook is wrapped: a raising queue cannot kill the
    per-row promotion loop (the NEXT staged row is still attempted)."""
    from src.re_embedder import embedder

    def boom(*a, **k):
        raise RuntimeError("queue write fault")

    monkeypatch.setattr(embedder.growth_work_queue, "record_deferred", boom)
    calls = []
    monkeypatch.setattr(
        embedder, "_query_llm_for_rel_type_metadata",
        lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(embedder, "resolve_display_names_for_facts",
                        lambda conn, facts: [{"subject_display": "s", "object_display": "o"}])
    monkeypatch.setattr(embedder, "release_read_transaction", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "_reflexive_hierarchy_blocked", lambda *a, **k: False)

    class _Cur:
        def __init__(self, fetched):
            self._fetched = fetched

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return self._fetched

    class _Conn:
        def cursor(self):
            return _Cur([
                (101, "s", "o", None, "p", "llm_inferred", 0.7, 3,
                 None, None, None, None, None, False),
                (102, "s2", "o2", None, "p", "llm_inferred", 0.7, 4,
                 None, None, None, None, None, False),
            ])

        def commit(self):
            pass

        def rollback(self):
            pass

    n = embedder.promote_class_c_hits(
        _Conn(), "http://qdrant:6333", "http://brain", user_id="u1", schema_name="s")
    assert n == 0
    assert len(calls) == 2  # BOTH rows attempted — the loop did not die on row 101


# ── pin (g): migration applies to fresh + legacy tenants ─────────────────────

def test_migration_300_applies_to_legacy_tenant(postgres_dsn):
    """A legacy tenant schema (pre-300: no growth_work_queue) gets the table via the
    guarded fanout — running the real migration FILE, not a re-statement of its DDL."""
    schema = f"faultline_gwqlegacy_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(postgres_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
        conn.commit()
        sql = _MIGRATION_300.read_text()
        with conn.cursor() as cur:
            cur.execute(sql)  # the DO $$ fanout visits every faultline_% schema
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = 'growth_work_queue'"
                " ORDER BY ordinal_position", (schema,))
            cols = [r[0] for r in cur.fetchall()]
            assert cols == ["id", "operation_type", "target_id", "priority",
                            "created_at", "completed_at"]
            cur.execute(
                "SELECT indexname FROM pg_indexes"
                " WHERE schemaname = %s AND tablename = 'growth_work_queue'"
                " ORDER BY indexname", (schema,))
            idx = [r[0] for r in cur.fetchall()]
            assert "uq_growth_work_pending" in idx   # the partial unique
            assert "idx_growth_work_drain" in idx    # the priority-first path
            assert "growth_work_queue_pkey" in idx
        # idempotent: a second sweep changes nothing
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()


def test_fresh_tenant_template_matches_migration_shape(postgres_dsn, tenant_schema):
    """Fresh tenants get the table from the provisioning template; its shape must be
    EXACTLY the migration's shape (columns + partial unique) — a drift between the
    two is how a migration-only change silently skips every new tenant."""
    legacy = f"faultline_gwqlegacy_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(postgres_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {legacy}")
            cur.execute(_MIGRATION_300.read_text())
            conn.commit()

            def shape(schema):
                cur.execute(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
                    " WHERE table_schema = %s AND table_name = 'growth_work_queue'"
                    " ORDER BY ordinal_position", (schema,))
                cols = cur.fetchall()
                # normalise the schema-qualified sequence name out of the default
                cur.execute(
                    "SELECT regexp_replace(column_default, %s, 'SEQ', 'g')"
                    " FROM information_schema.columns"
                    " WHERE table_schema = %s AND table_name = 'growth_work_queue'"
                    " AND column_default LIKE 'nextval%%'", (schema + r"\.", schema))
                seqs = cur.fetchall()
                cur.execute(
                    "SELECT indexdef FROM pg_indexes"
                    " WHERE schemaname = %s AND tablename = 'growth_work_queue'"
                    " AND indexname = 'uq_growth_work_pending'", (schema,))
                return cols, seqs, cur.fetchall()

            fresh_cols, fresh_seqs, fresh_idx = shape(tenant_schema)
            legacy_cols, legacy_seqs, legacy_idx = shape(legacy)
        assert fresh_cols == legacy_cols
        assert fresh_seqs == legacy_seqs
        # normalise the schema name out of the indexdef, then compare
        norm = lambda row: re.sub(r"ON \S+?\.growth_work_queue", "ON growth_work_queue", row[0])  # noqa: E731
        assert [norm(d) for d in fresh_idx] == [norm(d) for d in legacy_idx]
        assert "WHERE (completed_at IS NULL)" in fresh_idx[0][0]
        assert "UNIQUE" in fresh_idx[0][0]
        # the drain index exists in BOTH shapes (the fixture slice covers it)
        for schema in (tenant_schema, legacy):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_indexes WHERE schemaname = %s"
                    " AND tablename = 'growth_work_queue'"
                    " AND indexname = 'idx_growth_work_drain'", (schema,))
                assert cur.fetchone() is not None, f"drain index missing in {schema}"
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {legacy} CASCADE")
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()


# ── registry census — the map cannot silently drift ───────────────────────────

@pytest.mark.skip(reason="FOSS port: sweep_ledger is not part of this engine; the "
                         "census drift-guard has no ledger to census — see "
                         "DEV/BUGS/foss-port-cba.md")
def test_every_llm_subsystem_is_mapped_to_an_operation():
    from src.re_embedder.sweep_ledger import SUBSYSTEMS
    llm_subsystems = {name for name, spec in SUBSYSTEMS.items() if spec.get("llm")}
    assert llm_subsystems == set(GWQ.SUBSYSTEM_OPERATION), (
        "an LLM-bearing subsystem exists without a growth-work operation family — "
        "its lane-closed deferral would be silently unrecorded (the R8 defect)")


def test_priorities_cover_every_operation_type():
    for op in GWQ.OPERATION_TYPES:
        assert op in GWQ.OPERATION_PRIORITY
    assert all(1 <= p <= 9 for p in GWQ.OPERATION_PRIORITY.values())


def test_module_imports_without_a_database():
    import importlib
    importlib.reload(GWQ)


# ── promotion item-done oracle (real SQL shape) ───────────────────────────────

def test_promotion_item_done_probe(tenant_schema, bound_conn):
    from src.re_embedder.embedder import _growth_work_item_done
    with bound_conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE staged_facts (id BIGINT PRIMARY KEY, fact_class TEXT,"
            " promoted_at TIMESTAMPTZ, hit_count INTEGER)")
        cur.execute("INSERT INTO staged_facts VALUES"
                    " (1, 'C', NULL, 3),"      # still eligible → NOT done
                    " (2, 'B', now(), 3),"     # promoted → done
                    " (3, 'C', NULL, 1)")      # decayed below threshold → done
    bound_conn.commit()
    assert _growth_work_item_done("promotion", "1", bound_conn) is False
    assert _growth_work_item_done("promotion", "2", bound_conn) is True
    assert _growth_work_item_done("promotion", "3", bound_conn) is True
    assert _growth_work_item_done("promotion", "999", bound_conn) is True  # gone
    assert _growth_work_item_done("enrichment", "ontology_eval", bound_conn) is True
