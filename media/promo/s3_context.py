"""Scene 3 — why MORE context makes the answer worse.

The causal chain has to be visible, not implied:
    retrieve more  ->  context grows  ->  your row drifts toward the middle
                   ->  and the middle is exactly where accuracy collapses.

So the context bar and the accuracy curve share one x-axis, and BOTH are
driven by a single "how much did you retrieve" tracker. As it climbs, the fill
extends, the marker for your row slides right, and the curve sags underneath
it — the dot falls because of the growth, on screen, in one continuous move.

The curve is a qualitative shape, not fabricated data: the y-axis is labelled
low/high with no numbers, and the finding is attributed on screen.
"""

from manim import *
import numpy as np

from theme import *

BAR_W = 10.4
BAR_H = 0.8
BAR_Y = 1.95
BAR_X0 = -BAR_W / 2
N_SLICES = 40
START_SLICES = 8
TOK_PER_CHUNK = 1240
ROW_FRAC = 0.55          # where your row sits inside whatever was retrieved

AX_Y0 = -2.05
AX_H = 1.85


def acc(x, L):
    """Accuracy at relative position x, for a context of severity L in [0,1].

    L=0 (a short context) is near-flat and high — the model reads all of it.
    L=1 (a long context) is the familiar sag: edges survive, middle collapses.
    """
    long_shape = 0.50 + 0.42 * (2 * x - 1) ** 2
    return 0.92 - L * (0.92 - long_shape)


class ContextCollapse(FilmScene):
    def construct(self):
        wm = watermark()
        ch = chapter("03", "WHY MORE CONTEXT MAKES IT WORSE")
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

        self.play(Create(frame), FadeIn(cw_lbl), FadeIn(start_l), FadeIn(end_l),
                  run_time=0.7)

        sw = BAR_W / N_SLICES
        slices = VGroup()
        for i in range(N_SLICES):
            s = Rectangle(
                width=sw * 0.86, height=BAR_H * 0.86, stroke_width=0,
                fill_color=VIOLET, fill_opacity=0.42,
            ).move_to([BAR_X0 + sw * (i + 0.5), BAR_Y, 0])
            slices.add(s)

        grow = ValueTracker(0.0)   # 0 = retrieved a little, 1 = retrieved a lot

        def shown():
            return START_SLICES + grow.get_value() * (N_SLICES - START_SLICES)

        def row_x():
            """Absolute x of your row: ROW_FRAC through whatever was retrieved."""
            return BAR_X0 + BAR_W * (shown() / N_SLICES) * ROW_FRAC

        def row_u():
            return (shown() / N_SLICES) * ROW_FRAC

        self.say("You retrieve a handful of chunks. That is your context.",
                 run_time=0.55)
        self.play(
            LaggedStart(*[FadeIn(s, scale=0.6) for s in slices[:START_SLICES]],
                        lag_ratio=0.08),
            run_time=1.2,
        )

        tok = always_redraw(
            lambda: mono(f"{int(shown()) * TOK_PER_CHUNK:,} tokens",
                         size=0.28, color=DIM)
            .next_to(frame, UP, buff=0.2).align_to(frame, RIGHT)
        )
        self.add(tok)

        # the row you actually needed
        marker = always_redraw(
            lambda: Rectangle(
                width=sw * 0.86, height=BAR_H * 0.86, stroke_width=0,
                fill_color=GOOD, fill_opacity=0.95,
            ).move_to([row_x(), BAR_Y, 0])
        )
        self.add(marker)
        tmark = label("the row you needed", size=0.22, color=GOOD)
        tmark.next_to(frame, UP, buff=0.62).shift(LEFT * 3.1)
        self.play(FadeIn(tmark), run_time=0.5)

        # -------------------------------------------------- the accuracy curve
        x_axis = Line([BAR_X0, AX_Y0, 0], [BAR_X0 + BAR_W, AX_Y0, 0],
                      stroke_color=FAINT, stroke_width=1.8)
        y_axis = Line([BAR_X0, AX_Y0, 0], [BAR_X0, AX_Y0 + AX_H, 0],
                      stroke_color=FAINT, stroke_width=1.8)
        y_hi = label("high", size=0.19, color=DIM).next_to(
            y_axis.get_top(), LEFT, buff=0.18)
        y_lo = label("low", size=0.19, color=DIM).next_to(
            y_axis.get_bottom(), LEFT, buff=0.18)
        y_name = label("ANSWER ACCURACY", size=0.2, color=DIM, weight="BOLD")
        y_name.rotate(PI / 2).next_to(y_axis, LEFT, buff=0.62)

        def to_screen(u, L):
            return np.array([BAR_X0 + u * BAR_W,
                             AX_Y0 + acc(u, L) * AX_H * 0.92, 0.0])

        curve = always_redraw(
            lambda: VMobject(stroke_color=ACCENT, stroke_width=3.4)
            .set_points_smoothly([
                to_screen(u, grow.get_value()) for u in np.linspace(0, 1, 40)
            ])
        )
        drop = always_redraw(
            lambda: DashedLine(
                [row_x(), BAR_Y - BAR_H / 2, 0],
                to_screen(row_u(), grow.get_value()),
                stroke_color=GOOD, stroke_width=1.8, dash_length=0.09,
            ).set_opacity(0.6)
        )
        hit = always_redraw(
            lambda: Dot(to_screen(row_u(), grow.get_value()),
                        radius=0.085, color=GOOD)
        )

        self.play(Create(x_axis), Create(y_axis),
                  FadeIn(y_hi), FadeIn(y_lo), FadeIn(y_name), run_time=0.8)
        self.add(curve, drop, hit)
        self.play(FadeIn(curve), FadeIn(drop), FadeIn(hit), run_time=0.7)
        c = cite('Liu et al., "Lost in the Middle," TACL 2023')
        self.play(FadeIn(c), run_time=0.45)
        self.say("Short context: the model reads all of it. Your row lands well.",
                 color=GOOD, accent=GOOD, run_time=0.55, hold=2.0)

        # ----------------------------- the whole argument, in one continuous move
        self.say("But precision was bad, so you retrieved more.", run_time=0.55)
        self.play(
            LaggedStart(*[FadeIn(s, scale=0.6) for s in slices[START_SLICES:]],
                        lag_ratio=0.02),
            grow.animate.set_value(1.0),
            run_time=4.0,
            rate_func=rate_functions.ease_in_out_cubic,
        )
        self.wait(0.4)
        self.say("The context grew — and the middle of it collapsed.",
                 color=WARN, accent=WARN, run_time=0.6, hold=2.0)
        self.say("Your row is now buried exactly where accuracy is worst.",
                 color=WARN, accent=WARN, run_time=0.6, hold=1.6)

        # ------------------------------------------------------ it writes anyway
        self.play(
            FadeOut(curve), FadeOut(x_axis), FadeOut(y_axis), FadeOut(y_hi),
            FadeOut(y_lo), FadeOut(y_name), FadeOut(drop), FadeOut(hit),
            FadeOut(c), FadeOut(start_l), FadeOut(end_l), FadeOut(tok),
            FadeOut(slices), FadeOut(frame), FadeOut(cw_lbl), FadeOut(marker),
            FadeOut(tmark),
            run_time=0.9,
        )

        self.say("So it does what it always does.", run_time=0.55)

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
