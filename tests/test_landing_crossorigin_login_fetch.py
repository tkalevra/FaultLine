"""LOG IN = ENTRANCE PICKER NAVIGATION (owner ruling 2026-11-02, BINDING).

Supersedes the same-page cross-origin credential fetch pins (BUGS/same-page-login-20261102):
the card's Log In button NEVER collects or sends credentials. Clicking it must attempt
exactly ONE navigation to the entrance picker (/access.html) with ZERO network calls —
the password + MFA exchange happens entirely inside the chosen console on the app plane.
The email/password fields remain for CREATE (signup) only; the card-side "Forgot
password?" link is REMOVED (reset lives on the realm's own sign-in page).

Also pins the new minimal entrance picker (access.html): tagline, then exactly two cards
labelled "Tenant" and "Seat", each carrying a ? tooltip with the short who-is-this-for
description; the wordy teasers and forgot-password hint paragraphs are gone; the
"go back to the site" link and bottom banner remain.
"""
import json
import os
import re
import shutil
import subprocess
import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "dashboard" / "render_login_nav.js"
ACCESS = ROOT / "saas" / "landing" / "access.html"
LANDING_JS = ROOT / "saas" / "landing" / "landing.js"
INSTALL_HINT = "npm install --prefix tests/dashboard"
_MODULE_MISSING = re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    if not HARNESS.exists():
        pytest.skip(f"render harness missing at {HARNESS.relative_to(ROOT)}")
    env = dict(os.environ)
    roots = [str(HARNESS.parent / "node_modules"), str(ROOT / "node_modules")]
    if env.get("NODE_PATH"):
        roots.append(env["NODE_PATH"])
    env["NODE_PATH"] = os.pathsep.join(roots)
    try:
        out = subprocess.run([node, str(HARNESS)], capture_output=True, text=True,
                             timeout=180, cwd=str(ROOT), env=env)
    except subprocess.TimeoutExpired:
        pytest.fail("render harness timed out after 180s")
    if out.returncode != 0:
        missing = _MODULE_MISSING.search(out.stderr or "")
        if missing:
            pytest.skip(f"node module {missing.group(1)!r} is not installed - run:  {INSTALL_HINT}")
        pytest.fail("render harness exited %d:\n%s" % (out.returncode, (out.stderr or "")[-3000:]))
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail("render harness emitted non-JSON stdout: %s" % exc)


def test_log_in_navigates_to_picker_and_never_sends_credentials(report):
    steps = report["steps"]
    assert steps["login_present"] and steps["go_present"], "card buttons missing"
    assert steps["login_label"] == "Log In", "Log In label must stay exact"
    assert steps["nav_attempts_after_login_click"] == 1, (
        "Log In must attempt exactly ONE navigation (the entrance picker)")
    assert steps["fetches_after_login_click"] == 0, "Log In must issue ZERO network calls"
    assert not steps["fetched_unified_login"], "no unified-login POST may fire from the card"
    assert not steps["fetched_any_credentials_body"], "no credential body may ever be sent"


def test_forgot_password_gone_signup_fields_kept(report):
    steps = report["steps"]
    assert steps["forgot_absent"], "card-side Forgot password? link must be GONE"
    assert steps["email_field_present"], "email field must STAY — Create (signup) still needs it"


def test_card_no_longer_submits_login_mode():
    src = LANDING_JS.read_text(encoding="utf-8")
    assert "authSubmit('login')" not in src, "authLogin must no longer submit a login mode"
    assert "authForgot" not in src, "authForgot must be gone from landing.js"
    assert "/access.html" in src, "Log In must navigate to the entrance picker"


def test_access_page_minimal_picker_shape():
    src = ACCESS.read_text(encoding="utf-8")
    # exactly two cards, cleanly named, with ? tooltips
    assert '>Tenant</span>' in src and '>Seat</span>' in src, "cards must be named Tenant / Seat"
    assert src.count('class="fl-q" title=') == 2, "each card must carry exactly one ? tooltip"
    # wordy copy gone
    assert "fl-teaser" not in src, "teaser paragraphs must be gone"
    assert "fl-hint" not in src, "forgot-password hint paragraphs must be gone"
    assert "Forgot your password?" not in src, "forgot-password copy must be gone"
    assert "splash-prompt" not in src, "the Customer access h1 must be gone"
    # kept, per ruling
    assert "go back to the site" in src, "go-back link must stay"
    assert "splash-foot" in src, "bottom banner must stay"
    assert "Validated memory for AI" in src, "tagline must stay"
