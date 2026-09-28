"""Pins for the per-seat sweep work ledger (`src/re_embedder/sweep_ledger.py`).

These tests pin the INVARIANTS, not the implementation. Each one names the failure it prevents,
because a green suite that does not execute the path it claims to cover is worth nothing (see
the "A GREEN SUITE IS NOT EVIDENCE" section of CLAUDE.md — four ways the tests lied in one day).

The DB-touching behaviour is proven separately and end-to-end in the internal design record,
which drives the REAL subsystem functions against a REAL Postgres and counts REAL LLM calls.
What is pinned HERE is the decision logic, the fail-safe direction, and the flag-off contract.
"""
import importlib
import os

import pytest

import src.re_embedder.sweep_ledger as SL


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("REEMBED_SWEEP_SKIP", "REEMBED_SWEEP_MARK", "REEMBED_SWEEP_MAX_INTERVAL",
              "REEMBED_SWEEP_MAX_JITTER", "REEMBED_SWEEP_SNAPSHOT_CACHE", "REEMBED_INTERVAL"):
        monkeypatch.delenv(k, raising=False)
    yield


def _snap(due):
    return SL.Snapshot(due)


# ──────────────────────────────────────────────────────────────────────────────
# INVARIANT 1 — FAIL-SAFE DIRECTION IS "RUN"
# ──────────────────────────────────────────────────────────────────────────────
def test_flag_off_every_subsystem_runs():
    """FLAG-OFF IS BYTE-FOR-BYTE LEGACY. If this goes red the feature is no longer safely
    rollback-able, which is the one property that makes shipping it reasonable at all."""
    assert SL.enabled() is False
    snap = _snap({})           # an empty ledger: nothing is due for anyone
    for name in SL.SUBSYSTEMS:
        assert SL.claim(snap, "seat-a", name) is True


def test_snapshot_with_flag_off_is_the_always_run_sentinel():
    s = SL.snapshot("postgresql://nowhere/db", ["seat-a"])
    assert s.always_run is True and s.reason == "flag_off"
    assert s.seat_has_work("seat-a") is True


def test_unreachable_database_runs_everything(monkeypatch):
    """A DB blip or a MISSING TABLE (nobody ran migration 207 yet) must not silence the sweep.
    Skipping work that was needed is a correctness bug; running work that was not is waste."""
    monkeypatch.setenv("REEMBED_SWEEP_SKIP", "true")
    s = SL.snapshot("postgresql://user:pw@127.0.0.1:1/nope?connect_timeout=1", ["seat-a"])
    assert s.always_run is True
    assert s.reason.startswith("snapshot_failed")
    assert all(SL.claim(s, "seat-a", n) for n in SL.SUBSYSTEMS)


def test_unknown_subsystem_runs(monkeypatch):
    monkeypatch.setenv("REEMBED_SWEEP_SKIP", "true")
    assert SL.claim(_snap({}), "seat-a", "a_subsystem_nobody_registered") is True


def test_seat_with_no_ledger_row_is_due(monkeypatch):
    """A seat first seen after a deploy, or after someone TRUNCATEd the ledger, must run once.
    That is the 'runs once, harmlessly' answer to a wiped coordination store."""
    monkeypatch.setenv("REEMBED_SWEEP_SKIP", "true")
    snap = _snap({})
    assert snap.seat_has_work("brand-new-seat") is False   # nothing recorded as due...
    # ...but a real snapshot() over a seat with no rows marks every subsystem due; that path is
    # exercised in the e2e proof. Here we pin the shape snapshot() produces for that case:
    due = {"brand-new-seat": {n: 0 for n in SL.SUBSYSTEMS}}
    assert _snap(due).seat_has_work("brand-new-seat") is True


# ──────────────────────────────────────────────────────────────────────────────
# INVARIANT 4 — EXCLUSIONS ARE UNCONDITIONAL
# ──────────────────────────────────────────────────────────────────────────────
def test_excluded_subsystems_always_run_even_with_the_flag_on(monkeypatch):
    """These are the passes that legitimately make progress on IDENTICAL input — decay windows,
    retry queues, age-floored backfills. A token would be permanently clean while real work
    piled up behind it. If someone adds one of these to a guarded call site by mistake, claim()
    still lets it through."""
    monkeypatch.setenv("REEMBED_SWEEP_SKIP", "true")
    snap = _snap({})                     # nothing due for anyone
    for name in SL.EXCLUDED:
        assert SL.claim(snap, "seat-a", name) is True, f"{name} must never be token-skipped"


def test_excluded_and_guarded_sets_are_disjoint():
    """A name in both would be ambiguous — and the EXCLUDED branch wins, silently disabling a
    guard someone believed they had."""
    assert not (set(SL.SUBSYSTEMS) & set(SL.EXCLUDED))


def test_every_exclusion_states_a_reason():
    """The brief requires exclusions to be explicit AND justified. An empty reason means the
    next reader cannot tell a deliberate exclusion from an oversight."""
    for name, reason in SL.EXCLUDED.items():
        assert len(reason) > 40, f"{name} exclusion needs a real reason, got {reason!r}"


def test_the_time_based_passes_are_all_excluded():
    """Named explicitly so a future refactor cannot quietly start gating them."""
    for name in ("decay_ontology_candidates", "expire_staged_facts", "decay_class_c_hits",
                 "reextract_episodic", "drain_pending_documents", "promote_staged_facts",
                 "promote_class_c_hits", "reconcile_qdrant"):
        assert name in SL.EXCLUDED


# ──────────────────────────────────────────────────────────────────────────────
# THE DECISION — dirty / clean / max-run bound
# ──────────────────────────────────────────────────────────────────────────────
def test_clean_row_inside_the_interval_is_skipped(monkeypatch):
    monkeypatch.setenv("REEMBED_SWEEP_SKIP", "true")
    snap = _snap({})                      # snapshot() emits nothing for a clean, fresh row
    assert SL.claim(snap, "seat-a", "ontology_eval") is False
    assert snap.seat_has_work("seat-a") is False


def test_dirty_row_is_due_and_carries_the_observed_token(monkeypatch):
    monkeypatch.setenv("REEMBED_SWEEP_SKIP", "true")
    snap = _snap({"seat-a": {"ontology_eval": 7}})
    assert SL.claim(snap, "seat-a", "ontology_eval") is True
    assert snap.observed_token("seat-a", "ontology_eval") == 7
    # ...and ONLY that subsystem, on ONLY that seat.
    assert SL.claim(snap, "seat-a", "classify_climb") is False
    assert SL.claim(snap, "seat-b", "ontology_eval") is False


def test_max_interval_default_and_override(monkeypatch):
    assert SL.max_interval_seconds() == 3600
    monkeypatch.setenv("REEMBED_SWEEP_MAX_INTERVAL", "120")
    assert SL.max_interval_seconds() == 120
    monkeypatch.setenv("REEMBED_SWEEP_MAX_INTERVAL", "not-a-number")
    assert SL.max_interval_seconds() == 3600      # garbage → the safe default, never 0


def test_max_interval_jitter_is_deterministic_and_bounded(monkeypatch):
    """A seat's max-run slot must be STABLE across restarts (otherwise the bound is a random
    walk) and SPREAD across seats (otherwise 5,000 seats wake in the same cycle and hammer one
    brain — the self-inflicted over-subscription that opens the breaker for everyone)."""
    monkeypatch.setenv("REEMBED_SWEEP_MAX_INTERVAL", "3600")
    monkeypatch.setenv("REEMBED_SWEEP_MAX_JITTER", "0.25")
    a1 = SL._effective_max_interval("seat-a", "ontology_eval")
    a2 = SL._effective_max_interval("seat-a", "ontology_eval")
    assert a1 == a2                                   # deterministic
    assert 2700 <= a1 <= 4500                         # within ±25%
    spread = {SL._effective_max_interval(f"seat-{i}", "ontology_eval") for i in range(50)}
    assert len(spread) > 20, "jitter is not spreading seats — thundering-herd risk"


def test_jitter_can_be_disabled():
    os.environ["REEMBED_SWEEP_MAX_JITTER"] = "0"
    try:
        assert SL._effective_max_interval("seat-a", "ontology_eval") == 3600
    finally:
        del os.environ["REEMBED_SWEEP_MAX_JITTER"]


# ──────────────────────────────────────────────────────────────────────────────
# THE WRITER HALF — table → subsystem fan-out
# ──────────────────────────────────────────────────────────────────────────────
def test_table_map_is_derived_from_the_registry_and_cannot_drift():
    for name, spec in SL.SUBSYSTEMS.items():
        for table in spec["tables"]:
            assert name in SL.TABLE_SUBSYSTEMS[table], (
                f"{name} reads {table} but the fan-out map does not route it there")


def test_ingest_bundle_wakes_the_llm_heavy_ontology_subsystems():
    """THE MISSED-WRITER RISK, pinned. If a future edit narrows INGEST_TABLES and drops one of
    these, that subsystem stops waking on an ingest and only runs on the max-run bound — the
    exact silent-staleness failure this design has to be defended against."""
    woken = set(SL.subsystems_for_tables(*SL.INGEST_TABLES))
    for name in ("ontology_eval", "whatis_classify", "classify_climb", "synonym_convergence",
                 "head_tail_sweep", "orphan_stub_and_nl_fill", "taxonomy_discovery",
                 "pending_placement_drain", "rung6_convergence", "cue_class_growth",
                 "name_conflicts", "suspect_preferred_names"):
        assert name in woken, f"an ingest no longer wakes {name}"


def test_retract_bundle_wakes_the_retraction_and_fact_consumers():
    woken = set(SL.subsystems_for_tables(*SL.RETRACT_TABLES))
    assert {"retraction_outcomes", "correction_eval", "synonym_convergence",
            "classify_climb"} <= woken


def test_a_single_table_wakes_only_its_consumers():
    """The whole point of per-subsystem tokens: one table's write must not wake all sixteen."""
    woken = set(SL.subsystems_for_tables("retraction_outcomes"))
    assert woken == {"retraction_outcomes"}
    assert len(woken) < len(SL.SUBSYSTEMS)


def test_marking_is_independently_gated_and_defaults_on(monkeypatch):
    """Marking and skipping are separate levers so an operator can populate the ledger and watch
    the tokens move BEFORE enabling any skip."""
    assert SL.marking_enabled() is True
    monkeypatch.setenv("REEMBED_SWEEP_MARK", "false")
    assert SL.marking_enabled() is False
    assert SL.mark_dirty_conn(object(), "seat-a", "facts") is False


def test_mark_never_raises_on_a_broken_connection():
    """A coordination optimisation must never be able to fail a user's ingest."""
    class Boom:
        def cursor(self):
            raise RuntimeError("connection is gone")
    assert SL.mark_dirty_conn(Boom(), "seat-a", "facts") is False
    assert SL.mark_dirty_schema_conn(Boom(), "faultline_x", "facts") is False
    assert SL.mark_dirty_dsn("postgresql://user:pw@127.0.0.1:1/nope?connect_timeout=1",
                             "seat-a", "facts") is False


def test_mark_with_no_recognised_table_is_a_noop():
    assert SL.mark_dirty_conn(object(), "seat-a", "a_table_no_subsystem_reads") is False


def test_record_run_is_a_noop_when_disabled():
    assert SL.record_run(object(), "seat-a", "ontology_eval", 3) is False


# ──────────────────────────────────────────────────────────────────────────────
# REGISTRY HYGIENE
# ──────────────────────────────────────────────────────────────────────────────
def test_every_subsystem_declares_tables_a_reason_and_an_audit_probe():
    """`tables` is the routing key (a missing one = a subsystem that never wakes), `why` is how
    the next reader checks the routing is right, and `audit_sql` is the MISSED-WRITER DETECTOR —
    without it the one real risk of a dirty-flag design is unobservable."""
    for name, spec in SL.SUBSYSTEMS.items():
        assert spec.get("tables"), f"{name} declares no input tables"
        assert len(spec.get("why", "")) > 40, f"{name} needs a real justification"
        assert spec.get("audit_sql"), f"{name} has no missed-writer audit probe"
        assert isinstance(spec.get("llm"), bool), f"{name} must declare whether it calls an LLM"


def test_the_measured_llm_offenders_are_all_guarded():
    """The four lanes behind the measured 153-calls-in-20-minutes / 12-all-failing numbers."""
    for name in ("head_tail_sweep", "whatis_classify", "synonym_convergence",
                 "orphan_stub_and_nl_fill", "ontology_eval", "classify_climb"):
        assert SL.SUBSYSTEMS[name]["llm"] is True


def test_the_log_volume_offender_is_guarded():
    """5,104 `re_embedder.suspect_preferred_name` WARNING lines in 20 minutes on prod
    (~370k/day) — an identical census re-emitted every cycle, destroying the log ring that the
    next incident will need."""
    assert "suspect_preferred_names" in SL.SUBSYSTEMS
    assert SL.SUBSYSTEMS["suspect_preferred_names"]["tables"] == ("entity_aliases",)


def test_module_imports_without_a_database_or_redis():
    """The sweep loop imports this at module scope; an import that needs a live dependency would
    turn a coordination feature into a startup failure."""
    importlib.reload(SL)
    assert SL.SUBSYSTEMS and SL.EXCLUDED
