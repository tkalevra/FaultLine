"""FOSS-006 pins: the install docs tell the truth about models and disk.

Reality (measured, fresh-install gauntlet): the GLiNER2 weights, the embedding model and
the spaCy model are baked into the image at BUILD (the Dockerfile RUNs from_pretrained;
the runtime starts HF_HUB_OFFLINE=1 and cannot download). The README claimed a
first-START download (~500 MB, 3-5 minutes) that never happens, and documented no disk
footprint at all (measured: ~16 GB fresh install, ~3 GB steady state after builder prune)."""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _readme():
    return (_ROOT / "README.md").read_text(encoding="utf-8")


def _requirements():
    return _readme().split("## Requirements", 1)[1].split("\n## ", 1)[0]


# ── the stale claim is gone ──────────────────────────────────────────────────

def test_no_first_start_model_download_claim():
    readme = _readme()
    assert not re.search(r"first start downloads", readme, re.I)
    assert "500 MB" not in readme
    assert not re.search(r"[Tt]akes 3[\u2013\u2014-]5 minutes", readme)


def test_readme_claim_matches_dockerfile_reality():
    """The claim may only come back if the Dockerfile stops baking the models."""
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "GLiNER2.from_pretrained" in dockerfile, "weights are baked at build"
    assert "HF_HUB_OFFLINE=1" in dockerfile, "the runtime cannot download anything"
    readme = _readme()
    assert "docker compose build" in readme, "the download happens at BUILD, named as such"
    assert re.search(r"baked", readme, re.I), "the baked-weights truth must be stated"
    assert "HF_HUB_OFFLINE" in readme, "the runtime-cannot-download fact must be stated"


# ── the disk footprint is documented ─────────────────────────────────────────

def test_requirements_carry_the_measured_disk_footprint():
    req = _requirements()
    assert "16 GB" in req, "the headline first build+boot number"
    assert "~2.9 GB" in req, "images"
    assert "~4.5 GB" in req, "build cache"
    assert "builder prune" in req, "the steady-state command"
    assert "~3 GB steady" in req, "the steady-state number after the prune"
