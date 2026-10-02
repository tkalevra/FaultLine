"""Critic pins for branch foss-install-fixes (05f1a0a0). Each test is RED on that head.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml

from src.provisioning import boot_migrations as bm

_ROOT = Path(__file__).resolve().parents[1]


# ── #158 → issue #162: the comment-only re-stamp must not swallow a real SQL change ──

class _Conn:
    def close(self):
        pass


def _boot_with(tmp_path, monkeypatch, sql_bytes: bytes, applied_checksum: str):
    mid = "097_tombstone"
    (tmp_path / f"{mid}.sql").write_bytes(sql_bytes)
    ran, stamped = [], []
    monkeypatch.setattr(bm, "_connect", lambda dsn: _Conn())
    monkeypatch.setattr(bm, "_ensure_ledger", lambda conn: None)
    monkeypatch.setattr(bm, "_live_schemas", lambda conn: ["public", "faultline_t1"])
    monkeypatch.setattr(bm, "_applied_map", lambda conn: {
        ("public", mid): applied_checksum, ("faultline_t1", mid): applied_checksum})
    monkeypatch.setattr(bm, "_run_psql", lambda dsn, path: ran.append(path) or "")
    monkeypatch.setattr(bm, "_stamp", lambda conn, pairs, checksum, status, error=None:
                        stamped.append((list(pairs), checksum, status)) or len(list(pairs)))
    bm.run_boot_migrations(dsn="postgresql://unused", migrations_dir=str(tmp_path), echo=False)
    return ran, stamped


def test_listed_prior_does_not_restamp_a_file_whose_sql_changed(tmp_path, monkeypatch):
    """A schema stamped with a listed PRIOR checksum is re-stamped to WHATEVER the file hashes
    to today. If 097 later gets a real SQL change and the entry is not removed, every existing
    install skips the new SQL. The prior must map to the one reviewed current checksum."""
    prior = next(iter(bm._COMMENT_ONLY_PRIOR_CHECKSUMS["097_tombstone"]))
    sql = (_ROOT / "migrations" / "097_tombstone.sql").read_bytes()
    changed = sql + b"\nALTER TABLE IF EXISTS facts ADD COLUMN IF NOT EXISTS critic_probe INT;\n"
    ran, stamped = _boot_with(tmp_path, monkeypatch, changed, prior)
    assert ran, ("097 with CHANGED SQL was re-stamped as applied and never run "
                 f"(stamped={[(s[1][:12], s[2]) for s in stamped]})")


def test_comment_only_proof_sees_changes_after_dashes_inside_string_literals():
    """The git-history proof strips `--.*$` per line, so a change inside a string literal after
    `--` is invisible to it: it would certify a data change as comment-only."""
    from tests.test_foss_migration_comment_scrub import _strip_sql_comments
    a = "INSERT INTO public.rel_types (rel_type, label) VALUES ('x', 'a -- old label');"
    b = "INSERT INTO public.rel_types (rel_type, label) VALUES ('x', 'a -- NEW label');"
    assert _strip_sql_comments(a) != _strip_sql_comments(b)


# ── #149 → issue #165: env_file hands the network-facing MCP container every secret ──

def test_mcp_service_does_not_receive_the_whole_env_file():
    """faultline-mcp is the one service published on the network. With `env_file: .env` it now
    carries LLM_API_KEY, a pinned FAULTLINE_ADMIN_TOKEN and every other .env secret, none of
    which it reads. Least privilege: the backend gets env_file, the MCP an explicit list."""
    compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    mcp = compose["services"]["faultline-mcp"]
    assert "env_file" not in mcp, mcp.get("env_file")


# ── issue #163: quickstart re-run drops every operator-set key ──

def test_wizard_rerun_keeps_operator_set_keys(tmp_path, monkeypatch):
    """#143 tells a second stack to set FAULTLINE_PREFIX / ports in .env, and #151 says a re-run
    keeps credentials. write_env rebuilds .env from .env.example, so a re-run silently drops
    FAULTLINE_PREFIX (next `up` reuses stack 1's container names), the ports, and a pinned
    FAULTLINE_ADMIN_TOKEN (the backend falls back to the persisted first-boot token)."""
    import quickstart as qs
    env = tmp_path / ".env"
    env.write_text("FAULTLINE_PREFIX=faultline2\nFAULTLINE_MCP_PORT=8012\n"
                   "FAULTLINE_API_PORT=8010\nFAULTLINE_ADMIN_TOKEN=pinned-operator-token\n"
                   "LLM_BACKEND_TYPE=ollama\nMCP_API_KEY=k\n", encoding="utf-8")
    monkeypatch.setattr(qs, "ENV_PATH", str(env))
    monkeypatch.setattr(qs, "ENV_EXAMPLE", str(_ROOT / ".env.example"))
    monkeypatch.setattr(qs, "ask_yes", lambda *a, **k: True)
    monkeypatch.setattr(qs, "language_env", lambda lang=None: {})
    cfg = {"LLM_BACKEND_TYPE": "ollama", "LLM_BASE_URL": "http://h:11434",
           "LLM_API_KEY": "", "WGM_LLM_MODEL": "qwen2.5"}
    qs.write_env(cfg, "k", "", {})
    out = env.read_text(encoding="utf-8")
    missing = [k for k in ("FAULTLINE_PREFIX=faultline2", "FAULTLINE_MCP_PORT=8012",
                           "FAULTLINE_API_PORT=8010", "FAULTLINE_ADMIN_TOKEN=pinned-operator-token")
               if k not in out]
    assert not missing, f"re-run dropped {missing}"


# ── #145 → issue #164: un-pinning the env token revives the first-boot token ──

def test_unpinning_env_token_does_not_revive_the_first_boot_token(monkeypatch):
    """First boot (env unset) persists H(t0) and prints t0 to the container log. The operator
    later pins FAULTLINE_ADMIN_TOKEN (e.g. because t0 leaked from the log) and boots: the row
    is left untouched. Removing the pin (or a wizard re-run dropping it, see above) brings t0
    back as a valid operator credential with no rotate and no log line."""
    from src.api import operator_token as ot
    t0 = "first-boot-token-printed-to-logs"
    row = {"h": hashlib.sha256(t0.encode()).hexdigest()}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            self._sql, self._p = sql, params
            if sql.lstrip().upper().startswith("DELETE"):
                row["h"] = None
            elif "DO UPDATE" in sql and params:
                row["h"] = params[0]
        def fetchone(self):
            if "RETURNING" in self._sql:
                return None
            return (row["h"],) if row["h"] else None

    class _Db:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Db())
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://unused")
    ot._reset_cache_for_tests()
    monkeypatch.setenv("FAULTLINE_ADMIN_TOKEN", "operator-pinned")
    assert ot.ensure_operator_token() == "env"
    monkeypatch.delenv("FAULTLINE_ADMIN_TOKEN")
    ot._reset_cache_for_tests()
    assert not ot.operator_token_ok(t0), "the leaked first-boot token is valid again"
