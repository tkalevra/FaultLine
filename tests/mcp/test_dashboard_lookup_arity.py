"""Every credential-less request 500'd inside the auth path — a fail-safe defeated by arity.

THE BUG (found on a live FOSS stack, not in review):

    TypeError: _dashboard_lookup() missing 1 required positional argument: 'params'
      http_server.py:231  in _any_dashboard_credential_configured
      http_server.py:268  in _resolve_principal
      http_server.py:289  in require_auth

`_dashboard_lookup(query, params)` declares `params` as a REQUIRED positional. Both probes
inside `_any_dashboard_credential_configured` passed only the query. The callee is written to
degrade gracefully — *"Any error -> None (auth falls back to env); the MCP path never
hard-fails on a DB hiccup"* — but a missing argument raises at the CALL SITE, before the
callee's `try` is ever entered, so that fail-safe could not run.

WHY IT MATTERED SO MUCH: `_resolve_principal` consults it precisely when `credentials is
None`, i.e. the anonymous path. That is the DEFAULT posture of a fresh self-hosted install
with no `MCP_API_KEY` set, so a first-time FOSS user got HTTP 500 on every single call.

WHY REVIEW MISSED IT: the two well-formed call sites (`_resolve_seat_token_db`,
`_mcp_key_active_db`) pass params correctly and sit ~20 lines above, so the shape reads as
right. Only executing the anonymous path reveals it.
"""
import ast
import inspect
from pathlib import Path

from src.mcp import http_server


def test_the_anonymous_path_returns_a_bool_instead_of_raising():
    """THE regression. Before the fix this raised TypeError and every anonymous call 500'd.

    With no dashboard DSN configured `_dashboard_lookup` returns None early, so this must
    come back False — the honest "no credential has been minted" answer — never an exception.
    """
    result = http_server._any_dashboard_credential_configured()
    assert isinstance(result, bool)


def test_resolve_principal_survives_a_credential_less_request():
    """The real caller, one frame up. This is the frame that actually produced the 500."""
    http_server._resolve_principal(None)  # must not raise


def test_every_dashboard_lookup_call_passes_both_arguments():
    """AST audit — a NEW call site cannot reintroduce this silently.

    Arity is checked against the live signature rather than a hardcoded 2, so adding a
    parameter to `_dashboard_lookup` does not quietly invalidate this guard.
    """
    required = [
        name
        for name, p in inspect.signature(http_server._dashboard_lookup).parameters.items()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]

    source = Path(inspect.getfile(http_server)).read_text()
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_dashboard_lookup":
            continue
        supplied = len(node.args) + len(node.keywords)
        if supplied < len(required):
            offenders.append(f"line {node.lineno}: {supplied} arg(s), needs {len(required)}")

    assert not offenders, (
        "_dashboard_lookup called with too few arguments — this raises at the CALL SITE, "
        "before the callee's fail-safe `try`, so it becomes an HTTP 500 rather than a "
        "graceful degrade:\n  " + "\n  ".join(offenders)
    )
