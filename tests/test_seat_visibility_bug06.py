"""BUG-06 (P2, operator-reported, 2026-10-31): individual-seat login emails invisible on
global-admin seat surfaces + no active/revoked status filter on the seats table.

Verdicts over ``tests/dashboard/render_seat_visibility.js`` (jsdom drive of the REAL
index.html + app.js in demo mode), plus static pins. Observations live in the harness;
pass/fail lives here, per house convention (see test_operator_console_ux.py).

Fix surface (all client-side; the backend already folds login_email at
saas/admin.py list_seats via user_console.login_map_for_account):
  * Users-tab seat cards render .sc-login-email on BOTH tabs (pre-existing on the card
    header; pinned here so it cannot regress silently);
  * the Overview glance table gains a Login column (email or an explicit 'no login');
  * status-filter chips (#users-status-filter, roster kind-filter idiom) narrow the seat
    table by exact account_users.status, jumping to the tab that can hold the status —
    and a MANUAL tab click resets the chip instead of rendering an empty table;
  * a non-active card carries a ONE-CLICK 're-activate seat' lever (POST assign door with
    the same user_id = register_seat's reactivate branch); the retire/deprovision lever
    stays in the active card's actions dropdown (2 clicks);
  * the Subscribers individuals table keeps u.email as column 1; roster rows are ACCOUNTS
    (the Singles house account is one row) and carry no seat email by design — pinned by
    the docstring static check below so the "why not" is enforced, not just written.
"""
import json
import os
import re
import shutil
import subprocess
import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "dashboard" / "render_seat_visibility.js"
APP = ROOT / "saas" / "dashboard" / "app.js"
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
        pytest.fail(f"render harness emitted non-JSON on stdout ({exc})")


# ── (a) login email visible on every seat-list surface ─────────────────────────────

def test_users_tab_removed_cards_render_login_email(report):
    by_seat = {c["seat"]: c for c in report["removed_tab_cards"]}
    bug01 = by_seat["74d54e93-0000-4000-8000-00000000000a"]
    assert bug01["email"] == "owner.demo@example.com", (
        "the BUG-01 revoked seat must be identifiable by its login email on the removed tab")
    assert bug01["has_reactivate"], "a removed card carries the re-activate lever"
    for c in report["active_tab_cards"]:
        assert "email" in c  # shape guard; demo actives carry no login (pinned below)


def test_overview_glance_table_has_login_column(report):
    assert report["overview_header_has_login"]
    rows = report["overview_glance"]
    assert rows, "glance table rendered no rows"
    revoked = [cells for cells in (r["cells"] for r in rows)
               if "owner.demo@example.com" in cells]
    assert revoked, "the BUG-01 seat's email must render in the Overview glance table"
    for cells in (r["cells"] for r in rows):
        assert cells[1] == "no login" or "@" in cells[1], (
            "every glance row shows an email or an explicit 'no login', never a blank cell")


# ── (b) status filter ───────────────────────────────────────────────────────────────

def test_status_chips_present_with_counts(report):
    chips = {c["key"]: c for c in report["chips"]}
    assert set(chips) >= {"all", "active", "revoked"}, (
        "chips must cover at least all/active/revoked")
    assert "5" in chips["all"]["label"] and "2" in chips["active"]["label"] and \
        "2" in chips["revoked"]["label"] and "1" in chips["deprovisioned"]["label"], \
        "chip counts reflect the demo payload (2 active, 2 revoked, 1 deprovisioned)"


def test_revoked_chip_shows_only_revoked_and_jumps_tab(report):
    a = report["after_revoked_chip"]
    assert a["users_tab"] == "removed"
    assert len(a["shown"]) == 2   # the login-fold revoked seat + the owui-only revoked seat
    assert a["chip_active"]


def test_active_chip_returns_to_active_tab(report):
    a = report["after_active_chip"]
    assert a["users_tab"] == "active"
    assert len(a["shown_statuses"]) == 2   # the two demo active seats


def test_manual_tab_click_resets_chip_not_empty_table(report):
    a = report["after_manual_tab_click"]
    assert a["users_tab"] == "active"
    assert a["seat_status"] == "all", "a manual tab click resets the status chip to all"
    assert a["shown"] == 2, "no intentionally-empty filtered table after switching tabs"


# ── (c) BUG-01 shape: findable by email, lever in <=2 clicks ────────────────────────

def test_revoked_seat_findable_by_email_with_one_click_lever(report):
    assert report["bug01_findable_by_email"]
    assert report["bug01_card"]["reactivate_button_text"] == "↻ re-activate seat"


def test_reactivate_lever_confirms_then_reactivates(report):
    m = report["reactivate_modal"]
    assert m["shown"] and m["title"] == "Re-activate seat"
    assert m["body_mentions_email"], "the confirm names the seat by its email handle"
    a = report["after_reactivate"]
    assert a["seat_status_now"] == "active"
    assert a["duplicate_created"], "reactivation must not mint a second seat row"
    assert a["users_tab"] == "active" and a["seat_status_filter"] == "all"


def test_retire_lever_reachable_on_active_cards(report):
    assert report["retire_lever"]["menu_opens"], (
        "the actions dropdown (deprovision inside) must still open on active cards")


# ── static half: every seat-list renderer references login_email or documents why ────

def test_every_seat_list_renderer_pins_login_email():
    src = APP.read_text(encoding="utf-8")
    for anchor, needle in [
        ("function renderUsers()", "u.login_email"),
        ("function loadOverviewUsers()", "u.login_email"),
        ("for (const u of rows) {", "u.email"),   # Subscribers individuals table
    ]:
        i = src.find(anchor)
        assert i >= 0, f"renderer {anchor!r} not found — app.js restructured?"
        window = src[i:i + 4000]
        assert needle in window or "BUG-06" in window, (
            f"renderer near {anchor!r} neither renders {needle} nor carries a BUG-06 why-not note")
    j = src.find("function buildRosterRow(")
    assert j >= 0
    assert "BUG-06" in src[j - 1500:j], (
        "buildRosterRow's BUG-06 why-no-seat-email docstring was dropped")


# ── scope extension (live prod evidence: ct8ball seat invisible, owui_email NULL) ────

def test_older_seat_with_null_login_falls_back_to_owui_email(report):
    assert report["owui_fallback"]["legacy_seat_email"] == "legacy.seat@example.com", (
        "a revoked seat whose only handle is account_users.owui_email must render that "
        "email (fallback chain login_email -> owui_email), never a bare uuid")


def test_revoked_chip_shows_both_revoked_seats_with_handles(report):
    a = report["after_revoked_chip"]
    emails = sorted(x["email"] for x in a["shown"])
    assert emails == ["legacy.seat@example.com", "owner.demo@example.com"], (
        "the revoked chip must surface BOTH the login-fold seat and the owui-only seat")


def test_delete_lever_present_danger_and_functional(report):
    d = report["delete_lever"]
    assert d["card_present"] and d["menu_item_present"] and d["menu_item_danger"], (
        "the soft-delete lever must sit in the actions menu, danger-styled")
    a = d["after"]
    # honest post-state (mirrors the real fold): the login ROW survives — has_login stays
    # true, login_status flips to 'disabled' (the address stays claimed; restore lives in
    # Moderation). The SEAT row is untouched by the login delete.
    assert a["seat_has_login"] is True, "the login row survives the soft-delete (claimed address)"
    assert a["login_status"] == "disabled", "the login flips to disabled"
    assert a["seat_status"] == "active", "soft-delete keeps the SEAT (only the login goes)"
    assert a["msg_ok"], "the operator gets an explicit soft-deleted confirmation"


def test_backend_folds_owui_email_and_individual_id():
    src = (ROOT / "saas" / "admin.py").read_text(encoding="utf-8")
    i = src.find("def list_account_seats(")
    assert i >= 0
    block = src[i:i + 2500]
    assert "include_upstream_identity=_is_operator" in block, (
        "the PII opt-in must stay gated on the operator realm")
    assert 'seat["login_individual_id"]' in block, (
        "the moderation verbs need the individual_id folded onto the seat")
    app = APP.read_text(encoding="utf-8")
    assert "function seatEmailFor(u)" in app and "u.owui_email" in app, (
        "the login_email -> owui_email fallback chain must survive in app.js")
