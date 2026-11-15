"""BUG-11 (P1) — the Google/GitHub sign-in buttons never rendered on the login card.

Root cause A (crash): user.js::wireOAuthButtons declared `const r` INSIDE the try block,
so the later `r.start_base` read threw ReferenceError AFTER the row was un-hidden and
BEFORE any anchor was appended — live symptom: visible separator, ZERO provider buttons.
Root cause B (style): style.css styled only `button.btn`; the provider anchors
(<a class="btn">) rendered as browser-default blue links. House style is SQUARE,
monochrome token colours — no pill, no provider-brand colours (owner directive:
"keep the colours shape etc.").

Behavioural pins run the REAL user.html + user.js in jsdom via
tests/dashboard/render_oauth_signin_buttons.js (the render_oauth_return.js pattern).
node_modules is not committed — without it these SKIP loudly and the STATIC assertions
below still pin the code shape (scope fix, cache-bust, shared selector groups).

Run: python3 tools/fltest.py --bug OAUTH-SIGNIN-BUTTONS --test tests/test_oauth_signin_buttons.py
"""

import json
import pathlib
import subprocess

import pytest

HERE = pathlib.Path(__file__).resolve().parent
DASH = HERE / "dashboard"
HARNESS = DASH / "render_oauth_signin_buttons.js"
SAAS = HERE.parent / "saas" / "dashboard"


def _harness_ready() -> bool:
    return (DASH / "node_modules").is_dir() and HARNESS.is_file()


def _run(mode):
    res = subprocess.run(
        ["node", str(HARNESS), mode],
        capture_output=True, text=True, timeout=120, cwd=str(DASH))
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.skipif(not _harness_ready(), reason=(
    "jsdom harness deps not installed — run `npm install --prefix tests/dashboard`; "
    "static assertions below still ran"))
def test_two_provider_buttons_render_with_start_base_hrefs():
    """PIN (the repro — FAILED on next @89e914c4): with both providers configured the
    row becomes visible and #oauth-btns ends with exactly TWO anchors, correct labels,
    and hrefs prefixed by the advertised start_base."""
    out = _run("both")
    assert out["uncaught"] == []                    # no ReferenceError survived the fix
    s = out["steps"]
    assert s["row_exists"] is True
    assert s["row_hidden"] is False                 # row IS visible
    assert s["button_count"] == 2                   # exactly two — the double-boot trap
    assert s["labels"] == ["▸ Continue with Google", "▸ Continue with GitHub"]
    assert s["hrefs"] == [
        "https://mcp.faultline.ca/me/oauth/google/start",
        "https://mcp.faultline.ca/me/oauth/github/start"]


@pytest.mark.skipif(not _harness_ready(), reason=(
    "jsdom harness deps not installed — run `npm install --prefix tests/dashboard`; "
    "static assertions below still ran"))
@pytest.mark.parametrize("mode", ["fail", "empty"])
def test_optional_chrome_never_breaks_the_login_card(mode):
    """PIN (silent-catch contract): providers fetch failing OR empty → row stays
    hidden, no crash, no uncaught rejection. Optional chrome never blocks login."""
    out = _run(mode)
    assert out["uncaught"] == []
    s = out["steps"]
    assert s["row_hidden"] is True
    assert s["button_count"] == 0


@pytest.mark.skipif(not _harness_ready(), reason=(
    "jsdom harness deps not installed — run `npm install --prefix tests/dashboard`; "
    "static assertions below still ran"))
def test_anchor_buttons_share_the_button_house_style():
    """PIN: a.btn inherits the SAME house rules as button.btn — one shared selector
    group (square: no border-radius; token colours --fg/--border; no brand colours),
    and the anchor kills the UA link underline. var()-derived border/colour cannot be
    compared via jsdom computed styles (var() unresolved there) — the shared selector
    group is what makes drift impossible, and that is pinned on the stylesheet text."""
    out = _run("style")
    assert out["uncaught"] == []
    s = out["steps"]
    assert s["base_group_shared"] is True           # 'button.btn, a.btn {' one group
    assert s["hover_shared"] is True
    assert s["square_no_radius"] is True            # SQUARE — never a pill
    assert s["token_fg"] is True                    # color: var(--fg)
    assert s["token_border"] is True                # border: 1px solid var(--border)
    assert s["anchor_kills_link_underline"] is True
    assert s["no_brand_colours"] is True            # no google/github/hex literals
    assert s["computed_same_padding"] is True       # literal props DO compute equal
    assert s["computed_same_font"] is True
    assert s["computed_anchor_no_underline"] is True
    assert s["computed_same_radius"] is True


# ── STATIC pins (run even without node/jsdom) ────────────────────────────────────

def _user_js():
    return (SAAS / "user.js").read_text(encoding="utf-8")


def _style_css():
    return (SAAS / "style.css").read_text(encoding="utf-8")


def test_static_no_block_scoped_api_response_in_wireoauthbuttons():
    """The api() response binding must be declared OUTSIDE the try block — the exact
    ReferenceError (const r dying with the try) must not come back."""
    src = _user_js()
    assert "try { r = await api('GET', '/me/oauth/providers');" in src
    assert "let r = null;" in src
    assert "const r = await api('GET', '/me/oauth/providers')" not in src


def test_static_cache_bust_bumped():
    html = (SAAS / "user.html").read_text(encoding="utf-8")
    assert 'user.js?v=75' in html
    assert 'user.js?v=74' not in html


def test_static_style_css_shares_button_rules_with_anchors():
    css = _style_css()
    for sel in [
        "button.btn, a.btn {",
        "button.btn:hover, a.btn:hover",
        "button.btn.accent, a.btn.accent",
        "button.btn.danger, a.btn.danger",
        "button.btn.sm, a.btn.sm",
        "a.btn { text-decoration: none; }",
    ]:
        assert sel in css, sel
