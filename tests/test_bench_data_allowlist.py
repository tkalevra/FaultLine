"""IS4 — ``_BENCH_DATA_FILES`` (src/mcp/http_server.py, GET /dashboard/data/{fname}) is PINNED.

CodeQL py/path-injection flagged the ``os.path.join``/``FileResponse`` on ``fname``. Since the
CQ-hardening pass (2026-09-17) the door is structurally clean, not just allowlist-gated: the
response path is BUILT from the closed allowlist literal into ``_BENCH_DATA_PATHS`` and the
request token only LOOKS UP a prebuilt constant path — no request bytes reach a filesystem
path, so the two in-source ``# codeql[py/path-injection]`` annotations are gone. This module
pins three things:

  1. STRUCTURE — the frozenset literal in the source contains only bare ``*.json`` basenames
     (no separator, no ``..``, no absolute path), read by ``ast`` so a future edit that slips a
     path in is caught without importing the nested route; the path dict is built from it; the
     lookup precedes every filesystem use; no path-injection annotation lingers.
  2. BEHAVIOUR — through the real app, an OPERATOR (the only principal past the auth gate) is
     refused for every traversal / near-miss name (404, never a file), and admitted for the
     allowlisted names.
  3. GATING — an anonymous caller is 403'd regardless of name.

Operator key is bound per test (never at import) — see tests/conftest.py.
Run: FLTEST_LEDGER=... python3 tools/fltest.py --bug IS5 --test tests/test_bench_data_allowlist.py
"""

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.mcp.http_server as h

SRC = Path(h.__file__).read_text(encoding="utf-8")
OP_KEY = "bench-allowlist-op-key"
BASENAME = re.compile(r"^[a-z0-9_]+\.json$")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)


@pytest.fixture
def client():
    return TestClient(h.app, follow_redirects=False)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


def _allowlist_from_source() -> set:
    """The ONE ``_BENCH_DATA_FILES = frozenset({...})`` literal, extracted with ast."""
    tree = ast.parse(SRC)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_BENCH_DATA_FILES" for t in node.targets):
            call = node.value
            assert isinstance(call, ast.Call) and getattr(call.func, "id", "") == "frozenset"
            (arg,) = call.args
            assert isinstance(arg, ast.Set)
            found.append({ast.literal_eval(e) for e in arg.elts})
    assert len(found) == 1, f"expected exactly one _BENCH_DATA_FILES literal, got {len(found)}"
    return found[0]


def test_allowlist_is_a_closed_set_of_bare_json_basenames():
    names = _allowlist_from_source()
    assert names, "allowlist must not be empty"
    for n in names:
        assert isinstance(n, str) and BASENAME.match(n), n
        assert "/" not in n and "\\" not in n and ".." not in n and not n.startswith(".")


def test_membership_lookup_precedes_every_filesystem_use():
    """CQ-hardening 2026-09-17: the gate IS the lookup — ``fpath`` comes from
    ``_BENCH_DATA_PATHS.get(fname)``, a mapping BUILT from the closed allowlist literal, so no
    request bytes ever reach a path builder. A non-member name yields None → the gated 404,
    before any filesystem call."""
    body = SRC[SRC.index("_BENCH_DATA_FILES = frozenset"):]
    body = body[:body.index("PUBLIC anonymized leaderboard")]
    gate = body.index("_BENCH_DATA_PATHS = {name: os.path.join(_bench_data_dir, name)")
    lookup = body.index("fpath = _BENCH_DATA_PATHS.get(fname)")
    for use in ("os.path.isfile(fpath)", "FileResponse(fpath"):
        assert body.index(use) > lookup > gate, f"{use} must come AFTER the allowlist lookup"
    # the only os.path.join under this door sees the allowlist NAME, never the request token
    join = body.index("os.path.join(_bench_data_dir, name)")
    assert "for name in _BENCH_DATA_FILES" in body[join:body.index("}", join) + 1], \
        "the path dict must be built from the closed allowlist literal"


def test_no_path_injection_annotation_remains():
    """The structural fix removed the need for the two in-source codeql[] annotations — a
    request-derived token no longer reaches a filesystem path, so there is nothing to
    suppress. This also keeps the placement rule honest: an annotation over the OLD lines
    would now be stale documentation of a fixed finding."""
    assert "codeql[py/path-injection]" not in SRC, \
        "py/path-injection is fixed at the seam — the annotation must not linger"


@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "%2e%2e/%2e%2e/etc/passwd",
    "test_runs.json/../../etc/passwd", "test_runs.json%00.txt", "test_runs.json%2e",
    "TEST_RUNS.JSON", ".test_runs.json", "test_runs.json ", "%2Ftest_runs.json",
    "test_runs.jsonx", "secrets.json", "..", ".", "..json",
])
def test_traversal_and_near_miss_names_refused_even_for_operator(client, bad):
    """A traversal / near-miss name NEVER yields a JSON data file. (Some shapes never reach the
    route: the client/Starlette normalise ``..`` segments, so ``/dashboard/data/..`` lands on
    the dashboard HTML index and ``../../etc/passwd`` on the top-level offline page — neither
    is a file read under ``_bench_data_dir``, which is the property under test.)"""
    r = client.get(f"/dashboard/data/{bad}", headers=_op())
    ctype = (r.headers.get("content-type") or "").lower()
    assert not (r.status_code == 200 and "application/json" in ctype), \
        (bad, r.status_code, ctype, r.text[:200])
    assert "root:" not in r.text and "runs" not in r.text[:40], (bad, r.text[:200])


def test_allowlisted_names_admitted_for_operator(client):
    for name in sorted(_allowlist_from_source()):
        r = client.get(f"/dashboard/data/{name}", headers=_op())
        assert r.status_code == 200, (name, r.status_code, r.text[:200])


def test_anonymous_still_refused_regardless_of_name(client):
    for name in sorted(_allowlist_from_source()):
        assert client.get(f"/dashboard/data/{name}").status_code == 403
