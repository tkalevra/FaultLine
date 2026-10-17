"""FOSS-003 pins: ./setup.sh runs on POSIX-sh-only hosts.

setup.sh's first act is a helpful python3 check — but the bash shebang aborted the
interpreter lookup before it on bash-less hosts ("env: can't execute 'bash'"), and
'set -o pipefail' is a runtime bashism dash would still die on. sh -n cannot catch
that one, so it is pinned textually; the launcher dependency is pinned in the README
Requirements (the section that never mentioned bash at all)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
SETUP = _ROOT / "setup.sh"


def test_shebang_is_bin_sh():
    first = SETUP.read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/bin/sh", first


def test_sh_n_syntax_check_passes():
    r = subprocess.run(["sh", "-n", str(SETUP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_nonposix_options_left():
    text = SETUP.read_text(encoding="utf-8")
    assert "pipefail" not in text, "set -o pipefail is a bashism: dash dies on it at runtime"
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("bash" in ln.lower() for ln in code_lines), \
        "the launcher must not invoke bash (mentioning it in a comment is fine)"


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_setup_sh_runs_under_sh_with_no_python_but_the_stub(tmp_path):
    """End-to-end, bash-free: run setup.sh via its shebang with a PATH holding ONLY a stub
    python3. Proves the launcher itself executes under /bin/sh and delegates to the wizard."""
    stubdir = tmp_path / "bin"
    stubdir.mkdir()
    stub = stubdir / "python3"
    stub.write_text("#!/bin/sh\necho \"STUB '\"$@\"'\"\n", encoding="utf-8")
    stub.chmod(0o755)
    r = subprocess.run([str(SETUP)], capture_output=True, text=True,
                       env={"PATH": str(stubdir)})
    assert r.returncode == 0, r.stderr
    assert "STUB" in r.stdout and "quickstart.py" in r.stdout, r.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_missing_python_message_names_the_direct_fallback(tmp_path):
    """With no python on PATH at all, the guard must still help — naming python3
    quickstart.py as the direct fallback (that message is the launcher's whole job)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    r = subprocess.run([str(SETUP)], capture_output=True, text=True,
                       env={"PATH": str(empty)})
    assert r.returncode != 0
    assert "quickstart.py" in r.stderr, r.stderr


def test_readme_requirements_name_the_launcher_dependency():
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    req = readme.split("## Requirements", 1)[1].split("\n## ", 1)[0]
    assert "setup.sh" in req
    assert "python3 quickstart.py" in req, "the direct no-shell path must be named"
    assert "/bin/sh" in req and "bash" in req, \
        "the real launcher dependency (POSIX sh, no bash) must be stated"
