"""#158: no references to private-tree paths in the public tree, without re-running migrations.

Scrubbing a reference from a migration header changes the file's checksum, and the boot ledger
would then re-run that file against every existing tenant (several fan out INSERT/UPDATE). The
ledger therefore treats a listed PRIOR checksum of a comment-only edit as already applied.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.provisioning import boot_migrations as bm

_ROOT = Path(__file__).resolve().parents[1]
_PRIVATE_PREFIX = "DEV" + "/"          # spelled so this file does not match itself
_SCRUB_BASE = "a11b3d03"               # the public head the scrub was made against


def _tracked_files() -> list[Path]:
    if shutil.which("git"):
        out = subprocess.run(["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return [_ROOT / p for p in out.stdout.splitlines()]
    return [p for p in _ROOT.rglob("*") if p.is_file() and ".git" not in p.parts
            and "__pycache__" not in p.parts]


def test_no_private_tree_paths_in_the_public_tree():
    hits = []
    for p in _tracked_files():
        if p.name == ".gitignore" or not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _PRIVATE_PREFIX in line:
                hits.append(f"{p.relative_to(_ROOT)}:{i}")
    assert not hits, hits


def _strip_sql_comments(sql: str) -> list[str]:
    """SQL with comments removed, as non-empty lines. String-literal aware (#162): `--` or `/*`
    inside '...', "..." or $tag$...$tag$ is data, not a comment, so a change there is seen."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:   # doubled quote = escaped
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j < 0 else j + len(tag)
                out.append(sql[i:j])
                i = j
                continue
        out.append(c)
        i += 1
    return [ln.rstrip() for ln in "".join(out).splitlines() if ln.strip()]


@pytest.mark.parametrize("migration_id", sorted(bm._COMMENT_ONLY_PRIOR_CHECKSUMS))
def test_expected_current_checksum_matches_the_file(migration_id):
    """Every mapping names the file's reviewed current checksum. Runs without git history: if
    the file changes again, the mapping goes stale and this fails instead of re-stamping."""
    current = bm._checksum((_ROOT / "migrations" / f"{migration_id}.sql").read_bytes())
    expected = set(bm._COMMENT_ONLY_PRIOR_CHECKSUMS[migration_id].values())
    assert expected == {current}, f"{migration_id}: file changed since its comment-only review"


@pytest.mark.parametrize("migration_id", sorted(bm._COMMENT_ONLY_PRIOR_CHECKSUMS))
def test_listed_priors_differ_only_in_comments(migration_id):
    """Proof from git history that every listed prior is a comment-only revision. Without the
    pre-scrub commit in the clone, the expected-current test above still guards the mapping."""
    path = f"migrations/{migration_id}.sql"
    old = None
    if shutil.which("git"):
        r = subprocess.run(["git", "show", f"{_SCRUB_BASE}:{path}"], cwd=_ROOT, capture_output=True)
        old = r.stdout if r.returncode == 0 else None
    if old is None:
        test_expected_current_checksum_matches_the_file(migration_id)
        return
    assert hashlib.sha256(old).hexdigest() in bm._COMMENT_ONLY_PRIOR_CHECKSUMS[migration_id]
    current = (_ROOT / path).read_text(encoding="utf-8")
    assert _strip_sql_comments(old.decode("utf-8")) == _strip_sql_comments(current)


def test_comment_stripper_is_string_literal_aware():
    a = "INSERT INTO t VALUES ('a -- old'); -- trailing"
    b = "INSERT INTO t VALUES ('a -- new'); -- other"
    assert _strip_sql_comments(a) != _strip_sql_comments(b)
    assert _strip_sql_comments("SELECT 1; -- x") == _strip_sql_comments("SELECT 1; -- y")
    assert _strip_sql_comments("DO $$ BEGIN -- kept $$;") != _strip_sql_comments("DO $$ BEGIN -- changed $$;")


class _Conn:
    def close(self):
        pass


def _boot(tmp_path, monkeypatch, applied_checksum):
    mid = "097_tombstone"
    shutil.copy(_ROOT / "migrations" / f"{mid}.sql", tmp_path / f"{mid}.sql")
    ran, stamped = [], []
    monkeypatch.setattr(bm, "_connect", lambda dsn: _Conn())
    monkeypatch.setattr(bm, "_ensure_ledger", lambda conn: None)
    monkeypatch.setattr(bm, "_live_schemas", lambda conn: ["public", "faultline_t1"])
    monkeypatch.setattr(bm, "_applied_map", lambda conn: {
        ("public", mid): applied_checksum, ("faultline_t1", mid): applied_checksum})
    monkeypatch.setattr(bm, "_run_psql", lambda dsn, path: ran.append(path) or "")
    monkeypatch.setattr(bm, "_stamp", lambda conn, pairs, checksum, status, error=None:
                        stamped.append((list(pairs), checksum, status)) or len(list(pairs)))
    summary = bm.run_boot_migrations(dsn="postgresql://unused", migrations_dir=str(tmp_path), echo=False)
    return summary, ran, stamped


def test_comment_only_revision_is_restamped_not_rerun(tmp_path, monkeypatch):
    prior = next(iter(bm._COMMENT_ONLY_PRIOR_CHECKSUMS["097_tombstone"]))
    summary, ran, stamped = _boot(tmp_path, monkeypatch, prior)
    assert ran == [], "a comment-only edit must not re-run a migration against existing tenants"
    current = bm._checksum((_ROOT / "migrations" / "097_tombstone.sql").read_bytes())
    assert stamped and stamped[0][1] == current and stamped[0][2] == "applied"
    assert "097_tombstone" in summary.skipped


def test_listed_prior_with_a_changed_file_reruns(tmp_path, monkeypatch):
    """#162: a listed prior re-stamps only while the file hashes to the reviewed current value."""
    prior = next(iter(bm._COMMENT_ONLY_PRIOR_CHECKSUMS["097_tombstone"]))
    mid = "097_tombstone"
    (tmp_path / f"{mid}.sql").write_bytes((_ROOT / "migrations" / f"{mid}.sql").read_bytes() + b"\nSELECT 2;\n")
    ran = []
    monkeypatch.setattr(bm, "_connect", lambda dsn: _Conn())
    monkeypatch.setattr(bm, "_ensure_ledger", lambda conn: None)
    monkeypatch.setattr(bm, "_live_schemas", lambda conn: ["public"])
    monkeypatch.setattr(bm, "_applied_map", lambda conn: {("public", mid): prior})
    monkeypatch.setattr(bm, "_run_psql", lambda dsn, path: ran.append(path) or "")
    monkeypatch.setattr(bm, "_stamp", lambda *a, **k: 0)
    bm.run_boot_migrations(dsn="postgresql://unused", migrations_dir=str(tmp_path), echo=False)
    assert len(ran) == 1


def test_unknown_checksum_still_reruns(tmp_path, monkeypatch):
    summary, ran, _ = _boot(tmp_path, monkeypatch, "0" * 64)
    assert len(ran) == 1, "a real change must still re-run"
