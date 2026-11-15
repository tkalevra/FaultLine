"""PUBLIC community leaderboard PAGE — served on the public/tenant face, NOT operator-gated.

Phase 4. The leaderboard (saas/dashboard/leaderboard.html + leaderboard.js) is a PUBLIC page.
It must:
  * be reachable on the tenant + internal faces (the public front door), and on the admin face
    (an operator convenience) — 404 only on the corporate Volenti face;
  * NOT appear in the operator-only exact set (that would hide it from the public/tenant face);
  * be CSP-safe: an EXTERNAL script only, no inline <script> body and no on*= handlers (the
    dashboard CSP is script-src 'self' with no 'unsafe-inline').
  * The /leaderboard/data endpoint stays gate-free on the public faces.

SAAS_MODE (which decides the import-time SaaS wiring + face gate) is the suite-wide baseline set
once in tests/conftest.py, not written into os.environ here — a module-level write is process-wide
and made import order decide other modules' configuration.
tests/ is gitignored → add with ``git add -f``.

Run: python3 tools/fltest.py --bug BENCH-LB-PUBLIC --test tests/test_saas_leaderboard_public.py
"""

import os
import re

import pytest

import src.mcp.http_server as h

_DASH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "saas", "dashboard")


def test_leaderboard_not_in_operator_only_set():
    """The page + its JS must NOT be in the operator-only exact set (that would hide them from
    the public/tenant face)."""
    assert "/dashboard/leaderboard.html" not in h._OPERATOR_CONSOLE_EXACT
    assert "/dashboard/leaderboard.js" not in h._OPERATOR_CONSOLE_EXACT
    assert "/dashboard/leaderboard.html" not in h._TENANT_CONSOLE_EXACT


@pytest.mark.parametrize("face,served", [
    ("tenant", True), ("internal", True), ("admin", True), ("volenti", False)])
def test_leaderboard_page_face_gating(face, served):
    """The page serves on public/tenant/admin faces; only the corporate Volenti face 404s it."""
    blocked = h._gate_blocks(face, "/dashboard/leaderboard.html")
    assert (not blocked) == served, (face, blocked)


@pytest.mark.parametrize("face,served", [
    ("tenant", True), ("internal", True), ("admin", True), ("volenti", False)])
def test_leaderboard_data_endpoint_face_gating(face, served):
    """The public data endpoint mirrors the page's reachability."""
    blocked = h._gate_blocks(face, "/leaderboard/data")
    assert (not blocked) == served, (face, blocked)


def test_leaderboard_files_exist():
    assert os.path.isfile(os.path.join(_DASH, "leaderboard.html"))
    assert os.path.isfile(os.path.join(_DASH, "leaderboard.js"))


def test_leaderboard_html_is_csp_safe():
    """No inline <script> body, no on*= event handlers — the dashboard CSP forbids inline JS."""
    html = open(os.path.join(_DASH, "leaderboard.html"), encoding="utf-8").read()
    # The only <script> must be an external src= reference (no inline body).
    for m in re.finditer(r"<script\b([^>]*)>", html, re.I):
        attrs = m.group(1)
        assert "src=" in attrs, f"inline <script> found: {m.group(0)!r}"
    # No inline event handlers (onclick=, onload=, …). Guard against real handlers, not the
    # substring 'on' inside attribute names like content=.
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", html, re.I), "inline on*= handler in leaderboard.html"


def test_leaderboard_data_endpoint_public_no_auth():
    """End-to-end: an anonymous GET /leaderboard/data returns 200 with a results list (no auth).
    The anonymization of the CONTENT is proven in test_saas_community_bench.py."""
    from fastapi.testclient import TestClient
    import saas.platform_settings as _ps
    _orig = _ps.offline_state
    _ps.offline_state = lambda: {"offline": False, "message": ""}
    try:
        client = TestClient(h.app, follow_redirects=False)
        r = client.get("/leaderboard/data")   # no Authorization header
        assert r.status_code == 200, (r.status_code, r.text[:200])
        body = r.json()
        assert "results" in body and isinstance(body["results"], list)
        # No identity column names anywhere in the public payload.
        blob = r.text.lower()
        assert "account_id" not in blob and "user_id" not in blob
    finally:
        _ps.offline_state = _orig
