"""Scene 5 — FaultLine: the write path.

The logo's turnstile becomes the mechanism: statements are checked at the gate
before anything is stored. What passes lands as typed relationships in a graph;
what can't be grounded is rejected and never becomes a memory. Then a
correction, which supersedes rather than competes.
"""

from manim import *
import numpy as np

from theme import *

GATE_X = -1.15
CARD_X = -4.5
NODE_X = 1.85
VAL_X = 4.95


def pill(text, color=INK, stroke=ACCENT, size=0.26, fill=PANEL, h=0.6):
    t = mono(text, size=size, color=color)
    r = RoundedRectangle(
        width=t.width + 0.6, height=h, corner_radius=h / 2,
        stroke_color=stroke, stroke_width=1.8,
        fill_color=fill, fill_opacity=1.0,
    )
    t.move_to(r)
    return VGroup(r, t)


def stmt_card(text, color=DIM, tcolor=INK, w=4.3):
    t = mono(text, size=0.24, color=tcolor)
    if t.width > w - 0.5:
        t.scale_to_fit_width(w - 0.5)
    r = RoundedRectangle(
        width=w, height=0.68, corner_radius=0.08,
        stroke_color=color, stroke_width=1.6,
        fill_color=PANEL, fill_opacity=1.0,
    )
    t.move_to(r)
    return VGroup(r, t)


def rel(a, b, text, color=FAINT, tcolor=DIM):
    ln = Line(
        a.get_right(), b.get_left(), buff=0.1,
        stroke_color=color, stroke_width=1.8,
    )
    lb = Text(text, font=MONO, color=tcolor).scale(0.2)
    lb.move_to(ln.get_center() + UP * 0.22)
    return VGroup(ln, lb)


class TheGate(FilmScene):
    def construct(self):
        wm = watermark(0.0)   # held back until the logo has been established

        # ------------------------------------------------------ the logo forms
        mark = turnstile(h=1.5, stroke=7.0, color=INK).move_to([0, 0.75, 0])
        word = Text("FAULTLINE", font=FONT, weight="LIGHT", color=INK).scale(0.95)
        word.set_stroke(width=0)
        word.move_to([0, -0.55, 0])
        sub = Text("WRITE-VALIDATED MEMORY", font=FONT, weight="NORMAL",
                   color=DIM).scale(0.28)
        sub.move_to([0, -1.25, 0])

        self.cue("@logo")
        self.play(Create(mark[0]), run_time=0.6)
        self.play(Create(mark[1]), run_time=0.5)
        self.play(
            LaggedStart(
                *[FadeIn(ch, shift=UP * 0.18) for ch in word],
                lag_ratio=0.06,
            ),
            run_time=1.1,
        )
        self.play(FadeIn(sub), run_time=0.6)
        self.wait(1.1)

        # the mark becomes the gate it always depicted
        gate = turnstile(h=3.4, stroke=5.0, color=INK)
        gate.move_to([GATE_X, 0.55, 0])
        ch_lbl = chapter("05", "WHAT FAULTLINE DOES INSTEAD")

        self.play(
            FadeOut(word, shift=DOWN * 0.25),
            FadeOut(sub, shift=DOWN * 0.25),
            Transform(mark, gate),
            run_time=1.2,
            rate_func=rate_functions.ease_in_out_cubic,
        )
        gate_lbl = label("VALIDATION GATE", size=0.22, color=INK, weight="BOLD")
        gate_lbl.next_to(gate, UP, buff=0.3)
        self.play(
            FadeIn(ch_lbl, shift=RIGHT * 0.2),
            FadeIn(gate_lbl),
            wm.animate.set_opacity(0.30),
            run_time=0.7,
        )
        self.add(wm)

        # ------------------------------------------------- statements arrive
        self.say("Nothing is stored until it has been checked.", run_time=0.55)

        graph = VGroup()

        # --- 1. a grounded fact passes
        c1 = stmt_card('"DevBox is at 10.0.1.5"').move_to([CARD_X, 1.6, 0])
        self.play(FadeIn(c1, shift=RIGHT * 0.3), run_time=0.6)
        self.play(c1.animate.move_to([GATE_X, 1.6, 0]).set_opacity(0.0),
                  run_time=0.8, rate_func=rate_functions.ease_in_sine)
        self.play(
            mark.animate.set_color(GOOD),
            Flash(gate.get_center() + UP * 1.05, color=GOOD,
                  line_length=0.2, num_lines=12, flash_radius=0.4),
            run_time=0.45,
        )

        dev = pill("DevBox", stroke=GOOD).move_to([NODE_X, 0.85, 0])
        ip1 = pill("10.0.1.5", stroke=GOOD, color=GOOD).move_to([VAL_X, 1.9, 0])
        e1 = rel(dev, ip1, "has_ip")
        self.play(
            mark.animate.set_color(INK),
            FadeIn(dev, scale=0.7),
            run_time=0.5,
        )
        self.play(Create(e1[0]), FadeIn(e1[1]), FadeIn(ip1, scale=0.7), run_time=0.7)
        graph.add(dev, ip1, e1)

        # --- 2. more structure
        wks = pill("workstation", stroke=ACCENT).move_to([VAL_X, 0.85, 0])
        e2 = rel(dev, wks, "type")
        owner = pill("Christopher", stroke=ACCENT).move_to([VAL_X, -0.2, 0])
        e3 = rel(dev, owner, "owner")
        self.play(
            LaggedStart(
                AnimationGroup(Create(e2[0]), FadeIn(e2[1]), FadeIn(wks, scale=0.7)),
                AnimationGroup(Create(e3[0]), FadeIn(e3[1]), FadeIn(owner, scale=0.7)),
                lag_ratio=0.45,
            ),
            run_time=1.5,
        )
        graph.add(wks, e2, owner, e3)
        self.say("Facts land as typed relationships — not text, not chunks.",
                 run_time=0.6, hold=0.9)

        # --- 3. an ungrounded claim is turned away
        c2 = stmt_card('"DevBox was probably decommissioned"', color=WARN,
                       tcolor=WARN)
        c2.move_to([CARD_X, -1.75, 0])
        self.play(FadeIn(c2, shift=RIGHT * 0.3), run_time=0.6)
        self.play(c2.animate.move_to([GATE_X - 1.5, -1.75, 0]),
                  run_time=0.7, rate_func=rate_functions.ease_in_sine)
        self.play(
            Wiggle(c2, scale_value=1.06, rotation_angle=0.02 * TAU),
            mark.animate.set_color(BAD),
            run_time=0.7,
        )
        rej = label("REJECTED — not grounded", size=0.23, color=BAD, weight="BOLD")
        rej.next_to(c2, DOWN, buff=0.3)
        self.play(
            c2[0].animate.set_stroke(BAD), c2[1].animate.set_color(BAD),
            FadeIn(rej),
            run_time=0.5,
        )
        self.play(
            c2.animate.shift(DOWN * 1.1).set_opacity(0.0),
            rej.animate.shift(DOWN * 1.1).set_opacity(0.0),
            mark.animate.set_color(INK),
            run_time=0.8,
        )
        self.remove(c2, rej)
        self.say("A hallucination never becomes a memory.",
                 color=GOOD, accent=GOOD, run_time=0.6, hold=1.5)

        # ------------------------------------------------------- a correction
        self.say("And when something changes —", run_time=0.5)

        c3 = stmt_card('"Actually, DevBox moved to 10.0.1.10"', color=ACCENT)
        c3.move_to([CARD_X, -1.75, 0])
        self.play(FadeIn(c3, shift=RIGHT * 0.3), run_time=0.6)
        self.play(c3.animate.move_to([GATE_X, -1.75, 0]).set_opacity(0.0),
                  run_time=0.8, rate_func=rate_functions.ease_in_sine)
        self.play(
            mark.animate.set_color(GOOD),
            Flash(gate.get_center() + DOWN * 2.3, color=GOOD,
                  line_length=0.2, num_lines=12, flash_radius=0.4),
            run_time=0.45,
        )
        self.play(mark.animate.set_color(INK), run_time=0.3)

        # the edge retargets; the old value is archived, not left to compete
        arch_frame = RoundedRectangle(
            width=2.9, height=0.95, corner_radius=0.1,
            stroke_color=FAINT, stroke_width=1.4, fill_opacity=0,
        ).move_to([VAL_X, -1.85, 0])
        arch_lbl = label("ARCHIVED", size=0.2, color=DIM, weight="BOLD")
        arch_lbl.next_to(arch_frame, UP, buff=0.14)

        ip2 = pill("10.0.1.10", stroke=GOOD, color=GOOD).move_to([VAL_X, 1.9, 0])
        e1b = rel(dev, ip2, "has_ip")

        self.play(Create(arch_frame), FadeIn(arch_lbl), run_time=0.6)
        self.play(
            ip1.animate.move_to([VAL_X, -1.85, 0]).scale(0.82).set_opacity(0.45),
            FadeOut(e1[0]), FadeOut(e1[1]),
            run_time=0.9,
            rate_func=rate_functions.ease_in_out_cubic,
        )
        stamp = Text("superseded", font=MONO, color=DIM).scale(0.17)
        stamp.next_to(ip1, DOWN, buff=0.12)
        self.play(
            FadeIn(ip2, scale=0.7), Create(e1b[0]), FadeIn(e1b[1]),
            FadeIn(stamp),
            run_time=0.9,
        )

        self.say("the record is superseded — the old value archived, not competing.",
                 color=GOOD, accent=GOOD, size=0.42, run_time=0.6, hold=1.8)

        self.play(
            FadeOut(arch_frame), FadeOut(arch_lbl), FadeOut(ip1), FadeOut(stamp),
            FadeOut(mark), FadeOut(gate_lbl), FadeOut(ch_lbl),
            run_time=0.8,
        )
        self.unsay(run_time=0.4)
        # leaves the graph standing for scene 6
        self.wait(0.2)
