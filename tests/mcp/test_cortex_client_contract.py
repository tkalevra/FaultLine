"""The cortex CLIENT CONTRACT — the hooks and the server must agree on the endpoint names.

WHY THIS FILE EXISTS (a measured, four-day silent outage — 2026-08-10)
---------------------------------------------------------------------
Commit 4f653e72 (2026-08-06, "P3 namespacing") renamed the agent-cortex tools:

    irecall   -> cortex_recall
    iremember -> cortex_remember

The server was updated everywhere. The FIVE client hooks in ``.claude/hooks/`` were not, and
kept POSTing ``/irecall`` and ``/iremember``. Every call 404'd.

The damage was invisible, and that is the whole point of this test. Each hook is deliberately
fail-safe ("any error -> surface nothing, exit 0") because a hook must never break a session.
So a DEAD cortex looked EXACTLY like a cortex with nothing relevant to say: no error, no log,
no warning. The agent (correctly) reads silence as "no relevant notes" and carries on. It ran
broken for four days and cost a full session of operational memory — the same silent-failure
class this codebase forbids everywhere else.

A green MCP suite did not catch it because every existing test drives the SERVER. Nothing
asserted that the shipped CLIENTS still point at a route the server serves. That is the gap
these tests close: they are a CROSS-CHECK between two files that are edited by different
people at different times, in different languages, with no import relationship.

If this file goes red, do NOT "fix" it by editing the expected strings. Read the failure:
a tool was renamed and the hooks in ``.claude/hooks/`` must be renamed with it, or every
agent using them goes quietly memory-less again.

Related hardening in the same commit: ``cortex_surface.py`` now warns loudly on a non-2xx AND
on a tool-level ``{"error": ...}`` (which arrives HTTP **200**, so a status check cannot see it).
"""
import importlib.util
import os

import pytest

import src.mcp.http_server as h
from src.mcp import tools as mcp_tools


HOOKS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".claude", "hooks",
)


def _load_hook(name):
    """Import a hook module BY PATH. The hooks are not a package and are never imported by
    the app — loading them here is precisely how we assert on what the clients actually send.
    Skips (rather than fails) when the hooks are absent, e.g. a FOSS checkout that ships the
    engine without the agent-cortex tooling."""
    path = os.path.join(HOOKS_DIR, name + ".py")
    if not os.path.isfile(path):
        pytest.skip(f"{name}.py not present in this checkout")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rest_paths():
    """Every REST shorthand path the server actually mounts, from its OWN op table."""
    return {op["path"] for op in h._TOOL_OPS}


def _advertised_tool_names():
    return {t["name"] for t in mcp_tools.TOOLS}


# ── The server half ────────────────────────────────────────────────────────────────────

def test_server_advertises_the_cortex_pair_under_the_namespaced_names():
    """The rename is the CURRENT contract: cortex_recall / cortex_remember, not irecall."""
    names = _advertised_tool_names()
    assert "cortex_recall" in names
    assert "cortex_remember" in names
    # The retired names must not come back silently — a re-rename is a client-breaking change
    # and has to be a deliberate, visible edit to this test.
    assert "irecall" not in names
    assert "iremember" not in names


def test_server_serves_a_rest_door_for_the_cortex_pair():
    """96227f2a added these precisely so non-MCP clients (the hooks, any OpenAPI client) can
    reach the cortex. If the REST door is dropped, the hooks lose their transport."""
    paths = _rest_paths()
    assert "/cortex_recall" in paths
    assert "/cortex_remember" in paths


# ── The client half, and the cross-check that would have caught the outage ──────────────

def test_hook_endpoint_paths_are_defined_in_exactly_one_place():
    """cortex_warm.py owns the path constants; every other hook imports them. Five hardcoded
    copies is what let the rename rot in four files at once."""
    warm = _load_hook("cortex_warm")
    assert warm.CORTEX_RECALL_PATH == "/cortex_recall"
    assert warm.CORTEX_REMEMBER_PATH == "/cortex_remember"


@pytest.mark.parametrize("attr", ["CORTEX_RECALL_PATH", "CORTEX_REMEMBER_PATH"])
def test_the_path_the_hooks_post_to_is_a_path_the_server_serves(attr):
    """THE REGRESSION TEST. Cross-checks the client's constant against the server's own op
    table. A rename on either side turns this red instead of going silent in production."""
    warm = _load_hook("cortex_warm")
    assert getattr(warm, attr) in _rest_paths(), (
        f"{attr}={getattr(warm, attr)!r} is NOT served by src/mcp/http_server.py::_TOOL_OPS. "
        "A cortex tool was renamed and .claude/hooks/ was not updated — this is the exact "
        "silent 404 of 2026-08-10. Fix the hooks, do not edit this assertion."
    )


def test_no_hook_still_references_a_retired_cortex_path():
    """Belt and braces: catch a stray hardcoded /irecall or /iremember anywhere in the hooks,
    including one reintroduced by a copy-paste. Docstrings/comments are allowed to MENTION the
    old names (they explain the history) — only an actual request path is a failure, so we
    look for the URL-building form the hooks use."""
    if not os.path.isdir(HOOKS_DIR):
        pytest.skip("hooks dir not present in this checkout")
    offenders = []
    for fname in sorted(os.listdir(HOOKS_DIR)):
        if not (fname.startswith("cortex_") and fname.endswith(".py")):
            continue
        body = open(os.path.join(HOOKS_DIR, fname), "r", errors="replace").read()
        for dead in ("{MCP}/irecall", "{MCP}/iremember"):
            if dead in body:
                offenders.append(f"{fname}: {dead}")
    assert not offenders, (
        "hook(s) still POST to a retired cortex path: " + "; ".join(offenders)
    )


# ── The OTHER shipped client: the test harness itself ──────────────────────────────────

def test_fltest_posts_to_the_current_cortex_paths():
    """tools/fltest.py is a shipped cortex client too (red-run capture + pre-run recall),
    and it kept POSTing the retired /iremember for weeks after the P3 rename — a silent
    404 eaten by its own non-blocking handler, so red-run lessons were dropped exactly
    when they mattered. Nothing caught it because the hook scan above only reads
    .claude/hooks/. This pins the harness to the CURRENT contract paths, both halves."""
    fltest_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "tools", "fltest.py",
    )
    if not os.path.isfile(fltest_path):
        pytest.skip("tools/fltest.py not present in this checkout")
    body = open(fltest_path, "r", errors="replace").read()
    for dead in ('"/iremember"', "'/iremember'", '"/irecall"', "'/irecall'"):
        assert dead not in body, (
            f"fltest still builds a request to the retired path {dead} — the red-run "
            f"lesson capture is silently 404ing. Point it at /cortex_remember."
        )
    assert '"/cortex_remember"' in body, "fltest must POST lessons to /cortex_remember"
    assert '"/cortex_recall"' in body, "fltest must run its pre-flight recall on /cortex_recall"


def test_surface_hook_treats_a_tool_level_error_as_a_failure_not_as_silence():
    """A tool-level failure (unknown/renamed tool) returns HTTP **200** with {"error": ...}
    in the payload, so the status check cannot see it. _unwrap must surface it as an error
    rather than an empty dict, which would render as 'no relevant notes' — the silence that
    hid the outage."""
    surface = _load_hook("cortex_surface")
    # JSON-RPC envelope carrying a tool-level error, exactly as the live server returns it.
    rpc = ('{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text",'
           '"text":"{\\"error\\": \\"Unknown tool: irecall\\"}"}]}}')
    assert surface._unwrap(rpc).get("error") == "Unknown tool: irecall"
    # A flat REST body must pass straight through unchanged (the hooks use the REST door).
    assert surface._unwrap('{"memory":"- [gotcha] something"}')["memory"].startswith("- [gotcha]")
    # An auth body is not a memory payload and must not read as an empty cortex.
    assert surface._unwrap('{"detail":"Unauthorized"}').get("memory") is None
