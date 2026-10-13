"""#214 pin: a documented way back from a full disk."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _section(text: str, title: str) -> str:
    i = text.index(title)
    j = text.find("\n## ", i + len(title))
    return text[i:] if j < 0 else text[i:j]


def test_deployment_has_a_disk_full_procedure():
    sec = _section((_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8"), "## Disk full")
    for must in ("docker system df", "-json.log", "docker builder prune", "truncate -s 0",
                 "docker compose up -d postgres", "ready to accept connections",
                 "replays its WAL", "curl -s http://localhost:8000/health", "LogConfig",
                 "--force-recreate", "FAULTLINE_DISK_REFUSE_PCT"):
        assert must in sec, must
    # restart order: postgres before the backend
    assert sec.index("up -d postgres") < sec.index("up -d faultline ") \
        if "up -d faultline " in sec else sec.index("up -d postgres") < sec.index("up -d faultline")
    assert "Never delete files inside the Postgres volume" in sec


def test_readme_points_to_it():
    assert "DEPLOYMENT.md#disk-full" in (_ROOT / "README.md").read_text(encoding="utf-8")
