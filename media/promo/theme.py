"""Shared visual language for the FaultLine promo film.

Palette is derived from the logo (near-black field, bone-white ink) with a
signal set chosen for contrast on #0F0F0F.
"""

import os
import sys

from manim import *
import numpy as np

# Point FL_CUES at a file and render with --dry_run to dump narration cue
# points (scene, time within the scene, line). build.py consumes these to place
# the voiceover on each beat instead of hand-timing it.
#
# Written to a file rather than stderr: manim's rich progress display owns the
# terminal during play(), and swallows anything printed while it is live.
CUE_PATH = os.environ.get("FL_CUES")


def _emit_cue(kind, scene, t, tag):
    if not CUE_PATH:
        return
    with open(CUE_PATH, "a", encoding="utf-8") as fh:
        fh.write(f"{kind}\t{scene}\t{t:.3f}\t{tag}\n")

# ---------------------------------------------------------------- palette
BG = "#0F0F0F"
PANEL = "#161616"
INK = "#E8E8E8"
DIM = "#8B8B8B"
FAINT = "#333333"
HAIR = "#242424"

ACCENT = "#58A6FF"   # the pipeline / neutral signal
GOOD = "#3FB950"     # validated, authoritative
BAD = "#F85149"      # rejected, poisoned, wrong
WARN = "#D29922"     # stale, competing, uncertain
VIOLET = "#BC8CFF"   # vector / similarity lane

FONT = "Inter"
MONO = "Source Code Pro"


# ------------------------------------------------------------- typography
def h1(text, size=54, color=INK, weight="BOLD"):
    return Text(text, font=FONT, weight=weight, color=color).scale_to_fit_height(
        size / 100
    )


def title(text, size=0.62, color=INK, weight="BOLD"):
    return Text(text, font=FONT, weight=weight, color=color).scale(size)


def body(text, size=0.36, color=DIM, weight="NORMAL"):
    return Text(text, font=FONT, weight=weight, color=color).scale(size)


def label(text, size=0.24, color=DIM, weight="MEDIUM"):
    return Text(text, font=FONT, weight=weight, color=color).scale(size)


def mono(text, size=0.3, color=INK, weight="NORMAL"):
    return Text(text, font=MONO, weight=weight, color=color).scale(size)


def chapter(num, text):
    """Top-left chapter marker."""
    g = VGroup(
        Text(num, font=MONO, weight="BOLD", color=ACCENT).scale(0.26),
        Text(text, font=FONT, weight="MEDIUM", color=DIM).scale(0.26),
    ).arrange(RIGHT, buff=0.28)
    g.to_corner(UL, buff=0.55)
    return g


def cite(text):
    """Academic citation, parked just above the watermark so they never collide."""
    t = Text(text, font=FONT, weight="NORMAL", color=DIM).scale(0.21)
    t.to_corner(DR, buff=0.5).shift(UP * 0.48)
    return t


def caption(text, weight="MEDIUM", color=INK, size=0.42):
    """The line of argument that sits under the diagram."""
    return Text(text, font=FONT, weight=weight, color=color).scale(size)


LT_Y = -2.72          # lower-third baseline, shared by every scene
LT_X = -6.35          # left margin


def lower_third(text, color=INK, accent=ACCENT, size=0.44, weight="MEDIUM"):
    """A film-style lower third: accent tick + line of argument.

    Every scene speaks through this band, so the eye always knows where the
    claim is going to appear.
    """
    tick = Line(UP * 0.16, DOWN * 0.16, stroke_color=accent, stroke_width=3.0)
    t = Text(text, font=FONT, weight=weight, color=color).scale(size)
    g = VGroup(tick, t).arrange(RIGHT, buff=0.3)
    g.move_to([LT_X + g.width / 2, LT_Y, 0])
    return g


def watermark(opacity=0.30):
    """Small brand lockup, bottom-right, present throughout."""
    mark = turnstile(h=0.3, stroke=2.4, color=INK)
    word = Text("FAULTLINE", font=FONT, weight="MEDIUM", color=INK).scale(0.21)
    word.set_stroke(width=0)
    g = VGroup(mark, word).arrange(RIGHT, buff=0.22)
    g.set_opacity(opacity)
    g.to_corner(DR, buff=0.55)
    return g


class FilmScene(Scene):
    """Base scene: dark field, persistent watermark, one talking line at a time."""

    def setup(self):
        self.camera.background_color = BG
        self._lt = None

    def brand(self, opacity=0.30):
        wm = watermark(opacity)
        self.add(wm)
        return wm

    def tear_down(self):
        _emit_cue("END", type(self).__name__,
                  getattr(self.renderer, "time", 0.0), "-")

    def cue(self, tag):
        """Mark a narration beat at the current time on the scene's clock."""
        _emit_cue("CUE", type(self).__name__,
                  getattr(self.renderer, "time", 0.0), tag)

    def say(self, text, color=INK, accent=ACCENT, size=0.44, weight="MEDIUM",
            run_time=0.55, hold=0.0):
        """Swap the lower third. Returns the new mobject."""
        new = lower_third(text, color=color, accent=accent, size=size, weight=weight)
        if self._lt is None:
            self.cue(text)
            self.play(FadeIn(new, shift=UP * 0.12), run_time=run_time)
        else:
            # sequenced, not crossfaded: two lines sharing one baseline
            # dissolving through each other reads as a smear
            self.play(FadeOut(self._lt, shift=UP * 0.26), run_time=0.26)
            self.cue(text)
            self.play(FadeIn(new, shift=UP * 0.14), run_time=run_time)
        self._lt = new
        if hold:
            self.wait(hold)
        return new

    def unsay(self, run_time=0.45):
        if self._lt is not None:
            self.play(FadeOut(self._lt, shift=DOWN * 0.1), run_time=run_time)
            self._lt = None


def turnstile(h=1.0, stroke=6.0, color=INK):
    """The ⊢ mark, drawn as geometry so it can be animated.

    Left: the vertical bar (the gate). Right: the horizontal entailment arm.
    """
    bar = Line(UP * h / 2, DOWN * h / 2, stroke_color=color, stroke_width=stroke)
    arm = Line(ORIGIN, RIGHT * h * 0.62, stroke_color=color, stroke_width=stroke)
    arm.move_to(bar.get_center() + RIGHT * h * 0.31)
    return VGroup(bar, arm)


# ------------------------------------------------------------------ parts
def doc_glyph(w=0.78, h=1.0, color=DIM, lines=4):
    """A sheet-of-paper icon."""
    page = RoundedRectangle(
        width=w, height=h, corner_radius=0.06,
        stroke_color=color, stroke_width=2.0, fill_color=PANEL, fill_opacity=1.0,
    )
    rules = VGroup()
    for i in range(lines):
        ln = Line(
            LEFT * (w * 0.30), RIGHT * (w * 0.30),
            stroke_color=color, stroke_width=1.6,
        )
        if i == lines - 1:
            ln.scale(0.55, about_point=ln.get_left())
        rules.add(ln)
    rules.arrange(DOWN, buff=h * 0.135).move_to(page)
    return VGroup(page, rules)


def box(text, w=2.1, h=1.05, color=INK, stroke=ACCENT, size=0.28, radius=0.1,
        weight="MEDIUM", fill=PANEL):
    r = RoundedRectangle(
        width=w, height=h, corner_radius=radius,
        stroke_color=stroke, stroke_width=2.2, fill_color=fill, fill_opacity=1.0,
    )
    t = Text(text, font=FONT, weight=weight, color=color).scale(size)
    if t.width > w * 0.84:
        t.scale_to_fit_width(w * 0.84)
    t.move_to(r)
    return VGroup(r, t)


def chunk_sq(size=0.2, color=VIOLET, opacity=0.16):
    return Square(
        side_length=size, stroke_color=color, stroke_width=1.5,
        fill_color=color, fill_opacity=opacity,
    )


def hairline(width=12.0, color=HAIR):
    return Line(LEFT * width / 2, RIGHT * width / 2, stroke_color=color, stroke_width=1.2)


def flow_arrow(start, end, color=FAINT, width=2.0):
    return Arrow(
        start, end, buff=0.18, stroke_width=width, color=color,
        max_tip_length_to_length_ratio=0.14, tip_length=0.16,
    )


# -------------------------------------------------------------- vector cloud
def cloud_points(n=150, seed=7, rx=2.55, ry=1.72, center=np.array([0.0, 0.0, 0.0])):
    """Deterministic blue-noise-ish scatter inside an ellipse.

    Deterministic so the cloud can be rebuilt identically in a later scene and
    the cut between them reads as one continuous shot.
    """
    rng = np.random.default_rng(seed)
    pts = []
    while len(pts) < n:
        x, y = rng.uniform(-1, 1, 2)
        if x * x + y * y > 1.0:
            continue
        # push outward slightly so the middle isn't a solid blob
        r = (x * x + y * y) ** 0.5
        k = 0.42 + 0.58 * r
        p = np.array([x * rx * k / max(r, 1e-6) * r, y * ry * k / max(r, 1e-6) * r, 0.0])
        pts.append(center + p)
    return np.array(pts)


def make_cloud(points, radius=0.043, color=VIOLET, opacity=0.72):
    return VGroup(
        *[
            Dot(p, radius=radius, color=color, fill_opacity=opacity)
            for p in points
        ]
    )


def nearest_k(points, query, k):
    """Real top-k by euclidean distance — the math on screen is the actual math."""
    d = np.linalg.norm(points[:, :2] - np.array(query)[:2], axis=1)
    return list(np.argsort(d)[:k])
