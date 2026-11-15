"""DAILY BUDGET + LANE (dprompt-155 §2.3) — the paced-RPD core and the lane that defers.

What this file pins, link by link:

  lane_from_raw          — both lanes case-insensitively; garbage/empty/None → None (never
                           mapped onto a lane — a client header cannot widen anything).
  daily_cap_from_limits  — rpd arrives as DATA; FL_DAILY_BUDGET_REQ wins; unknown stays
                           unknown (None → no gate, never an invented cap).
  daily_window_s         — 86400 rolling default; FL_DAILY_WINDOW_S override.
  _llm_lane_from_header  — the REAL middleware function from src.api.main (this repo's
                           existing suites import main directly, so we do too), called with
                           synthetic request/call_next: valid header binds the lane for the
                           request, absent/invalid leaves it byte-for-byte untouched.
  try_take_daily/peek    — against a REAL Redis and the REAL Lua script (constraint 11: a
                           mock that short-circuits the Lua is not coverage): exhaustion,
                           persistence across a client "restart", ROLLING reset with an
                           explicit no-UTC-midnight kill, brain-scope isolation, fail-open
                           degradation.
  the SEAM               — apply_rate_limit_sync under the background lane defers
                           INSTANTLY (<2s, not the 30s LLM_RATE_MAX_WAIT_S wedge) once the
                           day is spent, while the SAME exhausted budget never refuses an
                           interactive caller.
  AST pins               — constraint 12: the probe `_daily_gate(..., spend=False)` must BE
                           the `if` test positioned BEFORE the minute-token acquire; the
                           `spend=True` take must follow a GRANTED acquire. Position is not
                           effect, so the probe pin is MUTATION-TESTED: disabling the probe
                           in-memory must break the instant-defer property.
  embedder lane header   — every httpx.post to /harvest-spans or /extract/rewrite carries
                            _BACKEND_LANE_HEADERS, proven on the source (all call sites) AND
                            behaviorally (_reextract_row_edges against a recording stub).
  metronome regression   — the owner-confirmed defect (2026-08-15): the header learner used
                            to flatten EVERY window (incl. 2400/day) into min(limit/window)
                            rps, and _rate_burst_capacity floored capacity to ~1 → a
                            1-per-40s metronome no multi-call turn could satisfy. Pinned
                            here: minute window drives rate+burst, day window publishes a
                            QUOTA (day cap) instead, bare day-scale resets publish quota
                            not pace, the floor SEEDS a restarted process (no re-metronome),
                            and _daily_gate applies env > learned > table. The flattening
                            mutant (all windows join the per-second min) is killed by
                            mutation test.

Redis-gated half runs against a scratch Redis (constraint 11):
    docker run --rm -d -p 55445:6379 --name fltest-redis-dailylane redis:7-alpine
    FLTEST_REDIS_URL=redis://127.0.0.1:55445/0 python3 tools/fltest.py --bug DPROMPT155 \\
        --test "tests/test_daily_budget_and_lane.py --color=no"

NOTE: redis_coord.purge_scope() does NOT know the llmdaily namespace — daily keys are
cleaned directly via RC.client().delete(RC.key(daily_budget.NS_DAILY, scope)).
"""

import ast
import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api import daily_budget as DB
from src.api import llm_calls as LC
from src.api import llm_lane as LANE
from src.api import redis_coord as RC

REPO = Path(__file__).resolve().parents[1]
REDIS_URL = os.environ.get("FLTEST_REDIS_URL", "")

needs_redis = pytest.mark.skipif(
    not REDIS_URL,
    reason="FLTEST_REDIS_URL not set — this suite exercises the real daily-take Lua seam",
)

_RAN = {"live": False}


@pytest.fixture
def redis_env(monkeypatch):
    """Scratch Redis wiring, mirroring tests/test_rate_bucket_redis_loop.py."""
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_RATE_BUDGET", "true")
    monkeypatch.setenv("REDIS_COORD_SOCKET_TIMEOUT", "2")
    for _e in ("FL_DAILY_BUDGET_REQ", "FL_DAILY_WINDOW_S", "FL_LLM_DEFAULT_LANE"):
        monkeypatch.delenv(_e, raising=False)
    RC.reset_client()
    if RC.client() is None:
        pytest.skip("scratch Redis not reachable")
    _RAN["live"] = True
    yield
    RC.reset_client()


def _brain_cfg(tag, key=None, base="https://openrouter.ai/v1", model="gpt-test",
               owner=None):
    return {
        "account_id": owner or f"acct-{tag}",
        "brain_scope": "account",
        "base_url": base,
        "model": model,
        "api_key": key or f"sk-key-{tag}",
    }


def _scope(tag="A", **kw):
    s = RC.scope_for_brain(_brain_cfg(tag, **kw))
    assert s is not None, "the test cfg must resolve a real brain scope"
    return s


def _daily_key(scope):
    return RC.key(DB.NS_DAILY, scope)


def _wipe_daily(scope):
    RC.client().delete(_daily_key(scope))
    return scope


# ══════════════════════════════════════════════════════════════════════════════
# A. UNGATED — pure functions, middleware contract, source pins
# ══════════════════════════════════════════════════════════════════════════════

def test_lane_from_raw_accepts_both_lanes_case_insensitively():
    assert LANE.lane_from_raw("background") == LANE.LANE_BACKGROUND
    assert LANE.lane_from_raw("BACKGROUND") == LANE.LANE_BACKGROUND
    assert LANE.lane_from_raw("  Background ") == LANE.LANE_BACKGROUND
    assert LANE.lane_from_raw("interactive") == LANE.LANE_INTERACTIVE
    assert LANE.lane_from_raw("INTERACTIVE") == LANE.LANE_INTERACTIVE
    assert LANE.lane_from_raw(" Interactive\t") == LANE.LANE_INTERACTIVE


def test_lane_from_raw_garbage_empty_none_maps_to_no_opinion():
    for bad in ("banana", "background-ish", "0", " ", "", None, 123):
        assert LANE.lane_from_raw(bad) is None, f"{bad!r} must not map onto a lane"


def test_daily_cap_from_limits_passthrough_override_and_unknown(monkeypatch):
    monkeypatch.delenv("FL_DAILY_BUDGET_REQ", raising=False)
    assert DB.daily_cap_from_limits({"rpd": 50}) == 50
    assert DB.daily_cap_from_limits({"rpd": "100"}) == 100
    monkeypatch.setenv("FL_DAILY_BUDGET_REQ", "7")
    assert DB.daily_cap_from_limits({"rpd": 50}) == 7, "env override must WIN over the table"
    assert DB.daily_cap_from_limits(None) == 7
    monkeypatch.delenv("FL_DAILY_BUDGET_REQ", raising=False)
    # unknown stays unknown — never an invented cap
    assert DB.daily_cap_from_limits({}) is None
    assert DB.daily_cap_from_limits(None) is None
    assert DB.daily_cap_from_limits({"rpd": None}) is None
    assert DB.daily_cap_from_limits({"rpm": 20}) is None
    # non-positive figures are not caps
    assert DB.daily_cap_from_limits({"rpd": 0}) is None
    assert DB.daily_cap_from_limits({"rpd": -5}) is None
    assert DB.daily_cap_from_limits({"rpd": "junk"}) is None


def test_daily_window_s_default_and_env_override(monkeypatch):
    monkeypatch.delenv("FL_DAILY_WINDOW_S", raising=False)
    assert DB.daily_window_s() == 86400, "the default window is one ROLLING day"
    monkeypatch.setenv("FL_DAILY_WINDOW_S", "3600")
    assert DB.daily_window_s() == 3600
    monkeypatch.setenv("FL_DAILY_WINDOW_S", "garbage")
    assert DB.daily_window_s() == 86400
    monkeypatch.setenv("FL_DAILY_WINDOW_S", "0")
    assert DB.daily_window_s() == 86400, "a non-positive window must not be a window"


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def _run_middleware(main_mod, header_value, outer_lane=None):
    """Call the REAL middleware with a synthetic request; return (lane seen by call_next, resp)."""
    headers = {} if header_value is None else {LANE.LANE_HEADER: header_value}
    req = _FakeRequest(headers)
    seen = []

    async def call_next(r):
        seen.append(LANE.current_lane())
        return "resp-sentinel"

    def _call():
        return main_mod._llm_lane_from_header(req, call_next)

    if outer_lane is not None:
        with LANE.use_lane(outer_lane):
            resp = asyncio.run(_call())
    else:
        resp = asyncio.run(_call())
    return seen[0], resp


def test_lane_header_middleware_contract_on_the_real_app(monkeypatch):
    """EFFECT half of the middleware pin: the real src.api.main middleware function binds
    a VALID header for the request and leaves the lane UNTOUCHED on absent/invalid."""
    main = pytest.importorskip("src.api.main")   # existing suites import main directly
    monkeypatch.delenv("FL_LLM_DEFAULT_LANE", raising=False)   # restored afterwards

    lane, resp = _run_middleware(main, "background")
    assert lane == LANE.LANE_BACKGROUND and resp == "resp-sentinel"
    lane, _ = _run_middleware(main, "Interactive")
    assert lane == LANE.LANE_INTERACTIVE, "header matching must be case-insensitive"

    # absent / garbage → the middleware must not touch the lane (here: process default)
    lane, _ = _run_middleware(main, None)
    assert lane == LANE.LANE_INTERACTIVE
    lane, _ = _run_middleware(main, "banana")
    assert lane == LANE.LANE_INTERACTIVE

    # untouched means REALLY untouched: an outer BACKGROUND lane survives a garbage header,
    # and a valid header still overrides it for this request's scope.
    lane, _ = _run_middleware(main, "banana", outer_lane=LANE.LANE_BACKGROUND)
    assert lane == LANE.LANE_BACKGROUND, "garbage header must not reset the outer lane"
    lane, _ = _run_middleware(main, "interactive", outer_lane=LANE.LANE_BACKGROUND)
    assert lane == LANE.LANE_INTERACTIVE


def test_lane_header_middleware_is_registered_on_the_app_by_ast():
    """POSITION half of the middleware pin: `_llm_lane_from_header` is decorated
    `@app.middleware("http")` in main.py and consults llm_lane (not a forked copy). Bound
    to the effect half by asserting the AST def line IS the line of the function object
    tested above."""
    main = pytest.importorskip("src.api.main")
    tree = ast.parse((REPO / "src" / "api" / "main.py").read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_llm_lane_from_header"),
              None)
    assert fn is not None, "_llm_lane_from_header must exist in src/api/main.py"
    decs = [ast.unparse(d) for d in fn.decorator_list]
    assert any("app.middleware" in d and "http" in d for d in decs), (
        f"middleware must be registered via @app.middleware(\"http\"), got {decs}")
    body_src = "\n".join(ast.unparse(n) for n in fn.body)
    for needle in ("LANE_HEADER", "lane_from_raw", "use_lane"):
        assert needle in body_src, f"middleware body must use llm_lane.{needle}"
    # co_firstlineno points at the FIRST DECORATOR line; the def sits len(decorators) below
    assert main._llm_lane_from_header.__code__.co_firstlineno == \
        fn.lineno - len(fn.decorator_list), (
        "the AST pin must describe the very function the contract test calls")


_PROBE_PATH = "/_fltest_lane_probe_dprompt155"


def test_middleware_binds_lane_through_real_asgi_traversal(monkeypatch):
    """The middleware EFFECT through Starlette's REAL task-spawn path — not a direct
    function call (a direct call would stay green through a starlette context-regression
    that drops the ContextVar copy when spawning the handler task). A probe route is
    added to the REAL app, driven via httpx.ASGITransport, and REMOVED afterwards so no
    route leaks into other suites. `to_thread` proves the binding also reaches worker
    threads (the re_embedder's sweeps run there)."""
    import httpx
    main = pytest.importorskip("src.api.main")   # the critic pass confirmed this imports
    monkeypatch.delenv("FL_LLM_DEFAULT_LANE", raising=False)
    app = main.app

    @app.get(_PROBE_PATH)
    async def _fltest_lane_probe():                    # noqa: DC — removed in teardown
        return {"lane": LANE.current_lane(),
                "to_thread": await asyncio.to_thread(LANE.current_lane)}

    async def _get(headers):
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(_PROBE_PATH, headers=headers)
            assert r.status_code == 200, r.text
            return r.json()

    try:
        # a VALID header binds the lane for the WHOLE request — handler task AND to_thread
        body = asyncio.run(_get({LANE.LANE_HEADER: "background"}))
        assert body["lane"] == LANE.LANE_BACKGROUND and \
            body["to_thread"] == LANE.LANE_BACKGROUND, (
            f"header background must bind through the real ASGI traversal, got {body}")
        body = asyncio.run(_get({LANE.LANE_HEADER: "Interactive"}))
        assert body["lane"] == LANE.LANE_INTERACTIVE and \
            body["to_thread"] == LANE.LANE_INTERACTIVE

        # absent / garbage → the middleware must not touch the lane (interactive default)
        for hdrs in ({}, {LANE.LANE_HEADER: "banana"}, {LANE.LANE_HEADER: ""}):
            body = asyncio.run(_get(hdrs))
            assert body["lane"] == LANE.LANE_INTERACTIVE and \
                body["to_thread"] == LANE.LANE_INTERACTIVE, (
                f"header {hdrs} must leave the lane untouched, got {body}")
    finally:
        app.router.routes[:] = [r for r in app.router.routes
                                if getattr(r, "path", None) != _PROBE_PATH]


# ─── AST pin: the daily-gate probe IS the if-test, BEFORE the acquire ─────────

def _gate_call(node, spend_value):
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                and sub.func.id == "_daily_gate"):
            for kw in sub.keywords:
                if (kw.arg == "spend" and isinstance(kw.value, ast.Constant)
                        and kw.value.value is spend_value):
                    return sub
    return None


def _fn_def(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def _probe_if(fn):
    """The If whose TEST (not body) carries the spend=False probe — grep-shaped pins
    stay green through `if False and _daily_gate(...)`, this does not."""
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and _gate_call(node.test, False) is not None:
            return node
    return None


def _acquire_call(fn):
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_get_rate_bucket"):
            return node
    return None


def _spend_return_if(fn):
    """The If whose BODY returns the spend=True take (the `if granted ...` guard)."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Return) and isinstance(sub.value, ast.Call)
                    and _gate_call(sub.value, True) is not None):
                if any(isinstance(t, ast.Name) and t.id == "granted"
                       for t in ast.walk(node.test)):
                    return node
    return None


@pytest.mark.parametrize("fn_name", ["apply_rate_limit_sync", "apply_rate_limit_async"])
def test_ast_the_daily_probe_is_the_if_test_before_the_acquire(fn_name):
    """POSITION pin (constraint 12): the instant-defer probe (`spend=False`) must BE the
    `if` test and must precede the minute-token acquire; the `spend=True` take must sit
    behind a GRANTED acquire. The EFFECT halves are the end-to-end seam test (instant
    defer, <2s) and the mutant-kill test below — a pin the mutant passes is not a pin."""
    tree = ast.parse((REPO / "src" / "api" / "llm_calls.py").read_text())
    fn = _fn_def(tree, fn_name)

    probe_if = _probe_if(fn)
    assert probe_if is not None, (
        f"{fn_name}: the spend=False probe must appear in an `if` TEST position")
    probe = _gate_call(probe_if.test, False)
    acquire = _acquire_call(fn)
    assert acquire is not None, f"{fn_name}: must acquire the minute-token bucket"
    assert probe.lineno < acquire.lineno, (
        f"{fn_name}: the daily probe (line {probe.lineno}) must run BEFORE the minute "
        f"token acquire (line {acquire.lineno}) — probe-after-acquire is the 30s wedge")
    # the probe must also be guarded by the lane: background only (a user is never refused)
    test_src = ast.unparse(probe_if.test)
    assert "is_background" in test_src, (
        f"{fn_name}: the probe if-test must be lane-gated (background only), got: {test_src}")

    spend_if = _spend_return_if(fn)
    assert spend_if is not None, (
        f"{fn_name}: the spend=True take must be returned from an `if granted` body")
    spend = _gate_call(spend_if, True)
    assert spend.lineno > acquire.lineno, (
        f"{fn_name}: the daily spend (line {spend.lineno}) must FOLLOW the acquire "
        f"(line {acquire.lineno}) — a minute-deferred call must not burn daily budget")


# ─── Embedder: the lane header on every LLM-bearing backend post ──────────────

def _llm_post_calls(tree):
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "post"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "httpx" and node.args):
            url_src = ast.unparse(node.args[0])
            if "/harvest-spans" in url_src or "/extract/rewrite" in url_src:
                out.append(node)
    return out


def test_embedder_source_every_llm_backend_post_carries_the_lane_header():
    """SOURCE pin: every httpx.post to /harvest-spans or /extract/rewrite in the embedder
    passes headers= referencing _BACKEND_LANE_HEADERS (or a dict with the lane header).
    The scan itself finds the sites — a NEW site without headers fails this test too."""
    tree = ast.parse((REPO / "src" / "re_embedder" / "embedder.py").read_text())
    posts = _llm_post_calls(tree)
    assert len(posts) >= 4, (
        f"expected the four LLM-bearing posts (2× /harvest-spans, 2× /extract/rewrite), "
        f"found {len(posts)}")
    for call in posts:
        headers_kw = next((kw for kw in call.keywords if kw.arg == "headers"), None)
        assert headers_kw is not None, (
            f"httpx.post({ast.unparse(call.args[0])}) at line {call.lineno} has NO "
            f"headers= — its extraction rides the API's INTERACTIVE lane and fires uncapped")
        src = ast.unparse(headers_kw.value)
        assert "_BACKEND_LANE_HEADERS" in src or LANE.LANE_HEADER in src, (
            f"httpx.post at line {call.lineno} headers must carry the lane header, got: {src}")


def test_embedder_effect_reextract_posts_ride_the_background_lane(monkeypatch):
    """EFFECT half of the embedder pin: the REAL _reextract_row_edges sends the lane
    header (value 'background') on BOTH of its LLM-bearing posts."""
    E = pytest.importorskip("src.re_embedder.embedder")
    assert E._BACKEND_LANE_HEADERS == {LANE.LANE_HEADER: LANE.LANE_BACKGROUND}

    recorded = []

    class _Resp:
        def __init__(self, payload):
            self._p = payload
        def raise_for_status(self):
            return None
        def json(self):
            return self._p

    def _fake_post(url, **kw):
        recorded.append((url, kw.get("headers")))
        if url.endswith("/harvest-spans"):
            # /harvest-spans success bodies carry NO status key (see _reextract_row_edges'
            # positive gate); the spine yielding nothing falls through to /extract/rewrite.
            return _Resp({"edges": [], "spans": 0})
        # The REAL /extract/rewrite success shape — status="success" is the only status
        # that endpoint returns on success, and the re-mine's positive gate requires it.
        return _Resp({"status": "success",
                      "edges": [{"subject": "a", "predicate": "p", "object": "b"}]})

    monkeypatch.setattr(E.httpx, "post", _fake_post)
    edges = E._reextract_row_edges("some text", "user-1", "http://backend:8000", "spine")
    urls = [u for u, _ in recorded]
    assert any(u.endswith("/harvest-spans") for u in urls)
    assert any(u.endswith("/extract/rewrite") for u in urls)
    for url, headers in recorded:
        assert headers and headers.get(LANE.LANE_HEADER) == LANE.LANE_BACKGROUND, (
            f"post to {url} must declare the background lane, got headers={headers!r}")
    assert len(edges) == 1, "the extraction path must have completed (not just been called)"


# ══════════════════════════════════════════════════════════════════════════════
# B. REDIS-GATED — the real Lua take, against a real Redis
# ══════════════════════════════════════════════════════════════════════════════

@needs_redis
def test_take_to_exhaustion_then_refusal_with_future_reset(redis_env):
    """cap N → N grants, then ok=False with reset_ms in the FUTURE and peek remaining 0.
    The key TTL must be >= 2× window (daily state can never expire mid-window)."""
    s = _wipe_daily(_scope("exhaust"))
    r1 = DB.try_take_daily(s, 2, 86400)
    assert r1 == {"ok": True, "used": 1, "remaining": 1}
    r2 = DB.try_take_daily(s, 2, 86400)
    assert r2 == {"ok": True, "used": 2, "remaining": 0}
    now_ms = int(time.time() * 1000)
    r3 = DB.try_take_daily(s, 2, 86400)
    assert r3["ok"] is False and r3["remaining"] == 0
    assert r3["reset_ms"] > now_ms, "the refusal must point at a FUTURE reset"
    assert r3["reset_ms"] <= now_ms + 86400 * 1000 + 2000, "reset is one window out"
    peek = DB.peek_daily(s)
    assert peek is not None and peek["remaining"] == 0 and peek["used"] == 2
    ttl = RC.client().ttl(_daily_key(s))
    assert ttl >= 2 * 86400 - 5, f"daily hash TTL {ttl}s must be >= 2× window"


@needs_redis
def test_daily_state_survives_a_client_restart(redis_env):
    """The budget lives in Redis, not memory: a brand-new client (a 'deploy') CONTINUES
    the used count — it must not reset the day to full."""
    s = _wipe_daily(_scope("restart"))
    assert DB.try_take_daily(s, 3, 86400)["used"] == 1
    assert DB.try_take_daily(s, 3, 86400)["used"] == 2
    RC.reset_client()                       # new client, same Redis — the "restart"
    assert RC.client() is not None
    r = DB.try_take_daily(s, 3, 86400)
    assert r == {"ok": True, "used": 3, "remaining": 0}, (
        "a restart must CONTINUE the count (used=3), not reset it (used=1)")


@needs_redis
def test_rolling_window_reset_with_no_utc_midnight_anywhere(redis_env):
    """ROLLING reset: window_start older than window_s → reset. And the no-midnight pin:
    a window_start ONE SECOND BEFORE the most recent UTC midnight is NOT expired under
    rolling semantics (only seconds-since-midnight have elapsed < 86400) — but a
    UTC-midnight implementation WOULD have reset it (the date changed). Asserting
    REFUSED there kills the midnight mutant at any hour the test runs."""
    s = _wipe_daily(_scope("rolling"))
    assert DB.try_take_daily(s, 1, 86400)["ok"] is True     # cap 1: the day is spent NOW

    now = time.time()
    midnight = int(now // 86400) * 86400            # epoch-aligned == UTC midnight
    k = _daily_key(s)
    RC.client().hset(k, "window_start_ms", (midnight - 1) * 1000)
    r = DB.try_take_daily(s, 1, 86400)
    assert r["ok"] is False, (
        "a window opened just before UTC midnight must NOT reset mid-window — rolling, "
        "not calendar-day semantics")
    assert DB.peek_daily(s)["used"] == 1

    now_ms = int(time.time() * 1000)
    RC.client().hset(k, "window_start_ms", now_ms - (86401 * 1000))   # 1s past the window
    r2 = DB.try_take_daily(s, 1, 86400)
    assert r2 == {"ok": True, "used": 1, "remaining": 0}, (
        "a window_start one second past window_s must roll: reset then grant")
    peek = DB.peek_daily(s)
    assert peek["window_start_ms"] > now_ms - 5000, "the rolled window must open at ~now"


@needs_redis
def test_scope_isolation_brain_a_exhausted_leaves_brain_b(redis_env):
    """THE ISOLATION LAW: the daily budget is per BRAIN (same provider host, different
    key digest). Brain A's spent day must not cost brain B a single request."""
    a = _wipe_daily(_scope("isoA", key="sk-1"))
    b = _wipe_daily(_scope("isoB", key="sk-2"))
    assert a != b, "different owner+key on the same host MUST be different digests"
    assert _daily_key(a) != _daily_key(b)

    assert DB.try_take_daily(a, 1, 86400)["ok"] is True        # A's whole day: 1 request
    assert DB.try_take_daily(a, 1, 86400)["ok"] is False       # A exhausted
    rb = DB.try_take_daily(b, 1, 86400)
    assert rb == {"ok": True, "used": 1, "remaining": 0}, (
        "brain B has its OWN day — A's exhaustion must not leak across the digest")
    assert DB.peek_daily(a)["remaining"] == 0
    assert DB.peek_daily(b)["remaining"] == 0   # B spent its own single request, not A's


@needs_redis
def test_degraded_redis_fails_open_and_never_raises(redis_env, monkeypatch):
    """Redis UNAVAILABLE (client() → None): the daily budget degrades FAIL-OPEN —
    ok=True degraded, peek None, never an exception. The code under test here is the
    degradation BRANCH, so stubbing the client (not Redis) is the honest seam. Also the
    ONE fail-closed case: a known cap of zero must refuse, never grant."""
    s = _scope("degraded")
    monkeypatch.setattr(RC, "client", lambda: None)
    r = DB.try_take_daily(s, 5, 86400)
    assert r.get("ok") is True and r.get("degraded") is True
    assert DB.peek_daily(s) is None
    # a non-Scope (e.g. scope_for_brain → None) is likewise degraded, never a raise
    r2 = DB.try_take_daily(None, 5, 86400)
    assert r2.get("ok") is True and r2.get("degraded") is True
    # the single fail-closed case: cap<=0 with everything healthy → defer, not grant
    monkeypatch.undo()
    r3 = DB.try_take_daily(s, 0, 86400)
    assert r3 == {"ok": False, "reason": "no_cap"}


class _FastBucket:
    """Minute-token stand-in: grants instantly. The seam under test is the DAILY gate;
    the test must never sleep on a real 5-RPM bucket."""
    def acquire_sync(self, *a, **k):
        return True
    async def acquire_async(self, *a, **k):
        return True


def _wire_seam(monkeypatch, scope, base_url="https://openrouter.ai/v1"):
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setattr(LC, "_resolve_rate_limit_key", lambda: f"brain:{scope.ident}")
    monkeypatch.setattr(LC, "_resolve_endpoint_and_model",
                        lambda: (base_url, "gpt-test"))
    monkeypatch.setattr(LC, "_get_rate_bucket", lambda *a, **k: _FastBucket())


@needs_redis
def test_end_to_end_seam_background_defers_instantly_interactive_never_refused(
        redis_env, monkeypatch):
    """THE LOAD-BEARING SEAM (dprompt-155): openrouter.ai table entry (rpd 50) with
    FL_DAILY_BUDGET_REQ=2, background lane. Two calls proceed; the third defers QUICKLY
    (elapsed < 2.0s — the no-30s-wedge instant-defer pin, which is the EFFECT half of
    the AST probe pin). The SAME exhausted budget under the interactive lane returns
    True — a user is never refused, and their call does not even SPEND the day."""
    s = _wipe_daily(_scope("seam"))
    _wire_seam(monkeypatch, s)
    monkeypatch.setenv("FL_DAILY_BUDGET_REQ", "2")

    with LANE.use_lane(LANE.LANE_BACKGROUND):
        assert LC.apply_rate_limit_sync("TEST") is True      # spend 1
        assert LC.apply_rate_limit_sync("TEST") is True      # spend 2 — the day is gone
        t0 = time.monotonic()
        third = LC.apply_rate_limit_sync("TEST")
        elapsed = time.monotonic() - t0
        assert third is False, "an exhausted day must DEFER a background call"
        assert elapsed < 2.0, (
            f"background defer took {elapsed:.2f}s — the probe must defer BEFORE the "
            f"minute-token wait (the 30s LLM_RATE_MAX_WAIT_S wedge is the defect)")
    assert DB.peek_daily(s)["remaining"] == 0

    with LANE.use_lane(LANE.LANE_INTERACTIVE):
        t1 = time.monotonic()
        user_call = LC.apply_rate_limit_sync("TEST")
        assert user_call is True, "the interactive lane is NEVER refused by the daily gate"
        assert time.monotonic() - t1 < 2.0
    peek = DB.peek_daily(s)
    # UPDATE (owner directive 2026-08-15, force=not background): an interactive call is
    # still NEVER refused, but it now COUNTS its spend past cap (used 2 → 3; remaining
    # clamps to 0) — the provider sees the traffic, so the meter must too. The pre-
    # directive "must not even SPEND" contract is superseded; the never-refused pin stands.
    assert peek["used"] == 3, (
        f"the interactive call must be COUNTED past cap, got used={peek['used']}")
    assert peek["remaining"] == 0


_SLOW_ACQUIRE_S = 2.2


class _SlowBucket:
    """A minute bucket that GRANTS, but slowly — simulates the wedged minute-token wait
    the probe exists to skip. Any bucket shape is irrelevant to the daily gate: only
    the DELAY matters."""
    def acquire_sync(self, *a, **k):
        time.sleep(_SLOW_ACQUIRE_S)
        return True


@needs_redis
def test_mutant_probe_disabled_breaks_the_instant_defer(redis_env, monkeypatch):
    """MUTATION TEST for the AST probe pin (constraint 12): rewrite apply_rate_limit_sync
    in-memory with the probe `if` test replaced by `False` (the exact `if False and
    guard(...)` shape grep-pins survive), re-exec it, and show the instant-defer property
    BREAKS — the mutant wedges on the slow acquire for _SLOW_ACQUIRE_S before deferring.
    A pin the mutant passes is not a pin. The real function under identical conditions
    defers instantly."""
    s = _wipe_daily(_scope("mutant"))
    _wire_seam(monkeypatch, s)
    monkeypatch.setenv("FL_DAILY_BUDGET_REQ", "2")
    # exhaust the day for real (probe must SEE remaining == 0)
    assert DB.try_take_daily(s, 2, 86400)["ok"] is True
    assert DB.try_take_daily(s, 2, 86400)["ok"] is True

    tree = ast.parse((REPO / "src" / "api" / "llm_calls.py").read_text())
    fn = _fn_def(tree, "apply_rate_limit_sync")
    probe_if = _probe_if(fn)
    assert probe_if is not None
    probe_if.test = ast.Constant(value=False)     # ← the mutation
    ast.fix_missing_locations(fn)

    real_fn = LC.apply_rate_limit_sync
    try:
        exec(compile(ast.unparse(fn), "<mutant-apply-rate-limit-sync>", "exec"),
             LC.__dict__)
        assert LC.apply_rate_limit_sync is not real_fn, "the mutant must be installed"

        monkeypatch.setattr(LC, "_get_rate_bucket", lambda *a, **k: _SlowBucket())
        with LANE.use_lane(LANE.LANE_BACKGROUND):
            t0 = time.monotonic()
            mutant_result = LC.apply_rate_limit_sync("TEST")
            mutant_elapsed = time.monotonic() - t0
        assert mutant_result is False
        assert mutant_elapsed > 2.0, (
            f"the probe-disabled MUTANT deferred in {mutant_elapsed:.2f}s — it PASSES the "
            f"<2s instant-defer bar, so the AST pin guards nothing")

        monkeypatch.setattr(LC, "_get_rate_bucket", lambda *a, **k: _FastBucket())
        with LANE.use_lane(LANE.LANE_BACKGROUND):
            t1 = time.monotonic()
            real_result = real_fn("TEST")
            real_elapsed = time.monotonic() - t1
        assert real_result is False and real_elapsed < 1.0, (
            "the REAL function must still defer instantly under identical conditions")
    finally:
        LC.apply_rate_limit_sync = real_fn     # restore — never leak the mutant


@needs_redis
def test_seam_exhausted_budget_rolls_when_window_expires(redis_env, monkeypatch):
    """CRITIC BLIND SPOT 1 (the probe/window deadlock): once remaining hits 0, the probe
    must defer ONLY while the stored window is still LIVE — after reset_ms the call falls
    through and the atomic take's Lua (the ONLY code that rolls the window) gets to run.
    Driven through the SEAM (apply_rate_limit_sync under use_lane(BACKGROUND)), never
    try_take_daily directly — the direct call was exactly how the pre-fix deadlock stayed
    green. Plus the AST discipline: the probe if-test in _daily_gate must compare BOTH
    used (against the CURRENT cap — an operator cap raise must apply immediately, critic
    pass 2) AND reset_ms; the reset_ms clause is mutation-tested below."""
    s = _wipe_daily(_scope("roll"))
    _wire_seam(monkeypatch, s)
    monkeypatch.setenv("FL_DAILY_BUDGET_REQ", "2")

    with LANE.use_lane(LANE.LANE_BACKGROUND):
        assert LC.apply_rate_limit_sync("TEST") is True      # spend 1
        assert LC.apply_rate_limit_sync("TEST") is True      # spend 2 — day spent
        # LIVE window: the probe defers (the reset is still in the future)
        assert LC.apply_rate_limit_sync("TEST") is False, \
            "an exhausted day inside a live window must still defer"

        # Back-date the stored window past its length (fixture setup — the point is that
        # the PROBE, not a direct take, must let the call through so the Lua can roll it)
        now_ms = int(time.time() * 1000)
        RC.client().hset(_daily_key(s), "window_start_ms", now_ms - (86401 * 1000))
        t0 = time.monotonic()
        rolled = LC.apply_rate_limit_sync("TEST")
        assert rolled is True, (
            "once the stored window is expired the probe must PROCEED — gating on a "
            "stale `remaining` alone deadlocks the budget at 0 forever")
        assert time.monotonic() - t0 < 2.0

    peek = DB.peek_daily(s)
    assert peek["used"] == 1, "the spend must have ROLLED the window (used reset to 1)"
    assert peek["remaining"] == 1
    assert peek["window_start_ms"] > now_ms - 5000, "the new window must open at ~now"
    assert peek["reset_ms"] > now_ms

    # ── AST pin: the probe if-test in _daily_gate compares BOTH used and reset_ms ──
    tree = ast.parse((REPO / "src" / "api" / "llm_calls.py").read_text())
    gate_fn = _fn_def(tree, "_daily_gate")
    probe_if = next((n for n in ast.walk(gate_fn)
                     if isinstance(n, ast.If) and "used" in ast.unparse(n.test)),
                    None)
    assert probe_if is not None, "the spend=False probe if must exist inside _daily_gate"
    test_src = ast.unparse(probe_if.test)
    compares = [ast.unparse(c) for c in ast.walk(probe_if.test)
                if isinstance(c, ast.Compare)]
    assert any("used" in c for c in compares), (
        f"the probe test must contain a comparison on used — the CURRENT cap, not the "
        f"stored remaining (a cap raise must apply immediately), got: {test_src}")
    assert any("reset_ms" in c for c in compares), (
        f"the probe test must ALSO compare against reset_ms — without it the window can "
        f"never roll (the pre-fix deadlock), got: {test_src}")

    # ── MUTATION KILL: drop the reset_ms clause → the roll-through must BREAK ──
    m = _wipe_daily(_scope("rollmutant"))
    _wire_seam(monkeypatch, m)
    with LANE.use_lane(LANE.LANE_BACKGROUND):
        assert LC.apply_rate_limit_sync("TEST") is True
        assert LC.apply_rate_limit_sync("TEST") is True      # exhausted, live window
        now_ms2 = int(time.time() * 1000)
        RC.client().hset(_daily_key(m), "window_start_ms", now_ms2 - (86401 * 1000))

        used_compare = next(c for c in ast.walk(probe_if.test)
                            if isinstance(c, ast.Compare) and "used" in ast.unparse(c))
        probe_if.test = used_compare              # ← the mutation: reset_ms clause dropped
        ast.fix_missing_locations(gate_fn)
        real_gate = LC._daily_gate
        try:
            exec(compile(ast.unparse(gate_fn), "<mutant-daily-gate>", "exec"), LC.__dict__)
            assert LC._daily_gate is not real_gate, "the mutant must be installed"
            mutant_call = LC.apply_rate_limit_sync("TEST")
            assert mutant_call is False, (
                "the reset_ms-dropped MUTANT must still defer on the expired window — "
                "if it proceeds, this mutant-kill pins nothing (mutant not distinguishable)")
        finally:
            LC._daily_gate = real_gate             # restore — never leak the mutant
        real_call = LC.apply_rate_limit_sync("TEST")
        assert real_call is True, (
            "the REAL gate must proceed on the same expired window the mutant wedged on")


@needs_redis
def test_probe_honors_cap_raise_immediately(redis_env, monkeypatch):
    """CRITIC PASS 2 defect: the probe must compare used against the CALLER'S CURRENT cap,
    not a figure stored in (or derived from) the hash. FL_DAILY_BUDGET_REQ 2 → exhaust →
    defer; raise the env to 200 → the very next background call PROCEEDS (no window wait)
    and the atomic take re-persists cap=200 into the hash. Under the old remaining-based
    probe this stayed deferred until reset — an operator cap raise ignored for a day."""
    s = _wipe_daily(_scope("capraise"))
    _wire_seam(monkeypatch, s)
    monkeypatch.setenv("FL_DAILY_BUDGET_REQ", "2")

    with LANE.use_lane(LANE.LANE_BACKGROUND):
        assert LC.apply_rate_limit_sync("TEST") is True      # spend 1
        assert LC.apply_rate_limit_sync("TEST") is True      # spend 2 — day spent
        assert LC.apply_rate_limit_sync("TEST") is False, "live window, spent cap → defer"

        monkeypatch.setenv("FL_DAILY_BUDGET_REQ", "200")     # ← the operator raises the cap
        t0 = time.monotonic()
        raised = LC.apply_rate_limit_sync("TEST")
        assert raised is True, (
            "a cap RAISE must apply on the very next call — not at the next window reset")
        assert time.monotonic() - t0 < 2.0

    peek = DB.peek_daily(s)
    assert peek["cap"] == 200, f"the take must re-persist the raised cap, got {peek}"
    assert peek["used"] == 3, "the proceeding call must have SPENT against the raised cap"
    assert peek["remaining"] == 197
    assert peek["window_start_ms"] > 0


def test_deferred_llm_call_returns_distinguishable_rate_deferred_envelope(monkeypatch):
    """CRITIC PASS 2 defect: a rate-limiter DEFER must be distinguishable from an empty
    answer. Both retry twins return EXACTLY {"error": "rate_deferred"} when the pacer
    defers and raise_on_unavailable=False; with True they raise LLMUnavailable(reason
    'rate_deferred') — the pre-existing opt-in contract, unchanged. The pacer defer is
    forced directly (monkeypatched apply_rate_limit_*) with the breaker held closed so
    the branch under test is reached deterministically."""
    monkeypatch.setattr(LC, "apply_rate_limit_sync", lambda operation="": False)

    async def _defer_async(operation=""):
        return False
    monkeypatch.setattr(LC, "apply_rate_limit_async", _defer_async)
    monkeypatch.setattr(LC, "_breaker_is_open", lambda breaker: False)
    # tests/conftest.py sets SAAS_MODE=1, under which BOTH the SaaS no-brain fail-closed
    # gate (early {} before the pacer) and _get_endpoint_list() (empty → no endpoints)
    # fire without a bound tenant brain. The seam under test is the defer ENVELOPE, not
    # SaaS brain resolution — hold both open; the defer branch is reached before any HTTP.
    monkeypatch.setattr(LC, "_saas_brain_unavailable", lambda: False)
    monkeypatch.setattr(LC, "_get_endpoint_list",
                        lambda: ["http://fltest.invalid/v1/chat/completions"])

    msgs = [{"role": "user", "content": "hello"}]
    out_sync = LC.call_llm_with_retry_sync(
        msgs, "test-model", user_id="fltest-defer", max_retries=1,
        operation="TEST", raise_on_unavailable=False)
    assert out_sync == {"error": "rate_deferred"}, (
        f"the sync defer envelope must be EXACTLY the distinguishable marker, got {out_sync!r}")
    out_async = asyncio.run(LC.call_llm_with_retry_async(
        msgs, "test-model", user_id="fltest-defer", max_retries=1,
        operation="TEST", raise_on_unavailable=False))
    assert out_async == {"error": "rate_deferred"}, (
        f"the async twin must return the SAME marker, got {out_async!r}")

    with pytest.raises(LANE.LLMUnavailable) as ei_sync:
        LC.call_llm_with_retry_sync(
            msgs, "test-model", user_id="fltest-defer", max_retries=1,
            operation="TEST", raise_on_unavailable=True)
    assert ei_sync.value.reason == "rate_deferred"

    async def _expect_raise():
        with pytest.raises(LANE.LLMUnavailable) as ei_async:
            await LC.call_llm_with_retry_async(
                msgs, "test-model", user_id="fltest-defer", max_retries=1,
                operation="TEST", raise_on_unavailable=True)
        return ei_async.value.reason
    assert asyncio.run(_expect_raise()) == "rate_deferred"


def test_embedder_doc_lane_maps_rate_deferred_to_brain_unavailable(monkeypatch):
    """CRITIC PASS 2 defect: a `rate_deferred` degradation envelope is a brain MERELY
    PACED, not a chunk failure — the real doc-lane branch must raise
    DocumentBrainUnavailable (→ the document re-pends, at-least-once) instead of
    DocumentChunkExtractionFailed (→ terminal 'partial'). EFFECT-level: the REAL
    _document_chunk_edges driven with a stub httpx, spine empty so the /extract/rewrite
    envelope is what decides. A non-paced reason is kept as the control arm."""
    E = pytest.importorskip("src.re_embedder.embedder")
    assert E.DOC_CHUNK_FAILURE_LOUD is True, \
        "the loud-failure branch is the lane under test — enable it for the test env"

    class _Resp:
        def __init__(self, payload):
            self._p = payload
        def raise_for_status(self):
            return None
        def json(self):
            return self._p

    _SPINE_EMPTY = {"status": "ok", "edges": []}

    def _post_with(envelope):
        def _post(url, **kw):
            if url.endswith("/harvest-spans"):
                return _Resp(dict(_SPINE_EMPTY))
            return _Resp(envelope)
        return _post

    paced = {"status": "degraded", "extraction_degraded": True,
             "error": "rate_deferred", "chunks_failed": 1, "chunks_total": 1}
    monkeypatch.setattr(E.httpx, "post", _post_with(paced))
    with pytest.raises(E.DocumentBrainUnavailable) as ei:
        E._document_chunk_edges(
            "Photosynthesis converts carbon dioxide and water into glucose and oxygen.",
            "user-1", "http://backend:8000", "rewrite")
    assert not isinstance(ei.value, E.DocumentChunkExtractionFailed), (
        "a paced day must NEVER terminally fail a user's document upload")
    assert "rate_deferred" in str(ei.value)

    # control arm: a NON-paced degraded reason stays a single-CHUNK failure (partial
    # edges ride on the exception) — proves the branch discriminates, not just redirects
    other = {"status": "degraded", "extraction_degraded": True,
             "error": "model_timeout", "chunks_failed": 1, "chunks_total": 2,
             "edges": [{"subject": "a", "predicate": "p", "object": "b"}]}
    monkeypatch.setattr(E.httpx, "post", _post_with(other))
    with pytest.raises(E.DocumentChunkExtractionFailed) as ei2:
        E._document_chunk_edges(
            "Photosynthesis converts carbon dioxide and water into glucose and oxygen.",
            "user-1", "http://backend:8000", "rewrite")
    assert not isinstance(ei2.value, E.DocumentBrainUnavailable)
    assert getattr(ei2.value, "partial_edges", None), \
        "a partial degradation must KEEP its successfully-extracted edges"


class _RecCursor:
    """Recording cursor for driving the REAL drain re-pend handler: every executed
    SQL string (+ params) lands in the shared sink. fetchall answers the pending-doc
    SELECT; fetchone answers the claim UPDATE's RETURNING (chunks, source_ref, attempts)."""

    def __init__(self, sink):
        self._sink = sink
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split())
        self._sink.append(self._last)

    def fetchall(self):
        return [("doc-paced-1",)]

    def fetchone(self):
        # The drain first PROBES whether this schema carries `brain_deferred_since`
        # (migration 264) to decide which bound governs: wall clock where the column
        # exists, legacy attempt count where it does not. This fake answered EVERY
        # fetchone with the claim tuple below, so it answered the probe truthily too and
        # silently promoted itself to a migrated schema -- where an attempt count is NOT
        # a terminator and the control arm below could never fire. Answer the probe as a
        # PRE-264 schema, which is the contract this test was written to pin.
        # Both rungs are covered explicitly in tests/test_document_brain_defer_clock.py.
        if "information_schema.columns" in self._last:
            return None
        return (["one chunk of text"], "src-ref", 5)   # attempts=5 == _DOC_MAX_ATTEMPTS


class _RecConn:
    def __init__(self):
        self.sqls = []

    def cursor(self):
        return _RecCursor(self.sqls)

    def commit(self):
        return None

    def rollback(self):
        return None


def _drain_one_paced_doc(E, monkeypatch, reason):
    """Run ONE document through the REAL drain_pending_documents re-pend handler with
    the claim already at attempts=5 (== _DOC_MAX_ATTEMPTS) and the chunk processor
    raising DocumentBrainUnavailable(reason). Returns (captured SQL list, finalized)."""
    conn = _RecConn()

    def _raise(*a, **kw):
        raise E.DocumentBrainUnavailable(reason)
    # chunk extraction classification is pinned by the doc-lane test above; here the code
    # under test is the RE-PEND handler, so the module-level chunk seam raises directly.
    monkeypatch.setattr(E, "_process_document_chunk", _raise)
    monkeypatch.setattr(E, "DOC_CHUNK_QUEUE", False)          # legacy serial lane, 1 chunk
    monkeypatch.setattr(E, "_backend_is_reachable", lambda url: True)
    finalized = E.drain_pending_documents(conn, "http://backend:8000", "user-1",
                                          schema_name=None, statement_route="rewrite")
    return conn.sqls, finalized


def test_paced_repend_does_not_burn_the_attempt_budget(monkeypatch):
    """CRITIC PASS 3: a rate_deferred re-pend must NOT consume the redelivery budget —
    at MAX attempts a PACED defer still re-pends (status='pending') and GIVES THE CLAIM
    ATTEMPT BACK (attempts = GREATEST(attempts - 1, 0)), never terminating; the CONTROL
    arm (a non-paced DocumentBrainUnavailable, e.g. breaker open) at the same max
    attempts DOES terminate with the 'error' UPDATE and gives nothing back. Driven
    through the REAL handler inside drain_pending_documents via a recording conn."""
    E = pytest.importorskip("src.re_embedder.embedder")
    assert E.DOC_CHUNK_FAILURE_LOUD is True, "the loud-failure lane is the one in force"

    # ── PACED arm: rate_deferred at attempts == max ──
    sqls, finalized = _drain_one_paced_doc(
        E, monkeypatch, "rate_deferred: daily budget exhausted")
    giveback = [s for s in sqls if "GREATEST(attempts - 1, 0)" in s]
    assert len(giveback) == 1, (
        f"the paced re-pend must give the burned claim-attempt back, executed SQL: {sqls}")
    assert "status = 'pending'" in giveback[0] and "started_at = NULL" in giveback[0], (
        f"the paced re-pend must return the document to the queue, got: {giveback[0]}")
    assert not any("status = 'error'" in s for s in sqls), (
        f"an at-max PACED defer must NEVER terminate, executed SQL: {sqls}")
    assert not any("status = 'ready'" in s or "status = 'partial'" in s for s in sqls)
    assert finalized == 0, "a paced re-pend finalizes nothing (_exhausted forced False)"

    # ── CONTROL arm: non-paced brain outage at the same max attempts ──
    sqls2, finalized2 = _drain_one_paced_doc(
        E, monkeypatch, "circuit_breaker_open: breaker open after failures")
    terminal = [s for s in sqls2 if "status = 'error'" in s]
    assert len(terminal) == 1, (
        f"a NON-paced outage at max attempts must terminate with the error UPDATE, "
        f"executed SQL: {sqls2}")
    assert not any("GREATEST(attempts - 1, 0)" in s for s in sqls2), (
        "the attempt give-back is for PACED defers only — real failures still count")
    assert finalized2 == 1, "the terminal path counts as finalized"


class _ReexCursor:
    """Recording cursor for the episodic reextract loop: captures executed SQL, answers
    the eligibility SELECT with two fair-game rows (intent None), and reports rowcount=1
    (an old row) so a poison-stamp attempt, if made, is visible in the SQL sink."""

    def __init__(self, sink):
        self._sink = sink
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink.append(" ".join(sql.split()))

    def fetchall(self):
        return [
            (9001, "an old user turn", "src-1", None, "2026-01-01", None),
            (9002, "a second user turn", "src-2", None, "2026-01-02", None),
        ]


class _ReexConn:
    def __init__(self):
        self.sqls = []

    def cursor(self):
        return _ReexCursor(self.sqls)

    def commit(self):
        return None

    def rollback(self):
        return None


def test_paced_reextract_does_not_stamp_poison_rows(monkeypatch):
    """CRITIC PASS 4: a rate_deferred pace never reached the provider, so the reextract
    row loop must NOT stamp reextracted_at (the poison-row guard) and must NOT count it
    toward the consecutive-failure storm bound. Effect-driven through the REAL
    reextract_episodic loop with the module-level _reextract_row_edges seam raising, a
    recording conn, and the storm bound armed at 1 (REEXTRACT_BACKLOG_DRAIN + FAIL_ABORT=1)
    so the count difference is OBSERVABLE: the paced arm processes BOTH rows (no abort),
    the control arm aborts after the first. Plus the reason carry-through: the REAL
    _reextract_row_edges raises a RuntimeError whose str CONTAINS rate_deferred when
    /extract/rewrite answers {"status":"degraded","error":"rate_deferred"}."""
    E = pytest.importorskip("src.re_embedder.embedder")

    # ── reason carry-through: the real extractor preserves the pacer's envelope ──
    class _Resp:
        def __init__(self, payload):
            self._p = payload
        def raise_for_status(self):
            return None
        def json(self):
            return self._p

    paced_env = {"status": "degraded", "extraction_degraded": True,
                 "error": "rate_deferred", "edges": []}
    monkeypatch.setattr(
        E.httpx, "post",
        lambda url, **kw: _Resp({"status": "ok", "edges": []}
                                if url.endswith("/harvest-spans") else paced_env))
    with pytest.raises(RuntimeError) as ei:
        E._reextract_row_edges("a retained turn", "user-1",
                               "http://backend:8000", "spine")
    assert "rate_deferred" in str(ei.value), (
        f"the degraded envelope's reason must ride the raise (it feeds the loop's "
        f"_paced classification), got: {str(ei.value)!r}")

    # ── the row-loop handler: paced vs control, storm bound armed at 1 ──
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)
    monkeypatch.setattr(E, "_REEXTRACT_FAIL_ABORT", 1)
    crits = []
    monkeypatch.setattr(E, "_doc_log_crit",
                        lambda event, **fields: crits.append(event))

    def _run(raise_reason):
        calls = {"n": 0}
        conn = _ReexConn()

        def _raise(*a, **kw):
            calls["n"] += 1
            raise RuntimeError(raise_reason)
        mp = pytest.MonkeyPatch()
        mp.setattr(E, "_reextract_row_edges", _raise)
        try:
            processed = E.reextract_episodic(conn, "http://backend:8000", "user-1",
                                             schema_name=None, batch_size=5,
                                             statement_route="rewrite")
        finally:
            mp.undo()
        return conn.sqls, calls["n"], processed

    # PACED arm: neither row is stamped, the failure is not counted (no storm abort —
    # BOTH rows are attempted), nothing is finalized
    sqls, calls, processed = _run("extraction degraded (brain unavailable) — rate_deferred")
    assert calls == 2, "a paced row must not trip the storm bound — both rows attempted"
    assert not any(s.startswith("UPDATE") and "reextracted_at" in s for s in sqls), (
        f"a paced row must NOT be stamped (poison guard skipped) — the only "
        f"reextracted_at SQL may be the eligibility scan, executed: {sqls}")
    assert processed == 0
    assert "re_embedder.reextract_batch_aborted" not in crits, (
        "a paced defer must not count toward the consecutive-failure abort")

    # CONTROL arm: a real PER-ROW failure DOES stamp the poison guard (>30d WHERE
    # clause), counts (abort after row 1 — the second row is never attempted), and fires
    # the loud batch-abort crit.
    #
    # The control must NOT be a reason riding the "extraction degraded (brain unavailable)"
    # envelope. Everything on that envelope — circuit_breaker_open, tenant_egress_blocked,
    # all_retries_exhausted:*, the bare "unknown" default — describes the LANE being down,
    # which is never this row's fault and must never burn its one best-effort pass. This
    # control previously used circuit_breaker_open on that envelope, which made "a lane-wide
    # outage stamps rows" the asserted contract. A genuine poison row fails on its OWN
    # content, and surfaces as an ordinary exception.
    sqls2, calls2, processed2 = _run("ValueError: could not parse edge JSON for this row")
    poison = [s for s in sqls2
              if "reextracted_at" in s and "INTERVAL '30 days'" in s]
    assert len(poison) == 1, (
        f"a NON-paced failure must run the poison stamp with the >30d WHERE clause, "
        f"executed SQL: {sqls2}")
    assert calls2 == 1, "the counted failure must trip the storm bound and abort"
    assert "re_embedder.reextract_batch_aborted" in crits, (
        "the abort must be a VISIBLE critical, not a silent short count")
    assert processed2 == 0


def test_mcp_defer_worker_lane_crosses_http(monkeypatch):
    """CRITIC PASS 5 (EFFECT level): the MCP statement-defer worker's LLM work executes in
    the API process — only the lane header carries BACKGROUND across the socket. The REAL
    `_statement_rewrite_ingest_deferred` and `_ingest_statement_via_spine` driven with a
    recording `_http_client` stub (the tests/mcp patch seam): deferred=True → the lane
    header rides /extract/rewrite, /harvest-spans AND /ingest posts (the /ingest header
    matters because /ingest's own LLM-fallback self-call forwards the inbound lane);
    deferred=False (inline callers — a user is waiting) → NO lane header on any post."""
    S = pytest.importorskip("src.mcp.server")

    _EDGE = {"subject": "user", "rel_type": "owns", "object": "corolla"}
    recorded = []

    class _Resp:
        def __init__(self, payload):
            self._p = payload
        def raise_for_status(self):
            return None
        def json(self):
            return self._p

    async def _post(url, **kw):
        headers = kw.get("headers")
        recorded.append((url.split("/")[-1], headers))
        if "/extract/rewrite" in url:
            return _Resp({"edges": [dict(_EDGE)]})
        if "/harvest-spans" in url:
            return _Resp({"edges": [dict(_EDGE)]})
        if "/ingest" in url:
            return _Resp({"status": "valid", "committed": 1, "staged": 0})
        return _Resp({})

    class _Client:
        async def post(self, url, **kw):
            return await _post(url, **kw)

    monkeypatch.setattr(S, "_http_client", _Client())

    def _lane_on(hdrs):
        return bool(hdrs) and hdrs.get(LANE.LANE_HEADER) == LANE.LANE_BACKGROUND

    # ── deferred=True: the background lane rides EVERY LLM-bearing post ──
    recorded.clear()
    out = asyncio.run(S._statement_rewrite_ingest_deferred(
        "I drive a Corolla to work", "user-1", deferred=True))
    assert isinstance(out, dict) and out.get("committed") == 1
    posts = dict(recorded)          # {path: headers} — rewrite then ingest
    assert set(posts) == {"rewrite", "ingest"}, f"unexpected posts: {recorded}"
    for path, hdrs in posts.items():
        assert _lane_on(hdrs), (
            f"deferred=True must send the background lane header on /{path}, got {hdrs!r}")

    recorded.clear()
    out2 = asyncio.run(S._ingest_statement_via_spine(
        "I drive a Corolla to work", "user-1", deferred=True))
    assert isinstance(out2, dict) and out2.get("committed") == 1
    posts2 = dict(recorded)         # harvest-spans then ingest
    assert set(posts2) == {"harvest-spans", "ingest"}, f"unexpected posts: {recorded}"
    for path, hdrs in posts2.items():
        assert _lane_on(hdrs), (
            f"deferred=True must send the background lane header on /{path}, got {hdrs!r}")

    # ── CONTROL: deferred=False (inline callers) — NO lane header anywhere ──
    recorded.clear()
    out3 = asyncio.run(S._statement_rewrite_ingest_deferred(
        "I drive a Corolla to work", "user-1", deferred=False))
    assert isinstance(out3, dict) and out3.get("committed") == 1
    recorded.clear()
    out4 = asyncio.run(S._ingest_statement_via_spine(
        "I drive a Corolla to work", "user-1", deferred=False))
    assert isinstance(out4, dict) and out4.get("committed") == 1
    assert recorded, "the control arms must actually have posted"
    for path, hdrs in recorded:
        assert not (hdrs or {}).get(LANE.LANE_HEADER), (
            f"deferred=False (a user is waiting) must stay INTERACTIVE — no lane header "
            f"on /{path}, got {hdrs!r}")


# ══════════════════════════════════════════════════════════════════════════════
# C. REDIS-GATED — the metronome regression (owner-confirmed defect 2026-08-15)
# ══════════════════════════════════════════════════════════════════════════════

# A host OUTSIDE the published-limits table: the effective clamp must come from
# LLM_MAX_RPM alone (8 rps) so the learned target is the header arithmetic, not a
# table clamp. (A table host with rpm 5 would clamp effective to ~0.067 < 0.075 and
# the rate assertions below would pin the wrong thing.)
_METRO_BASE = "https://api.cerebras-fltest.invalid/v1"

# Cerebras' SUFFIXED fixed-window shape (no reset header — the suffix IS the window):
# a Free-Trial minute bucket (5/min) inside an hour (150/hr) and a day (2400/day) cap.
_CEREBRAS_HEADERS = {
    "x-ratelimit-limit-requests-minute": "5",
    "x-ratelimit-remaining-requests-minute": "4",
    "x-ratelimit-limit-requests-hour": "150",
    "x-ratelimit-remaining-requests-hour": "120",
    "x-ratelimit-limit-requests-day": "2400",
    "x-ratelimit-remaining-requests-day": "2000",
}


def _resp(headers, status=200):
    return SimpleNamespace(status_code=status, headers=headers)


def _wire_learner(monkeypatch, scope, base=_METRO_BASE):
    """Wire the REAL learner to a brain-scoped key: _observe_rate_headers resolves the
    key + endpoint through these seams; the bucket itself is the REAL _get_rate_bucket
    path (shared-budget scope attached), never a stub."""
    key = f"brain:{scope.ident}"
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("LLM_RATE_HEADER_LEARNING", "1")
    monkeypatch.setenv("LLM_MAX_RPM", "480")            # effective clamp = 8 rps
    for _e in ("LLM_RATE_BURST", "FL_RATE_LEARN_PACE_WINDOW_MAX_S"):
        monkeypatch.delenv(_e, raising=False)
    monkeypatch.setattr(LC, "_resolve_rate_limit_key", lambda: key)
    monkeypatch.setattr(LC, "_resolve_endpoint_and_model",
                        lambda: (base, "gpt-test"))
    return key


def _learn_cerebras(monkeypatch, scope):
    """Create the real bucket for a FRESH scope, then drive the REAL learner once with
    the Cerebras-shaped windowed headers. Returns (key, bucket)."""
    key = _wire_learner(monkeypatch, scope)
    bucket = LC._get_rate_bucket(key, 8.0, endpoint_tag=_METRO_BASE)
    LC._observe_rate_headers(_resp(_CEREBRAS_HEADERS), "TEST")
    return key, bucket


def _fresh_metro_scope(tag):
    """A metronome-test scope with BOTH Redis sides wiped: the shared llmrate bucket
    hash (tokens/floor/pause — a leftover partially-refilled bucket from an earlier
    run against the same scratch redis would make the acquire counts nondeterministic)
    and the daily budget hash (purge_scope does not know the llmdaily namespace)."""
    s = _scope(tag, base=_METRO_BASE)
    RC.purge_scope(s)
    return _wipe_daily(s)


@needs_redis
def test_metronome_regression_minute_window_drives_rate_and_burst(redis_env,
                                                                  monkeypatch):
    """THE REGRESSION PIN: with {minute:5/4, hour:150/120, day:2400/2000} the learner
    must pace off the MINUTE window only — rate = 5/min × headroom(0.9) = 0.075 rps,
    burst capacity = the window's request allowance = 5 — while the day window is
    published as a QUOTA to the daily controller, never divided into the rate. Then
    the unit-of-work property the metronome killed: a multi-call turn draws its
    permits INSTANTLY (4 immediate acquires all granted; the 5th takes the last token
    of the allowance; the 6th is refused)."""
    s = _fresh_metro_scope("metro1")
    key, bucket = _learn_cerebras(monkeypatch, s)

    # minute window drives the INSTANTANEOUS pace — not the day's 0.025 flattening
    assert bucket.rate == pytest.approx(0.075), (
        f"rate must be minute-driven 5rpm×0.9 = 0.075 rps, got {bucket.rate} "
        f"(the day-flattened metronome is 0.025)")
    # the minute window's request allowance IS the burst capacity
    assert bucket.capacity == 5.0, (
        f"capacity must be the learned allowance 5, got {bucket.capacity} "
        f"(the metronome floored it to 1)")
    assert bucket.floor_capacity == 5, "the allowance must persist as the floor burst"
    assert bucket.learner_floor_rps == pytest.approx(0.075)

    # the day window publishes a QUOTA at the SAME coord scope the key resolves to
    dscope = LC._coord_scope_for_rate_key(key)
    assert dscope is not None and dscope.ident == s.ident, (
        "the rate key and the learned day cap must land on the SAME brain scope")
    assert DB.learned_day_cap(dscope) == {"day_cap": 2400, "day_window_s": 86400}, (
        f"day window (2400/day) must be quota-published, got "
        f"{DB.learned_day_cap(dscope)}")

    # UNIT OF WORK: a 4-call conversational turn proceeds as a unit — all immediate.
    # (deferrable=True so a refusal is a refusal, not the interactive fail-open.)
    unit = [bucket.acquire_sync(max_wait=0.001, deferrable=True) for _ in range(4)]
    assert all(unit is True for unit in unit), (
        f"4 immediate acquires must ALL be granted (capacity {bucket.capacity}), "
        f"got {unit}")
    assert bucket.acquire_sync(max_wait=0.001, deferrable=True) is True, (
        "the 5th acquire takes the last token of the 5-allowance")
    t0 = time.monotonic()
    assert bucket.acquire_sync(max_wait=0.001, deferrable=True) is False, (
        "the 6th immediate acquire must be refused — the window allowance is spent "
        "(the bucket refills at 0.075 rps, ~13s to the next token, not instant)")
    assert time.monotonic() - t0 < 2.0, "the refusal must be immediate, not a wedge"


def _find_pace_guard(root):
    """(stmt_list, index, node) of the `if w_secs <= pace_window_max:` guard inside
    `_observe_rate_headers`'s windows loop — the exact line the fix added."""
    for _, val in ast.iter_fields(root):
        if isinstance(val, list):
            for i, item in enumerate(val):
                if (isinstance(item, ast.If)
                        and ast.unparse(item.test) == "w_secs <= pace_window_max"):
                    return val, i, item
                if isinstance(item, ast.stmt):
                    got = _find_pace_guard(item)
                    if got:
                        return got
        elif isinstance(val, ast.AST):
            got = _find_pace_guard(val)
            if got:
                return got
    return None


@needs_redis
def test_metronome_mutant_day_window_flattening_is_killed(redis_env, monkeypatch):
    """MUTATION KILL for the pace-window split (AST discipline, same as the probe pin):
    rewrite `_observe_rate_headers` in-memory back to the OLD windows loop — the
    `w_secs <= pace_window_max` guard REMOVED (so ALL windows, incl. 2400/day, join
    per_min_caps) and the learned_allowance capture disabled (the old code had no
    burst allowance, so capacity fell back to ~1s of tokens) — re-exec, drive the SAME
    headers, and show the metronome RESURFACES: rate ≈ 0.025 (the day flattening), so
    rate != 0.075, and capacity 1 means 4 immediate acquires CANNOT all succeed. A pin
    the mutant passes is not a pin. Restore in finally."""
    found = _find_pace_guard(_fn_def(
        ast.parse((REPO / "src" / "api" / "llm_calls.py").read_text()),
        "_observe_rate_headers"))
    assert found is not None, (
        "the pace-window guard `if w_secs <= pace_window_max:` must exist in "
        "_observe_rate_headers — the AST mutant targets exactly it")
    guard_list, guard_idx, guard = found

    s = _fresh_metro_scope("metro2")
    key = _wire_learner(monkeypatch, s)

    tree = ast.parse((REPO / "src" / "api" / "llm_calls.py").read_text())
    fn = _fn_def(tree, "_observe_rate_headers")
    lst, idx, guard_node = _find_pace_guard(fn)
    # mutation 2 of 2: the allowance capture goes first so it survives the promotion
    allow_if = next(n for n in guard_node.body
                    if isinstance(n, ast.If)
                    and "learned_allowance" in ast.unparse(n.test))
    allow_if.test = ast.Constant(value=False)          # never capture an allowance
    # mutation 1 of 2: remove the guard — ALL windows join per_min_caps (the defect);
    # the day-quota elif rides away with the guard's orelse, as in the old loop.
    lst[idx:idx + 1] = guard_node.body
    ast.fix_missing_locations(fn)

    real_fn = LC._observe_rate_headers
    try:
        exec(compile(ast.unparse(fn), "<mutant-observe-rate-headers>", "exec"),
             LC.__dict__)
        assert LC._observe_rate_headers is not real_fn, "the mutant must be installed"

        mutant = LC._get_rate_bucket(key, 8.0, endpoint_tag=_METRO_BASE)
        LC._observe_rate_headers(_resp(_CEREBRAS_HEADERS), "TEST")

        # the day flattening resurfaces: min(5/min, 150/hr, 2400/day)×0.9 = 0.025 rps
        assert mutant.rate == pytest.approx(0.025), (
            f"the mutant must flatten the day into the rate (0.025 rps), got "
            f"{mutant.rate} — if this is 0.075 the mutant failed to restore the defect")
        assert abs(mutant.rate - 0.075) > 0.01, (
            "the mutant's rate must be distinguishable from the fixed 0.075 — this "
            "assertion is the kill")
        # no allowance captured → capacity falls back to ~1s of tokens = 1
        assert mutant.floor_capacity == 0.0
        assert mutant.capacity == 1.0, (
            f"the old capacity default must floor the burst to 1, got "
            f"{mutant.capacity}")
        # and the day-quota publish dies with the guard (the old loop had no quota arm)
        assert DB.learned_day_cap(s) is None, (
            "the old loop published no day cap — quota and pace died together")

        # BURST kill: with capacity 1 and rate 0.025, 4 immediate acquires are
        # impossible — acquire 1 takes the only token, acquire 2 needs ~40s.
        burst = [mutant.acquire_sync(max_wait=0.001, deferrable=True)
                 for _ in range(4)]
        assert burst[0] is True, "the first (and only) token must still be granted"
        assert not all(b is True for b in burst), (
            f"the mutant must FAIL the 4-immediate-acquires unit-of-work assertion, "
            f"got {burst} — the metronome is back")
    finally:
        LC._observe_rate_headers = real_fn      # restore — never leak the mutant

    # the REAL learner under identical conditions shows the fixed shape
    real_bucket = LC._get_rate_bucket(key, 8.0, endpoint_tag=_METRO_BASE)
    LC._observe_rate_headers(_resp(_CEREBRAS_HEADERS), "TEST")
    assert real_bucket.rate == pytest.approx(0.075) and real_bucket.capacity == 5.0


@needs_redis
def test_bare_family_day_scale_reset_publishes_quota_not_pace(redis_env,
                                                              monkeypatch):
    """The BARE family (x-ratelimit-limit/remaining/reset — the family matcher reads
    exactly these unsuffixed keys) with a DAY-scale reset (86000s ≥ 43200): quota
    semantics. The daily controller learns cap=50, and NO per-second rate may be
    derived from the day (the bucket's pace is unchanged from its creation value —
    in particular not remaining/reset ≈ 10/86000 rps)."""
    s = _fresh_metro_scope("bare1")
    key = _wire_learner(monkeypatch, s)
    bucket = LC._get_rate_bucket(key, 6.0, endpoint_tag=_METRO_BASE)
    rate_before, cap_before = bucket.rate, bucket.capacity

    LC._observe_rate_headers(_resp({
        "x-ratelimit-limit": "50",
        "x-ratelimit-remaining": "10",
        "x-ratelimit-reset": "86000",
    }), "TEST")

    # no pace, no floor, no burst learned off a day-scale bare reset
    assert bucket.rate == pytest.approx(rate_before), (
        f"the rate must be UNCHANGED ({rate_before}), got {bucket.rate} — a day-scale "
        f"reset must never divide into the per-second rate")
    assert bucket.rate != pytest.approx(10 / 86000, rel=0.05), (
        "the bucket must not be paced at remaining/reset (10/86000 rps)")
    assert bucket.capacity == cap_before
    assert bucket.learner_floor_rps == 0.0 and bucket.floor_capacity == 0.0
    # the allowance IS learned — as the daily quota
    assert DB.learned_day_cap(s) == {"day_cap": 50, "day_window_s": 86400}, (
        f"the bare day window's limit (50) must publish as the day cap, got "
        f"{DB.learned_day_cap(s)}")


@needs_redis
def test_floor_capacity_survives_cross_process_restart(redis_env, monkeypatch):
    """The no-re-metronome-on-restart seam: after one process learns rate 0.075 +
    burst 5 and publishes the floor (with floor_cap) to the shared hash, a RESTARTED
    process (buckets cleared + fresh Redis client) creating a bucket for the SAME key
    SEEDS rate/capacity/floor from the shared state — it does not re-metronome to
    capacity 1 while waiting for its own first response."""
    s = _fresh_metro_scope("restart1")
    key, bucket = _learn_cerebras(monkeypatch, s)
    assert bucket.rate == pytest.approx(0.075) and bucket.capacity == 5.0

    shared = RC.brain_floor_state(s)
    assert shared is not None, "the learned floor must be in the shared Redis hash"
    assert shared["floor_rps"] == pytest.approx(0.075)
    assert shared.get("floor_cap") == 5.0, (
        f"floor_cap must ride the shared floor (cross-process burst), got {shared}")

    # the "restart": in-process buckets gone AND a brand-new Redis client
    LC.reset_rate_limiter()
    RC.reset_client()
    assert RC.client() is not None, "the restarted client must reconnect to scratch"

    fresh = LC._get_rate_bucket(key, 8.0, endpoint_tag=_METRO_BASE)
    assert fresh is not bucket, "the restarted process must have a NEW bucket"
    assert fresh.rate == pytest.approx(0.075), (
        f"a fresh bucket must SEED the shared rate 0.075, got {fresh.rate} "
        f"(unseeded it starts at the 8 rps clamp and re-metronomes)")
    assert fresh.capacity == 5.0, (
        f"a fresh bucket must SEED the shared burst 5, got {fresh.capacity} "
        f"(capacity 1 here IS the metronome re-appearing on restart)")
    assert fresh.floor_capacity == 5.0
    assert fresh.learner_floor_rps == pytest.approx(0.075)
    # and the seeded bucket serves the unit of work immediately, without any response
    unit = [fresh.acquire_sync(max_wait=0.001, deferrable=True) for _ in range(4)]
    assert all(b is True for b in unit), (
        f"the seeded bucket must grant a 4-call turn out of the box, got {unit}")


@needs_redis
def test_daily_gate_precedence_env_over_learned_over_table(redis_env, monkeypatch):
    """The daily-gate cap precedence, all three ranks against a real Redis budget:
    FL_DAILY_BUDGET_REQ (operator) > learned day cap (provider's own day header) >
    table rpd. Driven through the seam (LC._daily_gate) with the openrouter table
    entry (rpd 50) as the table arm's floor."""
    monkeypatch.delenv("FL_DAILY_BUDGET_REQ", raising=False)
    _OR = "https://openrouter.ai/v1"

    # ── env > learned: learned cap says 2, operator says 3 → the gate follows 3 ──
    s1 = _wipe_daily(_scope("precenv"))
    assert DB.publish_day_cap(s1, 2, 86400) is True
    k1 = f"brain:{s1.ident}"
    monkeypatch.setenv("FL_DAILY_BUDGET_REQ", "3")
    with LANE.use_lane(LANE.LANE_BACKGROUND):
        assert LC._daily_gate(k1, _OR, spend=True) is True        # used 1
        assert LC._daily_gate(k1, _OR, spend=True) is True        # used 2 — the
        # LEARNED cap is spent here; the env cap must keep the gate open
        assert LC._daily_gate(k1, _OR, spend=False) is True, (
            "env(3) must OUTRANK the learned cap(2) — the probe may not defer at "
            "used=2 while the operator's cap is 3")
        assert LC._daily_gate(k1, _OR, spend=True) is True        # used 3 — env spent
        assert LC._daily_gate(k1, _OR, spend=False) is False, (
            "the gate must defer at the ENV cap (3), not the learned one (2)")
    monkeypatch.delenv("FL_DAILY_BUDGET_REQ", raising=False)

    # ── learned > table: learned 2 vs the openrouter table rpd 50 ──
    s2 = _wipe_daily(_scope("preclearned"))
    assert DB.publish_day_cap(s2, 2, 86400) is True
    k2 = f"brain:{s2.ident}"
    with LANE.use_lane(LANE.LANE_BACKGROUND):
        assert LC._daily_gate(k2, _OR, spend=True) is True
        assert LC._daily_gate(k2, _OR, spend=True) is True
        assert LC._daily_gate(k2, _OR, spend=False) is False, (
            "the LEARNED cap(2) must OUTRANK the table rpd(50) — deferring at used=2 "
            "is the measurement beating the table")

    # ── table: neither env nor learned → the openrouter rpd (50) gates ──
    s3 = _wipe_daily(_scope("prectable"))
    k3 = f"brain:{s3.ident}"
    with LANE.use_lane(LANE.LANE_BACKGROUND):
        assert LC._daily_gate(k3, _OR, spend=False) is True
        spent = [LC._daily_gate(k3, _OR, spend=True) for _ in range(50)]
        assert all(spent) and len(spent) == 50, (
            "the table rpd must fund exactly 50 background requests")
        assert LC._daily_gate(k3, _OR, spend=False) is False, (
            "with neither env nor learned cap the TABLE rpd(50) must gate the day")


@needs_redis
def test_untagged_or_mismatched_shared_floor_does_not_seed(redis_env, monkeypatch):
    """SEED GATING (critic 2026-08-15 + confirmation pass): `_get_rate_bucket`'s
    seed-from-shared-floor block must adopt a published floor ONLY when its floor_src
    tag MATCHES the endpoint_tag being called. An UNTAGGED floor (old-build write, or a
    mid-publish read between the Lua floor write and the floor_src hset) must NOT seed
    — with learner_source="" the stale-floor clear can never fire and the clamp becomes
    immortal; re-tagging with the CURRENT endpoint is the same blast radius. Three arms,
    each on a FRESH scope (both Redis sides wiped — no cross-test refill flakes) with a
    FRESH in-process bucket (limiter reset between arms)."""
    _FLOOR = {"floor_rps": "0.075", "floor_rank": "2",
              "floor_prov": "measured", "floor_cap": "5.0"}

    def _plant(scope, floor_src):
        h = dict(_FLOOR)
        if floor_src is not None:
            h["floor_src"] = floor_src
        RC.client().hset(RC.key("llmrate", scope, "bucket"), mapping=h)

    def _fresh_bucket(key, tag):
        LC.reset_rate_limiter()                       # fresh in-process bucket per arm
        return LC._get_rate_bucket(key, 8.0, endpoint_tag=tag)   # keyword — explicit

    # ── arm 1: UNTAGGED floor (no floor_src field at all) → NO seed ──
    s1 = _fresh_metro_scope("seed1")
    k1 = f"brain:{s1.ident}"
    _plant(s1, floor_src=None)
    b1 = _fresh_bucket(k1, _METRO_BASE)
    assert b1.learner_floor_rps == 0.0, (
        "an UNTAGGED floor must NOT seed — learner_source stays empty and the "
        "stale-floor clear can never fire (an immortal clamp)")
    assert b1.rate == 8.0 and b1.learner_source == ""

    # ── arm 2: floor_src MATCHES the endpoint_tag → seeds rate + capacity ──
    s2 = _fresh_metro_scope("seed2")
    k2 = f"brain:{s2.ident}"
    _plant(s2, floor_src=_METRO_BASE)
    b2 = _fresh_bucket(k2, _METRO_BASE)
    assert b2.learner_floor_rps == pytest.approx(0.075)
    assert b2.rate == pytest.approx(0.075), "the seed clamps to the shared floor"
    assert b2.learner_source == _METRO_BASE, "the seed must adopt the floor's OWN tag"
    assert b2.capacity == pytest.approx(5.0), "the floor's burst allowance seeds too"

    # ── arm 3: floor_src is a DIFFERENT host → NO seed (a brain switch must not adopt
    # the previous provider's tier) ──
    s3 = _fresh_metro_scope("seed3")
    k3 = f"brain:{s3.ident}"
    _plant(s3, floor_src="https://api.other-provider.invalid/v1")
    b3 = _fresh_bucket(k3, _METRO_BASE)
    assert b3.learner_floor_rps == 0.0, (
        "a floor learned on a DIFFERENT endpoint must NOT seed this bucket")
    assert b3.rate == 8.0 and b3.learner_source == ""


@needs_redis
def test_interactive_counts_past_cap_and_never_defers(redis_env, monkeypatch):
    """INTERACTIVE DAILY-BUDGET HOLE (owner directive 2026-08-15): the gate's spend path
    passes force=not is_background() — an INTERACTIVE spend COUNTS even past cap (used
    may exceed cap; the provider still sees the traffic, so the meter must too) and NEVER
    defers; background keeps force=False (a refused take defers). Driven at the SEAM
    (LC._daily_gate under use_lane) against a fresh scope. EFFECT-level throughout:
    returned dicts + peek, no AST pins."""
    s = _fresh_metro_scope("force1")
    key = f"brain:{s.ident}"
    monkeypatch.setenv("FL_DAILY_BUDGET_REQ", "3")

    # ── background arm: 3 spends ok, then the probe defers, used == cap ──
    with LANE.use_lane(LANE.LANE_BACKGROUND):
        assert LC._daily_gate(key, _METRO_BASE, spend=True) is True   # used 1
        assert LC._daily_gate(key, _METRO_BASE, spend=True) is True   # used 2
        assert LC._daily_gate(key, _METRO_BASE, spend=True) is True   # used 3
        assert LC._daily_gate(key, _METRO_BASE, spend=False) is False, \
            "a spent day must still defer the background lane"
    peek = DB.peek_daily(s)
    assert peek["used"] == 3 and peek["remaining"] == 0

    # ── interactive arm: 2 more spends BOTH proceed; used exceeds cap; remaining
    # clamps to 0 (the Lua's math.max(cap - used, 0) — never negative) ──
    with LANE.use_lane(LANE.LANE_INTERACTIVE):
        assert LC._daily_gate(key, _METRO_BASE, spend=True) is True, \
            "an interactive spend is NEVER refused by the daily gate"
        assert LC._daily_gate(key, _METRO_BASE, spend=True) is True
    peek2 = DB.peek_daily(s)
    assert peek2["used"] == 5, (
        f"interactive spends must COUNT past the cap (real traffic the provider sees), "
        f"got used={peek2['used']}")
    assert peek2["remaining"] == 0, (
        f"remaining must clamp to 0 past cap, never negative — got {peek2['remaining']}")

    # ── background re-probe after the over-cap interactive spends → still defers:
    # the probe sees REAL usage (used 5 >= cap 3 inside a live window) ──
    with LANE.use_lane(LANE.LANE_BACKGROUND):
        assert LC._daily_gate(key, _METRO_BASE, spend=False) is False, (
            "the background lane must see the interactive over-spend — the meter is real")
        assert LC._daily_gate(key, _METRO_BASE, spend=True) is False, (
            "a background TAKE past cap must be refused (force=False on this lane)")


@needs_redis
def test_zzz_the_live_lane_actually_ran():
    """Charter §5 discipline: a green run that exercised nothing is a FALSE GREEN."""
    assert _RAN["live"], "no live-Redis test executed — this file proved nothing"


def test_a_frozen_backend_does_not_burn_every_rows_single_pass(monkeypatch):
    """`backend ingest disabled (knowledge-store mode)` is raised with the comment
    "nothing stored -> do NOT stamp", and the poison guard was stamping it anyway. A frozen
    backend answers that for EVERY row, so one freeze terminally skipped every >30d row in
    the seat -- the silent loss the guard's own block comment exists to prevent, reached by
    a lane the envelope check does not cover."""
    E = pytest.importorskip("src.re_embedder.embedder")
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)
    monkeypatch.setattr(E, "_REEXTRACT_FAIL_ABORT", 1)
    monkeypatch.setattr(E, "_doc_log_crit", lambda event, **f: None)

    conn = _ReexConn()

    def _raise(*a, **kw):
        raise RuntimeError("backend ingest disabled (knowledge-store mode)")

    mp = pytest.MonkeyPatch()
    mp.setattr(E, "_reextract_row_edges", _raise)
    try:
        E.reextract_episodic(conn, "http://backend:8000", "user-1",
                             schema_name=None, batch_size=5, statement_route="rewrite")
    finally:
        mp.undo()

    poison = [s for s in conn.sqls
              if "reextracted_at" in s and "INTERVAL '30 days'" in s]
    assert poison == [], (
        f"a frozen backend is lane-wide and must NOT stamp the poison guard, executed: {poison}")


def test_a_degraded_lane_envelope_alone_exempts_the_poison_guard(monkeypatch):
    """THE HEADLINE EXEMPTION, WHICH WAS ENTIRELY UNTESTED. Removing _degraded_lane from
    _exempt left every other test in this file green, so the main arm of the poison-fuse
    fix had no coverage at all -- the same shape as the weakness it replaced. A reason that
    is NEITHER rate_deferred NOR backend-down still rides the degraded envelope, and the
    envelope is what earns the exemption: if the LLM lane did not answer, the row is not at
    fault, whatever the reason word."""
    E = pytest.importorskip("src.re_embedder.embedder")
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)
    monkeypatch.setattr(E, "_REEXTRACT_FAIL_ABORT", 1)
    monkeypatch.setattr(E, "_doc_log_crit", lambda event, **f: None)

    conn = _ReexConn()

    def _raise(*a, **kw):
        # circuit_breaker_open: not paced, not backend-down. Only the envelope exempts it.
        raise RuntimeError("extraction degraded (brain unavailable) - circuit_breaker_open")

    mp = pytest.MonkeyPatch()
    mp.setattr(E, "_reextract_row_edges", _raise)
    try:
        E.reextract_episodic(conn, "http://backend:8000", "user-1",
                             schema_name=None, batch_size=5, statement_route="rewrite")
    finally:
        mp.undo()

    poison = [s for s in conn.sqls
              if "reextracted_at" in s and "INTERVAL '30 days'" in s]
    assert poison == [], (
        f"the degraded envelope alone must exempt the poison guard, executed: {poison}")
