"""Critic pins for the download-bloat trim (gliner2→peft→unpinned torch CUDA resolve).

Measured on the pre-fix Dockerfile: `pip install -e . ".[api]"` resolved 108 packages,
~2.3 GB of wheels — torch 2.14.1 (CUDA) plus nvidia-cudnn-cu13, nvidia-cublas, nvidia-nccl,
nvidia-nvshmem, cuda-toolkit, triton — for a CPU-only workload. Across the main/es/it
images and rebuilds this is what users report as "downloading 100 GB". Each pin is RED on
that head and guards one seam of the trim.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

CPU_INDEX = "https://download.pytorch.org/whl/cpu"


def _dockerfile_lines() -> list[str]:
    return (_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()


def _pip_run_lines() -> list[tuple[int, str]]:
    """(line_no, text) of every RUN line that invokes pip."""
    out = []
    for i, line in enumerate(_dockerfile_lines()):
        if line.startswith("RUN") and "pip install" in line:
            out.append((i, line))
    return out


def test_cpu_torch_is_preinstalled_before_the_project_install():
    """THE core pin. gliner2==1.3.2 → peft → torch UNPINNED: the default Linux resolve is the
    CUDA build (~2 GB of nvidia-*/triton wheels, ~7 GB installed). The pinned +cpu wheel,
    installed FIRST from the CPU index, satisfies peft so the CUDA stack never enters the
    resolve. If this pin is RED, fresh builds are quietly fat again."""
    runs = _pip_run_lines()
    cpu = [(i, l) for i, l in runs
           if re.search(r"torch==[0-9.]+\+cpu", l) and CPU_INDEX in l]
    assert cpu, "no pinned +cpu torch pre-install from the PyTorch CPU index in the Dockerfile"
    proj = [(i, l) for i, l in runs if "-e ." in l or ".[api]" in l]
    assert proj, "project install line not found"
    assert cpu[0][0] < proj[0][0], (
        "the +cpu torch pre-install must come BEFORE the project install, or peft's "
        "unpinned torch constraint resolves the CUDA build")


def test_no_non_cpu_package_index_anywhere_in_the_build():
    """The only --index-url in the build is the PyTorch CPU index. Any other index (or an
    unpinned torch without one) is how the CUDA stack sneaks back in."""
    for i, line in enumerate(_dockerfile_lines()):
        if "--index-url" in line or "--extra-index-url" in line:
            assert CPU_INDEX in line, f"Dockerfile line {i+1}: unexpected package index: {line!r}"


def test_every_pinned_torch_is_cpu():
    """A torch pin WITHOUT +cpu on the default index is the CUDA build on Linux."""
    for i, line in _pip_run_lines():
        for m in re.finditer(r"torch[=<>~!]+[^ ]+", line):
            spec = m.group(0)
            assert "+cpu" in spec, f"Dockerfile line {i+1}: non-CPU torch spec {spec!r}"


def test_gliner2_bake_is_parametrized():
    """The GLiNER2 bake RUN line must read the model id from the environment (ARG GLINER_MODEL),
    never a bare literal — a literal here can drift from main.py's default and then
    from_pretrained() fails closed under HF_HUB_OFFLINE=1."""
    bake = [l for l in _dockerfile_lines() if "GLiNER2.from_pretrained" in l]
    assert bake, "GLiNER2 bake step not found in Dockerfile"
    assert any("os.environ" in l for l in bake), (
        "GLiNER2 bake line hardcodes the model id instead of reading GLINER_MODEL")


def test_runtime_gliner2_load_reads_env_not_a_literal():
    """The runtime load site (src/api/main.py) must resolve the model via GLINER_MODEL so a
    baked variant is what loads; a bare literal silently re-couples code to one weights repo."""
    lines = (_ROOT / "src/api/main.py").read_text(encoding="utf-8").splitlines()
    hits = [i for i, l in enumerate(lines) if "GLiNER2.from_pretrained" in l]
    assert hits, "GLiNER2 runtime load site not found in src/api/main.py"
    for i in hits:
        window = "\n".join(lines[max(0, i - 3):i + 1])
        assert "GLINER_MODEL" in window, (
            f"src/api/main.py:{i+1}: GLiNER2 load does not read GLINER_MODEL — "
            "hardcoded weights repo")


def test_no_second_gliner_model_download_path():
    """src/gli_ner/extractor.py used to `GLiNER.from_pretrained("urchade/gliner_medium-v2.1")`
    — a SECOND ~600 MB model, from a package (gliner) that is not even a dependency, with
    zero callers. On a non-container/source install (no HF_HUB_OFFLINE) that is a silent
    600 MB download. The module is deleted; nothing may reference it or that repo again."""
    src = _ROOT / "src"
    assert not (src / "gli_ner").exists(), "dead src/gli_ner module is back — delete it again"
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        assert "urchade" not in text, f"{py} references a second GLiNER weights repo (urchade/…)"
        assert "from gliner import" not in text and "import gliner\n" not in text, (
            f"{py} imports the un-declared 'gliner' package (not in pyproject)")


def test_offline_env_vars_present():
    """Models are baked; runtime must not phone home (HF update checks or downloads)."""
    df = _dockerfile_lines()
    assert any(l.startswith("ENV HF_HUB_OFFLINE=1") for l in df), "HF_HUB_OFFLINE=1 lost"
    assert any(l.startswith("ENV TRANSFORMERS_OFFLINE=1") for l in df), (
        "TRANSFORMERS_OFFLINE=1 lost")
