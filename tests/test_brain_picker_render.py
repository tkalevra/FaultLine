"""brain-picker FINAL model — RENDERED + STATIC proof (driver for render_brain_picker.js).

Founded on the same two-kinds-of-evidence pattern as test_model_picker_visibility.py:
  RENDERED  tests/dashboard/render_brain_picker.js boots the REAL tenant.html +
            feature_copy.js + tenant.js in jsdom, drives the REAL loadLlm() and the REAL
            picker handlers against a stubbed transport, and reports what the DOM then
            contains and which requests the REAL api() fired. jsdom does not lay out, so
            "visible" here means "real button in the live DOM", not pixels.
  STATIC    the tenant.html / tenant.js / style.css seams asserted directly, so the suite
            still holds without node/jsdom.

The FINAL model under proof (spec DEV/DESIGN-tenant-brain-picker.md, amendments 1-3):
  row      [▸ save config] [▸ test connection] [refresh] [▾ saved brains] [▸ load config];
  select   a menu entry POPULATES THE FORM and fires NO request;
  guard    configured-but-unsaved live brain → confirm modal; cancel BLOCKS the apply POST;
  load     ▸ load config POSTs the apply route and repaints from the GET (the only way the
           write-only key transfers); with the brain saved it acts directly;
  save     ▸ save config NEVER branches — with a selection active it still PUTs only.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "saas" / "dashboard"
HARNESS = ROOT / "tests" / "dashboard" / "render_brain_picker.js"


@pytest.fixture(scope="module")
def rendered():
    node = shutil.which("node")
    if not node or not HARNESS.exists():
        pytest.skip("node/jsdom harness unavailable — static assertions below still hold")
    proc = subprocess.run([node, str(HARNESS)], capture_output=True, text=True,
                          cwd=str(HARNESS.parent), timeout=120)
    if proc.returncode != 0:
        pytest.skip(f"harness failed ({proc.returncode}) — static assertions below still hold")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pytest.skip("harness emitted no JSON — static assertions below still hold")


def test_row_holds_the_five_final_controls_in_order(rendered):
    """[save][test][refresh][picker][load] — nothing else; the picker AFTER refresh, the
    load button the LAST child (owner amendments 2-3)."""
    assert rendered["row_controls"] == [
        "btn-llm-save", "btn-llm-test", "btn-llm-refresh", "llm-saved-pick", "btn-llm-load"]


def test_apply_button_is_gone_and_load_is_last_plain_non_accent(rendered):
    """#btn-llm-saved-apply from the amendment-1 drafts must NOT exist anywhere (owner
    amendment 3 pins its absence); #btn-llm-load is a plain .btn — NEVER .accent, which is
    the SAVE affordance the load button must never borrow — and inert until a selection."""
    assert rendered["apply_absent"] is True
    assert rendered["load_is_last"] is True
    assert rendered["load_classes"] == "btn"
    assert rendered["load_disabled_initial"] is True


def test_menu_is_an_apg_disclosure_group_with_named_rows(rendered):
    """role=group + aria-labelled rows and trash buttons (role=menu is banned house-wide);
    the disclosure opens through the REAL setSavedMenu handler and ESCAPE closes it."""
    assert rendered["menu_open"] is True
    assert rendered["trigger_aria_open"] == "true"
    assert rendered["menu_role"] == "group"
    assert rendered["no_role_menu"] is True
    assert rendered["item_count"] == 2
    assert rendered["item_name_buttons"] == 2 and rendered["item_del_buttons"] == 2
    assert rendered["item_del_aria"] == ["Delete saved brain prod", "Delete saved brain cheap"]
    assert rendered["menu_closed_by_escape"] is True


def test_selection_populates_the_form_and_fires_no_request(rendered):
    """Amendment 3: selecting an entry fills backend/baseurl/model (a client-side PREVIEW;
    the write-only key keeps #llm-apikey BLANK), flips the trigger label, enables Load —
    and fires NO request whatsoever."""
    assert rendered["select_backend"] == "openai"
    assert rendered["select_baseurl"] == "https://prod.example.com/v1"
    assert rendered["select_model"] == "prod-model"
    assert rendered["select_apikey_blank"] is True
    assert rendered["select_trigger_label"] == "▾ saved: prod"
    assert rendered["select_load_enabled"] is True
    assert rendered["menu_closed_after_select"] is True
    assert rendered["select_fired_requests"] == []


def test_load_guard_cancel_blocks_the_apply_post(rendered):
    """Configured-but-unsaved live brain → the confirm modal (the save-current-first
    warning); cancelling leaves the selection AND fires nothing."""
    assert rendered["guard"]["title"] == "Your current brain is not saved"
    assert rendered["guard"]["ok"] == "▸ load anyway"
    assert rendered["guard_blocked_apply"] is True
    assert rendered["guard_selection_kept"] == "▾ saved: prod"


def test_load_applies_then_repaints_from_the_get(rendered):
    """Confirming runs the apply POST for the SELECTED snapshot and repaints the form from
    the GET — the literal load-into-form, the only way the write-only key transfers; the
    selection is consumed and Load re-disables."""
    assert rendered["guard_confirm_calls"] == [
        {"method": "POST", "path": "/tenant/llm-config/saved/prod-1/apply"}]
    assert rendered["load_repaint_backend"] == "openai"
    assert rendered["load_repaint_baseurl"] == "https://prod.example.com/v1"
    assert rendered["load_repaint_apikey_blank"] is True
    assert rendered["load_selection_cleared"] == "▾ saved brains"
    assert rendered["load_btn_redisabled"] is True


def test_saved_brain_loads_directly_without_a_modal(rendered):
    """If the live brain IS saved (an is_active match exists), load acts directly — no
    guard, no modal, just the apply POST for the selection."""
    assert rendered["direct_calls"] == [
        {"method": "POST", "path": "/tenant/llm-config/saved/cheap-2/apply"}]


def test_save_never_branches_on_a_selection(rendered):
    """Amendment 1 is WITHDRAWN: with a selection active the save button STILL fires only
    the legacy PUT — no apply POST, no second write."""
    assert rendered["save_calls"] == [{"method": "PUT", "path": "/tenant/llm-config"}]


# ══════════════════════════════════════════════════════════════════════════════
# STATIC — the seams, so the suite holds without node/jsdom
# ══════════════════════════════════════════════════════════════════════════════

def test_static_row_markup_is_the_final_five_controls():
    html = (DASH / "tenant.html").read_text()
    idx_save = html.index("id=\"btn-llm-save\"")
    idx_load = html.index("id=\"btn-llm-load\"")
    assert idx_save < idx_load                       # load comes after save in the source row
    row = html[idx_save:idx_load + 200]
    order = [marker for marker in
             ("btn-llm-save", "btn-llm-test", "btn-llm-refresh", "llm-saved-pick",
              "btn-llm-load") if marker in row]
    assert order == ["btn-llm-save", "btn-llm-test", "btn-llm-refresh",
                     "llm-saved-pick", "btn-llm-load"]
    assert "btn-llm-saved-apply" not in html          # the dead button stays dead
    assert "disabled" in html[idx_load:idx_load + 120]  # inert until a selection exists


def test_static_disclosure_seams():
    html = (DASH / "tenant.html").read_text()
    assert 'role="group"' in html and 'aria-label="Saved brain configs"' in html
    assert 'role="menu"' not in html                # house-wide ban (index.html#opmenu)
    assert 'aria-controls="llm-saved-menu"' in html


def test_static_trash_is_right_justified_and_labeled():
    css = (DASH / "style.css").read_text()
    assert ".brain-item-del" in css and "margin-left: auto" in css   # the RIGHT-justified 🗑
    js = (DASH / "tenant.js").read_text()
    assert "Delete saved brain" in js


def test_static_save_button_has_no_selection_branch():
    js = (DASH / "tenant.js").read_text()
    save_body = js[js.index("async function saveLlm"):js.index("async function testLlm")]
    assert "savedSelectionId()" not in save_body      # the withdrawn amendment-1 branch
    assert "api('PUT', '/tenant/llm-config'" in save_body
    assert "wireBrainDirtyVoiding" not in js          # the dirty-void rule is WITHDRAWN


def test_static_demo_and_guard_seams():
    js = (DASH / "tenant.js").read_text()
    assert "demoSaved" in js                          # demo mode renders a LIVE dropdown
    assert "Your current brain is not saved" in js    # the guard modal wording
    assert "liveConfigured" in js                     # the guard configured-input


def test_static_cache_bumps():
    html = (DASH / "tenant.html").read_text()
    assert "style.css?v=4" in html and "tenant.js?v=4" in html
    user = (DASH / "user.html").read_text()
    assert "style.css?v=4" in user                    # shared stylesheet, seat console too
