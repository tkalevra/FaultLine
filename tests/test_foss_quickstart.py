"""quickstart.py fresh-install pins (#150, #151, #152). Stdlib-only wizard, no stack needed."""
from __future__ import annotations

import importlib
import os
import stat
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def qs(tmp_path, monkeypatch):
    """quickstart with ENV paths redirected into tmp_path (never touches the repo .env)."""
    sys.path.insert(0, str(_ROOT))
    try:
        mod = importlib.import_module("quickstart")
    finally:
        sys.path.remove(str(_ROOT))
    example = tmp_path / ".env.example"
    example.write_text((_ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(mod, "ENV_EXAMPLE", str(example))
    monkeypatch.setattr(mod, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mod, "HERE", str(tmp_path))
    monkeypatch.setattr(mod, "language_env", lambda lang=None: {})
    monkeypatch.umask = os.umask(0o022)  # the common default that made the files world-readable
    yield mod
    os.umask(monkeypatch.umask)


_CFG = {"LLM_BACKEND_TYPE": "openai", "LLM_BASE_URL": "https://api.example.invalid",
        "LLM_API_KEY": "sk-test-secret", "WGM_LLM_MODEL": "m"}


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


# ── #150: .env / .env.bak are owner-only ────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_env_and_backup_are_written_0600(qs, monkeypatch):
    qs.write_env(_CFG, "mcp-key-1", "")
    assert _mode(qs.ENV_PATH) == 0o600
    os.chmod(qs.ENV_PATH, 0o644)  # an older install's loose file
    monkeypatch.setattr(qs, "ask_yes", lambda *a, **k: True)  # "overwrite? y"
    qs.write_env(_CFG, "mcp-key-2", "")
    assert _mode(qs.ENV_PATH) == 0o600, "an existing loose .env must be tightened"
    assert _mode(qs.ENV_PATH + ".bak") == 0o600, ".env.bak holds the same keys"
    assert "MCP_API_KEY=mcp-key-1" in Path(qs.ENV_PATH + ".bak").read_text()


# ── #151: a re-run keeps the existing keys by default ───────────────────────

def _drive_main(qs, monkeypatch, inputs=None, build_rc=None):
    """Run quickstart.main() with every interactive step but the one under test stubbed.

    Every prompt answers Enter (accept the default) unless ``inputs`` says otherwise."""
    captured = {}
    answers = list(inputs or [])
    monkeypatch.setattr("builtins.input", lambda *_a: answers.pop(0) if answers else "")
    monkeypatch.setattr(sys, "argv", ["quickstart.py"])
    monkeypatch.setattr(qs, "language_gate", lambda: None)
    monkeypatch.setattr(qs, "check_prereqs", lambda: None)
    monkeypatch.setattr(qs, "configure_backend", lambda: dict(_CFG))
    monkeypatch.setattr(qs, "configure_embeddings", lambda cfg: {})
    monkeypatch.setattr(qs, "configure_identity", lambda: "")
    monkeypatch.setattr(qs, "configure_naming", lambda: {})
    monkeypatch.setattr(qs, "write_env", lambda cfg, key, uid, ov: captured.setdefault("mcp_key", key))
    monkeypatch.setattr(qs, "print_next_steps", lambda key: None)
    monkeypatch.setattr(qs, "_poll_health", lambda *a, **k: captured.setdefault("polled", True))
    monkeypatch.setattr(qs, "print_integration_guide", lambda *a: captured.setdefault("guide", True))
    if build_rc is not None:
        monkeypatch.setattr(qs, "ask_yes", lambda prompt, default_yes=True: True if "Build" in prompt else default_yes)
        monkeypatch.setattr(qs.subprocess, "run",
                            lambda *a, **k: types.SimpleNamespace(returncode=build_rc))
    return captured


def test_rerun_keeps_the_existing_mcp_key_by_default(qs, monkeypatch):
    Path(qs.ENV_PATH).write_text("MCP_API_KEY=the-key-clients-use\n", encoding="utf-8")
    captured = _drive_main(qs, monkeypatch)
    qs.main()
    assert captured["mcp_key"] == "the-key-clients-use", "a re-run must not silently rotate MCP_API_KEY"


def test_first_run_still_generates_a_key(qs, monkeypatch):
    captured = _drive_main(qs, monkeypatch)
    qs.main()
    assert captured["mcp_key"] and len(captured["mcp_key"]) >= 32


def test_rerun_keeps_the_llm_key_on_enter(qs, monkeypatch):
    Path(qs.ENV_PATH).write_text("LLM_BACKEND_TYPE=openai\nLLM_API_KEY=sk-kept\n", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda *_a: "")
    url, key = qs._prompt_connection("openai")
    assert key == "sk-kept"


# ── #152: a failed compose build exits non-zero and skips the guide ─────────

def test_failed_build_exits_nonzero_without_the_guide(qs, monkeypatch):
    captured = _drive_main(qs, monkeypatch, build_rc=1)
    with pytest.raises(SystemExit) as ei:
        qs.main()
    assert ei.value.code not in (0, None)
    assert "polled" not in captured and "guide" not in captured


def test_successful_build_polls_and_prints_the_guide(qs, monkeypatch):
    captured = _drive_main(qs, monkeypatch, build_rc=0)
    qs.main()
    assert captured.get("polled") and captured.get("guide")



# ── #163: a re-run keeps operator-set keys the wizard does not manage ───────

def test_rerun_preserves_unmanaged_keys(qs, monkeypatch):
    Path(qs.ENV_PATH).write_text("FAULTLINE_PREFIX=faultline2\nFAULTLINE_ADMIN_TOKEN=pinned\n"
                                 "MY_CUSTOM=1\nWGM_LLM_MODEL=old-model\n", encoding="utf-8")
    monkeypatch.setattr(qs, "ask_yes", lambda *a, **k: True)
    qs.write_env(_CFG, "k", "")
    out = Path(qs.ENV_PATH).read_text()
    for kv in ("FAULTLINE_PREFIX=faultline2", "FAULTLINE_ADMIN_TOKEN=pinned", "MY_CUSTOM=1"):
        assert kv in out, kv
    assert "WGM_LLM_MODEL=m" in out and "WGM_LLM_MODEL=old-model" not in out, "the wizard's answer wins"
    assert out.count("FAULTLINE_PREFIX=") == 1
