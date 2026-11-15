"""A `# nosemgrep:` comment must never swallow the code that followed it.

Regression pin for the 2026-09-14 semgrep-suppression round (654d9382 … 3f250759): the tool that
appended suppression comments joined the NEXT source line onto the comment in five places —
`yield conn` (src/api/db_read.py::read_only_connection → every per-tenant overlay read failed
with "'NoneType' object is not an iterator" and fell back to seed-only floors), `return conn`
(src/ontology/canonical.py::_connect), `conn.commit()` (tools/cortex_orphan_reaper.py),
and the SQL f-string argument of two `cur.execute(` calls (src/fact_store/store.py::retract,
src/mcp/server.py::_known_entity_ids). CPython accepted every one of them; only the behaviour
changed. Signature of the join: the swallowed line's INDENTATION survives inside the comment as
a run of >= 4 spaces followed by code. Measured live on pre-prod (rel_type_overlay.tenant_fetch_failed).
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
ROOTS = ("src", "saas", "tools", "benchmarks", "scripts", "ops")
# comment text, then >=4 spaces, then something that is not another comment
JOIN = re.compile(r"#\s*nosemgrep:.*\S {4,}(?!#)\S")
# a suppression must name a namespaced rule id (semgrep docs: rule-ids referenced with their
# namespace) — bare `# nosemgrep` blanket-ignores every rule on the line
BARE = re.compile(r"#\s*nosemgrep\s*(?::\s*)?(?:$|[^a-z])")
NAMESPACED = re.compile(r"#\s*nosemgrep:\s*[a-z0-9_-]+(?:\.[a-z0-9_-]+)+")
# an actual suppression comment (not prose that merely mentions the word)
SUPPRESSION = re.compile(r"#\s*nosemgrep\b")


def _lines():
    for root in ROOTS:
        for p in (REPO / root).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if "nosemgrep" in line:
                    yield p.relative_to(REPO), i, line


def test_no_nosemgrep_comment_swallows_code():
    bad = [f"{p}:{i}: {line.strip()[:120]}" for p, i, line in _lines() if JOIN.search(line)]
    assert not bad, "nosemgrep comment joined with the following source line:\n" + "\n".join(bad)


def test_every_nosemgrep_names_a_namespaced_rule():
    bad = [f"{p}:{i}" for p, i, line in _lines()
           if SUPPRESSION.search(line) and not NAMESPACED.search(line)]
    assert not bad, "bare/unnamespaced nosemgrep (blanket ignore):\n" + "\n".join(bad)


def test_census_is_not_vacuous():
    assert sum(1 for _ in _lines()) > 50, "expected the suppression round's comments to be present"
