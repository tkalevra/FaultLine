"""seed-030-first-boot — the alias seed must land on the FIRST boot, and a rejected seed must be loud.

THE WOUND (SB1, measured before the fix on a fresh throwaway, one migration pass):
    migrations/030_rel_type_aliases.sql seeds 75 alias rows (73 distinct) in ONE multi-row INSERT with an FK to
    rel_types(rel_type). `member_of` is named by two of those rows and INSERTed by no migration
    at all (only UPDATEd by 022/024/030_strengthen/031/081/089/096), so PostgreSQL rejects the
    whole statement (SQLSTATE 23503) — zero aliases land, the ledger records the file `failed`,
    and it only applies on boot #2 after src/api/main.py::_ensure_schema mints member_of/has_os/
    ip_address into public at app start. Every first boot, every single-pass throwaway and every
    tenant provisioned before boot #2 carried no married_to/spouse_of/husband_of/wife_of → spouse.

THE FIX (SB2, option (a) — seed ORDER, one owner per seeded rel):
    migrations/021_seed_code_referenced_rel_types.sql inserts the three rels the corpus assumes
    but never seeds, BEFORE 022 so the metadata UPDATEs the ledger stamps on boot #1 land on the
    rows. `_ensure_schema` keeps its net but now log_crit's if it ever has to insert.

WHAT THESE PINS DRIVE (nothing stubbed — real psql via run_boot_migrations, real
create_user_schema, the real `_ensure_schema` from src.api.main):
    * a PRISTINE corpus = the on-disk migrations dir minus the fix file, copied to a temp dir
      (the wound reproduced without touching the tree);
    * FRESH databases created beside the throwaway POSTGRES_DSN (`<db>_sb_pristine`,
      `<db>_sb_fixed`), dropped at teardown. Refuses the db named `faultline`.

Requires POSTGRES_DSN (a throwaway). Run through tools/fltest.py, never raw pytest.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import uuid
from typing import Dict, List, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

import pytest

import src.provisioning.boot_migrations as bm

_DSN = os.environ.get("POSTGRES_DSN")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MIG_DIR = os.path.join(_REPO, "migrations")
_FIX_FILE = "021_seed_code_referenced_rel_types.sql"
_ALIAS_FILE = "030_rel_type_aliases.sql"
_DIRECTIONALITY_FILE = "031_rel_type_directionality_metadata.sql"

pytestmark = pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN not set (needs a throwaway db)")


# ── helpers ───────────────────────────────────────────────────────────────────────────
def _db_name(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/")


def _with_db(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))


def _fresh_database(suffix: str) -> str:
    """CREATE a fresh database beside the throwaway and return its DSN. Never `faultline`."""
    import psycopg2

    base = _db_name(_DSN)
    assert base and base != "faultline", "refusing to derive throwaways from the real inventory db"
    name = f"{base}_{suffix}"[:63]
    assert name != "faultline"
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    return _with_db(_DSN, name)


def _drop_database(dsn: str) -> None:
    import psycopg2

    name = _db_name(dsn)
    if name == "faultline":  # belt and braces
        return
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        conn.close()


def _q(dsn: str, sql: str, params=None):
    import psycopg2

    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _ledger_status(dsn: str, migration_id: str) -> List[Tuple[str, str]]:
    return _q(dsn, "SELECT schema_name, status FROM public.schema_migration_status "
                   "WHERE migration_id = %s ORDER BY 1", (migration_id,))


def _alias_names(dsn: str) -> Set[str]:
    return {r[0] for r in _q(dsn, "SELECT alias FROM public.rel_type_aliases")}


def _alias_tuples(dsn: str, schema: str = "public") -> Set[tuple]:
    return set(_q(dsn, f"SELECT canonical_rel_type, alias, source, requires_inversion, "
                       f"is_symmetric, inverse_alias FROM {schema}.rel_type_aliases"))


def _rel_types(dsn: str) -> Set[str]:
    return {r[0] for r in _q(dsn, "SELECT rel_type FROM public.rel_types")}


def _corpus(tmp_root: str, *, without: Tuple[str, ...] = (), only_before: str = None) -> str:
    """Copy the on-disk migrations into a temp dir, optionally minus files / truncated before an id."""
    d = os.path.join(tmp_root, "migrations")
    os.makedirs(d, exist_ok=True)
    for name in sorted(os.listdir(_MIG_DIR)):
        if not name.endswith(".sql") or name in without:
            continue
        if only_before is not None and name >= only_before:
            continue
        shutil.copy(os.path.join(_MIG_DIR, name), os.path.join(d, name))
    return d


# ── SQL-parse the seed files (a census, not a spelling grep) ─────────────────────────
def _parse_insert_rows(path: str, table: str) -> List[Tuple[str, ...]]:
    """Every VALUES tuple of `INSERT INTO <table> (...) VALUES (...), (...)` in a migration
    file, read through sqlparse's token tree — the census walks the statement's `Values`
    group and its parenthesised rows (depth-tracked), it does not grep names."""
    import sqlparse
    from sqlparse import sql as S, tokens as T

    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    rows: List[Tuple[str, ...]] = []
    for stmt in sqlparse.parse(text):
        if stmt.get_type() != "INSERT":
            continue
        # the target: the first Name after INTO (schema-qualified or bare)
        names = [t.value.strip('"') for t in stmt.flatten() if t.ttype in T.Name]
        if table not in names:
            continue
        values = [t for t in stmt.tokens if isinstance(t, S.Values)]
        if not values:  # older sqlparse: the tuples sit at top level after the VALUES keyword
            values = [stmt]
        for node in values:
            depth, current, armed = 0, [], False
            for t in node.flatten():
                if t.ttype is T.Keyword and t.normalized == "VALUES":
                    armed = True
                    continue
                if t.ttype is T.Keyword and t.normalized == "ON":  # ON CONFLICT ... ends the rows
                    break
                if not armed:
                    continue
                if t.ttype is T.Punctuation and t.value == "(":
                    depth += 1
                    if depth == 1:
                        current = []
                    continue
                if t.ttype is T.Punctuation and t.value == ")":
                    depth -= 1
                    if depth == 0 and current:
                        rows.append(tuple(current))
                    continue
                if depth >= 1 and t.ttype in T.Literal.String.Single:
                    current.append(t.value.strip("'"))
    return rows


def _aliases_seeded_elsewhere(mig_dir: str) -> Set[str]:
    """Every alias some migration OTHER than 030 inserts into rel_type_aliases (094/099/121 …),
    parsed the same way — so '030-exclusive' is a measured set, not an assumption."""
    out: Set[str] = set()
    for name in sorted(os.listdir(mig_dir)):
        if not name.endswith(".sql") or name == _ALIAS_FILE:
            continue
        for row in _parse_insert_rows(os.path.join(mig_dir, name), "rel_type_aliases"):
            if len(row) >= 2:
                out.add(row[1])
    return out


def _parse_031_directionality(path: str) -> Dict[str, Tuple[bool, bool, str]]:
    """alias -> (requires_inversion, is_symmetric, inverse_alias) from 031's UPDATE statements."""
    import sqlparse
    from sqlparse import tokens as T

    out: Dict[str, Tuple[bool, bool, str]] = {}
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    for stmt in sqlparse.parse(text):
        if stmt.get_type() != "UPDATE":
            continue
        raw = str(stmt)
        if "rel_type_aliases" not in raw or "requires_inversion" not in raw:
            continue
        m_inv = re.search(r"requires_inversion\s*=\s*(TRUE|FALSE)", raw, re.I)
        m_sym = re.search(r"is_symmetric\s*=\s*(TRUE|FALSE)", raw, re.I)
        m_ali = re.search(r"inverse_alias\s*=\s*'([^']+)'", raw, re.I)
        m_in = re.search(r"alias\s+IN\s*\(([^)]*)\)", raw, re.I)
        if not (m_inv and m_sym and m_ali and m_in):
            continue
        for alias in re.findall(r"'([^']+)'", m_in.group(1)):
            out[alias] = (m_inv.group(1).upper() == "TRUE", m_sym.group(1).upper() == "TRUE", m_ali.group(1))
    return out


def _ensure_schema_seed_list() -> List[str]:
    """The `_MISSING_TYPES` literal inside main.py::_ensure_schema, read by AST (never a regex)."""
    with open(os.path.join(_REPO, "src", "api", "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_ensure_schema":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "_MISSING_TYPES" for t in sub.targets):
                    return [row[0] for row in ast.literal_eval(sub.value)]
    raise AssertionError("_ensure_schema._MISSING_TYPES not found")


# ── fixtures: the two driven databases ────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pristine(tmp_path_factory):
    """PRISTINE corpus (fix file removed), boot #1 → real _ensure_schema → boot #2. Yields the
    state captured after each stage."""
    corpus = _corpus(str(tmp_path_factory.mktemp("pristine")), without=(_FIX_FILE,))
    assert not os.path.exists(os.path.join(corpus, _FIX_FILE))
    dsn = _fresh_database("sb_pristine")
    state: Dict[str, object] = {"dsn": dsn, "corpus": corpus}
    try:
        state["boot1"] = bm.run_boot_migrations(dsn, corpus, echo=False)
        state["boot1_ledger_030"] = _ledger_status(dsn, "030_rel_type_aliases")
        state["boot1_aliases"] = _alias_names(dsn)
        state["boot1_rel_types"] = _rel_types(dsn)
        # boot #2's precondition on the pristine path is APP START: the real runtime seed.
        from src.api.main import _ensure_schema
        _ensure_schema(dsn)
        state["after_ensure_rel_types"] = _rel_types(dsn)
        state["boot2"] = bm.run_boot_migrations(dsn, corpus, echo=False)
        state["boot2_ledger_030"] = _ledger_status(dsn, "030_rel_type_aliases")
        state["boot2_aliases"] = _alias_names(dsn)
        state["boot2_tuples"] = _alias_tuples(dsn)
        yield state
    finally:
        _drop_database(dsn)


@pytest.fixture(scope="module")
def fixed():
    """The REAL on-disk corpus (with the fix), boot #1 only; boot #2 and a tenant mint later."""
    dsn = _fresh_database("sb_fixed")
    state: Dict[str, object] = {"dsn": dsn}
    try:
        state["boot1"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        state["boot1_ledger_030"] = _ledger_status(dsn, "030_rel_type_aliases")
        state["boot1_ledger_021"] = _ledger_status(dsn, "021_seed_code_referenced_rel_types")
        state["boot1_aliases"] = _alias_names(dsn)
        state["boot1_tuples"] = _alias_tuples(dsn)
        state["boot1_rel_types"] = _rel_types(dsn)
        yield state
    finally:
        _drop_database(dsn)


# ── SB1: the wound, reproduced and then papered over by boot #2 ───────────────────────
def test_sb1_pristine_first_boot_rejects_030_and_boot2_applies_it(pristine):
    s = pristine
    rows_030 = _parse_insert_rows(os.path.join(s["corpus"], _ALIAS_FILE), "rel_type_aliases")
    assert len(rows_030) == 75, f"030 parse must see its 75 VALUES rows (73 distinct aliases), saw {len(rows_030)}"
    aliases_030 = {r[1] for r in rows_030}
    exclusive_030 = aliases_030 - _aliases_seeded_elsewhere(s["corpus"])
    assert {"married_to", "spouse_of", "husband_of", "wife_of"} <= exclusive_030
    assert len(exclusive_030) >= 60, len(exclusive_030)

    # boot #1: rejected, recorded failed (NEVER applied), zero of 030's own rows present
    assert s["boot1_ledger_030"] == [("public", "failed")], s["boot1_ledger_030"]
    assert "030_rel_type_aliases" in [m for m, _n in s["boot1"].errored]
    assert exclusive_030 & s["boot1_aliases"] == set(), "030 landed rows on a pristine first boot?"
    assert "member_of" not in s["boot1_rel_types"], "no migration inserts member_of on the pristine corpus"
    assert "married_to" not in s["boot1_aliases"]

    # app start mints the FK target at runtime; boot #2 then applies the file
    assert {"member_of", "has_os", "ip_address"} <= s["after_ensure_rel_types"]
    assert s["boot2_ledger_030"] == [("public", "applied")], s["boot2_ledger_030"]
    assert aliases_030 <= s["boot2_aliases"]
    assert {"married_to", "spouse_of", "husband_of", "wife_of"} <= s["boot2_aliases"]


def test_sb1_pristine_boot2_has_lost_031s_directionality_forever(pristine):
    """The second, quieter loss: 031's directionality UPDATEs were stamped `applied` on boot #1
    with no rows to hit, so the twice-booted pristine box carries married_to → spouse with
    is_symmetric=false / inverse_alias NULL. Pinned so nobody 'fixes' SB3 by matching this set."""
    want = _parse_031_directionality(os.path.join(pristine["corpus"], _DIRECTIONALITY_FILE))
    assert "married_to" in want and want["married_to"][1] is True  # 031 says symmetric
    got = {t[1]: (t[3], t[4], t[5]) for t in pristine["boot2_tuples"]}
    assert got["married_to"] == (False, False, None), got["married_to"]


# ── SB4: loud, structured, never applied ──────────────────────────────────────────────
def test_sb4_rejected_migration_is_diagnosed_with_sqlstate_and_fk_target(pristine):
    diag = pristine["boot1"].diagnoses.get("030_rel_type_aliases")
    assert diag, "a rejected file must carry a structured diagnosis on the BootSummary"
    d = diag[0]
    assert d["sqlstate"] == "23503", d  # foreign_key_violation (PostgreSQL Appendix A)
    assert d["missing_fk_target"] == "rel_types.canonical_rel_type=member_of", d
    assert "violates foreign key constraint" in (d["message"] or "")
    assert d["line"] and d["line"].endswith(f"{_ALIAS_FILE}:96"), d


def test_sb4_rejected_statement_logs_crit(pristine, tmp_path, monkeypatch):
    """The CRIT record is emitted per rejected statement, through the same log_crit sink main.py
    uses, with migration_id + sqlstate + missing_fk_target. Driven on the real psql output."""
    calls: List[dict] = []
    monkeypatch.setattr(bm, "_log_crit", lambda logger, msg, **kw: calls.append({"msg": msg, **kw}))
    monkeypatch.setattr(bm, "_log", object())
    dsn = _fresh_database("sb_crit")
    try:
        corpus = _corpus(str(tmp_path), without=(_FIX_FILE,), only_before="031_")
        bm.run_boot_migrations(dsn, corpus, echo=False)
    finally:
        _drop_database(dsn)
    hits = [c for c in calls if c["migration_id"] == "030_rel_type_aliases"]
    assert hits, [c.get("migration_id") for c in calls]
    assert hits[0]["msg"] == "boot_migrations.statement_rejected"
    assert hits[0]["sqlstate"] == "23503"
    assert hits[0]["missing_fk_target"] == "rel_types.canonical_rel_type=member_of"


def test_sb4_run_psql_asks_for_verbose_error_reports(monkeypatch):
    seen: List[List[str]] = []

    class _P:
        stdout = ""
        stderr = ""

    monkeypatch.setattr(bm.subprocess, "run", lambda argv, **kw: seen.append(argv) or _P())
    bm._run_psql("postgresql://x/y", "/tmp/z.sql")
    assert seen and seen[0][:2] == ["psql", "postgresql://x/y"]
    assert "-v" in seen[0] and "VERBOSITY=verbose" in seen[0], seen[0]
    assert seen[0][-2:] == ["-f", "/tmp/z.sql"]


def test_sb4_diagnose_errors_reads_both_verbose_and_legacy_shapes():
    verbose = (
        'psql:/app/migrations/030_rel_type_aliases.sql:96: ERROR:  23503: insert or update on table '
        '"rel_type_aliases" violates foreign key constraint "rel_type_aliases_canonical_rel_type_fkey"\n'
        'DETAIL:  Key (canonical_rel_type)=(member_of) is not present in table "rel_types".\n'
        'SCHEMA NAME:  public\nTABLE NAME:  rel_type_aliases\n'
        'CONSTRAINT NAME:  rel_type_aliases_canonical_rel_type_fkey\nLOCATION:  ri_ReportViolation, ri_triggers.c:2608\n'
    )
    [d] = bm.diagnose_errors(verbose)
    assert d == {"sqlstate": "23503",
                 "message": 'insert or update on table "rel_type_aliases" violates foreign key '
                            'constraint "rel_type_aliases_canonical_rel_type_fkey"',
                 "line": "psql:/app/migrations/030_rel_type_aliases.sql:96",
                 "missing_fk_target": "rel_types.canonical_rel_type=member_of"}
    legacy = 'psql:x.sql:3: ERROR:  column "fact_provenance" does not exist\n'
    [d2] = bm.diagnose_errors(legacy)
    assert d2["sqlstate"] is None and d2["missing_fk_target"] is None
    assert d2["message"] == 'column "fact_provenance" does not exist'
    assert bm.diagnose_errors("CREATE TABLE\nINSERT 0 3\n") == []


# ── SB2: seed order/content + the census ──────────────────────────────────────────────
def test_sb2_census_every_030_canonical_is_seeded_by_a_preceding_migration(tmp_path):
    """Run ONLY the migrations that sort before 030_rel_type_aliases.sql on a fresh db, then
    require every canonical_rel_type 030's INSERT names to exist. SQL-parsed, driven."""
    rows_030 = _parse_insert_rows(os.path.join(_MIG_DIR, _ALIAS_FILE), "rel_type_aliases")
    canonicals = {r[0] for r in rows_030}
    assert len(rows_030) == 75 and "member_of" in canonicals
    dsn = _fresh_database("sb_census")
    try:
        prefix = _corpus(str(tmp_path), only_before=_ALIAS_FILE)
        assert _FIX_FILE in os.listdir(prefix) and _ALIAS_FILE not in os.listdir(prefix)
        bm.run_boot_migrations(dsn, prefix, echo=False)
        present = _rel_types(dsn)
    finally:
        _drop_database(dsn)
    missing = sorted(canonicals - present)
    assert not missing, f"030 FK-depends on rel_types no preceding migration seeds: {missing}"


def test_sb2_one_owner_every_runtime_seeded_rel_is_seeded_by_a_migration(fixed):
    """_ensure_schema's `_MISSING_TYPES` (read by AST) must ALL exist after one migration pass —
    the corpus is the owner; the runtime loop is a net that finds nothing to do."""
    seeds = _ensure_schema_seed_list()
    assert {"member_of", "has_os", "ip_address"} <= set(seeds)
    missing = sorted(set(seeds) - fixed["boot1_rel_types"])
    assert not missing, f"runtime-only owners remain: {missing}"
    rows_021 = _parse_insert_rows(os.path.join(_MIG_DIR, _FIX_FILE), "rel_types")
    assert {r[0] for r in rows_021} == {"member_of", "has_os", "ip_address"}


def test_sb2_ensure_schema_finds_nothing_to_do_on_a_migrated_db(fixed, monkeypatch):
    import src.api.main as main

    crit: List[dict] = []
    monkeypatch.setattr(main, "log_crit", lambda logger, msg, **kw: crit.append({"msg": msg, **kw}))
    before = _q(fixed["dsn"], "SELECT count(*) FROM public.rel_types")[0][0]
    main._ensure_schema(fixed["dsn"])
    after = _q(fixed["dsn"], "SELECT count(*) FROM public.rel_types")[0][0]
    assert after == before, "the runtime net inserted a rel on a migrated database"
    assert not [c for c in crit if c["msg"] == "startup.rel_type_seeded_at_runtime"], crit


def test_sb2_ensure_schema_is_loud_when_the_seed_has_a_gap(fixed, monkeypatch):
    """Anti-vacuity for the pin above: delete a seeded rel, the net re-inserts it AND CRITs."""
    import psycopg2
    import src.api.main as main

    crit: List[dict] = []
    monkeypatch.setattr(main, "log_crit", lambda logger, msg, **kw: crit.append({"msg": msg, **kw}))
    with psycopg2.connect(fixed["dsn"]) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM public.rel_types WHERE rel_type = 'has_os'")
    main._ensure_schema(fixed["dsn"])
    assert "has_os" in _rel_types(fixed["dsn"])
    hits = [c for c in crit if c["msg"] == "startup.rel_type_seeded_at_runtime"]
    assert hits and hits[0]["rel_types"] == ["has_os"], crit


# ── SB3: first-boot parity ────────────────────────────────────────────────────────────
def test_sb3_one_pass_lands_030_and_matches_the_boot2_alias_set(pristine, fixed):
    f = fixed
    assert f["boot1_ledger_021"] == [("public", "applied")], f["boot1_ledger_021"]
    assert f["boot1_ledger_030"] == [("public", "applied")], f["boot1_ledger_030"]
    assert "030_rel_type_aliases" not in [m for m, _n in f["boot1"].errored]
    assert {"member_of", "has_os", "ip_address"} <= f["boot1_rel_types"]
    # count + set diff = ∅ against the pristine boot-#2 alias set
    assert len(f["boot1_aliases"]) == len(pristine["boot2_aliases"])
    assert f["boot1_aliases"] ^ pristine["boot2_aliases"] == set()
    assert {"married_to", "spouse_of", "husband_of", "wife_of"} <= f["boot1_aliases"]


def test_sb3_first_boot_carries_031s_directionality_the_pristine_path_lost(fixed):
    want = _parse_031_directionality(os.path.join(_MIG_DIR, _DIRECTIONALITY_FILE))
    seeded = {r[1] for r in _parse_insert_rows(os.path.join(_MIG_DIR, _ALIAS_FILE), "rel_type_aliases")}
    # 031 also names 'child_of' in an alias IN-list; no seed ever inserts that alias, so the
    # contract is: every alias 031 addresses THAT 030 SEEDS carries 031's flags.
    want = {a: w for a, w in want.items() if a in seeded}
    assert len(want) >= 12
    got = {t[1]: (t[3], t[4], t[5]) for t in fixed["boot1_tuples"]}
    wrong = {a: (got.get(a), w) for a, w in want.items() if got.get(a) != w}
    assert not wrong, wrong
    assert got["married_to"] == (False, True, "spouse_of")


def test_sb3_the_only_tuple_divergence_from_pristine_boot2_is_lost_metadata(pristine, fixed):
    """Every (canonical, alias) pair agrees except `parent`, where the pristine path let 094
    (later) win over 030 (earlier) because 030 physically ran after it; on the fixed path the
    corpus order — the order every pre-ledger box applied — decides. Everything else that
    differs is 031 metadata the pristine path lost (pinned above)."""
    pairs_fixed = {(t[0], t[1]) for t in fixed["boot1_tuples"]}
    pairs_pristine = {(t[0], t[1]) for t in pristine["boot2_tuples"]}
    diff_aliases = {a for _c, a in pairs_fixed ^ pairs_pristine}
    assert diff_aliases == {"parent"}, diff_aliases
    assert ("parent_of", "parent") in pairs_fixed and ("child_of", "parent") in pairs_pristine


def test_sb3_second_boot_is_a_no_op_for_the_seed(fixed):
    boot2 = bm.run_boot_migrations(fixed["dsn"], _MIG_DIR, echo=False)
    for mid in ("021_seed_code_referenced_rel_types", "030_rel_type_aliases",
                "031_rel_type_directionality_metadata", "094_per_tenant_rel_type_aliases"):
        assert mid in boot2.skipped, f"{mid} re-ran on boot #2"
    assert _alias_tuples(fixed["dsn"]) == fixed["boot1_tuples"]


def test_sb3_tenant_provisioned_after_first_boot_carries_the_per_tenant_copy(fixed, monkeypatch):
    """Driven through the real provisioner: the tenant's rel_type_aliases (094's per-tenant
    copy, seeded at mint from public) equals public's — married_to → spouse included."""
    import psycopg2

    from src.provisioning.schema_manager import create_user_schema

    monkeypatch.setenv("POSTGRES_DSN", fixed["dsn"])
    monkeypatch.setenv("FAULTLINE_MIGRATIONS_DIR", _MIG_DIR)
    user_id = str(uuid.uuid4())
    schema, status = create_user_schema(user_id, user_id.replace("-", "_"))
    assert status == "ready", status
    try:
        tenant = _alias_tuples(fixed["dsn"], schema)
        assert tenant == _alias_tuples(fixed["dsn"], "public")
        assert len(tenant) == len(fixed["boot1_aliases"])
        by_alias = {t[1]: t for t in tenant}
        assert by_alias["married_to"][0] == "spouse" and by_alias["married_to"][4] is True
        with psycopg2.connect(fixed["dsn"]) as conn, conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("SELECT rel_type, is_hierarchy_rel, natural_language FROM rel_types "
                        "WHERE rel_type IN ('member_of','has_os','ip_address') ORDER BY 1")
            rows = cur.fetchall()
        assert [r[0] for r in rows] == ["has_os", "ip_address", "member_of"]
        member = [r for r in rows if r[0] == "member_of"][0]
        assert member[1] is True and member[2] == "X is a member of Y", member  # 022 + 031 landed
    finally:
        conn = psycopg2.connect(fixed["dsn"])
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute("DELETE FROM public.schema_migration_status WHERE schema_name = %s", (schema,))
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
        conn.close()
