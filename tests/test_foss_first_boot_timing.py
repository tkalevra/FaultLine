"""FOSS-007 pins: first-boot timing is documented honestly and every waiter covers it.

Measured (fresh-install gauntlet): first boot 5-15 min on commodity CPUs (baked-model
load, single core, ~silent); warm restart 1-2 min. The old budgets all gave up on a
healthy stack: docs said '~2 minutes'/'Wait ~60s', the wizard's poll held 240 s in
silence, and the compose healthcheck's start_period was 60 s."""
from __future__ import annotations

import importlib
import inspect
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_FIRST_BOOT_RANGE = re.compile(r"5[\u2013-]15\s*min", re.I)
_WARM_RANGE = re.compile(r"1[\u2013-]2\s*min", re.I)


def _readme():
    return (_ROOT / "README.md").read_text(encoding="utf-8")


def _qs():
    sys.path.insert(0, str(_ROOT))
    try:
        return importlib.import_module("quickstart")
    finally:
        sys.path.remove(str(_ROOT))


# ── docs: measured range in, dead budgets out ────────────────────────────────

def test_readme_states_the_measured_first_boot_range():
    readme = _readme()
    assert _FIRST_BOOT_RANGE.search(readme), "first boot 5-15 min must be stated"
    assert _WARM_RANGE.search(readme), "warm restart 1-2 min must be stated"
    assert not re.search(r"[Ww]ait ~60s", readme)
    assert "~2 minutes" not in readme and "Takes 3" not in readme


def test_wizard_strings_state_the_range():
    src = (_ROOT / "quickstart.py").read_text(encoding="utf-8")
    assert "~2 minutes" not in src
    assert "Wait ~60s" not in src
    assert _FIRST_BOOT_RANGE.search(src), "the wizard's own copy must carry the measured range"
    assert _WARM_RANGE.search(src)


# ── the wizard's health poll ─────────────────────────────────────────────────

def test_poll_default_budget_covers_first_boot():
    default = inspect.signature(_qs()._poll_health).parameters["timeout"].default
    assert default >= 900, default


def test_poll_emits_minute_progress_lines(monkeypatch, capsys):
    """A silent 'unhealthy' window read as a broken install: the poll must narrate."""
    qs = _qs()

    class _Clock:
        now = 0.0

        def monotonic(self):
            return _Clock.now

        def sleep(self, s):
            _Clock.now += s

    clock = _Clock()
    monkeypatch.setattr(qs.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(qs.time, "sleep", clock.sleep)

    def _down(*a, **k):
        raise ConnectionError("not up yet")

    monkeypatch.setattr(qs, "_http_get", _down)
    qs._poll_health(timeout=185)
    out = capsys.readouterr().out
    for s in (60, 120, 180):
        assert f"still loading models ({s}s)..." in out, s
    assert "not healthy yet within the wait" in out, "the failure shape must survive"


def test_poll_success_shape_untouched(monkeypatch, capsys):
    qs = _qs()
    monkeypatch.setattr(qs.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(qs, "_http_get",
                        lambda *a, **k: (200, '{"database": "ok", "llm": "ok"}'))
    qs._poll_health()
    out = capsys.readouterr().out
    assert "backend up" in out and "database:ok" in out and "llm:ok" in out
    assert "still loading models" not in out


# ── the compose healthchecks ─────────────────────────────────────────────────

def _seconds(v) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", str(v).strip())
    assert m, f"unparseable duration: {v!r}"
    return float(m.group(1)) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, None: 1}[m.group(2)]


def _compose():
    import yaml
    return yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_every_healthchecked_service_has_a_start_period():
    services = _compose()["services"]
    missing = [name for name, svc in services.items()
               if svc.get("healthcheck") and "start_period" not in svc["healthcheck"]]
    assert not missing, missing


def test_api_start_period_covers_the_documented_first_boot():
    hc = _compose()["services"]["faultline"]["healthcheck"]
    assert _seconds(hc["start_period"]) >= 900, hc["start_period"]
    assert _seconds(hc["interval"]) <= 60, "warm restarts still need a sharp interval"


def test_sidecar_start_periods_stay_sane():
    services = _compose()["services"]
    for name in ("postgres", "redis"):
        hc = services[name]["healthcheck"]
        assert 30 <= _seconds(hc["start_period"]) <= 300, (name, hc["start_period"])


def test_portainer_variant_api_start_period_too():
    import yaml
    raw = (_ROOT / "docker-compose-portainer-withoutqdrant.yml").read_text(encoding="utf-8")
    services = yaml.safe_load(raw)["services"]
    hc = services["faultline"]["healthcheck"]
    assert _seconds(hc["start_period"]) >= 900, hc["start_period"]
