# FaultLine — promo film

A ~2:45 animated explainer that argues the case for FaultLine: what a vector /
RAG pipeline actually is, the three places it breaks, and what FaultLine does
instead.

Rendered with [Manim](https://www.manim.community/) (real motion graphics, not
slide transitions), narrated with [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M),
assembled with ffmpeg.

Outputs, all written next to `--out`:

- `faultline-promo.mp4` — 1920×1080, 60 fps, H.264 + AAC, ~2:46
- `faultline-promo-silent.mp4` — no audio, for recording your own read
- `faultline-promo.srt` — subtitles, generated from the same cue timings as the
  voiceover (worth having: most social platforms autoplay muted)

---

## Structure

| file | what it covers |
|---|---|
| `theme.py` | palette, typography, lower-third system, shared parts, `FilmScene` base |
| `s1_pipeline.py` | **01** what a RAG pipeline is: documents → chunks → vectors → top-k → LLM |
| `s2_failure.py` | **02** where RAG breaks by accident: nearest ≠ correct; nothing can be removed |
| `s3_context.py` | **03** why *more* context makes the answer worse |
| `s4_poison.py` | **04** RAG poisoning — the same failure, on purpose |
| `s5_gate.py` | **05** the logo's turnstile becomes the validation gate; graph; correction |
| `s6_walk.py` | **06** anchor + deterministic walk, the Class-C note, end card |
| `narration.py` | the spoken script, keyed to on-screen cue points |
| `build.py` | the whole pipeline: cues → voiceover → render → mux |

Scene boundaries are **match cuts**: each scene rebuilds the previous scene's
closing geometry (same seed, same coordinates) so the joins read as one
continuous shot rather than six clips.

---

## Building

Needs Python 3.13 (Manim does not yet build cleanly on 3.14), cairo/pango
headers, and a full ffmpeg with libx264.

```bash
# system deps (Fedora)
sudo dnf install cairo-devel pango-devel python3.13-devel gcc
sudo dnf swap ffmpeg-free ffmpeg --allowerasing     # ffmpeg-free has no libx264

python3.13 -m venv .venv && . .venv/bin/activate
pip install manim

# narration comes from a kokoro-fastapi service (one already runs on the NAS)
python build.py --all --quality h --kokoro-url http://192.168.40.10:8880
```

To run the voice service yourself:

```bash
docker run -d --name kokoro-fastapi -p 8880:8880 \
  ghcr.io/remsky/kokoro-fastapi-cpu:latest
```

Pick a different narrator with `--kokoro-voice` (`GET /v1/audio/voices` lists
all 67; `am_michael` is the default, `bm_george` and `am_onyx` are the strongest
alternates). `--speed` defaults to 1.15 because Kokoro reads slower than the
scene timings were originally cut for.

`--quality l` renders a 480p15 draft in a couple of minutes — use it while
iterating. Individual stages: `--cues`, `--vo`, `--render`, `--mux`.

### How the narration stays in sync

Hand-timing a voiceover against animation is where these things usually rot.
Instead, `say()` writes a cue point — scene, time on the scene's clock, and the
exact caption text — every time the lower third changes. `build.py` dry-runs the
scenes to collect those cues, looks each one up in `narration.py`, and places the
synthesized line at that timestamp.

Two consequences worth knowing:

- **The mapping cannot silently drift.** If you reword a caption without
  rewording its narration key, the build fails and names the offending cue.
- **Cue times are rescaled.** Manim rounds every animation up to a whole frame,
  so a scene's real duration runs slightly longer than its dry-run clock. Each
  cue is scaled by `real_duration / dry_run_duration`, which keeps the voice from
  drifting late across a scene.

`build.py` also reports any line whose audio runs past the beat it belongs to,
so overlaps are caught rather than shipped.

### Changing the voice or the words

Edit `narration.py` — keys must match the `say()` strings exactly.

There is **no music bed** — nothing suitably licensed was available offline. The
mix leaves plenty of headroom (peaks at ~0.89) if you want to lay one under it.

### Why Kokoro, and a licensing warning

This film is an advertisement, so the narration has to be **commercially
licensable**. That rules out more of the field than you would expect:

| voice | verdict |
|---|---|
| **Kokoro-82M** | ✅ MIT code, **Apache-2.0 weights**. Runs on CPU. What this uses. |
| Chatterbox (Resemble AI) | ✅ MIT, richer output — but GPU-oriented, slow on CPU |
| Piper voice `en_US-ryan-high` | ❌ trained on RyanSpeech, **CC BY-NC-SA 4.0 — non-commercial** |
| XTTS-v2 | ❌ CPML, non-commercial (and Coqui folded, so no licence is purchasable) |
| F5-TTS | ❌ MIT code but the released checkpoints are CC-BY-NC |
| Fish Speech / OpenAudio, Higgs Audio | ❌ commercial use requires a paid licence |

The first cut of this film was narrated with the Piper `ryan` voice before that
dataset licence was checked. It is non-commercial, so it was replaced. **Piper
voices carry per-voice licences** — the engine being permissive tells you
nothing about the voice. Always read the voice's `MODEL_CARD`.

---

## Claims and sources

Everything asserted on screen is either mechanical (visible in this repo) or
attributed to published work.

| on screen | source |
|---|---|
| accuracy is highest at the edges of a long context and sags in the middle | Liu et al., *Lost in the Middle: How Language Models Use Long Contexts*, TACL 2023 — [arXiv:2307.03172](https://arxiv.org/abs/2307.03172) |
| 5 injected texts, ~90% attack success against a corpus of millions | Zou, Geng, Wang & Jia, *PoisonedRAG: Knowledge Corruption Attacks to RAG*, USENIX Security 2025 — [arXiv:2402.07867](https://arxiv.org/abs/2402.07867) |
| FaultLine's write gate, graph walk, supersede/archive, Class-C vector tier | [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) — "Why this isn't RAG (mechanically)" |

The accuracy curve in scene 3 is drawn as a **qualitative shape**, not plotted
data: the y-axis is labelled low/high with no numbers, because inventing a
precise curve and attributing it would misrepresent the paper.

Scene 6 deliberately keeps the point made in [`HONESTY.md`](../../HONESTY.md) —
FaultLine does still run a vector index, as a short-term Class-C scratchpad that
is never the source of truth. Leaving that out would have made a cleaner ad and
a false one.

The `DevBox` / `10.0.1.x` examples are illustrative, and match the ones already
used in the project README.
