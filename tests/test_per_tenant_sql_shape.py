"""Two SQL-shape defects that cannot fail loudly enough to be noticed, pinned.

Both were live in `_normalize_entity_ids` (src/api/main.py) and neither could ever have
executed successfully. They sat undetected because the enclosing block catches Exception and
logs `startup.entity_id_normalization_failed`, and the branch only runs when there is legacy
string-id data to normalize — so on a healthy tenant the code is simply never reached.

DEFECT 1 — A PER-TENANT TABLE NAMED A `user_id` IT DOES NOT HAVE.
    INSERT INTO entities (id, entity_type) VALUES (%s, 'unknown')
    ON CONFLICT (id, user_id) DO NOTHING
The per-tenant `entities` table is `id TEXT NOT NULL PRIMARY KEY` plus
entity_type/node_role/created_at — there is no `user_id` column, because the bound per-tenant
`search_path` IS the tenant scope. `(id, user_id)` is the legacy `public` shape.

This is not a harmless redundancy. Naming an absent column raises, and inside a multi-statement
block a raise ABORTS THE TRANSACTION: `conn.commit()` is never reached, so every preceding
facts/staged_facts UPDATE is discarded and the run reports success-shaped failure having changed
nothing. This repo has already paid for this exact class once — an `entity_aliases.user_id`
probe aborted `/query` transactions and the symptom surfaced as "no facts found" on memories
that were sitting right there in the database.

DEFECT 2 — TWO PARAMETERS FOR ONE PLACEHOLDER.
The same call passed `(surrogate, user_id)` against a single `%s`, so psycopg2 raised while
formatting, before the statement was ever sent to the server.

Run: python3 tools/fltest.py --bug PERTENANT-SQL --test tests/test_per_tenant_sql_shape.py
"""

import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# The files that carry hand-written per-tenant SQL. Kept explicit (not a glob) so the scan is
# fast and so adding a file to it is a deliberate act.
SQL_HEAVY = [
    "src/api/main.py",
    "src/entity_registry/registry.py",
    "src/re_embedder/embedder.py",
    "src/api/canonicalize.py",
]


def _literal_sql(node):
    """The SQL string when the argument is a pure literal (or implicit concat), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_sql(node.left), _literal_sql(node.right)
        return None if left is None or right is None else left + right
    return None


def _mismatches(path):
    """(lineno, placeholders, params, sql) for every literal execute() whose counts disagree.

    Deliberately CONSERVATIVE — only inspects calls where BOTH the SQL and the parameter tuple
    are literals, and skips anything containing `%%` (a literal percent, e.g. a LIKE pattern),
    because a dynamic query cannot be checked statically and a false alarm here would train
    people to ignore the test.
    """
    tree = ast.parse((REPO / path).read_text())
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and len(node.args) == 2):
            continue
        sql = _literal_sql(node.args[0])
        if sql is None or "%%" in sql:
            continue
        if not isinstance(node.args[1], ast.Tuple):
            continue
        n_placeholders = sql.count("%s")
        n_params = len(node.args[1].elts)
        if n_placeholders != n_params:
            out.append((node.lineno, n_placeholders, n_params, " ".join(sql.split())[:90]))
    return out


@pytest.mark.parametrize("path", SQL_HEAVY)
def test_every_literal_execute_has_matching_placeholder_and_param_counts(path):
    """DEFECT 2. psycopg2 raises on a count mismatch — the statement never reaches the server."""
    bad = _mismatches(path)
    assert not bad, (
        f"{path}: literal execute() calls whose %s count != len(params):\n"
        + "\n".join(f"  line {ln}: placeholders={p} params={q} :: {s}" for ln, p, q, s in bad)
    )


def test_the_scanner_actually_catches_a_mismatch():
    """TEETH. A green scan is only evidence if the scanner can fail — prove it on a fixture.

    Without this, a scanner that silently matched nothing would look identical to a clean repo.
    """
    src = (
        "def f(cur, a, b):\n"
        "    cur.execute(\n"
        "        'INSERT INTO entities (id, entity_type) VALUES (%s, \\'unknown\\') '\n"
        "        'ON CONFLICT (id) DO NOTHING',\n"
        "        (a, b)\n"
        "    )\n"
    )
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute" and len(node.args) == 2):
            sql = _literal_sql(node.args[0])
            if sql and isinstance(node.args[1], ast.Tuple):
                if sql.count("%s") != len(node.args[1].elts):
                    found.append(node.lineno)
    assert found, "the scanner failed to flag a 1-placeholder/2-param call — it has no teeth"


# ── DEFECT 1: the per-tenant tables have no user_id, so no SQL may name one ──────────────

# Tables that are per-tenant and carry NO user_id column (the bound search_path is the scope).
# Verified against a live tenant schema and against src/provisioning/templates/user_schema.sql.
NO_USER_ID_TABLES = ("entities", "entity_aliases", "entity_name_conflicts")


def test_the_per_tenant_template_really_has_no_user_id_on_those_tables():
    """Ground the pin in the DDL rather than in a claim, so it stays true if the schema moves."""
    ddl = (REPO / "src/provisioning/templates/user_schema.sql").read_text()
    for table in NO_USER_ID_TABLES:
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);", ddl, re.S)
        assert m, f"could not locate the per-tenant DDL for {table}"
        body = m.group(1)
        columns = [ln.strip().split()[0].lower() for ln in body.splitlines()
                   if ln.strip() and not ln.strip().startswith("--")]
        assert "user_id" not in columns, (
            f"{table} now HAS a user_id column — this pin and the code comments that cite it "
            f"are out of date; re-verify before relaxing anything."
        )


@pytest.mark.parametrize("path", SQL_HEAVY)
def test_no_sql_names_user_id_in_a_conflict_target_for_a_per_tenant_table(path):
    """DEFECT 1. `ON CONFLICT (id, user_id)` on a per-tenant table raises and aborts the txn."""
    text = (REPO / path).read_text()
    offenders = [
        (i + 1, line.strip())
        for i, line in enumerate(text.splitlines())
        if re.search(r"ON\s+CONFLICT\s*\(\s*id\s*,\s*user_id\s*\)", line, re.I)
    ]
    assert not offenders, (
        f"{path}: ON CONFLICT names user_id, which the per-tenant tables "
        f"{NO_USER_ID_TABLES} do not have:\n"
        + "\n".join(f"  line {ln}: {s}" for ln, s in offenders)
    )
