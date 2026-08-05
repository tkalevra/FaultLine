"""Scene 3 — Bloated context, and what it does to the model.

The context bar and the accuracy curve deliberately share one x-axis, so the
argument is made geometrically: the fact you needed is buried at the position
where models read worst.

The curve is drawn as a qualitative shape, not fabricated data — the y-axis is
labelled low/high and the finding is attributed on screen.
"""

from manim import *
import numpy as np

from theme import *

BAR_W = 10.4
BAR_H = 0.8
BAR_Y = 1.95
BAR_X0 = -BAR_W / 2
N_SLICES = 44
TRUTH_SLICE = 24          # where the row you needed ends up

AX_Y0 = -2.05             # accuracy axis baseline
AX_H = 1.85


def acc(x):
    """Qualitative shape: strong at the edges, sagging through the middle."""
    return 0.50 + 0.42 * (2 * x - 1) ** 2 + 0.06 * (1 - x)


class ContextCollapse(FilmScene):
    def construct(self):
        wm = watermark()
        ch = chapter("03", "WHAT THE MODEL ACTUALLY SEES")
        self.play(FadeIn(ch, shift=RIGHT * 0.2), FadeIn(wm), run_time=0.6)

        # ------------------------------------------------- the context window
        frame = RoundedRectangle(
            width=BAR_W + 0.12, height=BAR_H + 0.12, corner_radius=0.07,
            stroke_color=HAIR, stroke_width=1.6, fill_opacity=0,
        ).move_to([0, BAR_Y, 0])
        cw_lbl = label("CONTEXT WINDOW", size=0.23, color=INK, weight="BOLD")
        cw_lbl.next_to(frame, UP, buff=0.2).align_to(frame, LEFT)

        start_l = label("start", size=0.2, color=DIM)
        start_l.next_to(frame, DOWN, buff=0.16).align_to(frame, LEFT)
        end_l = label("end", size=0.2, color=DIM)
        end_l.next_to(frame, DOWN, buff=0.16).align_to(frame, RIGHT)

        self.play(Create(frame), FadeIn(cw_lbl), run_time=0.6)

        sw = BAR_W / N_SLICES
        slices = VGroup()
        for i in range(N_SLICES):
            is_truth = i == TRUTH_SLICE
            s = Rectangle(
                width=sw * 0.86, height=BAR_H * 0.86,
                stroke_width=0,
                fill_color=GOOD if is_truth else VIOLET,
                fill_opacity=0.95 if is_truth else 0.42,
            )
            s.move_to([BAR_X0 + sw * (i + 0.5), BAR_Y, 0])
            slices.add(s)

        tok = ValueTracker(0)
        tok_read = always_redraw(
            lambda: mono(f"{int(tok.get_value()):,} tokens", size=0.28, color=DIM)
            .next_to(frame, UP, buff=0.2).align_to(frame, RIGHT)
        )
        self.add(tok_read)

        self.say("Everything retrieved gets pasted in front of your question.",
                 run_time=0.55)
        self.play(
            LaggedStart(*[FadeIn(s, scale=0.6) for s in slices], lag_ratio=0.028),
            tok.animate.set_value(49_600),
            run_time=2.6,
        )
        self.wait(0.3)

        # the one row that mattered
        truth = slices[TRUTH_SLICE]
        tmark = label("the one row you needed", size=0.22, color=GOOD)
        tmark.next_to(truth, UP, buff=0.55)
        tarrow = Arrow(
            tmark.get_bottom(), truth.get_top(), buff=0.1,
            stroke_width=2.0, color=GOOD, tip_length=0.14,
            max_tip_length_to_length_ratio=0.4,
        )
        self.play(
            FadeIn(tmark), GrowArrow(tarrow),
            Flash(truth, color=GOOD, line_length=0.14, num_lines=12, flash_radius=0.45),
            run_time=0.8,
        )
        self.say("One of them is the answer. The model has to find it.",
                 run_time=0.55, hold=0.9)

        self.play(FadeOut(tmark), FadeOut(tarrow), FadeIn(start_l), FadeIn(end_l),
                  run_time=0.5)

        # -------------------------------------------------- the accuracy curve
        x_axis = Line(
            [BAR_X0, AX_Y0, 0], [BAR_X0 + BAR_W, AX_Y0, 0],
            stroke_color=FAINT, stroke_width=1.8,
        )
        y_axis = Line(
            [BAR_X0, AX_Y0, 0], [BAR_X0, AX_Y0 + AX_H, 0],
            stroke_color=FAINT, stroke_width=1.8,
        )
        y_hi = label("high", size=0.19, color=DIM).next_to(
            y_axis.get_top(), LEFT, buff=0.18)
        y_lo = label("low", size=0.19, color=DIM).next_to(
            y_axis.get_bottom(), LEFT, buff=0.18)
        y_name = label("ACCURACY", size=0.2, color=DIM, weight="BOLD")
        y_name.rotate(PI / 2).next_to(y_axis, LEFT, buff=0.62)

        self.play(
            Create(x_axis), Create(y_axis),
            FadeIn(y_hi), FadeIn(y_lo), FadeIn(y_name),
            run_time=0.8,
        )

        def to_screen(x):
            return np.array([BAR_X0 + x * BAR_W, AX_Y0 + acc(x) * AX_H * 0.92, 0.0])

        curve = VMobject(stroke_color=ACCENT, stroke_width=3.4)
        curve.set_points_smoothly([to_screen(x) for x in np.linspace(0, 1, 40)])

        self.say("Long context does not mean evenly read.", run_time=0.55)
        self.play(Create(curve), run_time=1.9, rate_func=rate_functions.ease_in_out_sine)

        c = cite('Liu et al., "Lost in the Middle," TACL 2023')
        self.play(FadeIn(c), run_time=0.5)

        # drop the buried fact onto the curve
        xpos = (TRUTH_SLICE + 0.5) / N_SLICES
        drop = DashedLine(
            truth.get_bottom(), to_screen(xpos),
            stroke_color=GOOD, stroke_width=1.8, dash_length=0.09,
        ).set_opacity(0.65)
        hit = Dot(to_screen(xpos), radius=0.085, color=GOOD)
        self.play(Create(drop), run_time=0.9)
        self.play(
            FadeIn(hit, scale=2.2),
            Flash(hit, color=GOOD, line_length=0.16, num_lines=12, flash_radius=0.3),
            run_time=0.6,
        )
        self.say("Accuracy is highest at the edges — and sags in the middle.",
                 color=WARN, accent=WARN, run_time=0.6, hold=1.6)

        # ------------------------------------------------------ it writes anyway
        self.play(
            FadeOut(curve), FadeOut(x_axis), FadeOut(y_axis), FadeOut(y_hi),
            FadeOut(y_lo), FadeOut(y_name), FadeOut(drop), FadeOut(hit),
            FadeOut(c), FadeOut(start_l), FadeOut(end_l), FadeOut(tok_read),
            FadeOut(slices), FadeOut(frame), FadeOut(cw_lbl),
            run_time=0.9,
        )

        self.say("So it does the thing it always does.", run_time=0.55)

        # positioned by hand: Text() trims the trailing space, so arrange()
        # would butt "at" straight against the address
        out1 = mono("DevBox is at", size=0.44, color=INK)
        out2 = mono("10.0.1.5", size=0.44, color=WARN).next_to(out1, RIGHT, buff=0.17)
        out3 = mono(", managed by the network team.", size=0.44, color=BAD)
        out3.next_to(out2, RIGHT, buff=0.03)
        line = VGroup(out1, out2, out3)
        line.move_to([0, 0.75, 0])

        self.play(AddTextLetterByLetter(out1, run_time=0.7))
        self.play(AddTextLetterByLetter(out2, run_time=0.5))
        self.play(AddTextLetterByLetter(out3, run_time=1.1))
        self.wait(0.3)

        u1 = Underline(out2, color=WARN, stroke_width=2.4).shift(DOWN * 0.06)
        u2 = Underline(out3, color=BAD, stroke_width=2.4).shift(DOWN * 0.06)
        n1 = label("stale — superseded 6 months ago", size=0.24, color=WARN)
        n1.next_to(u1, DOWN, buff=0.32).shift(LEFT * 0.4)
        n2 = label("never appeared in any document", size=0.24, color=BAD)
        n2.next_to(u2, DOWN, buff=1.05)

        self.play(Create(u1), FadeIn(n1, shift=DOWN * 0.1), run_time=0.7)
        self.play(Create(u2), FadeIn(n2, shift=DOWN * 0.1), run_time=0.7)

        self.say("It will not say “I don’t know.” It fills the gap.",
                 color=BAD, accent=BAD, run_time=0.6, hold=1.9)

        self.play(
            FadeOut(line), FadeOut(u1), FadeOut(u2), FadeOut(n1), FadeOut(n2),
            FadeOut(ch),
            run_time=0.8,
        )
        self.unsay(run_time=0.4)
        self.wait(0.2)
