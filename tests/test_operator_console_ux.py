"""Operator console IA / legibility / findability — the contract this branch must not break.

TWO KINDS OF EVIDENCE, kept separate on purpose:

  RENDERED  ``tests/dashboard/render_operator_console.js`` runs the REAL index.html plus the
            real feature_copy.js / app.js / tour.js in jsdom and drives the real router,
            painters and tour engine. It proves WHICH elements exist with what text, classes
            and wiring, and WHICH code path a click takes. jsdom does not lay out and does
            not apply CSS, so it proves behaviour, never pixels.

  STATIC    Everything pixel-shaped — the contrast tokens, the type hierarchy, the sticky
            rule — is asserted against the real stylesheet here, with the WCAG ratios
            computed rather than eyeballed.

┌─ RUNNING THE RENDERED HALF ────────────────────────────────────────────────────────┐
│                                                                                    │
│     npm install --prefix tests/dashboard                                           │
│                                                                                    │
│ That is the whole setup. ``tests/dashboard/package.json`` declares jsdom; node      │
│ resolves it out of ``tests/dashboard/node_modules`` on its own, because that is the │
│ harness script's own directory.                                                     │
│                                                                                    │
│ ``node_modules`` is deliberately NOT committed — it is large, platform-specific,    │
│ and ``tests/`` is gitignored wholesale, so it could not be committed by accident    │
│ even if someone tried. WITHOUT it, the ~32 rendered cases SKIP with that exact      │
│ command in the skip reason and the static assertions still run. They do not ERROR,  │
│ and they do not silently pass.                                                      │
│                                                                                    │
│ This note exists because the first cut of this file got it wrong in a way worth     │
│ recording: the guard only caught "node missing" and "failed to LAUNCH". A missing   │
│ dependency is neither — node launches fine and then exits 1 with                    │
│ "Cannot find module 'jsdom'". So on any machine but the author's, 32 of 44 cases    │
│ ERRORED on setup. A suite that errors for everyone else gets labelled broken and    │
│ ignored, which is how coverage dies for good; and the errored half was precisely    │
│ the evidence that needed EXECUTION to be believed. Hence the three-way split in the │
│ fixture below — absent dependency SKIPS with the remedy, a genuinely broken harness │
│ still FAILS loudly. See CLAUDE.md, "A GREEN SUITE IS NOT EVIDENCE".                 │
└────────────────────────────────────────────────────────────────────────────────────┘

Why these assertions exist, one line each:
  * the router binds ``.navrail .nav-item`` by data-view; re-nesting the nav inside
    <details> must stay transparent to it, and ``a.nav-ext`` must stay unbound.
    ⚠️ The router class was ``.sidenav`` until the 2026-08-12 IA restructure (tenant left ·
    operational right · the operator's own account in a bottom-left popup). ``.sidenav`` is
    now the RAIL-STYLING class carried by the two rails only, so a nav item in the popup
    would have been INERT under the old descendant selector — it would render, look
    clickable, and do nothing. ``.navrail`` is carried by all three containers. The split
    itself is asserted in tests/test_operator_ia_and_status_banner.py;
  * ``--muted`` on the dark canvas measured 3.34:1 — below WCAG 2.2 SC 1.4.3 AA (4.5:1)
    for every 10–13px string in the console;
  * the comp / mark-as-paid / extend-trial controls are painted from JS template literals,
    so a search of the markup cannot find them — the action index is what makes them
    findable, and it must resolve to views that actually exist;
  * ``openClient`` is async and ends in switchView, so ``openClient(id); switchView('billing')``
    raced and bounced the operator off Billing onto Overview;
  * the log/audit "all accounts" toggles were removed — both views must now be
    unconditionally tenant-scoped, and must not fire a request with no tenant selected.
"""
import html.parser
import inspect
import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "saas" / "dashboard"
HARNESS = ROOT / "tests" / "dashboard" / "render_operator_console.js"

CSS = (DASH / "style.css").read_text(encoding="utf-8")
INDEX = (DASH / "index.html").read_text(encoding="utf-8")
# Comments legitimately quote the OLD values they replaced; strip them before asserting.
CSS_LIVE = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)

TENANT_VIEWS = {"overview", "plan", "admins", "users", "billing", "logs", "danger"}


# ══════════════════════════════════════════════════════════════════════════════════════
# EXPECTED_VIEWS — DERIVED FROM THE MARKUP, NOT A HAND-TYPED CONSTANT
# ══════════════════════════════════════════════════════════════════════════════════════
# 🔴 WHY THIS IS NOT `EXPECTED_VIEWS = 24` ANY MORE. It was, and the operator-IA restructure
# shipped with FOUR RED TESTS because of it — including the three that exist precisely to
# prove the restructure broke nothing:
#     test_switchview_routes_every_view_without_throwing
#     test_clicking_the_nav_navigates_and_disabled_items_stay_inert
#     test_header_shows_only_on_tenant_scoped_views
# The IA change was landed with its own safety net already failing, so a nav item that HAD
# gone inert would have been indistinguishable from the stale count. A literal that has to be
# hand-edited every time the nav grows is a literal that will be stale exactly when it matters.
#
# 🔴 AND WHY THIS IS STILL A REAL ASSERTION, NOT A TAUTOLOGY. The number is derived from the
# STATIC MARKUP (index.html, parsed here), while every test below compares it against what the
# JSDOM HARNESS actually rendered and clicked. Two independent readings of the same page must
# agree — so "a nav item exists in the HTML but the router never reaches it" is still a RED.
# What is no longer red is "somebody added a nav item and did not edit this file".
_NAV_ITEM_VIEW_RE = re.compile(
    r'<(?:button|a)\b[^>]*\bclass="[^"]*\bnav-item\b[^"]*"[^>]*\bdata-view="([a-z0-9_-]+)"',
    re.I)
#: Also match the attribute order the other way round — the markup is hand-written and both
#: orders occur in this repo. Missing one silently under-counts, which is the same defect.
_NAV_VIEW_ITEM_RE = re.compile(
    r'<(?:button|a)\b[^>]*\bdata-view="([a-z0-9_-]+)"[^>]*\bclass="[^"]*\bnav-item\b[^"]*"',
    re.I)


def _nav_views_from_markup(html: str) -> set:
    return set(_NAV_ITEM_VIEW_RE.findall(html)) | set(_NAV_VIEW_ITEM_RE.findall(html))


NAV_VIEWS = _nav_views_from_markup(INDEX)
EXPECTED_VIEWS = len(NAV_VIEWS)


def test_the_derived_nav_count_is_sane_before_anything_relies_on_it():
    """A guard on the derivation itself. If the regex stops matching (markup reshaped, class
    renamed), EXPECTED_VIEWS silently collapses toward 0 and every count assertion below
    becomes vacuous — a derived constant fails OPEN unless something pins its floor."""
    assert EXPECTED_VIEWS >= 20, (
        "only %d nav views were parsed out of index.html — the derivation in this file has "
        "probably stopped matching the markup, which would make every count assertion below "
        "vacuous. Parsed: %s" % (EXPECTED_VIEWS, sorted(NAV_VIEWS)))
    missing = TENANT_VIEWS - NAV_VIEWS
    assert not missing, (
        "tenant-scoped views named in TENANT_VIEWS are absent from the nav markup: %s"
        % sorted(missing))


# ════════════════════════════════════════════════════════════════════════════════════
# RENDERED — the real page, the real router, the real painters
# ════════════════════════════════════════════════════════════════════════════════════

INSTALL_HINT = "npm install --prefix tests/dashboard"

# Node reports an unresolvable require() as ERR_MODULE_NOT_FOUND / MODULE_NOT_FOUND with this
# text on stderr. Matched on the MESSAGE rather than the exit code because a harness that is
# merely missing its dependency and a harness that is genuinely broken both exit 1.
_MODULE_MISSING = re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")


@pytest.fixture(scope="module")
def report():
    """Run the real render harness, or skip with the one command that fixes it.

    THREE outcomes, deliberately distinct — the whole point of this fixture:
      * toolchain absent (no node, no harness file) → SKIP, naming what is missing;
      * dependency absent ("Cannot find module")    → SKIP, naming the install command;
      * anything else                               → FAIL, loudly, with stderr.

    The third branch is why this is not simply ``skip on any non-zero``: a harness broken by
    a real regression must not be able to disguise itself as an environment problem. (The
    sibling ``tests/test_seat_upstream_identity.py`` skips on ANY non-zero return and so has
    exactly that blind spot — noted, not changed here.)
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH — install Node.js to run the rendered half; "
                    "the static assertions in this module still run")
    if not HARNESS.exists():
        pytest.skip(f"render harness missing at {HARNESS.relative_to(ROOT)} — "
                    "the static assertions in this module still run")

    env = dict(os.environ)
    # Node already resolves from the harness's own tests/dashboard/node_modules; these extra
    # roots just let a repo-root or pre-existing install satisfy it too.
    roots = [str(HARNESS.parent / "node_modules"), str(ROOT / "node_modules")]
    if env.get("NODE_PATH"):
        roots.append(env["NODE_PATH"])
    env["NODE_PATH"] = os.pathsep.join(roots)

    try:
        out = subprocess.run([node, str(HARNESS)], capture_output=True, text=True,
                             timeout=180, cwd=str(ROOT), env=env)
    except subprocess.TimeoutExpired:                         # pragma: no cover
        pytest.fail("render harness timed out after 180s — it hangs or waits on the network")
    except OSError as exc:                                    # pragma: no cover
        pytest.skip(f"render harness failed to launch: {exc}")

    if out.returncode != 0:
        missing = _MODULE_MISSING.search(out.stderr or "")
        if missing:
            pytest.skip(
                f"node module {missing.group(1)!r} is not installed — run:  {INSTALL_HINT}  "
                f"(declared in tests/dashboard/package.json; node_modules is intentionally "
                f"not committed). The static assertions in this module still run.")
        pytest.fail(f"render harness exited {out.returncode} — this is a REAL failure, not a "
                    f"missing dependency:\n{(out.stderr or '')[-3000:]}")

    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:                       # pragma: no cover
        pytest.fail(f"render harness emitted non-JSON on stdout ({exc}); "
                    f"stderr tail:\n{(out.stderr or '')[-2000:]}")


# ── router contract ────────────────────────────────────────────────────────────────

def test_all_24_nav_items_survive_the_resection(report):
    m = report["markup"]
    assert m["nav_item_count"] == EXPECTED_VIEWS
    assert len(set(m["nav_views"])) == EXPECTED_VIEWS, "a data-view was duplicated"
    assert m["all_nav_items_are_buttons"], "the router installs onclick on buttons"


def test_every_nav_view_has_a_panel_and_every_panel_a_nav_entry(report):
    m = report["markup"]
    assert m["nav_views_without_panel"] == []
    assert m["panels_without_nav"] == []


def test_the_seven_tenant_scoped_cs_markers_survive(report):
    m = report["markup"]
    assert set(m["cs_views"]) == TENANT_VIEWS
    assert m["cs_all_start_disabled"]


def test_benchmarks_link_stays_an_unbound_anchor(report):
    """a.nav-ext plain-navigates to bench.html; widening the router selector would break it."""
    m = report["markup"]
    assert m["nav_ext_count"] == 1 and m["nav_ext_is_anchor"]
    assert not m["nav_ext_is_nav_item"]
    assert not m["nav_ext_has_data_view"]
    assert not report["clicks"]["nav_ext_onclick_installed"]


def test_switchview_routes_every_view_without_throwing(report):
    r = report["router"]
    assert r["errors"] == []
    globals_ = {v: d for v, d in r["routes"].items() if v not in TENANT_VIEWS}
    assert len(globals_) == EXPECTED_VIEWS - len(TENANT_VIEWS)
    for v, d in globals_.items():
        assert d["landed"] == v and d["active"] == v and d["panel_visible"], f"{v} → {d}"


def test_tenant_views_still_bounce_to_the_roster_with_no_tenant(report):
    """Pre-existing guard — the re-section must not have changed it."""
    for v in TENANT_VIEWS:
        assert report["router"]["routes"][v]["landed"] == "clients"


def test_clicking_the_nav_navigates_and_disabled_items_stay_inert(report):
    c = report["clicks"]
    assert c["enabled_navigated"] == EXPECTED_VIEWS - len(TENANT_VIEWS)
    assert c["disabled_inert"] == len(TENANT_VIEWS)


def test_set_client_nav_enabled_still_drives_all_seven(report):
    assert report["cs_toggle"]["all_enabled_on_true"]
    assert report["cs_toggle"]["all_disabled_on_false"]


# ── nav sections ───────────────────────────────────────────────────────────────────

def test_nav_is_sectioned_with_the_consoles_own_details_idiom(report):
    """Still 6 sections, now split across TWO rails (IA restructure: tenant left,
    operational right). The one relaxation: a nav item may ALSO live in the bottom-left
    operator popup, which IS itself a disclosure and is therefore not additionally wrapped
    in a <details>. Rail items are unchanged — every one is still inside a section."""
    m = report["markup"]
    assert m["section_count"] == 6
    assert m["sections_all_have_summary"]
    assert m["every_nav_item_inside_a_section"]
    assert m["every_rail_nav_item_inside_a_section"]
    # data-remember is what wireDetailsMemory() persists; without it a section forgets itself.
    assert all(k for k in m["section_keys"]), m["section_keys"]


def test_the_two_billing_entries_live_in_different_sections(report):
    """Manage→billing-overview and Tenant→billing shared the identical label 'Billing'."""
    where = report["markup"]["section_of"]
    assert where["billing-overview"] == "nav-sect-money"
    assert where["billing"] == "nav-sect-client"
    assert 'data-view="billing-overview">Billing overview<' in INDEX


def test_a_collapsed_section_still_shows_its_pending_count(report):
    """Collapsing must not trade legibility for a missed approval queue."""
    roll = report["rollup"]
    assert roll["closed"]["text"] == "5" and roll["closed"]["hidden"] is False
    assert roll["open"]["hidden"] is True, "an open section's items carry their own badges"
    assert roll["non_numeric"] == "!", "the non-numeric 'offline' badge must not roll up as NaN"


def test_the_tour_reaches_targets_inside_collapsed_sections(report):
    """The documented spotlight trap: a target in a closed <details> has a zero rect."""
    t = report["tour"]
    assert t["public_surface"]
    assert t["tenants_section_opened_by_tour"], "create-tenant could not reach its nav target"
    assert t["client_section_opened_by_tour"], "event-log could not reach the Audit nav target"
    assert any(s["hole_painted"] for s in t["create_tenant_steps"]), t["create_tenant_steps"]


# ── tenant header ──────────────────────────────────────────────────────────────────

def test_header_shows_only_on_tenant_scoped_views(report):
    h = report["header"]
    assert set(h["shown_on"]) == TENANT_VIEWS
    assert h["hidden_on_count"] == EXPECTED_VIEWS - len(TENANT_VIEWS)


def test_header_renders_without_the_removed_nav_sub_label(report):
    assert not report["markup"]["nav_client_name_present"]
    assert report["header"]["ch_name_text"] == "Northwind Ltd"


def test_header_has_an_exit_and_a_switch_affordance(report):
    assert report["markup"]["header_exit_buttons"] == ["btn-ch-roster", "btn-ch-close"]
    ex = report["header_exit"]
    assert ex["all_tenants"] == {"view": "clients", "current": "acct-1"}, "browsing keeps the selection"
    assert ex["close_tenant"]["current"] == ""
    assert ex["close_tenant"]["cs_all_disabled"]
    assert ex["close_tenant"]["header_hidden"]


# ── the openClient race ────────────────────────────────────────────────────────────

def test_openclient_race_is_reproduced_and_fixed(report):
    """`openClient(id); switchView('billing')` loses to the awaited continuation."""
    r = report["race"]
    assert r["legacy_shape_lands_on"] == "overview", "the race no longer reproduces — re-check the harness"
    assert r["with_target_arg_lands_on"] == "billing"
    assert r["default_target_lands_on"] == "overview", "the default landing must be unchanged"


# ── findability: the action index + search ─────────────────────────────────────────

def test_every_action_resolves_to_a_real_view(report):
    assert report["scripts"]["actions_with_unknown_view"] == []
    assert len(report["scripts"]["actions"]) >= 10


@pytest.mark.parametrize("term,view", [
    ("comped", "billing"), ("comp", "billing"),
    ("mark as paid", "billing"), ("paid", "billing"),
    ("extend trial", "subscribers"), ("trial", "subscribers"),
])
def test_the_owners_own_words_find_the_control(report, term, view):
    """The three controls the owner could not find, searched for in HIS words."""
    hits = report["search"]["queries"][term]
    assert hits, f"'{term}' returned nothing"
    assert hits[0]["view"] == view, hits[:3]


def test_search_also_finds_views_and_tenants(report):
    q = report["search"]["queries"]
    assert any(h["view"] == "trash" for h in q["trash"])
    assert any(h["kind"] == "tenant" and h["accountId"] == "acct-1" for h in q["northwind"])
    assert any(h["kind"] == "tenant" for h in q["acme clinic"])


def test_search_does_not_thrash_on_noise(report):
    q = report["search"]["queries"]
    assert q["c"] == [], "a single character must not open the dropdown"
    assert q["zzzqqq"] == []
    assert len(q["a"]) <= 8, "results are capped"


def test_search_navigates_on_activation(report):
    s = report["search"]
    assert s["dropdown"]["visible"] and s["dropdown"]["hit_count"] >= 1
    assert s["dropdown"]["first_preselected"], "Enter needs a pre-selected row"
    assert s["activate_paid"]["landed"] == "billing"
    assert s["activate_paid"]["box_closed"] and s["activate_paid"]["input_cleared"]
    assert s["activate_tenant"] == {"current": "acct-1", "landed": "overview"}


def test_a_tenant_scoped_hit_explains_itself_instead_of_no_opping(report):
    t = report["search"]["tenant_scoped_no_tenant"]
    assert "pick a tenant first" in t["hint_text"]
    assert t["landed"] == "clients"
    assert "per-tenant control" in t["message"]


# ── billing controls: read the PAINTER'S output ────────────────────────────────────

def test_billing_controls_use_the_operators_vocabulary(report):
    b = report["billing"]["normal"]
    assert "mark as paid" in (b["mark_paid_label"] or "").lower()
    assert "comp account" in (b["comp_label"] or "").lower()
    assert "record payment" not in b["panel_text"].lower(), "the accounting jargon label is gone"
    assert b["mark_paid_title"] and b["comp_title"], "both carry an explanatory title"
    assert "restore billing" in report["billing"]["comped"]["comp_label"].lower()


def test_extend_trial_is_labelled_and_sized_to_be_found(report):
    e = report["extend_trial"]
    assert e is not None
    assert e["label"] == "extend trial", "bare 'extend' beside a Trial column is ambiguous"
    assert "sm" in e["classes"] and "xs" not in e["classes"].split()
    assert e["title"]


# ── firehose removal ───────────────────────────────────────────────────────────────

def test_the_scope_changing_firehose_toggles_are_gone(report):
    m = report["markup"]
    assert not m["log_firehose_present"]
    assert not m["audit_firehose_present"]
    assert m["log_scope_note"] == "this tenant · newest first"


def test_log_and_audit_are_unconditionally_tenant_scoped(report):
    s = report["log_scope"]
    assert len(s["calls_with_tenant"]) == 2
    assert all("account_id=acct-1" in c for c in s["calls_with_tenant"]), s["calls_with_tenant"]
    assert s["calls_without_tenant"] == [], "no tenant selected must mean no request at all"
    assert s["scope_note_after_query"] == "this tenant · newest first"


# ── CSP ────────────────────────────────────────────────────────────────────────────

def test_dashboard_html_stays_csp_clean(report):
    """script-src 'self' with no 'unsafe-inline' — an inline-script build renders BLANK."""
    m = report["markup"]
    assert m["inline_on_handlers"] == 0
    assert m["inline_script_bodies"] == 0
    assert not m["has_javascript_url"]


# ════════════════════════════════════════════════════════════════════════════════════
# STATIC — the pixel-shaped half, computed against the real stylesheet
# ════════════════════════════════════════════════════════════════════════════════════

def _luminance(hex_colour):
    h = hex_colour.lstrip("#")
    chans = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        chans.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]


def contrast(fg, bg):
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _token(block_selector, name):
    """Read a custom property out of a specific :root block in the real stylesheet."""
    m = re.search(re.escape(block_selector) + r"\s*\{(.*?)\}", CSS, re.S)
    assert m, f"{block_selector} block not found"
    t = re.search(re.escape(name) + r":\s*(#[0-9a-fA-F]{6})", m.group(1))
    assert t, f"{name} not found in {block_selector}"
    return t.group(1)


AA_SMALL_TEXT = 4.5   # WCAG 2.2 SC 1.4.3, Level AA, text below 18.66px


def test_muted_text_clears_wcag_aa_on_both_dark_surfaces():
    """Every --muted string in this console is 10–13px; #666666 measured 3.34 / 3.15."""
    muted = _token(":root", "--muted")
    bg = _token(":root", "--bg")
    bg_soft = _token(":root", "--bg-soft")
    assert contrast(muted, bg) >= AA_SMALL_TEXT, f"{muted} on {bg} = {contrast(muted, bg):.2f}"
    assert contrast(muted, bg_soft) >= AA_SMALL_TEXT, f"{muted} on {bg_soft} = {contrast(muted, bg_soft):.2f}"


def test_muted_stays_visibly_secondary_to_the_foreground():
    """Fixing contrast must not flatten the type hierarchy into one grey."""
    muted, fg, bg = _token(":root", "--muted"), _token(":root", "--fg"), _token(":root", "--bg")
    assert contrast(fg, bg) > contrast(muted, bg) + 3


def test_light_theme_muted_and_accent_clear_wcag_aa():
    sel = ':root[data-theme="light"]'
    bg = _token(sel, "--bg")
    for name in ("--muted", "--accent"):
        val = _token(sel, name)
        assert contrast(val, bg) >= AA_SMALL_TEXT, f"{name} {val} on {bg} = {contrast(val, bg):.2f}"


def test_dark_accent_is_unchanged_and_already_passing():
    accent, bg = _token(":root", "--accent"), _token(":root", "--bg")
    assert accent.upper() == "#3FB950"
    assert contrast(accent, bg) >= AA_SMALL_TEXT


def test_the_accent_picker_can_actually_reach_every_tint():
    """Literal rgba(63,185,80,…) tints kept the console green on every other swatch."""
    assert "rgba(63,185,80" not in CSS_LIVE.replace(" ", "")
    assert "color-mix(in srgb, var(--accent)" in CSS_LIVE


def test_nav_section_heading_outranks_its_own_items():
    """The old group heading was 10px over 12px items — the label was quieter than its children."""
    summary = re.search(r"\.sidenav details\.nav-sect > summary \{(.*?)\}", CSS, re.S)
    item = re.search(r"\.sidenav \.nav-item \{(.*?)\}", CSS, re.S)
    assert summary and item
    s_size = float(re.search(r"font-size:\s*([\d.]+)px", summary.group(1)).group(1))
    i_size = float(re.search(r"font-size:\s*([\d.]+)px", item.group(1)).group(1))
    assert s_size > i_size, f"heading {s_size}px must outrank items {i_size}px"
    assert "font-weight: 500" in summary.group(1), "300/400/500 only — 700 is a synthesized fake-bold"
    assert "var(--fg)" in summary.group(1) and "var(--muted)" in item.group(1)


def test_the_self_hosted_font_ships_no_face_above_500():
    """Guards the rule above: 500 is the heaviest REAL weight, so 700 would be fake-bold.

    Asserts on the @font-face declarations — the faces actually bundled and served — not on
    every font-weight in the file. Those differ, and the gap is a real pre-existing finding
    rather than a bug this branch introduced: ~21 rules elsewhere in this stylesheet ask for
    600/700 (.bench-score-big, .sub-tile .t-v, .lb-rank, .cortex-stat .cs-val, …) and the
    browser SYNTHESIZES them by smearing the 500 face. Recorded here so a future weight
    choice is made knowingly; not changed, because restyling 21 unrelated components is
    nothing to do with the operator nav.
    """
    faces = {int(w) for w in re.findall(
        r"@font-face\s*\{[^}]*?font-weight:\s*(\d{3})", CSS, re.S)}
    assert faces == {300, 400, 500}, sorted(faces)


def test_tenant_name_reads_as_a_title_not_a_big_sentence():
    m = re.search(r"\.client-header \.ch-name \{(.*?)\}", CSS, re.S)
    assert m and "font-weight: 500" in m.group(1), m.group(1) if m else "rule missing"


def test_tenant_header_is_sticky_so_identity_cannot_scroll_away():
    m = re.search(r"^\.client-header \{(.*?)\}", CSS, re.S | re.M)
    assert m and "position: sticky" in m.group(1)
    # ...and is explicitly un-stuck on the narrow layout, where it would eat the viewport.
    assert "position: static !important" in CSS


def test_search_box_survives_the_narrow_wrapping_flex_nav():
    """At <=760px .sidenav is a wrapping flex ROW; an item with no basis collapses."""
    narrow = CSS[CSS.index("@media (max-width: 760px)"):]
    assert re.search(r"\.sidenav \.nav-find \{[^}]*flex:\s*1 0 100%", narrow)
    assert re.search(r"\.sidenav details\.nav-sect \{[^}]*flex:\s*1 0 100%", narrow)


def test_the_action_index_lives_in_the_single_source_of_truth():
    """Three copies of the MCP tool descriptions drifted once; do not start a fourth here."""
    fc = (DASH / "feature_copy.js").read_text(encoding="utf-8")
    assert "window.FL_ACTIONS" in fc
    app = (DASH / "app.js").read_text(encoding="utf-8")
    # An ASSIGNMENT, not a mention — app.js documents where the index lives in a comment.
    assert not re.search(r"window\.FL_ACTIONS\s*=", app), "app.js must READ the index, never redefine it"
    assert "window.FL_ACTIONS" in app, "app.js must actually consume it"


def test_no_dashboard_html_carries_an_inline_handler():
    for page in ("index.html", "tenant.html", "user.html", "bench.html", "leaderboard.html"):
        text = (DASH / page).read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", text), page
        assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>\s*\S", text), page


# ════════════════════════════════════════════════════════════════════════════════════
# THE HARNESS'S OWN RUNNABILITY — these run with or without jsdom, by construction
# ════════════════════════════════════════════════════════════════════════════════════
# The rendered half of this module was, for one commit, unrunnable by anyone but its author:
# jsdom was installed only in the author's throwaway worktree, declared nowhere, and the skip
# guard did not recognise the failure — so 32 of 44 cases ERRORED on setup elsewhere. These
# assertions exist so that cannot recur silently.

def test_the_render_harness_dependency_is_declared():
    """A dependency nobody can install is a dependency nobody has."""
    pkg_path = HARNESS.parent / "package.json"
    assert pkg_path.exists(), (
        f"{pkg_path.relative_to(ROOT)} is missing — the jsdom harnesses in this directory "
        f"would then be unrunnable from a fresh checkout")
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    declared = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    assert "jsdom" in declared, declared
    assert INSTALL_HINT in pkg.get("description", ""), (
        "the install command belongs where someone looking at the manifest will see it")


def test_node_modules_is_not_committed():
    """Large, platform-specific, and tests/ is gitignored wholesale — it cannot ride along."""
    tracked = subprocess.run(["git", "ls-files", "tests/dashboard/"],
                             capture_output=True, text=True, cwd=str(ROOT))
    if tracked.returncode != 0:                               # pragma: no cover
        pytest.skip("not a git checkout")
    files = [f for f in tracked.stdout.split() if f]
    assert not any("node_modules" in f for f in files), \
        [f for f in files if "node_modules" in f][:5]
    # ...and the harness + its manifest ARE tracked, or a fresh clone gets neither.
    assert "tests/dashboard/render_operator_console.js" in files, files
    assert "tests/dashboard/package.json" in files, files


def test_the_skip_guard_recognises_a_missing_module():
    """The exact failure the first cut missed: node launches, then exits 1 on resolution.

    Asserted against the real regex the fixture uses, on real node stderr text.
    """
    real_stderr = (
        "node:internal/modules/cjs/loader:1386\n  throw err;\n  ^\n\n"
        "Error: Cannot find module 'jsdom'\nRequire stack:\n"
        "- /repo/tests/dashboard/render_operator_console.js\n"
        "    at Module._resolveFilename (node:internal/modules/cjs/loader:1383:15)\n")
    m = _MODULE_MISSING.search(real_stderr)
    assert m and m.group(1) == "jsdom"
    # A genuine harness error must NOT match, or a real regression would skip instead of fail.
    assert not _MODULE_MISSING.search(
        "TypeError: Cannot read properties of null (reading 'classList')\n"
        "    at render (/repo/tests/dashboard/render_operator_console.js:120:9)\n")
    assert not _MODULE_MISSING.search("AssertionError: nav_item_count was 23\n")


def test_the_skip_message_tells_you_how_to_unskip():
    assert INSTALL_HINT.startswith("npm install")
    assert "tests/dashboard" in INSTALL_HINT
    doc = pathlib.Path(__file__).read_text(encoding="utf-8")
    assert doc.count(INSTALL_HINT) >= 2, "the remedy belongs in the docstring AND the skip"


# ══════════════════════════════════════════════════════════════════════════════════════
# FACE-GATE DRIFT — every served file must be CLASSIFIED, not merely unlisted
# ══════════════════════════════════════════════════════════════════════════════════════
# 🔴 THE DEFECT CLASS. `StaticFiles` serves ANY file dropped into saas/dashboard/ or
# saas/landing/. The per-face frozensets in src/mcp/http_server.py are the ONLY thing that
# hides an operator page from the customer face (and vice versa), and nothing forces a new
# file to be registered. Measured at 944fbaad by enumerating the filesystem against the
# gate — not by reading the gate:
#     /dashboard/billing.html + /dashboard/billing.js   (the OPERATOR billing dashboard)
#         reachable at https://app.faultline.ca/dashboard/billing.html — the CUSTOMER face.
#     13 landing files absent from _LANDING_EXACT → served on the ADMIN face.
# Both are drift, not a decision. Reading the allowlist can never find this; only comparing
# it to the filesystem can.
#
# 🔴 AND WHY THE TEST NEEDS `_FACE_SHARED_BY_DESIGN`. "Shared on purpose" and "nobody ever
# registered it" are byte-identical in the gate's behaviour. Without a third set to declare
# intent, this test could only be written as "everything must be in an allowlist" — which is
# WRONG (style.css / tour.js / status_banner.js must be in neither, or they 404 on the console
# that needs them) and would be silenced by adding them, breaking the consoles. So the rule is:
# every file is in exactly one of {operator, tenant, landing, shared-by-design}.

def _served_files():
    """Every file StaticFiles will hand out, as the URL path it is served at."""
    out = []
    for base, prefix in ((DASH, "/dashboard"), (ROOT / "saas" / "landing", "")):
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(base).as_posix()
            if "node_modules" in rel or "__pycache__" in rel or rel.endswith(".pyc"):
                continue
            out.append((prefix + "/" + rel) if prefix else ("/" + rel))
    return out


@pytest.fixture(scope="module")
def gate():
    os.environ.setdefault("SAAS_MODE", "1")
    try:
        from src.mcp import http_server as H
    except Exception as exc:                                   # pragma: no cover
        pytest.fail("could not import src.mcp.http_server to audit the face gate: %r" % (exc,))
    return H


def test_every_dashboard_and_landing_file_is_classified_by_the_face_gate(gate):
    """🔴 THE DRIFT GUARD. A file that no allowlist mentions and that is not declared
    shared-by-design is a file somebody added without deciding who may see it — which is
    exactly how the operator billing dashboard ended up on the customer face."""
    classified = (set(gate._OPERATOR_CONSOLE_EXACT) | set(gate._TENANT_CONSOLE_EXACT)
                  | set(gate._LANDING_EXACT) | set(gate._FACE_SHARED_BY_DESIGN)
                  | set(gate._GATE_ALWAYS_ALLOW_EXACT))
    prefixes = tuple(gate._LANDING_PREFIX) + tuple(gate._GATE_ALWAYS_ALLOW_PREFIX)
    unclassified = [
        p for p in _served_files()
        if p not in classified and not p.startswith(prefixes)
    ]
    assert unclassified == [], (
        "%d served file(s) are in NO allowlist and are not declared shared-by-design. Each is "
        "silently visible on every face. Put each one in _OPERATOR_CONSOLE_EXACT, "
        "_TENANT_CONSOLE_EXACT, _LANDING_EXACT, or — only if it genuinely belongs on every "
        "face — _FACE_SHARED_BY_DESIGN, in src/mcp/http_server.py.\n  %s"
        % (len(unclassified), "\n  ".join(unclassified)))


def test_the_operator_billing_dashboard_is_not_served_on_the_customer_face(gate):
    """The specific regression. billing.js was edited in this commit; billing.html predates it.
    Both are operator surfaces and both were reachable at app.faultline.ca."""
    for path in ("/dashboard/billing.html", "/dashboard/billing.js"):
        assert gate._gate_blocks("tenant", path), path + " is served on the TENANT face"
        assert not gate._gate_blocks("admin", path), path + " must still work for the operator"


def test_the_shared_dashboard_assets_stay_in_neither_face_list(gate):
    """The opposite mistake, pinned so a later 'tidy-up' cannot make it. Listing a genuinely
    shared script in either frozenset 404s it on the console that needs it."""
    for path in ("/dashboard/style.css", "/dashboard/status_banner.js",
                 "/dashboard/feature_copy.js", "/dashboard/tour.js", "/dashboard/alerts.js"):
        assert path not in gate._OPERATOR_CONSOLE_EXACT, path
        assert path not in gate._TENANT_CONSOLE_EXACT, path
        for face in ("tenant", "admin"):
            assert not gate._gate_blocks(face, path), (path, face)


# ══════════════════════════════════════════════════════════════════════════════════════
# CAPACITY REDACTION — no money-shaped key may survive for a non-billing admin
# ══════════════════════════════════════════════════════════════════════════════════════

_MONEY_KEY_RE = re.compile(
    r"price|cost|rate|amount|cents|fee|charge|currency|money|total|spend|billed|invoice",
    re.I)

#: 🔴 EXPLICIT EXEMPTIONS, NOT A LOOSER REGEX. ``total_licensed`` / ``total_used`` are SEAT
#: COUNTS and are exactly what the Plan tab must still render without billing access. The
#: temptation is to drop ``total`` from the pattern above; that is the wrong repair, because a
#: future money field literally called ``total`` or ``period_total`` would then walk straight
#: through. Naming the two known non-money keys keeps the pattern fail-CLOSED: any NEW
#: ``total*`` key is a RED until a human looks at it and decides.
_NON_MONEY_KEYS = frozenset({"total_licensed", "total_used"})

#: The REAL shape of entitlements.capacity_report (read off the function, 2026-08-13) plus
#: two decoys. The decoys are the point: they stand in for the NEXT money field somebody adds
#: upstream, and an allow-list must drop them without anybody editing this test.
_CAPACITY_SHAPE = {
    "account_id": "acct-1", "account_kind": "tenant",
    "types": [{"seat_type": "expert", "licensed": 3, "base": 0, "provisioned": 3, "used": 2,
               "available": 1, "unit_price": 45.0, "card_unit_price": 99.0}],
    "total_licensed": 8, "total_used": 5,
    "bundles": [{"bundle_id": "b1", "label": "Starter", "items": [], "price": 250.0}],
    "prices": {"standard": 10.0, "expert": 45.0},
    "base_package_price": 1500.0, "included_standard": 5,
    "seat_cap": None, "ladder": ["standard", "advanced", "expert", "agent"],
    "term_months": 6, "expires_at": None, "expired": False,
    # decoys — a future field added upstream, never named in any redaction list
    "monthly_spend_cents": 173500,
    "next_invoice_total": 1735.0,
}


@pytest.fixture(scope="module")
def tenant_console_mod():
    os.environ.setdefault("SAAS_MODE", "1")
    try:
        from saas import tenant_console
    except Exception as exc:                                   # pragma: no cover
        pytest.fail("could not import saas.tenant_console: %r" % (exc,))
    return tenant_console


def test_no_money_shaped_key_survives_capacity_redaction(tenant_console_mod):
    """🔴 THE REGRESSION THIS COMMIT INTRODUCED, AND THE GUARD THAT GENERALISES IT.

    ``entitlements.capacity_report`` gained ``card_unit_price`` — the LIVE seat rate card — at
    944fbaad (0 occurrences at 944fbaad~1). ``_redact_capacity_prices`` was a DENY-LIST naming
    exactly ``unit_price``, so ``GET /tenant/capacity`` started serving the rate card to a
    tenant admin the account owner had deliberately excluded from billing
    (``can_view_billing = false``). Nothing failed; the leak is invisible.

    This test does not name ``card_unit_price``. It asserts the PROPERTY — no money-shaped key
    survives — so the NEXT such field is caught the day it is added, whatever it is called."""
    red = tenant_console_mod._redact_capacity_prices(_CAPACITY_SHAPE)

    def walk(node, path="report"):
        found = []
        if isinstance(node, dict):
            for k, v in node.items():
                here = path + "." + str(k)
                if _MONEY_KEY_RE.search(str(k)) and str(k) not in _NON_MONEY_KEYS:
                    found.append(here)
                found += walk(v, here)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                found += walk(v, "%s[%d]" % (path, i))
        return found

    leaked = walk(red)
    assert leaked == [], (
        "money-shaped key(s) survived redaction for a caller without can_view_billing: %s. "
        "_redact_capacity_prices is an ALLOW-LIST — a money field reaches the customer only "
        "if somebody added it to _CAPACITY_NONBILLING_*." % leaked)
    assert red["billing_visible"] is False


def test_capacity_redaction_keeps_the_counts_the_plan_tab_renders(tenant_console_mod):
    """The allow-list must not over-redact: the Plan tab still has to render capacity."""
    red = tenant_console_mod._redact_capacity_prices(_CAPACITY_SHAPE)
    for key in ("account_id", "account_kind", "types", "bundles", "total_licensed",
                "total_used", "included_standard", "ladder", "term_months", "expired"):
        assert key in red, key
    t = red["types"][0]
    for key in ("seat_type", "licensed", "base", "provisioned", "used", "available"):
        assert key in t, key
    assert red["bundles"][0]["label"] == "Starter"
    assert red["bundles"][0]["items"] == []


def test_capacity_redaction_is_an_allow_list_not_a_deny_list(tenant_console_mod):
    """SHAPE OF THE FIX, pinned. A deny-list is what made card_unit_price leak, and a future
    'simplification' back to popping named keys would silently re-open it. An unknown field —
    money-shaped or not — must be DROPPED, which is only true of an allow-list."""
    out = tenant_console_mod._redact_capacity_prices(
        dict(_CAPACITY_SHAPE, some_future_field_nobody_thought_about="secret"))
    assert "some_future_field_nobody_thought_about" not in out
    src = inspect.getsource(tenant_console_mod._redact_capacity_prices)
    assert ".pop(" not in src, (
        "_redact_capacity_prices went back to popping named keys — that is the deny-list "
        "shape whose failure direction is DISCLOSURE")


# ══════════════════════════════════════════════════════════════════════════════════════
# THE PUBLIC TERMS PAGE — the site must state the term it actually sells
# ══════════════════════════════════════════════════════════════════════════════════════

LANDING = ROOT / "saas" / "landing"
TERMS_HTML = LANDING / "terms.html"


def test_the_public_terms_page_exists_and_is_linked_from_the_site():
    """The site advertised "cancel anytime" on five surfaces while the agreement published in
    this same commit states a multi-month term — and there was NO Terms link anywhere."""
    assert TERMS_HTML.is_file(), "saas/landing/terms.html is missing"
    linking = [p.name for p in sorted(LANDING.glob("*.html"))
               if 'href="terms.html' in p.read_text(encoding="utf-8")]
    for page in ("index.html", "numbers.html", "business.html", "tour.html"):
        assert page in linking, page + " has no Terms link"


def test_the_public_terms_page_states_the_real_term():
    """🔴 THE ONE DUPLICATED NUMBER, GUARDED. The term length lives in
    saas/platform_settings.py and is frozen onto each published contract template; terms.html
    restates it in prose because a static page cannot read the database. Duplication that
    cannot be removed is duplication a test has to guard — so every <span class="term-months">
    on the page must equal the platform default. If an operator changes the term, this goes RED
    and names the file to edit."""
    from saas import platform_settings as ps
    expected = str(ps._DEFAULT_CONTRACT_TERM_MONTHS)
    html = TERMS_HTML.read_text(encoding="utf-8")
    stated = re.findall(r'<span class="term-months">([^<]+)</span>', html)
    assert stated, "terms.html carries no <span class=\"term-months\"> marker"
    assert set(stated) == {expected}, (
        "terms.html states term month values %s but platform_settings says %s"
        % (sorted(set(stated)), expected))


def test_cancel_anytime_is_not_claimed_on_tenant_facing_pages():
    """Individuals genuinely ARE month-to-month (agreement §3, "Individual accounts are
    different"), so "cancel anytime" is TRUE for them. It is false for Team/Enterprise, and
    every one of the five occurrences was an ALL-TIER claim."""
    bad = []
    for p in sorted(LANDING.glob("*.html")):
        # Comments are stripped FIRST: terms.html's own header comment QUOTES the defective
        # string it exists to fix ("the marketing site said \"billed monthly, cancel anytime\"").
        # A file that documents a rule must not be indistinguishable from one that breaks it.
        for i, line in enumerate(
                _strip_comments(p.read_text(encoding="utf-8")).splitlines(), 1):
            if re.search(r"cancel\s+any\s?time", line, re.I):
                bad.append("%s:%d" % (p.name, i))
    assert bad == [], (
        "un-audienced 'cancel anytime' claim(s) at %s — the agreement's §3 term applies to "
        "team and company accounts" % bad)


def test_the_site_advertises_no_capability_that_does_not_exist():
    """🔴 SSO WAS SOLD ON FOUR PAGES AND DOES NOT EXIST. No SAML/OIDC route, config, flag or
    migration anywhere in src/ or saas/ (verified). Selling it in the present tense is the same
    false-clause class as a contract promising a feature we have not built, so every surviving
    mention must be explicitly marked as roadmap — and if SSO is ever built, this test is the
    thing that tells you to go and un-mark them."""
    code_hits = []
    for base in (ROOT / "src", ROOT / "saas"):
        for p in base.rglob("*.py"):
            if "node_modules" in p.as_posix():
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\b(saml|oidc|openid[_ ]?connect)\b", txt, re.I):
                code_hits.append(p.as_posix())
    if code_hits:
        pytest.fail(
            "SSO/SAML/OIDC now appears in code (%s) — if it SHIPPED, remove the 'roadmap' "
            "markers from the marketing pages and delete this test's expectation."
            % code_hits[:5])
    unmarked = []
    for p in sorted(LANDING.glob("*.html")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bSSO\b", line) and not re.search(r"roadmap", line, re.I):
                unmarked.append("%s:%d" % (p.name, i))
    assert unmarked == [], (
        "SSO claimed in the present tense at %s while no SAML/OIDC code exists" % unmarked)


def test_no_landing_link_points_at_a_document_we_do_not_serve():
    """/privacy 404'd on six pages and /.well-known/security.txt on nine, while security.html
    declared RFC 9116 compliance for the file it pointed at. Resolves every same-origin,
    non-anchor href on the marketing site against the files actually on disk."""
    missing = []
    for p in sorted(LANDING.glob("*.html")):
        html = p.read_text(encoding="utf-8")
        for href in re.findall(r'href="([^"#?]+)"', html):
            # tel: is an RFC 3966 URI scheme, NOT a same-origin path. It was missing from this
            # list, so a phone number on the supplier-disclosure page was reported as a dead
            # link to a document we do not serve. sms: is the same shape and is excluded for
            # the same reason rather than waiting to be tripped over.
            if href.startswith(("http://", "https://", "mailto:", "tel:", "sms:",
                                "data:", "//")):
                continue
            target = LANDING / href.lstrip("/")
            if not (target.is_file() or target.is_dir()):
                missing.append("%s → %s" % (p.name, href))
    assert missing == [], "dead same-origin link(s): %s" % sorted(set(missing))


def test_the_security_txt_satisfies_rfc_9116s_mandatory_fields():
    """RFC 9116 §2.5.3: Contact "MUST always be present in a 'security.txt' file".
    §2.5.5: Expires "MUST always be present and MUST NOT appear more than once", RFC 3339,
    "RECOMMENDED that the value ... be less than a year into the future".
    §3: the file MUST live under /.well-known/."""
    import datetime as dt
    f = LANDING / ".well-known" / "security.txt"
    assert f.is_file(), "security.html declares RFC 9116 compliance; the file must exist"
    lines = [ln for ln in f.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    fields = {}
    for ln in lines:
        k, _, v = ln.partition(":")
        fields.setdefault(k.strip().lower(), []).append(v.strip())
    assert "contact" in fields, "RFC 9116 §2.5.3 — Contact MUST be present"
    assert len(fields.get("expires", [])) == 1, (
        "RFC 9116 §2.5.5 — Expires MUST be present exactly once")
    exp = dt.datetime.fromisoformat(fields["expires"][0].replace("Z", "+00:00"))
    now = dt.datetime.now(dt.timezone.utc)
    assert exp > now, "the published security.txt has EXPIRED — renew it (RFC 9116 §2.5.5)"
    assert exp < now + dt.timedelta(days=366), (
        "RFC 9116 §2.5.5 RECOMMENDS Expires be less than a year out")


# ══════════════════════════════════════════════════════════════════════════════════════
# CSP — no inline script and no inline event handler, anywhere we serve
# ══════════════════════════════════════════════════════════════════════════════════════

def _strip_comments(html: str) -> str:
    """A file that DOCUMENTS the rule must not be indistinguishable from one that BREAKS it —
    an HTML comment explaining 'no inline <script>' otherwise trips a text search."""
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


class _CspScan(html.parser.HTMLParser):
    """🔴 A REAL PARSER, NOT A REGEX — and this is not fastidiousness, it is a measured lesson.

    A regex scan of these files false-positives on its OWN DOCUMENTATION, twice over:
      * ``saas/dashboard/help/index.html`` line 19 is a CSS comment INSIDE a ``<style>`` block
        that literally reads "inline ``<script>``/on*= are not [allowed]" — a regex for
        ``<script...>`` matches the prose and reports a violation in the file that explains
        the rule. (Python's HTMLParser puts ``<style>``/``<script>`` content into CDATA mode,
        so a parser never sees it as markup.)
      * ``<script type="application/ld+json">`` blocks — ~30 across this site — are DATA
        BLOCKS, never prepared as script, so ``script-src`` does not check them. Verified in
        Chromium under the verbatim ``_CSP_HTML``: zero violations from any of them.

    What genuinely violates ``script-src 'self'`` and is collected here: an inline
    ``<script>`` with executable content, any ``on*=`` attribute, and any
    ``href="javascript:"``.
    """

    _EXEC_TYPES = ("", "text/javascript", "application/javascript", "module")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inline_scripts = []
        self.handlers = []
        self.js_urls = []
        self._script_depth = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        for k, v in a.items():
            if k.startswith("on"):
                self.handlers.append("<%s %s=…> line %d" % (tag, k, self.getpos()[0]))
            if k in ("href", "src", "action") and v.strip().lower().startswith("javascript:"):
                self.js_urls.append("<%s %s=javascript:…> line %d" % (tag, k, self.getpos()[0]))
        if tag == "script":
            typ = a.get("type", "").strip().lower()
            if "src" not in a and typ in self._EXEC_TYPES:
                self._script_depth = self.getpos()[0]

    def handle_data(self, data):
        if self._script_depth and data.strip():
            self.inline_scripts.append("inline <script> at line %d" % self._script_depth)
            self._script_depth = 0

    def handle_endtag(self, tag):
        if tag == "script":
            self._script_depth = 0


@pytest.mark.parametrize("page", sorted(
    [p.as_posix() for p in (ROOT / "saas" / "landing").rglob("*.html")]
    + [p.as_posix() for p in (ROOT / "saas" / "dashboard").rglob("*.html")
       if "node_modules" not in p.as_posix()]))
def test_no_inline_event_handler_or_inline_script_survives_the_csp(page):
    """`script-src 'self'` with NO 'unsafe-inline'. An inline handler renders fine and is
    simply NOT EXECUTED — wiki.html and wiki-longmemeval.html carried
    `<form onsubmit="return false">`, so Enter submitted the form and threw the search away.
    Silent in every way except a real browser console.

    `<script type="application/ld+json">` is EXEMPT and deliberately so: it is a DATA BLOCK,
    never prepared as script, so script-src does not check it (verified in Chromium — 0
    violations across ~30 such blocks on this site)."""
    scan = _CspScan()
    scan.feed(pathlib.Path(page).read_text(encoding="utf-8"))
    name = pathlib.Path(page).relative_to(ROOT).as_posix()
    assert scan.handlers == [], "%s carries inline event handler(s): %s" % (name, scan.handlers)
    assert scan.js_urls == [], "%s carries javascript: URL(s): %s" % (name, scan.js_urls)
    assert scan.inline_scripts == [], (
        "%s carries inline <script> block(s): %s" % (name, scan.inline_scripts))


def test_the_csp_scanner_can_still_see_a_real_violation():
    """TEETH ON THE SCANNER. A CSP audit that quietly stops detecting is worse than none, and
    this one was deliberately made lenient in three specific ways (comments, CSS text, ld+json).
    Each exemption is proven not to have swallowed the real thing."""
    real = _CspScan()
    real.feed('<form onsubmit="return false"><a href="javascript:void(0)">x</a>'
              '<script>alert(1)</script>')
    assert len(real.handlers) == 1 and len(real.js_urls) == 1 and len(real.inline_scripts) == 1

    exempt = _CspScan()
    exempt.feed('<!-- an onsubmit="x" in a comment, and <script>evil()</script> -->'
                '<style>/* inline <script>/on*= are not allowed */</style>'
                '<script type="application/ld+json">{"@type":"Offer"}</script>'
                '<script src="/dashboard/app.js"></script>')
    assert exempt.handlers == [] and exempt.inline_scripts == [] and exempt.js_urls == []


# ══════════════════════════════════════════════════════════════════════════════════════
# SATELLITE PAGES THAT LOAD app.js — boot() must not assume the operator-console shell
# ══════════════════════════════════════════════════════════════════════════════════════

BILLING_HARNESS = ROOT / "tests" / "dashboard" / "render_billing_page.js"


@pytest.fixture(scope="module")
def billing_render():
    """Run billing.html through jsdom with the REAL app.js + billing.js.

    Same three-way skip contract as the operator-console fixture: no toolchain → SKIP,
    missing dependency → SKIP naming the fix, anything else → FAIL loudly."""
    if shutil.which("node") is None or not BILLING_HARNESS.is_file():
        pytest.skip("node or tests/dashboard/render_billing_page.js not available")
    out = subprocess.run(["node", str(BILLING_HARNESS), "billing.html"],
                         capture_output=True, text=True, cwd=str(ROOT))
    miss = _MODULE_MISSING.search(out.stderr or "")
    if miss:
        pytest.skip("%s missing — run: %s" % (miss.group(1), INSTALL_HINT))
    if out.returncode != 0:
        pytest.fail("billing render harness exited %d; stderr tail:\n%s"
                    % (out.returncode, (out.stderr or "")[-2000:]))
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:                        # pragma: no cover
        pytest.fail("billing harness emitted non-JSON (%s); stderr:\n%s"
                    % (exc, (out.stderr or "")[-2000:]))


def test_boot_survives_a_page_without_the_login_shell(billing_render):
    """🔴 THE BUG, EXECUTED. /dashboard/billing.html loads app.js but has no login shell, and
    boot() opened with an unguarded `$('in-endpoint').value = …`. It threw a TypeError on its
    FIRST statement, so theme, accent picker, login, MFA, connect-with-key, the router, the
    operator menu and the tour never bound — on a page that looked completely normal.

    A source-level "there is an if-guard" assertion could not prove this: `if (false && !x)`
    keeps the text and restores the bug. TEETH-PROVEN with exactly that mutation, which
    reproduces the original error verbatim:
        TypeError: Cannot set properties of null (setting 'value') at boot"""
    r = billing_render
    assert r["has_login_shell"] is False, (
        "billing.html grew a #in-endpoint — this test no longer exercises the guard")
    assert r["boot_is_defined"] and r["boot_ran"], r
    assert r["boot_threw"] is None, (
        "boot() threw on a page with no login shell:\n%s" % r["boot_threw"])
    assert r["jsdom_errors"] == [], r["jsdom_errors"]
    assert r["panel_host_present"], "billing.js had no host element to render into"


# ══════════════════════════════════════════════════════════════════════════════════════
# DEMO DATA — never a real identity, never a raw transport string in the verbiage slot
# ══════════════════════════════════════════════════════════════════════════════════════

#: The owner's REAL development seat id. It is all over CLAUDE.md as FAULTLINE_USER_ID and
#: names a live per-tenant schema; it was seeded as the visible demo account id on the public
#: individual console, so every prospect who clicked "load demo data" saw it.
_OWNER_DEV_UUID = "10d7d879-63cd-4f31-92ce-f2c9edb760ab"


def _strip_js_comments(src: str) -> str:
    """Third time this repo has paid for it: a file that DOCUMENTS a rule must not be
    indistinguishable, to a scanner, from one that BREAKS it. Both my own explanatory comments
    and renderUnreadable's own docstring quote the exact strings these tests forbid."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def test_no_real_seat_uuid_is_seeded_into_the_public_demo_console():
    """user.js drives the PUBLIC individual console — the one a stranger can open. A demo
    identity must be unmistakably fake on sight, so nobody pastes it into a support ticket,
    probes it, or mistakes it for a real seat.

    ⚠️ SCOPE, STATED HONESTLY: tenant.js and app.js are OPERATOR/TENANT-authenticated demo
    fixtures and still carry the id in several seat rosters and log lines. Those are xfail'd
    rather than silently excluded — they are the same class of leak with a much smaller
    audience, and they are a follow-up, not something to fix quietly inside a UX pass."""
    src = _strip_js_comments((DASH / "user.js").read_text(encoding="utf-8"))
    assert _OWNER_DEV_UUID not in src, (
        "user.js seeds the owner's REAL development seat id as demo data on the PUBLIC console")


def test_the_authenticated_demo_fixtures_residual_is_recorded_not_forgotten():
    """⚠️ REPORTED, NOT FIXED — and pinned so it cannot quietly grow.

    tenant.js and app.js still seed the same real UUID across seat rosters, log lines and
    meter maps. Their audience is an ALREADY-SIGNED-IN operator or tenant admin, not a
    stranger, so the exposure is materially different from the public console — but it is the
    same class and it is an owner call whether to churn two large concurrently-edited demo
    fixtures for it. This test records the CURRENT counts: it goes RED if the leak spreads,
    and RED (telling you to update it) if somebody cleans it up."""
    counts = {js: _strip_js_comments((DASH / js).read_text(encoding="utf-8")).count(_OWNER_DEV_UUID)
              for js in ("tenant.js", "app.js")}
    assert counts == {"tenant.js": 5, "app.js": 4}, (
        "the authenticated demo fixtures' use of the owner's real dev UUID CHANGED (%s). If it "
        "grew, stop; if it was cleaned up, delete this test and widen the public-console one "
        "above to cover these files." % counts)


def test_the_unreadable_banner_keeps_its_honest_sentence(gate):
    """🔴 A TRUTHY NOTE USED TO REPLACE THE VERBIAGE. `verbiage: note || '<honest sentence>'`
    meant any truthy note DISPLACED it — and the note is a raw transport string, so a customer
    whose session had merely expired read `401 — not signed in / session expired` in the slot
    reserved for "what this means for you".

    The note is KEPT (losing it is the opposite mistake) but subordinated to a secondary chip
    and degraded_reason. Asserted on the source because renderUnreadable is a pure builder."""
    js = _strip_js_comments((DASH / "status_banner.js").read_text(encoding="utf-8"))
    body = js[js.index("function renderUnreadable"):]
    body = body[:body.index("\n  }")]
    assert "note ||" not in body, (
        "renderUnreadable is back to letting the technical note replace the verbiage")
    assert "verbiage: honest" in body, "the verbiage must be the constant honest sentence"
    assert "secondary:" in body and "read_error" in body, (
        "the technical note must survive somewhere subordinate, not be dropped")


# ── the per-seat rate grid must not inherit the wide-table floor ──────────────────────────────

def _style_css() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "saas" / "dashboard" / "style.css").read_text(
        encoding="utf-8")


def _index_html() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "saas" / "dashboard" / "index.html").read_text(
        encoding="utf-8")


def test_the_seat_rate_grid_does_not_carry_the_wide_table_class():
    """The per-seat rates matrix is THREE columns (Seat type · Enterprise · Team).

    It carried ``.sub-table``, whose ``min-width: 880px`` was written for the 8-column rosters.
    Two consequences at once, both reported by the owner: the table ran off the right of the
    panel, and three columns were spread across 880px, which reads as wasted space because it
    is. A floor sized for a different table is how a 3-column grid ends up with a scrollbar."""
    import re
    html = _index_html()
    m = re.search(r'<table class="([^"]*)" id="seatrates-table"', html)
    assert m, "seatrates-table markup not found (or its attribute order changed)"
    classes = set(m.group(1).split())
    assert "sub-table" not in classes, (
        "seatrates-table carries .sub-table again — that imposes min-width:880px on a 3-column "
        "grid and pushes it off the panel")
    assert "rate-grid" in classes, "seatrates-table lost .rate-grid"
    # .no-scroll is still required, or the <=900px layer converts it to a block and drops the
    # column alignment inside a scroll box.
    assert "no-scroll" in classes, "seatrates-table must keep .no-scroll"


def test_the_rate_grid_rule_actually_releases_the_floor():
    """A class that does not override BOTH min-width rules changes nothing.

    ``.table-scroll > table { min-width: 100% }`` would still stretch the three columns across
    the panel even with the 880px floor gone, so the rule has to set width:auto AND min-width:0.
    """
    css = _style_css()
    import re
    m = re.search(r"\.table-scroll > table\.rate-grid\s*\{([^}]*)\}", css)
    assert m, ".rate-grid rule is missing from style.css"
    body = m.group(1)
    assert "min-width: 0" in body, ".rate-grid must clear the inherited min-width"
    assert "width: auto" in body, (
        ".rate-grid must set width:auto or `.table-scroll > table { min-width:100% }` still "
        "stretches it to fill the panel")


def test_the_genuinely_wide_tables_keep_their_floor():
    """THE CONTROL. Fixing the narrow grid must not weaken the rule for the tables it was
    written for — the 8-column rosters and the base-price catalog still need to scroll in their
    own box rather than push the PAGE sideways."""
    css = _style_css()
    assert ".table-scroll > table.sub-table { min-width: 880px; }" in css, (
        "the 880px floor for genuinely wide tables was removed or edited — the rosters need it")
    html = _index_html()
    assert 'class="no-scroll sub-table" id="pricing-base-table"' in html, (
        "the 8-column base-price catalog lost .sub-table")
    assert html.count('<table class="no-scroll sub-table">') >= 2, (
        "the wide roster tables lost .sub-table")
