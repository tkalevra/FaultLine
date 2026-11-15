"""TENANT-CONSOLE ADMIN password-reset — the SURFACE this lane owns, pinned as outcomes.

Covers DEV/DESIGN-tenant-console-password-reset.md (the dashboard_users realm — the
/dashboard/tenant.html login, NOT the /me individual realm). The two-tier approval machinery
and every route are older work with their own pins (tests/test_saas_reset_approvals.py,
tests/test_saas_account_type.py); this file pins what THIS lane changed and the two invariants
a reader of the page/console would experience:

  1. access.html — BOTH chooser cards carry a forgot-password hint; the tenant card says the
     reset is HELD FOR APPROVAL (operator-held — the honest wording, not "we'll email you a
     link"), the individual card points at the My Memory sign-in. Parser-based (HTMLParser,
     never a regex — the load-bearing method note of test_landing_link_integrity.py applies
     here too), and the hints must introduce NO new links (the cards themselves are the links;
     a nested <a> would be invalid HTML anyway).
  2. The operator "reset pw" action is CONFIRM-GATED: clicking it makes NO api call until the
     operator confirms in the modal; confirming posts EXACTLY ONE call to the break-glass
     route; the temp password lands in the once-only secret-box. Proven against the REAL
     index.html + app.js in jsdom via the shared render harness — a source-regex pin can go
     green while the actual click path silently un-wires (the exact failure shape the render
     harness exists to catch).

Run: python3 tools/fltest.py --bug TENANT-RESET --test tests/test_tenant_console_reset_surface.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
ACCESS_HTML = os.path.join(ROOT, "saas", "landing", "access.html")
APP_JS = os.path.join(ROOT, "saas", "dashboard", "app.js")
HARNESS = os.path.join(_HERE, "dashboard", "render_operator_console.js")

INSTALL_HINT = "npm install --prefix tests/dashboard"
_MODULE_MISSING = re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")

# void elements (HTML5): fire handle_starttag with NO matching endtag
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "param", "source", "track", "wbr"})


# ══════════════════════════════════════════════════════════════════════════════════
# 1. access.html — the honest forgot hints, parsed the way a browser parses
# ══════════════════════════════════════════════════════════════════════════════════

class _CardParser(HTMLParser):
    """Collects each access.html chooser card as {href, text, has_nested_anchor}."""

    def __init__(self):
        super().__init__()
        self.cards: list[dict] = []
        self._depth = 0          # nesting depth inside an <a class="flavour">
        self._nested_anchor = 0  # an <a> opened while already inside one (invalid HTML)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "flavour" in (a.get("class") or ""):
            self._depth = 1
            self.cards.append({"href": a.get("href") or "", "text": "", "nested_anchor": False})
        elif self._depth:
            if tag in _VOID:      # <br>/<img> never fire an endtag — do not inflate depth
                return
            self._depth += 1
            if tag == "a":
                self._nested_anchor += 1
                self.cards[-1]["nested_anchor"] = True

    def handle_endtag(self, tag):
        # every close reduces depth (the card's own </a> AND nested spans/<b>/…), so an
        # unbalanced inline tag can never make the NEXT card's anchor read as "nested"
        if self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            self.cards[-1]["text"] += data


def _parse_access_cards() -> list[dict]:
    with open(ACCESS_HTML, encoding="utf-8") as fh:
        p = _CardParser()
        p.feed(fh.read())
    return p.cards


def _card(cards: list[dict], needle: str) -> dict:
    for c in cards:
        if needle in c["href"]:
            return c
    raise AssertionError(f"no chooser card links to anything matching {needle!r} in access.html")


def test_both_cards_carry_a_forgot_password_hint():
    cards = _parse_access_cards()
    assert len(cards) == 2, f"expected exactly the two doors, got {len(cards)}"
    for c in cards:
        assert "forgot" in c["text"].lower(), (
            f"the card → {c['href']} lost its forgot-password hint")


def test_tenant_card_hint_is_honest_about_the_hold():
    """The tenant-admin reset is operator-held (Tier-2, default ON). The hint must SAY that —
    a stranded admin reading only this page must not believe a reset email is on its way."""
    text = _card(_parse_access_cards(), "tenant.html")["text"].lower()
    assert "forgot" in text
    # the HOLD, in words a customer understands — one of these must be present
    assert ("held for approval" in text or "operator-held" in text), (
        f"tenant-card hint no longer states the reset is held for approval: {text!r}")


def test_individual_card_hint_points_at_the_self_serve_flow():
    text = _card(_parse_access_cards(), "user.html")["text"].lower()
    assert "forgot" in text
    assert "my memory" in text or "sign-in" in text, (
        f"individual-card hint no longer points at the My Memory sign-in: {text!r}")
    # the /me realm really does email a link — the hint may honestly promise one
    assert "email" in text


def test_hints_introduce_no_new_links_and_no_inline_handlers():
    """The cards themselves are the links; a hint must stay plain text (a nested <a> is
    invalid HTML — parsers would sever the card anchor around it)."""
    p = _CardParser()
    with open(ACCESS_HTML, encoding="utf-8") as fh:
        p.feed(fh.read())
    for c in p.cards:
        assert not c["nested_anchor"], f"nested <a> inside the card → {c['href']}"
    assert {c["href"] for c in p.cards} == {
        "https://app.faultline.ca/dashboard/tenant.html",
        "https://app.faultline.ca/dashboard/user.html",
    }


# ══════════════════════════════════════════════════════════════════════════════════
# 2. the operator "reset pw" click path — rendered, real, confirm-gated
# ══════════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def render():
    """Run the shared render harness (same three-outcome discipline as
    test_operator_console_ux.py: absent toolchain / dependency → SKIP with the fix; a real
    harness failure → FAIL loudly)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH — the static access.html pins above still ran")
    if not os.path.exists(HARNESS):
        pytest.skip(f"render harness missing at {HARNESS}")

    env = dict(os.environ)
    roots = [os.path.join(os.path.dirname(HARNESS), "node_modules"),
             os.path.join(ROOT, "node_modules")]
    if env.get("NODE_PATH"):
        roots.append(env["NODE_PATH"])
    env["NODE_PATH"] = os.pathsep.join(roots)

    try:
        out = subprocess.run([node, HARNESS], capture_output=True, text=True,
                             timeout=180, cwd=ROOT, env=env)
    except subprocess.TimeoutExpired:                         # pragma: no cover
        pytest.fail("render harness timed out after 180s")
    if out.returncode != 0:
        missing = _MODULE_MISSING.search(out.stderr or "")
        if missing:
            pytest.skip(f"node module {missing.group(1)!r} is not installed — run:  "
                        f"{INSTALL_HINT}  (the static pins above still ran)")
        pytest.fail(f"render harness exited {out.returncode} — a REAL failure, not a missing "
                    f"dependency:\n{(out.stderr or '')[-3000:]}")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:                       # pragma: no cover
        pytest.fail(f"render harness emitted non-JSON ({exc})")


def test_the_reset_action_was_found_in_the_rendered_console(render):
    r = render.get("admins_reset") or {}
    assert r.get("button_found") is True, (
        "no 'reset pw' button rendered in the accounts → login oversight table — the "
        "break-glass action is unreachable in the UI")


def test_clicking_reset_makes_no_api_call_until_confirmed(render):
    r = render.get("admins_reset") or {}
    assert r.get("click_makes_no_api_call") is True, (
        "the 'reset pw' click fired the break-glass reset IMMEDIATELY — a mis-click kills a "
        "customer admin's password with zero confirmation")
    assert r.get("modal_shown") is True, "no confirmation modal appeared on the reset click"
    assert r.get("modal_closed_after_confirm") is True, (
        "the modal did not close when the operator confirmed — the confirm path is broken")


def test_the_confirm_names_the_target_and_the_consequence(render):
    r = render.get("admins_reset") or {}
    assert r.get("modal_names_target") is True, "confirm modal does not name the admin"
    assert r.get("modal_says_password_stops") is True, (
        "confirm modal does not say the current password stops working")


def test_confirming_posts_exactly_one_break_glass_call(render):
    r = render.get("admins_reset") or {}
    assert r.get("confirm_posts_exactly_one") is True, (
        "confirming did not produce exactly one api call")
    assert r.get("post_is_breakglass_route") is True, (
        "the confirmed call is not POST /admin/dashboard-users/{id}/reset-password — the "
        "button has drifted off the break-glass route")


def test_the_temp_password_lands_in_the_once_only_secret_box(render):
    r = render.get("admins_reset") or {}
    assert r.get("temp_shown_once_secret_box") is True, (
        "the temp password was not displayed in the once-only secret-box after the reset")


def test_the_break_glass_route_is_pinned_in_the_source_too():
    """Belt-and-suspenders for the harness-skipped environments: app.js must still reference
    the break-glass route from inside the confirm-gated reset action (a rename on either side
    leaves this dangling)."""
    src = open(APP_JS, encoding="utf-8").read()
    m = re.search(r"async function resetAdminPassword\(.*?\n\}", src, re.S)
    assert m, "resetAdminPassword vanished from app.js"
    body = m.group(0)
    assert "confirmModal(" in body, "resetAdminPassword is no longer confirm-gated"
    assert "/reset-password" in body and "dashboard-users" in body, (
        "resetAdminPassword no longer targets the break-glass route")
