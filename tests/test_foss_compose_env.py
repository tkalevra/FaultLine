"""docker-compose.yml / .env.example consistency pins (#149, #143, #144, #157).

#149: compose had no env_file, so ~40 documented .env keys (timeouts, embeddings, schema prefix,
pipeline flags) never reached the containers, the wizard's external-embedder and schema-prefix
choices were silently ignored, a custom DB name rewrote POSTGRES_DSN but not POSTGRES_DB, and
.env.example's defaults disagreed with compose's (openwebui vs ollama).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]


def _env_example() -> dict:
    out = {}
    for line in (_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _env_files(svc: str) -> list[str]:
    raw = SERVICES[svc].get("env_file") or []
    raw = [raw] if isinstance(raw, (str, dict)) else raw
    return [e["path"] if isinstance(e, dict) else e for e in raw]


# ── #149 ─────────────────────────────────────────────────────────────────────

def test_backend_reads_dot_env():
    assert ".env" in _env_files("faultline"), "the backend must receive every .env key (env_file)"


def test_mcp_gets_only_the_keys_it_reads():
    """#165: the network-facing MCP must not hold LLM_API_KEY or other .env secrets."""
    mcp = SERVICES["faultline-mcp"]
    assert "env_file" not in mcp
    env = mcp.get("environment") or {}
    for secret in ("LLM_API_KEY", "FAULTLINE_ADMIN_TOKEN", "POSTGRES_PASSWORD", "EMBEDDING_API_KEY"):
        assert secret not in env, secret


def test_env_file_is_optional():
    for svc in ("faultline",):
        entries = SERVICES[svc]["env_file"]
        assert all(isinstance(e, dict) and e.get("required") is False for e in entries), \
            "a bare `docker compose up` without .env must still start"


def test_env_example_defaults_match_compose_defaults():
    env = _env_example()
    mismatched = []
    for svc, d in SERVICES.items():
        for k, v in (d.get("environment") or {}).items():
            m = re.fullmatch(r"\$\{([A-Z0-9_]+):-(.*)\}", str(v))
            if m and m.group(1) in env and env[m.group(1)] != m.group(2):
                mismatched.append(f"{svc}.{k}: compose={m.group(2)!r} .env.example={env[m.group(1)]!r}")
    assert not mismatched, mismatched


def test_env_example_does_not_pin_a_second_model():
    """With env_file, an active PATTERN_EXTRACTION_MODEL would override the wizard's model."""
    assert "PATTERN_EXTRACTION_MODEL" not in _env_example()


def test_wizard_custom_db_name_sets_postgres_db(monkeypatch):
    import importlib
    import sys
    sys.path.insert(0, str(_ROOT))
    try:
        qs = importlib.import_module("quickstart")
    finally:
        sys.path.remove(str(_ROOT))
    answers = iter(["n", "myprefix", "mydb", "mycoll"])
    monkeypatch.setattr("builtins.input", lambda *_a: next(answers))
    monkeypatch.setattr(qs, "_port_open", lambda *a, **k: False)
    ov = qs.configure_naming()
    assert ov["POSTGRES_DSN"].endswith("/mydb")
    assert ov.get("POSTGRES_DB") == "mydb", "the postgres service must create the DSN's database"


# ── #143: -p can isolate; names, ports and image are env-overridable, defaults unchanged ──

def _resolve(value: str, env: dict) -> str:
    return re.sub(r"\$\{([A-Z0-9_]+):-([^}]*)\}", lambda m: env.get(m.group(1)) or m.group(2), str(value))


_DEFAULT_NAMES = {"faultline": "faultline-api", "faultline-mcp": "faultline-mcp",
                  "postgres": "faultline-postgres", "qdrant": "faultline-qdrant",
                  "redis": "faultline-redis", "ollama": "faultline-ollama"}


def test_container_names_are_overridable_with_unchanged_defaults():
    for svc, default in _DEFAULT_NAMES.items():
        raw = SERVICES[svc]["container_name"]
        assert "${" in raw, f"{svc}: hard-coded container_name blocks a second stack"
        assert _resolve(raw, {}) == default
        assert _resolve(raw, {"FAULTLINE_PREFIX": "fl2"}) != default


def test_published_host_ports_are_overridable_with_unchanged_defaults():
    defaults = {"faultline": "127.0.0.1:8000:8000", "faultline-mcp": "8002:8002"}
    for svc, want in defaults.items():
        ports = SERVICES[svc].get("ports") or []
        assert all("${" in p for p in ports), f"{svc}: hard-coded host port"
        assert [_resolve(p, {}) for p in ports] == [want]


def test_image_tag_is_overridable():
    for svc in ("faultline", "faultline-mcp"):
        raw = SERVICES[svc]["image"]
        assert _resolve(raw, {}) == "faultline:latest" and "${" in raw


# ── #144: only the MCP server is published on the network ───────────────────

def test_only_mcp_is_network_facing():
    for path in ("docker-compose.yml", "docker-compose-portainer-withoutqdrant.yml"):
        services = yaml.safe_load((_ROOT / path).read_text())["services"]
        for svc, d in services.items():
            for p in d.get("ports") or []:
                p = str(p)
                if svc == "faultline-mcp":
                    continue
                assert p.startswith("127.0.0.1:"), f"{path}: {svc} publishes {p} on every interface"


# ── #157: FaultLine's network has a predictable name to attach OpenWebUI to ──

def test_network_has_a_fixed_overridable_name():
    net = COMPOSE["networks"]["faultline-net"]
    assert _resolve(net.get("name", ""), {}) == "faultline-net"
    assert "FAULTLINE_PREFIX" in net["name"], "a second stack must get its own network"


def test_readme_documents_joining_the_network():
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "external: true" in readme and "docker network connect faultline-net" in readme



def test_built_images_are_never_pulled():
    """`up` tried to pull faultline:latest first and printed 'pull access denied' (#156)."""
    for svc in ("faultline", "faultline-mcp"):
        assert SERVICES[svc].get("pull_policy") == "never", svc
