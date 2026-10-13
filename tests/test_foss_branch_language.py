"""FOSS release gate: the shipped defaults match the branch's language (#216, #217).

An independent user installed the Italian branch and got an English stack: compose defaulted
SPACY_MODEL to en_core_web_sm and FAULTLINE_LANGUAGE to blank, and every branch shared the
image tag and container names, so a language install reused or duplicated the English one.
Each branch sets BRANCH_LANG below; every default that carries a language must agree with it.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest
import yaml

BRANCH_LANG = "en"   # main = en; set per language branch (es, it)

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
_SVC = _COMPOSE["services"]


def _default(v) -> str:
    m = re.fullmatch(r"\$\{[A-Z0-9_]+:-([^}]*)\}", str(v).strip())
    return m.group(1) if m else str(v)


def _env_example() -> dict:
    out = {}
    for line in (_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _model_lang(model: str) -> str:
    return model.split("_", 1)[0]


def test_compose_spacy_model_is_the_branch_language():
    model = _default(_SVC["faultline"]["build"]["args"]["SPACY_MODEL"])
    assert _model_lang(model) == BRANCH_LANG, model


def test_compose_install_language_is_the_branch_language():
    lang = _default(_SVC["faultline"]["environment"]["FAULTLINE_LANGUAGE"])
    assert lang == ("" if BRANCH_LANG == "en" else BRANCH_LANG), lang


def test_env_example_agrees():
    env = _env_example()
    assert _model_lang(env["SPACY_MODEL"]) == BRANCH_LANG, env["SPACY_MODEL"]
    assert env.get("FAULTLINE_LANGUAGE", "") == ("" if BRANCH_LANG == "en" else BRANCH_LANG)


def test_dockerfile_bakes_the_branch_language():
    args = re.findall(r"^ARG SPACY_MODEL=(\S+)", (_ROOT / "Dockerfile").read_text(), re.M)
    assert args and all(_model_lang(a) == BRANCH_LANG for a in args), args


def test_compose_and_env_example_name_the_same_model():
    env = _env_example()
    assert _default(_SVC["faultline"]["build"]["args"]["SPACY_MODEL"]) == env["SPACY_MODEL"]


def test_quickstart_defaults_to_the_branch_language(monkeypatch):
    sys.path.insert(0, str(_ROOT))
    try:
        qs = importlib.import_module("quickstart")
    finally:
        sys.path.remove(str(_ROOT))
    assert qs._DEFAULT_LANG == BRANCH_LANG
    monkeypatch.setenv("_FL_LANG", "")      # recorded, so the gate's own write is undone
    assert qs.language_env()["FAULTLINE_LANGUAGE"] == BRANCH_LANG
    # Enter at the language gate keeps this branch
    seen = {}
    monkeypatch.setattr(qs, "choose", lambda prompt, opts: seen.setdefault("first", opts[0][0]))
    monkeypatch.setattr(qs, "_current_branch", lambda: None)
    monkeypatch.delenv("_FL_LANG_SWITCHED", raising=False)
    qs.language_gate()
    assert seen["first"] == BRANCH_LANG


def test_stack_names_are_the_branch_own():
    prefix = "faultline" if BRANCH_LANG == "en" else f"faultline-{BRANCH_LANG}"
    for svc in ("faultline", "faultline-mcp"):
        assert _default(_SVC[svc]["image"]) == f"{prefix}:latest"
    for name, svc in _SVC.items():
        cn = svc["container_name"]
        assert cn.startswith("${FAULTLINE_PREFIX:-%s}" % prefix), (name, cn)
