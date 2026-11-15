"""Landing auth-row pin (BUGS: v74 wrapped-by-arithmetic authrow; OWNER RULINGS 2026-11-02).

HISTORY. On the landing auth card the pair "Create my memory" + "Organisation / tenant
console ->" could NEVER sit in-line: the card is 322px wide (content width) and the pair
needed ~347px+, so .authrow (flex-wrap) always wrapped the console link onto its own row.
Ruling 1: primary label went SHORT - 'Create' / 'Log In', exact capitalization.

OWNER RULING (2026-11-02, FINAL — supersedes the single-button + mode-flip design):
unified login means the card carries exactly TWO PERMANENT BUTTONS side by side -
'Create' (seat signup) and 'Log In' (unified door, discriminates after entry) - and NO
mode-flip toggle ('Already have an account? Log in >' is GONE) and NO 'Organisation /
tenant console' door-picker. The tenant door remains reachable via /access.html (the
both-realm chooser the unified outcome still hands to), direct URLs, and the hint.

This module runs tests/dashboard/render_landing_authrow.js (jsdom drive of the REAL
index.html + landing.js bytes) and pins the OUTCOME a reader sees: Create + Log In
side by side in one .authrow, door-picker and toggle gone.
"""
import json
import os
import re
import shutil
import subprocess
import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "dashboard" / "render_landing_authrow.js"
LANDING_JS = ROOT / "saas" / "landing" / "landing.js"
INSTALL_HINT = "npm install --prefix tests/dashboard"

_MODULE_MISSING = re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH - install Node.js to run the rendered half")
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
    except subprocess.TimeoutExpired:                          # pragma: no cover
        pytest.fail("render harness timed out after 180s")
    except OSError as exc:                                     # pragma: no cover
        pytest.skip(f"render harness failed to launch: {exc}")

    if out.returncode != 0:
        missing = _MODULE_MISSING.search(out.stderr or "")
        if missing:
            pytest.skip(f"node module {missing.group(1)!r} is not installed - run:  {INSTALL_HINT}")
        pytest.fail("render harness exited %d - REAL failure, not a missing dependency:\n%s"
                    % (out.returncode, (out.stderr or "")[-3000:]))
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:                        # pragma: no cover
        pytest.fail("render harness emitted non-JSON stdout: %s\n%s" % (exc, out.stdout[-2000:]))


def test_two_permanent_buttons_side_by_side(report):
    steps = report["steps"]
    assert steps["go_label_is_create"], (
        "button one must read exactly 'Create'; got %r" % steps["go_label"])
    assert steps["login_label_is_log_in"], (
        "button two must read exactly 'Log In'; got %r" % steps["login_label"])
    assert steps["pair_in_same_authrow"], "Create and Log In must sit side by side in one .authrow"
    assert steps["go_is_button"] and steps["login_is_button"], "both must be real buttons"


def test_mode_flip_toggle_removed(report):
    steps = report["steps"]
    assert steps["authSwap_absent"], "authSwap mode-flip link must be GONE (two permanent buttons)"
    assert steps["lead_text_absent"], "'Already have an account?' lead text must be GONE"


def test_console_door_picker_removed(report):
    steps = report["steps"]
    assert steps["authConsole_absent"], "authConsole door-picker link must be GONE (unified door discriminates after login)"
    assert steps["console_label_absent"], "'Organisation / tenant console' label must not appear anywhere on the card"


def test_no_stale_long_primary_label_in_landing_js():
    # static half: the old long primary label is gone from the auth card source
    src = LANDING_JS.read_text(encoding="utf-8")
    assert "Create my memory" not in src, "stale long primary label still in landing.js"
    assert "'Sign in'" not in src, "stale 'Sign in' primary label still in landing.js"
    assert 'id="authGo">Create<' in src, "'Create' button label missing"
    assert '>Log In</button>' in src, "'Log In' button label missing"
    assert "authSwap" not in src, "mode-flip handler still in landing.js"
    assert "authConsole" not in src, "door-picker still in landing.js"
