"""Scene 2 — Where the pipeline breaks.

Two failures, in order of how much they cost you:
  A. Nearest is not correct — the row you needed ranks 47th.
  B. Nothing can be removed — a correction is just another chunk, and the
     stale sentence is duplicated across chunks with no key to delete by.
Ends by raising k, which hands the bloat problem to scene 3.

Opens on the exact geometry scene 1 closed on, so the cut is invisible.
"""

from manim import *
import numpy as np

from theme import *

STAGE_Y = 0.55
CLOUD_CENTER = np.array([3.35, STAGE_Y, 0.0])
QUERY_POINT = np.array([3.05, 0.25, 0.0])


def chunk_card(text, score, w=5.3, color=VIOLET, text_color=INK, score_color=DIM):
    body_t = mono(text, size=0.26, color=text_color)
    if body_t.width > w - 1.5:
        body_t.scale_to_fit_width(w - 1.5)
    sc = mono(score, size=0.24, color=score_color)
    frame = RoundedRectangle(
        width=w, height=0.72, corner_radius=0.08,
        stroke_color=color, stroke_width=1.6,
        fill_color=PANEL, fill_opacity=1.0,
    )
    body_t.move_to(frame.get_left() + RIGHT * (body_t.width / 2 + 0.28))
    sc.move_to(frame.get_right() + LEFT * (sc.width / 2 + 0.28))
    return VGroup(frame, body_t, sc)


class RagFailure(FilmScene):
    def construct(self):
        # ---- rebuild scene 1's closing frame, unanimated: the cut is a match cut
        pts = cloud_points(n=150, seed=7, center=CLOUD_CENTER)
        cloud = make_cloud(pts)
        store_frame = RoundedRectangle(
            width=6.0, height=4.1, corner_radius=0.14,
            stroke_color=HAIR, stroke_width=1.6, fill_opacity=0,
        ).move_to(CLOUD_CENTER)
        store_lbl = label("VECTOR STORE", size=0.23, color=VIOLET, weight="BOLD")
        store_lbl.next_to(store_frame, UP, buff=0.18).align_to(store_frame, LEFT)
        wm = watermark()
        self.add(cloud, store_frame, store_lbl, wm)

        ch = chapter("02", "WHERE RAG BREAKS BY ACCIDENT")
        self.play(FadeIn(ch, shift=RIGHT * 0.2), run_time=0.6)

        # =============================================== A. nearest ≠ correct
        k = 3
        idx = nearest_k(pts, QUERY_POINT, k)
        rad = float(np.linalg.norm(pts[idx[-1]][:2] - QUERY_POINT[:2])) + 0.1

        qdot = Dot(QUERY_POINT, radius=0.075, color=ACCENT)
        ring = Circle(
            radius=rad, arc_center=QUERY_POINT,
            stroke_color=ACCENT, stroke_width=2.0, fill_opacity=0.0,
        )
        self.play(FadeIn(qdot, scale=2.0), Create(ring), run_time=0.7)
        hits = VGroup(*[cloud[i] for i in idx])
        self.play(*[h.animate.set_color(ACCENT).scale(1.8) for h in hits], run_time=0.5)

        self.say("You asked for one fact. Here is what came back.", run_time=0.55)

        cards = VGroup(
            chunk_card('"DevBox runs Ubuntu 22.04 LTS…"', "0.83"),
            chunk_card('"…dev boxes take IPs from DHCP…"', "0.81"),
            chunk_card('"DevBox was rebuilt last spring…"', "0.78"),
        ).arrange(DOWN, buff=0.26)
        cards.move_to([-3.55, STAGE_Y + 0.62, 0])

        links = VGroup(
            *[
                Line(
                    hits[i].get_center(), cards[i].get_right(),
                    stroke_color=ACCENT, stroke_width=1.2,
                ).set_opacity(0.35)
                for i in range(k)
            ]
        )
        self.play(
            LaggedStart(
                *[
                    AnimationGroup(Create(links[i]), FadeIn(cards[i], shift=RIGHT * 0.2))
                    for i in range(k)
                ],
                lag_ratio=0.25,
            ),
            run_time=1.6,
        )
        self.wait(0.4)

        # the row you actually needed is sitting outside the radius
        far_i = int(np.argsort(np.linalg.norm(pts[:, :2] - QUERY_POINT[:2], axis=1))[46])
        truth_dot = cloud[far_i]
        truth = chunk_card(
            '"DevBox has IP 10.0.1.10"', "rank 47", color=GOOD,
            text_color=GOOD, score_color=GOOD,
        )
        truth.next_to(cards, DOWN, buff=0.4).align_to(cards, LEFT)
        tlink = Line(
            truth_dot.get_center(), truth.get_right(),
            stroke_color=GOOD, stroke_width=1.2,
        ).set_opacity(0.4)

        self.play(
            truth_dot.animate.set_color(GOOD).scale(2.0),
            Create(tlink),
            FadeIn(truth, shift=RIGHT * 0.2),
            run_time=1.0,
        )
        self.say("The row you needed ranked 47th. Similarity is not truth.",
                 color=WARN, accent=WARN, run_time=0.6, hold=1.7)

        self.play(
            FadeOut(cards), FadeOut(links), FadeOut(truth), FadeOut(tlink),
            FadeOut(ring),
            truth_dot.animate.set_color(VIOLET).scale(1 / 2.0),
            *[h.animate.set_color(VIOLET).scale(1 / 1.8) for h in hits],
            run_time=0.8,
        )

        # ============================================ B. nothing can be removed
        self.say("So you correct it.", run_time=0.5)

        correction = mono('"Actually, DevBox moved to 10.0.1.10."',
                          size=0.32, color=INK)
        correction.move_to([-3.55, 2.15, 0])
        self.play(AddTextLetterByLetter(correction, run_time=1.2))

        old = chunk_card('"DevBox has IP 10.0.1.5"', "6 months old", color=WARN,
                         text_color=WARN, score_color=WARN, w=5.3)
        new = chunk_card('"DevBox has IP 10.0.1.10"', "today", color=GOOD,
                         text_color=GOOD, score_color=GOOD, w=5.3)
        pair = VGroup(new, old).arrange(DOWN, buff=0.3).move_to([-3.55, 0.35, 0])

        # the correction lands as a brand new point; the old one is untouched
        new_i = 44
        new_dot = cloud[new_i]
        old_i = 61
        old_dot = cloud[old_i]

        self.play(
            FadeIn(new, shift=DOWN * 0.2),
            new_dot.animate.set_color(GOOD).scale(1.9),
            run_time=0.8,
        )
        self.play(
            FadeIn(old, shift=UP * 0.2),
            old_dot.animate.set_color(WARN).scale(1.9),
            run_time=0.8,
        )
        self.say("The old chunk is still there. Both get retrieved.",
                 run_time=0.55, hold=0.8)

        # try to delete it
        strike = Line(
            old.get_left() + RIGHT * 0.12, old.get_right() + LEFT * 0.12,
            stroke_color=BAD, stroke_width=3.0,
        )
        del_lbl = label("DELETE", size=0.24, color=BAD, weight="BOLD")
        del_lbl.next_to(old, DOWN, buff=0.28)
        self.play(Create(strike), FadeIn(del_lbl), run_time=0.6)

        # ...and the same sentence resurfaces from six other chunks
        dupes = [12, 29, 73, 98, 111, 134]
        dupe_dots = VGroup(*[cloud[i] for i in dupes])
        self.play(
            FadeOut(strike), FadeOut(del_lbl),
            old.animate.set_opacity(0.25),
            old_dot.animate.set_color(FAINT).scale(1 / 1.9),
            run_time=0.6,
        )
        self.play(
            LaggedStart(
                *[d.animate.set_color(WARN).scale(1.9) for d in dupe_dots],
                lag_ratio=0.12,
            ),
            run_time=1.2,
        )
        dup_lbl = label("same sentence, 6 other chunks", size=0.24, color=WARN)
        dup_lbl.next_to(store_frame, DOWN, buff=0.26)
        self.play(FadeIn(dup_lbl), run_time=0.4)

        self.say("There is no row to update — only copies to outvote.",
                 color=BAD, accent=BAD, run_time=0.6, hold=1.8)

        # =================================================== C. so you raise k
        self.play(
            FadeOut(pair), FadeOut(correction), FadeOut(dup_lbl),
            *[d.animate.set_color(VIOLET).scale(1 / 1.9) for d in dupe_dots],
            new_dot.animate.set_color(VIOLET).scale(1 / 1.9),
            old_dot.animate.set_color(VIOLET),
            run_time=0.8,
        )
        self.say("You can't fix precision. So you compensate with volume.",
                 run_time=0.55)

        kt = ValueTracker(3)
        kread = always_redraw(
            lambda: mono(f"top-k = {int(kt.get_value()):d}", size=0.42, color=ACCENT)
            .move_to([-3.55, 1.15, 0])
        )
        tokread = always_redraw(
            lambda: mono(
                f"{int(kt.get_value()) * 1240:,} tokens of context",
                size=0.34, color=DIM,
            ).move_to([-3.55, 0.45, 0])
        )
        big_ring = always_redraw(
            lambda: Circle(
                radius=float(
                    np.linalg.norm(
                        pts[nearest_k(pts, QUERY_POINT, int(kt.get_value()))[-1]][:2]
                        - QUERY_POINT[:2]
                    )
                ) + 0.1,
                arc_center=QUERY_POINT,
                stroke_color=ACCENT, stroke_width=2.0, fill_opacity=0.06,
                fill_color=ACCENT,
            )
        )
        self.add(big_ring, kread, tokread)
        self.play(kt.animate.set_value(40), run_time=3.0,
                  rate_func=rate_functions.ease_in_out_cubic)

        swept = nearest_k(pts, QUERY_POINT, 40)
        self.play(
            LaggedStart(
                *[cloud[i].animate.set_color(ACCENT) for i in swept],
                lag_ratio=0.012,
            ),
            run_time=1.0,
        )
        self.say("Forty chunks. To answer one question.",
                 color=WARN, accent=WARN, run_time=0.6, hold=1.4)

        self.play(
            FadeOut(ch), FadeOut(qdot), FadeOut(store_lbl), FadeOut(store_frame),
            FadeOut(big_ring), FadeOut(kread), FadeOut(tokread),
            FadeOut(cloud),
            run_time=0.9,
        )
        self.unsay(run_time=0.4)
        self.wait(0.2)
