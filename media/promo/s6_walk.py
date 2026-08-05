"""Scene 6 — the read path, the honest note, and the close.

Retrieval is an anchor resolution followed by a deterministic walk, so the
same question returns the same rows. Then the part most vendors would leave
out: the vector index still exists here, as a short-term scratchpad only.
"""

from manim import *
import numpy as np

from theme import *

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


def rel(a, b, text, color=FAINT, tcolor=DIM):
    ln = Line(
        a.get_right(), b.get_left(), buff=0.1,
        stroke_color=color, stroke_width=1.8,
    )
    lb = Text(text, font=MONO, color=tcolor).scale(0.2)
    lb.move_to(ln.get_center() + UP * 0.22)
    return VGroup(ln, lb)


class TheWalk(FilmScene):
    def construct(self):
        # ---- rebuild scene 5's closing graph exactly: the cut is a match cut
        wm = watermark()
        dev = pill("DevBox", stroke=GOOD).move_to([NODE_X, 0.85, 0])
        ip = pill("10.0.1.10", stroke=GOOD, color=GOOD).move_to([VAL_X, 1.9, 0])
        wks = pill("workstation", stroke=ACCENT).move_to([VAL_X, 0.85, 0])
        owner = pill("Christopher", stroke=ACCENT).move_to([VAL_X, -0.2, 0])
        e_ip = rel(dev, ip, "has_ip")
        e_ty = rel(dev, wks, "type")
        e_ow = rel(dev, owner, "owner")
        graph = VGroup(e_ip, e_ty, e_ow, dev, ip, wks, owner)
        self.add(graph, wm)

        ch = chapter("06", "ASKING FOR IT BACK")
        self.play(FadeIn(ch, shift=RIGHT * 0.2), run_time=0.6)

        # ------------------------------------------------------- the question
        q = mono('"What is DevBox\'s IP?"', size=0.36, color=INK)
        q.move_to([-3.9, 2.05, 0])
        self.play(AddTextLetterByLetter(q, run_time=1.2))

        # ---------------------------------------------------- anchor resolution
        anchor = label("anchor resolved  →  DevBox", size=0.26, color=ACCENT)
        anchor.move_to([-3.9, 1.15, 0])
        self.play(
            FadeIn(anchor, shift=RIGHT * 0.2),
            dev[0].animate.set_stroke(ACCENT, width=3.0),
            Flash(dev, color=ACCENT, line_length=0.18, num_lines=14,
                  flash_radius=0.7),
            run_time=0.9,
        )
        self.say("No search. It resolves the entity.", run_time=0.55)

        # ------------------------------------------------------------ the walk
        step = label("walk  →  has_ip", size=0.26, color=ACCENT)
        step.move_to([-3.9, 0.6, 0])
        self.play(FadeIn(step, shift=RIGHT * 0.2), run_time=0.4)

        trav = Dot(dev.get_right(), radius=0.08, color=ACCENT)
        self.add(trav)
        self.play(
            e_ip[0].animate.set_stroke(ACCENT, width=3.0),
            e_ip[1].animate.set_color(ACCENT),
            MoveAlongPath(trav, e_ip[0]),
            run_time=1.1,
            rate_func=rate_functions.ease_in_out_cubic,
        )
        self.play(
            FadeOut(trav, scale=0.4),
            Flash(ip, color=GOOD, line_length=0.18, num_lines=14, flash_radius=0.8),
            ip[0].animate.set_stroke(GOOD, width=3.0),
            run_time=0.6,
        )

        # ---------------------------------------------------------- the result
        row = VGroup(
            mono("DevBox", size=0.28, color=INK),
            mono("has_ip", size=0.28, color=DIM),
            mono("10.0.1.10", size=0.28, color=GOOD),
        ).arrange(RIGHT, buff=0.45)
        rframe = RoundedRectangle(
            width=row.width + 0.8, height=0.85, corner_radius=0.09,
            stroke_color=GOOD, stroke_width=1.8, fill_color=PANEL, fill_opacity=1.0,
        )
        row.move_to(rframe)
        result = VGroup(rframe, row).move_to([-3.9, -0.5, 0])
        rlbl = label("1 row  ·  180 tokens  ·  no interpretation required",
                     size=0.23, color=DIM)
        rlbl.next_to(result, DOWN, buff=0.3)

        self.play(FadeIn(result, shift=UP * 0.2), run_time=0.7)
        self.play(FadeIn(rlbl), run_time=0.45)
        self.say("It returns the row. Not the neighbourhood.",
                 color=GOOD, accent=GOOD, run_time=0.6, hold=1.3)

        # ------------------------------------------------------- determinism
        self.say("Same question, same walk, same rows — every time.",
                 color=GOOD, accent=GOOD, run_time=0.6, hold=1.4)

        # ------------------------------------------- the honest note on vectors
        self.play(
            FadeOut(q), FadeOut(anchor), FadeOut(step), FadeOut(result),
            FadeOut(rlbl),
            run_time=0.7,
        )

        cls_frame = RoundedRectangle(
            width=4.2, height=1.9, corner_radius=0.12,
            stroke_color=VIOLET, stroke_width=1.6, fill_opacity=0,
        ).move_to([-3.7, 0.15, 0])
        cls_lbl = label("CLASS C — SHORT-TERM SCRATCHPAD", size=0.2,
                        color=VIOLET, weight="BOLD")
        cls_lbl.next_to(cls_frame, UP, buff=0.18)
        rng = np.random.default_rng(9)
        specks = VGroup(
            *[
                Dot(
                    np.array([-3.7, 0.15, 0])
                    + np.array([*rng.uniform([-1.7, -0.6], [1.7, 0.6]), 0.0]),
                    radius=0.045, color=VIOLET, fill_opacity=0.7,
                )
                for _ in range(14)
            ]
        )
        self.play(
            Create(cls_frame), FadeIn(cls_lbl),
            LaggedStart(*[FadeIn(s, scale=0.5) for s in specks], lag_ratio=0.05),
            run_time=1.3,
        )
        self.say("Yes — there is still a vector index here.", run_time=0.55)

        promo = Arrow(
            cls_frame.get_right(), dev.get_left(), buff=0.25,
            stroke_width=2.2, color=VIOLET, tip_length=0.18,
            max_tip_length_to_length_ratio=0.1,
        )
        promo_l = label("promoted only once proven", size=0.21, color=VIOLET)
        promo_l.next_to(promo, UP, buff=0.14)
        self.play(GrowArrow(promo), FadeIn(promo_l), run_time=0.9)
        self.say("It holds what can't be classified yet — never the source of truth.",
                 color=VIOLET, accent=VIOLET, size=0.4, run_time=0.6, hold=1.7)

        # ---------------------------------------------------------- the close
        self.play(
            FadeOut(graph), FadeOut(cls_frame), FadeOut(cls_lbl), FadeOut(specks),
            FadeOut(promo), FadeOut(promo_l), FadeOut(ch), FadeOut(wm),
            run_time=0.9,
        )
        self.unsay(run_time=0.4)

        claims = VGroup(
            body("Runs entirely on your machine.", size=0.42, color=INK),
            body("One memory, shared across every model — over MCP.",
                 size=0.42, color=INK),
            body("Open source. AGPLv3.", size=0.42, color=INK),
        ).arrange(DOWN, buff=0.42, aligned_edge=LEFT)
        claims.move_to([0, 0.35, 0])

        self.cue("@claims")
        self.play(
            LaggedStart(*[FadeIn(c, shift=UP * 0.2) for c in claims], lag_ratio=0.35),
            run_time=2.0,
        )
        self.wait(1.4)
        self.play(FadeOut(claims, shift=UP * 0.2), run_time=0.7)

        mark = turnstile(h=1.4, stroke=6.5, color=INK).move_to([0, 0.95, 0])
        word = Text("FAULTLINE", font=FONT, weight="LIGHT", color=INK).scale(0.95)
        word.set_stroke(width=0).move_to([0, -0.35, 0])
        sub = Text("WRITE-VALIDATED MEMORY", font=FONT, color=DIM).scale(0.28)
        sub.move_to([0, -1.05, 0])
        url = Text("github.com/tkalevra/FaultLine", font=MONO, color=ACCENT).scale(0.3)
        url.move_to([0, -2.05, 0])

        self.cue("@endcard")
        self.play(Create(mark[0]), run_time=0.5)
        self.play(Create(mark[1]), run_time=0.4)
        self.play(
            LaggedStart(*[FadeIn(c, shift=UP * 0.15) for c in word], lag_ratio=0.05),
            run_time=0.9,
        )
        self.play(FadeIn(sub), run_time=0.5)
        self.play(FadeIn(url, shift=UP * 0.15), run_time=0.6)
        self.wait(2.2)
        self.play(
            FadeOut(VGroup(mark, word, sub, url)),
            run_time=1.1,
        )
        self.wait(0.4)
