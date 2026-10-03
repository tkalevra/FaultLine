"""#212 pins: Redis runs as a cache without persistence, so shutdown cannot block on a full disk.

With `--appendonly yes` a full disk made Redis log "Error trying to save the DB, can't exit" and
`docker compose stop` hung until the kill timeout. Redis holds nothing durable in FOSS:
idempotency dedup, embedding cache, rate-budget counters, telemetry events and work-queue
claims (Postgres is the ledger). So no RDB, no AOF, no volume.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = sorted(_ROOT.glob("docker-compose*.yml"))


def _grace_s(v) -> int:
    m = re.fullmatch(r"(\d+)(s|m)?", str(v))
    assert m, v
    return int(m.group(1)) * (60 if m.group(2) == "m" else 1)


def _persistence_problems(svc: dict) -> list[str]:
    cmd = svc.get("command")
    argv = cmd if isinstance(cmd, list) else str(cmd or "").split()
    bad = []
    if "--appendonly" not in argv or argv[argv.index("--appendonly") + 1] != "no":
        bad.append("AOF not disabled")
    if "--save" not in argv or argv[argv.index("--save") + 1] not in ("", "''", '""'):
        bad.append("RDB snapshots not disabled")
    if any(":/data" in str(v) for v in (svc.get("volumes") or [])):
        bad.append("a /data volume is still mounted")
    if "stop_grace_period" not in svc:
        bad.append("no stop_grace_period")
    return bad


def _redis_services(path):
    services = yaml.safe_load(path.read_text())["services"]
    return {n: s for n, s in services.items() if "redis" in str(s.get("image", ""))}


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_redis_has_no_persistence(path):
    reds = _redis_services(path)
    assert reds, "every shipped compose file runs a redis"
    for name, svc in reds.items():
        assert not _persistence_problems(svc), (name, _persistence_problems(svc))


def test_control_old_shape_is_caught():
    old = {"image": "redis:7-alpine", "command": "redis-server --appendonly yes",
           "volumes": ["redis_data:/data"]}
    assert len(_persistence_problems(old)) == 4


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_postgres_gets_time_for_its_shutdown_checkpoint(path):
    services = yaml.safe_load(path.read_text())["services"]
    pg = [s for s in services.values() if "postgres" in str(s.get("image", ""))]
    assert pg and all(_grace_s(s.get("stop_grace_period", "0")) >= 30 for s in pg)


def test_no_redis_volume_declared():
    vols = yaml.safe_load((_ROOT / "docker-compose.yml").read_text()).get("volumes") or {}
    assert "redis_data" not in vols
