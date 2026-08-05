"""Scene 1 — What a RAG / vector pipeline actually is.

Documents -> chunks -> embeddings -> a similarity index, then the query path:
question -> vector -> top-k nearest -> LLM. The closing frame leaves the cloud
exactly where scene 2 picks it up, so the cut reads as one continuous shot.
"""

from manim import *
import numpy as np

from theme import *

STAGE_Y = 0.55
CLOUD_CENTER = np.array([3.35, STAGE_Y, 0.0])
QUERY_POINT = np.array([3.05, 0.25, 0.0])
LEFT_X = -3.6


class RagPipeline(FilmScene):
    def construct(self):
        ch = chapter("01", "WHAT A RAG PIPELINE IS")
        wm = watermark()
        self.play(FadeIn(ch, shift=RIGHT * 0.2), FadeIn(wm), run_time=0.7)

        # ------------------------------------------------ documents come in
        docs = VGroup(*[doc_glyph(w=0.72, h=0.94) for _ in range(3)])
        docs.arrange(RIGHT, buff=0.2).move_to([-5.25, STAGE_Y, 0])

        self.play(
            LaggedStart(*[FadeIn(d, shift=UP * 0.35) for d in docs], lag_ratio=0.18),
            run_time=1.2,
        )
        self.say("Your documents go in.", run_time=0.5)

        # ------------------------------------------------------- chunking
        splitter = box("split", w=1.5, h=1.15, stroke=FAINT, color=DIM, size=0.26)
        splitter.move_to([-2.55, STAGE_Y, 0])

        a1 = flow_arrow(docs.get_right(), splitter.get_left())
        self.play(Create(splitter[0]), FadeIn(splitter[1]), GrowArrow(a1), run_time=0.7)

        chunks = VGroup(*[chunk_sq() for _ in range(28)])
        chunks.arrange_in_grid(rows=4, cols=7, buff=0.14)
        chunks.move_to([0.75, STAGE_Y, 0])
        a1b = flow_arrow(splitter.get_right(), chunks.get_left())

        self.play(GrowArrow(a1b), run_time=0.3)
        self.play(
            LaggedStart(*[FadeIn(c, scale=0.4) for c in chunks], lag_ratio=0.035),
            run_time=1.6,
        )
        self.say("Chopped into fixed-size chunks.", run_time=0.5, hold=0.5)

        # ------------------------------------------------------ embedding
        self.play(
            FadeOut(docs, shift=LEFT * 0.4),
            FadeOut(a1), FadeOut(a1b), FadeOut(splitter),
            chunks.animate.move_to([-5.05, STAGE_Y, 0]).scale(0.9),
            run_time=0.9,
        )

        embedder = box("embedding model", w=2.4, h=1.3, stroke=VIOLET, size=0.26)
        embedder.move_to([-1.95, STAGE_Y, 0])
        a2 = flow_arrow(chunks.get_right(), embedder.get_left())

        self.play(Create(embedder[0]), FadeIn(embedder[1]), GrowArrow(a2), run_time=0.7)

        # ------------------------------------------------- the vector store
        pts = cloud_points(n=150, seed=7, center=CLOUD_CENTER)
        cloud = make_cloud(pts)

        store_frame = RoundedRectangle(
            width=6.0, height=4.1, corner_radius=0.14,
            stroke_color=HAIR, stroke_width=1.6, fill_opacity=0,
        ).move_to(CLOUD_CENTER)
        store_lbl = label("VECTOR STORE", size=0.23, color=VIOLET, weight="BOLD")
        store_lbl.next_to(store_frame, UP, buff=0.18).align_to(store_frame, LEFT)

        self.play(Create(store_frame), FadeIn(store_lbl), run_time=0.7)

        # each chunk passes through the embedder and lands as a coordinate
        order = np.random.default_rng(3).permutation(len(chunks))
        anims = []
        for i, ci in enumerate(order):
            src = chunks[int(ci)]
            dst = cloud[i]
            anims.append(
                Succession(
                    src.animate(run_time=0.4, rate_func=rate_functions.ease_in_sine)
                    .move_to(embedder.get_center()).scale(0.25).set_opacity(0.0),
                    Transform(src, dst, run_time=0.5,
                              rate_func=rate_functions.ease_out_cubic),
                )
            )
        self.say("Each one becomes a vector — a point in space.", run_time=0.5)
        self.play(LaggedStart(*anims, lag_ratio=0.05), run_time=2.6)

        self.play(
            LaggedStart(
                *[FadeIn(d, scale=0.5) for d in cloud[len(chunks):]],
                lag_ratio=0.006,
            ),
            run_time=1.4,
        )
        self.add(cloud)
        self.remove(chunks)

        count = label("1,240,000 chunks", size=0.22, color=DIM)
        count.next_to(store_frame, UP, buff=0.18).align_to(store_frame, RIGHT)

        self.play(FadeOut(embedder), FadeOut(a2), FadeIn(count), run_time=0.6)
        self.say("Text becomes coordinates. Meaning becomes distance.", run_time=0.6,
                 hold=1.4)

        # name the thing, plainly — the rest of the film argues about it
        named = VGroup(
            title("RAG", size=0.72, color=ACCENT),
            body("retrieval-augmented generation", size=0.36, color=DIM),
        ).arrange(DOWN, buff=0.24)
        named.move_to([-3.6, 0.75, 0])
        self.play(FadeIn(named[0], shift=UP * 0.15), run_time=0.5)
        self.play(FadeIn(named[1]), run_time=0.4)
        self.say("This is RAG — retrieval-augmented generation.", run_time=0.55,
                 hold=1.5)
        self.play(FadeOut(named), run_time=0.5)

        # ------------------------------------------------------ query path
        q_prompt = mono('"What is DevBox\'s IP?"', size=0.36, color=INK)
        q_prompt.move_to([LEFT_X, 1.55, 0])

        self.play(AddTextLetterByLetter(q_prompt, run_time=1.3))
        self.wait(0.3)

        qdot = Dot(q_prompt.get_center(), radius=0.075, color=ACCENT)
        self.play(FadeOut(q_prompt), FadeIn(qdot, scale=2.0), run_time=0.45)
        qlbl = label("query vector", size=0.22, color=ACCENT)
        qlbl.next_to(qdot, DOWN, buff=0.25)
        self.play(FadeIn(qlbl), run_time=0.3)
        self.say("Your question is embedded the same way.", run_time=0.5)

        self.play(
            qdot.animate.move_to(QUERY_POINT),
            FadeOut(qlbl),
            run_time=1.0,
            rate_func=rate_functions.ease_in_out_cubic,
        )

        # similarity search: grow a radius until it has swept up k neighbours
        k = 5
        idx = nearest_k(pts, QUERY_POINT, k)
        rad = float(np.linalg.norm(pts[idx[-1]][:2] - QUERY_POINT[:2])) + 0.09

        ring = Circle(
            radius=0.05, arc_center=QUERY_POINT,
            stroke_color=ACCENT, stroke_width=2.0, fill_opacity=0.0,
        )
        self.add(ring)
        self.play(
            ring.animate.scale(rad / 0.05),
            run_time=1.3,
            rate_func=rate_functions.ease_out_cubic,
        )

        hits = VGroup(*[cloud[i] for i in idx])
        self.play(
            *[h.animate.set_color(ACCENT).scale(1.9) for h in hits],
            Flash(qdot, color=ACCENT, line_length=0.18, num_lines=14, flash_radius=0.3),
            run_time=0.65,
        )

        topk = label("top-k nearest neighbours", size=0.24, color=ACCENT)
        topk.next_to(store_frame, DOWN, buff=0.26)
        self.play(FadeIn(topk), run_time=0.4)
        self.say("It returns whatever sits closest.", run_time=0.5, hold=0.6)

        # ----------------------------------------------------- into the LLM
        llm = box("LLM", w=1.9, h=1.15, stroke=INK, color=INK, size=0.32, weight="BOLD")
        llm.move_to([LEFT_X, STAGE_Y + 0.2, 0])

        self.play(FadeOut(topk), Create(llm[0]), FadeIn(llm[1]), run_time=0.6)

        travellers = VGroup(*[h.copy() for h in hits])
        self.add(travellers)
        self.play(
            LaggedStart(
                *[
                    t.animate.move_to(llm.get_center()).scale(0.4).set_opacity(0.0)
                    for t in travellers
                ],
                lag_ratio=0.12,
            ),
            run_time=1.3,
            rate_func=rate_functions.ease_in_out_sine,
        )
        self.remove(travellers)

        answer = mono("DevBox is at 10.0.1.5.", size=0.32, color=INK)
        answer.next_to(llm, DOWN, buff=1.05)
        self.play(
            Flash(llm, color=INK, line_length=0.2, num_lines=16, flash_radius=0.95),
            FadeIn(answer, shift=DOWN * 0.15),
            run_time=0.85,
        )
        self.wait(0.5)

        # ------------------------------------------------------- the point
        self.say("Nothing here was verified. It was ranked.",
                 color=WARN, accent=WARN, run_time=0.6, hold=1.7)

        # leave the cloud exactly where scene 2 picks it up
        self.play(
            FadeOut(answer), FadeOut(llm), FadeOut(ch), FadeOut(qdot),
            FadeOut(ring), FadeOut(count),
            *[h.animate.set_color(VIOLET).scale(1 / 1.9) for h in hits],
            run_time=0.8,
        )
        self.unsay(run_time=0.4)
        self.wait(0.25)
