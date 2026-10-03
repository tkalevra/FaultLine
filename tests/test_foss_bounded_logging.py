"""#210 pins: container logs are bounded, and the re-embedder's repeating warnings are aggregated.

An unattended install filled its host disk: Docker's default json-file driver keeps every line
forever, Qdrant logged every REST poll at INFO, and the re-embedder re-emitted one WARNING per
suspect entity and one CRITICAL per tenant on every cycle.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = sorted(_ROOT.glob("docker-compose*.yml"))


def _default(value) -> str:
    """Resolve ${VAR:-default} to its default, as compose does with an empty environment."""
    return re.sub(r"\$\{([A-Z0-9_]+):-([^}]*)\}", lambda m: m.group(2), str(value))


def _size_bytes(v: str) -> int:
    m = re.fullmatch(r"(\d+)([kmg]?)", v.strip().lower())
    assert m, v
    return int(m.group(1)) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[m.group(2)]


def _unbounded_services(services: dict) -> list[str]:
    bad = []
    for name, svc in services.items():
        lg = svc.get("logging") or {}
        opts = lg.get("options") or {}
        if lg.get("driver") not in ("json-file", "local"):
            bad.append(f"{name}: driver={lg.get('driver')!r}")
            continue
        if "max-size" not in opts or "max-file" not in opts:
            bad.append(f"{name}: no max-size/max-file")
            continue
        size, files = _default(opts["max-size"]), int(_default(opts["max-file"]))
        if _size_bytes(size) * files > 1024**3:
            bad.append(f"{name}: default bound {size} x {files} exceeds 1 GiB")
    return bad


def test_compose_files_found():
    names = {p.name for p in COMPOSE_FILES}
    assert {"docker-compose.yml", "docker-compose-portainer-withoutqdrant.yml"} <= names


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_every_compose_service_has_bounded_logging(path):
    services = yaml.safe_load(path.read_text())["services"]
    assert not _unbounded_services(services), _unbounded_services(services)


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_log_bounds_are_env_overridable_via_shared_anchor(path):
    text = path.read_text()
    assert "x-logging: &default-logging" in text
    services = yaml.safe_load(text)["services"]
    assert text.count("logging: *default-logging") == len(services), \
        "every service must use the shared anchor, not a private copy"
    opts = yaml.safe_load(text)["x-logging"]["options"]
    assert "FAULTLINE_LOG_MAX_SIZE" in opts["max-size"]
    assert "FAULTLINE_LOG_MAX_FILE" in opts["max-file"]


def test_control_unbounded_service_is_detected():
    """Control: the checker is not vacuous. A service added without a logging block is caught,
    whatever it is called."""
    fake = {"brand-new-sidecar": {"image": "example/sidecar"},
            "huge": {"logging": {"driver": "json-file",
                                 "options": {"max-size": "500m", "max-file": "9"}}}}
    bad = _unbounded_services(fake)
    assert len(bad) == 2 and bad[0].startswith("brand-new-sidecar")


def test_qdrant_defaults_to_warn():
    services = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())["services"]
    lvl = services["qdrant"]["environment"]["QDRANT__LOG_LEVEL"]
    assert _default(lvl) == "WARN" and "QDRANT_LOG_LEVEL" in lvl


# ── re-embedder aggregation ──────────────────────────────────────────────────

emb = pytest.importorskip("src.re_embedder.embedder")


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def rollback(self):
        pass


def test_suspect_preferred_names_no_per_entity_warning(caplog):
    rows = [(f"00000000-0000-0000-0000-{i:012d}", f"alias{i}", "inferred") for i in range(50)]
    with caplog.at_level(logging.DEBUG, logger=emb.log.name):
        stats = emb.flag_suspect_preferred_names(_Conn(rows))
    assert stats["flagged"] == 50
    warn = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warn == [], "per-suspect lines must be DEBUG, not WARNING"
    assert sum("suspect_preferred_name " in r.getMessage() for r in caplog.records) == 50


def test_cycle_summary_is_one_line_with_counts(caplog):
    agg = emb._CycleLogSummary()
    for t in range(7):
        agg.add("suspect_preferred_name", f"tenant_{t}", count=40)
        agg.add("pending_placement_backlog_undrainable", f"tenant_{t}",
                unplaceable_rels=2, unplaceable_facts=10)
    for g in range(3):
        agg.add("ghost_tenant_skipped", f"ghost_{g}", count=1)
    with caplog.at_level(logging.DEBUG, logger=emb.log.name):
        line = agg.flush(emb.log)
    recs = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(recs) == 1 and recs[0].getMessage() == line
    assert "suspect_preferred_name(tenants=7 count=280)" in line
    assert "pending_placement_backlog_undrainable(tenants=7 unplaceable_rels=14 " \
           "unplaceable_facts=70)" in line
    assert "ghost_tenant_skipped(tenants=3 count=3)" in line
    assert agg.flush(emb.log) is None, "counters reset after a flush; quiet cycle logs nothing"


def test_main_loop_flushes_once_per_cycle():
    src = (_ROOT / "src/re_embedder/embedder.py").read_text()
    loop_tail = src[src.index("re_embedder.loop_error"):]
    loop_tail = loop_tail[:loop_tail.index("time.sleep(interval)")]
    assert "_cycle_summary.flush()" in loop_tail
    for event in ("re_embedder.ghost_tenant_skipped schema",
                  "re_embedder.pending_placement_backlog_undrainable"):
        i = src.index(event)
        assert "log.debug(" in src[i - 200:i], f"{event} detail must be DEBUG"
