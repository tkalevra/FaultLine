"""BUG-10 defect 2 (P2, owner-directed, 2026-10-31): signup must ASK what you are signing
up for — Single (individual memory) vs Tenant/Organisation (team workspace) — BEFORE the
email/password form, routing to the right door.

Verdicts over ``tests/dashboard/render_signup_chooser.js`` (jsdom drive of the REAL
signup.html + signup.js), plus static pins for the half jsdom cannot see (the navigation
DESTINATION — jsdom's window.location is frozen, so the URL string is pinned against the
signup.js source) and the no-JS / CSP contracts. Observations live in the harness;
pass/fail lives here, per house convention (see test_seat_visibility_bug06.py).

Defect 1's email pins (operator notify + individual welcome + dedupe) live in
test_signup_admin_notify_pins.py — this file owns the front-door CHOOSER only.
"""
import json
import os
import re
import shutil
import subprocess
import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "dashboard" / "render_signup_chooser.js"
PAGE = ROOT / "saas" / "landing" / "signup.html"
SCRIPT = ROOT / "saas" / "landing" / "signup.js"
TEAM_PAGE = ROOT / "saas" / "landing" / "signup-team.html"
INSTALL_HINT = "npm install --prefix tests/dashboard"

_MODULE_MISSING = re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH — install Node.js to run the rendered half; "
                    "the static assertions in this module still run")
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
            pytest.skip(f"node module {missing.group(1)!r} is not installed — run:  {INSTALL_HINT}")
        pytest.fail(f"render harness exited {out.returncode} — REAL failure, not a missing "
                    f"dependency:\n{(out.stderr or '')[-3000:]}")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:                        # pragma: no cover
        pytest.fail(f"render harness emitted non-JSON stdout: {exc}\n{out.stdout[-2000:]}")


# ══════════════════════════════════════════════════════════════════════════════════
# (1) THE CHOOSER IS THE QUESTION, ASKED FIRST
# ══════════════════════════════════════════════════════════════════════════════════

def test_chooser_present_and_first(report):
    """The account-type chooser exists, sits BEFORE the form in the DOM, and asks the
    owner's question ("what are you signing up for?") with two type=button options."""
    s = report["steps"]
    assert s["chooser_present"], "no #acct-chooser on the signup page"
    assert s["chooser_before_form"], "the chooser must precede the form in the DOM"
    assert "signing up for" in (s["chooser_question"] or "").lower()
    assert s["buttons_are_type_button"], "chooser buttons must never submit the form"


def test_chooser_copy_is_honest(report):
    """Single and Tenant get DIFFERENT, honest copy — the single option says personal /
    one person; the team option says shared workspace / seats / owner."""
    s = report["steps"]
    assert s["copy_differs"]
    single = (s["single_copy"] or "").lower()
    team = (s["team_copy"] or "").lower()
    assert "one person" in single and "personal" in single
    assert "team" in team and "workspace" in team and "seats" in team
    assert "owner" in team, "the team option must say you become the workspace owner"


# ══════════════════════════════════════════════════════════════════════════════════
# (2) THE ROUTING — Single stays on the /me door, Tenant goes to the team door
# ══════════════════════════════════════════════════════════════════════════════════

def test_form_hidden_until_single_chosen(report):
    """With JS on, the email/password form is HIDDEN until "Just me" is chosen — nobody
    types a password before deciding what they are creating."""
    s = report["steps"]
    assert s["initial_form_hidden"] and s["initial_chooser_visible"]
    assert s["single_shows_form"] and s["single_hides_chooser"]
    assert s["single_focuses_email"], "choosing single should focus the email field"
    assert s["form_fields"], "the individual form fields must survive the chooser change"


def test_team_choice_navigates(report):
    """The team choice FIRES a navigation (jsdom proves the attempt; the destination is
    pinned statically below)."""
    s = report["steps"]
    assert s["team_click_fires_navigation"], "team click did not attempt a navigation"
    assert s["team_nav_attempts"] == 1


def test_team_destination_is_the_team_door_static():
    """The navigation destination, pinned against the REAL signup.js source (jsdom's
    frozen window.location cannot yield it): the team button assigns
    signup-team.html + the carried query string."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'TEAM_SIGNUP_URL\s*=\s*"signup-team\.html"', src), \
        "the team door must be signup-team.html"
    assert re.search(r"window\.location\.assign\(TEAM_SIGNUP_URL \+ q\)", src), \
        "the team click must navigate, carrying the query string"
    assert TEAM_PAGE.exists(), "signup-team.html must exist as the tenant door"


def test_switch_link_to_team_door(report):
    """The in-form escape hatch links to the team door (one click away after choosing)."""
    s = report["steps"]
    assert s["switch_link_present"]
    assert s["switch_link_target"] == "signup-team.html"


def test_no_inline_handlers(report):
    """CSP contract intact: the chooser adds zero inline handlers (script-src 'self')."""
    assert report["steps"]["no_inline_handlers"]
    assert not report["errors"], f"harness errors: {report['errors']}"


# ══════════════════════════════════════════════════════════════════════════════════
# (3) NO-JS SAFETY + CROSS-DOOR SYMMETRY (static — no browser needed)
# ══════════════════════════════════════════════════════════════════════════════════

def test_nojs_visitors_still_reach_the_form():
    """The markup ships the form WITHOUT a hidden attribute — a no-JS visitor sees the
    form (the chooser buttons are inert type=button); only JS hides it behind the
    question. A chooser that bricks signup without JS is a regression."""
    html = PAGE.read_text(encoding="utf-8")
    m = re.search(r'<form class="signup-card" id="signup-form"[^>]*>', html)
    assert m, "signup form missing"
    assert "hidden" not in m.group(0), "the form must NOT ship hidden (no-JS visitors)"


def test_team_door_links_back():
    """Symmetry: the team page keeps its "just you, not a team?" link back to the
    individual door, so a wrong choice at the chooser is always correctable."""
    html = TEAM_PAGE.read_text(encoding="utf-8")
    assert 'href="signup.html"' in html
