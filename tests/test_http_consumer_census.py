"""Round-3 CONSUMER census — every HTTP request site in saas/ src/ tools/, classified by the URL
it carries and the client it rides, resolved with ``ast`` (import aliases included).

Round 2's census classified CONSTRUCTIONS ("this client is operator infra"), which cannot see a
tenant URL handed to an "operator" client — exactly how ``src/api/main.py``'s three hand-rolled
LLM sites POSTed the tenant brain through ``llm_calls._llm_http_client`` unseen. This census
walks the CALLS instead:

  1. resolve aliases: ``import httpx as _h``, ``from httpx import AsyncClient as C``,
     ``from src.api.llm_calls import _llm_http_client``, ``from .llm_config import guarded_client``;
  2. find every name bound to an HTTP client — a constructor call (aliased or not), a guarded
     factory, a ``with … as client`` / ``async with … as client`` target, an imported shared
     client, a module attribute (``_mcp_server._http_client``) — and every module-level
     ``httpx.post(...)``-style call;
  3. for every ``.post/.get/.put/.patch/.delete/.stream/.send/.request/.build_request`` on such
     a receiver (and ``urlopen``/``urllib.request.Request``), classify:
       * GUARDED  — the receiver is a guarded client (``guarded_client`` / ``guarded_async_client``
                    / ``shared_guarded_*``), or a with-target of ``(_guarded or httpx.Client(...))``;
       * OPERATOR — the URL expression's source names operator infrastructure (Qdrant, the
                    FaultLine backend/MCP, cortex, Portainer, dashboard, Redis) — see
                    ``OPERATOR_URL_MARKERS``;
       * TOLERATED — an explicitly named legacy env-brain seam (``llm_source_ip`` twins,
                    ``_post_llm_sync``'s no-tenant branch) — listed in ``TOLERATED`` with a reason;
     anything else FAILS ("a tenant-controllable URL on an unguarded client").

Self-test: the three plant shapes the critic used (aliased constructor, shared-client reuse,
module-level ``httpx.post(url)``) are written to a temp file and must all be flagged.

Run: FLTEST_LEDGER=... python3 tools/fltest.py --bug IS5 --test tests/test_http_consumer_census.py
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ["saas", "src", "tools"]

HTTP_MODULES = {"httpx", "httpcore", "requests", "aiohttp", "urllib.request"}
CLIENT_CTORS = {"Client", "AsyncClient", "ClientSession", "Session"}
GUARD_FACTORIES = {"guarded_client", "guarded_async_client",
                   "shared_guarded_client", "shared_guarded_async_client"}
REQUEST_METHODS = {"post", "get", "put", "patch", "delete", "head", "options",
                   "stream", "send", "request", "build_request", "urlopen",
                   "post_sync", "post_async"}   # llm_source_ip's wrappers
#: Names that are HTTP clients wherever they appear (shared/pooled clients bound in one module
#: and imported or reached as an attribute elsewhere). Discovered dynamically too (below).
KNOWN_CLIENT_NAMES = {"_llm_http_client", "_http_client", "_http_client_sync"}

#: A URL expression whose SOURCE contains one of these is operator infrastructure.
OPERATOR_URL_MARKERS = (
    "qdrant", "_qdrant", "QDRANT", "collections/", "/collections",
    "faultline_url", "FAULTLINE_API_URL", "backend_url", "backend_api_url", "api_url",
    "_api_base", "_backend", "BACKEND", "/internal/", "/health", "/ingest", "/extract/",
    "/classify-intent", "/provisioning", "/query", "/harvest-spans", "/store_context",
    "/retract", "/confidence-gate", "/structure/", "/learn", "/cortex", "_cortex", "CORTEX",
    "portainer", "PORTAINER", "dashboard", "DASH", "redis", "REDIS", "stripe", "STRIPE",
    "_QDRANT_URL", "qdrant_url", "url + path", "verify_url", "_bench",
    "_LLM_URL", "QWEN_API_URL",   # the OPERATOR's env brain (/health probe, non-SaaS)
)

#: Whole files whose every request targets operator infrastructure by construction
#: (Qdrant-only maintenance scripts, deploy/verify CLIs, the cortex CLI). Named, not inferred.
OPERATOR_FILES = {
    "tools/migrate_qdrant_partition.py": "Qdrant migration CLI (QDRANT_URL env)",
    "tools/cortex_orphan_reaper.py": "Qdrant cortex-collection reaper (--qdrant-url)",
    "tools/preprod_deploy.py": "Portainer / pre-prod verification CLI (operator config + env)",
    "tools/fltest.py": "cortex + dashboard publish CLI (env URLs)",
    "tools/capture_failure.py": "cortex CLI (CORTEX_RECALL_URL env)",
    "tools/fl_brain_switch.py": "operator CLI against WGM_LLM_BASE_URL (the operator's own env brain)",
    "tools/bf9_litmus.py": "the BUG-FAMILY-9 ON-image door-litmus driver (the pre-prod MCP door — operator infra)",
    "saas/memory_ops.py": "Qdrant only (_qdrant_url())",
    "saas/onboarding.py": "OneTimeSecret share API (OTS_API_URL env) — operator-configured",
}

#: (file, receiver-or-callee, raw url source) → reason. Legacy env-brain seams only.
TOLERATED = {
    ("src/api/llm_source_ip.py", "default_client", "<param url of helper post_sync>"): "cross-module helper: its only callers are llm_calls._post_llm_sync's no-tenant branch (recorded there as TOLERATED)",
    ("src/api/llm_source_ip.py", "default_client", "<param url of helper post_async>"): "same, async",
    ("src/api/llm_source_ip.py", "client", "<param url of helper post_sync>"): "the source-IP-bound twin inside the same helper (LLM_SOURCE_IPS)",
    ("src/api/llm_source_ip.py", "client", "<param url of helper post_async>"): "same, async",
    ("src/re_embedder/embedder.py", "_http_client_sync", "embed_url"): "embed_text's no-tenant branch (elif after the guarded `_byo.post` branch) — EMBEDDING_API_URL / env brain",
    ("src/re_embedder/embedder.py", "_http_client", "embed_url"): "embed_text's last-resort branch, same",
    ("src/wgm/rel_type_inference.py", "client", "<param llm_url of helper infer_rel_type_metadata>"): "NO in-repo caller (dead lane) — route through the guard if ever wired",
    ("src/mcp/server.py", "_client()", "<param url of helper _get>"): "uncalled twin of _post (MCP → FaultLine backend by construction; no caller in the repo) — the shared client now comes through the `_client()` accessor (gauntlet inprocess-mcp-client)",
    ("src/mcp/server.py", "fresh", "<param url of helper _get>"): "same helper, fresh-client branch",
}


_LINES_CACHE = {}


def _src(node, text):
    """Fast source segment (ast.get_source_segment re-splits the whole file per call, which is
    minutes on a 60k-line module)."""
    lines = _LINES_CACHE.get(hash(text))
    if lines is None:
        lines = text.splitlines(keepends=True)
        _LINES_CACHE[hash(text)] = lines
    try:
        l0, c0, l1, c1 = node.lineno - 1, node.col_offset, node.end_lineno - 1, node.end_col_offset
    except AttributeError:
        return ""
    if l0 == l1:
        return lines[l0].encode()[c0:c1].decode(errors="replace")
    parts = [lines[l0].encode()[c0:]] + [ln.encode() for ln in lines[l0 + 1:l1]] + [lines[l1].encode()[:c1]]
    return b"".join(parts).decode(errors="replace")


def _dotted(node):
    """'a.b.c' for Name/Attribute chains, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class _Census(ast.NodeVisitor):
    def __init__(self, rel, text):
        self.rel, self.text = rel, text
        self.mod_alias = {}      # local name → http module ("httpx", "urllib.request", …)
        self.ctor_alias = {}     # local name → "httpx.AsyncClient" etc.
        self.guard_alias = set() # local names of guard factories
        self.client_names = set(KNOWN_CLIENT_NAMES)   # names bound to a plain client
        self.guarded_names = set()                    # names bound to a guarded client
        self.calls = []          # (line, receiver_src, url_src, cls)
        self.scope = {}          # name → RHS source, within the current function
        self.params = set()      # the current function's parameters
        self.func = None
        self.helpers = {}        # function name → param index that is its URL (HTTP helpers)

    # ── imports ───────────────────────────────────────────────────────────────
    def visit_Import(self, node):
        for a in node.names:
            if a.name in HTTP_MODULES or a.name == "urllib":
                self.mod_alias[a.asname or a.name.split(".")[0]] = a.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        mod = node.module or ""
        for a in node.names:
            local = a.asname or a.name
            if mod in HTTP_MODULES and a.name in CLIENT_CTORS:
                self.ctor_alias[local] = f"{mod}.{a.name}"
            elif mod == "urllib" and a.name == "request":
                self.mod_alias[local] = "urllib.request"
            elif mod in HTTP_MODULES:
                self.mod_alias[local] = f"{mod}.{a.name}"
            elif a.name in GUARD_FACTORIES:
                self.guard_alias.add(local)
            elif a.name in KNOWN_CLIENT_NAMES:
                self.client_names.add(local)
        self.generic_visit(node)

    # ── what does an expression build? ───────────────────────────────────────
    def _kind(self, expr):
        """'guarded' | 'client' | None for an expression that may construct a client."""
        if isinstance(expr, ast.BoolOp):          # (_guarded or httpx.Client(...))
            kinds = [self._kind(v) for v in expr.values]
            return "guarded" if "guarded" in kinds else ("client" if "client" in kinds else None)
        if isinstance(expr, ast.Await):
            return self._kind(expr.value)
        if isinstance(expr, ast.Call):
            f = expr.func
            name = _dotted(f) or ""
            last = name.split(".")[-1]
            if last in GUARD_FACTORIES or last in self.guard_alias:
                return "guarded"
            if name in self.ctor_alias:
                return "client"
            root = name.split(".")[0]
            if root in self.mod_alias and last in CLIENT_CTORS:
                return "client"
            if root in self.mod_alias and last == "Request":   # urllib.request.Request(url)
                return "urlreq"
            if ("client" in last.lower() and self.mod_alias        # _sync_client_for(ip) — only in a
                    and not any(x in last.lower() for x in ("redis", "pg", "db"))):  # module that imports httpx
                return "client"
        if isinstance(expr, ast.Name):
            if expr.id in self.guarded_names:
                return "guarded"
            if expr.id in self.client_names:
                return "client"
        if isinstance(expr, ast.Attribute):
            if expr.attr in self.client_names:
                return "client"
        return None

    def _bind(self, target, expr):
        kind = self._kind(expr)
        name = target.id if isinstance(target, ast.Name) else (
            target.attr if isinstance(target, ast.Attribute) else None)
        if not name:
            return
        if kind == "guarded":
            self.guarded_names.add(name); self.client_names.discard(name)
        elif kind == "client":
            self.client_names.add(name); self.guarded_names.discard(name)
        elif kind == "urlreq":
            self.calls.append((expr.lineno, _dotted(expr.func) or "Request",
                               _src(expr.args[0], self.text) if expr.args else "?", None))

    def _enter_func(self, node):
        saved = (self.scope, self.params, self.func)
        self.scope, self.func = {}, node.name
        self.params = {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
        self.generic_visit(node)
        self.scope, self.params, self.func = saved

    visit_FunctionDef = _enter_func
    visit_AsyncFunctionDef = _enter_func

    def _resolve_url(self, url):
        """The URL expression's source, plus the RHS of every simple name it uses (one hop),
        so ``f"{base}/x"`` with ``base = _qdrant_url()`` classifies by what ``base`` IS."""
        if url is None:
            return "?", None
        src = _src(url, self.text)
        names = [n.id for n in ast.walk(url) if isinstance(n, ast.Name)]
        extra = [f"{n}={self.scope[n]}" for n in names if n in self.scope]
        param = next((n for n in names if n in self.params and n not in self.scope), None)
        return (src + (" ← " + "; ".join(extra) if extra else "")), param

    def visit_Assign(self, node):
        for t in node.targets:
            self._bind(t, node.value)
            if isinstance(t, ast.Name):
                self.scope[t.id] = _src(node.value, self.text)[:160]
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self._bind(node.target, node.value)
        self.generic_visit(node)

    def _with(self, node):
        for item in node.items:
            if item.optional_vars is not None:
                self._bind(item.optional_vars, item.context_expr)
        self.generic_visit(node)

    visit_With = _with
    visit_AsyncWith = _with

    # ── the request calls ─────────────────────────────────────────────────────
    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in REQUEST_METHODS:
            recv = f.value
            recv_name = _dotted(recv) or _src(recv, self.text)
            root = (recv_name or "").split(".")[0]
            kind = None
            if root in self.mod_alias and f.attr != "build_request":
                kind = "module"                      # httpx.post(url) / urllib.request.urlopen
            elif isinstance(recv, ast.Name) and recv.id in self.guarded_names:
                kind = "guarded"
            elif isinstance(recv, ast.Name) and recv.id in self.client_names:
                kind = "client"
            elif isinstance(recv, ast.Name) and recv.id in self.params and "client" in recv.id:
                kind = "client"        # a client PASSED IN (llm_source_ip.post_sync(default_client, …))
            elif isinstance(recv, ast.Attribute) and recv.attr in self.client_names:
                kind = "client"
            elif isinstance(recv, ast.Call):        # httpx.AsyncClient().post(url)
                k = self._kind(recv)
                kind = {"guarded": "guarded", "client": "client"}.get(k)
            elif _dotted(f) in ("llm_source_ip.post_sync", "llm_source_ip.post_async"):
                kind = "client"
            if kind:
                url = None
                if node.args:
                    url = node.args[0]
                for kw in node.keywords:
                    if kw.arg == "url":
                        url = kw.value
                if _dotted(f) in ("llm_source_ip.post_sync", "llm_source_ip.post_async"):
                    url = node.args[1] if len(node.args) > 1 else None   # (default_client, url, …)
                if f.attr in ("stream", "request", "build_request"):      # (method, url, …)
                    url = node.args[1] if len(node.args) > 1 else url
                self._record(node.lineno, recv_name, url, kind)
        # the guarded engine seams: _post_llm_sync(default_client, url, …)
        fname = _dotted(f) or ""
        if fname.split(".")[-1] in ("_post_llm_sync", "_post_llm_async"):
            self._record(node.lineno, fname, node.args[1] if len(node.args) > 1 else None, "guarded")
        # calls to a helper this module already proved is an HTTP helper (pass 2 re-walks)
        if self.helpers.get(fname) is not None:
            i, pname = self.helpers[fname]
            url = next((kw.value for kw in node.keywords if kw.arg == pname), None)
            if url is None and len(node.args) > i:
                url = node.args[i]
            self._record(node.lineno, fname, url, "helper")
        self.generic_visit(node)

    def _record(self, line, recv, url, kind):
        url_src, param = self._resolve_url(url)
        operator_shaped = any(m in url_src for m in OPERATOR_URL_MARKERS)
        if param is not None and self.func and kind != "guarded" and not operator_shaped:
            # the URL is a PARAMETER of the enclosing function → it is an HTTP helper; its
            # CALLERS are the real request sites (pass 2).
            fn = self.func
            self.helpers.setdefault(fn, None)
            self.calls.append((line, recv, f"<param {param} of helper {fn}>", "helper-def"))
            return
        self.calls.append((line, recv, url_src, kind))


def _classify(rel, line, recv, url, kind):
    if kind == "guarded":
        return "GUARDED", ""
    if kind == "helper-def":
        return "HELPER", ""      # judged at its call sites (in this module)
    if kind == "helper-uncalled":
        # a URL-parameter helper with NO caller in its module: its callers are elsewhere (or
        # nowhere) and cannot be judged here — must be named in TOLERATED with the cross-module
        # caller that judges it, or as a dead lane.
        key = (rel, recv, url)
        if key in TOLERATED:
            return "TOLERATED", TOLERATED[key]
        return "UNGUARDED", f"{rel}:{line}: {recv} in URL-parameter helper `{url}` has no caller in this module — name its caller (TOLERATED) or route it"
    if kind in ("client", "module", "helper", None):
        if rel in OPERATOR_FILES:
            return "OPERATOR", OPERATOR_FILES[rel]
        if any(m in url for m in OPERATOR_URL_MARKERS):
            return "OPERATOR", ""
        key = (rel, recv, url.split(" ← ")[0])
        if key in TOLERATED:
            return "TOLERATED", TOLERATED[key]
        # send(request)/stream on a NON-guarded client: the URL lives in the request object.
        return "UNGUARDED", f"{rel}:{line}: {recv}(...) with URL `{url}` rides an unguarded client"
    return "UNKNOWN", f"{rel}:{line}: {recv}"


def census_file(rel, text):
    tree = ast.parse(text)
    c = _Census(rel, text)
    c.visit(tree)
    if c.helpers:
        # pass 2: the helper's URL-parameter index, then re-walk so calls to it are recorded
        for fn in list(c.helpers):
            node = next(n for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn)
            args = [a.arg for a in node.args.posonlyargs + node.args.args]
            row = next(r for r in c.calls if r[3] == "helper-def" and f"helper {fn}>" in r[2])
            pname = row[2].split("<param ")[1].split(" of")[0]
            c.helpers[fn] = (args.index(pname) if pname in args else 0, pname)
        c.calls = [r for r in c.calls if r[3] == "helper-def"]
        c.visit(tree)
        called = {r[1] for r in c.calls if r[3] == "helper"}
        c.calls = [(ln, recv, url, ("helper-uncalled" if (kind == "helper-def" and
                                                          url.split("helper ")[1].rstrip(">") not in called)
                                    else kind))
                   for ln, recv, url, kind in c.calls]
    seen, rows = set(), []
    for ln, recv, url, kind in c.calls:
        if (ln, recv, url) in seen:
            continue
        seen.add((ln, recv, url))
        rows.append((ln, recv, url, kind, *_classify(rel, ln, recv, url, kind)))
    return rows


def _all_files():
    for scope in SCOPES:
        for f in sorted((ROOT / scope).rglob("*.py")):
            p = f.as_posix()
            if "/tests/" in p or "/.codeql/" in p or "/FaultLine/" in p:
                continue
            yield f.relative_to(ROOT).as_posix(), f.read_text(encoding="utf-8", errors="replace")


ALL = [(rel, row) for rel, text in _all_files() for row in census_file(rel, text)]


def test_census_saw_the_known_consumers():
    """Sanity: the census must SEE the sites this gauntlet is about (a silent AST miss would
    read as green). Both the tenant seams and an operator seam."""
    seen = {(rel, row[1]) for rel, row in ALL}
    for want in [("src/api/main.py", "_post_llm_sync"), ("saas/llm_config.py", "client"),
                 ("saas/agent_proxy.py", "client"), ("saas/agent_gauntlet.py", "client"),
                 ("src/api/llm_calls.py", "llm_source_ip"),
                 ("src/mcp/server.py", "_brain_request"), ("src/api/cortex.py", "httpx"),
                 ("src/mcp/server.py", "fetcher")]:
        assert want in seen, want


@pytest.mark.parametrize("entry", ALL, ids=lambda e: f"{e[0]}:{e[1][0]}")
def test_every_request_site_is_guarded_or_operator(entry):
    rel, (ln, recv, url, kind, cls, why) = entry
    assert cls in ("GUARDED", "OPERATOR", "TOLERATED", "HELPER"), why


def test_tolerated_rows_all_exist():
    keys = {(rel, row[1], row[2].split(" ← ")[0]) for rel, row in ALL}
    for k in TOLERATED:
        assert k in keys, f"stale TOLERATED row: {k}"


def test_plants_are_flagged(tmp_path):
    """The critic's three plant shapes must each be caught."""
    plant = '''
import httpx as _h
from httpx import AsyncClient as C
from src.api.llm_calls import _llm_http_client

def a(url):
    return _h.post(url, json={})                      # module-level, aliased module

async def b(url):
    async with C(timeout=3) as cl:                    # aliased constructor
        return await cl.post(url, json={})

def c(url):
    return _llm_http_client.post(url, json={})        # shared-client reuse

def d(url):
    client = _h.Client()
    with client as k:
        return k.stream("POST", url)                  # helper reuse via with-as
'''
    rows = census_file("plant.py", plant)
    flagged = [r for r in rows if r[4] == "UNGUARDED"]
    assert len(flagged) == 4, rows
    # …and the same four shapes with the URL built INSIDE (no parameter) are flagged as sites
    inline = plant.replace("(url)", "()").replace("url,", 'f"https://{host}/v1",').replace("url)", 'f"https://{host}/v1")')
    rows2 = census_file("plant2.py", inline)
    assert len([r for r in rows2 if r[4] == "UNGUARDED"]) == 4, rows2
    ok = census_file("ok.py", '''
from saas.llm_config import guarded_client
import httpx
def a(url):
    with guarded_client(timeout=1) as g:
        return g.post(url)
def b():
    return httpx.get(f"{_qdrant_url()}/collections")
''')
    assert [r[4] for r in ok] == ["GUARDED", "OPERATOR"], ok
