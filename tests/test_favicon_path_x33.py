"""Structural pin for the favicon brand mark (issue #33, gauntlet favicon-path).

THE WOUND: both FaultLine favicon.svg files drew the brand mark ⊢ (U+22A2, RIGHT TACK) as an
SVG <text font-family="Georgia, serif"> element. SVG favicons render with SYSTEM fonts — no
webfonts — and Georgia has no U+22A2 glyph, so the Windows serif fallback chain for the
Mathematical Operators block terminates in a CJK font (SimSun/MingLiU font-linking covers
U+22A2). New users on Windows saw "a Chinese symbol" in the tab (Reddit xXApolloXx3825,
2026-09-27; validated against production — /favicon.svg and /dashboard/favicon.svg were
byte-identical <text> SVGs, and Chrome prefers the SVG over the valid favicon.ico because
the HTML lists it first). In this open-core tree the one product favicon is webui/favicon.svg
(served by the web UI's index.html and ecosystem.html).

THE FLOOR (issue #33 frozen bar 1): every favicon.svg in the repo is font-free vector —
  * NO <text> element anywhere in the document,
  * NO font-family in any spelling (attribute, presentation attribute, or inside style=),
  * at least one <path> with a d attribute carrying the mark,
  * the ⊢ character itself never appears in a favicon SVG (the glyph is drawn, never typed),
  * every coordinate in the path data lies inside the file's own viewBox,
  * the FaultLine mark keeps its brand fill #3FB950 on the 64x64 dark tile.

The in-page ⊢ marks (signup splash, .oc-ico, .mono footers, the inline <svg><text> logos in
the web UI HTML) render with PAGE fonts and are deliberately out of scope — only the
favicon context (system fonts, no CSS) is wounded.

Each rule is exercised against the PRE-FIX shape re-introduced programmatically (the old
<text> line swapped back in) so the floor is proven to go red, not merely to pass.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# dirs that never hold product favicons (build/vcs/agent scratch)
SKIP_DIRS = {".git", "node_modules", ".worktrees", ".claude", "__pycache__", ".pytest_cache"}

# every favicon.svg that must exist — a rename must not silently vacate the pin
EXPECTED = {
    "webui/favicon.svg",
}

# the FaultLine tack favicons: brand fill on the 64x64 dark tile
FAULTLINE_FAVICONS = {
    "webui/favicon.svg": "#3FB950",
}

TACK = "\u22a2"  # ⊢ RIGHT TACK
PRE_FIX_LINE = (
    '  <text x="32" y="46" text-anchor="middle" font-family="Georgia, serif" '
    'font-size="44" font-weight="700" fill="#3FB950">\u22a2</text>'
)
POST_FIX_LINE = '  <path d="M18 16 H27 V27 H46 V36 H27 V46 H18 Z" fill="#3FB950"/>'


def favicon_svgs() -> dict[str, Path]:
    """Every favicon.svg in the repo (exact name — .ico/.png are rasters, out of scope)."""
    out: dict[str, Path] = {}
    for p in REPO.rglob("favicon.svg"):
        if not any(part in SKIP_DIRS for part in p.relative_to(REPO).parts):
            out[p.relative_to(REPO).as_posix()] = p
    return out


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def check(text: str) -> list[str]:
    """Return violations (empty == the favicon is font-free vector)."""
    v: list[str] = []
    if "<text" in text.lower():
        v.append("contains a <text> element (renders with system fonts as a favicon)")
    if "font-family" in text.lower():
        v.append("mentions font-family (attribute or style)")
    if TACK in text:
        v.append("contains the U+22A2 character itself (the mark must be drawn, not typed)")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return [f"does not parse as XML: {e}"]
    paths = [el for el in root.iter() if localname(el.tag) == "path"]
    with_d = [el for el in paths if el.get("d")]
    if not with_d:
        v.append("no <path> with a d attribute (nothing draws the mark)")
    vb = root.get("viewBox")
    if vb:
        try:
            _min_x, _min_y, max_x, max_y = (float(n) for n in vb.split())
            for el in with_d:
                for num in re.findall(r"[-+]?\d*\.?\d+", el.get("d")):
                    if not (0 <= float(num) <= max(max_x, max_y)):
                        v.append(f"path coordinate {num} outside viewBox {vb!r}")
                        break
                else:
                    continue
                break
        except ValueError:
            v.append(f"unparseable viewBox {vb!r}")
    return v


# ── the floor ────────────────────────────────────────────────────────────────────────────
def test_every_expected_favicon_svg_exists():
    found = set(favicon_svgs())
    missing = EXPECTED - found
    assert not missing, f"favicon.svg files disappeared (pin vacated): {sorted(missing)}"


@pytest.mark.parametrize("rel", sorted(EXPECTED))
def test_favicon_svg_is_font_free_vector(rel: str):
    p = REPO / rel
    assert p.exists(), f"{rel} missing"
    assert check(p.read_text()) == [], f"{rel}: {check(p.read_text())}"


@pytest.mark.parametrize("rel,fill", sorted(FAULTLINE_FAVICONS.items()))
def test_faultline_mark_keeps_brand_fill_and_tile(rel: str, fill: str):
    """Frozen bar 2: the mark is the same brand — same fill, same 64x64 dark tile."""
    text = (REPO / rel).read_text()
    root = ET.fromstring(text)
    assert root.get("viewBox") == "0 0 64 64"
    rects = [el for el in root.iter() if localname(el.tag) == "rect"]
    assert rects and rects[0].get("fill") == "#0F0F0F", "the dark tile background is gone"
    marks = [el for el in root.iter() if localname(el.tag) == "path" and el.get("d")]
    assert marks, "no drawn mark"
    assert any(el.get("fill") == fill for el in marks), f"mark lost brand fill {fill}"


def test_tack_path_is_the_right_tack_shape():
    """The drawn mark is recognizably ⊢ (RIGHT TACK / turnstile): a left vertical bar with
    a horizontal tee extending right from the bar's VERTICAL CENTER — the typographic
    turnstile, not a Γ (arm at top) or L (arm at bottom). Owner-ruling 2026-09-25:
    'the right piece appears to be rendered at the top, rather than in the font it's
    mid-way down — a turnstile.'"""
    text = (REPO / "webui/favicon.svg").read_text()
    d = next(el.get("d") for el in ET.fromstring(text).iter()
             if localname(el.tag) == "path" and el.get("d"))
    nums = [float(n) for n in re.findall(r"[-+]?\d*\.?\d+", d)]
    xs, ys = nums[0::2], nums[1::2]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    w, h = right - left, bottom - top
    # wide as the old 44px glyph's cap span, tall as its cap height, similar weight
    assert 20 <= w <= 36, f"mark width {w} not glyph-like"
    assert 24 <= h <= 36, f"mark height {h} not glyph-like"
    distinct_ys = sorted(set(ys))
    bar_center = (top + bottom) / 2
    # the arm's center must be near the bar's vertical center (the turnstile): the
    # arm-top is distinct_ys[1] and the arm-bottom is distinct_ys[2] (or the reverse);
    # their mean must sit within 25% of the bar's half-height from the bar center
    if len(distinct_ys) >= 4:
        arm_ys = distinct_ys[1:3]  # the two interior y-edges bounding the arm
        arm_center = sum(arm_ys) / 2
        assert abs(arm_center - bar_center) <= h * 0.25, (
            f"turnstile arm not vertically centered: arm_center={arm_center}, "
            f"bar_center={bar_center}, ys={distinct_ys}")
    else:
        raise AssertionError(f"expected >=4 distinct y-edges for the turnstile outline: {distinct_ys}")
    # the vertical bar spans the full height on the LEFT edge (its inner x sits left of
    # centre), and the tee reaches past it to the right
    centre = (left + right) / 2
    uniq_xs = sorted(set(xs))
    assert len(uniq_xs) >= 3, f"expected 3 distinct x edges (bar outer/inner, tee tip): {uniq_xs}"
    assert uniq_xs[1] <= centre + 1e-9, (
        f"vertical bar's inner edge not on the left half: {uniq_xs}, centre={centre}")


def test_every_favicon_svg_in_repo_is_font_free():
    """No favicon.svg anywhere in the tree may regress to a typed glyph — not only the
    EXPECTED ones (a new face added later is held to the same floor)."""
    bad = {rel: check(p.read_text()) for rel, p in favicon_svgs().items()}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"favicon.svg files that are not font-free vector: {bad}"


# ── the floor goes RED on the pre-fix shape (mutation self-check) ────────────────────────
@pytest.mark.parametrize("rel", sorted(FAULTLINE_FAVICONS))
def test_floor_goes_red_on_prefix_text_mark(rel: str):
    fixed = (REPO / rel).read_text()
    assert POST_FIX_LINE in fixed, f"mutation anchor missing in {rel} (mark changed shape?)"
    reverted = fixed.replace(POST_FIX_LINE, PRE_FIX_LINE, 1)
    assert reverted != fixed
    v = check(reverted)
    assert v and any("<text>" in x or "font-family" in x for x in v), (
        f"{rel}: pre-fix shape did not red the floor: {v}")


@pytest.mark.parametrize(
    "label,needle,expect",
    [
        ("glyph character typed into a path-less svg", TACK, "U+22A2"),
        ("font-family smuggled via style attribute", 'style="font-family:Georgia"', "font-family"),
    ],
)
def test_floor_goes_red_on_reintroduced_spelling(label: str, needle: str, expect: str):
    """The checker catches the glyph however it comes back — as the character itself or as a
    font-family inside style="" (where an attribute-only grep would miss it)."""
    fixed = (REPO / "webui/favicon.svg").read_text()
    assert needle not in fixed, f"{label}: needle already present?"
    if needle == TACK:
        mutated = fixed.replace("</svg>", f"  <title>{TACK}</title>\n</svg>", 1)
    else:
        mutated = fixed.replace(
            'fill="#3FB950"/>', f'fill="#3FB950" style="font-family:Georgia"/>', 1)
    v = check(mutated)
    assert any(expect in x for x in v), f"{label}: expected a violation containing {expect!r}, got {v}"
