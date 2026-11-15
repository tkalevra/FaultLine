"""#229 — brain auto-posture: measured p95 tightens per-op budgets and defers growth.

The wound (prod 2026-10-07): the tenant brain (NVIDIA) degraded to 200-responses-after-30s;
FaultLine measured it (brain_probe telemetry) and did nothing — every per-op budget was
waited out into 60s+ turns until a MANUAL env edit (REFRAME 30→8, ENRICHMENT 60→6) saved the
demo. These pins hold the AUTOMATIC version of that edit:

  * slow p95 (≥ TRIGGER_RATIO × the op's STATIC budget, quorate) → the op's EFFECTIVE budget
    (``LLMTimeouts.get``) caps at the fail-open floor — asserted as the returned value;
  * cooldown expiry → static budget restored, ring cleared (fresh-window hysteresis), the
    POSTURE lane row recovers to ok;
  * kill switch ``BRAIN_AUTOPOSTURE=0`` → static budgets byte-for-byte, observe a no-op;
  * growth deferral fires only for the DEGRADED brain (the re_embedder admission seam);
  * ANY posture error fails OPEN — the caller keeps its static budget, the call is never
    blocked;
  * the trigger is evaluated against the STATIC budget, never the postured one (the
    feedback-loop pin), and the floor never RAISES a deliberately-lower operator budget.

DB-free by design: the posture state is in-process and the POSTURE lane rows are read from
``brain_health``'s in-memory last-write-wins buffer (its flush cadence and DB round-trip are
the #68 lane's problem, pinned there). Only the LLM is irrelevant here — the sample feed is
stubbed probe history, per the issue's verify bar.

  python3 tools/fltest.py --bug 229 --test tests/test_brain_posture_229.py
"""

import contextlib
import os
import uuid

import pytest

from saas import brain_health as bh
from saas import brain_posture as bp
from saas.llm_context import bind_explicit_llm_config, reset_llm_config
from src.api.llm_calls import LLMTimeouts

pytestmark = pytest.mark.usefixtures("_pin_ready")


@contextlib.contextmanager
def _on_brain(cfg):
    """Bind cfg for the duration — LLMTimeouts.get() modulates the CURRENTLY BOUND brain
    (the ContextVar the real request path sets), so every get() read in a pin must run
    bound, exactly like a live call."""
    tok = bind_explicit_llm_config(cfg)
    try:
        yield
    finally:
        reset_llm_config(tok)


# ── fixtures ───────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _env():
    """Module-scoped MonkeyPatch, never module-level os.environ (#63). SAAS_MODE=1 because
    the explicit-bind seam refuses without it; REFRAME budget left at its code default (6.0)
    so the pins exercise the DEFAULT static budget, not one the test itself chose."""
    mp = pytest.MonkeyPatch()
    mp.setenv("SAAS_MODE", "1")
    mp.delenv("LLM_TIMEOUT_REFRAME", raising=False)
    mp.delenv("BRAIN_AUTOPOSTURE", raising=False)
    mp.delenv("BRAIN_POSTURE_FLOOR_S", raising=False)
    yield mp
    mp.undo()


@pytest.fixture(autouse=True)
def _pin_ready(monkeypatch):
    """Fresh posture + a quiet alarm stream; no async flush attempts from these pins (the
    flush lane needs a DB and belongs to #68)."""
    bp.reset_for_tests()
    monkeypatch.setattr(bh, "_maybe_flush_async", lambda: None)
    yield
    bp.reset_for_tests()


def _brain(account_id, base_url="https://nim.example/v1", model="nemotron-30b", scope="account",
           seat_user_id=None):
    cfg = {"account_id": account_id, "brain_scope": scope, "base_url": base_url,
           "model": model}
    if seat_user_id:
        cfg["seat_user_id"] = seat_user_id
    return cfg


def _feed(cfg, seat, op, *, n, elapsed_s, budget_s, outcome="fail", kind="timeout"):
    """Feed n stub probe observations with the brain bound, exactly as probe_event's tee
    would (cfg resolved from the caller's context)."""
    tok = bind_explicit_llm_config(cfg)
    try:
        for _ in range(n):
            bp.observe(seat, op, outcome, elapsed_s=elapsed_s, budget_s=budget_s,
                       fault={"kind": kind} if kind else None)
    finally:
        reset_llm_config(tok)


def _posture_row(seat):
    ev = bh._last_events.get((seat, bh.LANE_POSTURE))
    return (ev["outcome"], ev["reason"]) if ev else None


# ── 1. slow p95 → the EFFECTIVE budget tightens to the floor ───────────────────────────

def test_slow_p95_tightens_the_effective_budget_to_the_floor():
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    static = LLMTimeouts.static("REFRAME")
    assert static > bp.FLOOR_S                      # precondition: room to tighten
    # 7 samples: below quorum → nothing (a couple of outliers must not posture a seat)
    _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES - 1, elapsed_s=static, budget_s=static)
    assert bp.timeout_ceiling("REFRAME", cfg) is None
    with _on_brain(cfg):
        assert LLMTimeouts.get("REFRAME") == static
    # the 8th at-cutoff sample → tighten
    _feed(cfg, seat, "REFRAME", n=1, elapsed_s=static, budget_s=static)
    assert bp.timeout_ceiling("REFRAME", cfg) == bp.FLOOR_S
    with _on_brain(cfg):
        got = LLMTimeouts.get("REFRAME")          # ← the posture-returned timeout VALUE
        assert got == bp.FLOOR_S, (got, bp.FLOOR_S)
    # static is untouched — the ceiling modulates, it never rewrites the budget
    assert LLMTimeouts.static("REFRAME") == static
    # loud on the alarm-stream surface: the observing seat's POSTURE lane row reads FAIL
    assert _posture_row(seat) == ("fail", "posture_tighten")
    # per-OP: another op on the same brain keeps its static budget
    assert bp.timeout_ceiling("EXTRACT", cfg) is None


def test_tighten_row_only_for_the_observing_seat():
    """A shared org brain tightens for every seat ON it (documented), but the POSTURE lane
    row lands only on the seat whose traffic observed the degrade — a second seat's card is
    not lit by another seat's evidence."""
    seat_a, seat_b, acct = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    _feed(cfg, seat_a, "REFRAME", n=bp.MIN_SAMPLES, elapsed_s=6.0, budget_s=6.0)
    assert _posture_row(seat_a) == ("fail", "posture_tighten")
    assert _posture_row(seat_b) is None
    # seat_b's ops run on the SAME physical brain → same tightened ceiling
    assert bp.timeout_ceiling("REFRAME", cfg) == bp.FLOOR_S


# ── 2. admission: only latency observations vote ──────────────────────────────────────

def test_fast_refusals_and_availability_faults_never_posture():
    """sample_ms admission is reused verbatim: a 401 in 90ms is not latency, an unreachable
    host is an availability fault — neither may tighten a budget (the wound was SLOW, not
    DEAD; a dead brain is the fail-closed lane's problem)."""
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES + 4, elapsed_s=0.09, budget_s=6.0,
          kind="refused")
    _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES + 4, elapsed_s=6.0, budget_s=6.0,
          kind="unreachable")
    assert bp.timeout_ceiling("REFRAME", cfg) is None
    assert bp.status(cfg)["samples_by_op"].get("REFRAME", 0) == 0


def test_probe_event_tees_into_the_posture():
    """The wiring pin: the alarm stream's own entry point feeds the posture (brain_health
    step 4), not just a direct observe() call."""
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    tok = bind_explicit_llm_config(cfg)
    try:
        static = LLMTimeouts.static("REFRAME")
        for _ in range(bp.MIN_SAMPLES):
            bh.probe_event(seat, "REFRAME", "fail", operation="REFRAME",
                           elapsed_s=static, budget_s=static,
                           fault={"kind": "timeout"})
    finally:
        reset_llm_config(tok)
    with _on_brain(cfg):
        assert LLMTimeouts.get("REFRAME") == bp.FLOOR_S


# ── 3. cooldown expiry → recovery ──────────────────────────────────────────────────────

def test_cooldown_expiry_recovers_the_static_budget(monkeypatch):
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    static = LLMTimeouts.static("REFRAME")
    _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES, elapsed_s=static, budget_s=static)
    with _on_brain(cfg):
        assert LLMTimeouts.get("REFRAME") == bp.FLOOR_S
    # time-travel past the cooldown (the clock is indirected for exactly this)
    real_now = bp._now
    monkeypatch.setattr(bp, "_now", lambda: real_now() + bp.COOLDOWN_S + 1.0)
    with _on_brain(cfg):
        assert LLMTimeouts.get("REFRAME") == static
    assert bp.timeout_ceiling("REFRAME", cfg) is None
    # loud recovery on the SAME seat's card (last-write-wins clears the fail)
    assert _posture_row(seat) == ("ok", None)
    # hysteresis by construction: the ring was cleared at recovery
    assert bp.status(cfg)["samples_by_op"].get("REFRAME", 0) == 0
    monkeypatch.undo()


def test_trigger_is_evaluated_against_the_static_budget_never_the_postured_one(monkeypatch):
    """THE FEEDBACK-LOOP PIN. Tightened to the floor, every call fails at ~the floor; if the
    trigger compared p95 against the POSTURED budget it would re-tighten forever and the
    cooldown would never be a recovery. Whole lifecycle in one test: degrade → tighten →
    postured-cutoff traffic during the window → expiry → STATIC budget, no re-tighten."""
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    static = LLMTimeouts.static("REFRAME")
    _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES, elapsed_s=static, budget_s=static)
    with _on_brain(cfg):
        assert LLMTimeouts.get("REFRAME") == bp.FLOOR_S
    # during the tightened window the op runs at the floor; failures land AT the floor
    _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES + 4, elapsed_s=bp.FLOOR_S, budget_s=bp.FLOOR_S)
    real_now = bp._now
    monkeypatch.setattr(bp, "_now", lambda: real_now() + bp.COOLDOWN_S + 1.0)
    with _on_brain(cfg):
        assert LLMTimeouts.get("REFRAME") == static   # recovered, not re-tightened
    # and a fresh window of floor-speed observations does not re-tighten either
    monkeypatch.undo()
    _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES, elapsed_s=bp.FLOOR_S, budget_s=bp.FLOOR_S)
    assert bp.timeout_ceiling("REFRAME", cfg) is None


# ── 4. the kill switch ─────────────────────────────────────────────────────────────────

def test_kill_switch_restores_static_budgets_and_gates_every_read(monkeypatch):
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    static = LLMTimeouts.static("REFRAME")
    _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES, elapsed_s=static, budget_s=static)
    with _on_brain(cfg):
        assert LLMTimeouts.get("REFRAME") == bp.FLOOR_S
    monkeypatch.setenv("BRAIN_AUTOPOSTURE", "0")
    try:
        with _on_brain(cfg):
            assert LLMTimeouts.get("REFRAME") == static  # live env read, no restart needed
        assert bp.timeout_ceiling("REFRAME", cfg) is None
        assert bp.growth_deferred(cfg) is None
        # observe is a no-op too — the ring does not GROW while switched off (the pre-switch
        # samples legitimately survive: the switch is a read gate, not a state purge)
        before = bp.status(cfg)["samples_by_op"].get("REFRAME", 0)
        _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES, elapsed_s=static, budget_s=static)
        assert bp.status(cfg)["samples_by_op"].get("REFRAME", 0) == before
    finally:
        monkeypatch.delenv("BRAIN_AUTOPOSTURE")
    # and back on: the in-process state survived, the ceiling applies again (rollback lever
    # is reversible both ways without dropping the process)
    with _on_brain(cfg):
        assert LLMTimeouts.get("REFRAME") == bp.FLOOR_S


# ── 5. the floor is a MINIMUM-ONLY cap ─────────────────────────────────────────────────

def test_floor_never_raises_a_deliberately_lower_operator_budget(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_REFRAME", "3")       # an operator who deliberately set 3s
    try:
        seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
        cfg = _brain(acct)
        assert LLMTimeouts.static("REFRAME") == 3.0
        _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES, elapsed_s=3.0, budget_s=3.0)
        # tightened (the row proves it) — but 4.0 > 3.0, so the operator's budget STANDS
        assert _posture_row(seat) == ("fail", "posture_tighten")
        with _on_brain(cfg):
            assert LLMTimeouts.get("REFRAME") == 3.0
    finally:
        monkeypatch.delenv("LLM_TIMEOUT_REFRAME")


# ── 6. growth deferral — only the degraded brain ───────────────────────────────────────

def test_growth_defer_fires_only_for_the_degraded_brain():
    seat, acct_a, acct_b = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    slow = _brain(acct_a, base_url="https://slow.example/v1")
    other = _brain(acct_b, base_url="https://fast.example/v1")
    _feed(slow, seat, "ENRICHMENT", n=bp.MIN_SAMPLES, elapsed_s=15.0, budget_s=15.0)
    rem = bp.growth_deferred(slow)
    assert rem and 0 < rem <= bp.COOLDOWN_S, rem
    # a DIFFERENT brain on a different account: untouched
    assert bp.growth_deferred(other) is None
    # kill the switch → the defer read is inert even for the degraded brain


def test_growth_defer_kill_switch(monkeypatch):
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    _feed(cfg, seat, "ENRICHMENT", n=bp.MIN_SAMPLES, elapsed_s=15.0, budget_s=15.0)
    monkeypatch.setenv("BRAIN_AUTOPOSTURE", "0")
    try:
        assert bp.growth_deferred(cfg) is None
    finally:
        monkeypatch.delenv("BRAIN_AUTOPOSTURE")


def test_reembedder_admission_seam_skips_only_the_degraded_brain(caplog):
    """The EXISTING validate-before-spend seam (re_embedder preflight): a degraded brain's
    growth subsystems skip UNRECORDED (stay due, resume after cooldown); a healthy brain's
    proceed. No second admission system — this is the same gate, one more question."""
    from src.re_embedder.embedder import _reembedder_llm_lane_open
    seat, acct_a, acct_b = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    slow = _brain(acct_a, base_url="https://slow.example/v1")
    other = _brain(acct_b, base_url="https://fast.example/v1")
    _feed(slow, seat, "ENRICHMENT", n=bp.MIN_SAMPLES, elapsed_s=15.0, budget_s=15.0)

    tok = bind_explicit_llm_config(slow)
    try:
        with caplog.at_level("INFO", logger="src.re_embedder.embedder"):
            assert _reembedder_llm_lane_open(seat, "ontology_eval", "faultline_x") is False
        assert any("brain_posture_cooldown" in r.getMessage() for r in caplog.records), \
            [r.getMessage() for r in caplog.records]
    finally:
        reset_llm_config(tok)

    tok = bind_explicit_llm_config(other)
    try:
        assert _reembedder_llm_lane_open(seat, "ontology_eval", "faultline_x") is True
    finally:
        reset_llm_config(tok)


# ── 7. fail-open: a posture error never blocks a call nor tightens below the floor ─────

def test_a_posture_error_never_blocks_a_call(monkeypatch):
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    static = LLMTimeouts.static("REFRAME")
    # the ceiling read itself blows up
    monkeypatch.setattr(bp, "timeout_ceiling",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("posture bug")))
    assert LLMTimeouts.get("REFRAME") == static          # static answer, no raise
    monkeypatch.undo()
    # the observer blows up
    monkeypatch.setattr(bp, "observe",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("posture bug")))
    tok = bind_explicit_llm_config(cfg)
    try:
        bh.probe_event(seat, "REFRAME", "fail", operation="REFRAME",
                       elapsed_s=static, budget_s=static, fault={"kind": "timeout"})
    finally:
        reset_llm_config(tok)
        monkeypatch.undo()
    # the growth-defer read blows up → growth proceeds (fail-open in the RUN direction)
    monkeypatch.setattr(bp, "growth_deferred",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("posture bug")))
    from src.re_embedder.embedder import _reembedder_llm_lane_open
    tok = bind_explicit_llm_config(cfg)
    try:
        assert _reembedder_llm_lane_open(seat, "ontology_eval", "faultline_x") is True
    finally:
        reset_llm_config(tok)
        monkeypatch.undo()


# ── 8. knob hygiene ────────────────────────────────────────────────────────────────────

def test_knob_typo_falls_back_to_the_documented_default_loudly(monkeypatch):
    """A typo'd env value must never kill the posture silently (every consumer swallows
    exceptions by contract — an import crash would read as 'posture off' with no signal)."""
    monkeypatch.setenv("BRAIN_POSTURE_FLOOR_S", "4x")
    monkeypatch.setenv("BRAIN_POSTURE_MIN_SAMPLES", "eight")
    assert bp._env_float("BRAIN_POSTURE_FLOOR_S", 4.0) == 4.0
    assert bp._env_int("BRAIN_POSTURE_MIN_SAMPLES", 8) == 8
    assert len(bp._KNOB_PARSE_ERRORS) >= 2
    assert any("BRAIN_POSTURE_FLOOR_S" in e for e in bp._KNOB_PARSE_ERRORS)


# ── 9. loudness is exactly-once and shaped (consolidated from the inherited draft) ─────

def test_tighten_and_recover_log_exactly_once_each(monkeypatch):
    """Every transition is loud ONCE: one tighten log event, one POSTURE row; one recover
    log event, one ok row. Double emission would flap the card for a single incident."""
    events: list[tuple] = []
    real_log = bp._log_event
    monkeypatch.setattr(bp, "_log_event",
                        lambda event, level="info", **kw: events.append((event, dict(kw))))
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    static = LLMTimeouts.static("REFRAME")
    _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES, elapsed_s=static, budget_s=static)
    # more over-budget traffic during the window must NOT re-tighten / re-log
    _feed(cfg, seat, "REFRAME", n=bp.MIN_SAMPLES, elapsed_s=static, budget_s=static)
    real_now = bp._now
    monkeypatch.setattr(bp, "_now", lambda: real_now() + bp.COOLDOWN_S + 1.0)
    bp.timeout_ceiling("REFRAME", cfg)
    bp.timeout_ceiling("REFRAME", cfg)                     # second lazy read: no double recover
    names = [e[0] for e in events]
    assert names.count("brain_posture.tighten") == 1, names
    assert names.count("brain_posture.recover") == 1, names
    monkeypatch.undo()


def test_unbound_context_has_no_opinion():
    """SAAS off / open core / no brain bound: the ceiling is None and the budget is the
    static one, byte-for-byte (there is nothing to modulate)."""
    assert bp.timeout_ceiling("REFRAME", None) is None
    assert LLMTimeouts.get("REFRAME") == LLMTimeouts.static("REFRAME")


def test_reembedder_preflight_posture_import_error_fails_open(monkeypatch):
    """Open-core shape (no saas package) or any import fault at the seam: growth proceeds
    exactly as today — the defer may never fail CLOSED."""
    import builtins
    from src.re_embedder.embedder import _reembedder_llm_lane_open
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    cfg = _brain(acct)
    _feed(cfg, seat, "ENRICHMENT", n=bp.MIN_SAMPLES, elapsed_s=15.0, budget_s=15.0)
    real_import = builtins.__import__

    def _no_posture(name, *a, **kw):
        if name == "saas" and a and a[2] and "brain_posture" in a[2]:
            raise ImportError("no posture module")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_posture)
    tok = bind_explicit_llm_config(cfg)
    try:
        assert _reembedder_llm_lane_open(seat, "ontology_eval", "faultline_x") is True
    finally:
        reset_llm_config(tok)


def test_reframe_failopen_shape_unchanged_at_the_floor(monkeypatch):
    """REFRAME's fail-open contract when the brain cannot answer is atoms=[] (the caller
    falls back to raw text / deterministic segmentation) — the EXACT fail-open the tightened
    budget routes into, not a new one. The brain is a deterministically-dead loopback port
    (instant connection refusal — no DNS variance), bound so the call runs against it under
    the tightened floor."""
    seat, acct = str(uuid.uuid4()), str(uuid.uuid4())
    dead = _brain(acct, base_url="http://127.0.0.1:9/v1")   # port 9: refuse, fast
    static = LLMTimeouts.static("REFRAME")
    _feed(dead, seat, "REFRAME", n=bp.MIN_SAMPLES, elapsed_s=static, budget_s=static)
    tok = bind_explicit_llm_config(dead)
    try:
        import time as _time
        from src.extraction.reframe import reframe_to_atomic
        assert LLMTimeouts.get("REFRAME") == bp.FLOOR_S      # running AT the floor
        t0 = _time.monotonic()
        result = __import__("asyncio").run(
            reframe_to_atomic("My dog is called Fraggle and he is a good boy.",
                              user_id=seat))
        elapsed = _time.monotonic() - t0
        assert result.atoms == []                            # fail-open: keep raw text
        assert elapsed < bp.FLOOR_S + 2.0, elapsed           # never the old wait-sum
    finally:
        reset_llm_config(tok)
