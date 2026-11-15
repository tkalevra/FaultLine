"""FETCH MODELS cost legend: the consoles must COLOR the fetched models and show the legend.

OWNER REQUIREMENT (the UX this pins): when a user inputs their endpoint and clicks FETCH
MODELS, the returned models are color-coded by reasoning-cost class — GREEN (reasoning off
or switchable), ORANGE (low/reducible reasoning only), RED (always reasons), GREY
(unverified, calls work) — and a RIGHT-JUSTIFIED tooltip legend spells out what each color
means. Motivation: a new user has no idea what a reasoning model costs in their plan; the
legend must let them hook into a brain that improves performance and reduces cost without
reading a provider doc first.

The classes themselves are computed SERVER-SIDE
(saas/tenant_console.py / saas/user_console.py models endpoints ->
src/api/llm_client.classify_model_class — see tests/test_model_class.py for the mapping);
this file pins the WIRING and the RENDERING.

EVIDENCE, two kinds (same precedent as test_model_picker_visibility.py):
  RENDERED  tests/dashboard/render_model_picker.js runs the REAL tenant.html/user.html +
            the real feature_copy.js + tenant.js/user.js in jsdom, drives the REAL
            fetchModels()/fetchBrainModels() against a stubbed transport that answers with
            {models, classes}, and reports the chip classes + legend the DOM then contains.
            jsdom does not lay out, so "right-justified" is pinned STATICALLY on the CSS
            (margin-left:auto inside the flex chip row), not on pixels.
  STATIC    the endpoint/JS/CSS seams asserted directly, so the suite still holds without
            node/jsdom.
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
MODELS = ["glm-4.5-air", "glm-5.3", "gemini-3-pro", "self-hosted-llm"]
CLASSES = {
    "glm-4.5-air": "green",
    "glm-5.3": "red",
    "gemini-3-pro": "orange",
    "self-hosted-llm": "grey",
}

LEGEND_TOOLTIP_BITS = ("GREEN", "no reasoning cost", "ORANGE", "reasoning reducible",
                       "RED", "always reasons", "GREY", "unverified")


@pytest.fixture(scope="module")
def rendered():
    node = shutil.which("node")
    if not node or not HARNESS.exists():
        pytest.skip("node/jsdom harness unavailable — static assertions below still hold")
    env = dict(os.environ)
    env["NODE_PATH"] = str(HARNESS.parent / "node_modules")
    try:
        out = subprocess.run([node, str(HARNESS), json.dumps(MODELS), json.dumps(CLASSES)],
                             capture_output=True, text=True, timeout=120,
                             cwd=str(ROOT), env=env)
    except Exception as exc:                      # pragma: no cover
        pytest.skip(f"jsdom render failed to launch: {exc}")
    if out.returncode != 0:                       # pragma: no cover
        pytest.fail(f"jsdom render failed: {out.stderr[-2000:]}")
    return json.loads(out.stdout)


# ════════════════════════════════════════════════════════════════════════════════════
# RENDERED — the chips carry their class and the legend renders on BOTH consoles
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("side", ["tenant", "seat"])
def test_each_chip_carries_its_cost_class(rendered, side):
    c = rendered[side]
    assert c["chip_texts"] == MODELS
    assert c["chip_classes"] == [f"model-chip mc-{CLASSES[m]}" for m in MODELS]


@pytest.mark.parametrize("side", ["tenant", "seat"])
def test_chip_tooltip_names_its_color_and_meaning(rendered, side):
    """Hovering a RED chip must say 'always reasons', not just 'use this model'."""
    c = rendered[side]
    assert "RED" in c["chip_titles"][1] and "always reasons" in c["chip_titles"][1]
    assert "GREEN" in c["chip_titles"][0]


@pytest.mark.parametrize("side", ["tenant", "seat"])
def test_legend_renders_with_the_full_color_key(rendered, side):
    c = rendered[side]
    assert c["legend_present"], "the right-justified cost legend is missing"
    assert all(bit in c["legend_title"] for bit in LEGEND_TOOLTIP_BITS), c["legend_title"]


# ════════════════════════════════════════════════════════════════════════════════════
# STATIC — hold even without node/jsdom
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("js,fn", [("tenant.js", "rebuildModelSelect"),
                                   ("user.js", "rebuildBrainModelSelect")])
def test_rebuilders_accept_and_apply_the_classes_map(js, fn):
    src = (DASH / js).read_text(encoding="utf-8")
    assert f"function {fn}(models, current, classes)" in src, \
        f"{js}::{fn} lost the classes parameter"
    assert "mc-' + cls" in src, f"{js} must paint the mc-<class> chip class"
    assert "MODEL_CLASS_COPY[cls]" in src, f"{js} chip tooltip must name the color's meaning"
    # The legend is only rendered when the fetch actually returned classes AND ids —
    # an empty refetch must leave the container :empty (pixel-identical to before).
    assert "ids.length && classes && Object.keys(classes).length" in src


@pytest.mark.parametrize("js", ["tenant.js", "user.js"])
def test_fetch_paths_forward_the_server_classes(js):
    src = (DASH / js).read_text(encoding="utf-8")
    assert "r.classes" in src, f"{js} must pass the endpoint's classes map through"


def test_backend_endpoints_return_the_classes_map():
    from textwrap import dedent
    tenant_src = (ROOT / "saas" / "tenant_console.py").read_text(encoding="utf-8")
    assert "classify_model_class" in tenant_src
    assert 'return {"models": models, "classes": classes}' in tenant_src
    user_src = (ROOT / "saas" / "user_console.py").read_text(encoding="utf-8")
    assert "classify_model_class" in user_src
    assert 'result["classes"]' in user_src


def test_css_colors_all_four_classes_and_right_justifies_the_legend():
    css = (DASH / "style.css").read_text(encoding="utf-8")
    for cls in ("mc-green", "mc-orange", "mc-red", "mc-grey"):
        assert f".model-chip.{cls}" in css, f"{cls} chip color missing"
    i = css.index(".model-legend {")
    block = css[i:css.index("}", i)]
    assert "margin-left: auto" in block, "legend must be right-justified in the chip row"
    # A light-theme override exists so the colors do not wash out on --bg #faf9f7.
    assert 'data-theme="light"] .model-chip.mc-green' in css
