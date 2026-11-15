"""BENCHGATE — a benchmark run must not score what it did not measure.

Pins the four gates in `benchmarks/harness/bench_health.py`:
  1. EXCLUSION      — `shape="error"` leaves the DENOMINATOR, with a named reason, and can
                      never flatter a score.
  2. DEGRADATION    — a rolling degraded-work rate halts the run instead of drifting.
  3. LIVENESS       — a REAL COMPLETION is required; NOT CONFIGURED is never reported as OK.
  4. BRAIN IDENTITY — probed, compared start-vs-end, and a mid-run change fails the run.

The exclusion tests are driven by the SHAPE OF THE REAL FAILURES captured on 2026-08-01
(`/home/chris/lme-preprod/seat{0..3}.json`): 80 of 87 questions carried `shape="error"` with
`MCPTransportError … unreachable: timed out`, `[Errno 111]`, and `/recall_memory HTTP 500`, and
every one of them was scored as a capture miss.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "harness"))
import bench_health as bh  # noqa: E402


# ── 1. EXCLUSION ─────────────────────────────────────────────────────────────────────────
# The verbatim `matched_on` strings from the 2026-08-01 artefacts.
_REAL_FAILURES = [
    ("ERROR MCPTransportError: MCP http://192.168.40.10:8002/ingest_document unreachable: timed out",
     "transport_unreachable"),
    ("ERROR MCPTransportError: MCP http://192.168.40.10:8002/ingest_document unreachable: "
     "<urlopen error [Errno 111] Connection refused>", "transport_unreachable"),
    ("ERROR MCPTransportError: MCP http://192.168.40.10:8002/recall_memory HTTP 500: "
     "Internal Server Error", "backend_5xx"),
    ("ERROR ingest_incomplete: 4/12 turns failed transiently after 3 retries", "ingest_incomplete"),
    ("ERROR MCPTransportError: document 41 never reached ready/error/partial within 2700s",
     "doc_lane_stuck"),
]


@pytest.mark.parametrize("matched_on,reason", _REAL_FAILURES)
def test_every_real_infra_failure_is_classified_not_scored(matched_on, reason):
    """THE BUG: the old exclusion test was `matched_on.startswith("ERROR ingest_incomplete")`.

    Four of these five REAL strings do not match that prefix, so four of five infrastructure
    failures were counted as capture misses. Each must now classify to a named reason.
    """
    assert bh.classify_exclusion(matched_on) == reason
    assert bh.classify_exclusion(matched_on) != "unclassified_error"


def test_last_nights_run_reads_n7_excluded80_not_a_confident_zero():
    """Replay of the REAL 2026-08-01 distribution: 87 attempted, 80 errored, 7 measured.

    Published then: `det_n=87 … det_accuracy 0.000`. Required now: n=7, excluded=80, VOID.
    """
    led = bh.ExclusionLedger()
    for i in range(70):
        led.record(f"q{i}", "ERROR MCPTransportError: MCP http://x/ingest_document "
                            "unreachable: timed out")
    for i in range(10):
        led.record(f"r{i}", "ERROR MCPTransportError: MCP http://x/recall_memory HTTP 500: "
                            "Internal Server Error")
    attempted = 87
    assert led.count == 80
    assert attempted - led.count == 7                      # the DENOMINATOR
    assert led.by_reason == {"transport_unreachable": 70, "backend_5xx": 10}
    assert led.verdict(attempted) == "VOID"
    banner = led.banner(attempted)
    assert "MEASUREMENT VOID" in banner and "80/87" in banner and "92.0%" in banner


def test_a_mostly_excluded_run_can_never_look_like_a_good_score():
    """Exclusion must never be a route to a flattering number.

    Structurally it cannot inflate a rate (an excluded question was a would-be MISS and leaves
    numerator AND denominator together), but the stronger guarantee is that a run past the
    budget is not allowed to publish at all: verdict VOID + a non-zero exit code.
    """
    led = bh.ExclusionLedger()
    for i in range(99):
        led.record(f"q{i}", "ERROR MCPTransportError: unreachable: timed out")
    # 99 of 100 excluded, and the single measured question HIT -> a naive rate would read 1.000.
    assert led.verdict(100) == "VOID"
    h = bh.RunHealth()
    h.exclusions = led
    assert h.verdict(100) == "VOID"
    assert h.rollup(100)["trustworthy"] is False
    assert bh.EXIT_MEASUREMENT_VOID != 0


def test_a_clean_run_is_untouched():
    """No exclusions -> verdict OK, no banner, denominator unchanged. Scoring semantics for a
    cleanly-measured question are NOT altered by any of this."""
    led = bh.ExclusionLedger()
    assert led.count == 0
    assert led.verdict(50) == "OK"
    assert led.banner(50) == ""
    h = bh.RunHealth()
    assert h.measured_n(50) == 50


def test_legacy_key_keeps_its_legacy_meaning():
    """`det_excluded_ingest_incomplete` rides in every archived artefact meaning
    'ingest-incomplete only'. It must not silently start meaning 'all exclusions'."""
    led = bh.ExclusionLedger()
    led.record("a", "ERROR ingest_incomplete: 2/9 turns failed")
    led.record("b", "ERROR MCPTransportError: unreachable: timed out")
    assert led.ingest_incomplete == 1
    assert led.count == 2


# ── 2. DEGRADATION BUDGET ────────────────────────────────────────────────────────────────
def test_healthy_baseline_does_not_halt():
    """Measured healthy runs sit at 0.3%-1.8% degraded. The gate must not fire there."""
    b = bh.DegradationBudget(window=20, rate=0.10, min_samples=5)
    for _ in range(20):
        b.add(100, 1)          # 1.0%
    assert b.window_rate() == pytest.approx(0.01)
    assert not b.breached()


def test_breaker_storm_halts():
    """The archive's sick runs measured 8.9%, 15.9%, 21.7%. The gate must fire there."""
    b = bh.DegradationBudget(window=20, rate=0.10, min_samples=5)
    for _ in range(10):
        b.add(100, 22)         # 22%
    assert b.breached()
    assert "HALTING" in b.banner()


def test_never_halts_before_min_samples():
    """A 1/1 = 100% rate on the first question is noise, not a trend."""
    b = bh.DegradationBudget(window=20, rate=0.10, min_samples=5)
    b.add(1, 1)
    assert not b.breached()
    for _ in range(4):
        b.add(1, 1)
    assert b.breached()


def test_window_is_rolling_so_a_recovered_rig_stops_halting():
    b = bh.DegradationBudget(window=5, rate=0.10, min_samples=5)
    for _ in range(5):
        b.add(10, 5)
    assert b.breached()
    for _ in range(5):
        b.add(10, 0)
    assert not b.breached()
    assert b.overall_rate() == pytest.approx(0.25)   # history is still reported honestly


# ── the two envelope readers: VERIFY THE READ ────────────────────────────────────────────
def test_turn_mode_reads_the_backend_extraction_degraded_envelope():
    """TURNS mode genuinely carries `extraction_degraded` (main.py:19914 -> server.py:2604)."""
    assert bh.turn_degradation({"status": "stored", "extraction_degraded": True,
                                "llm_calls_unanswered": 3}) == (1, 1, "extraction_degraded")
    assert bh.turn_degradation({"status": "stored", "committed": 4}) == (1, 0, "")
    assert bh.turn_degradation({"status": "degraded"})[1] == 1
    assert bh.turn_degradation({"status": "stored", "retry_pending": True})[1] == 1


def test_document_mode_reads_the_registry_row_not_a_field_that_never_arrives():
    """DOCUMENT mode CANNOT carry `extraction_degraded`: the lane is async and the HTTP
    response is the `pending` enqueue envelope, returned before extraction runs. The signal is
    `documents.chunks_failed` / `status` off the terminal registry row."""
    enq = {"status": "pending", "document_id": 7, "chunks": 40}
    assert bh.document_degradation({"status": "ready", "chunks_failed": 0}, enq) == (40, 0, "")
    assert bh.document_degradation({"status": "partial", "chunks_failed": 9}, enq) == (
        40, 9, "chunks_failed")
    assert bh.document_degradation({"status": "error", "chunks_failed": 0}, enq) == (
        40, 40, "document_error")
    # Registry poll unavailable -> contribute NO sample. We never claim health we did not
    # observe, and we never fabricate a failure either.
    assert bh.document_degradation(None, {"status": "pending", "document_id": 7}) == (0, 0, "")


def test_a_synchronously_terminal_document_is_read_off_the_response():
    """The doc lane does not always reach the async registry. When `/ingest_document` answers
    synchronously the terminal counts are on the HTTP response and `doc_terminal` is absent."""
    sync = {"status": "partial", "chunks": 30, "chunks_failed": 6}
    assert bh.document_degradation(None, sync) == (30, 6, "chunks_failed")


def test_a_refused_haystack_is_reported_but_never_scored_as_degradation():
    """FOUND IN REAL DATA (`seat0.json`, question `6a1eabeb`): a 486,480-char haystack came
    back `{"status":"rejected", … "Input contains disallowed content"}` — the MCP injection
    guard refused the WHOLE question, nothing was ingested, and it was scored as a capture
    miss. That is a product DECISION, so it is NOT converted into degradation or an exclusion
    (that would change scoring semantics). It is surfaced as a named OBSERVATION so it stops
    being invisible. Whether a refused haystack should be scored at all is an owner call.
    """
    rejected = {"status": "rejected", "chunks": 0,
                "reason": "Input contains disallowed content"}
    assert bh.document_degradation(None, rejected) == (0, 0, "document_rejected")
    b = bh.DegradationBudget()
    b.add(0, 0, "document_rejected")
    assert b.summary()["observations"] == {"document_rejected": 1}
    assert b.summary()["signals"] == {}
    assert b.total_degraded == 0
    assert not b.breached()


# ── 3. LIVENESS ──────────────────────────────────────────────────────────────────────────
def test_liveness_demands_a_real_completion_not_a_socket(monkeypatch):
    """A listening socket is NOT a brain — the whole lesson of the 07-30 outage. /health
    answering while completions return empty must FAIL."""
    monkeypatch.setattr(bh, "BRAIN_URL", "http://brain:8080")
    monkeypatch.setattr(bh, "BRAIN_HOST", "")

    def fake(url, payload=None, timeout=20, headers=None):
        if url.endswith("/health"):
            return {"status": "ok"}                       # socket is up
        if url.endswith("/v1/chat/completions"):
            return {"choices": [{"message": {"content": ""}}]}   # ...and serves NOTHING
        if url.endswith("/internal/ingest-route"):
            return {"statement_extractor": "spine"}
        return {}

    monkeypatch.setattr(bh, "_http_json", fake)
    live = bh.brain_liveness(timeout=1)
    assert live["checks"]["health"]["ok"] is True
    assert live["checks"]["completion"]["ok"] is False
    assert live["ok"] is False


def test_liveness_requires_backend_agreement_on_the_same_poll(monkeypatch):
    monkeypatch.setattr(bh, "BRAIN_URL", "http://brain:8080")
    monkeypatch.setattr(bh, "BRAIN_HOST", "")

    def fake(url, payload=None, timeout=20, headers=None):
        if url.endswith("/internal/ingest-route"):
            raise OSError("connection refused")
        if url.endswith("/v1/chat/completions"):
            return {"choices": [{"message": {"content": "ok"}}]}
        return {"status": "ok"}

    monkeypatch.setattr(bh, "_http_json", fake)
    live = bh.brain_liveness(timeout=1)
    assert live["checks"]["completion"]["ok"] is True
    assert live["checks"]["backend"]["ok"] is False
    assert live["ok"] is False


def test_unconfigured_completion_probe_is_never_reported_as_passing(monkeypatch):
    """NOT CONFIGURED != PASSING. An unprobed brain is an unknown brain and the artefact must
    say so rather than implying the check succeeded."""
    monkeypatch.setattr(bh, "BRAIN_URL", "")
    monkeypatch.setattr(bh, "BRAIN_HOST", "")
    monkeypatch.setattr(bh, "_http_json",
                        lambda *a, **k: {"statement_extractor": "spine"})
    live = bh.brain_liveness(timeout=1)
    assert live["checks"]["completion"]["ok"] is None          # not True
    assert "NOT CONFIGURED" in live["checks"]["completion"]["note"]
    # ...and the fact that it did not run is a first-class flag, carried into every artefact,
    # not something a reader has to notice inside a nested dict.
    assert live["completion_probe_configured"] is False
    assert "BENCH_BRAIN_URL" in bh.UNCONFIGURED_PROBE_WARNING


def test_a_configured_and_answering_brain_marks_the_probe_as_run(monkeypatch):
    monkeypatch.setattr(bh, "BRAIN_URL", "http://brain:8080")
    monkeypatch.setattr(bh, "BRAIN_HOST", "")
    monkeypatch.setattr(bh, "_http_json", lambda url, payload=None, timeout=20, headers=None: (
        {"choices": [{"message": {"content": "ok"}}]} if url.endswith("/v1/chat/completions")
        else {"statement_extractor": "spine", "status": "ok"}))
    live = bh.brain_liveness(timeout=1)
    assert live["completion_probe_configured"] is True
    assert live["checks"]["completion"]["ok"] is True
    assert live["ok"] is True


def test_failed_liveness_halts_the_run():
    h = bh.RunHealth()
    ok = h.note_liveness({"ok": False, "checks": {"completion": {"ok": False}}}, at_question=30)
    assert ok is False
    assert h.halted["reason"] == "brain_liveness_failed"
    assert h.verdict(100) == "VOID"


# ── 4. BRAIN IDENTITY ────────────────────────────────────────────────────────────────────
def test_brain_identity_is_probed_not_asserted(monkeypatch):
    monkeypatch.setattr(bh, "BRAIN_URL", "http://brain:8080")

    def fake(url, payload=None, timeout=20, headers=None):
        if url.endswith("/v1/models"):
            return {"data": [{"id": "qwen3.6-35b"}]}
        if url.endswith("/props"):
            return {"n_ctx": 32768, "total_slots": 4}
        return {}

    monkeypatch.setattr(bh, "_http_json", fake)
    ident = bh.probe_brain_identity(timeout=1)
    assert ident["probed"] is True
    assert ident["models"] == ["qwen3.6-35b"]
    assert ident["n_ctx"] == 32768 and ident["total_slots"] == 4


def test_unprobed_brain_is_never_a_fabricated_identity(monkeypatch):
    monkeypatch.setattr(bh, "BRAIN_URL", "")
    ident = bh.probe_brain_identity(timeout=1)
    assert ident["probed"] is False
    assert ident["models"] == []
    assert "NOT PROBED" in ident["error"]


def test_a_brain_that_changes_mid_run_fails_the_run():
    """A run whose brain changed compared two different products and called the difference a
    result. This is the failure the hardcoded 'aurora' label hid."""
    h = bh.RunHealth()
    h.note_brain_start({"probed": True, "models": ["qwen3.6-35b"], "n_ctx": 32768,
                        "total_slots": 4})
    assert h.note_brain_end({"probed": True, "models": ["deepseek-v4-pro"], "n_ctx": 65536,
                             "total_slots": 1}) is False
    assert h.halted["reason"] == "brain_identity_changed"


def test_a_context_shrink_alone_fails_the_run():
    """n_ctx is in the identity key on purpose: the same model id with a smaller context
    TRUNCATES haystacks, which silently changes what the run measures."""
    h = bh.RunHealth()
    h.note_brain_start({"probed": True, "models": ["m"], "n_ctx": 65536, "total_slots": 4})
    assert h.note_brain_end({"probed": True, "models": ["m"], "n_ctx": 8192,
                             "total_slots": 4}) is False


def test_unprobed_at_either_end_does_not_claim_a_change():
    h = bh.RunHealth()
    h.note_brain_start({"probed": False})
    assert h.note_brain_end({"probed": True, "models": ["m"]}) is True
    assert h.halted is None
    assert h.rollup(10)["brain_identity_stable"] is None      # unknown, not "stable"


# ── the rollup a reader actually reads ───────────────────────────────────────────────────
def test_summary_is_additive_and_carries_the_scorer_ceiling():
    """Keep artefacts backward-readable: existing keys survive untouched; the health block is
    added alongside. And every summary states the scorer's REAL ceiling (~0.68, not 1.0)."""
    h = bh.RunHealth()
    h.exclusions.record("q1", "ERROR MCPTransportError: unreachable: timed out")
    summary = {"n": 10, "correct": 3, "accuracy": 0.3, "det_n": 9, "det_accuracy": 0.333,
               "det_excluded_ingest_incomplete": 0}
    out = h.apply_to_summary(dict(summary), 10)
    for k, v in summary.items():
        assert out[k] == v, f"{k} was mutated — archived readers would break"
    assert out["det_excluded"] == 1
    assert out["det_excluded_reasons"] == {"transport_unreachable": 1}
    assert out["measurement_verdict"] == "DEGRADED"
    assert out["scorer_ceiling"] == pytest.approx(0.68)
    assert "0.68" in out["health"]["scorer_ceiling_note"]
    assert out["health"]["thresholds"]["degrade_halt_rate"] == bh.DEGRADE_HALT_RATE


# ── the headline pair the dashboard renders ──────────────────────────────────────────────
def _import_harness(monkeypatch):
    monkeypatch.setenv("BENCH_USER_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("BENCH_DISPOSABLE_SEAT", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("BENCH_DETERMINISTIC_SCORE", "1")
    monkeypatch.setenv("BENCH_DET_ONLY", "1")
    monkeypatch.setenv("MCP_RECALL_URL", "http://rig:8002/recall_memory")
    # owui_pathway reads FAULTLINE_TOKEN at IMPORT time (module-level os.environ[...]).
    # These tests never touch the network — they exercise _checkpoint/HEALTH only — but the
    # import chain demands the credential regardless. Without this stub the import died with
    # KeyError: FAULTLINE_TOKEN: a harness-env gap, not a behaviour verdict (the pinned
    # summary contracts below are unaffected).
    monkeypatch.setenv("FAULTLINE_TOKEN", "bench-test-token-not-used")
    for m in ("run_longmemeval", "bench_health"):
        sys.modules.pop(m, None)
    import importlib
    return importlib.import_module("run_longmemeval")


def test_n_and_accuracy_must_share_a_denominator(tmp_path, monkeypatch):
    """THE HEADLINE LIE, pinned. The 2026-08-01 artefacts published `n=87` beside
    `accuracy=0.000` for a run that measured SEVEN questions: `accuracy` was already the det
    rate over MEASURED questions while `n` stayed the count ATTEMPTED. The two fields most
    readers look at must describe the same population.
    """
    R = _import_harness(monkeypatch)
    for i in range(80):
        R.HEALTH.exclusions.record(f"q{i}", "ERROR MCPTransportError: unreachable: timed out")
    out = tmp_path / "art.json"
    R._checkpoint(str(out), [], 87, 0, det_correct=0, det_excluded=80)
    import json as _json
    s = _json.load(open(out))["summary"]
    assert s["n"] == 7, "n must be the MEASURED population, not the attempted one"
    assert s["n_attempted"] == 87, "the attempted count must survive, additively"
    assert s["det_n"] == 7
    assert s["det_excluded"] == 80
    assert s["measurement_verdict"] == "VOID"
    # And a run that excluded 92% of its questions must not be publishable as a result.
    assert s["health"]["trustworthy"] is False
