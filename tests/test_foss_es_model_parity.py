"""FOSS-002 parity pin: the wizard's es spaCy model MUST equal the branch default.

Four sources state the es branch's spaCy model: quickstart.py's language table (what a
wizard install writes into .env), .env.example, the Dockerfile ARG (what the image bakes)
and the docker-compose default. This branch's MEASURED default is es_core_news_md —
.env.example's own comment block documents sm's failure modes (sentence-initial 1sg
preterite verbs mis-tag as PROPN, possessive DETs mis-parse as flat names). The wizard
table disagreed (sm), so a wizard install baked and ran the weaker model while every
hand-configured path used md. This pin reads all four files and fails on ANY mismatch —
a single source of truth by construction, not by comment.

The it entry is NOT part of this pin's parity claim beyond its own recorded state:
it_core_news_sm IS it's internally consistent default (FOSS-002 branch matrix: it N/A).
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# The owner decision recorded in the FOSS-002 fix: md is the measured es default
# (.env.example's comment block is the measurement record — sm's specific failure
# modes are documented there). Flipping all four files to sm consistently would need
# a re-measurement and is NOT what this pin allows silently.
_MEASURED_ES_DEFAULT = "es_core_news_md"


def _quickstart_language_table() -> dict:
    """Import quickstart (stdlib-only wizard) and return its _LANGUAGES table."""
    sys.path.insert(0, str(_ROOT))
    try:
        mod = importlib.import_module("quickstart")
    finally:
        sys.path.remove(str(_ROOT))
    return mod._LANGUAGES


def _env_example_model() -> str:
    for line in (_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^SPACY_MODEL=(\S+)", line)
        if m:
            return m.group(1).strip()
    raise AssertionError("SPACY_MODEL= line not found in .env.example")


def _dockerfile_arg_models() -> list[str]:
    """EVERY ARG SPACY_MODEL in the Dockerfile (builder + runtime stages must agree)."""
    out = re.findall(
        r"^ARG SPACY_MODEL=(\S+)",
        (_ROOT / "Dockerfile").read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if not out:
        raise AssertionError("ARG SPACY_MODEL not found in Dockerfile")
    return [v.strip() for v in out]


def _compose_default_model() -> str:
    """The compose-level default inside the SPACY_MODEL substitution for the backend."""
    text = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    m = re.search(r"SPACY_MODEL:\s*\$\{SPACY_MODEL:-(\S+)\}", text)
    if not m:
        raise AssertionError("SPACY_MODEL substitution default not found in docker-compose.yml")
    return m.group(1).strip().strip("\"")


def test_es_wizard_model_equals_every_branch_default():
    """The four files must state ONE es model — the wizard may not silently disagree."""
    wizard = _quickstart_language_table()["es"]["spacy_model"]
    envex = _env_example_model()
    dockerfile = _dockerfile_arg_models()
    compose = _compose_default_model()
    assert wizard == envex == compose and set(dockerfile) == {wizard}, (
        f"es SPACY_MODEL parity broken (FOSS-002): wizard={wizard!r} "
        f".env.example={envex!r} Dockerfile={dockerfile!r} compose={compose!r} — "
        "all four must carry the same model")


def test_es_default_is_the_measured_md_not_sm():
    """The parity target is the MEASURED md default, not whatever the files happen to say.

    Guards the FOSS-002 owner decision: harmonizing all four files DOWN to sm (the weaker
    model whose failure modes .env.example documents) would pass the equality pin above
    while quietly degrading every fresh es install."""
    assert _quickstart_language_table()["es"]["spacy_model"] == _MEASURED_ES_DEFAULT
    assert _env_example_model() == _MEASURED_ES_DEFAULT
    assert set(_dockerfile_arg_models()) == {_MEASURED_ES_DEFAULT}
    assert _compose_default_model() == _MEASURED_ES_DEFAULT


def test_it_wizard_entry_stays_its_own_consistent_default():
    """FOSS-002 is es-only: it_core_news_sm IS it's consistent branch default — untouched."""
    assert _quickstart_language_table()["it"]["spacy_model"] == "it_core_news_sm"
