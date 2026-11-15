"""#63 pin: every test-to-test import resolves to a file that is actually COMMITTED.

`tests/` is gitignored, so a test file only ships when someone remembers `git add -f`. On the
public tree (foss-origin/main 5cf98143, still true at 0f7aa4ca) `tests/test_purenp_scalar_surfacing.py`
does `from tests.test_schedule_scalar_surfacing import _FakeDB, _FakeCur`, but the helper was never
force-added, so `pytest tests/` on a clean checkout stops with ModuleNotFoundError at collection.
The working tree cannot catch that — the helper is on disk there. The git INDEX can.

This reads the committed tree (`git ls-files`) and every tracked test module's imports via AST, and
fails on any `tests.*` (or bare sibling-module) import whose target is not tracked. Skips outside a
git checkout (e.g. a `git archive` staging dir), where the question has no answer.
"""
import ast
import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tracked(ref=None):
    cmd = ["git", "-C", _ROOT] + (["ls-tree", "-r", "--name-only", ref] if ref else ["ls-files"])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return set(out.stdout.split())


def _source(path, ref=None):
    if ref is None:
        with open(os.path.join(_ROOT, path), encoding="utf-8") as fh:
            return fh.read()
    return subprocess.run(["git", "-C", _ROOT, "show", f"{ref}:{path}"],
                          capture_output=True, text=True, timeout=30).stdout


def untracked_test_imports(ref=None):
    """[(importing file, imported module)] for test imports whose target is not committed."""
    files = _tracked(ref)
    if files is None:
        return None
    tests_mods = {f[:-3].replace("/", ".") for f in files
                  if f.startswith("tests/") and f.endswith(".py")}
    tests_pkgs = {os.path.dirname(f).replace("/", ".") for f in files if f.startswith("tests/")}
    bare = {m.rsplit(".", 1)[-1] for m in tests_mods if m.count(".") == 1}  # tests/<x>.py
    missing = []
    for f in sorted(files):
        if not (f.startswith("tests/") and f.endswith(".py")):
            continue
        try:
            tree = ast.parse(_source(f, ref))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue
            for mod in names:
                if mod.split(".")[0] != "tests":
                    continue
                if mod in tests_mods or mod in tests_pkgs:
                    continue
                missing.append((f, mod))
        # bare sibling imports (`import migration_census` with tests/ on sys.path): only flag a
        # name that exists on DISK under tests/ but is not tracked — anything else is a library.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".")[0]
                    if top in bare:
                        continue
                    if ref is None and os.path.isfile(os.path.join(_ROOT, "tests", top + ".py")):
                        missing.append((f, top))
    return missing


def test_every_test_to_test_import_is_committed():
    missing = untracked_test_imports()
    if missing is None:
        pytest.skip("not a git checkout — nothing to compare the imports against")
    assert not missing, (
        "tests import modules that are not committed (tests/ is gitignored — `git add -f` them, "
        f"or move the shared helper into a tracked module): {missing}")


def test_the_guard_sees_an_uncommitted_helper(tmp_path, monkeypatch):
    """Control: the guard is not vacuous — an import of an untracked tests module is reported."""
    files = _tracked()
    if files is None:
        pytest.skip("not a git checkout")
    probe = "tests/test_purenp_scalar_surfacing.py"
    if probe not in files:
        pytest.skip(f"{probe} is not in this tree")
    fake = files - {"tests/test_schedule_scalar_surfacing.py"}  # the public-tree omission
    monkeypatch.setattr(sys.modules[__name__], "_tracked", lambda ref=None: fake)
    assert (probe, "tests.test_schedule_scalar_surfacing") in untracked_test_imports()
