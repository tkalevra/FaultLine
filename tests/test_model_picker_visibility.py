"""BUG-03 (P3): "fetch models" has no visible dropdown — the picker must RENDER, not lurk.

THE WOUND (owner-observed on prod 2026-10-30): tenant console → LLM BRAIN → fetch models
→ INTENT is a visible dropdown of the endpoint's models; NOTHING appears. Root cause: the
fetched ids were appended ONLY into the hidden <datalist id="llm-model-options"> attached
to the free-text input — a surface that renders nothing until the input is focused, and even
then only as browser autocomplete. The sole visible feedback was a hint line ("N model(s)
available — pick one.") pointing at an affordance that did not exist on screen. The seat
console (user.html/user.js) had the same pattern.

THE FIX (saas/dashboard/tenant.js, tenant.html, user.html, user.js, style.css): the SAME
rebuild pass that fills the datalist now ALSO renders ALWAYS-VISIBLE clickable chips of the
model ids in #llm-model-chips under the field. A chip click fills the free-text input (which
remains the single source of truth) and focuses it; manual/pasted ids work exactly as before.
The chips describe the LAST COMPLETED fetch: BOTH consoles clear them the moment a new fetch
starts (and on a backend switch), so a stale id from a different endpoint can never linger
as a one-click wrong choice under a base URL that changed since it was fetched.

EVIDENCE, two kinds (see test_operator_console_ux.py for the precedent):
  RENDERED  tests/dashboard/render_model_picker.js runs the REAL tenant.html/user.html + the
            real feature_copy.js + tenant.js/user.js in jsdom, drives the REAL
            fetchModels()/fetchBrainModels() through a stubbed transport, and reports what
            the DOM then contains. jsdom does not lay out, so VISIBILITY here means "real
            <button> elements inside a non-hidden container that CSS styles as a chip row"
            — not pixels.
  STATIC    the HTML/markup/CSS/JS seams asserted directly, so the suite still holds
            without node/jsdom.
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "saas" / "dashboard"
HARNESS = ROOT / "tests" / "dashboard" / "render_model_picker.js"
MODELS = ["gpt-4o-mini", "gpt-4.1", "o3-mini"]


@pytest.fixture(scope="module")
def rendered():
    node = shutil.which("node")
    if not node or not HARNESS.exists():
        pytest.skip("node/jsdom harness unavailable — static assertions below still hold")
    env = dict(os.environ)
    env["NODE_PATH"] = str(HARNESS.parent / "node_modules")
    try:
        out = subprocess.run([node, str(HARNESS), json.dumps(MODELS)],
                             capture_output=True, text=True, timeout=120,
                             cwd=str(ROOT), env=env)
    except Exception as exc:                      # pragma: no cover
        pytest.skip(f"jsdom render failed to launch: {exc}")
    if out.returncode != 0:                       # pragma: no cover
        pytest.fail(f"jsdom render failed: {out.stderr[-2000:]}")
    return json.loads(out.stdout)


# ════════════════════════════════════════════════════════════════════════════════════
# RENDERED — what the page actually contains after a successful fetch
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("side", ["tenant", "seat"])
def test_fetch_renders_one_visible_chip_per_model(rendered, side):
    """The affordance the fetch button promises appears WITH the list: N real <button>s in a
    non-hidden container. This is the exact assertion BUG-03's brief asks for."""
    c = rendered[side]
    assert c["chips_container_present"], "#llm-model-chips container is missing"
    assert not c["chips_container_has_hidden_class"], "the chips container is hidden"
    assert c["chip_count"] == len(MODELS)
    assert c["chip_texts"] == MODELS
    assert c["chip_types_button"], "chips must be real buttons (clickable, focusable)"


@pytest.mark.parametrize("side", ["tenant", "seat"])
def test_clicking_a_chip_fills_the_free_text_input(rendered, side):
    """A chip click fills #llm-model — the input stays the single source of truth — and
    focuses it so the browser's own datalist filtering takes over. The 2nd chip is clicked
    so 'it works because it is first' cannot pass."""
    c = rendered[side]
    assert c["input_value_after_chip_click"] == MODELS[1]
    assert c["input_focused_after_chip_click"]


def test_manual_free_text_entry_still_works(rendered):
    """BUG-03's brief: keep free-text entry. A hand-typed id (untrimmed on purpose) is what
    chosenModel() returns — the chip surface changed nothing about the value path."""
    assert rendered["tenant"]["manual_entry_value"] == "hand-typed-model"


def test_the_datalist_lane_survives(rendered):
    """The datalist (type-to-filter autocomplete) is still populated — the chips are ADDED
    surface, not a replacement."""
    assert rendered["tenant"]["datalist_option_count"] == len(MODELS)


def test_the_hint_names_the_visible_surface(rendered):
    """The note no longer points at an affordance that is not on screen."""
    assert "click one below" in rendered["tenant"]["note_text"]


@pytest.mark.parametrize("side", ["tenant", "seat"])
def test_empty_refetch_clears_stale_chips(rendered, side):
    """An endpoint that answers 'no models' must not leave the PREVIOUS endpoint's ids as
    one-click choices — on EITHER console."""
    assert rendered[side]["chip_count_after_empty_refetch"] == 0


def test_seat_console_renderer_paints_chips_from_a_config_payload(rendered):
    """renderLlmEndpoint() (the page-load path) paints chips from S.llm.models WHEN the
    payload carries them — a renderer-contract pin, NOT a persistence claim. Today the
    backend hardcodes the GET payload's list empty (saas/user_console.py, cfg["models"] = []),
    so in production the seat console's chips are per-session: they appear after a live
    fetch and do NOT survive a reload. If the backend ever starts persisting the list, this
    is the path that renders it; if it renders nothing, that path is broken."""
    c = rendered["seat"]
    assert c["chip_texts_after_render_llm_endpoint"] == ["stored-1", "stored-2"]
    assert c["input_value_after_render"] == "m-a"


# ════════════════════════════════════════════════════════════════════════════════════
# STATIC — hold even without node/jsdom
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("page", ["tenant.html", "user.html"])
def test_both_consoles_carry_the_chip_container(page):
    html = (DASH / page).read_text(encoding="utf-8")
    assert 'id="llm-model-chips"' in html, f"{page} lost the #llm-model-chips container"
    # The container sits INSIDE the model field, after the datalist and note.
    i_dl, i_note, i_chips = (html.index(s) for s in
                             ('id="llm-model-options"', 'id="llm-model-note"', 'id="llm-model-chips"'))
    assert i_dl < i_note < i_chips


def test_css_styles_the_chip_row_and_collapses_when_empty():
    css = (DASH / "style.css").read_text(encoding="utf-8")
    assert ".model-chips" in css and ".model-chip" in css
    # An empty chip row must not take vertical space (a field with no fetched list renders
    # pixel-identical to before).
    assert ".model-chips:empty" in css


@pytest.mark.parametrize("js", ["tenant.js", "user.js"])
def test_both_rebuilders_render_chips(js):
    src = (DASH / js).read_text(encoding="utf-8")
    fn = "rebuildModelSelect" if js == "tenant.js" else "rebuildBrainModelSelect"
    assert f"function {fn}(" in src
    assert "llm-model-chips" in src, f"{js}::{fn} no longer renders the chip surface"
    assert "createElement('button')" in src, "chips must be built as buttons, not divs/spans"


@pytest.mark.parametrize("js,fn", [("tenant.js", "rebuildModelSelect([], chosenModel())"),
                                   ("user.js", "rebuildBrainModelSelect([], $('llm-model').value)")])
def test_both_fetch_paths_clear_chips_at_fetch_start(js, fn):
    """The clear must ride the FETCH START, not only the success/empty arms: a transport
    failure or 4xx/5xx would otherwise leave the previous endpoint's chips on screen."""
    src = (DASH / js).read_text(encoding="utf-8")
    i_fetch = src.index("fetching models" if js == "tenant.js" else "fetching models…")
    window = src[i_fetch:i_fetch + 700]
    assert fn in window, f"{js} must clear the chip surface when a fetch starts"
