"""first-touch-cold-path round 2, gap E — `/classify-intent` must not freeze the event loop.

THE AMPLIFIER (measured): `main.py::classify_intent` is `async def` and used to call
`model.classify_text(...)` (torch CPU inference) and `_spacy_cue_route(text)` (a spaCy parse, plus
a 2.7s lazy model load on the first request after boot) SYNCHRONOUSLY. While those ran, every
other endpoint — including the no-DB `GET /internal/ingest-route` — was frozen behind them, which
is exactly how all three of the MCP's first calls timed out on pre-prod. The fix moves both under
`await asyncio.to_thread(...)` (torch and spaCy release the GIL inside their C/Cython cores).

Two pins, because the critic proved that reverting both hunks left 124/124 green:
  1. an AST pin on the SOURCE — `classify_intent` awaits `asyncio.to_thread` for exactly those two
     callables (regardless of how the surrounding code is worded);
  2. a DRIVEN pin on the BEHAVIOUR — through the real ASGI app with a fake model that blocks for
     ``_SLOW_S`` seconds, a concurrent trivial request (the pure-async 404 control and the sync-def
     `/internal/ingest-route`) must answer well inside that window. Revert the hunks and the
     controls take ≥ ``_SLOW_S``.

Run through tools/fltest.py, never raw pytest.
"""
import ast
import asyncio
import os
import sys
import time

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MAIN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "src", "api", "main.py")
_SLOW_S = 1.5          # how long the fake inference blocks its thread
_CONTROL_BUDGET_S = 0.5  # a free loop answers a trivial request in milliseconds; this is generous


# ── 1. SOURCE PIN ───────────────────────────────────────────────────────────────────────

_CPU_BOUND_GLOBALS = {"_spacy_cue_route", "_detect_feeling_states",
                      "_is_source_goal_transition_statement"}
_LINGUISTICS_MODULE = "src.extraction.linguistics"


def _threaded_scopes_and_targets(fn):
    """Every `await asyncio.to_thread(<target>, ...)` in fn: the set of TARGET names/attrs and the
    set of scope nodes (nested defs / lambdas) that are themselves to_thread targets."""
    nested_defs = {n.name: n for n in ast.walk(fn)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn}
    targets, scopes = set(), set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        f = call.func
        if not (isinstance(f, ast.Attribute) and f.attr == "to_thread"
                and isinstance(f.value, ast.Name) and f.value.id == "asyncio" and call.args):
            continue
        tgt = call.args[0]
        if isinstance(tgt, ast.Lambda):
            scopes.add(tgt)
        elif isinstance(tgt, ast.Name):
            targets.add(tgt.id)
            if tgt.id in nested_defs:
                scopes.add(nested_defs[tgt.id])
        elif isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name):
            targets.add(f"{tgt.value.id}.{tgt.attr}")
    return targets, scopes


def _spacy_bound_aliases(fn) -> set[str]:
    """Names bound by `from src.extraction.linguistics import X as Y` inside fn (alias-proof)."""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module == _LINGUISTICS_MODULE:
            for al in node.names:
                out.add(al.asname or al.name)
    return out


def _offending_bare_calls(fn) -> list[tuple[int, str]]:
    """Calls of a CPU-bound callee that are NOT inside a scope handed to asyncio.to_thread."""
    _, scopes = _threaded_scopes_and_targets(fn)
    bound = _CPU_BOUND_GLOBALS | _spacy_bound_aliases(fn)
    offences = []
    def visit(node, inside_threaded):
        if node in scopes:
            inside_threaded = True
        if isinstance(node, ast.Call) and not inside_threaded:
            f = node.func
            name = None
            if isinstance(f, ast.Name):
                name = f.id
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                name = f"{f.value.id}.{f.attr}"
            if name in bound or name == "model.classify_text":
                offences.append((node.lineno, name))
        for child in ast.iter_child_nodes(node):
            visit(child, inside_threaded)
    visit(fn, False)
    return offences


def test_classify_intent_source_runs_every_cpu_bound_call_off_the_loop():
    """ALIAS-PROOF source pin: inside `classify_intent`, every call of (a) anything imported from
    src.extraction.linguistics (spaCy-bound — the first call also loads the model + its torch
    import chain), (b) `_spacy_cue_route` / `_detect_feeling_states`, (c) `model.classify_text`
    must sit inside a scope handed to `asyncio.to_thread` (a nested def or lambda that IS the
    to_thread target) or be the to_thread target itself. A bare call anywhere in the handler
    is the event-loop freeze coming back under a new spelling."""
    tree = ast.parse(open(_MAIN_PATH).read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "classify_intent")
    targets, _ = _threaded_scopes_and_targets(fn)
    assert "model.classify_text" in targets, targets
    assert "_spacy_cue_route" in targets, targets
    assert not _offending_bare_calls(fn), _offending_bare_calls(fn)


# ── 2. DRIVEN PIN ───────────────────────────────────────────────────────────────────────

class _SlowModel:
    """A GLiNER2 stand-in whose inference BLOCKS its thread (time.sleep, not asyncio.sleep —
    exactly what torch does to the caller). Returns the STATEMENT label the handler maps."""
    def __init__(self, label: str):
        self.label = label
        self.calls = 0

    def classify_text(self, text, schema, include_confidence=True):
        self.calls += 1
        time.sleep(_SLOW_S)
        return {"intent": {"label": self.label, "confidence": 0.99}}


@pytest.fixture
def app_and_model(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)   # no DB reads inside the handler
    import src.api.main as main
    # Tenant gate: the seat is "ready" without a DB.
    async def _ready(*_a, **_k):
        return None
    monkeypatch.setattr(main, "_ensure_tenant_ready", _ready)
    # spaCy cue route: BLOCK its thread too, so the driven pin covers both offloads.
    def _slow_cue(_text, *_a, **_k):
        time.sleep(0.4)
        return None, None
    monkeypatch.setattr(main, "_spacy_cue_route", _slow_cue)
    monkeypatch.setattr(main, "QUERY_INTERROGATIVE_PREROUTE", False)
    monkeypatch.setattr(main, "CORRECTION_BYPASS_GATE", True)
    # The deterministic-first router (INTENT_DETERMINISTIC_FIRST, default ON) legitimately
    # OWNS the driven sentence ("My sister Dana …" — speaker-anchored, cue-free, non-negated)
    # and returns before GLiNER2. THIS pin is about the GLiNER2 off-loop discipline, so it
    # runs with the router OFF; the router's own off-loop discipline is pinned below.
    monkeypatch.setattr(main, "INTENT_DETERMINISTIC_FIRST", False)
    # STEADY STATE, like the container: `_warm_linguistics` loads the spaCy layer (and its torch
    # import chain) at BOOT, off the request path. Loading it lazily inside the first request
    # holds the GIL through a Python-heavy import even in a thread (measured ~0.6s worst gap
    # here) — a once-per-process cost this pin is not about. Warm it the way startup does.
    try:
        from src.extraction.linguistics import linguistics_available as _la
        _la()
    except Exception:  # noqa: BLE001 — layer absent → the pre-routes no-op, the pin still holds
        pass
    # The label the handler will map back to STATEMENT.
    descs = main._build_intent_descriptions_for_gliner2()
    model = _SlowModel(descs["STATEMENT"])
    main.app.dependency_overrides[main.get_gliner_model] = lambda: model
    try:
        yield main.app, model
    finally:
        main.app.dependency_overrides.pop(main.get_gliner_model, None)


async def _drive(app):
    """Run ONE /classify-intent and, for its ENTIRE duration, keep poking two trivial endpoints
    (the pure-async 404 control and the sync-def /internal/ingest-route). Record the WORST
    latency either control saw. A single control fired at a fixed offset can land while the
    handler happens to be awaiting a thread (the round-1 pin did, and stayed green under a
    revert); a continuous poller cannot miss a ≥``_SLOW_S`` freeze wherever it sits in the
    handler."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                 timeout=60.0) as c:
        done = asyncio.Event()

        async def classify():
            try:
                return await c.post(
                    "/classify-intent",
                    params={"user_id": "921514dd-512e-44e1-8f0c-f58090027bd9"},
                    json={"text": "My sister Dana moved to Halifax last spring."})
            finally:
                done.set()

        async def poll(path, expect):
            """Worst wall-clock gap between consecutive poll ITERATIONS (request + a 0.05s
            sleep). Measuring request latency alone misses a freeze that lands inside the
            sleep — the loop stalls, the sleep simply resumes late, and the next request
            starts a fresh clock. The iteration gap is a heartbeat: a ≥``_SLOW_S`` freeze
            anywhere in the handler shows up as one gap ≥``_SLOW_S``."""
            worst = 0.0
            n = 0
            while not done.is_set():
                t0 = time.monotonic()
                r = await c.get(path)
                assert r.status_code == expect, (path, r.status_code)
                await asyncio.sleep(0.05)
                worst = max(worst, time.monotonic() - t0)
                n += 1
            return worst, n

        cls, (w404, n404), (wroute, nroute) = await asyncio.gather(
            classify(), poll("/__ft_control_does_not_exist", 404), poll("/internal/ingest-route", 200))
        return cls, (w404, n404), (wroute, nroute)


def test_concurrent_requests_answer_while_inference_sleeps_in_a_thread(app_and_model):
    app, model = app_and_model
    cls, (w404, n404), (wroute, nroute) = asyncio.run(_drive(app))
    assert model.calls == 1
    assert cls.status_code == 200 and cls.json().get("intent") == "STATEMENT", cls.text
    assert n404 >= 5 and nroute >= 5, (n404, nroute)  # the pollers actually ran during classify
    assert w404 < _CONTROL_BUDGET_S, (
        f"worst pure-async 404 heartbeat gap {w404:.2f}s over {n404} polls while classify "
        f"blocked ≥{_SLOW_S}s in its thread — the loop froze (inference running ON the loop)")
    assert wroute < _CONTROL_BUDGET_S, (
        f"worst GET /internal/ingest-route heartbeat gap {wroute:.2f}s over {nroute} polls — loop frozen")


def test_deterministic_first_router_also_runs_off_the_loop(app_and_model, monkeypatch):
    """The router's tier-2 lane (INTENT_DETERMINISTIC_FIRST, default ON) legitimately owns the
    driven sentence — GLiNER2 is never consulted — and its own spaCy work (the cue re-derive,
    the negation scan, the assertion count, the anchoring probe) must run in the SAME off-loop
    discipline: while the lane's threaded unit holds its thread, the pollers keep answering."""
    import src.api.main as main
    monkeypatch.setattr(main, "INTENT_DETERMINISTIC_FIRST", True)
    app, model = app_and_model
    cls, (w404, n404), (wroute, nroute) = asyncio.run(_drive(app))
    assert model.calls == 0, "the tier-2 lane must own the turn — GLiNER2 never consulted"
    assert cls.status_code == 200 and cls.json().get("intent") == "STATEMENT", cls.text
    assert n404 >= 5 and nroute >= 5, (n404, nroute)
    assert w404 < _CONTROL_BUDGET_S and wroute < _CONTROL_BUDGET_S, (
        f"router-lane spaCy work froze the loop (w404={w404:.2f}s wroute={wroute:.2f}s)")
