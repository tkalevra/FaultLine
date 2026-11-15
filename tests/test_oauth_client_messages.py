"""OAUTH-CLIENT-MESSAGES — what the login card actually renders for each OAuth return code.

BUG-01: (a) the OAuth sign-up door now holds 'pending_email' (activation gate wired), so the
client must render the check-your-inbox wording for the new `pending_email` fragment — NOT the
approval message; (b) a revoked seat (`error:disabled`) must read as an access state with a
lever (contact the administrator), not as "sign-in failed: …" (which the owner read as
"sign-up is broken" and filed the wrong bug).

Behavioural pins run the REAL user.html + user.js in jsdom via tests/dashboard/render_oauth_return.js
(the render_seats.js pattern). node_modules is not committed — without it these SKIP loudly and the
STATIC assertions below still pin the copy and the code shape.

Run: python3 tools/fltest.py --bug OAUTH-CLIENT-MESSAGES --test tests/test_oauth_client_messages.py
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
DASH_HARNESS = HERE / "dashboard" / "render_oauth_return.js"
USER_JS = HERE.parent / "saas" / "dashboard" / "user.js"


def _harness_ready() -> bool:
    return (HERE / "dashboard" / "node_modules").is_dir() and DASH_HARNESS.is_file()


@pytest.mark.skipif(not _harness_ready(), reason=(
    "jsdom harness deps not installed — run `npm install --prefix tests/dashboard`; "
    "static assertions below still ran"))
def test_rendered_messages_per_fragment_code():
    codes = ["pending_email", "error:disabled", "pending", "error:state"]
    res = subprocess.run(
        ["node", str(DASH_HARNESS), json.dumps(codes)],
        capture_output=True, text=True, timeout=120, cwd=str(HERE / "dashboard"))
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)

    # pending_email: since BUG-08 this code renders the ACTIVATE/RESEND card (a step,
    # not a banner) — the login-msg stays empty and the activate step carries the copy.
    # The card itself (button, cooldown, resend click) is pinned by the dedicated
    # render_activate_screen.js harness in test_saas_oauth_retry_activate_screen.py.
    assert out["pending_email"] == ""

    # error:disabled: a revoked seat is an ACCESS state — distinct copy, and crucially NOT
    # prefixed "sign-in failed:" (that prefix is what read as a broken signup)
    assert "revoked" in out["error:disabled"] and 'class="msg err"' in out["error:disabled"]
    assert not out["error:disabled"].startswith("sign-in failed")
    assert "sign-in failed" not in out["error:disabled"]

    # pending (approval valve) keeps its pre-existing message, untouched by this lane
    assert "awaiting approval" in out["pending"]
    # an ordinary error code still renders the generic prefixed form
    assert "sign-in failed: " in out["error:state"]


# ── static pins (run even without jsdom) ──────────────────────────────────────────

def test_user_js_has_pending_email_and_unprefixed_disabled():
    src = USER_JS.read_text(encoding="utf-8")
    # the pending_email branch exists BEFORE the generic error fall-through
    assert "code === 'pending_email'" in src
    assert src.index("code === 'pending_email'") < src.index("const ec = code.startsWith('error:')")
    # disabled is special-cased so its message carries no 'sign-in failed: ' prefix
    assert "if (ec === 'disabled')" in src
    # the copy: honest about the seat, names the lever, and says it is not a signup problem
    assert "has been revoked by its administrator" in src
    assert "not a sign-up problem" in src
    # the fragment contract comment lists the new terminal code
    assert "pending_email | mfa:" in src


def test_user_js_oauth_err_map_still_generic_safe():
    src = USER_JS.read_text(encoding="utf-8")
    # the map keeps a disabled entry (used by the special case) and the generic fallback
    assert "disabled:" in src and "unavailable:" in src
