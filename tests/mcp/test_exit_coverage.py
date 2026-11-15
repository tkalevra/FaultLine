"""No status-emitting tool exit may go undriven — enforced by measurement, not by listing.

WHY THIS FILE EXISTS
    Four review rounds ran the same loop: a reviewer renamed one exit's status to a success
    word, the whole suite stayed green, the exit got a test, and the next reviewer found
    another one. Rounds 13, 14 and 15 each ended that way. The last verdict named the pattern
    exactly — the fix had been applied "only to the exits the reviewer happened to name", so
    the identical defect was still live five exits over.

    Fixing instance number six would repeat the loop. What was missing is a check on the CLASS:
    every place in src/mcp that answers a caller with a `status` must be EXECUTED by some test,
    because an exit no test executes is an exit whose verdict nothing can notice being changed.

    So this measures. It runs the suite under coverage in a child process and fails if any
    `{"status": ...}` site in src/mcp went unreached. A new exit added tomorrow fails here until
    someone drives it — which is the property the previous four rounds never had.

    The child is a full suite run, so this file is the slow one in the battery (~40s). That is
    the price of the guarantee being measured rather than asserted.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
MCP = REPO / "src" / "mcp"

# Exits that are deliberately NOT required to be driven, each with the reason it is exempt.
# An entry here is a decision on the record, not a silence: the point of the registry is that
# skipping an exit costs someone a written justification.
# Exits deliberately NOT required to be driven. KEYED PER STATUS — `module.py::function::status`
# — never per function.
#
# That granularity is the fix for a hole a reviewer walked straight through: when keys were
# per-FUNCTION, the single line
#     "server.py::_remember_facts_tool_impl": "entitlement-layer refusal, not a tool result"
# silenced the entire primary write tool — nine real exits (`rejected`, `no_ingest`, `degraded`,
# `stored`, …) plus a freshly injected undriven one — and both guard tests still passed. An
# escape hatch that can swallow a whole tool in one line is not an exemption list, it is an
# off switch. Silencing a tool now costs one visible line per status, each with its own reason.
EXEMPT: dict[str, str] = {
    # ── SaaS control plane ────────────────────────────────────────────────────────────────
    # Refusals issued by the ENTITLEMENT layer before a tool runs. They are now routed through
    # `_control_plane_envelope` on both HTTP doors, so their VERDICT is covered; these entries
    # only record that the payload-builders themselves are not driven on this bench, where
    # SAAS_MODE is off. (`_saas_quota_block` builds its payload in `saas/metering.py`, outside
    # this scan's directory — noted here because an earlier version of this comment wrongly
    # claimed it built no status literal at all.)
    "http_server.py::_saas_api_addon_block::api_not_enabled": "SaaS entitlement refusal; SAAS_MODE off on this bench",
    "http_server.py::_saas_api_addon_block::seat_api_not_enabled": "SaaS entitlement refusal; SAAS_MODE off on this bench",
    "http_server.py::_saas_patient_provisioning_block::patient_lane_disabled": "SaaS clinical-lane refusal; SAAS_MODE off on this bench",
    "http_server.py::_saas_patient_provisioning_block::api_not_enabled": "SaaS clinical-lane refusal; SAAS_MODE off on this bench",
    "http_server.py::_saas_patient_provisioning_block::insufficient_scope": "SaaS clinical-lane refusal; SAAS_MODE off on this bench",
    # ── Liveness ─────────────────────────────────────────────────────────────────────────
    # `{"status": "ok"}` from a health endpoint is not a tool verdict; no caller branches on it
    # to decide whether their memory was written.
    "http_server.py::health::ok": "health probe, not a tool result",
    # `_offline_response(request, message)` returns a 503 maintenance PAGE or JSON for the
    # SaaS dashboard when the whole service is blocked. It is not a tool result — no tool
    # dispatches through it, and it takes a Request rather than returning a tool envelope.
    "http_server.py::_offline_response::offline": "service-wide 503 maintenance response, not a tool result",
    "api_v1.py::v1_health::ok": "health probe, not a tool result",
}

# WHICH FUNCTIONS MAY BE EXEMPTED AT ALL — a WHITELIST, deliberately, because the blacklist
# that stood here was walked around in one line.
#
# The previous version listed nine tool-answering functions and refused those. A reviewer
# simply exempted a tenth — `http_server.py::rest_ingest_file::rejected` — and turned a genuine
# RED green. A blacklist has to anticipate every name; a whitelist only has to name the handful
# of places that genuinely do not answer a tool call, and anything new is refused by default.
#
# These four are it: two health probes, the entitlement layer (refusals issued BEFORE any tool
# runs, whose verdicts are covered separately by `_control_plane_envelope`), and the
# service-wide maintenance response. Everything else in src/mcp that emits a status is
# answering somebody's tool call and must be driven.
EXEMPTABLE_FUNCTIONS = ("health", "v1_health", "_offline_response",
                        "_saas_api_addon_block", "_saas_patient_provisioning_block")


def _exempt_statuses(key: str) -> set:
    """The statuses EXEMPT lists for one module::function site key."""
    return {k.split("::", 2)[2] for k in EXEMPT if k.startswith(key + "::")}


def _status_builders(tree) -> set:
    """Functions whose FIRST parameter is named `status` — i.e. status-builders.

    `ingest_document_tool` constructs every one of its exits through a local
    `_summary(status, **overrides)` helper, so none of them is a `{"status": "<literal>"}`
    dict and the literal-only scan could not see ANY of its eight exits — including the
    partial-acceptance branch that was the previous round's worst finding. Detecting builders
    by their signature rather than by name keeps this true for the next one somebody writes.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args
            if args and args[0].arg == "status":
                names.add(node.name)
    return names


def _fstring_marker(node: ast.JoinedStr) -> str | None:
    """A stable marker for an f-string status, from its constant segments.

    `f"chunk_{i}_done"` is a status this scan must SEE even though no literal resolves it;
    the constant segments joined (prefixed `f:`) are a stable key. The dynamic parts are
    deliberately dropped — the key exists so the site is COUNTED and can be driven or
    exempted, not so the exact runtime word is predicted.
    """
    parts = [v.value for v in node.values if isinstance(v, ast.Constant)
             and isinstance(v.value, str)]
    if not parts:
        return None
    return "f:" + "".join(parts)


def _local_literal_bindings(tree, parent) -> dict:
    """Per-FUNCTION local bindings of names to status literals, nearest-scope.

    The merged tree moved several status exits into NESTED CLOSURES (_walk, _cortex_lane,
    _ingest_task bodies) whose status is held in a LOCAL VARIABLE first (`_st = "ok"` …
    `{"status": _st}`). A literal-only scan records zero of them, which is exactly the
    "remaining way to add an exit the guard cannot see" this extension closes. Bindings are
    keyed by the NEAREST enclosing function of the ASSIGNMENT (not the outer tool), so a
    closure's locals do not leak into the enclosing tool's namespace. A name bound to more
    than one literal (a conditional assignment) carries ALL of them; over-recording a
    possible value is safe here — it can only add a site this guard then asks to be driven.
    """
    by_func: dict[str, dict[str, list[str]]] = {}

    def nearest_func(node):
        p = parent.get(node)
        while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            p = parent.get(p)
        return p.name if p is not None else None

    def values_of(expr):
        """Every literal string `expr` can denote (constant, f-string marker, or either arm
        of a conditional expression)."""
        out = []
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            out.append(expr.value)
        elif isinstance(expr, ast.JoinedStr):
            m = _fstring_marker(expr)
            if m:
                out.append(m)
        elif isinstance(expr, ast.IfExp):
            out.extend(values_of(expr.body))
            out.extend(values_of(expr.orelse))
        return out

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        fn = nearest_func(node)
        if fn is None:
            continue
        if isinstance(node, ast.Assign):
            pairs = [(t, node.value) for t in node.targets if isinstance(t, ast.Name)]
        else:
            pairs = ([(node.target, node.value)]
                     if isinstance(node.target, ast.Name) and node.value is not None else [])
        for target, value in pairs:
            vals = values_of(value)
            if vals:
                by_func.setdefault(fn, {}).setdefault(target.id, []).extend(vals)
    return by_func


def _exit_sites() -> dict:
    """Every place src/mcp answers a caller with a status, keyed module::function -> {lines}.

    Six shapes, because a scan that sees only some of them is how four rounds of reviewers
    each found an exit it could not see:
      * `{"status": "<literal>"}`      — a dict literal
      * `status="<literal>"`           — a keyword argument
      * `_summary("<literal>", ...)`   — a call to a status-builder (first parameter `status`)
      * `{"status": _MODULE_CONST}`    — a module-level string constant (the freeze-switch
                                         house style)
      * `{"status": _local}`           — a LOCAL variable bound to a literal or f-string in
                                         the same (possibly nested-closure) function — the
                                         form the merged tree's nested closures use
      * `status=f"..."` / ternaries    — an f-string status, or either arm of a conditional
                                         expression, recorded per arm so each is drivable
    """
    sites: dict = {}
    for path in sorted(MCP.glob("*.py")):
        tree = ast.parse(path.read_text())
        builders = _status_builders(tree)
        # Module-level `NAME = "literal"` bindings. The codebase's own house style for several
        # exits is `{"status": _INGEST_DISABLED_STATUS, ...}` — eleven live exits that a
        # literal-only scan recorded zero times. Resolving the constant is not an edge case;
        # it is how this repository actually writes a status.
        consts = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        consts[t.id] = node.value.value

        parent = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        locals_by_func = _local_literal_bindings(tree, parent)

        def enclosing(node):
            p = parent.get(node)
            while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                p = parent.get(p)
            return p.name if p is not None else "<module>"

        def literals_of(node, func_name):
            """Yield (anchor, value) for every status literal this node can denote."""
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield (node, node.value)
            elif isinstance(node, ast.Name):
                if node.id in consts:
                    yield (node, consts[node.id])
                for v in locals_by_func.get(func_name, {}).get(node.id, []):
                    yield (node, v)
            elif isinstance(node, ast.JoinedStr):
                m = _fstring_marker(node)
                if m:
                    yield (node, m)
            elif isinstance(node, ast.IfExp):
                yield from literals_of(node.body, func_name)
                yield from literals_of(node.orelse, func_name)

        def stmt_span(node):
            """The line SPAN of the enclosing statement, and why a span rather than a line.

            Coverage tracks STATEMENTS, but CPython 3.11 attributes a multi-line
            `return _tool_response({` ... `})` unevenly: the FIRST line can be reported
            missing while its continuation lines are executed (measured live on pre-prod,
            2026-08-19: lines 2667/2707/2741 of http_server.py "missing" while 2668/2708/2742
            — the dict bodies of the very same returns — executed). Anchoring to one line
            made the guard report driven exits as undriven. A statement is executed when ANY
            of its lines is, so the site records the whole span and the check asks whether
            every line of the span is missing.
            """
            cur = node
            while cur is not None and not isinstance(cur, ast.stmt):
                cur = parent.get(cur)
            if cur is None:
                lo = getattr(node, "lineno", 0)
                return lo, lo
            lo = getattr(cur, "lineno", getattr(node, "lineno", 0))
            hi = getattr(cur, "end_lineno", lo) or lo
            return lo, hi

        def record_all(node, value_node):
            fn = enclosing(node)
            lo, hi = stmt_span(value_node)
            span = f"{lo}" if lo == hi else f"{lo}-{hi}"
            for anchor, lit in literals_of(value_node, fn):
                key = f"{path.name}::{fn}"
                sites.setdefault(key, set()).add(f"{span}:{lit}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "status":
                        record_all(node, v)
            elif isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name in builders and node.args:
                    record_all(node, node.args[0])
                for kw in node.keywords:
                    if kw.arg == "status":
                        record_all(node, kw.value)
    return sites


@pytest.mark.skipif(os.environ.get("EXIT_COVERAGE_CHILD") == "1",
                    reason="inner run: this test spawns the suite and must not recurse")
def test_every_status_exit_is_executed_by_some_test(tmp_path):
    pytest.importorskip("coverage")
    data_file = tmp_path / ".coverage"
    env = {**os.environ, "EXIT_COVERAGE_CHILD": "1", "COVERAGE_FILE": str(data_file)}
    env.setdefault("SPACY_MODEL", "en_core_web_sm")
    child = subprocess.run(
        [sys.executable, "-m", "coverage", "run", f"--source={MCP}", "-m",
         "pytest", "-q", str(REPO / "tests" / "mcp"), "-p", "no:cacheprovider"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=1800,
    )
    assert child.returncode == 0, (
        "the inner suite must pass before its coverage means anything:\n"
        + child.stdout[-3000:] + child.stderr[-2000:])

    report = tmp_path / "cov.json"
    dump = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", str(report), "-q"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    assert report.exists(), f"coverage produced no report: {dump.stdout}{dump.stderr}"
    files = json.loads(report.read_text())["files"]
    missing_by_file = {
        pathlib.Path(name).name: set(info["missing_lines"]) for name, info in files.items()
    }

    undriven: dict[str, list[str]] = {}
    for key, entries in _exit_sites().items():
        module = key.split("::", 1)[0]
        missing = missing_by_file.get(module, set())
        # An exit may be recorded at several statements; it is DRIVEN if any recording's
        # statement executed (any line of its span ran), and undriven only when EVERY
        # recording is fully missing. Entries carry a line SPAN ("lo" or "lo-hi") because
        # CPython 3.11 reports a multi-line return's first line missing while its
        # continuation lines execute (see stmt_span).
        by_status: dict[str, list[bool]] = {}
        for entry in sorted(entries):
            span, _, status = entry.partition(":")
            lo, _, hi = span.partition("-")
            lines = range(int(lo), int(hi or lo) + 1)
            by_status.setdefault(status, []).append(all(l in missing for l in lines))
        dead = [f"{key}::{status}" for status, deads in sorted(by_status.items())
                if status not in _exempt_statuses(key) and all(deads)]
        if dead:
            undriven[key] = dead

    assert undriven == {}, (
        "these exits answer a caller with a status that NO test executes, so nothing would "
        "notice the verdict being reworded into a success — the defect four review rounds "
        "found one instance at a time:\n"
        + "\n".join(f"  {k}\n      " + ", ".join(v) for k, v in sorted(undriven.items()))
        + "\n\nDrive each one, or add it to EXEMPT in this file with the reason it is not a "
          "tool verdict.")


def test_the_exemptions_are_all_real_and_none_are_stale():
    """An EXEMPT entry naming an exit that no longer exists is a silence nobody is watching."""
    real = {f"{key}::{entry.partition(':')[2]}"
            for key, entries in _exit_sites().items() for entry in entries}
    stale = sorted(k for k in EXEMPT if k not in real)
    assert stale == [], f"EXEMPT names exits that no longer exist: {stale}"
    unreasoned = sorted(k for k, why in EXEMPT.items() if len(why.strip()) < 15)
    assert unreasoned == [], f"EXEMPT entries without a real reason: {unreasoned}"


def test_only_whitelisted_functions_can_be_exempted_at_all():
    """The escape hatch, closed properly this time.

    Round 16 silenced the entire primary write tool with one EXEMPT line. Round 17 walked
    around the nine-name blacklist that replaced it by exempting a tenth function. So the rule
    is inverted: an exemption is refused unless its function is one of the few that genuinely
    does not answer a tool call. New code cannot quietly become exemptible.
    """
    offenders = sorted(k for k in EXEMPT if k.split("::")[1] not in EXEMPTABLE_FUNCTIONS)
    assert offenders == [], (
        f"these EXEMPT entries name functions that are not on the whitelist: {offenders}. "
        f"Drive the exit instead — hiding a tool's verdict is the defect this file exists to "
        f"catch. If the function genuinely does not answer a tool call, add it to "
        f"EXEMPTABLE_FUNCTIONS with the reasoning, where it will be reviewed.")
