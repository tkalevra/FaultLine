"""first-touch-cold-path gauntlet — FT1 "no silent downgrade" + FT3 "episodic honesty".

THE WOUND (pre-prod 2026-09-14, fresh seat): the MCP's first backend calls after provisioning
each hit ReadTimeout and the TRANSPORT substituted its own decisions —
`intent_classify_fallback … defaulting to STATEMENT`, `statement_extractor_route_fallback …
defaulting to rewrite` (while GET /internal/ingest-route said `spine`), and a CRITICAL
`episodic_capture_missed` for an append that later DID land.

THE CONTRACT pinned here (src/mcp/server.py — `_brain_request`, `BrainUnavailable`,
`_classify_and_gate`, `_statement_extractor_route`, `_episodic_capture`,
`_remember_facts_tool_impl`, `retract_fact_tool`):
  * a brain endpoint gets ONE in-turn retry on the transient class (httpx.TransportError /
    retryable status) — https://www.python-httpx.org/exceptions/ puts every timeout under
    TransportError, which is exactly "no settled answer";
  * a retry that lands is used as the brain's decision — no `_fallback`, no CRITICAL;
  * exhaustion NEVER substitutes a weaker intent/route: the turn returns the loud degraded
    status carrying the real cause (never `no_ingest` on stated content), the verbatim is
    retained, and — for a route failure with a brain-settled STATEMENT intent — the turn is
    queued on the deferred drain (which re-asks the brain), never pushed down `rewrite`;
  * retract's old "defaulting to RETRACTION" arm (a DESTRUCTIVE transport guess) is gone.

Driven entirely against a stubbed `_http_client` — no live backend, no LLM. Run through
`tools/fltest.py --bug FT5 --test tests/test_mcp_no_silent_route_downgrade.py`.
"""
import ast
import asyncio
import io
import json
import os
import sys
from contextlib import redirect_stderr
from unittest.mock import MagicMock

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.mcp.server as S  # noqa: E402
from src.mcp.server import remember_facts_tool, retract_fact_tool  # noqa: E402

# Soft import so the REVERT-RED run (pre-contract server.py) fails on the behavioural
# assertions below rather than at collection: on the old code the symbol does not exist and
# every `pytest.raises(BrainUnavailable)` then demands the exception the old code never raised.
BrainUnavailable = getattr(S, "BrainUnavailable", None) or type("BrainUnavailableMissing", (Exception,), {})

_UID = "921514dd-512e-44e1-8f0c-f58090027bd9"
_TEXT = "My sister Dana moved to Halifax last spring and she works as a marine biologist."


def _resp(json_body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=json_body)
    if status >= 400:
        r.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(str(status), request=MagicMock(), response=r))
    else:
        r.raise_for_status = MagicMock()
    return r


class _Backend:
    """Scripted backend. `script[endpoint]` is a list consumed one entry per call; the last
    entry repeats once exhausted. An entry that is an Exception instance is RAISED (a transport
    failure); anything else is returned as the JSON body (200)."""

    def __init__(self, script: dict):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []
        self.bodies: dict[str, list] = {}

    def _key(self, url):
        for k in self.script:
            if k in url:
                return k
        return None

    def _step(self, url):
        k = self._key(url)
        self.calls.append(k or url)
        if k is None:
            return _resp({"status": "ok"})
        seq = self.script[k]
        step = seq[0] if len(seq) == 1 else seq.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, tuple):  # (body, status)
            return _resp(step[0], step[1])
        return _resp(step)

    async def post(self, url, *a, **k):
        key = self._key(url) or url
        self.bodies.setdefault(key, []).append(k.get("json"))
        return self._step(url)

    async def get(self, url, *a, **k):
        return self._step(url)


@pytest.fixture(autouse=True)
def _pin_ready(monkeypatch):
    S._FAULTLINE_URL_DETECTED = True
    S._provisioned_users.add(_UID)
    # brain-independence lane: a classify-timeout test below marks the brain-slow window
    # open for 60s of wall time — without this reset the LATER recall tests silently take
    # the fast path and their call-sequence pins break (order-dependent red).
    S._brain_slow_reset("test isolation")
    S._statement_defer_queue = None
    S._statement_defer_task = None
    for k in S._statement_defer_stats:
        S._statement_defer_stats[k] = 0
    # the deferred drain worker must not actually run against the stub during the test
    monkeypatch.setattr(S, "_statement_defer_worker", _sleep_forever)
    # keep the experience/notice lanes out of the way (they are additive, not under test)
    monkeypatch.setattr(S, "_with_experience", _passthrough)
    # round 3: the read-back POLLS over the brain-declared window; keep it short for tests
    # (declared deadline 0.3s + one probe budget 0.2s → ~0.5s window, backoff 0.25/0.5).
    # round 4: the episodic re-append drain — reset state, pin its backoff to 0 (soft on the
    # attributes so a REVERT-RED run against an older server.py fails on behaviour, not setup)
    _decl = getattr(S, "_brain_declared", None)
    if isinstance(_decl, dict):
        _decl.clear()
    _knobs = ("_EPISODIC_REAPPEND_BASE_S", "_EPISODIC_REAPPEND_MAX_DELAY_S",
              "_EPISODIC_UNCONFIRMED_CRIT_S", "_EPISODIC_SPOOL_DIR", "_episodic_spool_recovered",
              "_episodic_spool_unwritable_logged", "_EPISODIC_PROV_KICK_INTERVAL_S")
    _saved = {k: getattr(S, k, None) for k in _knobs}
    if hasattr(S, "_EPISODIC_REAPPEND_BASE_S"):
        S._EPISODIC_REAPPEND_BASE_S = 0.0
        S._EPISODIC_REAPPEND_MAX_DELAY_S = 0.0
        S._EPISODIC_UNCONFIRMED_CRIT_S = 3600.0  # tests that want the CRITICAL set it to 0
        if hasattr(S, "_EPISODIC_PROV_KICK_INTERVAL_S"):
            S._EPISODIC_PROV_KICK_INTERVAL_S = 0.0  # round 10: a kick per seat_unknown unless a test pins it
        S._episodic_spool_unwritable_logged = False
        import tempfile
        S._EPISODIC_SPOOL_DIR = tempfile.mkdtemp(prefix="ft-spool-")
        S._episodic_spool_recovered = False
        S._episodic_reappend_queue = None
        S._episodic_reappend_task = None
        S._episodic_reappend_inflight = None
        if hasattr(S, "_episodic_reappend_parked"):  # round 11: the delay heap + per-seat kicks
            S._episodic_reappend_parked[:] = []
            S._episodic_seat_kicks.clear()
            S._episodic_reappend_wake = None
        for k in S._episodic_reappend_stats:
            S._episodic_reappend_stats[k] = 0
    yield
    if isinstance(_decl, dict):
        _decl.clear()
    S._brain_slow_reset("test isolation")
    if hasattr(S, "_EPISODIC_REAPPEND_BASE_S"):
        _t = getattr(S, "_episodic_reappend_task", None)
        if _t is not None and not _t.done():
            _t.cancel()
        import shutil
        shutil.rmtree(S._EPISODIC_SPOOL_DIR, ignore_errors=True)
        for k, v in _saved.items():
            if v is not None or hasattr(S, k):
                setattr(S, k, v)
        S._episodic_reappend_queue = None
        S._episodic_reappend_task = None
        S._episodic_reappend_inflight = None
    if S._statement_defer_task is not None and not S._statement_defer_task.done():
        S._statement_defer_task.cancel()
    S._statement_defer_queue = None
    S._statement_defer_task = None
    S._provisioned_users.discard(_UID)


async def _sleep_forever():
    await asyncio.sleep(3600)


async def _passthrough(user_id, text, result):
    return result


_WATCHDOG_S = 25.0   # round 14: no test may hold the loop longer than this
_STALL_S = 4.0       # …and the consumer's loop-liveness heartbeat may not stall longer than this


class _Poison:
    """Swapped in as the backend when the loop is declared dead: a consumer that never yields is
    still CALLING the backend in its tight loop — SystemExit out of that call ends its loop and
    its thread, so a hung mutant cannot keep a core spinning for the rest of the session."""
    def __getattr__(self, name):
        async def _boom(*a, **k):
            raise SystemExit("watchdog: event loop stalled")
        return _boom


def _run(coro, watchdog_s=_WATCHDOG_S):
    """Run `coro` on its own loop in a WATCHDOG THREAD. `wait_for` can bound an await, but nothing
    inside a loop can bound a consumer that never yields the loop — only another thread can. The
    watchdog fails the test (in seconds, never a hang) when the thread outlives `watchdog_s`, or
    when the drain's loop-liveness heartbeat (`S._episodic_loop_alive_mono`, stamped from a loop
    callback per pick) stalls for `_STALL_S` while a pick is in flight."""
    import threading, time as _time
    buf = io.StringIO()
    box = {}
    def body():
        with redirect_stderr(buf):
            try:
                box["out"] = asyncio.run(coro)
            except BaseException as exc:  # noqa: BLE001 — re-raised in the test thread
                box["exc"] = exc
    t = threading.Thread(target=body, daemon=True)
    t.start()
    t0 = _time.monotonic()
    stalled = None
    while t.is_alive():
        t.join(0.1)
        alive = max(getattr(S, "_episodic_loop_alive_mono", 0.0), t0)  # never stamped → count from the start
        task = getattr(S, "_episodic_reappend_task", None)
        worker_alive = task is not None and not task.done()
        # round 15: the stall is keyed on the LOOP TURNING (the ticker callback stamps every 0.25s
        # while the worker lives), never on how long a pick takes — a slow but yielding pick is fine
        if worker_alive and (_time.monotonic() - alive) > _STALL_S:
            stalled = f"loop-liveness heartbeat stalled > {_STALL_S}s while the drain worker is alive (the loop is not turning)"
            break
        if _time.monotonic() - t0 > watchdog_s:
            stalled = f"the test's event loop did not finish within {watchdog_s}s"
            break
    if stalled:
        S._http_client = _Poison()
        t.join(3.0)
        raise AssertionError(f"WATCHDOG: {stalled}; alive={t.is_alive()}\n{buf.getvalue()[-1500:]}")
    if "exc" in box:
        raise box["exc"]
    return box.get("out"), buf.getvalue()


def _timeout(msg="read timed out"):
    return httpx.ReadTimeout(msg, request=MagicMock())


_SPINE_OK = {
    "/classify-intent": [{"intent": "STATEMENT", "confidence": 1.0}],
    "/confidence-gate": [{"threshold": 0.7}],
    "/episodic/append": [{"status": "stored", "id": 1}],
    "/internal/ingest-route": [{"statement_extractor": "spine"}],
}


# ── the retry-once contract ─────────────────────────────────────────────────────────────

def test_brain_request_retries_once_then_uses_the_landed_answer(monkeypatch):
    """A single ReadTimeout on /classify-intent is retried in-turn; the landed answer is the
    brain's decision. No `_fallback` line, no CRITICAL, intent NOT guessed."""
    be = _Backend({
        "/classify-intent": [_timeout(), {"intent": "CORRECTION", "confidence": 0.93}],
        "/confidence-gate": [{"threshold": 0.7}],
    })
    monkeypatch.setattr(S, "_http_client", be)
    (intent, conf, gate), log = _run(S._classify_and_gate(_TEXT, _UID))
    assert intent == "CORRECTION" and conf == pytest.approx(0.93)
    assert be.calls.count("/classify-intent") == 2, be.calls
    assert "_fallback" not in log
    assert "brain_call.retry_landed" in log


def test_brain_request_gives_up_after_one_retry_with_the_real_cause(monkeypatch):
    be = _Backend({"/classify-intent": [_timeout("boom")]})
    monkeypatch.setattr(S, "_http_client", be)
    with pytest.raises(BrainUnavailable) as ei:
        _run(S._classify_and_gate(_TEXT, _UID))
    assert be.calls.count("/classify-intent") == getattr(S, "_BRAIN_RETRY_ATTEMPTS", None) == 2
    assert ei.value.endpoint == "/classify-intent"
    assert "ReadTimeout" in ei.value.cause and ei.value.attempts == 2


def test_route_never_defaults_to_rewrite_on_timeout(monkeypatch):
    """The old fail-safe returned "rewrite" on ANY error. A brain that says `spine` and then
    times out must NOT be answered with `rewrite` by the transport."""
    be = _Backend({"/internal/ingest-route": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be)
    with pytest.raises(BrainUnavailable) as ei:
        _run(S._statement_extractor_route(_UID))
    assert ei.value.endpoint == "/internal/ingest-route"
    assert be.calls.count("/internal/ingest-route") == 2


def test_route_retry_lands_and_the_brain_decision_is_used(monkeypatch):
    be = _Backend({"/internal/ingest-route": [_timeout(), {"statement_extractor": "spine"}]})
    monkeypatch.setattr(S, "_http_client", be)
    route, log = _run(S._statement_extractor_route(_UID))
    assert route == "spine"
    assert "statement_extractor_route_fallback" not in log and "_fallback" not in log


def test_backend_503_provisioning_signal_is_retried_then_surfaced(monkeypatch):
    """The backend's own `_provisioning_retry_503` is in the retry set: one retry, then loud."""
    be = _Backend({"/internal/ingest-route": [({"status": "provisioning"}, 503)]})
    monkeypatch.setattr(S, "_http_client", be)
    with pytest.raises(BrainUnavailable) as ei:
        _run(S._statement_extractor_route(_UID))
    assert "HTTP 503" in ei.value.cause


def test_brain_timeouts_are_env_driven_not_literals(monkeypatch):
    # brain-independence lane (2026-10-25): the TABLE default stays 10.0 — the WRITE lanes
    # (remember/retract) consume escalated verdicts whose server-side budgets are 4s+4s.
    # The RECALL probe rides its own tight knob (MCP_CLASSIFY_TIMEOUT_S, default 3s) passed
    # explicitly at recall's call site — under the LLM-tier budgets it is escaping.
    assert S._brain_timeout("CLASSIFY_INTENT") == 10.0
    monkeypatch.setenv("MCP_BRAIN_TIMEOUT_CLASSIFY_INTENT", "3.5")
    assert S._brain_timeout("classify_intent") == 3.5
    monkeypatch.delenv("MCP_CLASSIFY_TIMEOUT_S", raising=False)
    assert S._env_budget("MCP_CLASSIFY_TIMEOUT_S", 3.0) == 3.0   # the recall probe default
    monkeypatch.setenv("MCP_CLASSIFY_TIMEOUT_S", "1.5")
    assert S._env_budget("MCP_CLASSIFY_TIMEOUT_S", 3.0) == 1.5
    monkeypatch.setenv("MCP_BRAIN_TIMEOUT_INGEST_ROUTE", "nonsense")
    assert S._brain_timeout("INGEST_ROUTE") == 5.0  # invalid → default, not a crash


# ── FT1 through the tool: remember_facts ────────────────────────────────────────────────

def test_remember_facts_classify_timeout_is_loud_not_statement(monkeypatch):
    """Both /classify-intent attempts time out → loud degraded with the cause; the verbatim IS
    retained (episodic append happened); NOTHING was routed as STATEMENT (no /internal/ingest-
    route, no /harvest-spans, no /extract/rewrite, no /ingest); never `no_ingest`."""
    be = _Backend({
        "/classify-intent": [_timeout()],
        "/episodic/append": [{"status": "stored", "id": 1}],
    })
    monkeypatch.setattr(S, "_http_client", be)
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    assert out["status"] == "degraded" and out["isError"] is True
    assert out["status"] != "no_ingest"
    assert out["brain_unavailable"] == "/classify-intent"
    assert "ReadTimeout" in out["cause"] and out["attempts"] == 2
    assert out["committed"] == 0
    assert "/episodic/append" in be.calls, "verbatim floor must still be written"
    for forbidden in ("/internal/ingest-route", "/harvest-spans", "/extract/rewrite", "/ingest"):
        assert forbidden not in be.calls, f"{forbidden} was called — the transport guessed STATEMENT"
    assert "intent_classify_fallback" not in log
    assert "defaulting to STATEMENT" not in log
    assert "brain_unavailable" in log  # the CRITICAL names the real cause


def test_remember_facts_route_timeout_defers_never_rewrites(monkeypatch):
    """Intent settled (STATEMENT), then /internal/ingest-route times out twice → the turn is
    queued on the deferred drain (which re-asks the brain) and the caller is told loudly. The
    detect-only /extract/rewrite lane is NEVER entered by transport decision."""
    be = _Backend({
        "/classify-intent": [{"intent": "STATEMENT", "confidence": 1.0}],
        "/confidence-gate": [{"threshold": 0.7}],
        "/episodic/append": [{"status": "stored", "id": 1}],
        "/internal/ingest-route": [_timeout()],
    })
    monkeypatch.setattr(S, "_http_client", be)
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    assert out["status"] == "degraded" and out["isError"] is True
    assert out["brain_unavailable"] == "/internal/ingest-route"
    assert out["deferred"] is True and out["queued"] is True and out["retry_pending"] is True
    assert S._statement_defer_stats["deferred"] == 1
    assert S._statement_defer_queue is not None and S._statement_defer_queue.qsize() == 1
    # Vehicle update (foss-es uplift port): the producer's queue item grew a lane + timestamp
    # ((text, user_id, ingest_source, lane, ts) — the docstring calls the 3-tuple "pre-lane
    # legacy"). The MECHANISM assertion is unchanged: authorship (text, uid, "mcp") preserved.
    queued_text, queued_uid, queued_src, queued_lane, _queued_ts = S._statement_defer_queue.get_nowait()
    assert (queued_text, queued_uid, queued_src) == (_TEXT, _UID, "mcp")  # authorship preserved
    assert queued_lane == "STATEMENT"
    for forbidden in ("/extract/rewrite", "/harvest-spans", "/ingest"):
        assert forbidden not in be.calls, f"{forbidden} was called — the transport substituted a route"
    assert "statement_extractor_route_fallback" not in log
    assert "defaulting to rewrite" not in log
    assert be.calls.count("/internal/ingest-route") == 2


def test_remember_facts_healthy_path_unchanged_when_retry_lands(monkeypatch):
    """A blip on the FIRST classify attempt that lands on the retry must produce a normal
    spine turn: brain decision honoured, zero fallback lines, zero CRITICALs."""
    be = _Backend({
        "/classify-intent": [_timeout(), {"intent": "STATEMENT", "confidence": 1.0}],
        "/confidence-gate": [{"threshold": 0.7}],
        "/episodic/append": [{"status": "stored", "id": 1}],
        "/internal/ingest-route": [{"statement_extractor": "spine"}],
        "/harvest-spans": [{"edges": [{"subject": "dana", "rel_type": "lives_in",
                                       "object": "halifax"}]}],
        "/ingest": [{"status": "valid", "committed": 1, "staged": 0, "facts": []}],
    })
    monkeypatch.setattr(S, "_http_client", be)
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    assert out.get("committed") == 1, out
    assert "/harvest-spans" in be.calls and "/extract/rewrite" not in be.calls
    assert "_fallback" not in log
    assert "CRITICAL" not in log and "episodic_capture_missed" not in log


# ── FT3: episodic honesty ───────────────────────────────────────────────────────────────

_JOIN_S = 15.0  # round 13: every drain join is bounded — fltest has no pytest timeout


def _reset_drain():
    """The queue/worker are bound to the loop that created them; a test that runs a second
    `asyncio.run` must start the drain fresh (module state, like the fixture does)."""
    t = getattr(S, "_episodic_reappend_task", None)
    if t is not None and not t.done():
        t.cancel()
    S._episodic_reappend_queue = None
    S._episodic_reappend_task = None
    if hasattr(S, "_episodic_reappend_parked"):
        S._episodic_reappend_parked[:] = []
        S._episodic_seat_kicks.clear()
        S._episodic_reappend_wake = None


async def _capture_then_drain(text, uid, source="STATEMENT"):
    """Run one capture and, if a re-append was queued, let the drain finish it (in-loop).
    Returns the capture's retention verdict."""
    _reset_drain()
    v = await S._episodic_capture(text, uid, source)
    q = S._episodic_reappend_queue
    if q is not None:
        await asyncio.wait_for(q.join(), timeout=_JOIN_S)  # bounded: a hanging mutant FAILS, never hangs the file
    return v


def test_episodic_critical_only_when_genuinely_not_retained(monkeypatch):
    # (a) first append times out, retry lands → NO CRITICAL, retry logged, no probe, no drain
    be = _Backend({"/episodic/append": [_timeout(), {"status": "stored", "id": 7}]})
    monkeypatch.setattr(S, "_http_client", be)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    assert "episodic_capture_missed" not in log and "brain_call.retry_landed" in log
    assert be.calls.count("/episodic/append") == 2 and "/episodic/probe" not in be.calls
    assert S._episodic_reappend_stats["queued"] == 0
    # (b) a soft_error BODY at HTTP 200 on the in-turn attempt is still a non-capture
    be3 = _Backend({"/episodic/append": [{"status": "soft_error", "id": None}]})
    monkeypatch.setattr(S, "_http_client", be3)
    _, log3 = _run(_capture_then_drain(_TEXT, _UID))
    assert "episodic_capture_missed" in log3


def test_probe_absent_never_escalates_it_queues_an_idempotent_reappend(monkeypatch):
    """ROUND 4: both appends time out, the fast-path probe says ABSENT (the row is stalled
    before dispatch, or genuinely absent — the transport cannot tell) → WARN unverified + a
    re-append with the SAME turn_key on the drain; the drain's `stored` answer confirms. No
    CRITICAL anywhere, no wall-clock verdict."""
    be = _Backend({"/episodic/append": [_timeout(), _timeout(), {"status": "stored", "id": 5}],
                   "/episodic/probe": [{"retained": False, "id": None, "status": "ok"}]})
    monkeypatch.setattr(S, "_http_client", be)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    assert "CRITICAL" not in log and "episodic_capture_missed" not in log
    assert "WARN episodic_capture_unverified" in log and "re-append QUEUED" in log
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=5" in log
    assert be.calls.count("/episodic/probe") == 1  # fast path only — never polled to a verdict
    keys = {b["turn_key"] for b in be.bodies["/episodic/append"]}
    assert len(be.bodies["/episodic/append"]) == 3 and len(keys) == 1, be.bodies  # same key, all 3
    assert S._episodic_reappend_stats["confirmed"] == 1 and S._episodic_reappend_stats["missed"] == 0


def test_drain_deduplicated_answer_is_a_confirmation(monkeypatch):
    """The late-dispatched original landed first; the drain's re-append conflicts on turn_key
    and is answered `deduplicated` — that IS the confirmation (one row server-side)."""
    be = _Backend({"/episodic/append": [_timeout(), _timeout(),
                                        {"status": "stored", "id": 3, "deduplicated": True}],
                   "/episodic/probe": [_timeout("probe dead")]})
    monkeypatch.setattr(S, "_http_client", be)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=3" in log
    assert "CRITICAL" not in log


def test_transport_exhaustion_is_never_terminal_and_recovery_confirms(monkeypatch):
    """ROUND 5: the API is down for longer than any attempt count would cover (12 transport
    failures, more than the old 8-attempt span), then comes back → the turn is STILL queued,
    the drain confirms it, `CRITICAL episodic_capture_unconfirmed` was emitted exactly ONCE
    past the loud threshold and then cleared; never `missed`, never a terminal WARN."""
    S._EPISODIC_UNCONFIRMED_CRIT_S = 0.0  # loud immediately
    # 2 in-turn failures + 9 drain attempts × 2 calls (each a _brain_request attempt+retry) =
    # 20 transport failures; the 10th drain attempt lands. 10 > 8 (round-4 cap) and > 7.
    be = _Backend({"/episodic/append": [_timeout("dead")] * 20 + [{"status": "stored", "id": 11}],
                   "/episodic/probe": [_timeout("dead")]})
    monkeypatch.setattr(S, "_http_client", be)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    assert "episodic_capture_missed" not in log and "episodic_reappend_exhausted" not in log
    assert log.count("CRITICAL episodic_capture_unconfirmed") == 1, log
    assert "turn_key=" in log and f"user={_UID[:8]}" in log
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=11" in log
    assert "on drain attempt 10 " in log, log  # attempts > 8 (and > 7) — any cap ≤ 9 goes red here
    assert "episodic_capture_unconfirmed_cleared" in log
    assert S._episodic_reappend_stats["confirmed"] == 1 and S._episodic_reappend_stats["unconfirmed_crit"] == 1
    assert be.calls.count("/episodic/append") == 21
    keys = {b["turn_key"] for b in be.bodies["/episodic/append"]}
    assert len(keys) == 1  # the same key on every one of the 21 attempts


def test_server_503_is_transport_never_a_refusal(monkeypatch):
    """The server answering 503 {"status":"unavailable","retryable":true} (DB refusing
    connections) keeps the drain retrying — never a CRITICAL missed (the round-4 false-CRITICAL
    shape). A 200 `unavailable` body is the same class."""
    S._EPISODIC_UNCONFIRMED_CRIT_S = 3600.0
    be = _Backend({"/episodic/append": [_timeout()] * 2
                                        + [({"status": "unavailable", "retryable": True}, 503)] * 6
                                        + [{"status": "stored", "id": 2}],
                   "/episodic/probe": [({"status": "unavailable", "retryable": True}, 503)]})
    monkeypatch.setattr(S, "_http_client", be)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    assert "CRITICAL" not in log and "episodic_capture_missed" not in log
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=2" in log
    assert "transport: HTTP 503" in log or "transport: http=503" in log
    # in-turn: 200 body `unavailable` on the FIRST attempt → queued straight away, no probe
    be2 = _Backend({"/episodic/append": [{"status": "unavailable", "retryable": True},
                                         {"status": "stored", "id": 3}]})
    monkeypatch.setattr(S, "_http_client", be2)
    _, log2 = _run(_capture_then_drain(_TEXT, _UID))
    assert "server answered retryable" in log2 and "episodic_capture_confirmed_by_drain" in log2
    assert "CRITICAL" not in log2


def test_drain_positive_refusal_is_terminal_on_the_first_answer(monkeypatch):
    """The backend ANSWERS the re-append with a refusal (soft_error body / non-retry 4xx) — the
    server reserves that class for malformed key / unknown seat / policy, so the FIRST such answer
    is a confirmed loss: CRITICAL episodic_capture_missed, and the queue behind it is not blocked."""
    be = _Backend({"/episodic/append": [_timeout(), _timeout(), {"status": "soft_error", "id": None}],
                   "/episodic/probe": [{"retained": False, "id": None, "status": "ok"}]})
    monkeypatch.setattr(S, "_http_client", be)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    assert "CRITICAL episodic_capture_missed" in log and "POSITIVELY refused" in log
    assert S._episodic_reappend_stats["missed"] == 1
    # REFUSE FAST (round 6): the FIRST refusal-class answer is terminal — 2 in-turn timeouts + 1
    assert be.calls.count("/episodic/append") == 2 + 1, be.calls
    # and a 4xx refusal is the same class
    be2 = _Backend({"/episodic/append": [_timeout(), _timeout(), ({"detail": "no"}, 422)],
                    "/episodic/probe": [{"retained": False, "id": None, "status": "ok"}]})
    monkeypatch.setattr(S, "_http_client", be2)
    _, log2 = _run(_capture_then_drain(_TEXT, _UID))
    assert "CRITICAL episodic_capture_missed" in log2 and "http=422" in log2


def test_probe_fast_path_retained_clears_without_a_drain(monkeypatch):
    be = _Backend({"/episodic/append": [_timeout()],
                   "/episodic/probe": [{"retained": True, "id": 7, "status": "ok"}]})
    monkeypatch.setattr(S, "_http_client", be)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    assert "episodic_capture_confirmed_after_timeout" in log and "episodic_id=7" in log
    assert "WARN" not in log and "CRITICAL" not in log
    assert S._episodic_reappend_stats["queued"] == 0
    key = be.bodies["/episodic/append"][0]["turn_key"]
    assert be.bodies["/episodic/probe"][-1] == {"user_id": _UID, "raw_text": _TEXT,
                                                "source": "mcp", "turn_key": key}


def test_same_turn_key_on_both_attempts_and_a_new_key_per_turn(monkeypatch):
    be = _Backend({"/episodic/append": [_timeout(), {"status": "stored", "id": 3, "deduplicated": True}]})
    monkeypatch.setattr(S, "_http_client", be)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    b1, b2 = be.bodies["/episodic/append"]
    assert b1["turn_key"] and b1["turn_key"] == b2["turn_key"] and len(b1["turn_key"]) >= 16
    assert "CRITICAL" not in log and "WARN" not in log
    be2 = _Backend({"/episodic/append": [{"status": "stored", "id": 4}]})
    monkeypatch.setattr(S, "_http_client", be2)
    _run(_capture_then_drain(_TEXT, _UID))
    assert be2.bodies["/episodic/append"][0]["turn_key"] != b1["turn_key"]


def test_no_wall_clock_window_decides_retention(monkeypatch):
    """A declared deadline of 0 (unbounded) or none at all must change nothing: retention is
    decided by an ANSWERED append only. There is no window symbol left to consult."""
    assert not hasattr(S, "_episodic_probe_window_s") and not hasattr(S, "_episodic_probe_poll")
    for declared in (0, None, 30):
        S._brain_declared.clear()
        if declared is not None:
            S._brain_declared["episodic_append_deadline_s"] = declared
        be = _Backend({"/episodic/append": [_timeout(), _timeout(), {"status": "stored", "id": 1}],
                       "/episodic/probe": [{"retained": False, "id": None, "status": "ok"}]})
        monkeypatch.setattr(S, "_http_client", be)
        _, log = _run(_capture_then_drain(_TEXT, _UID))
        assert "CRITICAL" not in log and "episodic_capture_confirmed_by_drain" in log, declared
        assert be.calls.count("/episodic/probe") == 1


def test_reappend_queue_full_is_loud_and_leaves_the_turn_unverified(monkeypatch):
    be = _Backend({"/episodic/append": [_timeout("dead")], "/episodic/probe": [_timeout("dead")]})
    monkeypatch.setattr(S, "_http_client", be)
    _real = S._queue_episodic_reappend
    async def _prefill_then_real(body):
        S._episodic_reappend_queue = asyncio.Queue(maxsize=1)
        S._episodic_reappend_queue.put_nowait({"x": 1})
        S._episodic_reappend_task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        return await _real(body)
    monkeypatch.setattr(S, "_queue_episodic_reappend", _prefill_then_real)
    _, log = _run(S._episodic_capture(_TEXT, _UID, "STATEMENT"))
    assert "CRITICAL episodic_reappend_queue_full" in log and "NOT queued (drain refused)" in log
    assert "episodic_capture_missed" not in log
    S._episodic_reappend_task.cancel()


# ── round-5: worker robustness, shutdown inventory, spool, and honest wording ──────────────

def test_cancelled_inflight_body_is_requeued(monkeypatch):
    gate = {}
    class _Blocking(_Backend):
        async def post(self, url, *a, **k):
            if "/episodic/append" in url and gate.get("block"):
                await asyncio.Event().wait()  # never returns — in flight until cancelled
            return await super().post(url, *a, **k)
    be = _Blocking({"/episodic/append": [_timeout(), _timeout()], "/episodic/probe": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be)
    async def drive():
        await S._episodic_capture(_TEXT, _UID, "STATEMENT")  # queues (worker started)
        gate["block"] = True
        await asyncio.sleep(0.05)  # let the worker pick the body up and block in flight
        assert S._episodic_reappend_inflight is not None
        S._episodic_reappend_task.cancel()
        try:
            await S._episodic_reappend_task
        except asyncio.CancelledError:
            pass
        return S._episodic_reappend_queue.qsize()
    depth, log = _run(drive())
    assert depth == 1 and "episodic_reappend_requeued_on_cancel" in log
    assert S._episodic_reappend_stats["requeued_on_cancel"] == 1
    assert S._episodic_reappend_inflight is None
    # round 6: task_done() exactly once — no ValueError over the cancel, no spurious worker_died
    assert "worker_died" not in log and "ValueError" not in log


def test_task_done_is_called_exactly_once_per_get_across_a_cancel(monkeypatch):
    """ROUND 7 pin of the round-6 fix, instrumented on the QUEUE so the double-call shape goes
    red by COUNT (the old pin passed on the round-5 module because the ValueError surfaced after
    the test had already returned): one get() → exactly one task_done(), even when the in-flight
    attempt is cancelled; the worker ends CANCELLED, not with an exception."""
    class _Blocking(_Backend):
        async def post(self, url, *a, **k):
            if "/episodic/append" in url and calls.get("block"):
                await asyncio.Event().wait()
            return await super().post(url, *a, **k)
    calls = {}
    be = _Blocking({"/episodic/append": [_timeout(), _timeout()], "/episodic/probe": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be)
    counts = {"get": 0, "task_done": 0}
    async def drive():
        _reset_drain()
        q = asyncio.Queue(maxsize=S._INGEST_DEFER_QUEUE_MAX)
        real_get, real_done = q.get, q.task_done
        async def counted_get():
            counts["get"] += 1
            return await real_get()
        def counted_done():
            counts["task_done"] += 1
            return real_done()
        q.get, q.task_done = counted_get, counted_done
        S._episodic_reappend_queue = q
        await S._episodic_capture(_TEXT, _UID, "STATEMENT")   # queues, starts the worker
        calls["block"] = True
        await asyncio.sleep(0.05)
        assert counts["get"] == 1 and S._episodic_reappend_inflight is not None
        t = S._episodic_reappend_task
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)  # let done-callbacks run
        return t.cancelled(), (None if t.cancelled() else t.exception())
    (cancelled, exc), log = _run(drive())
    assert counts["get"] == 1 and counts["task_done"] == 1, counts
    assert cancelled is True and exc is None
    assert "worker_died" not in log


def test_worker_death_is_critical_and_it_restarts(monkeypatch):
    be = _Backend({"/episodic/append": [_timeout(), _timeout(), {"status": "stored", "id": 8}],
                   "/episodic/probe": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be)
    async def drive():
        _reset_drain()
        # a poisoned worker: crash it on entry, then let the real one take over
        real = S._episodic_reappend_worker
        async def _dies():
            raise RuntimeError("worker crashed")
        monkeypatch.setattr(S, "_episodic_reappend_worker", _dies)
        await S._episodic_capture(_TEXT, _UID, "STATEMENT")
        await asyncio.sleep(0.02)
        monkeypatch.setattr(S, "_episodic_reappend_worker", real)
        S._ensure_reappend_worker()  # supervision: a dead worker is restarted
        await asyncio.wait_for(S._episodic_reappend_queue.join(), timeout=_JOIN_S)
    _, log = _run(drive())
    assert "CRITICAL episodic_reappend_worker_died" in log
    assert S._episodic_reappend_stats["worker_restarts"] == 1
    assert "episodic_capture_confirmed_by_drain" in log


def test_shutdown_flush_logs_a_critical_inventory_of_unconfirmed_turns(monkeypatch):
    be = _Backend({"/episodic/append": [_timeout("dead")], "/episodic/probe": [_timeout("dead")]})
    monkeypatch.setattr(S, "_http_client", be)
    async def drive():
        _reset_drain()
        await S._episodic_capture("turn one", _UID, "STATEMENT")
        await S._episodic_capture("turn two", _UID, "STATEMENT")
        return await S.episodic_shutdown_flush(timeout_s=0.1)
    left, log = _run(drive())
    keys = {b["turn_key"] for b in be.bodies["/episodic/append"]}
    assert len(left) == 2 and {k for _, k in left} == keys and all(u == _UID[:8] for u, _ in left)
    assert "CRITICAL episodic_shutdown_unconfirmed_inventory" in log and "2 turn(s) NOT confirmed" in log
    inv_line = next(l for l in log.splitlines() if "episodic_shutdown_unconfirmed_inventory" in l)
    for k in keys:
        assert k in inv_line  # every unconfirmed key is named IN the inventory line itself
    # round 6: the inventory is printed BEFORE the flush wait (SIGKILL-safe), then a completion line
    assert log.index("episodic_shutdown_unconfirmed_inventory") < log.index("episodic_shutdown_flush:")
    assert "episodic_shutdown_complete: flushed 0 of 2; 2 still unconfirmed" in log
    assert S._episodic_reappend_task.done()


def test_shutdown_inventory_is_printed_before_a_flush_that_would_outlive_the_grace(monkeypatch):
    """A flush budget of 0.5s with a dead backend: the inventory line must already be in the log
    when the flush starts, and the whole call returns inside ~the budget."""
    be = _Backend({"/episodic/append": [_timeout("dead")], "/episodic/probe": [_timeout("dead")]})
    monkeypatch.setattr(S, "_http_client", be)
    import time
    async def drive():
        _reset_drain()
        await S._episodic_capture("turn one", _UID, "STATEMENT")
        t0 = time.monotonic()
        left = await S.episodic_shutdown_flush(timeout_s=0.5)
        return left, time.monotonic() - t0
    (left, took), log = _run(drive())
    assert len(left) == 1 and took < 2.0, took
    lines = log.splitlines()
    i_inv = next(i for i, l in enumerate(lines) if "episodic_shutdown_unconfirmed_inventory" in l)
    i_fl = next(i for i, l in enumerate(lines) if "episodic_shutdown_flush:" in l)
    i_done = next(i for i, l in enumerate(lines) if "episodic_shutdown_complete" in l)
    assert i_inv < i_fl < i_done
    assert left[0][1] in lines[i_inv]


def test_unconfirmed_critical_is_per_turn_not_only_the_head(monkeypatch):
    """Three turns queued behind a dead backend, threshold 0: THREE CRITICALs, one per turn_key,
    each exactly once — the two waiting behind the stuck head are loud too."""
    S._EPISODIC_UNCONFIRMED_CRIT_S = 0.0
    be = _Backend({"/episodic/append": [_timeout("dead")], "/episodic/probe": [_timeout("dead")]})
    monkeypatch.setattr(S, "_http_client", be)
    async def drive():
        _reset_drain()
        for t in ("turn one", "turn two", "turn three"):
            await S._episodic_capture(t, _UID, "STATEMENT")
        await asyncio.sleep(0.2)  # a few head attempts + sweeps
        S._episodic_reappend_task.cancel()
        try:
            await S._episodic_reappend_task
        except asyncio.CancelledError:
            pass
    _, log = _run(drive())
    keys = {b["turn_key"] for b in be.bodies["/episodic/append"]}
    assert len(keys) == 3
    crit = [l for l in log.splitlines() if "CRITICAL episodic_capture_unconfirmed" in l]
    assert len(crit) == 3, crit
    assert {next(k for k in keys if k in l) for l in crit} == keys  # one per key
    assert S._episodic_reappend_stats["unconfirmed_crit"] == 3


def test_corrupt_spool_file_is_critical_once_and_quarantined(monkeypatch):
    bad = os.path.join(S._EPISODIC_SPOOL_DIR, "notjson.json")
    os.makedirs(S._EPISODIC_SPOOL_DIR, exist_ok=True)
    with open(bad, "w") as fh:
        fh.write("{ this is not json")
    incomplete = os.path.join(S._EPISODIC_SPOOL_DIR, "incomplete.json")
    with open(incomplete, "w") as fh:
        fh.write('{"turn_key": "abc"}')  # no user_id / raw_text
    be = _Backend({"/episodic/append": [{"status": "stored", "id": 1}]})
    monkeypatch.setattr(S, "_http_client", be)
    async def recover_twice():
        _reset_drain()
        S._episodic_spool_recovered = False
        n1 = await S.episodic_spool_recover()
        S._episodic_spool_recovered = False
        n2 = await S.episodic_spool_recover()
        return n1, n2
    (n1, n2), log = _run(recover_twice())
    assert n1 == 0 and n2 == 0
    assert log.count("CRITICAL episodic_spool_corrupt") == 2  # once per bad file, never again
    assert os.path.exists(bad + ".corrupt") and os.path.exists(incomplete + ".corrupt")
    assert not os.path.exists(bad) and not os.path.exists(incomplete)
    assert S._episodic_reappend_stats["spool_quarantined"] == 2


def test_unwritable_spool_is_one_critical_and_memory_only(monkeypatch):
    S._EPISODIC_SPOOL_DIR = "/proc/definitely-not-writable/spool"
    be = _Backend({"/episodic/append": [_timeout(), _timeout(), {"status": "stored", "id": 4}],
                   "/episodic/probe": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    assert log.count("CRITICAL episodic_spool_unwritable") == 1
    assert "IN MEMORY ONLY" in log and "episodic_capture_confirmed_by_drain" in log
    # a second turn does not repeat the CRITICAL (WARN only)
    be2 = _Backend({"/episodic/append": [_timeout(), _timeout(), {"status": "stored", "id": 5}],
                    "/episodic/probe": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be2)
    _, log2 = _run(_capture_then_drain(_TEXT, _UID))
    assert "CRITICAL episodic_spool_unwritable" not in log2 and "WARN episodic_spool_write_failed" in log2


def test_spool_written_on_queue_removed_on_confirm_and_recovered_at_start(monkeypatch):
    be = _Backend({"/episodic/append": [_timeout(), _timeout(), {"status": "stored", "id": 5}],
                   "/episodic/probe": [{"retained": False, "id": None, "status": "ok"}]})
    monkeypatch.setattr(S, "_http_client", be)
    seen = {}
    real_once = S._episodic_reappend_once
    async def spy(body):
        seen["file_during_flight"] = os.path.exists(S._spool_path(body["turn_key"]))
        return await real_once(body)
    monkeypatch.setattr(S, "_episodic_reappend_once", spy)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    key = be.bodies["/episodic/append"][0]["turn_key"]
    assert seen["file_during_flight"] is True, "the spool file exists while the turn is unconfirmed"
    assert not os.path.exists(S._spool_path(key)), "removed on confirmation"
    # recovery: a spool file left by a previous process is re-queued and confirmed
    S._spool_write({"user_id": _UID, "raw_text": "left over", "source": "mcp", "turn_key": "deadbeef" * 4})
    S._episodic_spool_recovered = False
    be2 = _Backend({"/episodic/append": [{"status": "stored", "id": 9, "deduplicated": True}]})
    monkeypatch.setattr(S, "_http_client", be2)
    async def recover():
        _reset_drain()
        n = await S.episodic_spool_recover()
        await asyncio.wait_for(S._episodic_reappend_queue.join(), timeout=_JOIN_S)
        return n
    n, log2 = _run(recover())
    assert n == 1 and "episodic_spool_recovered: 1" in log2 and "episodic_capture_confirmed_by_drain" in log2
    assert "episodic_spool_recover: re-queued turn_key=deadbeefdeadbeef" in log2  # per-turn outcome trail
    assert be2.bodies["/episodic/append"][0]["turn_key"] == "deadbeef" * 4
    assert not os.path.exists(S._spool_path("deadbeef" * 4))


def test_degraded_wording_says_pending_until_an_answered_write(monkeypatch):
    """The tool may say "kept verbatim" ONLY when the capture was answered; a queued retry is
    PENDING and the envelope carries `retention`."""
    S._EPISODIC_UNCONFIRMED_CRIT_S = 3600.0
    # route timeout + episodic pending (both appends time out, probe absent, drain dead)
    be = _Backend({**_SPINE_OK, "/episodic/append": [_timeout("dead")],
                   "/episodic/probe": [{"retained": False, "id": None, "status": "ok"}],
                   "/internal/ingest-route": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S, "_queue_episodic_reappend", _AsyncTrue())
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    assert out["retention"] == "pending"
    assert "PENDING" in out["message"] and "kept verbatim" not in out["message"]
    # classify unavailable + episodic pending
    be2 = _Backend({"/classify-intent": [_timeout()], "/episodic/append": [_timeout("dead")],
                    "/episodic/probe": [{"retained": False, "id": None, "status": "ok"}]})
    monkeypatch.setattr(S, "_http_client", be2)
    out2, _ = _run(remember_facts_tool(_TEXT, _UID))
    assert out2["retention"] == "pending" and "PENDING" in out2["message"]
    assert "kept verbatim" not in out2["message"]
    # confirmed capture → the wording may say kept verbatim
    be3 = _Backend({**_SPINE_OK, "/internal/ingest-route": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be3)
    out3, _ = _run(remember_facts_tool(_TEXT, _UID))
    assert out3["retention"] == "confirmed" and "kept verbatim" in out3["message"]


class _AsyncTrue:
    async def __call__(self, *a, **k):
        return True


# ── round-3 nits: a malformed brain answer and a non-retry-set status are LOUD, never guessed ──

def test_classify_200_without_intent_is_malformed_not_statement(monkeypatch):
    be = _Backend({"/classify-intent": [{"confidence": 0.9}]})  # no intent key
    monkeypatch.setattr(S, "_http_client", be)
    with pytest.raises(BrainUnavailable) as ei:
        _run(S._classify_and_gate(_TEXT, _UID))
    assert "malformed" in ei.value.cause and be.calls.count("/classify-intent") == 1


def test_route_non_retry_status_is_loud_not_a_bare_500(monkeypatch):
    be = _Backend({"/internal/ingest-route": [({"detail": "nope"}, 404)]})
    monkeypatch.setattr(S, "_http_client", be)
    with pytest.raises(BrainUnavailable) as ei:
        _run(S._statement_extractor_route(_UID))
    assert ei.value.cause == "HTTP 404"
    # and through the tool: queued + degraded, not an exception out of remember_facts
    be2 = _Backend({**_SPINE_OK, "/internal/ingest-route": [({"detail": "nope"}, 404)]})
    monkeypatch.setattr(S, "_http_client", be2)
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    assert out["status"] == "degraded" and out["queued"] is True and out["cause"] == "HTTP 404"


# ── retract: no destructive default ─────────────────────────────────────────────────────

def test_retract_fact_never_defaults_to_retraction_on_timeout(monkeypatch):
    be = _Backend({"/classify-intent": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be)
    out, log = _run(retract_fact_tool("forget that", _UID))
    assert out["status"] == "degraded" and out["isError"] is True
    assert out["brain_unavailable"] == "/classify-intent"
    assert "/retract/correct" not in be.calls, "a destructive call was made on a transport guess"
    assert "defaulting to RETRACTION" not in log and "_fallback" not in log


# ── GAP A: a transport failure INSIDE the chosen extractor is not a route decision ────────

def test_spine_harvest_timeout_never_becomes_a_rewrite_run(monkeypatch):
    """The critic's reproduction: brain says spine, /harvest-spans raises ReadTimeout. The old
    code returned None → /extract/rewrite → /ingest → {"status":"valid","committed":1} with no
    cause. Now: one retry, then queued + loud; /extract/rewrite and /ingest are NEVER called
    and the status is never `valid`."""
    be = _Backend({**_SPINE_OK, "/harvest-spans": [_timeout()],
                   "/extract/rewrite": [{"edges": [{"subject": "dana", "rel_type": "lives_in",
                                                    "object": "halifax"}]}],
                   "/ingest": [{"status": "valid", "committed": 1, "staged": 0, "facts": []}]})
    monkeypatch.setattr(S, "_http_client", be)
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    assert out["status"] == "degraded" and out["isError"] is True, out
    assert out["status"] != "valid" and out["committed"] == 0
    assert out["brain_unavailable"] == "/harvest-spans" and "ReadTimeout" in out["cause"]
    assert out["deferred"] is True and out["queued"] is True and out["retry_pending"] is True
    assert be.calls.count("/harvest-spans") == 2, be.calls
    assert "/extract/rewrite" not in be.calls and "/ingest" not in be.calls, be.calls
    assert "statement_spine_harvest_failed" not in log and "falling back" not in log
    assert "brain_unavailable" in log
    q_text, q_uid, q_src, q_lane, _q_ts = S._statement_defer_queue.get_nowait()
    assert (q_text, q_uid, q_src) == (_TEXT, _UID, "mcp")
    assert q_lane == "STATEMENT"


def test_spine_harvest_retry_lands_and_the_turn_is_a_normal_spine_turn(monkeypatch):
    be = _Backend({**_SPINE_OK,
                   "/harvest-spans": [_timeout(), {"edges": [{"subject": "dana", "rel_type": "lives_in",
                                                              "object": "halifax"}]}],
                   "/ingest": [{"status": "valid", "committed": 1, "staged": 0, "facts": []}]})
    monkeypatch.setattr(S, "_http_client", be)
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    assert out.get("committed") == 1 and out.get("status") == "valid", out
    assert be.calls.count("/harvest-spans") == 2 and "/extract/rewrite" not in be.calls
    assert "CRITICAL" not in log and "_fallback" not in log


def test_spine_zero_edges_is_the_brains_decision_and_rewrite_fallback_stays(monkeypatch):
    """The documented fail-safe survives: the brain ANSWERED with zero edges → /extract/rewrite
    is consulted. That is a decision, not a transport substitution."""
    be = _Backend({**_SPINE_OK, "/harvest-spans": [{"edges": []}],
                   "/extract/rewrite": [{"edges": [{"subject": "dana", "rel_type": "lives_in",
                                                    "object": "halifax"}]}],
                   "/ingest": [{"status": "valid", "committed": 1, "staged": 0, "facts": []}]})
    monkeypatch.setattr(S, "_http_client", be)
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    assert out.get("committed") == 1
    assert be.calls.count("/harvest-spans") == 1 and "/extract/rewrite" in be.calls
    assert "brain answered with 0 edge(s)" in log


def test_rewrite_lane_transport_failure_queues_instead_of_substituting_the_harvest(monkeypatch):
    """Brain says rewrite; /extract/rewrite times out twice → queued + loud. The old arm ran the
    deterministic harvest (a different, weaker extractor) and reported 'stored'."""
    be = _Backend({**_SPINE_OK, "/internal/ingest-route": [{"statement_extractor": "rewrite"}],
                   "/extract/rewrite": [_timeout()],
                   "/harvest-spans": [{"edges": [{"subject": "dana", "rel_type": "lives_in",
                                                  "object": "halifax"}]}]})
    monkeypatch.setattr(S, "_http_client", be)
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    assert out["status"] == "degraded" and out["brain_unavailable"] == "/extract/rewrite"
    assert out["queued"] is True and out["status"] != "stored"
    assert be.calls.count("/extract/rewrite") == 2
    assert "/harvest-spans" not in be.calls and "/ingest" not in be.calls


def test_document_lane_spine_timeout_fails_and_queues_the_chunk(monkeypatch):
    """Sync document lane, brain says spine, the chunk's /harvest-spans times out → the chunk is
    a FAILED + queued chunk; the per-chunk /extract/rewrite fail-safe is NOT entered."""
    from src.mcp.server import ingest_document_tool
    be = _Backend({"/internal/ingest-route": [{"statement_extractor": "spine"}],
                   "/episodic/append": [{"status": "stored", "id": 1}],
                   "/harvest-spans": [_timeout()],
                   "/extract/rewrite": [{"edges": []}],
                   "/documents": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S, "_check_injection_signals", lambda *_a, **_k: None)
    out, log = _run(ingest_document_tool("Rufus is a dog. Rufus lives in Elmstead.", _UID))
    assert out["chunks"] >= 1 and out["chunks_failed"] == out["chunks"], out
    assert out["chunks_deferred_retry"] == out["chunks"], out
    assert "/extract/rewrite" not in be.calls, be.calls
    assert "brain_unavailable" in log and "no route substituted" in log


def test_deferred_worker_treats_spine_transport_failure_as_a_blip(monkeypatch):
    """The deferred drain's own path: a transport failure inside the spine RAISES, so the worker's
    retry arm fires (it used to read None as 'no edges' and drop the turn)."""
    be = _Backend({"/internal/ingest-route": [{"statement_extractor": "spine"}],
                   "/harvest-spans": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be)
    with pytest.raises(BrainUnavailable):
        _run(S._ingest_statement_via_spine(_TEXT, _UID, deferred=True))
    be2 = _Backend({"/extract/rewrite": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be2)
    with pytest.raises(BrainUnavailable):
        _run(S._statement_rewrite_ingest_deferred(_TEXT, _UID, deferred=True))


def test_extractor_timeouts_come_from_the_registry_not_literals(monkeypatch):
    assert S._brain_timeout("HARVEST_SPANS") == 60.0 and S._brain_timeout("EXTRACT_REWRITE") == 60.0
    monkeypatch.setenv("MCP_BRAIN_TIMEOUT_HARVEST_SPANS", "12")
    assert S._brain_timeout("harvest_spans") == 12.0


# ── AST census: NO except-handler on a route-bearing path may decide a route ─────────────

_ROUTE_FUNCS = {
    "_brain_request", "_classify_and_gate", "_statement_extractor_route",
    "_ingest_statement_via_spine", "_statement_rewrite_ingest_deferred", "_statement_defer_worker",
    "_remember_facts_tool_impl", "retract_fact_tool", "recall_memory_tool",
    "ingest_document_tool", "_process_chunk", "_brain_unavailable_after_intent",
}
_ROUTE_WORDS = {"spine", "rewrite", "STATEMENT", "QUERY", "RETRACTION", "CORRECTION"}
_EXTRACTOR_CALLS = {"_harvest_turn_facts", "_ground_self_predication_facts",
                    "_statement_rewrite_ingest_deferred", "_ingest_statement_via_spine"}
# The one documented exception: recall is a READ; on a dead classifier it walks (no write, no
# guess). Named here on purpose — a new entry needs the same justification in code.
_ALLOWED = {("recall_memory_tool", "QUERY")}


def _func_of(tree):
    """Map every node to the innermost enclosing function name."""
    owner = {}
    def visit(node, cur):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cur = node.name
        owner[node] = cur
        for child in ast.iter_child_nodes(node):
            visit(child, cur)
    visit(tree, None)
    return owner


def test_no_except_handler_on_a_route_path_decides_a_route_or_extractor():
    """Regardless of log wording: inside the route-bearing functions, an except-handler must
    not `return None` (the None→rewrite wiring), assign/return a route or intent literal, or
    call another extractor. Transport failures RAISE; only the brain's answers pick lanes."""
    tree = ast.parse(open(S.__file__).read())
    owner = _func_of(tree)
    offences = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or owner.get(node) not in _ROUTE_FUNCS:
            continue
        fn = owner[node]
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and (sub.value is None or (
                    isinstance(sub.value, ast.Constant) and sub.value.value is None)):
                offences.append((fn, sub.lineno, "return None in except"))
            if isinstance(sub, (ast.Return, ast.Assign)):
                val = sub.value
                if isinstance(val, ast.Constant) and val.value in _ROUTE_WORDS:
                    if (fn, val.value) not in _ALLOWED:
                        offences.append((fn, sub.lineno, f"route literal {val.value!r} in except"))
            if isinstance(sub, ast.Call):
                name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                if name in _EXTRACTOR_CALLS:
                    offences.append((fn, sub.lineno, f"extractor call {name} in except"))
    assert not offences, offences


# ── the transport-wide grep: no route-substituting `_fallback` log lines remain ─────────

def test_no_route_substituting_fallback_log_lines_in_server_source():
    """The wound's three log lines must not exist as emitted strings any more. (Comments are
    excluded; `recall_ingest_fallback_error` is recall's NON-EATING ingest-attempt error and
    substitutes no route — it is named in the allowlist on purpose, not swept under it.)"""
    src = open(S.__file__).read().splitlines()
    emitted = [l for l in src if "_log(" in l and "_fallback" in l and not l.strip().startswith("#")]
    assert all("recall_ingest_fallback_error" in l for l in emitted), emitted
    for gone in ("intent_classify_fallback", "statement_extractor_route_fallback",
                 "confidence_gate_fallback", "defaulting to STATEMENT", "defaulting to rewrite",
                 "defaulting to RETRACTION"):
        assert not any(gone in l for l in emitted), gone


# ── ROUND 7: the provisioning gate never fabricates "provisioning" for an unreachable backend ──

def test_provisioning_gate_unreachable_is_loud_proceeds_and_caches_nothing(monkeypatch):
    be = _Backend({"/provisioning/status": [_timeout("api dead")]})
    monkeypatch.setattr(S, "_http_client", be)
    S._provisioned_users.discard(_UID)
    out, log = _run(S._ensure_provisioned(_UID))
    assert out == "unreachable" and bool(out) is True  # truthy → the door PROCEEDS to the tool
    assert "CRITICAL provisioning_probe_unavailable" in log and "NOT reporting 'provisioning'" in log
    assert _UID not in S._provisioned_users  # a transport failure never populates the cache
    assert be.calls.count("/provisioning/status") == 2  # attempt + the one in-turn retry, then loud


def test_provisioning_gate_backend_says_not_ready_keeps_the_honest_sentence(monkeypatch):
    monkeypatch.setenv("MCP_PROVISIONING_WAIT_SEC", "0.3")
    monkeypatch.setenv("MCP_PROVISIONING_POLL_SEC", "0.05")
    be = _Backend({"/provisioning/status": [{"status": "provisioning"}]})
    monkeypatch.setattr(S, "_http_client", be)
    S._provisioned_users.discard(_UID)
    out, log = _run(S._ensure_provisioned(_UID))
    assert out == "" and bool(out) is False  # falsy → the provisioning sentence, which is TRUE here
    assert "CRITICAL" not in log and _UID not in S._provisioned_users
    assert be.calls.count("/provisioning/status") >= 3


def test_provisioning_gate_ready_is_cached_and_a_later_outage_hits_the_tool_route(monkeypatch):
    be = _Backend({"/provisioning/status": [{"status": "ready"}]})
    monkeypatch.setattr(S, "_http_client", be)
    S._provisioned_users.discard(_UID)
    out, _ = _run(S._ensure_provisioned(_UID))
    assert out == "ready" and _UID in S._provisioned_users
    # mid-wait outage: backend said provisioning, then vanished → unreachable, not "not ready"
    S._provisioned_users.discard(_UID)
    be2 = _Backend({"/provisioning/status": [{"status": "provisioning"}, _timeout("gone")]})
    monkeypatch.setenv("MCP_PROVISIONING_POLL_SEC", "0.01")
    monkeypatch.setattr(S, "_http_client", be2)
    out2, log2 = _run(S._ensure_provisioned(_UID))
    assert out2 == "unreachable" and "poll 1" in log2


def test_door_takes_the_degraded_pending_route_when_the_gate_is_unreachable(monkeypatch):
    """The MCP JSON-RPC door (`_call_tool`) with the API dead BEFORE the first request of a fresh
    process: NOT the provisioning sentence — the tool runs, classify is loud, the turn is
    retained-or-pending with a `retention` verdict and a `cause`."""
    S._provisioned_users.discard(_UID)
    be = _Backend({"/provisioning/status": [_timeout("api dead")],
                   "/classify-intent": [_timeout("api dead")],
                   "/episodic/append": [_timeout("api dead")],
                   "/episodic/probe": [_timeout("api dead")]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S, "_queue_episodic_reappend", _AsyncTrue())
    monkeypatch.setattr(S, "_send_progress", lambda *a, **k: None)
    res, log = _run(S._call_tool("remember_facts", {"text": _TEXT, "user_id": _UID}))
    body = json.loads(res["content"][0]["text"])
    assert body.get("status") != "provisioning", body
    assert "Memory is being set up" not in json.dumps(body)
    assert body["status"] == "degraded" and body["retention"] == "pending"
    assert body["brain_unavailable"] == "/classify-intent" and body["cause"]
    assert "CRITICAL provisioning_probe_unavailable" in log and "CRITICAL brain_unavailable" in log
    assert "WARN episodic_capture_unverified" in log


# ── ROUND 8: the gate classifies the backend's ANSWER, not only the transport ─────────────

def test_probe_body_classification_table():
    c = S._classify_probe_body
    assert c({"status": "ready"})[0] == "ready"
    assert c({"status": "not_found"})[0] == "not_found"
    assert c({"status": "provisioning"})[0] == "wait" and c({"status": "pending"})[0] == "wait"
    # the helper's EXCEPT-dict (probe failed, no seat row) → unreachable, with the backend's ids
    kind, detail, corr = c({"status": "error", "error_message": "Failed to check status (ref x1)",
                            "correlation_id": "x1"})
    assert kind == "unreachable" and "Failed to check status" in detail and corr == "x1"
    # a provisioning ROW in error (has schema_name) → terminal
    assert c({"status": "error", "schema_name": "faultline_x", "error_message": "template failed"})[0] == "failed"
    assert c({"status": "failed", "schema_name": "faultline_x"})[0] == "failed"
    assert c({"status": "weird"})[0] == "failed"
    assert c({})[0] == "unreachable" and c("nonsense")[0] == "unreachable"


def test_gate_200_error_without_row_is_unreachable_loud_and_degraded(monkeypatch):
    """API up, its DB refusing: the backend answers 200 {status:error, error_message, correlation_id}.
    Round 7 polled that for 90s into the provisioning sentence. Now: unreachable in ONE probe,
    CRITICAL carrying the backend's correlation id, and the door takes the degraded/pending route."""
    S._provisioned_users.discard(_UID)
    err = {"status": "error", "user_id": _UID, "error_message": "Failed to check status (ref c0ffee)",
           "correlation_id": "c0ffee"}
    be = _Backend({"/provisioning/status": [err], "/classify-intent": [_timeout("db down")],
                   "/episodic/append": [({"status": "unavailable", "retryable": True}, 503)],
                   "/episodic/probe": [({"status": "unavailable", "retryable": True}, 503)]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S, "_queue_episodic_reappend", _AsyncTrue())
    monkeypatch.setattr(S, "_send_progress", lambda *a, **k: None)
    import time
    t0 = time.monotonic()
    res, log = _run(S._call_tool("remember_facts", {"text": _TEXT, "user_id": _UID}))
    assert time.monotonic() - t0 < 5.0
    body = json.loads(res["content"][0]["text"])
    assert body["status"] == "degraded" and body["retention"] == "pending", body
    assert "Memory is being set up" not in json.dumps(body)
    assert be.calls.count("/provisioning/status") == 1  # classified on the FIRST answer, no 90s poll
    assert "CRITICAL provisioning_probe_unavailable" in log and "correlation_id=c0ffee" in log
    assert _UID not in S._provisioned_users


def test_gate_failed_row_is_a_terminal_envelope_not_a_wait(monkeypatch):
    S._provisioned_users.discard(_UID)
    row = {"status": "failed", "user_id": _UID, "schema_name": "faultline_x",
           "error_message": "template apply failed: relation X", "correlation_id": "dead01"}
    be = _Backend({"/provisioning/status": [row]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S, "_send_progress", lambda *a, **k: None)
    import time
    t0 = time.monotonic()
    res, log = _run(S._call_tool("remember_facts", {"text": _TEXT, "user_id": _UID}))
    assert time.monotonic() - t0 < 2.0
    body = json.loads(res["content"][0]["text"])
    assert body["status"] == "provisioning_failed" and body["isError"] is True and body["terminal"] is True
    assert body["correlation_id"] == "dead01" and "template apply failed" in body["error_message"]
    assert "Memory is being set up" not in json.dumps(body) and "re-provision" in body["message"]
    assert be.calls.count("/provisioning/status") == 1
    assert "CRITICAL provisioning_failed_terminal" in log and "/classify-intent" not in be.calls
    # the gate value itself is falsy AND carries the failure
    S._provisioned_users.discard(_UID)
    monkeypatch.setattr(S, "_http_client", _Backend({"/provisioning/status": [row]}))
    g, _ = _run(S._ensure_provisioned(_UID))
    assert not g and isinstance(g, S._GateFailed) and g.correlation_id == "dead01"
    # an `error` ROW (with schema_name) is the same terminal class
    S._provisioned_users.discard(_UID)
    monkeypatch.setattr(S, "_http_client", _Backend({"/provisioning/status": [
        {"status": "error", "schema_name": "faultline_x", "error_message": "mint failed"}]}))
    g2, _ = _run(S._ensure_provisioned(_UID))
    assert isinstance(g2, S._GateFailed) and g2.provisioning_status == "error"


def test_gate_provisioning_status_keeps_the_honest_wait(monkeypatch):
    monkeypatch.setenv("MCP_PROVISIONING_WAIT_SEC", "0.2"); monkeypatch.setenv("MCP_PROVISIONING_POLL_SEC", "0.05")
    S._provisioned_users.discard(_UID)
    be = _Backend({"/provisioning/status": [{"status": "provisioning", "user_id": _UID}]})
    monkeypatch.setattr(S, "_http_client", be)
    g, log = _run(S._ensure_provisioned(_UID))
    assert g == "" and not isinstance(g, S._GateFailed) and "CRITICAL" not in log
    env = S.provisioning_gate_envelope(g, facts=[])
    assert env["status"] == "provisioning" and env["facts"] == [] and "being set up" in env["message"]


def test_rest_and_rpc_recall_return_a_loud_envelope_when_the_walk_cannot_reach_the_brain(monkeypatch):
    """Round 8 (b): `_walk` posts /query through the brain seam — a dead API is a degraded
    envelope with the cause from BOTH doors, never a bare 500."""
    S._provisioned_users.add(_UID)
    be = _Backend({"/classify-intent": [_timeout()], "/harvest-spans": [_timeout()],
                   "/query": [_timeout("api dead")], "/cortex/recall": [_timeout()],
                   "/documents": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S, "_send_progress", lambda *a, **k: None)
    out, log = _run(S.recall_memory_tool("what do you know", _UID))
    assert out["isError"] is True and out["status"] == "degraded" and out["brain_unavailable"] == "/query"
    assert "retrieval failure" in out["memory"] and "brain_unavailable" in log
    assert be.calls.count("/query") == 2
    # and the REST door: the real ASGI app, no handler needed — the tool returns the envelope.
    # (SaaS off for this probe: the SaaS offline gate is a DIFFERENT honest 503 that fires when
    # its platform-settings read has no DB — not the 500 this pin is about.)
    monkeypatch.setenv("SAAS_MODE", "")
    import httpx as _hx
    import src.mcp.http_server as H
    monkeypatch.setattr(H, "MCP_API_KEY", "")  # open door for the probe (auth is not under test)
    async def rest():
        async with _hx.AsyncClient(transport=_hx.ASGITransport(app=H.app), base_url="http://t") as c:
            return await c.post("/recall_memory", json={"query": "what do you know", "user_id": _UID},
                                headers={"X-OpenWebUI-User-Id": _UID})
    r, _ = _run(rest())
    assert r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"), (r.status_code, r.text[:120])
    assert r.json().get("isError") is True and r.json().get("brain_unavailable") == "/query"


def test_drain_kicks_provisioning_for_an_unknown_seat_then_confirms(monkeypatch):
    """Round 8 residual closed: a seat first seen while the backend was down has no schema; the
    drain's re-append is answered soft_error reason=seat_unknown → the drain provisions the seat
    (bounded) and the next re-append lands. Never `missed` for a seat that can be provisioned."""
    S._provisioned_users.discard(_UID)
    be = _Backend({"/episodic/append": [_timeout(), _timeout(),
                                        {"status": "soft_error", "id": None, "reason": "seat_unknown"},
                                        {"status": "stored", "id": 2}],
                   "/episodic/probe": [_timeout()],
                   "/provisioning/status": [{"status": "not_found"}, {"status": "ready"}]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S.asyncio, "sleep", _fast_sleep)  # skip the 6s enqueue wait
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    assert "episodic_reappend_provisioning_kick" in log and "gate='ready'" in log
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=2" in log
    assert "episodic_capture_missed" not in log
    # a seat that CANNOT be provisioned (terminal row) is refused after the kick
    S._provisioned_users.discard(_UID)
    be2 = _Backend({"/episodic/append": [_timeout(), _timeout(),
                                         {"status": "soft_error", "id": None, "reason": "seat_unknown"}],
                    "/episodic/probe": [_timeout()],
                    "/provisioning/status": [{"status": "failed", "schema_name": "x", "error_message": "boom"}]})
    monkeypatch.setattr(S, "_http_client", be2)
    _, log2 = _run(_capture_then_drain(_TEXT, _UID))
    assert "CRITICAL episodic_capture_missed" in log2 and "provisioning terminal" in log2


async def _fast_sleep(_s):
    return None


# ── ROUND 9: seat_unknown is never a positive refusal in-turn; retention on EVERY envelope ─────

def test_append_answer_classification_is_one_table():
    c = S._classify_append_answer
    assert c(200, {"status": "stored", "id": 1})[0] == "confirmed"
    assert c(200, {"status": "stored", "id": 1, "deduplicated": True})[0] == "confirmed"
    assert c(200, {"status": "ingest_disabled"})[0] == "skipped"
    assert c(503, {"status": "unavailable", "retryable": True})[0] == "transport"
    assert c(200, {"status": "unavailable"})[0] == "transport" and c(502, {})[0] == "transport"
    assert c(200, {"status": "soft_error", "reason": "seat_unknown"})[0] == "seat_unknown"
    assert c(200, {"status": "soft_error", "reason": "InvalidSchemaName"})[0] == "seat_unknown"
    assert c(200, {"status": "soft_error", "reason": "ProgrammingError"})[0] == "refused"
    assert c(422, {"status": "soft_error", "reason": "malformed turn_key"})[0] == "refused"
    assert c(200, {"status": "soft_error"})[0] == "refused"


def test_d4_seat_unknown_in_turn_is_pending_queued_and_cache_invalidated(monkeypatch):
    """D4 (the SaaS purge/re-add shape): the seat is cached `ready` on a warm MCP, its schema was
    purged underneath, a re-provision is in flight. The in-turn append answers 200 soft_error
    reason=seat_unknown. Old: CRITICAL missed, nothing queued, stale cache kept, door said
    `valid` with no retention. Now: verdict `pending`, cache invalidated, the SAME turn_key on
    the drain, the drain's provisioning kick lands, confirmed — and the door's final envelope
    carries `retention`."""
    S._provisioned_users.add(_UID)
    be = _Backend({**_SPINE_OK,
                   # in-turn: seat_unknown; the drain's first re-append: still unknown (the
                   # re-mint is in flight) → kick → ready → the next re-append lands.
                   "/episodic/append": [{"status": "soft_error", "id": None, "reason": "seat_unknown"},
                                        {"status": "soft_error", "id": None, "reason": "seat_unknown"},
                                        {"status": "stored", "id": 3}],
                   "/provisioning/status": [{"status": "provisioning"}, {"status": "ready"}],
                   "/harvest-spans": [{"edges": [{"subject": "dana", "rel_type": "lives_in", "object": "halifax"}]}],
                   "/ingest": [{"status": "valid", "committed": 1, "staged": 0, "facts": []}]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S.asyncio, "sleep", _fast_sleep)
    cache_after_capture = {}
    real_cap = S._episodic_capture_impl
    async def spy(text, uid, intent, *, source="mcp"):
        v = await real_cap(text, uid, intent, source=source)
        cache_after_capture["in_cache"] = uid in S._provisioned_users
        return v
    monkeypatch.setattr(S, "_episodic_capture_impl", spy)
    async def drive():
        _reset_drain()
        out = await remember_facts_tool(_TEXT, _UID)
        await asyncio.wait_for(S._episodic_reappend_queue.join(), timeout=_JOIN_S)
        return out
    out, log = _run(drive())
    assert "CRITICAL episodic_capture_missed" not in log
    assert "WARN episodic_capture_seat_unknown" in log and "cache INVALIDATED" in log
    assert cache_after_capture["in_cache"] is False
    assert out["retention"] == "pending" and out.get("status") == "valid", out
    assert "episodic_reappend_provisioning_kick" in log and "gate='ready'" in log
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=3" in log
    keys = {b["turn_key"] for b in be.bodies["/episodic/append"]}
    assert len(keys) == 1 and len(be.bodies["/episodic/append"]) == 3  # same key in-turn and on the drain


def test_d4b_seat_unknown_without_a_concurrent_remint_recovers_via_the_drain(monkeypatch):
    """No re-provision in flight: the drain's kick enqueues it (not_found → ready) — the seat
    recovers on this process, no MCP restart."""
    S._provisioned_users.add(_UID)
    be = _Backend({"/episodic/append": [{"status": "soft_error", "reason": "seat_unknown"},
                                        {"status": "soft_error", "reason": "seat_unknown"},
                                        {"status": "stored", "id": 4}],
                   "/provisioning/status": [{"status": "not_found"}, {"status": "ready"}]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S.asyncio, "sleep", _fast_sleep)
    v, log = _run(_capture_then_drain(_TEXT, _UID))
    assert v == "pending"
    assert "Provisioning enqueued" in log and "gate='ready'" in log  # the DRAIN enqueued the re-mint
    assert _UID in S._provisioned_users  # re-cached by the kick's POSITIVE ready answer only
    assert "episodic_capture_confirmed_by_drain" in log and "CRITICAL" not in log


def test_payload_refusal_is_still_terminal_in_turn(monkeypatch):
    be = _Backend({"/episodic/append": [({"status": "soft_error", "reason": "malformed turn_key"}, 422)]})
    monkeypatch.setattr(S, "_http_client", be)
    v, log = _run(S._episodic_capture(_TEXT, _UID, "STATEMENT"))
    assert v == "missed" and "CRITICAL episodic_capture_missed" in log
    assert S._episodic_reappend_stats["queued"] == 0


@pytest.mark.parametrize("shape,script,expect_status", [
    ("valid_spine", {**_SPINE_OK,
                     "/harvest-spans": [{"edges": [{"subject": "d", "rel_type": "lives_in", "object": "h"}]}],
                     "/ingest": [{"status": "valid", "committed": 1, "staged": 0, "facts": []}]}, "valid"),
    ("valid_rewrite", {**_SPINE_OK, "/internal/ingest-route": [{"statement_extractor": "rewrite"}],
                       "/extract/rewrite": [{"edges": [{"subject": "d", "rel_type": "lives_in", "object": "h"}]}],
                       "/ingest": [{"status": "valid", "committed": 1, "staged": 0, "facts": []}]}, "valid"),
    ("degraded_route", {**_SPINE_OK, "/internal/ingest-route": [_timeout()]}, "degraded"),
    ("degraded_classify", {"/classify-intent": [_timeout()], "/episodic/append": [{"status": "stored", "id": 1}]}, "degraded"),
    ("query_detected", {**_SPINE_OK, "/classify-intent": [{"intent": "QUERY", "confidence": 1.0}],
                        "/harvest-spans": [{"edges": []}]}, "query_detected"),
    ("no_ingest_short", {**_SPINE_OK}, "no_ingest"),
    ("spine_zero_edges_rewrite_none", {**_SPINE_OK, "/harvest-spans": [{"edges": []}],
                                       "/extract/rewrite": [{"edges": []}]}, "no_ingest"),
])
def test_every_remember_facts_envelope_carries_retention(monkeypatch, shape, script, expect_status):
    """Retention census over the write door's outcome shapes (round 9): `status: valid` without
    `retention` was a loss shape. Stamped at the ONE wrapper seam, so every dict carries it."""
    be = _Backend(script)
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S, "_queue_episodic_reappend", _AsyncTrue())
    monkeypatch.setattr(S, "_harvest_turn_facts", _AsyncZero())
    monkeypatch.setattr(S, "_ground_self_predication_facts", _AsyncZero())
    text = "ok thanks" if shape == "no_ingest_short" else _TEXT
    out, _ = _run(remember_facts_tool(text, _UID))
    assert out.get("status") == expect_status, (shape, out)
    assert "retention" in out, (shape, out)
    assert out["retention"] in ("confirmed", "pending", "unverified", "missed", "skipped", "not_attempted")
    if expect_status == "valid":
        assert out["retention"] == "confirmed"


def test_rejected_turn_carries_retention_not_attempted(monkeypatch):
    monkeypatch.setattr(S, "_check_injection_signals", lambda *_a, **_k: "stubbed injection")
    out, _ = _run(remember_facts_tool(_TEXT, _UID))
    assert out["status"] == "rejected" and out["retention"] == "not_attempted"


def test_v1_terminal_provisioning_failure_carries_ids_as_fields(monkeypatch):
    import src.mcp.api_v1 as V
    g = S._GateFailed("failed", "template apply failed", "dead02")
    captured = {}
    def fake_api_error(code, message, status, detail=None):
        captured.update(code=code, message=message, status=status, detail=detail)
        return {"stub": True}
    monkeypatch.setattr(V, "_api_error", fake_api_error)
    env = S.provisioning_gate_envelope(g)
    # mirror the /v1 recall door's branch on the shared envelope
    if env.get("terminal"):
        V._api_error(env["status"], env["message"], 409, detail={
            "terminal": True, "provisioning_status": env.get("provisioning_status"),
            "correlation_id": env.get("correlation_id"), "error_message": env.get("error_message")})
    assert captured["status"] == 409 and captured["code"] == "provisioning_failed"
    assert captured["detail"]["correlation_id"] == "dead02" and captured["detail"]["terminal"] is True


class _AsyncZero:
    async def __call__(self, *a, **k):
        return 0


# ── ROUND 10: seat_unknown while the seat is STILL BEING MINTED is never terminal ──────────────

def _prov(n):
    return [{"status": "provisioning"}] * n


def _zero_wait_budget(monkeypatch):
    """The gate's wait budget → 0 so an honest `provisioning` answer exhausts after exactly ONE
    poll (initial probe + 1 poll = 2 status calls per kick). `_env_float` treats "0" in the env
    as invalid → default 90s, so the accessor is pinned rather than the env."""
    real = S._env_float
    monkeypatch.setattr(S, "_env_float",
                        lambda name, default: 0.0 if name == "MCP_PROVISIONING_WAIT_SEC" else real(name, default))


def test_d4e_slow_remint_seat_unknown_is_never_terminal_and_confirms_when_ready(monkeypatch):
    """D4e (driven on the real stack by the critic): the seat was purged under a warm cache and
    the re-mint is SLOW — the gate honestly answers `provisioning` past its wait budget, again
    and again, while every re-append answers seat_unknown. Old arm: a LITERAL `prov_kicks <= 2`
    rewrote the THIRD seat_unknown to `refused` → CRITICAL missed + spool deleted BEFORE the
    seat turned ready, for a turn the door reported `retention: pending`. Now: seat_unknown ×5
    with the gate saying provisioning → the turn stays queued (spool intact throughout), the
    per-turn CRITICAL unconfirmed fires ONCE past the threshold, the seat turns ready → the
    same turn_key is confirmed 1|1. Never missed."""
    S._provisioned_users.add(_UID)
    S._EPISODIC_UNCONFIRMED_CRIT_S = 0.0     # every backoff is "past the threshold"
    S._EPISODIC_PROV_KICK_INTERVAL_S = 0.0   # a kick on every seat_unknown (interval pinned below)
    _zero_wait_budget(monkeypatch)  # honest wait exhausts after ONE poll (deterministic call counts)
    su = {"status": "soft_error", "id": None, "reason": "seat_unknown"}
    be = _Backend({"/episodic/append": [su, su, su, su, su, su, {"status": "stored", "id": 9}],
                   # each kick = initial probe + one poll → "" four times, then ready
                   "/provisioning/status": _prov(8) + [{"status": "ready"}]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S.asyncio, "sleep", _fast_sleep)
    spool_seen: list[bool] = []
    real_once = S._episodic_reappend_once
    async def once_spy(body):
        spool_seen.append(os.path.exists(S._spool_path(body["turn_key"])))
        return await real_once(body)
    monkeypatch.setattr(S, "_episodic_reappend_once", once_spy)
    removes: list[str] = []
    real_rm = S._spool_remove
    monkeypatch.setattr(S, "_spool_remove", lambda k: (removes.append(k), real_rm(k)))
    v, log = _run(_capture_then_drain(_TEXT, _UID))
    assert v == "pending"
    assert "episodic_capture_missed" not in log, log
    assert log.count("episodic_reappend_provisioning_kick") == 5
    assert log.count("gate=''") == 4 and "gate='ready'" in log
    assert log.count("CRITICAL episodic_capture_unconfirmed") == 1   # once per turn, not per attempt
    assert "episodic_capture_unconfirmed_cleared" in log
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=9" in log
    keys = {b["turn_key"] for b in be.bodies["/episodic/append"]}
    assert len(keys) == 1 and len(be.bodies["/episodic/append"]) == 7
    assert spool_seen and all(spool_seen), spool_seen        # the spool file was present on EVERY drain attempt
    assert removes == [next(iter(keys))]                     # removed exactly once — on confirmation
    assert not os.listdir(S._EPISODIC_SPOOL_DIR)
    assert S._episodic_reappend_stats["missed"] == 0 and S._episodic_reappend_stats["confirmed"] == 1


def test_kick_is_rate_limited_by_env_interval_not_a_literal_count(monkeypatch):
    """Between kicks a seat_unknown backs off like transport: with a long interval only ONE
    kick fires; the seat turning ready on its own (someone else's mint) confirms the turn."""
    S._provisioned_users.add(_UID)
    S._EPISODIC_PROV_KICK_INTERVAL_S = 3600.0
    _zero_wait_budget(monkeypatch)
    su = {"status": "soft_error", "reason": "seat_unknown"}
    be = _Backend({"/episodic/append": [su, su, su, su, {"status": "stored", "id": 10}],
                   "/provisioning/status": _prov(2)})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S.asyncio, "sleep", _fast_sleep)
    v, log = _run(_capture_then_drain(_TEXT, _UID))
    assert v == "pending" and "episodic_capture_missed" not in log
    assert log.count("episodic_reappend_provisioning_kick") == 1
    assert "next after 3600s" in log                      # the wait between kicks is the env value
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=10" in log
    assert be.calls.count("/provisioning/status") == 2   # one kick = initial probe + one poll
    # the knob is env-driven, not a literal
    src = open(S.__file__, encoding="utf-8").read()
    assert '_env_budget("MCP_EPISODIC_PROV_KICK_INTERVAL_S"' in src  # round 11: parsed with a floor
    assert "prov_kicks\"] <= 2" not in src and "after 2 provisioning kicks" not in src


def test_drain_terminal_only_on_a_failed_row_and_names_the_correlation_id(monkeypatch):
    S._provisioned_users.add(_UID)
    S._EPISODIC_PROV_KICK_INTERVAL_S = 0.0
    su = {"status": "soft_error", "reason": "seat_unknown"}
    be = _Backend({"/episodic/append": [su, su],
                   "/provisioning/status": [{"status": "failed", "schema_name": "x",
                                             "error_message": "template apply failed",
                                             "correlation_id": "corr-77"}]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S.asyncio, "sleep", _fast_sleep)
    v, log = _run(_capture_then_drain(_TEXT, _UID))
    assert v == "pending"
    assert "CRITICAL episodic_capture_missed" in log and "provisioning terminal" in log
    assert "correlation_id=corr-77" in log
    assert not os.listdir(S._EPISODIC_SPOOL_DIR)   # terminal → the spool file IS removed


@pytest.mark.parametrize("verdict", ["confirmed", "pending", "missed", "unverified"])
def test_rewrite_prose_is_gated_on_the_retention_verdict(monkeypatch, verdict):
    """Prose-gating census (round 10): "Captured the user's own words" is a RETENTION claim — only
    `confirmed` may say it; every other verdict gets that verdict's clause from the ONE table, and
    a not-kept verdict beside `status: valid` is stamped `verbatim_retained: false`."""
    be = _Backend({**_SPINE_OK, "/internal/ingest-route": [{"statement_extractor": "rewrite"}],
                   "/extract/rewrite": [{"edges": [{"subject": "d", "rel_type": "lives_in", "object": "h"}]}],
                   "/ingest": [{"status": "valid", "committed": 1, "staged": 0, "facts": []}]})
    monkeypatch.setattr(S, "_http_client", be)
    async def fake_capture(text, uid, intent, *, source="mcp"):
        return verdict
    monkeypatch.setattr(S, "_episodic_capture_impl", fake_capture)
    monkeypatch.setattr(S, "_harvest_turn_facts", _AsyncZero())
    monkeypatch.setattr(S, "_ground_self_predication_facts", _AsyncZero())
    out, _ = _run(remember_facts_tool(_TEXT, _UID))
    assert out["status"] == "valid" and out["retention"] == verdict, out
    msg = out.get("message") or ""
    if verdict == "confirmed":
        assert "Captured the user's own words" in msg and "verbatim_retained" not in out
    else:
        assert "Captured the user's own words" not in msg, msg
        assert S._RETENTION_CLAUSE[verdict] in msg, msg
    if verdict in ("missed", "unverified"):
        assert out["verbatim_retained"] is False
    else:
        assert "verbatim_retained" not in out


def test_spine_valid_with_missed_retention_cannot_read_as_retained(monkeypatch):
    be = _Backend({**_SPINE_OK,
                   "/harvest-spans": [{"edges": [{"subject": "d", "rel_type": "lives_in", "object": "h"}]}],
                   "/ingest": [{"status": "valid", "committed": 1, "staged": 0, "facts": []}]})
    monkeypatch.setattr(S, "_http_client", be)
    async def fake_capture(text, uid, intent, *, source="mcp"):
        return "missed"
    monkeypatch.setattr(S, "_episodic_capture_impl", fake_capture)
    monkeypatch.setattr(S, "_harvest_turn_facts", _AsyncZero())
    monkeypatch.setattr(S, "_ground_self_predication_facts", _AsyncZero())
    out, _ = _run(remember_facts_tool(_TEXT, _UID))
    assert out["status"] == "valid" and out["retention"] == "missed"
    assert out["verbatim_retained"] is False
    assert S._RETENTION_CLAUSE["missed"] in (out.get("message") or "")


def test_permanent_ingest_rejection_reports_nothing_pending(monkeypatch):
    """A 400-class /ingest answer is a deterministic decision: `retry_pending: false` must not sit
    beside `edges_pending: N` (nothing IS pending). All three write sites."""
    edge = [{"subject": "d", "rel_type": "lives_in", "object": "h"}]
    rej = ({"detail": "constraint violation"}, 400)
    # rewrite lane
    be = _Backend({**_SPINE_OK, "/internal/ingest-route": [{"statement_extractor": "rewrite"}],
                   "/extract/rewrite": [{"edges": edge}], "/ingest": [rej]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S, "_harvest_turn_facts", _AsyncZero())
    monkeypatch.setattr(S, "_ground_self_predication_facts", _AsyncZero())
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    assert "ingest_permanent_failure" in log
    assert out["retry_pending"] is False and out["edges_pending"] == 0, out
    # spine lane
    be2 = _Backend({**_SPINE_OK, "/harvest-spans": [{"edges": edge}], "/ingest": [rej]})
    monkeypatch.setattr(S, "_http_client", be2)
    out2, _ = _run(S._ingest_statement_via_spine(_TEXT, _UID, "mcp", retention="confirmed"))
    assert out2["retry_pending"] is False and out2["edges_pending"] == 0, out2
    # learn lane
    be3 = _Backend({"/ingest": [rej]})
    monkeypatch.setattr(S, "_http_client", be3)
    out3, _ = _run(S.learn_facts_tool("Dog is a subclass of animal", _UID))
    assert out3["retry_pending"] is False and out3["edges_pending"] == 0, out3
    # and a genuinely deferred failure still says so
    be4 = _Backend({"/ingest": [_timeout()]})
    monkeypatch.setattr(S, "_http_client", be4)
    monkeypatch.setattr(S.asyncio, "sleep", _fast_sleep)
    out4, _ = _run(S.learn_facts_tool("Dog is a subclass of animal", _UID))
    assert out4["retry_pending"] is True and out4["edges_pending"] == 1, out4


def test_rest_learn_facts_is_gated_like_every_other_write_door(monkeypatch):
    S._provisioned_users.discard(_UID)
    monkeypatch.setenv("SAAS_MODE", "")
    import httpx as _hx
    import src.mcp.http_server as H
    monkeypatch.setattr(H, "MCP_API_KEY", "")
    g = S._GateFailed("failed", "template apply failed", "corr-88")
    async def gate(uid):
        return g
    monkeypatch.setattr(S, "_ensure_provisioned", gate)
    called = []
    async def tool(**k):
        called.append(k)
        return {"status": "learned"}
    monkeypatch.setattr(S, "learn_facts_tool", tool)
    async def rest():
        async with _hx.AsyncClient(transport=_hx.ASGITransport(app=H.app), base_url="http://t") as c:
            return await c.post("/learn_facts", json={"text": "Dog is a subclass of animal", "user_id": _UID},
                                headers={"X-OpenWebUI-User-Id": _UID})
    r, _ = _run(rest())
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body.get("status") == "provisioning_failed" and body.get("terminal") is True
    assert body.get("correlation_id") == "corr-88" and body.get("committed") == 0
    assert called == []   # the tool never ran behind a failed gate


# ── ROUND 11: one consumer, no head-of-line blocking; env knob floor; never terminal by time/count ─

def test_kick_interval_env_is_parsed_with_a_floor_and_never_spins(monkeypatch):
    """`0` / negative / empty / garbage log once and fall back to the default; a value under the
    floor is raised to it. The module constant is the parsed value, never a bare float(env)."""
    for raw in ("0", "-3", "", "abc", "nan"):
        monkeypatch.setenv("MCP_EPISODIC_PROV_KICK_INTERVAL_S", raw)
        out, log = _run(_aval(S._env_budget, "MCP_EPISODIC_PROV_KICK_INTERVAL_S", 120.0, 2.0))
        assert out == 120.0, (raw, out)
        assert log.count("env.invalid") == (1 if raw != "nan" or out == 120.0 else 0), (raw, log)
    monkeypatch.setenv("MCP_EPISODIC_PROV_KICK_INTERVAL_S", "0.5")
    out, _ = _run(_aval(S._env_budget, "MCP_EPISODIC_PROV_KICK_INTERVAL_S", 120.0, 2.0))
    assert out == 2.0  # floored at the transport min backoff
    monkeypatch.setenv("MCP_EPISODIC_PROV_KICK_INTERVAL_S", "300")
    out, _ = _run(_aval(S._env_budget, "MCP_EPISODIC_PROV_KICK_INTERVAL_S", 120.0, 2.0))
    assert out == 300.0
    src = open(S.__file__, encoding="utf-8").read()
    assert "_EPISODIC_PROV_KICK_INTERVAL_S = _env_budget(" in src
    assert 'float(os.environ.get("MCP_EPISODIC_PROV_KICK_INTERVAL_S"' not in src


async def _aval(fn, *a):
    return fn(*a)


_UID_B = "6e2f1c0a-1111-4bbb-8ccc-0123456789ab"


def test_a_parked_seat_never_blocks_another_seats_turn(monkeypatch):
    """Seat A is held (seat_unknown; its provisioning kick takes 0.5s of REAL time and answers
    the honest wait); seat B is healthy. A is queued FIRST. B must confirm within its own latency
    (well under the kick's 0.5s) while A is parked waiting on its kick — never behind it. A
    confirms later when its stub finally answers stored. The critic measured the old shape:
    14 turns of two healthy seats waited 136s behind one held seat."""
    import time as _time
    real_sleep = asyncio.sleep
    su = {"status": "soft_error", "reason": "seat_unknown"}

    class _PerSeat(_Backend):
        async def post(self, url, *a, **k):
            if "/episodic/append" in url and k.get("json", {}).get("user_id") == _UID_B:
                self.bodies.setdefault("B", []).append(k["json"])
                return _resp({"status": "stored", "id": 42})
            return await super().post(url, *a, **k)

    be = _PerSeat({"/episodic/append": [su, su, {"status": "stored", "id": 7}]})
    monkeypatch.setattr(S, "_http_client", be)
    S._EPISODIC_PROV_KICK_INTERVAL_S = 0.0
    async def slow_gate(uid):
        await real_sleep(0.5)
        return ""
    monkeypatch.setattr(S, "_ensure_provisioned", slow_gate)
    times = {}
    real_once = S._episodic_reappend_once
    async def spy(body):
        v = await real_once(body)
        if v[0] == "confirmed":
            times[body["user_id"]] = _time.monotonic()
            if body["user_id"] == _UID_B:
                times["A_state_when_B_confirmed"] = (
                    len(S._episodic_reappend_parked), S._episodic_reappend_stats["confirmed"])
        return v
    monkeypatch.setattr(S, "_episodic_reappend_once", spy)
    async def drive():
        _reset_drain()
        t0 = _time.monotonic()
        await S._queue_episodic_reappend({"user_id": _UID, "raw_text": "held", "turn_key": "a" * 32,
                                          "source": "mcp", "intent": "STATEMENT"})
        await S._queue_episodic_reappend({"user_id": _UID_B, "raw_text": "fine", "turn_key": "b" * 32,
                                          "source": "mcp", "intent": "STATEMENT"})
        # bounded: a consumer that blocks on A's kick (the mutant) never resolves B in time
        await asyncio.wait_for(S._episodic_reappend_queue.join(), timeout=5.0)
        return t0
    t0, log = _run(drive())
    assert times[_UID_B] - t0 < 0.25, times[_UID_B] - t0          # B: its own latency
    assert times[_UID] - t0 > 0.9, times[_UID] - t0               # A: after two 0.5s kicks
    assert times["A_state_when_B_confirmed"] == (1, 0)             # A was PARKED, unresolved
    assert "episodic_reappend_parked: seat_unknown" in log and "waiting on it" in log
    assert "episodic_capture_missed" not in log
    assert S._episodic_reappend_stats["confirmed"] == 2 and not os.listdir(S._EPISODIC_SPOOL_DIR)


def test_never_terminal_by_elapsed_time_or_sighting_count_while_the_gate_says_provisioning(monkeypatch):
    """A seat held far past any "reasonable" threshold: the turn has been queued for 700s and
    seat_unknown was seen 40 times with the gate honestly answering provisioning throughout. It
    is STILL queued/pending (never missed), the ONE per-turn CRITICAL blames THIS SEAT (not the
    memory service), and when the seat finally exists the same turn_key is confirmed 1|1."""
    S._provisioned_users.add(_UID)
    S._EPISODIC_UNCONFIRMED_CRIT_S = 300.0
    S._EPISODIC_PROV_KICK_INTERVAL_S = 0.0
    _zero_wait_budget(monkeypatch)
    real_meta = S._new_meta
    def old_meta():
        m = real_meta()
        m["queued_mono"] -= 700.0  # queued "ten minutes ago"
        return m
    monkeypatch.setattr(S, "_new_meta", old_meta)
    su = {"status": "soft_error", "reason": "seat_unknown"}
    be = _Backend({"/episodic/append": [su] * 41 + [{"status": "stored", "id": 11}],
                   "/provisioning/status": _prov(1)})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S.asyncio, "sleep", _fast_sleep)
    v, log = _run(_capture_then_drain(_TEXT, _UID))
    assert v == "pending"
    assert "episodic_capture_missed" not in log and S._episodic_reappend_stats["missed"] == 0
    assert log.count("episodic_reappend_provisioning_kick") == 40
    assert log.count("CRITICAL episodic_capture_unconfirmed") == 1
    crit = next(l for l in log.splitlines() if "CRITICAL episodic_capture_unconfirmed" in l)
    assert "this SEAT is still unknown" in crit and "other seats are NOT affected" in crit
    assert "memory service has not ANSWERED" not in crit
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=11" in log
    assert len({b["turn_key"] for b in be.bodies["/episodic/append"]}) == 1
    assert not os.listdir(S._EPISODIC_SPOOL_DIR)


def test_transport_critical_blames_the_transport_not_the_seat(monkeypatch):
    S._EPISODIC_UNCONFIRMED_CRIT_S = 0.0
    be = _Backend({"/episodic/append": [_timeout("dead"), _timeout("dead"), _timeout("dead"),
                                        _timeout("dead"), {"status": "stored", "id": 12}],
                   "/episodic/probe": [_timeout("dead")]})
    monkeypatch.setattr(S, "_http_client", be)
    _, log = _run(_capture_then_drain(_TEXT, _UID))
    crit = next(l for l in log.splitlines() if "CRITICAL episodic_capture_unconfirmed" in l)
    assert "memory service has not ANSWERED" in crit and "this SEAT" not in crit
    assert "episodic_capture_unconfirmed_cleared" in log


# ── ROUND 12 → 13: every module-level env knob is safe AT IMPORT, fed a VALUE CORPUS ──────────

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HUGE_INT = "9" * 400
_HUGE_ONE = "1" + "0" * 400
_CORPUS = ["", "abc", "nan", "inf", "-inf", "1e400", "0x10", "1.5", "-1", "0", " 42 ",
           _HUGE_INT, _HUGE_ONE, "1e308"]

# THE CONTRACT, BY HAND (round 14 — not derived from `_env_parse`): raw value → per kind, what the
# knob must hold BEFORE the knob's own floor/ceiling are applied, and the WARN kind of the parse
# step. ("default", "invalid") = the parse rejected it; ("value", n) = it parsed to n. The clamp
# step is the second hand-written rule in `_expected`: below floor → floor + "below_floor",
# above ceiling → ceiling + "above_ceiling". Exactly one WARN per knob at most.
_CORPUS_TABLE = {
    "":        {"int": ("default", "invalid"), "float": ("default", "invalid")},
    "abc":     {"int": ("default", "invalid"), "float": ("default", "invalid")},
    "nan":     {"int": ("default", "invalid"), "float": ("default", "invalid")},
    "inf":     {"int": ("default", "invalid"), "float": ("default", "invalid")},
    "-inf":    {"int": ("default", "invalid"), "float": ("default", "invalid")},
    "1e400":   {"int": ("default", "invalid"), "float": ("default", "invalid")},   # float('1e400') is inf
    "0x10":    {"int": ("value", 16),          "float": ("default", "invalid")},   # float('0x10') raises
    "1.5":     {"int": ("default", "invalid"), "float": ("value", 1.5)},           # no silent truncation
    "-1":      {"int": ("value", -1),          "float": ("default", "invalid")},   # budgets: <= 0 invalid
    "0":       {"int": ("value", 0),           "float": ("default", "invalid")},
    " 42 ":    {"int": ("value", 42),          "float": ("value", 42.0)},
    _HUGE_INT: {"int": ("value", int(_HUGE_INT)), "float": ("default", "invalid")},  # int → ceiling; float('9'*400) is inf
    _HUGE_ONE: {"int": ("value", int(_HUGE_ONE)), "float": ("default", "invalid")},  # int → ceiling; float → inf → invalid
    "1e308":   {"int": ("value", 10 ** 308),  "float": ("value", 1e308)},                # → ceiling
}


def _expected(helper, value, default, floor, ceiling):
    """Hand-written contract: (effective value, warn kind or None). `default` None = computed
    (the caller checks shape only when the parse step rejects)."""
    kind = "int" if helper == "_env_int" else "float"
    step, arg = _CORPUS_TABLE[value][kind]
    if step == "default":
        v, warn = default, "invalid"
    else:
        v, warn = arg, None
    if v is None:
        return None, warn
    if floor is not None and v < floor:
        return floor, "below_floor"
    if ceiling is not None and v > ceiling:
        return ceiling, "above_ceiling"
    return v, warn



# THE LIMITS, BY HAND (round 14): a literal copy of what every knob's floor and ceiling must be —
# the expectations below read THIS, and one pin asserts the module's `_ENV_LIMITS` equals it, so
# the table can neither drift nor vanish (an older module without a table fails every ceiling case).
_LIMITS = {
    "MCP_INGEST_RETRY_ATTEMPTS": (1, 10),
    "MCP_INGEST_RETRY_BASE_S": (0.0, 3600.0),
    "MCP_INGEST_RETRY_MAX_INFLIGHT": (1, 100000),
    "MCP_INGEST_DEFER_QUEUE_MAX": (1, 100000),
    "MCP_INGEST_DEFER_ATTEMPTS": (1, 50),
    "MCP_INGEST_DEFER_BASE_S": (0.1, 3600.0),
    "MCP_BACKOFF_CAP_S": (0.0, 3600.0),
    "MCP_STATEMENT_DEFER_EXTRACTION_ATTEMPTS": (1, 50),
    "MCP_STATEMENT_DEFER_BACKOFF_S": (0.1, 3600.0),
    # Vehicle update (foss-es uplift port): the saas-lineage server added this knob to
    # _ENV_LIMITS without updating this hand-written census copy — the drift the pin
    # exists to catch, present on the saas tree itself. Table value verbatim from
    # server.py.
    "MCP_RECALL_TOTAL_BUDGET_S": (1.0, 600.0),
    "MEMORY_TEACHES_MAX_LINES": (0, 100),
    "RECALL_ASSERT_CONF_FLOOR": (0.0, 1.0),
    "EXPERIENCE_SURFACING_MAX_LINES": (0, 100),
    "EXPERIENCE_SURFACING_TIMEOUT": (0.0, 3600.0),
    "DOC_TURN_MAX_CHARS": (200, 1_000_000),
    "MCP_EPISODIC_REAPPEND_BASE_S": (0.0, 3600.0),
    "MCP_EPISODIC_REAPPEND_MAX_DELAY_S": (0.0, 3600.0),
    "MCP_EPISODIC_UNCONFIRMED_CRIT_S": (0.0, 86400.0),
    "MCP_STOP_GRACE_S": (1.0, 3600.0),
    "MCP_EPISODIC_SHUTDOWN_FLUSH_S": (0.0, 3600.0),
    "MCP_EPISODIC_PROV_KICK_INTERVAL_S": (2.0, 86400.0),
    "MCP_PROVISIONING_WAIT_SEC": (1.0, 600.0),
    "MCP_PROVISIONING_POLL_SEC": (0.1, 60.0),
    "MCP_PROVISIONING_ENQUEUE_SETTLE_S": (0.0, 60.0),
    "MCP_TURN_WALL_S": (5.0, 3600.0),
    # brain-independence lane (2026-10-25): the classify route probe + the brain-slow breaker
    "MCP_CLASSIFY_TIMEOUT_S": (0.05, 600.0),
    "MCP_BRAIN_SLOW_COOLDOWN_S": (0.0, 3600.0),
    "CORTEX_STALE_DAYS": (0, 3650),
    "CORTEX_LESSON_BUDGET_CHARS": (0, 100000),
    "INGEST_FILE_B64_MAX_BYTES": (1, 1 << 30),
    "MCP_HSTS_MAX_AGE": (0, 10 * 365 * 86400),
}
_BRAIN_TIMEOUT_LIMITS = (0.05, 600.0)  # every MCP_BRAIN_TIMEOUT_<OP>, by prefix rule (hand-written copy)


def _module_level_env_knobs(path):
    """(env_name, constant_name, helper, default, floor, ceiling) for every module-level
    `X = _env_*("NAME", …)` (nested module-scope blocks included) plus a census of every BARE
    int()/float() env parse REACHABLE AT IMPORT: module scope incl. if/try/with/for bodies AND
    `if` test expressions, class bodies, decorator arguments and default arguments of any def,
    and the bodies of module-defined functions called from import-time code — two levels deep.
    "env parse" = an int()/float() call whose source mentions `environ` or `getenv` under any
    alias (`os.environ.get`, `E.get`, `G(`, `os.environ[...]`)."""
    import ast as _ast
    tree = _ast.parse(open(path, encoding="utf-8").read())
    funcs = {n.name: n for n in tree.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
    knobs, bare = [], []

    def is_env_parse(call):
        if not (isinstance(call, _ast.Call) and isinstance(call.func, _ast.Name) and call.func.id in ("int", "float")):
            return False
        src = _ast.unparse(call)
        return "environ" in src or "getenv" in src

    def import_time_nodes(node):
        """Yield every AST node that EXECUTES at import inside `node` — skipping function/lambda
        bodies (but not their decorators / default arguments)."""
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                yield from import_time_nodes(d)
            for d in list(node.args.defaults) + [k for k in node.args.kw_defaults if k is not None]:
                yield from import_time_nodes(d)
            return
        if isinstance(node, _ast.Lambda):
            return
        yield node
        for child in _ast.iter_child_nodes(node):
            yield from import_time_nodes(child)

    def scan(nodes, where):
        called = []
        for n in nodes:
            if is_env_parse(n):
                bare.append(f"{where}: {_ast.unparse(n)[:80]}")
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name) and n.func.id in funcs:
                called.append(n.func.id)
        return called

    def lit(n):
        return n.value if isinstance(n, _ast.Constant) else (-n.operand.value if isinstance(n, _ast.UnaryOp) and isinstance(n.operand, _ast.Constant) else None)

    # knobs: module-scope assignments (nested blocks included)
    def module_stmts(body):
        for node in body:
            yield node
            if isinstance(node, (_ast.If, _ast.Try, _ast.With, _ast.For, _ast.While)):
                inner = list(getattr(node, "body", [])) + list(getattr(node, "orelse", [])) + list(getattr(node, "finalbody", []))
                for h in getattr(node, "handlers", []):
                    inner += list(h.body)
                yield from module_stmts(inner)
    for node in module_stmts(tree.body):
        targets = None
        if isinstance(node, _ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, _ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if targets and (isinstance(value, _ast.Call) and isinstance(value.func, _ast.Name)
                        and value.func.id.startswith("_env_") and value.args and isinstance(value.args[0], _ast.Constant)):
            floor, ceiling = _LIMITS.get(value.args[0].value, (None, None))
            knobs.append((value.args[0].value, _ast.unparse(targets[0]), value.func.id, lit(value.args[1]), floor, ceiling))

    # bare parses: everything that executes at import, then two levels of module-scope calls
    level1 = scan(list(import_time_nodes(tree)), "import-time code")
    seen = set()
    frontier = [(f, 1) for f in level1]
    while frontier:
        fname, depth = frontier.pop()
        if fname in seen or depth > 2:
            continue
        seen.add(fname)
        body_nodes = [n for stmt in funcs[fname].body for n in _ast.walk(stmt)]
        for callee in scan(body_nodes, f"{fname}() called at module scope (level {depth})"):
            frontier.append((callee, depth + 1))
    # ROUND 15: FUNCTION bodies too — any env read that feeds int()/float() ANYWHERE in the
    # module (the ten MCP_BRAIN_TIMEOUT_<OP> reads and the provisioning wait/poll pair lived in
    # functions and bypassed both the table and the census), including the two-step form
    # `raw = os.environ.get(..); v = float(raw)`. The parse core itself is the one exemption.
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)) or fn.name.startswith("_env_parse"):
            continue
        env_names = set()
        for n in _ast.walk(fn):
            if isinstance(n, _ast.Assign) and ("environ" in _ast.unparse(n.value) or "getenv" in _ast.unparse(n.value)):
                for t in n.targets:
                    if isinstance(t, _ast.Name):
                        env_names.add(t.id)
        for n in _ast.walk(fn):
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name) and n.func.id in ("int", "float"):
                src = _ast.unparse(n)
                if "environ" in src or "getenv" in src or any(
                        isinstance(a, _ast.Name) and a.id in env_names for a in n.args):
                    bare.append(f"{fn.name}() function body: {src[:80]}")
    return knobs, sorted(set(bare))


def _fresh_import(env_name, value, const):
    import subprocess
    env = dict(os.environ)
    env[env_name] = value
    env.setdefault("PYTHONPATH", _REPO)
    code = f"import src.mcp.server as S; print('VALUE=', repr(S.{const}))"
    p = subprocess.run([sys.executable, "-c", code], cwd=_REPO, env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def _fresh_import_all_knobs(knobs, value):
    import subprocess
    env = dict(os.environ)
    for env_name, *_ in knobs:
        env[env_name] = value
    env.setdefault("PYTHONPATH", _REPO)
    consts = ", ".join(f"{c!r}: S.{c}" for _, c, *_ in knobs)
    code = f"import src.mcp.server as S, json; print('VALUES=' + json.dumps({{{consts}}}))"
    p = subprocess.run([sys.executable, "-c", code], cwd=_REPO, env=env, capture_output=True, text=True, timeout=90)
    return p.returncode, p.stdout, p.stderr


def test_no_module_level_knob_still_parses_through_a_bare_int_or_float():
    knobs, bare = _module_level_env_knobs(os.path.join(_REPO, "src", "mcp", "server.py"))
    assert bare == [], f"bare int()/float() env parses reachable at import (garbage crashes the import): {bare}"
    assert any(k[0] == "MCP_EPISODIC_PROV_KICK_INTERVAL_S" for k in knobs)
    assert len(knobs) >= 18, knobs
    _, bare_http = _module_level_env_knobs(os.path.join(_REPO, "src", "mcp", "http_server.py"))
    assert bare_http == [], bare_http


def test_census_walker_sees_if_try_and_module_called_functions(tmp_path):
    """The census itself is pinned: a bare parse inside a module-scope `if`, a `try`, and a
    function called at module scope must all be caught."""
    f = tmp_path / "m.py"
    f.write_text("import os\nif True:\n    A = int(os.environ.get('X', '1'))\ntry:\n    B = float(os.getenv('Y', '2'))\n"
                 "except ValueError:\n    B = 2.0\ndef boot():\n    return int(os.environ.get('Z', '3'))\nC = boot()\n")
    _, bare = _module_level_env_knobs(str(f))
    assert len(bare) >= 3 and any("'X'" in b for b in bare) and any("'Y'" in b for b in bare) and any("boot()" in b for b in bare), bare


@pytest.mark.parametrize("value", _CORPUS)
def test_every_knob_fed_the_corpus_boots_a_fresh_interpreter_with_the_contract_value(value):
    """For EVERY module-level knob at once, set to `value`, a FRESH interpreter imports the module
    with exit 0, ≤ ONE WARN per knob (exactly one iff the HAND-WRITTEN table says invalid /
    below-floor / above-ceiling, naming that knob and that kind), and each constant holds the
    table's value (default, the floor, the ceiling, or the parsed number) — and is NEVER outside
    the knob table's range. Deleting the non-finite rejection, the OverflowError catch, the loud
    clamp, or the ceiling goes red here."""
    knobs, _ = _module_level_env_knobs(os.path.join(_REPO, "src", "mcp", "server.py"))
    rc, out, err = _fresh_import_all_knobs(knobs, value)
    assert rc == 0 and "Traceback" not in err, (value, err[-1200:])
    values = json.loads(out.split("VALUES=", 1)[1].strip().splitlines()[0])
    problems = []
    for env_name, const, helper, default, floor, ceiling in knobs:
        expected, warn_kind = _expected(helper, value, default, floor, ceiling)
        warns = [l for l in err.splitlines() if f" {env_name}=" in l and "WARN env." in l]
        got = values[const]
        if expected is None:  # computed default rejected-path: shape only (finite, within the table's range)
            ok = isinstance(got, (int, float)) and got == got and abs(got) != float("inf")
            ok = ok and (floor is None or got >= floor) and (ceiling is None or got <= ceiling)
            if not ok:
                problems.append((env_name, value, got, "finite in-range default expected"))
        elif got != expected:
            problems.append((env_name, value, got, expected))
        if floor is not None and got < floor or ceiling is not None and got > ceiling:
            problems.append((env_name, value, got, "OUT OF THE TABLE'S RANGE"))
        if len(warns) != (1 if warn_kind else 0):
            problems.append((env_name, value, "warns", warns))
        if warn_kind and warn_kind not in warns[0]:
            problems.append((env_name, value, "warn kind", warns[0][:100], warn_kind))
    assert problems == [], problems


def test_env_int_rejects_non_integral_loudly_and_clamps_loudly(monkeypatch):
    """`1.5` is INVALID for an int knob (no silent truncation — a stated choice); a value under the
    floor is clamped WITH a WARN; hex and padded decimals are accepted like int()."""
    monkeypatch.setenv("K_INT", "1.5")
    v, log = _run(_aval(S._env_int, "K_INT", 7, 1))
    assert v == 7 and "WARN env.invalid: K_INT='1.5'" in log
    monkeypatch.setenv("K_INT", "0")
    v, log = _run(_aval(S._env_int, "K_INT", 7, 1))
    assert v == 1 and "WARN env.below_floor: K_INT='0'" in log
    for raw, exp in (("0x10", 16), (" 42 ", 42), ("1e3", 1000)):
        monkeypatch.setenv("K_INT", raw)
        v, log = _run(_aval(S._env_int, "K_INT", 7, 1))
        assert v == exp and "WARN" not in log, (raw, v, log)
    for raw in ("inf", "-inf", "1e400", "nan"):
        monkeypatch.setenv("K_INT", raw)
        v, log = _run(_aval(S._env_int, "K_INT", 7, 1))
        assert v == 7 and "WARN env.invalid" in log, (raw, v, log)
        monkeypatch.setenv("K_F", raw)
        v, log = _run(_aval(S._env_float, "K_F", 2.5))
        assert v == 2.5 and "WARN env.invalid" in log, (raw, v, log)


# ── ROUND 12: the placement-blind pins made real ─────────────────────────────────────────────

def test_between_kicks_path_is_never_terminal_by_count_or_time(monkeypatch):
    """The PRODUCTION path (interval 120s): after ONE kick the seat's sightings back off between
    kicks. Six between-kick sightings on a turn queued 700s ago, gate answering provisioning →
    still pending, never missed, then confirmed on the same key. A terminal arm on THIS path (the
    critic's surviving mutant placement) goes red here."""
    S._provisioned_users.add(_UID)
    S._EPISODIC_PROV_KICK_INTERVAL_S = 3600.0
    S._EPISODIC_UNCONFIRMED_CRIT_S = 300.0
    _zero_wait_budget(monkeypatch)
    real_meta = S._new_meta
    def old_meta():
        m = real_meta(); m["queued_mono"] -= 700.0; return m
    monkeypatch.setattr(S, "_new_meta", old_meta)
    su = {"status": "soft_error", "reason": "seat_unknown"}
    be = _Backend({"/episodic/append": [su] * 8 + [{"status": "stored", "id": 21}],
                   "/provisioning/status": _prov(1)})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S.asyncio, "sleep", _fast_sleep)
    async def drive():
        _reset_drain()
        v = await S._episodic_capture(_TEXT, _UID, "STATEMENT")
        await asyncio.wait_for(S._episodic_reappend_queue.join(), timeout=10.0)
        return v
    v, log = _run(drive())
    assert v == "pending"
    assert log.count("episodic_reappend_provisioning_kick") == 1          # one kick, then between-kick backoffs
    assert log.count("episodic_reappend_retry: attempt") >= 6             # ≥ 6 between-kick sightings
    assert "episodic_capture_missed" not in log and S._episodic_reappend_stats["missed"] == 0
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=21" in log
    assert len({b["turn_key"] for b in be.bodies["/episodic/append"]}) == 1
    assert not os.listdir(S._EPISODIC_SPOOL_DIR)


def test_parked_seat_is_rekeyed_and_picked_within_one_tick_of_its_kick(monkeypatch):
    """A turn parked on its seat's kick (next_at = +inf) must be re-keyed by the kick's done-
    callback and picked by the consumer within one loop tick — an unregistered waiter or a
    picker that skips the parked heap when the intake is empty leaves it parked forever (bounded
    here, so those mutants FAIL in seconds instead of hanging)."""
    import time as _time
    real_sleep = asyncio.sleep
    su = {"status": "soft_error", "reason": "seat_unknown"}
    be = _Backend({"/episodic/append": [su, {"status": "stored", "id": 31}]})
    monkeypatch.setattr(S, "_http_client", be)
    S._EPISODIC_PROV_KICK_INTERVAL_S = 0.0
    marks = {}
    async def slow_gate(uid):
        await real_sleep(0.3)
        marks["kick_done"] = _time.monotonic()
        return "ready"
    monkeypatch.setattr(S, "_ensure_provisioned", slow_gate)
    real_once = S._episodic_reappend_once
    async def spy(body):
        if "kick_done" in marks and "picked_after_kick" not in marks:
            marks["picked_after_kick"] = _time.monotonic()
        return await real_once(body)
    monkeypatch.setattr(S, "_episodic_reappend_once", spy)
    async def drive():
        _reset_drain()
        await S._queue_episodic_reappend({"user_id": _UID, "raw_text": "t", "turn_key": "c" * 32,
                                          "source": "mcp", "intent": "STATEMENT"})
        await asyncio.wait_for(S._episodic_reappend_queue.join(), timeout=4.0)
    _, log = _run(drive())
    assert "picked_after_kick" in marks, log[-600:]
    assert marks["picked_after_kick"] - marks["kick_done"] < 0.05, marks
    assert "episodic_reappend_kick_unparked: 1 turn(s)" in log
    assert "episodic_capture_confirmed_by_drain" in log and "episodic_id=31" in log


def test_consumer_yields_between_picks_under_a_zero_delay_parked_storm():
    """Dead backend + zero backoff = a parked storm of immediate re-attempts. The consumer must
    still give the loop a turn per pick (through a future, not `asyncio.sleep`, which tests stub
    out) — otherwise the driver coroutine never resumes. Watchdog THREAD so a consumer that never
    yields fails in seconds instead of hanging the runner."""
    import threading
    be = _Backend({"/episodic/append": [_timeout("dead")], "/episodic/probe": [_timeout("dead")]})
    result = {}
    async def drive():
        S._http_client = be
        _reset_drain()
        await S._episodic_capture("storm", _UID, "STATEMENT")
        await asyncio.sleep(0.05)   # the driver must get the loop back while the storm runs
        result["attempts"] = S._episodic_reappend_stats["queued"] and S._all_unconfirmed_entries()[0][1]["attempts"]
        S._episodic_reappend_task.cancel()
        try:
            await S._episodic_reappend_task
        except asyncio.CancelledError:
            pass
        result["done"] = True
    saved = S._http_client
    t = threading.Thread(target=lambda: asyncio.run(drive()), daemon=True)
    t.start(); t.join(timeout=5.0)
    S._http_client = saved
    assert result.get("done") is True, "the consumer never yielded: the driver coroutine did not resume in 5s"
    assert result["attempts"] >= 2


def test_second_turn_of_a_seat_registers_on_the_in_flight_kick_and_is_rekeyed(monkeypatch):
    """Round-13 probe shape: turn 1 starts the seat's kick; turn 2 of the SAME seat arrives while
    that kick is in flight → it must be registered as a waiter on THAT kick (not start another,
    not park unregistered) and be re-keyed with turn 1 when the kick completes. Bounded, so an
    unregistered in-flight waiter FAILS in 4s instead of hanging."""
    real_sleep = asyncio.sleep
    su = {"status": "soft_error", "reason": "seat_unknown"}
    be = _Backend({"/episodic/append": [su, su, {"status": "stored", "id": 51}, {"status": "stored", "id": 52}]})
    monkeypatch.setattr(S, "_http_client", be)
    S._EPISODIC_PROV_KICK_INTERVAL_S = 3600.0
    kicks = []
    async def slow_gate(uid):
        kicks.append(uid)
        await real_sleep(0.4)
        return "ready"
    monkeypatch.setattr(S, "_ensure_provisioned", slow_gate)
    async def drive():
        _reset_drain()
        await S._queue_episodic_reappend({"user_id": _UID, "raw_text": "one", "turn_key": "d" * 32,
                                          "source": "mcp", "intent": "STATEMENT"})
        await real_sleep(0.1)  # turn 1 has been attempted and parked on its kick
        await S._queue_episodic_reappend({"user_id": _UID, "raw_text": "two", "turn_key": "e" * 32,
                                          "source": "mcp", "intent": "STATEMENT"})
        await asyncio.wait_for(S._episodic_reappend_queue.join(), timeout=4.0)
    _, log = _run(drive())
    assert kicks == [_UID]                                   # ONE kick for the seat, not one per turn
    assert "waiting on the seat's in-flight provisioning kick" in log
    assert "episodic_reappend_kick_unparked: 2 turn(s)" in log
    assert log.count("episodic_capture_confirmed_by_drain") == 2
    assert S._episodic_reappend_stats["confirmed"] == 2 and not S._episodic_reappend_parked


def test_picker_wakes_at_a_future_due_time_with_an_empty_intake(monkeypatch):
    """A turn parked with a REAL backoff (0.15s) and nothing else queued: the picker must wake at
    the due time on its own (the wait carries the heap head's due time as its timeout) — a picker
    that waits for intake or a wake event only would sleep until the next unrelated turn arrives.
    Bounded, so that mutant FAILS in 4s."""
    import time as _time
    S._EPISODIC_REAPPEND_BASE_S = 0.15
    S._EPISODIC_REAPPEND_MAX_DELAY_S = 0.15
    monkeypatch.setattr(S, "_ingest_backoff_delay", lambda attempt, base: 0.15)  # deterministic delay
    be = _Backend({"/episodic/append": [_timeout("blip"), _timeout("blip"), {"status": "stored", "id": 61}],
                   "/episodic/probe": [_timeout("blip")]})
    monkeypatch.setattr(S, "_http_client", be)
    stamps = []
    real_once = S._episodic_reappend_once
    async def spy(body):
        stamps.append(_time.monotonic())
        return await real_once(body)
    monkeypatch.setattr(S, "_episodic_reappend_once", spy)
    async def drive():
        _reset_drain()
        await S._queue_episodic_reappend({"user_id": _UID, "raw_text": "t", "turn_key": "f" * 32,
                                          "source": "mcp", "intent": "STATEMENT"})
        await asyncio.wait_for(S._episodic_reappend_queue.join(), timeout=4.0)
    _, log = _run(drive())
    assert len(stamps) >= 2 and "episodic_capture_confirmed_by_drain" in log
    assert stamps[1] - stamps[0] >= 0.12, stamps   # the 0.15s park was honoured, and woken on its own


# ── ROUND 14: ceilings, capped backoff, floor on the default path, census reach, watchdog ──────

def test_every_knob_has_a_floor_and_a_ceiling_in_the_one_table():
    knobs, _ = _module_level_env_knobs(os.path.join(_REPO, "src", "mcp", "server.py"))
    assert dict(getattr(S, "_ENV_LIMITS", {})) == _LIMITS, "the module's knob table drifted from the hand-written copy"
    missing = [k[0] for k in knobs if k[0] not in _LIMITS or _LIMITS[k[0]][1] is None]
    assert missing == [], f"knobs without a ceiling in the table: {missing}"
    # class maxima, stated
    assert S._ENV_LIMITS["MCP_INGEST_RETRY_ATTEMPTS"] == (1, 10)
    assert S._ENV_LIMITS["MCP_INGEST_DEFER_ATTEMPTS"][1] <= 50
    assert all(S._ENV_LIMITS[k][1] <= 3600 for k in ("MCP_INGEST_RETRY_BASE_S", "MCP_BACKOFF_CAP_S",
                                                     "MCP_EPISODIC_REAPPEND_MAX_DELAY_S", "MCP_STOP_GRACE_S"))
    assert S._ENV_LIMITS["MCP_EPISODIC_PROV_KICK_INTERVAL_S"] == (2.0, 86400.0)
    # no call site re-declares a floor: the table is the one place
    src = open(S.__file__, encoding="utf-8").read()
    assert not [l for l in src.splitlines() if l.startswith("_") and "_env_" in l and "floor=" in l]


def test_backoff_is_capped_from_the_table_and_a_huge_attempt_count_defers_within_the_bound(monkeypatch):
    """`base * 2**attempt` was uncapped (a 400-digit attempt count requested 1.34e13 s of sleep and
    the turn hung). Now every backoff is `uniform(0, min(MCP_BACKOFF_CAP_S, base*2**attempt))`, the
    cap table-bounded, and the attempt count ceilinged at 10 — so a dead backend returns the loud
    pending/deferred status within attempts × (cap + timeout), never hangs."""
    assert S._ingest_backoff_delay(400, 0.75) <= S._BACKOFF_CAP_S
    assert S._ingest_backoff_delay(10 ** 400, 0.75) <= S._BACKOFF_CAP_S
    assert S._ingest_backoff_delay(3, 0.0) == 0.0
    # the module constant is table-clamped even from a 12-digit env value (fresh interpreter)
    rc, out, err = _fresh_import("MCP_INGEST_RETRY_ATTEMPTS", "999999999999", "_INGEST_RETRY_ATTEMPTS")
    assert rc == 0 and "VALUE= 10" in out and "above_ceiling" in err
    # and the real seam against a dead backend: exactly the ceiling's attempts, then DEFERRED
    monkeypatch.setattr(S, "_INGEST_RETRY_ATTEMPTS", 10)
    monkeypatch.setattr(S, "_INGEST_RETRY_BASE_S", 0.0)
    be = _Backend({"/ingest": [_timeout("dead")]})
    monkeypatch.setattr(S, "_http_client", be)
    S._ingest_defer_queue = None
    monkeypatch.setattr(S, "_ingest_defer_worker", _sleep_forever)
    import time as _time
    t0 = _time.monotonic()
    (data, reason), log = _run(S._ingest_with_retry({"user_id": _UID, "text": "t", "edges": [{"subject": "a", "rel_type": "b", "object": "c"}], "source": "mcp"}, label="pin"))
    assert data is None and reason == "deferred", (data, reason, log[-300:])
    assert be.calls.count("/ingest") == 10 and _time.monotonic() - t0 < 5.0
    assert "ingest_turn_not_committed" in log and "deferred_retry=queued" in log
    src = open(S.__file__, encoding="utf-8").read()
    assert "base * (2 ** attempt)" not in src   # the uncapped form is gone
    assert src.count("_ingest_backoff_delay(") >= 5  # every backoff site goes through the capped helper


def test_floor_and_ceiling_apply_on_the_default_path_too(monkeypatch):
    """A default below its floor (or above its ceiling) — from the table or the call — is clamped
    LOUDLY even when the env is unset or invalid; a value below the floor can never come out."""
    monkeypatch.setitem(S._ENV_LIMITS, "K_PIN", (2.0, 100.0))
    monkeypatch.delenv("K_PIN", raising=False)
    v, log = _run(_aval(S._env_budget, "K_PIN", 0.5))
    assert v == 2.0 and "WARN env.below_floor: K_PIN='<default 0.5>'" in log
    monkeypatch.setenv("K_PIN", "abc")
    v, log = _run(_aval(S._env_budget, "K_PIN", 0.5))
    assert v == 2.0 and "env.invalid" in log and "below_floor" in log
    monkeypatch.delenv("K_PIN", raising=False)
    v, log = _run(_aval(S._env_budget, "K_PIN", 500.0))
    assert v == 100.0 and "WARN env.above_ceiling: K_PIN='<default 500.0>'" in log
    monkeypatch.setenv("K_PIN", "1e308")
    v, log = _run(_aval(S._env_budget, "K_PIN", 5.0))
    assert v == 100.0 and "above_ceiling" in log and log.count("WARN env.") == 1


def test_census_walker_reaches_class_bodies_decorators_defaults_if_tests_two_level_calls_and_aliases(tmp_path):
    """Each hiding place is planted in a scratch module and must be caught: a class body, a
    decorator argument, a default argument, an `if` TEST expression, a two-level module-scope call
    chain, and aliased `environ` / `getenv` / subscript access."""
    plants = {
        "class body":     "class Cfg:\n    A = int(os.environ.get('P_CLASS', '1'))\n",
        "decorator arg":  "def deco(n):\n    return lambda f: f\n@deco(int(os.getenv('P_DECO', '2')))\ndef h():\n    pass\n",
        "default arg":    "def g(x=float(os.environ.get('P_DEFAULT', '3'))):\n    return x\n",
        "if test":        "if int(os.environ.get('P_IFTEST', '0')) > 1:\n    pass\n",
        "two-level call": "def inner():\n    return int(os.environ.get('P_TWO', '4'))\ndef outer():\n    return inner()\nC = outer()\n",
        "aliased environ": "from os import environ as E, getenv as G\nD = int(E.get('P_ALIAS1', '5'))\nF = float(G('P_ALIAS2', '6'))\nH = int(os.environ['P_ALIAS3'])\n",
    }
    for label, code in plants.items():
        f = tmp_path / f"{label.replace(' ', '_')}.py"
        f.write_text("import os\n" + code)
        _, bare = _module_level_env_knobs(str(f))
        assert bare, f"census missed the plant: {label}\n{code}"
    f = tmp_path / "clean.py"
    f.write_text("import os\nX = os.environ.get('P_STR', 'a')\nY = os.environ.get('P_STR2', 'b').strip().lower()\n")
    _, bare = _module_level_env_knobs(str(f))
    assert bare == [], bare   # string parses are not flagged
    f = tmp_path / "fnonly.py"
    f.write_text("import os\ndef unused():\n    return int(os.environ.get('P_UNUSED', '1'))\n")
    _, bare = _module_level_env_knobs(str(f))
    assert len(bare) == 1 and "unused() function body" in bare[0], bare   # round 15: function bodies count too


def test_watchdog_runner_fails_a_consumer_that_never_yields_in_seconds(monkeypatch):
    """The runner itself is pinned: a consumer that never gives the loop back (simulated by a
    backend whose call spins the CPU without awaiting) trips the loop-liveness watchdog in
    ~_STALL_S seconds with an AssertionError — never a hang of the file."""
    import time as _time
    class _Spin:
        async def post(self, url, *a, **k):
            _time.sleep(0.2)  # blocking: the loop cannot turn while this runs
            raise httpx.ReadTimeout("dead", request=MagicMock())
        async def get(self, url, *a, **k):
            return _resp({"status": "ready"})
    monkeypatch.setattr(S, "_http_client", _Spin())
    async def _no_yield():
        return None
    monkeypatch.setattr(S, "_yield_once", _no_yield)  # the mutant: no per-pick yield
    async def drive():
        _reset_drain()
        await S._episodic_capture("spin", _UID, "STATEMENT")
        await asyncio.sleep(30)  # never resumes under the mutant
    t0 = _time.monotonic()
    with pytest.raises(AssertionError, match="WATCHDOG"):
        _run(drive())
    assert _time.monotonic() - t0 < _WATCHDOG_S


# ── ROUND 15: function-level knobs, the per-turn wall, the heartbeat's production half ─────────

_FN_KNOBS = [  # (env name, helper kind, default, accessor expression evaluated twice in the fresh interpreter)
    ("MCP_BRAIN_TIMEOUT_CLASSIFY_INTENT", "_env_float", 10.0, "S._brain_timeout('CLASSIFY_INTENT')"),
    ("MCP_BRAIN_TIMEOUT_HARVEST_SPANS", "_env_float", 60.0, "S._brain_timeout('HARVEST_SPANS')"),
    ("MCP_PROVISIONING_WAIT_SEC", "_env_float", 90.0, "S._env_float('MCP_PROVISIONING_WAIT_SEC', 90.0)"),
    ("MCP_PROVISIONING_POLL_SEC", "_env_float", 2.0, "S._env_float('MCP_PROVISIONING_POLL_SEC', 2.0)"),
    ("MCP_PROVISIONING_ENQUEUE_SETTLE_S", "_env_float", 6.0, "S._env_float('MCP_PROVISIONING_ENQUEUE_SETTLE_S', 6.0)"),
    ("CORTEX_LESSON_BUDGET_CHARS", "_env_int", 500, "S._env_int('CORTEX_LESSON_BUDGET_CHARS', 500)"),
]


def test_function_level_knobs_have_table_rows_and_the_brain_family_a_prefix_rule():
    assert dict(S._ENV_LIMITS) == _LIMITS, "the module's knob table drifted from the hand-written copy"
    for op in S._BRAIN_TIMEOUT_DEFAULTS:
        assert S._env_limits_for(f"MCP_BRAIN_TIMEOUT_{op}") == _BRAIN_TIMEOUT_LIMITS, op
    assert S._env_limits_for("MCP_PROVISIONING_POLL_SEC") == (0.1, 60.0)
    assert S._env_limits_for("SOME_UNKNOWN_KNOB") == (None, None)
    # the census walks FUNCTION bodies now: the module is clean of function-level bare parses
    _, bare = _module_level_env_knobs(os.path.join(_REPO, "src", "mcp", "server.py"))
    assert bare == [], bare


@pytest.mark.parametrize("value", _CORPUS)
def test_function_level_knobs_fed_the_corpus_warn_once_per_process_and_clamp(value):
    """The per-call family (brain timeouts, provisioning wait/poll, lesson budget) set to `value`:
    a FRESH interpreter, each accessor called TWICE — exactly one WARN per bad knob per process
    (parsed-once cache), both calls equal, value per the hand-written tables."""
    import subprocess
    env = dict(os.environ)
    for name, *_ in _FN_KNOBS:
        env[name] = value
    env.setdefault("PYTHONPATH", _REPO)
    code = "import src.mcp.server as S, json; out = {}\n"
    for name, _h, _d, expr in _FN_KNOBS:
        code += f"out[{name!r}] = [{expr}, {expr}]\n"
    code += "print('VALUES=' + json.dumps(out))"
    p = subprocess.run([sys.executable, "-c", code], cwd=_REPO, env=env, capture_output=True, text=True, timeout=90)
    assert p.returncode == 0 and "Traceback" not in p.stderr, (value, p.stderr[-1200:])
    values = json.loads(p.stdout.split("VALUES=", 1)[1].strip().splitlines()[0])
    problems = []
    for name, helper, default, _expr in _FN_KNOBS:
        floor, ceiling = _BRAIN_TIMEOUT_LIMITS if name.startswith("MCP_BRAIN_TIMEOUT_") else _LIMITS[name]
        expected, warn_kind = _expected(helper, value, default, floor, ceiling)
        a, b = values[name]
        warns = [l for l in p.stderr.splitlines() if f" {name}=" in l and "WARN env." in l]
        if a != b or a != expected:
            problems.append((name, value, (a, b), expected))
        if len(warns) != (1 if warn_kind else 0) or (warn_kind and warn_kind not in warns[0]):
            problems.append((name, value, "warns", warns))
    assert problems == [], problems


def test_census_catches_a_function_level_bare_parse_and_the_two_step_form(tmp_path):
    f = tmp_path / "fn.py"
    f.write_text("import os\ndef budget(op):\n    raw = os.environ.get('MCP_X_' + op)\n    if raw is not None:\n"
                 "        return float(raw)\n    return 1.0\ndef other():\n    return int(os.getenv('Y', '3'))\n")
    _, bare = _module_level_env_knobs(str(f))
    assert len(bare) == 2 and any("budget()" in b for b in bare) and any("other()" in b for b in bare), bare


class _BlackHole:
    """A backend that never answers: every call waits out its `timeout` then raises ReadTimeout
    (honours the per-call budget the way a real black-holed socket does)."""
    def __init__(self):
        self.calls = []
        self.real_sleep = asyncio.sleep
    async def _hang(self, url, timeout=None, **k):
        self.calls.append((url.rsplit("/", 1)[-1], timeout))
        await self.real_sleep(float(timeout) if timeout else 10.0)
        raise httpx.ReadTimeout("black hole", request=MagicMock())
    async def post(self, url, *a, **k):
        return await self._hang(url, **k)
    async def get(self, url, *a, **k):
        return await self._hang(url, **k)


def test_turn_wall_bounds_a_black_holed_turn_and_the_loud_status_returns_in_time(monkeypatch):
    """MCP_TURN_WALL_S bounds brain budgets + capture + /ingest retries: with a black-holed
    backend the write door returns the LOUD status (degraded, retention pending/unverified, the
    turn queued for the drain) within the wall — never after the caller (OpenWebUI's 300 s
    AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER) has hung up. Default brain budgets would take 2×10 s for
    classify alone; the wall here is 3 s."""
    import time as _time
    S._provisioned_users.add(_UID)
    be = _BlackHole()
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setattr(S, "_TURN_WALL_S", 3.0)
    monkeypatch.setattr(S, "_queue_episodic_reappend", _AsyncTrue())
    t0 = _time.monotonic()
    out, log = _run(remember_facts_tool(_TEXT, _UID))
    wall = _time.monotonic() - t0
    assert wall < 3.0 + 1.5, (wall, log[-400:])
    assert out["status"] == "degraded" and out["isError"] is True, out
    assert out["retention"] in ("pending", "unverified"), out
    assert "turn_wall_exhausted" in log or "turn wall" in log
    assert all((t or 10) <= 3.05 for _, t in be.calls), be.calls   # every per-call timeout was capped by the wall
    # and the learn door: /ingest retries stop when the wall cannot fit the next attempt → deferred
    S._ingest_defer_queue = None
    monkeypatch.setattr(S, "_ingest_defer_worker", _sleep_forever)
    monkeypatch.setattr(S, "_INGEST_RETRY_BASE_S", 0.5)
    t0 = _time.monotonic()
    out2, log2 = _run(S.learn_facts_tool("Dog is a subclass of animal", _UID))
    assert _time.monotonic() - t0 < 3.0 + 1.5
    assert out2["status"] == "degraded" and out2["retry_pending"] is True, out2
    assert "ingest_retry_turn_wall" in log2 and "deferred_retry=queued" in log2
    assert S._ENV_LIMITS["MCP_TURN_WALL_S"] == (5.0, 3600.0) and 5.0 <= S._env_budget("MCP_TURN_WALL_S", 240.0) < 300.0


def test_liveness_ticker_stamps_while_the_worker_lives_and_a_slow_yielding_pick_is_not_a_stall(monkeypatch):
    """Production half of the heartbeat: the loop-callback ticker advances the stamp every 0.25 s
    while the drain worker is alive — even during a pick that legitimately takes 1.2 s but
    YIELDS (the stall is keyed on the loop turning, not on pick duration). A ticker that does
    not stamp goes red here."""
    import time as _time
    real_sleep = asyncio.sleep
    class _Slow(_Backend):
        async def post(self, url, *a, **k):
            if "/episodic/append" in url:
                await real_sleep(1.2)  # slow but yielding
            return await super().post(url, *a, **k)
    be = _Slow({"/episodic/append": [{"status": "stored", "id": 71}]})
    monkeypatch.setattr(S, "_http_client", be)
    samples = []
    async def drive():
        _reset_drain()
        await S._queue_episodic_reappend({"user_id": _UID, "raw_text": "t", "turn_key": "9" * 32,
                                          "source": "mcp", "intent": "STATEMENT"})
        for _ in range(5):
            await real_sleep(0.3)
            samples.append(S._episodic_loop_alive_mono)
        await asyncio.wait_for(S._episodic_reappend_queue.join(), timeout=5.0)
    _, log = _run(drive())
    assert samples[0] > 0 and samples[-1] > samples[0] and len(set(samples)) >= 3, samples
    assert "episodic_capture_confirmed_by_drain" in log


def test_defer_exhausted_sentence_is_truthful_per_lane():
    c = S._defer_exhausted_consequence
    assert "DROPPED" in c("learn_facts", {"source": "llm_learn"}) and "retains no verbatim" in c("learn_facts", {"source": "llm_learn"})
    assert "PROVENANCE-PRESERVED" in c("remember_facts.rewrite", {"source": "mcp"})
    assert "user_stated stays user_stated" in c("statement_via_spine", {"source": "mcp"})
    assert "llm_inferred" in c("recall.divert", {"source": "unattested"})
    src = open(S.__file__, encoding="utf-8").read()
    assert "it re-ingests as \\n" not in src and "re-ingests as llm_inferred, NOT user_stated" not in src


class _HonestSlow(_Backend):
    """A backend that HONOURS the per-call timeout (round 16): an append needs 2 s of the caller's
    patience; given less it waits out the timeout and raises ReadTimeout like a real socket."""
    def __init__(self, script, need_s=2.0):
        super().__init__(script)
        self.need_s = need_s
        self.real_sleep = asyncio.sleep
        self.timeouts: list = []
    async def post(self, url, *a, timeout=None, **k):
        if "/episodic/append" in url:
            self.timeouts.append(timeout)
            t = float(timeout) if timeout else 10.0
            if t < self.need_s:
                await self.real_sleep(t)
                raise httpx.ReadTimeout("honest timeout", request=MagicMock())
            await self.real_sleep(self.need_s)
        return await super().post(url, *a, **k)


def test_background_workers_never_inherit_the_turn_wall(monkeypatch):
    """Measured live (round 15): the drain worker was created from inside a turn and inherited the
    turn's ABSOLUTE deadline through the task's copied context — after the wall expired every
    re-append answered `turn_wall_exhausted` forever and the seat's words sat in the spool 305 s+.
    BEHAVIOURAL pin (round 16): a timeout-HONOURING backend that needs 2 s; a turn opens a 2 s
    wall, queues a re-append, and expires; the drain (started from INSIDE that turn) must still
    confirm with an UNBOUNDED per-call timeout. With the deadline-clear deleted from
    `_background` the worker's first attempt is capped by the dying wall, every later attempt is
    `turn_wall_exhausted`, and the join times out — red in 8 s."""
    be = _HonestSlow({"/episodic/append": [{"status": "stored", "id": 81}]})
    monkeypatch.setattr(S, "_http_client", be)
    real_sleep = asyncio.sleep
    async def drive():
        _reset_drain()
        tok = S._turn_wall_open(2.0)
        try:
            await real_sleep(1.5)      # 0.5 s of wall left when the turn queues the re-append
            await S._queue_episodic_reappend({"user_id": _UID, "raw_text": "t", "turn_key": "a1" * 16,
                                              "source": "mcp", "intent": "STATEMENT"})
            await real_sleep(0.7)      # the wall has now expired for the TURN
        finally:
            S._turn_deadline.reset(tok)
        await asyncio.wait_for(S._episodic_reappend_queue.join(), timeout=8.0)
    _, log = _run(drive())
    assert "episodic_capture_confirmed_by_drain" in log and "turn_wall_exhausted" not in log, log[-600:]
    assert be.timeouts and all((t or 10) >= 2.0 for t in be.timeouts), be.timeouts   # never capped by a turn


# ROUND 17: the allowlist is keyed PER CALL SITE — (file, innermost enclosing def, callee of the
# created coroutine / scheduled callback) — never per function, so a def with one allowlisted
# site and one routed site goes red the moment the routed one reverts. The alternative is an
# explicit `# background: inherits-wall <reason>` marker on the exact line.
_TASK_CREATION_ALLOWLIST = {
    ("server.py", "_background", "_outside_turn"): "the wrapper itself — the one sanctioned create_task",
    ("server.py", "_recall_memory_tool_impl", "_cortex_lane"): "recall's per-request fan-out: part of THIS request, inherits its wall (bounded by the TurnBoundedClient)",
    ("server.py", "_recall_memory_tool_impl", "_pending_documents_notice"): "recall's per-request fan-out: part of THIS request",
    ("server.py", "_recall_memory_tool_impl", "remember_facts_tool"): "recall's STATEMENT divert: part of THIS request (keeps the outer wall; brain/ingest bounded)",
    ("server.py", "_recall_memory_tool_impl", "_harvest_turn_facts"): "recall's harvest: part of THIS request (its /harvest-spans post is wall-bounded by the client)",
    ("server.py", "_recall_memory_tool_impl", "_episodic_capture"): "recall's FAST-PATH retention (brain-independence lane): replaces the harvest on a brain-slow turn; one /episodic/append via _brain_request, wall-bounded",
    ("server.py", "_recall_memory_tool_impl", "_walk"): "recall's walk: part of THIS request (via _brain_request, wall-bounded)",
    ("server.py", "_next_reappend_work", "get"): "the drain picker's intake future — inside the worker, already outside any wall",
    ("server.py", "_next_reappend_work", "wait"): "the drain picker's wake future — inside the worker, already outside any wall",
    ("server.py", "_liveness_tick", "_liveness_tick"): "the loop-callback liveness ticker re-arming itself (call_later; no coroutine, no wall)",
    ("server.py", "_start_liveness_ticker", "_liveness_tick"): "arming the liveness ticker (call_later)",
    ("server.py", "_yield_once", "set_result"): "the per-pick yield future's resolver (call_soon)",
    ("agent_proxy.py", "agent_chat", "_recall"): "the proxy request's own recall under wait_for(_RECALL_DEADLINE_S): part of THIS request",
}
_INHERITS_WALL_MARKER = "# background: inherits-wall"


def _task_creations(path):
    """Every asyncio.create_task / ensure_future / loop.create_task / loop.call_soon / call_later
    site: (innermost enclosing def, callee name of the scheduled coroutine/callback, source line
    text, has-marker)."""
    import ast as _ast
    text = open(path, encoding="utf-8").read()
    tree = _ast.parse(text)
    lines = text.splitlines()
    out = []
    def callee_of(call):
        if not call.args:
            return ""
        a = call.args[0]
        if isinstance(a, _ast.Call):
            f = a.func
            return f.attr if isinstance(f, _ast.Attribute) else (f.id if isinstance(f, _ast.Name) else _ast.unparse(f))
        if isinstance(a, _ast.Attribute):
            return a.attr
        if isinstance(a, _ast.Name):
            return a.id
        return _ast.unparse(a)[:40]
    def visit(node, fn):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            fn = node.name  # the INNERMOST enclosing def names the site
        for child in _ast.iter_child_nodes(node):
            if isinstance(child, _ast.Call):
                f = child.func
                name = f.attr if isinstance(f, _ast.Attribute) else (f.id if isinstance(f, _ast.Name) else "")
                if name in ("create_task", "ensure_future", "call_soon", "call_later", "call_at"):
                    callee = callee_of(child) if name in ("create_task", "ensure_future") else (
                        callee_of(child) if name == "call_soon" else (_ast.unparse(child.args[1])[:40] if len(child.args) > 1 else ""))
                    line = lines[child.lineno - 1] if child.lineno - 1 < len(lines) else ""
                    out.append((fn, callee, line.strip()[:100], _INHERITS_WALL_MARKER in line))
            visit(child, fn)
    visit(tree, None)
    return out


def _census_files():
    files = []
    for d in ("src/mcp", "saas"):
        base = os.path.join(_REPO, d)
        if os.path.isdir(base):
            files += sorted(os.path.join(d, f) for f in os.listdir(base) if f.endswith(".py"))
    return files


def _bare_sites(path):
    fname = os.path.basename(path)
    return [(fn, callee, line) for fn, callee, line, marked in _task_creations(path)
            if not marked and (fname, fn, callee) not in _TASK_CREATION_ALLOWLIST]


def test_every_task_creation_in_mcp_and_saas_is_routed_or_a_documented_call_site():
    """All of src/mcp/ and saas/: every task/callback creation is `_background` (which the census
    does not see as create_task) or an allowlisted (file, def, callee) site or a marked line."""
    files = _census_files()
    assert any(f.endswith("bench_selftest.py") for f in files) and any(f.endswith("agent_proxy.py") for f in files)
    bad = {rel: _bare_sites(os.path.join(_REPO, rel)) for rel in files}
    bad = {k: v for k, v in bad.items() if v}
    assert bad == {}, f"bare task creation inside a possibly-walled request (route through server._background, allowlist the CALL SITE with a reason, or mark the line): {bad}"
    # the two agent_proxy writes and the bench runner are ROUTED (not allowlisted): a revert of any one goes red
    ap = open(os.path.join(_REPO, "saas", "agent_proxy.py"), encoding="utf-8").read()
    assert ap.count("_bg_task(_expand(") == 1 and ap.count("_bg_task2(_train(") == 1
    assert "_bg_task(_runner())" in open(os.path.join(_REPO, "saas", "bench_selftest.py"), encoding="utf-8").read()
    # and the Stripe webhook processing is ROUTED too (round 18 — the wall-inheritance residual
    # closed): routed through _background with the task reference held, and the bare form GONE.
    sr = open(os.path.join(_REPO, "saas", "stripe_routes.py"), encoding="utf-8").read()
    assert "from src.mcp.server import _background as _bg_task" in sr
    assert sr.count("= _bg_task(_async_process(") == 1
    assert "asyncio.create_task(_async_process(" not in sr


def test_task_census_is_per_call_site_not_per_function(tmp_path):
    """A def with one allowlisted site and one routed site: reverting the routed one must red."""
    f = tmp_path / "agent_proxy.py"
    NL = chr(10)
    f.write_text(NL.join(["import asyncio", "async def agent_chat():",
                          "    _rtask = asyncio.ensure_future(_recall(q=1))",
                          "    _bg = _background(_expand(topic=1))", ""]))
    assert _bare_sites(str(f)) == []                     # the allowlisted recall + the routed expand: clean
    f.write_text(NL.join(["import asyncio", "async def agent_chat():",
                          "    _rtask = asyncio.ensure_future(_recall(q=1))",
                          "    _et = asyncio.create_task(_expand(topic=1))", ""]))
    bad = _bare_sites(str(f))
    assert len(bad) == 1 and bad[0][1] == "_expand", bad   # the reverted site is red even though agent_chat has an allowlisted site
    f.write_text(NL.join(["import asyncio", "async def agent_chat():",
                          "    _t = asyncio.create_task(_fanout())  " + _INHERITS_WALL_MARKER + " the request's own fan-out", ""]))
    assert _bare_sites(str(f)) == []                     # an explicit per-line marker is the other way in


def test_provisioning_gate_wait_is_capped_by_the_remaining_turn_wall(monkeypatch):
    """The gate's `min(_wait_budget, _rem)`: with 1.2 s of wall left and a 90 s wait budget, a
    backend that honestly answers `provisioning` on every poll must yield the honest wait ("")
    at ~1.2 s — not "unreachable" (a fabricated transport verdict for a backend that answered)
    and not a 90 s poll. Mutant (cap removed): the polls outlive the wall, the next probe is
    `turn_wall_exhausted` → "unreachable" + a CRITICAL → red."""
    import time as _time
    S._provisioned_users.discard(_UID)
    be = _Backend({"/provisioning/status": [{"status": "provisioning"}]})
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setenv("MCP_PROVISIONING_POLL_SEC", "0.2")
    real_sleep = asyncio.sleep
    async def drive():
        tok = S._turn_wall_open(1.2)
        try:
            t0 = _time.monotonic()
            g = await asyncio.wait_for(S._ensure_provisioned(_UID), timeout=6.0)
            return g, _time.monotonic() - t0
        finally:
            S._turn_deadline.reset(tok)
    (g, el), log = _run(drive())
    assert g == "" and not isinstance(g, S._GateFailed), (g, log[-400:])
    assert 0.8 <= el <= 2.5, el
    assert "provisioning_probe_unavailable" not in log and "turn_wall_exhausted" not in log
    assert "Provisioning not ready after" in log and "budget" in log


def test_rest_door_opens_the_turn_wall_before_the_provisioning_gate(monkeypatch):
    """The gate runs at the REST door BEFORE the tool; the wall must already be open there (an ASGI
    middleware opens one per request), or a black-holed probe adds its full budget on top of the
    wall — measured live: 30 s on a 15 s wall."""
    import time as _time
    S._provisioned_users.discard(_UID)
    monkeypatch.setenv("SAAS_MODE", "")
    import httpx as _hx
    import src.mcp.http_server as H
    monkeypatch.setattr(H, "MCP_API_KEY", "")
    monkeypatch.setattr(S, "_http_client", _BlackHole())
    monkeypatch.setattr(S, "_TURN_WALL_S", 2.0)
    monkeypatch.setattr(S, "_queue_episodic_reappend", _AsyncTrue())
    async def rest():
        async with _hx.AsyncClient(transport=_hx.ASGITransport(app=H.app), base_url="http://t") as c:
            return await c.post("/remember_facts", json={"text": _TEXT, "user_id": _UID},
                                headers={"X-OpenWebUI-User-Id": _UID})
    t0 = _time.monotonic()
    r, log = _run(rest())
    wall = _time.monotonic() - t0
    assert wall < 2.0 + 1.5, (wall, log[-400:])
    assert r.status_code == 200 and r.json().get("isError") is True, r.text[:200]
    assert "provisioning_probe_unavailable" in log   # the gate's black-holed probe was bounded, not skipped
    assert any("_TurnWallMiddleware" in str(m) or "TurnWall" in str(m) for m in H.app.user_middleware)


# ── ROUND 17: gate probe latency, the leak mutant, recall's inherit-the-wall lanes honour it ──

class _LatentProvisioning(_Backend):
    """Answers `provisioning` after a fixed probe latency; records the wall time of every answer."""
    def __init__(self, latency_s):
        super().__init__({"/provisioning/status": [{"status": "provisioning"}]})
        self.latency_s = latency_s
        self.real_sleep = asyncio.sleep
        self.answered_at: list = []
    async def get(self, url, *a, **k):
        import time as _time
        await self.real_sleep(self.latency_s)
        self.answered_at.append(_time.monotonic())
        return await super().get(url, *a, **k)


@pytest.mark.parametrize("wall_s,poll_s,probe_s", [(1.2, 0.2, 0.03), (2.0, 0.6, 0.03), (2.0, 0.6, 0.25)])
def test_gate_never_lets_a_probe_answer_after_the_wall(monkeypatch, wall_s, poll_s, probe_s):
    """`elapsed + poll_interval + last_probe_latency >= budget` stops the loop BEFORE a sleep +
    probe that would outlive the wall: every probe answer lands inside the wall, the verdict is
    the honest wait (""), never a fabricated `unreachable`."""
    import time as _time
    S._provisioned_users.discard(_UID)
    be = _LatentProvisioning(probe_s)
    monkeypatch.setattr(S, "_http_client", be)
    monkeypatch.setenv("MCP_PROVISIONING_POLL_SEC", str(poll_s))
    S._ENV_PARSED.clear()
    async def drive():
        tok = S._turn_wall_open(wall_s)
        t0 = _time.monotonic()
        try:
            g = await asyncio.wait_for(S._ensure_provisioned(_UID), timeout=wall_s + 5)
        finally:
            S._turn_deadline.reset(tok)
        return g, t0
    (g, t0), log = _run(drive())
    assert g == "" and "provisioning_probe_unavailable" not in log and "turn_wall_exhausted" not in log, log[-500:]
    assert be.answered_at and all(t - t0 <= wall_s + 0.02 for t in be.answered_at), [round(t - t0, 3) for t in be.answered_at]
    assert len(be.answered_at) >= 2


def test_spawning_a_background_task_does_not_clear_the_callers_own_deadline():
    """The leak mutant: clearing the deadline in the CALLER's context (instead of inside the task)
    would strip the turn of its own wall. A turn that spawns a background task must still see
    its deadline afterwards, and the task must see none."""
    seen = {}
    async def bg():
        seen["task_rem"] = S._turn_wall_remaining()
    async def drive():
        tok = S._turn_wall_open(5.0)
        try:
            before = S._turn_wall_remaining()
            t = S._background(bg())
            await t
            after = S._turn_wall_remaining()
        finally:
            S._turn_deadline.reset(tok)
        return before, after
    (before, after), _ = _run(drive())
    assert before is not None and after is not None and 4.0 < after <= 5.0, (before, after)
    assert seen["task_rem"] is None


def test_recalls_inherit_the_wall_lanes_honour_it_through_the_bounded_client(monkeypatch):
    """The allowlisted per-request fan-out (harvest's /harvest-spans, the divert, the walk) runs
    INSIDE the wall and must be bounded by it: with the shared client wrapped in
    `TurnBoundedClient` and a black-holed backend, a recall on a 3 s wall returns within the wall
    and every backend call's timeout is capped by it (measured before: /harvest-spans 13 s on a
    3 s wall)."""
    import time as _time
    S._provisioned_users.add(_UID)
    class _ClassifyThenBlackHole(_BlackHole):
        """classify + gate answer at once (so the harvest and the walk are started); everything
        else black-holes honouring its timeout."""
        async def post(self, url, *a, **k):
            if "/classify-intent" in url:
                return _resp({"intent": "QUERY", "confidence": 1.0})
            return await self._hang(url, **k)
        async def get(self, url, *a, **k):
            if "/confidence-gate" in url:
                return _resp({"threshold": 0.7})
            return await self._hang(url, **k)
    raw = _ClassifyThenBlackHole()
    monkeypatch.setattr(S, "_http_client", S.TurnBoundedClient(raw))
    monkeypatch.setattr(S, "_TURN_WALL_S", 3.0)
    monkeypatch.setattr(S, "_send_progress", lambda *a, **k: None)
    t0 = _time.monotonic()
    out, log = _run(S.recall_memory_tool("my sister Dana moved to Halifax", _UID))
    wall = _time.monotonic() - t0
    assert wall < 3.0 + 2.5, (wall, log[-500:])
    assert isinstance(out, dict) and (out.get("isError") or "retrieval failure" in str(out.get("memory", "")))
    assert raw.calls and all((t or 10) <= 3.05 for _, t in raw.calls), raw.calls
    assert any("harvest" in ep for ep, _ in raw.calls), raw.calls   # the harvest lane ran AND was bounded
    # and the wrapper is what the doors install
    src = open(os.path.join(_REPO, "src", "mcp", "http_server.py"), encoding="utf-8").read()
    assert "_mcp.TurnBoundedClient(httpx.AsyncClient(" in src
    assert "TurnBoundedClient(httpx.AsyncClient(" in open(S.__file__, encoding="utf-8").read()
