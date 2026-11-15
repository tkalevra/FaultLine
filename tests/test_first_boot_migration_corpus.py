"""first-boot-migration-corpus — every migration applies on the FIRST boot of a fresh database.

THE WOUND (FB1, measured on a fresh throwaway, one pass over the corpus at 75a55f7e):
    six files were rejected by PostgreSQL and recorded `failed` by the boot ledger —
      003_confirmation_source        42P01  ALTERs staged_facts, which 012 creates nine files later
      016_builtin_scalar_rel_types   42703  names head_types, which 017 renames into being one file later
      060_atomic_scalar_patterns     23514  INSERTs rel_types with source='bootstrap' — never admitted
      073_provenance_cleanup         42703  UPDATEs public.staged_facts.fact_provenance — never exists
      089_wire_hierarchy_flags       42703  UPDATEs rel_types.has_transitivity — an entity_taxonomies column
      110_has_state_scalar_predicate 23514  storage_target='entity_attributes' — never admitted
    Three of them (003, 016, 110) heal on boot #2 — and the healing re-runs the failed file ALONE,
    after everything it should have preceded, so 016's ON CONFLICT DO UPDATE then clobbers what
    024/030/033/101/129 refined (measured: age.head_types {ANY} → {Person}). The other three
    (060, 073, 089) fail on EVERY boot of EVERY box and re-run every start: 089's tenant fan-out
    never lands at all (the DO block rolls back), 073 re-executes its whole fan-out each boot,
    and 060's re-run re-INSERTs the has_phone / has_port regexes that 184 / 198 had corrected —
    both live again beside the corrected rows (the clock-time false positive 198 fixed included).
    Plus: 030_strengthen's five hierarchy UPDATEs were dead on every path (guarded on
    `is_hierarchy_rel = false`, which 022 sets TRUE eight files earlier).

THE FIX (FB2): each at its cause — 003 → 013 (after 012); 016 → 017_seed_… (after 017's rename,
DO UPDATE guarded to the untyped-row convention so the one-time re-run on a migrated box is a
no-op); 073's block 1b guards the column like its own block 1f; 089 and 110 drop the statements
that target objects / values that never existed; 060 drops its dead INSERT, 275 seeds the one rel
it alone carried (has_port), 276 removes the resurrected pattern rows; 030_strengthen's dead
UPDATEs are deleted with the reason (making them live would switch on a never-enforced type
constraint on the L4 ladder — an ingest decision, not a boot-order repair).

WHAT THESE PINS DRIVE (nothing stubbed): real `run_boot_migrations` (psql per file, real
ledger) over FRESH databases created beside the throwaway POSTGRES_DSN; the PRISTINE corpus is
`git archive 75a55f7e migrations` (the commit before this lane — stable however HEAD moves);
the static census (tests/migration_census.py) parses every file with sqlparse.
Requires POSTGRES_DSN (a throwaway, never `faultline`). Run through tools/fltest.py.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

import pytest

import src.provisioning.boot_migrations as bm

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import migration_census as census  # noqa: E402

_DSN = os.environ.get("POSTGRES_DSN")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# FB_MIGRATIONS_DIR lets the revert-red run point the "fixed" pins at another corpus.
_MIG_DIR = os.environ.get("FB_MIGRATIONS_DIR") or os.path.join(_REPO, "migrations")
_PRISTINE_SHA = "75a55f7e"  # saas HEAD before this lane: the six-file wound as shipped

pytestmark = pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN not set (needs a throwaway db)")

_SIX = {
    "003_confirmation_source": {"42P01"},
    "016_builtin_scalar_rel_types": {"42703"},
    "060_atomic_scalar_patterns": {"23514"},
    "073_provenance_cleanup": {"42703"},
    "089_wire_hierarchy_flags": {"42703"},
    "110_has_state_scalar_predicate": {"23514"},
}
_SIX_OBJECTS = {
    "003_confirmation_source": 'relation "staged_facts" does not exist',
    "016_builtin_scalar_rel_types": 'column "head_types" of relation "rel_types" does not exist',
    "060_atomic_scalar_patterns": 'violates check constraint "rel_types_source_check"',
    "073_provenance_cleanup": 'column "fact_provenance" does not exist',
    "089_wire_hierarchy_flags": 'column "has_transitivity" of relation "rel_types" does not exist',
    "110_has_state_scalar_predicate": 'violates check constraint "check_storage_target"',
}
_EVERY_BOOT = {"060_atomic_scalar_patterns", "073_provenance_cleanup", "089_wire_hierarchy_flags"}
_TOUCHED = {  # the files this lane changed, by their migration id in the FIXED corpus
    "013_confirmation_source", "017_seed_builtin_scalar_rel_types",
    "053_add_provisioning_heartbeat", "058_extraction_patterns",
    "084_cleanup_synthetic_related_to", "184_phone_atomic_pattern_nanp",
    "030_strengthen_rel_types_wikidata_ontology", "060_atomic_scalar_patterns",
    "073_provenance_cleanup", "089_wire_hierarchy_flags", "110_has_state_scalar_predicate",
    "275_seed_has_port_rel_type", "276_retire_resurrected_060_pattern_rows",
}
_OLD_PORT_RE = r"(?:port\s+|:)([1-9]\d{0,4})\b"
_OLD_PHONE_RE = r"\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}\b"
_META_TABLES = ("rel_types", "rel_type_aliases", "extraction_patterns", "entity_taxonomies")
_NEW_PHONE_RE = r"(?<!\d)(?:\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}|\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})\b"


# ── db helpers ────────────────────────────────────────────────────────────────────────
def _db_name(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/")


def _with_db(dsn: str, name: str) -> str:
    p = urlsplit(dsn)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


# Per-process database names: under `pytest -n N` this module's tests can be spread over xdist
# workers, and each worker's module fixtures DROP/CREATE the same derived names - measured as
# pg_database_datname_index UniqueViolation / "database ... does not exist" (#44, green alone).
_WORKER_SUFFIX = "_" + (os.environ.get("PYTEST_XDIST_WORKER") or "solo")


def _fresh_database(suffix: str) -> str:
    import psycopg2

    base = _db_name(_DSN)
    assert base and base != "faultline", "refusing to derive throwaways from the real inventory db"
    name = f"{base}_{suffix}{_WORKER_SUFFIX}"[:63]
    assert name != "faultline"
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    return _with_db(_DSN, name)


def _clone_database(src_dsn: str, suffix: str) -> str:
    """CREATE DATABASE <base>_<suffix> TEMPLATE <src> — a byte-identical copy of a prepared box,
    so a MUTANT corpus can be driven over the same starting state as the real one."""
    import psycopg2

    base = _db_name(_DSN)
    name = f"{base}_{suffix}{_WORKER_SUFFIX}"[:63]
    assert name != "faultline"
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}" TEMPLATE "{_db_name(src_dsn)}"')
    finally:
        conn.close()
    return _with_db(_DSN, name)


def _drop_database(dsn: str) -> None:
    import psycopg2

    name = _db_name(dsn)
    if name == "faultline":
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

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()  # a lingering session blocks CREATE DATABASE ... TEMPLATE on this db


def _ledger_not_applied(dsn: str) -> Dict[str, str]:
    return {m: s for m, s in _q(dsn, "SELECT migration_id, status FROM public.schema_migration_status "
                                     "WHERE status <> 'applied' ORDER BY 1")}


def _columns(dsn: str, table: str) -> List[str]:
    return [r[0] for r in _q(dsn, "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                                  "AND table_name=%s ORDER BY ordinal_position", (table,))]


def _snapshot(dsn: str) -> Dict[str, Set[tuple]]:
    """Full-row tuple sets of the public metadata tables the six files touch. Volatile columns are
    dropped (ids, timestamps); `value_distribution.last_updated` (033 stamps NOW()) is normalized."""
    snap: Dict[str, Set[tuple]] = {}
    for t in _META_TABLES:
        cols = [c for c in _columns(dsn, t) if c not in ("id", "created_at", "updated_at", "last_matched_at")]
        rows = _q(dsn, "SELECT " + ", ".join(cols) + f" FROM public.{t}")
        out = set()
        for r in rows:
            cells = []
            for c, v in zip(cols, r):
                if c == "value_distribution" and isinstance(v, dict):
                    v = {k: x for k, x in v.items() if k != "last_updated"}
                cells.append((c, json.dumps(v, sort_keys=True, default=str)))
            out.add(tuple(cells))
        snap[t] = out
    return snap


def _by_key(rows: Set[tuple], key: str) -> Dict[str, dict]:
    """key VALUE (decoded) -> {column: json-encoded value}."""
    return {json.loads(dict(r)[key]): dict(r) for r in rows}


def _functions(dsn: str) -> Set[str]:
    return {r[0] for r in _q(dsn, "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                                  "WHERE n.nspname = 'public'")}


def _tables(dsn: str) -> Set[str]:
    return {r[0] for r in _q(dsn, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}


# ── corpora ───────────────────────────────────────────────────────────────────────────
def _pristine_corpus(tmp_root: str) -> str:
    """The corpus as it stood at _PRISTINE_SHA, from git — never the working tree."""
    out = os.path.join(tmp_root, "pristine")
    os.makedirs(out, exist_ok=True)
    try:
        archive = subprocess.run(["git", "-C", _REPO, "archive", _PRISTINE_SHA, "migrations"],
                                 capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip(f"git archive {_PRISTINE_SHA} unavailable: {exc}")
    subprocess.run(["tar", "-x", "-C", out], input=archive, check=True)
    d = os.path.join(out, "migrations")
    assert os.path.exists(os.path.join(d, "003_confirmation_source.sql"))
    assert not os.path.exists(os.path.join(d, "275_seed_has_port_rel_type.sql"))
    return d


def _copy_corpus(src: str, tmp_root: str, name: str) -> str:
    d = os.path.join(tmp_root, name)
    shutil.copytree(src, d)
    return d


# ── fixtures ──────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pristine_dir(tmp_path_factory):
    return _pristine_corpus(str(tmp_path_factory.mktemp("corpus")))


@pytest.fixture(scope="module")
def pristine(pristine_dir):
    """PRISTINE corpus, fresh db: boot #1 (the wound), boots #2 and #3 (what a many-boot box
    carries), then the FIXED corpus over that migrated box three times (FB2 harmlessness)."""
    dsn = _fresh_database("fb_pristine")
    s: Dict[str, object] = {"dsn": dsn}
    try:
        s["boot1"] = bm.run_boot_migrations(dsn, pristine_dir, echo=False)
        s["boot1_not_applied"] = _ledger_not_applied(dsn)
        s["boot2"] = bm.run_boot_migrations(dsn, pristine_dir, echo=False)
        s["boot2_not_applied"] = _ledger_not_applied(dsn)
        s["boot3"] = bm.run_boot_migrations(dsn, pristine_dir, echo=False)
        s["boot3_not_applied"] = _ledger_not_applied(dsn)
        s["old3_snapshot"] = _snapshot(dsn)
        s["old3_functions"] = _functions(dsn)
        s["old3_hierarchy"] = _q(dsn, "SELECT rel_type, head_types, tail_types, is_hierarchy_rel FROM public.rel_types "
                                      "WHERE rel_type IN ('instance_of','subclass_of','part_of','is_a','member_of') ORDER BY 1")
        # the migrated box meets the FIXED corpus
        s["fix_boot"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        s["fix_not_applied"] = _ledger_not_applied(dsn)
        s["fixed_on_old_snapshot"] = _snapshot(dsn)
        s["fix_boot2"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        s["fix_boot3"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        s["fixed_on_old_snapshot3"] = _snapshot(dsn)
        yield s
    finally:
        _drop_database(dsn)


@pytest.fixture(scope="module")
def legacy_box(pristine_dir, monkeypatch_module):
    """The PRODUCTION shape: a box migrated by the pre-ledger sweep (every file, in order — 129's
    age → {ANY} lands AFTER 016's clobber), then its first LEDGER boot (stamps everything, so a
    later new migration id runs ALONE). Then the fixed corpus, once. A TEMPLATE clone of the
    pre-fix state carries a MUTANT corpus (017_seed with its guard removed) for the anti-vacuity
    half of the guard pin."""
    dsn = _fresh_database("fb_legacy")
    s: Dict[str, object] = {"dsn": dsn}
    clone = None
    try:
        monkeypatch_module.setenv("FAULTLINE_MIGRATION_LEDGER", "false")
        s["legacy1"] = bm.run_boot_migrations(dsn, pristine_dir, echo=False)
        monkeypatch_module.delenv("FAULTLINE_MIGRATION_LEDGER", raising=False)
        s["ledger1"] = bm.run_boot_migrations(dsn, pristine_dir, echo=False)
        s["ledger1_not_applied"] = _ledger_not_applied(dsn)
        s["before"] = _snapshot(dsn)
        clone = _clone_database(dsn, "fb_legacy_mut")
        s["fix_boot"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        s["fix_not_applied"] = _ledger_not_applied(dsn)
        s["after"] = _snapshot(dsn)
        s["fix_boot2"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        s["fix_not_applied2"] = _ledger_not_applied(dsn)
        s["fix_boot3"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        s["after3"] = _snapshot(dsn)
        # MUTANT: the same corpus with 017_seed's skeleton guard deleted
        mut = _copy_corpus(_MIG_DIR, os.path.dirname(pristine_dir), "mutant_unguarded_017")
        p017 = os.path.join(mut, "017_seed_builtin_scalar_rel_types.sql")
        text = open(p017).read()
        assert "WHERE rel_types.head_types IS NULL OR rel_types.head_types = '{}'" in text
        open(p017, "w").write(text.replace("\nWHERE rel_types.head_types IS NULL OR rel_types.head_types = '{}';", ";"))
        s["mutant_boot"] = bm.run_boot_migrations(clone, mut, echo=False)
        s["mutant_after"] = _snapshot(clone)
        yield s
    finally:
        _drop_database(dsn)
        if clone:
            _drop_database(clone)


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def fixed():
    """FIXED corpus (the on-disk migrations), fresh db: ONE pass, then two more."""
    dsn = _fresh_database("fb_fixed")
    s: Dict[str, object] = {"dsn": dsn}
    try:
        s["boot1"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        s["boot1_not_applied"] = _ledger_not_applied(dsn)
        s["boot1_snapshot"] = _snapshot(dsn)
        s["boot1_functions"] = _functions(dsn)
        s["boot1_tables"] = _tables(dsn)
        s["boot1_staged_cols"] = _columns(dsn, "staged_facts")
        s["boot2"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        s["boot3"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        s["boot3_snapshot"] = _snapshot(dsn)
        yield s
    finally:
        _drop_database(dsn)


# ── FB1: the exact failed set, SQLSTATE + object, on a pristine first boot ────────────
def test_fb1_pristine_first_boot_rejects_exactly_six_files_with_these_sqlstates(pristine):
    s = pristine
    assert set(s["boot1_not_applied"]) == set(_SIX), s["boot1_not_applied"]
    assert all(v == "failed" for v in s["boot1_not_applied"].values())
    errored = {m for m, _n in s["boot1"].errored}
    assert errored == set(_SIX), errored
    for mid, states in _SIX.items():
        diag = s["boot1"].diagnoses.get(mid) or []
        assert diag, f"{mid}: no structured diagnosis"
        assert {d["sqlstate"] for d in diag} == states, (mid, diag)
        assert any(_SIX_OBJECTS[mid] in (d["message"] or "") for d in diag), (mid, diag)
    # per-statement counts measured on the pristine corpus
    assert len(s["boot1"].diagnoses["003_confirmation_source"]) == 3
    assert len(s["boot1"].diagnoses["089_wire_hierarchy_flags"]) == 2


def test_fb1_three_of_six_fail_on_every_boot_of_every_box(pristine):
    """060 / 073 / 089 are not first-boot wounds: their targets never exist on any path, so the
    pristine box re-runs them on boot #2 AND boot #3 and never stamps them applied."""
    s = pristine
    assert set(s["boot2_not_applied"]) == _EVERY_BOOT, s["boot2_not_applied"]
    assert set(s["boot3_not_applied"]) == _EVERY_BOOT, s["boot3_not_applied"]
    assert set(s["boot3"].ran) == _EVERY_BOOT


def test_fb1_pristine_rerun_of_060_resurrects_the_patterns_184_and_198_corrected(pristine):
    pats = _by_key_multi(pristine["old3_snapshot"]["extraction_patterns"], "rel_type")
    port = {r["pattern_regex"] for r in pats["has_port"]}
    phone = {r["pattern_regex"] for r in pats["has_phone"]}
    assert _OLD_PORT_RE in port and len(port) == 2, port
    assert _OLD_PHONE_RE in phone and len(phone) == 2, phone
    assert all(r["is_active"] is True for r in pats["has_port"] + pats["has_phone"])
    # and has_port, the one rel 060 alone seeded, never existed
    assert "has_port" not in _by_key(pristine["old3_snapshot"]["rel_types"], "rel_type")


def _by_key_multi(rows: Set[tuple], key: str) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(json.loads(d[key]), []).append({k: json.loads(v) for k, v in d.items()})
    return out


def test_fb1_030_strengthen_hierarchy_updates_were_dead_on_every_path(pristine, pristine_dir):
    """The pristine file CARRIES typed values for the five hierarchy rels behind a guard 022
    makes unsatisfiable; after three boots every one of them is still untyped."""
    text = open(os.path.join(pristine_dir, "030_strengthen_rel_types_wikidata_ontology.sql")).read()
    guarded = re.findall(r"WHERE rel_type = '(\w+)' AND \(is_hierarchy_rel IS NULL OR is_hierarchy_rel = false\)", text)
    assert set(guarded) == {"instance_of", "subclass_of", "part_of", "is_a", "member_of"}, guarded
    for rel, head, tail, hier in pristine["old3_hierarchy"]:
        assert head is None and tail is None and hier is True, (rel, head, tail, hier)


# ── FB2: the fix at each cause; idempotent on a three-boot box ─────────────────────────
def test_fb2_fixed_corpus_first_boot_has_zero_failed_files(fixed):
    assert fixed["boot1_not_applied"] == {}, fixed["boot1_not_applied"]
    assert fixed["boot1"].errored == [], fixed["boot1"].errored
    assert fixed["boot1"].diagnoses == {}


def test_fb2_fixed_corpus_boots_two_and_three_run_nothing(fixed):
    assert fixed["boot2"].ran == [] and fixed["boot3"].ran == []
    assert fixed["boot2"].errored == [] and fixed["boot3"].errored == []
    assert fixed["boot3_snapshot"] == fixed["boot1_snapshot"]


def test_fb2_renamed_and_edited_files_are_a_one_time_no_op_on_a_migrated_box(pristine):
    """The ledger sees a renamed / edited file as new work and runs it ONCE on every migrated
    box. Driven on the pristine three-boot db: exactly the touched files run, none errors, and the
    ONLY metadata that changes is the two intended repairs (275's has_port row appears; 276 removes
    the two resurrected pattern rows). Boots after that run nothing."""
    s = pristine
    assert set(s["fix_boot"].ran) == _TOUCHED, s["fix_boot"].ran
    assert s["fix_boot"].errored == [] and s["fix_not_applied"] == {}
    assert s["fix_boot2"].ran == [] and s["fix_boot3"].ran == []
    before, after = s["old3_snapshot"], s["fixed_on_old_snapshot"]
    assert s["fixed_on_old_snapshot3"] == after
    for t in ("rel_type_aliases", "entity_taxonomies"):
        assert before[t] == after[t], t
    rel_added = _by_key(after["rel_types"] - before["rel_types"], "rel_type")
    assert set(rel_added) == {"has_port"} and (before["rel_types"] - after["rel_types"]) == set()
    removed = _by_key_multi(before["extraction_patterns"] - after["extraction_patterns"], "rel_type")
    assert after["extraction_patterns"] - before["extraction_patterns"] == set()
    assert {(k, r["pattern_regex"]) for k, rs in removed.items() for r in rs} == {
        ("has_port", _OLD_PORT_RE), ("has_phone", _OLD_PHONE_RE)}


def test_fb2_on_a_legacy_migrated_box_the_one_time_rerun_keeps_129s_correction(legacy_box):
    """PRODUCTION shape (legacy sweep, then ledger). The renamed 017_seed runs ALONE once there;
    with its skeleton guard it touches nothing (age keeps 129's {ANY}); the MUTANT without the
    guard re-clobbers age to {Person} on a box where 129 will never run again — the exact
    regression the guard exists to prevent, driven on a TEMPLATE clone of the same state."""
    s = legacy_box
    # the production shape's standing failed set: the three every-boot files, 184 (its has_phone
    # UPDATE collides with the row 060's re-run resurrected — 23505) and the two unguarded-DDL
    # files 053 / 058 (42701 / 42P07 on a box that already carried their objects)
    assert set(s["ledger1_not_applied"]) == _EVERY_BOOT | {
        "184_phone_atomic_pattern_nanp", "053_add_provisioning_heartbeat", "058_extraction_patterns"}, s["ledger1_not_applied"]
    ran = set(s["fix_boot"].ran)
    assert ran == _TOUCHED, ran ^ _TOUCHED
    # 184 (now guarded on the corrected sibling) runs BEFORE 276 on that boot and touches
    # nothing instead of colliding — the production shape converges in ONE boot
    assert s["fix_boot"].errored == [], s["fix_boot"].errored
    assert s["fix_not_applied"] == {}, s["fix_not_applied"]
    assert s["fix_boot2"].ran == [] and s["fix_not_applied2"] == {} and s["fix_boot3"].ran == []
    before, after = s["before"], s["after"]
    age_before = json.loads(_by_key(before["rel_types"], "rel_type")["age"]["head_types"])
    age_after = json.loads(_by_key(after["rel_types"], "rel_type")["age"]["head_types"])
    assert age_before == ["ANY"] and age_after == ["ANY"]
    rel_added = _by_key(after["rel_types"] - before["rel_types"], "rel_type")
    assert set(rel_added) == {"has_port"} and (before["rel_types"] - after["rel_types"]) == set()
    removed = _by_key_multi(before["extraction_patterns"] - after["extraction_patterns"], "rel_type")
    assert {(k, r["pattern_regex"]) for k, rs in removed.items() for r in rs} == {
        ("has_port", _OLD_PORT_RE), ("has_phone", _OLD_PHONE_RE)}
    for t in ("rel_type_aliases", "entity_taxonomies"):
        assert before[t] == after[t], t
    assert s["after3"] == after
    # anti-vacuity: the unguarded mutant DOES regress the same box
    mut_age = json.loads(_by_key(s["mutant_after"]["rel_types"], "rel_type")["age"]["head_types"])
    assert mut_age == ["Person"], mut_age
    assert s["mutant_boot"].errored == []


def test_fb2_has_port_row_carries_its_siblings_metadata(fixed):
    rel = {k: json.loads(v) for k, v in _by_key(fixed["boot1_snapshot"]["rel_types"], "rel_type")["has_port"].items()}
    sib = {k: json.loads(v) for k, v in _by_key(fixed["boot1_snapshot"]["rel_types"], "rel_type")["has_uuid"].items()}
    for col in ("tail_types", "source", "category", "fact_class", "storage_target", "is_leaf_only",
                "temporal_class", "is_hierarchy_rel", "is_symmetric", "correction_behavior"):
        assert rel[col] == sib[col], (col, rel[col], sib[col])
    assert rel["scalar_datatype"] == "integer" and rel["value_min"] == 1 and rel["value_max"] == 65535


def test_fb2_dead_statements_are_gone_and_the_survivors_are_intact():
    """Parse-level pins on the edited files: the offending statements no longer exist, the
    statements that do the real work still do."""
    def stmts(name: str) -> List[str]:
        import sqlparse
        text = census._strip_comments_quote_aware(open(os.path.join(_MIG_DIR, name)).read())
        return [" ".join(x.split()) for x in sqlparse.split(text)]

    s089 = "\n".join(stmts("089_wire_hierarchy_flags.sql"))
    assert "has_transitivity" not in s089 and "is_hierarchy_rel = true" in s089
    s110 = "\n".join(stmts("110_has_state_scalar_predicate.sql"))
    assert "entity_attributes" not in s110 and "('has_state', 'Has State'" in s110
    s060 = "\n".join(stmts("060_atomic_scalar_patterns.sql"))
    assert "INSERT INTO rel_types" not in s060 and "INSERT INTO extraction_patterns" in s060
    s030 = "\n".join(stmts("030_strengthen_rel_types_wikidata_ontology.sql"))
    assert "is_hierarchy_rel IS NULL OR is_hierarchy_rel = false" not in s030
    assert "WHERE rel_type = 'created_by'" in s030
    s073 = "\n".join(stmts("073_provenance_cleanup.sql"))
    assert s073.count("column_name = 'fact_provenance'") >= 2  # 1b now tests it like 1f
    s017 = "\n".join(stmts("017_seed_builtin_scalar_rel_types.sql"))
    assert "WHERE rel_types.head_types IS NULL" in s017
    s184 = "\n".join(stmts("184_phone_atomic_pattern_nanp.sql"))
    assert s184.count("AND NOT EXISTS") == 2  # public + tenant arms guarded on the corrected sibling
    s084 = "\n".join(stmts("084_cleanup_synthetic_related_to.sql"))
    assert "subject_id = user_id" not in s084 and "alias = 'user'" not in s084
    assert s084.count("AND subject_id = $1") == 2 and "replace(substring(_schema FROM length('faultline_') + 1), '_', '-')" in s084
    s013 = "\n".join(stmts("013_confirmation_source.sql"))
    assert "CREATE OR REPLACE FUNCTION" not in s013 and "confirmation_source" in s013
    names = set(os.listdir(_MIG_DIR))
    assert "003_confirmation_source.sql" not in names and "016_builtin_scalar_rel_types.sql" not in names


# ── FB3: first-boot parity ────────────────────────────────────────────────────────────
def test_fb3_one_pass_equals_three_passes_on_the_fixed_corpus(fixed):
    assert fixed["boot1_snapshot"] == fixed["boot3_snapshot"]
    for t in _META_TABLES:
        assert len(fixed["boot1_snapshot"][t]) > 0, t


def test_fb3_fresh_first_boot_equals_a_migrated_box_after_the_fix_except_the_documented_residual(pristine, fixed):
    """Reference = the pristine three-boot box after the fixed corpus ran over it (FB2). The
    tuple set-diff on every table the six files touch is EMPTY except ONE row, stated here so it
    is never mistaken for parity: `age.head_types` is {ANY} on the fresh path (migration 129) and
    {Person} on the old path — 016's boot-#2 re-run clobbered 129's correction on every
    ledger-era fresh database, and that box's ledger has 129 stamped applied, so no forward pass
    restores it. PERMANENTLY unrecoverable on the old path; the fresh path is the truth."""
    fresh, old = fixed["boot1_snapshot"], pristine["fixed_on_old_snapshot"]
    for t in ("rel_type_aliases", "extraction_patterns", "entity_taxonomies"):
        assert fresh[t] ^ old[t] == set(), (t, fresh[t] ^ old[t])
    diff_fresh = _by_key(fresh["rel_types"] - old["rel_types"], "rel_type")
    diff_old = _by_key(old["rel_types"] - fresh["rel_types"], "rel_type")
    assert set(diff_fresh) == set(diff_old) == {"age"}, (set(diff_fresh), set(diff_old))
    a, b = diff_fresh["age"], diff_old["age"]
    changed = {k for k in a if a[k] != b[k]}
    assert changed == {"head_types"}, changed
    assert json.loads(a["head_types"]) == ["ANY"] and json.loads(b["head_types"]) == ["Person"]


def test_fb3_structure_the_six_files_own_lands_on_the_first_boot(fixed, pristine):
    s = fixed
    assert "confirmation_source" in s["boot1_staged_cols"]            # 013 (was 003)
    assert "staged_fact_confirmations" in s["boot1_tables"]           # 013 (was 003)
    assert "fact_provenance" not in s["boot1_staged_cols"]            # 073's premise: it never exists on public
    # 003's dead functions: absent on both paths (073 drops them; 013 no longer re-creates them)
    assert not ({"promote_staged_fact", "record_confirmation"} & s["boot1_functions"])
    assert not ({"promote_staged_fact", "record_confirmation"} & pristine["old3_functions"])
    # the column SET of public.staged_facts is identical to the old path (ordinal differs: the old
    # path appended confirmation_source on boot #2, the fresh path adds it at 013 — position is
    # not semantics, and nothing selects `*` from public.staged_facts into a positional target)
    assert set(s["boot1_staged_cols"]) == set(_columns(pristine["dsn"], "staged_facts"))
    rel = _by_key(s["boot1_snapshot"]["rel_types"], "rel_type")
    has_state = {k: json.loads(v) for k, v in rel["has_state"].items()}
    assert has_state["tail_types"] == ["Concept"] and has_state["storage_target"] == "facts"  # 111 owns it
    scal = {k: json.loads(v) for k, v in rel["nationality"].items()}
    assert scal["tail_types"] == ["SCALAR"] and scal["category"] == "identity"               # 017_seed landed


# ── FB4: the static order census ──────────────────────────────────────────────────────
def test_fb4_census_reproduces_the_six_plus_084_from_the_pristine_corpus(pristine_dir):
    r = census.run_census(pristine_dir)
    assert r.red_files == sorted(set(_SIX) | {"084_cleanup_synthetic_related_to"}), census.format_findings(r)
    kinds = {f.file: set() for f in r.findings}
    for f in r.findings:
        kinds[f.file].add((f.kind, f.obj))
    assert ("missing_table", "staged_facts") in kinds["003_confirmation_source"]
    assert ("missing_column", "rel_types.head_types") in kinds["016_builtin_scalar_rel_types"]
    assert ("check_violation", "rel_types.source") in kinds["060_atomic_scalar_patterns"]
    assert ("missing_column", "staged_facts.fact_provenance") in kinds["073_provenance_cleanup"]
    assert ("missing_column", "rel_types.has_transitivity") in kinds["089_wire_hierarchy_flags"]
    assert ("check_violation", "rel_types.storage_target") in kinds["110_has_state_scalar_predicate"]
    # the tenant arm: 084's DELETE resolved against the tenant template — no user_id on facts
    assert ("missing_column", "tenant:facts.user_id") in kinds["084_cleanup_synthetic_related_to"]
    assert ("missing_column", "tenant:staged_facts.user_id") in kinds["084_cleanup_synthetic_related_to"]
    # and 089's fan-out is judged too (the tenant rel_types has no has_transitivity either)
    assert ("missing_column", "tenant:rel_types.has_transitivity") in kinds["089_wire_hierarchy_flags"]
    assert r.unmodelled == [], r.unmodelled


def test_fb4_census_is_clean_on_the_fixed_corpus():
    r = census.run_census(_MIG_DIR)
    assert r.findings == [], census.format_findings(r)
    assert r.unmodelled == [], r.unmodelled
    assert r.files >= 280 and r.statements > 1700
    # the skipped set is small, itemised, and only the three shapes the docstring names
    assert r.dynamic_skipped == len(r.skipped_detail) <= 14, r.skipped_detail
    whys = {why for _f, why, _t in r.skipped_detail}
    assert whys <= {"EXECUTE of a variable (no template text)", "substituted identifier (%I / %s inside a name)",
                    "schema family 'other:seed_%%' has no template"}, whys
    # the four `SET search_path TO %I` blocks are now judged against the tenant template, not skipped
    assert not [t for _f, _w, t in r.skipped_detail if "search_path" in t], r.skipped_detail


def test_fb4_census_model_is_sound_against_the_migrated_database(fixed):
    """Every table / column the census BELIEVES exists after the walk exists on the real
    migrated db — the model never hallucinates structure (else a missing-object finding could be
    masked by an invented one)."""
    c = census.Census()
    c.run(_MIG_DIR)
    real = {(t, col) for t in _tables(fixed["dsn"]) for col in _columns(fixed["dsn"], t)}
    real_tables = {t for t, _c in real}
    believed_tables = {t for t in c.model.tables if not t.startswith("pg_catalog.") and not t.startswith("_")}
    ghost_tables = believed_tables - real_tables
    assert not ghost_tables, ghost_tables
    ghost_cols = {(t, col) for t in believed_tables for col in c.model.tables[t].columns
                  if col != "*" and (t, col) not in real}
    assert not ghost_cols, sorted(ghost_cols)
    # and the model saw most of the real public shape (a census that skipped everything is vacuous)
    assert len(believed_tables) >= 0.9 * len(real_tables - {"schema_migration_status"}), (
        len(believed_tables), len(real_tables))


def test_fb4_census_fires_on_a_planted_misordering_and_a_planted_check_violation(tmp_path):
    corpus = _copy_corpus(_MIG_DIR, str(tmp_path), "planted")
    # (a) a late file moved early: 275 names rel_types columns 101 adds — and rel_types itself
    #     does not exist before 005
    shutil.copy(os.path.join(corpus, "275_seed_has_port_rel_type.sql"), os.path.join(corpus, "004_planted_early.sql"))
    # (b) a value the CHECK never admits, planted late
    with open(os.path.join(corpus, "277_planted_check.sql"), "w") as fh:
        fh.write("INSERT INTO public.rel_types (rel_type, label, source) VALUES ('planted', 'P', 'bootstrap') "
                 "ON CONFLICT (rel_type) DO NOTHING;\n")
    # (c) a column referenced before it exists, inside a DO block WITHOUT a guard for it
    with open(os.path.join(corpus, "004_planted_column.sql"), "w") as fh:
        fh.write("DO $$ BEGIN UPDATE facts SET fact_provenance = 'x' WHERE 1 = 0; END $$;\n")
    # (d) PRECISION: the same reference GUARDED on the column must NOT fire
    with open(os.path.join(corpus, "004_planted_guarded.sql"), "w") as fh:
        fh.write("DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'facts' "
                 "AND column_name = 'fact_provenance') THEN UPDATE facts SET fact_provenance = 'x' WHERE 1 = 0; "
                 "END IF; END $$;\n")
    # (e) READ-SIDE: a column referenced only in a WHERE, on the public table, before it exists
    with open(os.path.join(corpus, "004_planted_where.sql"), "w") as fh:
        fh.write("DELETE FROM facts WHERE fact_provenance = 'x' AND rel_type IN (SELECT rel_type FROM pending_types WHERE nope = 1);\n")
    # (f) TENANT FAN-OUT: 084's exact shape, re-planted as a fresh late file
    with open(os.path.join(corpus, "277_planted_tenant_fanout.sql"), "w") as fh:
        fh.write("DO $$ DECLARE _schema TEXT; BEGIN FOR _schema IN SELECT schema_name FROM information_schema.schemata "
                 "WHERE schema_name LIKE 'faultline\\_%' LOOP EXECUTE format($d$ DELETE FROM %I.staged_facts "
                 "WHERE rel_type = 'related_to' AND subject_id = user_id $d$, _schema); END LOOP; END $$;\n")
    # (g) `SET search_path TO %I` then a PLAIN statement: judged against the tenant template
    with open(os.path.join(corpus, "277_planted_search_path.sql"), "w") as fh:
        fh.write("DO $$ DECLARE _schema TEXT; BEGIN FOR _schema IN SELECT schema_name FROM information_schema.schemata "
                 "WHERE schema_name LIKE 'faultline\\_%' LOOP EXECUTE format('SET search_path TO %I', _schema); "
                 "DELETE FROM staged_facts WHERE user_id = 1; RESET search_path; END LOOP; END $$;\n")
    # (h) `EXECUTE 'a' || _schema || '.b'` literal chain: joined and judged, never truncated
    with open(os.path.join(corpus, "277_planted_concat.sql"), "w") as fh:
        fh.write("DO $$ DECLARE _schema TEXT; BEGIN FOR _schema IN SELECT schema_name FROM information_schema.schemata "
                 "WHERE schema_name LIKE 'faultline\\_%' LOOP EXECUTE 'DELETE FROM ' || _schema || '.staged_facts WHERE "
                 "rel_type = ''related_to'' AND ' || 'user_id = 1'; END LOOP; END $$;\n")
    r = census.run_census(corpus)
    assert set(r.red_files) == {"004_planted_early", "277_planted_check", "004_planted_column",
                                "004_planted_where", "277_planted_tenant_fanout",
                                "277_planted_search_path", "277_planted_concat"}, r.red_files
    kinds_all = {(f.file, f.kind, f.obj) for f in r.findings}
    assert ("004_planted_where", "missing_column", "facts.fact_provenance") in kinds_all
    assert ("004_planted_where", "missing_column", "pending_types.nope") in kinds_all
    assert ("277_planted_tenant_fanout", "missing_column", "tenant:staged_facts.user_id") in kinds_all
    assert ("277_planted_search_path", "missing_column", "tenant:staged_facts.user_id") in kinds_all
    assert ("277_planted_concat", "missing_column", "tenant:staged_facts.user_id") in kinds_all
    assert not [d for d in r.skipped_detail if d[0].startswith("277_planted")], r.skipped_detail
    kinds = {(f.file, f.kind, f.obj) for f in r.findings}
    assert ("004_planted_early", "missing_table", "rel_types") in kinds
    assert ("277_planted_check", "check_violation", "rel_types.source") in kinds
    assert ("004_planted_column", "missing_column", "facts.fact_provenance") in kinds
    assert not [f for f in r.findings if f.file == "004_planted_guarded"]


def test_fb4_census_fires_when_a_fixed_file_is_moved_back_to_its_old_position(tmp_path):
    """The exact regression this lane fixes, re-planted: 013 → 003 and 017_seed → 016."""
    corpus = _copy_corpus(_MIG_DIR, str(tmp_path), "regress")
    os.rename(os.path.join(corpus, "013_confirmation_source.sql"), os.path.join(corpus, "003_confirmation_source.sql"))
    os.rename(os.path.join(corpus, "017_seed_builtin_scalar_rel_types.sql"),
              os.path.join(corpus, "016_builtin_scalar_rel_types.sql"))
    r = census.run_census(corpus)
    assert set(r.red_files) == {"003_confirmation_source", "016_builtin_scalar_rel_types"}, r.red_files


# ── FB6: the production-facing half — TENANT-BEARING boxes (275 / 276 / 084 fan-outs) ────
_SEAT_TOPIC = "11111111-2222-4333-8444-555555555555"   # an expanded-topic entity id
_CHILD = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"        # a child entity (orphan-binder edge subject)
_IMPOSTOR = "99999999-8888-4777-8666-555555555555"     # a NON-seat entity that happens to carry alias 'user'
_ALIAS_PREDICATE = "subject_id IN (SELECT entity_id FROM %I.entity_aliases WHERE alias = 'user')"


def _plant_tenant(dsn: str, schema: str, seat: str, *, grown_has_port: bool, identity: str) -> Dict[str, object]:
    """Plant the states the fan-outs must respect, through plain SQL on the tenant schema:
      * (A only) a tenant-GROWN has_port rel (source='engine') — authority: tenant > seed;
      * extraction_pattern_matches rows pointing at the two RESURRECTED 060 rows the tenant copied
        from public at provisioning (two per row — FK telemetry 276 must carry over);
      * 084's exact leftover signature (seat related_to topic, llm_learned) in staged_facts AND
        facts, plus the two rows its guards protect (child-subject edge; user_stated edge);
      * the seat's IDENTITY state — `identity`: "renamed" (alias 'user' demoted, a user-stated
        name preferred) or "stripped" (no 'user' alias at all: bench resets / pre-b1a62cc9 seats);
      * a NON-seat entity wearing alias 'user' with its own llm_learned related_to rows — those
        are NOT the hack's leftovers and must survive."""
    import psycopg2

    conn = psycopg2.connect(dsn)
    planted: Dict[str, object] = {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("SELECT id, rel_type FROM extraction_patterns WHERE category = 'scalar_atomic' "
                        "AND source = 'bootstrap' AND pattern_regex IN (%s, %s) ORDER BY rel_type",
                        (_OLD_PORT_RE, _OLD_PHONE_RE))
            old_rows = cur.fetchall()
            assert len(old_rows) == 2, old_rows  # the resurrected rows were copied into the tenant
            planted["old_ids"] = {rt: pid for pid, rt in old_rows}
            for pid, _rt in old_rows:
                for txt in ("10:30", "+1 519 555 0100"):
                    cur.execute("INSERT INTO extraction_pattern_matches (pattern_id, user_id, matched_text) VALUES (%s, %s, %s)",
                                (pid, seat, txt))
            cur.execute("SELECT count(*) FROM extraction_pattern_matches")
            planted["match_count"] = cur.fetchone()[0]
            if grown_has_port:
                cur.execute("INSERT INTO rel_types (rel_type, label, source, engine_generated, confidence, category) "
                            "VALUES ('has_port', 'has port (grown)', 'engine', true, 0.55, 'grown')")
            # seat identity state
            if identity == "renamed":
                cur.execute("UPDATE entity_aliases SET is_preferred = false WHERE entity_id = %s AND alias = 'user'", (seat,))
                cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source) "
                            "VALUES (%s, 'chris', true, 'user_stated')", (seat,))
            elif identity == "stripped":
                cur.execute("DELETE FROM entity_aliases WHERE entity_id = %s AND alias = 'user'", (seat,))
            else:
                raise AssertionError(identity)
            cur.execute("SELECT count(*) FROM entity_aliases WHERE entity_id = %s AND alias = 'user' AND is_preferred", (seat,))
            assert cur.fetchone()[0] == 0  # in NEITHER state is 'user' the seat's preferred alias
            # an impostor: a non-seat entity carrying alias 'user'
            cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'Person') ON CONFLICT (id) DO NOTHING", (_IMPOSTOR,))
            cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source) "
                        "VALUES (%s, 'user', true, 'unspecified') ON CONFLICT (entity_id, alias) DO NOTHING", (_IMPOSTOR,))
            # 084's signature + the two guarded survivors, in both tables
            for table in ("staged_facts", "facts"):
                cur.execute(f"INSERT INTO {table} (subject_id, object_id, rel_type, fact_provenance, fact_class) VALUES "
                            f"(%s, %s, 'related_to', 'llm_learned', 'B')", (seat, _SEAT_TOPIC))        # TARGET
                cur.execute(f"INSERT INTO {table} (subject_id, object_id, rel_type, fact_provenance, fact_class) VALUES "
                            f"(%s, %s, 'related_to', 'llm_learned', 'B')", (_CHILD, seat))             # survivor: child subject
                cur.execute(f"INSERT INTO {table} (subject_id, object_id, rel_type, fact_provenance, fact_class) VALUES "
                            f"(%s, %s, 'related_to', 'user_stated', 'B')", (seat, _CHILD))             # survivor: user_stated
                cur.execute(f"INSERT INTO {table} (subject_id, object_id, rel_type, fact_provenance, fact_class) VALUES "
                            f"(%s, %s, 'related_to', 'llm_learned', 'B')", (_IMPOSTOR, _SEAT_TOPIC))   # survivor: impostor
    finally:
        conn.close()
    return planted


def _tenant_state(dsn: str, schema: str) -> Dict[str, object]:
    st: Dict[str, object] = {}
    st["has_port"] = _q(dsn, f"SELECT source, confidence, category, tail_types FROM {schema}.rel_types WHERE rel_type = 'has_port'")
    st["patterns"] = {(r[0], r[1], r[2]) for r in _q(
        dsn, f"SELECT rel_type, pattern_regex, is_active FROM {schema}.extraction_patterns WHERE rel_type IN ('has_port','has_phone')")}
    st["matches"] = _q(dsn, f"SELECT m.pattern_id, p.rel_type, p.pattern_regex, m.matched_text FROM {schema}.extraction_pattern_matches m "
                            f"LEFT JOIN {schema}.extraction_patterns p ON p.id = m.pattern_id ORDER BY m.id")
    for table in ("staged_facts", "facts"):
        st[table] = {tuple(r) for r in _q(dsn, f"SELECT subject_id, object_id, fact_provenance FROM {schema}.{table} WHERE rel_type = 'related_to'")}
    return st


def _tenant_pins(st: Dict[str, object], seat: str, planted: Dict[str, object], *, grown: bool) -> List[str]:
    """The per-tenant contract after the fixed corpus. Returns the violated pins (empty = clean) so
    the same contract can be evaluated on a MUTANT drive and shown to go red."""
    bad: List[str] = []
    hp = st["has_port"]
    if grown:
        if hp != [("engine", 0.55, "grown", None)]:
            bad.append(f"grown has_port overwritten: {hp}")
    elif hp != [("builtin", 0.7, "network", ["SCALAR"])]:
        bad.append(f"seeded has_port wrong: {hp}")
    regs = {(rt, rx) for rt, rx, _a in st["patterns"]}
    if (("has_port", _OLD_PORT_RE) in regs) or (("has_phone", _OLD_PHONE_RE) in regs):
        bad.append("resurrected 060 rows still present")
    if ("has_phone", _NEW_PHONE_RE) not in regs or not any(rt == "has_port" and rx != _OLD_PORT_RE for rt, rx in regs):
        bad.append("corrected rows missing")
    if len(st["matches"]) != planted["match_count"]:
        bad.append(f"match rows lost: {len(st['matches'])} != {planted['match_count']}")
    if any(rt is None for _pid, rt, _rx, _t in st["matches"]):
        bad.append("match row with a dangling pattern_id")
    if any(rx in (_OLD_PORT_RE, _OLD_PHONE_RE) for _pid, _rt, rx, _t in st["matches"]):
        bad.append("match rows still point at the resurrected rows")
    for table in ("staged_facts", "facts"):
        rows = st[table]
        if (seat, _SEAT_TOPIC, "llm_learned") in rows:
            bad.append(f"084 target survived in {table}")
        if (_CHILD, seat, "llm_learned") not in rows:
            bad.append(f"084 deleted the child-subject edge in {table}")
        if (seat, _CHILD, "user_stated") not in rows:
            bad.append(f"084 deleted the user_stated edge in {table}")
        if (_IMPOSTOR, _SEAT_TOPIC, "llm_learned") not in rows:
            bad.append(f"084 deleted the impostor's rows in {table} (a non-seat entity aliased 'user')")
    return bad


@pytest.fixture(scope="module")
def tenant_box(pristine_dir, monkeypatch_module, tmp_path_factory):
    """Pristine corpus ×2 (the resurrected rows now stand in public), then TWO real tenants via
    create_user_schema (they copy public's rows), planted, then pristine boot #3 (084 fails on the
    tenant-bearing box), then TEMPLATE clones for the two mutants, then the FIXED corpus."""
    dsn = _fresh_database("fb_tenants")
    s: Dict[str, object] = {"dsn": dsn, "tenants": {}}
    clones: List[str] = []
    try:
        bm.run_boot_migrations(dsn, pristine_dir, echo=False)
        bm.run_boot_migrations(dsn, pristine_dir, echo=False)
        monkeypatch_module.setenv("POSTGRES_DSN", dsn)
        monkeypatch_module.setenv("FAULTLINE_MIGRATIONS_DIR", pristine_dir)
        from src.provisioning.schema_manager import create_user_schema

        import uuid as _uuid
        for label, grown, identity in (("A", True, "renamed"), ("B", False, "stripped")):
            seat = str(_uuid.uuid4())
            schema, status = create_user_schema(seat, seat.replace("-", "_"))
            assert status == "ready", (label, status)
            planted = _plant_tenant(dsn, schema, seat, grown_has_port=grown, identity=identity)
            s["tenants"][label] = {"seat": seat, "schema": schema, "grown": grown, "identity": identity, "planted": planted}
        # the tenant-bearing box's next PRE-LEDGER sweep (production's shape: its tenants predate
        # the ledger, and every file ran over them on every boot)
        monkeypatch_module.setenv("FAULTLINE_MIGRATION_LEDGER", "false")
        s["boot3"] = bm.run_boot_migrations(dsn, pristine_dir, echo=False)
        monkeypatch_module.delenv("FAULTLINE_MIGRATION_LEDGER", raising=False)
        s["ledger_084"] = _q(dsn, "SELECT schema_name, status FROM public.schema_migration_status "
                                  "WHERE migration_id = '084_cleanup_synthetic_related_to' ORDER BY 1")
        for t in s["tenants"].values():
            t["before"] = _tenant_state(dsn, t["schema"])
        # mutant clones of the PRE-FIX tenant-bearing state
        clones = [_clone_database(dsn, "fb_tenants_m3"), _clone_database(dsn, "fb_tenants_m6"),
                  _clone_database(dsn, "fb_tenants_m7")]
        s["fix_boot"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        s["fix_not_applied"] = _ledger_not_applied(dsn)
        for t in s["tenants"].values():
            t["after"] = _tenant_state(dsn, t["schema"])
        s["fix_boot2"] = bm.run_boot_migrations(dsn, _MIG_DIR, echo=False)
        # M3: 276 without the telemetry re-point (both arms); M6: 275's tenant arm DO UPDATE
        root = str(tmp_path_factory.mktemp("mutants"))
        m3 = _copy_corpus(_MIG_DIR, root, "m3")
        p276 = os.path.join(m3, "276_retire_resurrected_060_pattern_rows.sql")
        t276 = open(p276).read()
        assert t276.count("SET pattern_id = ") == 2
        t276 = re.sub(r"\n\s*UPDATE public\.extraction_pattern_matches SET pattern_id = _new_id WHERE pattern_id = _old_id;", "", t276)
        t276 = re.sub(r"\n\s*EXECUTE format\('UPDATE %I\.extraction_pattern_matches SET pattern_id = \$1 WHERE pattern_id = \$2', _schema\)\n\s*USING _new_id, _old_id;", "", t276)
        assert t276.count("SET pattern_id = ") == 0
        open(p276, "w").write(t276)
        m6 = _copy_corpus(_MIG_DIR, root, "m6")
        p275 = os.path.join(m6, "275_seed_has_port_rel_type.sql")
        t275 = open(p275).read()
        assert t275.count("ON CONFLICT (rel_type) DO NOTHING") == 2
        head, tail = t275.split("-- ── 2.", 1)
        tail = tail.replace("ON CONFLICT (rel_type) DO NOTHING",
                            "ON CONFLICT (rel_type) DO UPDATE SET source = EXCLUDED.source, confidence = EXCLUDED.confidence, "
                            "category = EXCLUDED.category, tail_types = EXCLUDED.tail_types")
        open(p275, "w").write(head + "-- ── 2." + tail)
        # M7: 084's DELETEs keyed on the alias STRING instead of the seat identity
        m7 = _copy_corpus(_MIG_DIR, root, "m7")
        p084 = os.path.join(m7, "084_cleanup_synthetic_related_to.sql")
        t084 = open(p084).read()
        assert t084.count("AND subject_id = $1") == 2
        t084 = t084.replace("AND subject_id = $1", "AND " + _ALIAS_PREDICATE)
        t084 = t084.replace("$del$, _schema) USING _seat;", "$del$, _schema, _schema);")
        assert t084.count("USING _seat") == 1 and t084.count(_ALIAS_PREDICATE) == 2  # only the identity probe keeps _seat
        open(p084, "w").write(t084)
        s["m3_boot"] = bm.run_boot_migrations(clones[0], m3, echo=False)
        s["m6_boot"] = bm.run_boot_migrations(clones[1], m6, echo=False)
        s["m7_boot"] = bm.run_boot_migrations(clones[2], m7, echo=False)
        for t in s["tenants"].values():
            t["m3"] = _tenant_state(clones[0], t["schema"])
            t["m6"] = _tenant_state(clones[1], t["schema"])
            t["m7"] = _tenant_state(clones[2], t["schema"])
        yield s
    finally:
        monkeypatch_module.delenv("POSTGRES_DSN", raising=False)
        monkeypatch_module.delenv("FAULTLINE_MIGRATIONS_DIR", raising=False)
        for c in clones:
            _drop_database(c)
        _drop_database(dsn)


def test_fb6_census_tenant_model_is_sound_against_a_real_tenant_schema(tenant_box):
    """Every table / column the census's TENANT model believes exists is present on a real
    provisioned tenant (template + fan-out DDL never hallucinate structure)."""
    c = census.Census()
    c.run(_MIG_DIR)
    dsn = tenant_box["dsn"]
    for t in tenant_box["tenants"].values():
        schema = t["schema"]
        real = {(r[0], r[1]) for r in _q(dsn, "SELECT table_name, column_name FROM information_schema.columns "
                                              "WHERE table_schema = %s", (schema,))}
        real_tables = {tb for tb, _c in real}
        believed = {tb for tb in c.tenant.tables if not tb.startswith("_")}
        assert not (believed - real_tables), believed - real_tables
        ghost = {(tb, col) for tb in believed for col in c.tenant.tables[tb].columns if col != "*" and (tb, col) not in real}
        assert not ghost, sorted(ghost)
        assert len(believed) >= 0.9 * len(real_tables), (len(believed), len(real_tables))


def test_fb6_pristine_084_fails_on_every_boot_of_a_tenant_bearing_box(tenant_box):
    """`DELETE FROM %I.facts WHERE subject_id = user_id` — no such column on a tenant's facts /
    staged_facts (only entity_attributes carries user_id), SQLSTATE 42703, the whole fan-out rolls
    back: the leftover rows it was written to blow away are all still there."""
    s = tenant_box
    assert "084_cleanup_synthetic_related_to" in [m for m, _n in s["boot3"].errored]
    diag = s["boot3"].diagnoses["084_cleanup_synthetic_related_to"]
    assert [d["sqlstate"] for d in diag] == ["42703"] and 'column "user_id" does not exist' in diag[0]["message"], diag
    for t in s["tenants"].values():
        for table in ("staged_facts", "facts"):
            assert (t["seat"], _SEAT_TOPIC, "llm_learned") in t["before"][table], (t["schema"], table)
    # and the tenants copied the resurrected rows from public at provisioning
    for t in s["tenants"].values():
        regs = {(rt, rx) for rt, rx, _a in t["before"]["patterns"]}
        assert ("has_port", _OLD_PORT_RE) in regs and ("has_phone", _OLD_PHONE_RE) in regs
    # under the ledger the file is worse than failed: a tenant minted after 084 shipped is STAMPED
    # applied for it at mint (stamp_schema_as_current), so the ledger never even re-runs it there
    schemas = {t["schema"] for t in s["tenants"].values()}
    assert {(sc, st) for sc, st in s["ledger_084"] if sc in schemas} == {(sc, "applied") for sc in schemas}


def test_fb6_fixed_corpus_over_the_tenant_bearing_box_holds_every_per_tenant_pin(tenant_box):
    s = tenant_box
    assert s["fix_boot"].errored == [], s["fix_boot"].errored
    assert s["fix_not_applied"] == {}, s["fix_not_applied"]
    assert s["fix_boot2"].ran == [] and s["fix_boot2"].errored == []
    for label, t in s["tenants"].items():
        bad = _tenant_pins(t["after"], t["seat"], t["planted"], grown=t["grown"])
        assert bad == [], (label, bad)
        # the telemetry moved to the CORRECTED rows, all of it
        assert len(t["after"]["matches"]) == t["planted"]["match_count"] == 4
        assert {rt for _p, rt, _rx, _t in t["after"]["matches"]} == {"has_port", "has_phone"}


def test_fb6_084_keys_on_the_seat_identity_not_an_alias_string(tenant_box):
    """(a) a seat with NO 'user' alias (B, stripped) is cleaned; (b) a seat that renamed itself
    (A: 'user' demoted, 'chris' preferred) is cleaned; (c) the impostor entity wearing alias
    'user' keeps its llm_learned rows on both tenants. Driven; the pins live in _tenant_pins."""
    s = tenant_box
    a, b = s["tenants"]["A"], s["tenants"]["B"]
    assert a["identity"] == "renamed" and b["identity"] == "stripped"
    for t in (a, b):
        for table in ("staged_facts", "facts"):
            rows = t["after"][table]
            assert (t["seat"], _SEAT_TOPIC, "llm_learned") not in rows, (t["identity"], table)     # cleaned
            assert (_IMPOSTOR, _SEAT_TOPIC, "llm_learned") in rows, (t["identity"], table)         # untouched
            assert (_CHILD, t["seat"], "llm_learned") in rows and (t["seat"], _CHILD, "user_stated") in rows
    # the identity the file reconstructs IS the seat's entities row (schema_manager's derivation)
    for t in (a, b):
        rebuilt = t["schema"][len("faultline_"):].replace("_", "-")
        assert rebuilt == t["seat"]
        assert _q(s["dsn"], f"SELECT count(*) FROM {t['schema']}.entities WHERE id = %s", (t["seat"],)) == [(1,)]


def test_fb6_mutant_m7_alias_string_predicate_misses_the_stripped_seat_and_hits_the_impostor(tenant_box):
    """(d) re-plant `subject_id IN (SELECT entity_id FROM entity_aliases WHERE alias = 'user')`:
    B (no 'user' alias) is silently NOT cleaned, and on both tenants the impostor's rows are
    deleted — two wrong outcomes the identity predicate cannot produce."""
    s = tenant_box
    assert s["m7_boot"].errored == []
    a, b = s["tenants"]["A"], s["tenants"]["B"]
    bad_b = _tenant_pins(b["m7"], b["seat"], b["planted"], grown=False)
    assert any(x.startswith("084 target survived") for x in bad_b), bad_b
    for t in (a, b):
        bad = _tenant_pins(t["m7"], t["seat"], t["planted"], grown=t["grown"])
        assert any("impostor" in x for x in bad), (t["identity"], bad)


def test_fb6_mutant_m3_dropping_the_repoint_loses_the_tenant_telemetry(tenant_box):
    """Re-plant M3 (276 without the extraction_pattern_matches re-point) on a TEMPLATE clone of the
    same pre-fix state: the FK is ON DELETE CASCADE, so the delete takes the match rows with it."""
    s = tenant_box
    assert s["m3_boot"].errored == []
    for label, t in s["tenants"].items():
        bad = _tenant_pins(t["m3"], t["seat"], t["planted"], grown=t["grown"])
        assert any(b.startswith("match rows lost") for b in bad), (label, bad)
        assert len(t["m3"]["matches"]) == 0


def test_fb6_mutant_m6_do_update_overrides_the_tenant_grown_has_port(tenant_box):
    """Re-plant M6 (275's tenant arm as ON CONFLICT DO UPDATE): the tenant-GROWN has_port row on
    tenant A is overwritten by the seed — authority order violated (tenant > seed); B unchanged."""
    s = tenant_box
    assert s["m6_boot"].errored == []
    a, b = s["tenants"]["A"], s["tenants"]["B"]
    bad_a = _tenant_pins(a["m6"], a["seat"], a["planted"], grown=True)
    assert any(x.startswith("grown has_port overwritten") for x in bad_a), bad_a
    assert a["m6"]["has_port"] == [("builtin", 0.7, "network", ["SCALAR"])]
    assert _tenant_pins(b["m6"], b["seat"], b["planted"], grown=False) == []
