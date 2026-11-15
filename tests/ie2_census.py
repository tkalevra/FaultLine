"""AST census (IE2): no expression rooted in a caught exception reaches a response — or a ROW a
response later renders (round 3: persisted-error sinks), or a StreamingResponse chunk.

Sinks: every ``return`` value (in ANY function — a helper's except-dict is a body the moment a
route returns it), every ``HTTPException(...)``, every ``*Response(...)`` / ``_jsonrpc_error(...)``
constructor. Multi-line constructors and ``error=`` / ``detail=`` / ``message=`` kwargs are just
AST nodes here, so they are covered.

Sources: a name bound by ``except … as e`` — tainted INSIDE that handler's body only (Python
deletes the binding at handler exit, so an ``e`` elsewhere is another variable); any name
ASSIGNED inside the handler from a tainted expression stays tainted for the rest of the
function (``last_detail = f"…{exc}"``, ``out["error"] = str(exc)``, ``report.update(reason=…)``);
``exc.args`` / ``exc.__traceback__`` / ``traceback.format_exc()`` / ``sys.exc_info()``.

Sanitizers (each named because it is a redactor or returns no exception text): the seam
(``public_error`` / ``public_detail`` / ``provider_detail``), the redactors the codebase already
declares (``brain_fault._redact`` / ``brain_fault.describe`` / ``payments_stripe._secret_free_reason``),
``llm_config._models_error`` (type name only), ``type(exc)`` / ``isinstance`` / ``hasattr``, and
attribute reads of authored fields (``.public_message``, ``.status_code``, ``.pgcode``, our typed
exceptions' ``.message`` / ``.cause`` / ``.endpoint`` / ``.as_fields()``).
"""
from __future__ import annotations
import ast, pathlib, sys

# TEXT REDACTORS — the only functions the census trusts with an already-FORMATTED exception
# string. Each is pinned table-driven in tests/test_no_exception_text_in_responses.py
# (``test_every_census_sanitizer_strips_credentials``): driven with a string carrying a canary
# key, a flk_/flu_ token, an `api_key=…` label and a DSN password, none survives.
# ⚠️ ROUND-4 CORRECTION: this set used to also list ``public_detail`` / ``provider_detail`` /
# ``describe`` / ``_models_error`` / ``_secret_free_reason`` / ``_brain_unavailable_after_intent``
# as "sanitizers" — i.e. any call receiving the exception was trusted by NAME. Passing the
# exception OBJECT to a function is now modelled as a HANDOFF (see ``_rooted``): the callee's
# own body is censused like any other function, so ``_secret_free_reason`` (which returned
# ``str(exc)[:500]``) is caught in ITS return and needs no allow-list entry. Only functions
# that take TEXT and return TEXT need trusting, and those are the redactors.
SEAM_FUNCS = {"_redact", "redact_line", "describe", "redact_text"}   # redact_value is shape-only (no key rule): not a text seam
# Formatting builtins: passing the object to these PRODUCES text — never a handoff.
TEXT_BUILTINS = {"str", "repr", "format", "ascii", "bytes", "print"}
# ``response`` / ``request`` are NOT safe (round 4): ``exc.response.text`` is the PROVIDER's
# body — the text class ``describe`` / ``redact_text`` exist to launder.
SAFE_EXC_ATTRS = {"status_code", "pgcode", "public_message", "__class__", "code", "errno",
                  "retry_after", "headers",
                  "message", "cause", "endpoint", "attempts", "as_fields"}
TEXT_EXC_ATTRS = {"args", "__traceback__", "__cause__", "__context__", "__notes__"}
SAFE_CALLS = {"type", "isinstance", "hasattr", "id", "len", "bool", "issubclass"}
# BrainUnavailable: its ``cause`` constructor argument is rendered into the MCP tool result by
# ``as_fields()`` (the loud degraded status), so constructing it IS a body write.
SINK_CALL_NAMES = {"HTTPException", "_jsonrpc_error", "BrainUnavailable"}


def _name_of(func):
    if isinstance(func, ast.Name): return func.id
    if isinstance(func, ast.Attribute): return func.attr
    return None


def _walk_own(node):
    """Children of `node` without descending into nested def/lambda/class bodies."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _rooted(expr, tainted, objects=frozenset()):
    """True if `expr` carries a tainted name outside a sanitizing context. ``objects`` = names
    holding the exception OBJECT unformatted (handoff-eligible as bare call arguments)."""
    class V(ast.NodeVisitor):
        def __init__(self, tainted): self.hit = False; self.t = set(tainted); self.objects = set(objects)
        def visit_Call(self, n):
            fname = _name_of(n.func)
            if fname in SEAM_FUNCS or fname in SAFE_CALLS:
                return
            # getattr(exc, "<literal>") is an attribute read — judged by the SAME attribute rule
            if fname == "getattr" and len(n.args) >= 2 and isinstance(n.args[0], ast.Name) \
                    and n.args[0].id in self.t and isinstance(n.args[1], ast.Constant):
                attr = str(n.args[1].value)
                if attr in TEXT_EXC_ATTRS or attr not in SAFE_EXC_ATTRS: self.hit = True
                for extra in n.args[2:]: self.visit(extra)
                return
            # getattr(<exc.response>, "status_code"/…) — the same response-metadata rule
            if fname == "getattr" and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant):
                base = n.args[0]
                is_resp = (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
                           and base.value.id in self.t and base.attr in ("response", "request")) or \
                          (isinstance(base, ast.Call) and _name_of(base.func) == "getattr" and len(base.args) >= 2
                           and isinstance(base.args[0], ast.Name) and base.args[0].id in self.t
                           and isinstance(base.args[1], ast.Constant) and base.args[1].value in ("response", "request"))
                if is_resp:
                    if str(n.args[1].value) not in ("status_code", "headers", "reason_phrase", "url", "method"):
                        self.hit = True
                    for extra in n.args[2:]: self.visit(extra)
                    return
            if fname in TEXT_BUILTINS or isinstance(n.func, ast.Attribute) and n.func.attr in ("format", "join"):
                self.generic_visit(n); return
            # A bare exception OBJECT (or a bare object alias) as a call argument is a HANDOFF:
            # the callee is censused on its own — UNLESS the callee is a module-local FORMATTER
            # (its parameter's text reaches a body sink), in which case this call IS text.
            fmt = _FORMATTER_CACHE.get(fname, set())
            self.visit(n.func)
            for i, a in enumerate(n.args):
                if isinstance(a, ast.Name) and a.id in self.objects:
                    if i in fmt: self.hit = True
                    continue
                self.visit(a)
            for k in n.keywords:
                if isinstance(k.value, ast.Name) and k.value.id in self.objects:
                    if k.arg in fmt: self.hit = True
                    continue
                self.visit(k.value)
        def visit_Attribute(self, n):
            if isinstance(n.value, ast.Name) and n.value.id in self.t:
                if n.attr in TEXT_EXC_ATTRS: self.hit = True
                elif n.attr in SAFE_EXC_ATTRS: return
                else: self.hit = True
                return
            # ``exc.response.status_code`` / ``.headers`` / ``.reason_phrase``: numbers and
            # transport metadata off the provider response — safe; ``.text`` / ``.json()`` /
            # ``.content`` are the provider's words — text.
            if isinstance(n.value, ast.Attribute) and isinstance(n.value.value, ast.Name) \
                    and n.value.value.id in self.t and n.value.attr in ("response", "request"):
                if n.attr in ("status_code", "headers", "reason_phrase", "url", "method"):
                    return
                self.hit = True
                return
            self.generic_visit(n)
        def _comp(self, n):
            shadow = {t.id for g in n.generators for t in ast.walk(g.target) if isinstance(t, ast.Name)}
            inner = V(self.t - shadow); inner.objects = self.objects
            for g in n.generators:
                inner.visit(g.iter)
                for cond in g.ifs: inner.visit(cond)
            for part in ("elt", "key", "value"):
                if hasattr(n, part): inner.visit(getattr(n, part))
            if inner.hit: self.hit = True
        visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _comp
        def visit_Lambda(self, n):
            return  # a lambda body is evaluated later; its own def is analysed separately
        def visit_IfExp(self, n):
            self.visit(n.body); self.visit(n.orelse)   # a condition yields no text
        def visit_Compare(self, n):
            return                                     # a comparison yields a bool
        def visit_Name(self, n):
            if n.id in self.t: self.hit = True
    v = V(tainted); v.visit(expr); return v.hit


def _tb_call(expr):
    for n in ast.walk(expr):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) \
           and n.func.value.id == "traceback" and n.func.attr.startswith(("format_", "extract_", "print_")):
            return True
        if isinstance(n, ast.Call) and _name_of(n.func) == "exc_info":
            return True
    return False


def _assign_targets(n):
    if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        ts = n.targets if isinstance(n, ast.Assign) else [n.target]
        out = []
        for t in ts:
            if isinstance(t, (ast.Tuple, ast.List)):
                out += [e for e in t.elts]
            else:
                out.append(t)
        return out
    return []


def _base_name(t):
    while isinstance(t, (ast.Subscript, ast.Attribute)): t = t.value
    return t.id if isinstance(t, ast.Name) else None


def _propagate(scope_nodes, tainted, objects=None):
    """One fixpoint pass over assignments / mutating calls in `scope_nodes` (a list of nodes).

    ``objects`` = names holding the exception OBJECT unformatted (the except name and bare
    aliases of it: ``value = e``). Storing a bare object into a container
    (``tally["fatal_error"] = value``) is a handoff, not text — the container is not tainted;
    FORMATTING it anywhere (``str(value)``) taints as usual."""
    if objects is None:
        objects = set()
    changed = True
    while changed:
        changed = False
        for n in scope_nodes:
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value is not None:
                if isinstance(n.value, ast.Name) and n.value.id in objects:
                    for t in _assign_targets(n):
                        if isinstance(t, ast.Name) and t.id not in objects:
                            objects.add(t.id); tainted.add(t.id); changed = True
                    continue
                if isinstance(n.value, ast.Tuple) and all(isinstance(e, (ast.Name, ast.Constant)) for e in n.value.elts) \
                        and any(isinstance(e, ast.Name) and e.id in objects for e in n.value.elts):
                    # ``outcome, value = "failed", e`` — unpack of a bare object
                    for t, v in zip(_assign_targets(n), n.value.elts):
                        if isinstance(t, ast.Name) and isinstance(v, ast.Name) and v.id in objects and t.id not in objects:
                            objects.add(t.id); tainted.add(t.id); changed = True
                    continue
                if _rooted(n.value, tainted, objects) or _tb_call(n.value):
                    for t in _assign_targets(n):
                        b = _base_name(t)
                        if b and b not in tainted: tainted.add(b); changed = True
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and \
                    n.func.attr in ("update", "append", "setdefault", "extend", "insert", "__setitem__"):
                args = list(n.args) + [k.value for k in n.keywords]
                if any(_rooted(a, tainted, objects) or _tb_call(a) for a in args):
                    b = _base_name(n.func.value)
                    if b and b not in tainted: tainted.add(b); changed = True
    return tainted


# Persisted-error sinks (round 3): a DB write whose parameters carry exception text is a body
# the moment a status route renders the row (user_provisioning.error_message, documents.error,
# document_chunks.reason …). Method names of the DB-API / helpers that WRITE.
STORE_CALL_NAMES = {"execute", "executemany", "execute_values", "execute_batch", "mogrify",
                    "copy_from", "insert", "update", "upsert"}
# A generator's `yield` feeding StreamingResponse is a body chunk.


def _is_object_handoff(value, exc_names, tainted, objects=frozenset()):
    """A `return exc` / `return ("failed", exc)` hands the exception OBJECT (unformatted) to the
    caller's protocol — no text is produced here. The consumer must render it through the seam;
    that is pinned by tests on the two consumers (embedder._on_result, cpu_lane's parent
    re-raise). Anything that FORMATS the exception (`str(exc)`, an f-string, `.args`) is a sink."""
    elts = list(value.elts) if isinstance(value, ast.Tuple) else [value]
    rest = [e for e in elts if not (isinstance(e, ast.Name) and e.id in exc_names)]
    return len(rest) < len(elts) and not any(_rooted(e, tainted, objects) or _tb_call(e) for e in rest)


def _sinks(nodes, tainted, exc_names=frozenset(), objects=frozenset(), sink_params=None):
    """``sink_params``: {function name: set of parameter names that reach a DB write inside
    that function} — the ONE-HOP handoff (round 4): calling ``f(e)`` / ``f(reason=str(e))`` where
    ``f``'s parameter is persisted is a store at the CALL site."""
    out = []
    objs = set(objects) | set(exc_names)

    def _arg_hot(a):
        """A sink argument is hot if it is the bare OBJECT itself (text-in-waiting at a body /
        row) or carries formatted exception text; a bare object handed to a nested call
        (``public_detail(e)``) inside the argument is a handoff, judged by the callee."""
        if isinstance(a, ast.Name) and a.id in objs:
            return True
        return _rooted(a, tainted, objs) or _tb_call(a)

    for n in nodes:
        if isinstance(n, ast.Return) and n.value is not None:
            if _rooted(n.value, tainted, objs) or _tb_call(n.value):
                if _is_object_handoff(n.value, exc_names, tainted, objs):
                    continue
                out.append((n.lineno, "return", ast.unparse(n.value)[:110]))
        elif isinstance(n, (ast.Yield, ast.YieldFrom)) and n.value is not None:
            if _rooted(n.value, tainted, objs) or _tb_call(n.value):
                out.append((n.lineno, "yield", ast.unparse(n.value)[:110]))
        elif isinstance(n, ast.Call):
            fname = _name_of(n.func) or ""
            args = list(n.args) + [k.value for k in n.keywords]
            if fname in SINK_CALL_NAMES or fname.endswith("Response"):
                if any(_arg_hot(a) for a in args):
                    out.append((n.lineno, fname, ast.unparse(n)[:110]))
            elif fname in STORE_CALL_NAMES and isinstance(n.func, ast.Attribute):
                # only the PARAMETERS (a tainted SQL string would be a different bug class)
                if any(_arg_hot(a) for a in args[1:] + [k.value for k in n.keywords]):
                    out.append((n.lineno, "store:" + fname, ast.unparse(n)[:110]))
            elif sink_params and fname in sink_params:
                # one hop: the callee persists this parameter
                names = sink_params[fname]
                hit = False
                for k in n.keywords:
                    if k.arg in names and _arg_hot(k.value): hit = True
                for i, a in enumerate(n.args):
                    if i in names and _arg_hot(a): hit = True
                if hit:
                    out.append((n.lineno, "store-via:" + fname, ast.unparse(n)[:110]))
    return out


def _persisting_params(fn):
    """Parameter names/positions of `fn` that flow into a DB-write parameter inside its body
    (intra-function taint from the parameter itself). Feeds the one-hop rule."""
    params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    body = list(_walk_own(fn))
    out = set()
    for i, pname in enumerate(params):
        t = _propagate(body, {pname}, set())
        for n in body:
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in STORE_CALL_NAMES:
                args = list(n.args)[1:] + [k.value for k in n.keywords]
                if any(_rooted(a, t) for a in args):
                    out.add(pname); out.add(i)
            elif isinstance(n, ast.Call) and _name_of(n.func) in _SINK_PARAM_CACHE:
                # a callee that persists (transitively, one more hop): does OUR param reach it?
                names = _SINK_PARAM_CACHE[_name_of(n.func)]
                if any((k.arg in names and _rooted(k.value, t)) for k in n.keywords) or \
                   any((j in names and _rooted(a, t)) for j, a in enumerate(n.args)):
                    out.add(pname); out.add(i)
    return out


_SINK_PARAM_CACHE: dict = {}
#: {function name: set of parameter names/positions whose FORMATTED text reaches a return/yield/
#: Response sink inside that function} — a "formatter". A bare exception handed to a formatter's
#: parameter is NOT a handoff: the call produces exception text (``return h(e)`` where
#: ``def h(e): return {"error": str(e)}``). Module-local, like the persisting table.
_FORMATTER_CACHE: dict = {}


def _formatting_params(fn):
    params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    body = list(_walk_own(fn))
    out = set()
    for i, pname in enumerate(params):
        t = _propagate(body, {pname}, set())
        # the parameter itself is an OBJECT here: only FORMATTED uses count, so pass it as an object
        if _sinks(body, t, frozenset(), {pname}, None):
            out.add(pname); out.add(i)
    return out


def census_function(fn, sink_params=None):
    findings = []
    body_nodes = list(_walk_own(fn))
    handlers = [h for h in body_nodes if isinstance(h, ast.ExceptHandler) and h.name]
    # names that stay tainted after a handler exits (assigned inside from the exception)
    persistent: set = set()
    objects: set = set()
    for h in handlers:
        hnodes = [x for b in h.body for x in [b, *_walk_own(b)]]
        objs = {h.name}
        t = _propagate(hnodes, {h.name} | set(persistent), objs)
        persistent |= (t - {h.name})
        objects |= (objs - {h.name})
    # traceback calls anywhere taint their targets too
    persistent = _propagate(body_nodes, persistent, objects)
    # sinks: inside a handler body the exception name is live; elsewhere only persistent names
    handler_node_ids = set()
    for h in handlers:
        hnodes = [x for b in h.body for x in [b, *_walk_own(b)]]
        handler_node_ids |= {id(x) for x in hnodes}
        for (ln, kind, txt) in _sinks(hnodes, persistent | {h.name}, {h.name}, objects, sink_params):
            findings.append((ln, fn.name, kind, txt))
    outside = [x for x in body_nodes if id(x) not in handler_node_ids]
    for (ln, kind, txt) in _sinks(outside, persistent, frozenset(), objects, sink_params):
        findings.append((ln, fn.name, kind, txt))
    return sorted(set(findings))


def census(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fns = [fn for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))]
    # module-local table of persisting parameters (two passes: _finish → _record_result)
    _SINK_PARAM_CACHE.clear(); _FORMATTER_CACHE.clear()
    for _ in range(2):
        for fn in fns:
            sp = _persisting_params(fn)
            if sp: _SINK_PARAM_CACHE[fn.name] = sp
            fp = _formatting_params(fn)
            if fp: _FORMATTER_CACHE[fn.name] = fp
    sink_params = dict(_SINK_PARAM_CACHE)
    out = []
    for fn in fns:
        out += census_function(fn, sink_params)
    return out


#: Sites the census still reports, each with the reason it is NOT fixed in this lane. The test
#: asserts the census output equals EXACTLY this set: a new site cannot hide behind it and a
#: fixed one must be removed from it.
KNOWN_RESIDUALS = {
    # ``_context_rejection(exc, …)`` reads the PROVIDER body off ``exc.response`` and returns
    # ``{"error": "context_overrun", "message": redact_text(<provider sentence>),
    #   "n_prompt_tokens"/"n_ctx": <ints parsed from that body>}``. Since round 4 the sentence
    # goes through ``src/api/errors.py::redact_text`` (the saas redactor when installed); the
    # integers are numbers. The census's formatter rule is coarse — a parameter whose derived
    # text reaches a return makes the function a formatter — so the two callers that hand
    # ``e`` to it are still reported. Declared, not hidden; the message half is pinned by
    # ``test_every_census_sanitizer_strips_credentials`` (redact_text row).
    ("src/api/llm_calls.py", "call_llm_with_retry_sync", "return", "_ctx_rej"),
    ("src/api/llm_calls.py", "call_llm_with_retry_async", "return", "_ctx_rej"),
}


def census_tree(root):
    files = sorted((root / "saas").glob("*.py")) + sorted((root / "src").rglob("*.py"))
    out = []
    for f in files:
        for (ln, fn, kind, txt) in census(f):
            out.append((f.relative_to(root).as_posix(), ln, fn, kind, txt))
    return out


if __name__ == "__main__":
    root = pathlib.Path(sys.argv[1])
    rows = census_tree(root)
    for (rel, ln, fn, kind, txt) in rows:
        tag = "  (known residual)" if (rel, fn, kind, txt) in KNOWN_RESIDUALS else ""
        print(f"{rel}:{ln} [{fn}] {kind}: {txt}{tag}")
    print("TOTAL", len(rows), "known residuals", sum((r[0], r[2], r[3], r[4]) in KNOWN_RESIDUALS for r in rows))
