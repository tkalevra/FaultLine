"""Unit tests for the event-driven per-tenant re-embedder supervisor (Deliverable C).

Fast, infrastructure-free: no real Postgres / Redis / LLM. The comparator is driven off a
faked psycopg2-style connection/cursor; the worker's per-tenant WORK functions are mocked on
``embedder`` so call-patterns (not real DB writes) are asserted; the yield gate is driven by
monkeypatching the phase-level yield verdict ``supervisor._brain_busy`` (the open core's
redis_coord offers no non-consuming budget peek, so the verdict itself is the seam).

Covers the five bars from the task:
  1. COMPARATOR honesty — identical signals ⇒ "no work"; any one field changed ⇒ "work".
  2. IDLE ⇒ no work call — an unchanged signal across two passes calls ZERO per-tenant work
     functions on the second pass.
  3. YIELD gate — a near-empty brain bucket defers the LLM-bearing phase but the non-LLM
     phases still run.
  4. FAULT ISOLATION — a worker whose per-tenant function raises is logged and the OTHER
     tenant's worker still completes a pass.
  5. OPT-IN default — ``REEMBEDDER_SUPERVISOR`` unset → the legacy ``main()`` path is selected.
"""

import asyncio

import pytest

from src.re_embedder import embedder, supervisor
from src.api import redis_coord


# ── Fake DB plumbing for the comparator ────────────────────────────────────────────────

class SignalCursor:
    """Classifies a comparator SELECT by its FROM-clause and returns a canned row."""

    def __init__(self, rows):
        self._rows = rows
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = sql.lower()
        self._result = None
        if s.startswith("set search_path"):
            return
        for fragment, row in self._rows.items():
            if fragment in s:
                self._result = row
                return

    def fetchone(self):
        return self._result


class SignalConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return SignalCursor(self._rows)

    def commit(self):
        pass

    def rollback(self):
        pass


# ── 1. COMPARATOR honesty ──────────────────────────────────────────────────────────────

def test_comparator_returns_the_six_tuple_with_documents_pending():
    rows = {
        "from staged_facts": (5, "2026-01-01 00:00:00"),
        "from episodic_log": (2,),
        "from ontology_evaluations": (1,),
        "from entity_name_conflicts": (0,),
        "to_regclass('documents')": ("documents",),   # the documents table EXISTS for this tenant
        "from documents": (3,),                        # 3 pending documents
    }
    sig = supervisor.tenant_work_signal(SignalConn(rows), "faultline_demo")
    assert sig == (5, "2026-01-01 00:00:00", 2, 1, 0, 3)


def test_comparator_documents_absent_reads_zero_pending():
    """A tenant with NO documents table reads 0 pending (not an error that forces 'run')."""
    rows = {
        "from staged_facts": (5, "t1"),
        "from episodic_log": (2,),
        "from ontology_evaluations": (1,),
        "from entity_name_conflicts": (0,),
        # NOTE: no to_regclass('documents') entry → fetchone() returns None → docs_pending = 0
    }
    sig = supervisor.tenant_work_signal(SignalConn(rows), "sch")
    assert sig == (5, "t1", 2, 1, 0, 0)


def test_comparator_identical_signals_are_equal():
    rows = {
        "from staged_facts": (5, "t1"),
        "from episodic_log": (2,),
        "from ontology_evaluations": (1,),
        "from entity_name_conflicts": (0,),
        "to_regclass('documents')": ("documents",),
        "from documents": (0,),
    }
    a = supervisor.tenant_work_signal(SignalConn(rows), "sch")
    b = supervisor.tenant_work_signal(SignalConn(rows), "sch")
    assert a == b, "identical DB state ⇒ identical signal ⇒ 'no work'"


@pytest.mark.parametrize(
    "fragment,new_row",
    [
        ("from staged_facts", (6, "t1")),          # staged count changed
        ("from staged_facts", (5, "t2")),          # staged max(updated_at) changed
        ("from episodic_log", (3,)),               # episodic pending changed
        ("from ontology_evaluations", (2,)),       # ontology pending changed
        ("from entity_name_conflicts", (1,)),      # name-conflict pending changed
        ("from documents", (4,)),                  # documents pending changed (critic C D1)
    ],
)
def test_comparator_any_one_field_changed_is_work(fragment, new_row):
    base = {
        "from staged_facts": (5, "t1"),
        "from episodic_log": (2,),
        "from ontology_evaluations": (1,),
        "from entity_name_conflicts": (0,),
        "to_regclass('documents')": ("documents",),
        "from documents": (0,),
    }
    before = supervisor.tenant_work_signal(SignalConn(base), "sch")
    changed = dict(base, **{fragment: new_row})
    after = supervisor.tenant_work_signal(SignalConn(changed), "sch")
    assert after != before, f"changing {fragment} must flip the signal ⇒ 'work'"


def test_comparator_probe_error_is_fail_safe_to_run():
    class BoomConn:
        def cursor(self):
            raise RuntimeError("admin connection is dead")

        def commit(self):
            pass

        def rollback(self):
            pass

    sig = supervisor.tenant_work_signal(BoomConn(), "faultline_broken")
    # Fail-safe sentinel: never equal to a real 6-tuple ⇒ always treated as "changed" ⇒ work runs.
    assert sig != (0, "epoch", 0, 0, 0, 0)
    assert sig == supervisor._SIGNAL_ERROR


# ── 2. IDLE ⇒ no work call ─────────────────────────────────────────────────────────────

def test_idle_tenant_calls_no_work_on_the_second_pass(monkeypatch):
    monkeypatch.setenv("REEMBEDDER_IDLE_S", "0")  # no real waiting in the test
    monkeypatch.setenv("REEMBEDDER_IDLE_MAX_S", "0")

    sig = (5, "t1", 2, 1, 0)
    work_calls = []

    def fake_signal(_dsn, _schema):
        return sig

    def fake_work(**kwargs):
        work_calls.append(kwargs.get("schema_name"))

    async def driver():
        await supervisor._tenant_worker(
            "uid", "faultline_idle",
            postgres_dsn="dsn", backend_api_url="http://x", qdrant_url="http://q",
            qwen_api_url="http://qw", reextract_enabled=True, ingest_enabled=True,
            work_lock=asyncio.Lock(),
            signal_fn=fake_signal,
            work_fn=fake_work,
            _max_passes=2,
        )

    asyncio.run(driver())

    # Pass 1: last_signal was None ⇒ "changed" ⇒ work runs once (discovers state).
    # Pass 2: signal unchanged ⇒ idle ⇒ ZERO work calls.
    assert work_calls == ["faultline_idle"], (
        "the unchanged second pass must call NO per-tenant work function"
    )


# ── 3. YIELD gate ──────────────────────────────────────────────────────────────────────

def test_yield_gate_defers_llm_phase_but_runs_non_llm(monkeypatch):
    # Brain bucket below the floor ⇒ the brain is busy (a user turn is likely in flight).
    monkeypatch.setattr(supervisor, "_brain_busy", lambda scope: True)

    ran = []

    def _track(name):
        return lambda *a, **k: ran.append(name)

    monkeypatch.setattr(embedder, "promote_staged_facts", _track("promote_staged"))
    monkeypatch.setattr(embedder, "expire_staged_facts", _track("expire_staged"))
    monkeypatch.setattr(embedder, "reextract_episodic", _track("reextract_episodic"))
    monkeypatch.setattr(embedder, "drain_pending_documents", _track("drain_documents"))
    monkeypatch.setattr(embedder, "promote_class_c_hits", _track("promote_class_c"))
    monkeypatch.setattr(embedder, "decay_class_c_hits", _track("decay_class_c"))
    monkeypatch.setattr(embedder, "evaluate_ontology_candidates", _track("ontology_eval"))
    monkeypatch.setattr(embedder, "resolve_name_conflicts", _track("name_conflicts"))
    monkeypatch.setattr(embedder, "_reconcile_hierarchy_links", _track("hierarchy_reconcile"))
    monkeypatch.setattr(embedder, "_upgrade_staged_facts_with_known_rels", _track("staged_rel_upgrade"))

    class NoOpConn:
        def cursor(self):
            return SignalCursor({})

        def commit(self):
            pass

    supervisor._run_phases_on_conn(
        NoOpConn(),
        user_id="uid", schema_name="faultline_busy",
        postgres_dsn="dsn", backend_api_url="http://x", qdrant_url="http://q",
        qwen_api_url="http://qw", statement_route="rewrite",
        brain_scope=redis_coord.scope_single_deployment(),
        ingest_enabled=True, reextract_enabled=True,
    )

    llm_phases = {"reextract_episodic", "drain_documents", "ontology_eval", "name_conflicts"}
    non_llm_phases = {
        "promote_staged", "expire_staged", "promote_class_c", "decay_class_c",
        "hierarchy_reconcile", "staged_rel_upgrade",
    }
    assert set(ran) == non_llm_phases, (
        f"non-LLM phases must all run; got {sorted(ran)}"
    )
    assert ran.count("promote_staged") == 1
    assert not (set(ran) & llm_phases), (
        f"LLM-bearing phases must be DEFERRED when the brain bucket is low; ran {sorted(ran)}"
    )


def test_yield_gate_idle_brain_runs_all_phases(monkeypatch):
    # Plenty of budget ⇒ nothing deferred ⇒ every phase runs (LLM ones included).
    monkeypatch.setattr(supervisor, "_brain_busy", lambda scope: False)

    ran = []
    for name in (
        "promote_staged_facts", "expire_staged_facts", "reextract_episodic",
        "drain_pending_documents", "promote_class_c_hits", "decay_class_c_hits",
        "evaluate_ontology_candidates", "resolve_name_conflicts",
        "_reconcile_hierarchy_links", "_upgrade_staged_facts_with_known_rels",
    ):
        monkeypatch.setattr(embedder, name, lambda *a, _n=name, **k: ran.append(_n))

    class NoOpConn:
        def cursor(self):
            return SignalCursor({})

        def commit(self):
            pass

    supervisor._run_phases_on_conn(
        NoOpConn(),
        user_id="uid", schema_name="faultline_calm",
        postgres_dsn="dsn", backend_api_url="http://x", qdrant_url="http://q",
        qwen_api_url="http://qw", statement_route="rewrite",
        brain_scope=redis_coord.scope_single_deployment(),
        ingest_enabled=True, reextract_enabled=True,
    )
    assert "reextract_episodic" in ran and "evaluate_ontology_candidates" in ran, (
        "with a healthy brain bucket the LLM-bearing phases must NOT be deferred"
    )


def test_yield_gate_none_scope_never_defers(monkeypatch):
    # No phase-level scope (the open core default): brain_scope is None ⇒ no shared budget ⇒ never yield ⇒ LLM phase runs.
    called = {"reextract": False}
    monkeypatch.setattr(embedder, "promote_staged_facts", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "expire_staged_facts", lambda *a, **k: None)
    monkeypatch.setattr(
        embedder, "reextract_episodic",
        lambda *a, **k: called.__setitem__("reextract", True),
    )
    for name in (
        "drain_pending_documents", "promote_class_c_hits", "decay_class_c_hits",
        "evaluate_ontology_candidates", "resolve_name_conflicts",
        "_reconcile_hierarchy_links", "_upgrade_staged_facts_with_known_rels",
    ):
        monkeypatch.setattr(embedder, name, lambda *a, **k: None)

    class NoOpConn:
        def cursor(self):
            return SignalCursor({})

        def commit(self):
            pass

    supervisor._run_phases_on_conn(
        NoOpConn(),
        user_id="uid", schema_name="faultline_foss",
        postgres_dsn="dsn", backend_api_url="http://x", qdrant_url="http://q",
        qwen_api_url="http://qw", statement_route="rewrite",
        brain_scope=None,
        ingest_enabled=True, reextract_enabled=True,
    )
    assert called["reextract"] is True


# ── 4. FAULT ISOLATION ─────────────────────────────────────────────────────────────────

def test_one_phase_raising_does_not_abort_the_cycle(monkeypatch):
    monkeypatch.setattr(supervisor, "_brain_busy", lambda scope: False)
    order = []

    def boom(*a, **k):
        order.append("promote_class_c_RAISED")
        raise RuntimeError("simulate a wedged Class-C promotion")

    monkeypatch.setattr(embedder, "promote_staged_facts", lambda *a, **k: order.append("promote_staged"))
    monkeypatch.setattr(embedder, "expire_staged_facts", lambda *a, **k: order.append("expire_staged"))
    monkeypatch.setattr(embedder, "reextract_episodic", lambda *a, **k: order.append("reextract"))
    monkeypatch.setattr(embedder, "drain_pending_documents", lambda *a, **k: order.append("drain"))
    monkeypatch.setattr(embedder, "promote_class_c_hits", boom)
    monkeypatch.setattr(embedder, "decay_class_c_hits", lambda *a, **k: order.append("decay_class_c"))
    monkeypatch.setattr(embedder, "evaluate_ontology_candidates", lambda *a, **k: order.append("ontology"))
    monkeypatch.setattr(embedder, "resolve_name_conflicts", lambda *a, **k: order.append("name_conflicts"))
    monkeypatch.setattr(embedder, "_reconcile_hierarchy_links", lambda *a, **k: order.append("hierarchy"))
    monkeypatch.setattr(embedder, "_upgrade_staged_facts_with_known_rels", lambda *a, **k: order.append("upgrade"))

    class NoOpConn:
        def cursor(self):
            return SignalCursor({})

        def commit(self):
            pass

    supervisor._run_phases_on_conn(
        NoOpConn(),
        user_id="uid", schema_name="faultline_flaky",
        postgres_dsn="dsn", backend_api_url="http://x", qdrant_url="http://q",
        qwen_api_url="http://qw", statement_route="rewrite",
        brain_scope=redis_coord.scope_single_deployment(),
        ingest_enabled=True, reextract_enabled=True,
    )
    # The phase BEFORE the raise ran, the raising phase is recorded, and phases AFTER it run.
    assert "promote_staged" in order
    assert "promote_class_c_RAISED" in order
    assert "ontology" in order and "upgrade" in order, (
        "a phase raising must be isolated; later phases still run"
    )


def test_supervisor_keeps_other_tenant_alive_when_one_raises(monkeypatch):
    """Two tenants: A's work always raises, B's work records. The supervisor must keep B's
    worker alive so B still completes a pass (each tenant is its own task)."""
    # Avoid any real HTTP for the per-pass route probe so the test is network-free and the
    # supervise window is not eaten by a connection attempt.
    monkeypatch.setattr(supervisor, "_resolve_statement_route", lambda url: "rewrite")
    completed = []
    raised = []

    def work_fn(**kwargs):
        schema = kwargs.get("schema_name")
        if schema == "faultline_A":
            raised.append(schema)
            raise RuntimeError("tenant A blows up mid-work")
        completed.append(schema)

    def signal_fn(_dsn, schema):
        # Always "changed" so work runs on pass 1.
        return (f"sig-{schema}-1", "t", 0, 0, 0)

    async def driver():
        await supervisor.run_supervisor(
            postgres_dsn="dsn",
            backend_api_url="http://x",
            qdrant_url="http://q",
            _tenants=[("uA", "faultline_A"), ("uB", "faultline_B")],
            _max_passes=1,
            _supervise_for=4.0,
            _signal_fn=signal_fn,
            _work_fn=work_fn,
        )

    asyncio.run(driver())

    assert "faultline_B" in completed, "the non-raising tenant must complete a pass"
    assert "faultline_A" in raised, "the raising tenant's work ran and raised (isolated, logged)"


# ── 5. OPT-IN default ──────────────────────────────────────────────────────────────────

def test_supervisor_is_off_by_default(monkeypatch):
    monkeypatch.delenv("REEMBEDDER_SUPERVISOR", raising=False)
    assert supervisor.supervisor_enabled() is False, (
        "unset ⇒ legacy main() path selected byte-for-byte"
    )


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "On"])
def test_supervisor_truthy_values_opt_in(monkeypatch, val):
    monkeypatch.setenv("REEMBEDDER_SUPERVISOR", val)
    assert supervisor.supervisor_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "", "anything-else"])
def test_supervisor_non_truthy_values_stay_on_legacy(monkeypatch, val):
    monkeypatch.setenv("REEMBEDDER_SUPERVISOR", val)
    assert supervisor.supervisor_enabled() is False


def test_embedder_main_uses_the_opt_in_gate():
    """The legacy main() path is selected when the flag is off. Assert the branch by reading
    the entry source (never actually run main())."""
    import inspect
    src = inspect.getsource(embedder)
    assert 'REEMBEDDER_SUPERVISOR' in src, "embedder.__main__ must gate on REEMBEDDER_SUPERVISOR"
    assert "run_supervisor" in src, "embedder.__main__ must dispatch to run_supervisor when opted-in"
    # main() is still present AND reachable on the else branch.
    assert "\n    else:\n        main()" in src, (
        "the legacy main() call must remain on the else branch (byte-for-byte when off)"
    )
