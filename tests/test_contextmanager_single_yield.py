"""A @contextmanager generator must yield exactly once per execution path.

Regression pin for 654d9382, which duplicated `yield conn` in src/api/cortex.py::_cortex_conn
while adding a nosemgrep comment on the line above it. contextlib resumes the generator at
__exit__ and expects StopIteration; a second yield raises RuntimeError("generator didn't stop")
AFTER the body ran — a write commits, then the caller's except path fires and everything after
the `with` is skipped. Measured live on pre-prod 2026-09-14.

Two legs: (1) a static census over src/ + saas/ + tools/ — every @contextmanager whose yields are
not on mutually exclusive branches is a defect; (2) a driven leg — _cortex_conn with a stubbed
psycopg2 connection completes a no-op body without raising.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("src", "saas", "tools")


def _yields_on_one_path(fn: ast.AST) -> bool:
    """True when every execution path through the function body reaches at most ONE yield.

    Path semantics, not a flat count: `if` / `try` branches are alternatives, a branch that ends
    in `return` terminates its path (so a yield after the `if` is NOT on that path), and
    with/for/while bodies are on the enclosing path. Nested defs don't share the frame.
    """
    def is_yield_stmt(st) -> bool:
        return isinstance(st, ast.Expr) and isinstance(st.value, (ast.Yield, ast.YieldFrom))

    def paths(stmts) -> list[tuple[int, bool]]:
        """All (yields, terminated) outcomes of executing `stmts` in order."""
        outcomes = [(0, False)]
        for st in stmts:
            nxt = []
            for y, term in outcomes:
                if term:
                    nxt.append((y, True))
                    continue
                for sy, sterm in stmt_paths(st):
                    nxt.append((y + sy, sterm))
            outcomes = nxt
        return outcomes

    def stmt_paths(st) -> list[tuple[int, bool]]:
        if is_yield_stmt(st):
            return [(1, False)]
        if isinstance(st, ast.Return):
            return [(0, True)]
        if isinstance(st, ast.If):
            return paths(st.body) + paths(st.orelse)
        if isinstance(st, ast.Try):
            outs = []
            for by, bterm in paths(st.body):
                for oy, oterm in ([(0, False)] if bterm else paths(st.orelse)):
                    for fy, fterm in paths(st.finalbody):
                        outs.append((by + oy + fy, bterm or oterm or fterm))
            for h in st.handlers:
                for hy, hterm in paths(h.body):
                    for fy, fterm in paths(st.finalbody):
                        outs.append((hy + fy, hterm or fterm))
            return outs
        if isinstance(st, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
            outs = []
            for by, bterm in paths(st.body):
                for oy, oterm in ([(0, False)] if bterm else paths(getattr(st, "orelse", []))):
                    outs.append((by + oy, bterm or oterm))
            return outs
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return [(0, False)]
        n = sum(1 for x in ast.walk(st) if isinstance(x, (ast.Yield, ast.YieldFrom)))
        return [(n, False)]

    outcomes = [y for y, _ in paths(fn.body)]
    # exactly one: a path with ZERO yields means contextlib gets a plain function/None back
    # ("'NoneType' object is not an iterator" — db_read.read_only_connection, 654d9382), a path
    # with TWO means "generator didn't stop" (cortex._cortex_conn, same commit).
    return max(outcomes) <= 1 and max(outcomes) >= 1


def _contextmanagers():
    for root in SCAN_ROOTS:
        for p in (REPO / root).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any("contextmanager" in ast.unparse(d) for d in fn.decorator_list):
                    yield p.relative_to(REPO), fn


def test_every_contextmanager_yields_once_per_path():
    bad = [f"{p}::{fn.name}" for p, fn in _contextmanagers() if not _yields_on_one_path(fn)]
    assert not bad, f"@contextmanager not yielding exactly once per path (0 → NoneType is not an iterator; 2 → generator didn't stop): {bad}"


def test_census_is_not_vacuous():
    names = {fn.name for _, fn in _contextmanagers()}
    assert "_cortex_conn" in names, "census must see src/api/cortex.py::_cortex_conn"


def test_cortex_conn_completes_noop_body(monkeypatch):
    sys.path.insert(0, str(REPO))
    from src.api import cortex

    class _Cur:
        executed: list = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a):
            _Cur.executed.append(sql)

    class _Conn:
        closed = False

        def cursor(self):
            return _Cur()

        def close(self):
            _Conn.closed = True

    fake = types.SimpleNamespace(connect=lambda dsn: _Conn())
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setattr(cortex, "_dsn", lambda: "postgresql://x")
    with cortex._cortex_conn("flagent_0123456789ab_cdef_0123_4567_89abcdef0123") as conn:
        assert isinstance(conn, _Conn)
    assert any("SET search_path TO flagent_" in s for s in _Cur.executed)
    assert _Conn.closed is True
