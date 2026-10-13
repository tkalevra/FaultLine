"""#217 pins: quickstart detects a FaultLine stack from another checkout and asks.

A user installed a language branch in a second directory and got an extra Postgres: compose
names a project after its directory, so the second checkout silently built a second
Postgres/Redis set. The wizard now detects stacks and data volumes that belong to another
compose project and asks reuse / replace / side-by-side. The image tag and the
container/volume prefix stay env-overridable.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def qs(monkeypatch, tmp_path):
    sys.path.insert(0, str(_ROOT))
    try:
        mod = importlib.import_module("quickstart")
    finally:
        sys.path.remove(str(_ROOT))
    monkeypatch.setattr(mod, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mod, "HERE", str(tmp_path / "FaultLine-es"))
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    monkeypatch.setattr(mod, "_port_open", lambda *a, **k: False)
    return mod


def _fake_docker(monkeypatch, qs, ps="", volumes=""):
    calls = []

    def _run(argv, **k):
        calls.append(argv)
        out = ps if argv[1] == "ps" else volumes if argv[1] == "volume" else ""
        return types.SimpleNamespace(returncode=0, stdout=out)
    monkeypatch.setattr(qs.subprocess, "run", _run)
    return calls


_PS_OTHER = ("faultline-api\tfaultline\t/home/u/FaultLine\tfaultline:latest\trunning\n"
             "faultline-postgres\tfaultline\t/home/u/FaultLine\tpostgres:16-alpine\trunning\n"
             "fl-test-pg\t\t\tpostgres:16\trunning\n")
_VOL_OTHER = "faultline_postgres_data\tfaultline\nwebapp_postgres_data\twebapp\n"


def test_detects_a_stack_from_another_checkout(qs, monkeypatch):
    _fake_docker(monkeypatch, qs, _PS_OTHER, _VOL_OTHER)
    found = qs.detect_other_stacks()
    assert list(found) == ["faultline"], found
    st = found["faultline"]
    assert st["dir"] == "/home/u/FaultLine"
    assert any("faultline-postgres" in c for c in st["containers"])
    assert st["volumes"] == ["faultline_postgres_data"]


def test_own_project_and_unrelated_containers_are_ignored(qs, monkeypatch):
    """Control: this checkout's own stack and someone else's postgres are not 'another stack'."""
    monkeypatch.setattr(qs, "HERE", "/home/u/FaultLine")
    _fake_docker(monkeypatch, qs, _PS_OTHER, _VOL_OTHER)
    assert qs.detect_other_stacks() == {}


def test_no_docker_means_no_question(qs, monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(qs.subprocess, "run", _boom)
    monkeypatch.setattr("builtins.input", lambda *_a: pytest.fail("must not prompt"))
    assert qs.configure_existing_stacks() == {}


@pytest.mark.parametrize("answer,want", [
    ("1", {"COMPOSE_PROJECT_NAME": "faultline"}),
    ("2", {}),
])
def test_reuse_and_replace(qs, monkeypatch, capsys, answer, want):
    calls = _fake_docker(monkeypatch, qs, _PS_OTHER, _VOL_OTHER)
    monkeypatch.setattr("builtins.input", lambda *_a: answer)
    assert qs.configure_existing_stacks() == want
    if answer == "2":
        out = capsys.readouterr().out
        assert "docker compose down -v" in out
        assert all(c[1] in ("ps", "volume") for c in calls), "the wizard never removes anything"


def test_side_by_side_gets_its_own_names_ports_and_image(qs, monkeypatch):
    _fake_docker(monkeypatch, qs, _PS_OTHER, _VOL_OTHER)
    monkeypatch.setattr("builtins.input", lambda *_a: "3")
    ov = qs.configure_existing_stacks()
    assert ov["COMPOSE_PROJECT_NAME"] == ov["FAULTLINE_PREFIX"] == "faultline-es"
    assert ov["FAULTLINE_IMAGE"] == "faultline-es:latest"
    assert ov["FAULTLINE_API_PORT"] != "8000" and ov["FAULTLINE_MCP_PORT"] != "8002"


def test_wizard_asks_before_writing_env(qs):
    src = (_ROOT / "quickstart.py").read_text()
    i, j = src.index("configure_existing_stacks()\n"), src.index("    write_env(cfg, mcp_key")
    assert i < j


def test_image_and_prefix_are_overridable():
    services = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())["services"]
    for svc in ("faultline", "faultline-mcp"):
        assert "${FAULTLINE_IMAGE:-" in services[svc]["image"]
    for name, svc in services.items():
        assert "${FAULTLINE_PREFIX:-" in svc["container_name"], name
