"""Scene 4 — RAG poisoning.

The write path has no gate, so anything that reaches the index is treated as
knowledge. Attacker-crafted texts are embedded by the same model and are
engineered to land nearest the target question.

Attribution on screen: Zou et al., PoisonedRAG, USENIX Security 2025 —
five injected texts, ~90% attack success against a corpus of millions.
"""

from manim import *
import numpy as np

from theme import *

STAGE_Y = 0.55
CLOUD_CENTER = np.array([3.35, STAGE_Y, 0.0])
QUERY_POINT = np.array([3.05, 0.25, 0.0])


class Poisoning(FilmScene):
    def construct(self):
        wm = watermark()
        ch = chapter("04", "AND THERE IS NO GATE")
        self.play(FadeIn(ch, shift=RIGHT * 0.2), FadeIn(wm), run_time=0.6)

        # ------------------------------------------------------- the index
        pts = cloud_points(n=320, seed=11, rx=2.6, ry=1.78, center=CLOUD_CENTER)
        cloud = make_cloud(pts, radius=0.034, opacity=0.6)
        store_frame = RoundedRectangle(
            width=6.0, height=4.1, corner_radius=0.14,
            stroke_color=HAIR, stroke_width=1.6, fill_opacity=0,
        ).move_to(CLOUD_CENTER)
        store_lbl = label("VECTOR STORE", size=0.23, color=VIOLET, weight="BOLD")
        store_lbl.next_to(store_frame, UP, buff=0.18).align_to(store_frame, LEFT)
        count = label("millions of chunks", size=0.22, color=DIM)
        count.next_to(store_frame, UP, buff=0.18).align_to(store_frame, RIGHT)

        self.play(
            Create(store_frame), FadeIn(store_lbl), FadeIn(count),
            LaggedStart(*[FadeIn(d, scale=0.5) for d in cloud], lag_ratio=0.002),
            run_time=1.8,
        )

        # -------------------------------------------------- where writes come from
        sources = VGroup(
            label("a crawled web page", size=0.26, color=DIM),
            label("a shared drive", size=0.26, color=DIM),
            label("a wiki anyone can edit", size=0.26, color=DIM),
            label("a support ticket", size=0.26, color=DIM),
        ).arrange(DOWN, buff=0.28, aligned_edge=LEFT)
        sources.move_to([-4.6, STAGE_Y + 0.25, 0])

        self.say("Anything that reaches the index becomes knowledge.", run_time=0.55)
        self.play(
            LaggedStart(*[FadeIn(s, shift=RIGHT * 0.2) for s in sources],
                        lag_ratio=0.14),
            run_time=1.4,
        )
        self.wait(0.4)

        # ------------------------------------------------------- the injection
        self.play(FadeOut(sources), run_time=0.5)

        atk = box("5 crafted texts", w=3.0, h=1.15, stroke=BAD, color=BAD, size=0.28)
        atk.move_to([-4.6, STAGE_Y + 0.25, 0])
        emb = box("embedding model", w=2.3, h=1.0, stroke=VIOLET, size=0.24)
        emb.move_to([-1.3, STAGE_Y + 0.25, 0])
        a1 = flow_arrow(atk.get_right(), emb.get_left())

        self.play(Create(atk[0]), FadeIn(atk[1]), run_time=0.6)
        self.play(Create(emb[0]), FadeIn(emb[1]), GrowArrow(a1), run_time=0.6)
        self.say("The same pipeline. No validation. No gate.",
                 color=BAD, accent=BAD, run_time=0.55)

        # crafted to land right on top of the target question
        rng = np.random.default_rng(5)
        poison_pts = [
            QUERY_POINT + np.array([*(rng.uniform(-0.24, 0.24, 2)), 0.0])
            for _ in range(5)
        ]
        poison = VGroup(*[Dot(p, radius=0.062, color=BAD) for p in poison_pts])

        travellers = VGroup(
            *[Square(side_length=0.2, stroke_color=BAD, stroke_width=1.6,
                     fill_color=BAD, fill_opacity=0.2).move_to(atk.get_center())
              for _ in range(5)]
        )
        self.add(travellers)
        self.play(
            LaggedStart(
                *[
                    Succession(
                        travellers[i].animate(run_time=0.45).move_to(emb.get_center())
                        .scale(0.3).set_opacity(0.0),
                        Transform(travellers[i], poison[i], run_time=0.6),
                    )
                    for i in range(5)
                ],
                lag_ratio=0.16,
            ),
            run_time=1.8,
        )
        self.add(poison)
        self.remove(travellers)
        self.play(FadeOut(atk), FadeOut(emb), FadeOut(a1), run_time=0.5)

        # ---------------------------------------------------------- the query
        qdot = Dot(QUERY_POINT, radius=0.075, color=ACCENT)
        self.play(FadeIn(qdot, scale=2.0), run_time=0.45)
        self.say("Ask the question they targeted.", run_time=0.5)

        ring = Circle(
            radius=0.06, arc_center=QUERY_POINT,
            stroke_color=ACCENT, stroke_width=2.0, fill_opacity=0.0,
        )
        self.add(ring)
        self.play(ring.animate.scale(0.42 / 0.06), run_time=1.2,
                  rate_func=rate_functions.ease_out_cubic)
        self.play(
            *[p.animate.scale(1.7) for p in poison],
            Flash(qdot, color=BAD, line_length=0.2, num_lines=14, flash_radius=0.35),
            run_time=0.6,
        )

        llm = box("LLM", w=1.9, h=1.15, stroke=INK, color=INK, size=0.32, weight="BOLD")
        llm.move_to([-3.9, STAGE_Y + 0.45, 0])
        self.play(Create(llm[0]), FadeIn(llm[1]), run_time=0.5)

        trav2 = VGroup(*[p.copy() for p in poison])
        self.add(trav2)
        self.play(
            LaggedStart(
                *[t.animate.move_to(llm.get_center()).scale(0.4).set_opacity(0.0)
                  for t in trav2],
                lag_ratio=0.1,
            ),
            run_time=1.2,
        )
        self.remove(trav2)

        answer = mono("DevBox is at 203.0.113.66.", size=0.34, color=BAD)
        answer.next_to(llm, DOWN, buff=0.95)
        self.play(
            Flash(llm, color=BAD, line_length=0.2, num_lines=16, flash_radius=0.95),
            FadeIn(answer, shift=DOWN * 0.15),
            run_time=0.85,
        )
        atk_lbl = label("the attacker's answer", size=0.23, color=BAD)
        atk_lbl.next_to(answer, DOWN, buff=0.3)
        self.play(FadeIn(atk_lbl), run_time=0.4)

        self.say("It is retrieved, trusted, and repeated as fact.",
                 color=BAD, accent=BAD, run_time=0.6, hold=1.2)

        # ------------------------------------------------------------ the stat
        self.play(
            FadeOut(llm), FadeOut(answer), FadeOut(atk_lbl), FadeOut(qdot),
            FadeOut(ring), FadeOut(store_frame), FadeOut(store_lbl), FadeOut(count),
            FadeOut(cloud), FadeOut(poison),
            run_time=0.9,
        )

        big = Text("5", font=FONT, weight="BOLD", color=BAD).scale(2.6)
        big_l = body("injected texts", size=0.4).next_to(big, DOWN, buff=0.3)
        g1 = VGroup(big, big_l)
        pct = Text("90%", font=FONT, weight="BOLD", color=BAD).scale(2.6)
        pct_l = body("attack success rate", size=0.4).next_to(pct, DOWN, buff=0.3)
        g2 = VGroup(pct, pct_l)
        stats = VGroup(g1, g2).arrange(RIGHT, buff=2.6).move_to([0, 0.75, 0])

        self.play(FadeIn(g1, shift=UP * 0.2), run_time=0.7)
        self.play(FadeIn(g2, shift=UP * 0.2), run_time=0.7)
        c = cite("Zou et al., PoisonedRAG, USENIX Security 2025")
        self.play(FadeIn(c), run_time=0.5)
        self.say("Five documents. In a corpus of millions.",
                 color=BAD, accent=BAD, run_time=0.6, hold=1.9)

        self.play(FadeOut(stats), FadeOut(c), FadeOut(ch), run_time=0.8)
        self.unsay(run_time=0.4)
        self.wait(0.2)
