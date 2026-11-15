"""gauntlet inprocess-mcp-client — IM1..IM5.

THE WOUND (pre-prod 2026-09-15 at 24266d6f, `DEV/review/gauntlet/inprocess-mcp-client/round_1/
preprod_litmus_0of12.log`): the user-facing self-benchmark (`saas/bench_selftest.py`) drove
`src.mcp.server`'s tools IN-PROCESS in a host process where NO transport lifespan had run.
`src/mcp/server.py::_http_client` is assigned in exactly two places — `server.py::run_mcp_server`
(stdio) and `src/mcp/http_server.py::lifespan` (HTTP) — so in any other process it was `None`,
`_brain_request` did `await _http_client.post(...)` → `AttributeError: 'NoneType' object has no
attribute 'post'` → `BrainUnavailable` on EVERY brain call → 0/12 claims, all errored, every
recall reading "I could not reach memory just now". Two of the in-process consumers
(`saas/agent_gauntlet.py`, `saas/agent_autoscope.py`) carried a copy-pasted
`if getattr(_mcp_server, "_http_client", None) is None: _mcp_server._http_client =
_httpx.AsyncClient(timeout=30.0)` guard — a BARE client, so even they bypassed the round-17
turn wall (`TurnBoundedClient`).

THE CONTRACT pinned here:
  IM1  a fresh process (module imported, `_http_client` None) completes a brain call and a
       `remember_facts_tool` turn against a LOCAL stub backend — through ONE accessor,
       `server.py::_client()`, which lazily creates the wall-bounded `TurnBoundedClient` and logs
       `http_client.lazy_created` once. The revert-shaped leg (accessor bypassed) reproduces the
       old AttributeError→BrainUnavailable path, so the pin can fail.
  IM2  an AST census: zero `_http_client.<attr>` reads in server.py outside `run_mcp_server`
       (the stdio lifecycle site) and the accessor pair; `_client()` call-site COUNT asserted
       (≥ 27 = the 26 `await _http_client.post/get(` sites + the /learn lane's inline guard);
       anti-vacuity: the walker finds `def _client` and catches a planted direct read.
  IM3  every in-process consumer is DRIVEN with `_http_client=None` against the stub and the stub
       records the real brain call; the two copy-pasted guards are gone (AST, saas/).
  IM4  lifecycle: the lazily-created client is closed by `close_lazy_client()` — executed through
       main.py's REAL shutdown statements (extracted by AST from `lifespan`, run against fakes for
       the API's own clients) — and a transport-assigned client is untouched by that hook;
       http_server's own lifespan still closes its own (driven, SAAS off).
  IM5  revert-red: with the accessor removed (`revert_red.txt`), IM1/IM2/IM3 go red.

Stub backend: a REAL loopback HTTP server (threaded `http.server`) answering the brain endpoints
with minimal JSON and recording every hit — no DB, no LLM, no live backend. Run through
`FLTEST_LEDGER=/tmp/ledger_inprocess.json python3 tools/fltest.py --bug inprocess-mcp-client
--test tests/test_inprocess_mcp_client.py`.
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import src.mcp.server as S  # noqa: E402

# Soft on the REVERT-RED run (accessor removed): the behavioural legs must fail on behaviour.
BrainUnavailable = getattr(S, "BrainUnavailable", None) or type("BrainUnavailableMissing", (Exception,), {})

_UID = "00000000-0000-4000-8000-00000000abcd"
_STATEMENT = "I really like tea in the morning."
_QUESTION = "what do I like?"
_RECALL_TEXT = "You like tea."


# ── the local stub backend ──────────────────────────────────────────────────────────────

def _make_stub():
    """A real HTTP server on 127.0.0.1:<random>. `hits` = [(method, path, json_body)]."""
    hits: list[tuple[str, str, object]] = []
    lock = threading.Lock()

    def answer(path, body):
        if "/classify-intent" in path:
            text = str(body.get("text", "")) if isinstance(body, dict) else ""
            return {"intent": "QUERY" if text.rstrip().endswith("?") else "STATEMENT",
                    "confidence": 0.99}
        if "/confidence-gate" in path:
            return {"threshold": 0.7}
        if "/internal/ingest-route" in path:
            return {"statement_extractor": "spine"}
        if "/episodic/append" in path:
            return {"status": "stored", "id": 1}
        if "/episodic/probe" in path:
            return {"status": "found"}
        if "/harvest-spans" in path:
            return {"edges": [{"subject": "user", "rel_type": "likes", "object": "tea",
                               "subject_type": "Person", "object_type": "Object",
                               "confidence": 0.9}], "status": "ok"}
        if "/ingest" in path:
            return {"status": "valid", "committed": 1, "staged": 0}
        if "/query" in path:
            return {"anchor": "user",
                    "facts": [{"subject": "user", "rel_type": "likes", "object": "tea",
                               "definition": _RECALL_TEXT, "fact_class": "A",
                               "fact_provenance": "user_stated", "confidence": 1.0}],
                    "preferred_names": {}, "attributes": {}}
        if "/provisioning/status" in path:
            return {"status": "ready"}
        if "/learn" in path:
            return {"status": "queued"}
        if "/chat/completions" in path:   # the BYO brain, when a test points it here
            return {"id": "stub", "object": "chat.completion", "model": "stub-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "Tea, then."}}]}
        return {"status": "ok"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence the access log
            pass

        def _do(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw) if raw else None
            except Exception:
                body = raw.decode("utf-8", "replace")
            with lock:
                hits.append((self.command, self.path.split("?")[0], body))
            payload = json.dumps(answer(self.path, body)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _do
        do_POST = _do

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}", hits


@pytest.fixture(scope="module")
def stub():
    srv, url, hits = _make_stub()
    yield SimpleNamespace(url=url, hits=hits)
    srv.shutdown()


def _paths(stub) -> list[str]:
    return [p for _, p, _ in stub.hits]


# ── fresh-process semantics on the imported module ──────────────────────────────────────

async def _sleep_forever():
    await asyncio.sleep(3600)


async def _passthrough(user_id, text, result):
    return result


@pytest.fixture
def fresh(stub, monkeypatch):
    """The module as a host process finds it: no transport lifespan ran → `_http_client` None.
    The backend URL is the stub; the seat is pinned provisioned; the drains are kept quiet."""
    monkeypatch.setattr(S, "FAULTLINE_API_URL", stub.url)
    S._FAULTLINE_URL_DETECTED = True
    S._provisioned_users.add(_UID)
    S._http_client = None
    if hasattr(S, "_lazy_http_client"):
        S._lazy_http_client = None
    S._statement_defer_queue = None
    S._statement_defer_task = None
    monkeypatch.setattr(S, "_statement_defer_worker", _sleep_forever)
    monkeypatch.setattr(S, "_with_experience", _passthrough)
    if hasattr(S, "_EPISODIC_REAPPEND_BASE_S"):
        import tempfile
        monkeypatch.setattr(S, "_EPISODIC_REAPPEND_BASE_S", 0.0)
        monkeypatch.setattr(S, "_EPISODIC_REAPPEND_MAX_DELAY_S", 0.0)
        monkeypatch.setattr(S, "_EPISODIC_UNCONFIRMED_CRIT_S", 3600.0)
        monkeypatch.setattr(S, "_EPISODIC_SPOOL_DIR", tempfile.mkdtemp(prefix="im-spool-"))
        S._episodic_reappend_queue = None
        S._episodic_reappend_task = None
        S._episodic_reappend_inflight = None
    del stub.hits[:]
    yield stub
    S._provisioned_users.discard(_UID)
    for name in ("_statement_defer_task", "_episodic_reappend_task"):
        t = getattr(S, name, None)
        if t is not None and not t.done():
            t.cancel()
    S._statement_defer_queue = None
    S._statement_defer_task = None


def _run(coro):
    """Run on a fresh loop with stderr captured (server.py logs via `_log` → stderr)."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        out = asyncio.run(coro)
    return out, buf.getvalue()


async def _close_lazy():
    if hasattr(S, "close_lazy_client"):
        await S.close_lazy_client()


# ═══════════════════════════════════════════════════════════════════════════════════════
# IM1 — the repro, fixed, and the revert-shaped leg
# ═══════════════════════════════════════════════════════════════════════════════════════

def test_im1_brain_request_completes_with_no_transport_lifespan(fresh):
    async def body():
        assert S._http_client is None
        r = await S._brain_request("CLASSIFY_INTENT", "POST",
                                   f"{S.FAULTLINE_API_URL}/classify-intent",
                                   json={"text": _STATEMENT, "user_id": _UID})
        code = r.status_code
        client = S._http_client
        await _close_lazy()
        return code, client
    (code, client), log = _run(body())
    assert code == 200
    assert _paths(fresh) == ["/classify-intent"], _paths(fresh)
    assert type(client).__name__ == "TurnBoundedClient", type(client)   # wall-bounded, not bare
    assert "http_client.lazy_created" in log, log[-800:]


def test_im1_remember_facts_tool_completes_in_process(fresh):
    async def body():
        r = await S.remember_facts_tool(_STATEMENT, _UID)
        await _close_lazy()
        return r
    r, log = _run(body())
    assert r.get("status") == "valid" and r.get("committed") == 1, r
    assert not r.get("brain_unavailable"), r
    seen = set(_paths(fresh))
    assert {"/classify-intent", "/harvest-spans", "/ingest"} <= seen, seen
    assert "AttributeError" not in log


def test_im1_recall_memory_tool_completes_in_process(fresh):
    async def body():
        r = await S.recall_memory_tool(_QUESTION, _UID)
        await _close_lazy()
        return r
    r, _ = _run(body())
    assert _RECALL_TEXT in str(r.get("memory")), r
    assert "/query" in _paths(fresh)


def test_im1_lazy_creation_happens_once_and_honours_an_assigned_client(fresh):
    """Two calls → one client (one `lazy_created` line); a transport-assigned client is returned
    as-is and never replaced."""
    async def body():
        a = S._client()
        b = S._client()
        assert a is b
        await _close_lazy()
        assigned = S.TurnBoundedClient(httpx.AsyncClient(timeout=30.0))
        S._http_client = assigned
        try:
            assert S._client() is assigned
            assert S._lazy_http_client is None            # not ours
            assert await S.close_lazy_client() is False   # nothing lazily owned → no-op
            assert S._http_client is assigned             # untouched
        finally:
            await assigned.aclose()
            S._http_client = None
    _, log = _run(body())
    assert log.count("http_client.lazy_created") == 1, log


def test_im1_revert_shape_accessor_bypassed_reproduces_the_wound(fresh, monkeypatch):
    """Prove the pin can fail: read `_http_client` raw (what every site did before) and the old
    path comes straight back — AttributeError → BrainUnavailable, remember_facts degraded, ZERO
    hits on the backend. This is byte-for-byte the pre-prod litmus shape."""
    monkeypatch.setattr(S, "_client", lambda: S._http_client, raising=False)

    async def body():
        with pytest.raises(BrainUnavailable) as ei:
            await S._brain_request("CLASSIFY_INTENT", "POST",
                                   f"{S.FAULTLINE_API_URL}/classify-intent",
                                   json={"text": _STATEMENT, "user_id": _UID})
        r = await S.remember_facts_tool(_STATEMENT, _UID)
        return str(ei.value), r
    (cause, r), log = _run(body())
    assert "AttributeError" in cause, cause
    assert r.get("status") == "degraded" and r.get("brain_unavailable") == "/classify-intent", r
    # the exception TEXT goes to the error seam's CRIT line; the transport log carries the type
    assert "brain_call.transport_error" in log and "cause=AttributeError" in log, log[-1200:]
    assert _paths(fresh) == [], _paths(fresh)


def test_im1_truly_fresh_python_process(fresh):
    """Fresh-PROCESS semantics, literally: a subprocess imports src.mcp.server the way a host
    process does (no transport), and the brain call + the tool turn complete against the stub."""
    code = textwrap.dedent(f"""
        import asyncio, os, sys, json
        sys.path.insert(0, {str(ROOT)!r})
        import src.mcp.server as S
        S.FAULTLINE_API_URL = {fresh.url!r}
        S._FAULTLINE_URL_DETECTED = True
        S._provisioned_users.add({_UID!r})
        async def main():
            assert S._http_client is None, S._http_client
            r = await S._brain_request("CLASSIFY_INTENT", "POST", S.FAULTLINE_API_URL + "/classify-intent",
                                       json={{"text": {_STATEMENT!r}, "user_id": {_UID!r}}})
            t = await S.remember_facts_tool({_STATEMENT!r}, {_UID!r})
            closed = await S.close_lazy_client()
            print(json.dumps({{"code": r.status_code, "status": t.get("status"),
                               "committed": t.get("committed"), "closed": closed}}))
        asyncio.run(main())
    """)
    env = dict(os.environ, FAULTLINE_API_URL=fresh.url, SPACY_MODEL="en_core_web_sm")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=180, env=env, cwd=str(ROOT))
    assert p.returncode == 0, p.stderr[-2000:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out == {"code": 200, "status": "valid", "committed": 1, "closed": True}, out
    assert "http_client.lazy_created" in p.stderr
    assert "/classify-intent" in _paths(fresh)


# ═══════════════════════════════════════════════════════════════════════════════════════
# IM2 — the census: THE INVARIANT, not a spelling (round 2)
# ═══════════════════════════════════════════════════════════════════════════════════════
#
# Round-1 census pinned a syntactic SHAPE (`Name(_http_client).attr` inside server.py, exempted
# by function NAME) and three critic mutations survived it: an alias (`_c = _http_client;
# await _c.get(...)`), a NESTED `def _client(): return _http_client` shadowing the accessor,
# and `_mcp_server._http_client.get(...)` in saas/ reached through the module alias.
# The invariant is "no raw read of the shared client outside the accessor", pinned as:
#   (a) server.py: EVERY `ast.Name(id='_http_client', ctx=Load)` — as an attribute receiver,
#       an alias source, a return value, anything — must sit inside one of the THREE
#       module-level FunctionDefs `_client`, `close_lazy_client`, `run_mcp_server`, identified by
#       AST NODE IDENTITY (parent walk), never by name; any FunctionDef named `_client` that is
#       not the module-level one is a red; any rebinding of the name `_client` is a red.
#   (b) saas/ + src/: every `.<attr>` on `_http_client` reached through ANY alias of the
#       module (`import src.mcp.server as X`, `from src.mcp import server [as Y]`, a dotted
#       `src.mcp.server`, a name bound from `sys.modules.get("src.mcp.server")` /
#       `sys.modules[...]`), plus `from src.mcp.server import _http_client` and string probes
#       `"_http_client"` (getattr) — resolved per file by AST — must be exactly the HTTP
#       transport's own two touches inside `http_server.py::lifespan` (identity). Count asserted.
#   (c) anti-vacuity: each survived spelling is planted in a TEMP COPY and the census fires.

_SERVER_PY = ROOT / "src" / "mcp" / "server.py"
_HTTP_SERVER_PY = ROOT / "src" / "mcp" / "http_server.py"
_MODULE = "src.mcp.server"
_ACCESSOR = "_client"
_ALLOWED_SERVER_DEFS = ("_client", "close_lazy_client", "run_mcp_server")


def _set_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._im_parent = parent  # type: ignore[attr-defined]


def _inside(node: ast.AST, allowed: list) -> bool:
    """True iff one of `allowed` (compared by IDENTITY) is an ancestor of `node`."""
    cur = getattr(node, "_im_parent", None)
    while cur is not None:
        if any(cur is a for a in allowed):
            return True
        cur = getattr(cur, "_im_parent", None)
    return False


def _enclosing_def(node: ast.AST) -> str | None:
    cur = getattr(node, "_im_parent", None)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = getattr(cur, "_im_parent", None)
    return None


def census_server(src: str) -> dict:
    """(a) — every Load of `_http_client` in server.py, classified by node identity."""
    tree = ast.parse(src)
    _set_parents(tree)
    module_defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    allowed = [module_defs[n] for n in _ALLOWED_SERVER_DEFS if n in module_defs]
    loads = [(n.lineno, _enclosing_def(n), _inside(n, allowed))
             for n in ast.walk(tree)
             if isinstance(n, ast.Name) and n.id == "_http_client" and isinstance(n.ctx, ast.Load)]
    stray_loads = [(ln, fn) for ln, fn, ok in loads if not ok]
    shadow_defs = [(n.lineno, _enclosing_def(n)) for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == _ACCESSOR
                   and n is not module_defs.get(_ACCESSOR)]
    rebinding = [n.lineno for n in ast.walk(tree)
                 if isinstance(n, ast.Name) and n.id == _ACCESSOR and not isinstance(n.ctx, ast.Load)]
    calls = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == _ACCESSOR]
    ctor_in_accessor = 0
    if _ACCESSOR in module_defs:
        ctor_in_accessor = sum(1 for n in ast.walk(module_defs[_ACCESSOR]) if isinstance(n, ast.Call)
                               and isinstance(n.func, ast.Name) and n.func.id == "TurnBoundedClient")
    return {"has_accessor": _ACCESSOR in module_defs, "loads": loads, "stray_loads": stray_loads,
            "shadow_defs": shadow_defs, "rebinding": rebinding, "calls": calls,
            "ctor_in_accessor": ctor_in_accessor,
            "allowed_present": [n for n in _ALLOWED_SERVER_DEFS if n in module_defs]}


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _module_aliases(tree: ast.AST) -> tuple[set[str], list[int]]:
    """Names that refer to src.mcp.server in this file, plus lines that import `_http_client`
    directly from it (always a red)."""
    aliases: set[str] = set()
    direct: list[int] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == _MODULE:
                    aliases.add(a.asname or "src")   # bare `import src.mcp.server` binds `src`
        elif isinstance(n, ast.ImportFrom):
            if n.module == "src.mcp":
                for a in n.names:
                    if a.name == "server":
                        aliases.add(a.asname or "server")
            elif n.module == _MODULE:
                for a in n.names:
                    if a.name == "_http_client":
                        direct.append(n.lineno)
        elif isinstance(n, ast.Assign):
            v = n.value
            is_sysmod = False
            if isinstance(v, ast.Call) and _dotted(v.func) == "sys.modules.get" and v.args \
                    and isinstance(v.args[0], ast.Constant) and v.args[0].value == _MODULE:
                is_sysmod = True
            if isinstance(v, ast.Subscript) and _dotted(v.value) == "sys.modules" \
                    and isinstance(v.slice, ast.Constant) and v.slice.value == _MODULE:
                is_sysmod = True
            if is_sysmod:
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        aliases.add(t.id)
    return aliases, direct


def census_consumers(sources: dict[str, str]) -> dict:
    """(b) — every touch of `<module alias>._http_client` across the given {relpath: source}."""
    touches: list[tuple[str, int, str | None, str]] = []   # (file, line, enclosing def, kind)
    for rel, src in sources.items():
        tree = ast.parse(src)
        _set_parents(tree)
        aliases, direct = _module_aliases(tree)
        for ln in direct:
            touches.append((rel, ln, None, "from-import"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr == "_http_client":
                recv = _dotted(n.value)
                if recv and (recv in aliases or recv == _MODULE
                             or any(recv.startswith(a + ".") for a in aliases)):
                    kind = "store" if isinstance(n.ctx, ast.Store) else "load"
                    touches.append((rel, n.lineno, _enclosing_def(n), kind))
            elif isinstance(n, ast.Constant) and n.value == "_http_client":
                touches.append((rel, n.lineno, _enclosing_def(n), "string-probe"))
    return {"touches": touches}


def _repo_sources() -> dict[str, str]:
    out = {}
    for scope in ("saas", "src"):
        for f in sorted((ROOT / scope).rglob("*.py")):
            posix = f.as_posix()
            if "/tests/" in posix or "/.codeql/" in posix or "/FaultLine/" in posix:
                continue
            out[f.relative_to(ROOT).as_posix()] = f.read_text(encoding="utf-8")
    return out


def _allowed_consumer_touches(sources: dict[str, str]) -> list[tuple[str, int]]:
    """The ONLY tolerated touches: inside http_server.py's module-level `lifespan` (identity)."""
    src = sources.get("src/mcp/http_server.py")
    if src is None:
        return []
    tree = ast.parse(src)
    _set_parents(tree)
    lifespan = next((n for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "lifespan"), None)
    if lifespan is None:
        return []
    return [("src/mcp/http_server.py", n.lineno) for n in ast.walk(lifespan)
            if isinstance(n, ast.Attribute) and n.attr == "_http_client"]


def test_im2_server_every_load_sits_inside_the_accessor_or_stdio_lifecycle():
    c = census_server(_SERVER_PY.read_text(encoding="utf-8"))
    assert c["has_accessor"], "anti-vacuity: the walker did not find the module-level accessor"
    assert c["allowed_present"] == list(_ALLOWED_SERVER_DEFS), c["allowed_present"]
    assert c["stray_loads"] == [], f"raw Load of _http_client outside the accessor/lifecycle: {c['stray_loads']}"
    assert c["shadow_defs"] == [], f"a FunctionDef named _client that is not the module-level accessor: {c['shadow_defs']}"
    assert c["rebinding"] == [], f"the accessor name is rebound: {c['rebinding']}"
    # the three legitimate Loads: `c = _http_client` (accessor), `if _http_client is lazy`
    # (close_lazy_client), `await _http_client.aclose()` (run_mcp_server) — and nothing else
    assert len(c["loads"]) == 3, c["loads"]
    assert sorted(fn for _, fn, _ in c["loads"]) == sorted(_ALLOWED_SERVER_DEFS), c["loads"]


def test_im2_server_accessor_call_site_count():
    """32 textual `_http_client` mentions at HEAD decomposed as: 26 `await _http_client.post/get(`
    sites + 1 inline `client = _http_client` guard in the /learn lane (both migrated) + the
    declaration + 1 comment + `global` + the stdio assign + the stdio aclose. So ≥ 27 real
    accessor call sites must exist — COUNTED by the AST, never grepped as a spelling."""
    c = census_server(_SERVER_PY.read_text(encoding="utf-8"))
    assert len(c["calls"]) >= 27, f"only {len(c['calls'])} `_client()` call sites: {c['calls']}"
    assert c["ctor_in_accessor"] == 1, "the accessor must construct exactly one TurnBoundedClient"


def test_im2_consumers_only_touch_is_http_server_lifespan():
    sources = _repo_sources()
    allowed = _allowed_consumer_touches(sources)
    assert len(allowed) == 2, f"http_server.lifespan must hold exactly its assign + aclose: {allowed}"
    c = census_consumers(sources)
    stray = [t for t in c["touches"] if (t[0], t[1]) not in allowed]
    assert stray == [], f"raw touches of the shared client through a module alias: {stray}"
    assert len(c["touches"]) == 2, c["touches"]          # count asserted: the two, no more


# ── (c) anti-vacuity: the three survived spellings, planted in TEMP COPIES ──────────────

def _plant_in_function(src: str, func: str, lines: list[str]) -> str:
    """Insert `lines` as the first statements of `func` (module-level def) in a copy of `src`."""
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func)
    first = fn.body[0]
    src_lines = src.splitlines(keepends=True)
    indent = " " * first.col_offset
    at = first.lineno - 1
    return "".join(src_lines[:at]) + "".join(indent + l + "\n" for l in lines) + "".join(src_lines[at:])


def _plant_after(src: str, anchor: str, line: str) -> str:
    idx = src.index(anchor)
    eol = src.index("\n", idx) + 1
    line_start = src.rfind("\n", 0, idx) + 1
    indent = src[line_start:idx][: len(src[line_start:idx]) - len(src[line_start:idx].lstrip())]
    return src[:eol] + indent + line + "\n" + src[eol:]


@pytest.fixture
def temp_tree(tmp_path):
    """A TEMP COPY of the four files the plants touch; the census is run over the copies."""
    files = {}
    for rel in ("src/mcp/server.py", "src/mcp/http_server.py", "saas/agent_gauntlet.py", "saas/agent_proxy.py"):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
        files[rel] = dst
    return files


def _read_all(files: dict) -> dict[str, str]:
    return {rel: p.read_text(encoding="utf-8") for rel, p in files.items()}


def test_im2_plant_mut2_alias_load_in_query_tool_fires(temp_tree):
    """MUT2: `_c = _http_client; await _c.get(...)` in query_tool — a Load outside the three."""
    p = temp_tree["src/mcp/server.py"]
    p.write_text(_plant_in_function(p.read_text(encoding="utf-8"), "query_tool", [
        "_c = _http_client",
        "await _c.get(f\"{FAULTLINE_API_URL}/health\")",
    ]), encoding="utf-8")
    c = census_server(p.read_text(encoding="utf-8"))
    assert c["stray_loads"] and all(fn == "query_tool" for _, fn in c["stray_loads"]), c["stray_loads"]
    assert census_server(_SERVER_PY.read_text(encoding="utf-8"))["stray_loads"] == []   # control


def test_im2_plant_mut6_nested_accessor_shadow_fires(temp_tree):
    """MUT6: a nested `def _client(): return _http_client` inside query_tool — red twice: a
    FunctionDef named _client that is not the module-level node, AND a Load outside the three
    (the nested def is not the allowed node, by identity)."""
    p = temp_tree["src/mcp/server.py"]
    p.write_text(_plant_in_function(p.read_text(encoding="utf-8"), "query_tool", [
        "def _client():",
        "    return _http_client",
    ]), encoding="utf-8")
    c = census_server(p.read_text(encoding="utf-8"))
    assert c["shadow_defs"] and c["shadow_defs"][0][1] == "query_tool", c["shadow_defs"]
    assert c["stray_loads"] and c["stray_loads"][0][1] == "_client", c["stray_loads"]
    assert c["has_accessor"]                                             # the real one still exists


def test_im2_plant_mut5_alias_attribute_read_in_saas_fires(temp_tree):
    """MUT5: `_mcp_server._http_client.get(...)` in agent_gauntlet (module alias from
    `from src.mcp import server as _mcp_server`) and `_srv._http_client.get(...)` in agent_proxy
    (alias from `import src.mcp.server as _srv`) — both resolved by the per-file alias walk."""
    g = temp_tree["saas/agent_gauntlet.py"]
    gs = g.read_text(encoding="utf-8")
    gs = _plant_after(gs, "from src.mcp.server import recall_memory_tool as _recall",
                      "from src.mcp import server as _mcp_server")
    gs = _plant_after(gs, "from src.mcp import server as _mcp_server",
                      "await _mcp_server._http_client.get(\"http://x/health\")")
    g.write_text(gs, encoding="utf-8")
    a = temp_tree["saas/agent_proxy.py"]
    as_ = a.read_text(encoding="utf-8")
    as_ = _plant_after(as_, "from src.mcp.server import recall_memory_tool as _recall",
                       "import src.mcp.server as _srv")
    as_ = _plant_after(as_, "import src.mcp.server as _srv", "await _srv._http_client.get(\"http://x/health\")")
    a.write_text(as_, encoding="utf-8")
    c = census_consumers(_read_all(temp_tree))
    allowed = _allowed_consumer_touches(_read_all(temp_tree))
    stray = [t for t in c["touches"] if (t[0], t[1]) not in allowed]
    assert {t[0] for t in stray} == {"saas/agent_gauntlet.py", "saas/agent_proxy.py"}, stray
    assert all(t[3] == "load" for t in stray), stray


def test_im2_plant_sys_modules_alias_and_direct_import_fire(temp_tree):
    """Two further spellings a critic could reach for: a name bound from
    `sys.modules.get("src.mcp.server")` then `.` -read, and `from src.mcp.server import _http_client`."""
    a = temp_tree["saas/agent_proxy.py"]
    as_ = a.read_text(encoding="utf-8")
    as_ = _plant_after(as_, "from src.mcp.server import recall_memory_tool as _recall",
                       "_m = sys.modules.get(\"src.mcp.server\")")
    as_ = _plant_after(as_, "_m = sys.modules.get(\"src.mcp.server\")", "await _m._http_client.get(\"http://x\")")
    a.write_text(as_, encoding="utf-8")
    g = temp_tree["saas/agent_gauntlet.py"]
    g.write_text(_plant_after(g.read_text(encoding="utf-8"),
                              "from src.mcp.server import recall_memory_tool as _recall",
                              "from src.mcp.server import _http_client as _raw"), encoding="utf-8")
    c = census_consumers(_read_all(temp_tree))
    kinds = {(t[0], t[3]) for t in c["touches"] if t[0] != "src/mcp/http_server.py"}
    assert ("saas/agent_proxy.py", "load") in kinds and ("saas/agent_gauntlet.py", "from-import") in kinds, kinds


def test_im2_plant_getattr_guard_fires(temp_tree):
    """The original copy-pasted guard shape (string probe + attribute store) still reads red."""
    g = temp_tree["saas/agent_gauntlet.py"]
    gs = g.read_text(encoding="utf-8")
    gs = _plant_after(gs, "from src.mcp.server import recall_memory_tool as _recall",
                      "from src.mcp import server as _mcp_server")
    gs = _plant_after(gs, "from src.mcp import server as _mcp_server",
                      "if getattr(_mcp_server, \"_http_client\", None) is None: _mcp_server._http_client = object()")
    g.write_text(gs, encoding="utf-8")
    c = census_consumers(_read_all(temp_tree))
    kinds = {t[3] for t in c["touches"] if t[0] == "saas/agent_gauntlet.py"}
    assert kinds == {"string-probe", "store"}, c["touches"]


def test_im3_copy_pasted_guards_are_deleted_from_saas():
    """The round-1 leg, kept: no assignment / string probe of `_http_client` anywhere under saas/."""
    c = census_consumers({k: v for k, v in _repo_sources().items() if k.startswith("saas/")})
    assert c["touches"] == [], c["touches"]


# ═══════════════════════════════════════════════════════════════════════════════════════
# IM3 — the consumers, driven with the client absent
# ═══════════════════════════════════════════════════════════════════════════════════════

def _fast_asyncio():
    """bench_selftest's settle sleeps (2.0 + 0.4 + 1.5 s per claim) collapsed for the drive; every
    other name it uses is the real one."""
    async def _sleep(_s):
        await asyncio.sleep(0)
    return SimpleNamespace(sleep=_sleep, to_thread=asyncio.to_thread, Lock=asyncio.Lock)


def test_im3_bench_selftest_run_selftest_drives_real_brain_calls(fresh, monkeypatch):
    """The self-benchmark itself (the 0/12 producer). Stubbed at the narrowest seams it needs and
    cannot have locally: the control-plane account/seat/brain rows (`_accounts.create_account`,
    `_seats.register_seat`, `_llm_config.upsert_config`), provisioning (`ensure_user_provisioned`,
    `_wait_provisioned`), the sandbox reset/teardown (psycopg2 TRUNCATE / DROP), the claim library
    (one claim), and the per-claim settle sleeps. NOT stubbed: the tools themselves, the MCP
    module's client, the backend calls — those hit the stub server over real HTTP."""
    from saas import bench_selftest as B
    from src.provisioning import provisioning_status as PS

    monkeypatch.setattr(B, "saas_mode_enabled", lambda: True)
    claim = {"name": "preference-tea", "category": "preference", "setup_turns": [_STATEMENT],
             "query": _QUESTION, "why": "drive"}
    lib = SimpleNamespace(CLAIMS=[claim], assess=lambda c, text: {
        "passed": "tea" in text.lower(), "expected": "contains: tea",
        "checks": [{"kind": "contains", "needle": "tea", "ok": "tea" in text.lower()}]})
    monkeypatch.setattr(B, "_load_claim_lib", lambda: lib)
    monkeypatch.setattr(B._accounts, "create_account",
                        lambda **kw: {"account_id": "acct-sandbox", "slug": kw.get("slug")})
    monkeypatch.setattr(B._seats, "register_seat", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(B._llm_config, "upsert_config", lambda *a, **kw: None)
    monkeypatch.setattr(PS, "ensure_user_provisioned", lambda *a, **kw: None)

    async def _provisioned(seat_uuid, timeout=180.0):
        S._provisioned_users.add(seat_uuid)       # what the backend's ready answer would do
        return True
    monkeypatch.setattr(B, "_wait_provisioned", _provisioned)
    monkeypatch.setattr(B, "_reset_sandbox", lambda seat_uuid, **kw: True)
    torn = {}
    monkeypatch.setattr(B, "_teardown_sandbox", lambda a, s, u: torn.update(seat=u) or {})
    monkeypatch.setattr(B, "asyncio", _fast_asyncio())

    async def body():
        assert S._http_client is None
        res = await B.run_selftest("acct-owner", {"backend_type": "openai",
                                                  "base_url": "http://brain.example.test/v1",
                                                  "model": "stub-model"})
        await _close_lazy()
        return res
    res, log = _run(body())
    assert res["claims_total"] == 1 and res["claims_passed"] == 1 and res["claims_errored"] == 0, res
    assert _RECALL_TEXT in res["claims"][0]["actual_recall"], res["claims"][0]
    seen = set(_paths(fresh))
    assert {"/classify-intent", "/harvest-spans", "/ingest", "/query"} <= seen, seen
    assert torn.get("seat"), "the sandbox teardown still runs"
    assert "could not reach memory" not in log and "AttributeError" not in log


def test_im3_agent_gauntlet_chat_reply_recalls_in_process(fresh, monkeypatch):
    """`chat_reply_for_question` with the guard GONE: the fresh recall is a real `/query` on the
    backend. Stubbed: the scope config + seat label (control-plane DB reads) and the seat brain
    (None → the honest 'no brain' abstention AFTER the recall, which is the part under test)."""
    from saas import agent_gauntlet as G
    from saas import accounts as A, agent_control as AC, seat_brain as SB
    monkeypatch.setattr(A, "get_agent_scope_config", lambda *a, **kw: {})
    monkeypatch.setattr(AC, "_resolve_seat_label", lambda *a, **kw: "Test Co")
    monkeypatch.setattr(SB, "resolve_brain", lambda *a, **kw: None)

    async def body():
        assert S._http_client is None
        out = await G.chat_reply_for_question("acct", _UID, _QUESTION)
        await _close_lazy()
        return out
    (text, usage, hiccups), _ = _run(body())
    assert usage["recall_calls"] == 1 and usage["recall_chars"] > 0, (usage, hiccups)
    assert "recall" not in hiccups, hiccups
    assert hiccups.get("brain") == "no brain configured", hiccups   # reached the brain step
    assert "/query" in _paths(fresh)


def test_im3_agent_autoscope_draft_scope_recalls_in_process(fresh, monkeypatch):
    """`draft_scope` with the guard GONE: the corpus recall is a real `/query`. Stubbed: seat label
    and the seat brain (None → `no_grounding` carrying the grounded_chars it actually recalled)."""
    from saas import agent_autoscope as AS
    from saas import agent_control as AC, seat_brain as SB
    monkeypatch.setattr(AC, "_resolve_seat_label", lambda *a, **kw: "Test Co")
    monkeypatch.setattr(SB, "resolve_brain", lambda *a, **kw: None)
    monkeypatch.setattr(AS, "_RECALL_PROBE", _QUESTION, raising=False)

    async def body():
        assert S._http_client is None
        out = await AS.draft_scope("acct", _UID)
        await _close_lazy()
        return out
    out, _ = _run(body())
    assert out["status"] == "no_grounding" and "no brain" in out["reason"], out
    assert out["grounded_chars"] >= 40, out          # the recall landed; only the brain is absent
    assert "/query" in _paths(fresh)


def test_im3_agent_control_expand_route_dispatches_learn_in_process(fresh, monkeypatch):
    """`POST /v1/agent/expand` → `_learn_via_llm` → the background `/learn` POST through the
    accessor. Driven through the REAL route on an ASGI app (httpx ASGITransport on this loop, so
    the fire-and-forget task runs here). Stubbed: the delegate auth seams `attach()` receives,
    SaaS/source gates, serve entitlement, the per-seat write gate, metering."""
    from fastapi import APIRouter, FastAPI
    from saas import agent_control as AC, agent_entitlement as AE, agent_mode as AM, metering as M
    monkeypatch.setattr(AC, "_require_saas", lambda: None)
    monkeypatch.setattr(AC, "_source_allowed", lambda request: (True, "127.0.0.1", "peer"))
    monkeypatch.setattr(AE, "serve_allowed", lambda *a, **kw: (True, None))
    monkeypatch.setattr(AM, "write_gate_block", lambda *a, **kw: None)

    async def _bump(*a, **kw):
        return None
    monkeypatch.setattr(M, "bump_for_tool", _bump)

    router = APIRouter()
    AC.attach(router, require_auth=lambda: "seat-token-principal",
              resolve_user_id=lambda request, hdr, principal: _UID,
              account_for=lambda principal, user_id: "acct", log=lambda m: None)
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    async def body():
        assert S._http_client is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            r = await c.post("/v1/agent/expand", json={"topic": "networking", "online": False})
        # the /learn dispatch is fire-and-forget (`_background`): wait for the stub to see it
        for _ in range(200):
            if "/learn" in _paths(fresh):
                break
            await asyncio.sleep(0.02)
        await _close_lazy()
        return r.status_code, r.json()
    (code, data), _ = _run(body())
    assert code == 200 and data.get("status") == "pending", (code, data)
    assert "/internal/ingest-route" in _paths(fresh)     # the freeze probe, through the accessor
    assert "/learn" in _paths(fresh), _paths(fresh)       # the real dispatch landed


def test_im3_agent_proxy_chat_route_recalls_and_trains_in_process(fresh, monkeypatch):
    """The 7th consumer: `POST /v1/agent/chat` (saas/agent_proxy.py) → in-process
    `recall_memory_tool` (grounding) and the fire-and-forget `remember_facts_tool` training
    write, both against the stub; the BYO brain is the stub's /chat/completions. Driven through
    the REAL route on an ASGI app. Stubbed: the transport auth seams `build_agent_proxy_router`
    receives, serve entitlement, the seat brain (→ the stub), the seat/scope/prompt control-plane
    reads (→ {}), the per-seat ingest valve (→ allowed), and the egress guard factory (→ a plain
    httpx client, because the guard rightly refuses a loopback brain)."""
    from fastapi import FastAPI
    from saas import agent_proxy as AP, accounts as A, agent_entitlement as AE, agent_mode as AM
    from saas import seat_brain as SB, llm_config as LC, recall_cache as RC
    RC.reset()
    monkeypatch.setattr(AE, "serve_allowed", lambda *a, **kw: (True, None))
    monkeypatch.setattr(SB, "resolve_brain", lambda *a, **kw: {
        "backend_type": "openai", "base_url": fresh.url + "/v1", "model": "stub-model"})
    for name in ("get_agent_scope_config", "get_agent_seat_config", "get_agent_settings",
                 "get_agent_seat_chat_llm_decrypted"):
        monkeypatch.setattr(A, name, lambda *a, **kw: {})
    monkeypatch.setattr(AM, "ingest_allowed", lambda *a, **kw: True)
    monkeypatch.setattr(LC, "guarded_async_client",
                        lambda timeout=None, **kw: httpx.AsyncClient(timeout=timeout))

    router = AP.build_agent_proxy_router(
        require_auth=lambda: "seat-token-principal",
        resolve_user_id=lambda request, hdr, principal: _UID,
        account_for=lambda principal, user_id: "acct", log=lambda m: None)
    app = FastAPI()
    app.include_router(router)

    async def body():
        assert S._http_client is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            r = await c.post("/v1/agent/chat",
                             json={"messages": [{"role": "user", "content": _QUESTION}]})
        # the training write is fire-and-forget: wait for the stub to see its /ingest
        for _ in range(300):
            if "/ingest" in _paths(fresh):
                break
            await asyncio.sleep(0.02)
        await _close_lazy()
        return r.status_code, r.json()
    (code, data), _ = _run(body())
    assert code == 200 and data["choices"][0]["message"]["content"] == "Tea, then.", (code, data)
    seen = _paths(fresh)
    assert "/query" in seen, seen                              # the grounding recall
    assert "/v1/chat/completions" in seen, seen                # the brain, grounded
    assert "/ingest" in seen and "/harvest-spans" in seen, seen  # the training write landed


def test_im3_patients_sites_touch_only_the_provisioning_set(fresh, monkeypatch):
    """HONEST: `saas/patients.py`'s two `src.mcp.server` sites (`_readiness`,
    `deprovision_patient`) read/discard `_provisioned_users` — they make NO brain call by design.
    Driven with the client absent to pin that they neither raise nor touch the backend."""
    from saas import patients as P
    monkeypatch.setattr(P._seats if hasattr(P, "_seats") else P, "revoke_seat",
                        lambda *a, **kw: True, raising=False)
    from saas import seats as SE
    monkeypatch.setattr(SE, "revoke_seat", lambda *a, **kw: True)
    monkeypatch.setattr(P, "_audit_clinical", lambda *a, **kw: None)
    S._provisioned_users.add("patient-1")
    try:
        assert S._http_client is None
        assert P._readiness("patient-1") == "ready"
        assert P._readiness("patient-2") == "provisioning"
        out = P.deprovision_patient("acct", "patient-1", purge=False)
        assert out["ok"] and out["status"] == "revoked", out
        assert "patient-1" not in S._provisioned_users      # the discard site ran
    finally:
        S._provisioned_users.discard("patient-1")
    assert _paths(fresh) == [], "patients makes no brain call — none expected"
    assert S._http_client is None                             # and never created one


# ═══════════════════════════════════════════════════════════════════════════════════════
# IM4 — lifecycle
# ═══════════════════════════════════════════════════════════════════════════════════════

_MAIN_PY = ROOT / "src" / "api" / "main.py"


def _main_shutdown_block() -> str:
    """The statements AFTER `yield` in `src/api/main.py::lifespan` — main.py's REAL shutdown seam,
    extracted by AST (not a regex over the text), so the test executes exactly what the API runs."""
    tree = ast.parse(_MAIN_PY.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    idx = next(i for i, st in enumerate(fn.body)
               if isinstance(st, ast.Expr) and isinstance(st.value, ast.Yield))
    tail = fn.body[idx + 1:]
    assert tail, "lifespan has no shutdown statements after yield"
    return "\n".join(ast.unparse(st) for st in tail)


def _run_main_shutdown(fake_llm_calls_close):
    """Execute main.py's shutdown block with the API's OWN names bound to fakes (its httpx pools,
    redis, gliner, registry, log) and `sys` real — so `sys.modules.get("src.mcp.server")` finds
    the imported module and the block's `close_lazy_client()` await is the real one."""
    # the block assigns `_http_client = None` etc. — module globals in main.py, so declare them
    # global here too (the real lifespan carries the same `global` statement at its top)
    src = ("async def _shutdown():\n    global _gliner2_model, _rel_type_registry, _http_client, "
           "_http_client_sync, _idempotency_mgr, _redis_client\n"
           + textwrap.indent(_main_shutdown_block(), "    "))
    closed = SimpleNamespace(api_async=False, api_sync=False)

    class _AC:
        async def aclose(self):
            closed.api_async = True

    class _SC:
        def close(self):
            closed.api_sync = True

    class _Log:
        def warning(self, *a, **kw):
            pass

    import types
    fake_llm_calls = types.ModuleType("src.api.llm_calls")
    fake_llm_calls.close_llm_http_client = fake_llm_calls_close
    ns = {"sys": sys, "log": _Log(), "_http_client": _AC(), "_http_client_sync": _SC(),
          "_redis_client": None, "_gliner2_model": object(), "_rel_type_registry": object(),
          "_idempotency_mgr": None}
    saved = sys.modules.get("src.api.llm_calls")
    sys.modules["src.api.llm_calls"] = fake_llm_calls
    try:
        exec(compile(src, str(_MAIN_PY) + "::lifespan[shutdown]", "exec"), ns)
        asyncio.run(ns["_shutdown"]())
    finally:
        if saved is not None:
            sys.modules["src.api.llm_calls"] = saved
        else:
            sys.modules.pop("src.api.llm_calls", None)
    return closed


def test_im4_lazily_created_client_is_closed_by_api_shutdown(fresh):
    """Lazily created inside the host process → main.py's shutdown block closes it (the inner
    httpx pool reads is_closed) and the module forgets it."""
    async def create():
        c = S._client()
        return c
    lazy, _ = _run(create())
    inner = lazy._inner
    assert not inner.is_closed
    assert S._lazy_http_client is lazy and S._http_client is lazy
    closed = _run_main_shutdown(lambda: None)
    assert closed.api_async and closed.api_sync                     # the API's own closes still ran
    assert inner.is_closed, "the lazily-created MCP client leaked past API shutdown"
    assert S._http_client is None and S._lazy_http_client is None


def test_im4_transport_assigned_client_is_untouched_by_api_shutdown(fresh):
    """A client a transport lifespan assigned is NOT the API's to close — the hook leaves it."""
    assigned = S.TurnBoundedClient(httpx.AsyncClient(timeout=30.0))
    S._http_client = assigned
    try:
        _run_main_shutdown(lambda: None)
        assert not assigned._inner.is_closed
        assert S._http_client is assigned
    finally:
        asyncio.run(assigned.aclose())
        S._http_client = None


def test_im4_lazy_then_transport_assign_closes_the_orphan_not_the_transport(fresh):
    """Ownership is by IDENTITY: a transport assignment after a lazy creation is honoured, and
    shutdown closes the orphaned lazy client (no leak) while the transport's stays open."""
    async def body():
        lazy = S._client()
        assigned = S.TurnBoundedClient(httpx.AsyncClient(timeout=30.0))
        S._http_client = assigned                      # what run_mcp_server / lifespan do
        assert await S.close_lazy_client() is True
        try:
            assert lazy._inner.is_closed
            assert not assigned._inner.is_closed
            assert S._http_client is assigned
        finally:
            await assigned.aclose()
            S._http_client = None
    _run(body())


def test_im4_http_server_lifespan_still_closes_its_own(fresh, monkeypatch):
    """The HTTP transport's lifespan assigns its own wall-bounded client and closes it on exit —
    driven (spool recovery pointed at a scratch dir)."""
    import tempfile
    import src.mcp.http_server as H
    # tests/conftest.py pins SAAS_MODE=1, so the fail-closed startup guards want a bearer set
    # and no pinned default identity — exactly the deployed posture.
    monkeypatch.setattr(H, "MCP_API_KEY", "test-bearer-for-the-lifespan-drive", raising=False)
    monkeypatch.setattr(S, "FAULTLINE_USER_ID", "", raising=False)
    monkeypatch.setattr(S, "_EPISODIC_SPOOL_DIR", tempfile.mkdtemp(prefix="im-lifespan-"), raising=False)

    async def body():
        async with H.lifespan(H.app):
            c = S._http_client
            assert type(c).__name__ == "TurnBoundedClient"
            assert S._client() is c                   # the accessor honours the transport's client
            assert S._lazy_http_client is None        # nothing lazily owned
            inner = c._inner
        return inner
    inner, _ = _run(body())
    assert inner.is_closed, "http_server's lifespan no longer closes its own client"
    S._http_client = None
