"""BUG-01 — the tenant console's Google/GitHub sign-in buttons render SERVER-DRIVEN only.

The page (saas/dashboard/tenant.html + tenant.js) must not carry a hardcoded provider
list: GET /tenant/oauth/providers decides which buttons exist, and the row stays hidden
when none are configured (the same grammar the individual card pinned in
test_oauth_signin_buttons.py). Behavioural pins run the REAL page in jsdom via
tests/dashboard/render_tenant_oauth_signin_buttons.js; node_modules is not committed -
without it these SKIP loudly and the STATIC assertions below still pin the code shape.

Run: python3 -m pytest tests/test_tenant_oauth_signin_buttons.py -q
"""

import json
import pathlib
import subprocess

import pytest

HERE = pathlib.Path(__file__).resolve().parent
DASH = HERE / "dashboard"
HARNESS = DASH / "render_tenant_oauth_signin_buttons.js"
TENANT_HTML = HERE.parent / "saas" / "dashboard" / "tenant.html"
TENANT_JS = HERE.parent / "saas" / "dashboard" / "tenant.js"


def _harness_ready() -> bool:
    return (DASH / "node_modules").is_dir() and HARNESS.is_file()


def _run(mode):
    res = subprocess.run(
        ["node", str(HARNESS), mode],
        capture_output=True, text=True, timeout=120, cwd=str(DASH))
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


# -- STATIC pins (always run) ------------------------------------------------------

def test_page_has_server_driven_provider_row():
    html = TENANT_HTML.read_text(encoding="utf-8")
    assert 'id="oauth-row"' in html
    assert 'id="oauth-btns"' in html
    # the row lives INSIDE the creds step (shown with the email+password form)
    creds = html.split('id="login-step-creds"', 1)[1].split('id="login-step-mfa"', 1)[0]
    assert 'id="oauth-row"' in creds
    # row starts hidden - the SERVER's list un-hides it, never the page's own opinion
    assert 'id="oauth-row" class="hidden"' in html


def test_no_hardcoded_provider_buttons_in_page():
    """PIN: the page decides NOTHING about providers - no anchor to any /start door is
    hardcoded; buttons come only from the providers fetch (grep the real files)."""
    html = TENANT_HTML.read_text(encoding="utf-8")
    assert "/oauth/google/start" not in html
    assert "/oauth/github/start" not in html
    js = TENANT_JS.read_text(encoding="utf-8")
    assert "/oauth/google/start" not in js
    assert "/oauth/github/start" not in js
    # the button list is built from the server payload, never a literal array
    assert "providers" in js and "forEach" in js


def test_fragment_handlers_wired():
    """PIN: the #oauth= fragment is handled - mfa: into the EXISTING mfa step, session:
    exchanged at /tenant/oauth/session, errors into #login-msg; and the fragment is
    stripped before handling."""
    js = TENANT_JS.read_text(encoding="utf-8")
    assert "handleOAuthReturn" in js
    assert "code.startsWith('mfa:')" in js
    assert "code.startsWith('session:')" in js
    assert "'/tenant/oauth/session'" in js
    assert "history.replaceState" in js           # strip before handling
    assert "showMfaStep" in js


# -- BEHAVIOURAL pins (jsdom; skip loudly without node_modules) --------------------

@pytest.mark.skipif(not _harness_ready(), reason=(
    "jsdom harness deps not installed - run npm install --prefix tests/dashboard; "
    "static assertions above still ran"))
def test_two_provider_buttons_render_with_start_base_hrefs():
    out = _run("both")
    assert out["uncaught"] == []
    s = out["steps"]
    assert s["row_exists"] is True
    assert s["row_hidden"] is False
    assert s["button_count"] == 2
    assert s["labels"] == ["\u25b8 Continue with Google", "\u25b8 Continue with GitHub"]
    assert s["hrefs"] == [
        "https://mcp.faultline.ca/tenant/oauth/google/start",
        "https://mcp.faultline.ca/tenant/oauth/github/start"]


@pytest.mark.skipif(not _harness_ready(), reason="jsdom harness deps not installed")
def test_empty_provider_list_keeps_row_hidden():
    out = _run("empty")
    s = out["steps"]
    assert s["row_exists"] is True
    assert s["row_hidden"] is True
    assert s["button_count"] == 0


@pytest.mark.skipif(not _harness_ready(), reason="jsdom harness deps not installed")
def test_providers_fetch_failure_never_crashes_card():
    out = _run("fail")
    assert out["uncaught"] == []
    assert out["steps"]["row_hidden"] is True
