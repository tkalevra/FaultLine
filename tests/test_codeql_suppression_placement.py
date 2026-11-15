"""Every ``# codeql[...]`` in the repo is ALONE on its line, directly above the flagged code.

CodeQL's alert-suppression reader (github/codeql ``shared/util/codeql/util/suppression/
AlertSuppression.qll``) binds a ``codeql[rule-id]`` comment to the FOLLOWING line and only when
the comment is the whole line — a trailing same-line comment suppresses nothing. The sibling
lane's ``tests/test_log_redaction.py`` (IE4) judges honesty + placement for its three rules and
pins their count; this module is the repo-wide PLACEMENT census for every rule, plus the pinned
count for this lane's rules (py/path-injection, py/polynomial-redos,
py/weak-sensitive-data-hashing) — extend, do not duplicate.

Run: FLTEST_LEDGER=... python3 tools/fltest.py --bug IS5 --test tests/test_codeql_suppression_placement.py
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ["src", "saas", "scripts", "tools", "quickstart.py"]
ALONE_RE = re.compile(r"^\s*#\s*codeql\[(py/[a-z0-9-]+)\]\s+(.+)$")
#: An annotation is `# codeql[py/<rule-id>]` — prose such as "every # codeql[...]" in a string
#: literal (tools/gauntlet.py) is not one.
DETECT_RE = re.compile(r"#\s*codeql\[py/[a-z0-9-]+\]")
THIS_LANE_RULES = ("py/path-injection", "py/polynomial-redos", "py/weak-sensitive-data-hashing")
#: Pinned: 2 path-injection (http_server), 1 polynomial-redos (main.py coord split),
#: 2 weak-sensitive-data-hashing (keys.py, redis_coord.py). A new one is a conscious act.
EXPECTED_THIS_LANE = 5


def _all_annotations():
    found = []
    for scope in SCOPES:
        base = ROOT / scope
        files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for f in files:
            posix = f.as_posix()
            if "/.codeql/" in posix or "/tests/" in posix or "/FaultLine/" in posix:
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            for i, line in enumerate(lines):
                if DETECT_RE.search(line):
                    found.append((f.relative_to(ROOT).as_posix(), i + 1, line,
                                  lines[i + 1] if i + 1 < len(lines) else ""))
    return found


def test_this_lane_suppression_count_is_pinned():
    mine = [e for e in _all_annotations() if any(f"codeql[{r}]" in e[2] for r in THIS_LANE_RULES)]
    assert len(mine) == EXPECTED_THIS_LANE, "\n".join(f"{f}:{n}" for f, n, _, _ in mine)


@pytest.mark.parametrize("entry", _all_annotations(), ids=lambda e: f"{e[0]}:{e[1]}")
def test_every_codeql_annotation_is_alone_on_its_line_above_code(entry):
    rel, n, line, next_line = entry
    if not line.lstrip().startswith("#"):
        pytest.fail(f"{rel}:{n}: codeql[] after code on the same line is INERT — move it to its own line above")
    m = ALONE_RE.match(line)
    assert m, f"{rel}:{n}: must be `# codeql[py/rule] <reason>` alone on the line"
    assert len(m.group(2).strip()) >= 40, f"{rel}:{n}: say WHY (source expression + why it is safe)"
    nxt = next_line.strip()
    assert nxt and not nxt.startswith("#"), f"{rel}:{n}: the line directly after must be the flagged code"
