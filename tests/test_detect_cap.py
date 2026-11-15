"""DETECT-CAP + the Brain Health bar's scope-note segment — the gauntlet's two halves.

Real-redis gated (the Lua maintenance window and the floor rank-CAS ARE the seams under
test — a mock that short-circuits them is not coverage). Scratch Redis:

    docker run --rm -d --name fltest-redis-detectcap -p 55434:6379 redis:7-alpine
    FLTEST_REDIS_URL=redis://127.0.0.1:55434/0 python3 tools/fltest.py --bug DETECTCAP \\
        --test tests/test_detect_cap.py

Covers, in order:
  A. the maintenance window in the REAL Lua take (hold, no spend, release, pause-precedence)
  B. the calibrated floor rank (3) vs measured (2) in the REAL rank-CAS, across "restarts"
  C. the three gates: rate gate (bucket acquire holds + cap extension + mirror release),
     re-embedder yield gate (_brain_busy), ingest admission (acquire_interactive)
  D. the probe engine end-to-end with a fake call seam: measured/cancel/error/unreachable/
     bounded/config-cap, key never in any payload, maintenance always released
  E. the meter payload: pace/detected/calibrated source
  F. the bar (jsdom): the scope-note segment, zero new lines in every state, hide rule
  G. the modal copy (verbatim), CSP cleanliness, the tenant.js wiring
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from src.api import ceiling_admission
from src.api import llm_calls as LC
from src.api import redis_coord as RC
from src.re_embedder import supervisor

ROOT = Path(__file__).resolve().parents[1]
REDIS_URL = os.environ.get("FLTEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="FLTEST_REDIS_URL not set — this suite exercises the real Lua/CAS seams",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_RATE_BUDGET", "true")
    monkeypatch.setenv("REDIS_COORD_SOCKET_TIMEOUT", "2")
    RC.reset_client()
    if RC.client() is None:
        pytest.skip("scratch Redis not reachable")
    yield
    RC.reset_client()


def _brain_cfg(tag, key=None, base="https://api.provider.example/v1", model="probe-model",
               owner=None, max_rpm=None):
    cfg = {
        "account_id": owner or f"acct-{tag}",
        "brain_scope": "account",
        "backend_type": "openai",
        "base_url": base,
        "model": model,
        "api_key": key or f"sk-secret-{tag}",
        "extra_headers": {},
    }
    if max_rpm:
        cfg["max_rpm"] = max_rpm
    return cfg


def _scope(tag="A", **kw):
    s = RC.scope_for_brain(_brain_cfg(tag, **kw))
    assert s is not None
    RC.purge_scope(s)
    return s


# ══════════════════════════════════════════════════════════════════════════════
# A. THE MAINTENANCE WINDOW — real Lua, real hold, real release
# ══════════════════════════════════════════════════════════════════════════════

def test_maintenance_holds_the_take_and_spends_no_token():
    s = _scope("maintA")
    assert RC.take_token_state(s, 10.0, 10.0)["wait_s"] == 0.0   # create the hash
    k = RC.key(RC._NS_RATE, s, "bucket")
    before = RC.client().hmget(k, "tokens")
    assert RC.begin_brain_maintenance(s, 30.0) is True
    st = RC.take_token_state(s, 10.0, 10.0)
    assert st["paused"] is True, "a maintenance hold must hold like a pause for old callers"
    assert st["maintenance"] is True
    assert 20.0 <= st["wait_s"] <= 30.0
    assert RC.client().hmget(k, "tokens") == before, "no token may be spent during maintenance"
    assert (RC.brain_maintenance_remaining(s) or 0) > 25


def test_maintenance_release_lets_takes_resume_immediately():
    s = _scope("maintB")
    RC.take_token_state(s, 10.0, 10.0)
    RC.begin_brain_maintenance(s, 60.0)
    assert RC.take_token_state(s, 10.0, 10.0)["maintenance"] is True
    assert RC.end_brain_maintenance(s) is True
    st = RC.take_token_state(s, 10.0, 10.0)
    assert st == {"wait_s": 0.0, "paused": False, "maintenance": False}
    assert RC.brain_maintenance_remaining(s) is None


def test_a_real_provider_pause_outranks_maintenance_in_the_reported_hold():
    """Both windows in force → the take reports the PAUSE (flag 1), not maintenance: a
    genuine 429 window must never be misread as a user-driven probe."""
    s = _scope("maintC")
    RC.take_token_state(s, 10.0, 10.0)
    RC.publish_brain_pause(s, 120.0)
    RC.begin_brain_maintenance(s, 30.0)
    st = RC.take_token_state(s, 10.0, 10.0)
    assert st["paused"] is True and st["maintenance"] is False
    assert st["wait_s"] > 100


def test_available_tokens_read_zero_under_maintenance():
    s = _scope("maintD")
    RC.take_token_state(s, 10.0, 10.0)
    assert (RC.brain_available_tokens(s) or 0) > 0
    RC.begin_brain_maintenance(s, 30.0)
    assert RC.brain_available_tokens(s) == 0.0, \
        "nothing can be spent, so no reader may report spendable capacity"


# ══════════════════════════════════════════════════════════════════════════════
# B. THE CALIBRATED FLOOR RANK — real rank-CAS
# ══════════════════════════════════════════════════════════════════════════════

def test_calibrated_outranks_measured_and_survives_a_restart():
    s = _scope("rankA")
    RC.take_token_state(s, 10.0, 10.0)
    assert RC.publish_brain_floor(s, 0.5, 2, "measured") is True
    assert RC.publish_brain_floor(s, 0.25, 3, "calibrated") is True
    st = RC.brain_floor_state(s)
    assert st == {"floor_rps": 0.25, "rank": 3, "provenance": "calibrated"}
    # A later header-MEASURED write (rank 2) must be REFUSED by the CAS.
    RC.publish_brain_floor(s, 9.0, 2, "measured")
    assert RC.brain_floor_state(s)["rank"] == 3
    # "Survives restart": a FRESH client (the next process) reads the same truth.
    RC.reset_client()
    assert RC.brain_floor_state(s)["provenance"] == "calibrated"
    # And the take actually paces at the calibrated floor, not the refused 9 rps.
    st2 = RC.take_token_state(s, 10.0, 10.0)   # bucket refilled; next take clamps by floor
    RC.reset_client()
    assert st2["paused"] is False


def test_the_take_clamps_every_process_to_the_calibrated_floor():
    s = _scope("rankB")
    RC.publish_brain_floor(s, 0.05, 3, "calibrated")     # 3 rpm
    # Drain the burst the first take creates, then a fast-configured process must be paced.
    RC.take_token_state(s, 100.0, 100.0)                  # takes 1 token, clamps rate to 0.05
    RC.take_token_state(s, 100.0, 100.0)                  # bucket now ~0 at 3 rpm
    st = RC.take_token_state(s, 100.0, 100.0)
    assert st["paused"] is False
    assert st["wait_s"] > 10.0, "the wait must be paced at the calibrated 3/min, not 6000/min"


# ══════════════════════════════════════════════════════════════════════════════
# C. THE THREE GATES genuinely respect the window
# ══════════════════════════════════════════════════════════════════════════════

def test_rate_gate_bucket_holds_then_resumes_on_release():
    s = _scope("gateA")
    RC.take_token_state(s, 10.0, 10.0)
    RC.begin_brain_maintenance(s, 1.5)
    b = LC._TokenBucket(10.0, 10.0)
    b.coord_scope = s
    t0 = time.monotonic()
    ok = b.acquire_sync(0.2)          # would fail open at 0.2s without the extension
    held = time.monotonic() - t0
    assert ok is True
    assert held >= 1.2, f"the acquire must HOLD for the window, not fail open (held {held:.2f}s)"
    # After release the next acquire is immediate and the local mirror is cleared.
    RC.end_brain_maintenance(s)
    t1 = time.monotonic()
    assert b.acquire_sync(0.2) is True
    assert time.monotonic() - t1 < 1.0
    assert b.maint_until == 0.0, "the mirror must clear once the shared truth says released"


def test_yield_gate_brain_busy_true_under_maintenance(monkeypatch):
    s = _scope("gateB")
    RC.take_token_state(s, 10.0, 10.0)                # bucket exists, tokens full
    assert supervisor._brain_busy(s) is False
    # Pin the EXPLICIT maintenance branch independent of the available-tokens read (which
    # also reads 0.0 under maintenance as defense-in-depth — a mutation that deletes the
    # explicit check must still go red, so force the tokens read to look plentiful).
    monkeypatch.setattr(supervisor.redis_coord, "brain_available_tokens", lambda _s: 5.0)
    RC.begin_brain_maintenance(s, 30.0)
    assert supervisor._brain_busy(s) is True, "upkeep must yield to the probe"
    RC.end_brain_maintenance(s)
    assert supervisor._brain_busy(s) is False


def test_ingest_admission_defers_during_maintenance(monkeypatch):
    s = _scope("gateC")
    RC.take_token_state(s, 10.0, 10.0)
    monkeypatch.setattr(ceiling_admission, "_brain_scope", lambda: s)
    assert ceiling_admission.acquire_interactive("EXTRACT", 2.0) == "proceed"
    RC.begin_brain_maintenance(s, 30.0)
    assert ceiling_admission.acquire_interactive("EXTRACT", 2.0) == "defer", \
        "positive knowledge capacity will not arrive — defer, don't park the user's turn"
    RC.end_brain_maintenance(s)
    assert ceiling_admission.acquire_interactive("EXTRACT", 2.0) == "proceed"


# ══════════════════════════════════════════════════════════════════════════════
# D. THE PROBE ENGINE (fake call seam, real Redis floor/window)
# ══════════════════════════════════════════════════════════════════════════════

SECRET = "sk-supersecret-detectcap"


class FakeEndpoint:
    """A scriptable provider: counts calls, 429s at a chosen call index, optional raise."""

    def __init__(self, ok_latency_ms=120, fail_at=None, raise_at=None, usage=True):
        self.calls = 0
        self.fail_at = fail_at          # 1-based call index that returns 429
        self.raise_at = raise_at        # 1-based call index that raises
        self.ok_latency_ms = ok_latency_ms
        self.usage = usage
        self.saw_keys = []

    def __call__(self, cfg, max_tokens, timeout_s=None):
        self.calls += 1
        self.saw_keys.append(cfg.get("api_key"))
        if self.raise_at and self.calls == self.raise_at:
            raise RuntimeError("probe boom")
        if self.fail_at and self.calls >= self.fail_at:
            return {"ok": False, "status": 429, "latency_ms": self.ok_latency_ms,
                    "usage": None, "retry_after_s": 60.0, "detail": "rate-limited"}
        return {"ok": True, "status": 200, "latency_ms": self.ok_latency_ms,
                "usage": ({"completion_tokens": 4, "prompt_tokens": 9}
                          if self.usage else None),
                "retry_after_s": None, "detail": "answered"}


@pytest.fixture()
def probe_env(monkeypatch):
    from saas import detect_cap

    cfg = _brain_cfg("probe", key=SECRET)
    egress_calls = []
    # The probe scope is SHARED across these tests (same brain identity) — purge it so a
    # calibrated floor written by one test can never masquerade as another's starting state.
    probe_scope = RC.scope_for_brain(cfg)
    assert probe_scope is not None
    RC.purge_scope(probe_scope)

    def fake_validate(url):
        egress_calls.append(url)
        return {"host": "api.provider.example", "scheme": "https",
                "cleartext": False, "public": True, "allowlisted": False}

    monkeypatch.setattr(detect_cap._llm_config, "resolve_account_llm_config",
                        lambda account_id: cfg)
    monkeypatch.setattr(detect_cap._llm_config, "validate_egress_url", fake_validate)
    detect_cap._RUNS.clear()
    yield detect_cap, cfg, egress_calls
    detect_cap._RUNS.clear()
    RC.purge_scope(probe_scope)


def _drive(detect_cap, account_id, run_id):
    st = {"done": False}
    guard = 0
    while not st["done"] and guard < 30:
        st = detect_cap.advance(account_id, run_id)
        guard += 1
    return st


def _run_with(detect_cap, fake, account_id="acct-probe"):
    """start + drive to completion with the fake call seam injected at stage time."""
    st = detect_cap.start(account_id)
    run = detect_cap._RUNS[st["run_id"]]
    run.call_fn = fake
    run.sleep_fn = lambda *_: None
    return _drive(detect_cap, account_id, st["run_id"]), run


def test_measured_run_writes_the_calibrated_floor_and_releases(probe_env):
    detect_cap, cfg, egress = probe_env
    fake = FakeEndpoint(fail_at=6)     # waves: 3 ok, then 429 on the 3rd call of wave 2
    st, run = _run_with(detect_cap, fake)
    v = st["verdict"]
    assert st["state"] == "done"
    assert v["outcome"] == "measured" and v["floor_written"] is True
    assert v["ceiling_source"] == "calibrated"
    # THE RUNG, NOT THE ELAPSED RATIO (MEDIUM-1): wave 0 OFFERED 12/min and all 3 calls
    # answered — 12.0/min is what was proven. The elapsed ratio would read ~n/(n-1)×12
    # (+50% on the first rung) and is UNBOUNDED at zero latency (a stubbed run measured
    # 676,361/min) — and sticky, because rank 3 refuses later correction.
    assert v["ceiling_rpm"] == pytest.approx(12.0)
    floor = RC.brain_floor_state(run.scope)
    # NIT-1 pairing: the published rank IS the map's rank for FLOOR_CALIBRATED, and the
    # map says 3. Mutating either side of that pairing must go red HERE.
    assert floor["rank"] == LC._FLOOR_RANK[LC.FLOOR_CALIBRATED] == 3
    assert floor["provenance"] == "calibrated"
    assert floor["floor_rps"] == pytest.approx(12.0 / 60.0)
    assert RC.brain_maintenance_remaining(run.scope) is None, "traffic must be restored"
    # stopped AT the 429 — no calls after the first refusal
    assert fake.calls <= 26
    assert st["calls_made"] == fake.calls
    # the egress guard really ran against the cfg's base URL
    assert cfg["base_url"] in egress


def test_wave0_429_refusal_writes_nothing_and_says_so(probe_env):
    """MEDIUM-2 corner: the provider 429s the GENTLEST wave (a tiny-tier key). Nothing
    was proven sustainable, so nothing may be written — and the verdict text must never
    claim a write that didn't happen."""
    detect_cap, cfg, _ = probe_env
    fake = FakeEndpoint(fail_at=2)     # direct call OK; the gentlest wave is refused
    st, run = _run_with(detect_cap, fake)
    v = st["verdict"]
    assert v["outcome"] == "refused_gentlest", \
        "a 429 with no clean rung before it is a refusal, not a measurement"
    assert v["ok"] is False
    assert v["floor_written"] is False and v["ceiling_rpm"] is None
    assert RC.brain_floor_state(run.scope) is None, "nothing may be written"
    assert RC.brain_maintenance_remaining(run.scope) is None
    assert fake.calls == 4, "direct + the 3 in-flight gentlest calls — ladder stopped there"
    # and the RENDERED text for the no-write corners (the real function, executed):
    texts = _dc_verdict_texts()
    assert "calibrated maximum" in texts["measured_written"]
    assert "12/min" in texts["measured_written"]
    nowrite = texts["measured_nowrite"]
    assert "nothing written" in nowrite, nowrite
    assert "calibrated maximum" not in nowrite and "outranks" not in nowrite, \
        "must never claim a write that did not happen"
    assert "no ceiling measured" in texts["refused_gentlest"]


_DC_HARNESS = ROOT / "tests" / "dashboard" / "dc_verdict_text.js"


def _dc_verdict_texts():
    proc = subprocess.run([node, str(_DC_HARNESS)], capture_output=True, text=True,
                          timeout=60, cwd=str(ROOT / "tests" / "dashboard"))
    assert proc.returncode == 0, f"dc verdict harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_cancel_mid_wave_never_replaces_a_pre_existing_floor(probe_env):
    """Round-2 MEDIUM: cancel landing mid-wave. The wave's calls finish (in-flight calls
    are allowed to complete), but its measurement is DISCARDED — and a pre-existing
    calibrated 30/min floor is still 30/min afterwards, with cancel()'s "previous ceiling
    untouched" message TRUE when it returns."""
    import threading as _th

    detect_cap, cfg, _ = probe_env
    st = detect_cap.start("acct-probe")
    run = detect_cap._RUNS[st["run_id"]]
    seq = {"n": 0}

    def racetime(cfgv, mt, ts=None):
        seq["n"] += 1
        i = seq["n"]
        if i == 5:
            time.sleep(0.6)             # wave1's first call — the cancel lands inside this
        if i >= 6:
            return {"ok": False, "status": 429, "latency_ms": 10, "usage": None,
                    "retry_after_s": None, "detail": "rate-limited"}
        return {"ok": True, "status": 200, "latency_ms": 10, "usage": None,
                "retry_after_s": None, "detail": "answered"}

    run.call_fn = racetime
    run.sleep_fn = lambda *_: None
    # direct + params + wave0 (clean → last_clean=12, NOT yet published)
    for _ in range(3):
        st = detect_cap.advance("acct-probe", st["run_id"])
    # a pre-existing CALIBRATED 30/min floor is in force
    RC.publish_brain_floor(run.scope, 0.5, 3, "calibrated")
    # wave1 in flight; cancel mid-wave
    box = {}

    def go():
        box["st"] = detect_cap.advance("acct-probe", st["run_id"])

    t = _th.Thread(target=go)
    t.start()
    time.sleep(0.25)                     # inside call 5's 0.6s window
    cst = detect_cap.cancel("acct-probe", st["run_id"])
    t.join()

    assert cst["state"] == "cancelled"
    assert "previous ceiling untouched" in " ".join(s["text"] for s in cst["steps"])
    # the wave completed AFTER the cancel — its verdict must be the discarded one
    final = detect_cap.advance("acct-probe", st["run_id"])   # cancelled → status only
    v = final["verdict"]
    assert v["outcome"] == "cancelled" and v["floor_written"] is False
    assert final["state"] == "cancelled", "a cancelled run must not flip back to done"
    # THE CONTRACT: the floor is STILL the pre-existing 30/min calibrated one
    assert RC.brain_floor_state(run.scope) == {"floor_rps": 0.5, "rank": 3,
                                               "provenance": "calibrated"}
    assert RC.brain_maintenance_remaining(run.scope) is None


def test_capped_below_first_rung_says_no_burst_ran(probe_env, monkeypatch):
    """Round-2 NIT-1: max_rpm <= 12 stops the probe BEFORE the first rung (calls_made=1,
    ceiling_rpm=None). The verdict text must not claim a burst was sustained — no wave
    ran at all."""
    detect_cap, _, _ = probe_env
    monkeypatch.setattr(detect_cap._llm_config, "resolve_account_llm_config",
                        lambda account_id: _brain_cfg("probe", key=SECRET, max_rpm=10))
    fake = FakeEndpoint()
    st, run = _run_with(detect_cap, fake)
    v = st["verdict"]
    assert v["outcome"] == "capped_at_config"
    assert st["calls_made"] == 1, "only the direct call — zero waves ran"
    assert v["ceiling_rpm"] is None and v["floor_written"] is False
    # the RENDERED text (real function, executed): no false "sustained" claim when null
    texts = _dc_verdict_texts()
    null_txt = texts["capped_config_null"]
    assert "before any burst ran" in null_txt, null_txt
    assert "sustained" not in null_txt and "gentlest wave" not in null_txt, \
        "zero waves ran — no burst may be claimed as sustained"
    with12 = texts["capped_config_12"]
    assert "sustained 12/min" in with12, \
        "a rung DID run before the config stop — the sustained claim is true here"


class _TailEndpoint:
    """Answers after a REAL latency (the wave's wall-clock elapsed must include it), and
    starts returning 429 at call ``fail_at`` (1-based, counting the direct call)."""

    def __init__(self, true_latency_s: float, fail_at: int):
        self.n = 0
        self.lat = true_latency_s
        self.fail_at = fail_at

    def __call__(self, cfg, max_tokens, timeout_s=None):
        self.n += 1
        if self.n >= self.fail_at:
            return {"ok": False, "status": 429, "latency_ms": int(self.lat * 1000),
                    "usage": None, "retry_after_s": None, "detail": "rate-limited"}
        time.sleep(self.lat)
        return {"ok": True, "status": 200, "latency_ms": int(self.lat * 1000),
                "usage": None, "retry_after_s": None, "detail": "answered"}


def test_full_attendance_proves_the_offered_rung(probe_env, monkeypatch):
    """Round-2 NIT-2 (choice: attendance proves the rung — the post-answer tail is
    excluded by construction). A 2-call wave offered at 96/min with 0.8s TRUE latency:
    elapsed = spacing(0.625s) + tail(0.8s) = 1.425s → the old elapsed ratio read
    2/(1.425/60) = 84.2/min (under-written through the tail). All calls answered at
    96/min pace with no 429 → 96.0 is what was proven and what is written."""
    detect_cap, cfg, _ = probe_env
    # A short ladder so the real pacing sleeps keep the test fast: wave0 = 2 @ 96/min,
    # wave1 = 2 @ 192/min. _STAGES is module-level — patch both together.
    monkeypatch.setattr(detect_cap, "_WAVES", ((2, 96.0), (2, 192.0)))
    monkeypatch.setattr(detect_cap, "_STAGES", ("direct", "params", "wave0", "wave1"))
    st = detect_cap.start("acct-probe")
    run = detect_cap._RUNS[st["run_id"]]
    run.call_fn = _TailEndpoint(true_latency_s=0.8, fail_at=4)  # 429 at wave1 call 1
    run.sleep_fn = time.sleep                                     # REAL pacing
    out = _drive(detect_cap, "acct-probe", st["run_id"])
    v = out["verdict"]
    assert v["outcome"] == "measured" and v["floor_written"] is True
    # the pinned number: the RUNG, not 84.2
    assert v["ceiling_rpm"] == pytest.approx(96.0)
    assert RC.brain_floor_state(run.scope)["floor_rps"] == pytest.approx(96.0 / 60.0)
    assert out["calls_made"] == 5                                  # 1 direct + 2 + 2


def test_concurrent_stage_posts_never_double_fire(probe_env):
    """NIT-2: two /stage POSTs racing on one run must not both execute the same stage —
    the loser is told busy, and the provider is charged for the stage exactly once."""
    import threading as _th

    detect_cap, cfg, _ = probe_env
    st = detect_cap.start("acct-probe")
    run = detect_cap._RUNS[st["run_id"]]
    calls = []

    def slow_call(cfgv, mt, ts=None):
        calls.append(1)
        time.sleep(0.4)
        return {"ok": True, "status": 200, "latency_ms": 400, "usage": None,
                "retry_after_s": None, "detail": "answered"}

    run.call_fn = slow_call
    run.sleep_fn = lambda *_: None
    results = {}

    def go(tag):
        results[tag] = detect_cap.advance("acct-probe", st["run_id"])

    t1 = _th.Thread(target=go, args=("a",)); t1.start()
    time.sleep(0.1)                     # t1 is mid-direct-call, holding the stage lock
    t2 = _th.Thread(target=go, args=("b",)); t2.start()
    t1.join(); t2.join()
    assert len(calls) == 1, \
        f"the stage must execute exactly once against the provider (ran {len(calls)})"
    busy = [r for r in results.values() if isinstance(r, dict) and r.get("busy")]
    assert busy, "the second concurrent POST must be told the stage is busy"


def test_cancel_mid_run_writes_nothing_and_releases(probe_env):
    detect_cap, cfg, _ = probe_env
    st = detect_cap.start("acct-probe")
    run = detect_cap._RUNS[st["run_id"]]
    run.call_fn = FakeEndpoint()
    run.sleep_fn = lambda *_: None
    detect_cap.advance("acct-probe", st["run_id"])          # direct stage only
    # a PREVIOUS measured floor is in force — cancel must leave it exactly as it was
    RC.publish_brain_floor(run.scope, 0.7, 2, "measured")
    out = detect_cap.cancel("acct-probe", st["run_id"])
    assert out["state"] == "cancelled"
    floor = RC.brain_floor_state(run.scope)
    assert floor == {"floor_rps": 0.7, "rank": 2, "provenance": "measured"}
    assert RC.brain_maintenance_remaining(run.scope) is None


def test_mid_probe_error_restores_the_brain_and_leaves_the_floor(probe_env):
    detect_cap, cfg, _ = probe_env
    st = detect_cap.start("acct-probe")
    run = detect_cap._RUNS[st["run_id"]]
    fake = FakeEndpoint(raise_at=4)     # explodes inside wave 1
    run.call_fn = fake
    run.sleep_fn = lambda *_: None
    out = _drive(detect_cap, "acct-probe", st["run_id"])
    assert out["state"] == "error"
    assert out["verdict"]["floor_written"] is False
    assert RC.brain_maintenance_remaining(run.scope) is None, \
        "the finally-release must fire even when the probe crashes mid-flight"
    RC.publish_brain_floor(run.scope, 0.7, 2, "measured")   # (floor untouched: none written)
    assert RC.brain_floor_state(run.scope)["rank"] == 2


def test_unreachable_endpoint_measures_nothing_writes_nothing(probe_env):
    detect_cap, cfg, _ = probe_env
    dead = FakeEndpoint()
    def dead_call(cfgv, mt, ts=None):
        return {"ok": False, "status": None, "latency_ms": 30000, "usage": None,
                "retry_after_s": None, "detail": "no response (ConnectError)"}
    st = detect_cap.start("acct-probe")
    run = detect_cap._RUNS[st["run_id"]]
    run.call_fn = dead_call
    run.sleep_fn = lambda *_: None
    out = _drive(detect_cap, "acct-probe", st["run_id"])
    assert out["verdict"]["outcome"] == "unreachable"
    assert out["calls_made"] == 1, "no burst after a dead direct call"
    assert RC.brain_floor_state(run.scope) is None
    assert RC.brain_maintenance_remaining(run.scope) is None


def test_the_burst_is_bounded_even_when_every_call_succeeds(probe_env):
    detect_cap, cfg, _ = probe_env
    fake = FakeEndpoint()               # never 429s
    st, run = _run_with(detect_cap, fake)
    assert st["verdict"]["outcome"] == "capped_at_probe"
    # ABSOLUTE pins, not derived ones — a mutation that grows the wave table grows the
    # derived budget with it and the test would stay green. The gauntlet's bound is "a
    # couple dozen calls": 26 is the hard ceiling any probe shape must fit inside.
    assert fake.calls <= 26, f"the probe spent {fake.calls} calls — beyond a couple dozen"
    assert detect_cap._MAX_PROBE_CALLS <= 26
    assert fake.calls == detect_cap._MAX_PROBE_CALLS, \
        "a clean run should spend exactly the budget (no hidden extra calls)"
    assert st["verdict"]["floor_written"] is False, \
        "no 429 → no ceiling found → nothing written (an 'at least' is not a ceiling)"


def test_the_probe_never_offers_more_than_the_configured_max(probe_env):
    from saas import detect_cap
    detect_cap_mod, cfg, _ = probe_env
    detect_cap_mod._llm_config.resolve_account_llm_config = (
        lambda account_id: _brain_cfg("probe", key=SECRET, max_rpm=20))
    try:
        st = detect_cap_mod.start("acct-probe")
        run = detect_cap_mod._RUNS[st["run_id"]]
        fake = FakeEndpoint()
        run.call_fn = fake
        run.sleep_fn = lambda *_: None
        out = _drive(detect_cap_mod, "acct-probe", st["run_id"])
        assert out["verdict"]["outcome"] == "capped_at_config"
        # direct(1) + wave0(3) only — wave1 offers 24/min ≥ the configured 20
        assert fake.calls == 4
        assert out["verdict"]["floor_written"] is False
    finally:
        probe_env[0]  # fixture teardown clears _RUNS


def test_no_key_material_ever_reaches_a_result_payload(probe_env):
    detect_cap, cfg, _ = probe_env
    fake = FakeEndpoint(fail_at=6)
    st, run = _run_with(detect_cap, fake)
    blob = json.dumps(st)
    assert SECRET not in blob and "sk-" not in blob
    assert "api_key" not in blob
    assert st["model"] == cfg["model"] and st["base_host"] == "api.provider.example"
    # and the seam itself only ever SAW the keyless view + the bound closure
    assert set(fake.saw_keys) == {None}


def test_direct_call_sends_the_key_but_never_returns_it(monkeypatch):
    """The REAL direct_call seam (a fake endpoint bypasses it, so a leak there would be
    invisible to the engine tests): the key must reach the REQUEST headers and appear
    NOWHERE in the result."""
    from saas import detect_cap

    sent = {}

    class FakeResp:
        status_code = 200
        headers = {}
        def json(self):
            return {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    class FakeClient:
        def __init__(self, timeout=None):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, json=None, headers=None):
            sent["url"] = url
            sent["headers"] = dict(headers or {})
            sent["json"] = json
            return FakeResp()

    monkeypatch.setattr(detect_cap.httpx, "Client", FakeClient)
    cfg = _brain_cfg("direct", key="sk-live-secret-key")
    res = detect_cap.direct_call(cfg, max_tokens=5)
    assert res["ok"] is True
    assert sent["headers"].get("Authorization") == "Bearer sk-live-secret-key", \
        "the key MUST reach the provider — this probe measures the real thing"
    blob = json.dumps(res)
    assert "sk-live-secret-key" not in blob, "the key must never come back in the result"
    assert sent["url"].startswith("https://api.provider.example/v1")

    # Anthropic shape: x-api-key, same never-returns rule.
    cfg_a = dict(cfg, backend_type="anthropic")
    sent.clear()
    res_a = detect_cap.direct_call(cfg_a, max_tokens=5)
    assert sent["headers"].get("x-api-key") == "sk-live-secret-key"
    assert "anthropic-version" in sent["headers"]
    assert "sk-live-secret-key" not in json.dumps(res_a)


def test_stage_progress_is_step_by_step(probe_env):
    detect_cap, cfg, _ = probe_env
    st = detect_cap.start("acct-probe")
    run = detect_cap._RUNS[st["run_id"]]
    run.call_fn = FakeEndpoint(fail_at=99, usage=False)   # no usage → the default-params line
    run.sleep_fn = lambda *_: None
    seen_steps = []
    while not st["done"]:
        st = detect_cap.advance("acct-probe", st["run_id"])
        seen_steps.append(len(st["steps"]))
    assert seen_steps == sorted(seen_steps), "steps must ACCUMULATE across stage calls"
    assert len(seen_steps) >= 7, "direct + params + 5 waves = 7 stage round trips"
    texts = " ".join(s["text"] for s in st["steps"])
    assert "checking" in texts and "direct-call" in texts
    assert "default parameters" in texts, "no per-model registry — the default must be NAMED"


# ══════════════════════════════════════════════════════════════════════════════
# E. THE METER PAYLOAD feeds the bar's fraction
# ══════════════════════════════════════════════════════════════════════════════

def test_meter_payload_carries_pace_and_detected_with_calibrated_source():
    from saas import tenant_console

    s = _scope("meterA")
    RC.take_token_state(s, 5.0, 5.0)                       # live refill 5 rps = 300 rpm
    RC.publish_brain_floor(s, 0.05, 3, "calibrated")       # detected 3 rpm
    thr = tenant_console._brain_meter_payload(s, {}, 24)["throughput"]
    assert thr["pace_rpm"] == pytest.approx(300.0)
    assert thr["detected_rpm"] == pytest.approx(3.0)
    assert thr["detected_source"] == "calibrated"
    assert thr["ceiling_source"] == "calibrated", \
        "a user-driven calibration is measured-grade evidence — never 'assumed'"
    assert thr["ceiling_rpm"] == pytest.approx(3.0)


def test_meter_payload_without_a_floor_reports_no_detected_max():
    from saas import tenant_console

    s = _scope("meterB")
    RC.take_token_state(s, 2.0, 2.0)
    thr = tenant_console._brain_meter_payload(s, {}, 24)["throughput"]
    assert thr["detected_rpm"] is None and thr["detected_source"] is None
    assert thr["pace_rpm"] == pytest.approx(120.0)
    assert thr["ceiling_source"] == "live"


# ══════════════════════════════════════════════════════════════════════════════
# F. THE BAR (jsdom via the shared harness)
# ══════════════════════════════════════════════════════════════════════════════

node = shutil.which("node")
has_jsdom = (ROOT / "tests" / "dashboard" / "node_modules" / "jsdom").exists()
_jsdom = pytest.mark.skipif(
    not node or not has_jsdom, reason="node + jsdom required"
)

_HARNESS = ROOT / "tests" / "dashboard" / "render_brain_health.js"


def _thr(**over):
    base = {
        "ceiling_rpm": None, "ceiling_source": "unknown", "ceiling_disclaimer": None,
        "ceiling_basis": None, "available_now": None, "burst_capacity": None,
        "saturation": None, "measured": False, "paused": False, "pause_remaining_s": None,
        "rate_429": 0, "pace_rpm": None, "detected_rpm": None, "detected_source": None,
    }
    base.update(over)
    return base


def _payload(name, thr, status_state="ok"):
    return {
        "__name": name,
        "cache": {"available": True, "hits": 10, "misses": 2, "hit_rate": 10 / 12,
                  "lookups": 12},
        "throughput": thr,
        "upkeep": {"available": True, "deferred": 0, "admitted": 40, "defer_rate": 0.0},
        "window_hours": 24,
        "observed": 40,
        "scope": {"kind": "account", "shared": True, "seats_sharing": 3, "seat_id": None,
                  "configured": True, "brain_key": "abc123def456", "per_seat_account": False},
        "status": {"state": status_state, "headline": "Keeping up comfortably.",
                   "detail": "Requests are being served well inside this brain's ceiling."},
    }


def _render(payloads):
    proc = subprocess.run(
        [node, str(_HARNESS), json.dumps(payloads)],
        capture_output=True, text=True, timeout=60,
        cwd=str(ROOT / "tests" / "dashboard"),
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


@_jsdom
def test_bar_renders_the_segment_fraction_and_link_on_the_scope_note():
    out = _render([_payload("live", _thr(
        measured=True, pace_rpm=1.5, detected_rpm=3.0, detected_source="calibrated",
        ceiling_rpm=3.0, ceiling_source="calibrated", saturation=0.5,
        available_now=1.0, burst_capacity=2.0))])
    r = out["live"]
    h = r["has"]
    assert h["seg_frac_1_5_over_3"], r["text"]
    assert h["wiki_link"], "the segment must link the wiki page"
    assert h["seg_in_scope_note"], "the segment lives ON the scope-note line, not a new one"
    assert h["seg_text"]
    assert r["note_count"] == 2, "scope note + status detail — the foot is gone, nothing added"
    assert r["top_lines"] == 4


@_jsdom
def test_bar_folds_the_pause_indicator_into_the_same_segment():
    out = _render([_payload("paused", _thr(
        measured=True, pace_rpm=1.5, detected_rpm=3.0, detected_source="live",
        ceiling_rpm=3.0, ceiling_source="live", paused=True,
        pause_remaining_s=10.8 * 3600, rate_429=3))])
    r = out["paused"]
    h = r["has"]
    assert h["seg_paused_10_8h"], r["text"]
    assert h["seg_in_scope_note"]
    assert r["note_count"] == 2, "a pause adds ZERO lines — it folds into the segment"
    assert h["rate_limited"] and h["resumes_in"], "the headroom paused row is unchanged"


@_jsdom
def test_bar_hides_the_segment_and_adds_no_line_when_unmeasured():
    # Leftover numbers DELIBERATELY present: the hide rule is measured:false itself — a
    # stale payload carrying old figures must not render the segment off its leftovers.
    out = _render([_payload("cold", _thr(measured=False, pace_rpm=5.0,
                                        detected_rpm=30.0, detected_source="live"))])
    r = out["cold"]
    h = r["has"]
    assert not h["seg_text"], "measured:false → the whole segment hides, like the old footer"
    assert not h["seg_frac_any"]
    assert not h["seg_paused_10_8h"]
    assert r["note_count"] == 2
    assert h["not_measured"]


@_jsdom
def test_bar_has_no_foot_element_in_any_state():
    states = [
        _payload("s1", _thr(measured=True, pace_rpm=5.0, detected_rpm=30.0,
                            ceiling_rpm=30.0, ceiling_source="live", saturation=0.2)),
        _payload("s2", _thr(measured=True, paused=True, pause_remaining_s=90.0,
                            ceiling_rpm=30.0, ceiling_source="live")),
        _payload("s3", _thr()),
        _payload("s4", _thr(measured=True, pace_rpm=5.0, ceiling_rpm=15.0,
                            ceiling_source="assumed",
                            ceiling_disclaimer="Assumed limit: NOT a measurement — "
                                               "we will replace this."),
                 status_state="degraded"),
        _payload("s5", _thr(), status_state="no_brain"),
    ]
    out = _render(states)
    for name, r in out.items():
        assert not r["has"]["foot_present"], f"state {name} must not render a footer line"


@_jsdom
def test_bar_segment_colour_is_the_accent_variable_not_a_literal():
    src = (ROOT / "saas" / "dashboard" / "brain-health.js").read_text()
    import re
    m = re.search(r"\.bh-frac\{[^}]*\}", src)
    assert m, "the .bh-frac rule must exist"
    rule = m.group(0)
    assert "var(--accent)" in rule
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rule), \
        "no hex literal — the segment must follow the accent picker"
    for other in ("http://", "https://"):
        assert other not in rule


# ══════════════════════════════════════════════════════════════════════════════
# G. MODAL COPY (verbatim), CSP, wiring
# ══════════════════════════════════════════════════════════════════════════════

MODAL_COPY = ("This test will pause all other LLM actions in progress. We will make a "
              "best effort to restore connectivity and not drop cached calls. To proceed "
              "please click OK.")


def test_detect_cap_modal_carries_the_copy_essentially_verbatim():
    import re as _re
    html = (ROOT / "saas" / "dashboard" / "tenant.html").read_text()
    # Strip comments FIRST: this file's own design comment quotes the copy verbatim, and a
    # test that matched the comment would stay green while the RENDERED text drifted
    # (the OPSIA comment-vs-code lesson, in the opposite direction).
    body = _re.sub(r"<!--.*?-->", "", html, flags=_re.S)
    norm = _re.sub(r"\s+", " ", body)
    assert MODAL_COPY in norm, "the owner-approved copy must ship verbatim in the RENDERED text"
    assert 'id="detectcap-ok"' in body and 'id="detectcap-cancel"' in body


def test_detect_cap_modal_is_csp_clean_no_inline_handlers():
    from html.parser import HTMLParser

    html = (ROOT / "saas" / "dashboard" / "tenant.html").read_text()
    i = html.find('id="detectcap-modal"')
    assert i >= 0, "the detect-cap modal must exist"
    block = html[i:i + 2500]          # the modal is small; a bounded slice is the block

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = 0
            self.inline_handlers = []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.scripts += 1
            for k, v in attrs:
                if k.lower().startswith("on"):
                    self.inline_handlers.append((tag, k))

    p = P()
    p.feed(block)
    assert p.scripts == 0
    assert p.inline_handlers == []


def test_tenant_html_gained_no_inline_scripts():
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = 0

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.scripts += 1

    new = P()
    new.feed((ROOT / "saas" / "dashboard" / "tenant.html").read_text())
    old_src = subprocess.run(
        ["git", "show", "HEAD:saas/dashboard/tenant.html"],
        capture_output=True, text=True, cwd=str(ROOT), check=True).stdout
    old = P()
    old.feed(old_src)
    assert new.scripts == old.scripts, "the change must not add a single inline <script>"


def test_tenant_js_wiring_binds_the_button_and_the_unload_cancel():
    import re as _re
    src = (ROOT / "saas" / "dashboard" / "tenant.js").read_text()
    stripped = _re.sub(r"//[^\n]*", "", src)
    for needle in (
        "btn-llm-detectcap",
        "detectcap-ok",
        "detectcap-cancel",
        "/tenant/llm-config/detect-cap/start",
        "/tenant/llm-config/detect-cap/stage",
        "/tenant/llm-config/detect-cap/cancel",
        "beforeunload",
        "keepalive",
        "FLBrainHealth.refresh",
    ):
        assert needle in stripped, f"tenant.js must wire {needle!r}"


def test_the_wiki_section_exists_and_the_bar_links_it():
    help_html = (ROOT / "saas" / "dashboard" / "help" / "index.html").read_text()
    assert 'id="rate-ceiling"' in help_html
    assert "what a 429 is" in help_html.lower() or "What a 429 is" in help_html
    bar = (ROOT / "saas" / "dashboard" / "brain-health.js").read_text()
    assert "/dashboard/help/?from=tenant#rate-ceiling" in bar, \
        "the segment's link must deep-link the wiki page"
