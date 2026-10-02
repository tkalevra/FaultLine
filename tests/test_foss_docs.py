"""Install-doc accuracy pins (#156, #155): the commands and claims a new user follows."""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DOCS = ["README.md", "DEPLOYMENT.md", "docs/DEPLOYMENT.md", "docs/MCP-SETUP.md", "webui/index.html"]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ── #156 ─────────────────────────────────────────────────────────────────────

def test_no_bare_docker_logs_on_a_compose_service_name():
    """`docker logs faultline` fails: the container is faultline-api. Use docker compose logs."""
    for rel in _DOCS:
        assert not re.search(r"docker logs faultline(\s|$)", _read(rel)), rel


def test_readme_lists_every_advertised_tool():
    from src.mcp.tools import _CANONICAL_ORDER
    readme = _read("README.md")
    missing = [t for t in _CANONICAL_ORDER if f"`{t}`" not in readme]
    assert not missing, missing


def test_no_hand_maintained_tool_count():
    for rel in _DOCS:
        assert not re.search(r"\ball (six|three|eight) tools\b", _read(rel)), rel


def test_manual_path_builds_the_image():
    readme = _read("README.md")
    assert "docker compose up -d --build" in readme
    assert not re.search(r"^docker compose up -d$", readme, re.M), "the bare form prints a pull error first"


def test_deployment_ports_table_matches_compose():
    dep = _read("DEPLOYMENT.md")
    assert "127.0.0.1:6333" in dep and "0.0.0.0:8002" in dep
    assert "| `postgres` | 5432 |" not in dep, "postgres is not published"


# ── #155 ─────────────────────────────────────────────────────────────────────

def test_readme_explains_first_login_and_seats():
    readme = _read("README.md")
    assert "First login and seats" in readme
    for needle in ("FAULTLINE_ADMIN_TOKEN", "http://localhost:8000/", "5 seats", "seat token",
                   "403 seat required"):
        assert needle in readme, needle


def test_deployment_explains_first_login_and_seats():
    assert "First login and seats" in _read("DEPLOYMENT.md")


def test_changelog_has_the_seat_cap_release():
    head = _read("CHANGELOG.md")[:6000]
    assert "Seat cap" in head and "2026-10-02" in head


def test_mcpb_asks_for_a_seat_token():
    import json
    cfg = json.loads(_read("tools/claude-desktop/manifest.json"))["user_config"]
    assert "seat token" in cfg["mcp_api_key"]["description"].lower()
    assert cfg["user_id"]["required"] is False


def test_wizard_next_steps_mention_console_and_seats(capsys):
    import importlib
    import sys
    sys.path.insert(0, str(_ROOT))
    try:
        qs = importlib.import_module("quickstart")
    finally:
        sys.path.remove(str(_ROOT))
    qs.print_next_steps("k")
    out = capsys.readouterr().out
    assert "http://localhost:8000/" in out and "FAULTLINE_ADMIN_TOKEN" in out and "Seats" in out
    # #166: the token is printed only once; after a recreate the logs no longer have it.
    assert "python -m src.api.operator_token --rotate" in out



# ── #167 (docs/config part) ──────────────────────────────────────────────────

def test_every_llm_timeout_knob_is_documented():
    from src.api.llm_calls import LLMTimeouts
    env = _read(".env.example")
    missing = [op for op in LLMTimeouts._DEFAULTS
               if not op.startswith("NATURAL_LANGUAGE") and op != "TAXONOMY_DISCOVERY"
               and f"LLM_TIMEOUT_{op}=" not in env]
    assert not missing, missing


def test_readme_warns_about_reasoning_models():
    readme = _read("README.md")
    assert "non-reasoning" in readme and "LLM_TIMEOUT_REFRAME" in readme
