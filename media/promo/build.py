#!/usr/bin/env python3
"""Build the FaultLine promo film end to end.

    python build.py --all                 # cues, voiceover, render, mux
    python build.py --quality l --all     # fast draft pass
    python build.py --mux                 # re-mux without re-rendering

Stages
  cues    dry-run every scene to recover the exact time of each narration beat
  vo      synthesise one WAV per beat with Piper
  render  render the scenes to mp4
  mux     concatenate the scenes, lay the voiceover on the timeline, mux

Cue times come off the dry-run clock; the rendered files differ from it very
slightly because manim rounds each animation up to a whole frame. Rather than
ignore that, every cue is rescaled by the ratio of the real scene duration to
the dry-run duration, so the voiceover cannot drift over the length of a scene.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent

SCENES = [
    ("s1_pipeline.py", "RagPipeline"),
    ("s2_failure.py", "RagFailure"),
    ("s3_context.py", "ContextCollapse"),
    ("s4_poison.py", "Poisoning"),
    ("s5_gate.py", "TheGate"),
    ("s6_walk.py", "TheWalk"),
]

QUALITY_DIR = {"l": "480p15", "m": "720p30", "h": "1080p60", "k": "2160p60"}


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


# --------------------------------------------------------------------- cues
def stage_cues(cfg) -> list[tuple[str, str, float, str]]:
    cue_file = cfg.work / "cues.tsv"
    cue_file.write_text("")
    env = dict(os.environ, PYTHONPATH=str(HERE), FL_CUES=str(cue_file))
    for fname, scene in SCENES:
        sh(
            [cfg.manim, "-ql", "--dry_run", "--disable_caching",
             "--media_dir", str(cfg.media), str(HERE / fname), scene],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    rows = []
    for line in cue_file.read_text(encoding="utf-8").splitlines():
        kind, scene, t, tag = line.split("\t", 3)
        rows.append((kind, scene, float(t), tag))
    print(f"[cues]   {sum(1 for r in rows if r[0] == 'CUE')} beats "
          f"across {len(SCENES)} scenes")
    return rows


# ----------------------------------------------------------------------- vo
def stage_vo(cfg, rows):
    from narration import SCRIPT

    cues = [r for r in rows if r[0] == "CUE"]
    missing = [(s, tag) for _, s, _, tag in cues if (s, tag) not in SCRIPT]
    if missing:
        raise SystemExit(
            "narration.py has no line for these cues:\n  "
            + "\n  ".join(f"{s} :: {t}" for s, t in missing)
        )
    extra = set(SCRIPT) - {(s, tag) for _, s, _, tag in cues}
    if extra:
        raise SystemExit(
            "narration.py has lines with no matching cue:\n  "
            + "\n  ".join(f"{s} :: {t}" for s, t in sorted(extra))
        )

    vo_dir = cfg.work / "vo"
    vo_dir.mkdir(parents=True, exist_ok=True)
    for i, (_, scene, _, tag) in enumerate(cues):
        entry = SCRIPT[(scene, tag)]
        text = entry[0] if isinstance(entry, tuple) else entry
        txt = vo_dir / f"{i:03d}.txt"
        txt.write_text(text, encoding="utf-8")
        wav = vo_dir / f"{i:03d}.wav"
        if cfg.tts == "kokoro":
            kokoro_say(cfg, text, wav)
        else:
            sh(
                [cfg.piper, "-m", str(cfg.voice), "-i", str(txt),
                 "-f", str(wav),
                 "--length-scale", str(cfg.length_scale),
                 "--sentence-silence", "0.22"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    print(f"[vo]     {len(cues)} lines synthesised via {cfg.tts}")


def kokoro_say(cfg, text, out: Path):
    """Synthesise one line through a kokoro-fastapi service.

    Kokoro (MIT code, Apache-2.0 weights) is the default because it is
    commercially licensable — several popular alternatives, including the
    Piper `ryan` voice this film originally used, are trained on
    non-commercial datasets and cannot be used in an advertisement.
    """
    req = urllib.request.Request(
        cfg.kokoro_url.rstrip("/") + "/v1/audio/speech",
        data=json.dumps({
            "model": "kokoro",
            "input": text,
            "voice": cfg.kokoro_voice,
            "response_format": "wav",
            "speed": cfg.speed,
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        out.write_bytes(r.read())


# ------------------------------------------------------------------- render
def stage_render(cfg):
    env = dict(os.environ, PYTHONPATH=str(HERE))
    for fname, scene in SCENES:
        print(f"[render] {scene} …", flush=True)
        sh(
            [cfg.manim, f"-q{cfg.quality}", "--disable_caching",
             "--media_dir", str(cfg.media), str(HERE / fname), scene],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def scene_path(cfg, fname, scene) -> Path:
    return (cfg.media / "videos" / Path(fname).stem
            / QUALITY_DIR[cfg.quality] / f"{scene}.mp4")


# ---------------------------------------------------------------------- mux
def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() == 2:
            data = data.reshape(-1, 2).mean(axis=1)
    return data, rate


def stage_mux(cfg, rows):
    from narration import SCRIPT

    parts = [scene_path(cfg, f, s) for f, s in SCENES]
    for p in parts:
        if not p.exists():
            raise SystemExit(f"missing rendered scene: {p}\nrun with --render")

    real = {s: probe_duration(p) for (_, s), p in zip(SCENES, parts)}
    dry = {scene: t for kind, scene, t, _ in rows if kind == "END"}

    # absolute start of each scene on the concatenated timeline
    starts, acc = {}, 0.0
    for _, s in SCENES:
        starts[s] = acc
        acc += real[s]
    total = acc
    print(f"[mux]    total runtime {int(total // 60)}:{total % 60:05.2f}")

    # ---- lay the voiceover down on a single float buffer
    cues = [r for r in rows if r[0] == "CUE"]
    rate = 22050
    track = np.zeros(int((total + 2.0) * rate), dtype=np.float32)
    vo_dir = cfg.work / "vo"
    placed = []
    for i, (_, scene, t, tag) in enumerate(cues):
        wav = vo_dir / f"{i:03d}.wav"
        data, rate = read_wav(wav)
        if track.shape[0] < int((total + 2.0) * rate):
            track = np.zeros(int((total + 2.0) * rate), dtype=np.float32)
        entry = SCRIPT[(scene, tag)]
        delay = entry[1] if isinstance(entry, tuple) else 0.0
        # rescale the dry-run cue onto the real scene clock
        scaled = t * (real[scene] / dry[scene])
        at = starts[scene] + scaled + delay
        i0 = int(at * rate)
        track[i0:i0 + data.shape[0]] += data
        placed.append((at, at + data.shape[0] / rate, scene, tag))

    # ---- report any line that runs past the beat it belongs to
    over = []
    for j, (a, b, scene, tag) in enumerate(placed):
        nxt = placed[j + 1][0] if j + 1 < len(placed) else total
        if b > nxt + 0.05:
            over.append((scene, tag, b - nxt))
    if over:
        print("[mux]    lines overrunning their beat:")
        for scene, tag, by in over:
            print(f"           +{by:4.2f}s  {scene} :: {tag[:52]}")

    # ---- subtitles, straight off the same timings
    def ts(sec):
        h, rem = divmod(max(sec, 0.0), 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")

    srt = []
    for j, (a, b, scene, tag) in enumerate(placed, start=1):
        entry = SCRIPT[(scene, tag)]
        text = entry[0] if isinstance(entry, tuple) else entry
        srt.append(f"{j}\n{ts(a)} --> {ts(b)}\n{text}\n")
    cfg.out.parent.mkdir(parents=True, exist_ok=True)
    srt_path = cfg.out.with_suffix(".srt")
    srt_path.write_text("\n".join(srt), encoding="utf-8")
    print(f"[mux]    wrote {srt_path}")

    peak = float(np.max(np.abs(track))) or 1.0
    track = track / peak * 0.89
    fade = int(1.2 * rate)
    track[-fade:] *= np.linspace(1.0, 0.0, fade)

    vo_master = cfg.work / "vo_master.wav"
    with wave.open(str(vo_master), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((track * 32767).astype(np.int16).tobytes())

    # ---- concatenate the scenes, then mux the voiceover on
    listing = cfg.work / "concat.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    silent = cfg.work / "silent.mp4"
    sh(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", str(listing), "-c", "copy", str(silent)])

    cfg.out.parent.mkdir(parents=True, exist_ok=True)
    sh([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(silent), "-i", str(vo_master),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-shortest", str(cfg.out),
    ])
    size = cfg.out.stat().st_size / 1e6
    print(f"[mux]    wrote {cfg.out}  ({size:.1f} MB)")

    # a silent master, for anyone who wants to record their own read
    sh([
        "ffmpeg", "-y", "-v", "error", "-i", str(silent),
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
        str(cfg.out.with_name(cfg.out.stem + "-silent.mp4")),
    ])
    print(f"[mux]    wrote {cfg.out.with_name(cfg.out.stem + '-silent.mp4')}")


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quality", default="h", choices=list(QUALITY_DIR))
    ap.add_argument("--work", default=str(HERE / "build"))
    ap.add_argument("--out", default=str(HERE / "out" / "faultline-promo.mp4"))
    ap.add_argument("--manim", default=shutil.which("manim") or "manim")
    ap.add_argument("--piper", default=shutil.which("piper") or "piper")
    ap.add_argument("--voice", required=False, default=os.environ.get("FL_VOICE", ""))
    ap.add_argument("--length-scale", type=float, default=1.0)
    ap.add_argument("--tts", default="kokoro", choices=("kokoro", "piper"),
                    help="kokoro is the default: Apache-2.0 weights, so the "
                         "audio is commercially usable")
    ap.add_argument("--kokoro-url",
                    default=os.environ.get("FL_KOKORO_URL",
                                           "http://192.168.40.10:8880"))
    ap.add_argument("--kokoro-voice",
                    default=os.environ.get("FL_KOKORO_VOICE", "am_michael"))
    ap.add_argument("--speed", type=float, default=1.15,
                    help="Kokoro reads slower than Piper; ~1.15 restores the "
                         "pacing the scene timings were cut for")
    for st in ("cues", "vo", "render", "mux"):
        ap.add_argument(f"--{st}", action="store_true")
    ap.add_argument("--all", action="store_true")
    cfg = ap.parse_args()

    cfg.work = Path(cfg.work)
    cfg.out = Path(cfg.out)
    cfg.media = cfg.work / "media"
    cfg.voice = Path(cfg.voice) if cfg.voice else None
    cfg.work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(HERE))

    do = {s: (getattr(cfg, s) or cfg.all) for s in ("cues", "vo", "render", "mux")}
    if not any(do.values()):
        ap.error("pick at least one stage, or --all")

    rows = None
    if do["cues"]:
        rows = stage_cues(cfg)
    if rows is None and (do["vo"] or do["mux"]):
        cue_file = cfg.work / "cues.tsv"
        if not cue_file.exists():
            raise SystemExit("no cues.tsv — run with --cues first")
        rows = [
            (k, s, float(t), tag)
            for k, s, t, tag in (
                ln.split("\t", 3)
                for ln in cue_file.read_text(encoding="utf-8").splitlines()
            )
        ]
    if do["vo"]:
        if cfg.tts == "piper" and (not cfg.voice or not cfg.voice.exists()):
            raise SystemExit("--voice must point at a Piper .onnx voice model")
        stage_vo(cfg, rows)
    if do["render"]:
        stage_render(cfg)
    if do["mux"]:
        stage_mux(cfg, rows)


if __name__ == "__main__":
    main()
